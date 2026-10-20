"""URL validation and platform adapters for supported public media links."""

import re
from urllib.parse import urlsplit, urlunsplit


class MediaInputError(ValueError):
    """A submitted URL is invalid or belongs to an unsupported platform."""


class YtDlpAdapter:
    key = ""
    label = ""
    hosts = ()

    def matches(self, host: str) -> bool:
        return any(host == domain or host.endswith("." + domain) for domain in self.hosts)

    def item_url(self, entry: dict, source_url: str, is_collection: bool) -> str:
        for key in ("webpage_url", "original_url", "url"):
            candidate = entry.get(key)
            if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                try:
                    normalized, _ = normalize_media_url(candidate)
                except MediaInputError:
                    continue
                return normalized

        extractor = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
        media_id = str(entry.get("id") or "")
        if "youtube" in extractor and re.fullmatch(r"[A-Za-z0-9_-]{11}", media_id):
            return f"https://www.youtube.com/watch?v={media_id}"
        if self.key == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", media_id):
            return f"https://www.youtube.com/watch?v={media_id}"
        if not is_collection:
            return source_url
        raise MediaInputError(f"yt-dlp did not provide a supported page URL for a {self.label} item.")


class YouTubeAdapter(YtDlpAdapter):
    key = "youtube"
    label = "YouTube"
    hosts = ("youtube.com", "youtu.be", "youtube-nocookie.com")


class InstagramAdapter(YtDlpAdapter):
    key = "instagram"
    label = "Instagram"
    hosts = ("instagram.com", "instagr.am")


class FacebookAdapter(YtDlpAdapter):
    key = "facebook"
    label = "Facebook"
    hosts = ("facebook.com", "fb.watch")


class TikTokAdapter(YtDlpAdapter):
    key = "tiktok"
    label = "TikTok"
    hosts = ("tiktok.com",)


class XAdapter(YtDlpAdapter):
    key = "x"
    label = "X (Twitter)"
    hosts = ("x.com", "twitter.com")


ADAPTERS = (
    YouTubeAdapter(),
    InstagramAdapter(),
    FacebookAdapter(),
    TikTokAdapter(),
    XAdapter(),
)
ADAPTER_BY_KEY = {adapter.key: adapter for adapter in ADAPTERS}


def normalize_media_url(value: str) -> tuple[str, YtDlpAdapter]:
    """Normalize and validate an HTTP(S) URL without accepting arbitrary hosts."""
    value = (value or "").strip()
    if not value:
        raise MediaInputError("Enter at least one media URL.")
    if len(value) > 2048:
        raise MediaInputError("The URL is too long (maximum 2048 characters).")
    if any(ord(char) < 32 for char in value):
        raise MediaInputError("The URL contains invalid control characters.")
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        value = "https://" + value

    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise MediaInputError("Enter a valid URL.") from exc
    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise MediaInputError("Only HTTP(S) media URLs are supported.")
    if parsed.username is not None or parsed.password is not None:
        raise MediaInputError("URLs containing embedded usernames or passwords are not supported.")
    if port not in (None, 80, 443):
        raise MediaInputError("Non-standard URL ports are not supported.")

    adapter = next((item for item in ADAPTERS if item.matches(host)), None)
    if not adapter:
        raise MediaInputError("Unsupported platform. Supported platforms are YouTube, Instagram, "
                              "Facebook, TikTok, and X (Twitter).")
    normalized = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, parsed.query, ""))
    return normalized, adapter
