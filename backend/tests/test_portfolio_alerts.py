"""Unit tests for the deterministic portfolio alert engine (pure logic)."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.portfolio_alerts import (
    _atr,
    _consecutive_run,
    build_alerts,
)


def holding(**overrides):
    h = {"id": 1, "symbol": "ABC", "portfolio_contribution": 10.0,
         "drawdown_from_peak_pct": 3.0, "gain_pct": 5.0, "verdict": "HOLD"}
    h.update(overrides)
    return h


def ohlc_from_changes(changes, base=100.0):
    """Build newest-first OHLC rows from newest-first daily % changes."""
    rows = []
    price = base
    d = date(2026, 7, 17)
    for i, chg in enumerate(changes):
        prev = price / (1 + chg / 100)
        rows.append({"trade_date": d - timedelta(days=i), "open_price": prev,
                     "high_price": max(price, prev), "low_price": min(price, prev),
                     "close_price": price, "prev_close": prev})
        price = prev
    return rows


def run(holdings, ohlc=None, trend=None, prev=None, ratios=None, conc=None,
        prev_verdicts=None, risk_limit=25.0, target=15.0, min_alloc=2.0):
    return build_alerts(holdings, ohlc or {}, trend or {}, prev or {}, ratios or {},
                        conc or {}, prev_verdicts or {}, risk_limit, target, min_alloc)


def rules(alerts):
    return [a["rule"] for a in alerts]


def sev(alerts, rule):
    return next(a["severity"] for a in alerts if a["rule"] == rule)


def test_consecutive_run_directions():
    assert _consecutive_run([-5.0, -6.1, -5.2, 2.0], -4.95) == 3
    assert _consecutive_run([5.0, 5.0, -1.0], 4.95) == 2
    assert _consecutive_run([1.0, -5.0], -4.95) == 0
    assert _consecutive_run([0.5, 1.0, -0.2], 0.0) == 2  # strictly green


def test_lower_circuit_streak_is_critical():
    alerts = run([holding(portfolio_contribution=10)], ohlc={"ABC": ohlc_from_changes([-5.0, -5.5, -6.0, 1.0])})
    assert sev(alerts, "consecutive_lower_circuit") == "critical"


def test_plain_red_and_green_streaks():
    alerts = run([holding()], ohlc={"ABC": ohlc_from_changes([-1, -2, -1, -0.5, -1, 2])})
    assert sev(alerts, "consecutive_red_days") == "high"
    alerts = run([holding()], ohlc={"ABC": ohlc_from_changes([1, 2, 1, 0.5, 1, -2])})
    assert sev(alerts, "consecutive_green_days") == "low"


def test_gap_down_and_up():
    rows = ohlc_from_changes([1.0, 1.0])
    rows[0]["open_price"] = rows[0]["prev_close"] * 0.96   # -4% gap
    alerts = run([holding()], ohlc={"ABC": rows})
    assert sev(alerts, "gap_down") == "high"

    rows = ohlc_from_changes([1.0, 1.0])
    rows[0]["open_price"] = rows[0]["prev_close"] * 1.04
    assert "gap_up" in rules(run([holding()], ohlc={"ABC": rows}))


def test_new_52w_high_and_low():
    trend = {"ABC": {"close_price": 200, "pct_from_high": 0.0}}
    alerts = run([holding()], trend=trend)
    assert sev(alerts, "new_52w_high") == "low"

    # steady decline: today's close below every prior close
    rows = ohlc_from_changes([-1.0] * 120)
    trend = {"ABC": {"close_price": rows[0]["close_price"]}}
    alerts = run([holding()], ohlc={"ABC": rows}, trend=trend)
    assert sev(alerts, "new_52w_low") == "critical"
    assert "breakdown" not in rules(alerts)  # suppressed by the 52w-low alert


def test_breakout_without_52w_high():
    # flat year, small pop today: 60d breakout but not necessarily 52w high
    rows = ohlc_from_changes([2.0] + [0.0] * 80)
    trend = {"ABC": {"close_price": rows[0]["close_price"], "pct_from_high": -5.0}}
    alerts = run([holding()], ohlc={"ABC": rows}, trend=trend)
    assert "breakout" in rules(alerts)


def test_atr_expansion():
    calm = ohlc_from_changes([0.2] * 30)
    wild = ohlc_from_changes([6, -5, 7, -6, 5] + [0.2] * 25)
    assert _atr(wild, 5) > _atr(calm, 5)
    alerts = run([holding()], ohlc={"ABC": wild})
    assert "atr_expansion" in rules(alerts)


def test_drawdown_highest_tier_only_and_severities():
    alerts = run([holding(drawdown_from_peak_pct=22.0)])
    assert sev(alerts, "drawdown_20") == "critical"
    assert sev(run([holding(drawdown_from_peak_pct=16.0)]), "drawdown_15") == "high"
    assert sev(run([holding(drawdown_from_peak_pct=11.0)]), "drawdown_10") == "medium"


def test_ema_break_most_significant_only():
    trend = {"ABC": {"close_price": 90, "ema_21": 95, "ema_50": 96, "ema_200": 100, "rsi_14": 50}}
    alerts = run([holding()], trend=trend)
    assert sev(alerts, "below_200dma") == "critical"
    assert "below_50ema" not in rules(alerts)

    trend = {"ABC": {"close_price": 97, "ema_21": 98, "ema_50": 96, "ema_200": 90, "rsi_14": 50}}
    assert sev(run([holding()], trend=trend), "below_20ema") == "medium"


def test_rs_and_momentum_deltas():
    trend = {"ABC": {"close_price": 100, "rs_score": 40, "momentum_score": 80}}
    prev = {"ABC": {"rs_score": 60, "momentum_score": 60}}
    alerts = run([holding()], trend=trend, prev=prev)
    assert sev(alerts, "rs_weakness") == "high"
    assert sev(alerts, "momentum_improvement") == "low"


def test_verdict_changed_severity_tracks_new_verdict():
    h = holding(verdict="TRIM", verdict_label="Trim")
    alerts = run([h], prev_verdicts={1: "HOLD"})
    assert sev(alerts, "verdict_changed") == "high"
    h = holding(verdict="EXIT", verdict_label="Exit", verdict_reasons=[{"detail": "broken"}])
    alerts = run([h], prev_verdicts={1: "HOLD"})
    assert sev(alerts, "verdict_changed") == "critical"
    assert "verdict_exit" in rules(alerts)
    # no change → no alert
    assert "verdict_changed" not in rules(run([holding()], prev_verdicts={1: "HOLD"}))


def test_allocation_ladder():
    assert sev(run([holding(portfolio_contribution=30)]), "position_risk_limit") == "critical"
    assert sev(run([holding(portfolio_contribution=16)]), "target_allocation_reached") == "medium"
    assert sev(run([holding(portfolio_contribution=1.5)]), "below_min_allocation") == "low"
    r = rules(run([holding(portfolio_contribution=10)]))
    assert not any(x in r for x in ("position_risk_limit", "target_allocation_reached", "below_min_allocation"))


def test_volume_spike_and_rsi_breakdown():
    trend = {"ABC": {"close_price": 100, "rsi_14": 25}}
    alerts = run([holding()], trend=trend, ratios={"ABC": 3.4})
    assert sev(alerts, "volume_spike") == "high"
    assert sev(alerts, "rsi_breakdown") == "high"


def test_portfolio_concentration():
    alerts = run([holding()], conc={"top_position_pct": 24.0, "top_sector_pct": 55.0})
    assert sev(alerts, "concentration_sector") == "high"
    assert "concentration_position" not in rules(alerts)


def test_sorted_by_severity():
    h1 = holding(symbol="AAA", drawdown_from_peak_pct=11.0)   # medium
    h2 = holding(symbol="BBB", drawdown_from_peak_pct=22.0)   # critical
    alerts = run([h1, h2])
    assert alerts[0]["severity"] == "critical"
