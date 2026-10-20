"""Copy a downloaded Google "Desktop app" OAuth client JSON into .env.

Usage:  .venv\\Scripts\\python scripts\\import_google_client.py [path-to-json]

Without a path, the newest client_secret*.json in your Downloads folder is used.
The client secret is never printed. After .env is updated the JSON file is
deleted so no extra copy of the secret is left on disk.
"""

import json
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT / ".env"


def find_json(arg: str | None) -> Path:
    if arg:
        return Path(arg).expanduser()
    downloads = Path.home() / "Downloads"
    candidates = sorted(downloads.glob("client_secret*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates:
        sys.exit(f"No client_secret*.json found in {downloads}. Download the Desktop client JSON first.")
    return candidates[0]


def set_env_value(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    if pattern.search(text):
        return pattern.sub(lambda _: line, text, count=1)
    return text.rstrip("\n") + f"\n{line}\n"


def main():
    path = find_json(sys.argv[1] if len(sys.argv) > 1 else None)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"Could not read {path.name}: {exc}")
    if "installed" not in data:
        kind = next(iter(data), "unknown")
        sys.exit(f"{path.name} is a '{kind}' client, not a 'Desktop app' client. Nothing was changed.")
    client = data["installed"]
    client_id, secret = client.get("client_id", ""), client.get("client_secret", "")
    if not client_id.endswith(".apps.googleusercontent.com") or not secret:
        sys.exit(f"{path.name} is missing the client id or secret. Nothing was changed.")

    text = ENV_PATH.read_text(encoding="utf-8") if ENV_PATH.exists() else ""
    text = set_env_value(text, "GOOGLE_CLIENT_ID", client_id)
    text = set_env_value(text, "GOOGLE_CLIENT_SECRET", secret)
    ENV_PATH.write_text(text, encoding="utf-8")
    path.unlink()

    project_number = client_id.split("-", 1)[0]
    print(f"Imported Desktop client {client_id.split('-', 1)[0]}-…apps.googleusercontent.com into .env")
    print(f"Client secret saved to .env (not shown). Deleted {path.name}.")
    print(f"Project number from the client id: {project_number}")


if __name__ == "__main__":
    main()
