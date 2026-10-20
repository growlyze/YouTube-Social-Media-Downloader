"""Store the Google refresh token in the OS credential store.

On Windows `keyring` uses Windows Credential Manager (DPAPI-protected, per
user). Nothing is written to a plain file, and if no secure backend is
available we refuse to store the token rather than falling back to plaintext.
"""

import json
import logging

import keyring
from keyring.errors import KeyringError, PasswordDeleteError

log = logging.getLogger("credentials")

SERVICE = "YT-Bulk-Downloader/google-drive"
ACCOUNT = "oauth-refresh-token"


class CredentialStoreError(Exception):
    pass


def _check_backend():
    backend = keyring.get_keyring()
    name = type(backend).__name__.lower()
    if "fail" in name or "null" in name or "plaintext" in name:
        raise CredentialStoreError("No secure credential store is available on this PC.")


def save(data: dict):
    _check_backend()
    try:
        keyring.set_password(SERVICE, ACCOUNT, json.dumps(data))
    except KeyringError as exc:
        raise CredentialStoreError(f"Could not save the Google sign-in securely: {exc}")


def load() -> dict | None:
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
    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except (PasswordDeleteError, KeyringError):
        pass
