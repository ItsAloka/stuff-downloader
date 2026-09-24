import pytest

from stuff_downloader.core.errors import friendly_message
from stuff_downloader.core.router import (
    HANDLE_UNSUPPORTED_REASON,
    SITE_CREDENTIALS_REASON,
    SITE_PRIVATE_HOST_REASON,
    durable_url,
    route,
    social_group,
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
        "https://spotify.com/premium",
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
        ("ERROR: Unsupported URL: https://example.test/x", "video, photo or file"),
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


# ── Spotify (plan §6.3, §M5) ───────────────────────────────────────────────────────────────
SPOTIFY_TRACK = "4cOdK2wGLETKBW3PvgPWqT"


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"https://open.spotify.com/track/{SPOTIFY_TRACK}?si=abc123&utm_source=x", "track"),
        (f"https://open.spotify.com/intl-de/track/{SPOTIFY_TRACK}", "track"),
        (f"https://open.spotify.com/embed/album/{SPOTIFY_TRACK}", "album"),
        (f"http://OPEN.SPOTIFY.COM/playlist/{SPOTIFY_TRACK}#frag", "playlist"),
        (f"https://play.spotify.com/album/{SPOTIFY_TRACK}", "album"),
    ],
)
def test_spotify_links_are_rebuilt_from_kind_and_id(text, kind):
    r = route(text)
    assert r.kind == "spotify" and r.ok and r.is_spotify and r.engine == "spotdl"
    assert r.spotify_kind == kind and r.spotify_id == SPOTIFY_TRACK
    # Nothing from the pasted text survives except the kind and the id.
    assert r.url == f"https://open.spotify.com/{kind}/{SPOTIFY_TRACK}"


@pytest.mark.parametrize(
    "text",
    [
        f"https://open.spotify.com/artist/{SPOTIFY_TRACK}",
        f"https://open.spotify.com/show/{SPOTIFY_TRACK}",
        f"https://open.spotify.com/episode/{SPOTIFY_TRACK}",
        "https://open.spotify.com/user/someone",
        "https://open.spotify.com/",
    ],
)
def test_spotify_pages_that_are_not_a_chosen_list_are_refused_by_name(text):
    r = route(text)
    assert r.kind == "unsupported" and not r.ok and "spotify" in r.reason.lower()


@pytest.mark.parametrize(
    "text",
    [
        "https://open.spotify.com/track/abc",
        f"https://open.spotify.com/track/{SPOTIFY_TRACK}x",
        f"https://open.spotify.com/track/{SPOTIFY_TRACK[:-1]}!",
        f"https://open.spotify.com/track/{SPOTIFY_TRACK}/extra",
        "https://open.spotify.com/track/",
    ],
)
def test_a_malformed_spotify_id_is_invalid(text):
    r = route(text)
    assert r.kind == "invalid" and not r.ok and r.url == "" and "spotify" in r.reason.lower()


def test_spotify_jobs_share_one_rate_limited_group():
    assert social_group(f"https://open.spotify.com/track/{SPOTIFY_TRACK}") == "spotify"


# ── P14: scheme-less links, neutral wording, the analyze chain (plan §5.3, R1) ─────────────
from stuff_downloader.core import router as _router  # noqa: E402


@pytest.mark.parametrize(
    ("text", "kind", "url"),
    [
        (
            "pbs.twimg.com/media/Gx1AbC?format=jpg&name=large",
            "video",
            "https://pbs.twimg.com/media/Gx1AbC?format=jpg&name=large",
        ),
        ("cdn.example.com/a/photo.jpg", "file", "https://cdn.example.com/a/photo.jpg"),
        (
            "www.youtube.com/watch?v=dQw4w9WgXcQ",
            "youtube",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        ),
        ("x.com/someone/status/1/photo/1", "gallery", "https://x.com/someone/status/1/photo/1"),
    ],
)
def test_a_link_without_a_scheme_gets_https_and_a_note(text, kind, url):
    r = route(text)
    assert (r.kind, r.url) == (kind, url)
    assert r.note == _router.ADDED_SCHEME_NOTE


@pytest.mark.parametrize(
    "text",
    [
        "localhost/x",
        "192.168.1.10/cam.jpg",
        "printer.local/scan.png",
        "mailto:someone@example.com",
        "javascript:alert(1)",
        "just-a-word",
        "//example.com/a",
    ],
)
def test_a_fragment_that_is_not_a_public_host_is_refused_neutrally(text):
    r = route(text)
    assert not r.ok and not r.note
    assert r.reason == _router.INCOMPLETE_LINK_REASON
    assert "video" not in r.reason.lower()


def test_a_full_link_gets_no_note():
    assert route("https://cdn.example.com/a/photo.jpg").note == ""


def test_router_refusals_never_assume_the_link_is_a_video():
    for reason in (
        _router.SITE_UNSUPPORTED_REASON,
        _router.INCOMPLETE_LINK_REASON,
        route("example.com").reason,
        route("ftp://example.com/a.jpg").reason,
    ):
        assert reason and "video page" not in reason.lower()


def test_the_analyze_chain_is_decided_in_core():
    page = route("https://www.example.com/post/123")
    assert page.kind == "video"
    chain = _router.analyze_fallbacks(page)
    assert [(r.kind, r.engine) for r in chain] == [
        ("gallery", "gallerydl"),
        ("file", "http"),
        ("page", "social"),  # R4: the page's own og:image, last
    ]
    assert all(r.url == page.url for r in chain)
    # Nothing else falls back, and a direct file is only ever probed after the page engines.
    for text in (
        "https://cdn.example.com/a.mp4",
        "https://www.instagram.com/some.user/",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        f"https://open.spotify.com/track/{SPOTIFY_TRACK}",
    ):
        assert _router.analyze_fallbacks(route(text)) == []


@pytest.mark.parametrize(
    "text",
    [
        "https://www.instagram.com/p/DdHWXPMyCL0/",
        "https://instagram.com/p/C0ffee123/?img_index=2",
        "https://www.instagram.com/some.user/p/C0ffee123/",
        "https://www.instagram.com/reel/C0ffee123/",
        "https://www.instagram.com/tv/C0ffee123/",
        "https://www.tiktok.com/@someone/photo/7300000000000000000",
        "https://www.tiktok.com/@someone/video/7300000000000000000",
        "https://vt.tiktok.com/ZSabc123/",
        "https://vm.tiktok.com/ZMabc123/",
        "https://x.com/AnimeePost/status/2102612420057292860",
        "https://twitter.com/someone/status/1700000000000000000/photo/1",
        "https://www.reddit.com/r/pics/comments/abc123/some_title/",
        "https://www.reddit.com/r/pics/s/AbCdEf123",
        "https://www.reddit.com/gallery/abc123",
        "https://redd.it/abc123",
        "https://www.instagram.com/stories/some.user/3141592653/",
        "https://www.instagram.com/stories/highlights/17900000000000000/",
    ],
)
def test_social_posts_try_the_no_login_extractor_then_ytdlp_then_gallery_dl(text):
    """R4 (plan §6): public posts go to social first; the chain is core's, not the GUI's."""
    r = route(text)
    assert r.kind == "social" and r.ok and r.engine == "social"
    chain = _router.analyze_fallbacks(r)
    assert [(c.kind, c.engine) for c in chain] == [("video", "ytdlp"), ("gallery", "gallerydl")]
    assert all(c.url == r.url for c in chain)


@pytest.mark.parametrize(
    "text",
    [
        "https://www.instagram.com/some.user/",
        "https://x.com/someone/media",
        "https://www.tiktok.com/@someone",
        "https://www.reddit.com/r/pics/",
        "https://www.instagram.com/p/",
        "https://x.com/someone/status/notanumber",
    ],
)
def test_profiles_and_malformed_posts_do_not_go_to_social(text):
    assert route(text).kind != "social"


def test_social_hosts_share_their_site_rate_limit_group():
    assert _router.social_group("https://vt.tiktok.com/ZSabc/") == "tiktok"
    assert _router.social_group("https://redd.it/abc123") == "reddit"
    assert _router.social_group("https://www.reddit.com/r/a/comments/b/") == "reddit"


@pytest.mark.parametrize(
    ("code", "falls_back"),
    [("unsupported", True), ("download_error", False), ("timeout", False), (None, False)],
)
def test_only_an_unsupported_answer_moves_down_the_chain(code, falls_back):
    assert _router.should_fall_back(code) is falls_back


# ── Apple Music, Deezer, Tidal, Amazon Music (plan §7 "Other music sites") ────────────────
@pytest.mark.parametrize(
    ("text", "service", "kind", "catalog_id", "url"),
    [
        (
            "https://music.apple.com/us/album/in-between-dreams/1440857781?uo=4",
            "apple", "album", "1440857781", "https://music.apple.com/us/album/1440857781",
        ),
        (
            "https://music.apple.com/gb/album/better-together/1440857781?i=1440857786&uo=4",
            "apple", "track", "1440857786", "https://music.apple.com/gb/song/1440857786",
        ),
        (
            "https://music.apple.com/jp/song/better-together/1440857786",
            "apple", "track", "1440857786", "https://music.apple.com/jp/song/1440857786",
        ),
        (
            "https://itunes.apple.com/us/album/x/id1440857781",
            "apple", "album", "1440857781", "https://music.apple.com/us/album/1440857781",
        ),
        (
            "https://www.deezer.com/fr/track/3135556?utm_source=x",
            "deezer", "track", "3135556", "https://www.deezer.com/track/3135556",
        ),
        (
            "https://deezer.com/album/302127",
            "deezer", "album", "302127", "https://www.deezer.com/album/302127",
        ),
        (
            "https://www.deezer.com/en/playlist/908622995",
            "deezer", "playlist", "908622995", "https://www.deezer.com/playlist/908622995",
        ),
    ],
)  # fmt: skip
def test_apple_music_and_deezer_links_are_rebuilt_from_kind_and_id(
    text, service, kind, catalog_id, url
):
    r = route(text)
    assert (r.kind, r.service, r.catalog_kind, r.catalog_id, r.url) == (
        "catalog", service, kind, catalog_id, url
    )  # fmt: skip
    assert r.engine == "music" and r.is_catalog and not r.is_spotify


def test_spotify_is_a_catalog_link_too():
    assert route("https://open.spotify.com/track/6OmhkSOpvYBokMKQxpIGx2").is_catalog


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("https://listen.tidal.com/album/123", "Tidal is not supported"),
        ("https://tidal.com/browse/track/123", "Tidal is not supported"),
        ("https://music.amazon.com/albums/B0ABC", "Amazon Music is not supported"),
        ("https://music.amazon.co.uk/albums/B0ABC", "Amazon Music is not supported"),
        ("https://www.amazon.com/music/player/albums/B0ABC", "Amazon Music is not supported"),
        ("https://music.apple.com/us/playlist/x/pl.u-abc", "Apple Music playlists"),
        ("https://link.deezer.com/s/abc", "copy the full deezer.com address"),
        ("https://deezer.page.link/abc", "copy the full deezer.com address"),
        ("https://www.deezer.com/artist/27", "No Deezer song"),
        ("https://music.apple.com/us/artist/x/909253", "No Apple Music song"),
    ],
)
def test_services_we_cannot_use_are_refused_by_name(text, reason):
    r = route(text)
    assert not r.ok and reason in r.reason


@pytest.mark.parametrize(
    "text",
    [
        "https://music.apple.com/us/album/x/0123",
        "https://music.apple.com/us/album/x/1440857781?i=abc",
        "https://www.deezer.com/track/12a",
    ],
)
def test_a_malformed_music_id_is_invalid(text):
    assert route(text).kind == "invalid"


def test_an_amazon_shop_page_is_not_mistaken_for_amazon_music():
    assert route("https://www.amazon.com/dp/B0ABC").kind == "video"
