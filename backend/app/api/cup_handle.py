"""
Cup & Handle API — reads the precomputed cup_handle_pattern table (populated
by the daily/weekly scan, app.services.cup_handle_scan).

GET /api/cup-handle            — screener list (timeframe + stage filters,
                                 sortable by pattern score); one row per stock
GET /api/cup-handle/{symbol}   — the symbol's daily & weekly patterns for the
                                 stock detail panel

Public (no auth) — same as the trend/superstrength screens. Returns 503 with
a clear message if migration_v10 hasn't been run yet.
"""
import math
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.database import get_pool

router = APIRouter()

VALID_TIMEFRAMES = ("daily", "weekly")
VALID_STAGES = ("cup_forming", "handle_forming", "breakout", "confirmed")
VALID_SORT = frozenset({
    "pattern_score", "cup_depth_pct", "cup_duration",
    "handle_depth_pct", "volume_ratio", "last_close", "computed_at",
})

_COLUMNS = """
    p.symbol, s.company_name, s.sector, s.cap_category,
    p.timeframe, p.stage, p.resistance, p.cup_depth_pct, p.cup_duration,
    p.handle_depth_pct, p.handle_duration, p.breakout, p.volume_ratio,
    p.pattern_score, p.last_close, p.meta, p.computed_at
"""


def _table_missing() -> HTTPException:
    return HTTPException(503, "Cup & Handle is not available yet — run sql/migration_v10_cup_handle.sql")


@router.get("", summary="Cup & Handle screener")
async def list_patterns(
    timeframe:    str            = Query("daily"),
    stage:        Optional[str]  = Query(None, description="all | cup_forming | handle_forming | breakout | confirmed"),
    min_score:    float          = Query(0, ge=0, le=100),
    sector:       Optional[str]  = Query(None),
    cap_category: Optional[str]  = Query(None),
    sort_by:      str            = Query("pattern_score"),
    order:        str            = Query("desc", pattern="^(asc|desc)$"),
    page:         int            = Query(1, ge=1),
    page_size:    int            = Query(50, ge=1, le=200),
    pool=Depends(get_pool),
):
    if timeframe not in VALID_TIMEFRAMES:
        raise HTTPException(422, "timeframe must be 'daily' or 'weekly'")
    if sort_by not in VALID_SORT:
        sort_by = "pattern_score"
    offset = (page - 1) * page_size

    params = [timeframe]
    conditions = ["p.timeframe = $1", "s.is_active = true"]
    p = 2

    if stage and stage != "all":
        if stage not in VALID_STAGES:
            raise HTTPException(422, "Invalid stage")
        conditions.append(f"p.stage = ${p}")
        params.append(stage); p += 1
    if min_score > 0:
        conditions.append(f"p.pattern_score >= ${p}")
        params.append(min_score); p += 1
    if sector:
        conditions.append(f"s.sector = ${p}")
        params.append(sector); p += 1
    if cap_category:
        if cap_category not in ("Large Cap", "Mid Cap", "Small Cap"):
            raise HTTPException(422, "cap_category must be Large Cap, Mid Cap, or Small Cap")
        conditions.append(f"s.cap_category = ${p}")
        params.append(cap_category); p += 1

    where = " and ".join(conditions)
    sort_dir = "asc" if order == "asc" else "desc"

    async with pool.acquire() as conn:
        try:
            total = await conn.fetchval(
                f"""select count(*) from cup_handle_pattern p
                    join symbols s on s.symbol = p.symbol where {where}""",
                *params,
            )
            rows = await conn.fetch(
                f"""select {_COLUMNS}
                    from cup_handle_pattern p
                    join symbols s on s.symbol = p.symbol
                    where {where}
                    order by p.{sort_by} {sort_dir} nulls last, p.symbol asc
                    limit ${p} offset ${p+1}""",
                *params, page_size, offset,
            )
        except asyncpg.UndefinedTableError:
            raise _table_missing()

    pages = math.ceil(total / page_size) if page_size and total else 0
    return {
        "timeframe": timeframe,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "data": [dict(r) for r in rows],
    }


@router.get("/{symbol}", summary="A symbol's Cup & Handle patterns (daily + weekly)")
async def symbol_patterns(symbol: str, pool=Depends(get_pool)):
    symbol = symbol.strip().upper()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(
                f"""select {_COLUMNS}
                    from cup_handle_pattern p
                    join symbols s on s.symbol = p.symbol
                    where p.symbol = $1""",
                symbol,
            )
        except asyncpg.UndefinedTableError:
            raise _table_missing()
    by_tf = {r["timeframe"]: dict(r) for r in rows}
    return {
        "symbol": symbol,
        "daily": by_tf.get("daily"),
        "weekly": by_tf.get("weekly"),
    }
