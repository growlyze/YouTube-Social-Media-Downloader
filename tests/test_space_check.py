"""Space Check: automatic stop/continue on low free space (local + Google Drive),
the force re-check, and auto-download after channel/playlist discovery."""

from types import SimpleNamespace

from database import repository
from services import space_service
from services.drive_service import DriveError
from workers.discovery_worker import DiscoveryWorker
from workers.download_worker import DownloadWorker
from workers.upload_worker import UploadWorker

from app import auto_download_source


def _disk(free_percent, total=1000):
    free = int(total * free_percent / 100)
    return SimpleNamespace(total=total, used=total - free, free=free)


def _reset_cache():
    space_service._local_cache.update(at=0.0, blocked=False)


def _video(video_id):
    return {"video_id": video_id, "title": "T", "url": "u", "channel_name": "Chan"}


# -- space_service -----------------------------------------------------------

def test_local_low_space_pauses_and_free_space_continues(temp_db, monkeypatch):
    monkeypatch.setattr(space_service.shutil, "disk_usage", lambda p: _disk(1))   # 1% < 5%
    result = space_service.run_checks()
    assert result["changed"] is True
    state = space_service.status_payload()
    assert state["local_pause"]["paused"] is True
    assert state["local"]["free_percent"] == 1.0
    assert space_service.downloads_blocked() is True

    # space frees up: the next check continues downloads by itself
    monkeypatch.setattr(space_service.shutil, "disk_usage", lambda p: _disk(50))
    _reset_cache()
    result = space_service.run_checks()
    assert result["changed"] is True
    assert space_service.status_payload()["local_pause"]["paused"] is False
    _reset_cache()
    assert space_service.downloads_blocked() is False
    assert any("continued automatically" in e["text"] for e in result["events"])


def test_space_check_disabled_clears_the_pause(temp_db, monkeypatch):
    monkeypatch.setattr(space_service.shutil, "disk_usage", lambda p: _disk(1))
    space_service.run_checks()
    assert space_service.downloads_blocked() is True
    repository.update_settings({"space_check_enabled": False})
    space_service.run_checks()
    _reset_cache()
    assert space_service.status_payload()["local_pause"]["paused"] is False
    assert space_service.downloads_blocked() is False


# -- download worker ---------------------------------------------------------

def test_disk_error_stops_downloads_until_space_is_back(temp_db, tmp_path, monkeypatch):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [_video("abcdefghijk")])
    repository.enqueue_downloads(["abcdefghijk"], download_dir=str(tmp_path))

    def no_space(job, settings, progress, should_stop):
        raise OSError("[Errno 28] No space left on device")

    worker = DownloadWorker(downloader=no_space, pacing=False)
    worker.start_queue()
    job = repository.claim_next_download()
    worker.run_job(job)

    item = repository.get_download(job["id"])
    assert item["status"] == "pending" and item["error_kind"] == "disk"
    assert not item["attempts"]                      # waiting for space is not a retry
    assert space_service.status_payload()["local_pause"]["paused"] is True
    assert worker._claim() is None                   # nothing new starts while space is low

    # a space check that finds room continues the queue automatically
    monkeypatch.setattr(space_service, "local_status",
                        lambda: {"ok": True, "enabled": True, "threshold": 5.0, "path": "x",
                                 "total": 100, "used": 10, "free": 90, "free_percent": 90.0})
    space_service.run_checks()
    _reset_cache()
    assert space_service.downloads_blocked() is False
    assert repository.claim_next_download()["id"] == job["id"]   # picked up again


def test_low_space_stops_the_queue_before_downloading(temp_db, tmp_path, monkeypatch):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [_video("abcdefghijk")])
    repository.enqueue_downloads(["abcdefghijk"], download_dir=str(tmp_path))
    worker = DownloadWorker(downloader=None, pacing=False)
    worker.start_queue()
    monkeypatch.setattr(space_service, "local_blocked", lambda: True)
    assert worker._claim() is None


# -- upload worker (Google Drive) --------------------------------------------

def test_drive_quota_stops_uploads_until_space_is_back(temp_db, tmp_path, monkeypatch):
    folder = tmp_path / "dl"
    folder.mkdir()
    path = folder / "a [abcdefghijk].mp4"
    path.write_bytes(b"video")
    repository.upsert_videos(None, [{"video_id": "abcdefghijk", "title": "T", "url": "u",
                                     "channel_name": "Chan", "platform": "youtube"}])
    repository.enqueue_downloads(["abcdefghijk"], download_dir=str(folder))
    item = repository.get_download_by_video("abcdefghijk")
    repository.update_download(item["id"], status="completed", file_path=str(path), file_size=5)
    repository.enqueue_uploads([item["id"]], "F1", "Folder")

    def drive_full(*args, **kwargs):
        raise DriveError("quota", "Your Google Drive storage is full.", pause=True)

    worker = UploadWorker(uploader=drive_full)
    worker.run_job(repository.claim_next_upload())
    assert space_service.uploads_blocked() is True
    assert repository.list_uploads("queue")["items"][0]["status"] == "pending"

    # a check that finds free Drive space resumes uploads automatically
    monkeypatch.setattr(space_service, "drive_status",
                        lambda: {"ok": True, "enabled": True, "connected": True, "threshold": 5.0,
                                 "total": 100, "used": 10, "free": 90, "free_percent": 90.0})
    space_service.run_checks()
    assert space_service.uploads_blocked() is False
    assert worker.is_running() is True


# -- routes ------------------------------------------------------------------

def test_space_routes_and_page(client, csrf):
    status = client.get("/api/space/status").get_json()
    assert {"local", "drive", "local_pause", "drive_pause", "events", "downloads", "uploads"} \
        <= set(status)
    checked = client.post("/api/space/check", headers=csrf).get_json()
    assert checked["checked"] is True and checked["next_check_at"]
    assert client.post("/api/space/check").status_code == 403          # CSRF
    assert client.get("/space").status_code == 200                     # sidebar page


def test_space_settings_validation(client, csrf):
    assert client.put("/api/settings", json={"space_min_free_percent": 99},
                      headers=csrf).status_code == 400
    assert client.put("/api/settings", json={"space_recheck_minutes": 1},
                      headers=csrf).status_code == 400
    resp = client.put("/api/settings", json={"space_min_free_percent": 10,
                                             "space_recheck_minutes": 30,
                                             "space_check_enabled": False}, headers=csrf)
    assert resp.status_code == 200
    settings = resp.get_json()["settings"]
    assert settings["space_min_free_percent"] == 10
    assert settings["space_recheck_minutes"] == 30
    assert settings["space_check_enabled"] is False


# -- auto-download after discovery -------------------------------------------

def test_discovery_calls_on_discovered(temp_db):
    calls = []

    def fake_iter(parsed, settings, is_cancelled=None):
        yield ("source", {"id": "UC9", "kind": "channel", "url": "https://www.youtube.com/@x",
                          "title": "Chan"})
        yield ("batch", "videos", [{"video_id": "abcdefghijk", "title": "V", "url": "u"}])

    worker = DiscoveryWorker(source_iterator=fake_iter, on_discovered=calls.append)
    job_id = repository.create_discovery_job("https://www.youtube.com/@x")
    worker.run_job(repository.get_discovery_job(job_id))
    assert calls == ["UC9"]
    assert repository.get_discovery_job(job_id)["status"] == "completed"


def test_auto_download_queues_everything_and_starts_the_queue(temp_db):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [_video(f"video{i:06d}") for i in range(3)])
    worker = DownloadWorker(downloader=None, pacing=False)
    assert auto_download_source("UC1", worker) == 3
    assert repository.download_counts()["pending"] == 3
    assert worker.is_running() is True                    # downloading starts by itself
    # discovering the same channel again adds nothing new
    assert auto_download_source("UC1", worker) == 0
    assert repository.download_counts()["pending"] == 3
