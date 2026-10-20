import sqlite3

from database import db, repository
from database.repository import VideoFilter


def _video(vid, **kw):
    row = {"video_id": vid, "title": f"Title {vid}", "url": f"https://www.youtube.com/watch?v={vid}",
           "duration": 100, "upload_date": "20240101", "video_type": "video"}
    row.update(kw)
    return row


def test_migrations_are_idempotent(tmp_path):
    path = tmp_path / "m.db"
    db.init_db(path)
    db.init_db(path)
    version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
    assert version == len(db.MIGRATIONS)


def test_v5_migration_preserves_existing_youtube_rows(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for index, migration in enumerate(db.MIGRATIONS[:4], start=1):
        conn.executescript(migration)
        conn.execute(f"PRAGMA user_version = {index}")
    conn.execute("INSERT INTO videos(video_id, url) VALUES(?, ?)",
                 ("abcdefghijk", "https://www.youtube.com/watch?v=abcdefghijk"))
    conn.commit()
    conn.close()

    db.init_db(path)
    row = repository.get_video("abcdefghijk")
    assert row["platform"] == "youtube" and row["external_id"] is None


def test_social_media_rows_can_share_the_existing_download_queue(temp_db, tmp_path):
    item = _video("instagram_123", platform="instagram", external_id="reel-123",
                  url="https://www.instagram.com/reel/reel-123/")
    repository.upsert_videos(None, [item])
    result = repository.enqueue_downloads([item["video_id"]], download_dir=str(tmp_path))
    queued = repository.get_download_by_video(item["video_id"])
    assert result["added"] == 1
    assert queued["platform"] == "instagram" and queued["external_id"] == "reel-123"
    assert queued["url"] == item["url"]


def test_settings_defaults_and_update(temp_db):
    assert repository.get_settings()["max_concurrent_downloads"] == 1
    repository.update_settings({"sleep_requests": 2.5})
    assert repository.get_settings()["sleep_requests"] == 2.5


def test_upsert_preserves_selection_and_known_values(temp_db):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [_video("aaaaaaaaaaa", upload_date="20240105", date_is_approx=False)])
    repository.set_selected(["aaaaaaaaaaa"], True)
    # a later, less detailed listing (no duration, approximate date) must not erase data
    repository.upsert_videos("UC1", [_video("aaaaaaaaaaa", duration=None, upload_date="20240110",
                                            date_is_approx=True, title=None)])
    row = repository.query_videos(VideoFilter(source_id="UC1"))["items"][0]
    assert row["selected"] == 1
    assert row["duration"] == 100
    assert row["upload_date"] == "20240105" and row["date_is_approx"] == 0
    assert row["title"] == "Title aaaaaaaaaaa"


def test_short_type_not_downgraded(temp_db):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [_video("sssssssssss", video_type="short")])
    repository.upsert_videos("UC1", [_video("sssssssssss", video_type="video")])
    assert repository.query_videos(VideoFilter())["items"][0]["video_type"] == "short"


def test_filters_paging_and_select_all(temp_db):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_source("PL1", "playlist", "https://y")
    videos = [_video(f"v{i:010d}", title=f"Cooking 100% {i}" if i % 2 else f"Travel_{i}",
                     duration=60 * i, upload_date=f"2024{(i % 12) + 1:02d}01",
                     video_type="short" if i % 5 == 0 else "video") for i in range(1, 31)]
    videos[2]["availability"] = "private"
    repository.upsert_videos("UC1", videos)
    repository.upsert_videos("PL1", videos[:4])

    assert repository.query_videos(VideoFilter(source_id="UC1"))["total"] == 30
    assert repository.query_videos(VideoFilter(source_id="PL1"))["total"] == 4
    assert repository.query_videos(VideoFilter(search="cooking"))["total"] == 15
    # LIKE wildcards in user input are escaped
    assert repository.query_videos(VideoFilter(search="100%"))["total"] == 15
    # unescaped, "g_1" would match "Cookin(g 1)00%"
    assert repository.query_videos(VideoFilter(search="g_1"))["total"] == 0
    assert repository.query_videos(VideoFilter(video_type="short"))["total"] == 6
    assert repository.query_videos(VideoFilter(min_duration=600, max_duration=1200))["total"] == 11
    assert repository.query_videos(VideoFilter(date_from="2024-03-01", date_to="2024-03-31"))["total"] == 3
    assert repository.query_videos(VideoFilter(availability="unavailable"))["total"] == 1
    assert repository.query_videos(VideoFilter(availability="available"))["total"] == 29

    page = repository.query_videos(VideoFilter(), sort="longest", page=2, page_size=7)
    assert page["pages"] == 5 and len(page["items"]) == 7
    assert page["items"][0]["duration"] == 60 * 23

    changed = repository.set_selected_by_filter(VideoFilter(video_type="short"), True)
    assert changed == 6
    assert repository.query_videos(VideoFilter(selected_only=True))["total"] == 6
    assert len(repository.get_selected_videos()) == 6


def test_sort_order_is_whitelisted(temp_db):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_videos("UC1", [_video("aaaaaaaaaaa")])
    result = repository.query_videos(VideoFilter(), sort="title; DROP TABLE videos")
    assert result["total"] == 1


def test_delete_source_keeps_shared_videos(temp_db):
    repository.upsert_source("UC1", "channel", "https://x")
    repository.upsert_source("PL1", "playlist", "https://y")
    repository.upsert_videos("UC1", [_video("aaaaaaaaaaa"), _video("bbbbbbbbbbb")])
    repository.upsert_videos("PL1", [_video("aaaaaaaaaaa")])
    repository.delete_source("UC1")
    ids = [v["video_id"] for v in repository.query_videos(VideoFilter())["items"]]
    assert ids == ["aaaaaaaaaaa"]


def test_interrupted_jobs_are_requeued(temp_db):
    job = repository.create_discovery_job("@x")
    repository.update_discovery_job(job, status="running")
    assert repository.requeue_interrupted_discovery_jobs() == 1
    assert repository.get_discovery_job(job)["status"] == "pending"
