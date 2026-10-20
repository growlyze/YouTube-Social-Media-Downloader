import pytest
import yt_dlp

from services.youtube_service import (InputError, iter_source, normalize_entry,
                                      parse_source_input, uploads_playlist_url)

SETTINGS = {"sleep_requests": 0, "max_retries": 1, "discovery_limit": 0, "js_runtime": "node"}


@pytest.mark.parametrize("text, kind, url", [
    ("@veritasium", "channel", "https://www.youtube.com/@veritasium"),
    ("https://www.youtube.com/@veritasium", "channel", "https://www.youtube.com/@veritasium"),
    ("youtube.com/@veritasium/shorts", "channel", "https://www.youtube.com/@veritasium"),
    ("https://m.youtube.com/@veritasium/videos?x=1", "channel", "https://www.youtube.com/@veritasium"),
    ("https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA", "channel",
     "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA"),
    ("UCHnyfMqiRRG1u-2MsSQLbXA", "channel", "https://www.youtube.com/channel/UCHnyfMqiRRG1u-2MsSQLbXA"),
    ("https://www.youtube.com/c/Veritasium", "channel", "https://www.youtube.com/c/Veritasium"),
    ("https://www.youtube.com/user/1veritasium", "channel", "https://www.youtube.com/user/1veritasium"),
    ("https://www.youtube.com/playlist?list=PLabc123XYZ", "playlist",
     "https://www.youtube.com/playlist?list=PLabc123XYZ"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc123XYZ", "playlist",
     "https://www.youtube.com/playlist?list=PLabc123XYZ"),
    ("https://youtu.be/dQw4w9WgXcQ?list=PLabc123XYZ", "playlist",
     "https://www.youtube.com/playlist?list=PLabc123XYZ"),
    ("PLabc123XYZ", "playlist", "https://www.youtube.com/playlist?list=PLabc123XYZ"),
])
def test_parse_valid_inputs(text, kind, url):
    parsed = parse_source_input(text)
    assert parsed.kind == kind
    assert parsed.url == url


def test_channel_tabs_follow_settings():
    assert parse_source_input("@x1", include_shorts=True).tabs == ["videos", "shorts"]
    assert parse_source_input("@x1", include_shorts=False).tabs == ["videos"]
    assert parse_source_input("@x1", include_shorts=True, include_streams=True).tabs == \
        ["videos", "shorts", "streams"]


@pytest.mark.parametrize("text", [
    "", "   ", "https://evil.example.com/@veritasium", "javascript:alert(1)",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ", "https://www.youtube.com/",
    "ftp://youtube.com/@x", "https://youtube.com.evil.com/@x", "x" * 600,
    "https://www.youtube.com/playlist?list=<script>",
])
def test_parse_rejects_invalid(text):
    with pytest.raises(InputError):
        parse_source_input(text)


def test_uploads_playlist_url():
    assert uploads_playlist_url("UCHnyfMqiRRG1u-2MsSQLbXA") == \
        "https://www.youtube.com/playlist?list=UUHnyfMqiRRG1u-2MsSQLbXA"
    assert uploads_playlist_url("bad") is None


def test_normalize_regular_entry():
    row = normalize_entry({"id": "dQw4w9WgXcQ", "title": "T", "duration": 212.0,
                           "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                           "timestamp": 1700000000, "view_count": 10, "ie_key": "Youtube"},
                          tab="videos", fallback_channel={"channel_id": "UCx", "channel": "Chan"})
    assert row["duration"] == 212
    assert row["video_type"] == "video"
    assert row["upload_date"] == "20231114" and row["date_is_approx"] is True
    assert row["channel_name"] == "Chan"
    assert row["thumbnail"].endswith("/dQw4w9WgXcQ/mqdefault.jpg")


def test_normalize_short_and_exact_date():
    row = normalize_entry({"id": "abcdefghijk", "title": "S", "upload_date": "20240102",
                           "url": "https://www.youtube.com/shorts/abcdefghijk"}, tab="shorts")
    assert row["video_type"] == "short"
    assert row["url"] == "https://www.youtube.com/shorts/abcdefghijk"
    assert row["upload_date"] == "20240102" and row["date_is_approx"] is False


def test_normalize_private_and_deleted_placeholders():
    assert normalize_entry({"id": "abcdefghijk", "title": "[Private video]"})["availability"] == "private"
    assert normalize_entry({"id": "abcdefghijk", "title": "[Deleted video]"})["availability"] == "deleted"


def test_normalize_live_and_skips_non_videos():
    assert normalize_entry({"id": "abcdefghijk", "live_status": "was_live"})["video_type"] == "live"
    assert normalize_entry({"id": "UCHnyfMqiRRG1u-2MsSQLbXA", "ie_key": "YoutubeTab"}) is None
    assert normalize_entry({"id": "short"}) is None
    assert normalize_entry(None) is None


def test_normalize_duration_strings():
    assert normalize_entry({"id": "abcdefghijk", "duration_string": "1:02:03"})["duration"] == 3723


class FakeYDL:
    """Minimal YoutubeDL stand-in returning canned tab listings."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, opts):
        self.opts = opts
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False, process=True):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result


def _entries(prefix, n):
    return ({"id": f"{prefix}{i:07d}"[:11].ljust(11, "x"), "title": f"{prefix} {i}", "duration": 60 + i}
            for i in range(n))


def test_iter_source_channel_videos_and_shorts():
    base = "https://www.youtube.com/@chan"
    fake = FakeYDL({
        f"{base}/videos": {"channel_id": "UCHnyfMqiRRG1u-2MsSQLbXA", "channel": "Chan",
                           "uploader_id": "@chan", "entries": _entries("vid", 120)},
        f"{base}/shorts": {"channel_id": "UCHnyfMqiRRG1u-2MsSQLbXA", "entries": _entries("sho", 7)},
    })
    events = list(iter_source(parse_source_input("@chan"), SETTINGS, batch_size=50, ydl_factory=fake))
    assert events[0][0] == "source"
    assert events[0][1]["id"] == "UCHnyfMqiRRG1u-2MsSQLbXA"
    assert events[0][1]["title"] == "Chan"
    batches = [e for e in events if e[0] == "batch"]
    assert [len(b[2]) for b in batches] == [50, 50, 20, 7]
    assert all(r["video_type"] == "short" for r in batches[-1][2])
    # pacing options are passed to yt-dlp
    assert fake.opts["sleep_interval_requests"] == 0
    assert fake.opts["extract_flat"] == "in_playlist"
    assert fake.opts["js_runtimes"] == {"node": {}}


def test_iter_source_missing_shorts_tab_is_skipped():
    base = "https://www.youtube.com/@chan"
    fake = FakeYDL({
        f"{base}/videos": {"channel_id": "UCHnyfMqiRRG1u-2MsSQLbXA", "entries": _entries("vid", 3)},
        f"{base}/shorts": yt_dlp.utils.DownloadError("ERROR: [youtube:tab] @chan: This channel does not have a shorts tab"),
    })
    events = list(iter_source(parse_source_input("@chan"), SETTINGS, ydl_factory=fake))
    assert ("tab_skipped", "shorts", "This channel has no shorts tab") in events


def test_iter_source_respects_limit_and_cancel():
    base = "https://www.youtube.com/@chan"
    fake = FakeYDL({f"{base}/videos": {"channel_id": "UCHnyfMqiRRG1u-2MsSQLbXA",
                                       "entries": _entries("vid", 500)}})
    events = list(iter_source(parse_source_input("@chan", include_shorts=False),
                              {**SETTINGS, "discovery_limit": 30}, ydl_factory=fake))
    assert sum(len(e[2]) for e in events if e[0] == "batch") == 30

    from services.youtube_service import DiscoveryCancelled
    with pytest.raises(DiscoveryCancelled):
        list(iter_source(parse_source_input("@chan", include_shorts=False), SETTINGS,
                         is_cancelled=lambda: True, ydl_factory=fake))


def test_iter_source_rate_limit_propagates():
    fake = FakeYDL({"https://www.youtube.com/playlist?list=PLabc123XYZ":
                    yt_dlp.utils.DownloadError("ERROR: HTTP Error 429: Too Many Requests")})
    with pytest.raises(yt_dlp.utils.DownloadError):
        list(iter_source(parse_source_input("PLabc123XYZ"), SETTINGS, ydl_factory=fake))
