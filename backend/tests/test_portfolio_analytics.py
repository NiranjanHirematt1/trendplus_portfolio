"""Unit tests for portfolio analytics pure functions."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.portfolio_analytics import (
    compute_peak_drawdowns,
    concentration_risk,
    xirr,
)
from app.services.portfolio_history import _replay_symbol_ledger, reconstruct_history


def test_xirr_simple_double_in_one_year():
    flows = [(date(2025, 1, 1), -100.0), (date(2026, 1, 1), 200.0)]
    result = xirr(flows)
    assert result is not None
    assert abs(result - 100.0) < 1.0  # ~100% annualized


def test_xirr_requires_both_signs():
    assert xirr([(date(2025, 1, 1), -100.0)]) is None
    assert xirr([(date(2025, 1, 1), -100.0), (date(2025, 6, 1), -50.0)]) is None


def test_concentration_flags_top_heavy_portfolio():
    holdings = [
        {"current_value": 80, "sector": "IT"},
        {"current_value": 20, "sector": "Pharma"},
    ]
    risk = concentration_risk(holdings)
    assert risk["top_position_pct"] == 80.0
    assert risk["flag"] == "concentrated"


def test_peak_drawdown_covers_partial_holdings_and_streaks():
    prices = [
        {"symbol": "ABC", "trade_date": date(2026, 1, d), "close_price": p}
        for d, p in [(5, 100), (6, 120), (7, 110), (8, 95), (9, 90)]
    ]
    holdings = [{
        "id": 1, "symbol": "ABC", "status": "PARTIAL",  # was skipped pre-fix
        "buy_date": date(2026, 1, 5), "current_price": 90, "avg_buy_price": 100,
    }]
    out = compute_peak_drawdowns(prices, holdings)
    assert 1 in out, "PARTIAL holdings must be included in trailing-stop tracking"
    assert out[1]["peak_price"] == 120
    assert out[1]["drawdown_from_peak_pct"] == 25.0
    assert out[1]["days_below_cost_streak"] == 2   # closes 95, 90
    assert out[1]["days_above_cost_streak"] == 0


def test_ledger_replay_weighted_avg_and_realized():
    txns = [
        {"id": 1, "txn_type": "BUY", "quantity": 10, "price": 100, "txn_date": date(2026, 1, 5), "charges": 0},
        {"id": 2, "txn_type": "BUY", "quantity": 10, "price": 200, "txn_date": date(2026, 1, 10), "charges": 0},
        {"id": 3, "txn_type": "SELL", "quantity": 5, "price": 180, "txn_date": date(2026, 1, 15), "charges": 10},
    ]
    timeline = _replay_symbol_ledger(txns)
    assert timeline[1]["avg_cost"] == 150.0        # weighted average after 2nd buy
    assert timeline[2]["avg_cost"] == 150.0        # sell leaves avg unchanged
    assert timeline[2]["realized_pnl"] == 5 * (180 - 150) - 10
    assert timeline[2]["quantity"] == 15


def test_reconstruct_history_values_by_date():
    txns = {"ABC": [
        {"id": 1, "txn_type": "BUY", "quantity": 10, "price": 100, "txn_date": date(2026, 1, 5), "charges": 0},
    ]}
    prices = {"ABC": {date(2026, 1, 5): 100.0, date(2026, 1, 6): 110.0}}
    series = reconstruct_history(txns, prices, [date(2026, 1, 5), date(2026, 1, 6)])
    assert series[0]["current_value"] == 1000.0
    assert series[1]["current_value"] == 1100.0
    assert series[1]["unrealized_pnl"] == 100.0
