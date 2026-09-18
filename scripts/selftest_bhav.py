"""Offline self-test for the bhavcopy fetch + parse layer.

No network, no database. Run it whenever you suspect NSE changed the file
format, or after touching nse_bhav.py:

    python scripts/selftest_bhav.py

Exits 0 if everything passes, 1 otherwise.
"""
import datetime
import io
import os
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import nse_bhav as nb


# ── fake httpx client ────────────────────────────────────────────────
class FakeResp:
    def __init__(self, status, content=b""):
        self.status_code = status
        self.content = content


class FakeClient:
    """Serves a map of url-substring -> FakeResp factory."""
    def __init__(self, routes):
        self.routes = routes
        self.gets, self.heads = [], []

    def _match(self, url):
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return FakeResp(404)

    def get(self, url, headers=None):
        self.gets.append(url)
        return self._match(url)

    def head(self, url):
        self.heads.append(url)
        return self._match(url)

    def __enter__(self): return self
    def __exit__(self, *a): return False


def patch_client(routes):
    fc = FakeClient(routes)
    nb.httpx.Client = lambda **kw: fc
    return fc


BIG = b"x" * 200_000
FRI = datetime.date(2026, 8, 14)   # Friday
SAT = datetime.date(2026, 8, 15)   # Saturday
MON = datetime.date(2026, 8, 17)   # Monday

results = []
def check(name, cond, extra=""):
    results.append((name, cond, extra))
    print(("PASS  " if cond else "FAIL  ") + name + (f"   {extra}" if extra and not cond else ""))


# 1 — weekend short-circuits with no HTTP at all
fc = patch_client({})
r = nb.fetch_bhav(SAT)
check("weekend -> no_file, zero requests",
      r.outcome == "no_file" and not fc.gets and not fc.heads, f"{r.outcome} gets={fc.gets}")

# 2 — happy path
fc = patch_client({"sec_bhavdata_full_14082026": FakeResp(200, BIG)})
r = nb.fetch_bhav(FRI, attempts=1)
check("file present -> ok/full",
      r.outcome == "ok" and r.source == "full" and r.csv_bytes == BIG, r.outcome)

# 3 — 404 -> no_file (never an error, never a holiday stamp)
fc = patch_client({})
r = nb.fetch_bhav(MON, attempts=1)
check("404 -> no_file", r.outcome == "no_file", r.outcome)

# 5 — 403 -> BLOCKED immediately, not a holiday
fc = patch_client({"sec_bhavdata_full": FakeResp(403, b"denied")})
try:
    nb.fetch_bhav(MON, attempts=1)
    check("403 -> BhavBlocked", False, "no exception raised")
except nb.BhavBlocked as e:
    check("403 -> BhavBlocked", "refusing" in str(e), str(e)[:80])

# 6 — HTML challenge page with HTTP 200 -> BLOCKED
fc = patch_client({"sec_bhavdata_full": FakeResp(200, b"<html><body>Access Denied" + b" " * 100_000)})
try:
    nb.fetch_bhav(MON, attempts=1)
    check("HTML 200 challenge -> BhavBlocked", False, "no exception raised")
except nb.BhavBlocked as e:
    check("HTML 200 challenge -> BhavBlocked", "HTML page" in str(e), str(e)[:80])

# 7 — tiny 200 body -> BLOCKED
fc = patch_client({"sec_bhavdata_full": FakeResp(200, b"SYMBOL,SERIES\n")})
try:
    nb.fetch_bhav(MON, attempts=1)
    check("tiny body -> BhavBlocked", False, "no exception raised")
except nb.BhavBlocked as e:
    check("tiny body -> BhavBlocked", "implausibly small" in str(e), str(e)[:80])

# 8 — too_early guard: asking for *today* before 17:00 IST
real_dt = nb.datetime.datetime
class MorningDT(real_dt):
    @classmethod
    def now(cls, tz=None):
        return real_dt(2026, 8, 17, 9, 30, tzinfo=nb.IST)
nb.datetime.datetime = MorningDT
fc = patch_client({})
r = nb.fetch_bhav(MON, attempts=1)
nb.datetime.datetime = real_dt
check("today before 17:00 IST -> too_early",
      r.outcome == "too_early" and not fc.gets, f"{r.outcome} gets={len(fc.gets)}")

# 9 — UDiFF fallback only when opted in
zbuf = io.BytesIO()
with zipfile.ZipFile(zbuf, "w") as zf:
    zf.writestr("BhavCopy_NSE_CM.csv", BIG.decode())
zbytes = zbuf.getvalue()
os.environ["NSE_ALLOW_UDIFF"] = "1"
fc = patch_client({"sec_bhavdata_full": FakeResp(404),
                   "BhavCopy_NSE_CM": FakeResp(200, zbytes)})
r = nb.fetch_bhav(FRI, attempts=1)
os.environ.pop("NSE_ALLOW_UDIFF")
check("UDiFF opt-in fallback unzips", r.outcome == "ok" and r.source == "udiff", r.outcome)


# ── normalise_bhav ───────────────────────────────────────────────────
# Real sec_bhavdata_full shape: space-padded headers AND values.
HEADER = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,"
          " LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,"
          " NO_OF_TRADES, DELIV_QTY, DELIV_PER")

def row(sym, series, o, h, l, c, trades, date="14-Aug-2026", prev=None):
    prev = prev if prev is not None else c - 1
    return (f"{sym}, {series}, {date}, {prev}, {o}, {h}, {l}, {c}, {c}, {c},"
            f" 100000, 500.00, {trades}, 50000, 50.00")

csv = "\n".join([HEADER,
    row("RELIANCE", "EQ", 100.5, 105.0, 99.0, 104.0, 9000),
    row("TCS",      "EQ", 200.0, 210.0, 198.0, 208.0, 5000),
    row("ILLIQUID", "EQ", 10.0,  11.0,  9.5,   10.5,  120),      # below 3000 trades
    row("SOMEBOND", "N1", 50.0,  51.0,  49.0,  50.5,  99999),    # wrong series
    row("BESTOCK",  "BE", 30.0,  31.0,  29.0,  30.5,  4000),
]) + "\n"

df = nb.normalise_bhav(csv.encode(), FRI, "full")
syms = sorted(df["SYMBOL"].tolist())
check("series + liquidity filter", syms == ["BESTOCK", "RELIANCE", "TCS"], str(syms))
check("OPEN is the real open, not close",
      float(df.loc[df.SYMBOL == "RELIANCE", "OPEN"].iloc[0]) == 100.5
      and float(df.loc[df.SYMBOL == "RELIANCE", "CLOSE"].iloc[0]) == 104.0)
check("PREVCLOSE parsed from file",
      float(df.loc[df.SYMBOL == "TCS", "PREVCLOSE"].iloc[0]) == 207.0)
check("values de-padded", df["SERIES"].tolist() == ["EQ", "EQ", "BE"])
check("liquidity threshold matches backfill", nb.MIN_TOTAL_TRADES == 3000)

# normalise_bhav safety net: caller date != file date is still fatal
bad = "\n".join([HEADER, row("RELIANCE", "EQ", 1, 2, 0.5, 1.5, 9000, date="13-Aug-2026")]) + "\n"
try:
    nb.normalise_bhav(bad.encode(), FRI, "full")
    check("normalise date mismatch is fatal", False, "no exception")
except RuntimeError as e:
    check("normalise date mismatch is fatal", "Date mismatch" in str(e), str(e)[:60])

# ── parse_bhav_date: the file's own date decides what gets loaded ────
check("parse_bhav_date reads DATE1", nb.parse_bhav_date(csv.encode()) == FRI)

# The 14-Sep-2026 case: holiday URL, previous session's data inside
holiday_csv = "\n".join([HEADER,
    row("RELIANCE", "EQ", 1, 2, 0.5, 1.5, 9000, date="11-Sep-2026"),
    row("TCS",      "EQ", 1, 2, 0.5, 1.5, 9000, date="11-Sep-2026")]) + "\n"
check("holiday file -> previous session date",
      nb.parse_bhav_date(holiday_csv.encode()) == datetime.date(2026, 9, 11))

check("UDiFF ISO date parsed",
      nb.parse_bhav_date(b"TradDt,TckrSymb\n2026-09-11,RELIANCE\n", "udiff")
      == datetime.date(2026, 9, 11))

for label, body in [
    ("no date column -> error", b"SYMBOL,SERIES\nRELIANCE,EQ\n"),
    ("garbage date -> error",   b"SYMBOL, DATE1\nRELIANCE, not-a-date\n"),
    ("two dates -> error",      b"SYMBOL, DATE1\nA, 11-Sep-2026\nB, 14-Sep-2026\n"),
]:
    try:
        nb.parse_bhav_date(body)
        check(label, False, "no exception")
    except RuntimeError:
        check(label, True)

# missing trade-count column must refuse, not silently load
noc = HEADER.replace(" NO_OF_TRADES,", "")
bad2 = "\n".join([noc, ", ".join(
    [p for i, p in enumerate(row("RELIANCE","EQ",1,2,0.5,1.5,9000).split(", "))
     if i != 12])]) + "\n"
try:
    nb.normalise_bhav(bad2.encode(), FRI, "full")
    check("missing trade-count refuses to load", False, "no exception")
except RuntimeError as e:
    check("missing trade-count refuses to load", "liquidity filter" in str(e), str(e)[:60])

# ── summary ──────────────────────────────────────────────────────────
bad_n = sum(1 for _, ok, _ in results if not ok)
print(f"\n{len(results) - bad_n}/{len(results)} checks passed")
sys.exit(1 if bad_n else 0)