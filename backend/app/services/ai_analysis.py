"""
AI-powered holding analysis via Google Gemini.

Sends a compact snapshot of each active holding (technicals + P/L) to Gemini
and asks it to classify each one into exactly one of four trader actions:
HOLD, TRIM, ADD MORE, EXIT ALL — with one-line reasoning.

This is a single batched call for the whole portfolio (cheaper, faster, and
lets the model reason about the holdings relative to each other) rather than
one call per holding.

Fails soft: if GEMINI_API_KEY is not configured or the call errors out, the
caller gets a clear error rather than a half-parsed result.
"""
import json
import logging
import re
from typing import Any
import os

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

GEMINI_URL_TMPL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
VALID_ACTIONS = {"HOLD", "TRIM", "ADD MORE", "EXIT ALL"}


class AIAnalysisError(Exception):
    pass


def _build_prompt(holdings: list[dict[str, Any]]) -> str:
    lines = [
        "You are a disciplined equity portfolio analyst reviewing an Indian (NSE) retail investor's holdings.",
        "For EACH holding below, decide exactly one action from this fixed set: HOLD, TRIM, ADD MORE, EXIT ALL.",
        "Base your call on unrealized gain/loss, momentum score (0-100), RSI-14, ADX-14, trend strength (trending_days out of 12), "
        "12-day price change, and how long the position has been held. Be decisive and concise.",
        "",
        "Holdings:",
    ]
    for h in holdings:
        lines.append(
            f"- id={h['id']} | {h['symbol']} ({h.get('sector') or 'Unclassified'}) | "
            f"gain={h.get('gain_pct')}% | days_held={h.get('days_held')} | "
            f"momentum_score={h.get('momentum_score')} | rsi_14={h.get('rsi_14')} | adx_14={h.get('adx_14')} | "
            f"trending_days={h.get('trending_days')}/12 | chg_12d={h.get('chg_12d')}%"
        )
    lines += [
        "",
        "Respond with ONLY a JSON array, no markdown fences, no commentary outside the JSON. "
        "Each element must look exactly like this shape:",
        '{"id": <holding id as integer>, "action": "HOLD" | "TRIM" | "ADD MORE" | "EXIT ALL", '
        '"reasoning": "<one short sentence, max 20 words>", "confidence": <integer 0-100>}',
    ]
    return "\n".join(lines)


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise AIAnalysisError("Gemini did not return a parseable JSON array")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise AIAnalysisError(f"Could not parse Gemini response as JSON: {exc}") from exc
    if not isinstance(data, list):
        raise AIAnalysisError("Gemini response JSON was not a list")
    return data


async def analyze_holdings(holdings: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Returns {holding_id: {"action": ..., "reasoning": ..., "confidence": ...}}."""
    if not settings.GEMINI_API_KEY:
        raise AIAnalysisError(
            "AI analysis is not configured. Set the GEMINI_API_KEY environment variable on the backend."
        )
    if not holdings:
        return {}

    prompt = _build_prompt(holdings)
    url = GEMINI_URL_TMPL.format(model=settings.GEMINI_MODEL)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048},
    }

    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, params={"key": settings.GEMINI_API_KEY}, json=body)
        except httpx.HTTPError as exc:
            raise AIAnalysisError(f"Could not reach Gemini: {exc}") from exc

    if resp.status_code != 200:
        logger.error("Gemini error %s: %s", resp.status_code, resp.text[:500])
        raise AIAnalysisError(f"Gemini API error ({resp.status_code}). Check your API key and quota.")

    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise AIAnalysisError("Unexpected Gemini response shape") from exc

    parsed = _extract_json_array(text)
    result: dict[int, dict[str, Any]] = {}
    for item in parsed:
        try:
            hid = int(item["id"])
            action = str(item["action"]).strip().upper()
            if action not in VALID_ACTIONS:
                action = "HOLD"
            result[hid] = {
                "action": action,
                "reasoning": str(item.get("reasoning", "")).strip()[:200],
                "confidence": max(0, min(100, int(item.get("confidence", 50)))),
            }
        except (KeyError, ValueError, TypeError):
            continue
    return result
