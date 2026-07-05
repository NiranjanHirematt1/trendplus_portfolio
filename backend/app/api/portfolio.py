from __future__ import annotations

from datetime import date
from decimal import Decimal
import math

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile

from app.api.deps import current_user
from app.core.database import get_pool
from app.models.portfolio import HoldingCreate, HoldingSold, HoldingUpdate, PortfolioCreate
from app.services import portfolio_intelligence as pi
from app.services.portfolio_analytics import concentration_risk, portfolio_xirr
from app.services.portfolio_import import parse_portfolio_file

router = APIRouter()


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


@router.get("/summary", summary="Portfolio Intelligence dashboard: health, morning brief, rotation and analytics")
async def portfolio_summary(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        rows = await enriched_holdings(conn, pid, active_only=False)
        active = [r for r in rows if r["status"] == "ACTIVE"]

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

    winning = [r for r in active if float(r["profit_loss"] or 0) > 0]
    losing = [r for r in active if float(r["profit_loss"] or 0) < 0]
    total_investment = sum(float(r["investment_amount"] or 0) for r in active)
    current_value = sum(float(r["current_value"] or 0) for r in active)
    sector_totals = {}
    for r in active:
        sector = r.get("sector") or "Unclassified"
        sector_totals[sector] = sector_totals.get(sector, 0) + float(r["current_value"] or 0)
    dead = [r for r in active if (r.get("days_held") or 0) > 180 and -5 <= float(r.get("gain_pct") or 0) <= 5]

    return {
        "portfolio_id": pid,
        "date": str(trade_date) if trade_date else None,
        "portfolio_health": health,
        "morning_brief": morning_brief,
        "capital_rotation": rotation,
        "opportunity_queue": opportunities,
        "cards": {
            "total_investment": total_investment,
            "current_value": current_value,
            "profit_loss": current_value - total_investment,
            "return_pct": ((current_value - total_investment) / total_investment * 100) if total_investment else 0,
            "xirr_pct": portfolio_xirr(active),
            "number_of_holdings": len(active),
            "winning_holdings": len(winning),
            "losing_holdings": len(losing),
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
    }


async def enriched_holdings(conn, portfolio_id: int, active_only: bool = True):
    status_filter = "and h.status = 'ACTIVE'" if active_only else ""
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
    result = [as_dict(row) for row in rows]

    active = [d for d in result if d["status"] == "ACTIVE"]
    if active:
        trade_date_row = await conn.fetchrow("select trade_date from v_latest_date")
        trade_date = trade_date_row["trade_date"] if trade_date_row else None
        symbols = [d["symbol"] for d in active]
        sector_momentum = await pi.fetch_sector_momentum(conn, trade_date) if trade_date else {}
        volume_ratios = await pi.fetch_volume_ratios(conn, symbols, trade_date) if trade_date else {}
        total_value = sum(float(d.get("current_value") or 0) for d in active)

        for d in active:
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

    return result


@router.get("/holdings", summary="List continuously tracked holdings")
async def list_holdings(user=Depends(current_user), pool=Depends(get_pool)):
    async with pool.acquire() as conn:
        pid = await ensure_portfolio(conn, user["id"])
        rows = await enriched_holdings(conn, pid, active_only=False)
    total_value = sum(float(r.get("current_value") or 0) for r in rows if r["status"] == "ACTIVE")
    for r in rows:
        if r["status"] == "ACTIVE":
            r["portfolio_contribution"] = (float(r.get("current_value") or 0) / total_value * 100) if total_value else 0
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