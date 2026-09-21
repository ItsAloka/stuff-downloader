"""Plain-language error messages for worker errors. No Qt imports.

Rules are tried in order, so the specific ones come first: a site that says "login required"
must not be reported as a generic extractor failure. Since M3 a link can name any site, the
wording avoids naming YouTube unless the rule is YouTube's own.
"""

from __future__ import annotations

import re

# A header value that slipped into failure text: everything after the name is removed. Mirrors
# the worker's site_login.redact_secrets, so the boundary that writes history does not depend
# on every worker engine having remembered to do it.
_SECRET = re.compile(
    r"(?i)\b(set-cookie|cookie|authorization|proxy-authorization|x-csrf-token)\b['\"]?\s*[:=]"
    r"\s*[^\r\n]*"
)


def redact_secrets(text: str) -> str:
    return _SECRET.sub(lambda m: f"{m.group(1)}: [removed]", text)

NOT_PUBLIC = "That video is not public on the site."

# (substring in the engine message, lower-case) -> message shown to the owner
_MESSAGE_RULES: tuple[tuple[str, str], ...] = (
    # Not public. Most specific first: "Private video. Sign in" is a private video, not a login.
    ("private video", "This video is private on the site."),
    ("sign in to confirm your age", "This video is age-restricted and needs a signed-in account."),
    ("account is private", "That account is private, so its videos cannot be downloaded."),
    ("members-only", "This video is for channel members only."),
    ("join this channel", "This video is for channel members only."),
    ("login required", NOT_PUBLIC),
    ("log in to", NOT_PUBLIC),
    ("sign in to", NOT_PUBLIC),
    ("http error 401", NOT_PUBLIC),
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
    "cookies_unavailable": (
        "The site login you chose could not be read. Close the browser, or pick a fresh"
        " cookies.txt file, and try again."
    ),
}

# Failures that mean "the site only shows this to signed-in viewers" (plan §6.4). Only these
# offer the advanced site-login option; every other failure is something cookies cannot fix,
# and offering a login there would only teach the owner to hand over a secret for nothing.
_NEEDS_LOGIN = (
    "private video",
    "sign in to confirm your age",
    "account is private",
    "members-only",
    "join this channel",
    "login required",
    "log in to",
    "sign in to",
    "http error 401",
)


def needs_site_login(code: str | None, message: str | None) -> bool:
    """Whether a failure is one a site login might fix. Never true for a cookie failure."""
    if code == "cookies_unavailable":
        return False
    text = (message or "").lower()
    return any(needle in text for needle in _NEEDS_LOGIN)


def friendly_message(code: str | None, message: str | None) -> str:
    text = (message or "").lower()
    for needle, friendly in _MESSAGE_RULES:
        if needle in text:
            return friendly
    if code in _CODE_MESSAGES:
        return _CODE_MESSAGES[code]
    return redact_secrets(message or "Unknown error").strip()[:300]
