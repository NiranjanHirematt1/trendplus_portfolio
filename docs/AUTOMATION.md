# Daily automation

No more downloading a bhavcopy by hand, no more running `backfill_to_supabase.py`
and `compute_today.py` yourself. Your PC does not need to be on.

## What runs, and when

| When (IST) | Where | What |
|---|---|---|
| 19:00, 21:00, 23:00 Mon–Fri | GitHub Actions | `scripts/run_engine_cli.py` — fetch bhavcopy → `price_history` → `compute_today` → Cup & Handle |
| 23:45 daily | Your PC (optional) | `scripts/pc_fallback.ps1` — same script, only acts if the cloud didn't |
| Sun 09:00 | GitHub Actions | `scripts/refresh_master_data.py` — EQUITY_L + sector/cap master |

Three cloud attempts, not one, because NSE publishes the file in the evening and
sometimes late. The script checks `market_calendar` first and exits immediately
if the date is already `done`, so runs 2 and 3 normally cost a few seconds.

There is no temp storage to set up. The bhavcopy is downloaded straight into the
runner's memory, written to Supabase, and thrown away with the runner.

## Why it wasn't working before

The old `run_engine_cli.py` downloaded from `www.nseindia.com/api/reports`. That
endpoint is behind NSE's bot protection: it needs a real browser cookie handshake
and refuses datacentre IPs, which is exactly what a GitHub runner has. The old
code treated *every* failure — 403, empty body, HTML challenge page — as "market
holiday", wrote `engine_status='skipped'` and exited **0**. So the workflow went
green every day while loading nothing, and there was no red X to notice.

Three things changed:

1. **Source.** Now `nsearchives.nseindia.com/products/content/sec_bhavdata_full_DDMMYYYY.csv`
   — a static file on a CDN, no session, no cookies. It is the same host
   `refresh_master_data.py` already uses successfully. It also carries
   `OPEN_PRICE` and `PREV_CLOSE`, which the old endpoint did not.
2. **Holiday is proven, not assumed.** When today's file is missing the script
   fetches a *canary* — the most recent earlier weekday, whose file definitely
   exists. Canary readable → today really is a holiday. Canary also unreadable →
   we are being blocked, and the job fails loudly instead of lying.
3. **Failures are loud.** Real errors exit non-zero (GitHub emails you) and write
   the actual message to `market_calendar.error_message`.

## Data correctness fixes

The CI path used to write different data than your manual path, which meant a day
loaded by the cloud was not comparable to a day you loaded yourself:

- **`open_price` was `close_price`.** The old comment said "bhav has no OPEN" —
  true of that endpoint, not of `sec_bhavdata_full`. Every CI-loaded day had
  open == close, so any open-based signal was meaningless on those dates.
- **No liquidity filter.** `backfill_to_supabase.py` keeps only
  `TOTALTRADES >= 3000`; the CI path kept everything. Breadth and ranking metrics
  shifted depending on who loaded the day.
- **`prev_close` guessed from the DB** rather than read from the file's
  `PREV_CLOSE`.
- **No date validation.** The file's own date is now checked against the
  requested date and a mismatch is fatal.
- **`datetime.date.today()` was UTC** on the runner. Now IST.

If you want to repair days the old CI loaded, re-run them with `--force`
(see below). Days you loaded manually with `backfill_to_supabase.py` are fine.

## One-time setup

**1. GitHub secret.** Settings → Secrets and variables → Actions → New secret:

```
Name:  DATABASE_URL
Value: <your Supabase connection string, same one in backend/.env>
```

Get the value from Supabase → Project Settings → Database → Connection string →
**Session pooler**. It looks like:

```
postgresql://postgres.<project-ref>:<password>@aws-<region>.pooler.supabase.com:5432/postgres
```

Three connection strings are offered and only this one is correct here:

| Option | Host / port | Verdict |
|---|---|---|
| Direct | `db.<ref>.supabase.co:5432` | ✗ IPv6-only; GitHub runners have no IPv6 |
| **Session pooler** | `...pooler.supabase.com:5432` | ✓ IPv4, supports prepared statements |
| Transaction pooler | `...pooler.supabase.com:6543` | ✗ no prepared statements — asyncpg breaks |

The transaction pooler is the trap: it looks equivalent, it's IPv4 too, but
asyncpg uses prepared statements by default and transaction mode rejects them.
You'd get `PreparedStatementError` / `DuplicatePreparedStatementError` partway
through the run rather than a clean failure at connect time.

Note the pooler username is `postgres.<project-ref>`, not plain `postgres`.

**2. Move the workflow files into place.** The two YAML files were delivered to
`docs/workflows/` rather than straight into `.github/workflows/`, because
workflow files are write-protected against remote tooling. Copy them yourself:

```powershell
copy docs\workflows\daily_run.yml .github\workflows\daily_run.yml
copy docs\workflows\weekly_master_refresh.yml .github\workflows\weekly_master_refresh.yml
rmdir /s /q docs\workflows
```

`daily_run.yml` replaces the existing one; `weekly_master_refresh.yml` is new.

**3. Push the changes.**

```bash
git add scripts/nse_bhav.py scripts/run_engine_cli.py scripts/selftest_bhav.py \
        scripts/pc_fallback.ps1 .github/workflows docs/AUTOMATION.md
git commit -m "Automate daily run: fetch bhavcopy from NSE archive CDN, fail loudly"
git push
```

**4. Prove it works before waiting for a cron.** Actions → *Daily Engine Run* →
Run workflow → set *Override date* to the last trading day → Run.

Watch the log. You want to see `Bhavcopy downloaded: … bytes (source=full)` and
then a `DONE | … symbols` line.

**5. Install the PC fallback** (optional but recommended for the first few weeks):

```powershell
cd C:\Users\user-pc\Downloads\trendplus-admin-approval-auth
powershell -ExecutionPolicy Bypass -File scripts\pc_fallback.ps1 -Install
```

It runs at 23:45, after all three cloud attempts, and does nothing at all if the
cloud already succeeded. `-StartWhenAvailable` is set, so if the PC was asleep it
runs when it next wakes. Logs land in `logs\engine_YYYYMMDD.log`.

Remove it later with `-Uninstall`.

## Running it by hand

From the repo root, with the venv active:

```bash
python scripts/run_engine_cli.py                      # today (IST)
python scripts/run_engine_cli.py --date 2026-08-14    # one specific day
python scripts/run_engine_cli.py --date 2026-08-14 --force   # recompute a done day
python scripts/run_engine_cli.py --catchup 10         # fill any gaps in the last 10 weekdays
```

`--catchup N` walks the last N weekdays oldest-first and processes any that are
neither loaded nor a known holiday. This is the recovery tool: if the cloud was
blocked for three days, one `--catchup 5` fixes all of them in order.

Check the fetch without touching the database at all:

```bash
python scripts/nse_bhav.py --date 2026-08-14
python scripts/nse_bhav.py --date 2026-08-14 --save /tmp/bhav.csv
```

Check the parsing logic still matches NSE's format:

```bash
python scripts/selftest_bhav.py
```

## When something goes wrong

Look at `market_calendar` first — the script records its own diagnosis there.

| `engine_status` | Meaning | Action |
|---|---|---|
| `done` | Loaded and computed | none |
| `skipped` | Proven non-trading day | none |
| `error` + "bhav download blocked" | NSE refused the runner's IP | run from your PC: `python scripts/run_engine_cli.py --catchup 5` |
| `error` + anything else | Read `error_message` | fix, then re-run that date |

```sql
select trade_date, engine_status, symbol_count, error_message
from market_calendar
order by trade_date desc
limit 15;
```

**If NSE starts blocking GitHub's IP range permanently**, you have three options,
cheapest first:

1. Leave the PC fallback installed — it already covers this, it just needs the PC
   on at some point each evening.
2. Set `NSE_ALLOW_UDIFF=1` in the workflow env. The script will then also try the
   newer zipped UDiFF bhavcopy at a different path when the primary 404s.
3. Move the job to a small VPS in India (or a Render cron job) and keep the same
   script — nothing in it is GitHub-specific.

## Files

| File | Role |
|---|---|
| `scripts/nse_bhav.py` | fetch + normalise the bhavcopy; holiday-vs-blocked logic |
| `scripts/run_engine_cli.py` | the daily job: guard → fetch → load → compute |
| `scripts/selftest_bhav.py` | offline checks, no network or DB needed |
| `scripts/pc_fallback.ps1` | Windows Task Scheduler safety net |
| `.github/workflows/daily_run.yml` | the three weekday runs |
| `.github/workflows/weekly_master_refresh.yml` | Sunday master-data refresh |

`backfill_to_supabase.py` stays as-is — it is still the right tool for loading a
folder of historical CSVs. You just won't need it daily any more.
