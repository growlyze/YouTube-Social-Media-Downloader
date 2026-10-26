"""Background free-space re-check (see services/space_service.py).

* The cheap local check runs on every poll (default 30 s) so downloads stop
  quickly when the disk fills up and continue soon after space frees up.
* The full check (local + Google Drive) runs every `space_recheck_minutes`
  (default 60), or immediately through `force_check()` from the Space Check
  page's "Recheck space and continue" button.
"""

import logging
import threading

from services import space_service

log = logging.getLogger("space-worker")


class SpaceCheckWorker:
    def __init__(self, poll_seconds=30.0):
        self._poll = poll_seconds
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="space-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self):
        self._wake.set()

    # -- checks ------------------------------------------------------------

    def force_check(self) -> dict:
        """Run the full check right now (no waiting for the timer) and return
        what happened, so the page can show it immediately."""
        result = space_service.run_checks()
        self.wake()
        return result

    def _loop(self):
        while not self._stop.is_set():
            try:
                if space_service.check_due():
                    result = space_service.run_checks()
                else:
                    result = space_service.refresh_local()
                if result.get("changed"):
                    log.info("Space check changed the queue state: %s",
                             result.get("events", ["(no details)"])[-1])
            except Exception:
                log.exception("Space check worker loop error")
            self._wake.wait(self._poll)
            self._wake.clear()
