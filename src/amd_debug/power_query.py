# SPDX-License-Identifier: MIT
"""Windowed reads, SI conversion, derived power and plotting of stored samples.

Average power for an interval is derived at query time as ``ΔE/Δt``; that is
the only meaningful quantity for an accumulating sensor sampled at a finite
cadence.  ``plotly`` is only needed by the figure builders and is imported
lazily.
"""

from __future__ import annotations

import fnmatch
import sqlite3
from typing import Optional, Sequence

import pandas as pd

from amd_debug.power_rails import energy_to_joules

WH_PER_J = 1.0 / 3600.0
# Gaps larger than this many median sample intervals reset energy deltas
GAP_FACTOR = 10.0
DEFAULT_INTERVAL_S = 5.0


def list_rails(conn: sqlite3.Connection) -> pd.DataFrame:
    """All rails known to the database."""
    return pd.read_sql_query(
        "SELECT id AS rail_id, device, channel, label "
        "FROM rails ORDER BY device, channel",
        conn,
    )


def filter_rails(
    rails: pd.DataFrame, patterns: Optional[Sequence[str]]
) -> pd.DataFrame:
    """Keep rails whose label matches any glob in ``patterns``."""
    if not patterns:
        return rails
    keep = rails["label"].apply(
        lambda lbl: any(fnmatch.fnmatchcase(lbl, p) for p in patterns)
    )
    return rails[keep].reset_index(drop=True)


def load_window(
    conn: sqlite3.Connection,
    start: float,
    end: float,
    rail_patterns: Optional[Sequence[str]] = None,
) -> pd.DataFrame:
    """Long-form DataFrame of raw energy samples joined with rail metadata."""
    rails = filter_rails(list_rails(conn), rail_patterns)
    if rails.empty:
        return pd.DataFrame()

    placeholders = ",".join(["?"] * len(rails))
    samples = pd.read_sql_query(
        f"""
        SELECT rail_id, ts, energy_raw, energy_scale
          FROM samples
         WHERE ts BETWEEN ? AND ?
           AND rail_id IN ({placeholders})
         ORDER BY ts
        """,
        conn,
        params=[start, end, *rails["rail_id"].tolist()],
    )
    if samples.empty:
        return samples

    df = samples.merge(rails, on="rail_id", how="left")
    # Labels aren't unique across devices; keep one series per rail
    shared = rails["label"].duplicated(keep=False)
    unique = (
        rails["label"]
        + " ("
        + rails["device"]
        + ":"
        + rails["channel"].astype(str)
        + ")"
    )
    names = dict(zip(rails["rail_id"], rails["label"].where(~shared, unique)))
    df["label"] = df["rail_id"].map(names)
    df["time"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    return df


def to_si(df: pd.DataFrame) -> pd.DataFrame:
    """Apply the per-rail scale (IIO milli-units) to produce ``energy_j``."""
    if df.empty:
        return df
    out = df.copy()
    out["energy_j"] = energy_to_joules(out["energy_raw"], out["energy_scale"])
    return out


def derive_power(df: pd.DataFrame) -> pd.DataFrame:
    """Add ``energy_j_delta``, ``energy_j_cum`` and ``power_w`` per rail.

    The first sample of each rail (or any sample after a gap of more than
    ``GAP_FACTOR`` median intervals) yields NaN power and zero energy delta, so
    cumulative traces don't jump across capture-service restarts.  ``dt_s`` is
    the interval length behind each delta (zero for those samples).  Frames
    that were already derived are returned unchanged.
    """
    if df.empty or "energy_j" not in df.columns or "power_w" in df.columns:
        return df
    pieces = []
    for _, grp in df.groupby("label", sort=False):
        g = grp.sort_values("time").copy()
        dt = g["ts"].diff()
        d_energy = g["energy_j"].diff()
        median_dt = DEFAULT_INTERVAL_S
        if dt.dropna().size:
            median_dt = dt.dropna().median() or DEFAULT_INTERVAL_S
        gap = dt > (GAP_FACTOR * median_dt)
        d_energy = d_energy.where(~gap).clip(lower=0.0)
        power_w = (d_energy / dt).where(~gap)
        g["dt_s"] = dt.where(~gap).fillna(0.0)
        g["energy_j_delta"] = d_energy.fillna(0.0)
        g["energy_j_cum"] = g["energy_j_delta"].cumsum()
        g["power_w"] = power_w
        pieces.append(g)
    return pd.concat(pieces, ignore_index=True)


def resample(df: pd.DataFrame, freq: Optional[str]) -> pd.DataFrame:
    """Resample per rail; ``freq=None`` returns ``df`` unchanged.

    Power is derived from the original timestamps first.  Each bin then sums
    the interval energies and elapsed times that fall in it, so energy is
    preserved and power is time-weighted.
    """
    if not freq or df.empty:
        return df
    df = derive_power(df)
    pieces = []
    for label, grp in df.groupby("label", sort=False):
        g = grp.set_index("time").assign(n=1)[["energy_j_delta", "dt_s", "n"]]
        g = g.resample(freq).sum()
        g = g[g["n"] > 0].drop(columns="n")
        g["energy_j_cum"] = g["energy_j_delta"].cumsum()
        g["power_w"] = (g["energy_j_delta"] / g["dt_s"]).where(g["dt_s"] > 0)
        g["label"] = label
        g["device"] = grp["device"].iloc[0]
        g = g.reset_index()
        g["ts"] = g["time"].astype("int64") / 1e9
        pieces.append(g)
    return pd.concat(pieces, ignore_index=True)


def _go():
    """Import plotly lazily so that capture works without it."""
    import plotly.graph_objects as go  # pylint: disable=import-outside-toplevel

    return go


def _per_rail_traces(df: pd.DataFrame, y_col: str) -> list:
    go = _go()
    traces = []
    for label, grp in df.groupby("label", sort=True):
        traces.append(
            go.Scatter(
                x=grp["time"],
                y=grp[y_col],
                mode="lines",
                name=label,
                hovertemplate="%{x}<br>%{y:.4f}<extra>" + label + "</extra>",
            )
        )
    return traces


def power_figure(df: pd.DataFrame):
    """Per-rail average power."""
    df = derive_power(df)
    fig = _go().Figure(_per_rail_traces(df, "power_w"))
    fig.update_layout(
        title="Per-rail average power (ΔE/Δt)",
        xaxis_title="Time (UTC)",
        yaxis_title="Power (W)",
        hovermode="x unified",
    )
    return fig


def cumulative_energy_figure(df: pd.DataFrame, unit: str = "Wh"):
    """Per-rail cumulative energy in ``Wh`` or ``J``."""
    df = derive_power(df)
    if unit == "Wh":
        df = df.assign(energy_plot=df["energy_j_cum"] * WH_PER_J)
    elif unit == "J":
        df = df.assign(energy_plot=df["energy_j_cum"])
    else:
        raise ValueError(f"unknown energy unit: {unit!r}")
    fig = _go().Figure(_per_rail_traces(df, "energy_plot"))
    fig.update_layout(
        title="Per-rail cumulative energy",
        xaxis_title="Time (UTC)",
        yaxis_title=f"Cumulative energy ({unit})",
        hovermode="x unified",
    )
    return fig


def aggregate_power_figure(df: pd.DataFrame):
    """Total power plus per-chip subtotals."""
    go = _go()
    df = derive_power(df)
    if df.empty:
        return go.Figure()
    valid = df.dropna(subset=["power_w"])
    by_device = valid.groupby(["time", "device"])["power_w"].sum().reset_index()
    total = valid.groupby("time")["power_w"].sum().reset_index()

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=total["time"],
            y=total["power_w"],
            mode="lines",
            name="TOTAL",
            line={"width": 3},
        )
    )
    for device, grp in by_device.groupby("device", sort=True):
        fig.add_trace(
            go.Scatter(
                x=grp["time"],
                y=grp["power_w"],
                mode="lines",
                name=device,
                line={"dash": "dot"},
            )
        )
    fig.update_layout(
        title="System average power (total and per-chip subtotals)",
        xaxis_title="Time (UTC)",
        yaxis_title="Power (W)",
        hovermode="x unified",
    )
    return fig


METRIC_BUILDERS = {
    "power": ("Average power (ΔE/Δt)", power_figure),
    "energy": ("Cumulative energy", cumulative_energy_figure),
    "aggregate": ("Aggregate power", aggregate_power_figure),
}
