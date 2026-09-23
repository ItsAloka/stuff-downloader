from __future__ import annotations

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import (
    QApplication,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
)

from stuff_downloader.core import playlist, settings
from stuff_downloader.gui.pages import DownloadsPage
from stuff_downloader.gui.widgets import JobCard, PlaylistCard, PreviewCard, SpotifyCard


@pytest.mark.parametrize("width", [520, 900, 1600])
@pytest.mark.parametrize("font_scale", [1.0, 1.5])
def test_queue_reflows_without_horizontal_scroll(qtbot, width, font_scale):
    page = DownloadsPage(settings.Settings())
    qtbot.addWidget(page)
    if font_scale > 1:
        font = page.font()
        font.setPointSizeF(font.pointSizeF() * font_scale)
        page.setFont(font)
    card = JobCard()
    card.title_label.setText("A very long song title with details " * 15)
    card.details_label.setText("A long status detail " * 10)
    page.queue_layout.addWidget(card)
    page.resize(width, 720)
    page.show()
    qtbot.wait(1)

    assert page.page_scroll.horizontalScrollBar().maximum() == 0
    assert card.width() <= page.page_scroll.viewport().width()
    assert card.title_label.width() < card.width()
    assert card.title_label.height() > card.title_label.fontMetrics().height()
    for control in (card.chip, card.pause_button, card.cancel_button,
                    page.pause_all_button, page.cancel_all_button):
        assert control.isVisible()
        assert control.width() >= control.minimumSizeHint().width()


def test_queue_card_actions_fit_and_accept_keyboard_focus(qtbot):
    card = JobCard()
    qtbot.addWidget(card)
    card.title_label.setText("Long unbroken title " * 30)
    for button in (card.open_button, card.folder_button, card.retry_button):
        button.show()
    card.resize(440, 320)
    card.show()
    qtbot.wait(1)

    for button in (card.open_button, card.folder_button, card.retry_button,
                   card.pause_button, card.cancel_button):
        assert button.isVisible()
        assert button.width() >= button.minimumSizeHint().width()
        assert button.geometry().right() <= card.width()
        button.setFocus()
        assert button.hasFocus()


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


def test_preview_cover_and_controls_keep_their_natural_geometry(qtbot):
    card = PreviewCard()
    qtbot.addWidget(card)
    card.title_label.setText("A very long title " * 12)
    card.resize(card.minimumSizeHint())
    card.show()
    qtbot.wait(1)

    assert card.cover.size().width() == 160 and card.cover.size().height() == 90
    assert card.preset_combo.width() >= 220
    assert card.resolution_combo.width() >= 220
    assert not _intersects(card.cover, card.preset_combo)
    assert not _intersects(card.cover, card.resolution_combo)


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
    assert card.preset_combo.geometry().top() > table_bottom
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


def test_playlist_name_editors_fit_their_rows(qtbot):
    card = PlaylistCard()
    qtbot.addWidget(card)
    from stuff_downloader.gui.theme import STYLE

    card.setStyleSheet(STYLE)
    card.set_entries(
        [
            playlist.PlaylistEntry(
                video_id="id000000001", index=1, url="https://youtu.be/id000000001", title="Song"
            )
        ]
    )
    card.show()
    qtbot.wait(1)
    editor = card.table.cellWidget(0, 6)
    # The on-screen height, after the table's item padding: a squashed editor clips its text.
    assert editor.height() >= editor.sizeHint().height()


def test_playlist_checkbox_indicator_has_room_at_window_sizes(qtbot):
    from stuff_downloader.gui.theme import STYLE

    card = PlaylistCard()
    qtbot.addWidget(card)
    card.setStyleSheet(STYLE)
    card.set_entries([playlist.PlaylistEntry(
        video_id="id000000001", index=1, url="https://youtu.be/id000000001", title="Song"
    )])
    for width in (520, 900, 1600):
        card.resize(width, 500)
        card.show()
        qtbot.wait(1)
        box = card.checkbox(0)
        option = QStyleOptionButton()
        option.initFrom(box)
        indicator = box.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, option, box)
        assert card.table.columnWidth(0) >= 44
        assert box.width() >= 24
        assert box.rect().contains(indicator)
        assert box.isChecked()


def test_playlist_metadata_and_file_name_copy(qtbot):
    card = PlaylistCard()
    qtbot.addWidget(card)
    card.set_entries([playlist.PlaylistEntry(
        video_id="id000000001", index=1, url="https://youtu.be/id000000001",
        title="Copyable Song", uploader="Copyable Artist"
    )])
    card.show()
    table = card.table
    for column, expected in ((2, "Copyable Song"), (3, "Copyable Artist")):
        table.setCurrentCell(0, column)
        table.setFocus()
        qtbot.keyClick(table, Qt.Key.Key_C, modifier=Qt.KeyboardModifier.ControlModifier)
        assert QApplication.clipboard().text() == expected
    editor = table.cellWidget(0, 6)
    editor.setFocus()
    qtbot.keyClick(editor, Qt.Key.Key_C, modifier=Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "Copyable Song"
    editor.setText("output-name")
    qtbot.keyClick(editor, Qt.Key.Key_C, modifier=Qt.KeyboardModifier.ControlModifier)
    assert QApplication.clipboard().text() == "output-name"
    assert card.selected_rows() == [0]
    card.apply_filter("missing")
    assert table.isRowHidden(0)
    card.apply_filter("")
    assert not table.isRowHidden(0)
    assert editor.text() == "output-name"


def test_playlist_right_click_copy_actions(qtbot, monkeypatch):
    card = PlaylistCard()
    qtbot.addWidget(card)
    card.set_entries([playlist.PlaylistEntry(
        video_id="id000000001", index=1, url="https://youtu.be/id000000001",
        title="Copyable Song", uploader="Copyable Artist"
    )])
    card.show()

    def choose_copy(menu, _position):
        actions = [
            action for action in menu.actions()
            if action.text().split("\t", 1)[0].replace("&", "") == "Copy"
        ]
        assert len(actions) == 1
        assert actions[0].isEnabled()
        actions[0].trigger()

    monkeypatch.setattr(QMenu, "exec", choose_copy)
    table = card.table
    for column, expected in ((2, "Copyable Song"), (3, "Copyable Artist")):
        rect = table.visualItemRect(table.item(0, column))
        point = rect.center()
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, table.mapToGlobal(point))
        table.contextMenuEvent(event)
        assert QApplication.clipboard().text() == expected

    editor = table.cellWidget(0, 6)
    point = editor.rect().center()
    event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, point, editor.mapToGlobal(point))
    editor.contextMenuEvent(event)
    assert QApplication.clipboard().text() == "Copyable Song"


def test_the_preview_title_uses_the_width_beside_the_cover(qtbot):
    # A hidden playlist row must not clamp the title column to a sliver.
    card = PreviewCard()
    qtbot.addWidget(card)
    card.title_label.setText("A YouTube Music song defaults to the MP3 music preset")
    card.resize(1200, 400)
    card.show()
    qtbot.wait(1)
    assert card.title_label.width() > 600
    assert card.title_label.geometry().left() - card.cover.geometry().right() < 40


def test_combos_and_checkboxes_follow_the_dark_theme(qtbot):
    from PyQt6.QtGui import QPalette

    from stuff_downloader.gui.theme import STYLE

    card = PreviewCard()
    qtbot.addWidget(card)
    card.setStyleSheet(STYLE)
    card.show()
    qtbot.wait(1)
    check_text = card.compatible_check.palette().color(QPalette.ColorRole.WindowText)
    assert check_text.lightness() > 150  # readable on the dark card
    combo = card.preset_combo.grab().toImage()
    middle = combo.pixelColor(combo.width() // 2, combo.height() // 2)
    assert middle.lightness() < 100  # not a white box on a dark page


def test_styled_combos_keep_an_arrow_and_unchecked_boxes_stay_visible(qtbot):
    from stuff_downloader.gui.theme import STYLE

    card = PreviewCard()
    qtbot.addWidget(card)
    card.setStyleSheet(STYLE)
    card.show()
    qtbot.wait(1)
    combo = card.preset_combo.grab().toImage()
    arrow_zone = [
        combo.pixelColor(x, y).lightness()
        for x in range(combo.width() - 22, combo.width())
        for y in range(combo.height())
    ]
    assert max(arrow_zone) > 120  # something visible where the arrow belongs

    box = card.compatible_check
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
