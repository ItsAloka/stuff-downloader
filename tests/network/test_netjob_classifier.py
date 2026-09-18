"""The transient/permanent split, tested offline because it is the risky half of netjob.

Deliberately NOT marked `network`: it runs in the default suite, downloads nothing, and needs no
engine runtime. The retry logic it guards only runs during network tests, but getting it wrong is
how a real defect gets retried into a green board -- so it is checked on every ordinary run.

The dangerous direction is a false positive. If "Video unavailable" or a bad merge were ever
classified transient, the suite would retry three times and then SKIP, and a genuine regression
would leave the board green with a reason nobody reads.
"""

from __future__ import annotations

import netjob
import pytest

TRANSIENT = [
    "ERROR: unable to download video data: HTTP Error 403: Forbidden",
    "HTTP Error 429: Too Many Requests",
    "HTTP Error 503: Service Unavailable",
    "Sign in to confirm you're not a bot",
    "The server is throttling the connection",
    "rate-limited, try again later",
    "Connection reset by peer",
    "read operation timed out",
    "The remote end closed the connection without response",
]

PERMANENT = [
    "ERROR: [youtube] aaaaaaaaaa0: Video unavailable",
    "ERROR: [youtube] aaaaaaaaaa0: This video is unavailable",
    "ERROR: Private video. Sign in if you have been granted access to this video",
    "ERROR: [youtube] aaaaaaaaaa0: Video unavailable. This video contains content from X",
    "no media information found",
    "ffmpeg could not merge the streams",
    "Requested format is not available",
    "ERROR: unable to open for writing: [Errno 13] Permission denied",
]


@pytest.mark.parametrize("message", TRANSIENT)
def test_a_provider_refusing_to_serve_us_is_transient(message):
    assert netjob.is_transient({"message": message}) is True


@pytest.mark.parametrize("message", PERMANENT)
def test_a_real_failure_is_never_transient(message):
    """The direction that matters: these must fail the suite, not skip it."""
    assert netjob.is_transient({"message": message}) is False


@pytest.mark.parametrize("data", [None, {}, "a string", 42, {"message": None}, {"code": "x"}])
def test_anything_unrecognisable_is_treated_as_a_real_failure(data):
    """Fail closed here too: if we cannot tell what happened, we do not retry it away."""
    assert netjob.is_transient(data) is False
