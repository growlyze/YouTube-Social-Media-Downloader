from database import repository


def _seed():
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [
        {"video_id": f"v{i:010d}", "title": f"T{i}", "url": "https://u", "channel_name": "Chan"}
        for i in range(4)])


def test_enqueue_from_selection_and_deselect(client, csrf):
    _seed()
    repository.set_selected(["v0000000000", "v0000000001"], True)
    resp = client.post("/api/downloads/enqueue", json={"from_selection": True, "filters": {"source": "UC1"}},
                       headers=csrf)
    body = resp.get_json()
    assert resp.status_code == 200 and body["added"] == 2 and body["queue_running"] is False
    assert repository.count_videos()["selected"] == 0
    again = client.post("/api/downloads/enqueue", json={"video_ids": ["v0000000000"]}, headers=csrf).get_json()
    assert again["already_queued"] == 1
    assert client.post("/api/downloads/enqueue", json={"from_selection": True}, headers=csrf).status_code == 400
    assert client.post("/api/downloads/enqueue", json={"video_ids": "x"}, headers=csrf).status_code == 400
    lib = client.get("/api/videos?download=queued").get_json()
    assert lib["total"] == 2 and lib["items"][0]["download_status"] == "pending"


def test_status_and_queue_controls(client, csrf):
    _seed()
    status = client.get("/api/downloads/status").get_json()
    assert status["queue"]["running"] is False
    assert "ffmpeg" in status["runtime"] and "js_runtime" in status["runtime"]
    assert client.post("/api/downloads/queue/start", headers=csrf).get_json()["queue"]["running"] is True
    assert client.post("/api/downloads/queue/pause", headers=csrf).get_json()["queue"]["running"] is False
    assert client.post("/api/downloads/queue/start").status_code == 403   # CSRF still enforced


def test_cancel_retry_remove(client, csrf):
    _seed()
    client.post("/api/downloads/enqueue", json={"video_ids": ["v0000000000"]}, headers=csrf)
    item = client.get("/api/downloads?view=queue").get_json()["items"][0]
    assert client.post(f"/api/downloads/{item['id']}/retry", headers=csrf).status_code == 409
    assert client.post(f"/api/downloads/{item['id']}/cancel", headers=csrf).status_code == 200
    assert client.get("/api/downloads?view=failed").get_json()["total"] == 1
    assert client.post(f"/api/downloads/{item['id']}/retry", headers=csrf).status_code == 200
    assert client.get("/api/downloads?view=queue").get_json()["total"] == 1
    assert client.delete(f"/api/downloads/{item['id']}", headers=csrf).status_code == 200
    assert client.get("/api/downloads?view=queue").get_json()["total"] == 0
    assert client.get("/api/downloads?view=bogus").status_code == 400


def test_local_files_and_open_folder_validation(client, csrf, tmp_path):
    _seed()
    dl = tmp_path / "dl"
    client.put("/api/settings", json={"download_dir": str(dl)}, headers=csrf)
    client.post("/api/downloads/enqueue", json={"video_ids": ["v0000000000", "v0000000001"]}, headers=csrf)
    inside = repository.get_download_by_video("v0000000000")
    outside = repository.get_download_by_video("v0000000001")
    repository.update_download(inside["id"], status="completed", file_path=str(dl / "Chan" / "gone.mp4"),
                               file_size=5)
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    repository.update_download(outside["id"], status="completed", file_path=str(secret), file_size=1)
    data = client.get("/api/local").get_json()
    assert data["total"] == 2
    assert all(i["file_exists"] is False or i["file_path"] == str(secret) for i in data["items"])
    assert client.post("/api/local/open", json={"download_id": outside["id"]}, headers=csrf).status_code == 403
    assert client.post("/api/local/open", json={"download_id": inside["id"]}, headers=csrf).status_code == 404
    assert client.post("/api/local/open", json={"download_id": 99999}, headers=csrf).status_code == 404


def test_new_pages_render(client):
    for path in ("/downloads", "/local", "/history"):
        resp = client.get(path)
        assert resp.status_code == 200, path
