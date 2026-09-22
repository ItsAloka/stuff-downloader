from __future__ import annotations

# ruff: noqa: E402, I001 -- Qt's platform must be selected before importing PyQt.

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", "C:/Windows/Fonts")

from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QThreadPool
from PyQt6.QtGui import QColor, QImage, QLinearGradient, QPainter
from PyQt6.QtWidgets import QApplication

from stuff_downloader.core import router, settings
from stuff_downloader.gui.pages import DownloadsPage
from stuff_downloader.gui.theme import STYLE
from stuff_downloader.gui.widgets import JobCard


OUT = Path(__file__).with_name("screenshots")
SIZES = {"small": (800, 600), "medium": (1280, 800), "maximized": (1920, 1080)}


# The repo is public: screenshots show a neutral folder, never the machine's real one.
NEUTRAL_FOLDER = r"Saving to  C:\Users\you\Downloads"


def neutral(page: DownloadsPage) -> None:
    page.folder_hint.setText(NEUTRAL_FOLDER)


def save(page: DownloadsPage, state: str) -> None:
    neutral(page)
    for label, size in SIZES.items():
        page.resize(*size)
        QApplication.processEvents()
        page.grab().save(str(OUT / f"responsive-{state}-{label}.png"))


app = QApplication.instance() or QApplication([])
app.setStyleSheet(STYLE)
OUT.mkdir(parents=True, exist_ok=True)
page = DownloadsPage(settings.Settings())


def fake_thumbnail(url: str) -> bytes:
    """A generated 16:9 picture per URL: the script never touches the network."""
    hue = sum(url.encode()) % 360
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    painter = QPainter(image)
    gradient = QLinearGradient(0, 0, 320, 180)
    gradient.setColorAt(0, QColor.fromHsv(hue, 160, 220))
    gradient.setColorAt(1, QColor.fromHsv((hue + 60) % 360, 200, 120))
    painter.fillRect(image.rect(), gradient)
    painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def settle() -> None:
    QThreadPool.globalInstance().waitForDone(3000)
    for _ in range(5):
        QApplication.processEvents()


page.thumbs.fetcher = fake_thumbnail
page.show_preview(
    {
        "title": "A long analyzed song title that remains readable without covering its controls",
        "artist": "Example Artist",
        "duration": 247,
        "extractor": "youtube",
        "formats": [],
    }
)
page.show()
save(page, "preview")
page._route = router.route("https://music.youtube.com/watch?v=dQw4w9WgXcQ")
page.show_preview(
    {
        "title": "A YouTube Music song defaults to the MP3 music preset",
        "artist": "Example Artist",
        "duration": 247,
        "extractor": "youtube",
        "formats": [],
    }
)
save(page, "ytmusic-default")

page.preview.hide()
page.show_playlist(
    {
        "kind": "playlist",
        "id": "PL-example",
        "title": "Responsive playlist preview",
        "uploader": "Example Channel",
        "entries": [
            {
                "id": f"id{i:09d}",
                "title": f"Song {i + 1}: a title long enough to demonstrate table sizing",
                "uploader": "Example Artist",
                "duration": 180 + i,
            }
            for i in range(12)
        ],
    }
)
settle()
save(page, "playlist")

page.playlist_card.hide()
page.empty_state.hide()
for index in range(30):
    card = JobCard()
    card.title_label.setText(f"Queued download {index + 1}")
    card.details_label.setText("Waiting · example.mp4")
    card.set_state("Queued", "queued")
    page.queue_layout.addWidget(card)
save(page, "queue")

# Items 3 + 5: finished cards with file actions (one file missing) next to running ones.
while page.queue_layout.count():
    item = page.queue_layout.takeAt(0)
    if item.widget() is not None:
        item.widget().hide()  # deleteLater needs an event loop this script never runs
        item.widget().deleteLater()
states = [
    ("Finished song.mp3", "Completed", "completed", "Done  ·  4.2 MB", True, ""),
    ("Moved afterwards.mp4", "Completed", "completed",
     "Done  ·  81 MB  ·  The file was moved or deleted.", False,
     "The file was moved or deleted."),
    ("Private video", "Failed", "failed", "This video is private on the site.", None, ""),
    ("Still downloading", "Downloading video", "active", "12 MB / 80 MB  ·  2 MB/s", None, ""),
]
for title, chip, state, details, file_ok, reason in states:
    card = JobCard()
    card.title_label.setText(title)
    card.set_state(chip, state)
    card.details_label.setText(details)
    card.set_progress(100 if state != "active" else 15)
    card.cancel_button.setVisible(state == "active")
    card.pause_button.setVisible(state == "active")
    card.retry_button.setVisible(state == "failed")
    card.open_button.setVisible(file_ok is not None)
    card.folder_button.setVisible(file_ok is not None)
    for button in (card.open_button, card.folder_button):
        button.setEnabled(bool(file_ok))
        button.setToolTip(reason)
    page.queue_layout.addWidget(card)
for card, url in zip(
    [page.queue_layout.itemAt(i).widget() for i in range(page.queue_layout.count())],
    ["https://i.ytimg.com/vi/aaaaaaaaaaa/mqdefault.jpg", None, None,
     "https://i.ytimg.com/vi/bbbbbbbbbbb/mqdefault.jpg"],
    strict=False,
):
    if isinstance(card, JobCard) and url:
        card.set_thumbnail(QImage.fromData(fake_thumbnail(url)))
page.clear_queue_button.setEnabled(True)
page.queue_summary.setText("1 active  ·  2 done  ·  1 failed")
page.resize(*SIZES["medium"])
neutral(page)
QApplication.processEvents()
page.grab().save(str(OUT / "queue-actions-medium.png"))

# Item 4: a direct image link previews the image itself.
import base64  # noqa: E402

page.playlist_card.hide()
page._route = router.route("https://cdn.example.com/photos/sunset.png")
page.show_preview(
    {
        "kind": "file",
        "title": "sunset",
        "extractor": "Direct file",
        "ext": "png",
        "filesize": 48_213,
        "formats": [],
        "thumbnail": {"data": base64.b64encode(fake_thumbnail("sunset")).decode()},
    }
)
page.preview.show()
page.resize(*SIZES["medium"])
neutral(page)
QApplication.processEvents()
page.grab().save(str(OUT / "direct-image-preview-medium.png"))

# Item 6B: a gallery with its "Save images as" choice.
page.preview.hide()
page._route = router.route("https://www.instagram.com/p/ABC123/")
page.show_gallery(
    {
        "kind": "gallery",
        "title": "Example post",
        "site": "Instagram",
        "uploader": "example",
        "items": [
            {
                "index": i,
                "kind": "image",
                "ext": "webp",
                "width": 1080,
                "height": 1350,
                "thumbnail": {"data": base64.b64encode(fake_thumbnail(str(i))).decode()},
            }
            for i in range(1, 7)
        ],
    }
)
page.gallery_card.image_format_combo.setCurrentIndex(1)
page.resize(*SIZES["medium"])
neutral(page)
QApplication.processEvents()
page.grab().save(str(OUT / "gallery-format-medium.png"))

# Item 9: Spotify matches with an uncertain one flagged, and the Change… dialog.
from stuff_downloader.core import spotify  # noqa: E402
from stuff_downloader.gui.widgets import MatchDialog  # noqa: E402

page.gallery_card.hide()
page._route = router.route("https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy")
page.show_spotify(
    {
        "kind": "spotify",
        "spotify_kind": "album",
        "spotify_id": "4aawyAB9vmqN3uQ7FjRGTy",
        "title": "Example album",
        "owner": "Example Artist",
        "tracks": [
            {"id": f"{i}" * 22, "title": t, "artists": ["Example Artist"], "album": "Example",
             "duration": d, "explicit": False}
            for i, (t, d) in enumerate(
                [("Opening Song", 201.0), ("Second Song", 185.0), ("Third Song", 240.0)], 1
            )
        ],
        "skipped": 0,
        "truncated": False,
    }
)
listing = page._spotify
results = [
    ("aaaaaaaaaaa", "Opening Song", "Example Artist", 202.0, []),
    ("bbbbbbbbbbb", "Second Song (Sped Up)", "Nightcore Hub", 150.0,
     [{"video_id": "ccccccccccc", "title": "Second Song", "channel": "Example Artist",
       "duration": 186.0}]),
    ("ddddddddddd", "Third Song", "Example Artist - Topic", 239.0, []),
]
for row, (track, (vid, title, channel, duration, cands)) in enumerate(
    zip(listing.tracks, results, strict=True)
):
    match = spotify.parse_match(
        {"track_id": track.track_id, "video_id": vid, "title": title, "channel": channel,
         "duration": duration, "confidence": 90, "candidates": cands},
        track,
    )
    page._spotify_matches[track.track_id] = match
    page.spotify_card.set_match(row, match)
    page._request_spotify_thumb(row, match)
page._update_uncertain()
settle()
page.resize(*SIZES["medium"])
neutral(page)
QApplication.processEvents()
page.grab().save(str(OUT / "spotify-uncertain-medium.png"))

second = listing.tracks[1]
current = page._spotify_matches[second.track_id]
dialog = MatchDialog(
    second,
    (spotify.Candidate(current.video_id, current.title, current.channel, current.duration),
     *current.candidates),
)
dialog.table.selectRow(1)
dialog.show()
QApplication.processEvents()
dialog.grab().save(str(OUT / "spotify-change-dialog.png"))
dialog.close()
page.close()
