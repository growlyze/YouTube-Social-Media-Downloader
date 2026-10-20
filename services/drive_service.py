"""Google Drive API v3 operations used by the app (drive.file scope).

Uploads use Drive's resumable protocol in 8 MB chunks: a dropped connection
only re-sends the current chunk (googleapiclient retries it), and progress is
reported per chunk. Each uploaded file is tagged with appProperties
{ytVideoId, ytDownloadId} so a second upload of the same video to the same
folder is detected and skipped instead of creating a duplicate.
"""

import json
import logging
import mimetypes
import time
from pathlib import Path

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError, ResumableUploadError
from googleapiclient.http import MediaFileUpload

from services import drive_auth

log = logging.getLogger("drive")

FOLDER_MIME = "application/vnd.google-apps.folder"
CHUNK_SIZE = 8 * 1024 * 1024


class DriveError(Exception):
    """kind: auth | quota | rate_limit | not_found | forbidden | network | local_file | server | unknown"""

    def __init__(self, kind, message, retryable=False, pause=False):
        super().__init__(message)
        self.kind, self.retryable, self.pause = kind, retryable, pause


class UploadCancelled(Exception):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason   # 'cancel' | 'pause'


def _service(credentials=None):
    creds = credentials or drive_auth.get_credentials()
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _error_reasons(exc: HttpError) -> list[str]:
    """Machine-readable reasons from a Google API error body."""
    try:
        data = json.loads(exc.content.decode("utf-8") if isinstance(exc.content, bytes) else exc.content)
        error = data.get("error", {}) if isinstance(data, dict) else {}
        items = (error.get("errors") or []) + (error.get("details") or [])
        return [str(i.get("reason")) for i in items if isinstance(i, dict) and i.get("reason")]
    except Exception:
        return []


def classify_http_error(exc: Exception) -> DriveError:
    """Turn Google API errors into actionable kinds."""
    if isinstance(exc, DriveError):
        return exc
    if isinstance(exc, drive_auth.AuthError):
        return DriveError("auth", str(exc), pause=True)
    status, reasons, message = None, [], str(exc)
    if isinstance(exc, HttpError):
        status = exc.resp.status if exc.resp is not None else None
        reasons = _error_reasons(exc)
        message = getattr(exc, "reason", "") or message
    text = f"{' '.join(reasons)} {message}".lower()
    if status == 401 or "invalid_grant" in text or "invalid credentials" in text:
        return DriveError("auth", "Google rejected the sign-in. Connect Google Drive again.", pause=True)
    if "storagequotaexceeded" in text or "storage quota" in text:
        return DriveError("quota", "Your Google Drive storage is full. Free up space, then retry.", pause=True)
    if status == 429 or "ratelimitexceeded" in text or "rate limit exceeded" in text:
        return DriveError("rate_limit", "Google Drive is rate-limiting uploads; will retry later.", retryable=True)
    if status == 404:
        return DriveError("not_found", "The Drive folder was not found or this app was not given access to it. "
                                       "Choose the folder again with “Choose Drive folder”.")
    if status == 403:
        return DriveError("forbidden", f"Google Drive refused the request: {message}")
    if status is not None and status >= 500:
        return DriveError("server", "Google Drive had a temporary server error; will retry.", retryable=True)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not isinstance(exc, HttpError):
        return DriveError("network", f"Network problem while talking to Google Drive: {exc}", retryable=True)
    return DriveError("unknown", message[:300], retryable=True)


def account_info(credentials=None) -> dict:
    try:
        about = _service(credentials).about().get(fields="user(emailAddress,displayName),storageQuota").execute()
    except Exception as exc:
        raise classify_http_error(exc)
    quota = about.get("storageQuota") or {}
    return {"email": (about.get("user") or {}).get("emailAddress"),
            "name": (about.get("user") or {}).get("displayName"),
            "quota_limit": int(quota["limit"]) if quota.get("limit") else None,
            "quota_usage": int(quota.get("usage") or 0)}


def get_folder(folder_id: str, credentials=None) -> dict:
    """Confirm the app can add files to this folder. Raises DriveError otherwise."""
    if not folder_id or len(folder_id) > 200:
        raise DriveError("not_found", "Invalid Drive folder id.")
    try:
        meta = _service(credentials).files().get(
            fileId=folder_id, supportsAllDrives=True,
            fields="id,name,mimeType,trashed,webViewLink,capabilities(canAddChildren)").execute()
    except Exception as exc:
        raise classify_http_error(exc)
    if meta.get("mimeType") != FOLDER_MIME:
        raise DriveError("not_found", f"“{meta.get('name')}” is not a folder.")
    if meta.get("trashed"):
        raise DriveError("not_found", f"The folder “{meta.get('name')}” is in the trash.")
    if not (meta.get("capabilities") or {}).get("canAddChildren", False):
        raise DriveError("forbidden", f"You can't add files to “{meta.get('name')}”.")
    return {"id": meta["id"], "name": meta.get("name"), "link": meta.get("webViewLink")}


def create_folder(name: str, credentials=None) -> dict:
    name = (name or "").strip()
    if not name or len(name) > 200:
        raise DriveError("unknown", "Enter a folder name (up to 200 characters).")
    try:
        meta = _service(credentials).files().create(
            body={"name": name, "mimeType": FOLDER_MIME}, fields="id,name,webViewLink").execute()
    except Exception as exc:
        raise classify_http_error(exc)
    return {"id": meta["id"], "name": meta["name"], "link": meta.get("webViewLink")}


def find_uploaded(folder_id: str, video_id: str, service=None) -> dict | None:
    """A non-trashed file this app uploaded for `video_id` into `folder_id`."""
    safe_id = video_id.replace("\\", "").replace("'", "")
    query = (f"'{folder_id}' in parents and trashed = false and "
             f"appProperties has {{ key='ytVideoId' and value='{safe_id}' }}")
    try:
        result = (service or _service()).files().list(
            q=query, spaces="drive", fields="files(id,name,size,webViewLink)", pageSize=5,
            supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    except Exception as exc:
        raise classify_http_error(exc)
    files = result.get("files") or []
    return files[0] if files else None


def upload_file(path: str, folder_id: str, video_id: str, download_id: int,
                progress=lambda fields: None, should_stop=lambda: None, service=None) -> dict:
    """Upload one local file into `folder_id`. Returns {id, name, link, size, skipped}."""
    local = Path(path or "")
    if not local.is_file():
        raise DriveError("local_file", f"The downloaded file is missing on disk: {path}")
    size = local.stat().st_size
    if size == 0:
        raise DriveError("local_file", "The downloaded file is empty; re-download it first.")

    service = service or _service()
    existing = find_uploaded(folder_id, video_id, service)
    if existing:
        return {"id": existing["id"], "name": existing.get("name"), "link": existing.get("webViewLink"),
                "size": int(existing.get("size") or 0), "skipped": True}

    mime = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
    media = MediaFileUpload(str(local), mimetype=mime, chunksize=CHUNK_SIZE, resumable=True)
    body = {"name": local.name, "parents": [folder_id],
            "appProperties": {"ytVideoId": video_id, "ytDownloadId": str(download_id)}}
    request = service.files().create(body=body, media_body=media, supportsAllDrives=True,
                                     fields="id,name,size,webViewLink")
    response, last_bytes, last_time = None, 0, time.monotonic()
    progress({"total_bytes": size, "uploaded_bytes": 0, "progress": 0.0})
    try:
        while response is None:
            reason = should_stop()
            if reason:
                raise UploadCancelled(reason)
            status, response = request.next_chunk(num_retries=3)
            if status:
                now = time.monotonic()
                done = status.resumable_progress
                speed = (done - last_bytes) / (now - last_time) if now > last_time else None
                last_bytes, last_time = done, now
                progress({"uploaded_bytes": done, "total_bytes": size,
                          "progress": round(done * 100 / size, 1), "speed": speed})
    except (UploadCancelled, DriveError):
        raise
    except (HttpError, ResumableUploadError) as exc:
        raise classify_http_error(exc)
    except (ConnectionError, TimeoutError, OSError) as exc:
        raise classify_http_error(exc)
    progress({"uploaded_bytes": size, "total_bytes": size, "progress": 100.0, "speed": None})
    return {"id": response["id"], "name": response.get("name"), "link": response.get("webViewLink"),
            "size": int(response.get("size") or size), "skipped": False}
