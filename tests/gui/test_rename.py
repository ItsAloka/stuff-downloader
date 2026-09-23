"""GUI: a typed output name reaches the job spec; an untouched one never does (item 2)."""

from __future__ import annotations

from PyQt6.QtCore import Qt
from test_main_window import _analyzed
from test_playlist_queue import expand, listing, runs, window  # noqa: F401  (fixtures)


def _download_playlist(page):
    return [job.spec for job in page.start_playlist_download()]


def _name_edit(page, row):
    return page.playlist_card.table.cellWidget(row, 6)


def test_a_single_item_sends_only_a_typed_name(window, runs, qtbot):  # noqa: F811
    page = _analyzed(window, runs, qtbot)
    assert page.preview.name_edit.text() == ""
    assert page.preview.name_edit.placeholderText()  # the title, as a hint
    assert "output_name" not in page.start_download().spec.options
    page.preview.name_edit.setText("   ")
    assert "output_name" not in page.start_download().spec.options
    page.preview.name_edit.setText("My clip")
    assert page.start_download().spec.options["output_name"] == "My clip"


def test_clicking_single_title_opens_name_editor(window, runs, qtbot):  # noqa: F811
    page = _analyzed(window, runs, qtbot)
    page.preview.name_edit.setText("Renamed")
    qtbot.mouseClick(page.preview.title_label, Qt.MouseButton.LeftButton)
    assert page.preview.name_edit.selectedText() == "Renamed"


def test_clicking_playlist_title_opens_its_name_editor(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(2, unavailable_last=False))
    _name_edit(page, 0).setText("Renamed")
    page.playlist_card._focus_name_on_title_click(0, page.playlist_card.TITLE_COLUMN)
    assert _name_edit(page, 0).selectedText() == "Renamed"


def test_untouched_playlist_rows_keep_the_default_template(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(3, unavailable_last=False))
    assert _name_edit(page, 0).text() == "" and _name_edit(page, 0).placeholderText() == "Track 0"
    names = page._playlist_output_names(page._listing.entries)
    assert names == {}


def test_an_edited_playlist_row_reaches_its_job_spec(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(3, unavailable_last=False))
    _name_edit(page, 1).setText("  Renamed  ")
    _name_edit(page, 2).setText(" ")
    specs = _download_playlist(page)
    by_index = {s.options["playlist_index"]: s.options for s in specs}
    assert by_index[2]["output_name"] == "Renamed"
    assert "output_name" not in by_index[1] and "output_name" not in by_index[3]


def test_names_that_sanitize_alike_are_made_unique(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(4, unavailable_last=False))
    for row, text in enumerate(["a:b", "a?b", "A_B", "CON"]):
        _name_edit(page, row).setText(text)
    names = page._playlist_output_names(page._listing.entries)
    assert list(names.values()) == ["a_b", "a_b (2)", "A_B (3)"]  # CON keeps the default


def test_a_long_duplicate_name_still_gets_a_suffix(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(2, unavailable_last=False))
    _name_edit(page, 0).setText("x" * 250)
    _name_edit(page, 1).setText("x" * 250)
    first, second = page._playlist_output_names(page._listing.entries).values()
    assert first == "x" * 200
    assert second.endswith(" (2)") and len(second) == 200
