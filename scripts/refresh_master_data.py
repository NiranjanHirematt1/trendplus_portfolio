#!/usr/bin/env python3
"""
scripts/refresh_master_data.py
───────────────────────────────
Downloads EQUITY_L and sector/cap master data into Supabase master_data table.

Sources (all public, no login needed):
  EQUITY_L     — nsearchives.nseindia.com  (fallback: local EQUITY_L.csv)
  Sector/Cap   — nsearchives.nseindia.com  (Nifty index constituent CSVs)

Run by weekly_master_refresh.yml every Sunday.
Run manually once to seed: python scripts/refresh_master_data.py
"""
import asyncio
import io
import logging
import os
import sys
import urllib.request
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
logger = logging.getLogger("refresh_master")

# ── URLs (nsearchives — works without browser session) ─────────────────
EQUITY_L_URL = "https://nsearchives.nseindia.com/content/equities/EQUITY_L.csv"

NIFTY_URLS = {
    "Large Cap": "https://nsearchives.nseindia.com/content/indices/ind_nifty100list.csv",
    "Mid Cap":   "https://nsearchives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "Small Cap": "https://nsearchives.nseindia.com/content/indices/ind_niftysmallcap100list.csv",
    "Nifty500":  "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
}

# Local fallback paths for EQUITY_L
EQUITY_L_LOCAL = [
    ROOT / "EQUITY_L.csv",
    ROOT / "data" / "EQUITY_L.csv",
    Path(os.environ.get("NSE_MASTER_CSV", "nonexistent")),
]

# Local sector master paths — your hand-crafted CSV with all symbols
# Columns expected: Symbol, Sector, CapCategory
SECTOR_LOCAL = [
    ROOT / "nse_sector_master.csv",
    ROOT / "data" / "nse_sector_master.csv",
    Path(os.environ.get("NSE_SECTOR_MASTER", "nonexistent")),
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/",
}


# ── Smart CSV parser (skips metadata rows at top) ──────────────────────
def _parse_nifty_csv(raw: bytes, name: str) -> pd.DataFrame:
    """
    Nifty CSVs from nsearchives have header at line 0 — no metadata.
    If niftyindices.com is used instead, there are metadata rows at top.
    This handles both: scan for the line containing 'SYMBOL' or 'ISIN'.
    """
    KEYWORDS = {"SYMBOL", "ISIN", "COMPANY", "NAME", "INDUSTRY", "SECTOR", "SERIES"}

    text  = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()

    header_idx = None
    for i, line in enumerate(lines[:20]):
        parts = {p.strip().upper() for p in line.split(",") if p.strip()}
        if parts & KEYWORDS:
            header_idx = i
            logger.info("  %s: header at line %d: %r", name, i, line[:70])
            break

    if header_idx is None:
        logger.warning("  %s: no header found. First 8 lines:", name)
        for i, l in enumerate(lines[:8]):
            logger.warning("    %2d | %r", i, l)
        return pd.DataFrame()

    data_text = "\n".join(lines[header_idx:])
    try:
        df = pd.read_csv(io.StringIO(data_text), dtype=str)
        df.columns = df.columns.str.strip().str.upper()
        df = df.dropna(how="all")
        if len(df) < 5:
            raise ValueError(f"Only {len(df)} rows — likely parse error")
        logger.info("  %s: %d rows, cols: %s", name, len(df), list(df.columns))
        return df
    except Exception as e:
        logger.warning("  %s parse failed: %s", name, e)
        return pd.DataFrame()


def _col(df: pd.DataFrame, candidates: list) -> str:
    for c in candidates:
        if c in df.columns:
            return c
    return ""


# ── EQUITY_L ───────────────────────────────────────────────────────────
def load_equity_l() -> tuple[str, int]:
    """
    Try nsearchives download first.
    If that fails, fall back to local EQUITY_L.csv file.
    """
    # 1. Try nsearchives (usually works, no session needed)
    try:
        logger.info("Downloading EQUITY_L from nsearchives...")
        req = urllib.request.Request(EQUITY_L_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as resp:
            text = resp.read().decode("utf-8", errors="replace")
        rows = [l for l in text.splitlines() if l.strip()]
        if len(rows) > 500:
            logger.info("EQUITY_L downloaded: %d data rows", len(rows) - 1)
            return text, len(rows) - 1
        logger.warning("EQUITY_L download returned only %d rows — trying local", len(rows))
    except Exception as e:
        logger.warning("EQUITY_L download failed: %s — trying local file", e)

    # 2. Local file fallback
    for p in EQUITY_L_LOCAL:
        if p.exists():
            logger.info("Reading EQUITY_L from local: %s", p)
            text = p.read_text(encoding="utf-8", errors="replace")
            rows = [l for l in text.splitlines() if l.strip()]
            logger.info("EQUITY_L local: %d data rows", len(rows) - 1)
            return text, len(rows) - 1

    raise FileNotFoundError(
        "EQUITY_L.csv not available.\n"
        "Place it at: trendplus/EQUITY_L.csv\n"
        "Or set:      NSE_MASTER_CSV=C:\\path\\to\\EQUITY_L.csv\n"
        "Download:    https://www.nseindia.com → Market Data → Equity → EQUITY_L.csv"
    )


# ── SECTOR MASTER ──────────────────────────────────────────────────────
def load_sector_master_local() -> tuple[str, int] | None:
    """
    Load your local nse_sector_master.csv first.
    This has more symbols than Nifty500 and matches what the Excel used.
    Returns (csv_text, row_count) or None if not found.
    """
    for p in SECTOR_LOCAL:
        if p and Path(p).exists():
            logger.info("Reading sector master from local: %s", p)
            text = Path(p).read_text(encoding="utf-8", errors="replace")
            rows = [l for l in text.splitlines() if l.strip()]
            logger.info("Local sector master: %d symbols", len(rows) - 1)
            return text, len(rows) - 1
    return None


async def download_sector_master() -> tuple[str, int]:
    logger.info("Downloading Nifty index CSVs...")

    raw: dict[str, bytes | None] = {}
    async with httpx.AsyncClient(
        headers=_HEADERS, follow_redirects=True, timeout=30
    ) as client:
        for cap, url in NIFTY_URLS.items():
            try:
                r = await client.get(url)
                r.raise_for_status()
                raw[cap] = r.content
                logger.info("  %-12s %d bytes", cap, len(r.content))
            except Exception as e:
                logger.warning("  %-12s FAILED: %s", cap, e)
                raw[cap] = None
            await asyncio.sleep(0.5)

    parsed = {
        cap: _parse_nifty_csv(b, cap)
        for cap, b in raw.items()
        if b is not None
    }

    rows: dict[str, dict] = {}

    # Nifty500 first — gives sector for all 500. Cap starts EMPTY, not "Other":
    # symbols.cap_category only accepts Large/Mid/Small Cap or "", so "Other"
    # violates the CHECK constraint and kills the nightly symbols upsert.
    # The ~150 Nifty500 names in none of the three cap indices stay "" = unknown.
    n500 = parsed.get("Nifty500", pd.DataFrame())
    if not n500.empty:
        sym_col = _col(n500, ["SYMBOL", "TICKER"])
        sec_col = _col(n500, ["INDUSTRY", "SECTOR", "SUB-SECTOR"])
        for _, r in n500.iterrows():
            sym = str(r.get(sym_col, "")).strip().upper() if sym_col else ""
            sec = str(r.get(sec_col, "")).strip()         if sec_col else ""
            if sym and sym != "NAN":
                rows[sym] = {"SYMBOL": sym, "SECTOR": sec, "CAP_CATEGORY": ""}
        logger.info("Nifty500 base: %d symbols", len(rows))

    # Assign cap — higher priority overwrites
    for cap in ["Small Cap", "Mid Cap", "Large Cap"]:
        df = parsed.get(cap, pd.DataFrame())
        if df.empty:
            logger.warning("No usable data for %s", cap)
            continue
        sym_col = _col(df, ["SYMBOL", "TICKER"])
        sec_col = _col(df, ["INDUSTRY", "SECTOR"])
        if not sym_col:
            logger.warning("%s: no symbol column in %s", cap, list(df.columns))
            continue
        count = 0
        for _, r in df.iterrows():
            sym = str(r.get(sym_col, "")).strip().upper()
            sec = str(r.get(sec_col, "")).strip() if sec_col else ""
            if not sym or sym == "NAN":
                continue
            if sym not in rows:
                rows[sym] = {"SYMBOL": sym, "SECTOR": sec, "CAP_CATEGORY": cap}
            else:
                rows[sym]["CAP_CATEGORY"] = cap
                if sec and not rows[sym].get("SECTOR"):
                    rows[sym]["SECTOR"] = sec
            count += 1
        logger.info("Assigned %-12s %d symbols", cap, count)

    if not rows:
        raise RuntimeError("All Nifty CSV parses failed — no sector data built")

    result = pd.DataFrame(list(rows.values()))[["SYMBOL", "SECTOR", "CAP_CATEGORY"]]
    logger.info("Sector master: %d symbols total", len(result))
    for cap, cnt in result["CAP_CATEGORY"].value_counts().items():
        logger.info("  %-12s %d", cap, cnt)

    return result.to_csv(index=False), len(result)


# ── DB UPSERT ──────────────────────────────────────────────────────────
async def upsert(pool, key: str, content: str, row_count: int, source_url: str):
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO master_data (key, content, row_count, source_url, updated_at)
            VALUES ($1, $2, $3, $4, now())
            ON CONFLICT (key) DO UPDATE SET
                content    = excluded.content,
                row_count  = excluded.row_count,
                source_url = excluded.source_url,
                updated_at = now()
            """,
            key, content, row_count, source_url,
        )
    logger.info("✓ Upserted %-15s rows: %d", key, row_count)


# ── MAIN ───────────────────────────────────────────────────────────────
async def main():
    db_url = os.environ.get("DATABASE_URL", "")
    if not db_url:
        try:
            from backend.app.core.config import settings
            db_url = settings.DATABASE_URL
            
        except Exception:
            pass
    if not db_url:
        logger.error("DATABASE_URL not set")
        sys.exit(1)

    logger.info("=" * 55)
    logger.info("  TrendPulse — Master Data Refresh")
    logger.info("=" * 55)

    errors  = []
    equity_text, equity_rows = None, 0
    sector_text, sector_rows = None, 0

    try:
        equity_text, equity_rows = load_equity_l()
    except Exception as e:
        logger.error("EQUITY_L: %s", e)
        errors.append("EQUITY_L")

    # Try local nse_sector_master.csv first (has more symbols than Nifty500)
    local_sector = load_sector_master_local()
    if local_sector:
        sector_text, sector_rows = local_sector
        logger.info("Using local sector master: %d symbols", sector_rows)
    else:
        try:
            sector_text, sector_rows = await download_sector_master()
        except Exception as e:
            logger.error("SECTOR_MASTER: %s", e)
            errors.append("SECTOR_MASTER")

    if not equity_text and not sector_text:
        logger.error("Nothing to upload — aborting")
        sys.exit(1)

    logger.info("Connecting to Supabase...")
    pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3, command_timeout=60)

    if equity_text:
        await upsert(pool, "EQUITY_L", equity_text, equity_rows, EQUITY_L_URL)
    if sector_text:
        await upsert(pool, "SECTOR_MASTER", sector_text, sector_rows, NIFTY_URLS["Nifty500"])

    await pool.close()

    logger.info("=" * 55)
    if errors:
        logger.warning("  Partial — skipped: %s", ", ".join(errors))
    else:
        logger.info("  Complete ✓")
    logger.info("=" * 55)

    if len(errors) == 2:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())