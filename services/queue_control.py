"""Global pause switch for automated platform traffic.

When a platform rate-limits or requests verification, workers call `pause()`;
nothing resumes until the user presses Resume.
The state is stored in SQLite so a paused queue stays paused after a restart.
"""

from datetime import datetime, timedelta, timezone

from database import repository

_KEY = "youtube_pause"


def pause(kind: str, message: str, cooldown_minutes: float | None = None):
    if cooldown_minutes is None:
        cooldown_minutes = float(repository.get_settings()["rate_limit_cooldown_minutes"])
    now = datetime.now(timezone.utc)
    repository.set_state(_KEY, {
        "kind": kind,
        "message": message,
        "paused_at": now.isoformat(timespec="seconds"),
        "resume_after": (now + timedelta(minutes=cooldown_minutes)).isoformat(timespec="seconds"),
    })


def resume():
    repository.set_state(_KEY, None)


def status() -> dict:
    """Return {'paused': bool, ...details, 'cooldown_remaining': seconds}."""
    state = repository.get_state(_KEY)
    if not state:
        return {"paused": False}
    remaining = 0
    try:
        resume_after = datetime.fromisoformat(state["resume_after"])
        remaining = max(0, int((resume_after - datetime.now(timezone.utc)).total_seconds()))
    except (KeyError, ValueError):
        pass
    return {"paused": True, "cooldown_remaining": remaining, **state}


def is_paused() -> bool:
    return repository.get_state(_KEY) is not None
