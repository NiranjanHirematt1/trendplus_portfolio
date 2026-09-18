#!/usr/bin/env python3
"""
scripts/run_engine_cli.py
──────────────────────────
The whole daily job, unattended. Replaces the manual routine of

    download bhavcopy → python scripts/backfill_to_supabase.py
                      → python scripts/compute_today.py

Steps
  1. latest = newest 'done' date in Supabase (v_latest_date)
  2. Download the bhavcopy published under today's URL      (scripts/nse_bhav.py)
  3. Read the date printed INSIDE the file (DATE1)
  4. file date <= latest  → nothing new (holiday / stale / already loaded) → exit 0
     file date >  latest  → upsert price_history, compute_today, Cup & Handle
                            — all stored under the FILE's date

Why the file's date: on some holidays NSE serves the previous session's file
under today's URL (14-Sep-2026 returned 11-Sep data). Comparing the file's own
date with the database means holidays need no special handling at all, and a
day's prices can never be written under the wrong date.

There is no catch-up sweep. A day NSE never published by the last run is
recovered manually: run the workflow with trade_date=YYYY-MM-DD.

Any real failure writes engine_status='error' with the actual message into
market_calendar and exits non-zero, so GitHub emails you.

Environment
  DATABASE_URL   Supabase PostgreSQL connection string   (required)
  TRADE_DATE     YYYY-MM-DD manual backfill of one date  (optional)
  FORCE_RUN      1 = recompute even if already 'done'    (optional)
  NSE_ATTEMPTS   download attempts, default 4            (optional)
  NSE_WAIT_SECS  seconds between attempts, default 90    (optional)
"""
import asyncio
import datetime
import logging
import os
import sys
import time
from pathlib import Path

ROOT    = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
SCRIPTS = ROOT / "scripts"
for p in (str(ROOT), str(BACKEND), str(SCRIPTS)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dotenv import load_dotenv
load_dotenv(BACKEND / ".env")
load_dotenv(ROOT / ".env")

import asyncpg
import pandas as pd

from nse_bhav import (
    BhavBlocked,
    MIN_TOTAL_TRADES,
    fetch_bhav,
    ist_today,
    normalise_bhav,
    parse_bhav_date,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("run_engine_cli")

DATABASE_URL = os.environ.get("DATABASE_URL", "")
BATCH_SIZE   = 2000


def _sf(val):
    """Safe float — None instead of NaN, so asyncpg writes SQL NULL."""
    try:
        v = float(val)
        return None if v != v else v
    except (TypeError, ValueError):
        return None


def _si(val):
    try:
        v = float(val)
        return None if v != v else int(v)
    except (TypeError, ValueError):
        return None


# ════════════════════════════════════════════════════════════════════
#  market_calendar helpers
# ════════════════════════════════════════════════════════════════════

async def already_done(conn, trade_date: datetime.date) -> bool:
    row = await conn.fetchrow(
        "select engine_status, symbol_count from market_calendar where trade_date = $1",
        trade_date,
    )
    return bool(row and row["engine_status"] == "done" and (row["symbol_count"] or 0) > 0)


async def latest_done_date(conn) -> datetime.date | None:
    """Newest fully computed trading day — the same view the website reads."""
    return await conn.fetchval("select trade_date from v_latest_date")


async def mark_error(trade_date: datetime.date, message: str) -> None:
    """Best-effort error record. Never raises — we are already failing."""
    if not DATABASE_URL:
        return
    try:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=2, timeout=30)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                insert into market_calendar (trade_date, engine_status, error_message)
                values ($1, 'error', $2)
                on conflict (trade_date) do update set
                    engine_status = 'error',
                    error_message = excluded.error_message
                """,
                trade_date, message[:1000],
            )
        await pool.close()
    except Exception as e:
        logger.warning("Could not record the error in market_calendar: %s", e)


# ════════════════════════════════════════════════════════════════════
#  STEP 2 — price_history upsert
# ════════════════════════════════════════════════════════════════════

async def _ensure_symbols(conn, symbols: list[str]) -> list[str]:
    """price_history has an FK → symbols, so every symbol must exist first.

    Returns the symbols that were newly created (new listings), so we can
    enrich them with a company name instead of leaving them blank.
    """
    rows = await conn.fetch(
        """
        insert into symbols (symbol)
        select distinct unnest($1::text[])
        on conflict (symbol) do nothing
        returning symbol
        """,
        symbols,
    )
    return [r["symbol"] for r in rows]


async def _enrich_new_symbols(conn, new_symbols: list[str]) -> None:
    """Fill company_name / ISIN for brand-new listings from master_data.

    Non-fatal: a new listing with a blank name is cosmetic, and the weekly
    master refresh picks it up anyway.
    """
    if not new_symbols:
        return
    try:
        row = await conn.fetchrow(
            "select content from master_data where key = 'EQUITY_L'"
        )
        if not row:
            return
        import io as _io
        m = pd.read_csv(_io.StringIO(row["content"]), dtype=str)
        m.columns = m.columns.str.strip().str.upper()
        name_col = next((c for c in ("NAME OF COMPANY", "COMPANY_NAME")
                         if c in m.columns), None)
        isin_col = next((c for c in ("ISIN NUMBER", "ISIN")
                         if c in m.columns), None)
        if "SYMBOL" not in m.columns or not name_col:
            return
        m["SYMBOL"] = m["SYMBOL"].str.strip()
        wanted = set(new_symbols)
        payload = [
            (r["SYMBOL"],
             str(r[name_col]).strip(),
             str(r[isin_col]).strip() if isin_col and pd.notna(r.get(isin_col)) else "")
            for _, r in m.iterrows()
            if r["SYMBOL"] in wanted and pd.notna(r[name_col])
        ]
        if not payload:
            return
        await conn.executemany(
            """
            update symbols set company_name = $2, isin = $3, updated_at = now()
            where symbol = $1
              and (company_name is null or company_name = '' or company_name = symbol)
            """,
            payload,
        )
        logger.info("Enriched %d of %d new listings from master_data",
                    len(payload), len(new_symbols))
    except Exception as e:
        logger.warning("New-symbol enrichment skipped (non-fatal): %s", e)


async def upload_bhav_to_supabase(conn, df: pd.DataFrame,
                                  trade_date: datetime.date) -> int:
    """Upsert one day of prices. Mirrors backfill_to_supabase.build_price_rows.

    open_price comes from the file's OPEN column. The old version of this script
    wrote close_price into open_price ("bhav has no OPEN") — true of the endpoint
    it used, but not of sec_bhavdata_full. Every CI-loaded day therefore had
    open == close, so any open-based signal was meaningless for those dates.
    """
    symbols = [str(s).strip() for s in df["SYMBOL"]]
    new_symbols = await _ensure_symbols(conn, symbols)
    if new_symbols:
        logger.info("New symbols created: %d (%s%s)", len(new_symbols),
                    ", ".join(new_symbols[:8]),
                    " ..." if len(new_symbols) > 8 else "")
    await _enrich_new_symbols(conn, new_symbols)

    has_prev = "PREVCLOSE" in df.columns

    # Fall back to the last stored close only where the file has no PREVCLOSE.
    prev_map: dict[str, float] = {}
    if not has_prev or bool(df["PREVCLOSE"].isna().any()):
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

    rows = []
    for r in df.itertuples(index=False):
        sym = str(r.SYMBOL).strip()
        prev = _sf(getattr(r, "PREVCLOSE", None)) if has_prev else None
        if prev is None:
            prev = prev_map.get(sym)
        rows.append((
            sym,
            trade_date,
            _sf(r.OPEN),
            _sf(r.HIGH),
            _sf(r.LOW),
            _sf(r.CLOSE),
            _si(getattr(r, "TOTTRDQTY", None)),
            _si(getattr(r, "TOTALTRADES", None)),
            prev,
        ))

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
                volume       = excluded.volume,
                total_trades = excluded.total_trades,
                prev_close   = excluded.prev_close
            """,
            rows[start:start + BATCH_SIZE],
        )

    logger.info("price_history: %d rows upserted for %s", len(rows), trade_date)
    return len(rows)


# ════════════════════════════════════════════════════════════════════
#  STEP 3 — compute_today (reads everything from Supabase)
# ════════════════════════════════════════════════════════════════════

async def run_compute_today(pool, trade_date: datetime.date) -> dict:
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
    return await compute_and_upsert_today(
        pool, hist, nse_master, sec_master, trade_date,
        return_excel=False,          # no Excel in CI — saves minutes
    )


# ════════════════════════════════════════════════════════════════════
#  MAIN
# ════════════════════════════════════════════════════════════════════

async def load_and_compute(pool, trade_date: datetime.date,
                           csv_bytes: bytes, source: str) -> None:
    """Upsert one day of prices and run every computation for it."""
    t0 = time.monotonic()

    df = normalise_bhav(csv_bytes, trade_date, source)
    logger.info("Bhavcopy ready: %d liquid EQ/BE rows (TOTALTRADES >= %d)",
                len(df), MIN_TOTAL_TRADES)

    async with pool.acquire() as conn:
        await upload_bhav_to_supabase(conn, df, trade_date)

    summary = await run_compute_today(pool, trade_date)

    # ── Cup & Handle (non-fatal) ─────────────────────────────────────
    try:
        from app.services.cup_handle_scan import run_cup_handle_scan
        logger.info("Cup & Handle: %s", await run_cup_handle_scan(pool, trade_date))
    except Exception as e:
        logger.exception("Cup & Handle scan failed (non-fatal): %s", e)

    logger.info("=" * 58)
    logger.info("  DONE  |  %s  |  %d symbols  |  %.1fs",
                trade_date, summary["symbols"], time.monotonic() - t0)
    logger.info("=" * 58)


async def run(requested: datetime.date, manual: bool, force: bool) -> str:
    """Returns a short outcome word for the log. Raises on real failure.

    Scheduled (manual=False): load the file only if its date is NEWER than
      the latest 'done' date in Supabase. Same or older → exit, no writes.
    Manual (TRADE_DATE set): backfill exactly that date. The file must carry
      that date (else it was a holiday); skipped if already done unless force.
    """
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")

    pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, command_timeout=300,
        max_inactive_connection_lifetime=60,
        server_settings={"statement_timeout": "0"},
    )
    try:
        async with pool.acquire() as conn:
            latest = await latest_done_date(conn)

        logger.info("=" * 58)
        logger.info("  TrendPulse — Daily Engine Run  |  requested %s  |  latest in DB %s%s",
                    requested, latest, "  |  MANUAL" if manual else "")
        logger.info("=" * 58)

        # ── Cheap exit: today is already loaded (the 18:17 / 19:17 runs) ──
        if not manual and not force and latest is not None and latest >= requested:
            logger.info("Latest date in DB (%s) is already %s — nothing to do.",
                        latest, requested)
            return "up_to_date"

        if manual and not force:
            async with pool.acquire() as conn:
                if await already_done(conn, requested):
                    logger.info("%s is already done — skipping (use force to recompute).",
                                requested)
                    return "up_to_date"

        # ── Download ─────────────────────────────────────────────────
        res = fetch_bhav(requested,
                         attempts=int(os.environ.get("NSE_ATTEMPTS", "4")),
                         wait_secs=int(os.environ.get("NSE_WAIT_SECS", "90")))
        if res.outcome != "ok":
            logger.info("No file for %s: %s — exiting, nothing written.",
                        requested, res.reason)
            return res.outcome

        # ── The file's own date decides everything ───────────────────
        file_date = parse_bhav_date(res.csv_bytes, res.source)
        logger.info("Requested %s  →  file is dated %s", requested, file_date)

        if manual:
            if file_date != requested:
                logger.info("NSE served %s data for %s — %s was not a trading day. "
                            "Nothing written.", file_date, requested, requested)
                return "not_trading_day"
        else:
            if latest is not None and file_date <= latest:
                logger.info("File date %s is not newer than latest %s "
                            "(holiday / stale file / already loaded) — "
                            "no update, no compute.", file_date, latest)
                return "no_new_data"
            if file_date > requested:
                raise RuntimeError(f"File is dated {file_date}, which is after the "
                                   f"requested {requested} — refusing to load.")

        await load_and_compute(pool, file_date, res.csv_bytes, res.source)
        return "done"
    finally:
        await pool.close()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="TrendPulse daily engine run")
    ap.add_argument("--date", default=os.environ.get("TRADE_DATE", "").strip(),
                    help="YYYY-MM-DD manual backfill (default: today in IST, scheduled mode)")
    ap.add_argument("--force", action="store_true",
                    default=os.environ.get("FORCE_RUN", "").strip()
                            in ("1", "true", "yes"),
                    help="recompute even if the date is already 'done'")
    args = ap.parse_args()

    manual = bool(args.date)
    requested = datetime.date.fromisoformat(args.date) if manual else ist_today()

    try:
        outcome = asyncio.run(run(requested, manual, args.force))
        logger.info("Outcome: %s", outcome)
        return 0
    except BhavBlocked as e:
        logger.error("BHAVCOPY DOWNLOAD BLOCKED — %s", e)
        logger.error("Data for %s was NOT loaded. Re-run the workflow later, or with "
                     "trade_date=%s once NSE is reachable.", requested, requested)
        asyncio.run(mark_error(requested, f"bhav download blocked: {e}"))
        return 2
    except Exception as e:
        logger.exception("Daily run failed: %s", e)
        asyncio.run(mark_error(requested, f"{type(e).__name__}: {e}"))
        return 1


if __name__ == "__main__":
    sys.exit(main())