"""Application paths and default settings.

All persistent state lives under DATA_DIR (database, logs, OAuth client file).
Downloaded videos default to DOWNLOADS_DIR but the location is configurable
from the Settings page.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def load_env_file(path=ENV_PATH) -> dict:
    """Read KEY=VALUE lines from .env into os.environ (existing variables win).
    Kept dependency-free; supports comments and optional quotes."""
    loaded = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return loaded
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            loaded[key] = value
            os.environ.setdefault(key, value)
    return loaded


load_env_file()


def google_secret(name: str) -> str:
    """Google values come from .env / environment, never from the database."""
    return os.environ.get(name, "").strip()
DATA_DIR = Path(os.environ.get("YTDL_DATA_DIR", BASE_DIR / "data")).resolve()
DOWNLOADS_DIR = Path(os.environ.get("YTDL_DOWNLOADS_DIR", BASE_DIR / "downloads")).resolve()
DB_PATH = DATA_DIR / "app.db"
LOG_DIR = DATA_DIR / "logs"

HOST = os.environ.get("YTDL_HOST", "127.0.0.1")
PORT = int(os.environ.get("YTDL_PORT", "5000"))

# Set by the Docker image. Downloads then always go to the mounted DOWNLOADS_DIR,
# and DOWNLOADS_LABEL is the matching folder on the host PC, shown in the pages.
IN_DOCKER = os.environ.get("YTDL_IN_DOCKER") == "1"
DOWNLOADS_LABEL = os.environ.get("YTDL_DOWNLOADS_LABEL", "").strip() or str(DOWNLOADS_DIR)

# Values stored in the `settings` table on first run. Users change them from
# the Settings page; code should read them through repository.get_settings().
DEFAULT_SETTINGS = {
    "download_dir": str(DOWNLOADS_DIR),
    "video_quality": "1080p",
    "video_container": "mp4",
    # Seconds slept between yt-dlp HTTP requests during extraction.
    "sleep_requests": 1.0,
    # Random sleep (min..max seconds) before each video download.
    "sleep_interval_min": 5.0,
    "sleep_interval_max": 15.0,
    "max_retries": 3,
    "max_concurrent_downloads": 1,
    # Minutes the queue stays paused after a rate-limit / bot-check error
    # before the user is told it is reasonable to resume manually.
    "rate_limit_cooldown_minutes": 60,
    "auto_upload": False,
    "drive_folder_id": "",
    "drive_folder_name": "",
    # Fallbacks only; GOOGLE_API_KEY / GOOGLE_PROJECT_NUMBER in .env take precedence.
    "google_api_key": "",
    "google_project_number": "",
    # Max entries fetched per channel tab (0 = no limit).
    "discovery_limit": 0,
    "include_shorts": True,
    "include_streams": False,
    # JavaScript runtime yt-dlp uses to solve YouTube challenges.
    "js_runtime": "node",
}

CLIENT_SECRET_PATH = DATA_DIR / "client_secret.json"

VIDEO_QUALITIES =["best", "2160p", "1440p", "1080p", "720p", "480p", "360p"]
VIDEO_CONTAINERS = ["mp4", "mkv", "webm", "any"]
JS_RUNTIMES = ["node", "deno", "bun", "quickjs"]


def ensure_dirs():
    for path in (DATA_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)
