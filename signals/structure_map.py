"""Regime → trade structure mapping."""
from __future__ import annotations

from dataclasses import dataclass

from signals.regime import Regime


@dataclass
class StructurePlan:
    primary: str
    secondary: str
    notes: str


MAP: dict[Regime, StructurePlan] = {
    Regime.TREND_UP: StructurePlan(
        primary="Long futures/stock on A-grade leaders; Bull Call Spread Nifty (ATM/ATM+1).",
        secondary="Debit call spreads on leader stocks; avoid naked short premium.",
        notes="Trail stops on 3-EMA 15m after +1R. Book 50% at +1.5R.",
    ),
    Regime.TREND_DOWN: StructurePlan(
        primary="Bear Put Spread Nifty/BankNifty; short futures on A-grade laggards.",
        secondary="Put debit spreads on weak stocks.",
        notes="Size down in high VIX; prefer defined-risk.",
    ),
    Regime.RANGE_LOW_VOL: StructurePlan(
        primary="Iron Condor Nifty weekly (20–25 delta wings, 100pt width).",
        secondary="Short strangle with defined-risk wings on BankNifty.",
        notes="Exit at 50% max profit or 1 wing tested. No new ICs if VIX > 14.",
    ),
    Regime.RANGE_HIGH_VOL: StructurePlan(
        primary="Defined-risk Nifty income only if option chain is fresh: Iron Fly / wide Iron Condor.",
        secondary="Wait in cash when max-pain/PCR is missing or stale.",
        notes="No naked option selling. Reduce size in high VIX.",
    ),
    Regime.VOL_EXPANSION: StructurePlan(
        primary="Long Straddle/Strangle ONLY around scheduled event (RBI, Fed, budget, result).",
        secondary="Long options on confirmed breakout of range extreme on volume.",
        notes="Exit at +40% or post-event IV crush > 15%.",
    ),
    Regime.VOL_CONTRACTION: StructurePlan(
        primary="Short premium: credit spreads, Iron Condor, calendars.",
        secondary="Ratio spreads if directional bias clear.",
        notes="Great for income. Watch for sudden expansion — gamma risk.",
    ),
    Regime.TREND_EMERGING_UP: StructurePlan(
        primary="Early positional Long futures/stock on emerging leaders; Bull Call Spread Nifty.",
        secondary="Debit call spreads on breaking-out stocks.",
        notes="Scale in with 0.75x position sizing as trend emerges.",
    ),
    Regime.TREND_EMERGING_DOWN: StructurePlan(
        primary="Early positional Bear Put Spread Nifty/BankNifty; short futures on emerging laggards.",
        secondary="Put debit spreads on breakdown stocks.",
        notes="Scale in with 0.75x position sizing as breakdown confirms.",
    ),
}


def plan_for(regime: Regime) -> StructurePlan:
    return MAP.get(regime, StructurePlan(primary="Hold — regime unrecognized", secondary=None, notes=""))


def build_structure_map(cfg=None) -> dict[str, dict[str, str]]:
    """Convert MAP into a serializable dictionary for the dashboard."""
    return {
        r.value: {
            "primary": plan.primary,
            "secondary": plan.secondary,
            "notes": plan.notes,
        }
        for r, plan in MAP.items()
    }
