import yt_dlp

from database import repository
from database.repository import VideoFilter
from services import queue_control
from workers.discovery_worker import DiscoveryWorker


def _rows(n, prefix="v"):
    return [{"video_id": f"{prefix}{i:010d}", "title": f"T{i}", "url": "https://u", "video_type": "video"}
            for i in range(n)]


def fake_source(events_or_exc):
    def iterator(parsed, settings, is_cancelled):
        for event in events_or_exc:
            if isinstance(event, Exception):
                raise event
            yield event
    return iterator


SOURCE = ("source", {"id": "UCHnyfMqiRRG1u-2MsSQLbXA", "kind": "channel", "title": "Chan",
                     "handle": "@chan", "url": "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA",
                     "thumbnail": None})


def _run(temp_db, events, url="@chan"):
    worker = DiscoveryWorker(source_iterator=fake_source(events))
    job_id = repository.create_discovery_job(url)
    worker.run_job(repository.get_discovery_job(job_id))
    return repository.get_discovery_job(job_id)


def test_successful_discovery_saves_videos(temp_db):
    job = _run(temp_db, [SOURCE, ("batch", "videos", _rows(50)), ("batch", "videos", _rows(10, "w")),
                         ("tab_skipped", "shorts", "none")])
    assert job["status"] == "completed"
    assert job["found_count"] == 60
    assert "no shorts tab" in job["message"]
    assert repository.get_source("UCHnyfMqiRRG1u-2MsSQLbXA")["video_count"] == 60
    assert repository.query_videos(VideoFilter())["total"] == 60


def test_rate_limit_pauses_queue_and_keeps_partial_results(temp_db):
    job = _run(temp_db, [SOURCE, ("batch", "videos", _rows(5)),
                         yt_dlp.utils.DownloadError("ERROR: HTTP Error 429: Too Many Requests")])
    assert job["status"] == "failed"
    assert job["error_kind"] == "rate_limited"
    assert "5 item(s) were saved" in job["message"]
    assert queue_control.is_paused()
    assert repository.query_videos(VideoFilter())["total"] == 5


def test_bot_check_pauses_queue(temp_db):
    job = _run(temp_db, [yt_dlp.utils.DownloadError("ERROR: Sign in to confirm you're not a bot")])
    assert job["error_kind"] == "bot_check"
    assert queue_control.status()["kind"] == "bot_check"


def test_unavailable_channel_does_not_pause(temp_db):
    job = _run(temp_db, [yt_dlp.utils.DownloadError("ERROR: This channel does not exist.")])
    assert job["status"] == "failed" and job["error_kind"] == "unavailable"
    assert not queue_control.is_paused()


def test_invalid_input_fails_cleanly(temp_db):
    job = _run(temp_db, [], url="https://example.com/@x")
    assert job["status"] == "failed" and job["error_kind"] == "invalid_input"


def test_worker_loop_skips_jobs_while_paused(temp_db):
    calls = []

    def iterator(parsed, settings, is_cancelled):
        calls.append(parsed.url)
        yield SOURCE

    queue_control.pause("rate_limited", "x", cooldown_minutes=1)
    worker = DiscoveryWorker(source_iterator=iterator, poll_seconds=0.05)
    job_id = worker.submit("@chan")
    worker.start()
    try:
        import time
        time.sleep(0.3)
        assert calls == []
        assert repository.get_discovery_job(job_id)["status"] == "pending"
        queue_control.resume()
        worker.wake()
        for _ in range(40):
            if repository.get_discovery_job(job_id)["status"] == "completed":
                break
            time.sleep(0.05)
        assert repository.get_discovery_job(job_id)["status"] == "completed"
    finally:
        worker.stop()


def test_cancel_running_job(temp_db):
    worker = DiscoveryWorker()
    job_id = repository.create_discovery_job("@chan")

    def iterator(parsed, settings, is_cancelled):
        yield SOURCE
        yield ("batch", "videos", _rows(3))
        worker.cancel(job_id)  # user presses Cancel mid-listing
        if is_cancelled():
            from services.youtube_service import DiscoveryCancelled
            raise DiscoveryCancelled()
        yield ("batch", "videos", _rows(3, "w"))

    repository.update_discovery_job(job_id, status="running")
    worker._iter_source = iterator
    worker.run_job(repository.get_discovery_job(job_id))
    job = repository.get_discovery_job(job_id)
    assert job["status"] == "cancelled" and job["found_count"] == 3
