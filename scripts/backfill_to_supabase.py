#!/usr/bin/env python3
"""
scripts/backfill_to_supabase.py
────────────────────────────────
ONE-TIME script. Run this ONCE to load all your local bhav CSV files
into Supabase price_history. After it completes successfully you can
delete your local CSV folder — the engine never needs local files again.

What it does
────────────
  1. Reads every YYYYMMDD_NSE.csv from LOCAL_BHAV_FOLDER
  2. Filters EQ + BE series
  3. Bulk-upserts every OHLCV row into price_history
  4. Also populates/updates the symbols table with ISIN

Also reads EQUITY_L.csv and nse_sector_master.csv and stores them in
the master_data table in Supabase so the engine can read them from DB
instead of from disk.

Usage
─────
  1. Fill in the three paths at the top of this file
  2. Make sure DATABASE_URL is in backend/.env (or set as env var)
  3. Run from repo root:

       python scripts/backfill_to_supabase.py

  4. Wait ~5-10 minutes for 252 files × 2143 symbols
  5. Delete LOCAL_BHAV_FOLDER when done

Environment
───────────
  DATABASE_URL   — Supabase PostgreSQL connection string
                   (from backend/.env or set in shell)
"""

import asyncio
import logging
import os
import re
import sys
import time
from pathlib import Path

import asyncpg
import numpy as np
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
# Make script runnable both from repo root and as a direct script path
for candidate in (ROOT, ROOT / "backend"):
    c = str(candidate)
    if c not in sys.path:
        sys.path.insert(0, c)

from backend.app.core.sector_mapping import normalize_sector_name

# ─────────────────────────────────────────────────────────────────────
#  CONFIGURATION — set these three paths before running
# ─────────────────────────────────────────────────────────────────────
LOCAL_BHAV_FOLDER  = r"C:\Users\user-pc\Downloads\200_Bhav"   # folder with YYYYMMDD_NSE.csv
NSE_MASTER_CSV     = r"C:\Users\user-pc\Downloads\EQUITY_L.csv"
NSE_SECTOR_MASTER  = r"C:\Users\user-pc\Downloads\nse_sector_master.csv"
# ─────────────────────────────────────────────────────────────────────

ALLOWED_SERIES = {"EQ", "BE"}
BATCH_SIZE     = 2000   # rows per executemany call

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("backfill")

load_dotenv(ROOT / ".env")

DATABASE_URL = os.environ.get("DATABASE_URL", "")


# ─────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────

def _sf(val):
    """Safe float — returns None instead of NaN."""
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


MIN_TOTAL_TRADES = 3000   # only rows with TOTALTRADES >= this are stored


def load_csv_file(fpath: str, trade_date: pd.Timestamp) -> pd.DataFrame:
    """Read one bhav CSV, filter series + liquidity, return clean DataFrame.

    Key fixes vs original:
      • Reads OPEN column (original silently fell back to CLOSE as the open proxy,
        causing every open_price in Supabase to equal close_price).
      • Validates the in-file TIMESTAMP against the filename date and RAISES if
        they disagree — prevents silent insertion of wrong-date data.
      • Applies TOTALTRADES >= MIN_TOTAL_TRADES filter here so dirty rows are
        never sent to Supabase.
      • Keeps PREVCLOSE so we can store a real prev_close value instead of NULL.
    """
    try:
        df = pd.read_csv(fpath, dtype=str)
    except Exception as e:
        log.warning("Cannot read %s: %s", fpath, e)
        return pd.DataFrame()

    df.columns = df.columns.str.strip().str.upper()

    required = {"SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE"}
    missing = required - set(df.columns)
    if missing:
        log.warning("%s missing required columns %s — skipped", fpath, missing)
        return pd.DataFrame()

    # ── Validate in-file TIMESTAMP against filename date ──────────────
    # The CSV has a TIMESTAMP column like "19-May-2026".  The filename is
    # "20260519_NSE.csv".  If they disagree we must NOT import the file —
    # importing it under the wrong date would corrupt every indicator that
    # uses price history.
    if "TIMESTAMP" in df.columns:
        sample_ts = df["TIMESTAMP"].dropna().iloc[0] if not df["TIMESTAMP"].dropna().empty else None
        if sample_ts:
            try:
                parsed_ts = pd.to_datetime(sample_ts, dayfirst=True).date()
                if parsed_ts != trade_date.date():
                    raise ValueError(
                        f"TIMESTAMP in file ({parsed_ts}) does not match "
                        f"filename date ({trade_date.date()}).  "
                        f"File: {fpath}  — ABORTED to prevent data corruption."
                    )
            except ValueError:
                raise   # re-raise the date mismatch error
            except Exception as e:
                log.warning("Could not parse TIMESTAMP '%s' in %s: %s — continuing", sample_ts, fpath, e)

    # ── Series filter ─────────────────────────────────────────────────
    df["SERIES"] = df["SERIES"].str.strip().str.upper()
    df = df[df["SERIES"].isin(ALLOWED_SERIES)].copy()
    if df.empty:
        return df

    # ── Numeric conversion ────────────────────────────────────────────
    for col in ["OPEN", "HIGH", "LOW", "CLOSE"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    if "PREVCLOSE" in df.columns:
        df["PREVCLOSE"] = pd.to_numeric(df["PREVCLOSE"], errors="coerce")
    if "TOTTRDQTY" in df.columns:
        df["TOTTRDQTY"] = pd.to_numeric(df["TOTTRDQTY"], errors="coerce")
    if "TOTALTRADES" in df.columns:
        df["TOTALTRADES"] = pd.to_numeric(df["TOTALTRADES"], errors="coerce")

    # ── Liquidity filter — only keep liquid stocks ────────────────────
    if "TOTALTRADES" in df.columns:
        before = len(df)
        df = df[df["TOTALTRADES"] >= MIN_TOTAL_TRADES].copy()
        log.debug("%s: liquidity filter removed %d rows (%d kept)",
                  fpath, before - len(df), len(df))
    else:
        log.warning("%s has no TOTALTRADES column — liquidity filter skipped", fpath)

    df["DATE"] = trade_date
    return df


def build_price_rows(df: pd.DataFrame, trade_date) -> list[tuple]:
    """
    Convert a single day's DataFrame into rows ready for price_history upsert.

    Key fixes vs original:
      • open_price now comes from the OPEN column (the original used CLOSE as a
        proxy for open, so every row had open_price == close_price).
      • prev_close is populated from PREVCLOSE when available instead of always
        being NULL — this gives the engine accurate daily-change data from day 1.
    """
    rows = []
    has_open      = "OPEN"      in df.columns
    has_prevclose = "PREVCLOSE" in df.columns

    for _, r in df.iterrows():
        open_px = _sf(r.get("OPEN"))  if has_open      else _sf(r.get("CLOSE"))
        prev_cl = _sf(r.get("PREVCLOSE")) if has_prevclose else None

        rows.append((
            str(r["SYMBOL"]).strip(),
            trade_date,
            open_px,                                                       # open_price
            _sf(r.get("HIGH")),                                            # high_price
            _sf(r.get("LOW")),                                             # low_price
            _sf(r.get("CLOSE")),                                           # close_price
            _si(r.get("TOTTRDQTY"))   if "TOTTRDQTY"   in r.index else None,  # volume
            _si(r.get("TOTALTRADES")) if "TOTALTRADES" in r.index else None,  # total_trades
            prev_cl,                                                       # prev_close
        ))
    return rows


async def upsert_price_batch(conn, rows: list[tuple]) -> None:
    """
    Upsert a batch of price_history rows.

    Fix vs original: prev_close is now updated on conflict (it was
    silently ignored before, so correcting a row via re-backfill would
    leave the stale prev_close in place).  Also removed the COALESCE
    trick for volume/total_trades — if the CSV has a value we want it,
    not the old DB value.
    """
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
        rows,
    )


async def upsert_symbols(conn, isin_map: pd.Series,
                         master_csv: str, sector_csv: str) -> None:
    """Populate symbols table from EQUITY_L + sector master."""
    sym_rows = []

    # Load master CSVs if available
    master_df = pd.DataFrame()
    sector_df = pd.DataFrame()

    if master_csv and Path(master_csv).exists():
        m = pd.read_csv(master_csv, dtype=str)
        m.columns = m.columns.str.strip().str.upper()
        if {"SYMBOL", "NAME OF COMPANY"}.issubset(m.columns):
            m = m.rename(columns={"NAME OF COMPANY": "COMPANY_NAME"})
            if "ISIN NUMBER" in m.columns:
                m = m.rename(columns={"ISIN NUMBER": "ISIN"})
            master_df = m[["SYMBOL"] +
                          [c for c in ["COMPANY_NAME", "ISIN"] if c in m.columns]]
            master_df = master_df.set_index("SYMBOL")
        log.info("NSE master loaded: %d symbols", len(master_df))

    if sector_csv and Path(sector_csv).exists():
        s = pd.read_csv(sector_csv, dtype=str)
        s.columns = s.columns.str.strip().str.upper()
        if "SYMBOL" in s.columns:
            sector_df = s.set_index("SYMBOL")
        log.info("Sector master loaded: %d symbols", len(sector_df))

    VALID_CAP = {"Large Cap", "Mid Cap", "Small Cap"}

    def _clean(val, fallback="") -> str:
        """Coerce any value to str, turning NaN/None/'nan' into fallback."""
        if val is None:
            return fallback
        s = str(val).strip()
        return fallback if s.lower() in ("nan", "none", "") else s

    all_symbols = isin_map.index.tolist()
    for sym in all_symbols:
        company = sym
        isin    = _clean(isin_map.get(sym, ""))
        sector  = ""
        cap     = ""

        if sym in master_df.index:
            row = master_df.loc[sym]
            company = _clean(row.get("COMPANY_NAME"), sym)
            isin    = _clean(row.get("ISIN"), isin)

        if sym in sector_df.index:
            row = sector_df.loc[sym]
            sector = normalize_sector_name(_clean(
                row.get("SECTOR") if "SECTOR" in row.index else row.iloc[0]
            ))
            for col in ["CAPCATEGORY", "CAP_CATEGORY", "CAP CATEGORY", "CAP"]:
                if col in sector_df.columns:
                    cap = _clean(sector_df.at[sym, col])
                    break

        # Enforce DB check constraint — only these three values or empty string
        if cap not in VALID_CAP:
            cap = ""

        sym_rows.append((sym, company, isin, sector, cap))

    await conn.executemany(
        """
        insert into symbols (symbol, company_name, isin, sector, cap_category)
        values ($1, $2, $3, $4, $5)
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
    log.info("Symbols upserted: %d", len(sym_rows))


async def store_master_files_in_db(conn,
                                   master_csv: str,
                                   sector_csv: str) -> None:
    """
    Store EQUITY_L.csv and nse_sector_master.csv content in the
    master_data table so the engine never needs local files again.
    """
    # Create table if it doesn't exist
    await conn.execute("""
        create table if not exists master_data (
            key        text primary key,
            content    text not null,
            updated_at timestamptz not null default now()
        )
    """)

    for key, path in [("EQUITY_L", master_csv),
                      ("SECTOR_MASTER", sector_csv)]:
        if path and Path(path).exists():
            content = Path(path).read_text(encoding="utf-8", errors="replace")
            await conn.execute(
                """
                insert into master_data (key, content)
                values ($1, $2)
                on conflict (key) do update set
                    content    = excluded.content,
                    updated_at = now()
                """,
                key, content,
            )
            log.info("Stored %s in master_data (%d chars)",
                     key, len(content))
        else:
            log.warning("File not found, skipping master_data for %s: %s",
                        key, path)


# ─────────────────────────────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────────────────────────────

async def main():
    if not DATABASE_URL:
        log.error("DATABASE_URL not set. Add it to backend/.env")
        sys.exit(1)

    bhav_folder = Path(LOCAL_BHAV_FOLDER)
    if not bhav_folder.exists():
        log.error("LOCAL_BHAV_FOLDER not found: %s", bhav_folder)
        sys.exit(1)

    # Discover all bhav files
    pattern = re.compile(r"^(\d{8})_NSE\.csv$", re.IGNORECASE)
    bhav_files = sorted(
        (f for f in bhav_folder.iterdir() if pattern.match(f.name)),
        key=lambda f: f.name,
    )
    if not bhav_files:
        log.error("No YYYYMMDD_NSE.csv files found in %s", bhav_folder)
        sys.exit(1)

    log.info("=" * 60)
    log.info("  TrendPulse — Backfill to Supabase")
    log.info("=" * 60)
    log.info("  Bhav folder : %s", bhav_folder)
    log.info("  Files found : %d", len(bhav_files))
    log.info("  First       : %s", bhav_files[0].name)
    log.info("  Last        : %s", bhav_files[-1].name)
    log.info("=" * 60)

    # Connect
    log.info("Connecting to Supabase...")
    pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=2, max_size=8, command_timeout=120,
    )

    t0 = time.monotonic()
    total_rows = 0

    async with pool.acquire() as conn:

        # ── 1. Store master files in DB ───────────────────────────────
        log.info("Storing master files in Supabase master_data table...")
        await store_master_files_in_db(conn, NSE_MASTER_CSV, NSE_SECTOR_MASTER)

        # ── 2. Scan ALL bhav files first to collect every symbol+ISIN ─
        # price_history has a FK → symbols, so symbols MUST exist first.
        log.info("Scanning all bhav files to collect symbols (pass 1/2)...")
        isin_map = {}
        for fpath in bhav_files:
            try:
                df_scan = pd.read_csv(str(fpath), dtype=str,
                                      usecols=lambda c: c.strip().upper()
                                                         in {"SYMBOL","SERIES","ISIN"})
            except Exception:
                continue
            df_scan.columns = df_scan.columns.str.strip().str.upper()
            if "SERIES" in df_scan.columns:
                df_scan = df_scan[df_scan["SERIES"].str.strip().str.upper()
                                  .isin(ALLOWED_SERIES)]
            if "ISIN" in df_scan.columns:
                for _, row in df_scan.dropna(subset=["ISIN"]).iterrows():
                    isin_map[str(row["SYMBOL"]).strip()] = str(row["ISIN"]).strip()
            else:
                for sym in df_scan["SYMBOL"].dropna().unique():
                    isin_map.setdefault(str(sym).strip(), "")

        log.info("Symbols found across all files: %d", len(isin_map))

        # ── 3. Upsert ALL symbols before touching price_history ───────
        log.info("Upserting symbols (must happen before price_history)...")
        isin_series = pd.Series(isin_map)
        await upsert_symbols(
            conn, isin_series, NSE_MASTER_CSV, NSE_SECTOR_MASTER
        )
        
       # ── 4. Fetch dates already in Supabase ───────────────────────
        log.info("Fetching already-uploaded dates from Supabase...")
        existing_dates = set()
        rows_db = await conn.fetch("select distinct trade_date from price_history")
        for row in rows_db:
            existing_dates.add(row["trade_date"])   # datetime.date objects
        log.info("Dates already in Supabase: %d — will skip these", len(existing_dates))

        log.info("Starting price_history upsert (pass 2/2 — new dates only)...")
        date_errors  = []   # collect date-mismatch filenames for final report
        for i, fpath in enumerate(bhav_files, 1):
            m    = pattern.match(fpath.name)
            dstr = m.group(1)

            try:
                tdate = pd.to_datetime(dstr, format="%Y%m%d").date()
            except ValueError as exc:
                log.error("[%d/%d] Cannot parse date from %s: %s — skipped",
                          i, len(bhav_files), fpath.name, exc)
                date_errors.append(fpath.name)
                continue

            if tdate in existing_dates:
                log.info("[%d/%d] %s — already in Supabase, skipping",
                         i, len(bhav_files), fpath.name)
                continue

            try:
                df = load_csv_file(str(fpath), pd.Timestamp(tdate))

            except ValueError as exc:
                # TIMESTAMP in file doesn't match filename — abort this file
                log.error("[%d/%d] DATE MISMATCH — skipping %s:\n  %s",
                          i, len(bhav_files), fpath.name, exc)
                date_errors.append(fpath.name)
                continue

            if df.empty:
                log.info("[%d/%d] %s — 0 rows after filtering (EQ/BE + TOTALTRADES≥%d)",
                         i, len(bhav_files), fpath.name, MIN_TOTAL_TRADES)
                continue

            rows = build_price_rows(df, tdate)
            for start in range(0, len(rows), BATCH_SIZE):
                await upsert_price_batch(conn, rows[start:start + BATCH_SIZE])

            total_rows += len(rows)
            log.info("[%d/%d] %s → %d rows (total so far: %d)",
                     i, len(bhav_files), fpath.name, len(rows), total_rows)

        if date_errors:
            log.warning("Files skipped due to date errors (%d): %s",
                        len(date_errors), date_errors)
        
    elapsed = time.monotonic() - t0

    log.info("=" * 60)
    log.info("  BACKFILL COMPLETE")
    log.info("  Files processed : %d", len(bhav_files))
    log.info("  Price rows      : %d", total_rows)
    log.info("  Time            : %.1fs", elapsed)
    log.info("=" * 60)
    log.info("")
    log.info("Next steps:")
    log.info("  1. Verify: psql $DATABASE_URL -c 'select count(*) from price_history'")
    log.info("  2. Run the engine once: python scripts/run_engine_cli.py")
    log.info("  3. Once engine run succeeds → delete LOCAL_BHAV_FOLDER")
    log.info("  4. Remove DATA_FOLDER from backend/.env and GitHub secrets")
    log.info("  5. Remove EQUITY_L_B64 and SECTOR_MASTER_B64 from GitHub secrets")

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())