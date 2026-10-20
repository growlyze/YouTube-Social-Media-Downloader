"""Resolve public media URLs and expand supported collections with yt-dlp."""

import hashlib
import re

import yt_dlp

from services.media_platforms import MediaInputError, normalize_media_url
from services.youtube_service import VIDEO_ID_RE, base_ydl_options

MAX_COLLECTION_ITEMS = 100


def _media_id(platform: str, external_id: str) -> str:
    if platform == "youtube" and VIDEO_ID_RE.fullmatch(external_id):
        return external_id
    digest = hashlib.sha256(f"{platform}:{external_id}".encode("utf-8")).hexdigest()[:24]
    return f"{platform}_{digest}"


def _duration(value):
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return None


def _thumbnail(entry):
    thumbnails = entry.get("thumbnails")
    if isinstance(thumbnails, list):
        for item in reversed(thumbnails):
            if isinstance(item, dict) and item.get("url"):
                return item["url"]
    return entry.get("thumbnail")


def _record(entry, source_url, outer_adapter, is_collection):
    item_url = outer_adapter.item_url(entry, source_url, is_collection)
    item_url, adapter = normalize_media_url(item_url)
    external_id = str(entry.get("id") or hashlib.sha256(item_url.encode("utf-8")).hexdigest()[:24])
    upload_date = entry.get("upload_date")
    if not isinstance(upload_date, str) or not re.fullmatch(r"\d{8}", upload_date):
        upload_date = None
    availability = entry.get("availability")
    return {
        "video_id": _media_id(adapter.key, external_id),
        "external_id": external_id,
        "platform": adapter.key,
        "title": str(entry.get("title") or f"{adapter.label} media")[:500],
        "channel_name": str(entry.get("channel") or entry.get("uploader")
                            or entry.get("creator") or adapter.label)[:160],
        "channel_id": entry.get("channel_id"),
        "duration": _duration(entry.get("duration")),
        "upload_date": upload_date,
        "date_is_approx": False,
        "video_type": "video",
        "availability": availability or "public",
        "live_status": entry.get("live_status"),
        "thumbnail": _thumbnail(entry),
        "url": item_url,
        "view_count": entry.get("view_count") if isinstance(entry.get("view_count"), int) else None,
    }


def resolve_media_url(url, settings=None, ydl_factory=None):
    """Return normalized media records; expand collections up to the safe cap."""
    normalized, adapter = normalize_media_url(url)
    options = base_ydl_options(settings or {})
    options.update({
        "skip_download": True,
        "extract_flat": "in_playlist",
        "ignoreerrors": False,
        "lazy_playlist": True,
        "noplaylist": False,
        "playlistend": MAX_COLLECTION_ITEMS + 1,
        "socket_timeout": 30,
    })
    factory = ydl_factory or yt_dlp.YoutubeDL
    with factory(options) as ydl:
        info = ydl.extract_info(normalized, download=False)
    if not info:
        raise MediaInputError("No media was found at that URL.")

    entries = info.get("entries")
    if entries is None:
        if info.get("_type") in ("playlist", "multi_video"):
            raise MediaInputError("No accessible media items were found in that collection.")
        entries = [info]
        is_collection = False
    else:
        entries = list(entries)
        is_collection = True
    truncated = len(entries) > MAX_COLLECTION_ITEMS
    entries = entries[:MAX_COLLECTION_ITEMS]
    records = []
    errors = []
    skipped = sum(not isinstance(entry, dict) for entry in entries)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            records.append(_record(entry, normalized, adapter, is_collection))
        except MediaInputError as exc:
            errors.append(str(exc))
    if not records:
        if errors:
            raise MediaInputError(errors[0])
        raise MediaInputError("No downloadable media items were found at that URL.")
    warnings = []
    if skipped:
        warnings.append(f"Skipped {skipped} collection item(s) yt-dlp could not resolve.")
    if errors:
        warnings.append(f"Skipped {len(errors)} item(s) without a supported page URL.")
    return {"items": records, "truncated": truncated, "platform": adapter.key, "warnings": warnings}
