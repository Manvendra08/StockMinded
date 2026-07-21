"""Fusion engine: hard fundamental filters first, then LLM verdict fusion.

Design (per plan §2.4):
  1. HARD FILTERS (pure Python) reject symbols that clearly fail the
     fundamental quality bar. Cheap, deterministic, no LLM cost.
  2. LLM FUSION only for survivors: match Telegram news/event thesis with
     fundamentals and emit a BUY/SELL/AVOID verdict with levels.
  3. POST-PROCESS: schema validation + attach regime context.

The fusion engine now considers:
  - News event type (order win, earnings, merger, regulatory, etc.)
  - Sentiment direction from the parser (POSITIVE/NEGATIVE/NEUTRAL)
  - Fundamental quality bar (hard filters)
  - Market regime context
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FilterResult:
    symbol: str
    passed: bool
    reason: str = ""
    fundamentals: dict | None = None


@dataclass
class Verdict:
    symbol: str
    verdict: str  # BUY | SELL | AVOID
    confidence: str  # HIGH | MEDIUM | LOW
    rationale: str = ""
    key_risks: str = ""
    entry_zone: str = ""
    stop_loss: str = ""
    target: str = ""
    fundamentals_json: str = ""
    regime_at_scan: str = ""
    # --- New fields for news/event-aware pipeline ---
    news_event: str = ""             # What happened (from parser)
    event_type: str = "unknown"      # order | earnings | merger | etc.
    sentiment_direction: str = "NEUTRAL"  # POSITIVE | NEGATIVE | NEUTRAL
    company_name: str = ""           # Full company name


# Sectors for which the Debt/Equity filter is bypassed (plan §2.4).
_DE_FILTER_BYPASS_SECTORS = {"BANKS", "FINANCIALS", "NBFC"}


def _sector_bypasses_de(fundamentals: dict) -> bool:
    sector = str(fundamentals.get("sector") or "").upper()
    return sector in _DE_FILTER_BYPASS_SECTORS


def apply_hard_filters(
    fundamentals: dict,
    filters: dict,
) -> tuple[bool, str]:
    """Return (passed, reason). ``filters`` keys: max_debt_to_equity,
    min_roce_pct, max_promoter_pledge_pct, min_sales_growth_3y_pct,
    min_profit_growth_3y_pct, min_qtr_sales_var_pct, min_qtr_profit_var_pct,
    margin_compression_reject (bool)."""
    # 1. Margin compression auto-reject (highest priority, data-driven).
    if filters.get("margin_compression_reject", True):
        if fundamentals.get("margin_decay_3q"):
            return False, "OPM declined 3 quarters sequentially (margin decay)"
        q_sales = fundamentals.get("qtr_sales_var_pct")
        q_profit = fundamentals.get("qtr_profit_var_pct")
        if q_sales is not None and q_profit is not None and q_sales > 0:
            if q_profit < 0.85 * q_sales:
                return False, (
                    f"Margin compression (Qtr Profit YoY {q_profit}% < "
                    f"0.85x Qtr Sales YoY {q_sales}%)"
                )

    checks = [
        ("debt_to_equity", filters.get("max_debt_to_equity"), "Excessive debt", "lt", True),
        ("roce_pct", filters.get("min_roce_pct"), "Poor capital returns", "gt", False),
        ("promoter_pledge_pct", filters.get("max_promoter_pledge_pct"), "High promoter pledge", "lt", False),
        ("sales_growth_3y_pct", filters.get("min_sales_growth_3y_pct"), "Insufficient growth", "gt", False),
        ("profit_growth_3y_pct", filters.get("min_profit_growth_3y_pct"), "Insufficient profit growth", "gt", False),
        ("qtr_sales_var_pct", filters.get("min_qtr_sales_var_pct"), "Weak quarterly sales", "gt", False),
        ("qtr_profit_var_pct", filters.get("min_qtr_profit_var_pct"), "Negative quarterly profit", "gt", False),
    ]
    for key, threshold, reason, direction, de_only in checks:
        if threshold is None:
            continue
        if de_only and _sector_bypasses_de(fundamentals):
            continue
        val = fundamentals.get(key)
        if val is None:
            # The free Screener page often omits Debt/Equity, Pledged % and
            # 3Y growth. Treat missing data as "no opinion" (skip) rather than
            # an automatic AVOID, so we don't reject every symbol. Margin
            # compression checks above already enforce the data we do have.
            logger.info("Hard filter skipped (data missing): %s for %s",
                        key, fundamentals.get("symbol", "?"))
            continue
        if direction == "lt" and not (val <= threshold):
            return False, f"{reason} ({key}={val} > {threshold})"
        if direction == "gt" and not (val >= threshold):
            return False, f"{reason} ({key}={val} < {threshold})"
    return True, "Passed hard filters"


# ── Event-type significance weights ──────────────────────────────────
# Some news events are more fundamentally significant than others.
# Used to adjust confidence in the fusion prompt.
_EVENT_SIGNIFICANCE = {
    "order": "HIGH",       # Revenue-critical: order wins directly impact top line
    "earnings": "HIGH",    # Core financials: quarterly results are decisive
    "merger": "HIGH",      # Structural: M&A changes the company fundamentally
    "regulatory": "MEDIUM",  # Can be positive (approval) or negative (penalty)
    "expansion": "MEDIUM",   # Capacity addition → future growth
    "management": "MEDIUM",  # Key hires/departures affect strategy
    "sector": "LOW",         # Sector-wide news, not company-specific
    "general": "LOW",        # Unclear or miscellaneous
}


_FUSION_SYSTEM = (
    "You are a senior Indian equities analyst performing fundamental investment analysis. "
    "Given a Telegram news/event about a company and its Screener.in NSE fundamentals, decide a directional investment verdict.\n\n"
    "IMPORTANT CONTEXT:\n"
    "- The news/event came from a Telegram channel that shares Indian market updates.\n"
    "- You must evaluate whether the news is TRULY material to the company's long-term fundamentals.\n"
    "- A large order win for a small company is more significant than for a large company.\n"
    "- Sentiment from the news source is a HINT, not a directive — verify with fundamentals.\n"
    "- If fundamentals are weak despite positive news, still verdict AVOID or BUY with LOW confidence.\n"
    "- If fundamentals are strong and news is positive, upgrade to HIGH confidence.\n\n"
    "Output ONLY JSON: {\n"
    "  \"symbol\": str,\n"
    "  \"verdict\": \"BUY\"|\"SELL\"|\"AVOID\",\n"
    "  \"confidence\": \"HIGH\"|\"MEDIUM\"|\"LOW\",\n"
    "  \"rationale\": str (2-3 sentences explaining the verdict),\n"
    "  \"key_risks\": str (comma-separated risks),\n"
    "  \"entry_zone\": str (e.g. '₹2450-2460'),\n"
    "  \"stop_loss\": str (e.g. '₹2380'),\n"
    "  \"target\": str (e.g. '₹2650')\n"
    "}. "
    "Set verdict AVOID if the news is not material enough to trade on, "
    "or if fundamentals contradict the news thesis."
)


def _fusion_prompt(
    symbol: str,
    context: str,
    fundamentals: dict,
    regime: str = "INVESTMENT",
    event_type: str = "general",
    sentiment: str = "NEUTRAL",
    company_name: str = "",
) -> str:
    significance = _EVENT_SIGNIFICANCE.get(event_type, "LOW")
    parts = [
        f"SYMBOL: {symbol}",
    ]
    if company_name:
        parts.append(f"COMPANY: {company_name}")
    parts.extend([
        f"NEWS EVENT ({event_type}, significance={significance}): {context}",
        f"SOURCE SENTIMENT: {sentiment}",
        f"FUNDAMENTALS: {json.dumps(fundamentals, default=str)}",
        "",
        "Provide the investment verdict and price levels (Entry, SL, Target) above.",
    ])
    return "\n".join(parts)


def fuse_symbol(
    symbol: str,
    context: str,
    fundamentals: dict,
    regime: str = "INVESTMENT",
    call_llm=None,
    model: str = "llama-3.3-70b-versatile",
    max_tokens: int = 2048,
    event_type: str = "general",
    sentiment: str = "NEUTRAL",
    company_name: str = "",
) -> Verdict:
    """Run LLM fusion for a single surviving symbol.

    Evaluates news/event context and fundamentals to emit a BUY/SELL/AVOID verdict
    with investment entry, SL, and target levels.
    """
    from datetime import datetime
    dt_str = datetime.now().strftime('%H:%M:%S')
    if call_llm is None:
        try:
            from data.ai_scraper import call_llm
        except Exception as e:  # pragma: no cover
            logger.error("call_llm unavailable: %s", e)
            print(f"\033[91m[{dt_str}] [LLM Verdict Fusion] Result: FAILED for {symbol}. call_llm unavailable: {e}\033[0m")
            return Verdict(symbol=symbol, verdict="AVOID", confidence="LOW",
                           rationale="LLM unavailable")

    print(f"\033[96m[{dt_str}] [LLM Verdict Fusion] Analyzing {symbol} ({company_name or 'Unknown company'})...\033[0m")
    try:
        resp = call_llm(
            prompt=_fusion_prompt(
                symbol, context, fundamentals, regime,
                event_type=event_type, sentiment=sentiment,
                company_name=company_name,
            ),
            system_prompt=_FUSION_SYSTEM,
            json_mode=True,
            max_tokens=max_tokens,
        )
    except Exception as e:
        logger.error("Fusion LLM failed for %s: %s", symbol, e)
        print(f"\033[91m[{datetime.now().strftime('%H:%M:%S')}] [LLM Verdict Fusion] Result: FAILED for {symbol}. API error: {e}\033[0m")
        return Verdict(symbol=symbol, verdict="AVOID", confidence="LOW",
                       rationale=f"LLM error: {e}")

    if not resp:
        print(f"\033[91m[{datetime.now().strftime('%H:%M:%S')}] [LLM Verdict Fusion] Result: FAILED for {symbol}. Empty response\033[0m")
        return Verdict(symbol=symbol, verdict="AVOID", confidence="LOW",
                       rationale="Empty LLM response")

    try:
        data = json.loads(resp) if isinstance(resp, str) else resp
        verdict = str(data.get("verdict", "AVOID")).upper()
        if verdict not in ("BUY", "SELL"):
            verdict = "AVOID"
        conf = str(data.get("confidence", "LOW")).upper()
        if conf not in ("HIGH", "MEDIUM", "LOW"):
            conf = "LOW"

        entry_zone = str(data.get("entry_zone", "")).strip()
        stop_loss = str(data.get("stop_loss", "")).strip()
        target = str(data.get("target", "")).strip()

        # Fallback level calculation if BUY verdict emitted but levels are missing
        if verdict == "BUY":
            cmp = fundamentals.get("current_price") or fundamentals.get("price")
            if cmp and isinstance(cmp, (int, float)) and cmp > 0:
                if not entry_zone or entry_zone in ("-", "—", "N/A", "None"):
                    entry_zone = f"₹{round(cmp * 0.99, 1)} - ₹{round(cmp * 1.01, 1)}"
                if not stop_loss or stop_loss in ("-", "—", "N/A", "None"):
                    stop_loss = f"₹{round(cmp * 0.93, 1)}"
                if not target or target in ("-", "—", "N/A", "None"):
                    target = f"₹{round(cmp * 1.20, 1)}"

        print(f"\033[92m[{datetime.now().strftime('%H:%M:%S')}] [LLM Verdict Fusion] Result: SUCCESS for {symbol}. Verdict: {verdict} ({conf})\033[0m")
        return Verdict(
            symbol=symbol,
            verdict=verdict,
            confidence=conf,
            rationale=str(data.get("rationale", ""))[:1000],
            key_risks=str(data.get("key_risks", ""))[:500],
            entry_zone=entry_zone[:60],
            stop_loss=stop_loss[:60],
            target=target[:60],
            fundamentals_json=json.dumps(fundamentals, default=str),
            regime_at_scan="INVESTMENT",
            news_event=context[:200],
            event_type=event_type,
            sentiment_direction=sentiment,
            company_name=company_name[:100],
        )
    except Exception as e:
        logger.error("Fusion parse failed for %s: %s", symbol, e)
        print(f"\033[91m[{datetime.now().strftime('%H:%M:%S')}] [LLM Verdict Fusion] Result: FAILED to parse for {symbol}. Error: {e}\033[0m")
        return Verdict(symbol=symbol, verdict="AVOID", confidence="LOW",
                       rationale=f"Parse error: {e}")


def run_fusion(
    extracted: list,  # list of signals.telegram_parser.ExtractedTicker
    fundamentals_fn,  # callable(symbol) -> dict | None
    filters: dict,
    regime: str = "UNKNOWN",
    call_llm=None,
    model: str = "llama-3.3-70b-versatile",
    max_tokens: int = 2048,
) -> list[Verdict]:
    """End-to-end: hard filters then LLM fusion. Returns Verdict list.

    Now passes news/event metadata (event_type, sentiment, company_name)
    through to the fusion LLM so it can weigh news significance against
    fundamentals.
    """
    verdicts: list[Verdict] = []
    for tk in extracted:
        try:
            fundamentals = fundamentals_fn(tk.symbol, tk.company_name)
        except TypeError:
            fundamentals = fundamentals_fn(tk.symbol)
        if fundamentals is None:
            verdicts.append(
                Verdict(
                    symbol=tk.symbol, verdict="AVOID", confidence="LOW",
                    rationale="Fundamentals unavailable",
                    news_event=getattr(tk, "news_event", ""),
                    event_type=getattr(tk, "event_type", "general"),
                    sentiment_direction=getattr(tk, "sentiment_direction", "NEUTRAL"),
                    company_name=getattr(tk, "company_name", ""),
                )
            )
            continue
        passed, reason = apply_hard_filters(fundamentals, filters)
        if not passed:
            verdicts.append(
                Verdict(
                    symbol=tk.symbol, verdict="AVOID", confidence="LOW",
                    rationale=reason,
                    fundamentals_json=json.dumps(fundamentals, default=str),
                    regime_at_scan=regime,
                    news_event=getattr(tk, "news_event", ""),
                    event_type=getattr(tk, "event_type", "general"),
                    sentiment_direction=getattr(tk, "sentiment_direction", "NEUTRAL"),
                    company_name=getattr(tk, "company_name", ""),
                )
            )
            continue
        verdicts.append(
            fuse_symbol(
                tk.symbol, tk.context, fundamentals, regime,
                call_llm=call_llm, model=model, max_tokens=max_tokens,
                event_type=getattr(tk, "event_type", "general"),
                sentiment=getattr(tk, "sentiment_direction", "NEUTRAL"),
                company_name=getattr(tk, "company_name", ""),
            )
        )
    return verdicts
