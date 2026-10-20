"""Phase 4: Google OAuth, Drive folder choice, manual uploads (all mocked; no network)."""
import json

import httplib2
import pytest
from googleapiclient.errors import HttpError

import config
from database import repository
from services import credentials_store, drive_auth, drive_service
from services.drive_service import DriveError, UploadCancelled
from workers.upload_worker import PAUSE_KEY, UploadWorker


def http_error(status, reason="", message="boom"):
    body = {"error": {"code": status, "message": message, "errors": [{"reason": reason, "message": message}]}}
    return HttpError(httplib2.Response({"status": status}), json.dumps(body).encode(), uri="https://x")


# -- credentials & OAuth -----------------------------------------------------

def test_credential_store_refuses_insecure_backend(monkeypatch):
    import importlib
    import keyring
    real = importlib.reload(credentials_store)          # undo the autouse in-memory patch

    class PlaintextKeyring:
        pass
    monkeypatch.setattr(keyring, "get_keyring", lambda: PlaintextKeyring())
    with pytest.raises(real.CredentialStoreError):
        real.save({"refresh_token": "x"})


def test_credential_file_used_when_configured(monkeypatch, tmp_path):
    import importlib
    real = importlib.reload(credentials_store)
    token = tmp_path / "secrets" / "google-token.json"
    monkeypatch.setenv("YTDL_CREDENTIALS_FILE", str(token))
    real.save({"refresh_token": "x"})
    assert real.load() == {"refresh_token": "x"}
    real.delete()
    assert not token.exists() and real.load() is None


def _client_file(tmp_path, kind="installed"):
    path = tmp_path / "client_secret.json"
    path.write_text(json.dumps({kind: {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec",
                                       "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                                       "token_uri": "https://oauth2.googleapis.com/token",
                                       "redirect_uris": ["http://localhost"]}}))
    return path


def test_client_config_validation(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", tmp_path / "missing.json")
    with pytest.raises(drive_auth.AuthError, match="No OAuth client"):
        drive_auth.load_client_config()
    with pytest.raises(drive_auth.AuthError, match="Desktop app"):
        drive_auth.load_client_config(_client_file(tmp_path, "web"))
    assert "installed" in drive_auth.load_client_config(_client_file(tmp_path))


class FakeCreds:
    def __init__(self, scopes, refresh_token="rt-123"):
        self.granted_scopes = scopes
        self.scopes = scopes
        self.refresh_token = refresh_token
        self.token = "access"
        self.valid = True


class FakeFlow:
    def __init__(self, creds):
        self.credentials = creds
        self.fetched_with = None

    def authorization_url(self, **kw):
        self.kw = kw
        return "https://accounts.google.com/o/oauth2/auth?state=S1", "S1"

    def fetch_token(self, code):
        self.fetched_with = code


def test_oauth_flow_stores_refresh_token_only_in_credential_store(tmp_path, monkeypatch, memory_credentials):
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", _client_file(tmp_path))
    flow = FakeFlow(FakeCreds(drive_auth.SCOPES))
    monkeypatch.setattr(drive_auth.Flow, "from_client_config", classmethod(lambda cls, *a, **k: flow))
    url = drive_auth.start_authorization()
    assert url.startswith("https://accounts.google.com/") and flow.kw["access_type"] == "offline"
    assert flow.kw["include_granted_scopes"] == "false"
    with pytest.raises(drive_auth.AuthError, match="expired or did not come from this app"):
        drive_auth.finish_authorization("WRONG", "code")
    drive_auth.start_authorization()
    drive_auth.finish_authorization("S1", "the-code")
    assert flow.fetched_with == "the-code"
    assert memory_credentials["token"] == {"refresh_token": "rt-123", "scopes": drive_auth.SCOPES}
    assert drive_auth.is_connected()
    # nothing secret written into the data folder
    assert not any("rt-123" in p.read_text(errors="ignore") for p in tmp_path.rglob("*") if p.is_file())


def test_oauth_rejects_missing_drive_permission(tmp_path, monkeypatch, memory_credentials):
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", _client_file(tmp_path))
    flow = FakeFlow(FakeCreds(["openid"]))
    monkeypatch.setattr(drive_auth.Flow, "from_client_config", classmethod(lambda cls, *a, **k: flow))
    drive_auth.start_authorization()
    with pytest.raises(drive_auth.AuthError, match="not granted"):
        drive_auth.finish_authorization("S1", "c")
    assert "token" not in memory_credentials


def test_scope_is_drive_file_only():
    assert drive_auth.SCOPES == ["https://www.googleapis.com/auth/drive.file"]


def test_oauth_callback_errors_redirect_to_settings(client):
    resp = client.get("/oauth2/callback?error=access_denied")
    assert resp.status_code == 302 and "/settings?drive_error=" in resp.headers["Location"]
    resp = client.get("/oauth2/callback?state=forged&code=x")
    assert resp.status_code == 302 and "drive_error" in resp.headers["Location"]


# -- error classification ----------------------------------------------------

@pytest.mark.parametrize("err, kind, retryable, pause", [
    (http_error(403, "storageQuotaExceeded", "The user's Drive storage quota has been exceeded."), "quota", False, True),
    (http_error(401, "authError", "Invalid Credentials"), "auth", False, True),
    (http_error(404, "notFound", "File not found"), "not_found", False, False),
    (http_error(403, "userRateLimitExceeded", "User rate limit exceeded"), "rate_limit", True, False),
    (http_error(429, "", "Too many"), "rate_limit", True, False),
    (http_error(503, "backendError", "Backend Error"), "server", True, False),
    (ConnectionResetError("reset"), "network", True, False),
    (drive_auth.AuthError("not connected"), "auth", False, True),
])
def test_classify_drive_errors(err, kind, retryable, pause):
    info = drive_service.classify_http_error(err)
    assert (info.kind, info.retryable, info.pause) == (kind, retryable, pause)


# -- upload_file with a fake Drive service -----------------------------------

class FakeStatus:
    def __init__(self, n):
        self.resumable_progress = n


class FakeRequest:
    def __init__(self, size, chunks=3, error=None):
        self.size, self.chunks, self.error, self.calls = size, chunks, error, 0

    def next_chunk(self, num_retries=0):
        self.calls += 1
        if self.error and self.calls == 2:
            raise self.error
        if self.calls < self.chunks:
            return FakeStatus(self.size * self.calls // self.chunks), None
        return None, {"id": "drive-file-1", "name": "v.mp4", "size": str(self.size), "webViewLink": "https://drive/x"}


class FakeFiles:
    def __init__(self, existing=None, request=None):
        self.existing, self.request, self.created = existing, request, None
        self.create_options = None

    def list(self, **kw):
        self.query = kw["q"]
        return type("X", (), {"execute": lambda s: {"files": [self.existing] if self.existing else []}})()

    def create(self, body, media_body, **kw):
        self.created = body
        self.create_options = kw
        return self.request


class FakeService:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def _video_file(tmp_path, size=3000):
    path = tmp_path / "Chan" / "2024-01-01 - T [abcdefghijk].mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_upload_reports_progress_and_tags_file(tmp_path):
    path = _video_file(tmp_path)
    files = FakeFiles(request=FakeRequest(3000))
    updates = []
    result = drive_service.upload_file(str(path), "FOLDER1", "abcdefghijk", 7, progress=updates.append,
                                       service=FakeService(files))
    assert result == {"id": "drive-file-1", "name": "v.mp4", "link": "https://drive/x", "size": 3000, "skipped": False}
    assert files.created["parents"] == ["FOLDER1"]
    assert files.create_options["supportsAllDrives"] is True
    assert files.created["appProperties"] == {"ytVideoId": "abcdefghijk", "ytDownloadId": "7"}
    assert [u["progress"] for u in updates] == [0.0, 33.3, 66.7, 100.0]
    assert "appProperties has { key='ytVideoId' and value='abcdefghijk' }" in files.query


def test_get_folder_supports_shared_drives_and_requires_write_access(monkeypatch):
    calls = []

    class Request:
        def __init__(self, result):
            self.result = result

        def execute(self):
            return self.result

    class Files:
        can_add_children = True

        def get(self, **kwargs):
            calls.append(kwargs)
            return Request({
                "id": kwargs["fileId"],
                "name": "Shared folder",
                "mimeType": drive_service.FOLDER_MIME,
                "trashed": False,
                "capabilities": {"canAddChildren": self.can_add_children},
            })

    files = Files()
    monkeypatch.setattr(drive_service, "_service", lambda credentials=None: FakeService(files))

    folder = drive_service.get_folder("SHARED_FOLDER_ID")

    assert folder == {"id": "SHARED_FOLDER_ID", "name": "Shared folder", "link": None}
    assert calls[0]["supportsAllDrives"] is True
    files.can_add_children = False
    with pytest.raises(DriveError) as exc:
        drive_service.get_folder("SHARED_FOLDER_ID")
    assert exc.value.kind == "forbidden"


def test_upload_skips_file_already_in_folder(tmp_path):
    path = _video_file(tmp_path)
    files = FakeFiles(existing={"id": "old", "name": "v.mp4", "size": "3000", "webViewLink": "L"}, request=None)
    result = drive_service.upload_file(str(path), "F", "abcdefghijk", 1, service=FakeService(files))
    assert result["skipped"] is True and result["id"] == "old" and files.created is None


def test_upload_missing_file_and_cancel_and_http_error(tmp_path):
    with pytest.raises(DriveError) as exc:
        drive_service.upload_file(str(tmp_path / "nope.mp4"), "F", "abcdefghijk", 1, service=FakeService(FakeFiles()))
    assert exc.value.kind == "local_file"
    path = _video_file(tmp_path)
    with pytest.raises(UploadCancelled):
        drive_service.upload_file(str(path), "F", "abcdefghijk", 1, should_stop=lambda: "cancel",
                                  service=FakeService(FakeFiles(request=FakeRequest(3000))))
    quota = FakeRequest(3000, error=http_error(403, "storageQuotaExceeded", "storage quota has been exceeded"))
    with pytest.raises(DriveError) as exc:
        drive_service.upload_file(str(path), "F", "abcdefghijk", 1, service=FakeService(FakeFiles(request=quota)))
    assert exc.value.kind == "quota"


# -- upload queue ------------------------------------------------------------

def _completed_download(tmp_path, vid="abcdefghijk", status="completed"):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [{"video_id": vid, "title": "T", "url": "u", "channel_name": "Chan"}])
    repository.enqueue_downloads([vid], download_dir=str(tmp_path))
    d = repository.get_download_by_video(vid)
    path = _video_file(tmp_path)
    repository.update_download(d["id"], status=status, file_path=str(path), file_size=3000)
    return repository.get_download(d["id"])


def test_enqueue_uploads_dedupes_per_folder(temp_db, tmp_path):
    d = _completed_download(tmp_path)
    assert repository.enqueue_uploads([d["id"]], "F1", "Folder 1")["added"] == 1
    assert repository.enqueue_uploads([d["id"]], "F1", "Folder 1")["already_queued"] == 1
    assert repository.enqueue_uploads([d["id"]], "F2", "Folder 2")["added"] == 1      # another folder is fine
    upload = repository.claim_next_upload()
    repository.update_upload(upload["id"], status="completed")
    assert repository.enqueue_uploads([d["id"]], "F1")["already_uploaded"] == 1
    assert repository.enqueue_uploads([99999], "F1")["missing"] == 1


def test_only_completed_downloads_can_be_uploaded(temp_db, tmp_path):
    d = _completed_download(tmp_path, status="failed")
    assert repository.enqueue_uploads([d["id"]], "F1")["not_downloaded"] == 1


def _fake_uploader(calls, error=None):
    def upload(path, folder_id, video_id, download_id, progress, should_stop):
        calls.append((path, folder_id, video_id))
        if error:
            raise error
        progress({"progress": 50.0, "uploaded_bytes": 1500, "total_bytes": 3000})
        return {"id": "DRIVE-ID", "name": "v.mp4", "link": "https://drive/x", "size": 3000, "skipped": False}
    return upload


def test_upload_success_saves_drive_id_and_leaves_download_untouched(temp_db, tmp_path):
    d = _completed_download(tmp_path)
    repository.enqueue_uploads([d["id"]], "F1", "Folder 1")
    calls = []
    UploadWorker(uploader=_fake_uploader(calls)).run_job(repository.claim_next_upload())
    up = repository.list_uploads("completed")["items"][0]
    assert up["drive_file_id"] == "DRIVE-ID" and up["web_link"] == "https://drive/x"
    assert calls == [(d["file_path"], "F1", "abcdefghijk")]
    assert repository.get_download(d["id"]) == d                     # download row unchanged


@pytest.mark.parametrize("error, expected_status, paused", [
    (DriveError("quota", "Drive full", pause=True), "pending", True),
    (DriveError("auth", "sign in again", pause=True), "pending", True),
    (DriveError("network", "flaky", retryable=True), "pending", False),
    (DriveError("not_found", "folder gone"), "failed", False),
    (RuntimeError("unexpected bug"), "pending", False),
])
def test_upload_failures_never_mark_download_failed(temp_db, tmp_path, error, expected_status, paused):
    d = _completed_download(tmp_path)
    repository.enqueue_uploads([d["id"]], "F1")
    worker = UploadWorker(uploader=_fake_uploader([], error))
    worker.run_job(repository.claim_next_upload())
    up = repository.list_uploads("queue" if expected_status == "pending" else "failed")["items"][0]
    assert up["status"] == expected_status
    assert bool(repository.get_state(PAUSE_KEY)) is paused
    assert repository.get_download(d["id"])["status"] == "completed"   # the key guarantee
    if paused:
        assert worker.is_running() is False
        worker.start_queue()
        assert worker.is_running() is True


def test_retryable_upload_error_backs_off_then_fails(temp_db, tmp_path):
    repository.update_settings({"max_retries": 1})
    d = _completed_download(tmp_path)
    repository.enqueue_uploads([d["id"]], "F1")
    worker = UploadWorker(uploader=_fake_uploader([], DriveError("server", "503", retryable=True)))
    worker.run_job(repository.claim_next_upload())
    up = repository.list_uploads("queue")["items"][0]
    assert up["attempts"] == 1 and up["next_attempt_at"] and "Retry 1/1" in up["error_message"]
    repository.update_upload(up["id"], next_attempt_at=None)
    worker.run_job(repository.claim_next_upload())
    assert repository.list_uploads("failed")["items"][0]["attempts"] == 2


def test_cancel_and_restart_recovery(temp_db, tmp_path):
    d = _completed_download(tmp_path)
    repository.enqueue_uploads([d["id"]], "F1")
    worker = UploadWorker()
    up = repository.claim_next_upload()                 # simulate crash mid-upload
    worker.start()
    try:
        assert repository.get_upload(up["id"])["status"] == "pending"
    finally:
        worker.stop()
    assert worker.cancel(up["id"]) and repository.get_upload(up["id"])["status"] == "cancelled"
    assert repository.retry_uploads([up["id"]]) == 1


# -- routes ------------------------------------------------------------------

def test_upload_enqueue_requires_folder_and_connection(client, csrf, tmp_path, memory_credentials):
    d = _completed_download(tmp_path)
    r = client.post("/api/uploads/enqueue", json={"download_ids": [d["id"]]}, headers=csrf)
    assert r.status_code == 400 and "folder" in r.get_json()["error"]
    repository.update_settings({"drive_folder_id": "F1", "drive_folder_name": "Folder 1"})
    r = client.post("/api/uploads/enqueue", json={"download_ids": [d["id"]]}, headers=csrf)
    assert r.status_code == 400 and "Connect" in r.get_json()["error"]
    memory_credentials["token"] = {"refresh_token": "x"}
    r = client.post("/api/uploads/enqueue", json={"download_ids": [d["id"]]}, headers=csrf)
    assert r.status_code == 200 and r.get_json()["added"] == 1
    assert client.post("/api/uploads/enqueue", json={"download_ids": "x"}, headers=csrf).status_code == 400
    assert client.post("/api/uploads/enqueue", json={"download_ids": [d["id"]]}).status_code == 403   # CSRF


def test_set_drive_folder_verifies_access(client, csrf, monkeypatch):
    monkeypatch.setattr(drive_service, "get_folder", lambda fid: {"id": fid, "name": "ICT Videos", "link": "L"})
    r = client.put("/api/drive/folder", json={"id": "1AbCdEfGhIj"}, headers=csrf)
    assert r.status_code == 200
    assert repository.get_settings()["drive_folder_name"] == "ICT Videos"

    def denied(fid):
        raise DriveError("not_found", "not given access")
    monkeypatch.setattr(drive_service, "get_folder", denied)
    assert client.put("/api/drive/folder", json={"id": "1Other12345"}, headers=csrf).status_code == 404
    assert repository.get_settings()["drive_folder_id"] == "1AbCdEfGhIj"      # unchanged on failure
    assert client.put("/api/drive/folder", json={"id": "bad id!"}, headers=csrf).status_code == 400


def test_picker_config_needs_key_and_returns_token(client, csrf, monkeypatch):
    assert client.post("/api/drive/picker-config", headers=csrf).status_code == 400
    client.put("/api/settings", json={"google_api_key": "test-google-picker-api-key-123456",
                                      "google_project_number": "123456789012"}, headers=csrf)
    monkeypatch.setattr(drive_auth, "access_token", lambda: "short-lived")
    body = client.post("/api/drive/picker-config", headers=csrf).get_json()
    assert body == {"token": "short-lived", "api_key": "test-google-picker-api-key-123456", "app_id": "123456789012"}
    assert client.post("/api/drive/picker-config").status_code == 403         # CSRF
    assert client.get("/api/drive/picker-config").status_code == 405


def test_google_settings_validation(client, csrf):
    assert client.put("/api/settings", json={"google_project_number": "my-project-id"}, headers=csrf).status_code == 400
    assert client.put("/api/settings", json={"google_api_key": "short"}, headers=csrf).status_code == 400


def test_drive_status_shape(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", tmp_path / "none.json")
    s = client.get("/api/drive/status").get_json()
    assert s["configured"] is False and "No OAuth client" in s["config_error"]
    assert s["connected"] is False and s["redirect_uri"].endswith("/oauth2/callback")


def test_picker_csp_only_on_settings_page(client):
    settings_csp = client.get("/settings").headers["Content-Security-Policy"]
    assert "https://apis.google.com" in settings_csp and "frame-src https://docs.google.com" in settings_csp
    for page in ("/", "/library", "/local", "/uploads"):
        csp = client.get(page).headers["Content-Security-Policy"]
        assert "apis.google.com" not in csp and "script-src 'self';" in csp


# -- secrets from .env ---------------------------------------------------------

def test_env_file_parsing(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# comment\nGOOGLE_API_KEY="test-google-picker-key-quoted"\nEMPTY=\nBAD LINE\n'
                   "GOOGLE_PROJECT_NUMBER=123456789012\n", encoding="utf-8")
    monkeypatch.delenv("EMPTY", raising=False)
    loaded = config.load_env_file(env)
    assert loaded["GOOGLE_API_KEY"] == "test-google-picker-key-quoted"
    assert loaded["GOOGLE_PROJECT_NUMBER"] == "123456789012" and "BAD LINE" not in loaded
    assert config.load_env_file(tmp_path / "missing.env") == {}


def test_env_values_take_precedence_for_picker(client, csrf, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-google-picker-key-from-env")
    monkeypatch.setenv("GOOGLE_PROJECT_NUMBER", "873303605942")
    monkeypatch.setattr(drive_auth, "access_token", lambda: "tok")
    body = client.post("/api/drive/picker-config", headers=csrf).get_json()
    assert body["api_key"] == "test-google-picker-key-from-env" and body["app_id"] == "873303605942"
    status = client.get("/api/drive/status").get_json()
    assert status["picker_ready"] and status["picker_from_env"]
    # never echoed back by the settings API
    assert "test-google-picker-key-from-env" not in client.get("/api/settings").get_data(as_text=True)


def test_oauth_client_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", tmp_path / "none.json")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    with pytest.raises(drive_auth.AuthError, match="both"):
        drive_auth.load_client_config()
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "GOCSPX-test")
    cfg = drive_auth.load_client_config()["installed"]
    assert cfg["client_id"] == "123-abc.apps.googleusercontent.com" and cfg["client_secret"] == "GOCSPX-test"
    assert drive_auth.client_source() == "env"
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "not-a-google-id")
    with pytest.raises(drive_auth.AuthError, match="apps.googleusercontent.com"):
        drive_auth.load_client_config()


def test_oauth_client_from_env_extracts_matching_desktop_json(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", tmp_path / "none.json")
    client_json = {
        "installed": {
            "client_id": "123-abc.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret",
        }
    }
    monkeypatch.setenv("GOOGLE_CLIENT_ID", client_json["installed"]["client_id"])
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", json.dumps(client_json))

    cfg = drive_auth.load_client_config()["installed"]

    assert cfg["client_id"] == client_json["installed"]["client_id"]
    assert cfg["client_secret"] == client_json["installed"]["client_secret"]


def test_oauth_client_from_env_rejects_mismatched_json_client(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CLIENT_SECRET_PATH", tmp_path / "none.json")
    client_json = {
        "installed": {
            "client_id": "other.apps.googleusercontent.com",
            "client_secret": "GOCSPX-secret",
        }
    }
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", json.dumps(client_json))

    with pytest.raises(drive_auth.AuthError, match="different OAuth clients"):
        drive_auth.load_client_config()
