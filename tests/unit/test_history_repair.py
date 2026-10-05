"""A damaged history database is rebuilt instead of crashing the app (a freelist like the one
an owner's database had: "Freelist: size is 0 but should be N")."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from stuff_downloader.core import history


def _store_with_free_pages(path):
    with history.Store(path) as store:
        for i in range(400):
            store.add_job(
                f"job{i}",
                "https://www.youtube.com/watch?v=x",
                "ytdlp",
                {"n": "x" * 400},
                "C:/out",
                title=f"Song {i}",
            )
        store.set_state("job0", "completed")
        store.forget_many(f"job{i}" for i in range(1, 400))  # leaves free pages behind


def _break_freelist(path):
    data = bytearray(path.read_bytes())
    data[36:40] = (0).to_bytes(4, "big")  # header: number of freelist pages
    path.write_bytes(bytes(data))


@pytest.fixture
def damaged(tmp_path):
    path = tmp_path / "history.sqlite3"
    _store_with_free_pages(path)
    _break_freelist(path)
    conn = sqlite3.connect(path)
    check = conn.execute("PRAGMA quick_check").fetchall()
    conn.close()
    assert check != [("ok",)]
    return path


def test_a_damaged_database_is_rebuilt_on_open_and_keeps_its_rows(damaged, caplog):
    caplog.set_level(logging.WARNING)
    with history.Store(damaged) as store:
        assert store._healthy()
        assert store.get("job0").state == "completed"
        store.add_group("g", "List", "https://example.com/", 1)
        store.set_state("job0", "failed")
        assert store.get("job0").state == "failed"
    assert "rebuilding" in caplog.text


def test_a_write_that_meets_damage_mid_session_is_repaired_and_retried(tmp_path):
    path = tmp_path / "history.sqlite3"
    _store_with_free_pages(path)
    store = history.Store(path)
    calls = []
    real_repair = store._repair

    def repair():
        calls.append(1)
        return real_repair()

    store._repair = repair
    real_conn = store._conn

    class Flaky:
        """The first statement fails the way a damaged file does; then the real connection."""

        failed = False

        def __getattr__(self, name):
            return getattr(real_conn, name)

        def execute(self, *args):
            if not Flaky.failed:
                Flaky.failed = True
                raise sqlite3.DatabaseError("database disk image is malformed")
            return real_conn.execute(*args)

    store._conn = Flaky()
    store.set_state("job0", "completed")
    store._conn = real_conn
    assert calls and store.get("job0").state == "completed"
    store.close()


def test_a_write_that_keeps_failing_is_dropped_not_raised(tmp_path):
    store = history.Store(tmp_path / "history.sqlite3")
    store._repair = lambda: False
    real_conn = store._conn

    class Broken:
        def __getattr__(self, name):
            return getattr(real_conn, name)

        def execute(self, *args):
            raise sqlite3.DatabaseError("database disk image is malformed")

    store._conn = Broken()
    store.set_state("missing", "completed")  # no exception
    assert store.restore_unfinished() == 0
    store._conn = real_conn
    store.close()


def test_an_unrepairable_database_is_set_aside_not_deleted(tmp_path):
    path = tmp_path / "history.sqlite3"
    path.write_bytes(b"SQLite format 3\x00" + b"\xff" * 4080)
    with history.Store(path) as store:
        store.add_group("g", "List", "https://example.com/", 1)
    kept = list(tmp_path.glob("history.sqlite3.damaged-*"))
    assert len(kept) == 1 and kept[0].read_bytes().startswith(b"SQLite format 3")
