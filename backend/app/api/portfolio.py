from __future__ import annotations

from datetime import date
from decimal import Decimal
import math

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import current_user
from app.core.database import get_pool
from app.models.portfolio import HoldingCreate, HoldingSold, HoldingUpdate, PortfolioCreate
from app.services.ai_analysis import AIAnalysisError, analyze_holdings
from app.services.portfolio_analytics import compute_peak_drawdowns, concentration_risk, portfolio_xirr
from app.services.portfolio_import import parse_portfolio_file

router = APIRouter()

# Rule-engine thresholds (tune here, not scattered through the code)
STOP_LOSS_PCT = -15                 # hard capital-protection floor, overrides technicals
TRAILING_STOP_DRAWDOWN_PCT = 20     # % fallen from post-buy high while momentum fades
TRAILING_STOP_MOMENTUM_MAX = 40     # momentum_score below this counts as "fading"
CONCENTRATION_TRIM_PCT = 25         # a single position above this % of the portfolio is a risk
DEAD_MONEY_DAYS = 180
DEAD_MONEY_BAND_PCT = 5


def num(value):
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def as_dict(row):
    data = dict(row)
    return {k: num(v) for k, v in data.items()}


def recommendation(row, portfolio_contribution_pct: float | None = None) -> dict:
    """Rule-based Hold/Trim/Add More/Exit verdict with a plain-English reason.

    Capital-protection rules (stop-loss, trailing-stop, concentration) are
    checked before the momentum/RS/trend score, because a trader needs to
    know "you're about to lose too much" even when the technicals still
    look fine.
    """
    if row.get("current_price") is None:
        return {"action": "REVIEW", "reason": "No recent price data for this symbol — check it's still tracked."}

    gain = float(row.get("gain_pct") or 0)
    momentum = float(row.get("momentum_score") or 0)
    rs = float(row.get("rs_score") or 0)
    trend = float(row.get("trending_days") or 0) / 12 * 100
    vol_penalty = min(abs(float(row.get("chg_5d") or 0)) * 2, 25)
    days_held = row.get("days_held") or 0
    drawdown = row.get("drawdown_from_peak_pct")
    near_high = row.get("near_52w_high")

    if gain <= STOP_LOSS_PCT:
        return {"action": "EXIT", "reason": f"Down {gain:.1f}% from entry — past the {abs(STOP_LOSS_PCT)}% stop-loss."}

    if drawdown is not None and drawdown >= TRAILING_STOP_DRAWDOWN_PCT and momentum < TRAILING_STOP_MOMENTUM_MAX:
        return {"action": "TRIM", "reason": f"{drawdown:.0f}% below its high since you bought, with fading momentum."}

    if portfolio_contribution_pct is not None and portfolio_contribution_pct >= CONCENTRATION_TRIM_PCT and gain > 0:
        return {"action": "TRIM", "reason": f"{portfolio_contribution_pct:.0f}% of your portfolio is in this one stock — trim to rebalance risk."}

    score = gain * 0.25 + momentum * 0.30 + rs * 0.20 + trend * 0.15 - vol_penalty * 0.10

    if days_held >= DEAD_MONEY_DAYS and abs(gain) <= DEAD_MONEY_BAND_PCT:
        return {"action": "TRIM", "reason": f"Flat for {days_held} days — capital may work harder elsewhere."}

    if near_high and score >= 55:
        return {"action": "ADD MORE", "reason": "Near its 52-week high with strong trend confirmation."}
    if score >= 65 and gain >= 0:
        return {"action": "ADD MORE", "reason": "Strong momentum, relative strength and trend support adding."}
    if score >= 45:
        return {"action": "HOLD", "reason": "Technicals are steady — no urgent action needed."}
    if score >= 25 or gain > 10:
        return {"action": "TRIM", "reason": "Momentum is fading — consider reducing position size."}
    return {"action": "EXIT", "reason": "Weak momentum, relative strength and trend strength."}


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


@router.get("/summary", summary="Portfolio dashboard and analytics")
async def portfolio_summary(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        rows = await enriched_holdings(conn, pid, active_only=False)
    apply_recommendations(rows)
    active = [r for r in rows if r["status"] == "ACTIVE"]
    total_investment = sum(float(r["investment_amount"] or 0) for r in active)
    current_value = sum(float(r["current_value"] or 0) for r in active)
    pnl = current_value - total_investment
    winning = [r for r in active if float(r["profit_loss"] or 0) > 0]
    losing = [r for r in active if float(r["profit_loss"] or 0) < 0]
    sector_totals = {}
    for r in active:
        sector = r.get("sector") or "Unclassified"
        sector_totals[sector] = sector_totals.get(sector, 0) + float(r["current_value"] or 0)
    dead = [r for r in active if (r.get("days_held") or 0) > DEAD_MONEY_DAYS and -DEAD_MONEY_BAND_PCT <= float(r.get("gain_pct") or 0) <= DEAD_MONEY_BAND_PCT]
    priced = [r for r in active if r.get("current_price") is not None]
    return {
        "portfolio_id": pid,
        "cards": {
            "total_investment": total_investment,
            "current_value": current_value,
            "profit_loss": pnl,
            "return_pct": (pnl / total_investment * 100) if total_investment else 0,
            "xirr_pct": portfolio_xirr(active),
            "number_of_holdings": len(active),
            "winning_holdings": len(winning),
            "losing_holdings": len(losing),
        },
        "risk": concentration_risk(active),
        "top_gainers": sorted(priced, key=lambda r: float(r.get("gain_pct") or 0), reverse=True)[:5],
        "top_losers": sorted(priced, key=lambda r: float(r.get("gain_pct") or 0))[:5],
        "winners_vs_losers": {
            "winning_capital_pct": (sum(float(r["current_value"] or 0) for r in winning) / current_value * 100) if current_value else 0,
            "losing_capital_pct": (sum(float(r["current_value"] or 0) for r in losing) / current_value * 100) if current_value else 0,
        },
        "sector_allocation": [{"sector": k, "value": v, "pct": (v / current_value * 100) if current_value else 0} for k, v in sorted(sector_totals.items())],
        "dead_money": dead,
        "needs_review": [r for r in active if r.get("recommendation", {}).get("action") == "REVIEW"],
    }


@router.post("/ai-analysis", summary="AI-powered Hold/Trim/Add More/Exit All analysis for active holdings")
async def ai_analysis(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        rows = await enriched_holdings(conn, pid, active_only=True)
    if not rows:
        return {"data": {}}
    try:
        result = await analyze_holdings(rows)
    except AIAnalysisError as exc:
        raise HTTPException(503, str(exc))
    return {"data": result}


async def enriched_holdings(conn, portfolio_id: int, active_only: bool = True):
    status_filter = "and h.status = 'ACTIVE'" if active_only else ""
    rows = await conn.fetch(
        f"""
        with latest as (select trade_date from v_latest_date)
        select h.*, not h.buy_date_confirmed as requires_confirmation,
               s.company_name, s.sector, s.cap_category,
               tr.close_price as current_price, tr.momentum_score, tr.rs_score, tr.trending_days,
               tr.chg_1d, tr.chg_5d, tr.chg_12d, tr.rsi_14, tr.adx_14,
               tr.near_52w_high, tr.pct_from_high, tr.high_52w,
               greatest(coalesce(h.sell_date, current_date) - h.buy_date, 0) as days_held,
               (h.quantity * h.avg_buy_price) as investment_amount,
               case when h.status = 'ACTIVE' then (h.quantity * tr.close_price) else (h.quantity * h.sell_price) end as current_value,
               case when h.status = 'ACTIVE' then (h.quantity * (tr.close_price - h.avg_buy_price)) else (h.quantity * (h.sell_price - h.avg_buy_price)) end as profit_loss,
               case when h.avg_buy_price > 0 then ((case when h.status = 'ACTIVE' then coalesce(tr.close_price, h.avg_buy_price) else h.sell_price end - h.avg_buy_price) / h.avg_buy_price * 100) end as gain_pct
        from holdings h
        join symbols s on s.symbol = h.symbol
        left join latest l on true
        left join trend_results tr on tr.symbol = h.symbol and tr.trade_date = l.trade_date
        where h.portfolio_id = $1 {status_filter}
        order by h.status, h.created_at desc
        """,
        portfolio_id,
    )
    result = []
    for row in rows:
        d = as_dict(row)
        if d["status"] == "ACTIVE" and d.get("days_held") and d.get("gain_pct") is not None:
            d["annualized_return"] = ((1 + d["gain_pct"] / 100) ** (365 / max(d["days_held"], 1)) - 1) * 100
        elif d["status"] == "ACTIVE":
            d["annualized_return"] = None
        result.append(d)

    # Peak-drawdown needs price history since the earliest buy date, fetched
    # once for the whole portfolio rather than per-holding.
    active = [r for r in result if r["status"] == "ACTIVE" and r.get("buy_date")]
    if active:
        symbols = list({r["symbol"] for r in active})
        earliest = min(r["buy_date"] for r in active)
        price_rows = await conn.fetch(
            "select symbol, trade_date, close_price from price_history where symbol = any($1) and trade_date >= $2",
            symbols, earliest,
        )
        drawdowns = compute_peak_drawdowns([dict(p) for p in price_rows], active)
        for r in result:
            if r["status"] == "ACTIVE":
                info = drawdowns.get(r["id"], {})
                r["peak_price_since_buy"] = info.get("peak_price")
                r["drawdown_from_peak_pct"] = info.get("drawdown_from_peak_pct")

    return result


def apply_recommendations(rows: list[dict]) -> None:
    """Sets portfolio_contribution and recommendation on ACTIVE rows in-place.
    Contribution must be known before concentration-aware rules can fire, so
    this always runs as a second pass after the portfolio total is known.
    """
    active = [r for r in rows if r["status"] == "ACTIVE"]
    total_value = sum(float(r.get("current_value") or 0) for r in active)
    for r in rows:
        if r["status"] != "ACTIVE":
            continue
        contribution = (float(r.get("current_value") or 0) / total_value * 100) if total_value else None
        r["portfolio_contribution"] = contribution
        r["recommendation"] = recommendation(r, contribution)


@router.get("/holdings", summary="List continuously tracked holdings")
async def list_holdings(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        rows = await enriched_holdings(conn, pid, active_only=False)
    apply_recommendations(rows)
    return {"data": rows}


@router.post("/holdings", summary="Add holding manually")
async def add_holding(payload: HoldingCreate, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        await assert_symbol(conn, payload.symbol)
        duplicate = await conn.fetchval("select true from holdings where portfolio_id = $1 and symbol = $2 and status = 'ACTIVE'", pid, payload.symbol)
        if duplicate:
            raise HTTPException(409, "This symbol already exists as an active holding. Edit the existing holding instead.")
        row = await conn.fetchrow(
            """
            insert into holdings (portfolio_id, user_id, symbol, quantity, avg_buy_price, buy_date, buy_date_confirmed)
            values ($1, $2, $3, $4, $5, $6, true) returning *
            """,
            pid, user["id"], payload.symbol, payload.quantity, payload.avg_buy_price, payload.buy_date,
        )
    d = as_dict(row)
    d["requires_confirmation"] = not d.pop("buy_date_confirmed")
    return d


@router.patch("/holdings/{holding_id}", summary="Edit holding")
async def edit_holding(holding_id: int, payload: HoldingUpdate, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        current = await conn.fetchrow("select * from holdings where id = $1 and portfolio_id = $2", holding_id, pid)
        if not current:
            raise HTTPException(404, "Holding not found")
        confirmed_value = None if payload.requires_confirmation is None else (not payload.requires_confirmation)
        row = await conn.fetchrow(
            """
            update holdings set
                quantity = coalesce($3, quantity),
                avg_buy_price = coalesce($4, avg_buy_price),
                buy_date = coalesce($5, buy_date),
                buy_date_confirmed = coalesce($6, buy_date_confirmed),
                updated_at = now()
            where id = $1 and portfolio_id = $2
            returning *
            """,
            holding_id, pid, payload.quantity, payload.avg_buy_price, payload.buy_date, confirmed_value,
        )
    d = as_dict(row)
    d["requires_confirmation"] = not d.pop("buy_date_confirmed")
    return d


@router.delete("/holdings/{holding_id}", summary="Delete holding")
async def delete_holding(holding_id: int, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        deleted = await conn.fetchval("delete from holdings where id = $1 and portfolio_id = $2 returning id", holding_id, pid)
    if not deleted:
        raise HTTPException(404, "Holding not found")
    return {"message": "Holding deleted"}


@router.post("/holdings/{holding_id}/sell", summary="Mark holding as sold")
async def mark_sold(holding_id: int, payload: HoldingSold, user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        row = await conn.fetchrow(
            """
            update holdings set status = 'SOLD', sell_date = $3, sell_price = $4, updated_at = now()
            where id = $1 and portfolio_id = $2 and status = 'ACTIVE'
            returning *
            """,
            holding_id, pid, payload.sell_date, payload.sell_price,
        )
    if not row:
        raise HTTPException(404, "Active holding not found")
    return as_dict(row)


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
            exists = await conn.fetchval("select true from holdings where portfolio_id = $1 and symbol = $2 and status = 'ACTIVE'", pid, symbol)
            if exists:
                skipped.append({"symbol": symbol, "reason": "Duplicate active holding", "row": item.source_row})
                continue
            row = await conn.fetchrow(
                """
                insert into holdings (portfolio_id, user_id, symbol, quantity, avg_buy_price, buy_date, buy_date_confirmed, import_source)
                values ($1, $2, $3, $4, $5, $6, $7, $8) returning *
                """,
                pid, user["id"], symbol, item.quantity, item.avg_buy_price, item.buy_date, not item.requires_confirmation, file.filename,
            )
            d = as_dict(row)
            d["requires_confirmation"] = not d.pop("buy_date_confirmed")
            imported.append(d)
    return {"imported": imported, "skipped": skipped, "warnings": warnings, "requires_confirmation": [r for r in imported if r.get("requires_confirmation")]}


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


@router.get("/holdings/sparklines", summary="Compact price sparklines for every active holding (one batched call)")
async def holdings_sparklines(user=Depends(current_user), pool=Depends(get_pool)):
    """Powers the inline mini-chart next to each holding in the table, the way
    Groww/Kite show a tiny trend line per position without opening a modal.
    Returns the last ~30 trading days of closes per symbol in one round trip
    instead of one request per row.
    """
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        symbols = [r["symbol"] for r in await conn.fetch(
            "select distinct symbol from holdings where portfolio_id = $1 and status = 'ACTIVE'", pid
        )]
        if not symbols:
            return {"data": {}}
        rows = await conn.fetch(
            """
            select symbol, trade_date, close_price
            from (
                select symbol, trade_date, close_price,
                       row_number() over (partition by symbol order by trade_date desc) as rn
                from price_history
                where symbol = any($1)
            ) ranked
            where rn <= 30
            order by symbol, trade_date
            """,
            symbols,
        )
    by_symbol: dict[str, list] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append({"trade_date": r["trade_date"], "close_price": num(r["close_price"])})
    return {"data": by_symbol}
