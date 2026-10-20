"""Google OAuth 2.0 for Drive (Desktop-app client, loopback redirect, PKCE).

Scope: drive.file only. The app can see/modify files it created and folders
the user explicitly picks with the Google Picker (or that the app created);
it cannot read the rest of the user's Drive.

Flow:
  1. start_authorization() -> Google consent URL (state + PKCE verifier kept in memory).
  2. Google redirects the browser to http://127.0.0.1:<port>/oauth2/callback.
  3. finish_authorization(state, code) exchanges the code; the refresh token is
     stored in Windows Credential Manager (credentials_store), never in a file.
"""

import json
import logging
import threading
import time

import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

import config
from services import credentials_store

log = logging.getLogger("drive-auth")

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_FLOW_TTL = 600

_pending: dict[str, tuple[float, Flow]] = {}
_lock = threading.Lock()
_cached: Credentials | None = None


class AuthError(Exception):
    """Drive is not configured/connected, or Google rejected the sign-in."""


def redirect_uri() -> str:
    return f"http://127.0.0.1:{config.PORT}/oauth2/callback"


def _client_from_env() -> dict | None:
    """Desktop-app client from GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET in .env."""
    client_id = config.google_secret("GOOGLE_CLIENT_ID")
    client_secret = config.google_secret("GOOGLE_CLIENT_SECRET")
    if not (client_id or client_secret):
        return None
    if not (client_id and client_secret):
        raise AuthError("Set both GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env.")
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise AuthError("GOOGLE_CLIENT_ID in .env should end with .apps.googleusercontent.com.")
    try:
        client_json = json.loads(client_secret)
    except json.JSONDecodeError:
        client_json = None
    if client_json is not None:
        if not isinstance(client_json, dict) or not isinstance(client_json.get("installed"), dict):
            raise AuthError("GOOGLE_CLIENT_SECRET contains JSON, but it is not a Desktop app client.")
        installed = client_json["installed"]
        if installed.get("client_id") != client_id:
            raise AuthError("GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET belong to different OAuth clients.")
        client_secret = installed.get("client_secret", "")
        if not client_secret:
            raise AuthError("The Desktop app client JSON in GOOGLE_CLIENT_SECRET has no client secret.")
    return {"installed": {
        "client_id": client_id, "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://127.0.0.1", "http://localhost"],
    }}


def client_source() -> str:
    """Where the OAuth client comes from: 'env', 'file' or 'none'."""
    if config.google_secret("GOOGLE_CLIENT_ID") or config.google_secret("GOOGLE_CLIENT_SECRET"):
        return "env"
    return "file" if config.CLIENT_SECRET_PATH.exists() else "none"


def load_client_config(path=None) -> dict:
    """OAuth client: GOOGLE_CLIENT_ID/SECRET from .env, else data/client_secret.json.
    Must be a *Desktop app* client."""
    if path is None:
        from_env = _client_from_env()
        if from_env:
            return from_env
    path = path or config.CLIENT_SECRET_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AuthError("No OAuth client yet. Put GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET of a "
                        f"'Desktop app' client in .env (or save its JSON as {path}).")
    except (OSError, ValueError) as exc:
        raise AuthError(f"Could not read {path.name}: {exc}")
    if "installed" not in data:
        kind = next(iter(data), "unknown")
        raise AuthError(f"{path.name} is a '{kind}' client. Create an OAuth client of type "
                        "'Desktop app' and download its JSON.")
    client = data["installed"]
    for key in ("client_id", "client_secret", "auth_uri", "token_uri"):
        if not client.get(key):
            raise AuthError(f"{path.name} is missing '{key}'.")
    return data


def is_configured() -> bool:
    try:
        load_client_config()
        return True
    except AuthError:
        return False


def start_authorization() -> str:
    client = load_client_config()
    flow = Flow.from_client_config(client, scopes=SCOPES, redirect_uri=redirect_uri(),
                                   autogenerate_code_verifier=True)
    url, state = flow.authorization_url(access_type="offline", prompt="consent",
                                        include_granted_scopes="false")
    now = time.monotonic()
    with _lock:
        for key in [k for k, (t, _) in _pending.items() if now - t > _FLOW_TTL]:
            _pending.pop(key, None)
        _pending[state] = (now, flow)
    return url


def finish_authorization(state: str, code: str) -> Credentials:
    global _cached
    with _lock:
        entry = _pending.pop(state or "", None)
    if not entry or time.monotonic() - entry[0] > _FLOW_TTL:
        raise AuthError("This sign-in link expired or did not come from this app. Click Connect again.")
    flow = entry[1]
    try:
        flow.fetch_token(code=code)
    except Exception as exc:  # oauthlib / requests errors
        raise AuthError(f"Google did not accept the sign-in: {type(exc).__name__}: {exc}")
    creds = flow.credentials
    granted = set(creds.granted_scopes or creds.scopes or [])
    if not set(SCOPES) <= granted:
        raise AuthError("Google Drive access was not granted. Tick the Drive permission on the consent screen.")
    if not creds.refresh_token:
        raise AuthError("Google did not return a refresh token. Remove the app's access at "
                        "myaccount.google.com/permissions and connect again.")
    credentials_store.save({"refresh_token": creds.refresh_token, "scopes": sorted(granted)})
    with _lock:
        _cached = creds
    log.info("Google Drive connected")
    return creds


def get_credentials() -> Credentials:
    """Valid credentials (refreshing the access token when needed)."""
    global _cached
    with _lock:
        creds = _cached
        if creds is None:
            stored = credentials_store.load()
            if not stored or not stored.get("refresh_token"):
                raise AuthError("Google Drive is not connected.")
            client = load_client_config()["installed"]
            creds = Credentials(token=None, refresh_token=stored["refresh_token"],
                                token_uri=client["token_uri"], client_id=client["client_id"],
                                client_secret=client["client_secret"], scopes=stored.get("scopes") or SCOPES)
        if not creds.valid:
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                _cached = None
                raise AuthError("Google rejected the saved sign-in (it may have expired or been revoked). "
                                f"Connect Google Drive again. ({exc.args[0] if exc.args else 'refresh failed'})")
        _cached = creds
        return creds


def access_token() -> str:
    """Short-lived access token, used by the Google Picker in the page."""
    return get_credentials().token


def is_connected() -> bool:
    return bool(credentials_store.load())


def disconnect():
    """Revoke the token at Google (best effort) and remove it from this PC."""
    global _cached
    stored = credentials_store.load()
    if stored and stored.get("refresh_token"):
        try:
            requests.post("https://oauth2.googleapis.com/revoke",
                          params={"token": stored["refresh_token"]}, timeout=10)
        except requests.RequestException:
            log.warning("Could not reach Google to revoke the token; removed locally anyway")
    credentials_store.delete()
    with _lock:
        _cached = None
    log.info("Google Drive disconnected")


def forget_cached():
    global _cached
    with _lock:
        _cached = None
