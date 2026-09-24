"""GUI: row, card and preview thumbnails (item 4). Every fetch here is a fake."""

from __future__ import annotations

import base64
import json
import threading

import pytest
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QColor, QImage
from test_main_window import _analyzed
from test_playlist_queue import expand, listing, runs, window  # noqa: F401  (fixtures)

from stuff_downloader.core import protocol
from stuff_downloader.core.gallery import GalleryItem
from stuff_downloader.core.protocol import Event
from stuff_downloader.core.spotify import Match, SpotifyTrack
from stuff_downloader.gui import thumbs
from stuff_downloader_worker.engines import http as worker_http

VID = "dQw4w9WgXcQ"
RED = QColor("#ff0000")


def png(width=64, height=36, color="#ff0000") -> bytes:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


class FakeFetch:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.calls: list[str] = []
        self.threads: list[str] = []

    def __call__(self, url):
        self.calls.append(url)
        self.threads.append(threading.current_thread().name)
        if self.error:
            raise self.error
        return self.body if self.body is not None else png()


def _loader(fake):
    loader = thumbs.ThumbnailLoader(fetcher=fake)
    got = []
    loader.loaded.connect(lambda url, image: got.append((url, image)))
    return loader, got


def _icon_colour(item):
    return item.icon().pixmap(48, 27).toImage().pixelColor(24, 13)


# ── URL and decode rules ─────────────────────────────────────────────────────────────────
def test_only_validated_video_ids_become_urls():
    assert thumbs.youtube_thumb_url(VID) == f"https://i.ytimg.com/vi/{VID}/mqdefault.jpg"
    for bad in [None, "", "short", "../../etc/pa", "dQw4w9WgXc?"]:
        assert thumbs.youtube_thumb_url(bad) is None


@pytest.mark.parametrize(
    "url",
    [
        "http://i.ytimg.com/vi/x/mqdefault.jpg",  # not https
        "https://evil.example/vi/x.jpg",
        "https://i.ytimg.com.evil.example/x.jpg",
        "https://user:pw@i.ytimg.com/x.jpg",
        "https://i.ytimg.com:8443/x.jpg",
        "file:///C:/Windows/win.ini",
        "javascript:alert(1)",
    ],
)
def test_nothing_outside_the_allow_list_is_fetched(qtbot, url, monkeypatch):
    fake = FakeFetch()
    loader, _ = _loader(fake)
    assert not loader.request(url)
    assert fake.calls == []
    monkeypatch.undo()  # the real fetcher, which must refuse before connecting
    with pytest.raises(thumbs.ThumbnailError):
        thumbs.fetch(url)


# ── Spotify's pictures (plan §7 item 2) ──────────────────────────────────────────────────
HASH = "ab67616d00001e02" + "c" * 24
SCDN = f"https://i.scdn.co/image/{HASH}"
T1 = "6OmhkSOpvYBokMKQxpIGx2"
OEMBED = f"https://open.spotify.com/oembed?url=https://open.spotify.com/track/{T1}"


def test_spotify_pictures_and_oembed_lookups_are_allowed_in_their_exact_form():
    assert thumbs.allowed(SCDN) and thumbs.allowed(OEMBED)


@pytest.mark.parametrize(
    "url",
    [
        f"https://image-cdn-ak.spotifycdn.com/image/{HASH}",  # only the normalized host
        f"https://i.scdn.co/image/{HASH}?x=1",
        f"https://i.scdn.co/other/{HASH}",
        f"http://i.scdn.co/image/{HASH}",
        "https://i.scdn.co/image/../../x",
        f"https://open.spotify.com/oembed?url=https://open.spotify.com/playlist/{T1}",
        f"{OEMBED}&format=xml",
        f"https://open.spotify.com/oembed?url=https://evil.example/track/{T1}",
        f"https://open.spotify.com/embed/track/{T1}",
    ],
)
def test_nothing_else_on_spotifys_hosts_is_fetched(url, monkeypatch):
    monkeypatch.undo()  # the real fetcher, which must refuse before connecting
    assert not thumbs.allowed(url)
    with pytest.raises(thumbs.ThumbnailError):
        thumbs.fetch(url)


def test_an_oembed_lookup_is_followed_to_its_picture_on_i_scdn_co(monkeypatch):
    monkeypatch.undo()  # the real fetcher; only its network call is replaced
    calls = []

    def get(url, cap):
        calls.append((url, cap))
        if url == OEMBED:
            answer = {"thumbnail_url": f"https://image-cdn-ak.spotifycdn.com/image/{HASH}"}
            return json.dumps(answer).encode()
        return png()

    monkeypatch.setattr(thumbs, "_get", get)
    assert thumbs.fetch(OEMBED) == png()
    assert calls == [(OEMBED, thumbs.MAX_OEMBED_BYTES), (SCDN, thumbs.MAX_BYTES)]


@pytest.mark.parametrize(
    "answer",
    [
        b"not json",
        b"[]",
        json.dumps({"thumbnail_url": "https://evil.example/image/" + HASH}).encode(),
        json.dumps({"thumbnail_url": SCDN + "?x=1"}).encode(),
        json.dumps({"thumbnail_url": "file:///C:/x.png"}).encode(),
        json.dumps({"html": "<iframe>"}).encode(),
    ],
)
def test_a_hostile_oembed_answer_fetches_nothing_more(monkeypatch, answer):
    monkeypatch.undo()
    calls = []

    def get(url, cap):
        calls.append(url)
        return answer

    monkeypatch.setattr(thumbs, "_get", get)
    with pytest.raises(thumbs.ThumbnailError):
        thumbs.fetch(OEMBED)
    assert calls == [OEMBED]


def test_decode_caps_bytes_and_pixels(qtbot):
    assert thumbs.decode_image(png(64, 36)).width() == 64
    big = thumbs.decode_image(png(1280, 720))
    assert big.width() <= 320 and big.height() <= 320  # decoded small, not scaled later
    assert thumbs.decode_image(png(thumbs.MAX_SIDE + 1, 4)) is None
    assert thumbs.decode_image(b"x" * (thumbs.MAX_BYTES + 1)) is None
    assert thumbs.decode_image(b"not an image") is None
    assert thumbs.decode_image(b"") is None


# ── the loader ───────────────────────────────────────────────────────────────────────────
def test_loads_off_the_gui_thread_and_caches_per_url(qtbot):
    fake = FakeFetch(body=png(1280, 720))
    loader, got = _loader(fake)
    url = thumbs.youtube_thumb_url(VID)
    assert loader.request(url)
    assert not loader.request(url)  # already in flight
    qtbot.waitUntil(lambda: bool(got), timeout=3000)
    assert fake.threads[0] != threading.main_thread().name
    image = loader.cached(url)
    assert image.width() <= thumbs.ROW_BOX.width()  # small in the cache, whatever arrived
    assert loader.cached(url) is not None
    assert not loader.request(url)  # cached
    assert fake.calls == [url]


@pytest.mark.parametrize("error", [OSError("timed out"), None])
def test_a_failure_leaves_the_placeholder_and_is_not_retried(qtbot, error):
    fake = FakeFetch(body=b"<html>", error=error)
    loader, got = _loader(fake)
    url = thumbs.youtube_thumb_url(VID)
    loader.request(url)
    qtbot.waitUntil(lambda: url in loader._cache, timeout=3000)
    assert got == [] and loader.cached(url) is None
    assert not loader.request(url)
    assert len(fake.calls) == 1


# ── where the pictures appear ────────────────────────────────────────────────────────────
def test_playlist_rows_load_lazily_and_show_the_image(window, runs, qtbot):  # noqa: F811
    page = window.downloads_page
    fake = FakeFetch()
    page.thumbs.fetcher = fake
    payload = listing(40, unavailable_last=False)
    for i, entry in enumerate(payload["entries"]):
        entry["id"] = f"{i:011d}"
    window.resize(1000, 700)
    window.show()
    page = expand(window, runs, payload=payload)
    qtbot.waitUntil(lambda: bool(fake.calls), timeout=3000)
    assert 0 < len(set(fake.calls)) < 40  # only the rows on screen
    item = page.playlist_card.table.item(0, 2)
    qtbot.waitUntil(lambda: _icon_colour(item) == RED, timeout=3000)
    last = thumbs.youtube_thumb_url(f"{39:011d}")
    assert last not in fake.calls
    bar = page.playlist_card.table.verticalScrollBar()
    bar.setValue(bar.maximum())
    qtbot.waitUntil(lambda: last in fake.calls, timeout=3000)


def test_playlist_queue_cards_get_the_row_thumbnail(window, runs, qtbot):  # noqa: F811
    page = window.downloads_page
    page.thumbs.fetcher = FakeFetch()
    page = expand(window, runs, payload=listing(2, unavailable_last=False))
    jobs = page.start_playlist_download()
    card = jobs[0].card
    qtbot.waitUntil(
        lambda: card.thumb.pixmap() is not None and not card.thumb.pixmap().isNull(),
        timeout=3000,
    )
    assert card.thumb.text() == ""


def test_a_direct_image_link_previews_the_real_image(window, runs, qtbot):  # noqa: F811
    page = window.downloads_page
    page.url_edit.setText("https://cdn.example.com/p/photo.png")
    page.analyze()
    run = runs[-1]
    payload = protocol.media_result(
        "image",
        ["image"],
        "photo",
        "https://cdn.example.com/p/photo.png",
        site="Direct file",
        ext="png",
        filesize=100,
        formats=[],
        preview={"data": base64.b64encode(png(64, 36, "#00ff00")).decode()},
        **worker_http.file_rows("image", "png", 100),
    )
    # Posted directly: the payload's own "kind" would shadow FakeRun.emit's argument.
    run.on_event(Event("result", run.spec.job_id, payload))
    qtbot.waitUntil(lambda: not page.result_card.isHidden())
    assert page._thumb is not None
    assert page._thumb.toImage().pixelColor(10, 10) == QColor("#00ff00")
    job = page.start_download()
    assert not job.card.thumb.pixmap().isNull()


def test_an_oversized_preview_falls_back_to_the_placeholder(window, runs, qtbot):  # noqa: F811
    huge = base64.b64encode(png(thumbs.MAX_SIDE + 1, 2)).decode()
    page = _analyzed(window, runs, qtbot, preview={"data": huge})
    assert page._thumb is None


def test_a_spotify_match_never_replaces_the_row_picture(window, qtbot):  # noqa: F811
    """Plan §5.6a: a Spotify row keeps Spotify's art; the YouTube match's picture is not
    fetched for the row, and nothing is drawn into the match column."""
    page = window.downloads_page
    fetch = FakeFetch()
    page.thumbs.fetcher = fetch
    card = page.spotify_card
    card.set_tracks([SpotifyTrack("t1" * 11, 1, "Song", ("Artist",), duration=200.0)])
    card.set_match(0, Match(track_id="t1" * 11, video_id=VID, manual=True))
    qtbot.wait(50)
    assert fetch.calls == []
    assert not card.table.has_art(0)
    assert card.table.item(0, card.MATCH_COLUMN).icon().isNull()


def test_gallery_tiles_decode_capped_and_tooltips_are_plain_text(window):  # noqa: F811
    card = window.downloads_page.gallery_card
    card.set_items(
        [
            # The label is built from site-provided fields; markup in them must stay text.
            GalleryItem(index=1, kind="image", ext="<b>x</b>", preview=png()),
            GalleryItem(index=2, kind="image", preview=png(thumbs.MAX_SIDE + 1, 2)),
        ]
    )
    tip = card.grid.item(0).toolTip()
    assert "&lt;B&gt;" in tip and "<B>" not in tip
    assert not card.grid.item(0).icon().isNull()
    assert card.grid.item(1).icon().isNull()  # refused, the kind glyph stays


# ── the Result card preview (plan §5.6, R3) ──────────────────────────────────────────────
def _card(qtbot):
    from stuff_downloader.gui.widgets import ResultCard

    card = ResultCard()
    qtbot.addWidget(card)
    return card


def _image(width, height, color):
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(color))
    return image


def test_a_video_preview_is_480x270_with_the_duration_in_its_corner(qtbot):
    card = _card(qtbot)
    card.set_preview(_image(1280, 720, "#00ff00"), {"kind": "video", "duration": 244})
    pixmap = card.cover.pixmap()
    assert (card.cover.width(), card.cover.height()) == (480, 270)
    assert (pixmap.width(), pixmap.height()) == (480, 270) and card.cover.text() == ""
    shown = pixmap.toImage()
    assert shown.pixelColor(20, 20) == QColor("#00ff00")
    # The duration badge is dark and sits in the bottom-right corner, over the picture.
    badge = shown.pixelColor(470, 256)
    assert badge.green() < 128


def test_a_music_preview_is_300x300_and_letterboxes_a_wide_picture(qtbot):
    card = _card(qtbot)
    card.set_preview(_image(640, 360, "#ff0000"), {"kind": "audio", "duration": 61})
    assert (card.cover.width(), card.cover.height()) == (300, 300)
    shown = card.cover.pixmap().toImage()
    assert shown.pixelColor(150, 150) == QColor("#ff0000")
    assert shown.pixelColor(150, 5) != QColor("#ff0000")  # bars, never stretched


def test_no_duration_means_no_badge(qtbot):
    card = _card(qtbot)
    card.set_preview(_image(480, 270, "#00ff00"), {"kind": "video"})
    assert card.cover.pixmap().toImage().pixelColor(470, 256) == QColor("#00ff00")


@pytest.mark.parametrize(("kind", "text"), [("audio", "🎵"), ("video", "🎞"), ("image", "🎞")])
def test_the_placeholder_shows_only_without_a_picture(qtbot, kind, text):
    card = _card(qtbot)
    card.set_preview(_image(10, 10, "#00ff00"), {"kind": kind})
    card.set_preview(None, {"kind": kind, "duration": 5})
    assert card.cover.text() == text and card.cover.pixmap().isNull()
