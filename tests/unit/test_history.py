"""Queue persistence and history: restore-as-paused, search and never touching files."""

from __future__ import annotations

import sqlite3
import threading

import pytest

from stuff_downloader.core import history


@pytest.fixture
def store(tmp_path):
    with history.Store(tmp_path / "db.sqlite3") as s:
        yield s


OPTIONS = {"mode": "download", "preset": "mp3_music", "playlist_index": 3}


def add(store, job_id="j1", state="queued", title="Song", group_id=""):
    store.add_job(job_id, "https://www.youtube.com/watch?v=dQw4w9WgXcQ", "ytdlp", OPTIONS,
                  "C:/dl", title=title, group_id=group_id, state=state)


def test_new_database_is_created_and_versioned(tmp_path):
    path = tmp_path / "nested" / "db.sqlite3"
    with history.Store(path):
        pass
    assert path.is_file()
    conn = sqlite3.connect(str(path))
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == history.SCHEMA_VERSION
    finally:
        conn.close()


def test_reopening_an_existing_database_keeps_its_rows(tmp_path):
    path = tmp_path / "db.sqlite3"
    with history.Store(path) as first:
        add(first)
    with history.Store(path) as second:
        assert second.get("j1") is not None


def test_a_newer_schema_is_refused_rather_than_downgraded(tmp_path):
    path = tmp_path / "db.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA user_version={history.SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="newer than this app"):
        history.Store(path)


def test_job_round_trip_records_options_and_files(store):
    add(store)
    store.set_state("j1", "completed", total_bytes=2048, files=["C:/dl/a.mp3"])
    record = store.get("j1")
    assert record is not None
    assert record.state == "completed" and record.total_bytes == 2048
    assert record.options == OPTIONS and record.preset == "mp3_music"
    assert record.playlist_index == 3 and record.files == ["C:/dl/a.mp3"]


def test_unfinished_jobs_come_back_paused_and_are_not_restarted(store):
    add(store, "active-job", state="active")
    add(store, "queued-job", state="queued")
    add(store, "done-job", state="completed")
    assert store.restore_unfinished() == 2
    assert {r.job_id for r in store.unfinished()} == {"active-job", "queued-job"}
    assert all(r.state == "paused" for r in store.unfinished())
    assert store.get("done-job").state == "completed"


def test_restore_is_idempotent(store):
    add(store, "a", state="active")
    assert store.restore_unfinished() == 1
    assert store.restore_unfinished() == 0


def test_progress_is_not_persisted_only_transitions(store):
    add(store)
    first = store.get("j1").updated_at
    store.set_state("j1", "active")
    assert store.get("j1").updated_at >= first
    assert not hasattr(store, "set_progress")


def test_search_returns_finished_jobs_newest_first(store):
    add(store, "a", title="Alpha")
    add(store, "b", title="Beta")
    store.set_state("a", "completed")
    store.set_state("b", "failed", error_code="download_error", error_message="nope")
    results = store.search()
    assert [r.job_id for r in results] == ["b", "a"]
    assert results[0].error_code == "download_error"


def test_search_ignores_unfinished_jobs(store):
    add(store, "a", title="Alpha", state="active")
    assert store.search() == []


def test_search_matches_title_or_url(store):
    add(store, "a", title="Night Changes")
    store.set_state("a", "completed")
    assert [r.job_id for r in store.search("night")] == ["a"]
    assert [r.job_id for r in store.search("dQw4w9")] == ["a"]
    assert store.search("nothing here") == []


def test_search_escapes_like_wildcards(store):
    add(store, "literal", title="100% real")
    add(store, "other", title="something else")
    for job_id in ("literal", "other"):
        store.set_state(job_id, "completed")
    assert [r.job_id for r in store.search("100%")] == ["literal"]
    assert [r.job_id for r in store.search("%")] == ["literal"]
    assert store.search("_") == []


def test_forget_removes_the_row_but_never_the_file(store, tmp_path):
    downloaded = tmp_path / "song.mp3"
    downloaded.write_bytes(b"audio")
    add(store)
    store.set_state("j1", "completed", files=[str(downloaded)])
    store.forget("j1")
    assert store.get("j1") is None and store.search() == []
    assert downloaded.is_file()


def test_writes_from_several_threads_do_not_corrupt_the_database(store):
    errors: list[Exception] = []

    def work(index: int) -> None:
        try:
            add(store, f"job-{index}")
            store.set_state(f"job-{index}", "completed", total_bytes=index + 1)
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(store.search(limit=50)) == 12


def test_database_lives_in_the_app_data_folder_not_the_download_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert history.database_path().parent == tmp_path / "local" / "StuffDownloader"


# ── persisted queue order ────────────────────────────────────────────────────────────────
def test_unfinished_comes_back_in_the_order_it_was_queued(store):
    for i in range(4):
        add(store, f"j{i}", state="paused")
    assert [r.job_id for r in store.unfinished()] == ["j0", "j1", "j2", "j3"]


def test_set_queue_order_survives_a_reopen(tmp_path):
    path = tmp_path / "db.sqlite3"
    with history.Store(path) as store:
        for i in range(4):
            add(store, f"j{i}", state="paused")
        assert store.set_queue_order(["j3", "j1"]) == 2
    with history.Store(path) as store:
        # j3 and j1 swap the slots they held; j0 and j2 keep theirs.
        assert [r.job_id for r in store.unfinished()] == ["j0", "j3", "j2", "j1"]
        assert store.get("j3").queue_position < store.get("j1").queue_position


def test_set_queue_order_ignores_unknown_ids_and_short_orders(store):
    for i in range(3):
        add(store, f"j{i}", state="paused")
    assert store.set_queue_order(["nobody", "also-nobody"]) == 0
    assert store.set_queue_order(["j2"]) == 0
    assert store.set_queue_order(["j2", "nobody", "j0"]) == 2
    assert [r.job_id for r in store.unfinished()] == ["j2", "j1", "j0"]


def test_a_version_1_database_gains_queue_positions_in_row_order(tmp_path):
    """The M2 database shipped without queue_position; opening it must migrate, not fail."""
    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(history._SCHEMA)
        conn.execute("PRAGMA user_version=1")
        for i in range(3):
            conn.execute(
                "INSERT INTO jobs (job_id, url, engine, state, created_at, updated_at)"
                " VALUES (?, 'https://x/y', 'ytdlp', 'paused', 1.0, 1.0)",
                (f"old{i}",),
            )
        conn.commit()
    finally:
        conn.close()
    with history.Store(path) as store:
        assert [r.job_id for r in store.unfinished()] == ["old0", "old1", "old2"]
        assert store.set_queue_order(["old2", "old0"]) == 2
        assert [r.job_id for r in store.unfinished()] == ["old2", "old1", "old0"]
