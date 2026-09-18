"""Plain-language error messages for worker errors. No Qt imports."""

from __future__ import annotations

# (substring in the engine message, lower-case) -> message shown to the owner
_MESSAGE_RULES: tuple[tuple[str, str], ...] = (
    ("private video", "This video is private on the site."),
    ("sign in to confirm your age", "This video is age-restricted and needs a signed-in account."),
    ("confirm you're not a bot", "YouTube asked for a bot check. Try again later."),
    ("confirm you’re not a bot", "YouTube asked for a bot check. Try again later."),
    ("members-only", "This video is for channel members only."),
    ("join this channel", "This video is for channel members only."),
    ("available in your country", "This video is blocked in your region."),
    ("geo restrict", "This video is blocked in your region."),
    ("premieres in", "This video has not premiered yet."),
    ("live event will begin", "This live stream has not started yet."),
    ("video unavailable", "This video is unavailable."),
    ("video is unavailable", "This video is unavailable."),
    ("has been removed", "This video has been removed."),
    ("http error 403", "The site refused the download (HTTP 403). Updating engines may help."),
    ("http error 429", "The site is rate-limiting requests. Wait a bit and retry."),
    ("unable to download webpage", "Could not reach the site. Check your connection."),
    ("getaddrinfo failed", "Could not reach the site. Check your connection."),
    ("timed out", "The connection timed out. Check your connection and retry."),
    ("ffmpeg is not installed", "FFmpeg is needed for this download. Check the Tools page."),
    ("ffmpeg not found", "FFmpeg is needed for this download. Check the Tools page."),
    ("requested format is not available", "That quality is no longer available. Try Auto/Best."),
)

_CODE_MESSAGES = {
    "cancelled": "Cancelled by you",
    "timeout": "Analyzing took too long and was stopped. Try again.",
    "engine_missing": "The download engine is not installed. Check the engine runtime.",
    "bad_options": "The download settings were rejected. This is a bug, please report it.",
    "no_output": "The download finished but no file was produced.",
    "worker_exited": "The downloader stopped unexpectedly.",
}


def friendly_message(code: str | None, message: str | None) -> str:
    text = (message or "").lower()
    for needle, friendly in _MESSAGE_RULES:
        if needle in text:
            return friendly
    if code in _CODE_MESSAGES:
        return _CODE_MESSAGES[code]
    return (message or "Unknown error").strip()[:300]
