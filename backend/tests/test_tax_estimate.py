"""Unit tests for the unrealized STCG/LTCG tax estimate."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.portfolio_intelligence import (
    LTCG_EXEMPTION_INR,
    LTCG_RATE,
    STCG_RATE,
    estimate_taxes,
)


def test_stcg_and_ltcg_split_on_365_day_boundary():
    active = [
        {"unrealized_pnl": 50_000, "days_held": 365},   # short-term (inclusive)
        {"unrealized_pnl": 200_000, "days_held": 366},  # long-term
    ]
    t = estimate_taxes(active)
    assert t["stcg_unrealized_gain"] == 50_000
    assert t["estimated_stcg_tax"] == round(50_000 * STCG_RATE, 2)
    assert t["ltcg_unrealized_gain"] == 200_000
    assert t["ltcg_exemption_used"] == LTCG_EXEMPTION_INR
    assert t["estimated_ltcg_tax"] == round((200_000 - LTCG_EXEMPTION_INR) * LTCG_RATE, 2)
    assert t["estimated_total_tax_if_sold_today"] == round(
        t["estimated_stcg_tax"] + t["estimated_ltcg_tax"], 2
    )


def test_ltcg_fully_covered_by_exemption():
    active = [{"unrealized_pnl": 100_000, "days_held": 400}]
    t = estimate_taxes(active)
    assert t["ltcg_exemption_used"] == 100_000
    assert t["estimated_ltcg_tax"] == 0
    assert t["estimated_total_tax_if_sold_today"] == 0


def test_losses_net_within_bucket_and_never_go_negative():
    active = [
        {"unrealized_pnl": -30_000, "days_held": 100},
        {"unrealized_pnl": 10_000, "days_held": 200},
        {"unrealized_pnl": -5_000, "days_held": 500},
    ]
    t = estimate_taxes(active)
    assert t["stcg_unrealized_gain"] == -20_000
    assert t["estimated_stcg_tax"] == 0
    assert t["ltcg_unrealized_gain"] == -5_000
    assert t["estimated_ltcg_tax"] == 0
    assert t["ltcg_exemption_used"] == 0
    assert t["estimated_total_tax_if_sold_today"] == 0


def test_missing_days_held_counts_as_short_term():
    active = [{"unrealized_pnl": 1_000, "days_held": None}]
    t = estimate_taxes(active)
    assert t["stcg_unrealized_gain"] == 1_000
    assert t["ltcg_unrealized_gain"] == 0


def test_empty_portfolio_returns_zeroes():
    t = estimate_taxes([])
    assert t["estimated_total_tax_if_sold_today"] == 0
    assert t["disclaimer"]
