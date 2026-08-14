"""Unit tests for the rules-based Cup & Handle engine (pure geometry).

Synthetic OHLCV series are built from a close curve — the engine reads
close/high/low/volume (never open, which is a proxy on this dataset), so we
carry a realistic frame the weekly resampler can also run on.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.cup_handle import (
    DAILY_CONFIG,
    STAGE_BREAKOUT,
    STAGE_CONFIRMED,
    STAGE_CUP,
    STAGE_HANDLE,
    detect_cup_handle,
    resample_weekly,
)


def df_from_closes(closes, volumes=None, start="2024-06-03"):
    """Wrap a close curve into an ascending daily OHLCV frame."""
    closes = [float(c) for c in closes]
    idx = pd.bdate_range(start=start, periods=len(closes))
    prev = [closes[0]] + closes[:-1]
    vols = volumes if volumes is not None else [100_000] * len(closes)
    return pd.DataFrame(
        {
            "open": prev,
            "high": [max(c, p) * 1.001 for c, p in zip(closes, prev)],
            "low": [min(c, p) * 0.999 for c, p in zip(closes, prev)],
            "close": closes,
            "volume": vols,
        },
        index=idx,
    )


def cup_closes(rim=100.0, depth_pct=25.0, width=40, lead=10,
               handle_bars=8, handle_depth_pct=6.0, tail=None):
    """A rounded (U) cup with a shallow handle, optionally with a tail leg."""
    depth = rim * depth_pct / 100.0
    bottom = rim - depth
    lead_vals = list(np.linspace(bottom + depth * 0.15, rim, lead, endpoint=False))
    t = np.linspace(0.0, 1.0, width)
    cup = rim - depth * np.sin(np.pi * t)                 # rim → bottom → rim
    hd = rim * handle_depth_pct / 100.0
    handle = rim - hd * np.sin(np.linspace(0.0, np.pi, handle_bars + 2))[1:-1]
    closes = lead_vals + list(cup) + list(handle)
    if tail is not None:
        closes += list(tail)
    return closes


def test_clean_cup_and_handle_forms():
    res = detect_cup_handle(df_from_closes(cup_closes()), DAILY_CONFIG)
    assert res is not None
    assert res["stage"] in (STAGE_HANDLE, STAGE_CUP)
    assert res["breakout"] is False
    assert DAILY_CONFIG.cup_max_depth_pct >= res["cup_depth_pct"] >= DAILY_CONFIG.cup_min_depth_pct
    assert res["resistance"] > 0
    assert 0 <= res["pattern_score"] <= 100


def test_valid_handle_is_recognised():
    res = detect_cup_handle(df_from_closes(cup_closes()), DAILY_CONFIG)
    assert res["stage"] == STAGE_HANDLE
    assert res["handle_depth_pct"] is not None
    assert res["handle_depth_pct"] < DAILY_CONFIG.handle_max_depth_pct


def test_breakout_with_volume_expansion():
    closes = cup_closes(tail=[97, 99, 101, 103.5])
    # Volume expands as price clears resistance (the 101 bar is the breakout).
    vols = [100_000] * (len(closes) - 2) + [400_000, 400_000]
    res = detect_cup_handle(df_from_closes(closes, volumes=vols), DAILY_CONFIG)
    assert res is not None
    assert res["breakout"] is True
    assert res["stage"] in (STAGE_BREAKOUT, STAGE_CONFIRMED)
    assert res["volume_ratio"] is not None and res["volume_ratio"] > 1.0


def test_confirmed_after_holding_above_resistance():
    # Breakout several bars ago, price holds above the lip → confirmed.
    res = detect_cup_handle(
        df_from_closes(cup_closes(tail=[97, 99, 102, 103, 104, 105, 106])),
        DAILY_CONFIG,
    )
    assert res is not None
    assert res["stage"] == STAGE_CONFIRMED
    assert res["breakout"] is True


def test_cup_still_forming_when_right_side_low():
    # Cup that hasn't recovered near the lip yet — still forming, no handle.
    closes = cup_closes(width=40, handle_bars=0, handle_depth_pct=0.0)
    # Chop off the recovery so the right side sits well below the rim.
    closes = closes[: int(len(closes) * 0.72)]
    res = detect_cup_handle(df_from_closes(closes), DAILY_CONFIG)
    if res is not None:
        assert res["stage"] == STAGE_CUP


def test_too_deep_cup_is_rejected():
    assert detect_cup_handle(df_from_closes(cup_closes(depth_pct=60.0)), DAILY_CONFIG) is None


def test_v_shape_is_rejected():
    rim, depth, width, lead = 100.0, 25.0, 40, 10
    d = rim * depth / 100.0
    bottom = rim - d
    t = np.linspace(0.0, 1.0, width)
    v = rim - d * (1.0 - np.abs(2.0 * t - 1.0))          # triangular dip
    lead_vals = list(np.linspace(bottom + d * 0.15, rim, lead, endpoint=False))
    handle = rim - (rim * 0.06) * np.sin(np.linspace(0.0, np.pi, 10))[1:-1]
    closes = lead_vals + list(v) + list(handle)
    assert detect_cup_handle(df_from_closes(closes), DAILY_CONFIG) is None


def test_too_few_bars_returns_none():
    assert detect_cup_handle(df_from_closes([100, 101, 99, 98, 100]), DAILY_CONFIG) is None


def test_flat_line_returns_none():
    assert detect_cup_handle(df_from_closes([100.0] * 60), DAILY_CONFIG) is None


def test_no_lookahead_stage_reflects_only_available_bars():
    # Truncating the breakout leg must NOT report a breakout.
    full = cup_closes(tail=[97, 99, 101, 103.5])
    pre = full[:-2]                                       # drop the breakout bars
    res = detect_cup_handle(df_from_closes(pre), DAILY_CONFIG)
    if res is not None:
        assert res["breakout"] is False


def test_resample_weekly_aggregates_ohlc():
    idx = pd.bdate_range(start="2025-01-06", periods=10)  # Monday start
    df = pd.DataFrame(
        {
            "open": range(10),
            "high": [i + 1 for i in range(10)],
            "low": [i - 1 for i in range(10)],
            "close": range(10),
            "volume": [1] * 10,
        },
        index=idx,
    )
    weekly = resample_weekly(df)
    assert len(weekly) == 2
    first = weekly.iloc[0]
    assert first["open"] == 0
    assert first["high"] == 5
    assert first["low"] == -1
    assert first["close"] == 4
    assert first["volume"] == 5
