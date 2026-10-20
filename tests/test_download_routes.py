from database import repository
from routes import downloads as download_routes


def _media(video_id, platform, url):
    return {
        "video_id": video_id, "external_id": video_id, "platform": platform,
        "title": f"{platform} media", "channel_name": platform.title(), "url": url,
        "video_type": "video", "availability": "public",
    }


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
    assert b"media-url-form" in client.get("/downloads").data
    assert b"Instagram" in client.get("/downloads").data and b"TikTok" in client.get("/downloads").data


def test_media_url_route_resolves_and_queues_multiple_platforms(client, csrf, monkeypatch):
    def resolve(url, settings):
        if "instagram" in url:
            item = _media("instagram_123", "instagram", url)
        else:
            item = _media("abcdefghijk", "youtube", url)
        return {"items": [item], "truncated": False, "platform": item["platform"]}

    monkeypatch.setattr(download_routes, "resolve_media_url", resolve)
    urls = ["https://instagram.com/reel/123/", "https://youtube.com/watch?v=abcdefghijk"]
    response = client.post("/api/downloads/enqueue-url", json={"urls": urls}, headers=csrf)
    body = response.get_json()
    assert response.status_code == 200
    assert body["added"] == 2 and body["expanded"] == 2 and body["errors"] == []
    assert repository.get_download_by_video("instagram_123")["platform"] == "instagram"
    assert repository.get_download_by_video("abcdefghijk")["platform"] == "youtube"

    repeated = client.post("/api/downloads/enqueue-url", json={"urls": urls}, headers=csrf).get_json()
    assert repeated["already_queued"] == 2 and repeated["added"] == 0


def test_media_url_route_reports_unsupported_and_login_required_links(client, csrf, monkeypatch):
    unsupported = client.post("/api/downloads/enqueue-url",
                              json={"urls": ["https://example.com/watch?v=1"]}, headers=csrf)
    assert unsupported.status_code == 200
    assert unsupported.get_json()["errors"][0]["message"].startswith("Unsupported platform")

    import yt_dlp

    def private_media(url, settings):
        raise yt_dlp.utils.DownloadError("ERROR: This Instagram account is private")

    monkeypatch.setattr(download_routes, "resolve_media_url", private_media)
    private = client.post("/api/downloads/enqueue-url",
                         json={"urls": ["https://instagram.com/reel/123/"]}, headers=csrf)
    assert private.status_code == 200
    assert private.get_json()["errors"][0]["message"] == "Private video or account content cannot be downloaded."


def test_media_url_route_validates_batch_size_and_csrf(client, csrf):
    too_many = client.post("/api/downloads/enqueue-url", json={"urls": ["https://x.com"] * 21},
                           headers=csrf)
    assert too_many.status_code == 400
    assert client.post("/api/downloads/enqueue-url", json={"urls": ["https://x.com"]}).status_code == 403
    assert client.post("/api/downloads/enqueue-url", json=["https://x.com"], headers=csrf).status_code == 400


def test_rate_limit_stops_inspecting_remaining_urls(client, csrf, monkeypatch):
    import yt_dlp

    calls = []

    def rate_limited(url, settings):
        calls.append(url)
        raise yt_dlp.utils.DownloadError("HTTP Error 429: Too Many Requests")

    monkeypatch.setattr(download_routes, "resolve_media_url", rate_limited)
    response = client.post("/api/downloads/enqueue-url",
                           json={"urls": ["https://instagram.com/p/1/", "https://x.com/user/status/2"]},
                           headers=csrf)
    body = response.get_json()
    assert len(calls) == 1
    assert body["errors"][0]["message"].startswith("The platform is rate-limiting")
    assert body["warnings"] and "1 URL(s) were not inspected" in body["warnings"][0]
    assert repository.get_state("youtube_pause")["kind"] == "rate_limited"
