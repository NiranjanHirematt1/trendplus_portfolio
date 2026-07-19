"""
Watchlist API — server-side, per-user watchlist for the screener.

GET    /api/watchlist            — list watched symbols with latest metrics
POST   /api/watchlist/{symbol}   — add a symbol (idempotent)
DELETE /api/watchlist/{symbol}   — remove a symbol

Requires login (bearer token, same as portfolio). Backed by watchlist_items
(migration v7); returns 503 with a clear message if the migration hasn't run.
"""
import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.api.deps import current_user
from app.core.database import get_pool

router = APIRouter()


def _table_missing() -> HTTPException:
    return HTTPException(503, "Watchlist is not available yet — run sql/migration_v7_verdicts_watchlist.sql")


@router.get("", summary="List watched symbols with latest screener metrics")
async def list_watchlist(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                """
                with latest as (select trade_date from v_latest_date)
                select w.symbol, w.note, w.created_at,
                       s.company_name, s.sector, s.cap_category,
                       tr.momentum_score, tr.rs_score, tr.trending_days,
                       tr.chg_1d, tr.chg_12d, tr.rsi_14, tr.close_price,
                       tr.near_52w_high, tr.ema_signal
                from watchlist_items w
                join symbols s on s.symbol = w.symbol
                left join latest l on true
                left join trend_results tr on tr.symbol = w.symbol and tr.trade_date = l.trade_date
                where w.user_id = $1
                order by w.created_at desc
                """,
                user["id"],
            )
        except asyncpg.UndefinedTableError:
            raise _table_missing()
    return {"data": [{k: (float(v) if hasattr(v, "as_tuple") else v) for k, v in dict(r).items()} for r in rows]}


@router.post("/{symbol}", summary="Add a symbol to the watchlist")
async def add_to_watchlist(symbol: str, user=Depends(current_user), pool=Depends(get_pool)):
    symbol = symbol.strip().upper()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("select true from symbols where symbol = $1 and is_active = true", symbol)
        if not exists:
            raise HTTPException(422, f"Unknown symbol: {symbol}")
        try:
            await conn.execute(
                "insert into watchlist_items (user_id, symbol) values ($1, $2) on conflict (user_id, symbol) do nothing",
                user["id"], symbol,
            )
        except asyncpg.UndefinedTableError:
            raise _table_missing()
    return {"symbol": symbol, "watched": True}


@router.delete("/{symbol}", summary="Remove a symbol from the watchlist")
async def remove_from_watchlist(symbol: str, user=Depends(current_user), pool=Depends(get_pool)):
    symbol = symbol.strip().upper()
    async with pool.acquire() as conn:
        try:
            deleted = await conn.fetchval(
                "delete from watchlist_items where user_id = $1 and symbol = $2 returning id",
                user["id"], symbol,
            )
        except asyncpg.UndefinedTableError:
            raise _table_missing()
    if not deleted:
        raise HTTPException(404, "Symbol is not on your watchlist")
    return {"symbol": symbol, "watched": False}
