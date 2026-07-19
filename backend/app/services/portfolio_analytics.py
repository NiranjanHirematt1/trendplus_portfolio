"""
Trader-grade portfolio analytics: XIRR, concentration/diversification risk,
and peak-drawdown tracking used by the Hold/Trim/Exit rule engine.

Tax calculation has been intentionally removed - that is the broker's
responsibility (contract notes / capital-gains statements), not ours.
"""
from __future__ import annotations

from datetime import date
from typing import Any

# -- XIRR (money-weighted return) ----------------------------------------
# Accounts for *when* each buy happened, unlike simple gain% which treats
# every rupee as invested for the same length of time.

def _npv(rate: float, cashflows: list[tuple[date, float]], today: date) -> float:
    total = 0.0
    for when, amount in cashflows:
        days = (when - today).days
        total += amount / ((1 + rate) ** (days / 365.0))
    return total


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """cashflows: list of (date, amount). Negative = money out (a buy), positive = money in
    (current value, treated as a hypothetical sale today). Returns annualized % or None
    if it can't be solved (e.g. all flows same sign, or no data)."""
    if len(cashflows) < 2:
        return None
    if not any(a < 0 for _, a in cashflows) or not any(a > 0 for _, a in cashflows):
        return None
    today = max(d for d, _ in cashflows)

    rate = 0.15  # initial guess: 15%
    for _ in range(100):
        npv = _npv(rate, cashflows, today)
        h = 1e-5
        d_npv = (_npv(rate + h, cashflows, today) - npv) / h
        if abs(d_npv) < 1e-12:
            break
        new_rate = rate - npv / d_npv
        if new_rate <= -0.99:
            new_rate = -0.5
        if abs(new_rate - rate) < 1e-7:
            rate = new_rate
            break
        rate = new_rate
    if rate <= -0.99 or rate > 100 or rate != rate:  # NaN guard
        return None
    return round(rate * 100, 2)


def portfolio_xirr(active_holdings: list[dict[str, Any]]) -> float | None:
    cashflows: list[tuple[date, float]] = []
    today = date.today()
    for h in active_holdings:
        buy_date = h.get("buy_date")
        investment = h.get("investment_amount")
        current_value = h.get("current_value")
        if not buy_date or investment is None or current_value is None:
            continue
        cashflows.append((buy_date, -float(investment)))
    total_current_value = sum(float(h.get("current_value") or 0) for h in active_holdings)
    if total_current_value > 0:
        cashflows.append((today, total_current_value))
    return xirr(cashflows)


# -- Concentration / diversification risk --------------------------------

def concentration_risk(active_holdings: list[dict[str, Any]]) -> dict[str, Any]:
    """Herfindahl-Hirschman Index on position weights, plus a friendlier 0-100 score
    and the single biggest position/sector for a quick risk flag."""
    values = [float(h.get("current_value") or 0) for h in active_holdings]
    total = sum(values)
    if total <= 0 or not values:
        return {"hhi": None, "diversification_score": None, "top_position_pct": None, "top_sector_pct": None}

    weights = [v / total for v in values]
    hhi = sum(w * w for w in weights)
    diversification_score = round((1 - hhi) * 100, 1)
    top_position_pct = round(max(weights) * 100, 1)

    sector_totals: dict[str, float] = {}
    for h in active_holdings:
        sector = h.get("sector") or "Unclassified"
        sector_totals[sector] = sector_totals.get(sector, 0) + float(h.get("current_value") or 0)
    top_sector_pct = round(max(sector_totals.values()) / total * 100, 1) if sector_totals else None

    return {
        "hhi": round(hhi, 4),
        "diversification_score": diversification_score,
        "top_position_pct": top_position_pct,
        "top_sector_pct": top_sector_pct,
        "flag": "concentrated" if top_position_pct > 25 or (top_sector_pct or 0) > 40 else "diversified",
    }


# -- Peak-drawdown since purchase -----------------------------------------
# Powers the trailing-stop rule: how far has each holding fallen from its
# highest close since the day it was bought? A stock can still be "up" on
# your original entry while having given back a lot of its gains - that's
# exactly the situation a trailing stop is meant to catch.

def compute_peak_drawdowns(price_rows: list[dict[str, Any]], holdings: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """price_rows: [{"symbol", "trade_date", "close_price"}, ...] for all symbols
    involved, going back at least as far as the earliest buy_date.
    Returns {holding_id: {"peak_price": float, "drawdown_from_peak_pct": float,
                          "days_below_cost_streak": int, "days_above_cost_streak": int}}.
    Applies to every open holding (ACTIVE and PARTIAL alike) — a partially
    sold position still has live exposure that a trailing stop must watch.
    """
    by_symbol: dict[str, list[tuple[date, float]]] = {}
    for pr in price_rows:
        by_symbol.setdefault(pr["symbol"], []).append((pr["trade_date"], float(pr["close_price"])))
    for points in by_symbol.values():
        points.sort()

    result: dict[int, dict[str, Any]] = {}
    for h in holdings:
        if h.get("status") not in ("ACTIVE", "PARTIAL") or not h.get("buy_date") or h.get("current_price") is None:
            continue
        points = by_symbol.get(h["symbol"], [])
        relevant = [p for d, p in points if d >= h["buy_date"]]
        if not relevant:
            continue
        peak = max(relevant)
        current = float(h["current_price"])
        if peak <= 0:
            continue

        # Trailing streak of closes below/above the holding's average cost —
        # "how long has this been underwater" in the most literal sense.
        below = above = 0
        cost = float(h.get("avg_buy_price") or 0)
        if cost > 0:
            for close in reversed(relevant):
                if close < cost and above == 0:
                    below += 1
                elif close >= cost and below == 0:
                    above += 1
                else:
                    break

        result[h["id"]] = {
            "peak_price": round(peak, 2),
            "drawdown_from_peak_pct": round((peak - current) / peak * 100, 2),
            "days_below_cost_streak": below,
            "days_above_cost_streak": above,
        }
    return result
