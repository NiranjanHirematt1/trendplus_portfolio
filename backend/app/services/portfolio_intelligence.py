"""
portfolio_intelligence.py
──────────────────────────────────────────────────────────────────────────
Deterministic Portfolio Intelligence engine for TrendPlus.

This module replaces the old rule-based ADD MORE / HOLD / TRIM / EXIT
verdict with a weighted Position Intelligence model built entirely from
data the momentum engine already computes (trend_results, sector_daily,
price_history). No new tables. No trading advice. Descriptive labels only.

Functions
─────────
  calculate_position_score(row, sector_momentum, volume_ratio, weight_pct)
      -> per-holding Position Score / Confidence / Quality / Risk Level

  calculate_portfolio_health(scored_holdings, concentration, benchmark)
      -> whole-portfolio composite health metrics

  find_rotation_candidates(scored_holdings, screener_rows, sector_momentum)
      -> "Capital Rotation Opportunities": weak holdings vs stronger,
         not-yet-held screener stocks

  build_opportunity_queue(screener_rows, held_symbols, sector_momentum)
      -> ranked list of top screener stocks not already in the portfolio

  generate_morning_brief(...)
      -> natural-language observations (no verdicts). This is the ONE
         function future Gemini/OpenAI integration should replace —
         everything else here stays deterministic and reusable.

Data-access helpers (fetch_volume_ratios, fetch_sector_momentum,
fetch_market_benchmark, fetch_previous_trend_snapshot) live here too so
route handlers stay thin (see AI-READY note in Part 11 of the spec).
"""
from __future__ import annotations

import datetime
from typing import Any, Optional

# ── Weights for the Position Score engine (Part 2 of the spec) ─────────
WEIGHTS = {
    "momentum": 0.25,
    "relative_strength": 0.20,
    "trend": 0.20,
    "volume": 0.10,
    "price_structure": 0.10,
    "drawdown_risk": 0.05,
    "near_high": 0.05,
    "sector": 0.05,
}

QUALITY_BUCKETS = [
    (85, "Elite Position"),
    (70, "Strong Position"),
    (55, "Healthy"),
    (40, "Watch Closely"),
    (25, "Weak Position"),
    (0, "High Risk"),
]

TREND_LABELS = [(80, "Strong Uptrend"), (60, "Improving"), (40, "Neutral"), (20, "Weakening"), (0, "Breaking Down")]
MOMENTUM_LABELS = [(80, "Accelerating"), (60, "Building"), (40, "Steady"), (20, "Fading"), (0, "Stalled")]
RS_LABELS = [(80, "Market Leader"), (60, "Above Average"), (40, "In-Line"), (20, "Below Average"), (0, "Lagging")]


def _clamp(v: Optional[float], lo: float = 0, hi: float = 100) -> float:
    if v is None:
        v = 0
    return max(lo, min(hi, float(v)))


def _f(v, default=0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _bucket_label(score: float, buckets: list[tuple[float, str]]) -> str:
    for threshold, label in buckets:
        if score >= threshold:
            return label
    return buckets[-1][1]


def _risk_level(drawdown_score: float, weight_pct: Optional[float]) -> str:
    level_idx = 0  # 0=Low 1=Moderate 2=Elevated 3=High
    if drawdown_score < 35:
        level_idx = 3
    elif drawdown_score < 55:
        level_idx = 2
    elif drawdown_score < 75:
        level_idx = 1
    if weight_pct is not None and weight_pct > 25:
        level_idx = min(level_idx + 1, 3)
    return ["Low", "Moderate", "Elevated", "High"][level_idx]


# ══════════════════════════════════════════════════════════════════════
#  PART 2 — Position Score Engine
# ══════════════════════════════════════════════════════════════════════

def calculate_position_score(
    row: dict[str, Any],
    sector_momentum: Optional[float] = None,
    volume_ratio: Optional[float] = None,
    weight_pct: Optional[float] = None,
) -> dict[str, Any]:
    """Build the Position Intelligence block for a single holding.

    `row` is expected to carry (any may be missing/None — engine degrades
    gracefully): momentum_score, rs_score, trending_days, adx_14, rsi_14,
    pct_from_high, rank_52w, ema_signal, macd_hist, gain_pct.
    """
    momentum = _clamp(row.get("momentum_score"))
    rs = _clamp(row.get("rs_score"))
    trending_days = _f(row.get("trending_days"))
    adx = _f(row.get("adx_14"))
    rsi = row.get("rsi_14")
    pct_from_high = row.get("pct_from_high")  # <= 0, 0 = at 52w high
    rank_52w = row.get("rank_52w")
    ema_signal = row.get("ema_signal")
    macd_hist = row.get("macd_hist")
    gain_pct = _f(row.get("gain_pct"))

    # Trend Strength — persistence (trending_days) blended with ADX conviction
    trend_strength = _clamp(trending_days / 12 * 100 * 0.65 + min(adx, 40) / 40 * 100 * 0.35)

    # Volume Quality — today's volume vs its own 20-session average.
    # Falls back to a neutral 50 when history isn't available yet.
    if volume_ratio is None:
        volume_quality = 50.0
    else:
        volume_quality = _clamp(50 + (volume_ratio - 1) * 50)

    # Price Structure — EMA posture + MACD direction + RSI "sweet spot"
    ema_component = {"golden_cross": 100, "above_200": 75, "approaching": 50}.get(ema_signal, 35)
    macd_component = 100 if _f(macd_hist) > 0 else 30
    if rsi is None:
        rsi_component = 50
    else:
        rsi_v = _f(rsi)
        if 50 <= rsi_v <= 70:
            rsi_component = 100
        elif rsi_v > 80 or rsi_v < 30:
            rsi_component = 40
        else:
            rsi_component = 70
    price_structure = _clamp(ema_component * 0.5 + macd_component * 0.25 + rsi_component * 0.25)

    # Drawdown Risk (higher score = lower risk) — distance from 52w high,
    # penalized further if the position itself is underwater.
    score_from_high = _clamp(100 + (_f(pct_from_high, -50)) * 2)
    unrealized_penalty = min(abs(gain_pct) * 1.5, 40) if gain_pct < 0 else 0
    drawdown_score = _clamp(score_from_high - unrealized_penalty)

    # Near 52-Week High — use the engine's own percentile rank when present
    near_high_score = _clamp(rank_52w) if rank_52w is not None else score_from_high

    # Sector Strength — sector's average momentum today, from the screener
    sector_score = _clamp(sector_momentum) if sector_momentum is not None else 50.0

    breakdown = {
        "momentum": round(momentum, 1),
        "relative_strength": round(rs, 1),
        "trend": round(trend_strength, 1),
        "volume": round(volume_quality, 1),
        "price_structure": round(price_structure, 1),
        "drawdown_risk": round(drawdown_score, 1),
        "near_high": round(near_high_score, 1),
        "sector": round(sector_score, 1),
    }

    position_score = round(sum(breakdown[k] * w for k, w in WEIGHTS.items()))

    # Confidence — data completeness + agreement between momentum & RS
    core_fields = [row.get("momentum_score"), row.get("rs_score"), row.get("adx_14"),
                   row.get("rsi_14"), pct_from_high, rank_52w]
    completeness = sum(1 for f in core_fields if f is not None) / len(core_fields) * 100
    agreement = 100 - abs(momentum - rs)
    confidence = round(_clamp(completeness * 0.4 + agreement * 0.3 + trend_strength * 0.3))

    quality = _bucket_label(position_score, QUALITY_BUCKETS)
    risk_level = _risk_level(drawdown_score, weight_pct)

    return {
        "position_score": position_score,
        "confidence": confidence,
        "position_quality": quality,
        "position_status": quality,
        "risk_level": risk_level,
        "trend_quality": _bucket_label(trend_strength, TREND_LABELS),
        "momentum_quality": _bucket_label(momentum, MOMENTUM_LABELS),
        "relative_strength_label": _bucket_label(rs, RS_LABELS),
        "score_breakdown": breakdown,
    }


# ══════════════════════════════════════════════════════════════════════
#  PART 3 — Portfolio Health
# ══════════════════════════════════════════════════════════════════════

def calculate_portfolio_health(
    scored_active: list[dict[str, Any]],
    concentration: dict[str, Any],
    benchmark: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """`scored_active` items must each carry current_value, gain_pct,
    position_score, momentum_score, chg_12d, and score_breakdown."""
    total_value = sum(_f(h.get("current_value")) for h in scored_active)
    if not scored_active or total_value <= 0:
        return {
            "portfolio_health": None, "momentum_health": None, "risk_score": None,
            "diversification_score": concentration.get("diversification_score"),
            "capital_efficiency": None, "alpha_vs_benchmark": None,
            "win_rate": None, "average_position_score": None,
        }

    def wavg(key_fn):
        return sum(key_fn(h) * _f(h.get("current_value")) for h in scored_active) / total_value

    average_position_score = wavg(lambda h: _f(h.get("position_score")))
    momentum_health = wavg(lambda h: _f(h.get("momentum_score")))
    winners = sum(1 for h in scored_active if _f(h.get("gain_pct")) > 0)
    win_rate = winners / len(scored_active) * 100

    top_position_pct = concentration.get("top_position_pct") or 0
    top_sector_pct = concentration.get("top_sector_pct") or 0
    concentration_component = _clamp(top_position_pct * 1.5)
    avg_drawdown_component = wavg(lambda h: _f((h.get("score_breakdown") or {}).get("drawdown_risk"), 50))
    risk_score = round(_clamp(concentration_component * 0.4 + (100 - avg_drawdown_component) * 0.6))

    diversification_score = concentration.get("diversification_score")
    if diversification_score is None:
        diversification_score = 50.0

    # Capital efficiency: % of capital sitting in Healthy-or-better positions
    efficient_value = sum(_f(h.get("current_value")) for h in scored_active if _f(h.get("position_score")) >= 55)
    capital_efficiency = round(efficient_value / total_value * 100, 1)

    # Alpha vs benchmark: portfolio's weighted 12-day return vs the
    # market-wide average 12-day return across all tracked stocks today.
    alpha_vs_benchmark = None
    if benchmark and benchmark.get("mkt_chg_12d") is not None:
        portfolio_chg_12d = wavg(lambda h: _f(h.get("chg_12d")))
        alpha_vs_benchmark = round(portfolio_chg_12d - _f(benchmark.get("mkt_chg_12d")), 2)

    portfolio_health = round(_clamp(
        momentum_health * 0.30 + average_position_score * 0.30
        + (100 - risk_score) * 0.20 + _clamp(diversification_score) * 0.20
    ))

    return {
        "portfolio_health": portfolio_health,
        "momentum_health": round(momentum_health, 1),
        "risk_score": risk_score,
        "diversification_score": diversification_score,
        "capital_efficiency": capital_efficiency,
        "alpha_vs_benchmark": alpha_vs_benchmark,
        "win_rate": round(win_rate, 1),
        "average_position_score": round(average_position_score, 1),
    }


# ══════════════════════════════════════════════════════════════════════
#  Data-access helpers (batched — keep route handlers thin)
# ══════════════════════════════════════════════════════════════════════

async def fetch_sector_momentum(conn, trade_date: datetime.date) -> dict[str, float]:
    rows = await conn.fetch(
        """
        select s.sector, avg(tr.momentum_score) as avg_momentum
        from trend_results tr
        join symbols s on s.symbol = tr.symbol
        where tr.trade_date = $1 and s.is_active = true and s.sector is not null and s.sector != ''
        group by s.sector
        """,
        trade_date,
    )
    return {r["sector"]: float(r["avg_momentum"]) for r in rows if r["avg_momentum"] is not None}


async def fetch_market_benchmark(conn, trade_date: datetime.date) -> dict[str, Optional[float]]:
    row = await conn.fetchrow(
        "select avg(momentum_score) as mkt_momentum, avg(chg_12d) as mkt_chg_12d from trend_results where trade_date = $1",
        trade_date,
    )
    if not row:
        return {"mkt_momentum": None, "mkt_chg_12d": None}
    return {
        "mkt_momentum": float(row["mkt_momentum"]) if row["mkt_momentum"] is not None else None,
        "mkt_chg_12d": float(row["mkt_chg_12d"]) if row["mkt_chg_12d"] is not None else None,
    }


async def fetch_volume_ratios(conn, symbols: list[str], trade_date: datetime.date) -> dict[str, float]:
    """Ratio of today's volume to each symbol's trailing 20-session average."""
    if not symbols:
        return {}
    rows = await conn.fetch(
        """
        with ranked as (
            select symbol, volume, trade_date,
                   row_number() over (partition by symbol order by trade_date desc) as rn
            from price_history
            where symbol = any($1::text[]) and trade_date <= $2
        )
        select symbol, avg(volume) as avg_vol,
               max(volume) filter (where rn = 1) as latest_vol
        from ranked
        where rn <= 20
        group by symbol
        """,
        symbols, trade_date,
    )
    ratios = {}
    for r in rows:
        avg_vol = float(r["avg_vol"] or 0)
        latest_vol = float(r["latest_vol"] or 0)
        if avg_vol > 0:
            ratios[r["symbol"]] = latest_vol / avg_vol
    return ratios


async def fetch_previous_trade_date(conn, trade_date: datetime.date) -> Optional[datetime.date]:
    row = await conn.fetchrow(
        "select trade_date from market_calendar where engine_status = 'done' and trade_date < $1 order by trade_date desc limit 1",
        trade_date,
    )
    return row["trade_date"] if row else None


async def fetch_rs_streaks(conn, symbols: list[str], trade_date: datetime.date,
                           lookback_sessions: int = 120) -> dict[str, dict[str, Any]]:
    """How long each symbol has been out- or underperforming the market.

    Counts consecutive sessions, ending at `trade_date` and walking backwards,
    in which rs_score stayed on the same side of 50 (>= 50 = outperforming).
    A missing rs_score breaks the streak. Returns
    {symbol: {"direction": "out"|"under", "days": n}} — symbols with no data
    are simply absent.
    """
    if not symbols:
        return {}
    rows = await conn.fetch(
        """
        with ranked as (
            select symbol, trade_date, rs_score,
                   row_number() over (partition by symbol order by trade_date desc) as rn
            from trend_results
            where symbol = any($1::text[]) and trade_date <= $2
        )
        select symbol, trade_date, rs_score from ranked
        where rn <= $3
        order by symbol, trade_date desc
        """,
        symbols, trade_date, lookback_sessions,
    )
    streaks: dict[str, dict[str, Any]] = {}
    current_symbol = None
    direction = None
    count = 0
    done = False

    def flush():
        if current_symbol is not None and direction is not None and count > 0:
            streaks[current_symbol] = {"direction": direction, "days": count}

    for r in rows:
        if r["symbol"] != current_symbol:
            flush()
            current_symbol, direction, count, done = r["symbol"], None, 0, False
        if done:
            continue
        rs = r["rs_score"]
        if rs is None:
            done = True
            continue
        side = "out" if float(rs) >= 50 else "under"
        if direction is None:
            direction = side
        if side != direction:
            done = True
            continue
        count += 1
    flush()
    return streaks


async def fetch_trend_snapshot(conn, symbols: list[str], trade_date: datetime.date) -> dict[str, dict]:
    if not symbols:
        return {}
    rows = await conn.fetch(
        """
        select symbol, momentum_score, rs_score, trending_days, adx_14, rsi_14,
               pct_from_high, rank_52w, ema_signal, macd_hist, close_price, chg_12d, volume
        from trend_results
        where symbol = any($1::text[]) and trade_date = $2
        """,
        symbols, trade_date,
    )
    return {r["symbol"]: dict(r) for r in rows}


# ══════════════════════════════════════════════════════════════════════
#  PART 5 — Capital Rotation
# ══════════════════════════════════════════════════════════════════════

WEAK_THRESHOLD = 55  # position_score below this is a rotation candidate
ROTATION_MIN_GAP = 15  # candidate momentum must exceed weak holding by this much


def _opportunity_score(candidate: dict[str, Any], sector_momentum: Optional[float]) -> float:
    momentum = _clamp(candidate.get("momentum_score"))
    rs = _clamp(candidate.get("rs_score"))
    trend = _clamp(_f(candidate.get("trending_days")) / 12 * 100)
    near_high = _clamp(candidate.get("rank_52w")) if candidate.get("rank_52w") is not None else 50
    sector = _clamp(sector_momentum) if sector_momentum is not None else 50
    return round(momentum * 0.40 + rs * 0.30 + trend * 0.20 + near_high * 0.05 + sector * 0.05)


def find_rotation_candidates(
    scored_active: list[dict[str, Any]],
    screener_rows: list[dict[str, Any]],
    sector_momentum: dict[str, float],
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Compare weak holdings against stronger, not-yet-held screener stocks.
    Purely observational — never instructs the user to buy or sell."""
    held_symbols = {h["symbol"] for h in scored_active}
    weak = [h for h in scored_active if _f(h.get("position_score")) < WEAK_THRESHOLD]
    if not weak or not screener_rows:
        return []

    candidates = [r for r in screener_rows if r.get("symbol") not in held_symbols]
    results = []
    for weak_holding in sorted(weak, key=lambda h: _f(h.get("position_score"))):
        best, best_score = None, -1
        for cand in candidates:
            cand_momentum = _clamp(cand.get("momentum_score"))
            if cand_momentum - _clamp(weak_holding.get("momentum_score")) < ROTATION_MIN_GAP:
                continue
            opp_score = _opportunity_score(cand, sector_momentum.get(cand.get("sector")))
            if opp_score > best_score:
                best, best_score = cand, opp_score
        if not best:
            continue

        reasons = []
        if _clamp(best.get("momentum_score")) - _clamp(weak_holding.get("momentum_score")) > 10:
            reasons.append("Higher momentum")
        if _clamp(best.get("rs_score")) - _clamp(weak_holding.get("rs_score")) > 10:
            reasons.append("Higher relative strength")
        w_sector_mom = sector_momentum.get(weak_holding.get("sector"))
        c_sector_mom = sector_momentum.get(best.get("sector"))
        if c_sector_mom is not None and w_sector_mom is not None and c_sector_mom > w_sector_mom + 5:
            reasons.append("Sector leadership")
        if best.get("ema_signal") == "golden_cross":
            reasons.append("Golden cross")
        if not reasons:
            reasons.append("Stronger overall momentum profile")

        results.append({
            "from": {
                "symbol": weak_holding["symbol"],
                "position_score": weak_holding.get("position_score"),
                "position_quality": weak_holding.get("position_quality"),
            },
            "to": {
                "symbol": best["symbol"],
                "company_name": best.get("company_name"),
                "sector": best.get("sector"),
                "momentum_score": best.get("momentum_score"),
                "rs_score": best.get("rs_score"),
            },
            "reasons": reasons,
            "opportunity_score": best_score,
            "potential_improvement": round(best_score - _f(weak_holding.get("position_score"))),
            "confidence": min(95, 60 + len(reasons) * 10),
        })
        candidates = [c for c in candidates if c["symbol"] != best["symbol"]]  # don't recommend twice
    return results[:limit]


# ══════════════════════════════════════════════════════════════════════
#  PART 6 — Opportunity Queue
# ══════════════════════════════════════════════════════════════════════

def build_opportunity_queue(
    screener_rows: list[dict[str, Any]],
    held_symbols: set[str],
    sector_momentum: dict[str, float],
    limit: int = 15,
) -> list[dict[str, Any]]:
    candidates = [r for r in screener_rows if r.get("symbol") not in held_symbols]
    ranked = []
    for cand in candidates:
        score = _opportunity_score(cand, sector_momentum.get(cand.get("sector")))
        reasons = []
        if _clamp(cand.get("momentum_score")) >= 80:
            reasons.append("Elite momentum")
        if _clamp(cand.get("rs_score")) >= 80:
            reasons.append("Market-leading RS")
        if cand.get("near_52w_high"):
            reasons.append("Near 52-week high")
        if cand.get("ema_signal") == "golden_cross":
            reasons.append("Golden cross")
        if not reasons:
            reasons.append("Broad-based strength")
        ranked.append({
            "symbol": cand["symbol"],
            "company_name": cand.get("company_name"),
            "sector": cand.get("sector"),
            "momentum_score": cand.get("momentum_score"),
            "rs_score": cand.get("rs_score"),
            "confidence": min(95, 55 + len(reasons) * 10),
            "reason": ", ".join(reasons),
            "opportunity_score": score,
        })
    ranked.sort(key=lambda r: r["opportunity_score"], reverse=True)
    for i, r in enumerate(ranked[:limit], start=1):
        r["rank"] = i
    return ranked[:limit]


# ══════════════════════════════════════════════════════════════════════
#  PART 4 / PART 11 — Morning Portfolio Brief
#  This is the one function a future Gemini/OpenAI integration should
#  replace. It is deterministic today and only reads data already
#  computed elsewhere in this module — no new writes, no new tables.
# ══════════════════════════════════════════════════════════════════════

def generate_morning_brief(
    scored_active: list[dict[str, Any]],
    health: dict[str, Any],
    previous_health: Optional[dict[str, Any]],
    rotation: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = {"Elite Position": 0, "Strong Position": 0, "Healthy": 0,
              "Watch Closely": 0, "Weak Position": 0, "High Risk": 0}
    for h in scored_active:
        q = h.get("position_quality")
        if q in counts:
            counts[q] += 1
    strong_count = counts["Elite Position"] + counts["Strong Position"] + counts["Healthy"]
    watch_count = counts["Watch Closely"]
    risk_count = counts["Weak Position"] + counts["High Risk"]

    highlights = []

    # Health trend, only if we have a real prior-session comparison
    if previous_health and previous_health.get("portfolio_health") is not None and health.get("portfolio_health") is not None:
        delta = health["portfolio_health"] - previous_health["portfolio_health"]
        if delta > 1:
            highlights.append(f"Portfolio Health improved to {health['portfolio_health']} from {previous_health['portfolio_health']} yesterday.")
        elif delta < -1:
            highlights.append(f"Portfolio Health slipped to {health['portfolio_health']} from {previous_health['portfolio_health']} yesterday.")
        else:
            highlights.append(f"Portfolio Health held steady at {health['portfolio_health']}.")

    # Strongest mover
    movers = sorted(scored_active, key=lambda h: _f(h.get("chg_1d")), reverse=True)
    if movers and _f(movers[0].get("chg_1d")) > 0:
        top = movers[0]
        if top.get("trend_quality") in ("Strong Uptrend",):
            highlights.append(f"{top['symbol']} continues making higher highs, up {_f(top.get('chg_1d')):.1f}% today.")
        else:
            highlights.append(f"{top['symbol']} led gainers today, up {_f(top.get('chg_1d')):.1f}%.")

    # Volume expansion
    vol_leaders = sorted(scored_active, key=lambda h: _f((h.get("score_breakdown") or {}).get("volume")), reverse=True)
    if vol_leaders and _f((vol_leaders[0].get("score_breakdown") or {}).get("volume")) >= 80:
        highlights.append(f"{vol_leaders[0]['symbol']} volume expanded well above its 20-day average.")

    # Newly entering breakout watch (near 52w high, improving structure)
    breakout_watch = [h for h in scored_active if _f((h.get("score_breakdown") or {}).get("near_high")) >= 90
                       and h.get("position_quality") not in ("Elite Position",)]
    if breakout_watch:
        highlights.append(f"{breakout_watch[0]['symbol']} entered breakout watch, trading near its 52-week high.")

    # Relative strength decliner
    laggards = sorted(scored_active, key=lambda h: _f(h.get("rs_score")))
    if laggards and _f(laggards[0].get("rs_score")) < 40:
        highlights.append(f"{laggards[0]['symbol']} is showing weak relative strength versus the broader market.")

    # Risk / rotation observation
    if risk_count:
        highlights.append(f"{risk_count} position{'s' if risk_count != 1 else ''} now flagged Weak Position or High Risk on the score engine.")
    if rotation:
        top_rot = rotation[0]
        highlights.append(
            f"The screener shows {top_rot['to']['symbol']} materially outperforming {top_rot['from']['symbol']} — see Capital Rotation Opportunities below."
        )

    return {
        "greeting": "Good Morning.",
        "highlights": highlights[:6],
        "summary": {
            "strong_positions": strong_count,
            "watch_closely": watch_count,
            "high_risk": risk_count,
        },
    }