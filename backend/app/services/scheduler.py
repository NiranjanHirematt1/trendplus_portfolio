"""
scheduler.py
────────────
Wraps the daily pipeline with:
  - engine_runs table logging (start / success / error)
  - market_calendar error marking on failure
  - clean logging output

Called by:
  - admin.py  (POST /api/admin/run and /api/admin/backfill)

Flow
────
  1. Insert engine_runs row  (status = 'running')
  2. Download today's bhav → raw bytes  (no file written to disk)
  3. Pass bytes to run_engine()
  4. Update engine_runs row  (status = 'success' / 'error')

Bug fixes vs old version
─────────────────────────
  • run_engine() now takes bhav_content: bytes — old call with
    data_folder / nse_master / sector_master has been removed.
  • summary dict no longer has "bhav_files_loaded" key — removed
    from the UPDATE statement; column left as NULL (acceptable).
  • Scheduler downloads bhav itself so admin /run trigger works
    without needing a separate download step.
"""
import logging
import datetime

from app.services.downloader import download_bhav_bytes
from app.services.engine_db  import run_engine
from app.services import portfolio_history

logger = logging.getLogger(__name__)


async def run_daily_pipeline(pool, trigger: str = "scheduled") -> dict:
    """
    Full daily pipeline with DB logging.

    Returns the summary dict from run_engine on success.
    Raises on failure (after writing error to DB).
    """
    today = datetime.date.today()

    # ── 1. Insert run record ──────────────────────────────────────────
    async with pool.acquire() as conn:
        run_id = await conn.fetchval(
            """
            insert into engine_runs (run_date, trigger, status)
            values ($1, $2, 'running')
            returning id
            """,
            today, trigger,
        )
    logger.info("[scheduler] Run #%d started  trigger=%s", run_id, trigger)

    # ── 2. Download today's bhav into memory ──────────────────────────
    bhav_bytes = await download_bhav_bytes(today)

    if bhav_bytes is None:
        # Market closed today (holiday / weekend) — mark as skipped, not error
        logger.info("[scheduler] No bhav data for %s — marking as skipped", today)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update engine_runs set
                    status      = 'error',
                    finished_at = now(),
                    error_message = 'Bhav download returned nothing — holiday or weekend'
                where id = $1
                """,
                run_id,
            )
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
        # Return an empty summary — caller should treat this as a no-op
        return {
            "trade_date":        str(today),
            "symbols_processed": 0,
            "duration_secs":     0.0,
            "skipped":           True,
        }

    # ── 3. Run engine ─────────────────────────────────────────────────
    try:
        summary = await run_engine(
            pool,
            bhav_content=bhav_bytes,
            trade_date=today,
        )

        # ── 4. Mark success ───────────────────────────────────────────
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update engine_runs set
                    status            = 'success',
                    finished_at       = now(),
                    symbols_processed = $1,
                    duration_secs     = $2
                where id = $3
                """,
                summary["symbols_processed"],
                summary["duration_secs"],
                run_id,
            )
        logger.info(
            "[scheduler] Run #%d SUCCESS — %d symbols in %.1fs",
            run_id, summary["symbols_processed"], summary["duration_secs"],
        )

        # ── 4b. Portfolio Performance History snapshots (Part 9) ───────
        # Non-fatal: a snapshotting failure must never fail the market-data
        # pipeline. Missed days are transparently reconstructed on demand
        # by GET /api/portfolio/performance-history.
        try:
            snapped = await portfolio_history.record_daily_snapshots(pool, today)
            logger.info("[scheduler] Portfolio snapshots recorded for %d portfolios (%s)", snapped, today)
        except Exception:
            logger.exception("[scheduler] Portfolio snapshot recording failed (non-fatal)")

        return summary

    except Exception as exc:
        err_msg = str(exc)[:1000]
        logger.exception("[scheduler] Run #%d FAILED: %s", run_id, err_msg)

        # ── 5. Mark failure ───────────────────────────────────────────
        async with pool.acquire() as conn:
            await conn.execute(
                """
                update engine_runs set
                    status        = 'error',
                    finished_at   = now(),
                    error_message = $1
                where id = $2
                """,
                err_msg, run_id,
            )
            await conn.execute(
                """
                insert into market_calendar
                    (trade_date, engine_status, error_message)
                values ($1, 'error', $2)
                on conflict (trade_date) do update set
                    engine_status = 'error',
                    error_message = excluded.error_message
                """,
                today, err_msg,
            )
        raise