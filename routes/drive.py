"""Google Drive connection, folder choice and the manual upload queue."""

import re
from urllib.parse import quote

from flask import Blueprint, current_app, jsonify, redirect, request

import config
from database import repository
from services import drive_auth, drive_service
from services.drive_service import DriveError

bp = Blueprint("drive", __name__)          # JSON API, mounted at /api
oauth_bp = Blueprint("oauth", __name__)    # browser redirect target, mounted at /


def _uploads():
    return current_app.extensions["upload_worker"]


def picker_values(settings=None) -> tuple[str, str]:
    """API key and project number: .env first, then (legacy) settings."""
    settings = settings or repository.get_settings()
    return (config.google_secret("GOOGLE_API_KEY") or settings["google_api_key"],
            config.google_secret("GOOGLE_PROJECT_NUMBER") or settings["google_project_number"])


def _drive_error(exc):
    err = drive_service.classify_http_error(exc)
    status = {"auth": 401, "not_found": 404, "forbidden": 403, "quota": 507, "rate_limit": 429}.get(err.kind, 502)
    return jsonify(error=str(err), kind=err.kind), status


# -- connection --------------------------------------------------------------

@bp.get("/drive/status")
def status():
    settings = repository.get_settings()
    api_key, project_number = picker_values(settings)
    result = {
        "client_file": str(config.CLIENT_SECRET_PATH),
        "client_source": drive_auth.client_source(),
        "configured": False, "config_error": None,
        "connected": drive_auth.is_connected(),
        "account": None, "account_error": None,
        "picker_ready": bool(api_key and project_number),
        "picker_from_env": bool(config.google_secret("GOOGLE_API_KEY")
                                and config.google_secret("GOOGLE_PROJECT_NUMBER")),
        "folder": {"id": settings["drive_folder_id"], "name": settings["drive_folder_name"]}
                  if settings["drive_folder_id"] else None,
        "redirect_uri": drive_auth.redirect_uri(),
    }
    try:
        drive_auth.load_client_config()
        result["configured"] = True
    except drive_auth.AuthError as exc:
        result["config_error"] = str(exc)
    if result["configured"] and result["connected"] and request.args.get("account") == "1":
        try:
            result["account"] = drive_service.account_info()
        except Exception as exc:
            result["account_error"] = str(drive_service.classify_http_error(exc))
    return jsonify(result)


@bp.post("/drive/connect")
def connect():
    try:
        return jsonify(auth_url=drive_auth.start_authorization())
    except drive_auth.AuthError as exc:
        return jsonify(error=str(exc)), 400


@bp.post("/drive/disconnect")
def disconnect():
    drive_auth.disconnect()
    return jsonify(ok=True)


@oauth_bp.get("/oauth2/callback")
def oauth_callback():
    """Google redirects here after consent. Protected by the one-time `state`
    value created in /api/drive/connect (acts as the CSRF check)."""
    if request.args.get("error"):
        reason = request.args.get("error")
        return redirect("/settings?drive_error=" + quote(f"Google sign-in was cancelled or failed ({reason})."))
    try:
        drive_auth.finish_authorization(request.args.get("state", ""), request.args.get("code", ""))
    except Exception as exc:
        return redirect("/settings?drive_error=" + quote(str(exc)[:300]))
    _uploads().wake()
    return redirect("/settings?drive=connected#drive")


# -- folder ------------------------------------------------------------------

@bp.post("/drive/picker-config")
def picker_config():
    """Everything the Google Picker needs in the page. POST + CSRF, so only
    this app's own pages can obtain the short-lived access token."""
    api_key, project_number = picker_values()
    if not (api_key and project_number):
        return jsonify(error="Set GOOGLE_API_KEY and GOOGLE_PROJECT_NUMBER in .env first."), 400
    try:
        token = drive_auth.access_token()
    except Exception as exc:
        return _drive_error(exc)
    return jsonify(token=token, api_key=api_key, app_id=project_number)


@bp.put("/drive/folder")
def set_folder():
    """Save the Drive folder after confirming the app can add files to it."""
    body = request.get_json(silent=True) or {}
    folder_id = str(body.get("id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,200}", folder_id):
        return jsonify(error="Invalid Drive folder id."), 400
    try:
        folder = drive_service.get_folder(folder_id)
    except Exception as exc:
        return _drive_error(exc)
    repository.update_settings({"drive_folder_id": folder["id"], "drive_folder_name": folder["name"]})
    return jsonify(folder=folder)


@bp.post("/drive/folder/create")
def create_folder():
    body = request.get_json(silent=True) or {}
    try:
        folder = drive_service.create_folder(str(body.get("name") or ""))
    except Exception as exc:
        return _drive_error(exc)
    repository.update_settings({"drive_folder_id": folder["id"], "drive_folder_name": folder["name"]})
    return jsonify(folder=folder)


# -- uploads -----------------------------------------------------------------

@bp.post("/uploads/enqueue")
def enqueue():
    body = request.get_json(silent=True) or {}
    ids = body.get("download_ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, int) for i in ids):
        return jsonify(error="'download_ids' must be a non-empty list of numbers."), 400
    settings = repository.get_settings()
    if not settings["drive_folder_id"]:
        return jsonify(error="Choose a Google Drive folder first (Settings → Google Drive)."), 400
    if not drive_auth.is_connected():
        return jsonify(error="Connect Google Drive first (Settings → Google Drive)."), 400
    result = repository.enqueue_uploads(ids, settings["drive_folder_id"], settings["drive_folder_name"])
    _uploads().wake()
    result["queue_running"] = _uploads().is_running()
    return jsonify(result)


@bp.get("/uploads/status")
def upload_status():
    return jsonify(queue=_uploads().snapshot(), counts=repository.upload_counts(),
                   next_retry_at=repository.next_upload_retry_time())


@bp.get("/uploads")
def list_uploads():
    view = request.args.get("view", "queue")
    if view not in repository.UPLOAD_VIEWS:
        return jsonify(error="Unknown view."), 400
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        return jsonify(error="page must be a number."), 400
    return jsonify(repository.list_uploads(view, page, 100))


@bp.post("/uploads/queue/start")
def start_uploads():
    _uploads().start_queue()
    return jsonify(queue=_uploads().snapshot())


@bp.post("/uploads/queue/pause")
def pause_uploads():
    _uploads().pause_queue()
    return jsonify(queue=_uploads().snapshot())


@bp.post("/uploads/<int:upload_id>/cancel")
def cancel_upload(upload_id):
    if not _uploads().cancel(upload_id):
        return jsonify(error="Only pending or uploading items can be cancelled."), 409
    return jsonify(ok=True)


@bp.post("/uploads/<int:upload_id>/retry")
def retry_upload(upload_id):
    if not repository.retry_uploads([upload_id]):
        return jsonify(error="Only failed or cancelled uploads can be retried."), 409
    _uploads().wake()
    return jsonify(ok=True)


@bp.post("/uploads/retry-failed")
def retry_failed_uploads():
    count = repository.retry_uploads()
    _uploads().wake()
    return jsonify(requeued=count)


@bp.delete("/uploads/<int:upload_id>")
def remove_upload(upload_id):
    if not repository.delete_upload(upload_id):
        return jsonify(error="Not found, or currently uploading (cancel it first)."), 409
    return jsonify(ok=True)
