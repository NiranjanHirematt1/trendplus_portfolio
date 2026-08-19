#!/usr/bin/env python3
"""
compute_all_dates.py
────────────────────
Reads price_history that's already in Supabase and computes
trend_results + sector_daily for EVERY trading date in the DB.

No bhav download needed — price history is already loaded.

Usage
─────
  cd trendplus
  backend\.venv\Scripts\activate
  python scripts/compute_all_dates.py

  # Or just one specific date:
  python scripts/compute_all_dates.py --date 2026-03-14

  # Only compute the most recent N dates:
  python scripts/compute_all_dates.py --last 30
"""

import asyncio
import argparse
import datetime
import io
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ── Path setup ───────────────────────────────────────────────────────
ROOT    = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

import asyncpg
import numpy as np
import pandas as pd
from backend.app.core.sector_mapping import normalize_sector_name

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("compute_all_dates")

# ── Import compute functions ──────────────────────────────────────────
from analytics_engine import (
    build_price_pivots,
    get_matrix_dates,
    compute_matrices,
    compute_period_changes,
    compute_all_period_changes,     # NEW: multi-horizon returns
    fill_flexible_period_changes,   # NEW: short-history fallback fill
    compute_52w_high,
    compute_rs_score,
    compute_weighted_rpi,
    compute_rsi,
    compute_adx,
    compute_momentum_score,
    compute_ema_signals,
    build_sector_12d,
    build_sector_5d,
    build_sector_today,
    LOOKBACK_DAYS,
    RSI_PERIOD,
    ADX_PERIOD,
    MIN_ROWS_ADX,
    W52_DAYS,
    EMA_FAST,
    EMA_SLOW,
    EMA_CROSS_WINDOW,
    EMA_MIN_ROWS,
)

MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL_P = 9
HISTORY_DAYS  = W52_DAYS + 50   # 302 days (52W + EMA200 + 12M return buffer)

_BATCH = 500


# ════════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════════

def _safe_float(val) -> Optional[float]:
    try:
        v = float(val)
        return None if (v != v) else v
    except (TypeError, ValueError):
        return None

def _clamp_rs(val) -> Optional[float]:
    """Clamp rs_score to 1–99 (DB constraint)."""
    v = _safe_float(val)
    if v is None:
        return None
    return max(1.0, min(99.0, v))

def _clamp_momentum(val) -> Optional[float]:
    """Clamp momentum_score to 0–100 (DB constraint)."""
    v = _safe_float(val)
    if v is None:
        return None
    return max(0.0, min(100.0, v))

def _safe_int(val) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None

def _safe_bool(val) -> Optional[bool]:
    if val is None or (isinstance(val, float) and val != val):
        return None
    return bool(val)


def compute_macd(close_piv: pd.DataFrame) -> pd.DataFrame:
    prices    = close_piv.T
    ema_fast  = prices.ewm(span=MACD_FAST,     adjust=False).mean()
    ema_slow  = prices.ewm(span=MACD_SLOW,     adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_sig  = macd_line.ewm(span=MACD_SIGNAL_P, adjust=False).mean()
    macd_hist = macd_line - macd_sig

    last = close_piv.shape[1] - 1
    out  = pd.DataFrame({
        "MACD Line":   macd_line.iloc[last],
        "MACD Signal": macd_sig.iloc[last],
        "MACD Hist":   macd_hist.iloc[last],
    }, index=close_piv.index)

    # Zero out values where we don't have enough rows
    for sym in out.index:
        series = close_piv.loc[sym]
        valid  = int(series.notna().sum())
        if valid < MACD_SLOW + MACD_SIGNAL_P:
            out.at[sym, "MACD Line"]   = None
            out.at[sym, "MACD Signal"] = None
            out.at[sym, "MACD Hist"]   = None
    return out


# ════════════════════════════════════════════════════════════════════
#  LOAD FULL PRICE HISTORY FROM DB
# ════════════════════════════════════════════════════════════════════

async def load_all_price_history(conn) -> pd.DataFrame:
    """Pull entire price_history table — returns DataFrame like bhav CSV."""
    rows = await conn.fetch(
        """
        SELECT ph.symbol AS "SYMBOL",
               ph.trade_date AS "DATE",
               ph.open_price  AS "OPEN",
               ph.high_price  AS "HIGH",
               ph.low_price   AS "LOW",
               ph.close_price AS "CLOSE",
               ph.volume      AS "TOTTRDQTY",
               ph.total_trades AS "TOTALTRADES",
               ph.prev_close   AS "PREVCLOSE"
        FROM price_history ph
        JOIN symbols s ON s.symbol = ph.symbol
        WHERE ph.close_price IS NOT NULL
        ORDER BY ph.trade_date ASC
        """
    )
    if not rows:
        raise RuntimeError("price_history is empty — run backfill_to_supabase.py first")

    df = pd.DataFrame([dict(r) for r in rows])
    df["DATE"] = pd.to_datetime(df["DATE"])
    for col in ["OPEN","HIGH","LOW","CLOSE","PREVCLOSE"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ["TOTTRDQTY","TOTALTRADES"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    # Per-day liquidity filter — same logic as main.py Excel code:
    # If a symbol traded >= 5000 times on a given day, include it for that day.
    # This means a symbol can appear on days it was liquid and be absent on days it wasn't.
    # We then only process symbols that appear on the specific trade_date we're computing.
    MIN_DAILY_TRADES = 5000
    if "TOTALTRADES" in df.columns:
        before = len(df)
        df["TOTALTRADES"] = pd.to_numeric(df["TOTALTRADES"], errors="coerce").fillna(0)
        df = df[df["TOTALTRADES"] >= MIN_DAILY_TRADES].copy()
        logger.info("Loaded price_history: %d rows (from %d), %d symbols, %d dates after trades>=5000 filter",
                    len(df), before, df["SYMBOL"].nunique(), df["DATE"].nunique())
    else:
        logger.info("Loaded price_history: %d rows, %d symbols, %d dates",
                    len(df), df["SYMBOL"].nunique(), df["DATE"].nunique())
    return df


async def load_master_from_db(conn):
    """Load EQUITY_L and sector master from master_data table."""
    rows = await conn.fetch("SELECT key, content FROM master_data")
    master = {}
    for r in rows:
        master[r["key"]] = r["content"]

    nse_master  = pd.DataFrame()
    sec_master  = pd.DataFrame()

    if "EQUITY_L" in master:
        try:
            df = pd.read_csv(io.StringIO(master["EQUITY_L"]), dtype=str)
            df.columns = [c.strip() for c in df.columns]
            sym_col  = next((c for c in df.columns if "SYMBOL" in c.upper()), None)
            name_col = next((c for c in df.columns if "NAME" in c.upper()), None)
            isin_col = next((c for c in df.columns if "ISIN" in c.upper()), None)
            if sym_col:
                df = df.rename(columns={sym_col:"Symbol"})
                if name_col: df = df.rename(columns={name_col:"Company Name"})
                if isin_col: df = df.rename(columns={isin_col:"ISIN"})
                nse_master = df[["Symbol"] + [c for c in ["Company Name","ISIN"] if c in df.columns]].copy()
                nse_master = nse_master.dropna(subset=["Symbol"])
                nse_master["Symbol"] = nse_master["Symbol"].str.strip().str.upper()
                nse_master = nse_master.set_index("Symbol")
            logger.info("EQUITY_L loaded: %d symbols", len(nse_master))
        except Exception as e:
            logger.warning("EQUITY_L parse error: %s", e)

    # Key stored by refresh_master_data.py is "SECTOR_MASTER"
    if "SECTOR_MASTER" in master:
        try:
            df = pd.read_csv(io.StringIO(master["SECTOR_MASTER"]), dtype=str)
            df.columns = [c.strip().upper() for c in df.columns]
            # Expected cols from refresh_master_data.py: SYMBOL, SECTOR, CAP_CATEGORY
            sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
            sec_col = next((c for c in df.columns if "SECTOR" in c), None)
            cap_col = next((c for c in df.columns if "CAP" in c), None)
            logger.info("SECTOR_MASTER cols: %s", list(df.columns))
            if sym_col and sec_col:
                rename = {sym_col: "Symbol", sec_col: "Sector"}
                if cap_col:
                    rename[cap_col] = "CapCategory"
                df = df.rename(columns=rename)
                keep = ["Symbol", "Sector"] + (["CapCategory"] if cap_col else [])
                sec_master = df[keep].copy()
                sec_master["Symbol"] = sec_master["Symbol"].str.strip().str.upper()
                sec_master = sec_master.dropna(subset=["Symbol"]).set_index("Symbol")
                logger.info("Sector master loaded: %d symbols", len(sec_master))
            else:
                logger.warning("SECTOR_MASTER missing SYMBOL or SECTOR col — found: %s",
                               list(df.columns))
        except Exception as e:
            logger.warning("Sector master parse error: %s", e)
    else:
        logger.warning("SECTOR_MASTER not found in master_data table. "
                       "Run: python scripts/refresh_master_data.py")

    return nse_master, sec_master


# ════════════════════════════════════════════════════════════════════
#  COMPUTE + UPSERT FOR ONE DATE
# ════════════════════════════════════════════════════════════════════

async def compute_and_upsert(
    conn,
    hist_full: pd.DataFrame,
    nse_master: pd.DataFrame,
    sec_master: pd.DataFrame,
    trade_date: datetime.date,
):
    """
    Given full price history and a target trade_date:
    - Slice the last HISTORY_DAYS of data UP TO trade_date
    - Compute all metrics as of that date
    - Upsert trend_results + sector_daily + market_calendar
    """
    t0 = time.monotonic()

    target_ts = pd.Timestamp(trade_date)

    # Slice: all history up to and including trade_date
    hist = hist_full[hist_full["DATE"] <= target_ts].copy()

    # Keep only last HISTORY_DAYS unique dates
    all_dates = sorted(hist["DATE"].unique())
    if len(all_dates) == 0:
        logger.warning("[%s] No price data — skipping", trade_date)
        return 0

    cutoff_date = all_dates[-min(HISTORY_DAYS, len(all_dates))]
    hist = hist[hist["DATE"] >= cutoff_date]

    # trade_date must be the last date in this slice
    if pd.Timestamp(trade_date) not in hist["DATE"].values:
        logger.warning("[%s] trade_date not in price_history — skipping", trade_date)
        return 0

    # Build pivots (build_price_pivots returns 4 values; open_piv unused here)
    close_piv, high_piv, low_piv, open_piv = build_price_pivots(hist)

    # Ensure trade_date is the last column
    if close_piv.columns[-1] != target_ts:
        logger.warning("[%s] Last pivot col is %s — skipping", trade_date, close_piv.columns[-1])
        return 0

    # Compute metrics
    matrix_dates = get_matrix_dates(close_piv, LOOKBACK_DAYS)
    trending, bool_matrix, pct_matrix = compute_matrices(close_piv, matrix_dates)
    chg_12d, chg_5d = compute_period_changes(close_piv, matrix_dates)
    # New multi-horizon returns (chg_3d, chg_1m, chg_2m, chg_3m, chg_6m, chg_12m)
    period_changes  = compute_all_period_changes(close_piv)
    _changes = fill_flexible_period_changes(close_piv, {
        "chg_5d":  chg_5d,
        "chg_12d": chg_12d,
        "chg_3d":  period_changes["chg_3d"],
    })
    chg_5d, chg_12d = _changes["chg_5d"], _changes["chg_12d"]
    period_changes["chg_3d"] = _changes["chg_3d"]
    high_52w, pct_from_high, near_high, rank_52w = compute_52w_high(close_piv, W52_DAYS)
    rs_score     = compute_rs_score(close_piv)
    weighted_rpi = compute_weighted_rpi(close_piv)
    rsi          = compute_rsi(close_piv, RSI_PERIOD)
    adx      = compute_adx(close_piv, high_piv, low_piv, ADX_PERIOD, MIN_ROWS_ADX)
    momentum = compute_momentum_score(trending, rsi, adx, rs_score, pct_from_high)
    macd     = compute_macd(close_piv)
    ema_df   = compute_ema_signals(
        close_piv, fast=EMA_FAST, slow=EMA_SLOW,
        cross_window=EMA_CROSS_WINDOW, min_rows=EMA_MIN_ROWS,
    )

    # Assemble result
    date_cols_nf   = list(reversed(matrix_dates))
    date_col_names = [d.strftime("%Y-%m-%d") for d in date_cols_nf]
    bool_out = bool_matrix[date_cols_nf].copy(); bool_out.columns = date_col_names
    pct_out  = pct_matrix[date_cols_nf].copy();  pct_out.columns  = date_col_names
    today_col = date_col_names[0]

    result = (
        pd.DataFrame({"Trending Days": trending})
        .join(chg_12d).join(chg_5d).join(period_changes)
        .join(adx).join(rsi)
        .join(high_52w).join(pct_from_high)
        .join(near_high).join(rank_52w)
        .join(rs_score).join(weighted_rpi).join(momentum)
        .join(macd).join(ema_df)
    )
    result.index.name = "Symbol"
    result = result.reset_index()

    # ── Merge NSE master FIRST (inner join shrinks result) ────────
    # Close/High/Low/pct_today must be assigned AFTER the join
    # so array lengths match the final symbol count
    if not nse_master.empty:
        before = len(result)
        result = result.join(nse_master, on="Symbol", how="inner")
        if "Company Name" not in result.columns:
            result["Company Name"] = result["Symbol"]
        if "ISIN" not in result.columns:
            result["ISIN"] = ""
        logger.info("[%s] NSE master join: %d → %d symbols (ETFs excluded)",
                    trade_date, before, len(result))
    else:
        result["Company Name"] = result["Symbol"]
        result["ISIN"] = ""

    # Merge sector master
    if not sec_master.empty:
        result = result.join(sec_master, on="Symbol", how="left")
        if "Sector" in result.columns:
            result["Sector"] = result["Sector"].map(normalize_sector_name)
    else:
        result["Sector"] = ""
        result["CapCategory"] = ""

    for col in ["Company Name","ISIN","Sector","CapCategory"]:
        if col in result.columns:
            result[col] = result[col].fillna("").astype(str).str.strip()

    # ── Now assign price/pct columns — lengths match result ──────
    result["Close"]   = close_piv.iloc[:, -1].reindex(result["Symbol"].values).values
    result["High"]    = high_piv.iloc[:, -1].reindex(result["Symbol"].values).values
    result["Low"]     = low_piv.iloc[:, -1].reindex(result["Symbol"].values).values
    pct_today         = pct_out[today_col].reindex(result["Symbol"].values)
    result["chg_1d"]  = pct_today.values

    # ── Volume / trades from hist ─────────────────────────────────
    today_raw = hist[hist["DATE"] == target_ts].set_index("SYMBOL")
    has_vol   = "TOTTRDQTY" in today_raw.columns

    # ── Build trend_results rows ──────────────────────────────────
    trend_rows = []
    for _, row in result.iterrows():
        sym = row["Symbol"]
        bm = {}; pm = {}
        for dc in date_col_names:
            if sym in bool_out.index:
                bv = bool_out.at[sym, dc]
                bm[dc] = bool(bv) if pd.notna(bv) else None
            if sym in pct_out.index:
                pv = pct_out.at[sym, dc]
                pm[dc] = round(float(pv), 4) if pd.notna(pv) else None

        vol = None; trades = None
        if has_vol and sym in today_raw.index:
            vol    = _safe_int(today_raw.at[sym, "TOTTRDQTY"])
            trades = _safe_int(today_raw.at[sym, "TOTALTRADES"]) if "TOTALTRADES" in today_raw.columns else None

        trend_rows.append((
            trade_date, sym,
            _safe_int(row.get("Trending Days")),
            json.dumps(bm), json.dumps(pm),
            _safe_float(row.get("chg_1d")),
            _safe_float(row.get("5d Change%")),
            _safe_float(row.get("12d Change%")),
            _safe_float(row.get("52W High")),
            _safe_float(row.get("52W High %")),
            _safe_bool(row.get("Near 52W High")),
            _safe_float(row.get("52W Rank")),
            _safe_float(row.get("RSI 14")),
            _safe_float(row.get("ADX 14")),
            _safe_float(row.get("MACD Line")),
            _safe_float(row.get("MACD Signal")),
            _safe_float(row.get("MACD Hist")),
            _safe_float(row.get("EMA 50")),
            _safe_float(row.get("EMA 200")),
            str(row.get("EMA Signal") or ""),
            _clamp_rs(row.get("RS Score")),
            _safe_float(row.get("Weighted RPI")),
            _clamp_momentum(row.get("Momentum Score")),
            _safe_float(row.get("Close")),   # open (no separate open in bhav)
            _safe_float(row.get("High")),
            _safe_float(row.get("Low")),
            _safe_float(row.get("Close")),
            vol, trades,
            _safe_float(row.get("chg_3d")),    # $30
            _safe_float(row.get("chg_1m")),    # $31
            _safe_float(row.get("chg_2m")),    # $32
            _safe_float(row.get("chg_3m")),    # $33
            _safe_float(row.get("chg_6m")),    # $34
            _safe_float(row.get("chg_12m")),   # $35
        ))

    # Upsert trend_results
    for batch_start in range(0, len(trend_rows), _BATCH):
        batch = trend_rows[batch_start : batch_start + _BATCH]
        await conn.executemany(
            """
            INSERT INTO trend_results (
                trade_date, symbol,
                trending_days, bool_matrix, pct_matrix,
                chg_1d, chg_5d, chg_12d,
                high_52w, pct_from_high, near_52w_high, rank_52w,
                rsi_14, adx_14,
                macd_line, macd_signal, macd_hist,
                ema_50, ema_200, ema_signal,
                rs_score, weighted_rpi, momentum_score,
                open_price, high_price, low_price, close_price,
                volume, total_trades,
                chg_3d, chg_1m, chg_2m, chg_3m, chg_6m, chg_12m
            ) VALUES (
                $1,$2,$3,$4::jsonb,$5::jsonb,
                $6,$7,$8,$9,$10,$11,$12,$13,$14,
                $15,$16,$17,$18,$19,$20,
                $21,$22,$23,$24,$25,$26,$27,$28,$29,
                $30,$31,$32,$33,$34,$35
            )
            ON CONFLICT (trade_date, symbol) DO UPDATE SET
                trending_days  = excluded.trending_days,
                bool_matrix    = excluded.bool_matrix,
                pct_matrix     = excluded.pct_matrix,
                chg_1d         = excluded.chg_1d,
                chg_5d         = excluded.chg_5d,
                chg_12d        = excluded.chg_12d,
                high_52w       = excluded.high_52w,
                pct_from_high  = excluded.pct_from_high,
                near_52w_high  = excluded.near_52w_high,
                rank_52w       = excluded.rank_52w,
                rsi_14         = excluded.rsi_14,
                adx_14         = excluded.adx_14,
                macd_line      = excluded.macd_line,
                macd_signal    = excluded.macd_signal,
                macd_hist      = excluded.macd_hist,
                ema_50         = excluded.ema_50,
                ema_200        = excluded.ema_200,
                ema_signal     = excluded.ema_signal,
                rs_score       = excluded.rs_score,
                weighted_rpi   = excluded.weighted_rpi,
                momentum_score = excluded.momentum_score,
                open_price     = excluded.open_price,
                high_price     = excluded.high_price,
                low_price      = excluded.low_price,
                close_price    = excluded.close_price,
                volume         = excluded.volume,
                total_trades   = excluded.total_trades,
                chg_3d         = excluded.chg_3d,
                chg_1m         = excluded.chg_1m,
                chg_2m         = excluded.chg_2m,
                chg_3m         = excluded.chg_3m,
                chg_6m         = excluded.chg_6m,
                chg_12m        = excluded.chg_12m
            """,
            batch,
        )

    # ── Sector daily ─────────────────────────────────────────────
    if "Sector" in result.columns:
        result_s = result.copy()
        result_s[today_col] = bool_out[today_col].reindex(result["Symbol"].values).values
        pct_today_series    = pd.Series(pct_today.values, index=result["Symbol"].values)

        sec_12d   = build_sector_12d(result_s)
        sec_5d    = build_sector_5d(result_s)
        sec_today = build_sector_today(result_s, today_col, pct_today_series)

        sec = (
            sec_12d
            .merge(sec_5d[["Sector","Avg 5d Change%"]], on="Sector", how="outer")
            .merge(sec_today[["Sector","Stocks Up","Stocks Down","% Stocks Up","Avg Today Change%"]], on="Sector", how="outer")
        )
        near_counts = (
            result_s[result_s["Near 52W High"] == True]
            .groupby("Sector")["Symbol"].count()
        )

        sector_rows = []
        for _, r in sec.iterrows():
            sector_name = str(r.get("Sector","")).strip()
            if not sector_name: continue
            sc = int(r.get("Stocks",0) or 0)
            su = int(r.get("Stocks Up",0) or 0)
            sd = int(r.get("Stocks Down",0) or 0)
            nh = int(near_counts.get(sector_name,0))
            sector_rows.append((
                trade_date, sector_name, sc, su, sd,
                _safe_float(r.get("% Stocks Up")),
                _safe_float(r.get("Avg Trending Days")),
                _safe_float(r.get("Avg 12d Change%")),
                _safe_float(r.get("Avg 5d Change%")),
                _safe_float(r.get("Avg Today Change%")),
                _safe_float(r.get("Avg RSI 14")),
                _safe_float(r.get("Avg ADX 14")),
                _safe_float(r.get("Avg RS Score")),
                _safe_float(r.get("Avg Momentum")),
                nh,
                round(nh/sc*100, 1) if sc else None,
            ))

        if sector_rows:
            await conn.executemany(
                """
                INSERT INTO sector_daily (
                    trade_date, sector,
                    stock_count, stocks_up, stocks_down, pct_stocks_up,
                    avg_trending_days, avg_chg_12d, avg_chg_5d, avg_chg_today,
                    avg_rsi_14, avg_adx_14, avg_rs_score, avg_momentum,
                    stocks_near_high, pct_near_high
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                ON CONFLICT (trade_date, sector) DO UPDATE SET
                    stock_count       = excluded.stock_count,
                    stocks_up         = excluded.stocks_up,
                    stocks_down       = excluded.stocks_down,
                    pct_stocks_up     = excluded.pct_stocks_up,
                    avg_trending_days = excluded.avg_trending_days,
                    avg_chg_12d       = excluded.avg_chg_12d,
                    avg_chg_5d        = excluded.avg_chg_5d,
                    avg_chg_today     = excluded.avg_chg_today,
                    avg_rsi_14        = excluded.avg_rsi_14,
                    avg_adx_14        = excluded.avg_adx_14,
                    avg_rs_score      = excluded.avg_rs_score,
                    avg_momentum      = excluded.avg_momentum,
                    stocks_near_high  = excluded.stocks_near_high,
                    pct_near_high     = excluded.pct_near_high
                """,
                sector_rows,
            )

    # ── market_calendar ───────────────────────────────────────────
    elapsed = round(time.monotonic() - t0, 2)
    await conn.execute(
        """
        INSERT INTO market_calendar
            (trade_date, is_trading_day, bhav_downloaded, engine_status,
             symbol_count, engine_duration_secs, processed_at)
        VALUES ($1, true, true, 'done', $2, $3, now())
        ON CONFLICT (trade_date) DO UPDATE SET
            is_trading_day       = true,
            bhav_downloaded      = true,
            engine_status        = 'done',
            symbol_count         = excluded.symbol_count,
            engine_duration_secs = excluded.engine_duration_secs,
            processed_at         = now(),
            error_message        = null
        """,
        trade_date, len(result), elapsed,
    )
    logger.info("[%s] ✓ %d symbols  %.1fs", trade_date, len(result), elapsed)
    return len(result)


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════

async def main():
    parser = argparse.ArgumentParser(description="Compute metrics for all dates in price_history")
    parser.add_argument("--date",  help="Single date YYYY-MM-DD")
    parser.add_argument("--last",  type=int, help="Only process last N trading dates")
    parser.add_argument("--skip-existing", action="store_true",
                        help="Skip dates already in market_calendar with status=done")
    args = parser.parse_args()

    DATABASE_URL = os.environ.get("DATABASE_URL","")
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set in backend/.env")
        sys.exit(1)

    logger.info("=" * 60)
    logger.info("  TrendPulse — Compute All Dates from price_history")
    logger.info("=" * 60)

    pool = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        command_timeout=120,
        # Keep connections alive — send a ping every 60s
        max_inactive_connection_lifetime=60,
    )

    # ── Load everything up front using a short-lived connection ──────
    async with pool.acquire() as conn:
        hist_full = await load_all_price_history(conn)
        nse_master, sec_master = await load_master_from_db(conn)

    # Get all unique trading dates
    all_dates = sorted(hist_full["DATE"].dt.date.unique())
    logger.info("Trading dates in price_history: %d", len(all_dates))

    # Filter by args
    if args.date:
        target = datetime.date.fromisoformat(args.date)
        dates_to_process = [d for d in all_dates if d == target]
    elif args.last:
        dates_to_process = all_dates[-args.last:]
    else:
        dates_to_process = all_dates

    # Skip already-done dates if requested
    if args.skip_existing:
        async with pool.acquire() as conn:
            done = await conn.fetch(
                "SELECT trade_date FROM market_calendar WHERE engine_status='done'"
            )
        done_set = {r["trade_date"] for r in done}
        before = len(dates_to_process)
        dates_to_process = [d for d in dates_to_process if d not in done_set]
        logger.info("Skipping %d already-done dates (%d remaining)",
                    before - len(dates_to_process), len(dates_to_process))

    logger.info("Processing %d dates...", len(dates_to_process))
    logger.info("=" * 60)

    # ── Process each date with a FRESH connection ─────────────────────
    # Critical: never hold one connection across multiple dates.
    # Supabase kills idle connections after ~5 min.
    total_t0 = time.monotonic()
    failed   = []
    for i, trade_date in enumerate(dates_to_process, 1):
        try:
            async with pool.acquire() as conn:
                await compute_and_upsert(
                    conn, hist_full, nse_master, sec_master, trade_date
                )
            if (i % 10) == 0 or i == len(dates_to_process):
                elapsed = time.monotonic() - total_t0
                eta = (elapsed / i) * (len(dates_to_process) - i)
                logger.info("Progress: %d/%d  |  %.0fs elapsed  |  ETA %.0fs",
                            i, len(dates_to_process), elapsed, eta)
        except Exception as e:
            logger.error("[%s] FAILED: %s", trade_date, e)
            failed.append(trade_date)
            # Brief pause then continue — don't abort entire run
            await asyncio.sleep(2)

    total_elapsed = time.monotonic() - total_t0
    logger.info("=" * 60)
    logger.info("  ALL DONE — %d dates in %.0fs (%.1f min)",
                len(dates_to_process), total_elapsed, total_elapsed / 60)
    if failed:
        logger.warning("  Failed dates (%d): %s", len(failed),
                       ", ".join(str(d) for d in failed))
        logger.warning("  Re-run with --skip-existing to retry only failed dates")
    logger.info("=" * 60)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())