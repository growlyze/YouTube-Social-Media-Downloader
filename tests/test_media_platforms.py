import pytest

from services.media_platforms import MediaInputError, adapter_for_url, normalize_media_url
from services.media_service import MAX_COLLECTION_ITEMS, resolve_media_url


@pytest.mark.parametrize(("url", "platform"), [
    ("https://www.youtube.com/watch?v=abcdefghijk", "youtube"),
    ("https://youtu.be/abcdefghijk", "youtube"),
    ("https://www.instagram.com/reel/abc123/", "instagram"),
    ("https://m.facebook.com/watch/?v=123", "facebook"),
    ("https://vm.tiktok.com/ZMabc123/", "tiktok"),
    ("https://x.com/example/status/123", "x"),
    ("https://twitter.com/example/status/123", "x"),
])
def test_supported_platform_detection(url, platform):
    assert adapter_for_url(url).key == platform


@pytest.mark.parametrize("url", [
    "",
    "https://youtube.com.evil.example/watch?v=abcdefghijk",
    "https://user:password@tiktok.com/@a/video/1",
    "ftp://instagram.com/p/abc/",
    "http://localhost:5000/private",
    "https://facebook.com:8443/watch",
])
def test_unsupported_or_unsafe_urls_are_rejected(url):
    with pytest.raises(MediaInputError):
        normalize_media_url(url)


def test_url_normalization_adds_scheme_and_drops_fragment():
    url, adapter = normalize_media_url("instagram.com/p/abc/#comments")
    assert url == "https://instagram.com/p/abc/"
    assert adapter.key == "instagram"


class FakeYDL:
    def __init__(self, options, info):
        self.options = options
        self.info = info
        self.extracted_url = None
        self.download = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def extract_info(self, url, download=False):
        self.extracted_url = url
        self.download = download
        return self.info


def test_media_resolution_extracts_single_item_and_platform_metadata():
    info = {
        "id": "reel-42",
        "title": "Public reel",
        "uploader": "creator",
        "duration": 12.8,
        "webpage_url": "https://www.instagram.com/reel/reel-42/",
        "thumbnail": "https://cdninstagram.com/thumb.jpg",
    }
    instances = []

    def factory(options):
        instance = FakeYDL(options, info)
        instances.append(instance)
        return instance

    result = resolve_media_url("https://instagram.com/reel/reel-42/", {}, ydl_factory=factory)
    fake = instances[0]
    item = result["items"][0]
    assert fake.download is False
    assert fake.options["skip_download"] is True
    assert "cookiefile" not in fake.options and "cookiesfrombrowser" not in fake.options
    assert item["platform"] == "instagram"
    assert item["external_id"] == "reel-42"
    assert item["title"] == "Public reel" and item["channel_name"] == "creator"
    assert item["duration"] == 12 and result["truncated"] is False


def test_youtube_playlist_entries_expand_to_individual_links():
    info = {"_type": "playlist", "entries": [
        {"id": "abcdefghijk", "ie_key": "Youtube", "title": "First"},
        {"id": "lmnopqrstuv", "ie_key": "Youtube", "title": "Second"},
    ]}
    result = resolve_media_url("https://youtube.com/playlist?list=PL123", {}, ydl_factory=lambda opts: FakeYDL(opts, info))
    assert len(result["items"]) == 2
    assert result["items"][0]["url"] == "https://www.youtube.com/watch?v=abcdefghijk"
    assert result["items"][1]["video_id"] == "lmnopqrstuv"


def test_collection_expansion_is_capped_and_reported():
    entries = [{"id": f"{index:011d}", "ie_key": "Youtube"} for index in range(MAX_COLLECTION_ITEMS + 1)]
    result = resolve_media_url("https://youtube.com/playlist?list=PL123", {},
                               ydl_factory=lambda opts: FakeYDL(opts, {"_type": "playlist", "entries": entries}))
    assert len(result["items"]) == MAX_COLLECTION_ITEMS
    assert result["truncated"] is True
