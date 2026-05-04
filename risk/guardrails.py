"""Daily/monthly stops, correlation check, margin cap, concurrent risk cap."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class GuardrailCheck:
    ok: bool
    reasons: list[str]

    def __bool__(self) -> bool:
        return self.ok


class Guardrails:
    def __init__(self, cfg: dict):
        # Validate required config keys exist
        if "risk" not in cfg:
            raise ValueError("Missing 'risk' section in config")
        if "account" not in cfg or "capital" not in cfg["account"]:
            raise ValueError("Missing 'account.capital' in config")
        
        r = cfg["risk"]
        cap = cfg["account"]["capital"]
        self.capital = float(cap)
        
        # Use .get() with sensible defaults for risk parameters
        self.daily_stop = self.capital * r.get("daily_stop_pct", 0.02)
        self.monthly_stop = self.capital * r.get("monthly_stop_pct", 0.05)
        self.concurrent_cap = self.capital * r.get("concurrent_open_pct", 0.25)
        self.margin_cap_pct = r.get("margin_util_cap", 0.80)
        self.corr_max = r.get("correlation_max", 0.75)

    def check_new_trade(self, *, proposed_risk: float, open_risk: float,
                        day_pnl: float, month_pnl: float,
                        margin_used_pct: float,
                        max_correlation_vs_open: float = 0.0) -> GuardrailCheck:
        reasons: list[str] = []

        if day_pnl <= -self.daily_stop:
            reasons.append(f"Daily stop hit: day P&L ₹{day_pnl:,.0f} ≤ −₹{self.daily_stop:,.0f}")

        if month_pnl <= -self.monthly_stop:
            reasons.append(f"Monthly stop hit: month P&L ₹{month_pnl:,.0f} ≤ −₹{self.monthly_stop:,.0f}")

        if open_risk + proposed_risk > self.concurrent_cap:
            reasons.append(
                f"Concurrent open risk would exceed ₹{self.concurrent_cap:,.0f} "
                f"(open ₹{open_risk:,.0f} + new ₹{proposed_risk:,.0f})"
            )

        if margin_used_pct > self.margin_cap_pct:
            reasons.append(f"Margin util {margin_used_pct:.0%} > cap {self.margin_cap_pct:.0%}")

        if max_correlation_vs_open > self.corr_max:
            reasons.append(
                f"Correlation {max_correlation_vs_open:.2f} with existing position > {self.corr_max:.2f}"
            )

        return GuardrailCheck(ok=not reasons, reasons=reasons)

    def eod_flatten_required(self, day_pnl: float) -> bool:
        return day_pnl <= -self.daily_stop

    def size_halve_next_month(self, month_pnl: float) -> bool:
        return month_pnl <= -self.monthly_stop
