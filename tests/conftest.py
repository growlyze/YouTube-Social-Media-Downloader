import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app  # noqa: E402
from database import db  # noqa: E402


@pytest.fixture(autouse=True)
def no_real_google_env(monkeypatch):
    """Tests must not see the real values loaded from the project's .env."""
    for name in ("GOOGLE_API_KEY", "GOOGLE_PROJECT_NUMBER", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def memory_credentials(monkeypatch):
    """Never touch the real Windows Credential Manager in tests."""
    from services import credentials_store, drive_auth
    store = {}
    monkeypatch.setattr(credentials_store, "save", lambda data: store.__setitem__("token", dict(data)))
    monkeypatch.setattr(credentials_store, "load", lambda: store.get("token"))
    monkeypatch.setattr(credentials_store, "delete", lambda: store.pop("token", None))
    drive_auth.forget_cached()
    yield store
    drive_auth.forget_cached()


@pytest.fixture
def temp_db(tmp_path):
    db.init_db(tmp_path / "test.db")
    return tmp_path / "test.db"


class FakeWorker:
    """Stands in for DiscoveryWorker in HTTP tests (no background thread)."""

    def __init__(self):
        from database import repository
        self.repo = repository
        self.submitted = []

    def start(self):
        pass

    def submit(self, url):
        job_id = self.repo.create_discovery_job(url)
        self.submitted.append(job_id)
        return job_id

    def cancel(self, job_id):
        job = self.repo.get_discovery_job(job_id)
        if not job or job["status"] not in ("pending", "running"):
            return False
        self.repo.update_discovery_job(job_id, status="cancelled")
        return True

    def wake(self):
        pass


@pytest.fixture
def app(tmp_path):
    application = create_app(db_path=tmp_path / "app.db", start_workers=False,
                             discovery_worker=FakeWorker())
    application.testing = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def csrf(app):
    return {"X-CSRF-Token": app.config["CSRF_TOKEN"]}
