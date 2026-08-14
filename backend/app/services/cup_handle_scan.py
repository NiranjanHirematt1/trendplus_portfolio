"""
cup_handle_scan.py
──────────────────────────────────────────────────────────────────────────
The Cup & Handle scan — runs after each bhav load (see
scripts/run_engine_cli.py) over EVERY active symbol, on both daily and
weekly candles, and precomputes one latest pattern per (symbol, timeframe)
into cup_handle_pattern. The screener + detail-panel APIs then read straight
from that table, so serving the whole ~2,000-name universe is a plain
indexed query.

For each symbol:
  • load ~1 year of daily OHLCV from price_history (ascending)
  • run the daily detector (app.services.cup_handle.detect_cup_handle)
  • resample to weekly candles and run the weekly detector
  • upsert a row per timeframe where a valid pattern exists; delete the row
    when a previously-detected pattern has dissolved

No look-ahead: the detector only ever sees candles up to the latest loaded
session. Re-running the same day simply overwrites with identical numbers.
"""
from __future__ import annotations

import datetime
import json
import logging
from typing import Any

import pandas as pd

from app.services.cup_handle import (
    DAILY_CONFIG,
    WEEKLY_CONFIG,
    detect_cup_handle,
    resample_weekly,
)

logger = logging.getLogger(__name__)

# ~1 trading year — the most history this dataset holds, and enough to build
# a weekly cup (which needs many months) from the daily candles.
DAILY_SESSIONS = 260
FETCH_BATCH = 400          # symbols per price_history fetch
UPSERT_BATCH = 500         # rows per executemany

# Columns carried into meta for the UI (everything the detector returns that
# isn't already a first-class column).
_META_KEYS = (
    "left_rim_price", "right_rim_price", "cup_bottom_price",
    "symmetry_pct", "roundness", "right_rim_recovery_pct",
    "breakout_level", "volume_confirmed",
)


# ── Data access ─────────────────────────────────────────────────────────────

async def fetch_active_symbols(conn) -> list[str]:
    rows = await conn.fetch(
        "select symbol from symbols where is_active = true order by symbol"
    )
    return [r["symbol"] for r in rows]


async def fetch_daily_ohlc(conn, symbols: list[str], sessions: int = DAILY_SESSIONS
                           ) -> dict[str, list[dict[str, Any]]]:
    """~1 year of OHLCV per symbol, oldest first (ascending) so the detector
    and the weekly resampler see the series in time order."""
    if not symbols:
        return {}
    rows = await conn.fetch(
        """
        with ranked as (
            select symbol, trade_date, open_price, high_price, low_price,
                   close_price, volume,
                   row_number() over (partition by symbol order by trade_date desc) as rn
            from price_history
            where symbol = any($1::text[])
        )
        select symbol, trade_date, open_price, high_price, low_price, close_price, volume
        from ranked where rn <= $2
        order by symbol, trade_date asc
        """,
        symbols, sessions,
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append(dict(r))
    return out


def _to_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Ascending daily OHLCV rows → DatetimeIndex frame with lower-case cols."""
    df = pd.DataFrame(rows)
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.set_index("trade_date").rename(columns={
        "open_price": "open", "high_price": "high",
        "low_price": "low", "close_price": "close",
    })
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.sort_index()


# ── Row shaping ─────────────────────────────────────────────────────────────

def _pattern_to_row(symbol: str, timeframe: str, res: dict[str, Any]) -> tuple:
    meta = {k: res.get(k) for k in _META_KEYS}
    return (
        symbol,
        timeframe,
        res["stage"],
        res.get("resistance"),
        res.get("cup_depth_pct"),
        res.get("cup_duration"),
        res.get("handle_depth_pct"),
        res.get("handle_duration"),
        bool(res.get("breakout")),
        res.get("volume_ratio"),
        res.get("pattern_score"),
        res.get("last_close"),
        json.dumps(meta, default=str),
    )


# ── Writes ──────────────────────────────────────────────────────────────────

async def _upsert(conn, rows: list[tuple]) -> None:
    if not rows:
        return
    await conn.executemany(
        """
        insert into cup_handle_pattern
            (symbol, timeframe, stage, resistance, cup_depth_pct, cup_duration,
             handle_depth_pct, handle_duration, breakout, volume_ratio,
             pattern_score, last_close, meta, computed_at)
        values ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13::jsonb, now())
        on conflict (symbol, timeframe) do update set
            stage            = excluded.stage,
            resistance       = excluded.resistance,
            cup_depth_pct    = excluded.cup_depth_pct,
            cup_duration     = excluded.cup_duration,
            handle_depth_pct = excluded.handle_depth_pct,
            handle_duration  = excluded.handle_duration,
            breakout         = excluded.breakout,
            volume_ratio     = excluded.volume_ratio,
            pattern_score    = excluded.pattern_score,
            last_close       = excluded.last_close,
            meta             = excluded.meta,
            computed_at      = now()
        """,
        rows,
    )


async def _delete(conn, keys: list[tuple]) -> None:
    if not keys:
        return
    await conn.executemany(
        "delete from cup_handle_pattern where symbol = $1 and timeframe = $2",
        keys,
    )


# ── Orchestration ───────────────────────────────────────────────────────────

async def run_cup_handle_scan(pool, trade_date: datetime.date | None = None) -> dict[str, Any]:
    """Scan every active symbol on daily + weekly candles and refresh
    cup_handle_pattern. Safe to run repeatedly — it overwrites in place."""
    summary = {
        "symbols": 0, "daily": 0, "weekly": 0,
        "cup_forming": 0, "handle_forming": 0, "breakout": 0, "confirmed": 0,
    }

    async with pool.acquire() as conn:
        symbols = await fetch_active_symbols(conn)
    if not symbols:
        logger.info("Cup & Handle scan: no active symbols")
        return summary

    upserts: list[tuple] = []
    deletes: list[tuple] = []

    for start in range(0, len(symbols), FETCH_BATCH):
        batch = symbols[start:start + FETCH_BATCH]
        async with pool.acquire() as conn:
            ohlc = await fetch_daily_ohlc(conn, batch)

        for symbol in batch:
            rows = ohlc.get(symbol)
            if not rows:
                continue
            summary["symbols"] += 1
            try:
                daily = _to_frame(rows)
            except Exception:
                logger.exception("Cup & Handle: bad frame for %s", symbol)
                continue

            frames = [("daily", daily, DAILY_CONFIG),
                      ("weekly", resample_weekly(daily), WEEKLY_CONFIG)]
            for timeframe, df, cfg in frames:
                try:
                    res = detect_cup_handle(df, cfg)
                except Exception:
                    logger.exception("Cup & Handle detect failed: %s (%s)", symbol, timeframe)
                    continue
                if res is None:
                    deletes.append((symbol, timeframe))
                else:
                    upserts.append(_pattern_to_row(symbol, timeframe, res))
                    summary[timeframe] += 1
                    summary[res["stage"]] = summary.get(res["stage"], 0) + 1

    async with pool.acquire() as conn:
        for i in range(0, len(upserts), UPSERT_BATCH):
            await _upsert(conn, upserts[i:i + UPSERT_BATCH])
        for i in range(0, len(deletes), UPSERT_BATCH):
            await _delete(conn, deletes[i:i + UPSERT_BATCH])

    logger.info("Cup & Handle scan: %s", summary)
    return summary
