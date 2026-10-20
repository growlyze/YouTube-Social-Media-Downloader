"""Regression: Phase 4 (Drive uploads) must not change how downloading works."""
from pathlib import Path

from database import repository
from services.drive_service import DriveError
from workers.download_worker import DownloadWorker
from workers.upload_worker import PAUSE_KEY, UploadWorker


def _seed(n=2):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [{"video_id": f"r{i:010d}", "title": f"T{i}", "url": "u",
                                      "channel_name": "Chan"} for i in range(n)])
    return [f"r{i:010d}" for i in range(n)]


def _downloader(job, settings, progress, should_stop):
    folder = Path(settings["download_dir"]) / "Chan"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"x [{job['video_id']}].mp4"
    path.write_bytes(b"video")
    return {"file_path": str(path), "file_size": 5, "format_desc": "1080p mp4", "info": {}}


def test_downloads_still_work_while_uploads_are_paused_by_an_error(temp_db, tmp_path):
    ids = _seed(2)
    repository.enqueue_downloads(ids, download_dir=str(tmp_path / "dl"))
    downloads = DownloadWorker(downloader=_downloader, pacing=False)
    downloads.run_job(repository.claim_next_download())
    first = repository.get_download_by_video(ids[0])

    # an upload fails with a pausing error (e.g. Drive storage full)
    repository.enqueue_uploads([first["id"]], "F1")
    def failing(*a, **k):
        raise DriveError("quota", "Drive full", pause=True)
    UploadWorker(uploader=failing).run_job(repository.claim_next_upload())
    assert repository.get_state(PAUSE_KEY)

    # downloading is unaffected: the next video downloads normally
    downloads.run_job(repository.claim_next_download())
    second = repository.get_download_by_video(ids[1])
    assert second["status"] == "completed" and Path(second["file_path"]).is_file()
    assert repository.get_download_by_video(ids[0])["status"] == "completed"


def test_upload_queue_does_not_block_or_reorder_downloads(temp_db, tmp_path):
    ids = _seed(2)
    repository.enqueue_downloads(ids, download_dir=str(tmp_path / "dl"))
    assert repository.claim_next_download()["video_id"] == ids[0]     # FIFO as before
    assert repository.download_counts()["pending"] == 1
    assert repository.upload_counts() == {"pending": 0, "uploading": 0, "completed": 0, "failed": 0, "cancelled": 0}


def test_download_dedupe_and_redownload_unchanged_by_uploads(temp_db, tmp_path):
    ids = _seed(1)
    repository.enqueue_downloads(ids, download_dir=str(tmp_path / "dl"))
    DownloadWorker(downloader=_downloader, pacing=False).run_job(repository.claim_next_download())
    d = repository.get_download_by_video(ids[0])
    repository.enqueue_uploads([d["id"]], "F1")
    assert repository.enqueue_downloads(ids)["already_downloaded"] == 1
    assert repository.enqueue_downloads(ids, redownload=True)["added"] == 1
    # the pending upload still points at the same download record
    assert repository.list_uploads("queue")["items"][0]["download_id"] == d["id"]


def test_removing_a_source_keeps_download_and_upload_history(temp_db, tmp_path):
    ids = _seed(1)
    repository.enqueue_downloads(ids, download_dir=str(tmp_path / "dl"))
    DownloadWorker(downloader=_downloader, pacing=False).run_job(repository.claim_next_download())
    d = repository.get_download_by_video(ids[0])
    repository.enqueue_uploads([d["id"]], "F1")
    repository.delete_source("UC1")
    assert repository.get_download(d["id"])["status"] == "completed"
    assert repository.list_uploads("queue")["total"] == 1


def test_existing_download_pages_and_apis_still_respond(client):
    for path in ("/", "/library", "/downloads", "/local", "/history", "/settings"):
        assert client.get(path).status_code == 200, path
    assert client.get("/api/downloads/status").status_code == 200
    assert client.get("/api/local").status_code == 200
