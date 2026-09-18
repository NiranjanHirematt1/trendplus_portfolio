#!/usr/bin/env python3
"""
scripts/nse_bhav.py
────────────────────
Fetch + normalise NSE end-of-day bhavcopy WITHOUT a browser session.

Why this module exists
──────────────────────
The old fetch in run_engine_cli.py called

    https://www.nseindia.com/api/reports?archives=...

That endpoint sits behind NSE's bot protection (Akamai).  It needs a full
browser cookie handshake and it rejects datacentre IP ranges — i.e. exactly
what GitHub Actions runs on.  Worse, the old code treated *any* failure
(403, empty body, HTML challenge page) as "market holiday", wrote
engine_status='skipped' and exited 0.  So the daily job went green every
single day while never actually loading data.

This module instead reads the STATIC ARCHIVE CDN:

    https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv

That is a plain file on a CDN — no session, no cookies, no challenge.
It is the same host scripts/refresh_master_data.py already uses successfully.
It is also the *better* file: it carries OPEN_PRICE and PREV_CLOSE, which the
old CI path did not have (it stored close_price as a stand-in for open_price).

Holidays — trust the date INSIDE the file
─────────────────────────────────────────
On some holidays NSE does not 404 the requested URL; it serves the PREVIOUS
trading day's file under today's name (e.g. 14-Sep-2026 was a holiday and
sec_bhavdata_full_14092026.csv contained DATE1 = 11-Sep-2026).  So the URL
date means nothing.  The caller must read the file's own date with
parse_bhav_date() and only load it if that date is newer than what the
database already has.  A holiday then simply looks like "nothing new".

Blocked vs. absent
──────────────────
404 = file not there (holiday or not published yet) → outcome "no_file".
403/429/HTML challenge/tiny body = NSE refusing us → raises BhavBlocked,
which fails the build loudly.  Never confuse the two.

Public API
──────────
    fetch_bhav(trade_date)     -> BhavFetch      (raises BhavBlocked)
    parse_bhav_date(csv_bytes, source) -> datetime.date   (date printed in the file)
    normalise_bhav(csv_bytes, trade_date, source) -> pandas.DataFrame
    ist_today()                -> datetime.date
"""
from __future__ import annotations

import datetime
import io
import logging
import os
import time
import zipfile
from dataclasses import dataclass

import httpx
import pandas as pd

log = logging.getLogger("nse_bhav")

# ─────────────────────────────────────────────────────────────────────
#  Constants
# ─────────────────────────────────────────────────────────────────────

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

ARCHIVE_HOST = "https://nsearchives.nseindia.com"

# Primary: "Full Bhavcopy and Security Deliverable data".
# Has SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,
# LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,
# NO_OF_TRADES, DELIV_QTY, DELIV_PER.  Header names AND values carry leading
# spaces in this file — everything below strips.
FULL_BHAV_URL = ARCHIVE_HOST + "/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"

# Optional transport fallback: the newer UDiFF bhavcopy (zipped, different
# column names entirely).  OFF by default — set NSE_ALLOW_UDIFF=1 to enable.
# Only ever tried when the primary file 404s, so it costs nothing normally.
UDIFF_URL = ARCHIVE_HOST + "/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,application/zip,application/octet-stream,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
    "Connection": "keep-alive",
}

ALLOWED_SERIES = {"EQ", "BE"}

# Must match backfill_to_supabase.py — otherwise days loaded by CI contain
# thousands of illiquid rows that days loaded manually do not, and every
# breadth / ranking metric silently shifts depending on who loaded the day.
MIN_TOTAL_TRADES = 3000

# A real bhavcopy is ~1-2 MB.  Anything tiny is a challenge page or an error.
MIN_PLAUSIBLE_BYTES = 50_000

# Don't even hit the CDN for *today* before this hour — the file cannot exist.
EARLIEST_PUBLISH_HOUR_IST = 17


# ─────────────────────────────────────────────────────────────────────
#  Errors / result types
# ─────────────────────────────────────────────────────────────────────

class BhavBlocked(RuntimeError):
    """The archive CDN could not be read at all. Never treat as a holiday."""


@dataclass
class BhavFetch:
    outcome: str                  # "ok" | "no_file" | "too_early"
    trade_date: datetime.date
    csv_bytes: bytes | None = None
    source: str = ""              # "full" | "udiff"
    reason: str = ""              # why, when outcome != "ok"


# ─────────────────────────────────────────────────────────────────────
#  Dates
# ─────────────────────────────────────────────────────────────────────

def ist_today() -> datetime.date:
    """Today's date in IST.

    datetime.date.today() on a GitHub runner is UTC.  A job that fires after
    18:30 IST would ask NSE for tomorrow's file and get a 404, which the old
    code would then report as a holiday.
    """
    return datetime.datetime.now(IST).date()


def _is_weekend(d: datetime.date) -> bool:
    return d.weekday() >= 5          # 5 = Sat, 6 = Sun


# ─────────────────────────────────────────────────────────────────────
#  Low-level HTTP
# ─────────────────────────────────────────────────────────────────────

def _looks_like_challenge(body: bytes) -> bool:
    """True if the body is an HTML block/challenge page rather than data."""
    head = body[:512].lstrip().lower()
    return head.startswith(b"<") or b"<html" in head or b"access denied" in head


def _get(client: httpx.Client, url: str) -> bytes | None:
    """GET a URL.

    Returns bytes on success, None if the file genuinely is not there (404).
    Raises BhavBlocked for anything that smells like refusal rather than absence.
    """
    try:
        r = client.get(url)
    except httpx.HTTPError as e:
        raise BhavBlocked(f"network error for {url}: {e}") from e

    if r.status_code == 404:
        log.info("404 (file not published): %s", url)
        return None

    if r.status_code in (401, 403, 407, 429, 503):
        raise BhavBlocked(
            f"HTTP {r.status_code} from {url} — NSE is refusing this client/IP, "
            f"not a missing file."
        )

    if r.status_code != 200:
        raise BhavBlocked(f"HTTP {r.status_code} from {url}")

    body = r.content

    if _looks_like_challenge(body):
        raise BhavBlocked(
            f"{url} returned an HTML page ({len(body)} bytes) instead of a file "
            f"— bot challenge or error page."
        )

    if len(body) < MIN_PLAUSIBLE_BYTES:
        raise BhavBlocked(
            f"{url} returned only {len(body)} bytes — implausibly small for a "
            f"bhavcopy; treating as a bad response, not a holiday."
        )

    return body


def _unzip_single_csv(body: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        names = [n for n in zf.namelist() if n.upper().endswith(".CSV")]
        if not names:
            raise BhavBlocked(f"zip contains no CSV: {zf.namelist()}")
        return zf.read(names[0])


# ─────────────────────────────────────────────────────────────────────
#  Fetch
# ─────────────────────────────────────────────────────────────────────

def _try_date(client: httpx.Client, d: datetime.date) -> tuple[bytes, str] | None:
    """One attempt at one date. Returns (csv_bytes, source) or None if absent."""
    body = _get(client, FULL_BHAV_URL.format(ddmmyyyy=d.strftime("%d%m%Y")))
    if body is not None:
        return body, "full"

    if os.environ.get("NSE_ALLOW_UDIFF", "").strip() in ("1", "true", "yes"):
        log.info("Primary absent — trying UDiFF fallback for %s", d)
        body = _get(client, UDIFF_URL.format(yyyymmdd=d.strftime("%Y%m%d")))
        if body is not None:
            return _unzip_single_csv(body), "udiff"

    return None


def fetch_bhav(
    trade_date: datetime.date,
    attempts: int = 4,
    wait_secs: int = 90,
) -> BhavFetch:
    """Download the bhavcopy published under trade_date's URL.

    "ok" does NOT mean the file holds trade_date's prices — on a holiday NSE
    may serve the previous session's file.  Always check parse_bhav_date().

    Outcomes: "ok" | "no_file" (404 after all attempts, or a weekend)
              | "too_early" (today, before NSE can have published).
    Raises BhavBlocked if NSE is refusing us — the caller must fail the job.
    """
    if _is_weekend(trade_date):
        return BhavFetch(
            outcome="no_file",
            trade_date=trade_date,
            reason=f"{trade_date} is a {trade_date:%A} — market closed",
        )

    now_ist = datetime.datetime.now(IST)
    if trade_date == now_ist.date() and now_ist.hour < EARLIEST_PUBLISH_HOUR_IST:
        return BhavFetch(
            outcome="too_early",
            trade_date=trade_date,
            reason=(f"it is {now_ist:%H:%M} IST; NSE publishes "
                    f"sec_bhavdata_full in the evening — nothing to fetch yet"),
        )

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=60.0) as client:
        for attempt in range(1, attempts + 1):
            log.info("Fetching bhavcopy for %s (attempt %d/%d)...",
                     trade_date, attempt, attempts)
            got = _try_date(client, trade_date)
            if got is not None:
                body, source = got
                log.info("Bhavcopy downloaded: %d bytes (source=%s)", len(body), source)
                return BhavFetch(
                    outcome="ok", trade_date=trade_date,
                    csv_bytes=body, source=source,
                )
            if attempt < attempts:
                log.info("Not published yet — waiting %ds before retry.", wait_secs)
                time.sleep(wait_secs)

    return BhavFetch(
        outcome="no_file",
        trade_date=trade_date,
        reason=(f"no bhavcopy at the {trade_date} URL after {attempts} attempt(s) "
                f"— holiday, or NSE has not published yet"),
    )


# ─────────────────────────────────────────────────────────────────────
#  File date
# ─────────────────────────────────────────────────────────────────────

_DATE_COLS = ("DATE1", "TRADDT", "TIMESTAMP")
_DATE_FORMATS = ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%B-%Y")


def _parse_one_date(raw: str) -> datetime.date | None:
    raw = str(raw).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def parse_bhav_date(csv_bytes: bytes, source: str = "full") -> datetime.date:
    """Return the trading date printed INSIDE the bhavcopy.

    This, not the URL we requested, is the date the prices belong to.
    Raises RuntimeError if there is no date column, it cannot be parsed, or
    rows disagree — never guess a date for price data.
    """
    df = pd.read_csv(io.StringIO(csv_bytes.decode("utf-8", errors="replace")),
                     dtype=str, nrows=500)
    df.columns = df.columns.str.strip().str.upper()
    col = next((c for c in _DATE_COLS if c in df.columns), None)
    if col is None:
        raise RuntimeError(
            f"Bhavcopy has no date column (looked for {_DATE_COLS}). "
            f"Got: {sorted(df.columns)}"
        )
    values = {v.strip() for v in df[col].dropna().astype(str) if v.strip()}
    if not values:
        raise RuntimeError(f"Bhavcopy date column {col} is empty")
    dates = {_parse_one_date(v) for v in values}
    if None in dates:
        bad = [v for v in values if _parse_one_date(v) is None][:3]
        raise RuntimeError(f"Could not parse bhavcopy date(s) {bad} in column {col}")
    if len(dates) != 1:
        raise RuntimeError(f"Bhavcopy contains more than one date: {sorted(dates)}")
    return dates.pop()


# ─────────────────────────────────────────────────────────────────────
#  Normalise
# ─────────────────────────────────────────────────────────────────────

# sec_bhavdata_full_DDMMYYYY.csv
_RENAME_FULL = {
    "OPEN_PRICE":   "OPEN",
    "HIGH_PRICE":   "HIGH",
    "LOW_PRICE":    "LOW",
    "CLOSE_PRICE":  "CLOSE",
    "PREV_CLOSE":   "PREVCLOSE",
    "TTL_TRD_QNTY": "TOTTRDQTY",
    "NO_OF_TRADES": "TOTALTRADES",
    "DATE1":        "TIMESTAMP",
}

# BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv (UDiFF)
_RENAME_UDIFF = {
    "TCKRSYMB":        "SYMBOL",
    "SCTYSRS":         "SERIES",
    "OPNPRIC":         "OPEN",
    "HGHPRIC":         "HIGH",
    "LWPRIC":          "LOW",
    "CLSPRIC":         "CLOSE",
    "PRVSCLSGPRIC":    "PREVCLOSE",
    "TTLTRADGVOL":     "TOTTRDQTY",
    "TTLNBOFTXSEXCTD": "TOTALTRADES",
    "TRADDT":          "TIMESTAMP",
}

REQUIRED = {"SYMBOL", "SERIES", "OPEN", "HIGH", "LOW", "CLOSE"}


def normalise_bhav(
    csv_bytes: bytes,
    trade_date: datetime.date,
    source: str = "full",
) -> pd.DataFrame:
    """Bytes → clean DataFrame with legacy column names.

    Applies exactly the same rules as backfill_to_supabase.py:
      • EQ + BE only
      • TOTALTRADES >= MIN_TOTAL_TRADES
      • in-file date validated against the requested date (hard error on mismatch)
    """
    df = pd.read_csv(io.StringIO(csv_bytes.decode("utf-8", errors="replace")), dtype=str)
    df.columns = df.columns.str.strip().str.upper()

    rename = _RENAME_UDIFF if source == "udiff" else _RENAME_FULL
    df = df.rename(columns={k: v for k, v in rename.items() if k in df.columns})

    missing = REQUIRED - set(df.columns)
    if missing:
        raise RuntimeError(
            f"Bhavcopy is missing columns {sorted(missing)}. "
            f"NSE probably changed the file format. Got: {sorted(df.columns)}"
        )

    # Values in sec_bhavdata_full are space-padded (" EQ", " 1234.50"), so the
    # series filter would match nothing without this. astype("string") rather
    # than a dtype==object check: pandas 3 gives dtype=str columns the new
    # StringDtype, so an object check silently no-ops there.
    for col in df.columns:
        df[col] = df[col].astype("string").str.strip()

    # ── Safety net: the caller passes the file's own date (parse_bhav_date),
    #    so this should always match. A mismatch means a caller bug — fatal.
    if "TIMESTAMP" in df.columns:
        sample = df["TIMESTAMP"].dropna()
        if not sample.empty:
            parsed = _parse_one_date(sample.iloc[0])
            if parsed is None:
                log.warning("Could not parse in-file date %r — skipping validation",
                            sample.iloc[0])
            elif parsed != trade_date:
                raise RuntimeError(
                    f"Date mismatch: file says {parsed}, caller asked for {trade_date}. "
                    f"Aborting to prevent writing a day's prices under the wrong date."
                )

    df["SERIES"] = df["SERIES"].str.upper()
    df = df[df["SERIES"].isin(ALLOWED_SERIES)].copy()
    if df.empty:
        raise RuntimeError("No EQ/BE rows in bhavcopy after series filter")

    for col in ("OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TOTALTRADES"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "TOTALTRADES" in df.columns:
        before = len(df)
        df = df[df["TOTALTRADES"] >= MIN_TOTAL_TRADES].copy()
        log.info("Liquidity filter (TOTALTRADES >= %d): %d → %d rows",
                 MIN_TOTAL_TRADES, before, len(df))
    else:
        raise RuntimeError(
            "Bhavcopy has no trade-count column, so the liquidity filter cannot "
            "be applied. Refusing to load — this day would not match days loaded "
            "by backfill_to_supabase.py."
        )

    if df.empty:
        raise RuntimeError(
            f"0 rows survived the liquidity filter for {trade_date} — "
            f"suspicious for a trading day."
        )

    # A normal NSE session leaves ~1000-2500 liquid EQ/BE names. Far fewer means
    # a half-day, a truncated file, or a format change worth a human look.
    if len(df) < 300:
        log.warning("Only %d liquid rows for %s — unusually thin, please verify.",
                    len(df), trade_date)

    df["DATE"] = pd.Timestamp(trade_date)
    return df


# ─────────────────────────────────────────────────────────────────────
#  CLI — for probing without touching the database
# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(
        description="Probe the NSE bhavcopy fetch without writing to Supabase."
    )
    ap.add_argument("--date", help="YYYY-MM-DD (default: today in IST)")
    ap.add_argument("--attempts", type=int, default=1)
    ap.add_argument("--wait", type=int, default=30)
    ap.add_argument("--save", help="write the raw CSV to this path")
    args = ap.parse_args()

    d = datetime.date.fromisoformat(args.date) if args.date else ist_today()
    res = fetch_bhav(d, attempts=args.attempts, wait_secs=args.wait)

    if res.outcome != "ok":
        print(f"{res.outcome.upper()}  {d}  — {res.reason}")
        raise SystemExit(0)

    if args.save:
        with open(args.save, "wb") as fh:
            fh.write(res.csv_bytes)
        print(f"raw CSV written to {args.save}")

    file_date = parse_bhav_date(res.csv_bytes, res.source)
    if file_date != d:
        print(f"NOTE  requested {d} but the file is dated {file_date}")
    clean = normalise_bhav(res.csv_bytes, file_date, res.source)
    print(f"OK  {file_date}  source={res.source}  liquid EQ/BE rows={len(clean)}")
    print(clean[["SYMBOL", "OPEN", "HIGH", "LOW", "CLOSE", "TOTALTRADES"]].head(10)
          .to_string(index=False))