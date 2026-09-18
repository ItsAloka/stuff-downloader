"""SQLite queue persistence and history (plan §6.1, §6.5). No Qt imports.

The database lives in the app's own data folder, never next to the downloaded files, so the
download folder never reveals what has been downloaded.

Threading: terminal events arrive on a runner reader thread while the GUI writes on the main
thread, so one connection is shared with ``check_same_thread=False`` behind a lock, and every
statement goes through that lock.

Nothing here ever deletes a downloaded file. ``forget`` removes the row only.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

SCHEMA_VERSION = 2

# A job in one of these states was still going when the app stopped, so it comes back paused.
UNFINISHED_STATES = ("queued", "active", "paused")
TERMINAL_STATES = ("completed", "failed", "cancelled", "skipped")

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    group_id   TEXT PRIMARY KEY,
    kind       TEXT NOT NULL DEFAULT 'playlist',
    title      TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    count      INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id         TEXT PRIMARY KEY,
    group_id       TEXT,
    url            TEXT NOT NULL,
    engine         TEXT NOT NULL,
    preset         TEXT NOT NULL DEFAULT '',
    options_json   TEXT NOT NULL DEFAULT '{}',
    output_dir     TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    playlist_index INTEGER,
    state          TEXT NOT NULL DEFAULT 'queued',
    error_code     TEXT NOT NULL DEFAULT '',
    error_message  TEXT NOT NULL DEFAULT '',
    total_bytes    INTEGER NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL,
    updated_at     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS job_files (
    job_id TEXT NOT NULL,
    path   TEXT NOT NULL,
    PRIMARY KEY (job_id, path)
);
CREATE INDEX IF NOT EXISTS jobs_state_idx ON jobs (state, updated_at DESC);
CREATE INDEX IF NOT EXISTS jobs_group_idx ON jobs (group_id);
"""


@dataclass
class JobRecord:
    job_id: str
    url: str
    engine: str
    state: str
    title: str = ""
    preset: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    output_dir: str = ""
    group_id: str = ""
    playlist_index: int | None = None
    queue_position: int = 0
    error_code: str = ""
    error_message: str = ""
    total_bytes: int = 0
    files: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0


def database_path() -> Path:
    return paths.data_dir() / "history.sqlite3"


def _escape_like(term: str) -> str:
    for ch in ("\\", "%", "_"):
        term = term.replace(ch, "\\" + ch)
    return term


class Store:
    """The queue and history database. Close it with ``close()`` or use it as a context manager."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path is not None else database_path()
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()

    # ── lifecycle ────────────────────────────────────────────────────────────────────────
    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"history database is version {version}, newer than this app ({SCHEMA_VERSION})"
            )
        if version < 1:
            self._conn.executescript(_SCHEMA)
        if version < 2:
            # Queue order the owner set by hand. Kept out of _SCHEMA so an existing v1 database
            # takes the same path a fresh one does.
            columns = {r["name"] for r in self._conn.execute("PRAGMA table_info(jobs)")}
            if "queue_position" not in columns:
                self._conn.execute(
                    "ALTER TABLE jobs ADD COLUMN queue_position INTEGER NOT NULL DEFAULT 0"
                )
                # Existing rows keep the order they already had.
                self._conn.execute("UPDATE jobs SET queue_position = rowid")
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── writes ───────────────────────────────────────────────────────────────────────────
    def add_group(self, group_id: str, title: str, source_url: str, count: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO groups (group_id, kind, title, source_url, count,"
                " created_at) VALUES (?, 'playlist', ?, ?, ?, ?)",
                (group_id, title, source_url, count, time.time()),
            )
            self._conn.commit()

    def add_job(
        self,
        job_id: str,
        url: str,
        engine: str,
        options: dict[str, Any],
        output_dir: str,
        title: str = "",
        group_id: str = "",
        state: str = "queued",
    ) -> None:
        now = time.time()
        with self._lock:
            position = self._conn.execute(
                "SELECT COALESCE(MAX(queue_position), 0) + 1 FROM jobs"
            ).fetchone()[0]
            self._conn.execute(
                "INSERT OR REPLACE INTO jobs (job_id, group_id, url, engine, preset,"
                " options_json, output_dir, title, playlist_index, state, queue_position,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    job_id,
                    group_id or None,
                    url,
                    engine,
                    str(options.get("preset") or ""),
                    json.dumps(options),
                    output_dir,
                    title,
                    options.get("playlist_index"),
                    state,
                    position,
                    now,
                    now,
                ),
            )
            self._conn.commit()

    def set_state(
        self,
        job_id: str,
        state: str,
        error_code: str = "",
        error_message: str = "",
        total_bytes: int = 0,
        files: Sequence[str] = (),
        title: str = "",
    ) -> None:
        """Record a state transition. Progress is deliberately not persisted."""
        with self._lock:
            fields = ["state = ?", "updated_at = ?"]
            values: list[Any] = [state, time.time()]
            if error_code or error_message:
                fields += ["error_code = ?", "error_message = ?"]
                values += [error_code, error_message[:500]]
            if total_bytes:
                fields.append("total_bytes = ?")
                values.append(int(total_bytes))
            if title:
                fields.append("title = ?")
                values.append(title)
            values.append(job_id)
            self._conn.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE job_id = ?", values)
            if files:
                self._conn.executemany(
                    "INSERT OR REPLACE INTO job_files (job_id, path) VALUES (?, ?)",
                    [(job_id, str(f)) for f in files],
                )
            self._conn.commit()

    def restore_unfinished(self) -> int:
        """Mark everything that was still running as paused. Nothing is restarted here."""
        with self._lock:
            placeholders = ", ".join("?" for _ in UNFINISHED_STATES)
            cursor = self._conn.execute(
                f"UPDATE jobs SET state = 'paused', updated_at = ? WHERE state IN"
                f" ({placeholders}) AND state != 'paused'",
                (time.time(), *UNFINISHED_STATES),
            )
            self._conn.commit()
            return cursor.rowcount

    def set_queue_order(self, job_ids: Sequence[str]) -> int:
        """Persist a hand-set queue order.

        Only the positions already held by the named jobs are reused, so jobs the caller did not
        name keep their place relative to the ones it did. Unknown ids are ignored.
        """
        with self._lock:
            known = {
                row["job_id"]: row["queue_position"]
                for row in self._conn.execute("SELECT job_id, queue_position FROM jobs")
            }
            named = [job_id for job_id in job_ids if job_id in known]
            if len(named) < 2:
                return 0
            slots = sorted(known[job_id] for job_id in named)
            now = time.time()
            self._conn.executemany(
                "UPDATE jobs SET queue_position = ?, updated_at = ? WHERE job_id = ?",
                [(slot, now, job_id) for slot, job_id in zip(slots, named, strict=True)],
            )
            self._conn.commit()
            return len(named)

    def forget(self, job_id: str) -> None:
        """Remove the history row. The downloaded file is never touched."""
        with self._lock:
            self._conn.execute("DELETE FROM job_files WHERE job_id = ?", (job_id,))
            self._conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            self._conn.commit()

    # ── reads ────────────────────────────────────────────────────────────────────────────
    def _rows_to_records(self, rows: Iterable[sqlite3.Row]) -> list[JobRecord]:
        records = []
        for row in rows:
            try:
                options = json.loads(row["options_json"])
            except ValueError:
                options = {}
            records.append(
                JobRecord(
                    job_id=row["job_id"],
                    url=row["url"],
                    engine=row["engine"],
                    state=row["state"],
                    title=row["title"],
                    preset=row["preset"],
                    options=options if isinstance(options, dict) else {},
                    output_dir=row["output_dir"],
                    group_id=row["group_id"] or "",
                    playlist_index=row["playlist_index"],
                    queue_position=row["queue_position"],
                    error_code=row["error_code"],
                    error_message=row["error_message"],
                    total_bytes=row["total_bytes"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
            )
        with self._lock:
            for record in records:
                record.files = [
                    r["path"]
                    for r in self._conn.execute(
                        "SELECT path FROM job_files WHERE job_id = ?", (record.job_id,)
                    )
                ]
        return records

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchall()
        records = self._rows_to_records(rows)
        return records[0] if records else None

    def unfinished(self) -> list[JobRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE state = 'paused' ORDER BY queue_position, created_at"
            ).fetchall()
        return self._rows_to_records(rows)

    def search(self, term: str = "", limit: int = 200) -> list[JobRecord]:
        """Finished jobs, newest first. ``term`` matches the title or the source URL."""
        placeholders = ", ".join("?" for _ in TERMINAL_STATES)
        sql = f"SELECT * FROM jobs WHERE state IN ({placeholders})"
        values: list[Any] = list(TERMINAL_STATES)
        term = term.strip()
        if term:
            pattern = f"%{_escape_like(term)}%"
            sql += " AND (title LIKE ? ESCAPE '\\' OR url LIKE ? ESCAPE '\\')"
            values += [pattern, pattern]
        # rowid breaks ties: two jobs that finish within the same clock tick must still come
        # back in a stable, newest-first order.
        sql += " ORDER BY updated_at DESC, rowid DESC LIMIT ?"
        values.append(max(1, min(1000, limit)))
        with self._lock:
            rows = self._conn.execute(sql, values).fetchall()
        return self._rows_to_records(rows)
