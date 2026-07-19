"""Unit tests for the deterministic verdict engine (pure logic, no DB)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.verdict_engine import evaluate_holding, evaluate_portfolio


def base_row(**overrides):
    row = {
        "position_score": 60, "gain_pct": 4.0, "days_held": 40,
        "momentum_score": 55, "rs_score": 55, "rsi_14": 55,
        "ema_signal": "above_200", "macd_hist": 0.5,
        "drawdown_from_peak_pct": 3.0, "portfolio_contribution": 8.0,
        "rel_streak_direction": None, "rel_streak_days": 0,
        "days_below_cost_streak": 0,
    }
    row.update(overrides)
    return row


def test_steady_position_holds():
    out = evaluate_holding(base_row())
    assert out["verdict"] == "HOLD"
    assert out["verdict_reasons"], "HOLD must still carry an explanation"
    assert out["verdict_label"] == "Hold"


def test_deep_drawdown_underwater_exits():
    out = evaluate_holding(base_row(gain_pct=-12, drawdown_from_peak_pct=25, position_score=30))
    assert out["verdict"] == "EXIT"
    rules = {r["rule"] for r in out["verdict_reasons"]}
    assert "deep_drawdown_underwater" in rules


def test_sustained_underperformance_exits():
    out = evaluate_holding(base_row(
        rel_streak_direction="under", rel_streak_days=25, position_score=35, gain_pct=-6,
    ))
    assert out["verdict"] == "EXIT"


def test_trailing_stop_trims_profitable_pullback():
    out = evaluate_holding(base_row(gain_pct=20, drawdown_from_peak_pct=14))
    assert out["verdict"] == "TRIM"
    rules = {r["rule"] for r in out["verdict_reasons"]}
    assert "trailing_stop" in rules


def test_overconcentration_alone_trims():
    out = evaluate_holding(base_row(portfolio_contribution=30))
    assert out["verdict"] == "TRIM"
    assert any(r["rule"] == "overconcentration" for r in out["verdict_reasons"])


def test_weak_single_signal_stays_hold():
    # A lone weak-position-score rule (1.0 vote) should not flip the verdict.
    out = evaluate_holding(base_row(position_score=35))
    assert out["verdict"] == "HOLD"
    assert any(r["rule"] == "weak_position_score" for r in out["verdict_reasons"])


def test_strong_position_adds_more():
    out = evaluate_holding(base_row(
        position_score=80, gain_pct=12, momentum_score=85, rs_score=82,
        ema_signal="golden_cross", drawdown_from_peak_pct=2,
        rel_streak_direction="out", rel_streak_days=15,
    ))
    assert out["verdict"] == "ADD_MORE"


def test_add_more_suppressed_when_overweight():
    out = evaluate_holding(base_row(
        position_score=80, gain_pct=12, momentum_score=85, rs_score=82,
        ema_signal="golden_cross", drawdown_from_peak_pct=2,
        portfolio_contribution=20,
    ))
    assert out["verdict"] == "HOLD"


def test_no_market_data_defaults_to_hold_with_note():
    out = evaluate_holding({"gain_pct": 0, "days_held": 5})
    assert out["verdict"] == "HOLD"
    assert out["verdict_reasons"][0]["rule"] == "no_market_data"


def test_capital_flags():
    draining = evaluate_holding(base_row(gain_pct=-10, momentum_score=30))
    assert draining["capital_flag"] == "draining"

    creating = evaluate_holding(base_row(gain_pct=18, momentum_score=70,
                                         rel_streak_direction="out", rel_streak_days=5))
    assert creating["capital_flag"] == "creating"

    idle = evaluate_holding(base_row(days_held=200, gain_pct=1.0, momentum_score=40))
    assert idle["capital_flag"] == "draining"  # dead money is a capital drag


def test_confidence_bounds_and_votes_shape():
    for row in (base_row(), base_row(gain_pct=-30, drawdown_from_peak_pct=40, position_score=10)):
        out = evaluate_holding(row)
        assert 20 <= out["verdict_confidence"] <= 95
        assert set(out["verdict_votes"]) == {"EXIT", "TRIM", "ADD_MORE"}


def test_evaluate_portfolio_mutates_in_place():
    rows = [base_row(), base_row(gain_pct=-15, drawdown_from_peak_pct=30, position_score=20)]
    evaluate_portfolio(rows)
    assert all("verdict" in r for r in rows)
    assert rows[1]["verdict"] == "EXIT"
