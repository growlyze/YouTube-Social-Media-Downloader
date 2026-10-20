"""Download one video with yt-dlp and verify the result.

Layout:  <download_dir>/<Channel>/[Shorts|Live/]<YYYY-MM-DD> - <title> [<id>].<ext>
The [<id>] suffix makes every file name unique and lets us detect a video that
is already on disk without trusting the database alone.

A download is only reported as successful when yt-dlp finished without error,
the final file exists inside the download folder, is non-empty, has no
leftover .part file, and (when FFmpeg is installed) ffprobe can read it.
"""

import glob
import logging
import os
import re
import subprocess
from pathlib import Path

import yt_dlp

from services import runtime as runtime_tools
from services.youtube_service import base_ydl_options

log = logging.getLogger("download")

PARTIAL_RE = re.compile(r"(\.part|\.ytdl|\.temp|\.part-Frag\d+.*|\.f\d+\.\w+(\.part)?|\.redownload\.\w+.*)$",
                        re.IGNORECASE)
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


class StopRequested(Exception):
    """The download was stopped on purpose. reason: 'cancel' or 'pause'."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class SkippedVideo(Exception):
    """yt-dlp refused the video on purpose (e.g. a live stream in progress)."""


class IntegrityError(Exception):
    """The downloaded file is missing, empty, outside the folder or unreadable."""


FFMPEG_REQUIRED_MESSAGE = (
    "YouTube offers no single-file (video+audio) format for this video, so FFmpeg is required "
    "to merge the separate video and audio streams. Install it with: winget install Gyan.FFmpeg "
    "(it is detected automatically), then press Retry.")


class FFmpegRequired(Exception):
    """No downloadable format exists without FFmpeg."""

    def __init__(self):
        super().__init__(FFMPEG_REQUIRED_MESSAGE)


# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------

def safe_folder_name(name: str | None, fallback="Unknown channel") -> str:
    """Make a channel name safe as a single Windows folder name."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name or "").strip(" .")
    name = re.sub(r"\s+", " ", name)[:80].strip(" .")
    if not name:
        name = fallback
    if name.upper().split(".")[0] in _RESERVED:
        name += "_"
    return name


def video_folder(download_dir: Path, video: dict) -> Path:
    folder = Path(download_dir) / safe_folder_name(video.get("channel_name"))
    if video.get("video_type") == "short":
        folder = folder / "Shorts"
    elif video.get("video_type") == "live":
        folder = folder / "Live"
    return folder


def _id_glob(folder: Path, video_id: str) -> list[str]:
    pattern = os.path.join(glob.escape(str(folder)), "**", "*" + glob.escape(f"[{video_id}]") + "*")
    return glob.glob(pattern, recursive=True)


def find_existing_file(download_dir: Path, video: dict) -> Path | None:
    """A completed file for this video id in its channel folder, if any."""
    channel_dir = Path(download_dir) / safe_folder_name(video.get("channel_name"))
    for match in _id_glob(channel_dir, video["video_id"]):
        path = Path(match)
        if path.is_file() and not PARTIAL_RE.search(path.name) and path.stat().st_size > 0:
            return path
    return None


def partial_files(download_dir: Path, video: dict) -> list[Path]:
    channel_dir = Path(download_dir) / safe_folder_name(video.get("channel_name"))
    return [Path(m) for m in _id_glob(channel_dir, video["video_id"])
            if PARTIAL_RE.search(m) and Path(m).is_file()]


def remove_partial_files(download_dir: Path, video: dict) -> int:
    removed = 0
    for path in partial_files(download_dir, video):
        try:
            path.unlink()
            removed += 1
        except OSError:
            log.warning("Could not remove partial file %s", path)
    return removed


def is_inside(path: Path, folder: Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(folder).resolve())
        return True
    except ValueError:
        return False


# --------------------------------------------------------------------------
# Formats
# --------------------------------------------------------------------------

def format_options(settings: dict, has_ffmpeg: bool) -> tuple[dict, str]:
    """yt-dlp format options for the configured quality/container."""
    quality = settings.get("video_quality", "1080p")
    container = settings.get("video_container", "mp4")
    height = int(quality[:-1]) if quality.endswith("p") and quality[:-1].isdigit() else None

    sort = []
    if height:
        sort.append(f"res:{height}")       # best resolution <= height
    if container == "mp4":
        sort.append("ext:mp4:m4a")         # prefer H.264/AAC that merges into mp4
    elif container == "webm":
        sort.append("ext:webm:webm")

    opts = {"format_sort": sort}
    label = f"{'best' if not height else '≤' + quality} {container}"
    if has_ffmpeg:
        opts["format"] = "bv*+ba/b"
        if container in ("mp4", "mkv", "webm"):
            opts["merge_output_format"] = container
    else:
        # Without FFmpeg yt-dlp cannot merge separate video+audio streams, so
        # only single-file formats work. YouTube rarely offers them any more,
        # so most videos fail with FFmpegRequired until FFmpeg is installed.
        opts["format"] = "b"
        label += " (single-file only: FFmpeg not installed)"
    return opts, label


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------

def _stage_for(info_dict: dict) -> str:
    vcodec, acodec = info_dict.get("vcodec"), info_dict.get("acodec")
    if vcodec == "none":
        return "Downloading audio"
    if acodec == "none":
        return "Downloading video"
    return "Downloading"


def verify_file(path: Path, download_dir: Path, ffprobe: str | None) -> int:
    """Raise IntegrityError unless the file looks like a finished video."""
    if not path or not path.is_file():
        raise IntegrityError(f"Download finished but the file was not found: {path}")
    if not is_inside(path, download_dir):
        raise IntegrityError("Downloaded file is outside the download folder; refusing it.")
    size = path.stat().st_size
    if size == 0:
        raise IntegrityError("Downloaded file is empty.")
    if Path(str(path) + ".part").exists():
        raise IntegrityError("A .part file still exists; the download is incomplete.")
    if ffprobe:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=120,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("ffprobe could not run on %s: %s", path, exc)
        else:
            if result.returncode != 0:
                raise IntegrityError(f"FFprobe could not read the file: {result.stderr.strip()[:200]}")
    return size


def download_video(job: dict, settings: dict, progress=lambda fields: None,
                   should_stop=lambda: None, ydl_factory=yt_dlp.YoutubeDL) -> dict:
    """Download job['video_id'] and return {file_path, file_size, format_desc, info}.

    `job` contains the download row joined with the video row.
    `progress(fields)` receives partial download-row updates.
    `should_stop()` returns None, 'cancel' or 'pause'.
    """
    download_dir = Path(settings["download_dir"])
    folder = video_folder(download_dir, job)
    folder.mkdir(parents=True, exist_ok=True)

    tools = runtime_tools.find_ffmpeg()
    fmt_opts, format_label = format_options(settings, bool(tools))

    existing = find_existing_file(download_dir, job) if job.get("redownload") else None
    suffix = ".redownload" if existing else ""
    rel_folder = os.path.relpath(folder, download_dir).replace("%", "%%")
    outtmpl = rel_folder + os.sep + "%(upload_date>%Y-%m-%d)s - %(title).150B [%(id)s]" + suffix + ".%(ext)s"

    state = {"stop": None, "skip": None, "file": None, "stage": "Downloading"}

    def hook(d):
        reason = should_stop()
        if reason:
            state["stop"] = reason
            raise yt_dlp.utils.DownloadCancelled(reason)
        if d.get("status") == "downloading":
            if d.get("filename") != state["file"]:
                state["file"] = d.get("filename")
                state["stage"] = _stage_for(d.get("info_dict") or {})
            done = d.get("downloaded_bytes")
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = round(done * 100 / total, 1) if done and total else None
            progress({"stage": state["stage"], "progress": pct, "downloaded_bytes": done,
                      "total_bytes": int(total) if total else None,
                      "speed": d.get("speed"), "eta": d.get("eta")})
        elif d.get("status") == "finished":
            progress({"stage": state["stage"] + " – done", "speed": None, "eta": 0})

    def pp_hook(d):
        if d.get("status") == "started":
            name = d.get("postprocessor") or ""
            progress({"stage": "Merging video and audio (FFmpeg)…" if "Merger" in name
                      else f"Post-processing ({name})…", "speed": None, "eta": None})

    def reject_live(info, *, incomplete=False):
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            state["skip"] = "Live stream in progress or upcoming premiere; it can't be downloaded yet."
            return state["skip"]
        return None

    opts = base_ydl_options(settings)
    opts.update(fmt_opts)
    opts.update({
        "paths": {"home": str(download_dir)},
        "outtmpl": {"default": outtmpl},
        "windowsfilenames": True,
        "continuedl": True,       # resume .part files
        "nopart": False,
        "overwrites": False,      # never clobber an existing file
        "noplaylist": True,
        "updatetime": False,
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "match_filter": reject_live,
        "fragment_retries": int(settings.get("max_retries", 3)),
    })
    if tools:
        opts["ffmpeg_location"] = tools["dir"]

    url = f"https://www.youtube.com/watch?v={job['video_id']}"
    try:
        with ydl_factory(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadCancelled:
        raise StopRequested(state["stop"] or "cancel")
    except yt_dlp.utils.DownloadError as exc:
        if not tools and "Requested format is not available" in str(exc):
            raise FFmpegRequired() from exc
        raise

    if state["skip"]:
        raise SkippedVideo(state["skip"])
    if not info:
        raise IntegrityError("yt-dlp returned no information for this video.")

    requested = info.get("requested_downloads") or [{}]
    filepath = requested[0].get("filepath") or info.get("filepath") or info.get("_filename")
    path = Path(filepath) if filepath else None
    if path and not path.is_absolute():
        path = download_dir / path

    size = verify_file(path, download_dir, tools.get("ffprobe") if tools else None)

    if suffix:
        final = path.with_name(path.name.replace(suffix + ".", ".", 1))
        if existing and existing.resolve() != final.resolve():
            existing.unlink(missing_ok=True)
        os.replace(path, final)
        path = final

    height = info.get("height")
    actual = f"{height}p {info.get('ext')}" if height else (info.get("ext") or "")
    if not tools:
        actual += " (no FFmpeg)"
    meta_keys = ("title", "channel", "channel_id", "uploader", "duration", "upload_date",
                 "availability", "live_status", "view_count")
    return {
        "file_path": str(path),
        "file_size": size,
        "format_desc": actual.strip() or format_label,
        "info": {k: info.get(k) for k in meta_keys},
    }
