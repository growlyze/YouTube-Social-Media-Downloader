import time
from pathlib import Path

import pytest
import yt_dlp

from database import repository
from database.repository import VideoFilter
from services import queue_control
from services.download_service import StopRequested
from workers.download_worker import RUNNING_KEY, DownloadWorker, retry_delay_seconds


def _seed(n=3, **kw):
    repository.upsert_source("UC1", "channel", "https://x")
    videos = [{"video_id": f"v{i:010d}", "title": f"T{i}", "url": "https://u", "channel_name": "Chan",
               "video_type": "video", "upload_date": "20240101", "date_is_approx": True, **kw}
              for i in range(n)]
    repository.upsert_videos("UC1", videos)
    return [v["video_id"] for v in videos]


@pytest.fixture
def settings_dir(temp_db, tmp_path):
    repository.update_settings({"download_dir": str(tmp_path / "dl"), "max_retries": 2})
    return tmp_path / "dl"


def fake_downloader(calls, outcome=None):
    """outcome(job) -> exception to raise, or None for success."""
    def download(job, settings, progress, should_stop):
        calls.append(job["video_id"])
        if outcome:
            exc = outcome(job)
            if exc:
                raise exc
        progress({"stage": "Downloading", "progress": 50.0, "speed": 10.0, "eta": 1})
        folder = Path(settings["download_dir"]) / "Chan"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"x [{job['video_id']}].mp4"
        path.write_bytes(b"data")
        return {"file_path": str(path), "file_size": 4, "format_desc": "360p mp4",
                "info": {"upload_date": "20240220", "duration": 321, "title": "Exact title"}}
    return download


def _run_next(worker):
    job = repository.claim_next_download()
    assert job, "expected a pending job"
    worker.run_job(job)
    return repository.get_download(job["id"])


# -- repository / queue ------------------------------------------------------

def test_enqueue_prevents_duplicates(settings_dir):
    ids = _seed(3)
    first = repository.enqueue_downloads(ids)
    assert first["added"] == 3
    again = repository.enqueue_downloads(ids + ["missing0000"])
    assert again["added"] == 0 and again["already_queued"] == 3 and again["missing"] == 1
    repository.update_download(repository.get_download_by_video(ids[0])["id"], status="completed")
    assert repository.enqueue_downloads([ids[0]])["already_downloaded"] == 1
    redo = repository.enqueue_downloads([ids[0]], redownload=True)
    assert redo["added"] == 1 and repository.get_download_by_video(ids[0])["redownload"] == 1


def test_enqueue_skips_unavailable_and_disabled_types(settings_dir):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [
        {"video_id": "private0001", "url": "u", "availability": "private"},
        {"video_id": "short000001", "url": "u", "video_type": "short"},
        {"video_id": "live0000001", "url": "u", "video_type": "live"},
    ])
    r = repository.enqueue_downloads(["private0001", "short000001", "live0000001"],
                                     include_shorts=False, include_live=False)
    assert r["skipped_unavailable"] == 1 and r["skipped_type"] == 2 and r["added"] == 0


def test_queue_is_fifo_and_respects_retry_backoff(settings_dir):
    ids = _seed(3)
    repository.enqueue_downloads(ids)
    first = repository.claim_next_download()
    assert first["video_id"] == ids[0]
    # a job waiting for its retry time is skipped, the next one is taken
    repository.update_download(first["id"], status="pending", next_attempt_at="2999-01-01 00:00:00")
    assert repository.claim_next_download()["video_id"] == ids[1]
    assert repository.claim_next_download()["video_id"] == ids[2]
    assert repository.claim_next_download() is None
    assert repository.next_retry_time() == "2999-01-01 00:00:00"


def test_retry_backoff_is_bounded():
    assert [retry_delay_seconds(n) for n in (1, 2, 3)] == [60, 120, 240]
    assert retry_delay_seconds(20) == 1800


# -- worker: outcomes --------------------------------------------------------

def test_successful_download_updates_metadata(settings_dir):
    ids = _seed(1)
    repository.enqueue_downloads(ids)
    calls = []
    done = _run_next(DownloadWorker(downloader=fake_downloader(calls), pacing=False))
    assert done["status"] == "completed" and done["progress"] == 100
    assert done["file_size"] == 4 and done["format_desc"] == "360p mp4"
    video = repository.get_video(ids[0])
    assert video["upload_date"] == "20240220" and video["date_is_approx"] == 0
    assert video["duration"] == 321 and video["title"] == "Exact title"
    row = repository.query_videos(VideoFilter(download="downloaded"))["items"][0]
    assert row["download_status"] == "completed"


def test_successful_download_queues_automatic_upload(settings_dir):
    ids = _seed(1)
    repository.update_settings({
        "auto_upload": True,
        "drive_folder_id": "folder-123",
        "drive_folder_name": "Backups",
    })
    repository.enqueue_downloads(ids)
    wakes = []
    done = _run_next(DownloadWorker(
        downloader=fake_downloader([]), pacing=False, upload_waker=lambda: wakes.append(True)))

    uploads = repository.list_uploads("queue")["items"]
    assert done["status"] == "completed"
    assert len(uploads) == 1
    assert uploads[0]["video_id"] == ids[0]
    assert uploads[0]["drive_folder_id"] == "folder-123"
    assert uploads[0]["drive_folder_name"] == "Backups"
    assert wakes == [True]


def test_social_media_item_uses_existing_auto_upload_queue(settings_dir):
    item = {
        "video_id": "instagram_0123456789abcdef01234567",
        "external_id": "reel-42",
        "platform": "instagram",
        "title": "Public reel",
        "channel_name": "creator",
        "url": "https://www.instagram.com/reel/reel-42/",
        "availability": "public",
    }
    repository.upsert_videos(None, [item])
    repository.update_settings({"auto_upload": True, "drive_folder_id": "folder-123"})
    repository.enqueue_downloads([item["video_id"]])
    done = _run_next(DownloadWorker(downloader=fake_downloader([]), pacing=False))

    uploads = repository.list_uploads("queue")["items"]
    assert done["status"] == "completed"
    assert len(uploads) == 1
    assert uploads[0]["video_id"] == item["video_id"]
    assert uploads[0]["platform"] == "instagram"


def test_existing_file_queues_automatic_upload(settings_dir):
    ids = _seed(1)
    folder = settings_dir / "Chan"
    folder.mkdir(parents=True)
    (folder / f"old [{ids[0]}].mkv").write_bytes(b"already here")
    repository.update_settings({"auto_upload": True, "drive_folder_id": "folder-123"})
    repository.enqueue_downloads(ids)

    done = _run_next(DownloadWorker(downloader=fake_downloader([]), pacing=False))

    assert done["status"] == "completed"
    assert repository.list_uploads("queue")["total"] == 1


def test_auto_upload_disabled_keeps_download_only(settings_dir):
    ids = _seed(1)
    repository.enqueue_downloads(ids)

    done = _run_next(DownloadWorker(downloader=fake_downloader([]), pacing=False))

    assert done["status"] == "completed"
    assert repository.upload_counts()["pending"] == 0


def test_existing_file_is_kept_and_not_downloaded_again(settings_dir):
    ids = _seed(1)
    folder = settings_dir / "Chan"
    folder.mkdir(parents=True)
    (folder / f"old [{ids[0]}].mkv").write_bytes(b"already here")
    repository.enqueue_downloads(ids)
    calls = []
    done = _run_next(DownloadWorker(downloader=fake_downloader(calls), pacing=False))
    assert calls == []
    assert done["status"] == "completed" and done["file_path"].endswith(f"old [{ids[0]}].mkv")
    assert (folder / f"old [{ids[0]}].mkv").read_bytes() == b"already here"


def test_partial_file_does_not_count_as_downloaded(settings_dir):
    ids = _seed(1)
    folder = settings_dir / "Chan"
    folder.mkdir(parents=True)
    (folder / f"x [{ids[0]}].mp4.part").write_bytes(b"half")
    repository.enqueue_downloads(ids)
    calls = []
    _run_next(DownloadWorker(downloader=fake_downloader(calls), pacing=False))
    assert calls == ids   # the downloader ran (and yt-dlp would resume the .part)


def test_retryable_error_is_retried_then_fails(settings_dir):
    ids = _seed(1)
    repository.enqueue_downloads(ids)
    worker = DownloadWorker(downloader=fake_downloader(
        [], lambda job: yt_dlp.utils.DownloadError("ERROR: Unable to download webpage: timed out")), pacing=False)
    job = _run_next(worker)
    assert job["status"] == "pending" and job["attempts"] == 1 and job["next_attempt_at"]
    assert job["error_kind"] == "network" and "Retry 1/2" in job["error_message"]
    for expected_attempts, expected_status in ((2, "pending"), (3, "failed")):
        repository.update_download(job["id"], next_attempt_at=None)  # skip the wait
        job = _run_next(worker)
        assert job["attempts"] == expected_attempts and job["status"] == expected_status
    assert "Failed after 3 attempt(s)" in job["error_message"]


@pytest.mark.parametrize("message, kind, text", [
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access", "private", "Private video"),
    ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", "unavailable", "unavailable"),
    ("ERROR: [youtube] x: Join this channel to get access to members-only content", "members_only", "Members-only"),
])
def test_permanent_errors_fail_immediately_with_clear_message(settings_dir, message, kind, text):
    ids = _seed(1)
    repository.enqueue_downloads(ids)
    worker = DownloadWorker(downloader=fake_downloader([], lambda job: yt_dlp.utils.DownloadError(message)),
                            pacing=False)
    job = _run_next(worker)
    assert job["status"] == "failed" and job["attempts"] == 1 and job["error_kind"] == kind
    assert text.lower() in job["error_message"].lower()
    assert not queue_control.is_paused()


@pytest.mark.parametrize("message, kind", [
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "rate_limited"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "bot_check"),
])
def test_rate_limit_pauses_everything_without_consuming_attempts(settings_dir, message, kind):
    ids = _seed(2)
    repository.enqueue_downloads(ids)
    calls = []
    worker = DownloadWorker(downloader=fake_downloader(calls, lambda job: yt_dlp.utils.DownloadError(message)),
                            pacing=False)
    job = _run_next(worker)
    assert job["status"] == "pending" and job["attempts"] == 0 and job["next_attempt_at"] is None
    assert queue_control.status()["kind"] == kind
    # nothing else is claimed by the worker loop while paused
    repository.set_state(RUNNING_KEY, True)
    assert worker._claim() is None
    assert calls == [ids[0]]


def test_missing_ffmpeg_fails_once_with_install_hint(settings_dir):
    from services.download_service import FFmpegRequired
    ids = _seed(1)
    repository.enqueue_downloads(ids)
    job = _run_next(DownloadWorker(downloader=fake_downloader([], lambda job: FFmpegRequired()), pacing=False))
    assert job["status"] == "failed" and job["error_kind"] == "ffmpeg"
    assert job["error_message"].startswith("This media has separate streams")
    assert job["error_message"].count("winget install Gyan.FFmpeg") == 1


def test_three_consecutive_403s_pause_queue(settings_dir):
    ids = _seed(3)
    repository.enqueue_downloads(ids)
    worker = DownloadWorker(downloader=fake_downloader(
        [], lambda job: yt_dlp.utils.DownloadError("ERROR: HTTP Error 403: Forbidden")), pacing=False)
    _run_next(worker)
    _run_next(worker)
    assert not queue_control.is_paused()
    _run_next(worker)
    assert queue_control.status()["kind"] == "forbidden"


def test_cancel_running_download_removes_partial(settings_dir):
    ids = _seed(1)
    repository.enqueue_downloads(ids)
    folder = settings_dir / "Chan"
    folder.mkdir(parents=True)
    part = folder / f"x [{ids[0]}].mp4.part"
    part.write_bytes(b"half")
    worker = DownloadWorker(pacing=False)

    def download(job, settings, progress, should_stop):
        worker.cancel(job["id"])          # user clicks Cancel mid-download
        if should_stop() == "cancel":
            raise StopRequested("cancel")
    worker._download = download
    job = _run_next(worker)
    assert job["status"] == "cancelled" and "Partial file removed" in job["error_message"]
    assert not part.exists()


def test_cancel_pending_item(settings_dir):
    ids = _seed(1)
    repository.enqueue_downloads(ids)
    worker = DownloadWorker()
    download_id = repository.get_download_by_video(ids[0])["id"]
    assert worker.cancel(download_id)
    assert repository.get_download(download_id)["status"] == "cancelled"
    assert not worker.cancel(download_id)


def test_pause_keeps_job_pending_for_resume(settings_dir):
    ids = _seed(1)
    repository.enqueue_downloads(ids)

    def download(job, settings, progress, should_stop):
        raise StopRequested("pause")
    job = _run_next(DownloadWorker(downloader=download, pacing=False))
    assert job["status"] == "pending" and "resume from the partial file" in job["error_message"]


def test_one_failure_does_not_stop_the_queue(settings_dir):
    ids = _seed(3)
    repository.enqueue_downloads(ids)
    calls = []

    def outcome(job):
        if job["video_id"] == ids[0]:
            raise RuntimeError("unexpected bug in one item")
    worker = DownloadWorker(downloader=fake_downloader(calls, outcome), poll_seconds=0.02, pacing=False)
    worker.start()
    try:
        worker.start_queue()
        for _ in range(100):
            counts = repository.download_counts()
            if counts["completed"] == 2 and counts["downloading"] == 0:
                break
            time.sleep(0.05)
    finally:
        worker.stop()
    assert calls[:3] == ids                      # sequential, FIFO order
    assert repository.download_counts()["completed"] == 2
    first = repository.get_download_by_video(ids[0])
    assert first["status"] == "pending" and first["attempts"] == 1   # bounded retry scheduled


def test_restart_recovery(settings_dir):
    ids = _seed(2)
    repository.enqueue_downloads(ids)
    repository.set_state(RUNNING_KEY, True)
    downloading = repository.claim_next_download()           # crash mid-download
    repository.update_download(repository.get_download_by_video(ids[1])["id"], status="completed",
                               file_path="C:/x.mp4", file_size=10)
    worker = DownloadWorker(downloader=fake_downloader([]))
    worker.start()
    try:
        recovered = repository.get_download(downloading["id"])
        assert recovered["status"] == "pending"               # never marked completed
        assert "partial file" in recovered["error_message"]
        assert repository.get_download_by_video(ids[1])["status"] == "completed"   # kept
        assert worker.is_running() is False                    # comes back paused
        assert "restarted" in worker.snapshot()["note"]
    finally:
        worker.stop()
