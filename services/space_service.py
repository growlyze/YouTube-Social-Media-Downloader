"""Automatic free-space checks for local downloads and Google Drive uploads.

* Local: the download worker stops claiming jobs while the download drive has
  less than `space_min_free_percent` free space, and continues by itself when a
  check finds enough space again.
* Google Drive: the upload worker stops while Drive is full (storage quota) and
  continues by itself once a check reports free space again.
* A background re-check runs every `space_recheck_minutes` (default 60), and the
  Space Check page offers an immediate "Recheck space and continue" button that
  does not wait for the timer.

The pause state lives in SQLite (`app_state`) so it survives restarts. This is
separate from rate-limit pauses (`services/queue_control`), which still require
the user to press Resume.
"""

import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import repository
from services import drive_auth, drive_service

log = logging.getLogger("space")

STATE_KEY = "space_state"
MAX_EVENTS = 12
_LOCAL_CACHE_SECONDS = 20.0
_local_cache = {"at": 0.0, "blocked": False}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _threshold(settings: dict | None = None) -> float:
    settings = settings or repository.get_settings()
    try:
        return float(settings.get("space_min_free_percent", 5.0))
    except (TypeError, ValueError):
        return 5.0


def _no_pause() -> dict:
    return {"paused": False, "message": None, "since": None}


def get_state() -> dict:
    state = repository.get_state(STATE_KEY)
    if not isinstance(state, dict):
        state = {}
    state.setdefault("local", _no_pause())
    state.setdefault("drive", _no_pause())
    state.setdefault("events", [])
    return state


def _save(state: dict):
    repository.set_state(STATE_KEY, state)


def _event(state: dict, text: str):
    """Record what happened (shown on the Space Check page)."""
    state["events"] = (state.get("events") or [])[-(MAX_EVENTS - 1):] + \
        [{"at": _now(), "text": text}]
    log.info(text)


def _existing_dir(path: Path) -> Path:
    """The folder (or nearest parent) whose disk we can measure."""
    path = Path(path)
    for candidate in (path, *path.parents):
        if candidate.is_dir():
            return candidate
    return Path(repository.get_settings()["download_dir"])


# -- snapshots ---------------------------------------------------------------

def local_status() -> dict:
    """Free space on the drive that holds the download folder."""
    settings = repository.get_settings()
    threshold = _threshold(settings)
    path = _existing_dir(Path(settings["download_dir"]))
    try:
        usage = shutil.disk_usage(str(path))
    except OSError as exc:
        return {"ok": True, "enabled": bool(settings.get("space_check_enabled", True)),
                "path": str(path), "threshold": threshold,
                "message": f"Could not read free space: {exc}"}
    free_percent = round(usage.free / usage.total * 100, 1) if usage.total else 100.0
    return {
        "ok": free_percent >= threshold,
        "enabled": bool(settings.get("space_check_enabled", True)),
        "path": str(path),
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "free_percent": free_percent,
        "threshold": threshold,
    }


def drive_status() -> dict:
    """Free space in Google Drive (needs a connected account)."""
    settings = repository.get_settings()
    threshold = _threshold(settings)
    enabled = bool(settings.get("space_check_enabled", True))
    if not drive_auth.is_connected():
        return {"ok": True, "enabled": enabled, "connected": False, "threshold": threshold,
                "message": "Google Drive is not connected; uploads are not affected."}
    try:
        info = drive_service.account_info()
    except Exception as exc:  # DriveError or network problem: never block on a check
        return {"ok": True, "enabled": enabled, "connected": True, "threshold": threshold,
                "message": f"Could not read Drive storage just now: {exc}"}
    limit = info.get("quota_limit")
    used = int(info.get("quota_usage") or 0)
    if not limit:
        return {"ok": True, "enabled": enabled, "connected": True, "threshold": threshold,
                "email": info.get("email"), "used": used,
                "message": "Google Drive reports no storage limit; uploads are not blocked."}
    free = max(0, int(limit) - used)
    free_percent = round(free / int(limit) * 100, 1)
    return {
        "ok": free_percent >= threshold,
        "enabled": enabled,
        "connected": True,
        "threshold": threshold,
        "email": info.get("email"),
        "total": int(limit),
        "used": used,
        "free": free,
        "free_percent": free_percent,
    }


# -- pause state -------------------------------------------------------------

def downloads_blocked() -> bool:
    """True while downloads must not start (disk full or a disk error pause)."""
    if get_state()["local"].get("paused"):
        return True
    return local_blocked()


def uploads_blocked() -> bool:
    """True while uploads must not start (Google Drive is full)."""
    return bool(get_state()["drive"].get("paused"))


def local_blocked() -> bool:
    """Quick live check (cached briefly) used by the download worker."""
    settings = repository.get_settings()
    if not settings.get("space_check_enabled", True):
        return False
    now = time.monotonic()
    if now - _local_cache["at"] < _LOCAL_CACHE_SECONDS:
        return _local_cache["blocked"]
    status = local_status()
    blocked = not status.get("ok", True)
    _local_cache.update(at=now, blocked=blocked)
    return blocked


def pause_for_disk_error(message: str, detail: str | None = None):
    """A yt-dlp 'disk' error: stop downloads and wait for free space."""
    text = (f"Downloads are waiting for free disk space. {message} "
            "They continue automatically when a space check finds enough free space.")
    if detail and detail not in text:
        text += f" Details: {detail}"
    state = get_state()
    if not state["local"].get("paused"):
        state["local"] = {"paused": True, "message": text, "since": _now()}
        _event(state, "Downloads stopped — " + text)
        _save(state)


def pause_for_quota(message: str):
    """Google Drive is full: stop uploads and wait for free space."""
    text = (f"{message} Uploads continue automatically when a space check finds "
            "free space in Google Drive.")
    state = get_state()
    if not state["drive"].get("paused"):
        state["drive"] = {"paused": True, "message": text, "since": _now()}
        _event(state, "Uploads stopped — " + text)
        _save(state)


# -- checks ------------------------------------------------------------------

def _apply_local(state: dict, status: dict) -> bool:
    changed = False
    if not status.get("enabled", True):
        if state["local"].get("paused"):
            state["local"] = _no_pause()
            _event(state, "Space checking is turned off in Settings; the space pause was cleared.")
            changed = True
    elif not status.get("ok", True):
        message = (f"Local disk space is low: {status.get('free_percent')}% free is below the "
                   f"{status.get('threshold')}% minimum. Downloads are stopped; they continue "
                   "automatically when space is free again.")
        if not state["local"].get("paused"):
            state["local"] = {"paused": True, "message": message, "since": _now()}
            _event(state, "Downloads stopped — " + message)
            changed = True
        else:
            state["local"]["message"] = message
    elif state["local"].get("paused"):
        state["local"] = _no_pause()
        _event(state, "Free space is available again; downloads continued automatically.")
        changed = True
    return changed


def _apply_drive(state: dict, status: dict) -> bool:
    changed = False
    if not status.get("enabled", True) or not status.get("connected", False):
        if state["drive"].get("paused"):
            state["drive"] = _no_pause()
            _event(state, "The Google Drive space pause was cleared.")
            changed = True
    elif not status.get("ok", True):
        message = (f"Google Drive space is low: {status.get('free_percent')}% free is below the "
                   f"{status.get('threshold')}% minimum. Uploads are stopped; they continue "
                   "automatically when space is free again.")
        if not state["drive"].get("paused"):
            state["drive"] = {"paused": True, "message": message, "since": _now()}
            _event(state, "Uploads stopped — " + message)
            changed = True
        else:
            state["drive"]["message"] = message
    elif state["drive"].get("paused"):
        state["drive"] = _no_pause()
        _event(state, "Google Drive space is available again; uploads continued automatically.")
        changed = True
    return changed


def refresh_local() -> dict:
    """Cheap local check now; updates the snapshot and the pause state."""
    status = local_status()
    state = get_state()
    changed = _apply_local(state, status)
    state["local_snapshot"] = status
    _save(state)
    return {"changed": changed, "status": status}


def check_due() -> bool:
    state = get_state()
    due = state.get("next_check_at")
    if not due:
        return True
    try:
        return datetime.now(timezone.utc) >= datetime.fromisoformat(due)
    except ValueError:
        return True


def run_checks() -> dict:
    """Full check of local disk and Google Drive; stops/continues the queues.

    Called by the background re-check (every `space_recheck_minutes`) and by the
    Space Check page's "Recheck space and continue" button.
    """
    settings = repository.get_settings()
    changed = refresh_local()["changed"]

    drive = drive_status()
    state = get_state()
    changed = _apply_drive(state, drive) or changed
    state["drive_snapshot"] = drive

    now = datetime.now(timezone.utc)
    try:
        minutes = float(settings.get("space_recheck_minutes", 60))
    except (TypeError, ValueError):
        minutes = 60.0
    state["checked_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
    state["next_check_at"] = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    _save(state)
    return {"changed": changed, "local": state.get("local_snapshot"), "drive": drive,
            "events": state.get("events", [])}


def status_payload() -> dict:
    """Everything the Space Check page and the pause banner need."""
    settings = repository.get_settings()
    state = get_state()
    local = state.get("local_snapshot") or local_status()
    drive = state.get("drive_snapshot") or drive_status()
    return {
        "enabled": bool(settings.get("space_check_enabled", True)),
        "threshold": _threshold(settings),
        "recheck_minutes": float(settings.get("space_recheck_minutes", 60)),
        "local": local,
        "drive": drive,
        "local_pause": state["local"],
        "drive_pause": state["drive"],
        "checked_at": state.get("checked_at"),
        "next_check_at": state.get("next_check_at"),
        "events": state.get("events", []),
    }
