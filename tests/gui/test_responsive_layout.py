from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QScrollArea, QSizePolicy

from stuff_downloader.core import playlist, router, settings
from stuff_downloader.gui.pages import DownloadsPage
from stuff_downloader.gui.widgets import JobCard, PlaylistCard, ResultCard, SpotifyCard, TrackTable
from stuff_downloader_worker.engines import ytdlp as worker_ytdlp

FIXTURE = Path(__file__).resolve().parents[1] / "unit" / "fixtures" / "youtube_video.json"
VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _media(**fields):
    info = json.loads(FIXTURE.read_text(encoding="utf-8"))
    info.update(fields)
    return worker_ytdlp.analyze_result(info, VIDEO_URL, None)


def _intersects(a, b) -> bool:
    return a.geometry().intersects(b.geometry())


def test_downloads_page_scrolls_instead_of_compressing_cards(qtbot):
    page = DownloadsPage(settings.Settings())
    qtbot.addWidget(page)
    page.resize(520, 420)
    cards = []
    for index in range(30):
        card = JobCard()
        card.title_label.setText(f"Queued item {index}")
        page.queue_layout.addWidget(card)
        cards.append(card)
    page.show()
    qtbot.wait(1)

    assert isinstance(page.page_scroll, QScrollArea)
    assert page.page_scroll.widgetResizable()
    assert page.page_scroll.verticalScrollBar().maximum() > 0
    assert all(card.height() >= card.minimumSizeHint().height() for card in cards)
    assert all(card.sizePolicy().verticalPolicy() == QSizePolicy.Policy.Minimum for card in cards)


def test_result_cover_and_tabs_keep_their_natural_geometry(qtbot):
    card = ResultCard()
    qtbot.addWidget(card)
    media = _media(title="A very long title " * 12)
    card.title_editor.set_title(media["title"])
    card.set_result(media, {t: media[f"{t}_rows"] for t in ("video", "audio", "image")})
    card.resize(card.minimumSizeHint())
    card.show()
    qtbot.wait(1)

    assert card.cover.size().width() == 480 and card.cover.size().height() == 270  # plan §5.6
    assert card.tabs.geometry().top() > card.cover.geometry().bottom()
    assert not _intersects(card.cover, card.title_editor)


@pytest.mark.parametrize("width", [1280, 1920])
def test_the_result_card_never_needs_a_horizontal_scrollbar(qtbot, width):
    from stuff_downloader.gui.theme import STYLE

    page = DownloadsPage(settings.Settings())
    qtbot.addWidget(page)
    page.setStyleSheet(STYLE)
    page._route = router.route(VIDEO_URL)
    page.show_result(_media(title="An extremely long title that goes on and on " * 8))
    page.resize(width - 220, 720 - 60)  # minus the sidebar and the window frame
    page.show()
    qtbot.wait(1)
    assert page.page_scroll.horizontalScrollBar().maximum() == 0
    for table in page.result_card.tables.values():
        assert table.horizontalScrollBar().maximum() == 0


def test_playlist_controls_are_below_a_six_row_table(qtbot):
    card = PlaylistCard()
    qtbot.addWidget(card)
    entries = [
        playlist.PlaylistEntry(
            video_id=f"id{i:09d}",
            index=i + 1,
            url=f"https://youtu.be/id{i:09d}",
            title=f"Song {i}",
        )
        for i in range(8)
    ]
    card.set_entries(entries)
    card.resize(card.minimumSizeHint())
    card.show()
    qtbot.wait(1)

    table_bottom = card.table.geometry().bottom()
    assert card.table.height() >= card.table.horizontalHeader().height() + 6 * 30
    assert card.select_all_button.geometry().top() > table_bottom
    assert card.filter_edit.geometry().top() > table_bottom
    assert card.format_combo.geometry().top() > table_bottom
    assert card.download_button.geometry().top() > table_bottom


def test_spotify_controls_are_below_table(qtbot):
    card = SpotifyCard()
    qtbot.addWidget(card)
    card.resize(card.minimumSizeHint())
    card.show()
    qtbot.wait(1)

    table_bottom = card.table.geometry().bottom()
    assert card.table.minimumHeight() >= card.table.horizontalHeader().height() + 6 * 36
    assert card.select_all_button.geometry().top() > table_bottom
    assert card.download_button.geometry().top() > table_bottom


def test_the_scrolling_page_keeps_the_dark_background(qtbot):
    from PyQt6.QtGui import QColor

    from stuff_downloader.gui.theme import BG, STYLE

    page = DownloadsPage(settings.Settings())
    qtbot.addWidget(page)
    page.setStyleSheet(STYLE)
    page.resize(800, 600)
    page.show()
    qtbot.wait(1)
    image = page.grab().toImage()
    # A spot beside the page title: the scroll viewport, not a card.
    assert image.pixelColor(700, 20).name() == QColor(BG).name()


def test_playlist_titles_are_edited_in_place_and_there_is_no_file_name_column(qtbot):
    from PyQt6.QtCore import Qt

    card = PlaylistCard()
    qtbot.addWidget(card)
    card.set_entries(
        [
            playlist.PlaylistEntry(
                video_id="id000000001", index=1, url="https://youtu.be/id000000001", title="Song"
            )
        ]
    )
    headers = [card.table.horizontalHeaderItem(c).text() for c in range(card.table.columnCount())]
    assert "File name" not in headers
    title = card.table.item(0, card.TITLE_COLUMN)
    assert title.flags() & Qt.ItemFlag.ItemIsEditable
    assert not card.table.item(0, TrackTable.ARTIST).flags() & Qt.ItemFlag.ItemIsEditable
    assert card.output_name(0) is None
    title.setText("My name")
    assert card.output_name(0) == "My name"


def test_the_pencil_sits_right_after_the_title_and_long_titles_are_elided(qtbot):
    # A hidden playlist row must not clamp the title column to a sliver either.
    card = ResultCard()
    qtbot.addWidget(card)
    card.title_editor.set_title("A YouTube Music song opens on its Audio tab")
    card.resize(1440, 400)  # beside the 480-wide preview
    card.show()
    qtbot.wait(1)
    editor = card.title_editor
    label = editor.label
    assert label.text() == "A YouTube Music song opens on its Audio tab"  # nothing cut
    assert 0 <= editor.pencil.geometry().left() - label.geometry().right() < 20
    assert editor.geometry().left() - card.cover.geometry().right() < 40

    long_title = "An extremely long title that keeps going " * 10
    editor.set_title(long_title)
    qtbot.wait(1)
    assert label.text().endswith("…") and label.full_text() == long_title
    assert long_title in label.toolTip()
    assert editor.pencil.geometry().right() <= editor.width()


def test_row_download_buttons_are_never_clipped(qtbot):
    from stuff_downloader.gui.theme import STYLE

    card = ResultCard()
    qtbot.addWidget(card)
    card.setStyleSheet(STYLE)
    media = _media()
    card.set_result(media, {t: media[f"{t}_rows"] for t in ("video", "audio", "image")})
    card.resize(1200, 900)
    card.show()
    for tab in card.tab_names():
        card.tabs.setCurrentWidget(card.pages[tab])
        qtbot.wait(1)
        for row in range(card.tables[tab].rowCount()):
            button = card.download_button(tab, row)
            assert button.height() >= button.sizeHint().height(), (tab, row)
            assert button.width() >= button.sizeHint().width(), (tab, row)


def test_the_result_tabs_follow_the_dark_theme(qtbot):
    card = ResultCard()
    qtbot.addWidget(card)
    media = _media()
    card.set_result(media, {t: media[f"{t}_rows"] for t in ("video", "audio", "image")})
    card.show()
    qtbot.wait(1)
    bar = card.tabs.tabBar()
    image = bar.grab().toImage()
    rect = bar.tabRect(1)  # an unselected tab
    # Beside the label text, inside the tab: the tab's own fill, not Fusion's light grey.
    assert image.pixelColor(rect.left() + 6, rect.bottom() - 3).lightness() < 100


def test_combos_and_checkboxes_follow_the_dark_theme(qtbot):
    from PyQt6.QtGui import QPalette

    from stuff_downloader.gui.theme import STYLE

    card = ResultCard()
    qtbot.addWidget(card)
    card.setStyleSheet(STYLE)
    playlist_card = PlaylistCard()
    qtbot.addWidget(playlist_card)
    playlist_card.setStyleSheet(STYLE)
    card.show()
    playlist_card.show()
    qtbot.wait(1)
    check_text = playlist_card.archive_check.palette().color(QPalette.ColorRole.WindowText)
    assert check_text.lightness() > 150  # readable on the dark card
    card.tabs.addTab(card.pages["video"], "Video")
    card.tabs.show()
    qtbot.wait(1)
    combo = card.container_combo.grab().toImage()
    middle = combo.pixelColor(combo.width() // 2, combo.height() // 2)
    assert middle.lightness() < 100  # not a white box on a dark page


def test_styled_combos_keep_an_arrow_and_unchecked_boxes_stay_visible(qtbot):
    from stuff_downloader.gui.theme import STYLE

    card = ResultCard()
    qtbot.addWidget(card)
    card.setStyleSheet(STYLE)
    card.tabs.addTab(card.pages["video"], "Video")
    card.show()
    qtbot.wait(1)
    combo = card.container_combo.grab().toImage()
    arrow_zone = [
        combo.pixelColor(x, y).lightness()
        for x in range(combo.width() - 22, combo.width())
        for y in range(combo.height())
    ]
    assert max(arrow_zone) > 120  # something visible where the arrow belongs

    playlist_card = PlaylistCard()
    qtbot.addWidget(playlist_card)
    playlist_card.setStyleSheet(STYLE)
    playlist_card.show()
    qtbot.wait(1)
    box = playlist_card.archive_check
    box.setChecked(True)
    checked = box.grab().toImage()
    box.setChecked(False)
    unchecked = box.grab().toImage()
    indicator = [(x, y) for x in range(16) for y in range(unchecked.height())]
    assert max(unchecked.pixelColor(x, y).lightness() for x, y in indicator) > 40  # a box outline
    assert any(checked.pixelColor(x, y) != unchecked.pixelColor(x, y) for x, y in indicator)


def test_every_theme_image_exists_and_is_bundled():
    import re
    from pathlib import Path

    from stuff_downloader.gui.theme import STYLE

    urls = re.findall(r"url\(([^)]+)\)", STYLE)
    assert urls
    for url in urls:
        path = Path(url)
        assert path.is_file(), url
        assert path.suffix == ".png"  # the frozen build bundles resources/*.png
    spec = (Path(__file__).resolve().parents[2] / "packaging" / "StuffDownloader.spec").read_text(
        encoding="utf-8"
    )
    assert 'RESOURCES.glob("*.png")' in spec


def test_the_gallery_grid_follows_the_dark_theme(qtbot):
    from PyQt6.QtGui import QPalette

    from stuff_downloader.gui.theme import STYLE
    from stuff_downloader.gui.widgets import GalleryCard

    card = GalleryCard()
    qtbot.addWidget(card)
    card.setStyleSheet(STYLE)
    card.resize(800, 500)
    card.show()
    qtbot.wait(1)
    grid = card.grid.grab().toImage()
    assert grid.pixelColor(grid.width() - 5, grid.height() - 5).lightness() < 80
    assert card.grid.palette().color(QPalette.ColorRole.Text).lightness() > 150


def test_spotify_change_buttons_fit_their_rows(qtbot):
    from stuff_downloader.core.spotify import SpotifyTrack
    from stuff_downloader.gui.theme import STYLE

    card = SpotifyCard()
    qtbot.addWidget(card)
    card.setStyleSheet(STYLE)
    card.set_tracks([SpotifyTrack("a" * 22, 1, "Song", ("A",), duration=100.0)])
    card.show()
    qtbot.wait(1)
    button = card.change_button(0)
    assert button.height() >= button.sizeHint().height()


def test_dialogs_follow_the_dark_theme_and_show_the_selection(qtbot):
    from PyQt6.QtWidgets import QApplication

    from stuff_downloader.core.spotify import Candidate, SpotifyTrack
    from stuff_downloader.gui.theme import BG, STYLE
    from stuff_downloader.gui.widgets import MatchDialog

    app = QApplication.instance()
    previous = app.styleSheet()
    app.setStyleSheet(STYLE)  # a dialog is a top-level window: the app's sheet reaches it
    try:
        track = SpotifyTrack("a" * 22, 1, "Song", ("A",), duration=100.0)
        dialog = MatchDialog(track, (Candidate("abcdefghijk", "Song", "A", 100.0),
                                     Candidate("bcdefghijkl", "Other", "B", 90.0)))
        qtbot.addWidget(dialog)
        dialog.resize(600, 360)
        dialog.show()
        qtbot.wait(1)
        image = dialog.grab().toImage()
        assert image.pixelColor(3, 3).name() == BG
        dialog.table.selectRow(1)
        qtbot.wait(1)
        table = dialog.table.grab().toImage()
        row = dialog.table.visualRect(dialog.table.model().index(1, 3))
        other = dialog.table.visualRect(dialog.table.model().index(0, 3))
        offset = dialog.table.horizontalHeader().height()
        chosen = table.pixelColor(row.center().x(), row.center().y() + offset)
        plain = table.pixelColor(other.center().x(), other.center().y() + offset)
        assert abs(chosen.lightness() - plain.lightness()) > 15 or chosen.hue() != plain.hue()
    finally:
        app.setStyleSheet(previous)
