"""Telegram alerts. Falls back to stdout when token missing."""
from __future__ import annotations

import os
import sys

import requests


_last_send = {
    "ts": None,
    "ok": None,
    "status": None,
    "error": None,
    "text_preview": None
}

class Alerter:
    def __init__(self, bot_token: str | None, chat_id: str | None):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send(self, text: str) -> bool:
        global _last_send
        import time
        
        # BUG-A01 FIX: Enforce Telegram 4096-char limit
        MAX_TG = 4096
        if len(text) > MAX_TG:
            text = text[:MAX_TG - 20] + "\n...[truncated]"
        
        _last_send["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _last_send["text_preview"] = text[:50] + "..." if len(text) > 50 else text

        if not self.bot_token or not self.chat_id:
            print(f"[ALERT:FALLBACK]\n{text}\n", file=sys.stdout, flush=True)
            _last_send["ok"] = False
            _last_send["status"] = "FALLBACK"
            _last_send["error"] = "Missing token or chat_id"
            return False
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            # BUG-26 FIX: Try Markdown first; if Telegram rejects it (unsupported
            # formatting), retry as plain text so the alert is still delivered.
            r = requests.post(url, json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
            try:
                data = r.json()
            except Exception:
                data = {}
            if r.ok and data.get("ok"):
                print(f"[ALERT:SENT]\n{text}\n", flush=True)
                _last_send["ok"] = True
                _last_send["status"] = r.status_code
                _last_send["error"] = None
                return True
            else:
                err_desc = data.get("description", "Unknown error")
                # Retry without parse_mode if Markdown parsing failed
                if not r.ok or not data.get("ok"):
                    r2 = requests.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=10)
                    try:
                        data2 = r2.json()
                    except Exception:
                        data2 = {}
                    if r2.ok and data2.get("ok"):
                        print(f"[ALERT:SENT:PLAIN]\n{text}\n", flush=True)
                        _last_send["ok"] = True
                        _last_send["status"] = r2.status_code
                        _last_send["error"] = f"Markdown failed ({err_desc}); sent as plain text"
                        return True
                print(f"[ALERT:FAIL] {r.status_code} - {err_desc}\n{text}\n", flush=True)
                _last_send["ok"] = False
                _last_send["status"] = r.status_code
                _last_send["error"] = err_desc
                return False
        except Exception as e:
            print(f"[ALERT:FAIL] exception: {e}\n{text}\n", flush=True)
            _last_send["ok"] = False
            _last_send["status"] = "EXCEPTION"
            _last_send["error"] = str(e)
            return False


def _badge_emoji(bias: str) -> str:
    return {"LONG": "🟢", "SHORT": "🔴"}.get(bias, "⚪")


def _regime_emoji(regime_val: str) -> str:
    return {
        "TREND_UP": "📈",
        "TREND_DOWN": "📉",
        "RANGE_LOW_VOL": "↔️",
        "RANGE_HIGH_VOL": "〰️",
        "VOL_EXPANSION": "💥",
        "VOL_CONTRACTION": "🫧",
    }.get(regime_val, "❓")


def _format_sector_line(sectors: list[tuple[str, float]], emoji: str) -> str:
    parts = [f"{s}({v:+.1f}%)" for s, v in sectors[:3]]
    return f"  {emoji} " + "  ".join(parts)


def _ai_sentiment_badge(ai_sentiment) -> str:
    if ai_sentiment is None:
        return ""
    if isinstance(ai_sentiment, dict):
        sent = str(ai_sentiment.get("overall_market_sentiment") or "").upper()
        conf = str(ai_sentiment.get("confidence") or "LOW").upper()
    else:
        sent = str(ai_sentiment).upper()
        conf = "MED"
    icon = {"BULLISH": "🟢", "POSITIVE": "🟢", "BEARISH": "🔴", "NEGATIVE": "🔴"}.get(sent, "⚪")
    return f"\n🤖 AI Sentiment: {icon} `{sent}` ({conf})"


def format_dashboard(regime_snap, flow_snap, structure_plan, longs, shorts) -> str:
    """Modern, scannable Telegram dashboard alert."""
    from datetime import datetime, timezone, timedelta

    regime_val = regime_snap.regime.value if hasattr(regime_snap.regime, 'value') else regime_snap.regime
    r_emoji = _regime_emoji(regime_val)
    b_emoji = _badge_emoji(flow_snap.smart_money_bias)

    # Timestamp
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist)
    ts_str = now_ist.strftime("%d %b, %H:%M IST")

    lines = []

    # ── Header ──
    lines.append(f"📊 *StockMinded — Morning Brief*")
    lines.append("")

    # ── Regime block ──
    lines.append(f"{r_emoji} *Regime:* `{regime_val}`")
    parts = [f"Trend {regime_snap.trend_score:+d}"]
    parts.append(f"VIX {regime_snap.vix}")
    if regime_snap.vix_5d_change_pct:
        parts.append(f"({regime_snap.vix_5d_change_pct:+.1f}% 5d)")
    parts.append(f"ADX {regime_snap.adx}")
    lines.append("  " + "  ·  ".join(parts))
    if regime_snap.breadth_pct_above_50dma is not None:
        lines.append(f"  Breadth {regime_snap.breadth_pct_above_50dma}% above 50-DMA")
    if regime_snap.notes and regime_snap.notes != "ok":
        lines.append(f"  _{regime_snap.notes}_")
    lines.append("")

    # ── Smart Money block ──
    lines.append(f"{b_emoji} *Smart Money:* `{flow_snap.smart_money_bias}`")
    fii_dii = flow_snap.fii_dii_5d_net_cr or {}
    fii_net = fii_dii.get("fii", 0)
    dii_net = fii_dii.get("dii", 0)
    lines.append(f"  FII {fii_net:+,.0f} Cr  ·  DII {dii_net:+,.0f} Cr  (5d cash)")

    # FII derivatives if available
    derivs = getattr(flow_snap, 'fii_derivatives_5d', {}) or {}
    if derivs:
        idx_fut = derivs.get("fii_index_futures_5d", 0)
        if idx_fut:
            lines.append(f"  FII Index Fut {idx_fut:+,.0f} Cr  (5d)")
    lines.append("")

    # ── Options Pulse ──
    lines.append("📈 *Options Pulse*")
    pcr_parts = []
    if flow_snap.pcr_oi is not None:
        pcr_parts.append(f"PCR OI `{flow_snap.pcr_oi}`")
    if flow_snap.pcr_vol is not None:
        pcr_parts.append(f"PCR Vol `{flow_snap.pcr_vol}`")
    if flow_snap.max_pain is not None:
        pcr_parts.append(f"MaxPain `{flow_snap.max_pain:,.0f}`")
    if pcr_parts:
        lines.append("  " + "  ·  ".join(pcr_parts))
    else:
        lines.append("  _Option chain unavailable_")
    lines.append("")

    # ── Sector Rotation ──
    if flow_snap.top_inflow_sectors or flow_snap.top_outflow_sectors:
        lines.append("🔄 *Sector Rotation*")
        if flow_snap.top_inflow_sectors:
            lines.append(_format_sector_line(flow_snap.top_inflow_sectors, "🟢"))
        if flow_snap.top_outflow_sectors:
            lines.append(_format_sector_line(flow_snap.top_outflow_sectors, "🔴"))
        lines.append("")

    # ── Playbook ──
    lines.append(f"🎯 *Playbook:* `{regime_val}`")
    lines.append(f"  {structure_plan.primary}")
    if structure_plan.secondary:
        lines.append(f"  _{structure_plan.secondary}_")
    lines.append("")

    # ── A-Grade Picks ──
    if longs or shorts:
        lines.append("📋 *A-Grade Picks*")
        if longs:
            syms = "  ".join(r.symbol for r in longs[:5])
            lines.append(f"  ▲ {syms}")
        if shorts:
            syms = "  ".join(r.symbol for r in shorts[:5])
            lines.append(f"  ▼ {syms}")
        lines.append("")

    # ── AI Sentiment (if available) ──
    ai_line = _ai_sentiment_badge(getattr(flow_snap, 'ai_sentiment', None))
    if ai_line:
        lines.append(ai_line)
        lines.append("")

def format_telegram_alert(verdict: dict) -> str:
    """Format a single investment verdict as a structured markdown trade card.

    ``verdict`` keys: symbol, verdict, confidence, rationale, key_risks,
    entry_zone, stop_loss, target, telegram_channel.
    """
    sym = verdict.get("symbol", "—")
    v = verdict.get("verdict", "AVOID")
    conf = verdict.get("confidence", "LOW")
    emoji = {"BUY": "🟢", "SELL": "🔴", "AVOID": "⚪"}.get(v, "⚪")
    lines = [
        f"{emoji} *{sym} — {v}*  `({conf})`",
    ]
    if verdict.get("entry_zone"):
        lines.append(f"  Entry: `{verdict['entry_zone']}`")
    if verdict.get("stop_loss"):
        lines.append(f"  SL: `{verdict['stop_loss']}`")
    if verdict.get("target"):
        lines.append(f"  Target: `{verdict['target']}`")
    if verdict.get("rationale"):
        ratio = verdict["rationale"]
        if len(ratio) > 280:
            ratio = ratio[:277] + "…"
        lines.append(f"  _{ratio}_")
    if verdict.get("key_risks"):
        lines.append(f"  ⚠️ Risks: {verdict['key_risks']}")
    if verdict.get("telegram_channel"):
        lines.append(f"  📡 src: {verdict['telegram_channel']}")
    return "\n".join(lines)


def send_telegram_alert(verdict: dict, bot_token: str | None, chat_id: str | None) -> bool:
    """Send a single verdict card via the Bot API (separate from userbot)."""
    alerter = Alerter(bot_token, chat_id)
    return alerter.send(format_telegram_alert(verdict))

