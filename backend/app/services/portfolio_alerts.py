"""
portfolio_alerts.py
──────────────────────────────────────────────────────────────────────────
Deterministic portfolio alert engine.

Scans the user's open holdings against market data the platform already
ingests (price_history OHLC, trend_results) plus portfolio state
(drawdowns, weights, verdicts, verdict history) and emits severity-ranked
alerts. Every rule is explicit and cheap; nothing calls external services.

Severity levels (worst first): critical, high, medium, low.
Positive events (new 52-week high, breakout, momentum improvement…) are
"low" — informational good news, never buried but never shouting.

Alert shape (stable API contract):
    {
        "severity": "critical" | "high" | "medium" | "low",
        "rule":     stable rule id,
        "symbol":   holding symbol, or None for portfolio-level alerts,
        "title":    short headline,
        "detail":   one sentence with the exact numbers that fired,
    }

Rules that need data TrendPlus does not ingest yet (delivery %, bulk
deals, promoter pledge, ratings, earnings, news) are intentionally absent
— see docs/PORTFOLIO_REDESIGN.md §2/§8. No fake data.
"""
from __future__ import annotations

from typing import Any, Optional

# ── Tunables (named so reasons and code can't drift) ──────────────────
CIRCUIT_PCT = 4.95           # daily move counted as circuit-grade (5% band)
CIRCUIT_SESSIONS = 3
GREEN_RED_SESSIONS = 5       # plain consecutive up/down closes
GAP_PCT = 3.0                # open vs previous close
ATR_FAST, ATR_SLOW = 5, 20
ATR_EXPANSION_RATIO = 1.8
RANGE_SESSIONS = 60          # breakout/breakdown lookback
RS_DELTA = 15.0              # rs_score change over DELTA_SESSIONS
MOMENTUM_DELTA = 15.0
DELTA_SESSIONS = 5
VOLUME_SPIKE_RATIO = 3.0
RSI_BREAKDOWN = 30.0
DRAWDOWN_TIERS = ((20.0, "critical"), (15.0, "high"), (10.0, "medium"))
TOP_POSITION_LIMIT = 25.0
TOP_SECTOR_LIMIT = 40.0
DEFAULT_RISK_LIMIT_PCT = 25.0
DEFAULT_TARGET_ALLOC_PCT = 15.0
DEFAULT_MIN_ALLOC_PCT = 2.0

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _f(v, default: Optional[float] = None) -> Optional[float]:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _daily_changes(ohlc: list[dict[str, Any]]) -> list[float]:
    """Daily % change per session, newest first, from OHLC rows that carry
    prev_close (weekends/holidays handled by the stored prev_close)."""
    out = []
    for r in ohlc:
        close, prev = _f(r.get("close_price")), _f(r.get("prev_close"))
        if close is None or not prev:
            continue
        out.append((close - prev) / prev * 100)
    return out


def _consecutive_run(changes: list[float], threshold: float) -> int:
    """Trailing run of sessions beyond `threshold` (>= if positive,
    <= if negative; threshold 0.0 counts strictly green, -0.0 strictly red)."""
    run = 0
    for chg in changes:
        if threshold > 0:
            hit = chg >= threshold
        elif threshold < 0:
            hit = chg <= threshold
        else:
            hit = chg > 0
        if not hit:
            break
        run += 1
    return run


def _consecutive_red(changes: list[float]) -> int:
    run = 0
    for chg in changes:
        if chg >= 0:
            break
        run += 1
    return run


def _atr(ohlc: list[dict[str, Any]], sessions: int) -> Optional[float]:
    """Average True Range over the most recent `sessions` rows (newest first)."""
    trs = []
    for r in ohlc[:sessions]:
        h, l, pc = _f(r.get("high_price")), _f(r.get("low_price")), _f(r.get("prev_close"))
        if h is None or l is None:
            continue
        tr = h - l
        if pc is not None:
            tr = max(tr, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < max(3, sessions // 2):
        return None
    return sum(trs) / len(trs)


def build_alerts(
    holdings: list[dict[str, Any]],
    ohlc_by_symbol: dict[str, list[dict[str, Any]]],
    trend_by_symbol: dict[str, dict[str, Any]],
    trend_prev_by_symbol: dict[str, dict[str, Any]],
    volume_ratios: dict[str, float],
    concentration: dict[str, Any],
    prev_verdicts: dict[int, str],
    risk_limit_pct: float = DEFAULT_RISK_LIMIT_PCT,
    target_alloc_pct: float = DEFAULT_TARGET_ALLOC_PCT,
    min_alloc_pct: float = DEFAULT_MIN_ALLOC_PCT,
) -> list[dict[str, Any]]:
    """holdings: enriched open holdings (drawdown_from_peak_pct, gain_pct,
    portfolio_contribution, verdict already attached).
    ohlc_by_symbol: {symbol: [~260 OHLC rows newest-first]} from price_history.
    trend_by_symbol / trend_prev_by_symbol: latest and DELTA_SESSIONS-ago
    trend_results rows. prev_verdicts: {holding_id: last recorded verdict}.
    """
    alerts: list[dict[str, Any]] = []

    def add(severity: str, rule: str, symbol: Optional[str], title: str, detail: str):
        alerts.append({"severity": severity, "rule": rule, "symbol": symbol,
                       "title": title, "detail": detail})

    for h in holdings:
        sym = h["symbol"]
        ohlc = ohlc_by_symbol.get(sym, [])
        changes = _daily_changes(ohlc)
        trend = trend_by_symbol.get(sym, {})
        prev = trend_prev_by_symbol.get(sym, {})
        close = _f(trend.get("close_price"))

        # ── Consecutive circuit-grade moves (dominate plain streaks) ──
        down_run = _consecutive_run(changes, -CIRCUIT_PCT)
        up_run = _consecutive_run(changes, CIRCUIT_PCT)
        if down_run >= CIRCUIT_SESSIONS:
            add("critical", "consecutive_lower_circuit", sym,
                f"{sym}: {down_run} straight -5% sessions",
                f"Has fallen 5% or more in each of the last {down_run} sessions — exit liquidity may be limited.")
        elif up_run >= CIRCUIT_SESSIONS:
            add("medium", "consecutive_upper_circuit", sym,
                f"{sym}: {up_run} straight +5% sessions",
                f"Has risen 5% or more in each of the last {up_run} sessions — extended move, watch for reversal.")
        else:
            # Plain green/red streaks (only when circuit rules didn't fire)
            green = _consecutive_run(changes, 0.0)
            red = _consecutive_red(changes)
            if red >= GREEN_RED_SESSIONS:
                add("high", "consecutive_red_days", sym,
                    f"{sym}: {red} straight red closes",
                    f"Has closed lower for {red} consecutive sessions.")
            elif green >= GREEN_RED_SESSIONS:
                add("low", "consecutive_green_days", sym,
                    f"{sym}: {green} straight green closes",
                    f"Has closed higher for {green} consecutive sessions.")

        # ── Gap up / gap down (today's open vs previous close) ────────
        if ohlc:
            o, pc = _f(ohlc[0].get("open_price")), _f(ohlc[0].get("prev_close"))
            if o is not None and pc:
                gap = (o - pc) / pc * 100
                if gap <= -GAP_PCT:
                    add("high", "gap_down", sym,
                        f"{sym}: gapped down {abs(gap):.1f}%",
                        f"Opened {abs(gap):.1f}% below the previous close — overnight news is moving this stock.")
                elif gap >= GAP_PCT:
                    add("low", "gap_up", sym,
                        f"{sym}: gapped up {gap:.1f}%",
                        f"Opened {gap:.1f}% above the previous close.")

        # ── 52-week high / low, breakout / breakdown ──────────────────
        closes_hist = [_f(r.get("close_price")) for r in ohlc[1:] if r.get("close_price") is not None]
        year = closes_hist[:252]
        window = closes_hist[:RANGE_SESSIONS]
        fired_extreme = False
        pct_from_high = _f(trend.get("pct_from_high"))
        if close is not None and pct_from_high is not None and pct_from_high >= 0:
            add("low", "new_52w_high", sym,
                f"{sym}: new 52-week high",
                f"Closed at {close:.2f}, its highest close of the past year.")
            fired_extreme = True
        elif close is not None and year and close <= min(year):
            add("critical", "new_52w_low", sym,
                f"{sym}: new 52-week low",
                f"Closed at {close:.2f}, its lowest close of the past year.")
            fired_extreme = True
        if not fired_extreme and close is not None and len(window) >= RANGE_SESSIONS // 2:
            if close > max(window):
                add("low", "breakout", sym,
                    f"{sym}: {RANGE_SESSIONS}-session breakout",
                    f"Closed at {close:.2f}, above its highest close of the last {RANGE_SESSIONS} sessions.")
            elif close < min(window):
                add("critical", "breakdown", sym,
                    f"{sym}: {RANGE_SESSIONS}-session breakdown",
                    f"Closed at {close:.2f}, below its lowest close of the last {RANGE_SESSIONS} sessions.")

        # ── ATR expansion (volatility regime change) ──────────────────
        atr_fast, atr_slow = _atr(ohlc, ATR_FAST), _atr(ohlc, ATR_SLOW)
        if atr_fast is not None and atr_slow and atr_fast / atr_slow >= ATR_EXPANSION_RATIO:
            add("high", "atr_expansion", sym,
                f"{sym}: volatility expanding",
                f"5-session ATR is {atr_fast / atr_slow:.1f}x its 20-session average — daily ranges are widening sharply.")

        # ── Drawdown from post-buy peak (highest tier only) ───────────
        drawdown = _f(h.get("drawdown_from_peak_pct"))
        if drawdown is not None:
            for tier, severity in DRAWDOWN_TIERS:
                if drawdown >= tier:
                    add(severity, f"drawdown_{int(tier)}", sym,
                        f"{sym}: {drawdown:.1f}% off its peak",
                        f"Down {drawdown:.1f}% from its highest close since you bought (threshold {tier:.0f}%).")
                    break

        # ── Volume spike ──────────────────────────────────────────────
        ratio = volume_ratios.get(sym)
        if ratio is not None and ratio >= VOLUME_SPIKE_RATIO:
            add("high", "volume_spike", sym,
                f"{sym}: volume {ratio:.1f}x average",
                f"Today's volume is {ratio:.1f}x its 20-session average — something is moving this stock.")

        # ── Moving-average breaks (most significant only) ─────────────
        if close is not None:
            ema200 = _f(trend.get("ema_200"))
            ema50 = _f(trend.get("ema_50"))
            ema21 = _f(trend.get("ema_21"))
            if ema200 is not None and close < ema200:
                add("critical", "below_200dma", sym,
                    f"{sym}: below 200 DMA",
                    f"Closed at {close:.2f}, under its 200-day average of {ema200:.2f} — long-term trend broken.")
            elif ema50 is not None and close < ema50:
                add("high", "below_50ema", sym,
                    f"{sym}: below 50 EMA",
                    f"Closed at {close:.2f}, under its 50-day EMA of {ema50:.2f}.")
            elif ema21 is not None and close < ema21:
                add("medium", "below_20ema", sym,
                    f"{sym}: below 20 EMA",
                    f"Closed at {close:.2f}, under its short-term (21-day) EMA of {ema21:.2f}.")

        # ── RSI breakdown ─────────────────────────────────────────────
        rsi = _f(trend.get("rsi_14"))
        if rsi is not None and rsi < RSI_BREAKDOWN:
            add("high", "rsi_breakdown", sym,
                f"{sym}: RSI {rsi:.0f}",
                f"RSI-14 has broken down to {rsi:.0f} — persistent selling pressure.")

        # ── Relative strength / momentum regime shifts ────────────────
        rs_now, rs_then = _f(trend.get("rs_score")), _f(prev.get("rs_score"))
        if rs_now is not None and rs_then is not None:
            delta = rs_now - rs_then
            if delta <= -RS_DELTA:
                add("high", "rs_weakness", sym,
                    f"{sym}: relative strength fading",
                    f"RS score dropped {abs(delta):.0f} points in {DELTA_SESSIONS} sessions ({rs_then:.0f} → {rs_now:.0f}).")
            elif delta >= RS_DELTA:
                add("low", "rs_improvement", sym,
                    f"{sym}: relative strength building",
                    f"RS score gained {delta:.0f} points in {DELTA_SESSIONS} sessions ({rs_then:.0f} → {rs_now:.0f}).")
        mom_now, mom_then = _f(trend.get("momentum_score")), _f(prev.get("momentum_score"))
        if mom_now is not None and mom_then is not None:
            delta = mom_now - mom_then
            if delta <= -MOMENTUM_DELTA:
                add("high", "momentum_deterioration", sym,
                    f"{sym}: momentum deteriorating",
                    f"Momentum score fell {abs(delta):.0f} points in {DELTA_SESSIONS} sessions ({mom_then:.0f} → {mom_now:.0f}).")
            elif delta >= MOMENTUM_DELTA:
                add("low", "momentum_improvement", sym,
                    f"{sym}: momentum accelerating",
                    f"Momentum score gained {delta:.0f} points in {DELTA_SESSIONS} sessions ({mom_then:.0f} → {mom_now:.0f}).")

        # ── Verdict engine: current state + changes ───────────────────
        if h.get("verdict") == "EXIT":
            add("critical", "verdict_exit", sym,
                f"{sym}: verdict is Exit",
                (h.get("verdict_reasons") or [{}])[0].get("detail", "The rule engine's exit criteria fired."))
        prior = prev_verdicts.get(h.get("id"))
        if prior and h.get("verdict") and prior != h["verdict"]:
            worse = {"EXIT": "critical", "TRIM": "high"}.get(h["verdict"])
            add(worse or "medium", "verdict_changed", sym,
                f"{sym}: verdict changed to {h.get('verdict_label') or h['verdict']}",
                f"The rule engine's call moved from {prior.replace('_', ' ').title()} to {(h.get('verdict_label') or h['verdict'])}.")

        # ── Allocation limits ─────────────────────────────────────────
        weight = _f(h.get("portfolio_contribution"))
        if weight is not None:
            if weight > risk_limit_pct:
                add("critical", "position_risk_limit", sym,
                    f"{sym}: {weight:.1f}% of portfolio",
                    f"Position weight {weight:.1f}% exceeds your {risk_limit_pct:.0f}% risk limit.")
            elif weight >= target_alloc_pct:
                add("medium", "target_allocation_reached", sym,
                    f"{sym}: reached target allocation",
                    f"Position weight {weight:.1f}% has reached your {target_alloc_pct:.0f}% target — consider rebalancing.")
            elif weight < min_alloc_pct:
                add("low", "below_min_allocation", sym,
                    f"{sym}: below minimum allocation",
                    f"Position weight {weight:.1f}% is under your {min_alloc_pct:.0f}% minimum — too small to matter, or worth topping up.")

    # ── Portfolio-level concentration ─────────────────────────────────
    top_pos = _f(concentration.get("top_position_pct"))
    if top_pos is not None and top_pos > TOP_POSITION_LIMIT and top_pos <= risk_limit_pct:
        add("medium", "concentration_position", None,
            f"Top position is {top_pos:.1f}% of capital",
            f"Your largest holding is {top_pos:.1f}% of the portfolio (guideline: under {TOP_POSITION_LIMIT:.0f}%).")
    top_sector = _f(concentration.get("top_sector_pct"))
    if top_sector is not None and top_sector > TOP_SECTOR_LIMIT:
        add("high", "concentration_sector", None,
            f"Top sector is {top_sector:.1f}% of capital",
            f"A single sector holds {top_sector:.1f}% of your capital (guideline: under {TOP_SECTOR_LIMIT:.0f}%).")

    alerts.sort(key=lambda a: (SEVERITY_ORDER[a["severity"]], a["symbol"] or ""))
    return alerts


# ── Data access ────────────────────────────────────────────────────────

async def fetch_ohlc_history(conn, symbols: list[str], sessions: int = 260) -> dict[str, list[dict[str, Any]]]:
    """~1 year of OHLC per symbol, newest first."""
    if not symbols:
        return {}
    rows = await conn.fetch(
        """
        with ranked as (
            select symbol, trade_date, open_price, high_price, low_price, close_price, prev_close,
                   row_number() over (partition by symbol order by trade_date desc) as rn
            from price_history
            where symbol = any($1::text[])
        )
        select symbol, trade_date, open_price, high_price, low_price, close_price, prev_close
        from ranked where rn <= $2
        order by symbol, trade_date desc
        """,
        symbols, sessions,
    )
    out: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        out.setdefault(r["symbol"], []).append(dict(r))
    return out


async def fetch_trend_levels(conn, symbols: list[str], trade_date) -> dict[str, dict[str, Any]]:
    if not symbols or trade_date is None:
        return {}
    rows = await conn.fetch(
        """
        select symbol, close_price, ema_21, ema_50, ema_200, rsi_14,
               rs_score, momentum_score, pct_from_high, high_52w
        from trend_results
        where symbol = any($1::text[]) and trade_date = $2
        """,
        symbols, trade_date,
    )
    return {r["symbol"]: dict(r) for r in rows}


async def fetch_trend_previous(conn, symbols: list[str], trade_date, sessions_back: int = DELTA_SESSIONS) -> dict[str, dict[str, Any]]:
    """rs_score / momentum_score as of `sessions_back` trading sessions ago."""
    if not symbols or trade_date is None:
        return {}
    rows = await conn.fetch(
        """
        with ranked as (
            select symbol, rs_score, momentum_score,
                   row_number() over (partition by symbol order by trade_date desc) as rn
            from trend_results
            where symbol = any($1::text[]) and trade_date <= $2
        )
        select symbol, rs_score, momentum_score from ranked where rn = $3
        """,
        symbols, trade_date, sessions_back + 1,
    )
    return {r["symbol"]: dict(r) for r in rows}


async def fetch_previous_verdicts(conn, holding_ids: list[int], before_date) -> dict[int, str]:
    """Most recent recorded verdict strictly before `before_date` per holding."""
    if not holding_ids or before_date is None:
        return {}
    try:
        rows = await conn.fetch(
            """
            select distinct on (holding_id) holding_id, verdict
            from holding_verdicts
            where holding_id = any($1::bigint[]) and trade_date < $2
            order by holding_id, trade_date desc
            """,
            holding_ids, before_date,
        )
    except Exception:
        return {}   # table missing (pre-v7) — verdict-change alerts just don't fire
    return {r["holding_id"]: r["verdict"] for r in rows}
