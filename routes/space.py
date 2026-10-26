"""JSON API for the Space Check page (local disk + Google Drive free space)."""

from flask import Blueprint, current_app, jsonify

from services import space_service

bp = Blueprint("space", __name__)


def _payload() -> dict:
    data = space_service.status_payload()
    data["downloads"] = current_app.extensions["download_worker"].snapshot()
    data["uploads"] = current_app.extensions["upload_worker"].snapshot()
    return data


@bp.get("/space/status")
def status():
    """Stored snapshots plus a fresh (cheap) local check so the page is live."""
    space_service.refresh_local()
    return jsonify(_payload())


@bp.post("/space/check")
def check():
    """Force check now: re-check local disk and Google Drive and continue the
    queues if space is available — no waiting for the hourly re-check."""
    worker = current_app.extensions["space_worker"]
    result = worker.force_check()
    data = _payload()
    data["checked"] = True
    data["events"] = result.get("events", data.get("events", []))
    return jsonify(data)
