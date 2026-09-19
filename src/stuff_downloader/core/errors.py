"""Plain-language error messages for worker errors. No Qt imports.

Rules are tried in order, so the specific ones come first: a site that says "login required"
must not be reported as a generic extractor failure. Since M3 a link can name any site, the
wording avoids naming YouTube unless the rule is YouTube's own.
"""

from __future__ import annotations

# (substring in the engine message, lower-case) -> message shown to the owner
_MESSAGE_RULES: tuple[tuple[str, str], ...] = (
    # Not public. Most specific first: "Private video. Sign in" is a private video, not a login.
    ("private video", "This video is private on the site."),
    ("sign in to confirm your age", "This video is age-restricted and needs a signed-in account."),
    ("account is private", "That account is private, so its videos cannot be downloaded."),
    ("members-only", "This video is for channel members only."),
    ("join this channel", "This video is for channel members only."),
    ("login required", "That video is not public. Signing in is not supported yet."),
    ("log in to", "That video is not public. Signing in is not supported yet."),
    ("sign in to", "That video is not public. Signing in is not supported yet."),
    ("http error 401", "That video is not public. Signing in is not supported yet."),
    # Playable nowhere we can reach it.
    ("drm protect", "This video is DRM protected and cannot be downloaded."),
    ("protected by drm", "This video is DRM protected and cannot be downloaded."),
    ("available in your country", "This video is blocked in your region."),
    ("geo restrict", "This video is blocked in your region."),
    ("not available from your location", "This video is blocked in your region."),
    ("premieres in", "This video has not premiered yet."),
    ("live event will begin", "This live stream has not started yet."),
    ("video unavailable", "This video is unavailable."),
    ("video is unavailable", "This video is unavailable."),
    ("requested content is not available", "That video is not available to download."),
    ("has been removed", "This video has been removed."),
    ("http error 404", "That page no longer exists on the site."),
    # The site is refusing us, or is unwell.
    ("confirm you're not a bot", "YouTube asked for a bot check. Try again later."),
    ("confirm you’re not a bot", "YouTube asked for a bot check. Try again later."),
    ("http error 403", "The site refused the download (HTTP 403). Updating engines may help."),
    ("http error 429", "The site is rate-limiting requests. Wait a bit and retry."),
    ("too many requests", "The site is rate-limiting requests. Wait a bit and retry."),
    ("rate-limit", "The site is rate-limiting requests. Wait a bit and retry."),
    ("http error 5", "The site had a server error. Try again later."),
    # No extractor, or the extractor found nothing. Last, so a reason above wins over these.
    ("unsupported url", "That link is not a video page we can read."),
    ("no video formats found", "No downloadable video was found on that page."),
    ("no media information found", "No downloadable video was found on that page."),
    ("unable to extract", "That page did not give us a video. The site may have changed."),
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
