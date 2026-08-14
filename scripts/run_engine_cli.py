#!/usr/bin/env python3
"""
scripts/run_engine_cli.py
──────────────────────────
Called by GitHub Actions every weekday at 4:15 PM IST.

Mirrors the exact manual workflow:
  1. Download today's bhav CSV from NSE into memory
  2. Upload it to price_history in Supabase
     (same as running backfill_to_supabase.py for one day)
  3. Run compute_today logic
     (same as running compute_today.py manually)

Only ONE secret needed:
  DATABASE_URL    Supabase PostgreSQL connection string
"""
import asyncio
import datetime
import io
import logging
import os
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT    = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")

import asyncpg
import httpx
import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_engine_cli")

DATABASE_URL  = os.environ.get("DATABASE_URL", "")
ALLOWED_SERIES = {"EQ", "BE"}
BATCH_SIZE     = 2000

# ── NSE download settings ─────────────────────────────────────────────
NSE_HOME     = "https://www.nseindia.com"
NSE_BHAV_URL = (
    "https://www.nseindia.com/api/reports"
    "?archives=%5B%7B%22name%22%3A%22CM%20-%20Bhavcopy(csv)%22"
    "%2C%22type%22%3A%22archives%22%2C%22category%22%3A%22capital-market%22"
    "%2C%22section%22%3A%22equities%22%7D%5D"
    "&date={date_str}&type=equities&mode=single"
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/market-data/bhavcopy-eod",
}


# ════════════════════════════════════════════════════════════════════
#  STEP 1 — Download bhav bytes from NSE
# ════════════════════════════════════════════════════════════════════

async def download_bhav_bytes(trade_date: datetime.date) -> bytes | None:
    """
    Download today's bhav CSV from NSE into memory.
    Returns raw CSV bytes, or None if market was closed / holiday.
    """
    date_str = trade_date.strftime("%d%b%Y").upper()   # e.g. 28MAR2026
    url      = NSE_BHAV_URL.format(date_str=date_str)

    logger.info("Downloading bhav for %s from NSE...", trade_date)

    try:
        async with httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
            timeout=30,
        ) as client:
            # Hit homepage first to get session cookie
            await client.get(NSE_HOME)
            await asyncio.sleep(1)

            r = await client.get(url)

        if r.status_code == 404:
            logger.warning("NSE returned 404 — market closed or holiday for %s", trade_date)
            return None

        if r.status_code != 200 or len(r.content) < 500:
            logger.warning("NSE returned %d / %d bytes — treating as holiday",
                           r.status_code, len(r.content))
            return None

        content = r.content

        # NSE sometimes returns a zip — extract the CSV
        if content[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(content)) as zf:
                csv_names = [n for n in zf.namelist()
                             if n.upper().endswith(".CSV")]
                if not csv_names:
                    logger.error("Zip has no CSV inside: %s", zf.namelist())
                    return None
                content = zf.read(csv_names[0])

        logger.info("Bhav downloaded: %d bytes", len(content))
        return content

    except Exception as e:
        logger.exception("Bhav download failed: %s", e)
        return None


# ════════════════════════════════════════════════════════════════════
#  STEP 2 — Parse bhav bytes and upload to price_history
# ════════════════════════════════════════════════════════════════════

def _sf(val):
    try:
        v = float(val)
        return None if v != v else v
    except (TypeError, ValueError):
        return None

def _si(val):
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


async def upload_bhav_to_supabase(
    conn,
    bhav_bytes: bytes,
    trade_date: datetime.date,
) -> int:
    """
    Parse bhav bytes, filter EQ+BE, upsert into price_history.
    Returns number of rows upserted.
    Same logic as backfill_to_supabase.py but for one day only.
    """
    try:
        df = pd.read_csv(io.StringIO(bhav_bytes.decode("utf-8")), dtype=str)
    except Exception as e:
        raise RuntimeError(f"Could not parse bhav CSV: {e}") from e

    df.columns = df.columns.str.strip().str.upper()

    # Normalize column names — handle both old and new NSE formats
    rename_map = {
        "HIGH_PRICE":    "HIGH",
        "LOW_PRICE":     "LOW",
        "CLOSE_PRICE":   "CLOSE",
        "OPEN_PRICE":    "OPEN",
        "TTL_TRD_QNTY":  "TOTTRDQTY",
        "NO_OF_TRADES":  "TOTALTRADES",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items()
                             if k in df.columns})

    required = {"SYMBOL", "SERIES", "HIGH", "LOW", "CLOSE"}
    if not required.issubset(df.columns):
        raise RuntimeError(
            f"Bhav CSV missing columns: {required - set(df.columns)}\n"
            f"Available columns: {list(df.columns)}"
        )

    df["SERIES"] = df["SERIES"].str.strip().str.upper()
    df = df[df["SERIES"].isin(ALLOWED_SERIES)].copy()
    if df.empty:
        raise RuntimeError("Bhav CSV has no EQ or BE rows after filtering")

    for col in ["HIGH", "LOW", "CLOSE"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "TOTTRDQTY" in df.columns:
        df["TOTTRDQTY"] = pd.to_numeric(df["TOTTRDQTY"], errors="coerce")
    if "TOTALTRADES" in df.columns:
        df["TOTALTRADES"] = pd.to_numeric(df["TOTALTRADES"], errors="coerce")

    logger.info("Bhav parsed: %d EQ+BE rows", len(df))

    # Get previous close for prev_close column
    prev_rows = await conn.fetch(
        """
        select distinct on (symbol) symbol, close_price
        from price_history
        where trade_date < $1
        order by symbol, trade_date desc
        """,
        trade_date,
    )
    prev_map = {r["symbol"]: r["close_price"] for r in prev_rows}

    # Build price_history rows
    rows = []
    for _, r in df.iterrows():
        sym = str(r["SYMBOL"]).strip()
        rows.append((
            sym,
            trade_date,
            _sf(r.get("CLOSE")),    # open proxy — bhav has no OPEN
            _sf(r.get("HIGH")),
            _sf(r.get("LOW")),
            _sf(r.get("CLOSE")),
            _si(r.get("TOTTRDQTY"))   if "TOTTRDQTY"   in r.index else None,
            _si(r.get("TOTALTRADES")) if "TOTALTRADES" in r.index else None,
            prev_map.get(sym),
        ))

    # Upsert symbols first (price_history has FK → symbols)
    sym_rows = [(str(r["SYMBOL"]).strip(),) for _, r in df.iterrows()]
    await conn.executemany(
        """
        insert into symbols (symbol)
        values ($1)
        on conflict (symbol) do nothing
        """,
        sym_rows,
    )

    # Upsert price_history
    for start in range(0, len(rows), BATCH_SIZE):
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
            rows[start : start + BATCH_SIZE],
        )

    logger.info("price_history upserted: %d rows for %s", len(rows), trade_date)
    return len(rows)


# ════════════════════════════════════════════════════════════════════
#  STEP 3 — Run compute_today logic (reads from Supabase)
# ════════════════════════════════════════════════════════════════════

async def run_compute_today(pool, trade_date: datetime.date) -> dict:
    """
    Calls compute_today.py's compute_and_upsert_today directly.
    This is exactly what you run manually — no duplication.
    """
    # Import compute_today from scripts folder
    scripts_dir = str(ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)

    from compute_today import (
        load_price_history,
        load_master_from_db,
        compute_and_upsert_today,
    )

    async with pool.acquire() as conn:
        logger.info("Loading price history from Supabase...")
        hist = await load_price_history(conn, trade_date)

        logger.info("Loading master data from Supabase...")
        nse_master, sec_master = await load_master_from_db(conn)

    logger.info("Computing metrics and upserting...")
    summary = await compute_and_upsert_today(
        pool, hist, nse_master, sec_master, trade_date,
        return_excel=False,   # no Excel needed in CI — saves time
    )
    return summary


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════

async def main():
    if not DATABASE_URL:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    # Allow date override via env var (for manual/backfill runs from Actions)
    date_override = os.environ.get("TRADE_DATE", "").strip()
    if date_override:
        today = datetime.date.fromisoformat(date_override)
        logger.info("Date override: %s", today)
    else:
        today = datetime.date.today()

    logger.info("=" * 55)
    logger.info("  TrendPulse — Daily Engine Run  %s", today)
    logger.info("=" * 55)

    # ── Step 1: Download bhav ──────────────────────────────────────────
    bhav_bytes = await download_bhav_bytes(today)

    if bhav_bytes is None:
        logger.warning("No bhav available — marking as skipped")
        try:
            pool = await asyncpg.create_pool(
                DATABASE_URL, min_size=1, max_size=2
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    insert into market_calendar
                        (trade_date, engine_status, is_trading_day)
                    values ($1, 'skipped', false)
                    on conflict (trade_date) do update set
                        engine_status = 'skipped'
                    """,
                    today,
                )
            await pool.close()
        except Exception:
            pass
        sys.exit(0)   # exit 0 — not a CI failure, just a holiday

    # ── Step 2: Connect to DB ──────────────────────────────────────────
    logger.info("Connecting to Supabase...")
    try:
        pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=2,
            max_size=5,
            command_timeout=180,
            max_inactive_connection_lifetime=60,
        )
    except Exception as e:
        logger.error("DB connection failed: %s", e)
        sys.exit(1)

    t0 = time.monotonic()

    # ── Step 3: Upload bhav to price_history ───────────────────────────
    logger.info("Uploading bhav to price_history...")
    try:
        async with pool.acquire() as conn:
            await upload_bhav_to_supabase(conn, bhav_bytes, today)
    except Exception as e:
        logger.exception("price_history upload failed: %s", e)
        await pool.close()
        sys.exit(1)

    # ── Step 4: Run compute_today ──────────────────────────────────────
    logger.info("Running compute_today...")
    try:
        summary = await run_compute_today(pool, today)
    except Exception as e:
        logger.exception("compute_today failed: %s", e)
        await pool.close()
        sys.exit(1)

    # ── Step 5: Cup & Handle scan (non-fatal — never breaks the daily run) ─
    logger.info("Running Cup & Handle scan...")
    try:
        from app.services.cup_handle_scan import run_cup_handle_scan
        scan = await run_cup_handle_scan(pool, today)
        logger.info("Cup & Handle: %s", scan)
    except Exception as e:
        logger.exception("Cup & Handle scan failed (non-fatal): %s", e)

    elapsed = time.monotonic() - t0
    logger.info("=" * 55)
    logger.info("  DONE  |  %d symbols  |  %.1fs", summary["symbols"], elapsed)
    logger.info("=" * 55)

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())