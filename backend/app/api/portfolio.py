from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
import math
from typing import Optional

import asyncpg
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import current_user
from app.core.database import get_pool
from app.models.portfolio import (
    HoldingBuyMore,
    HoldingCreate,
    HoldingSell,
    HoldingUpdate,
    PortfolioCreate,
)
from app.services import ai_analysis
from app.services import portfolio_history as ph
from app.services import portfolio_intelligence as pi
from app.services import verdict_engine
from app.services.ai_analysis import AIAnalysisError
from app.services.portfolio_analytics import compute_peak_drawdowns, concentration_risk, portfolio_xirr
from app.services.portfolio_import import parse_portfolio_file

logger = logging.getLogger(__name__)
router = APIRouter()

# Statuses that still represent live market exposure (as opposed to SOLD =
# fully exited, or ARCHIVED = soft-deleted). Position Score / Portfolio
# Health / AI Advisor / Morning Brief all operate on these.
OPEN_STATUSES = ("ACTIVE", "PARTIAL")

STATUS_LABELS = {
    "ACTIVE": "Active",
    "PARTIAL": "Partial Exit",
    "SOLD": "Fully Exited",
    "ARCHIVED": "Archived",
}


def num(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def as_dict(row):
    data = dict(row)
    return {k: num(v) for k, v in data.items()}


async def ensure_portfolio(conn, user_id) -> int:
    pid = await conn.fetchval("select id from portfolios where user_id = $1 order by created_at limit 1", user_id)
    if pid:
        return pid
    return await conn.fetchval(
        "insert into portfolios (user_id, portfolio_name, broker, is_active) values ($1, 'My Portfolio', 'manual', true) returning id",
        user_id,
    )


async def assert_symbol(conn, symbol: str):
    exists = await conn.fetchval("select true from symbols where symbol = $1 and is_active = true", symbol)
    if not exists:
        raise HTTPException(422, f"Unknown symbol: {symbol}")


async def get_holding_or_404(conn, holding_id: int, portfolio_id: int):
    row = await conn.fetchrow(
        "select * from holdings where id = $1 and portfolio_id = $2 and not is_archived",
        holding_id, portfolio_id,
    )
    if not row:
        raise HTTPException(404, "Holding not found")
    return row


async def has_sell_history(conn, holding_id: int) -> bool:
    return bool(await conn.fetchval(
        "select true from holding_transactions where holding_id = $1 and txn_type = 'SELL' limit 1",
        holding_id,
    ))


def determine_status(quantity: Decimal, had_sell_history: bool) -> str:
    if quantity <= 0:
        return "SOLD"
    return "PARTIAL" if had_sell_history else "ACTIVE"


async def fetch_screener_candidates(conn, trade_date, exclude_symbols: set[str], min_momentum: float = 55, limit: int = 200):
    """Reuses the same trend_results/symbols data the Screener page reads from
    to source Capital Rotation and Opportunity Queue candidates."""
    rows = await conn.fetch(
        """
        select s.symbol, s.company_name, s.sector, s.cap_category,
               tr.momentum_score, tr.rs_score, tr.trending_days, tr.chg_12d, tr.chg_5d, tr.chg_1d,
               tr.rank_52w, tr.near_52w_high, tr.ema_signal, tr.macd_hist, tr.close_price
        from trend_results tr
        join symbols s on s.symbol = tr.symbol
        where tr.trade_date = $1 and s.is_active = true and tr.momentum_score >= $2
        order by tr.momentum_score desc nulls last
        limit $3
        """,
        trade_date, min_momentum, limit,
    )
    return [as_dict(r) for r in rows if r["symbol"] not in exclude_symbols]


@router.get("", summary="List user portfolios")
async def list_portfolios(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        await ensure_portfolio(conn, user["id"])
        rows = await conn.fetch("select id, portfolio_name, created_at, updated_at from portfolios where user_id = $1 order by created_at", user["id"])
    return {"data": [as_dict(r) for r in rows]}


@router.post("", summary="Create portfolio")
async def create_portfolio(payload: PortfolioCreate, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "insert into portfolios (user_id, portfolio_name, broker, is_active) values ($1, $2, 'manual', true) returning id, portfolio_name, created_at, updated_at",
            user["id"], payload.portfolio_name.strip(),
        )
    return as_dict(row)


@router.get("/summary", summary="Portfolio Intelligence dashboard: health, morning brief, rotation and analytics")
async def portfolio_summary(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        rows = await enriched_holdings(conn, pid, scope="all")
        active = [r for r in rows if r["status"] in OPEN_STATUSES]
        sold = [r for r in rows if r["status"] == "SOLD"]

        trade_date_row = await conn.fetchrow("select trade_date from v_latest_date")
        trade_date = trade_date_row["trade_date"] if trade_date_row else None

        concentration = concentration_risk(active)
        benchmark = await pi.fetch_market_benchmark(conn, trade_date) if trade_date else None
        health = pi.calculate_portfolio_health(active, concentration, benchmark)

        rotation, opportunities, morning_brief, previous_health = [], [], None, None
        if trade_date and active:
            held_symbols = {r["symbol"] for r in active}
            screener_candidates = await fetch_screener_candidates(conn, trade_date, held_symbols)
            sector_momentum = await pi.fetch_sector_momentum(conn, trade_date)
            rotation = pi.find_rotation_candidates(active, screener_candidates, sector_momentum)
            opportunities = pi.build_opportunity_queue(screener_candidates, held_symbols, sector_momentum)

            prev_date = await pi.fetch_previous_trade_date(conn, trade_date)
            if prev_date:
                prev_snapshot = await pi.fetch_trend_snapshot(conn, list(held_symbols), prev_date)
                prev_scored = []
                for h in active:
                    snap = prev_snapshot.get(h["symbol"])
                    if not snap:
                        continue
                    prev_row = dict(h)
                    prev_row.update(snap)
                    close = snap.get("close_price")
                    if close and h.get("avg_buy_price"):
                        prev_row["gain_pct"] = (float(close) - float(h["avg_buy_price"])) / float(h["avg_buy_price"]) * 100
                    intel = pi.calculate_position_score(prev_row, sector_momentum.get(h.get("sector")))
                    prev_row.update(intel)
                    prev_row["current_value"] = float(h.get("quantity") or 0) * float(close or 0)
                    prev_scored.append(prev_row)
                if prev_scored:
                    previous_health = pi.calculate_portfolio_health(prev_scored, concentration, benchmark)

            morning_brief = pi.generate_morning_brief(active, health, previous_health, rotation)

    winning = [r for r in active if float(r["unrealized_pnl"] or 0) > 0]
    losing = [r for r in active if float(r["unrealized_pnl"] or 0) < 0]
    total_investment = sum(float(r["investment_amount"] or 0) for r in active)
    current_value = sum(float(r["current_value"] or 0) for r in active)
    unrealized_pnl = current_value - total_investment
    realized_pnl = sum(float(r.get("realized_pnl") or 0) for r in rows)  # includes partial exits + fully exited
    today_pnl = sum(float(r.get("today_change") or 0) for r in active)
    today_base = current_value - today_pnl  # yesterday's equivalent value of today's open holdings
    today_return_pct = (today_pnl / today_base * 100) if today_base else 0

    sector_totals = {}
    for r in active:
        sector = r.get("sector") or "Unclassified"
        sector_totals[sector] = sector_totals.get(sector, 0) + float(r["current_value"] or 0)
    dead = [r for r in active if (r.get("days_held") or 0) > 180 and -5 <= float(r.get("gain_pct") or 0) <= 5]
    trailing_stop_watch = sorted(
        [r for r in active if (r.get("drawdown_from_peak_pct") or 0) >= 12],
        key=lambda r: float(r.get("drawdown_from_peak_pct") or 0),
        reverse=True,
    )

    return {
        "portfolio_id": pid,
        "date": str(trade_date) if trade_date else None,
        "portfolio_health": health,
        "morning_brief": morning_brief,
        "capital_rotation": rotation,
        "opportunity_queue": opportunities,
        "cards": {
            # Part 5 — Portfolio Summary Cards
            "total_investment": total_investment,
            "current_value": current_value,
            "unrealized_pnl": unrealized_pnl,
            "realized_pnl": realized_pnl,
            "return_pct": ((current_value - total_investment) / total_investment * 100) if total_investment else 0,
            "number_of_holdings": len(active),
            "xirr_pct": portfolio_xirr(active),
            "winning_holdings": len(winning),
            "losing_holdings": len(losing),
            "fully_exited_holdings": len(sold),
            # Part 6 — Today's Performance
            "today_pnl": today_pnl,
            "today_return_pct": today_return_pct,
            # Back-compat aliases for older frontend builds
            "profit_loss": unrealized_pnl,
        },
        "risk": concentration,
        "tax_estimate": None,
        "top_gainers": sorted(active, key=lambda r: float(r.get("gain_pct") or 0), reverse=True)[:5],
        "top_losers": sorted(active, key=lambda r: float(r.get("gain_pct") or 0))[:5],
        "winners_vs_losers": {
            "winning_capital_pct": (sum(float(r["current_value"] or 0) for r in winning) / current_value * 100) if current_value else 0,
            "losing_capital_pct": (sum(float(r["current_value"] or 0) for r in losing) / current_value * 100) if current_value else 0,
        },
        "sector_allocation": [{"sector": k, "value": v, "pct": (v / current_value * 100) if current_value else 0} for k, v in sorted(sector_totals.items())],
        "dead_money": dead,
        "trailing_stop_watch": trailing_stop_watch,
        "verdict_summary": {
            v: len([r for r in active if r.get("verdict") == v])
            for v in ("ADD_MORE", "HOLD", "TRIM", "EXIT")
        },
        "capital_drains": sorted(
            [r for r in active if r.get("capital_flag") == "draining"],
            key=lambda r: float(r.get("unrealized_pnl") or 0),
        ),
        "value_creators": sorted(
            [r for r in active if r.get("capital_flag") == "creating"],
            key=lambda r: float(r.get("unrealized_pnl") or 0),
            reverse=True,
        ),
    }


async def apply_peak_drawdowns(conn, active: list[dict]) -> None:
    """Trailing-stop data: highest close since each holding's buy date, and how
    far the current price has pulled back from that peak. Mutates `active` in place."""
    buy_dates = [h["buy_date"] for h in active if h.get("buy_date")]
    if not buy_dates:
        return
    symbols = list({h["symbol"] for h in active})
    rows = await conn.fetch(
        """
        select symbol, trade_date, close_price
        from price_history
        where symbol = any($1::text[]) and trade_date >= $2
        """,
        symbols, min(buy_dates),
    )
    price_rows = [as_dict(r) for r in rows]
    drawdowns = compute_peak_drawdowns(price_rows, active)
    for h in active:
        dd = drawdowns.get(h["id"])
        h["peak_price"] = dd["peak_price"] if dd else None
        h["drawdown_from_peak_pct"] = dd["drawdown_from_peak_pct"] if dd else None
        h["days_below_cost_streak"] = dd["days_below_cost_streak"] if dd else None
        h["days_above_cost_streak"] = dd["days_above_cost_streak"] if dd else None


def _apply_today_change(d: dict) -> None:
    """today_change: rupee P/L attributable to today's move alone, derived
    from chg_1d (%) which the momentum engine already computes. prev_close
    is backed out algebraically rather than requiring a second price row."""
    chg_1d = d.get("chg_1d")
    qty = float(d.get("quantity") or 0)
    current_price = d.get("current_price")
    if chg_1d is None or current_price is None or qty <= 0:
        d["today_change"] = None
        d["today_change_pct"] = None
        return
    chg_1d = float(chg_1d)
    denom = 100 + chg_1d
    if abs(denom) < 1e-9:
        d["today_change"] = None
        d["today_change_pct"] = None
        return
    prev_close = float(current_price) / (denom / 100)
    d["today_change"] = round(qty * (float(current_price) - prev_close), 2)
    d["today_change_pct"] = round(chg_1d, 2)


async def enriched_holdings(conn, portfolio_id: int, scope: str = "open"):
    """scope: 'open' (ACTIVE/PARTIAL only), 'sold' (fully exited history),
    or 'all' (open + sold, still excluding archived rows)."""
    if scope == "open":
        status_filter = "and h.status in ('ACTIVE','PARTIAL')"
    elif scope == "sold":
        status_filter = "and h.status = 'SOLD'"
    else:
        status_filter = "and h.status in ('ACTIVE','PARTIAL','SOLD')"

    rows = await conn.fetch(
        f"""
        with latest as (select trade_date from v_latest_date)
        select h.*, not h.buy_date_confirmed as requires_confirmation,
               s.company_name, s.sector, s.cap_category,
               tr.close_price as current_price, tr.momentum_score, tr.rs_score, tr.trending_days,
               tr.chg_1d, tr.chg_5d, tr.chg_12d, tr.rsi_14, tr.adx_14,
               tr.pct_from_high, tr.near_52w_high, tr.rank_52w, tr.high_52w,
               tr.ema_signal, tr.macd_hist, tr.volume, tr.total_trades,
               greatest(coalesce(h.sell_date, current_date) - h.buy_date, 0) as days_held,
               case when h.status = 'SOLD' then (h.total_bought_quantity * h.avg_buy_price)
                    else (h.quantity * h.avg_buy_price) end as investment_amount,
               case when h.status in ('ACTIVE','PARTIAL') then (h.quantity * tr.close_price) else 0 end as current_value,
               case when h.status in ('ACTIVE','PARTIAL') then (h.quantity * (tr.close_price - h.avg_buy_price)) else 0 end as unrealized_pnl,
               case
                   when h.status in ('ACTIVE','PARTIAL') and h.avg_buy_price > 0
                       then ((coalesce(tr.close_price, h.avg_buy_price) - h.avg_buy_price) / h.avg_buy_price * 100)
                   when h.status = 'SOLD' and h.avg_buy_price > 0 and h.total_bought_quantity > 0
                       then (h.realized_pnl / (h.total_bought_quantity * h.avg_buy_price) * 100)
               end as gain_pct
        from holdings h
        join symbols s on s.symbol = h.symbol
        left join latest l on true
        left join trend_results tr on tr.symbol = h.symbol and tr.trade_date = l.trade_date
        where h.portfolio_id = $1 and not h.is_archived {status_filter}
        order by h.status, h.created_at desc
        """,
        portfolio_id,
    )
    result = [as_dict(row) for row in rows]

    # Back-compat: several places (frontend + this module's own /summary math)
    # still read "profit_loss" as a generic P/L column — unrealized for open
    # positions, realized for fully-exited ones.
    for d in result:
        d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
        d["profit_loss"] = d["unrealized_pnl"] if d["status"] in OPEN_STATUSES else d.get("realized_pnl")

    active = [d for d in result if d["status"] in OPEN_STATUSES]
    if active:
        trade_date_row = await conn.fetchrow("select trade_date from v_latest_date")
        trade_date = trade_date_row["trade_date"] if trade_date_row else None
        symbols = [d["symbol"] for d in active]
        sector_momentum = await pi.fetch_sector_momentum(conn, trade_date) if trade_date else {}
        volume_ratios = await pi.fetch_volume_ratios(conn, symbols, trade_date) if trade_date else {}
        rs_streaks = await pi.fetch_rs_streaks(conn, symbols, trade_date) if trade_date else {}
        total_value = sum(float(d.get("current_value") or 0) for d in active)

        for d in active:
            streak = rs_streaks.get(d["symbol"])
            d["rel_streak_direction"] = streak["direction"] if streak else None
            d["rel_streak_days"] = streak["days"] if streak else None
            weight_pct = (float(d.get("current_value") or 0) / total_value * 100) if total_value else None
            d["portfolio_contribution"] = weight_pct
            intel = pi.calculate_position_score(
                d,
                sector_momentum=sector_momentum.get(d.get("sector")),
                volume_ratio=volume_ratios.get(d["symbol"]),
                weight_pct=weight_pct,
            )
            d.update(intel)
            if d.get("days_held") and d.get("gain_pct") is not None:
                d["annualized_return"] = ((1 + d["gain_pct"] / 100) ** (365 / max(d["days_held"], 1)) - 1) * 100
            else:
                d["annualized_return"] = None
            _apply_today_change(d)

        await apply_peak_drawdowns(conn, active)

        # Verdicts need the full picture (scores + drawdowns + streaks),
        # so they run last. History is recorded per trading day so the UI
        # can show how long each verdict has been in effect.
        verdict_engine.evaluate_portfolio(active)
        if trade_date:
            await record_verdict_history(conn, trade_date, active)

    return result


async def record_verdict_history(conn, trade_date, active: list[dict]) -> None:
    """Persist today's verdict per holding (idempotent upsert) and annotate
    each row with verdict_since / verdict_age_sessions from the history.
    Degrades to a no-op if migration v7 (holding_verdicts) hasn't run yet."""
    try:
        await conn.executemany(
            """
            insert into holding_verdicts (holding_id, portfolio_id, symbol, trade_date,
                                          verdict, confidence, position_score, gain_pct, reasons)
            values ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            on conflict (holding_id, trade_date) do update set
                verdict = excluded.verdict,
                confidence = excluded.confidence,
                position_score = excluded.position_score,
                gain_pct = excluded.gain_pct,
                reasons = excluded.reasons
            """,
            [
                (h["id"], h["portfolio_id"], h["symbol"], trade_date,
                 h["verdict"], h.get("verdict_confidence"),
                 h.get("position_score"), h.get("gain_pct"), h.get("verdict_reasons"))
                for h in active
            ],
        )
        history = await conn.fetch(
            """
            select holding_id, trade_date, verdict
            from holding_verdicts
            where holding_id = any($1::bigint[]) and trade_date <= $2
            order by holding_id, trade_date desc
            """,
            [h["id"] for h in active], trade_date,
        )
    except asyncpg.UndefinedTableError:
        logger.warning("holding_verdicts table missing — run sql/migration_v7_verdicts_watchlist.sql")
        for h in active:
            h["verdict_since"] = None
            h["verdict_age_sessions"] = None
        return

    by_holding: dict[int, list] = {}
    for r in history:
        by_holding.setdefault(r["holding_id"], []).append(r)
    for h in active:
        rows_h = by_holding.get(h["id"], [])
        since, age = None, 0
        for r in rows_h:  # newest first
            if r["verdict"] != h["verdict"]:
                break
            since = r["trade_date"]
            age += 1
        h["verdict_since"] = str(since) if since else None
        h["verdict_age_sessions"] = age or None


@router.get("/holdings", summary="List continuously tracked holdings with dynamic sorting and row numbering")
async def list_holdings(
    sort_by: Optional[str] = None, 
    sort_order: Optional[str] = "desc", 
    user=Depends(current_user), 
    pool=Depends(get_pool)
):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        rows = await enriched_holdings(conn, pid, scope="all")
        
    total_value = sum(float(r.get("current_value") or 0) for r in rows if r["status"] in OPEN_STATUSES)
    
    for r in rows:
        if r["status"] in OPEN_STATUSES:
            r["portfolio_contribution"] = (float(r.get("current_value") or 0) / total_value * 100) if total_value else 0

    # 1. Apply Dynamic Sorting if requested
    if sort_by:
        reverse = sort_order.lower() != "asc"
        
        def get_sort_val(row_dict):
            val = row_dict.get(sort_by)
            # Ensure numbers stick together and strings stick together to prevent TypeErrors
            if isinstance(val, (int, float, Decimal)):
                return (0, float(val))
            elif val is not None:
                return (1, str(val).lower())
            else:
                # Send null/None values to the bottom regardless of sort order
                return (2, 0) if not reverse else (-1, 0)
                
        try:
            rows.sort(key=get_sort_val, reverse=reverse)
        except Exception:
            pass # Failsafe: if an unexpected sorting clash happens, retain default DB order

    # 2. Append Serial Number based on the final order
    for index, r in enumerate(rows, start=1):
        r["s_no"] = index

    return {"data": rows}


@router.post("/holdings", summary="Add holding manually")
async def add_holding(payload: HoldingCreate, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        await assert_symbol(conn, payload.symbol)
        duplicate = await conn.fetchval(
            "select true from holdings where portfolio_id = $1 and symbol = $2 and status in ('ACTIVE','PARTIAL') and not is_archived",
            pid, payload.symbol,
        )
        if duplicate:
            raise HTTPException(409, "This symbol already exists as an open holding. Use Buy More on the existing row instead.")
        async with conn.transaction():
            try:
                row = await conn.fetchrow(
                    """
                    insert into holdings (portfolio_id, user_id, symbol, quantity, avg_buy_price, buy_date,
                                           buy_date_confirmed, total_bought_quantity)
                    values ($1, $2, $3, $4, $5, $6, true, $4) returning *
                    """,
                    pid, user["id"], payload.symbol, payload.quantity, payload.avg_buy_price, payload.buy_date,
                )
            except asyncpg.UniqueViolationError:
                # Concurrent insert beat the duplicate pre-check (uq_holdings_open_symbol)
                raise HTTPException(409, "This symbol already exists as an open holding. Use Buy More on the existing row instead.")
            await conn.execute(
                """
                insert into holding_transactions (holding_id, portfolio_id, symbol, txn_type, quantity, price, txn_date, charges, notes)
                values ($1, $2, $3, 'BUY', $4, $5, $6, 0, 'Initial purchase')
                """,
                row["id"], pid, payload.symbol, payload.quantity, payload.avg_buy_price, payload.buy_date,
            )
    d = as_dict(row)
    d["requires_confirmation"] = not d.pop("buy_date_confirmed")
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    return d


@router.patch("/holdings/{holding_id}", summary="Edit holding (buy date / confirmation, or correct a still-untouched single-lot buy)")
async def edit_holding(holding_id: int, payload: HoldingUpdate, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        current = await get_holding_or_404(conn, holding_id, pid)

        if payload.quantity is not None or payload.avg_buy_price is not None:
            txn_count = await conn.fetchval(
                "select count(*) from holding_transactions where holding_id = $1", holding_id
            )
            sold_any = await has_sell_history(conn, holding_id)
            if txn_count != 1 or sold_any:
                raise HTTPException(
                    409,
                    "Quantity/average buy price can no longer be edited directly once this holding has "
                    "Buy More or Sell Position activity. Use Buy More or Sell Position instead so realized "
                    "P/L and the transaction ledger stay accurate.",
                )

        confirmed_value = None if payload.requires_confirmation is None else (not payload.requires_confirmation)
        async with conn.transaction():
            row = await conn.fetchrow(
                """
                update holdings set
                    quantity = coalesce($3, quantity),
                    avg_buy_price = coalesce($4, avg_buy_price),
                    total_bought_quantity = coalesce($3, total_bought_quantity),
                    buy_date = coalesce($5, buy_date),
                    buy_date_confirmed = coalesce($6, buy_date_confirmed),
                    updated_at = now()
                where id = $1 and portfolio_id = $2
                returning *
                """,
                holding_id, pid, payload.quantity, payload.avg_buy_price, payload.buy_date, confirmed_value,
            )
            if payload.quantity is not None or payload.avg_buy_price is not None:
                await conn.execute(
                    """
                    update holding_transactions set quantity = coalesce($2, quantity), price = coalesce($3, price),
                           txn_date = coalesce($4, txn_date)
                    where holding_id = $1 and txn_type = 'BUY'
                    """,
                    holding_id, payload.quantity, payload.avg_buy_price, payload.buy_date,
                )
            elif payload.buy_date is not None:
                # Correcting just the buy date must also reach the ledger, or
                # performance-history reconstruction keeps using the old date.
                # Only unambiguous with a single BUY lot — otherwise leave the
                # ledger alone (it is the source of truth for multi-lot rows).
                await conn.execute(
                    """
                    update holding_transactions set txn_date = $2
                    where holding_id = $1 and txn_type = 'BUY'
                      and (select count(*) from holding_transactions where holding_id = $1 and txn_type = 'BUY') = 1
                    """,
                    holding_id, payload.buy_date,
                )
    d = as_dict(row)
    d["requires_confirmation"] = not d.pop("buy_date_confirmed")
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    return d


@router.post("/holdings/{holding_id}/buy", summary="Buy More — additional purchase of an existing (or previously fully-exited) holding")
async def buy_more(holding_id: int, payload: HoldingBuyMore, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        async with conn.transaction():
            current = await conn.fetchrow(
                "select * from holdings where id = $1 and portfolio_id = $2 and not is_archived for update",
                holding_id, pid,
            )
            if not current:
                raise HTTPException(404, "Holding not found. If it was deleted, restore it first.")

            old_qty = Decimal(current["quantity"])
            old_avg = Decimal(current["avg_buy_price"])
            new_qty = old_qty + payload.quantity
            new_avg = ((old_qty * old_avg) + (payload.quantity * payload.price)) / new_qty
            new_total_bought = Decimal(current["total_bought_quantity"]) + payload.quantity

            is_reentry = old_qty <= 0  # buying back into a fully-exited holding
            if is_reentry:
                # A fresh position: it starts on the new purchase date, at the
                # new price, as ACTIVE. Prior sell history stays in the ledger
                # (and in cumulative realized_pnl) but must not make this new
                # lot look old ("days held") or partially exited.
                new_status = "ACTIVE"
                new_buy_date = payload.buy_date
            else:
                sold_any = await has_sell_history(conn, holding_id)
                new_status = determine_status(new_qty, sold_any)
                new_buy_date = min(current["buy_date"], payload.buy_date) if current["buy_date"] else payload.buy_date

            row = await conn.fetchrow(
                """
                update holdings set
                    quantity = $3,
                    avg_buy_price = $4,
                    total_bought_quantity = $5,
                    status = $6,
                    buy_date = $7,
                    sell_date = case when $8 then null else sell_date end,
                    sell_price = case when $8 then null else sell_price end,
                    updated_at = now()
                where id = $1 and portfolio_id = $2
                returning *
                """,
                holding_id, pid, new_qty, new_avg, new_total_bought, new_status, new_buy_date, is_reentry,
            )
            await conn.execute(
                """
                insert into holding_transactions (holding_id, portfolio_id, symbol, txn_type, quantity, price, txn_date, charges, notes)
                values ($1, $2, $3, 'BUY', $4, $5, $6, $7, $8)
                """,
                holding_id, pid, current["symbol"], payload.quantity, payload.price, payload.buy_date,
                payload.charges, payload.notes,
            )
    d = as_dict(row)
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    return d


@router.post("/holdings/{holding_id}/sell", summary="Sell Position — partial or full sale of an existing holding")
async def sell_position(holding_id: int, payload: HoldingSell, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        async with conn.transaction():
            current = await conn.fetchrow(
                "select * from holdings where id = $1 and portfolio_id = $2 and not is_archived for update",
                holding_id, pid,
            )
            if not current:
                raise HTTPException(404, "Holding not found")
            if current["status"] not in OPEN_STATUSES:
                raise HTTPException(409, "This holding has no open quantity left to sell.")

            held_qty = Decimal(current["quantity"])
            if payload.quantity > held_qty:
                raise HTTPException(
                    400,
                    f"Cannot sell {payload.quantity} shares — only {held_qty} are currently held.",
                )

            avg_buy_price = Decimal(current["avg_buy_price"])  # unchanged — Part 2 of the spec
            realized_pnl_txn = payload.quantity * (payload.sell_price - avg_buy_price) - payload.charges
            remaining_qty = held_qty - payload.quantity
            new_status = "SOLD" if remaining_qty <= 0 else "PARTIAL"
            new_realized_total = Decimal(current["realized_pnl"]) + realized_pnl_txn

            row = await conn.fetchrow(
                """
                update holdings set
                    quantity = $3,
                    status = $4,
                    realized_pnl = $5,
                    sell_date = $6,
                    sell_price = $7,
                    updated_at = now()
                where id = $1 and portfolio_id = $2
                returning *
                """,
                holding_id, pid, remaining_qty, new_status, new_realized_total, payload.sell_date, payload.sell_price,
            )
            await conn.execute(
                """
                insert into holding_transactions (holding_id, portfolio_id, symbol, txn_type, quantity, price, txn_date, charges, realized_pnl, notes)
                values ($1, $2, $3, 'SELL', $4, $5, $6, $7, $8, $9)
                """,
                holding_id, pid, current["symbol"], payload.quantity, payload.sell_price, payload.sell_date,
                payload.charges, realized_pnl_txn, payload.notes,
            )
    d = as_dict(row)
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    d["realized_pnl_this_sale"] = num(realized_pnl_txn)
    return d


@router.delete("/holdings/{holding_id}", summary="Archive (soft-delete) a holding")
async def delete_holding(holding_id: int, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        archived = await conn.fetchval(
            """
            update holdings set is_archived = true, archived_at = now(), updated_at = now()
            where id = $1 and portfolio_id = $2 and not is_archived
            returning id
            """,
            holding_id, pid,
        )
    if not archived:
        raise HTTPException(404, "Holding not found")
    return {"message": "Holding archived. It no longer appears in your portfolio, but its history and transactions are preserved and it can be restored.", "archived": True}


@router.post("/holdings/{holding_id}/restore", summary="Restore a previously archived holding")
async def restore_holding(holding_id: int, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        row = await conn.fetchrow(
            """
            update holdings set is_archived = false, archived_at = null, updated_at = now()
            where id = $1 and portfolio_id = $2 and is_archived
            returning *
            """,
            holding_id, pid,
        )
    if not row:
        raise HTTPException(404, "Archived holding not found")
    d = as_dict(row)
    d["status_label"] = STATUS_LABELS.get(d["status"], d["status"])
    return d


@router.get("/holdings/{holding_id}/transactions", summary="Buy/Sell transaction ledger for a holding")
async def holding_transactions(holding_id: int, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        holding = await conn.fetchrow("select id, symbol from holdings where id = $1 and portfolio_id = $2", holding_id, pid)
        if not holding:
            raise HTTPException(404, "Holding not found")
        rows = await conn.fetch(
            """
            select id, txn_type, quantity, price, txn_date, charges, realized_pnl, notes, created_at
            from holding_transactions
            where holding_id = $1
            order by txn_date desc, id desc
            """,
            holding_id,
        )
    data = [as_dict(r) for r in rows]
    total_bought = sum(r["quantity"] for r in data if r["txn_type"] == "BUY")
    total_sold = sum(r["quantity"] for r in data if r["txn_type"] == "SELL")
    total_realized = sum(r["realized_pnl"] or 0 for r in data if r["txn_type"] == "SELL")
    return {
        "symbol": holding["symbol"],
        "data": data,
        "summary": {"total_bought": total_bought, "total_sold": total_sold, "total_realized_pnl": total_realized},
    }


@router.get("/performance-history", summary="Portfolio value over time (1M/3M/6M/1Y/ALL)")
async def performance_history(range: str = "3M", user=Depends(current_user), pool=Depends(get_pool)):
    range_key = range.upper()
    if range_key not in ph.RANGE_DAYS:
        raise HTTPException(422, "range must be one of: 1M, 3M, 6M, 1Y, ALL")
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        trade_date_row = await conn.fetchrow("select trade_date from v_latest_date")
        today = trade_date_row["trade_date"] if trade_date_row else date.today()
        series = await ph.get_performance_history(conn, pid, range_key, today)
    return {"range": range_key, "data": series}


@router.post("/import", summary="Import portfolio holdings from broker CSV/XLS/XLSX")
async def import_holdings(file: UploadFile = File(...), user=Depends(current_user), pool=Depends(get_pool)):
    content = await file.read()
    try:
        parsed, warnings = parse_portfolio_file(file.filename or "portfolio.csv", content)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    imported, skipped = [], []
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        valid_symbols = {r["symbol"] for r in await conn.fetch("select symbol from symbols where is_active = true")}
        isin_map = {r["isin"]: r["symbol"] for r in await conn.fetch("select isin, symbol from symbols where is_active = true and isin is not null")}
        for item in parsed:
            symbol = item.symbol
            if symbol not in valid_symbols and item.isin and item.isin in isin_map:
                symbol = isin_map[item.isin]  # e.g. "APOLLO MICRO SYSTEMS LTD" -> resolved via ISIN to the real ticker
            if symbol not in valid_symbols:
                skipped.append({"symbol": item.symbol, "reason": "Unknown symbol", "row": item.source_row})
                continue
            exists = await conn.fetchval(
                "select true from holdings where portfolio_id = $1 and symbol = $2 and status in ('ACTIVE','PARTIAL') and not is_archived",
                pid, symbol,
            )
            if exists:
                skipped.append({"symbol": symbol, "reason": "Duplicate open holding — use Buy More on the existing row instead", "row": item.source_row})
                continue
            async with conn.transaction():
                row = await conn.fetchrow(
                    """
                    insert into holdings (portfolio_id, user_id, symbol, quantity, avg_buy_price, buy_date,
                                           buy_date_confirmed, import_source, total_bought_quantity)
                    values ($1, $2, $3, $4, $5, $6, $7, $8, $4) returning *
                    """,
                    pid, user["id"], symbol, item.quantity, item.avg_buy_price, item.buy_date, not item.requires_confirmation, file.filename,
                )
                if item.buy_date is not None:
                    await conn.execute(
                        """
                        insert into holding_transactions
                        (holding_id, portfolio_id, symbol, txn_type,
                        quantity, price, txn_date, charges, notes)
                        values ($1, $2, $3, 'BUY', $4, $5, $6, 0, $7)
                        """,
                        row["id"],
                        pid,
                        symbol,
                        item.quantity,
                        item.avg_buy_price,
                        item.buy_date,
                        f"Imported from {file.filename}",
                    )
                
            d = as_dict(row)
            d["requires_confirmation"] = not d.pop("buy_date_confirmed")
            imported.append(d)
    return {"imported": imported, "skipped": skipped, "warnings": warnings, "requires_confirmation": [r for r in imported if r.get("requires_confirmation")]}


@router.post("/ai-advisor", summary="AI-powered Hold/Trim/Add More/Exit All verdicts for open holdings (Gemini)")
async def ai_advisor(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        active = await enriched_holdings(conn, pid, scope="open")

    if not active:
        return {"data": []}

    try:
        verdicts = await ai_analysis.analyze_holdings(active)
    except AIAnalysisError as exc:
        raise HTTPException(503, str(exc))

    data = []
    for h in active:
        v = verdicts.get(h["id"], {})
        data.append({
            "id": h["id"],
            "symbol": h["symbol"],
            "company_name": h.get("company_name"),
            "sector": h.get("sector"),
            "gain_pct": h.get("gain_pct"),
            "position_score": h.get("position_score"),
            "status_label": h.get("status_label"),
            "action": v.get("action", "HOLD"),
            "reasoning": v.get("reasoning") or "No reasoning returned.",
            "confidence": v.get("confidence", 50),
        })
    return {"data": data}


@router.get("/holdings/{holding_id}/verdicts", summary="Verdict history for a holding (how the engine's call evolved)")
async def holding_verdict_history(holding_id: int, limit: int = 90, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        holding = await conn.fetchrow("select id, symbol from holdings where id = $1 and portfolio_id = $2", holding_id, pid)
        if not holding:
            raise HTTPException(404, "Holding not found")
        try:
            rows = await conn.fetch(
                """
                select trade_date, verdict, confidence, position_score, gain_pct, reasons
                from holding_verdicts
                where holding_id = $1
                order by trade_date desc
                limit $2
                """,
                holding_id, min(max(limit, 1), 365),
            )
        except asyncpg.UndefinedTableError:
            raise HTTPException(503, "Verdict history is not available yet — run sql/migration_v7_verdicts_watchlist.sql")
    return {"symbol": holding["symbol"], "data": [as_dict(r) for r in rows]}


@router.get("/holdings/{holding_id}/trendline", summary="Holding trendline from buy date to current/sell date")
async def holding_trendline(holding_id: int, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        h = await conn.fetchrow("select * from holdings where id = $1 and portfolio_id = $2", holding_id, pid)
        if not h:
            raise HTTPException(404, "Holding not found")
        end_date = h["sell_date"] or date.today()
        rows = await conn.fetch(
            """
            select trade_date, close_price
            from price_history
            where symbol = $1 and trade_date between $2 and $3
            order by trade_date
            """,
            h["symbol"], h["buy_date"], end_date,
        )
    return {"symbol": h["symbol"], "start_date": h["buy_date"], "end_date": end_date, "data": [as_dict(r) for r in rows]}