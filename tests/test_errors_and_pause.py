import pytest

from services import queue_control
from services.errors import classify_error


@pytest.mark.parametrize("message, kind, pause", [
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", "rate_limited", True),
    ("ERROR: [youtube] abc: Sign in to confirm you're not a bot. Use --cookies-from-browser", "bot_check", True),
    ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot.", "bot_check", True),
    ("ERROR: [youtube] abc: Video unavailable. This content isn't available, try again later.", "rate_limited", True),
    ("ERROR: [youtube] abc: Sign in to confirm your age. This video may be inappropriate", "age_restricted", False),
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "private", False),
    ("ERROR: [Instagram] This account is private", "private", False),
    ("ERROR: [TikTok] Login required to view this video", "login_required", False),
    ("ERROR: [X] HTTP Error 429: Too Many Requests", "rate_limited", True),
    ("ERROR: DRM-protected media is not downloadable", "drm", False),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content", "members_only", False),
    ("ERROR: [youtube] abc: Video unavailable. This video has been removed by the uploader", "unavailable", False),
    ("ERROR: [youtube] abc: The uploader has not made this video available in your country", "geo_blocked", False),
    ("ERROR: [youtube:tab] @x: This channel does not have a shorts tab", "no_tab", False),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "forbidden", False),
    ("ERROR: [youtube] abc: Requested format is not available", "format", False),
    ("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", "network", False),
    ("ERROR: [youtube] abc: Premieres in 3 hours", "upcoming", False),
    ("something entirely new", "unknown", False),
])
def test_classify(message, kind, pause):
    info = classify_error(message)
    assert info.kind == kind
    assert info.pause_queue is pause


def test_ansi_codes_are_ignored():
    assert classify_error("\x1b[0;31mERROR:\x1b[0m HTTP Error 429").kind == "rate_limited"


def test_pause_state_persists(temp_db):
    assert queue_control.status() == {"paused": False}
    queue_control.pause("rate_limited", "slow down", cooldown_minutes=30)
    status = queue_control.status()
    assert status["paused"] and status["kind"] == "rate_limited"
    assert 29 * 60 <= status["cooldown_remaining"] <= 30 * 60
    queue_control.resume()
    assert not queue_control.is_paused()
