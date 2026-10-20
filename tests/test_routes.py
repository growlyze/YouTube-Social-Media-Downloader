from database import repository


def _seed():
    repository.upsert_source("UC1", "channel", "https://www.youtube.com/channel/UC1", title="Chan")
    repository.upsert_videos("UC1", [
        {"video_id": f"v{i:010d}", "title": f"<b>Video {i}</b>", "url": "https://u", "duration": 60 * i,
         "video_type": "short" if i % 3 == 0 else "video", "upload_date": "20240101"}
        for i in range(1, 11)
    ])


def test_pages_render(client):
    for path in ("/", "/library", "/settings", "/downloads", "/local", "/history", "/uploads"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert b'name="csrf-token"' in resp.data
    assert client.get("/nope").status_code == 404
    downloads = client.get("/downloads").data
    assert b"media-url-form" in downloads and b"Instagram" in downloads and b"TikTok" in downloads
    assert b"Upload selected to Drive" in client.get("/local").data
    assert client.get("/api/downloads/status").status_code == 200
    assert client.get("/api/local").status_code == 200


def test_security_headers(client):
    resp = client.get("/")
    assert "script-src 'self'" in resp.headers["Content-Security-Policy"]
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_post_requires_csrf_token(client, csrf):
    assert client.post("/api/discover", json={"url": "@chan"}).status_code == 403
    assert client.post("/api/discover", json={"url": "@chan"},
                       headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/discover", json={"url": "@chan"}, headers=csrf).status_code == 202


def test_cross_origin_post_blocked(client, csrf):
    resp = client.post("/api/discover", json={"url": "@chan"},
                       headers={**csrf, "Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_host_header_checked_outside_tests(app, csrf):
    app.testing = False
    client = app.test_client()
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1:5000"}).status_code == 200


def test_discover_validates_input(client, csrf):
    resp = client.post("/api/discover", json={"url": "https://example.com/video"}, headers=csrf)
    assert resp.status_code == 400
    assert "youtube" in resp.get_json()["error"].lower()


def test_discover_job_lifecycle(client, csrf):
    job = client.post("/api/discover", json={"url": "@chan"}, headers=csrf).get_json()["job"]
    assert job["status"] == "pending"
    status = client.get(f"/api/discover/{job['id']}").get_json()
    assert status["job"]["input_url"] == "@chan"
    assert client.post(f"/api/discover/{job['id']}/cancel", headers=csrf).status_code == 200
    assert client.post(f"/api/discover/{job['id']}/cancel", headers=csrf).status_code == 409


def test_video_listing_filters_and_selection(client, csrf):
    _seed()
    data = client.get("/api/videos?source=UC1&type=short&page_size=2").get_json()
    assert data["total"] == 3 and len(data["items"]) == 2 and data["pages"] == 2
    data = client.get("/api/videos?max_minutes=3").get_json()
    assert data["total"] == 3
    assert client.get("/api/videos?date_from=bad").status_code == 400
    assert client.get("/api/videos?page_size=abc").status_code == 400

    resp = client.post("/api/videos/select", json={"all_matching": True, "filters": {"type": "short"},
                                                     "selected": True}, headers=csrf)
    assert resp.get_json()["changed"] == 3
    resp = client.post("/api/videos/select", json={"ids": ["v0000000001"], "selected": True}, headers=csrf)
    assert resp.get_json()["changed"] == 1
    assert client.get("/api/videos?selected_only=1").get_json()["total"] == 4
    assert client.post("/api/videos/select", json={"ids": "nope"}, headers=csrf).status_code == 400

    sources = client.get("/api/sources").get_json()["sources"]
    assert sources[0]["total"] == 10 and sources[0]["selected"] == 4 and sources[0]["shorts"] == 3


def test_delete_source(client, csrf):
    _seed()
    assert client.delete("/api/sources/UC1", headers=csrf).status_code == 200
    assert client.get("/api/videos").get_json()["total"] == 0
    assert client.delete("/api/sources/UC1", headers=csrf).status_code == 404


def test_settings_validation(client, csrf, tmp_path):
    resp = client.put("/api/settings", json={"sleep_requests": 3, "download_dir": str(tmp_path / "vids")},
                      headers=csrf)
    assert resp.status_code == 200
    assert resp.get_json()["settings"]["sleep_requests"] == 3
    assert (tmp_path / "vids").is_dir()

    for bad in ({"sleep_requests": -1}, {"video_quality": "8k"}, {"download_dir": "relative\\path"},
                {"max_concurrent_downloads": 50}, {"sleep_interval_min": 30, "sleep_interval_max": 10}):
        assert client.put("/api/settings", json=bad, headers=csrf).status_code == 400, bad


def test_auto_upload_requires_drive_setup(client, csrf, monkeypatch):
    from services import drive_auth

    monkeypatch.setattr(drive_auth, "is_connected", lambda: False)
    response = client.put("/api/settings", json={"auto_upload": True}, headers=csrf)
    assert response.status_code == 400
    assert "folder" in response.get_json()["error"].lower()
    assert "connect" in response.get_json()["error"].lower()

    repository.update_settings({"drive_folder_id": "folder-123"})
    monkeypatch.setattr(drive_auth, "is_connected", lambda: True)
    response = client.put("/api/settings", json={"auto_upload": True}, headers=csrf)
    assert response.status_code == 200
    assert response.get_json()["settings"]["auto_upload"] is True


def test_resume_clears_pause(client, csrf):
    from services import queue_control
    queue_control.pause("rate_limited", "slow", cooldown_minutes=10)
    assert client.get("/api/queue/pause").get_json()["paused"] is True
    assert client.post("/api/queue/resume", headers=csrf).get_json()["paused"] is False


def test_docker_mode_pins_download_folder(client, csrf, monkeypatch, tmp_path):
    import config
    monkeypatch.setattr(config, "IN_DOCKER", True)
    monkeypatch.setattr(config, "DOWNLOADS_DIR", tmp_path / "mounted")
    monkeypatch.setattr(config, "DOWNLOADS_LABEL", "D:/Videos")
    resp = client.put("/api/settings", json={"download_dir": str(tmp_path / "elsewhere")}, headers=csrf)
    assert resp.status_code == 200
    assert resp.get_json()["settings"]["download_dir"] == str(tmp_path / "mounted")
    assert client.post("/api/settings/pick-folder", headers=csrf).status_code == 501
    assert client.post("/api/folders/list", json={}, headers=csrf).status_code == 501
    page = client.get("/settings").data
    assert b"D:/Videos" in page and b"js-choose-folder" not in page
