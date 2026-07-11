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


def format_dashboard(regime_snap, flow_snap, structure_plan, longs, shorts) -> str:
    lines = []
    lines.append("*📊 Morning Dashboard*")
    lines.append("")
    # Safe access for regime value (handles both Enum and string)
    regime_val = regime_snap.regime.value if hasattr(regime_snap.regime, 'value') else regime_snap.regime
    lines.append(f"*Regime:* `{regime_val}`")
    lines.append(
        f"  trend={regime_snap.trend_score:+d}  VIX={regime_snap.vix} ({regime_snap.vix_5d_change_pct:+.1f}% 5d)  ADX={regime_snap.adx}"
    )
    if regime_snap.breadth_pct_above_50dma is not None:
        lines.append(f"  breadth>50DMA: {regime_snap.breadth_pct_above_50dma}%")
    lines.append(f"  notes: {regime_snap.notes}")
    lines.append("")
    lines.append(f"*Flows* — bias: `{flow_snap.smart_money_bias}`")
    lines.append(f"  FII/DII 5d (₹Cr): {flow_snap.fii_dii_5d_net_cr}")
    lines.append(f"  PCR OI={flow_snap.pcr_oi}  Vol={flow_snap.pcr_vol}  MaxPain={flow_snap.max_pain}")
    if flow_snap.top_inflow_sectors:
        lines.append(f"  🟢 in: {', '.join(f'{s}({v:+.1f}%)' for s, v in flow_snap.top_inflow_sectors)}")
    if flow_snap.top_outflow_sectors:
        lines.append(f"  🔴 out: {', '.join(f'{s}({v:+.1f}%)' for s, v in flow_snap.top_outflow_sectors)}")
    lines.append("")
    if longs:
        lines.append("*Leaders (A-grade long):* " + ", ".join(r.symbol for r in longs[:5]))
    if shorts:
        lines.append("*Laggards (A-grade short):* " + ", ".join(r.symbol for r in shorts[:5]))
    lines.append("")
    lines.append(f"*Structure:* {structure_plan.primary}")
    lines.append(f"  alt: {structure_plan.secondary}")
    lines.append(f"  notes: {structure_plan.notes}")
    return "\n".join(lines)
