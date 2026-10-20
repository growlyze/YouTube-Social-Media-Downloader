"""SQLite connection handling and schema migrations.

Each call to `connect()` opens a short-lived connection so worker threads and
request threads never share a connection object. WAL mode lets the web UI read
while a background worker writes.
"""

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_db_path: Path | None = None
_init_lock = threading.Lock()

# Each entry upgrades the schema by one version. Never edit an entry that has
# shipped; append a new one instead.
MIGRATIONS = [
    # v1: discovery (Phase 2)
    """
    CREATE TABLE sources (
        id                TEXT PRIMARY KEY,      -- UC... channel id or playlist id
        kind              TEXT NOT NULL,         -- 'channel' | 'playlist'
        title             TEXT,
        handle            TEXT,
        url               TEXT NOT NULL,
        thumbnail         TEXT,
        video_count       INTEGER NOT NULL DEFAULT 0,
        last_discovered_at TEXT,
        created_at        TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE videos (
        video_id      TEXT PRIMARY KEY,          -- stable 11-char YouTube id
        title         TEXT,
        channel_id    TEXT,
        channel_name  TEXT,
        duration      INTEGER,                   -- seconds
        upload_date   TEXT,                      -- YYYYMMDD
        date_is_approx INTEGER NOT NULL DEFAULT 0,
        video_type    TEXT NOT NULL DEFAULT 'video',  -- video | short | live
        availability  TEXT,                      -- public | private | deleted | members_only | ...
        live_status   TEXT,
        thumbnail     TEXT,
        url           TEXT NOT NULL,
        view_count    INTEGER,
        selected      INTEGER NOT NULL DEFAULT 0,
        discovered_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_videos_channel ON videos(channel_id);

    -- A video can belong to several sources (its channel and any playlists).
    CREATE TABLE source_videos (
        source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
        video_id  TEXT NOT NULL REFERENCES videos(video_id) ON DELETE CASCADE,
        position  INTEGER,
        PRIMARY KEY (source_id, video_id)
    );
    CREATE INDEX idx_source_videos_video ON source_videos(video_id);

    CREATE TABLE discovery_jobs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        input_url     TEXT NOT NULL,
        source_id     TEXT,
        status        TEXT NOT NULL DEFAULT 'pending', -- pending|running|completed|failed|cancelled
        found_count   INTEGER NOT NULL DEFAULT 0,
        message       TEXT,
        error_kind    TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        started_at    TEXT,
        finished_at   TEXT
    );

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL                      -- JSON encoded
    );

    CREATE TABLE app_state (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL                      -- JSON encoded
    );
    """,
    # v2: download queue (Phase 3). One row per video: the row is both the
    # queue entry and the download history, which makes duplicates impossible.
    """
    CREATE TABLE downloads (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        video_id         TEXT NOT NULL UNIQUE REFERENCES videos(video_id),
        status           TEXT NOT NULL DEFAULT 'pending',
            -- pending | downloading | completed | failed | cancelled
        position         INTEGER NOT NULL,       -- FIFO queue order
        redownload       INTEGER NOT NULL DEFAULT 0,
        attempts         INTEGER NOT NULL DEFAULT 0,  -- failed attempts so far
        next_attempt_at  TEXT,                   -- retry backoff (UTC)
        stage            TEXT,                   -- human readable step
        progress         REAL,                   -- 0..100
        downloaded_bytes INTEGER,
        total_bytes      INTEGER,
        speed            REAL,                   -- bytes/s
        eta              INTEGER,                -- seconds
        file_path        TEXT,
        file_size        INTEGER,
        format_desc      TEXT,
        error_kind       TEXT,
        error_message    TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        started_at       TEXT,
        finished_at      TEXT,
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_downloads_queue ON downloads(status, position);
    """,
    # v3: each queued item remembers the destination folder confirmed when it
    # was queued (NULL for older rows = use the current setting).
    """
    ALTER TABLE downloads ADD COLUMN download_dir TEXT;
    """,
    # v4: Google Drive upload queue (Phase 4). Independent of downloads: an
    # upload failure never changes the download row.
    """
    CREATE TABLE uploads (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        download_id      INTEGER NOT NULL REFERENCES downloads(id) ON DELETE CASCADE,
        video_id         TEXT NOT NULL,
        drive_folder_id  TEXT NOT NULL,
        drive_folder_name TEXT,
        status           TEXT NOT NULL DEFAULT 'pending',
            -- pending | uploading | completed | failed | cancelled
        position         INTEGER NOT NULL,
        attempts         INTEGER NOT NULL DEFAULT 0,
        next_attempt_at  TEXT,
        progress         REAL,
        uploaded_bytes   INTEGER,
        total_bytes      INTEGER,
        speed            REAL,
        drive_file_id    TEXT,
        web_link         TEXT,
        error_kind       TEXT,
        error_message    TEXT,
        created_at       TEXT NOT NULL DEFAULT (datetime('now')),
        started_at       TEXT,
        finished_at      TEXT,
        updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (download_id, drive_folder_id)   -- one upload per file per folder
    );
    CREATE INDEX idx_uploads_queue ON uploads(status, position);
    """,
]


def init_db(path: Path):
    """Set the database location and apply pending migrations."""
    global _db_path
    with _init_lock:
        _db_path = Path(path)
        _db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = _open()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(MIGRATIONS)):
                with conn:
                    conn.executescript(MIGRATIONS[index])
                    conn.execute(f"PRAGMA user_version = {index + 1}")
        finally:
            conn.close()


def _open() -> sqlite3.Connection:
    if _db_path is None:
        raise RuntimeError("init_db() has not been called")
    conn = sqlite3.connect(_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connect():
    """Yield a connection that commits on success and rolls back on error."""
    conn = _open()
    try:
        with conn:
            yield conn
    finally:
        conn.close()
