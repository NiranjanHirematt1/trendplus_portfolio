"""
GET /api/symbol/{sym}           — full snapshot + history + peers
GET /api/symbol/{sym}/price     — OHLCV for charting
GET /api/symbol/{sym}/metrics   — daily indicator history
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, HTTPException
from app.core.database import get_pool

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/{sym}", summary="Symbol snapshot + analytics history")
async def get_symbol(
    sym:  str,
    days: int = Query(60, ge=1, le=252),
    pool=Depends(get_pool),
):
    sym = sym.upper().strip()

    async with pool.acquire() as conn:
        info = await conn.fetchrow("select * from symbols where symbol = $1", sym)
        if not info:
            raise HTTPException(404, f"Symbol '{sym}' not found")

        latest = await conn.fetchrow(
            """select
                trade_date, trending_days,
                chg_1d, chg_3d, chg_5d, chg_12d,
                chg_1m, chg_2m, chg_3m, chg_6m, chg_12m,
                high_52w, pct_from_high, near_52w_high, rank_52w,
                rsi_14, adx_14,
                macd_line, macd_signal, macd_hist,
                ema_50, ema_200, ema_signal,
                ema_9, ema_21,
                rs_score, weighted_rpi,
                rpi_2w, rpi_3m, rpi_6m, rpi_6m_sma2w,
                rsi_1d, rsi_1w,
                momentum_score,
                open_price, high_price, low_price, close_price,
                volume, total_trades,
                bool_matrix, pct_matrix
               from trend_results
               where symbol = $1
               order by trade_date desc limit 1""",
            sym,
        )

        history = await conn.fetch(
            """select
                trade_date, trending_days,
                chg_1d, chg_3d, chg_5d, chg_12d,
                chg_1m, chg_2m, chg_3m, chg_6m, chg_12m,
                rsi_14, adx_14,
                macd_line, macd_signal, macd_hist,
                ema_50, ema_200, ema_signal,
                ema_9, ema_21,
                rs_score, weighted_rpi,
                rpi_2w, rpi_3m, rpi_6m, rpi_6m_sma2w,
                rsi_1d, rsi_1w,
                momentum_score,
                pct_from_high, near_52w_high, high_52w,
                close_price, volume, total_trades
               from trend_results
               where symbol = $1
               order by trade_date desc limit $2""",
            sym, days,
        )

        peers = []
        if info["sector"] and latest:
            peers = await conn.fetch(
                """select
                    s.symbol, s.company_name, s.cap_category,
                    tr.momentum_score, tr.rs_score, tr.trending_days,
                    tr.chg_12d, tr.rsi_14, tr.close_price,
                    tr.ema_signal, tr.macd_hist, tr.total_trades
                   from trend_results tr
                   join symbols s on s.symbol = tr.symbol
                   where tr.trade_date = $1
                     and s.sector = $2
                     and s.symbol != $3
                   order by tr.momentum_score desc nulls last
                   limit 15""",
                latest["trade_date"], info["sector"], sym,
            )

    return {
        "symbol":  sym,
        "info":    dict(info),
        "latest":  dict(latest) if latest else None,
        "history": [dict(r) for r in history],
        "peers":   [dict(r) for r in peers],
    }


@router.get("/{sym}/price", summary="OHLCV price history")
async def get_symbol_price(
    sym:  str,
    days: int = Query(252, ge=1, le=756),
    pool=Depends(get_pool),
):
    sym = sym.upper().strip()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """select
                trade_date, open_price, high_price,
                low_price, close_price, volume,
                total_trades, prev_close
               from price_history
               where symbol = $1
               order by trade_date desc limit $2""",
            sym, days,
        )
    if not rows:
        raise HTTPException(404, f"No price history for '{sym}'")
    return {"symbol": sym, "count": len(rows), "data": [dict(r) for r in rows]}


@router.get("/{sym}/metrics", summary="Daily indicator history")
async def get_symbol_metrics(
    sym:  str,
    days: int = Query(90, ge=1, le=252),
    pool=Depends(get_pool),
):
    sym = sym.upper().strip()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """select
                trade_date, trending_days,
                chg_1d, chg_3d, chg_5d, chg_12d,
                chg_1m, chg_2m, chg_3m, chg_6m, chg_12m,
                rsi_14, adx_14,
                macd_line, macd_signal, macd_hist,
                ema_50, ema_200, ema_signal,
                ema_9, ema_21,
                rs_score, weighted_rpi,
                rpi_2w, rpi_3m, rpi_6m, rpi_6m_sma2w,
                rsi_1d, rsi_1w,
                momentum_score,
                pct_from_high, near_52w_high, high_52w,
                close_price, volume, total_trades
               from trend_results
               where symbol = $1
               order by trade_date desc limit $2""",
            sym, days,
        )
    if not rows:
        raise HTTPException(404, f"No metrics for '{sym}'")
    return {"symbol": sym, "count": len(rows), "data": [dict(r) for r in rows]}