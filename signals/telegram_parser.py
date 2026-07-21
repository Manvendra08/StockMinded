"""LLM-based extraction of companies + news events from Telegram messages.

Two-pass design:
  1. SPAM FILTER — reject promotional/scam messages (investment offers, 
     guaranteed returns, "make 2 lakhs tomorrow", etc.) with zero LLM cost.
  2. NEWS EXTRACTION — for legitimate messages, extract company names,
     news/event context, event type, sentiment direction, and NSE ticker.

Handles Hindi/English (Hinglish) mixes, emojis, and fuzzy company names by
delegating normalization to the LLM and then post-filtering against the FNO
universe so only valid tradeable symbols survive.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ExtractedTicker:
    """A company mention extracted from a Telegram message.

    Backward-compatible with the old ticker-only interface (symbol, confidence,
    context) but now carries rich news/event metadata for the fusion engine.
    """
    symbol: str
    confidence: float
    context: str
    # --- New fields for news/event-aware pipeline ---
    company_name: str = ""           # Full company name from message
    news_event: str = ""             # What happened (e.g. "bags Rs 500 Cr order from ONGC")
    event_type: str = "general"      # order | earnings | merger | regulatory | expansion | management | sector | general
    sentiment_direction: str = "NEUTRAL"  # POSITIVE | NEGATIVE | NEUTRAL


# ── Spam / promo detection (cheap regex, no LLM cost) ────────────────

_SPAM_PATTERNS = [
    # Investment offers / guaranteed returns
    r"(?i)(?:invest\s+(?:any\s+)?amount|make\s+\d|guarantee[ds]?\s+return|double\s+your|quick\s+money|fast\s+money)",
    # Referral / join / deposit schemes
    r"(?i)(?:join\s+(?:now|our|telegram)|deposit\s+(?:any\s+)?amount|special\s+offer|limited\s+time)",
    # WhatsApp / Telegram group promotion
    r"(?i)(?:whatsapp|telegram)\s+(?:group|link|channel|join|number)",
    # "I want to help" scam pattern
    r"(?i)i\s+want\s+to\s+help\s+\d+",
    # Phone number patterns (Indian mobile)
    r"(?i)\+?91[\s-]?\d{5}[\s-]?\d{5}",
    # Crypto / forex / binary options spam
    r"(?i)(?:crypto|bitcoin|forex|binary\s+option|binance|coindcx)\s+(?:signal|call|tip)",
    # "Tomorrow morning" / "next day" guarantee patterns
    r"(?i)(?:tomorrow|next\s+day)\s+(?:morning|100%|sure|confirm)",
    # Excessive caps + exclamation (shouting promo)
    r"(?:[A-Z\s]{20,}[!]{2,})",
]


def is_spam(text: str) -> bool:
    """Quick regex-based spam/promo detection. Returns True if message is spam."""
    if not text or len(text.strip()) < 10:
        return True  # Too short to be meaningful news
    for pat in _SPAM_PATTERNS:
        if re.search(pat, text):
            return True
    return False


# ── LLM prompts for news extraction ──────────────────────────────────

_EXTRACTION_SYSTEM_PROMPT = (
    "You are an Indian equity research analyst specializing in NSE-listed companies. "
    "Read the Telegram message (may mix Hindi, English, Hinglish, emojis, typos) and:\n"
    "1. Determine if it contains a legitimate NEWS EVENT about an Indian company "
    "   (order win, earnings, merger, regulatory approval, expansion, management change, sector development).\n"
    "2. If YES, extract EVERY company mentioned with:\n"
    "   - company_name: full company name as written\n"
    "   - symbol: NSE trading symbol (UPPERCASE, no suffix like .NS/.BO). "
    "     e.g. 'Reliance Industries' -> 'RELIANCE', 'TCS' -> 'TCS', 'HDFC Bank' -> 'HDFCBANK'\n"
    "   - news_event: one-line summary of the news/event (<=80 chars)\n"
    "   - event_type: one of [order, earnings, merger, regulatory, expansion, management, sector, general]\n"
    "   - sentiment: POSITIVE if news is bullish, NEGATIVE if bearish, NEUTRAL if unclear\n"
    "   - confidence: [0.0-1.0] how confident you are this is a real, actionable company mention\n"
    "3. IGNORE indices (NIFTY, BANKNIFTY, FINNIFTY, SENSEX), ETFs, and generic market commentary.\n"
    "4. If the message is spam, promotional, or has no real company news, return {\"spam\": true}.\n\n"
    "Return ONLY JSON: {\n"
    "  \"spam\": false,\n"
    "  \"mentions\": [{\n"
    "    \"company_name\": str,\n"
    "    \"symbol\": str,\n"
    "    \"news_event\": str,\n"
    "    \"event_type\": str,\n"
    "    \"sentiment\": \"POSITIVE\"|\"NEGATIVE\"|\"NEUTRAL\",\n"
    "    \"confidence\": float\n"
    "  }]\n"
    "}\n"
    "If spam or no companies: {\"spam\": true, \"mentions\": []}"
)


def _coerce_symbol(raw: str) -> str:
    """Normalize an NSE ticker: uppercase, strip exchange suffixes, remove invalid chars."""
    if not raw:
        return ""
    s = raw.strip().upper()
    s = re.sub(r"\.NS$|\.BO$", "", s)
    s = re.sub(r"[^A-Z0-9&]", "", s)
    return s


def parse_message(
    text: str,
    call_llm=None,
    universe: set[str] | None = None,
    model: str = "llama-3.3-70b-versatile",
    min_confidence: float = 0.4,
) -> list[ExtractedTicker]:
    """Extract companies + news events from a single Telegram message.

    Two-pass design:
      1. Spam filter (regex, instant) — rejects promo/scam messages.
      2. LLM extraction — identifies companies, news context, event type, sentiment.

    Args:
        text: raw message text.
        call_llm: the project's ``data.ai_scraper.call_llm`` (injected to keep
            this module importable in tests without the LLM stack).
        universe: set of allowed NSE symbols; tickers outside it are dropped
            (set to None to skip the universe filter).
        model: model name passed through to call_llm.
        min_confidence: drop mentions below this confidence.
    Returns list of ExtractedTicker (may be empty).
    """
    if not text or not text.strip():
        return []

    clean = text.strip()
    from datetime import datetime
    dt_str = datetime.now().strftime('%H:%M:%S')

    # ── Pass 1: Spam filter (zero LLM cost) ──
    if is_spam(clean):
        logger.debug("Message rejected as spam: %s", clean[:80])
        return []

    if call_llm is None:
        try:
            from data.ai_scraper import call_llm
        except Exception as e:  # pragma: no cover
            logger.error("call_llm unavailable: %s", e)
            print(f"\033[91m[{dt_str}] [LLM Ticker Extract] Result: FAILED. call_llm unavailable: {e}\033[0m")
            return []

    # ── Pass 2: LLM extraction ──
    print(f"\033[96m[{dt_str}] [LLM Ticker Extract] Attempting ticker extraction on: \"{clean[:60]}...\"\033[0m")
    try:
        resp = call_llm(
            prompt=clean,
            system_prompt=_EXTRACTION_SYSTEM_PROMPT,
            json_mode=True,
            max_tokens=1536,
        )
    except Exception as e:
        logger.error("LLM extraction failed: %s", e)
        print(f"\033[91m[{datetime.now().strftime('%H:%M:%S')}] [LLM Ticker Extract] Result: FAILED. API error: {e}\033[0m")
        return []

    if not resp:
        print(f"\033[91m[{datetime.now().strftime('%H:%M:%S')}] [LLM Ticker Extract] Result: FAILED. Empty response\033[0m")
        return []

    try:
        if isinstance(resp, str):
            data = json.loads(resp)
        else:
            data = resp
    except Exception as e:
        logger.error("Failed to parse LLM JSON: %s", e)
        print(f"\033[91m[{datetime.now().strftime('%H:%M:%S')}] [LLM Ticker Extract] Result: FAILED. JSON Parse error: {e}\033[0m")
        return []

    # If LLM flagged as spam (second opinion beyond regex)
    if data.get("spam"):
        logger.debug("LLM flagged message as spam")
        print(f"\033[93m[{datetime.now().strftime('%H:%M:%S')}] [LLM Ticker Extract] Result: LLM flagged as spam (skipped)\033[0m")
        return []

    mentions_raw = data.get("mentions", []) if isinstance(data, dict) else []

    results: list[ExtractedTicker] = []
    for item in mentions_raw:
        if not isinstance(item, dict):
            continue

        sym = _coerce_symbol(str(item.get("symbol", "")))
        if not sym:
            continue

        # Skip indices and ETFs
        if sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "NIFTY50"):
            continue

        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf < min_confidence:
            continue

        # Universe filter: allow any NSE symbol if universe is None
        if universe is not None and sym not in universe:
            logger.debug("Symbol %s not in universe, skipping", sym)
            continue

        event_type = str(item.get("event_type", "general")).lower()
        if event_type not in ("order", "earnings", "merger", "regulatory", "expansion", "management", "sector", "general"):
            event_type = "general"

        sentiment = str(item.get("sentiment", "NEUTRAL")).upper()
        if sentiment not in ("POSITIVE", "NEGATIVE", "NEUTRAL"):
            sentiment = "NEUTRAL"

        results.append(
            ExtractedTicker(
                symbol=sym,
                confidence=round(conf, 3),
                context=str(item.get("news_event", ""))[:200],
                company_name=str(item.get("company_name", ""))[:100],
                news_event=str(item.get("news_event", ""))[:200],
                event_type=event_type,
                sentiment_direction=sentiment,
            )
        )

    print(f"\033[92m[{datetime.now().strftime('%H:%M:%S')}] [LLM Ticker Extract] Result: SUCCESS. Extracted symbols: {[t.symbol for t in results]}\033[0m")
    return results
