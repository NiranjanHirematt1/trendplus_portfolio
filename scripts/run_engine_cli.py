#!/usr/bin/env python3
"""
scripts/run_engine_cli.py
──────────────────────────
The whole daily job, unattended. Replaces the manual routine of

    download bhavcopy → python scripts/backfill_to_supabase.py
                      → python scripts/compute_today.py

Steps
  1. Skip fast if this trade date is already 'done' (unless FORCE_RUN=1)
  2. Fetch the bhavcopy from NSE's static archive CDN   (scripts/nse_bhav.py)
  3. Upsert it into price_history — same filters as backfill_to_supabase.py
  4. Run compute_today's compute_and_upsert_today
  5. Cup & Handle scan (non-fatal)

Any real failure writes engine_status='error' with the actual message into
market_calendar and exits non-zero, so GitHub emails you. Only a genuine
market holiday exits 0 — and "holiday" is proven by a canary fetch, never
assumed from a failed download. See scripts/nse_bhav.py for that reasoning.

Environment
  DATABASE_URL   Supabase PostgreSQL connection string   (required)
  TRADE_DATE     YYYY-MM-DD override                     (optional)
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


async def already_settled(conn, trade_date: datetime.date) -> bool:
    """True if this date needs no further work — loaded, or a known holiday.

    Used for the trailing dates of a catch-up sweep so we don't re-probe NSE
    for every past holiday on every run. The primary target date deliberately
    uses the stricter already_done(), so a day wrongly stamped 'skipped' still
    gets another chance that evening.
    """
    row = await conn.fetchrow(
        "select engine_status, symbol_count from market_calendar where trade_date = $1",
        trade_date,
    )
    if not row:
        return False
    if row["engine_status"] == "skipped":
        return True
    return row["engine_status"] == "done" and (row["symbol_count"] or 0) > 0


async def mark_holiday(conn, trade_date: datetime.date, reason: str) -> None:
    await conn.execute(
        """
        insert into market_calendar
            (trade_date, is_trading_day, bhav_downloaded, engine_status, error_message)
        values ($1, false, false, 'skipped', $2)
        on conflict (trade_date) do update set
            is_trading_day = false,
            engine_status  = 'skipped',
            error_message  = excluded.error_message
        """,
        trade_date, reason[:1000],
    )


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

async def process_date(pool, trade_date: datetime.date, force: bool,
                       attempts: int, wait: int, is_target: bool = True) -> str:
    """Run the full pipeline for one date. Returns 'done' | 'skipped' |
    'holiday' | 'too_early'. Raises on real failure."""
    # ── Idempotency ──────────────────────────────────────────────────
    async with pool.acquire() as conn:
        settled = (await already_done(conn, trade_date) if is_target
                   else await already_settled(conn, trade_date))
        if not force and settled:
            logger.info("%s needs no work — skipping. "
                        "Use --force to recompute.", trade_date)
            return "skipped"

    # ── Fetch ────────────────────────────────────────────────────────
    res = fetch_bhav(trade_date, attempts=attempts, wait_secs=wait)

    if res.outcome == "too_early":
        logger.info("Too early for %s: %s", trade_date, res.reason)
        return "too_early"

    if res.outcome == "holiday":
        logger.info("Market holiday: %s", res.reason)
        async with pool.acquire() as conn:
            await mark_holiday(conn, trade_date, res.reason)
        return "holiday"

    t0 = time.monotonic()

    # ── Parse + load ─────────────────────────────────────────────────
    df = normalise_bhav(res.csv_bytes, trade_date, res.source)
    logger.info("Bhavcopy ready: %d liquid EQ/BE rows (TOTALTRADES >= %d)",
                len(df), MIN_TOTAL_TRADES)

    async with pool.acquire() as conn:
        await upload_bhav_to_supabase(conn, df, trade_date)

    # ── Compute ──────────────────────────────────────────────────────
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
    return "done"


def _weekdays_back(end: datetime.date, n: int) -> list[datetime.date]:
    """The n most recent weekdays ending at `end`, oldest first."""
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


async def run(target: datetime.date, force: bool, catchup: int) -> None:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")

    dates = _weekdays_back(target, catchup) if catchup > 1 else [target]

    logger.info("=" * 58)
    logger.info("  TrendPulse — Daily Engine Run  |  %s  (IST)",
                ", ".join(str(d) for d in dates))
    logger.info("=" * 58)

    pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, command_timeout=300,
        max_inactive_connection_lifetime=60,
        server_settings={"statement_timeout": "0"},
    )

    attempts = int(os.environ.get("NSE_ATTEMPTS", "4"))
    wait     = int(os.environ.get("NSE_WAIT_SECS", "90"))
    # Older dates in a catch-up sweep either exist or don't — no point waiting.
    catch_attempts, catch_wait = 1, 0

    try:
        results: dict[datetime.date, str] = {}
        failures: list[str] = []
        for d in dates:
            is_target = d == dates[-1]
            try:
                results[d] = await process_date(
                    pool, d, force,
                    attempts if is_target else catch_attempts,
                    wait if is_target else catch_wait,
                    is_target=is_target,
                )
            except BhavBlocked:
                raise            # nothing else will work either — stop now
            except Exception as e:
                # One bad older day must not stop today's data from loading.
                if is_target:
                    raise
                logger.exception("Catch-up for %s failed: %s", d, e)
                results[d] = "failed"
                failures.append(f"{d}: {type(e).__name__}: {e}")
                await mark_error(d, f"{type(e).__name__}: {e}")

        if len(dates) > 1:
            logger.info("Catch-up summary: %s",
                        "  ".join(f"{d}={v}" for d, v in results.items()))
        if failures:
            raise RuntimeError("catch-up failures — " + " | ".join(failures))
    finally:
        await pool.close()


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(description="TrendPulse daily engine run")
    ap.add_argument("--date", default=os.environ.get("TRADE_DATE", "").strip(),
                    help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--force", action="store_true",
                    default=os.environ.get("FORCE_RUN", "").strip()
                            in ("1", "true", "yes"),
                    help="recompute even if the date is already 'done'")
    ap.add_argument("--catchup", type=int,
                    default=int(os.environ.get("CATCHUP_DAYS", "1") or 1),
                    help="also process the N-1 preceding weekdays that are "
                         "not yet done (default 1 = today only)")
    args = ap.parse_args()

    today = datetime.date.fromisoformat(args.date) if args.date else ist_today()

    try:
        asyncio.run(run(today, args.force, max(1, args.catchup)))
        return 0
    except BhavBlocked as e:
        # The one failure mode the old script hid. Loud on purpose.
        logger.error("BHAVCOPY DOWNLOAD BLOCKED — %s", e)
        logger.error("This is NOT a holiday. Data for %s was NOT loaded.", today)
        logger.error("Recover by running this script with TRADE_DATE=%s from a "
                     "machine on a normal ISP connection.", today)
        asyncio.run(mark_error(today, f"bhav download blocked: {e}"))
        return 2
    except Exception as e:
        logger.exception("Daily run failed: %s", e)
        asyncio.run(mark_error(today, f"{type(e).__name__}: {e}"))
        return 1


if __name__ == "__main__":
    sys.exit(main())
