"""Regression tests for the 2026-09-14 holiday outage.

What actually happened
──────────────────────
2026-09-14 was a Monday market holiday. NSE did NOT 404 that date — it served
sec_bhavdata_full_14092026.csv with HTTP 200 and 394,927 bytes of 2026-09-11
data. The date guard in normalise_bhav correctly refused it (that guard is the
reason no corrupt prices were written), but the fallout was wrong three ways:

  1. the sweep ran oldest-first, so 14 Sep was processed before 15 Sep
  2. a backlog failure raised, failing the whole job
  3. main()'s handler then called mark_error(15 Sep), demoting a row that said
     done/2213 to error/2213 — so 15 Sep was never considered complete and the
     same failure repeated on every subsequent run

These tests pin all three.
"""
import datetime
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import nse_bhav as nb

SEP11 = datetime.date(2026, 9, 11)   # Friday, real trading day
SEP14 = datetime.date(2026, 9, 14)   # Monday, HOLIDAY
SEP15 = datetime.date(2026, 9, 15)   # Tuesday, real trading day
SAT   = datetime.date(2026, 9, 12)

HEADER = ("SYMBOL, SERIES, DATE1, PREV_CLOSE, OPEN_PRICE, HIGH_PRICE, LOW_PRICE,"
          " LAST_PRICE, CLOSE_PRICE, AVG_PRICE, TTL_TRD_QNTY, TURNOVER_LACS,"
          " NO_OF_TRADES, DELIV_QTY, DELIV_PER")


def _bhav(date_str, n=400):
    """A file big enough to clear the plausibility floor, like the real ~395KB."""
    rows = [HEADER] + [
        f"SYM{i:04d}, EQ, {date_str}, 99.0, 100.5, 105.0, 99.0,"
        f" 104.0, 104.0, 102.0, 123456, 500.00, 9000, 50000, 50.00"
        for i in range(n)
    ]
    body = ("\n".join(rows) + "\n").encode()
    return body + b"# pad\n" * ((60_000 - len(body)) // 7 + 1)


SEP11_FILE = _bhav("11-Sep-2026")
SEP15_FILE = _bhav("15-Sep-2026")


class _Resp:
    def __init__(self, status, content=b""):
        self.status_code, self.content = status, content


class _Client:
    def __init__(self, routes):
        self.routes, self.gets, self.heads = routes, [], []

    def _match(self, url):
        for key, resp in self.routes.items():
            if key in url:
                return resp
        return _Resp(404)

    def get(self, url, headers=None):
        self.gets.append(url); return self._match(url)

    def head(self, url):
        self.heads.append(url); return self._match(url)

    def __enter__(self): return self
    def __exit__(self, *a): return False


@pytest.fixture
def nse(monkeypatch):
    """Route fake NSE responses and pin the IST clock."""
    holder = {}

    def configure(routes, at=None):
        client = _Client(routes)
        monkeypatch.setattr(nb.httpx, "Client", lambda **kw: client)
        if at is not None:
            real = nb.datetime.datetime

            class Frozen(real):
                @classmethod
                def now(cls, tz=None):
                    return real(*at, tzinfo=nb.IST)

            monkeypatch.setattr(nb.datetime, "datetime", Frozen)
        holder["client"] = client
        return client

    configure.holder = holder
    return configure


# ── peek_file_date ──────────────────────────────────────────────────
def test_peek_reads_the_files_own_date():
    assert nb.peek_file_date(SEP11_FILE, "full") == SEP11


@pytest.mark.parametrize("junk", [b"", b"not,a,csv", b"\x00\x01\x02"])
def test_peek_returns_none_rather_than_guessing(junk):
    assert nb.peek_file_date(junk, "full") is None


# ── the outage itself ───────────────────────────────────────────────
def test_holiday_served_as_stale_file_is_classified_as_holiday(nse):
    """THE regression: 14 Sep asked for from 15 Sep, NSE returns 11 Sep data."""
    nse({"sec_bhavdata_full_14092026": _Resp(200, SEP11_FILE)}, at=(2026, 9, 15, 7, 28))
    res = nb.fetch_bhav(SEP14, attempts=1, wait_secs=0)
    assert res.outcome == "holiday"
    assert "2026-09-11" in res.reason


def test_stale_file_on_the_day_itself_is_not_yet_a_verdict(nse):
    """At 17:17 a stale file may just mean 'not published'. Never guess."""
    nse({"sec_bhavdata_full_14092026": _Resp(200, SEP11_FILE)}, at=(2026, 9, 14, 17, 17))
    assert nb.fetch_bhav(SEP14, attempts=1, wait_secs=0).outcome == "not_published"


def test_stale_file_after_the_verdict_hour_is_a_holiday(nse):
    nse({"sec_bhavdata_full_14092026": _Resp(200, SEP11_FILE)}, at=(2026, 9, 14, 21, 0))
    assert nb.fetch_bhav(SEP14, attempts=1, wait_secs=0).outcome == "holiday"


# ── normal operation must be untouched ──────────────────────────────
def test_matching_date_still_loads(nse):
    nse({"sec_bhavdata_full_15092026": _Resp(200, SEP15_FILE)}, at=(2026, 9, 15, 19, 17))
    res = nb.fetch_bhav(SEP15, attempts=1, wait_secs=0)
    assert res.outcome == "ok"
    assert len(nb.normalise_bhav(res.csv_bytes, SEP15, res.source)) == 400


def test_future_data_under_a_past_filename_still_raises(nse):
    """Only OLDER content means holiday. Newer means something is wrong."""
    nse({"sec_bhavdata_full_14092026": _Resp(200, SEP15_FILE)}, at=(2026, 9, 16, 19, 0))
    with pytest.raises(RuntimeError, match="not a stale republish"):
        nb.fetch_bhav(SEP14, attempts=1, wait_secs=0)


def test_genuine_404_still_uses_the_canary(nse):
    nse({"sec_bhavdata_full_14092026": _Resp(404),
         "sec_bhavdata_full_11092026": _Resp(200, SEP11_FILE)}, at=(2026, 9, 16, 19, 0))
    assert nb.fetch_bhav(SEP14, attempts=1, wait_secs=0).outcome == "holiday"


def test_blocked_still_raises(nse):
    nse({"sec_bhavdata_full": _Resp(403, b"denied")}, at=(2026, 9, 16, 19, 0))
    with pytest.raises(nb.BhavBlocked):
        nb.fetch_bhav(SEP14, attempts=1, wait_secs=0)


def test_weekend_still_short_circuits_without_network(nse):
    client = nse({}, at=(2026, 9, 16, 19, 0))
    assert nb.fetch_bhav(SAT, attempts=1, wait_secs=0).outcome == "holiday"
    assert not client.gets and not client.heads


# ── orchestration: today must never wait on the past ────────────────
def _weekdays_back(end, n):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


def test_target_is_processed_before_any_backlog_date():
    dates   = _weekdays_back(SEP15, 5)
    backlog = [d for d in dates if d != SEP15]
    ordered = [SEP15] + backlog
    assert ordered[0] == SEP15
    assert ordered.index(SEP15) < ordered.index(SEP14)
    assert sorted(ordered) == sorted(dates) and len(set(ordered)) == 5


def test_orchestrator_no_longer_fails_the_job_over_backlog():
    src = (ROOT / "scripts" / "run_engine_cli.py").read_text(encoding="utf-8")
    assert 'raise RuntimeError("catch-up failures' not in src
    assert "ordered = [target] + backlog" in src
    assert "is distinct from 'done'" in src, "mark_error must not demote a done day"
