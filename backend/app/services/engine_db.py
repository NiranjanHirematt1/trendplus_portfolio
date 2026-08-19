"""
engine_db.py
────────────
DB-native analytics engine. No local files needed.

Workflow
────────
  1. Download today's bhav → parse in memory (never written to disk)
  2. Insert today's OHLCV rows → price_history
  3. Read last 262 days of OHLCV from price_history → build price pivots
  4. Read symbols + master data from DB → no local CSV files needed
  5. Compute all metrics (trending, RSI, ADX, MACD, EMA, RS, momentum,
     weighted_rpi, rpi_periods, rsi_short, ema_short)
  6. Upsert: trend_results · sector_daily · market_calendar

Prerequisites
─────────────
  • supabase_schema_v2.sql   must be run
  • migration_macd.sql       must be run
  • migration_ema.sql        must be run
  • master_data table created by backfill_to_supabase.py (stores EQUITY_L
    and nse_sector_master CSVs in Supabase — no local files needed)
  • price_history populated by backfill_to_supabase.py

Environment variables (backend/.env)
─────────────────────────────────────
  DATABASE_URL   — only variable needed; DATA_FOLDER / NSE_MASTER_CSV
                   / NSE_SECTOR_MASTER are no longer required

Fixes applied (2026-03-29)
──────────────────────────
  BUG 1: Added missing imports: compute_weighted_rpi, compute_rpi_periods,
          compute_rsi_short, compute_ema_short
  BUG 2: Actually call compute_rpi_periods, compute_rsi_short, compute_ema_short
          and join their results into the result DataFrame
  BUG 3: Added all 8 missing columns to trend_results INSERT tuple:
          rpi_2w, rpi_3m, rpi_6m, rpi_6m_sma2w, rsi_1d, rsi_1w, ema_9, ema_21
          Parameter count: 29 → 37
"""

import io
import json
import logging
import datetime
from typing import Optional

import numpy as np
import pandas as pd
import asyncpg

logger = logging.getLogger(__name__)

# ── Import compute functions from analytics_engine.py ────────────────
import sys, os
_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from analytics_engine import (             # noqa: E402
    build_price_pivots,
    get_matrix_dates,
    compute_matrices,
    compute_period_changes,
    compute_all_period_changes,     # NEW: multi-horizon returns
    fill_flexible_period_changes,   # NEW: short-history fallback fill
    compute_52w_high,
    compute_rs_score,
    compute_weighted_rpi,       # FIX BUG 1: was missing
    compute_rpi_periods,        # FIX BUG 1: was missing
    compute_rsi,
    compute_rsi_short,          # FIX BUG 1: was missing
    compute_adx,
    compute_momentum_score,
    compute_ema_signals,
    compute_ema_short,          # FIX BUG 1: was missing
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
    ALLOWED_SERIES,
)

# MACD parameters
MACD_FAST     = 12
MACD_SLOW     = 26
MACD_SIGNAL_P = 9
MACD_MIN_ROWS = MACD_SLOW + MACD_SIGNAL_P   # 35

# Days of price history to pull from DB.
# EMA 200 needs 210, 52W high needs 252, 12M return needs 253 — use a
# healthy buffer so a holiday-heavy stretch never starves chg_12m.
HISTORY_DAYS = W52_DAYS + 50   # 302

_BATCH = 500

from app.core.sector_mapping import normalize_sector_name

# ════════════════════════════════════════════════════════════════════
#  MACD
# ════════════════════════════════════════════════════════════════════

def compute_macd(close_piv: pd.DataFrame) -> pd.DataFrame:
    prices    = close_piv.T
    ema_fast  = prices.ewm(span=MACD_FAST,     adjust=False).mean()
    ema_slow  = prices.ewm(span=MACD_SLOW,     adjust=False).mean()
    macd_line = ema_fast - ema_slow
    macd_sig  = macd_line.ewm(span=MACD_SIGNAL_P, adjust=False).mean()
    macd_hist = macd_line - macd_sig

    result = pd.DataFrame({
        "MACD Line":   macd_line.iloc[-1],
        "MACD Signal": macd_sig.iloc[-1],
        "MACD Hist":   macd_hist.iloc[-1],
    })
    # Null out symbols with insufficient history
    insufficient = close_piv.notna().sum(axis=1)
    result.loc[insufficient[insufficient < MACD_MIN_ROWS].index,
               ["MACD Line", "MACD Signal", "MACD Hist"]] = np.nan
    return result


# ════════════════════════════════════════════════════════════════════
#  TYPE HELPERS
# ════════════════════════════════════════════════════════════════════

def _sf(val) -> Optional[float]:
    try:
        v = float(val)
        return None if v != v else v
    except (TypeError, ValueError):
        return None

def _si(val) -> Optional[int]:
    try:
        return int(val)
    except (TypeError, ValueError):
        return None

def _sb(val) -> Optional[bool]:
    if val is None or (isinstance(val, float) and val != val):
        return None
    return bool(val)

def _ema_sig(val) -> str:
    return val if val in ("golden_cross", "above_200", "approaching") else ""


# ════════════════════════════════════════════════════════════════════
#  DB READ HELPERS
# ════════════════════════════════════════════════════════════════════

async def _load_price_history(conn, days: int) -> pd.DataFrame:
    """
    Pull the last `days` trading dates of OHLCV from price_history.
    Returns DataFrame with columns:
        SYMBOL  DATE  CLOSE  HIGH  LOW  TOTTRDQTY  TOTALTRADES

    Note: NULL total_trades rows are kept (IS NULL OR >= 5000) so that
    symbols without trade count data are not silently dropped.
    """
    rows = await conn.fetch(
        """
        with date_cutoff as (
            select trade_date
            from   (
                select distinct trade_date
                from   price_history
                order  by trade_date desc
                limit  $1
            ) sub
            order by trade_date
            limit 1
        )
        select ph.symbol,
               ph.trade_date,
               ph.close_price,
               ph.high_price,
               ph.low_price,
               ph.volume,
               ph.total_trades
        from   price_history ph
        join   symbols s on s.symbol = ph.symbol
        where  ph.trade_date >= (select trade_date from date_cutoff)
          and  ph.close_price is not null
          and  (ph.total_trades is null or ph.total_trades >= 5000)
        order  by ph.symbol, ph.trade_date
        """,
        days,
    )
    if not rows:
        raise RuntimeError(
            "price_history is empty. Run backfill_to_supabase.py first."
        )

    df = pd.DataFrame(rows, columns=[
        "SYMBOL", "DATE", "CLOSE", "HIGH", "LOW", "TOTTRDQTY", "TOTALTRADES"
    ])
    df["DATE"] = pd.to_datetime(df["DATE"])
    for col in ["CLOSE", "HIGH", "LOW"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


async def _load_master_data(conn) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Read EQUITY_L and SECTOR_MASTER from the master_data table
    (stored by backfill_to_supabase.py).
    Returns (master_df, sector_df) indexed by SYMBOL.
    Returns empty DataFrames if rows are missing.
    """
    master_df = pd.DataFrame()
    sector_df = pd.DataFrame()

    try:
        row = await conn.fetchrow(
            "select content from master_data where key = 'EQUITY_L'"
        )
        if row:
            m = pd.read_csv(io.StringIO(row["content"]), dtype=str)
            m.columns = m.columns.str.strip().str.upper()
            rename = {}
            if "NAME OF COMPANY" in m.columns:
                rename["NAME OF COMPANY"] = "COMPANY_NAME"
            if "ISIN NUMBER" in m.columns:
                rename["ISIN NUMBER"] = "ISIN"
            if rename:
                m = m.rename(columns=rename)
            keep = [c for c in ["SYMBOL", "COMPANY_NAME", "ISIN"]
                    if c in m.columns]
            if "SYMBOL" in keep:
                master_df = m[keep].set_index("SYMBOL")
                logger.info("[engine] EQUITY_L loaded from DB: %d rows",
                            len(master_df))
        else:
            logger.warning("[engine] master_data is empty — run refresh_master_data.py first")
    except Exception as e:
        logger.warning("[engine] Could not load EQUITY_L from master_data: %s", e)

    try:
        row = await conn.fetchrow(
            "select content from master_data where key = 'SECTOR_MASTER'"
        )
        if row:
            s = pd.read_csv(io.StringIO(row["content"]), dtype=str)
            s.columns = s.columns.str.strip().str.upper()
            if "SYMBOL" in s.columns:
                sector_df = s.set_index("SYMBOL")
                logger.info("[engine] SECTOR_MASTER loaded from DB: %d rows",
                            len(sector_df))
    except Exception as e:
        logger.warning("[engine] Could not load SECTOR_MASTER from master_data: %s", e)

    return master_df, sector_df


async def _store_today_prices(
    conn,
    bhav_content: bytes,
    trade_date: datetime.date,
) -> pd.DataFrame:
    """
    Parse today's bhav bytes, upsert into price_history, return DataFrame.
    Never writes anything to disk.
    """
    try:
        raw = pd.read_csv(io.StringIO(bhav_content.decode("utf-8")), dtype=str)
    except Exception as e:
        raise RuntimeError(f"Could not parse bhav CSV: {e}") from e

    raw.columns = raw.columns.str.strip().str.upper()
    required = {"SYMBOL", "SERIES", "HIGH", "LOW", "CLOSE"}
    if not required.issubset(raw.columns):
        raise RuntimeError(
            f"Bhav CSV missing columns: {required - set(raw.columns)}"
        )

    raw["SERIES"] = raw["SERIES"].str.strip().str.upper()
    raw = raw[raw["SERIES"].isin(ALLOWED_SERIES)].copy()
    if raw.empty:
        raise RuntimeError("Bhav CSV has no EQ or BE rows")

    for col in ["HIGH", "LOW", "CLOSE"]:
        raw[col] = pd.to_numeric(raw[col], errors="coerce")
    if "TOTTRDQTY" in raw.columns:
        raw["TOTTRDQTY"] = pd.to_numeric(raw["TOTTRDQTY"], errors="coerce")
    if "TOTALTRADES" in raw.columns:
        raw["TOTALTRADES"] = pd.to_numeric(raw["TOTALTRADES"], errors="coerce")

    logger.info("[engine] Bhav parsed: %d EQ+BE rows for %s",
                len(raw), trade_date)

    # Fetch previous closes for prev_close column
    prev_rows = await conn.fetch(
        """
        select distinct on (symbol)
               symbol, close_price
        from   price_history
        where  trade_date < $1
        order  by symbol, trade_date desc
        """,
        trade_date,
    )
    prev_map = {r["symbol"]: r["close_price"] for r in prev_rows}

    # Build rows for price_history
    ph_rows = []
    for _, r in raw.iterrows():
        sym = str(r["SYMBOL"])
        ph_rows.append((
            sym,
            trade_date,
            _sf(r.get("CLOSE")),   # open proxy (bhav has no OPEN column)
            _sf(r.get("HIGH")),
            _sf(r.get("LOW")),
            _sf(r.get("CLOSE")),
            _si(r.get("TOTTRDQTY"))   if "TOTTRDQTY"   in r.index else None,
            _si(r.get("TOTALTRADES")) if "TOTALTRADES" in r.index else None,
            prev_map.get(sym),     # prev_close
        ))

    for start in range(0, len(ph_rows), _BATCH):
        await conn.executemany(
            """
            insert into price_history
                (symbol, trade_date, open_price, high_price, low_price,
                 close_price, volume, total_trades, prev_close)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            on conflict (symbol, trade_date) do update set
                open_price   = excluded.open_price,
                high_price   = excluded.high_price,
                low_price    = excluded.low_price,
                close_price  = excluded.close_price,
                volume       = coalesce(excluded.volume,       price_history.volume),
                total_trades = coalesce(excluded.total_trades, price_history.total_trades),
                prev_close   = excluded.prev_close
            """,
            ph_rows[start : start + _BATCH],
        )
    logger.info("[engine] price_history upserted: %d rows", len(ph_rows))

    raw["DATE"] = pd.Timestamp(trade_date)
    return raw


# ════════════════════════════════════════════════════════════════════
#  MAIN ENGINE
# ════════════════════════════════════════════════════════════════════

async def run_engine(
    pool: asyncpg.Pool,
    bhav_content: bytes,
    trade_date: Optional[datetime.date] = None,
) -> dict:
    """
    Full daily analytics run. Reads from Supabase, writes to Supabase.
    No local files touched.

    Parameters
    ----------
    pool         : asyncpg pool
    bhav_content : raw bytes of today's bhav CSV (from downloader)
    trade_date   : override date (defaults to today)

    Returns
    -------
    { trade_date, symbols_processed, duration_secs }
    """
    import time
    t0 = time.monotonic()

    if trade_date is None:
        trade_date = datetime.date.today()

    async with pool.acquire() as conn:

        # ── 1. Store today's bhav → price_history ─────────────────────
        today_raw = await _store_today_prices(conn, bhav_content, trade_date)

        # ── 2. Pull history from DB ────────────────────────────────────
        logger.info("[engine] Loading %d days from price_history...",
                    HISTORY_DAYS)
        hist = await _load_price_history(conn, HISTORY_DAYS)
        logger.info("[engine] Pivot input: %d symbols × %d dates",
                    hist["SYMBOL"].nunique(), hist["DATE"].nunique())

        # ── 3. Build price pivots ──────────────────────────────────────
        # build_price_pivots returns 4 values (adds open_); price_history read
        # here has no OPEN column so open_piv is unused (Close is the open proxy).
        close_piv, high_piv, low_piv, open_piv = build_price_pivots(hist)

        # ── 4. Compute all metrics ─────────────────────────────────────
        matrix_dates = get_matrix_dates(close_piv, LOOKBACK_DAYS)

        trending, bool_matrix, pct_matrix = compute_matrices(
            close_piv, matrix_dates
        )
        chg_12d, chg_5d = compute_period_changes(close_piv, matrix_dates)
        # New multi-horizon returns (chg_3d, chg_1m, chg_2m, chg_3m, chg_6m, chg_12m)
        period_changes = compute_all_period_changes(close_piv)
        _changes = fill_flexible_period_changes(close_piv, {
            "chg_5d":  chg_5d,
            "chg_12d": chg_12d,
            "chg_3d":  period_changes["chg_3d"],
        })
        chg_5d, chg_12d = _changes["chg_5d"], _changes["chg_12d"]
        period_changes["chg_3d"] = _changes["chg_3d"]
        high_52w, pct_from_high, near_high, rank_52w = compute_52w_high(
            close_piv, W52_DAYS
        )
        rs_score     = compute_rs_score(close_piv)
        weighted_rpi = compute_weighted_rpi(close_piv)     # → "Weighted RPI"
        rpi_periods  = compute_rpi_periods(close_piv)      # FIX BUG 2: now called
                                                            # → "RPI 2W", "RPI 3M",
                                                            #   "RPI 6M", "RPI 6M SMA2W"
        rsi          = compute_rsi(close_piv, RSI_PERIOD)  # → "RSI 14"
        rsi_short    = compute_rsi_short(close_piv)        # FIX BUG 2: now called
                                                            # → "RSI 1D", "RSI 1W"
        adx          = compute_adx(
            close_piv, high_piv, low_piv, ADX_PERIOD, MIN_ROWS_ADX
        )                                                   # → "ADX 14"
        momentum     = compute_momentum_score(
            trending, rsi, adx, rs_score, pct_from_high
        )                                                   # → "Momentum Score"
        macd         = compute_macd(close_piv)             # → "MACD Line", "MACD Signal", "MACD Hist"
        ema_df       = compute_ema_signals(
            close_piv,
            fast=EMA_FAST, slow=EMA_SLOW,
            cross_window=EMA_CROSS_WINDOW,
            min_rows=EMA_MIN_ROWS,
        )                                                   # → "EMA 50", "EMA 200", "EMA Signal"
        ema_short    = compute_ema_short(close_piv)        # FIX BUG 2: now called
                                                            # → "EMA 9", "EMA 21"

        # ── 5. Assemble result DataFrame ───────────────────────────────
        date_cols_nf   = list(reversed(matrix_dates))
        date_col_names = [d.strftime("%Y-%m-%d") for d in date_cols_nf]
        bool_out = bool_matrix[date_cols_nf].copy()
        bool_out.columns = date_col_names
        pct_out  = pct_matrix[date_cols_nf].copy()
        pct_out.columns  = date_col_names
        today_col = date_col_names[0]

        result = (
            pd.DataFrame({"Trending Days": trending})
            .join(chg_12d).join(chg_5d).join(period_changes)
            .join(adx).join(rsi).join(rsi_short)    # FIX BUG 2: rsi_short joined
            .join(high_52w).join(pct_from_high)
            .join(near_high).join(rank_52w)
            .join(rs_score).join(weighted_rpi)
            .join(rpi_periods)                       # FIX BUG 2: rpi_periods joined
            .join(momentum)
            .join(macd).join(ema_df).join(ema_short) # FIX BUG 2: ema_short joined
        )
        result.index.name = "Symbol"
        result = result.reset_index()

        result["Close"] = (
            close_piv.iloc[:, -1].reindex(result["Symbol"].values).values
        )
        result["High"] = (
            high_piv.iloc[:, -1].reindex(result["Symbol"].values).values
        )
        result["Low"] = (
            low_piv.iloc[:, -1].reindex(result["Symbol"].values).values
        )
        pct_today        = pct_out[today_col].reindex(result["Symbol"].values)
        result["chg_1d"] = pct_today.values

        vol_map = {}
        if "TOTTRDQTY" in today_raw.columns:
            vol_map = (
                today_raw.dropna(subset=["TOTTRDQTY"])
                .set_index("SYMBOL")[["TOTTRDQTY", "TOTALTRADES"]]
                .to_dict("index")
            )

        # ── 6. Load master data from DB ────────────────────────────────
        master_df, sector_df = await _load_master_data(conn)

        if not master_df.empty:
            before = len(result)
            result = result.join(master_df, on="Symbol", how="inner")
            logger.info("[engine] Master join: %d → %d (ETFs excluded)",
                        before, len(result))
            if "COMPANY_NAME" in result.columns:
                result = result.rename(columns={"COMPANY_NAME": "Company Name"})
            if "Company Name" not in result.columns:
                result["Company Name"] = result["Symbol"]
            if "ISIN" not in result.columns:
                result["ISIN"] = ""
        else:
            result["Company Name"] = result["Symbol"]
            result["ISIN"] = ""

        if not sector_df.empty:
            sec_col = next(
                (c for c in sector_df.columns if "SECTOR" in c.upper()), None
            )
            cap_col = next(
                (c for c in sector_df.columns if "CAP" in c.upper()), None
            )
            sym_upper = result["Symbol"].str.strip().str.upper().values
            if sec_col:
                result["Sector"] = (
                    sector_df[sec_col].reindex(sym_upper).map(normalize_sector_name).values
                )
            if cap_col:
                result["CapCategory"] = (
                    sector_df[cap_col].reindex(sym_upper).values
                )

        for col in ["Company Name", "ISIN", "Sector", "CapCategory"]:
            if col not in result.columns:
                result[col] = ""
            else:
                result[col] = result[col].fillna("").astype(str).str.strip()

        if "EMA Signal" in result.columns:
            result["EMA Signal"] = result["EMA Signal"].fillna("").astype(str)

        logger.info("[engine] Final: %d symbols for %s", len(result), trade_date)

        # ══════════════════════════════════════════════════════════════
        #  DB WRITES
        # ══════════════════════════════════════════════════════════════

        # 1. symbols
        sym_rows = [
            (str(r["Symbol"]), str(r.get("Company Name", r["Symbol"])),
             str(r.get("ISIN", "")), str(r.get("Sector", "")),
             str(r.get("CapCategory", "")))
            for _, r in result.iterrows()
        ]
        await conn.executemany(
            """
            insert into symbols (symbol, company_name, isin, sector, cap_category)
            values ($1,$2,$3,$4,$5)
            on conflict (symbol) do update set
                company_name = excluded.company_name,
                isin         = excluded.isin,
                sector       = excluded.sector,
                cap_category = excluded.cap_category,
                is_active    = true,
                updated_at   = now()
            """,
            sym_rows,
        )
        logger.info("[engine] Symbols: %d", len(sym_rows))

        # 2. trend_results
        # FIX BUG 3: tuple now has 37 params — added rpi_2w, rpi_3m, rpi_6m,
        #             rpi_6m_sma2w, rsi_1d, rsi_1w, ema_9, ema_21
        # Column names from analytics_engine.py verified:
        #   compute_rpi_periods → "RPI 2W", "RPI 3M", "RPI 6M", "RPI 6M SMA2W"
        #   compute_rsi_short   → "RSI 1D", "RSI 1W"
        #   compute_ema_short   → "EMA 9", "EMA 21"
        trend_rows = []
        for _, row in result.iterrows():
            sym  = row["Symbol"]
            bm, pm = {}, {}
            for dc in date_col_names:
                if sym in bool_out.index:
                    bv = bool_out.at[sym, dc]
                    bm[dc] = bool(bv) if pd.notna(bv) else None
                if sym in pct_out.index:
                    pv = pct_out.at[sym, dc]
                    pm[dc] = round(float(pv), 4) if pd.notna(pv) else None

            trend_rows.append((
                trade_date,                                                 # $1
                sym,                                                        # $2
                _si(row.get("Trending Days")),                              # $3
                json.dumps(bm),                                             # $4
                json.dumps(pm),                                             # $5
                _sf(row.get("chg_1d")),                                     # $6
                _sf(row.get("5d Change%")),                                 # $7
                _sf(row.get("12d Change%")),                                # $8
                _sf(row.get("52W High")),                                   # $9
                _sf(row.get("52W High %")),                                 # $10
                _sb(row.get("Near 52W High")),                              # $11
                _sf(row.get("52W Rank")),                                   # $12
                _sf(row.get("RSI 14")),                                     # $13
                _sf(row.get("RSI 1D")),                                     # $14  ← rsi_1d
                _sf(row.get("RSI 1W")),                                     # $15  ← rsi_1w
                _sf(row.get("ADX 14")),                                     # $16
                _sf(row.get("MACD Line")),                                  # $17
                _sf(row.get("MACD Signal")),                                # $18
                _sf(row.get("MACD Hist")),                                  # $19
                _sf(row.get("EMA 50")),                                     # $20
                _sf(row.get("EMA 200")),                                    # $21
                _ema_sig(row.get("EMA Signal", "")),                        # $22
                _sf(row.get("EMA 9")),                                      # $23  ← ema_9
                _sf(row.get("EMA 21")),                                     # $24  ← ema_21
                max(1.0, min(99.0, _sf(row.get("RS Score")) or 1.0)),       # $25  rs_score
                _sf(row.get("Weighted RPI")),                               # $26  weighted_rpi
                _sf(row.get("RPI 2W")),                                     # $27  ← rpi_2w
                _sf(row.get("RPI 3M")),                                     # $28  ← rpi_3m
                _sf(row.get("RPI 6M")),                                     # $29  ← rpi_6m
                _sf(row.get("RPI 6M SMA2W")),                               # $30  ← rpi_6m_sma2w
                max(0.0, min(100.0, _sf(row.get("Momentum Score")) or 0.0)),# $31  momentum_score
                _sf(row.get("Close")),                                      # $32  open proxy
                _sf(row.get("High")),                                       # $33
                _sf(row.get("Low")),                                        # $34
                _sf(row.get("Close")),                                      # $35
                _si(vol_map[sym]["TOTTRDQTY"])
                    if sym in vol_map else None,                             # $36
                _si(vol_map[sym]["TOTALTRADES"])
                    if sym in vol_map else None,                             # $37
                _sf(row.get("chg_3d")),                                     # $38
                _sf(row.get("chg_1m")),                                     # $39
                _sf(row.get("chg_2m")),                                     # $40
                _sf(row.get("chg_3m")),                                     # $41
                _sf(row.get("chg_6m")),                                     # $42
                _sf(row.get("chg_12m")),                                    # $43
            ))

        for start in range(0, len(trend_rows), _BATCH):
            await conn.executemany(
                """
                insert into trend_results (
                    trade_date, symbol,
                    trending_days, bool_matrix, pct_matrix,
                    chg_1d, chg_5d, chg_12d,
                    high_52w, pct_from_high, near_52w_high, rank_52w,
                    rsi_14, rsi_1d, rsi_1w, adx_14,
                    macd_line, macd_signal, macd_hist,
                    ema_50, ema_200, ema_signal,
                    ema_9, ema_21,
                    rs_score, weighted_rpi,
                    rpi_2w, rpi_3m, rpi_6m, rpi_6m_sma2w,
                    momentum_score,
                    open_price, high_price, low_price, close_price,
                    volume, total_trades,
                    chg_3d, chg_1m, chg_2m, chg_3m, chg_6m, chg_12m
                ) values (
                    $1,$2,$3,$4::jsonb,$5::jsonb,
                    $6,$7,$8,$9,$10,$11,$12,
                    $13,$14,$15,$16,
                    $17,$18,$19,
                    $20,$21,$22,
                    $23,$24,
                    $25,$26,
                    $27,$28,$29,$30,
                    $31,
                    $32,$33,$34,$35,$36,$37,
                    $38,$39,$40,$41,$42,$43
                )
                on conflict (trade_date, symbol) do update set
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
                    rsi_1d         = excluded.rsi_1d,
                    rsi_1w         = excluded.rsi_1w,
                    adx_14         = excluded.adx_14,
                    macd_line      = excluded.macd_line,
                    macd_signal    = excluded.macd_signal,
                    macd_hist      = excluded.macd_hist,
                    ema_50         = excluded.ema_50,
                    ema_200        = excluded.ema_200,
                    ema_signal     = excluded.ema_signal,
                    ema_9          = excluded.ema_9,
                    ema_21         = excluded.ema_21,
                    rs_score       = excluded.rs_score,
                    weighted_rpi   = excluded.weighted_rpi,
                    rpi_2w         = excluded.rpi_2w,
                    rpi_3m         = excluded.rpi_3m,
                    rpi_6m         = excluded.rpi_6m,
                    rpi_6m_sma2w   = excluded.rpi_6m_sma2w,
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
                trend_rows[start : start + _BATCH],
            )
        logger.info("[engine] Trend results: %d", len(trend_rows))

        # 3. sector_daily
        if "Sector" in result.columns:
            result_s = result.copy()
            result_s[today_col] = (
                bool_out[today_col].reindex(result["Symbol"].values).values
            )
            pct_today_s = pd.Series(
                pct_today.values, index=result["Symbol"].values
            )
            sec_12d   = build_sector_12d(result_s)
            sec_5d    = build_sector_5d(result_s)
            sec_today = build_sector_today(result_s, today_col, pct_today_s)
            sec = (
                sec_12d
                .merge(sec_5d[["Sector", "Avg 5d Change%"]],
                       on="Sector", how="outer")
                .merge(sec_today[["Sector", "Stocks Up", "Stocks Down",
                                   "% Stocks Up", "Avg Today Change%"]],
                       on="Sector", how="outer")
            )
            near_counts = (
                result_s[result_s["Near 52W High"] == True]
                .groupby("Sector")["Symbol"].count()
                .rename("near_high_count")
                if "Near 52W High" in result_s.columns else pd.Series(dtype=int)
            )
            sector_rows = []
            for _, r in sec.iterrows():
                sname = str(r.get("Sector", "")).strip()
                if not sname:
                    continue
                sc = int(r.get("Stocks", 0) or 0)
                su = int(r.get("Stocks Up", 0) or 0)
                sd = int(r.get("Stocks Down", 0) or 0)
                nh = int(near_counts.get(sname, 0))
                pn = round(nh / sc * 100, 1) if sc else None
                sector_rows.append((
                    trade_date, sname, sc, su, sd,
                    _sf(r.get("% Stocks Up")),
                    _sf(r.get("Avg Trending Days")),
                    _sf(r.get("Avg 12d Change%")),
                    _sf(r.get("Avg 5d Change%")),
                    _sf(r.get("Avg Today Change%")),
                    _sf(r.get("Avg RSI 14")),
                    _sf(r.get("Avg ADX 14")),
                    _sf(r.get("Avg RS Score")),
                    _sf(r.get("Avg Momentum")),
                    nh, pn,
                ))
            await conn.executemany(
                """
                insert into sector_daily (
                    trade_date, sector,
                    stock_count, stocks_up, stocks_down, pct_stocks_up,
                    avg_trending_days, avg_chg_12d, avg_chg_5d, avg_chg_today,
                    avg_rsi_14, avg_adx_14, avg_rs_score, avg_momentum,
                    stocks_near_high, pct_near_high
                ) values
                    ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                on conflict (trade_date, sector) do update set
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
            logger.info("[engine] Sector rows: %d", len(sector_rows))

        # 4. market_calendar
        duration = round(time.monotonic() - t0, 2)
        await conn.execute(
            """
            insert into market_calendar
                (trade_date, is_trading_day, bhav_downloaded, engine_status,
                 symbol_count, engine_duration_secs, processed_at)
            values ($1, true, true, 'done', $2, $3, now())
            on conflict (trade_date) do update set
                is_trading_day       = true,
                bhav_downloaded      = true,
                engine_status        = 'done',
                symbol_count         = excluded.symbol_count,
                engine_duration_secs = excluded.engine_duration_secs,
                processed_at         = now(),
                error_message        = null
            """,
            trade_date, len(result), duration,
        )

    summary = {
        "trade_date":        str(trade_date),
        "symbols_processed": len(result),
        "duration_secs":     duration,
    }
    logger.info("[engine] Complete — %s", summary)
    return summary