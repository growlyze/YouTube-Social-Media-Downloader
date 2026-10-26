"""JSON API for reading and validating application settings."""

import os
import re

from flask import Blueprint, jsonify, request

import config
from database import repository
from services import drive_auth
from services import folder_browser, folder_picker
from services.folder_picker import validate_destination

bp = Blueprint("settings", __name__)

DOCKER_FOLDER_MESSAGE = ("In Docker the download folder is set with HOST_DOWNLOADS_DIR in .env; "
                         "restart the app after changing it.")

_NUMBERS = {
    # key: (type, min, max)
    "sleep_requests": (float, 0, 60),
    "sleep_interval_min": (float, 0, 600),
    "sleep_interval_max": (float, 0, 1800),
    "max_retries": (int, 0, 10),
    "max_concurrent_downloads": (int, 1, 3),
    "rate_limit_cooldown_minutes": (int, 5, 1440),
    "discovery_limit": (int, 0, 100000),
    "space_min_free_percent": (float, 0.5, 50),
    "space_recheck_minutes": (int, 5, 1440),
}
_CHOICES = {
    "video_quality": config.VIDEO_QUALITIES,
    "video_container": config.VIDEO_CONTAINERS,
    "js_runtime": config.JS_RUNTIMES,
}
_BOOLS = ("auto_upload", "include_shorts", "include_streams", "space_check_enabled")


def validate_settings(body: dict, current: dict | None = None) -> tuple[dict, list[str]]:
    """Return (valid values to save, error messages). Unknown keys are ignored."""
    current = current or config.DEFAULT_SETTINGS
    clean, errors = {}, []
    for key, (kind, low, high) in _NUMBERS.items():
        if key in body:
            try:
                value = kind(body[key])
                if not low <= value <= high:
                    raise ValueError
                clean[key] = value
            except (TypeError, ValueError):
                errors.append(f"{key} must be a number between {low} and {high}.")
    for key, choices in _CHOICES.items():
        if key in body:
            if body[key] in choices:
                clean[key] = body[key]
            else:
                errors.append(f"{key} must be one of: {', '.join(choices)}.")
    for key in _BOOLS:
        if key in body:
            clean[key] = bool(body[key])
    if "google_api_key" in body:
        value = str(body["google_api_key"] or "").strip()
        if value and not re.fullmatch(r"[A-Za-z0-9_\-]{20,100}", value):
            errors.append("The Google API key looks wrong (it usually starts with 'AIza').")
        else:
            clean["google_api_key"] = value
    if "google_project_number" in body:
        value = str(body["google_project_number"] or "").strip()
        if value and not re.fullmatch(r"\d{6,20}", value):
            errors.append("The Google Cloud project number is digits only (not the project ID).")
        else:
            clean["google_project_number"] = value
    if "download_dir" in body and not config.IN_DOCKER:   # fixed volume in Docker
        path, error = validate_destination(body["download_dir"])
        if error:
            errors.append(error)
        else:
            clean["download_dir"] = path
    low = clean.get("sleep_interval_min", current["sleep_interval_min"])
    high = clean.get("sleep_interval_max", current["sleep_interval_max"])
    if high < low:
        errors.append("Maximum delay must be greater than or equal to the minimum delay.")
    return clean, errors


@bp.get("/settings")
def get_settings():
    return jsonify(settings=repository.get_settings(),
                   choices={k: v for k, v in _CHOICES.items()},
                   data_dir=str(config.DATA_DIR))


@bp.put("/settings")
def put_settings():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="Expected a JSON object."), 400
    current = repository.get_settings()
    clean, errors = validate_settings(body, current)
    if body.get("auto_upload") and not current.get("auto_upload") and not errors:
        if not current.get("drive_folder_id"):
            errors.append("Choose a Google Drive folder before enabling automatic uploads.")
        if not drive_auth.is_connected():
            errors.append("Connect Google Drive before enabling automatic uploads.")
    if errors:
        return jsonify(error=" ".join(errors), errors=errors), 400
    repository.update_settings(clean)
    return jsonify(settings=repository.get_settings())


@bp.post("/folders/list")
def folders_list():
    """List drives plus the sub-folders of `path` (default: current download
    folder). POST + CSRF so other websites can neither call nor read it."""
    if config.IN_DOCKER:
        return jsonify(error=DOCKER_FOLDER_MESSAGE), 501
    body = request.get_json(silent=True) or {}
    current = repository.get_settings()["download_dir"]
    path = body.get("path") or (current if os.path.isdir(current) else None)
    result = {"drives": folder_browser.list_drives(), "current": current}
    if path:
        try:
            result.update(folder_browser.list_folder(path))
        except folder_browser.BrowseError as exc:
            return jsonify(error=str(exc), **result), 400
    else:
        result.update(path=None, parent=None, folders=[], truncated=False)
    return jsonify(result)


@bp.post("/folders/create")
def folders_create():
    if config.IN_DOCKER:
        return jsonify(error=DOCKER_FOLDER_MESSAGE), 501
    body = request.get_json(silent=True) or {}
    try:
        path = folder_browser.create_folder(body.get("parent"), body.get("name"))
    except folder_browser.BrowseError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(path=path)


@bp.post("/settings/pick-folder")
def pick_folder():
    """Open the native folder dialog on this PC. Returns the chosen path but
    does not save it; the page asks the user to confirm, then PUTs settings."""
    if config.IN_DOCKER:
        return jsonify(error=DOCKER_FOLDER_MESSAGE), 501
    current = repository.get_settings()["download_dir"]
    try:
        path = folder_picker.pick_folder(current)
    except folder_picker.PickerBusy as exc:
        return jsonify(error=str(exc)), 409
    except folder_picker.PickerUnavailable as exc:
        return jsonify(error=str(exc)), 503
    return jsonify(path=path, cancelled=path is None, current=current)
