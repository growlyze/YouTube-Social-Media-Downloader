"""Data-access functions. All SQL lives here; routes and services call these."""

import json
from dataclasses import dataclass
from datetime import datetime, timezone

import config
from .db import connect


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# Settings and app state (JSON values)
# --------------------------------------------------------------------------

def get_settings() -> dict:
    settings = dict(config.DEFAULT_SETTINGS)
    with connect() as conn:
        for row in conn.execute("SELECT key, value FROM settings"):
            if row["key"] in settings:
                settings[row["key"]] = json.loads(row["value"])
    if config.IN_DOCKER:
        settings["download_dir"] = str(config.DOWNLOADS_DIR)
    return settings


def update_settings(values: dict):
    with connect() as conn:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            [(k, json.dumps(v)) for k, v in values.items()],
        )


def get_state(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value):
    with connect() as conn:
        if value is None:
            conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO app_state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, json.dumps(value)),
            )


# --------------------------------------------------------------------------
# Sources (channels / playlists)
# --------------------------------------------------------------------------

def upsert_source(source_id, kind, url, title=None, handle=None, thumbnail=None):
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO sources(id, kind, url, title, handle, thumbnail)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                url = excluded.url,
                title = COALESCE(excluded.title, sources.title),
                handle = COALESCE(excluded.handle, sources.handle),
                thumbnail = COALESCE(excluded.thumbnail, sources.thumbnail)
            """,
            (source_id, kind, url, title, handle, thumbnail),
        )


def mark_source_discovered(source_id):
    with connect() as conn:
        conn.execute(
            """
            UPDATE sources SET last_discovered_at = ?,
                video_count = (SELECT COUNT(*) FROM source_videos WHERE source_id = ?)
            WHERE id = ?
            """,
            (_now(), source_id, source_id),
        )


def get_source(source_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    return dict(row) if row else None


def list_sources():
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                   COUNT(sv.video_id) AS total,
                   COALESCE(SUM(v.selected), 0) AS selected,
                   COALESCE(SUM(v.video_type = 'short'), 0) AS shorts
            FROM sources s
            LEFT JOIN source_videos sv ON sv.source_id = s.id
            LEFT JOIN videos v ON v.video_id = sv.video_id
            GROUP BY s.id
            ORDER BY COALESCE(s.last_discovered_at, s.created_at) DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def delete_source(source_id):
    """Remove a source and any videos no longer referenced by another source.
    Videos with a download record are kept so download history survives."""
    with connect() as conn:
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.execute(
            "DELETE FROM videos WHERE video_id NOT IN (SELECT video_id FROM source_videos) "
            "AND video_id NOT IN (SELECT video_id FROM downloads)"
        )


# --------------------------------------------------------------------------
# Videos
# --------------------------------------------------------------------------

def upsert_videos(source_id, videos, start_position=0):
    """Insert or refresh discovered videos. Never clears user selection, and
    never overwrites a known value with NULL from a less detailed listing."""
    now = _now()
    rows = [
        (
            v["video_id"], v.get("title"), v.get("channel_id"), v.get("channel_name"),
            v.get("duration"), v.get("upload_date"), int(bool(v.get("date_is_approx"))),
            v.get("video_type") or "video", v.get("availability"), v.get("live_status"),
            v.get("thumbnail"), v["url"], v.get("view_count"), v.get("platform", "youtube"),
            v.get("external_id"), now,
        )
        for v in videos
    ]
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO videos(video_id, title, channel_id, channel_name, duration,
                upload_date, date_is_approx, video_type, availability, live_status,
                thumbnail, url, view_count, platform, external_id, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                title        = COALESCE(excluded.title, videos.title),
                channel_id   = COALESCE(excluded.channel_id, videos.channel_id),
                channel_name = COALESCE(excluded.channel_name, videos.channel_name),
                duration     = COALESCE(excluded.duration, videos.duration),
                upload_date  = CASE
                    WHEN excluded.upload_date IS NULL THEN videos.upload_date
                    WHEN videos.upload_date IS NOT NULL AND videos.date_is_approx = 0
                         AND excluded.date_is_approx = 1 THEN videos.upload_date
                    ELSE excluded.upload_date END,
                date_is_approx = CASE
                    WHEN excluded.upload_date IS NULL THEN videos.date_is_approx
                    WHEN videos.upload_date IS NOT NULL AND videos.date_is_approx = 0
                         AND excluded.date_is_approx = 1 THEN 0
                    ELSE excluded.date_is_approx END,
                -- the uploads playlist lists Shorts as plain videos; keep the
                -- more specific type learned from the Shorts/Live tabs
                video_type   = CASE WHEN excluded.video_type <> 'video'
                                    THEN excluded.video_type ELSE videos.video_type END,
                availability = COALESCE(excluded.availability, videos.availability),
                live_status  = COALESCE(excluded.live_status, videos.live_status),
                thumbnail    = COALESCE(excluded.thumbnail, videos.thumbnail),
                url          = excluded.url,
                platform     = excluded.platform,
                external_id  = COALESCE(excluded.external_id, videos.external_id),
                view_count   = COALESCE(excluded.view_count, videos.view_count),
                updated_at   = excluded.updated_at
            """,
            rows,
        )
        if source_id is not None:
            conn.executemany(
                "INSERT INTO source_videos(source_id, video_id, position) VALUES(?, ?, ?) "
                "ON CONFLICT(source_id, video_id) DO UPDATE SET "
                "position = COALESCE(source_videos.position, excluded.position)",
                [(source_id, v["video_id"], start_position + i) for i, v in enumerate(videos)],
            )


UNAVAILABLE = ("private", "deleted", "unavailable", "premium_only", "subscriber_only", "needs_auth")


@dataclass
class VideoFilter:
    source_id: str | None = None
    search: str = ""
    video_type: str = ""          # '', video, short, live
    date_from: str = ""           # YYYY-MM-DD
    date_to: str = ""
    min_duration: int | None = None  # seconds
    max_duration: int | None = None
    availability: str = ""        # '', available, unavailable
    selected_only: bool = False
    download: str = ""            # '', not_downloaded, downloaded, queued, failed

    def where(self):
        clauses, params = [], []
        if self.source_id:
            clauses.append("v.video_id IN (SELECT video_id FROM source_videos WHERE source_id = ?)")
            params.append(self.source_id)
        if self.search:
            clauses.append("v.title LIKE ? ESCAPE '\\'")
            escaped = self.search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if self.video_type:
            clauses.append("v.video_type = ?")
            params.append(self.video_type)
        if self.date_from:
            clauses.append("v.upload_date >= ?")
            params.append(self.date_from.replace("-", ""))
        if self.date_to:
            clauses.append("v.upload_date <= ?")
            params.append(self.date_to.replace("-", ""))
        if self.min_duration is not None:
            clauses.append("v.duration >= ?")
            params.append(self.min_duration)
        if self.max_duration is not None:
            clauses.append("v.duration <= ?")
            params.append(self.max_duration)
        marks = ",".join("?" * len(UNAVAILABLE))
        if self.availability == "available":
            clauses.append(f"(v.availability IS NULL OR v.availability NOT IN ({marks}))")
            params.extend(UNAVAILABLE)
        elif self.availability == "unavailable":
            clauses.append(f"v.availability IN ({marks})")
            params.extend(UNAVAILABLE)
        if self.selected_only:
            clauses.append("v.selected = 1")
        download_clause = {
            "not_downloaded": "v.video_id NOT IN (SELECT video_id FROM downloads WHERE status = 'completed')",
            "downloaded": "v.video_id IN (SELECT video_id FROM downloads WHERE status = 'completed')",
            "queued": "v.video_id IN (SELECT video_id FROM downloads WHERE status IN ('pending', 'downloading'))",
            "failed": "v.video_id IN (SELECT video_id FROM downloads WHERE status IN ('failed', 'cancelled'))",
        }.get(self.download)
        if download_clause:
            clauses.append(download_clause)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


SORTS = {
    "newest": "v.upload_date IS NULL, v.upload_date DESC, sv_pos",
    "oldest": "v.upload_date IS NULL, v.upload_date ASC, sv_pos",
    "title": "v.title COLLATE NOCASE ASC",
    "longest": "v.duration IS NULL, v.duration DESC",
    "shortest": "v.duration IS NULL, v.duration ASC",
    "position": "sv_pos",
}


def query_videos(flt: VideoFilter, sort="newest", page=1, page_size=50):
    where, params = flt.where()
    order = SORTS.get(sort, SORTS["newest"])
    page_size = max(1, min(int(page_size), 500))
    page = max(1, int(page))
    if flt.source_id:
        pos_expr = "(SELECT position FROM source_videos WHERE source_id = ? AND video_id = v.video_id)"
        pos_params = [flt.source_id]
    else:
        pos_expr, pos_params = "NULL", []
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM videos v{where}", params).fetchone()[0]
        selected = conn.execute(
            f"SELECT COUNT(*) FROM videos v{where}{' AND' if where else ' WHERE'} v.selected = 1",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"SELECT v.*, {pos_expr} AS sv_pos, "
            f"(SELECT status FROM downloads WHERE video_id = v.video_id) AS download_status, "
            f"(SELECT progress FROM downloads WHERE video_id = v.video_id) AS download_progress "
            f"FROM videos v{where} "
            f"ORDER BY {order}, v.video_id LIMIT ? OFFSET ?",
            pos_params + params + [page_size, (page - 1) * page_size],
        ).fetchall()
    return {
        "items": [dict(r) for r in rows],
        "total": total,
        "selected": selected,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
    }


def set_selected(video_ids, selected: bool):
    ids = list(video_ids)[:5000]
    if not ids:
        return 0
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE videos SET selected = ? WHERE video_id IN ({','.join('?' * len(ids))})",
            [int(selected), *ids],
        )
        return cur.rowcount


def set_selected_by_filter(flt: VideoFilter, selected: bool):
    where, params = flt.where()
    with connect() as conn:
        cur = conn.execute(f"UPDATE videos SET selected = ? WHERE video_id IN "
                           f"(SELECT v.video_id FROM videos v{where})", [int(selected), *params])
        return cur.rowcount


def matching_video_ids(flt: VideoFilter, limit=10000):
    """Video ids matching a filter, in channel/upload order (oldest first)."""
    where, params = flt.where()
    with connect() as conn:
        rows = conn.execute(
            f"SELECT v.video_id FROM videos v{where} "
            f"ORDER BY v.upload_date IS NULL, v.upload_date, v.video_id LIMIT ?",
            [*params, limit]).fetchall()
    return [r[0] for r in rows]


def count_videos():
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(selected), 0) AS selected, "
            "COALESCE(SUM(video_type = 'short'), 0) AS shorts FROM videos"
        ).fetchone()
    return dict(row)


# --------------------------------------------------------------------------
# Discovery jobs
# --------------------------------------------------------------------------

def create_discovery_job(input_url):
    with connect() as conn:
        cur = conn.execute("INSERT INTO discovery_jobs(input_url) VALUES(?)", (input_url,))
        return cur.lastrowid


def update_discovery_job(job_id, **fields):
    allowed = {"source_id", "status", "found_count", "message", "error_kind",
               "started_at", "finished_at"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE discovery_jobs SET {assignments} WHERE id = ?",
                     [*fields.values(), job_id])


def get_discovery_job(job_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM discovery_jobs WHERE id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def list_discovery_jobs(limit=20):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM discovery_jobs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def next_pending_discovery_job():
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM discovery_jobs WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def requeue_interrupted_discovery_jobs():
    """Jobs left 'running' by a crash or restart go back to pending; discovery
    is idempotent because videos are upserted by id."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE discovery_jobs SET status = 'pending', "
            "message = 'Restarted after the app was interrupted' WHERE status = 'running'"
        )
        return cur.rowcount


def get_video(video_id):
    with connect() as conn:
        row = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


def update_video_metadata(video_id, info: dict):
    """Store exact metadata learned from a full extraction (during download)."""
    upload_date = info.get("upload_date")
    if not (isinstance(upload_date, str) and len(upload_date) == 8 and upload_date.isdigit()):
        upload_date = None
    duration = info.get("duration")
    duration = int(duration) if isinstance(duration, (int, float)) and duration >= 0 else None
    view_count = info.get("view_count") if isinstance(info.get("view_count"), int) else None
    with connect() as conn:
        conn.execute(
            """
            UPDATE videos SET
                title        = COALESCE(?, title),
                channel_id   = COALESCE(?, channel_id),
                channel_name = COALESCE(?, channel_name),
                duration     = COALESCE(?, duration),
                upload_date  = COALESCE(?, upload_date),
                date_is_approx = CASE WHEN ? IS NOT NULL THEN 0 ELSE date_is_approx END,
                availability = COALESCE(?, availability),
                live_status  = COALESCE(?, live_status),
                view_count   = COALESCE(?, view_count),
                updated_at   = ?
            WHERE video_id = ?
            """,
            (info.get("title"), info.get("channel_id"), info.get("channel") or info.get("uploader"),
             duration, upload_date, upload_date, info.get("availability"), info.get("live_status"),
             view_count, _now(), video_id),
        )


# --------------------------------------------------------------------------
# Downloads (queue + history)
# --------------------------------------------------------------------------

ACTIVE_STATUSES = ("pending", "downloading")
_DOWNLOAD_FIELDS = {"status", "redownload", "attempts", "next_attempt_at", "stage", "progress",
                    "downloaded_bytes", "total_bytes", "speed", "eta", "file_path", "file_size",
                    "format_desc", "error_kind", "error_message", "started_at", "finished_at"}

_RESET_COLUMNS = """attempts = 0, next_attempt_at = NULL, stage = NULL, progress = NULL,
    downloaded_bytes = NULL, total_bytes = NULL, speed = NULL, eta = NULL,
    error_kind = NULL, error_message = NULL, started_at = NULL, finished_at = NULL"""


def enqueue_downloads(video_ids, redownload=False, include_shorts=True, include_live=True,
                      download_dir=None):
    """Add videos to the download queue, refusing duplicates by video id.

    `download_dir` is stored on each newly queued item so a batch is saved where
    the user confirmed, even if the setting changes later. A re-download keeps
    the folder of the existing file so it is replaced in place.

    Returns counts (added, already_queued, already_downloaded, skipped_unavailable,
    skipped_type, missing) and `queued_ids`.
    """
    result = {"added": 0, "already_queued": 0, "already_downloaded": 0,
              "skipped_unavailable": 0, "skipped_type": 0, "missing": 0, "queued_ids": []}
    ids = list(dict.fromkeys(video_ids))[:5000]
    now = _now()
    with connect() as conn:
        position = conn.execute("SELECT COALESCE(MAX(position), 0) FROM downloads").fetchone()[0]
        for video_id in ids:
            video = conn.execute("SELECT * FROM videos WHERE video_id = ?", (video_id,)).fetchone()
            if not video:
                result["missing"] += 1
                continue
            if video["availability"] in UNAVAILABLE:
                result["skipped_unavailable"] += 1
                continue
            if (video["video_type"] == "short" and not include_shorts) or \
               (video["video_type"] == "live" and not include_live):
                result["skipped_type"] += 1
                continue
            existing = conn.execute("SELECT * FROM downloads WHERE video_id = ?", (video_id,)).fetchone()
            if existing and existing["status"] in ACTIVE_STATUSES:
                result["already_queued"] += 1
                continue
            if existing and existing["status"] == "completed" and not redownload:
                result["already_downloaded"] += 1
                continue
            position += 1
            if existing:
                # failed/cancelled -> requeue to the current folder. completed + redownload ->
                # requeue in the original folder; the old file is kept until the new
                # download has fully succeeded.
                is_redownload = bool(redownload and existing["status"] == "completed")
                folder = (existing["download_dir"] or download_dir) if is_redownload else download_dir
                conn.execute(
                    f"UPDATE downloads SET status = 'pending', position = ?, redownload = ?, "
                    f"download_dir = ?, {_RESET_COLUMNS}, updated_at = ? WHERE id = ?",
                    (position, int(is_redownload), folder, now, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO downloads(video_id, status, position, download_dir, updated_at) "
                    "VALUES(?, 'pending', ?, ?, ?)",
                    (video_id, position, download_dir, now),
                )
            result["added"] += 1
            result["queued_ids"].append(video_id)
    return result


def claim_next_download():
    """Atomically move the first eligible pending job to 'downloading'."""
    now = _now()
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE downloads SET status = 'downloading', started_at = ?, updated_at = ?,
                stage = 'Starting…', speed = NULL, eta = NULL
            WHERE id = (SELECT id FROM downloads WHERE status = 'pending'
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                        ORDER BY position LIMIT 1)
              AND status = 'pending'
            RETURNING id
            """,
            (now, now, now),
        ).fetchone()
    return get_download(row["id"]) if row else None


def next_retry_time():
    """Earliest future next_attempt_at among pending jobs, or None."""
    with connect() as conn:
        row = conn.execute(
            "SELECT MIN(next_attempt_at) FROM downloads WHERE status = 'pending' "
            "AND next_attempt_at IS NOT NULL AND next_attempt_at > ?", (_now(),)
        ).fetchone()
    return row[0]


def update_download(download_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _DOWNLOAD_FIELDS}
    if not fields:
        return
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE downloads SET {assignments} WHERE id = ?", [*fields.values(), download_id])


_DOWNLOAD_SELECT = """
    SELECT d.*, v.title, v.channel_name, v.channel_id, v.thumbnail, v.video_type, v.duration,
           v.url, v.upload_date, v.date_is_approx, v.live_status, v.availability,
           v.platform, v.external_id
    FROM downloads d JOIN videos v ON v.video_id = d.video_id
"""


def get_download(download_id):
    with connect() as conn:
        row = conn.execute(_DOWNLOAD_SELECT + " WHERE d.id = ?", (download_id,)).fetchone()
    return dict(row) if row else None


def get_download_by_video(video_id):
    with connect() as conn:
        row = conn.execute(_DOWNLOAD_SELECT + " WHERE d.video_id = ?", (video_id,)).fetchone()
    return dict(row) if row else None


DOWNLOAD_VIEWS = {
    # view: (where clause, order)
    "queue": ("d.status IN ('pending', 'downloading')", "d.status = 'downloading' DESC, d.position"),
    "failed": ("d.status IN ('failed', 'cancelled')", "d.finished_at DESC"),
    "completed": ("d.status = 'completed'", "d.finished_at DESC"),
    "history": ("d.status IN ('completed', 'failed', 'cancelled')", "d.finished_at DESC"),
}


def list_downloads(view="queue", page=1, page_size=100, search=""):
    where, order = DOWNLOAD_VIEWS.get(view, DOWNLOAD_VIEWS["queue"])
    params = []
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where += " AND v.title LIKE ? ESCAPE '\\'"
        params.append(f"%{escaped}%")
    page_size = max(1, min(int(page_size), 500))
    page = max(1, int(page))
    with connect() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) FROM downloads d JOIN videos v ON v.video_id = d.video_id WHERE {where}",
            params).fetchone()[0]
        rows = conn.execute(
            f"{_DOWNLOAD_SELECT} WHERE {where} ORDER BY {order}, d.id LIMIT ? OFFSET ?",
            params + [page_size, (page - 1) * page_size]).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "page_size": page_size, "pages": max(1, -(-total // page_size))}


def download_counts():
    counts = {"pending": 0, "downloading": 0, "completed": 0, "failed": 0, "cancelled": 0}
    with connect() as conn:
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM downloads GROUP BY status"):
            counts[row["status"]] = row["n"]
        counts["completed_bytes"] = conn.execute(
            "SELECT COALESCE(SUM(file_size), 0) FROM downloads WHERE status = 'completed'").fetchone()[0]
    return counts


def delete_download(download_id):
    """Remove a queue/history record (never a running one). Files are untouched."""
    with connect() as conn:
        cur = conn.execute("DELETE FROM downloads WHERE id = ? AND status <> 'downloading'",
                           (download_id,))
        return cur.rowcount


def clear_downloads(statuses):
    statuses = [s for s in statuses if s in ("pending", "completed", "failed", "cancelled")]
    if not statuses:
        return 0
    with connect() as conn:
        cur = conn.execute(
            f"DELETE FROM downloads WHERE status IN ({','.join('?' * len(statuses))})", statuses)
        return cur.rowcount


def retry_downloads(download_ids=None):
    """Requeue failed/cancelled jobs (all of them when ids is None)."""
    now = _now()
    with connect() as conn:
        position = conn.execute("SELECT COALESCE(MAX(position), 0) FROM downloads").fetchone()[0]
        sql = "SELECT id FROM downloads WHERE status IN ('failed', 'cancelled')"
        params = []
        if download_ids is not None:
            ids = [int(i) for i in download_ids][:5000]
            if not ids:
                return 0
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params = ids
        ids = [r[0] for r in conn.execute(sql + " ORDER BY position", params)]
        for offset, download_id in enumerate(ids, 1):
            conn.execute(
                f"UPDATE downloads SET status = 'pending', position = ?, {_RESET_COLUMNS}, "
                f"updated_at = ? WHERE id = ?", (position + offset, now, download_id))
    return len(ids)


def requeue_interrupted_downloads():
    """After a crash/restart, 'downloading' rows go back to pending. They are
    never marked completed here; yt-dlp resumes from the .part file."""
    with connect() as conn:
        cur = conn.execute(
            "UPDATE downloads SET status = 'pending', stage = NULL, speed = NULL, eta = NULL, "
            "error_message = 'Interrupted by an app restart; will resume from the partial file.' "
            "WHERE status = 'downloading'")
        return cur.rowcount


# --------------------------------------------------------------------------
# Uploads (Google Drive queue). Never modifies the downloads table.
# --------------------------------------------------------------------------

_UPLOAD_FIELDS = {"status", "attempts", "next_attempt_at", "progress", "uploaded_bytes",
                  "total_bytes", "speed", "drive_file_id", "web_link", "error_kind",
                  "error_message", "started_at", "finished_at"}
_UPLOAD_RESET = """attempts = 0, next_attempt_at = NULL, progress = NULL, uploaded_bytes = NULL,
    speed = NULL, error_kind = NULL, error_message = NULL, started_at = NULL, finished_at = NULL"""


def enqueue_uploads(download_ids, folder_id, folder_name=None):
    """Queue completed downloads for upload to one Drive folder.

    Returns counts: added, already_queued, already_uploaded, not_downloaded, missing.
    """
    result = {"added": 0, "already_queued": 0, "already_uploaded": 0, "not_downloaded": 0, "missing": 0}
    now = _now()
    with connect() as conn:
        position = conn.execute("SELECT COALESCE(MAX(position), 0) FROM uploads").fetchone()[0]
        for download_id in list(dict.fromkeys(int(i) for i in download_ids))[:5000]:
            download = conn.execute("SELECT * FROM downloads WHERE id = ?", (download_id,)).fetchone()
            if not download:
                result["missing"] += 1
                continue
            if download["status"] != "completed" or not download["file_path"]:
                result["not_downloaded"] += 1
                continue
            existing = conn.execute("SELECT * FROM uploads WHERE download_id = ? AND drive_folder_id = ?",
                                    (download_id, folder_id)).fetchone()
            if existing and existing["status"] in ("pending", "uploading"):
                result["already_queued"] += 1
                continue
            if existing and existing["status"] == "completed":
                result["already_uploaded"] += 1
                continue
            position += 1
            if existing:   # failed / cancelled -> try again
                conn.execute(f"UPDATE uploads SET status = 'pending', position = ?, drive_folder_name = ?, "
                             f"{_UPLOAD_RESET}, updated_at = ? WHERE id = ?",
                             (position, folder_name, now, existing["id"]))
            else:
                conn.execute(
                    "INSERT INTO uploads(download_id, video_id, drive_folder_id, drive_folder_name, "
                    "position, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (download_id, download["video_id"], folder_id, folder_name, position, now))
            result["added"] += 1
    return result


_UPLOAD_SELECT = """
    SELECT u.*, d.file_path, d.file_size, d.status AS download_status,
           v.title, v.channel_name, v.thumbnail, v.url, v.video_type, v.platform
    FROM uploads u
    JOIN downloads d ON d.id = u.download_id
    JOIN videos v ON v.video_id = u.video_id
"""


def get_upload(upload_id):
    with connect() as conn:
        row = conn.execute(_UPLOAD_SELECT + " WHERE u.id = ?", (upload_id,)).fetchone()
    return dict(row) if row else None


def claim_next_upload():
    now = _now()
    with connect() as conn:
        row = conn.execute(
            """
            UPDATE uploads SET status = 'uploading', started_at = ?, updated_at = ?, speed = NULL
            WHERE id = (SELECT id FROM uploads WHERE status = 'pending'
                        AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                        ORDER BY position LIMIT 1)
              AND status = 'pending'
            RETURNING id
            """, (now, now, now)).fetchone()
    return get_upload(row["id"]) if row else None


def update_upload(upload_id, **fields):
    fields = {k: v for k, v in fields.items() if k in _UPLOAD_FIELDS}
    if not fields:
        return
    fields["updated_at"] = _now()
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE uploads SET {assignments} WHERE id = ?", [*fields.values(), upload_id])


UPLOAD_VIEWS = {
    "queue": ("u.status IN ('pending', 'uploading')", "u.status = 'uploading' DESC, u.position"),
    "failed": ("u.status IN ('failed', 'cancelled')", "u.finished_at DESC"),
    "completed": ("u.status = 'completed'", "u.finished_at DESC"),
}


def list_uploads(view="queue", page=1, page_size=100):
    where, order = UPLOAD_VIEWS.get(view, UPLOAD_VIEWS["queue"])
    page_size = max(1, min(int(page_size), 500))
    page = max(1, int(page))
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM uploads u WHERE {where}").fetchone()[0]
        rows = conn.execute(f"{_UPLOAD_SELECT} WHERE {where} ORDER BY {order}, u.id LIMIT ? OFFSET ?",
                            (page_size, (page - 1) * page_size)).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "page_size": page_size, "pages": max(1, -(-total // page_size))}


def upload_counts():
    counts = {"pending": 0, "uploading": 0, "completed": 0, "failed": 0, "cancelled": 0}
    with connect() as conn:
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM uploads GROUP BY status"):
            counts[row["status"]] = row["n"]
    return counts


def upload_status_by_download(download_ids, folder_id=None):
    """{download_id: latest upload row} for showing Drive status on Local Files."""
    ids = [int(i) for i in download_ids][:1000]
    if not ids:
        return {}
    sql = (f"SELECT * FROM uploads WHERE download_id IN ({','.join('?' * len(ids))})"
           + (" AND drive_folder_id = ?" if folder_id else "") + " ORDER BY updated_at")
    with connect() as conn:
        rows = conn.execute(sql, ids + ([folder_id] if folder_id else [])).fetchall()
    return {r["download_id"]: dict(r) for r in rows}


def next_upload_retry_time():
    with connect() as conn:
        return conn.execute(
            "SELECT MIN(next_attempt_at) FROM uploads WHERE status = 'pending' "
            "AND next_attempt_at IS NOT NULL AND next_attempt_at > ?", (_now(),)).fetchone()[0]


def retry_uploads(upload_ids=None):
    now = _now()
    with connect() as conn:
        position = conn.execute("SELECT COALESCE(MAX(position), 0) FROM uploads").fetchone()[0]
        sql, params = "SELECT id FROM uploads WHERE status IN ('failed', 'cancelled')", []
        if upload_ids is not None:
            ids = [int(i) for i in upload_ids][:5000]
            if not ids:
                return 0
            sql += f" AND id IN ({','.join('?' * len(ids))})"
            params = ids
        ids = [r[0] for r in conn.execute(sql + " ORDER BY position", params)]
        for offset, upload_id in enumerate(ids, 1):
            conn.execute(f"UPDATE uploads SET status = 'pending', position = ?, {_UPLOAD_RESET}, "
                         f"updated_at = ? WHERE id = ?", (position + offset, now, upload_id))
    return len(ids)


def delete_upload(upload_id):
    with connect() as conn:
        return conn.execute("DELETE FROM uploads WHERE id = ? AND status <> 'uploading'",
                            (upload_id,)).rowcount


def requeue_interrupted_uploads():
    with connect() as conn:
        return conn.execute(
            "UPDATE uploads SET status = 'pending', speed = NULL, progress = NULL, uploaded_bytes = NULL, "
            "error_message = 'Interrupted by an app restart; the upload will start again.' "
            "WHERE status = 'uploading'").rowcount
