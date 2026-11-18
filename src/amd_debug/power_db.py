# SPDX-License-Identifier: MIT
"""SQLite storage for per-rail energy-accumulator samples.

Raw accumulator values are stored as read from sysfs together with the scale
in effect when they were read, so a later scale change can't reinterpret old
samples.  Three tables are used:

- ``rails``: one row per discovered rail, identified by ``(device, channel)``;
  labels are display metadata and may be shared between devices
- ``samples``: ``(rail_id, ts, energy_raw, energy_scale)`` readings
- ``capture_runs``: one row per capture service run, used to detect gaps
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

from amd_debug.power_rails import PowerRail

SCHEMA = """
CREATE TABLE IF NOT EXISTS rails (
    id           INTEGER PRIMARY KEY,
    device       TEXT NOT NULL,
    channel      INTEGER NOT NULL,
    label        TEXT NOT NULL,
    UNIQUE (device, channel)
);

CREATE TABLE IF NOT EXISTS samples (
    rail_id     INTEGER NOT NULL REFERENCES rails(id),
    ts          REAL NOT NULL,
    energy_raw  REAL NOT NULL,
    energy_scale REAL NOT NULL,
    PRIMARY KEY (rail_id, ts)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS samples_ts ON samples(ts);

CREATE TABLE IF NOT EXISTS capture_runs (
    id         INTEGER PRIMARY KEY,
    started_ts REAL NOT NULL,
    ended_ts   REAL,
    note       TEXT
);
"""


def default_db_path() -> Path:
    """Default database path.

    ``$AMD_POWER_DB`` if set, otherwise ``$XDG_DATA_HOME/amd-debug-tools/power.db``
    (falling back to ``~/.local/share``).
    """
    env = os.environ.get("AMD_POWER_DB")
    if env:
        return Path(env)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "amd-debug-tools" / "power.db"


def connect(path) -> sqlite3.Connection:
    """Open (creating parent directories) the database in WAL mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.executescript(SCHEMA)


def upsert_rails(
    conn: sqlite3.Connection, rails: Iterable[PowerRail]
) -> Dict[PowerRail, int]:
    """Insert or update rails, returning a rail -> database id mapping."""
    mapping: Dict[PowerRail, int] = {}
    conn.execute("BEGIN")
    try:
        for rail in rails:
            conn.execute(
                """
                INSERT INTO rails (device, channel, label)
                VALUES (?, ?, ?)
                ON CONFLICT(device, channel) DO UPDATE SET
                    label = excluded.label
                """,
                (rail.device, rail.channel, rail.label),
            )
            row = conn.execute(
                "SELECT id FROM rails WHERE device = ? AND channel = ?",
                (rail.device, rail.channel),
            ).fetchone()
            mapping[rail] = int(row[0])
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return mapping


def start_run(conn: sqlite3.Connection, note: Optional[str] = None) -> int:
    """Record the start of a capture run and return its id."""
    cur = conn.execute(
        "INSERT INTO capture_runs (started_ts, note) VALUES (?, ?)",
        (time.time(), note),
    )
    return int(cur.lastrowid)


def end_run(conn: sqlite3.Connection, run_id: int) -> None:
    """Record the end of a capture run."""
    conn.execute(
        "UPDATE capture_runs SET ended_ts = ? WHERE id = ?",
        (time.time(), run_id),
    )


def insert_samples(
    conn: sqlite3.Connection, rows: Sequence[Tuple[int, float, float, float]]
) -> None:
    """Insert ``(rail_id, ts, energy_raw, energy_scale)`` rows; duplicates are ignored."""
    if not rows:
        return
    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO samples (rail_id, ts, energy_raw, energy_scale) "
            "VALUES (?, ?, ?, ?)",
            rows,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
