import pytest

from stuff_downloader.core.router import HANDLE_UNSUPPORTED_REASON, route

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
    ],
)
def test_invalid_input_is_rejected(text):
    r = route(text)
    assert r.kind == "invalid" and not r.ok and r.reason and r.url == ""


@pytest.mark.parametrize(
    "text",
    [
        f"https://www.youtube.com.evil.example/watch?v={VID}",
        f"https://evil.example/watch?v={VID}&host=youtube.com",
        "https://vimeo.com/123",
        "https://www.youtube.com/playlist?list=PLabc",
        f"https://user@evil.example/youtu.be/{VID}",
    ],
)
def test_other_hosts_and_playlists_are_unsupported(text):
    r = route(text)
    assert r.kind == "unsupported" and not r.ok and r.reason
