"""Background download queue.

* Sequential by default (`max_concurrent_downloads` = 1); up to 3 if the
  user raises it.
* A random pause (sleep_interval_min..max seconds) separates downloads.
* Recoverable errors are retried a bounded number of times with exponential
  backoff (1, 2, 4… minutes, capped at 30).
* Rate limits / bot checks pause ALL YouTube activity (queue_control) and the
  job goes back to pending untouched; nothing resumes until the user does.
* The user's Start/Pause switch is stored in SQLite. After a restart the queue
  comes back paused so nothing starts downloading unexpectedly.
"""

import logging
import random
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from database import repository
from services import queue_control
from services.download_service import (FFmpegRequired, IntegrityError, SkippedVideo,
                                       StopRequested, download_video, find_existing_file,
                                       remove_partial_files)
from services.errors import classify_error, clean_message

log = logging.getLogger("download-worker")

RUNNING_KEY = "download_queue_running"
NOTE_KEY = "download_queue_note"
MAX_THREADS = 3
FORBIDDEN_PAUSE_THRESHOLD = 3   # consecutive HTTP 403s before pausing everything


def _now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def retry_delay_seconds(attempt: int) -> int:
    """Backoff before retry number `attempt` (1-based): 60s, 120s, 240s… max 30 min."""
    return int(min(1800, 60 * 2 ** (attempt - 1)))


class DownloadWorker:
    def __init__(self, downloader=download_video, poll_seconds=2.0, progress_interval=1.0,
                 pacing=True, upload_waker=None):
        self._download = downloader
        self._poll = poll_seconds
        self._progress_interval = progress_interval
        self._pacing = pacing
        self._upload_waker = upload_waker
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._cancel_ids: set[int] = set()
        self._active: set[int] = set()
        self._next_start_at = 0.0          # monotonic time; pacing between downloads
        self._consecutive_forbidden = 0
        self._pause_cache = (0.0, False)
        self._running_cache = (0.0, True)

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        requeued = repository.requeue_interrupted_downloads()
        pending = repository.download_counts()["pending"]
        if repository.get_state(RUNNING_KEY) and pending:
            repository.set_state(NOTE_KEY, "The app was restarted, so the queue was paused. "
                                           "Press Start to continue.")
        repository.set_state(RUNNING_KEY, False)
        if requeued:
            log.info("Requeued %d interrupted download(s); partial files will be resumed", requeued)
        for index in range(MAX_THREADS):
            thread = threading.Thread(target=self._loop, name=f"download-worker-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout=10):
        self._stop.set()
        self._wake.set()
        for thread in self._threads:
            thread.join(timeout)

    # -- controls used by routes -------------------------------------------

    def is_running(self) -> bool:
        return bool(repository.get_state(RUNNING_KEY, False))

    def start_queue(self):
        repository.set_state(RUNNING_KEY, True)
        repository.set_state(NOTE_KEY, None)
        self._consecutive_forbidden = 0
        self.wake()

    def pause_queue(self):
        """Stop after interrupting the active download; its partial file is kept."""
        repository.set_state(RUNNING_KEY, False)
        self.wake()

    def cancel(self, download_id: int) -> bool:
        job = repository.get_download(download_id)
        if not job or job["status"] not in ("pending", "downloading"):
            return False
        if job["status"] == "pending":
            repository.update_download(download_id, status="cancelled", finished_at=_now_str(),
                                       error_message="Cancelled before it started.")
        else:
            with self._lock:
                self._cancel_ids.add(download_id)
        return True

    def wake(self):
        self._wake.set()

    def snapshot(self) -> dict:
        wait = max(0, int(self._next_start_at - time.monotonic()))
        with self._lock:
            active = len(self._active)
        return {"running": self.is_running(), "active": active,
                "pacing_wait": wait if active == 0 and self.is_running() else 0,
                "note": repository.get_state(NOTE_KEY)}

    # -- loop --------------------------------------------------------------

    def _youtube_paused(self) -> bool:
        stamp, value = self._pause_cache
        if time.monotonic() - stamp > 1.0:
            value = queue_control.is_paused()
            self._pause_cache = (time.monotonic(), value)
        return value

    def _sleep(self, seconds):
        self._wake.wait(seconds)
        self._wake.clear()

    def _loop(self):
        while not self._stop.is_set():
            try:
                job = self._claim()
                if job:
                    self.run_job(job)
                    continue
            except Exception:
                log.exception("Download worker loop error")
            self._sleep(self._poll)

    def _claim(self):
        if not self.is_running() or queue_control.is_paused():
            return None
        settings = repository.get_settings()
        with self._lock:
            if len(self._active) >= int(settings.get("max_concurrent_downloads", 1)):
                return None
            if self._pacing and time.monotonic() < self._next_start_at:
                return None
            job = repository.claim_next_download()
            if job:
                self._active.add(job["id"])
            return job

    def _should_stop(self, download_id):
        with self._lock:
            if download_id in self._cancel_ids:
                return "cancel"
        if self._stop.is_set() or not self.is_running_cached() or self._youtube_paused():
            return "pause"
        return None

    def is_running_cached(self):
        stamp, value = self._running_cache
        if time.monotonic() - stamp > 1.0:
            value = self.is_running()
            self._running_cache = (time.monotonic(), value)
        return value

    def _finish_pacing(self, settings):
        low = float(settings.get("sleep_interval_min", 5))
        high = max(low, float(settings.get("sleep_interval_max", 15)))
        self._next_start_at = time.monotonic() + random.uniform(low, high)

    # -- one job -----------------------------------------------------------

    def run_job(self, job: dict):
        download_id = job["id"]
        settings = repository.get_settings()
        # Save where the user confirmed when queueing; older rows use the setting.
        if job.get("download_dir"):
            settings["download_dir"] = job["download_dir"]
        download_dir = Path(settings["download_dir"])
        last_write = [0.0]
        self._running_cache = (0.0, True)

        def progress(fields):
            now = time.monotonic()
            # Throttle DB writes; always write stage changes.
            if now - last_write[0] >= self._progress_interval or "progress" not in fields:
                last_write[0] = now
                repository.update_download(download_id, **fields)

        try:
            if job.get("live_status") in ("is_live", "is_upcoming"):
                raise SkippedVideo("Live stream in progress or upcoming premiere; it can't be "
                                   "downloaded yet.")

            existing = None if job.get("redownload") else find_existing_file(download_dir, job)
            if existing:
                repository.update_download(
                    download_id, status="completed", finished_at=_now_str(), progress=100,
                    file_path=str(existing), file_size=existing.stat().st_size, stage=None,
                    format_desc="already on disk", speed=None, eta=None, error_kind=None,
                    error_message="A file for this video already existed; it was kept as-is.")
                self._queue_auto_upload(download_id)
                log.info("Video %s already on disk at %s", job["video_id"], existing)
                return

            result = self._download(job, settings, progress=progress,
                                    should_stop=lambda: self._should_stop(download_id))

            repository.update_download(
                download_id, status="completed", finished_at=_now_str(), progress=100,
                file_path=result["file_path"], file_size=result["file_size"],
                format_desc=result.get("format_desc"), stage=None, speed=None, eta=0,
                redownload=0, error_kind=None, error_message=None)
            if result.get("info"):
                repository.update_video_metadata(job["video_id"], result["info"])
            self._queue_auto_upload(download_id)
            self._consecutive_forbidden = 0
            log.info("Downloaded %s -> %s", job["video_id"], result["file_path"])
            if self._pacing:
                self._finish_pacing(settings)

        except StopRequested as stop:
            if stop.reason == "cancel":
                removed = remove_partial_files(download_dir, job)
                repository.update_download(
                    download_id, status="cancelled", finished_at=_now_str(), stage=None,
                    speed=None, eta=None,
                    error_message="Cancelled by you." + (" Partial file removed." if removed else ""))
            else:
                repository.update_download(
                    download_id, status="pending", stage=None, speed=None, eta=None,
                    started_at=None,
                    error_message="Paused; will resume from the partial file.")

        except SkippedVideo as exc:
            repository.update_download(download_id, status="failed", finished_at=_now_str(),
                                       stage=None, error_kind="upcoming", error_message=str(exc))

        except IntegrityError as exc:
            self._handle_failure(job, settings, "integrity", False,
                                 f"File check failed: {exc}", detail=None)

        except FFmpegRequired as exc:
            self._handle_failure(job, settings, "ffmpeg", False, str(exc), detail=None)

        except Exception as exc:  # yt-dlp DownloadError, network, disk, bugs
            info = classify_error(exc)
            detail = clean_message(exc)[:400]
            if not hasattr(exc, "msg") and info.kind == "unknown":
                log.exception("Unexpected error downloading %s", job["video_id"])
            else:
                log.warning("Download %s failed (%s): %s", job["video_id"], info.kind, detail)

            if info.pause_queue:
                self._pause_everything(job, info.kind, info.message, detail)
            elif info.kind == "forbidden":
                self._consecutive_forbidden += 1
                if self._consecutive_forbidden >= FORBIDDEN_PAUSE_THRESHOLD:
                    self._pause_everything(
                        job, "forbidden",
                        f"YouTube refused {self._consecutive_forbidden} downloads in a row (HTTP 403). "
                        "This often means the connection is being blocked. The queue is paused; "
                        "wait before resuming and keep yt-dlp updated.", detail)
                else:
                    self._handle_failure(job, settings, info.kind, True, info.message, detail)
            else:
                self._handle_failure(job, settings, info.kind, info.retryable, info.message, detail)
        finally:
            with self._lock:
                self._active.discard(download_id)
                self._cancel_ids.discard(download_id)

    def _queue_auto_upload(self, download_id):
        try:
            settings = repository.get_settings()
            if not settings.get("auto_upload"):
                return
            folder_id = settings.get("drive_folder_id")
            if not folder_id:
                message = ("Download completed, but automatic upload could not be queued: "
                           "choose a Google Drive folder in Settings.")
                log.error("Download %s completed without an automatic-upload destination", download_id)
                repository.update_download(download_id, error_message=message)
                return
            result = repository.enqueue_uploads(
                [download_id], folder_id, settings.get("drive_folder_name"))
        except Exception as exc:
            log.exception("Could not queue automatic upload for download %s", download_id)
            try:
                repository.update_download(
                    download_id,
                    error_message=f"Download completed, but automatic upload could not be queued: {exc}")
            except Exception:
                log.exception("Could not record automatic-upload queue failure for download %s",
                              download_id)
            return
        if result["added"] and self._upload_waker:
            try:
                self._upload_waker()
            except Exception:
                log.exception("Could not wake upload worker for download %s", download_id)

    def _pause_everything(self, job, kind, message, detail):
        queue_control.pause(kind, message)
        self._pause_cache = (time.monotonic(), True)
        repository.update_download(
            job["id"], status="pending", stage=None, speed=None, eta=None, started_at=None,
            error_kind=kind,
            error_message=f"{message} This video will be retried after you resume. Details: {detail}")

    def _handle_failure(self, job, settings, kind, retryable, message, detail):
        max_retries = int(settings.get("max_retries", 3))
        attempts = int(job.get("attempts") or 0) + 1
        text = message + (f" Details: {detail}" if detail and detail not in message else "")
        if retryable and attempts <= max_retries:
            delay = retry_delay_seconds(attempts)
            when = datetime.now(timezone.utc) + timedelta(seconds=delay)
            repository.update_download(
                job["id"], status="pending", attempts=attempts, stage=None, speed=None, eta=None,
                started_at=None, next_attempt_at=when.strftime("%Y-%m-%d %H:%M:%S"),
                error_kind=kind,
                error_message=f"Retry {attempts}/{max_retries} in {delay // 60} min. {text}")
        else:
            prefix = f"Failed after {attempts} attempt(s). " if retryable else ""
            repository.update_download(
                job["id"], status="failed", attempts=attempts, finished_at=_now_str(), stage=None,
                speed=None, eta=None, error_kind=kind, error_message=prefix + text)
