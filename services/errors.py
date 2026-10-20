"""Classify yt-dlp / network errors into actionable, platform-neutral kinds."""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorInfo:
    kind: str
    pause_queue: bool   # pause automated platform requests until the user resumes
    retryable: bool     # a later retry of the same item may succeed
    message: str        # friendly explanation for the UI


# Order matters: the first matching rule wins.
_RULES = [
    ("rate_limited", True, True,
     r"HTTP Error 429|Too Many Requests|rate.?limit|"
     r"This content isn.t available, try again later|try again later",
     "The platform is rate-limiting this connection (HTTP 429). The queue has been paused; "
     "wait before resuming."),
    ("bot_check", True, True,
     r"confirm you.re not a bot|not a bot|Sign in to confirm (?!your age)",
     "The platform requested sign-in or an account check. This app does not bypass access "
     "controls; wait before resuming or choose public media."),
    ("age_restricted", False, False,
     r"confirm your age|age.restricted|inappropriate for some users",
     "Age-restricted media requires sign-in; this app only downloads media available without "
     "authentication."),
    ("members_only", False, False,
     r"members.only|Join this channel|channel members|subscriber_only",
     "Members-only video; it cannot be downloaded without a membership."),
    ("private", False, False,
     r"Private video|This video is private|account is private|private account|is private|"
     r"protected (?:account|tweet|post)|only available to followers",
     "Private video or account content cannot be downloaded."),
    ("login_required", False, False,
     r"login required|requires? (?:a )?(?:login|sign.?in)|you must be logged in|"
     r"sign in to (?:view|download|access)|authentication required|log in to continue|"
     r"need to (?:log in|login|sign in)|only available to (?:registered|logged.?in|authenticated) users",
     "This media requires sign-in. This app does not use account cookies or bypass access controls."),
    ("drm", False, False,
     r"\bdrm\b|widevine|digital rights management",
     "DRM-protected media is not supported."),
    ("geo_blocked", False, False,
     r"available in your country|geo.?restrict|blocked it in your country",
     "This media is not available in your region."),
    ("upcoming", False, True,
     r"Premieres in|live event will begin|This live event|is_upcoming|Scheduled",
     "Upcoming premiere or live stream; it is not downloadable yet."),
    ("no_tab", False, False,
     r"does not have an? \w+ tab",
     "This channel does not have this tab."),
    ("unavailable", False, False,
     r"Video unavailable|has been removed|no longer available|does not exist|"
     r"account associated with this video has been terminated|copyright claim|"
     r"This channel does not exist|HTTP Error 404|playlist does not exist|"
     r"The playlist does not exist",
     "Media is unavailable (removed, deleted, or no longer exists)."),
    ("forbidden", False, True,
     r"HTTP Error 403|Forbidden",
     "The platform refused the request (HTTP 403). Repeated refusals may indicate temporary blocking."),
    ("js_runtime", False, False,
     r"JavaScript runtime|js.runtime|n challenge|Signature solving failed|EJS",
     "yt-dlp could not run its JavaScript challenge solver. Check the JavaScript runtime "
     "setting (Node.js is installed on this PC)."),
    ("format", False, False,
     r"Requested format is not available|No video formats found",
     "The requested quality/format is not available for this video."),
    ("ffmpeg", False, False,
     r"ffmpeg|ffprobe",
     "FFmpeg is missing or failed. Install FFmpeg (winget install Gyan.FFmpeg)."),
    ("network", False, True,
     r"timed out|timeout|getaddrinfo|Name or service not known|Connection (reset|refused|aborted)|"
     r"Failed to establish|Network is unreachable|Remote end closed|IncompleteRead|"
     r"Unable to download (webpage|API page)|SSL",
     "Network problem while contacting the platform."),
    ("disk", False, False,
     r"No space left|Errno 28|Permission denied|Errno 13",
     "Local disk error (full disk or permission denied)."),
]

_COMPILED = [(kind, pause, retry, re.compile(pattern, re.IGNORECASE), msg)
             for kind, pause, retry, pattern, msg in _RULES]

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def clean_message(text) -> str:
    text = _ANSI.sub("", str(text or "")).strip()
    return text.removeprefix("ERROR: ").strip()


def classify_error(error) -> ErrorInfo:
    text = clean_message(error)
    for kind, pause, retry, pattern, msg in _COMPILED:
        if pattern.search(text):
            return ErrorInfo(kind, pause, retry, msg)
    return ErrorInfo("unknown", False, True, text[:300] or "Unknown error")
