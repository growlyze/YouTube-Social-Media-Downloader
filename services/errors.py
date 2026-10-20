"""Classify yt-dlp / network error messages into actionable kinds.

The important distinction is between errors that are specific to one video
(private, deleted, members-only...) and errors that mean YouTube is pushing
back on this IP/session (HTTP 429, bot verification). The latter must pause
the whole queue instead of moving on to the next item and making it worse.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorInfo:
    kind: str
    pause_queue: bool   # stop all YouTube traffic until the user resumes
    retryable: bool     # a later retry of the same item may succeed
    message: str        # friendly explanation for the UI


# Order matters: the first matching rule wins.
_RULES = [
    ("rate_limited", True, True,
     r"HTTP Error 429|Too Many Requests|rate.?limit|"
     r"This content isn.t available, try again later|try again later",
     "YouTube is rate-limiting this connection (HTTP 429). The queue has been paused; "
     "wait before resuming."),
    ("bot_check", True, True,
     r"confirm you.re not a bot|not a bot|Sign in to confirm (?!your age)",
     "YouTube asked for bot verification. The queue has been paused; wait (often an hour or "
     "more) before resuming, or configure browser cookies."),
    ("age_restricted", False, False,
     r"confirm your age|age.restricted|inappropriate for some users",
     "Age-restricted video; it needs a signed-in session (browser cookies) to download."),
    ("members_only", False, False,
     r"members.only|Join this channel|channel members|subscriber_only",
     "Members-only video; it cannot be downloaded without a membership."),
    ("private", False, False,
     r"Private video|This video is private|is private",
     "Private video."),
    ("geo_blocked", False, False,
     r"available in your country|geo.?restrict|blocked it in your country",
     "This video is blocked in your country."),
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
     "Video or channel is unavailable (removed, deleted or does not exist)."),
    ("forbidden", False, True,
     r"HTTP Error 403|Forbidden",
     "YouTube refused the request (HTTP 403). Repeated 403s may indicate blocking."),
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
     "Network problem while contacting YouTube."),
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
