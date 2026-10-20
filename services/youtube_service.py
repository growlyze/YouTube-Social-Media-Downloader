"""YouTube source parsing and video discovery, built on yt-dlp.

Portions adapted from YT Channel Downloader (classes/YTChannel.py:
duration coercion, uploads-playlist and canonical-playlist handling),
Copyright (c) 2024 hyperfield, MIT License. See
third_party_licenses/yt-channel-downloader-LICENSE.txt.

Changes from the original: removed the PyQt6 dependency; replaced batched
`playlist_items` windows (which re-page the listing from the start for each
window) with a single lazy pass over yt-dlp's entry generator; added Shorts
and Live tabs, availability, thumbnails and approximate upload dates.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import yt_dlp
from yt_dlp.utils import parse_duration

from services import runtime as runtime_tools

log = logging.getLogger("youtube")

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
HANDLE_RE = re.compile(r"^@[A-Za-z0-9._\-À-￿]{1,100}$")
CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
PLAYLIST_ID_RE = re.compile(r"^(PL|UU|OL|FL|LL|RD|UUSH|UULF)[A-Za-z0-9_-]{2,80}$|^WL$")
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
CHANNEL_TABS = {"videos", "shorts", "streams", "featured", "playlists", "live", "community"}

TAB_TYPES = {"videos": "video", "shorts": "short", "streams": "live"}


class InputError(ValueError):
    """The user entered something that is not a channel, handle or playlist."""


@dataclass
class ParsedSource:
    kind: str            # 'channel' | 'playlist'
    url: str             # canonical base URL
    handle: str | None = None
    tabs: list = field(default_factory=list)  # channel tabs to scan


def parse_source_input(text: str, include_shorts=True, include_streams=False) -> ParsedSource:
    """Validate user input and turn it into a canonical channel/playlist URL."""
    text = (text or "").strip()
    if not text:
        raise InputError("Enter a YouTube channel URL, @handle or playlist URL.")
    if len(text) > 500:
        raise InputError("Input is too long.")

    tabs = ["videos"] + (["shorts"] if include_shorts else []) + (["streams"] if include_streams else [])

    if HANDLE_RE.match(text):
        return ParsedSource("channel", f"https://www.youtube.com/{text}", handle=text, tabs=tabs)
    if CHANNEL_ID_RE.match(text):
        return ParsedSource("channel", f"https://www.youtube.com/channel/{text}", tabs=tabs)
    if PLAYLIST_ID_RE.match(text):
        return ParsedSource("playlist", canonical_playlist_url(text))

    if not re.match(r"^https?://", text, re.IGNORECASE):
        text = "https://" + text
    parsed = urlparse(text)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() not in ("http", "https") or not host:
        raise InputError("Only http(s) YouTube links are supported.")

    query = parse_qs(parsed.query)
    list_id = (query.get("list") or [None])[0]

    if host == "youtu.be":
        if list_id:
            return ParsedSource("playlist", canonical_playlist_url(list_id))
        raise InputError("That is a single-video link. Enter a channel, @handle or playlist.")

    if host not in YOUTUBE_HOSTS:
        raise InputError("Only youtube.com and youtu.be links are supported.")

    if list_id:
        if not re.match(r"^[A-Za-z0-9_-]{2,100}$", list_id):
            raise InputError("The playlist id in that link is not valid.")
        return ParsedSource("playlist", canonical_playlist_url(list_id))

    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise InputError("Enter a channel, @handle or playlist link, not the YouTube home page.")
    first = parts[0]
    if first.startswith("@") and HANDLE_RE.match(first):
        return ParsedSource("channel", f"https://www.youtube.com/{first}", handle=first, tabs=tabs)
    if first == "channel" and len(parts) > 1 and CHANNEL_ID_RE.match(parts[1]):
        return ParsedSource("channel", f"https://www.youtube.com/channel/{parts[1]}", tabs=tabs)
    if first in ("c", "user") and len(parts) > 1 and re.match(r"^[\w.\-]{1,100}$", parts[1]):
        return ParsedSource("channel", f"https://www.youtube.com/{first}/{parts[1]}", tabs=tabs)
    if first in ("watch", "shorts", "live", "embed"):
        raise InputError("That is a single-video link. Enter a channel, @handle or playlist.")
    raise InputError("Could not recognise that link as a YouTube channel or playlist.")


def canonical_playlist_url(list_id_or_url: str) -> str:
    """Ensure we query the playlist endpoint even if a watch URL was provided."""
    list_id = list_id_or_url
    if "://" in list_id_or_url:
        list_id = (parse_qs(urlparse(list_id_or_url).query).get("list") or [""])[0]
    return f"https://www.youtube.com/playlist?list={list_id}"


def uploads_playlist_url(channel_id: str) -> str | None:
    """Every channel UCxxxx has an uploads playlist UUxxxx (all public uploads)."""
    if channel_id and CHANNEL_ID_RE.match(channel_id):
        return f"https://www.youtube.com/playlist?list=UU{channel_id[2:]}"
    return None


# --------------------------------------------------------------------------
# yt-dlp options
# --------------------------------------------------------------------------

class YDLLogger:
    """Route yt-dlp output to the app log instead of the console."""

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            log.debug(msg)

    def info(self, msg):
        log.debug(msg)

    def warning(self, msg):
        log.warning(msg)

    def error(self, msg):
        log.error(msg)


def base_ydl_options(settings: dict) -> dict:
    """Options shared by every yt-dlp call: conservative pacing and retries."""
    retries = int(settings.get("max_retries", 3))
    preferred = settings.get("js_runtime") or "node"
    runtime = runtime_tools.pick_js_runtime(preferred) or preferred
    return {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "logger": YDLLogger(),
        "socket_timeout": 30,
        "retries": retries,
        "extractor_retries": min(retries, 2),
        "sleep_interval_requests": float(settings.get("sleep_requests", 1.0)),
        # Exponential backoff between retries. Rate limits and bot checks are
        # not retried here: they fail fast and pause the whole queue.
        "retry_sleep_functions": {
            "http": lambda n: min(120, 5 * 2 ** n),
            "fragment": lambda n: min(60, 2 * 2 ** n),
            "extractor": lambda n: min(30, 3 * 2 ** n),
        },
        "js_runtimes": {runtime: {}},
    }


def discovery_ydl_options(settings: dict) -> dict:
    opts = base_ydl_options(settings)
    opts.update({
        "skip_download": True,
        "extract_flat": "in_playlist",
        "lazy_playlist": True,
        "ignoreerrors": False,
        # Parse "3 weeks ago" into an approximate upload date for flat listings.
        "extractor_args": {"youtubetab": {"approximate_date": [""]}},
    })
    return opts


# --------------------------------------------------------------------------
# Entry normalisation
# --------------------------------------------------------------------------

def _coerce_duration(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if value.isdigit():
            return int(value)
        parsed = parse_duration(value)
        return int(parsed) if parsed is not None else None
    return None


def extract_duration_seconds(info):
    """Extract a duration in seconds from the various yt-dlp duration fields."""
    if not info:
        return None
    for key in ("duration", "duration_seconds", "lengthSeconds", "length_seconds"):
        duration = _coerce_duration(info.get(key))
        if duration is not None:
            return duration
    duration_ms = info.get("duration_ms")
    if isinstance(duration_ms, (int, float)) and duration_ms >= 0:
        return int(duration_ms // 1000)
    return _coerce_duration(info.get("duration_string"))


_PLACEHOLDER_TITLES = {
    "[private video]": "private",
    "[deleted video]": "deleted",
    "[unavailable video]": "unavailable",
}


def normalize_entry(entry, tab=None, fallback_channel=None):
    """Convert a flat yt-dlp entry into a row for the `videos` table.

    Returns None for entries that are not individual videos (e.g. nested
    playlists or channels in a 'featured' listing).
    """
    if not isinstance(entry, dict):
        return None
    video_id = entry.get("id")
    if not video_id or not VIDEO_ID_RE.match(str(video_id)):
        return None
    ie_key = entry.get("ie_key") or ""
    if ie_key and ie_key not in ("Youtube", "YoutubeShorts"):
        return None

    title = entry.get("title") or entry.get("alt_title") or entry.get("fulltitle")
    availability = entry.get("availability")
    placeholder = _PLACEHOLDER_TITLES.get((title or "").strip().lower())
    if placeholder:
        availability = placeholder

    url = entry.get("url") or ""
    live_status = entry.get("live_status")
    if tab == "shorts" or "/shorts/" in url:
        video_type = "short"
    elif tab == "streams" or live_status in ("is_live", "was_live", "is_upcoming", "post_live"):
        video_type = "live"
    else:
        video_type = "video"

    upload_date, approx = entry.get("upload_date"), False
    if not upload_date:
        ts = entry.get("timestamp") or entry.get("release_timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            upload_date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y%m%d")
            approx = True
    if upload_date and not re.match(r"^\d{8}$", str(upload_date)):
        upload_date, approx = None, False

    fallback_channel = fallback_channel or {}
    return {
        "video_id": video_id,
        "title": title,
        "channel_id": entry.get("channel_id") or fallback_channel.get("channel_id"),
        "channel_name": entry.get("channel") or entry.get("uploader") or fallback_channel.get("channel"),
        "duration": extract_duration_seconds(entry),
        "upload_date": upload_date,
        "date_is_approx": approx,
        "video_type": video_type,
        "availability": availability,
        "live_status": live_status,
        "thumbnail": f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg",
        "url": (f"https://www.youtube.com/shorts/{video_id}" if video_type == "short"
                else f"https://www.youtube.com/watch?v={video_id}"),
        "view_count": entry.get("view_count") if isinstance(entry.get("view_count"), int) else None,
    }


def _pick_avatar(info):
    thumbs = [t for t in (info.get("thumbnails") or []) if isinstance(t, dict) and t.get("url")]
    avatars = [t for t in thumbs if "avatar" in str(t.get("id", ""))] or thumbs
    return avatars[-1]["url"] if avatars else None


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

class DiscoveryCancelled(Exception):
    pass


def iter_source(parsed: ParsedSource, settings: dict, is_cancelled=lambda: False,
                batch_size=50, ydl_factory=yt_dlp.YoutubeDL):
    """Yield discovery events for a channel or playlist.

    Events:
      ("source", {"id", "kind", "title", "handle", "url", "thumbnail"})
      ("batch", tab, [video rows])
      ("tab_skipped", tab, reason)

    yt-dlp errors propagate to the caller (the worker classifies them), except
    "this channel has no <tab> tab", which only skips that tab.
    """
    limit = int(settings.get("discovery_limit") or 0)
    opts = discovery_ydl_options(settings)
    targets = ([(tab, f"{parsed.url}/{tab}") for tab in parsed.tabs]
               if parsed.kind == "channel" else [(None, parsed.url)])

    source_emitted = False
    with ydl_factory(opts) as ydl:
        for tab, url in targets:
            if is_cancelled():
                raise DiscoveryCancelled()
            try:
                info = ydl.extract_info(url, download=False, process=False)
            except yt_dlp.utils.DownloadError as exc:
                if tab and re.search(r"does not have an? \w+ tab", str(exc)):
                    yield ("tab_skipped", tab, "This channel has no %s tab" % tab)
                    continue
                raise

            if not source_emitted:
                yield ("source", _source_meta(parsed, info))
                source_emitted = True
            fallback = {"channel_id": info.get("channel_id"),
                        "channel": info.get("channel") or info.get("uploader")}

            batch, count = [], 0
            for entry in info.get("entries") or []:
                if is_cancelled():
                    raise DiscoveryCancelled()
                row = normalize_entry(entry, tab=tab, fallback_channel=fallback)
                if row is None:
                    continue
                batch.append(row)
                count += 1
                if len(batch) >= batch_size:
                    yield ("batch", tab, batch)
                    batch = []
                if limit and count >= limit:
                    break
            if batch:
                yield ("batch", tab, batch)

    if not source_emitted:
        raise yt_dlp.utils.DownloadError(
            "ERROR: This channel does not have any videos, shorts or live tabs")


def _source_meta(parsed: ParsedSource, info: dict) -> dict:
    if parsed.kind == "channel":
        channel_id = info.get("channel_id") or info.get("id")
        title = info.get("channel") or info.get("uploader") or info.get("title") or ""
        # yt-dlp titles tabs as "Name - Videos"; keep the channel name only.
        title = re.sub(r" - (Videos|Shorts|Live)$", "", title)
        return {
            "id": channel_id,
            "kind": "channel",
            "title": title,
            "handle": info.get("uploader_id") or parsed.handle,
            "url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else parsed.url,
            "thumbnail": _pick_avatar(info),
        }
    return {
        "id": info.get("id"),
        "kind": "playlist",
        "title": info.get("title"),
        "handle": info.get("uploader_id"),
        "url": parsed.url,
        "thumbnail": _pick_avatar(info),
    }
