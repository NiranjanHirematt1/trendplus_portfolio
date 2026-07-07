"""
portfolio_history.py
──────────────────────────────────────────────────────────────────────────
Portfolio Performance History (Part 9 of the refactor spec).

Two complementary mechanisms:

  1. Daily snapshots (`portfolio_snapshots`) — one row per portfolio per
     trading day, written by `record_daily_snapshots()` right after the
     market-data engine finishes its run (see scheduler.run_daily_pipeline).
     This is the fast path: reading history is just a ranged SELECT.

  2. Ledger reconstruction (`reconstruct_history()`) — for date ranges that
     predate when snapshotting started (or for a brand-new portfolio),
     history is rebuilt on demand by replaying each symbol's BUY/SELL
     transactions against `price_history` closes. The result is used to
     answer the request immediately AND to backfill `portfolio_snapshots`
     so the next read for the same dates is instant.

Neither mechanism recalculates average buy price on sells — the ledger
replay uses the exact same weighted-average-on-buy / preserve-on-sell rule
as the live Sell Position / Buy More endpoints, so historical and current
figures always agree.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

RANGE_DAYS = {
    "1M": 30,
    "3M": 91,
    "6M": 182,
    "1Y": 365,
    "ALL": None,
}


def range_start_date(range_key: str, earliest_txn_date: date | None, today: date) -> date:
    days = RANGE_DAYS.get(range_key.upper(), 30)
    if days is None:
        return earliest_txn_date or today
    start = today - timedelta(days=days)
    if earliest_txn_date:
        return max(start, earliest_txn_date) if start < earliest_txn_date else start
    return start


def _replay_symbol_ledger(transactions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replays one symbol's chronological BUY/SELL rows into a timeline of
    (as_of_date, quantity_held, weighted_avg_cost, cumulative_realized_pnl)
    snapshots — one entry per transaction, valid until the next transaction.
    Mirrors the exact math used in the live sell/buy endpoints:
      * BUY  -> new_avg = (old_qty*old_avg + qty*price) / (old_qty+qty)
      * SELL -> avg unchanged; realized_pnl += qty*(sell_price-avg) - charges
    """
    timeline = []
    qty = 0.0
    avg = 0.0
    realized = 0.0
    for t in sorted(transactions, key=lambda r: (r["txn_date"], r["id"])):
        q = float(t["quantity"])
        p = float(t["price"])
        charges = float(t.get("charges") or 0)
        if t["txn_type"] == "BUY":
            new_qty = qty + q
            avg = ((qty * avg) + (q * p)) / new_qty if new_qty > 0 else 0.0
            qty = new_qty
        else:  # SELL
            realized += q * (p - avg) - charges
            qty = max(0.0, qty - q)
        timeline.append({"as_of": t["txn_date"], "quantity": qty, "avg_cost": avg, "realized_pnl": realized})
    return timeline


def _value_on_date(timeline: list[dict[str, Any]], on_date: date) -> tuple[float, float, float]:
    """Returns (quantity_held, avg_cost, cumulative_realized_pnl) as of `on_date`,
    using the last ledger entry on or before that date."""
    state = None
    for entry in timeline:
        if entry["as_of"] <= on_date:
            state = entry
        else:
            break
    if not state:
        return 0.0, 0.0, 0.0
    return state["quantity"], state["avg_cost"], state["realized_pnl"]


def reconstruct_history(
    transactions_by_symbol: dict[str, list[dict[str, Any]]],
    price_history_by_symbol: dict[str, dict[date, float]],
    trade_dates: list[date],
) -> list[dict[str, Any]]:
    """Builds a day-by-day portfolio value series.

    transactions_by_symbol: {symbol: [{id, txn_type, quantity, price, txn_date, charges}, ...]}
    price_history_by_symbol: {symbol: {trade_date: close_price}}
    trade_dates: sorted list of dates to produce a point for (typically the
                 distinct trade_date values available in price_history for
                 the involved symbols, within the requested range).

    Returns [{"date": date, "invested": float, "current_value": float,
              "unrealized_pnl": float, "realized_pnl": float}, ...]
    """
    timelines = {sym: _replay_symbol_ledger(txns) for sym, txns in transactions_by_symbol.items()}
    series = []
    for d in trade_dates:
        invested = 0.0
        current_value = 0.0
        realized_total = 0.0
        for sym, timeline in timelines.items():
            qty, avg_cost, realized = _value_on_date(timeline, d)
            realized_total += realized
            if qty <= 0:
                continue
            close = price_history_by_symbol.get(sym, {}).get(d)
            if close is None:
                continue  # no trading data for this symbol on this date (holiday/not yet listed)
            invested += qty * avg_cost
            current_value += qty * close
        series.append({
            "date": d,
            "invested": round(invested, 2),
            "current_value": round(current_value, 2),
            "unrealized_pnl": round(current_value - invested, 2),
            "realized_pnl": round(realized_total, 2),
        })
    return series


async def fetch_transactions_for_portfolio(conn, portfolio_id: int) -> dict[str, list[dict[str, Any]]]:
    rows = await conn.fetch(
        """
        select id, symbol, txn_type, quantity, price, txn_date, charges
        from holding_transactions
        where portfolio_id = $1
        order by symbol, txn_date, id
        """,
        portfolio_id,
    )
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(dict(r))
    return by_symbol


async def fetch_price_history_for_symbols(conn, symbols: list[str], start: date) -> dict[str, dict[date, float]]:
    if not symbols:
        return {}
    rows = await conn.fetch(
        """
        select symbol, trade_date, close_price
        from price_history
        where symbol = any($1::text[]) and trade_date >= $2
        order by trade_date
        """,
        symbols, start,
    )
    by_symbol: dict[str, dict[date, float]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], {})[r["trade_date"]] = float(r["close_price"])
    return by_symbol


async def get_performance_history(conn, portfolio_id: int, range_key: str, today: date) -> list[dict[str, Any]]:
    """Public entrypoint used by GET /api/portfolio/performance-history.
    Prefers cached snapshots; reconstructs and backfills when snapshots
    don't yet cover the requested range."""
    txns_by_symbol = await fetch_transactions_for_portfolio(conn, portfolio_id)
    if not txns_by_symbol:
        return []
    earliest = min(t["txn_date"] for txns in txns_by_symbol.values() for t in txns)
    start = range_start_date(range_key, earliest, today)

    snapshot_rows = await conn.fetch(
        """
        select snapshot_date, total_investment, current_value, unrealized_pnl, realized_pnl
        from portfolio_snapshots
        where portfolio_id = $1 and snapshot_date >= $2 and snapshot_date <= $3
        order by snapshot_date
        """,
        portfolio_id, start, today,
    )
    trade_dates_available = await conn.fetch(
        "select distinct trade_date from price_history where trade_date >= $1 and trade_date <= $2 order by trade_date",
        start, today,
    )
    expected_points = len(trade_dates_available)
    # Snapshots are considered a complete cache for this range only if they
    # cover (almost) every trading day we'd otherwise reconstruct — daily
    # scheduler runs should keep this true going forward.
    if expected_points and len(snapshot_rows) >= max(1, expected_points - 2):
        return [
            {
                "date": r["snapshot_date"],
                "invested": float(r["total_investment"]),
                "current_value": float(r["current_value"]),
                "unrealized_pnl": float(r["unrealized_pnl"]),
                "realized_pnl": float(r["realized_pnl"]),
            }
            for r in snapshot_rows
        ]

    symbols = list(txns_by_symbol.keys())
    price_history = await fetch_price_history_for_symbols(conn, symbols, start)
    trade_dates = [r["trade_date"] for r in trade_dates_available]
    series = reconstruct_history(txns_by_symbol, price_history, trade_dates)

    if series:
        await conn.executemany(
            """
            insert into portfolio_snapshots (portfolio_id, snapshot_date, total_investment, current_value, unrealized_pnl, realized_pnl, holdings_count)
            values ($1, $2, $3, $4, $5, $6, $7)
            on conflict (portfolio_id, snapshot_date) do update set
                total_investment = excluded.total_investment,
                current_value    = excluded.current_value,
                unrealized_pnl   = excluded.unrealized_pnl,
                realized_pnl     = excluded.realized_pnl,
                holdings_count   = excluded.holdings_count
            """,
            [
                (portfolio_id, s["date"], s["invested"], s["current_value"], s["unrealized_pnl"], s["realized_pnl"], 0)
                for s in series
            ],
        )
    return series


async def record_daily_snapshots(pool, trade_date: date) -> int:
    """Called once per trading day after the engine run finishes (see
    scheduler.run_daily_pipeline). Snapshots every portfolio that has at
    least one open (ACTIVE/PARTIAL) holding priced on `trade_date`.
    Returns the number of portfolios snapshotted."""
    async with pool.acquire() as conn:
        portfolio_ids = await conn.fetch(
            "select distinct portfolio_id from holdings where status in ('ACTIVE','PARTIAL') and not is_archived"
        )
        count = 0
        for row in portfolio_ids:
            pid = row["portfolio_id"]
            holdings = await conn.fetch(
                """
                select h.quantity, h.avg_buy_price, h.realized_pnl,
                       tr.close_price
                from holdings h
                left join trend_results tr on tr.symbol = h.symbol and tr.trade_date = $2
                where h.portfolio_id = $1 and h.status in ('ACTIVE','PARTIAL') and not h.is_archived
                """,
                pid, trade_date,
            )
            if not holdings:
                continue
            invested = sum(float(h["quantity"]) * float(h["avg_buy_price"]) for h in holdings)
            current_value = sum(
                float(h["quantity"]) * float(h["close_price"]) for h in holdings if h["close_price"] is not None
            )
            realized = sum(float(h["realized_pnl"] or 0) for h in holdings)
            await conn.execute(
                """
                insert into portfolio_snapshots (portfolio_id, snapshot_date, total_investment, current_value, unrealized_pnl, realized_pnl, holdings_count)
                values ($1, $2, $3, $4, $5, $6, $7)
                on conflict (portfolio_id, snapshot_date) do update set
                    total_investment = excluded.total_investment,
                    current_value    = excluded.current_value,
                    unrealized_pnl   = excluded.unrealized_pnl,
                    realized_pnl     = excluded.realized_pnl,
                    holdings_count   = excluded.holdings_count
                """,
                pid, trade_date, invested, current_value, current_value - invested, realized, len(holdings),
            )
            count += 1
    return count