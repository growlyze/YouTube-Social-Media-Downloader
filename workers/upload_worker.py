"""Background Google Drive upload queue (manual uploads, Phase 4).

Completely separate from the download queue: it reads finished downloads and
writes only to the `uploads` table, so an upload failure can never mark a
download as failed. One upload at a time.

* Transient errors (network, 5xx, Drive rate limits) are retried with backoff
  (1, 2, 4… minutes, at most `max_retries` times).
* Auth problems or a full Drive pause the upload queue until the user fixes
  the cause and presses Resume.
* After a restart, interrupted uploads go back to pending and start over
  (an unfinished resumable upload never creates a Drive file, and the
  ytVideoId tag prevents duplicates if it did finish).
"""

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from database import repository
from services import drive_service
from services.drive_service import DriveError, UploadCancelled
from workers.download_worker import retry_delay_seconds

log = logging.getLogger("upload-worker")

RUNNING_KEY = "upload_queue_running"
PAUSE_KEY = "upload_queue_pause"     # {"kind", "message"} when paused by an error


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class UploadWorker:
    def __init__(self, uploader=drive_service.upload_file, poll_seconds=2.0, progress_interval=1.0):
        self._upload = uploader
        self._poll = poll_seconds
        self._progress_interval = progress_interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread = None
        self._cancel_ids: set[int] = set()
        self.current_id = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        requeued = repository.requeue_interrupted_uploads()
        if requeued:
            log.info("Requeued %d interrupted upload(s)", requeued)
        if repository.get_state(RUNNING_KEY) is None:
            repository.set_state(RUNNING_KEY, True)
        self._thread = threading.Thread(target=self._loop, name="upload-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout=10):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    # -- controls ----------------------------------------------------------

    def is_running(self):
        return bool(repository.get_state(RUNNING_KEY, True)) and not repository.get_state(PAUSE_KEY)

    def start_queue(self):
        repository.set_state(RUNNING_KEY, True)
        repository.set_state(PAUSE_KEY, None)
        self.wake()

    def pause_queue(self):
        repository.set_state(RUNNING_KEY, False)
        self.wake()

    def cancel(self, upload_id):
        item = repository.get_upload(upload_id)
        if not item or item["status"] not in ("pending", "uploading"):
            return False
        if item["status"] == "pending":
            repository.update_upload(upload_id, status="cancelled", finished_at=_now_str(),
                                     error_message="Cancelled before it started.")
        else:
            with self._lock:
                self._cancel_ids.add(upload_id)
        return True

    def wake(self):
        self._wake.set()

    def snapshot(self):
        return {"running": self.is_running(),
                "user_paused": not repository.get_state(RUNNING_KEY, True),
                "error_pause": repository.get_state(PAUSE_KEY),
                "active": self.current_id is not None}

    # -- loop --------------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            try:
                if self.is_running():
                    item = repository.claim_next_upload()
                    if item:
                        self.run_job(item)
                        continue
            except Exception:
                log.exception("Upload worker loop error")
            self._wake.wait(self._poll)
            self._wake.clear()

    def _should_stop(self, upload_id):
        with self._lock:
            if upload_id in self._cancel_ids:
                return "cancel"
        if self._stop.is_set() or not self.is_running():
            return "pause"
        return None

    def run_job(self, item):
        upload_id = item["id"]
        self.current_id = upload_id
        last = [0.0]

        def progress(fields):
            now = time.monotonic()
            if now - last[0] >= self._progress_interval or fields.get("progress") in (0.0, 100.0):
                last[0] = now
                repository.update_upload(upload_id, **fields)

        try:
            if item.get("download_status") != "completed":
                raise DriveError("local_file", "The download is no longer marked as completed; "
                                               "re-download it before uploading.")
            result = self._upload(item["file_path"], item["drive_folder_id"], item["video_id"],
                                  item["download_id"], progress=progress,
                                  should_stop=lambda: self._should_stop(upload_id))
            note = ("Already in this Drive folder (uploaded earlier); not uploaded again."
                    if result.get("skipped") else None)
            repository.update_upload(upload_id, status="completed", finished_at=_now_str(), progress=100,
                                     drive_file_id=result["id"], web_link=result.get("link"),
                                     uploaded_bytes=result.get("size"), speed=None,
                                     error_kind=None, error_message=note)
            log.info("Uploaded %s to Drive folder %s (file %s)", item["video_id"],
                     item["drive_folder_id"], result["id"])
        except UploadCancelled as stop:
            if stop.reason == "cancel":
                repository.update_upload(upload_id, status="cancelled", finished_at=_now_str(), speed=None,
                                         error_message="Cancelled by you. Nothing was saved to Drive.")
            else:
                repository.update_upload(upload_id, status="pending", speed=None, started_at=None,
                                         error_message="Paused; the upload will start again when resumed.")
        except Exception as exc:
            err = drive_service.classify_http_error(exc)
            if err.kind == "unknown" and not isinstance(exc, DriveError):
                log.exception("Unexpected upload error for %s", item["video_id"])
            else:
                log.warning("Upload %s failed (%s): %s", item["video_id"], err.kind, err)
            self._handle_failure(item, err)
        finally:
            with self._lock:
                self._cancel_ids.discard(upload_id)
            self.current_id = None

    def _handle_failure(self, item, err: DriveError):
        if err.pause:
            repository.set_state(PAUSE_KEY, {"kind": err.kind, "message": str(err)})
            repository.update_upload(item["id"], status="pending", speed=None, started_at=None,
                                     error_kind=err.kind,
                                     error_message=f"{err} Uploads are paused until you fix this and press Resume.")
            return
        max_retries = int(repository.get_settings().get("max_retries", 3))
        attempts = int(item.get("attempts") or 0) + 1
        if err.retryable and attempts <= max_retries:
            delay = retry_delay_seconds(attempts)
            when = datetime.now(timezone.utc) + timedelta(seconds=delay)
            repository.update_upload(item["id"], status="pending", attempts=attempts, speed=None,
                                     started_at=None, next_attempt_at=when.strftime("%Y-%m-%d %H:%M:%S"),
                                     error_kind=err.kind,
                                     error_message=f"Retry {attempts}/{max_retries} in {delay // 60} min. {err}")
        else:
            prefix = f"Failed after {attempts} attempt(s). " if err.retryable else ""
            repository.update_upload(item["id"], status="failed", attempts=attempts, finished_at=_now_str(),
                                     speed=None, error_kind=err.kind, error_message=prefix + str(err))
