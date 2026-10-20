"""Download destination: picker endpoint, validation, and per-item folders."""
from pathlib import Path

import pytest

from database import repository
from services import folder_picker
from services.folder_picker import validate_destination
from workers.download_worker import DownloadWorker


# -- validation --------------------------------------------------------------

def test_validate_creates_missing_folder_and_checks_writability(tmp_path):
    target = tmp_path / "ICT Courses" / "nested"
    path, error = validate_destination(str(target))
    assert error is None and Path(path) == target.resolve() and target.is_dir()
    assert list(target.iterdir()) == []          # write-test file was removed


@pytest.mark.parametrize("raw, fragment", [
    ("", "Choose"), ("relative\\folder", "full path"), ("   ", "Choose"),
])
def test_validate_rejects_bad_input(raw, fragment):
    path, error = validate_destination(raw)
    assert path is None and fragment in error


def test_validate_rejects_file_and_missing_drive(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("x")
    assert "file, not a folder" in validate_destination(str(f))[1]
    import os
    if os.name == "nt":
        assert "not available" in validate_destination("Q:\\Nope\\Videos")[1] or \
            Path("Q:\\").exists()


def test_validate_reports_unwritable_folder(tmp_path, monkeypatch):
    def deny(self, data):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(Path, "write_bytes", deny)
    path, error = validate_destination(str(tmp_path))
    assert path is None and "not writable" in error


# -- endpoints ---------------------------------------------------------------

def test_pick_folder_returns_path_without_saving(client, csrf, monkeypatch, tmp_path):
    before = client.get("/api/settings").get_json()["settings"]["download_dir"]
    chosen = str(tmp_path / "E YouTube Downloads")
    monkeypatch.setattr(folder_picker, "pick_folder", lambda initial: chosen)
    body = client.post("/api/settings/pick-folder", headers=csrf).get_json()
    assert body == {"path": chosen, "cancelled": False, "current": before}
    # nothing changes until the user confirms with PUT /api/settings
    assert client.get("/api/settings").get_json()["settings"]["download_dir"] == before
    saved = client.put("/api/settings", json={"download_dir": chosen}, headers=csrf).get_json()
    assert saved["settings"]["download_dir"] == str(Path(chosen).resolve())
    assert Path(chosen).is_dir()


def test_pick_folder_cancel_busy_and_csrf(client, csrf, monkeypatch):
    monkeypatch.setattr(folder_picker, "pick_folder", lambda initial: None)
    assert client.post("/api/settings/pick-folder", headers=csrf).get_json()["cancelled"] is True

    def busy(initial):
        raise folder_picker.PickerBusy("already open")
    monkeypatch.setattr(folder_picker, "pick_folder", busy)
    assert client.post("/api/settings/pick-folder", headers=csrf).status_code == 409
    # protected like every other state-changing endpoint
    assert client.post("/api/settings/pick-folder").status_code == 403
    assert client.post("/api/settings/pick-folder",
                       headers={**csrf, "Origin": "https://evil.example"}).status_code == 403


def test_pick_folder_rejects_foreign_host(app, csrf, monkeypatch):
    monkeypatch.setattr(folder_picker, "pick_folder", lambda initial: "C:\\x")
    app.testing = False
    client = app.test_client()
    resp = client.post("/api/settings/pick-folder", headers={**csrf, "Host": "evil.example"})
    assert resp.status_code == 400


# -- per-item destination ----------------------------------------------------

def _seed():
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [{"video_id": f"v{i:010d}", "title": f"T{i}", "url": "u",
                                      "channel_name": "ICT Chan", "video_type": "short" if i == 1 else "video"}
                                     for i in range(2)])


def test_enqueue_snapshots_folder_and_later_change_does_not_move_it(client, csrf, tmp_path):
    _seed()
    first, second = tmp_path / "D ICT Courses", tmp_path / "Other"
    client.put("/api/settings", json={"download_dir": str(first)}, headers=csrf)
    client.post("/api/downloads/enqueue", json={"video_ids": ["v0000000000"]}, headers=csrf)
    client.put("/api/settings", json={"download_dir": str(second)}, headers=csrf)
    client.post("/api/downloads/enqueue", json={"video_ids": ["v0000000001"]}, headers=csrf)
    assert repository.get_download_by_video("v0000000000")["download_dir"] == str(first.resolve())
    assert repository.get_download_by_video("v0000000001")["download_dir"] == str(second.resolve())


def test_worker_downloads_into_the_items_folder_with_channel_and_shorts_subfolders(temp_db, tmp_path):
    from tests.test_download_service import FakeYDL
    from services import download_service, runtime as runtime_tools
    _seed()
    chosen = tmp_path / "E YouTube Downloads"
    repository.update_settings({"download_dir": str(tmp_path / "default"), "sleep_requests": 0})
    repository.enqueue_downloads(["v0000000000", "v0000000001"], download_dir=str(chosen))
    seen = []

    def downloader(job, settings, progress, should_stop):
        seen.append(settings["download_dir"])
        return download_service.download_video(job, settings, progress, should_stop, ydl_factory=FakeYDL())

    original = runtime_tools.find_ffmpeg
    runtime_tools.find_ffmpeg = lambda: None
    try:
        worker = DownloadWorker(downloader=downloader, pacing=False)
        worker.start_queue()        # the real progress hook stops downloads while paused
        for _ in range(2):
            worker.run_job(repository.claim_next_download())
    finally:
        runtime_tools.find_ffmpeg = original
    assert seen == [str(chosen), str(chosen)]
    video = Path(repository.get_download_by_video("v0000000000")["file_path"])
    short = Path(repository.get_download_by_video("v0000000001")["file_path"])
    assert video.parent == chosen / "ICT Chan"
    assert short.parent == chosen / "ICT Chan" / "Shorts"
    assert not (tmp_path / "default").exists()


def test_redownload_keeps_original_folder_retry_keeps_folder(temp_db, tmp_path):
    _seed()
    old_dir, new_dir = str(tmp_path / "old"), str(tmp_path / "new")
    repository.enqueue_downloads(["v0000000000", "v0000000001"], download_dir=old_dir)
    a = repository.get_download_by_video("v0000000000")
    b = repository.get_download_by_video("v0000000001")
    repository.update_download(a["id"], status="completed", file_path=old_dir + "\\x.mp4")
    repository.update_download(b["id"], status="failed")
    repository.enqueue_downloads(["v0000000000"], redownload=True, download_dir=new_dir)
    assert repository.get_download(a["id"])["download_dir"] == old_dir     # replaced in place
    repository.retry_downloads([b["id"]])
    assert repository.get_download(b["id"])["download_dir"] == old_dir     # retry keeps folder
    repository.update_download(b["id"], status="failed")
    repository.enqueue_downloads(["v0000000001"], download_dir=new_dir)  # re-queued from library
    assert repository.get_download(b["id"])["download_dir"] == new_dir


def test_open_folder_allows_items_own_folder_only(client, csrf, tmp_path):
    _seed()
    item_dir = tmp_path / "picked"
    client.put("/api/settings", json={"download_dir": str(tmp_path / "current")}, headers=csrf)
    repository.enqueue_downloads(["v0000000000"], download_dir=str(item_dir))
    item = repository.get_download_by_video("v0000000000")
    repository.update_download(item["id"], status="completed", file_path=str(item_dir / "ICT Chan" / "gone.mp4"))
    # inside the item's own folder -> allowed (404 only because the file is gone)
    assert client.post("/api/local/open", json={"download_id": item["id"]}, headers=csrf).status_code == 404
    repository.update_download(item["id"], file_path=str(tmp_path / "elsewhere" / "x.mp4"))
    assert client.post("/api/local/open", json={"download_id": item["id"]}, headers=csrf).status_code == 403


# -- in-page folder browser ----------------------------------------------------

def test_folder_list_shows_drives_and_only_subfolders(client, csrf, tmp_path):
    (tmp_path / "ICT Courses").mkdir()
    (tmp_path / "b folder").mkdir()
    (tmp_path / "$Recycle").mkdir()
    (tmp_path / "secret.txt").write_text("not listed")
    data = client.post("/api/folders/list", json={"path": str(tmp_path)}, headers=csrf).get_json()
    assert [f["name"] for f in data["folders"]] == ["b folder", "ICT Courses"]   # no files, no $ folders
    assert data["parent"] == str(tmp_path.parent)
    assert data["drives"] and all(d["path"].endswith(("\\", "/")) for d in data["drives"])
    assert "secret.txt" not in str(data)


def test_folder_list_defaults_to_current_folder_and_validates(client, csrf, tmp_path):
    client.put("/api/settings", json={"download_dir": str(tmp_path)}, headers=csrf)
    data = client.post("/api/folders/list", json={}, headers=csrf).get_json()
    assert data["path"] == str(tmp_path.resolve()) and data["current"] == str(tmp_path.resolve())
    assert client.post("/api/folders/list", json={"path": "relative"}, headers=csrf).status_code == 400
    assert client.post("/api/folders/list", json={"path": str(tmp_path / "missing")}, headers=csrf).status_code == 400


def test_folder_endpoints_require_csrf_and_same_origin(client, csrf, tmp_path):
    assert client.post("/api/folders/list", json={"path": str(tmp_path)}).status_code == 403
    assert client.post("/api/folders/list", json={"path": str(tmp_path)},
                       headers={**csrf, "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/folders/create", json={"parent": str(tmp_path), "name": "x"}).status_code == 403
    assert client.get("/api/folders/list").status_code == 405     # no GET (not readable cross-site)


def test_create_folder_and_reject_bad_names(client, csrf, tmp_path):
    resp = client.post("/api/folders/create", json={"parent": str(tmp_path), "name": "ICT Courses"}, headers=csrf)
    assert resp.status_code == 200 and (tmp_path / "ICT Courses").is_dir()
    for bad in ("..", "a/b", "a\b", "x:y", "CON", "", "trailing.", 'q"uote'):
        r = client.post("/api/folders/create", json={"parent": str(tmp_path), "name": bad}, headers=csrf)
        assert r.status_code == 400, bad
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == ["ICT Courses"]


def test_browse_then_select_saves_only_after_confirmed_put(client, csrf, tmp_path):
    before = client.get("/api/settings").get_json()["settings"]["download_dir"]
    created = client.post("/api/folders/create", json={"parent": str(tmp_path), "name": "Picked"},
                          headers=csrf).get_json()["path"]
    client.post("/api/folders/list", json={"path": created}, headers=csrf)
    assert client.get("/api/settings").get_json()["settings"]["download_dir"] == before
    saved = client.put("/api/settings", json={"download_dir": created}, headers=csrf).get_json()
    assert saved["settings"]["download_dir"] == str(Path(created).resolve())
