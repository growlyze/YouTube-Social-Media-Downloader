"""Detect external tools yt-dlp relies on: FFmpeg and a JavaScript runtime.

FFmpeg installed with `winget install Gyan.FFmpeg` lands under
%LOCALAPPDATA%\\Microsoft\\WinGet; the running app's PATH is not refreshed
after an install, so those folders are searched directly. Results are cached
briefly so a fresh install is picked up without restarting the app.
"""

import glob
import os
import shutil
import time
from pathlib import Path

_CACHE_SECONDS = 30
_cache: dict = {}

JS_RUNTIME_EXES = {"deno": "deno", "node": "node", "bun": "bun", "quickjs": "qjs"}


def _cached(key, finder):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    value = finder()
    _cache[key] = (time.monotonic(), value)
    return value


def _ffmpeg_candidates():
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        yield Path(local, "Microsoft", "WinGet", "Links")
        for match in glob.glob(os.path.join(local, "Microsoft", "WinGet", "Packages",
                                            "Gyan.FFmpeg*", "*", "bin")):
            yield Path(match)
    for root in (r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"):
        yield Path(root)


def _find_ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe and shutil.which("ffprobe"):
        return {"ffmpeg": exe, "ffprobe": shutil.which("ffprobe"), "dir": str(Path(exe).parent)}
    for folder in _ffmpeg_candidates():
        ffmpeg, ffprobe = folder / "ffmpeg.exe", folder / "ffprobe.exe"
        if ffmpeg.is_file():
            return {"ffmpeg": str(ffmpeg), "ffprobe": str(ffprobe) if ffprobe.is_file() else None,
                    "dir": str(folder)}
    return None


def find_ffmpeg():
    """Return {'ffmpeg', 'ffprobe', 'dir'} or None when FFmpeg is not installed."""
    return _cached("ffmpeg", _find_ffmpeg)


def available_js_runtimes():
    return _cached("js", lambda: [name for name, exe in JS_RUNTIME_EXES.items() if shutil.which(exe)])


def pick_js_runtime(preferred: str):
    """Use the preferred runtime if installed, otherwise the first one found."""
    found = available_js_runtimes()
    if preferred in found:
        return preferred
    return found[0] if found else None


def status(preferred_js: str) -> dict:
    ffmpeg = find_ffmpeg()
    js = pick_js_runtime(preferred_js)
    return {
        "ffmpeg": bool(ffmpeg),
        "ffmpeg_path": ffmpeg["ffmpeg"] if ffmpeg else None,
        "js_runtime": js,
        "js_runtime_preferred": preferred_js,
        "js_runtimes_found": available_js_runtimes(),
    }
