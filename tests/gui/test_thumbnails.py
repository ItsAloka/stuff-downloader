"""GUI: row, card and preview thumbnails (item 4). Every fetch here is a fake."""

from __future__ import annotations

import base64
import threading

import pytest
from PyQt6.QtCore import QBuffer, QByteArray, QIODevice
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QTableWidgetItem
from test_main_window import _analyzed
from test_playlist_queue import expand, listing, runs, window  # noqa: F401  (fixtures)

from stuff_downloader.core.gallery import GalleryItem
from stuff_downloader.core.protocol import Event
from stuff_downloader.core.spotify import Match
from stuff_downloader.gui import thumbs

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
    payload = {
        "kind": "file",
        "title": "photo",
        "extractor": "Direct file",
        "ext": "png",
        "filesize": 100,
        "formats": [],
        "thumbnail": {"data": base64.b64encode(png(64, 36, "#00ff00")).decode()},
    }
    # Posted directly: the payload's own "kind" would shadow FakeRun.emit's argument.
    run.on_event(Event("result", run.spec.job_id, payload))
    qtbot.waitUntil(lambda: not page.preview.isHidden())
    assert page._thumb is not None
    assert page._thumb.toImage().pixelColor(10, 10) == QColor("#00ff00")
    job = page.start_download()
    assert not job.card.thumb.pixmap().isNull()


def test_an_oversized_preview_falls_back_to_the_placeholder(window, runs, qtbot):  # noqa: F811
    huge = base64.b64encode(png(thumbs.MAX_SIDE + 1, 2)).decode()
    page = _analyzed(window, runs, qtbot, thumbnail={"data": huge})
    assert page._thumb is None


def test_non_youtube_video_preview_and_missing_image_fallback(window, runs, qtbot):  # noqa: F811
    thumb = {"data": base64.b64encode(png(64, 36, "#00ff00")).decode()}
    page = _analyzed(
        window, runs, qtbot, url="https://www.instagram.com/reel/abc123/", thumbnail=thumb
    )
    assert page._thumb is not None
    assert page._thumb.toImage().pixelColor(10, 10) == QColor("#00ff00")
    page = _analyzed(
        window, runs, qtbot, url="https://www.instagram.com/reel/abc123/", thumbnail=None
    )
    assert page._thumb is None
    assert page.preview.cover.text() == "No preview available"
    page = _analyzed(
        window,
        runs,
        qtbot,
        url="https://www.instagram.com/reel/abc123/",
        thumbnail={"data": "invalid-base64"},
    )
    assert page._thumb is None
    assert page.preview.cover.text() == "No preview available"


def test_spotify_cover_art_appears_in_listing_and_queue(window, runs, qtbot):  # noqa: F811
    page = window.downloads_page
    cover_url = "https://i.scdn.co/image/albumart"
    fake = FakeFetch()
    page.thumbs.fetcher = fake
    page.url_edit.setText("https://open.spotify.com/track/6OmhkSOpvYBokMKQxpIGx2")
    page.analyze()
    run = runs[-1]
    run.on_event(
        Event(
            "result",
            run.spec.job_id,
            {
                "kind": "spotify",
                "spotify_kind": "track",
                "spotify_id": "6OmhkSOpvYBokMKQxpIGx2",
                "title": "Song",
                "tracks": [
                    {
                        "id": "6OmhkSOpvYBokMKQxpIGx2",
                        "title": "Song",
                        "artists": ["Artist"],
                        "cover_url": cover_url,
                    }
                ],
            },
        )
    )
    item = page.spotify_card.table.item(0, page.spotify_card.MATCH_COLUMN)
    qtbot.waitUntil(lambda: _icon_colour(item) == RED, timeout=3000)
    track = page._spotify.tracks[0]
    page._spotify_matches[track.track_id] = Match(track_id=track.track_id, video_id=VID, manual=True)
    jobs = page.start_spotify_download()
    assert len(jobs) == 1
    qtbot.waitUntil(lambda: not jobs[0].card.thumb.pixmap().isNull(), timeout=3000)
    assert fake.calls == [cover_url]


def test_spotify_matches_show_the_matched_video(window, qtbot):  # noqa: F811
    page = window.downloads_page
    page.thumbs.fetcher = FakeFetch()
    table = page.spotify_card.table
    table.setRowCount(1)
    for column in range(table.columnCount()):
        table.setItem(0, column, QTableWidgetItem(""))
    match = Match(track_id="t1", video_id=VID, manual=True)
    page.spotify_card.set_match(0, match)
    page._request_spotify_thumb(0, match)
    item = table.item(0, page.spotify_card.MATCH_COLUMN)
    qtbot.waitUntil(lambda: _icon_colour(item) == RED, timeout=3000)


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
