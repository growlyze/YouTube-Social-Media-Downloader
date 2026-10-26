"""Background thread that runs channel/playlist discovery jobs one at a time.

Jobs are rows in `discovery_jobs`; the HTTP request that creates a job returns
immediately and the UI polls the job for progress. Only one job runs at a time
so discovery never issues parallel requests to YouTube.

When a discovery job completes, `on_discovered(source_id)` (wired up in app.py)
queues every discovered video that is not downloaded yet and starts the download
queue, so adding a channel or playlist starts downloading automatically.
"""

import logging
import threading
from datetime import datetime, timezone

import yt_dlp

from database import repository
from services import queue_control
from services.errors import classify_error, clean_message
from services.youtube_service import (DiscoveryCancelled, InputError, iter_source,
                                      parse_source_input)

log = logging.getLogger("discovery")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class DiscoveryWorker:
    def __init__(self, source_iterator=iter_source, poll_seconds=2.0, on_discovered=None):
        self._iter_source = source_iterator
        self._poll = poll_seconds
        self._on_discovered = on_discovered
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._cancel_ids: set[int] = set()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        requeued = repository.requeue_interrupted_discovery_jobs()
        if requeued:
            log.info("Requeued %d interrupted discovery job(s)", requeued)
        self._thread = threading.Thread(target=self._loop, name="discovery-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    # -- API used by routes ------------------------------------------------

    def submit(self, input_url: str) -> int:
        job_id = repository.create_discovery_job(input_url)
        self._wake.set()
        return job_id

    def cancel(self, job_id: int) -> bool:
        job = repository.get_discovery_job(job_id)
        if not job or job["status"] not in ("pending", "running"):
            return False
        if job["status"] == "pending":
            repository.update_discovery_job(job_id, status="cancelled", finished_at=_now(),
                                            message="Cancelled before it started")
        else:
            with self._lock:
                self._cancel_ids.add(job_id)
        return True

    def wake(self):
        self._wake.set()

    # -- worker loop -------------------------------------------------------

    def _loop(self):
        while not self._stop.is_set():
            try:
                if not queue_control.is_paused():
                    job = repository.next_pending_discovery_job()
                    if job:
                        self.run_job(job)
                        continue
            except Exception:  # keep the thread alive no matter what
                log.exception("Discovery worker loop error")
            self._wake.wait(self._poll)
            self._wake.clear()

    def run_job(self, job: dict):
        job_id = job["id"]
        repository.update_discovery_job(job_id, status="running", started_at=_now(),
                                        message="Starting…", error_kind=None)
        settings = repository.get_settings()
        found = 0
        source_id = None
        skipped_tabs = []

        def is_cancelled():
            with self._lock:
                return job_id in self._cancel_ids or self._stop.is_set()

        try:
            parsed = parse_source_input(job["input_url"], settings.get("include_shorts", True),
                                        settings.get("include_streams", False))
            position = 0
            for event in self._iter_source(parsed, settings, is_cancelled=is_cancelled):
                if event[0] == "source":
                    meta = event[1]
                    if not meta.get("id"):
                        raise InputError("YouTube did not return an id for this channel/playlist.")
                    source_id = meta["id"]
                    repository.upsert_source(source_id, meta["kind"], meta["url"], meta.get("title"),
                                             meta.get("handle"), meta.get("thumbnail"))
                    repository.update_discovery_job(job_id, source_id=source_id,
                                                    message=f"Listing {meta.get('title') or source_id}…")
                elif event[0] == "batch":
                    _, tab, rows = event
                    repository.upsert_videos(source_id, rows, start_position=position)
                    position += len(rows)
                    found += len(rows)
                    repository.update_discovery_job(
                        job_id, found_count=found,
                        message=f"Found {found} item(s) so far ({tab or 'playlist'})…")
                elif event[0] == "tab_skipped":
                    skipped_tabs.append(event[1])

            if source_id:
                repository.mark_source_discovered(source_id)
            note = f" (no {', '.join(skipped_tabs)} tab)" if skipped_tabs else ""
            repository.update_discovery_job(job_id, status="completed", finished_at=_now(),
                                            found_count=found,
                                            message=f"Found {found} item(s){note}.")
            log.info("Discovery job %s completed: %d items", job_id, found)
            self._auto_download(source_id)

        except DiscoveryCancelled:
            if source_id:
                repository.mark_source_discovered(source_id)
            stopping = self._stop.is_set()
            repository.update_discovery_job(
                job_id,
                status="pending" if stopping else "cancelled",
                finished_at=None if stopping else _now(),
                found_count=found,
                message=("Interrupted by shutdown; will resume on restart" if stopping
                         else f"Cancelled after {found} item(s)."))

        except InputError as exc:
            repository.update_discovery_job(job_id, status="failed", finished_at=_now(),
                                            error_kind="invalid_input", message=str(exc))

        except Exception as exc:  # yt-dlp DownloadError, network errors, bugs
            info = classify_error(exc)
            if not isinstance(exc, yt_dlp.utils.DownloadError):
                log.exception("Unexpected discovery error for job %s", job_id)
            else:
                log.warning("Discovery job %s failed (%s): %s", job_id, info.kind, clean_message(exc))
            if source_id:
                repository.mark_source_discovered(source_id)
            if info.pause_queue:
                queue_control.pause(info.kind, info.message)
            detail = clean_message(exc)[:300]
            partial = f" {found} item(s) were saved before the error." if found else ""
            repository.update_discovery_job(
                job_id, status="failed", finished_at=_now(), found_count=found,
                error_kind=info.kind, message=f"{info.message}{partial} Details: {detail}")
        finally:
            with self._lock:
                self._cancel_ids.discard(job_id)

    def _auto_download(self, source_id):
        """Queue everything this source found and start the download queue."""
        if not source_id or not self._on_discovered:
            return
        try:
            self._on_discovered(source_id)
        except Exception:
            log.exception("Automatic download after discovery failed for %s", source_id)
