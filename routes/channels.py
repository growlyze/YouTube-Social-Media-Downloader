"""JSON API for discovery, sources and the video library."""

import re

from flask import Blueprint, current_app, jsonify, request

from database import repository
from database.repository import VideoFilter
from services import queue_control
from services.youtube_service import InputError, parse_source_input

bp = Blueprint("channels", __name__)


def _worker():
    return current_app.extensions["discovery_worker"]


def _int_arg(value, name, minimum=None, maximum=None):
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InputError(f"'{name}' must be a whole number.")
    if minimum is not None and number < minimum or maximum is not None and number > maximum:
        raise InputError(f"'{name}' is out of range.")
    return number


def _filter_from(params) -> VideoFilter:
    for key in ("date_from", "date_to"):
        if params.get(key) and not re.match(r"^\d{4}-\d{2}-\d{2}$", params.get(key)):
            raise InputError(f"'{key}' must be a date (YYYY-MM-DD).")
    video_type = params.get("type") or ""
    if video_type not in ("", "video", "short", "live"):
        raise InputError("Unknown video type filter.")
    availability = params.get("availability") or ""
    if availability not in ("", "available", "unavailable"):
        raise InputError("Unknown availability filter.")
    download = params.get("download") or ""
    if download not in ("", "not_downloaded", "downloaded", "queued", "failed"):
        raise InputError("Unknown download status filter.")
    min_minutes = _int_arg(params.get("min_minutes"), "min_minutes", 0, 100000)
    max_minutes = _int_arg(params.get("max_minutes"), "max_minutes", 0, 100000)
    return VideoFilter(
        source_id=params.get("source") or None,
        search=(params.get("q") or "").strip()[:200],
        video_type=video_type,
        date_from=params.get("date_from") or "",
        date_to=params.get("date_to") or "",
        min_duration=min_minutes * 60 if min_minutes is not None else None,
        # inclusive upper bound: "max 5 minutes" includes 5:59
        max_duration=max_minutes * 60 + 59 if max_minutes is not None else None,
        availability=availability,
        selected_only=str(params.get("selected_only", "")).lower() in ("1", "true", "on"),
        download=download,
    )


@bp.errorhandler(InputError)
def input_error(exc):
    return jsonify(error=str(exc)), 400


# -- discovery ---------------------------------------------------------------

@bp.post("/discover")
def discover():
    body = request.get_json(silent=True) or {}
    text = str(body.get("url") or "").strip()
    settings = repository.get_settings()
    parse_source_input(text, settings["include_shorts"], settings["include_streams"])  # validate now
    pause = queue_control.status()
    job_id = _worker().submit(text)
    return jsonify(job=repository.get_discovery_job(job_id), pause=pause), 202


@bp.get("/discover/<int:job_id>")
def discovery_status(job_id):
    job = repository.get_discovery_job(job_id)
    if not job:
        return jsonify(error="Job not found"), 404
    return jsonify(job=job, pause=queue_control.status())


@bp.post("/discover/<int:job_id>/cancel")
def discovery_cancel(job_id):
    if not _worker().cancel(job_id):
        return jsonify(error="Job is not running or pending"), 409
    return jsonify(ok=True)


@bp.get("/discover")
def discovery_jobs():
    return jsonify(jobs=repository.list_discovery_jobs(20), pause=queue_control.status())


# -- queue pause state (shared with the download queue in Phase 3) -----------

@bp.get("/queue/pause")
def pause_status():
    return jsonify(queue_control.status())


@bp.post("/queue/resume")
def resume_queue():
    queue_control.resume()
    _worker().wake()
    download_worker = current_app.extensions.get("download_worker")
    if download_worker:
        download_worker.wake()
    return jsonify(queue_control.status())


# -- sources -----------------------------------------------------------------

@bp.get("/sources")
def sources():
    return jsonify(sources=repository.list_sources())


@bp.delete("/sources/<source_id>")
def delete_source(source_id):
    if not repository.get_source(source_id):
        return jsonify(error="Source not found"), 404
    repository.delete_source(source_id)
    return jsonify(ok=True)


# -- videos ------------------------------------------------------------------

@bp.get("/videos")
def videos():
    flt = _filter_from(request.args)
    page = _int_arg(request.args.get("page"), "page", 1) or 1
    page_size = _int_arg(request.args.get("page_size"), "page_size", 1, 500) or 50
    sort = request.args.get("sort") or "newest"
    return jsonify(repository.query_videos(flt, sort=sort, page=page, page_size=page_size))


@bp.post("/videos/select")
def select_videos():
    """Body: {"ids": [...], "selected": bool}  or
             {"all_matching": true, "filters": {...}, "selected": bool}"""
    body = request.get_json(silent=True) or {}
    selected = bool(body.get("selected", True))
    if body.get("all_matching"):
        flt = _filter_from(body.get("filters") or {})
        changed = repository.set_selected_by_filter(flt, selected)
    else:
        ids = body.get("ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise InputError("'ids' must be a list of video ids.")
        changed = repository.set_selected(ids, selected)
    return jsonify(changed=changed)


@bp.get("/stats")
def stats():
    return jsonify(videos=repository.count_videos(), sources=len(repository.list_sources()),
                   pause=queue_control.status())
