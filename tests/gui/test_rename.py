"""GUI: an edited title names the file; an untouched one never does (item 2, plan §5.5)."""

from __future__ import annotations

from test_main_window import _analyzed
from test_playlist_queue import expand, listing, runs, window  # noqa: F401  (fixtures)

from stuff_downloader.gui.widgets import TrackTable


def _download_playlist(page):
    return [job.spec for job in page.start_playlist_download()]


def _edit_title(page, row, text):
    """What the owner does by clicking the Title cell and typing."""
    card = page.playlist_card
    card.table.item(row, card.TITLE_COLUMN).setText(text)


def test_a_single_item_sends_only_an_edited_title(window, runs, qtbot):  # noqa: F811
    page = _analyzed(window, runs, qtbot)
    editor = page.result_card.title_editor
    assert "edited_title" not in page.start_download().spec.options
    editor.start_editing()
    editor.edit.setText("   ")  # a blank edit keeps the original
    editor.edit.editingFinished.emit()
    assert "edited_title" not in page.start_download().spec.options
    editor.start_editing()
    editor.edit.setText("My clip")
    editor.edit.editingFinished.emit()
    assert page.start_download().spec.options["edited_title"] == "My clip"


def test_untouched_playlist_rows_keep_the_default_template(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(3, unavailable_last=False))
    card = page.playlist_card
    assert card.table.item(0, card.TITLE_COLUMN).text() == "Track 0"
    assert card.output_name(0) is None
    names = page._playlist_output_names(page._listing.entries)
    assert names == {}


def test_an_edited_playlist_row_reaches_its_job_spec(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(3, unavailable_last=False))
    _edit_title(page, 1, "  Renamed  ")
    _edit_title(page, 2, " ")
    specs = _download_playlist(page)
    by_index = {s.options["playlist_index"]: s.options for s in specs}
    assert by_index[2]["edited_title"] == "Renamed"
    assert "edited_title" not in by_index[1] and "edited_title" not in by_index[3]
    assert not any("output_name" in options for options in by_index.values())


def test_a_title_edited_back_to_the_original_is_not_a_name(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(2, unavailable_last=False))
    _edit_title(page, 0, "Something")
    _edit_title(page, 0, "Track 0")
    assert page._playlist_output_names(page._listing.entries) == {}


def test_names_that_sanitize_alike_are_made_unique(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(4, unavailable_last=False))
    for row, text in enumerate(["a:b", "a?b", "A_B", "CON"]):
        _edit_title(page, row, text)
    names = page._playlist_output_names(page._listing.entries)
    assert list(names.values()) == ["a_b", "a_b (2)", "A_B (3)"]  # CON keeps the default


def test_a_long_duplicate_name_still_gets_a_suffix(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(2, unavailable_last=False))
    _edit_title(page, 0, "x" * 250)
    _edit_title(page, 1, "x" * 250)
    first, second = page._playlist_output_names(page._listing.entries).values()
    assert first == "x" * 200
    assert second.endswith(" (2)") and len(second) == 200


def test_no_file_name_column_or_field_appears_anywhere(window, runs, qtbot):  # noqa: F811
    """Plan §5.5: no "File name" field or column anywhere; this fails if one comes back."""
    from PyQt6.QtWidgets import QLabel, QLineEdit, QTableWidget

    page = _analyzed(window, runs, qtbot)
    expand(window, runs, payload=listing(2, unavailable_last=False))
    for table in window.findChildren(QTableWidget):
        headers = [
            (table.horizontalHeaderItem(c).text() if table.horizontalHeaderItem(c) else "")
            for c in range(table.columnCount())
        ]
        assert not any("file name" in h.lower() for h in headers), headers
    for label in window.findChildren(QLabel):
        assert label.text().strip().lower() != "file name"
    for edit in window.findChildren(QLineEdit):
        assert "file name" not in edit.placeholderText().lower()
    assert page.playlist_card.table.columnCount() == len(TrackTable.BASE_COLUMNS)
