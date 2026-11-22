# SPDX-License-Identifier: MIT
"""Unit tests for the power_db module."""

import sqlite3
from pathlib import Path

import pytest

from amd_debug import power_db as db
from amd_debug.power_rails import PowerRail


@pytest.fixture
def conn(tmp_path):
    """Initialized database connection."""
    c = db.connect(tmp_path / "sub" / "power.db")
    db.init_schema(c)
    yield c
    c.close()


def make_rail(label="R1", channel=1, scale=2.0, device="iio:device0"):
    """Build a PowerRail"""
    return PowerRail(device, channel, label, Path("/nonexistent"), scale)


def test_default_db_path_env(monkeypatch):
    """$AMD_POWER_DB wins"""
    monkeypatch.setenv("AMD_POWER_DB", "/tmp/x.db")
    assert db.default_db_path() == Path("/tmp/x.db")


def test_default_db_path_xdg(monkeypatch):
    """XDG_DATA_HOME is honored"""
    monkeypatch.delenv("AMD_POWER_DB", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg")
    assert db.default_db_path() == Path("/xdg/amd-debug-tools/power.db")


def test_default_db_path_home(monkeypatch):
    """Fallback to ~/.local/share"""
    monkeypatch.delenv("AMD_POWER_DB", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    path = db.default_db_path()
    assert path.parts[-3:] == ("share", "amd-debug-tools", "power.db")


def test_connect_creates_parent_and_wal(tmp_path):
    """Parent dirs created, WAL on"""
    c = db.connect(tmp_path / "a" / "b.db")
    assert (tmp_path / "a").is_dir()
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()


def test_init_schema_idempotent(conn):
    """Schema can be applied repeatedly"""
    db.init_schema(conn)
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"rails", "samples", "capture_runs"} <= names


def test_upsert_rails_updates_existing(conn):
    """Same device/channel keeps its id but picks up a new label"""
    r1 = make_rail("OLD")
    first = db.upsert_rails(conn, [r1])
    r2 = make_rail("NEW")
    second = db.upsert_rails(conn, [r2])
    assert first[r1] == second[r2]
    row = conn.execute("SELECT device, channel, label FROM rails").fetchone()
    assert row == ("iio:device0", 1, "NEW")


def test_upsert_rails_same_label_different_device(conn):
    """Shared labels on different devices get distinct ids"""
    a = make_rail("SAME", device="iio:device0")
    b = make_rail("SAME", device="iio:device1")
    ids = db.upsert_rails(conn, [a, b])
    assert ids[a] != ids[b]


def test_upsert_rails_rollback(conn):
    """Failure rolls back"""
    bad = make_rail(label=None)
    with pytest.raises(sqlite3.IntegrityError):
        db.upsert_rails(conn, [make_rail("OK"), bad])
    assert conn.execute("SELECT COUNT(*) FROM rails").fetchone()[0] == 0


def test_runs(conn):
    """start/end run bookkeeping"""
    run_id = db.start_run(conn, note="hello")
    assert conn.execute(
        "SELECT note, ended_ts FROM capture_runs WHERE id=?", (run_id,)
    ).fetchone() == ("hello", None)
    db.end_run(conn, run_id)
    assert (
        conn.execute(
            "SELECT ended_ts FROM capture_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        is not None
    )


def test_insert_samples(conn):
    """Samples inserted, duplicates ignored, empty is a no-op"""
    rid = db.upsert_rails(conn, [make_rail()])[make_rail()]
    db.insert_samples(conn, [])
    db.insert_samples(conn, [(rid, 1.0, 10.0, 2.0), (rid, 2.0, 20.0, 2.0)])
    db.insert_samples(conn, [(rid, 2.0, 99.0, 2.0)])
    rows = conn.execute("SELECT ts, energy_raw FROM samples ORDER BY ts").fetchall()
    assert rows == [(1.0, 10.0), (2.0, 20.0)]


def test_insert_samples_rollback(conn):
    """Bad foreign key rolls back whole batch"""
    rid = db.upsert_rails(conn, [make_rail()])[make_rail()]
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_samples(conn, [(rid, 1.0, 1.0, 1.0), (999, 2.0, 2.0, 1.0)])
    assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 0
