"""
Admin & Status API

GET  /api/admin/status          — engine status, latest date, run history
POST /api/admin/run             — trigger full engine run (async)
POST /api/admin/backfill        — trigger backfill of all bhav files (async)
GET  /api/admin/runs            — engine run history (last 30)
GET  /api/admin/calendar        — market calendar (last 30 days)

All POST endpoints require header:  X-Admin-Secret: <ADMIN_SECRET>
"""
import asyncio
import hmac
import logging
import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request
from app.core.config import settings
from app.core.database import get_pool
from app.api.deps import client_ip, decode_token_admin_optional
from app.services.audit import log_admin_action

logger = logging.getLogger(__name__)
router = APIRouter()


def _secret_ok(x_admin_secret: Optional[str]) -> bool:
    """Constant-time comparison of the shared admin secret (guards against timing attacks)."""
    if not x_admin_secret:
        return False
    return hmac.compare_digest(
        x_admin_secret.encode("utf-8"),
        (settings.ADMIN_SECRET or "").encode("utf-8"),
    )


def _verify_secret(x_admin_secret: str = Header(..., alias="X-Admin-Secret")) -> None:
    """Dependency — validates admin secret header in constant time."""
    if not _secret_ok(x_admin_secret):
        raise HTTPException(status_code=403, detail="Invalid admin secret")


async def engine_actor(
    request: Request,
    x_admin_secret: Optional[str] = Header(default=None, alias="X-Admin-Secret"),
    authorization: Optional[str] = Header(default=None),
    pool=Depends(get_pool),
) -> dict:
    """
    Authorises an engine run/backfill by EITHER:
      - a valid X-Admin-Secret header (programmatic / cron use — no identity), OR
      - a superadmin bearer token (interactive use from the admin panel).

    Returns an actor descriptor used for audit logging.
    """
    if _secret_ok(x_admin_secret):
        return {"admin_id": None, "actor_label": "x-admin-secret", "ip": client_ip(request)}

    admin = await decode_token_admin_optional(authorization, pool)
    if admin and admin.get("role") == "superadmin":
        return {"admin_id": admin["id"], "actor_label": admin["username"], "ip": client_ip(request)}
    if admin:
        raise HTTPException(status_code=403, detail="This action requires a superadmin account")
    raise HTTPException(status_code=403, detail="Invalid admin secret")


# ── STATUS ────────────────────────────────────────────────────────────
@router.get("/status", summary="Engine status and system info")
async def get_status(pool=Depends(get_pool)):
    """
    Returns overall system health:
    - last engine run (status, duration, symbol count)
    - latest trading date with data
    - total trading days processed
    - total symbols in DB
    """
    async with pool.acquire() as conn:
        # Latest completed run
        last_run = await conn.fetchrow(
            """select id, run_date, trigger, started_at, finished_at,
                      status, symbols_processed, bhav_files_loaded,
                      duration_secs, error_message
               from engine_runs
               order by started_at desc
               limit 1"""
        )
        # Latest trading date
        latest_date_row = await conn.fetchrow(
            "select trade_date from v_latest_date"
        )
        # Counts
        stats = await conn.fetchrow(
            """select
                (select count(*) from symbols where is_active = true)           as total_symbols,
                (select count(*) from market_calendar where engine_status='done') as total_days,
                (select count(*) from engine_runs where status='success')         as successful_runs,
                (select max(trade_date) from market_calendar
                 where engine_status = 'running')                                 as currently_running_for
            """
        )

    return {
        "latest_date":          str(latest_date_row["trade_date"]) if latest_date_row and latest_date_row["trade_date"] else None,
        "total_symbols":        stats["total_symbols"],
        "total_trading_days":   stats["total_days"],
        "successful_runs":      stats["successful_runs"],
        "currently_running":    stats["currently_running_for"] is not None,
        "last_run":             dict(last_run) if last_run else None,
    }


# ── RUN HISTORY ───────────────────────────────────────────────────────
@router.get("/runs", summary="Engine run history")
async def get_run_history(
    limit: int = Query(30, ge=1, le=100),
    pool=Depends(get_pool),
):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """select id, run_date, trigger, started_at, finished_at,
                      status, symbols_processed, bhav_files_loaded,
                      duration_secs, error_message
               from engine_runs
               order by started_at desc
               limit $1""",
            limit,
        )
    return {"count": len(rows), "runs": [dict(r) for r in rows]}


# ── CALENDAR ─────────────────────────────────────────────────────────
@router.get("/calendar", summary="Market calendar")
async def get_calendar(
    limit: int = Query(30, ge=1, le=252),
    pool=Depends(get_pool),
):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """select trade_date, is_trading_day, holiday_name,
                      bhav_downloaded, engine_status, symbol_count,
                      engine_duration_secs, error_message, processed_at
               from market_calendar
               order by trade_date desc
               limit $1""",
            limit,
        )
    return {"count": len(rows), "calendar": [dict(r) for r in rows]}


# ── TRIGGER DAILY RUN ────────────────────────────────────────────────
@router.post("/run", summary="Trigger engine run")
async def trigger_run(actor: dict = Depends(engine_actor), pool=Depends(get_pool)):
    """
    Triggers a full engine run in the background.
    The run loads all bhav files, computes metrics, and upserts to DB.
    Returns immediately — check /api/admin/status for progress.

    Requires a valid X-Admin-Secret header OR a superadmin bearer token.
    """
    # Check if a run is already in progress
    async with pool.acquire() as conn:
        running = await conn.fetchrow(
            "select id from engine_runs where status='running' limit 1"
        )
    if running:
        raise HTTPException(status_code=409,
            detail="An engine run is already in progress")

    asyncio.create_task(_run_engine_task(pool, trigger="manual"))
    await log_admin_action(
        pool, action="engine.run", actor_label=actor["actor_label"],
        admin_id=actor["admin_id"], target_type="engine", ip_address=actor["ip"],
    )
    return {"status": "triggered", "message": "Engine run started in background"}


# ── TRIGGER BACKFILL ──────────────────────────────────────────────────
@router.post("/backfill", summary="Trigger full historical backfill")
async def trigger_backfill(actor: dict = Depends(engine_actor), pool=Depends(get_pool)):
    """
    Re-processes ALL bhav files in DATA_FOLDER.
    Use this on first setup or to re-compute all historical data.
    Takes several minutes for 252 files × 2143 symbols.

    Requires a valid X-Admin-Secret header OR a superadmin bearer token.
    """
    async with pool.acquire() as conn:
        running = await conn.fetchrow(
            "select id from engine_runs where status='running' limit 1"
        )
    if running:
        raise HTTPException(status_code=409,
            detail="An engine run is already in progress")

    asyncio.create_task(_run_engine_task(pool, trigger="backfill"))
    await log_admin_action(
        pool, action="engine.backfill", actor_label=actor["actor_label"],
        admin_id=actor["admin_id"], target_type="engine", ip_address=actor["ip"],
    )
    return {"status": "triggered", "message": "Backfill started in background"}


# ── INTERNAL TASK ─────────────────────────────────────────────────────
async def _run_engine_task(pool, trigger: str = "manual"):
    """Background task — wraps the engine with DB logging."""
    from app.services.scheduler import run_daily_pipeline
    try:
        await run_daily_pipeline(pool, trigger=trigger)
    except Exception as e:
        logger.exception("Engine task failed: %s", e)
