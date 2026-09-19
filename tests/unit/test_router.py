import pytest

from stuff_downloader.core.errors import friendly_message
from stuff_downloader.core.router import (
    HANDLE_UNSUPPORTED_REASON,
    SITE_CREDENTIALS_REASON,
    SITE_PRIVATE_HOST_REASON,
    durable_url,
    route,
)

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize(
    "text",
    [
        f"https://www.youtube.com/watch?v={VID}",
        f"  https://youtube.com/watch?v={VID}&t=42s&si=tracking  ",
        f"http://m.youtube.com/watch?feature=share&v={VID}",
        f"https://youtu.be/{VID}?si=abc",
        f"https://www.youtube.com/shorts/{VID}",
        f"https://www.youtube.com/live/{VID}?feature=share",
        f"https://www.youtube.com/embed/{VID}",
        f"HTTPS://WWW.YOUTUBE.COM/watch?v={VID}",
    ],
)
def test_youtube_links_normalize_to_clean_watch_url(text):
    r = route(text)
    assert r.ok and r.engine == "ytdlp"
    assert r.url == f"https://www.youtube.com/watch?v={VID}"
    assert r.video_id == VID and not r.music and not r.playlist_id


def test_music_link_keeps_music_host():
    r = route(f"https://music.youtube.com/watch?v={VID}&feature=share")
    assert r.ok and r.music
    assert r.url == f"https://music.youtube.com/watch?v={VID}"


def test_watch_with_list_flags_playlist_but_downloads_single_video():
    r = route(f"https://www.youtube.com/watch?v={VID}&list=PLabc123_-XYZ&index=3")
    assert r.ok and r.playlist_id == "PLabc123_-XYZ"
    assert "list" not in r.url


def test_channel_url_derives_the_public_uploads_playlist():
    channel_id = "UC" + "A" * 22
    r = route(f"https://www.youtube.com/channel/{channel_id}?feature=share")
    assert r.ok and r.is_playlist
    assert r.playlist_id == "UU" + "A" * 22
    assert r.url == f"https://www.youtube.com/playlist?list=UU{'A' * 22}"


@pytest.mark.parametrize(
    "text",
    [
        "https://www.youtube.com/@example",
        "https://music.youtube.com/@example/videos",
    ],
)
def test_handle_urls_are_refused_honestly(text):
    r = route(text)
    assert r.kind == "unsupported" and r.reason == HANDLE_UNSUPPORTED_REASON


@pytest.mark.parametrize(
    "text",
    [
        "https://www.youtube.com/channel/not-a-channel",
        "https://www.youtube.com/channel/UC" + "A" * 21,
        "https://www.youtube.com/channel/UC" + "A" * 22 + "/videos",
    ],
)
def test_invalid_channel_urls_are_rejected(text):
    r = route(text)
    assert r.kind == "invalid" and not r.ok and r.reason


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "not a url",
        f"file:///C:/watch?v={VID}",
        f"javascript:alert(1)//youtube.com/watch?v={VID}",
        f"ftp://youtube.com/watch?v={VID}",
        "https://www.youtube.com/watch?v=short",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ%22%3E",
        "https://youtu.be/",
        f"https://www.youtube.com/watch?v={VID} https://evil.example/",
        "https://www.youtube.com/watch?v=" + "a" * 3000,
        "https://example.com:99999/x",  # a port urlsplit will not parse
        "https://example.com:abc/x",
    ],
)
def test_invalid_input_is_rejected(text):
    r = route(text)
    assert r.kind == "invalid" and not r.ok and r.reason and r.url == ""


@pytest.mark.parametrize(
    "text",
    [
        "https://www.youtube.com/playlist?list=PLabc",  # a YouTube list we cannot enumerate
        "https://www.youtube.com/@example/live",
    ],
)
def test_unusable_youtube_links_are_unsupported(text):
    r = route(text)
    assert r.kind == "unsupported" and not r.ok and r.reason


# --- M3: public video pages on other sites -------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://vimeo.com/123456789",
        "https://www.tiktok.com/@someone/video/7123456789012345678",
        "https://www.facebook.com/watch/?v=1234567890",
        # A lookalike host is not YouTube, but it is still a public site we may try.
        f"https://www.youtube.com.example.test.example/watch?v={VID}",
    ],
)
def test_public_site_links_route_to_the_video_engine(text):
    r = route(text)
    assert r.kind == "video" and r.ok and not r.is_youtube
    assert r.engine == "ytdlp"
    assert not r.video_id and not r.playlist_id and not r.is_playlist


def test_site_url_drops_the_fragment_and_normalizes_case_but_keeps_the_query():
    r = route("HTTPS://WWW.Vimeo.COM/channels/staff/123?quality=1080p#t=30")
    assert r.kind == "video"
    assert r.url == "https://www.vimeo.com/channels/staff/123?quality=1080p"


def test_site_url_keeps_an_explicit_port():
    r = route("https://videos.example.com:8443/watch/abc")
    assert r.url == "https://videos.example.com:8443/watch/abc"


@pytest.mark.parametrize(
    "text",
    [
        "https://localhost/watch/1",
        "http://127.0.0.1:8080/watch/1",
        "http://192.168.1.10/video.mp4",
        "http://[::1]/watch/1",
        "http://10.0.0.5/watch/1",
        "http://203.0.113.9/watch/1",  # an IP literal, public or not, is refused by name
        "http://nas.local/movies/1",
        "http://intranet/video",
        "http://router.home.arpa/cam",
    ],
)
def test_links_to_this_machine_or_a_private_network_are_refused(text):
    r = route(text)
    assert r.kind == "unsupported" and not r.ok
    assert r.reason == SITE_PRIVATE_HOST_REASON


@pytest.mark.parametrize(
    "text",
    [
        f"https://user@evil.example/youtu.be/{VID}",
        "https://user:pass@vimeo.com/123",
        f"https://user:pass@www.youtube.com/watch?v={VID}",
    ],
)
def test_links_carrying_credentials_are_refused_on_every_host(text):
    r = route(text)
    assert r.kind == "unsupported" and not r.ok
    assert r.reason == SITE_CREDENTIALS_REASON
    assert "pass" not in r.url and r.url == ""


@pytest.mark.parametrize(
    "text",
    [
        "https://open.spotify.com/track/abc",
        "https://vimeo.com",
        "https://vimeo.com/",
    ],
)
def test_sites_we_do_not_serve_yet_are_refused_by_name(text):
    r = route(text)
    assert r.kind == "unsupported" and not r.ok and r.reason


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("ERROR: Unsupported URL: https://example.test/x", "not a video page"),
        ("ERROR: [generic] video: No video formats found", "No downloadable video"),
        ("This video is DRM protected", "DRM"),
        ("ERROR: [instagram] Login required to access this post", "not public"),
        ("ERROR: [tiktok] This account is private", "private"),
        ("HTTP Error 429: Too Many Requests", "rate-limiting"),
        ("ERROR: [x] Requested content is not available", "not available"),
        ("ERROR: [generic] Unable to extract player data", "site may have changed"),
        ("HTTP Error 401: Unauthorized", "not public"),
        ("HTTP Error 404: Not Found", "no longer exists"),
        ("HTTP Error 503: Service Unavailable", "server error"),
        ("ERROR: The video is not available from your location", "region"),
    ],
)
def test_other_site_failures_read_as_plain_language(message, expected):
    assert expected.lower() in friendly_message("download_error", message).lower()


def test_an_unsupported_engine_message_is_still_shown_as_written():
    # The engine's own wording is more useful than a generic line, and it is URL-free.
    assert friendly_message("unsupported", "that link is not a playlist") == (
        "that link is not a playlist"
    )


# --- Security rework: every spelling of a local address, and what may be kept on disk -------


@pytest.mark.parametrize(
    "host",
    [
        # The canonical forms, which were already refused.
        "127.0.0.1",
        "localhost",
        "[::1]",
        "192.168.1.10",
        "10.0.0.5",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        # inet_aton shorthand: these all mean 127.0.0.1 and used to route as public sites.
        "127.1",
        "127.0.1",
        "0177.0.0.1",
        "0x7f.1",
        "0x7f.0.0.1",
        "2130706433",
        "017700000001",
        "0x7f000001",
        # A public address is refused too: the rule is the shape, not the range.
        "8.8.8.8",
        "1.1",
        # Cosmetic variations that must not reopen any of the above.
        "127.0.0.1.",
        "LOCALHOST",
        "ｌｏｃａｌｈｏｓｔ",  # fullwidth "localhost"
    ],
)
def test_no_spelling_of_a_local_or_numeric_host_is_routable(host):
    r = route(f"http://{host}/watch/1")
    assert r.kind == "unsupported" and not r.ok
    assert r.url == ""


@pytest.mark.parametrize(
    "host",
    [
        "vimeo.com",
        "www.tiktok.com",
        "videos.example.com",
        "example.co.uk",
        "a1.example.com",  # digits in a label are fine; an all-numeric host is not
        "1a.example.com",
        "x.1.example.com",
    ],
)
def test_named_public_sites_still_route(host):
    assert route(f"https://{host}/video/1").kind == "video"


def test_durable_url_keeps_a_youtube_link_whole_and_replayable():
    for url in (
        f"https://www.youtube.com/watch?v={VID}",
        f"https://music.youtube.com/watch?v={VID}",
        f"https://youtu.be/{VID}",
        "https://www.youtube.com/playlist?list=PLabc123_-XYZ",
    ):
        assert durable_url(url) == (url, False)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://vimeo.com/123?token=SECRET", "https://vimeo.com/123"),
        ("https://vimeo.com/123#t=30", "https://vimeo.com/123"),
        ("https://vimeo.com/123?a=1&sig=SECRET#x", "https://vimeo.com/123"),
        ("https://videos.example.com:8443/v/1?k=SECRET", "https://videos.example.com:8443/v/1"),
    ],
)
def test_durable_url_drops_a_generic_links_query_and_says_it_did(url, expected):
    safe, redacted = durable_url(url)
    assert safe == expected and redacted is True
    assert "SECRET" not in safe and "#" not in safe and "?" not in safe


def test_durable_url_reports_no_loss_when_there_was_nothing_to_remove():
    assert durable_url("https://vimeo.com/123") == ("https://vimeo.com/123", False)


def test_durable_url_never_raises_on_a_malformed_link():
    # A link we cannot parse is reported as unusable rather than stored half-understood.
    assert durable_url("https://example.com:99999/x") == ("", True)
    # An empty link has nothing to remove, so nothing is claimed to have been removed.
    assert durable_url("") == ("", False)
