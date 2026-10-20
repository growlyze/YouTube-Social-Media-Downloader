"""Store the Google refresh token in the OS credential store.

On Windows `keyring` uses Windows Credential Manager (DPAPI-protected, per
user). Nothing is written to a plain file, and if no secure backend is
available we refuse to store the token rather than falling back to plaintext.

The Docker image has no OS credential store, so it sets YTDL_CREDENTIALS_FILE
to a file inside its private data volume (owner read/write only) instead.
"""

import json
import logging
import os
from pathlib import Path

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

log = logging.getLogger("credentials")

SERVICE = "YT-Bulk-Downloader/google-drive"
ACCOUNT = "oauth-refresh-token"


class CredentialStoreError(Exception):
    pass


def _token_file() -> Path | None:
    path = os.environ.get("YTDL_CREDENTIALS_FILE", "").strip()
    return Path(path) if path else None


def _check_backend():
    backend = keyring.get_keyring()
    name = type(backend).__name__.lower()
    if "fail" in name or "null" in name or "plaintext" in name:
        raise CredentialStoreError("No secure credential store is available on this PC.")


def save(data: dict):
    if path := _token_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        return
    _check_backend()
    try:
        keyring.set_password(SERVICE, ACCOUNT, json.dumps(data))
    except KeyringError as exc:
        raise CredentialStoreError(f"Could not save the Google sign-in securely: {exc}")


def load() -> dict | None:
    if path := _token_file():
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
    else:
        try:
            raw = keyring.get_password(SERVICE, ACCOUNT)
        except KeyringError as exc:
            log.warning("Could not read the credential store: %s", type(exc).__name__)
            return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def delete():
    if path := _token_file():
        path.unlink(missing_ok=True)
        return
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except (PasswordDeleteError, KeyringError):
        pass
