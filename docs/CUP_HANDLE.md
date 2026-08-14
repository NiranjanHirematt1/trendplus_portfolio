# Cup & Handle Engine

A rules-based (no ML) Cup & Handle detector that scans the **entire** active
NSE universe on both **daily** and **weekly** candles after each bhav load,
and precomputes one pattern per (symbol, timeframe) for the screener and the
stock detail panel to read.

| | |
|---|---|
| Detector | `backend/app/services/cup_handle.py` |
| Scan | `backend/app/services/cup_handle_scan.py` |
| API | `backend/app/api/cup_handle.py` (`/api/cup-handle`) |
| Migration | `sql/migration_v10_cup_handle.sql` |
| Screener UI | `frontend/index.html` → **Cup & Handle** tab |
| Detail panel | `frontend/shared.js` (TPDP) → **Technical** tab |

## Data

Uses the existing `price_history` daily OHLCV — no separate data source.
Weekly candles are resampled from the daily series (`W-FRI` OHLCV
aggregation). Because the NSE bhav copy carries no true open, `open_price` is
a proxy for close; the engine reads only **close / high / low / volume**.

## Detection (`detect_cup_handle(df, cfg)`)

Everything is driven by a `CupHandleConfig` (percentages, never price levels,
so the same rules hold across every stock). `DAILY_CONFIG` and `WEEKLY_CONFIG`
are the two timeframe presets; the core engine is shared.

The engine locates a rounded base within the most recent window:

- **Left rim** — highest pivot early in the window; also the resistance level.
- **Cup bottom** — lowest close after the left rim.
- **Right rim** — the first post-bottom pivot that recovers back near the lip.
- Validates **cup depth**, **duration**, **rim symmetry**, **bottom centering**
  and a **rounded (U, not V) base**.
- **Handle** — a shallow pullback after the right rim, in the cup's upper half.
- **Breakout** — a close above `resistance × (1 + breakout_buffer)`, optionally
  with **volume expansion** vs the average.

### Stages

| Stage | Meaning |
|---|---|
| `cup_forming` | Valid rounded base; right side hasn't produced a clean handle yet |
| `handle_forming` | Cup complete; shallow handle pullback in progress below resistance |
| `breakout` | Price has just closed above resistance (+buffer) |
| `confirmed` | The breakout has held above resistance for a few bars |

### Pattern Quality Score (0–100)

A weighted blend of cup depth, cup duration, symmetry, roundedness,
right-rim recovery, handle quality, volume behaviour, and breakout progress.
Weights are configurable on `CupHandleConfig`.

### No look-ahead

The engine only ever indexes candles up to the last row of the frame it is
given. The scan passes candles up to the latest loaded session, so a breakout
is only ever called on data that already existed at that date.

## Scan

`run_cup_handle_scan(pool)` scans every active symbol on daily + weekly,
upserts detected patterns into `cup_handle_pattern`, and deletes rows whose
pattern has dissolved. It runs (non-fatally) at the end of the daily engine
(`scripts/run_engine_cli.py`) and is safe to re-run — it overwrites in place.
A full universe scan (~2,800 symbols × 2 timeframes) takes ~2–3 minutes.

## API (`/api/cup-handle`)

Public (no auth), reads straight from `cup_handle_pattern`:

| Method | Path | Notes |
|---|---|---|
| GET | `/api/cup-handle` | screener list — `timeframe`, `stage`, `min_score`, `sector`, `cap_category`, `sort_by`, `order`, `page`, `page_size` |
| GET | `/api/cup-handle/{symbol}` | the symbol's `daily` + `weekly` patterns for the detail panel |

`stage` matches one lifecycle stage exactly (`cup_forming` / `handle_forming`
/ `breakout` / `confirmed`); omit it (or pass `all`) for every stage.

## Setup

```bash
psql "$DATABASE_URL" -f sql/migration_v10_cup_handle.sql
# then run the scan once to populate (or wait for the next daily engine run):
python -c "import asyncio, os, asyncpg; from app.services.cup_handle_scan import run_cup_handle_scan; \
  asyncio.run((lambda: (lambda p: run_cup_handle_scan(p))(asyncpg.create_pool(os.environ['DATABASE_URL'])))())"
```

`migration_v10` also drops the retired v9 per-user "Cup & Handle Watch" tables.
