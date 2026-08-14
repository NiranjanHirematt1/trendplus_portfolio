"""
cup_handle.py
──────────────────────────────────────────────────────────────────────────
Rules-based Cup & Handle engine (no ML), shared by the daily/weekly scan,
the screener API and the stock detail panel.

`detect_cup_handle(df, cfg)` runs over one timeframe's OHLCV (daily or
weekly, sorted ascending) and returns a rich, fully-quantified pattern or
None. Everything is driven by a `CupHandleConfig` so the same core engine
serves both timeframes with different tunables — nothing about the geometry
is hard-coded to a price level, so it works uniformly across every NSE name.

Design notes
------------
• price_history.open_price on this dataset is a proxy for close (the NSE
  bhav copy carries no true open), so the engine reads only close/high/low
  and volume — never open.
• No look-ahead: the engine only ever indexes candles up to the last row of
  the frame it is given. The live scan passes candles up to the latest
  session, so a "breakout" can only be called on data that already existed.

Stages (the stable contract the scan + API + UI read):
  cup_forming    — a valid rounded base has formed; the right side has not
                   yet produced a clean handle (still recovering, or the
                   pullback isn't handle-shaped yet).
  handle_forming — cup complete (right rim recovered near the lip) and a
                   shallow pullback (the handle) is in progress below
                   resistance.
  breakout       — price has just closed above the cup resistance (+buffer).
  confirmed      — the breakout has held above resistance for a few bars.

`resample_weekly(df)` folds a daily OHLCV frame into weekly (W-FRI) candles
so the same engine can run on both timeframes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.signal import argrelextrema

# ── Stage constants ────────────────────────────────────────────────────────
STAGE_CUP = "cup_forming"
STAGE_HANDLE = "handle_forming"
STAGE_BREAKOUT = "breakout"
STAGE_CONFIRMED = "confirmed"


@dataclass(frozen=True)
class CupHandleConfig:
    """Every threshold the geometry depends on. Percentages, never prices, so
    the same numbers hold for a ₹15 stock and a ₹40,000 one."""

    # ── Pivot / window ──────────────────────────────────────────────────
    extrema_order: int          # a pivot must beat this many bars each side
    min_bars: int               # fewer candles than this → don't guess
    window_bars: int            # only the most recent N bars are considered
    left_search_frac: float     # left rim is sought in the first X of the window

    # ── Cup ─────────────────────────────────────────────────────────────
    cup_min_depth_pct: float    # shallower than this isn't a cup
    cup_max_depth_pct: float    # deeper than this is a crash, not a cup
    min_cup_bars: int           # left-rim → right-side span floor
    max_cup_bars: int           # …and ceiling
    rim_symmetry_pct: float     # left vs right rim may differ by at most this
    bottom_center_tol: float    # bottom within ±X of the rim-to-rim midpoint
    u_base_zone: float          # "the base" = lowest X of the cup's depth
    u_min_base_ratio: float     # ≥X of cup bars in the base → rounded (U), not V
    cup_complete_recovery_pct: float  # right side must reach X% of the lip to
                                      # count the cup as complete (handle-ready)

    # ── Handle ──────────────────────────────────────────────────────────
    handle_max_depth_pct: float     # a handle deeper than this is a new leg down
    handle_max_cup_ratio: float     # handle depth < this fraction of cup depth
    handle_upper_half: float        # handle low stays above this fraction of cup
    min_handle_bars: int            # need at least this many bars past the rim
    max_handle_bars: int            # …and no more than this

    # ── Breakout / volume ───────────────────────────────────────────────
    breakout_buffer_pct: float      # close must clear resistance by this much
    confirm_bars: int               # bars held above resistance → "confirmed"
    fresh_breakout_bars: int         # breakout no older than this → "breakout"
    vol_lookback: int               # bars used for the average-volume baseline
    volume_ratio_min: float         # breakout vol ≥ this × avg → confirmed vol

    # ── Score weights (need not sum to 1; normalised internally) ─────────
    w_depth: float = 1.0
    w_duration: float = 1.0
    w_symmetry: float = 1.0
    w_roundness: float = 1.0
    w_recovery: float = 1.0
    w_handle: float = 1.0
    w_volume: float = 1.0
    w_breakout: float = 1.5


# ── Timeframe presets ───────────────────────────────────────────────────────
# Daily: ~1 year of sessions available; a cup runs weeks to a few months.
DAILY_CONFIG = CupHandleConfig(
    extrema_order=3,
    min_bars=45,
    window_bars=170,
    left_search_frac=0.6,
    cup_min_depth_pct=10.0,
    cup_max_depth_pct=50.0,
    min_cup_bars=15,
    max_cup_bars=150,
    rim_symmetry_pct=10.0,
    bottom_center_tol=0.42,
    u_base_zone=0.35,
    u_min_base_ratio=0.42,
    cup_complete_recovery_pct=88.0,
    handle_max_depth_pct=16.0,
    handle_max_cup_ratio=0.55,
    handle_upper_half=0.45,
    min_handle_bars=3,
    max_handle_bars=35,
    breakout_buffer_pct=0.5,
    confirm_bars=3,
    fresh_breakout_bars=3,
    vol_lookback=50,
    volume_ratio_min=1.4,
)

# Weekly: only ~40–52 candles exist, so every span is scaled down.
WEEKLY_CONFIG = CupHandleConfig(
    extrema_order=2,
    min_bars=20,
    window_bars=80,
    left_search_frac=0.6,
    cup_min_depth_pct=12.0,
    cup_max_depth_pct=55.0,
    min_cup_bars=6,
    max_cup_bars=45,
    rim_symmetry_pct=12.0,
    bottom_center_tol=0.45,
    u_base_zone=0.35,
    u_min_base_ratio=0.38,
    cup_complete_recovery_pct=86.0,
    handle_max_depth_pct=18.0,
    handle_max_cup_ratio=0.60,
    handle_upper_half=0.42,
    min_handle_bars=1,
    max_handle_bars=10,
    breakout_buffer_pct=0.5,
    confirm_bars=2,
    fresh_breakout_bars=2,
    vol_lookback=12,
    volume_ratio_min=1.3,
)


# ── Small numeric helpers ───────────────────────────────────────────────────

def _series(df: pd.DataFrame, col: str) -> Optional[np.ndarray]:
    if df is None or col not in df.columns:
        return None
    return pd.to_numeric(df[col], errors="coerce").to_numpy(dtype=float)


def _band_score(x: float, lo: float, ideal_lo: float,
                ideal_hi: float, hi: float) -> float:
    """1.0 inside [ideal_lo, ideal_hi]; linearly ramps to 0 at lo and hi;
    0 beyond [lo, hi]. A trapezoid centred on the ideal band."""
    if x is None or np.isnan(x):
        return 0.0
    if x <= lo or x >= hi:
        return 0.0
    if ideal_lo <= x <= ideal_hi:
        return 1.0
    if x < ideal_lo:
        return (x - lo) / max(ideal_lo - lo, 1e-9)
    return (hi - x) / max(hi - ideal_hi, 1e-9)


def _clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


# ── Core engine ─────────────────────────────────────────────────────────────

def detect_cup_handle(df: pd.DataFrame,
                      cfg: CupHandleConfig = DAILY_CONFIG) -> Optional[dict[str, Any]]:
    """Detect a Cup & Handle on one timeframe of OHLCV (ascending).

    Returns a fully-quantified pattern dict (see module docstring for the
    stage vocabulary) or None when no valid rounded base is present.
    """
    closes = _series(df, "close")
    if closes is None:
        return None
    highs = _series(df, "high")
    lows = _series(df, "low")
    vols = _series(df, "volume")

    # Keep only rows with a usable close; carry the other series along.
    mask = ~np.isnan(closes)
    closes = closes[mask]
    if closes.size < cfg.min_bars:
        return None
    highs = highs[mask] if highs is not None else closes.copy()
    lows = lows[mask] if lows is not None else closes.copy()
    vols = vols[mask] if vols is not None else np.full(closes.size, np.nan)
    highs = np.where(np.isnan(highs), closes, highs)
    lows = np.where(np.isnan(lows), closes, lows)

    # Focus on the most recent window — that's where a *current* pattern lives.
    if closes.size > cfg.window_bars:
        off = closes.size - cfg.window_bars
        closes, highs, lows, vols = (a[off:] for a in (closes, highs, lows, vols))
    n = closes.size

    # ── Left rim: the highest pivot in the early part of the window ─────────
    search_end = max(int(n * cfg.left_search_frac), 2)
    maxima = argrelextrema(closes, np.greater_equal, order=cfg.extrema_order)[0]
    left_candidates = [i for i in maxima if i < search_end]
    if left_candidates:
        left_rim_idx = max(left_candidates, key=lambda i: closes[i])
    else:
        left_rim_idx = int(np.argmax(closes[:search_end]))

    left_rim = float(closes[left_rim_idx])
    if left_rim <= 0:
        return None

    # ── Cup bottom: lowest close after the left rim ─────────────────────────
    if left_rim_idx >= n - 2:
        return None
    bottom_rel = int(np.argmin(closes[left_rim_idx + 1:]))
    bottom_idx = left_rim_idx + 1 + bottom_rel
    bottom = float(closes[bottom_idx])
    if bottom <= 0 or bottom_idx >= n - 1:
        return None

    # Resistance is the cup lip (the left rim = the level to break out over).
    resistance = left_rim
    depth_price = resistance - bottom
    cup_depth_pct = depth_price / resistance * 100.0
    if not (cfg.cup_min_depth_pct <= cup_depth_pct <= cfg.cup_max_depth_pct):
        return None

    # ── Right rim: where the right side recovers back near the lip ──────────
    # The cup completes at the FIRST post-bottom pivot that climbs back near
    # resistance — everything after it (handle, breakout) is examined below.
    # If nothing recovers that far, the cup is still forming and the right rim
    # is just the recovery high (which sits below the lip).
    complete_thresh = resistance * cfg.cup_complete_recovery_pct / 100.0
    right_rim_idx = None
    for i in maxima:
        if i > bottom_idx and closes[i] >= complete_thresh:
            right_rim_idx = int(i)
            break
    if right_rim_idx is None:
        right_rim_idx = bottom_idx + 1 + int(np.argmax(closes[bottom_idx + 1:]))
    right_rim = float(closes[right_rim_idx])

    cup_bars = right_rim_idx - left_rim_idx
    if not (cfg.min_cup_bars <= cup_bars <= cfg.max_cup_bars):
        return None

    # ── Rounded (U) base, not a sharp V ─────────────────────────────────────
    cup_slice = closes[left_rim_idx:right_rim_idx + 1]
    base_zone = bottom + cfg.u_base_zone * depth_price
    base_ratio = float(np.count_nonzero(cup_slice <= base_zone)) / max(cup_slice.size, 1)
    if base_ratio < cfg.u_min_base_ratio:
        return None

    # ── Bottom roughly centred between the rims ─────────────────────────────
    center = (left_rim_idx + right_rim_idx) / 2.0
    center_off = abs(bottom_idx - center) / max(cup_bars, 1)
    if center_off > cfg.bottom_center_tol:
        return None

    recovery_pct = right_rim / resistance * 100.0
    symmetry_pct = abs(left_rim - right_rim) / max(left_rim, right_rim) * 100.0

    last_close = float(closes[-1])
    breakout_level = resistance * (1.0 + cfg.breakout_buffer_pct / 100.0)

    # ── Volume baseline (avg over the lookback before the last bar) ─────────
    def _avg_vol(upto: int) -> float:
        lo = max(0, upto - cfg.vol_lookback)
        seg = vols[lo:upto]
        seg = seg[~np.isnan(seg)]
        return float(np.mean(seg)) if seg.size else float("nan")

    # ── Stage resolution ────────────────────────────────────────────────────
    handle_depth_pct: Optional[float] = None
    handle_duration: Optional[int] = None
    breakout = False
    volume_ratio: Optional[float] = None
    volume_confirmed: Optional[bool] = None

    if recovery_pct < cfg.cup_complete_recovery_pct:
        # Right side hasn't climbed back near the lip — the cup is still forming.
        stage = STAGE_CUP
        avg = _avg_vol(n)
        if not np.isnan(avg) and avg > 0 and not np.isnan(vols[-1]):
            volume_ratio = round(vols[-1] / avg, 2)
    else:
        # Cup complete. Examine everything after the right rim for a handle /
        # breakout. Breakout = first bar past the rim closing above the buffer.
        post = np.arange(right_rim_idx + 1, n)
        bk_bars = [j for j in post if closes[j] >= breakout_level]
        handle_seg = closes[right_rim_idx + 1:]
        if handle_seg.size:
            handle_low = float(np.min(handle_seg))
            handle_depth_pct = round((right_rim - handle_low) / right_rim * 100.0, 2)
            handle_duration = int(n - 1 - right_rim_idx)

        if bk_bars and last_close >= resistance:
            first_bk = bk_bars[0]
            bars_since = n - 1 - first_bk
            held = float(np.min(closes[first_bk:])) >= resistance
            stage = (STAGE_CONFIRMED
                     if bars_since >= cfg.confirm_bars and held
                     else STAGE_BREAKOUT)
            breakout = True
            avg = _avg_vol(first_bk)
            if not np.isnan(avg) and avg > 0 and not np.isnan(vols[first_bk]):
                volume_ratio = round(vols[first_bk] / avg, 2)
                volume_confirmed = volume_ratio >= cfg.volume_ratio_min
        else:
            # No (held) breakout — is the pullback a clean handle?
            midpoint = bottom + cfg.handle_upper_half * depth_price
            handle_low_val = (bottom if not handle_seg.size
                              else float(np.min(handle_seg)))
            valid_handle = (
                handle_seg.size >= cfg.min_handle_bars
                and handle_seg.size <= cfg.max_handle_bars
                and handle_depth_pct is not None
                and handle_depth_pct <= cfg.handle_max_depth_pct
                and handle_depth_pct <= cfg.handle_max_cup_ratio * cup_depth_pct
                and handle_low_val >= midpoint
            )
            stage = STAGE_HANDLE if valid_handle else STAGE_CUP
            avg = _avg_vol(n)
            if not np.isnan(avg) and avg > 0 and not np.isnan(vols[-1]):
                volume_ratio = round(vols[-1] / avg, 2)

    score = _pattern_score(
        cfg, stage=stage, cup_depth_pct=cup_depth_pct, cup_bars=cup_bars,
        symmetry_pct=symmetry_pct, center_off=center_off, base_ratio=base_ratio,
        recovery_pct=recovery_pct, handle_depth_pct=handle_depth_pct,
        volume_ratio=volume_ratio, volume_confirmed=volume_confirmed,
    )

    return {
        "stage": stage,
        "resistance": round(resistance, 2),
        "breakout_level": round(breakout_level, 2),
        "cup_depth_pct": round(cup_depth_pct, 2),
        "cup_duration": int(cup_bars),
        "left_rim_price": round(left_rim, 2),
        "right_rim_price": round(right_rim, 2),
        "cup_bottom_price": round(bottom, 2),
        "symmetry_pct": round(symmetry_pct, 2),
        "roundness": round(base_ratio, 2),
        "right_rim_recovery_pct": round(recovery_pct, 2),
        "handle_depth_pct": handle_depth_pct,
        "handle_duration": handle_duration,
        "breakout": bool(breakout),
        "volume_ratio": volume_ratio,
        "volume_confirmed": volume_confirmed,
        "pattern_score": score,
        "last_close": round(last_close, 2),
    }


def _pattern_score(cfg: CupHandleConfig, *, stage: str, cup_depth_pct: float,
                   cup_bars: int, symmetry_pct: float, center_off: float,
                   base_ratio: float, recovery_pct: float,
                   handle_depth_pct: Optional[float],
                   volume_ratio: Optional[float],
                   volume_confirmed: Optional[bool]) -> float:
    """Blend the pattern's qualities into a single 0–100 score."""
    # Depth: ideal a moderate 15–35% base.
    depth = _band_score(cup_depth_pct, cfg.cup_min_depth_pct, 15.0, 35.0,
                        cfg.cup_max_depth_pct)
    # Duration: ideal the middle of the allowed span.
    span = cfg.max_cup_bars - cfg.min_cup_bars
    duration = _band_score(cup_bars, cfg.min_cup_bars,
                           cfg.min_cup_bars + span * 0.20,
                           cfg.min_cup_bars + span * 0.70, cfg.max_cup_bars)
    # Symmetry: rims close in price and the bottom centred in time.
    rim_sym = 1.0 - _clamp01(symmetry_pct / max(cfg.rim_symmetry_pct, 1e-9))
    time_sym = 1.0 - _clamp01(center_off / max(cfg.bottom_center_tol, 1e-9))
    symmetry = 0.5 * rim_sym + 0.5 * time_sym
    # Roundness: how broad the base is (U vs V).
    roundness = _clamp01((base_ratio - cfg.u_min_base_ratio) /
                         max(0.75 - cfg.u_min_base_ratio, 1e-9))
    # Recovery: how close the right side came back to the lip.
    recovery = _clamp01((recovery_pct - 60.0) / 40.0)
    # Handle: shallow is best; none yet → neutral.
    if handle_depth_pct is None:
        handle = 0.5
    else:
        handle = _band_score(handle_depth_pct, 0.0, 2.0,
                             cfg.handle_max_depth_pct * 0.5,
                             cfg.handle_max_depth_pct)
    # Volume: reward expansion on breakout / neutral otherwise.
    if volume_confirmed is True:
        volume = 1.0
    elif volume_ratio is None:
        volume = 0.5
    else:
        volume = _clamp01(volume_ratio / max(cfg.volume_ratio_min, 1e-9))
    # Breakout progress along the pattern's life.
    breakout = {STAGE_CUP: 0.25, STAGE_HANDLE: 0.5,
                STAGE_BREAKOUT: 0.85, STAGE_CONFIRMED: 1.0}.get(stage, 0.25)

    parts = [
        (cfg.w_depth, depth), (cfg.w_duration, duration),
        (cfg.w_symmetry, symmetry), (cfg.w_roundness, roundness),
        (cfg.w_recovery, recovery), (cfg.w_handle, handle),
        (cfg.w_volume, volume), (cfg.w_breakout, breakout),
    ]
    total_w = sum(w for w, _ in parts)
    raw = sum(w * v for w, v in parts) / max(total_w, 1e-9)
    return round(raw * 100.0, 1)


def resample_weekly(df: pd.DataFrame) -> pd.DataFrame:
    """Fold a daily OHLCV frame (DatetimeIndex, ascending) into weekly candles
    ending Friday. Weeks with no close are dropped."""
    weekly = df.resample("W-FRI").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    return weekly.dropna(subset=["close"])
