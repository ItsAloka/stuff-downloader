"""GUI: playlist expansion, batch enqueue, queue controls, restore-as-paused and history."""

from __future__ import annotations

import logging
import sqlite3

import pytest
from PyQt6.QtGui import QCloseEvent
from test_main_window import FakeRun, _analyzed  # reuse the runner stand-in

from stuff_downloader.core import history, settings, tools
from stuff_downloader.core.protocol import Event
from stuff_downloader.gui.main_window import MainWindow
from stuff_downloader.gui.pages import DownloadsPage

VID = "dQw4w9WgXcQ"
PLAYLIST_URL = "https://www.youtube.com/playlist?list=PLabc123_-XYZ"
SONG_IN_PLAYLIST = f"https://www.youtube.com/watch?v={VID}&list=PLabc123_-XYZ"


def listing(count=4, unavailable_last=True):
    entries = []
    for i in range(count):
        entries.append(
            {
                "id": f"vid{i:07d}aaa"[:11],
                "title": f"Track {i}",
                "uploader": "A",
                "duration": 60,
            }
        )
    entries[0]["id"] = VID
    if unavailable_last:
        entries[-1]["unavailable"] = "Private video"
    return {
        "kind": "playlist",
        "playlist_id": "PLabc123_-XYZ",
        "title": "Chill Mix",
        "uploader": "Someone",
        "entries": entries,
    }


@pytest.fixture
def window(qtbot, monkeypatch):
    from stuff_downloader.gui import pages

    FakeRun.instances = []
    monkeypatch.setattr(pages, "JobRun", FakeRun)
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [])
    w = MainWindow(settings.Settings())
    qtbot.addWidget(w)
    return w


@pytest.fixture
def runs():
    return FakeRun.instances


def expand(window, runs, url=PLAYLIST_URL, payload=None):
    page = window.downloads_page
    page.url_edit.setText(url)
    page.analyze()
    run = runs[-1]
    # Posted directly: the payload has its own "kind" field, which FakeRun.emit would shadow.
    run.on_event(Event("result", run.spec.job_id, payload or listing()))
    return page


# ── expansion ────────────────────────────────────────────────────────────────────────────
def test_a_playlist_link_is_read_with_the_playlist_mode(window, runs):
    page = window.downloads_page
    page.url_edit.setText(PLAYLIST_URL)
    page.analyze()
    (run,) = runs
    assert run.spec.options == {"mode": "playlist"}
    assert run.spec.url == PLAYLIST_URL


def test_the_table_lists_every_entry_and_unavailable_ones_cannot_be_selected(window, runs):
    page = expand(window, runs)
    card = page.playlist_card
    assert not card.isHidden() and page.preview.isHidden()
    assert card.title_label.text() == "Chill Mix"
    assert card.table.rowCount() == 4
    assert card.table.item(0, 2).text() == "Track 0"
    assert card.table.item(3, 5).text() == "Private video"
    assert card.checkbox(0).isChecked() and card.checkbox(0).isEnabled()
    assert not card.checkbox(3).isEnabled() and not card.checkbox(3).isChecked()
    assert card.selection_label.text() == "3 selected"


def test_select_all_none_and_filter(window, runs):
    page = expand(window, runs)
    card = page.playlist_card
    page._set_playlist_selection(False)
    assert card.selection_label.text() == "0 selected"
    assert not card.download_button.isEnabled()
    page._set_playlist_selection(True)
    assert card.selection_label.text() == "3 selected"

    card.filter_edit.setText("Track 1")
    assert card.table.isRowHidden(0) and not card.table.isRowHidden(1)
    card.filter_edit.setText("")
    assert not card.table.isRowHidden(0)


def test_a_playlist_that_is_not_one_reports_the_engine_error(window, runs):
    page = window.downloads_page
    page.url_edit.setText(PLAYLIST_URL)
    page.analyze()
    runs[-1].emit("error", code="unsupported", message="that link is not a playlist")
    assert page.playlist_card.isHidden()
    assert "not a playlist" in page.message_label.text()


def test_the_whole_playlist_button_reanalyzes_the_playlist(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, url=SONG_IN_PLAYLIST)
    assert not page.preview.playlist_button.isHidden()
    page.open_playlist()
    assert runs[-1].spec.url == PLAYLIST_URL
    assert runs[-1].spec.options == {"mode": "playlist"}


def test_a_radio_mix_offers_no_playlist_button_and_says_why(window, runs, qtbot):
    page = _analyzed(window, runs, qtbot, url=f"https://www.youtube.com/watch?v={VID}&list=RDxyz")
    assert page.preview.playlist_button.isHidden()
    assert "radio" in page.preview.playlist_label.text().lower()


# ── batch enqueue ────────────────────────────────────────────────────────────────────────
def test_selected_entries_become_numbered_jobs_behind_the_concurrency_limit(window, runs):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    assert len(jobs) == 3
    specs = [j.spec for j in jobs]
    assert [s.options["playlist_index"] for s in specs] == [1, 2, 3]
    assert all(s.options["playlist_title"] == "Chill Mix" for s in specs)
    assert all(s.options["playlist_count"] == 4 for s in specs)
    assert all(s.options["preset"] == "mp3_music" for s in specs)

    started = [r for r in runs if r.started and r.spec.options.get("mode") == "download"]
    assert len(started) == page.scheduler.max_concurrent == 3
    assert page.queue_summary.text() == "3 active"


def test_turning_the_archive_off_re_downloads(window, runs):
    page = expand(window, runs)
    page.playlist_card.archive_check.setChecked(False)
    jobs = page.start_playlist_download()
    assert all("archive" not in j.spec.options for j in jobs)


def test_nothing_selected_enqueues_nothing(window, runs):
    page = expand(window, runs)
    page._set_playlist_selection(False)
    assert page.start_playlist_download() == []
    assert page.jobs == {}


def test_the_group_row_aggregates_done_failed_and_skipped(window, runs):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    group = next(iter(page._groups.values()))
    assert group.card.summary_label.text() == "0 / 3 done"

    jobs[0].run.emit("result", files=[], total_bytes=10)
    jobs[1].run.emit("result", skipped=True, skipped_reason="Already downloaded")
    jobs[2].run.emit("error", code="download_error", message="Private video")
    text = group.card.summary_label.text()
    assert "1 / 3 done" in text and "1 already downloaded" in text and "1 failed" in text
    assert jobs[1].state == "skipped"
    assert jobs[1].card.chip.text() == "Already downloaded"


def test_a_finished_job_frees_its_slot_for_the_next_one(window, runs):
    page = expand(window, runs, payload=listing(count=5, unavailable_last=False))
    jobs = page.start_playlist_download()
    assert len(jobs) == 5
    assert sum(1 for j in jobs if j.state == "active") == 3
    assert jobs[3].state == "queued" and jobs[3].card.chip.text() == "Queued"
    jobs[0].run.emit("result", files=[], total_bytes=1)
    assert jobs[3].state == "active"


# ── queue controls ───────────────────────────────────────────────────────────────────────
def test_pausing_a_running_job_is_not_a_cancellation(window, runs):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    job = jobs[0]
    page.pause_job(job.spec.job_id)
    assert job.run is None and job.state == "paused"
    assert job.card.chip.text() == "Paused"
    assert "partial file is kept" in job.card.details_label.text()
    assert page.store.get(job.spec.job_id).state == "paused"


def test_pausing_a_queued_job_never_starts_a_worker(window, runs):
    page = expand(window, runs, payload=listing(count=5, unavailable_last=False))
    jobs = page.start_playlist_download()
    queued = jobs[4]
    before = len(runs)
    page.pause_job(queued.spec.job_id)
    assert queued.state == "paused" and len(runs) == before


def test_resuming_starts_the_job_again(window, runs):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    job = jobs[0]
    page.toggle_pause(job.spec.job_id)
    assert job.state == "paused"
    page.toggle_pause(job.spec.job_id)
    assert job.state == "active" and job.run is not None
    assert runs[-1].spec.job_id == job.spec.job_id


def test_pause_all_stops_everything_running_and_queued(window, runs):
    page = expand(window, runs, payload=listing(count=5, unavailable_last=False))
    jobs = page.start_playlist_download()
    page.pause_all()
    assert all(j.state == "paused" for j in jobs)
    assert page.scheduler.active_count == 0
    assert "5 paused" in page.queue_summary.text()


def test_cancelling_a_queued_job_marks_it_cancelled_without_a_worker(window, runs):
    page = expand(window, runs, payload=listing(count=5, unavailable_last=False))
    jobs = page.start_playlist_download()
    queued = jobs[4]
    page.cancel_job(queued.spec.job_id)
    assert queued.state == "cancelled"
    assert page.store.get(queued.spec.job_id).state == "cancelled"


# ── persistence ──────────────────────────────────────────────────────────────────────────
def test_every_job_is_recorded_as_it_moves(window, runs, tmp_path):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    record = page.store.get(jobs[0].spec.job_id)
    assert record.state == "active" and record.title == "Track 0"
    assert record.options["playlist_index"] == 1
    out = tmp_path / "song.mp3"
    out.write_bytes(b"x")
    jobs[0].run.emit("result", files=[str(out)], total_bytes=5)
    record = page.store.get(jobs[0].spec.job_id)
    assert record.state == "completed" and record.total_bytes == 5
    assert record.files == [str(out)]


def test_unfinished_jobs_come_back_paused_and_start_nothing(qtbot, monkeypatch, tmp_path):
    from stuff_downloader.gui import pages

    FakeRun.instances = []
    monkeypatch.setattr(pages, "JobRun", FakeRun)
    store = history.Store(tmp_path / "db.sqlite3")
    store.add_job(
        "old-job",
        f"https://www.youtube.com/watch?v={VID}",
        "ytdlp",
        {"mode": "download", "preset": "mp3_music", "playlist_index": 2},
        str(tmp_path),
        title="Half-done song",
        state="active",
    )
    page = DownloadsPage(settings.Settings(), store)
    qtbot.addWidget(page)
    job = page.jobs["old-job"]
    assert job.state == "paused" and job.card.chip.text() == "Paused"
    assert job.title == "Half-done song"
    assert FakeRun.instances == []  # nothing restarts on its own
    assert page.scheduler.active_count == 0

    page.resume_job("old-job")
    assert page.jobs["old-job"].state == "active"
    assert FakeRun.instances[-1].spec.job_id == "old-job"
    page.shutdown()


def test_a_restored_job_with_an_unknown_preset_is_skipped(qtbot, monkeypatch, tmp_path):
    from stuff_downloader.gui import pages

    FakeRun.instances = []
    monkeypatch.setattr(pages, "JobRun", FakeRun)
    store = history.Store(tmp_path / "db.sqlite3")
    store.add_job("weird", "https://www.youtube.com/watch?v=x", "ytdlp",
                  {"mode": "download", "preset": "from_the_future"}, str(tmp_path), state="active")
    page = DownloadsPage(settings.Settings(), store)
    qtbot.addWidget(page)
    assert page.jobs == {}
    page.shutdown()


# ── history page ─────────────────────────────────────────────────────────────────────────
def test_history_lists_finished_jobs_and_searches_them(window, runs, tmp_path):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    out = tmp_path / "song.mp3"
    out.write_bytes(b"x")
    jobs[0].run.emit("result", files=[str(out)], total_bytes=5)
    jobs[1].run.emit("error", code="download_error", message="Private video")

    history_page = window.history_page
    history_page.refresh()
    assert history_page.table.rowCount() == 2
    titles = {history_page.table.item(r, 0).text() for r in range(2)}
    assert titles == {"Track 0", "Track 1"}

    history_page.search_edit.setText("Track 0")
    assert history_page.table.rowCount() == 1
    assert history_page.table.item(0, 1).text() == "Completed"
    history_page.search_edit.setText("nothing at all")
    assert history_page.table.rowCount() == 0 and not history_page.empty_label.isHidden()


def test_history_remove_deletes_the_entry_but_keeps_the_file(window, runs, tmp_path):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    out = tmp_path / "song.mp3"
    out.write_bytes(b"x")
    jobs[0].run.emit("result", files=[str(out)], total_bytes=5)

    history_page = window.history_page
    history_page.refresh()
    history_page.table.setCurrentCell(0, 0)
    assert history_page.forget_selected()
    assert history_page.table.rowCount() == 0
    assert out.is_file()


def test_history_filters_and_download_again_signal(window, runs, tmp_path, qtbot):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"x")
    jobs[0].run.emit("result", files=[str(audio)], total_bytes=5)
    jobs[1].run.emit("error", code="download_error", message="Private video")

    history_page = window.history_page
    history_page.refresh()
    history_page.type_filter.setCurrentIndex(history_page.type_filter.findData("audio"))
    history_page.status_filter.setCurrentIndex(history_page.status_filter.findData("completed"))
    history_page.site_filter.setCurrentIndex(history_page.site_filter.findData("youtube.com"))
    assert history_page.table.rowCount() == 1
    with qtbot.waitSignal(history_page.download_again_requested) as blocker:
        history_page.table.setCurrentCell(0, 0)
        assert history_page.download_again_selected()
    assert blocker.args[0].job_id == jobs[0].spec.job_id
    assert page.store.get(jobs[0].spec.job_id).state == "completed"


def test_history_file_actions_refuse_a_path_outside_the_output_folder(window, runs, tmp_path):
    page = expand(window, runs)
    page._settings.download_dir = str(tmp_path / "out")
    (tmp_path / "out").mkdir()
    jobs = page.start_playlist_download()
    outside = tmp_path / "elsewhere.exe"
    outside.write_bytes(b"MZ")
    jobs[0].run.emit("result", files=[str(outside)], total_bytes=2)

    history_page = window.history_page
    history_page.refresh()
    history_page.table.setCurrentCell(0, 0)
    assert history_page.selected_file() is None
    assert not history_page.open_selected() and not history_page.show_selected_in_folder()


def test_history_show_in_folder_passes_explorer_separate_arguments(
    window, runs, tmp_path, monkeypatch
):
    from stuff_downloader.gui import pages

    out_dir = tmp_path / "my music"
    out_dir.mkdir()
    page = expand(window, runs)
    page._settings.download_dir = str(out_dir)
    jobs = page.start_playlist_download()
    song = out_dir / "A & B.mp3"
    song.write_bytes(b"x")
    jobs[0].run.emit("result", files=[str(song)], total_bytes=1)
    history_page = window.history_page
    history_page.refresh()
    history_page.table.setCurrentCell(0, 0)
    calls = []
    monkeypatch.setattr(pages.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(pages.sys, "platform", "win32")
    assert history_page.show_selected_in_folder()
    assert calls == [((["explorer.exe", "/select,", str(song.resolve())],), {})]


# ── store ownership ──────────────────────────────────────────────────────────────────────
def test_downloads_page_never_closes_a_store_it_was_given(window):
    """The window shares one store with both pages, so the page must not close it."""
    page = window.downloads_page
    assert page.owns_store is False and window.owns_store is True
    page.shutdown()
    window.history_page.refresh()  # would raise ProgrammingError on a closed database
    assert window.history_page.table.rowCount() == 0
    assert page.store.search() == []


def test_the_window_closes_the_shared_store_last(window, runs, tmp_path):
    page = expand(window, runs)
    jobs = page.start_playlist_download()
    out = tmp_path / "song.mp3"
    out.write_bytes(b"x")
    jobs[0].run.emit("result", files=[str(out)], total_bytes=5)

    window.close()
    with pytest.raises(sqlite3.ProgrammingError):
        window.store.search()


def test_a_failing_shutdown_is_logged_and_never_escapes_into_qt(window, caplog):
    """An exception reaching the Qt event loop aborts the process, so closeEvent swallows it."""

    def explode():
        raise RuntimeError("worker teardown exploded")

    window.downloads_page.shutdown = explode
    with caplog.at_level(logging.ERROR, logger="stuff_downloader.gui.main_window"):
        window.closeEvent(QCloseEvent())  # must not raise

    assert "Stopping downloads failed during shutdown" in caplog.text
    assert "worker teardown exploded" in caplog.text  # the cause is still recorded
    with pytest.raises(sqlite3.ProgrammingError):
        window.store.search()  # ...and the store was still closed


def test_a_caller_owned_store_survives_a_failing_shutdown(qtbot, monkeypatch, tmp_path):
    """MainWindow never closes a store it was handed, even on the exception path."""
    from stuff_downloader.gui import pages

    FakeRun.instances = []
    monkeypatch.setattr(pages, "JobRun", FakeRun)
    monkeypatch.setattr(tools, "check_all", lambda configured=None: [])
    store = history.Store(tmp_path / "caller.sqlite3")
    w = MainWindow(settings.Settings(), store)
    qtbot.addWidget(w)
    assert w.owns_store is False

    def explode():
        raise RuntimeError("worker teardown exploded")

    w.downloads_page.shutdown = explode
    w.closeEvent(QCloseEvent())  # must not raise
    assert store.search() == []  # still open: closing it is the caller's job
    store.close()


def test_a_standalone_page_still_closes_the_store_it_opened(qtbot, monkeypatch, tmp_path):
    from stuff_downloader.gui import pages

    FakeRun.instances = []
    monkeypatch.setattr(pages, "JobRun", FakeRun)
    monkeypatch.setattr(history, "database_path", lambda: tmp_path / "own.sqlite3")
    page = DownloadsPage(settings.Settings())
    qtbot.addWidget(page)
    assert page.owns_store is True
    page.shutdown()
    with pytest.raises(sqlite3.ProgrammingError):
        page.store.search()


def test_history_actions_do_nothing_without_a_selection(window):
    history_page = window.history_page
    history_page.refresh()
    assert history_page.selected_record() is None
    assert not history_page.forget_selected()
    assert not history_page.open_selected()


# ── drag to reorder ──────────────────────────────────────────────────────────────────────
def _queued_page(window, runs, count=5):
    """A page with two jobs running and the rest queued behind them."""
    page = window.downloads_page
    page.scheduler.set_max_concurrent(2)
    page = expand(window, runs, payload=listing(count=count, unavailable_last=False))
    page.start_playlist_download()
    return page


def test_dragging_a_queued_row_changes_the_order_jobs_start_in(window, runs):
    page = _queued_page(window, runs)
    queued = page.scheduler.queued_ids()
    assert len(queued) == 3
    last, first = queued[-1], queued[0]
    assert page.reorder_queue(last, first) is True
    assert page.scheduler.queued_ids() == [last, first, queued[1]]

    # The order the owner set is what actually runs next.
    running = list(page.scheduler._active)
    page._on_event(Event("result", running[0], {"files": [], "total_bytes": 1}))
    assert page.jobs[last].state == "active"


def test_a_reorder_is_persisted_and_survives_a_restart(window, runs, qtbot, tmp_path):
    page = _queued_page(window, runs)
    queued = page.scheduler.queued_ids()
    assert page.reorder_queue(queued[-1], queued[0]) is True
    expected = page.scheduler.queued_ids()
    page.pause_all()
    restored = [r.job_id for r in page.store.unfinished()]
    # The paused jobs come back in the order the owner left them, not the order they arrived.
    assert [j for j in restored if j in expected] == expected


def test_reorder_refuses_rows_that_are_not_queued(window, runs):
    page = _queued_page(window, runs)
    queued = page.scheduler.queued_ids()
    active = next(iter(page.scheduler._active))
    assert page.reorder_queue(active, queued[0]) is False
    assert page.reorder_queue(queued[0], active) is False
    assert page.reorder_queue(queued[0], queued[0]) is False
    assert page.reorder_queue("nobody", queued[0]) is False
    assert page.scheduler.queued_ids() == queued


def test_only_queued_rows_can_be_dragged(window, runs):
    page = _queued_page(window, runs)
    queued = page.scheduler.queued_ids()
    active = next(iter(page.scheduler._active))
    assert page.jobs[queued[0]].card.draggable is True
    assert page.jobs[active].card.draggable is False
    page.pause_job(queued[0])
    assert page.jobs[queued[0]].card.draggable is False
    page.resume_job(queued[0])
    assert page.jobs[queued[0]].card.draggable is True


def test_a_dropped_card_asks_the_page_to_reorder(window, runs):
    from PyQt6.QtCore import QMimeData, QPointF, Qt
    from PyQt6.QtGui import QDropEvent

    from stuff_downloader.gui.widgets import QUEUE_MIME

    page = _queued_page(window, runs)
    source, target = page.scheduler.queued_ids()[-1], page.scheduler.queued_ids()[0]
    data = QMimeData()
    data.setData(QUEUE_MIME, source.encode("utf-8"))
    event = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.MoveAction,
        data,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    page.jobs[target].card.dropEvent(event)
    assert event.isAccepted()
    assert page.scheduler.queued_ids()[0] == source

    # A drop carrying something else is refused. (Qt does not own the QMimeData, so the test
    # must keep it alive for as long as the event it was handed to.)
    empty = QMimeData()
    other = QDropEvent(
        QPointF(1, 1),
        Qt.DropAction.MoveAction,
        empty,
        Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    )
    page.jobs[target].card.dropEvent(other)
    assert not other.isAccepted()
