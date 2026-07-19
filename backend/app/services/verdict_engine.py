"""
verdict_engine.py
──────────────────────────────────────────────────────────────────────────
Deterministic, transparent verdict engine for portfolio holdings.

Produces exactly one of four verdicts per open holding — ADD_MORE, HOLD,
TRIM, EXIT — from explicit, inspectable rules over data the platform
already computes (position score, momentum, relative strength, drawdown
from peak, holding duration, concentration weight, performance streaks).

Design contract:
  * Machine-readable: every verdict carries the list of rules that fired,
    each with a stable rule id, a human sentence, and which verdict the
    rule voted for. Nothing is opaque — the UI can show "why" verbatim.
  * Deterministic: same inputs, same output. No AI calls. The Gemini
    advisor remains available separately as an optional second opinion.
  * Degrades gracefully: missing metrics simply don't fire rules; a
    holding with no market data at all gets HOLD with a data-quality note.

Also classifies each holding as draining capital, creating value, or
neutral — the portfolio-level "where is my money working" signal.
"""
from __future__ import annotations

from typing import Any, Optional

VERDICT_LABELS = {
    "ADD_MORE": "Add More",
    "HOLD": "Hold",
    "TRIM": "Trim",
    "EXIT": "Exit",
}

# Votes accumulate per verdict; strongest bucket wins with precedence
# EXIT > TRIM > ADD_MORE when scores tie (risk management first).
_PRECEDENCE = ("EXIT", "TRIM", "ADD_MORE")

# Thresholds are named so the reasoning strings and the code can't drift.
DEEP_DRAWDOWN_PCT = 20.0        # peak-to-current pullback that voids a thesis
TRAILING_STOP_PCT = 12.0        # pullback where profit protection kicks in
BIG_LOSS_PCT = -10.0            # unrealized loss considered thesis failure
OVERWEIGHT_PCT = 25.0           # single-position concentration ceiling
ADD_MAX_WEIGHT_PCT = 15.0       # don't advise adding above this weight
DEAD_MONEY_DAYS = 180           # stale-position age
DEAD_MONEY_BAND = 5.0           # |gain%| below this = going nowhere
UNDERPERF_EXIT_SESSIONS = 20    # sustained relative-weakness streak
UNDERPERF_TRIM_SESSIONS = 10
OUTPERF_ADD_SESSIONS = 8
RSI_OVEREXTENDED = 80.0


def _f(v, default: Optional[float] = None) -> Optional[float]:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def evaluate_holding(row: dict[str, Any]) -> dict[str, Any]:
    """Evaluate one enriched open holding.

    Reads (all optional): position_score, gain_pct, days_held, momentum_score,
    rs_score, rsi_14, ema_signal, macd_hist, drawdown_from_peak_pct,
    portfolio_contribution (weight %), rel_streak_direction ('out'|'under'),
    rel_streak_days, days_below_cost_streak.
    """
    reasons: list[dict[str, str]] = []
    votes = {"EXIT": 0.0, "TRIM": 0.0, "ADD_MORE": 0.0}

    def fire(rule: str, votes_for: str, weight: float, detail: str):
        votes[votes_for] += weight
        reasons.append({"rule": rule, "votes_for": votes_for, "detail": detail})

    score = _f(row.get("position_score"))
    gain = _f(row.get("gain_pct"), 0.0)
    days_held = int(_f(row.get("days_held"), 0) or 0)
    momentum = _f(row.get("momentum_score"))
    rs = _f(row.get("rs_score"))
    rsi = _f(row.get("rsi_14"))
    drawdown = _f(row.get("drawdown_from_peak_pct"))
    weight_pct = _f(row.get("portfolio_contribution"))
    ema_signal = row.get("ema_signal")
    macd_hist = _f(row.get("macd_hist"))
    streak_dir = row.get("rel_streak_direction")
    streak_days = int(_f(row.get("rel_streak_days"), 0) or 0)
    below_cost = int(_f(row.get("days_below_cost_streak"), 0) or 0)

    has_market_data = momentum is not None or rs is not None

    # ── EXIT rules ────────────────────────────────────────────────────
    if drawdown is not None and drawdown >= DEEP_DRAWDOWN_PCT and gain < 0:
        fire("deep_drawdown_underwater", "EXIT", 3.0,
             f"Down {drawdown:.1f}% from its post-buy peak while the position itself is at a loss — the original thesis has broken.")
    if streak_dir == "under" and streak_days >= UNDERPERF_EXIT_SESSIONS and (score is not None and score < 40):
        fire("sustained_underperformance", "EXIT", 3.0,
             f"Has lagged the market for {streak_days} straight sessions with a weak position score of {score:.0f}.")
    if score is not None and score < 25 and gain <= 0:
        fire("high_risk_score_loss", "EXIT", 2.0,
             f"Position score {score:.0f} is in the High Risk band and the position is not profitable.")
    if momentum is not None and rs is not None and momentum < 25 and rs < 30 and gain <= BIG_LOSS_PCT:
        fire("momentum_breakdown", "EXIT", 2.0,
             f"Momentum ({momentum:.0f}) and relative strength ({rs:.0f}) have both collapsed with a {abs(gain):.1f}% loss.")

    # ── TRIM rules ────────────────────────────────────────────────────
    if drawdown is not None and TRAILING_STOP_PCT <= drawdown < DEEP_DRAWDOWN_PCT and gain > 0:
        fire("trailing_stop", "TRIM", 2.0,
             f"Has given back {drawdown:.1f}% from its peak since purchase — consider protecting the remaining {gain:.1f}% gain.")
    if weight_pct is not None and weight_pct > OVERWEIGHT_PCT:
        fire("overconcentration", "TRIM", 1.5,
             f"Makes up {weight_pct:.1f}% of the portfolio (above the {OVERWEIGHT_PCT:.0f}% concentration ceiling).")
    if rsi is not None and rsi > RSI_OVEREXTENDED and gain > 15:
        fire("overextended_rsi", "TRIM", 1.0,
             f"RSI {rsi:.0f} is overextended after a {gain:.1f}% run — pullback risk is elevated.")
    if score is not None and 25 <= score < 40 and has_market_data:
        fire("weak_position_score", "TRIM", 1.0,
             f"Position score {score:.0f} sits in the Weak band — structure and momentum are deteriorating.")
    if days_held > DEAD_MONEY_DAYS and abs(gain) <= DEAD_MONEY_BAND and (momentum is None or momentum < 45):
        fire("dead_money", "TRIM", 1.0,
             f"Held {days_held} days for a {gain:+.1f}% result with no momentum — capital may work harder elsewhere.")
    if streak_dir == "under" and UNDERPERF_TRIM_SESSIONS <= streak_days < UNDERPERF_EXIT_SESSIONS:
        fire("relative_weakness", "TRIM", 0.75,
             f"Has underperformed the market for {streak_days} consecutive sessions.")

    # ── ADD_MORE rules (only when nothing risk-side has fired) ────────
    risk_pressure = votes["EXIT"] + votes["TRIM"]
    if risk_pressure == 0 and has_market_data:
        small_enough = weight_pct is None or weight_pct < ADD_MAX_WEIGHT_PCT
        if score is not None and score >= 70 and gain > 0 and small_enough and (drawdown is None or drawdown < 8):
            fire("strength_continuation", "ADD_MORE", 2.0,
                 f"Position score {score:.0f} with a {gain:.1f}% gain and price holding near its highs — strength is persisting.")
        if (momentum is not None and momentum >= 70 and rs is not None and rs >= 70
                and ema_signal in ("golden_cross", "above_200") and small_enough):
            fire("trend_leadership", "ADD_MORE", 1.5,
                 f"Momentum {momentum:.0f} and relative strength {rs:.0f} with a bullish long-term EMA posture.")
        if streak_dir == "out" and streak_days >= OUTPERF_ADD_SESSIONS and gain > 0 and small_enough:
            fire("sustained_outperformance", "ADD_MORE", 1.0,
                 f"Has outperformed the market for {streak_days} straight sessions.")

    # ── Decide ────────────────────────────────────────────────────────
    # EXIT needs exit-grade evidence (one 3.0 rule or a heavy combination).
    # Any solid risk pressure (>= 1.5 combined) is a TRIM. Weak signals
    # (a lone 0.75/1.0 rule) stay HOLD with their reasons still visible.
    verdict = "HOLD"
    if votes["EXIT"] >= 3.0:
        verdict = "EXIT"
    elif votes["EXIT"] + votes["TRIM"] >= 1.5:
        verdict = "TRIM"
    elif votes["ADD_MORE"] >= 2.0:
        verdict = "ADD_MORE"

    if not reasons:
        if not has_market_data:
            reasons.append({"rule": "no_market_data", "votes_for": "HOLD",
                            "detail": "No market data is available yet for this symbol — holding by default."})
        else:
            detail = "No exit, trim, or add-more rule fired — the position is behaving normally"
            if score is not None:
                detail += f" (position score {score:.0f})"
            reasons.append({"rule": "steady_state", "votes_for": "HOLD", "detail": detail + "."})

    # Confidence: how decisively the winning bucket beat the others,
    # scaled by how much data was actually available.
    winning = votes.get(verdict, 0.0)
    runner_up = max((v for k, v in votes.items() if k != verdict), default=0.0)
    margin = winning - runner_up
    data_fields = [score, momentum, rs, rsi, drawdown, weight_pct]
    completeness = sum(1 for f in data_fields if f is not None) / len(data_fields)
    if verdict == "HOLD":
        confidence = round(55 + 30 * completeness - min(runner_up, 1.5) * 10)
    else:
        confidence = round(min(95, 55 + margin * 12) * (0.6 + 0.4 * completeness))
    confidence = max(20, min(95, confidence))

    # ── Capital flag ─────────────────────────────────────────────────
    capital_flag, capital_reason = _capital_flag(gain, momentum, streak_dir, streak_days, below_cost, days_held)

    return {
        "verdict": verdict,
        "verdict_label": VERDICT_LABELS[verdict],
        "verdict_confidence": confidence,
        "verdict_reasons": reasons,
        "verdict_votes": {k: round(v, 2) for k, v in votes.items()},
        "capital_flag": capital_flag,
        "capital_flag_reason": capital_reason,
    }


def _capital_flag(gain: float, momentum: Optional[float], streak_dir, streak_days: int,
                  below_cost: int, days_held: int) -> tuple[str, str]:
    """Is this holding draining capital, creating value, or neutral?"""
    if gain < -5 and (momentum is None or momentum < 45):
        return "draining", f"Down {abs(gain):.1f}% with weak momentum — this position is eroding capital."
    if below_cost >= 30 and gain < 0:
        return "draining", f"Has closed below your cost for {below_cost} straight sessions."
    if days_held > DEAD_MONEY_DAYS and abs(gain) <= DEAD_MONEY_BAND:
        return "draining", f"Flat for {days_held} days — capital is idle, not compounding."
    if gain > 5 and (streak_dir == "out" or (momentum is not None and momentum >= 55)):
        return "creating", f"Up {gain:.1f}% and still showing market-beating strength."
    if gain > 0:
        return "neutral", f"Modestly profitable ({gain:+.1f}%) without a strong trend either way."
    return "neutral", "No decisive value-creation or capital-drain signal."


def evaluate_portfolio(active_rows: list[dict[str, Any]]) -> None:
    """Attach verdict fields to each enriched open holding, in place."""
    for row in active_rows:
        row.update(evaluate_holding(row))
