"""
GET /api/trend
────────────────────────────────────────────────────────
Primary screener endpoint. Full filter + sort support.

New filters vs v1:
  min_trades   int     minimum total_trades (liquidity filter)
  macd_bullish bool    true = macd_hist > 0
  ema_signal   text    golden_cross | above_200 | approaching
"""
import math
import datetime
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from app.core.database import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()

VALID_SORT = frozenset({
    "momentum_score", "rs_score", "trending_days",
    "chg_12d", "chg_5d", "chg_3d", "chg_1d",
    "chg_1m", "chg_2m", "chg_3m", "chg_6m", "chg_12m",
    "rsi_14", "adx_14",
    "rank_52w", "pct_from_high",
    "close_price", "total_trades", "volume",
    "macd_hist", "macd_line",
    "ema_50", "ema_200",
    "weighted_rpi",
    "rpi_2w", "rpi_3m", "rpi_6m", "rpi_6m_sma2w",
    "ema_9", "ema_21",
    "rsi_1d", "rsi_1w",
})


@router.get("", summary="Stock screener")
async def get_trend(
    date:          Optional[str]  = Query(None),
    min_trending:  int            = Query(0,    ge=0, le=12),
    min_rsi:       float          = Query(0,    ge=0, le=100),
    min_adx:       float          = Query(0,    ge=0, le=100),
    min_chg_12d:   float          = Query(-999),
    min_momentum:  float          = Query(0,    ge=0, le=100),
    min_trades:    int            = Query(0,    ge=0),
    sector:        Optional[str]  = Query(None),
    cap_category:  Optional[str]  = Query(None),
    near_52w_high: Optional[bool] = Query(None),
    macd_bullish:  Optional[bool] = Query(None),
    ema_signal:    Optional[str]  = Query(None),
    sort_by:       str            = Query("momentum_score"),
    order:         str            = Query("desc", pattern="^(asc|desc)$"),
    page:          int            = Query(1, ge=1),
    page_size:     int            = Query(50, ge=1, le=200),
    pool=Depends(get_pool),
):
    if sort_by not in VALID_SORT:
        sort_by = "momentum_score"

    offset = (page - 1) * page_size

    async with pool.acquire() as conn:

        # Resolve trade date — keep as datetime.date for asyncpg
        if date:
            try:
                trade_date = datetime.date.fromisoformat(date)
            except ValueError:
                raise HTTPException(422, "Invalid date format, use YYYY-MM-DD")
        else:
            row = await conn.fetchrow("select trade_date from v_latest_date")
            if not row or not row["trade_date"]:
                return {"date": None, "total": 0, "page": 1,
                        "page_size": page_size, "pages": 0, "data": []}
            trade_date = row["trade_date"]

        # Build dynamic WHERE
        params     = [trade_date]
        conditions = ["tr.trade_date = $1", "s.is_active = true"]
        p = 2

        if min_trending > 0:
            conditions.append(f"tr.trending_days >= ${p}")
            params.append(min_trending); p += 1
        if min_rsi > 0:
            conditions.append(f"tr.rsi_14 >= ${p}")
            params.append(min_rsi); p += 1
        if min_adx > 0:
            conditions.append(f"tr.adx_14 >= ${p}")
            params.append(min_adx); p += 1
        if min_chg_12d > -999:
            conditions.append(f"tr.chg_12d >= ${p}")
            params.append(min_chg_12d); p += 1
        if min_momentum > 0:
            conditions.append(f"tr.momentum_score >= ${p}")
            params.append(min_momentum); p += 1
        if min_trades > 0:
            conditions.append(f"tr.total_trades >= ${p}")
            params.append(min_trades); p += 1
        if sector:
            conditions.append(f"s.sector = ${p}")
            params.append(sector); p += 1
        if cap_category:
            if cap_category not in ("Large Cap", "Mid Cap", "Small Cap"):
                raise HTTPException(422, "cap_category must be Large Cap, Mid Cap, or Small Cap")
            conditions.append(f"s.cap_category = ${p}")
            params.append(cap_category); p += 1
        if near_52w_high is not None:
            conditions.append(f"tr.near_52w_high = ${p}")
            params.append(near_52w_high); p += 1
        if macd_bullish is not None:
            conditions.append("tr.macd_hist > 0" if macd_bullish else "tr.macd_hist <= 0")
        if ema_signal in ("golden_cross", "above_200", "approaching"):
            conditions.append(f"tr.ema_signal = ${p}")
            params.append(ema_signal); p += 1

        where_clause = " and ".join(conditions)
        sort_dir     = "asc" if order == "asc" else "desc"

        count_sql = f"""
            select count(*)
            from trend_results tr
            join symbols s on s.symbol = tr.symbol
            where {where_clause}
        """
        total = await conn.fetchval(count_sql, *params)

        data_sql = f"""
            select
                s.symbol, s.company_name, s.sector, s.cap_category, s.isin,
                tr.trending_days,
                tr.chg_1d, tr.chg_3d, tr.chg_5d, tr.chg_12d,
                tr.chg_1m, tr.chg_2m, tr.chg_3m, tr.chg_6m, tr.chg_12m,
                tr.high_52w, tr.pct_from_high, tr.near_52w_high, tr.rank_52w,
                tr.rsi_14, tr.rsi_1d, tr.rsi_1w, tr.adx_14,
                tr.macd_line, tr.macd_signal, tr.macd_hist,
                tr.ema_50, tr.ema_200, tr.ema_signal,
                tr.ema_9, tr.ema_21,
                tr.rs_score, tr.weighted_rpi,
                tr.rpi_2w, tr.rpi_3m, tr.rpi_6m, tr.rpi_6m_sma2w,
                tr.momentum_score,
                tr.open_price, tr.high_price, tr.low_price, tr.close_price,
                tr.volume, tr.total_trades,
                tr.bool_matrix, tr.pct_matrix
            from trend_results tr
            join symbols s on s.symbol = tr.symbol
            where {where_clause}
            order by tr.{sort_by} {sort_dir} nulls last
            limit ${p} offset ${p+1}
        """
        params.extend([page_size, offset])
        rows = await conn.fetch(data_sql, *params)

    pages = math.ceil(total / page_size) if page_size and total else 0
    return {
        "date":      str(trade_date),
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     pages,
        "data":      [dict(r) for r in rows],
    }


@router.get("/dates", summary="List available trading dates")
async def get_available_dates(
    limit: int = Query(30, ge=1, le=252),
    pool=Depends(get_pool),
):
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """select trade_date, symbol_count, engine_duration_secs
               from market_calendar
               where engine_status = 'done'
               order by trade_date desc limit $1""",
            limit,
        )
    return {"dates": [dict(r) for r in rows]}