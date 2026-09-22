"""GUI: the queue's Clear button and each card's Open / Show in folder (items 3 and 5)."""

from __future__ import annotations

from test_main_window import _analyzed
from test_playlist_queue import expand, listing, runs, window  # noqa: F401  (fixtures)


def _finish(page, runs, qtbot, state, files=None):  # noqa: F811
    job = page.start_download()
    run = next(r for r in runs if r.spec.job_id == job.spec.job_id)
    if state == "completed":
        run.emit("result", files=[str(f) for f in files or []], total_bytes=1)
    elif state == "failed":
        run.emit("error", code="download_error", message="ERROR: Private video")
    elif state == "skipped":
        run.emit("result", skipped=True, skipped_reason="Already downloaded", files=[])
    return job


def test_clear_is_disabled_until_something_has_finished(window, runs, qtbot):  # noqa: F811
    page = _analyzed(window, runs, qtbot)
    assert not page.clear_queue_button.isEnabled()
    page.start_download()  # running
    assert not page.clear_queue_button.isEnabled()


def test_clear_removes_only_finished_cards_and_keeps_history(
    window, runs, qtbot, tmp_path  # noqa: F811
):
    window.settings_page.set_folder(str(tmp_path))
    page = _analyzed(window, runs, qtbot)
    out = tmp_path / "done.mp4"
    out.write_bytes(b"x")
    done = _finish(page, runs, qtbot, "completed", [out])
    failed = _finish(page, runs, qtbot, "failed")
    skipped = _finish(page, runs, qtbot, "skipped")
    cancelled = page.start_download()
    page.cancel_job(cancelled.spec.job_id)
    running = page.start_download()
    assert page.clear_queue_button.isEnabled()

    page.clear_finished_jobs()

    assert set(page.jobs) == {running.spec.job_id}
    for job in (done, failed, skipped, cancelled):
        assert job.spec.job_id not in page.jobs
    assert not page.clear_queue_button.isEnabled()
    assert page.empty_state.isHidden()
    # History keeps every job, cleared or not.
    assert page.store._conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 5


def test_clearing_everything_brings_back_the_empty_state(window, runs, qtbot):  # noqa: F811
    page = _analyzed(window, runs, qtbot)
    _finish(page, runs, qtbot, "failed")
    page.clear_finished_jobs()
    assert not page.jobs and not page.empty_state.isHidden()


def test_a_playlist_is_cleared_only_once_all_of_it_has_finished(window, runs):  # noqa: F811
    page = expand(window, runs, payload=listing(2, unavailable_last=False))
    first, second = page.start_playlist_download()
    first.run.emit("error", code="download_error", message="ERROR: Private video")
    assert not page.clear_queue_button.isEnabled()  # the group's counts need the rest
    page.clear_finished_jobs()
    assert first.spec.job_id in page.jobs and len(page._groups) == 1
    second.run.emit("error", code="download_error", message="ERROR: Private video")
    page.clear_finished_jobs()
    assert not page.jobs and not page._groups


def test_open_uses_the_final_file_never_a_part_or_stream(
    window, runs, qtbot, tmp_path, monkeypatch  # noqa: F811
):
    from stuff_downloader.gui import pages

    window.settings_page.set_folder(str(tmp_path))
    page = _analyzed(window, runs, qtbot)
    names = ["Song.webm.part", "Song.f251.webm", "Song.mp3"]
    for name in names:
        (tmp_path / name).write_bytes(b"x")
    job = _finish(page, runs, qtbot, "completed", [tmp_path / n for n in names])
    opened = []
    monkeypatch.setattr(pages.QDesktopServices, "openUrl", lambda url: opened.append(url) or True)
    assert page.open_file(job.spec.job_id)
    assert opened[0].toLocalFile().endswith("/Song.mp3")


def test_a_folder_is_never_opened_as_the_file(window, runs, qtbot, tmp_path):  # noqa: F811
    window.settings_page.set_folder(str(tmp_path))
    page = _analyzed(window, runs, qtbot)
    (tmp_path / "sub").mkdir()
    job = _finish(page, runs, qtbot, "completed", [tmp_path / "sub"])
    assert not page.open_file(job.spec.job_id)
    assert not job.card.open_button.isEnabled()


def test_a_missing_file_disables_open_and_says_why(
    window, runs, qtbot, tmp_path, monkeypatch  # noqa: F811
):
    from stuff_downloader.gui import pages

    window.settings_page.set_folder(str(tmp_path))
    page = _analyzed(window, runs, qtbot)
    out = tmp_path / "Song.mp3"
    out.write_bytes(b"x")
    job = _finish(page, runs, qtbot, "completed", [out])
    assert job.card.open_button.isEnabled() and job.card.folder_button.isEnabled()

    out.unlink()  # moved or deleted after the download
    popen = []
    monkeypatch.setattr(pages.subprocess, "Popen", lambda args: popen.append(args))
    assert not page.show_in_folder(job.spec.job_id)
    assert not popen
    for button in (job.card.open_button, job.card.folder_button):
        assert not button.isHidden() and not button.isEnabled()
        assert "moved or deleted" in button.toolTip()
    assert job.card.details_label.text().endswith("The file was moved or deleted.")
    page.open_file(job.spec.job_id)  # asking again never repeats the reason
    assert job.card.details_label.text().count("moved or deleted") == 1


def test_show_in_folder_passes_explorer_an_argument_list(
    window, runs, qtbot, tmp_path, monkeypatch  # noqa: F811
):
    from stuff_downloader.gui import pages

    folder = tmp_path / "with space & ampersand"
    folder.mkdir()
    window.settings_page.set_folder(str(folder))
    page = _analyzed(window, runs, qtbot)
    out = folder / "A & B.mp3"
    out.write_bytes(b"x")
    job = _finish(page, runs, qtbot, "completed", [out])
    calls = []
    monkeypatch.setattr(pages.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(pages.sys, "platform", "win32")
    assert page.show_in_folder(job.spec.job_id)
    ((args, kwargs),) = calls
    assert args == (["explorer.exe", "/select,", str(out.resolve())],)
    assert "shell" not in kwargs
