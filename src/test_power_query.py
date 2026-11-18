#!/usr/bin/python3
# SPDX-License-Identifier: MIT
"""Unit tests for the power_query module."""

from pathlib import Path

import pandas as pd
import pytest

from amd_debug import power_db as db
from amd_debug import power_query as q
from amd_debug.power_rails import PowerRail


@pytest.fixture
def conn(tmp_path):
    """Database with two rails on two devices, 6 samples each, 5s apart."""
    c = db.connect(tmp_path / "p.db")
    db.init_schema(c)
    a = PowerRail("iio:device0", 1, "CPU_A", Path("/x"), 2.0)
    b = PowerRail("iio:device1", 1, "GPU_B", Path("/x"), 1.0)
    ids = db.upsert_rails(c, [a, b])
    rows = []
    for i in range(6):
        rows.append((ids[a], 100.0 + 5 * i, 10.0 * i, 2.0))
        rows.append((ids[b], 100.0 + 5 * i, 5.0 * i, 1.0))
    db.insert_samples(c, rows)
    yield c
    c.close()


def test_list_and_filter_rails(conn):
    """Listing and glob filtering"""
    rails = q.list_rails(conn)
    assert list(rails["label"]) == ["CPU_A", "GPU_B"]
    assert q.filter_rails(rails, None) is rails
    assert list(q.filter_rails(rails, ["CPU_*"])["label"]) == ["CPU_A"]
    assert q.filter_rails(rails, ["nope"]).empty


def test_load_window(conn):
    """Window selection"""
    df = q.load_window(conn, 100, 110)
    assert len(df) == 6
    assert "time" in df.columns
    assert q.load_window(conn, 1000, 2000).empty
    assert q.load_window(conn, 0, 1000, ["nope"]).empty


def test_to_si(conn):
    """Scaling"""
    assert q.to_si(pd.DataFrame()).empty
    df = q.to_si(q.load_window(conn, 0, 1000, ["CPU_A"]))
    assert list(df["energy_j"]) == pytest.approx([0, 0.02, 0.04, 0.06, 0.08, 0.1])


def test_derive_power(conn):
    """Power is dE/dt, first sample NaN"""
    df = q.derive_power(q.to_si(q.load_window(conn, 0, 1000, ["CPU_A"])))
    assert pd.isna(df["power_w"].iloc[0])
    assert list(df["power_w"].iloc[1:]) == pytest.approx([0.004] * 5)
    assert df["energy_j_cum"].iloc[-1] == pytest.approx(0.1)
    # passthrough cases
    assert q.derive_power(pd.DataFrame()).empty
    raw = q.load_window(conn, 0, 1000)
    assert q.derive_power(raw) is raw


def test_derive_power_gap_and_counter_reset():
    """Gaps reset deltas; a decreasing counter is clipped to zero"""
    df = pd.DataFrame(
        {
            "label": ["X"] * 5,
            "device": ["d"] * 5,
            "ts": [0.0, 5.0, 10.0, 1000.0, 1005.0],
            "energy_j": [0.0, 5.0, 2.0, 500.0, 505.0],
        }
    )
    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    out = q.derive_power(df)
    assert list(out["energy_j_delta"]) == [0.0, 5.0, 0.0, 0.0, 5.0]
    assert pd.isna(out["power_w"].iloc[3])
    assert out["power_w"].iloc[4] == 1.0


def test_derive_power_single_sample():
    """One sample can't have a median interval"""
    df = pd.DataFrame({"label": ["X"], "ts": [1.0], "energy_j": [1.0]})
    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    out = q.derive_power(df)
    assert pd.isna(out["power_w"].iloc[0])


def test_resample(conn):
    """Resampling reduces points per rail"""
    df = q.to_si(q.load_window(conn, 0, 1000))
    assert q.resample(df, None) is df
    out = q.resample(df, "10s")
    assert set(out["label"]) == {"CPU_A", "GPU_B"}
    assert len(out) < len(df)
    assert {"ts", "device", "power_w", "energy_j_cum"} <= set(out.columns)
    empty = pd.DataFrame()
    assert q.resample(empty, "10s") is empty


def test_resample_preserves_energy_and_power():
    """Constant 1 W sampled every 5s keeps 1 W and 20 J when resampled"""
    df = pd.DataFrame(
        {
            "label": ["X"] * 5,
            "device": ["d"] * 5,
            "ts": [0.0, 5.0, 10.0, 15.0, 20.0],
            "energy_j": [0.0, 5.0, 10.0, 15.0, 20.0],
        }
    )
    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    out = q.resample(df, "10s")
    assert out["energy_j_cum"].iloc[-1] == pytest.approx(20.0)
    assert list(out["power_w"].dropna()) == pytest.approx([1.0, 1.0, 1.0])


def test_shared_label_across_devices(tmp_path):
    """Rails on different devices with the same label stay separate"""
    c = db.connect(tmp_path / "p.db")
    db.init_schema(c)
    a = PowerRail("iio:device0", 1, "RAIL", Path("/x"), 1.0)
    b = PowerRail("iio:device1", 1, "RAIL", Path("/x"), 1.0)
    ids = db.upsert_rails(c, [a, b])
    assert ids[a] != ids[b]
    db.insert_samples(c, [(ids[a], 1.0, 1.0, 1.0), (ids[b], 1.0, 2.0, 1.0)])
    df = q.load_window(c, 0, 10)
    assert set(df["label"]) == {"RAIL (iio:device0:1)", "RAIL (iio:device1:1)"}
    c.close()


def test_scale_is_per_sample(tmp_path):
    """A later scale change doesn't alter earlier samples"""
    c = db.connect(tmp_path / "p.db")
    db.init_schema(c)
    r = PowerRail("iio:device0", 1, "R", Path("/x"), 1.0)
    rid = db.upsert_rails(c, [r])[r]
    db.insert_samples(c, [(rid, 1.0, 1000.0, 1.0), (rid, 2.0, 1000.0, 3.0)])
    df = q.to_si(q.load_window(c, 0, 10))
    assert list(df["energy_j"]) == pytest.approx([1.0, 3.0])
    c.close()


class TestFigures:
    """Figure builders (require plotly)"""

    @pytest.fixture(autouse=True)
    def _plotly(self):
        pytest.importorskip("plotly")

    @pytest.fixture
    def df(self, conn):
        """SI dataframe"""
        return q.to_si(q.load_window(conn, 0, 1000))

    def test_power_figure(self, df):
        """One trace per rail"""
        assert len(q.power_figure(df).data) == 2

    def test_energy_figure(self, df):
        """Units"""
        wh = q.cumulative_energy_figure(df, "Wh")
        j = q.cumulative_energy_figure(df, "J")
        assert wh.layout.yaxis.title.text.endswith("(Wh)")
        assert j.data[0].y[-1] == pytest.approx(wh.data[0].y[-1] * 3600)
        with pytest.raises(ValueError):
            q.cumulative_energy_figure(df, "kWh")

    def test_aggregate_figure(self, df):
        """TOTAL plus one per device"""
        fig = q.aggregate_power_figure(df)
        assert [t.name for t in fig.data] == ["TOTAL", "iio:device0", "iio:device1"]
        assert len(q.aggregate_power_figure(pd.DataFrame()).data) == 0

    def test_builders_registry(self):
        """All metrics registered"""
        assert set(q.METRIC_BUILDERS) == {"power", "energy", "aggregate"}
