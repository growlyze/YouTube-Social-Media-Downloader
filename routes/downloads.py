"""JSON API for the download queue, history and local files."""

import os
import subprocess
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from database import repository
from routes.channels import _filter_from
from services import queue_control
from services import runtime as runtime_tools
from services.download_service import is_inside
from services.youtube_service import InputError

bp = Blueprint("downloads", __name__)


def _worker():
    return current_app.extensions["download_worker"]


@bp.errorhandler(InputError)
def input_error(exc):
    return jsonify(error=str(exc)), 400


def _page_args():
    try:
        page = max(1, int(request.args.get("page", 1)))
        size = max(1, min(500, int(request.args.get("page_size", 100))))
    except ValueError:
        raise InputError("page and page_size must be whole numbers.")
    return page, size


@bp.get("/downloads/status")
def status():
    settings = repository.get_settings()
    return jsonify(
        queue=_worker().snapshot(),
        counts=repository.download_counts(),
        active=repository.list_downloads("queue", page_size=5)["items"],
        pause=queue_control.status(),
        runtime=runtime_tools.status(settings.get("js_runtime", "node")),
        next_retry_at=repository.next_retry_time(),
    )


@bp.get("/downloads")
def list_downloads():
    view = request.args.get("view", "queue")
    if view not in repository.DOWNLOAD_VIEWS:
        raise InputError("Unknown view.")
    page, size = _page_args()
    return jsonify(repository.list_downloads(view, page, size,
                                             (request.args.get("q") or "").strip()[:200]))


@bp.post("/downloads/enqueue")
def enqueue():
    """Body: {"video_ids": [...]} or {"from_selection": true, "filters": {...}};
    optional "redownload": true."""
    body = request.get_json(silent=True) or {}
    settings = repository.get_settings()
    if body.get("from_selection"):
        flt = _filter_from(body.get("filters") or {})
        flt.selected_only = True
        ids = repository.matching_video_ids(flt)
    else:
        ids = body.get("video_ids")
        if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
            raise InputError("'video_ids' must be a list of video ids.")
    if not ids:
        raise InputError("No videos selected.")
    result = repository.enqueue_downloads(
        ids, redownload=bool(body.get("redownload")),
        include_shorts=settings.get("include_shorts", True),
        include_live=settings.get("include_streams", False),
        download_dir=settings["download_dir"])
    if body.get("from_selection") and result["queued_ids"]:
        repository.set_selected(result["queued_ids"], False)
    _worker().wake()
    result["queue_running"] = _worker().is_running()
    result.pop("queued_ids")
    return jsonify(result)


@bp.post("/downloads/queue/start")
def start_queue():
    _worker().start_queue()
    return jsonify(queue=_worker().snapshot(), pause=queue_control.status())


@bp.post("/downloads/queue/pause")
def pause_queue():
    _worker().pause_queue()
    return jsonify(queue=_worker().snapshot())


@bp.post("/downloads/<int:download_id>/cancel")
def cancel(download_id):
    if not _worker().cancel(download_id):
        return jsonify(error="Only pending or downloading items can be cancelled."), 409
    return jsonify(ok=True)


@bp.post("/downloads/<int:download_id>/retry")
def retry(download_id):
    if not repository.retry_downloads([download_id]):
        return jsonify(error="Only failed or cancelled items can be retried."), 409
    _worker().wake()
    return jsonify(ok=True)


@bp.post("/downloads/retry-failed")
def retry_failed():
    count = repository.retry_downloads()
    _worker().wake()
    return jsonify(requeued=count)


@bp.delete("/downloads/<int:download_id>")
def remove(download_id):
    if not repository.delete_download(download_id):
        return jsonify(error="Item not found or currently downloading (cancel it first)."), 409
    return jsonify(ok=True)


@bp.post("/downloads/clear")
def clear():
    body = request.get_json(silent=True) or {}
    statuses = body.get("statuses")
    if not isinstance(statuses, list):
        raise InputError("'statuses' must be a list.")
    return jsonify(removed=repository.clear_downloads(statuses))


# -- local files -------------------------------------------------------------

@bp.get("/local")
def local_files():
    page, size = _page_args()
    data = repository.list_downloads("completed", page, size, (request.args.get("q") or "").strip()[:200])
    folder_id = repository.get_settings()["drive_folder_id"] or None
    uploads = repository.upload_status_by_download([i["id"] for i in data["items"]], folder_id)
    for item in data["items"]:
        item["file_exists"] = bool(item["file_path"]) and os.path.isfile(item["file_path"])
        upload = uploads.get(item["id"])
        item["upload"] = ({k: upload[k] for k in ("id", "status", "progress", "web_link", "error_message")}
                          if upload else None)
    return jsonify(data)


@bp.post("/local/open")
def open_folder():
    """Open the download folder, or reveal one downloaded file, in Explorer."""
    body = request.get_json(silent=True) or {}
    download_dir = Path(repository.get_settings()["download_dir"])
    download_dir.mkdir(parents=True, exist_ok=True)
    target, select = download_dir, False
    if body.get("download_id") is not None:
        try:
            item = repository.get_download(int(body["download_id"]))
        except (TypeError, ValueError):
            raise InputError("Invalid download id.")
        if not item or not item.get("file_path"):
            return jsonify(error="No file recorded for this item."), 404
        path = Path(item["file_path"])
        # Only reveal files inside the current download folder or the folder this
        # item was queued to; the path always comes from the database, never the request.
        roots = [download_dir] + ([Path(item["download_dir"])] if item.get("download_dir") else [])
        if not any(is_inside(path, root) for root in roots):
            return jsonify(error="That file is outside the download folder."), 403
        if path.is_file():
            target, select = path, True
        elif path.parent.is_dir():
            target = path.parent
        else:
            return jsonify(error="The file no longer exists."), 404
    if os.name != "nt":
        return jsonify(error="Opening folders is only supported on Windows.", path=str(target)), 501
    if select:
        subprocess.Popen(["explorer", f"/select,{target}"])
    else:
        os.startfile(str(target))  # noqa: S606 - local, validated path
    return jsonify(ok=True, path=str(target))
