"""Position sizing: directional by stop-distance, options by max-loss."""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizeResult:
    qty: int
    risk_rupees: float
    notional: float
    notes: str = ""


def directional_size(
    capital: float,
    per_trade_pct: float,
    entry: float,
    stop: float,
    lot_size: int = 1,
    *,
    direction: str = "LONG",
) -> SizeResult:
    """Size a directional trade by risk budget / stop-distance.

    Args:
        capital: Total account capital in rupees.
        per_trade_pct: Fraction of capital to risk per trade (e.g. 0.01).
        entry: Entry price.
        stop: Stop-loss price.
        lot_size: Minimum tradeable lot (1 for equities).
        direction: "LONG" or "SHORT".  Used to validate stop placement.

    Raises:
        ValueError: If stop is on the wrong side of entry for the given
            direction (e.g. stop > entry for a LONG trade).
    """
    # --- Direction-aware stop validation (P2 fix) ---
    if direction.upper() == "LONG" and stop >= entry:
        raise ValueError(
            f"Invalid stop for LONG trade: stop ({stop}) must be below entry ({entry}). "
            "Check whether entry and stop were passed in the wrong order."
        )
    if direction.upper() == "SHORT" and stop <= entry:
        raise ValueError(
            f"Invalid stop for SHORT trade: stop ({stop}) must be above entry ({entry}). "
            "Check whether entry and stop were passed in the wrong order."
        )

    risk_budget = capital * per_trade_pct
    per_unit_risk = abs(entry - stop)
    if per_unit_risk <= 0:
        return SizeResult(0, 0.0, 0.0, "invalid stop")
    raw_qty = risk_budget / per_unit_risk
    lots = math.floor(raw_qty / lot_size)
    qty = lots * lot_size
    return SizeResult(
        qty=qty,
        risk_rupees=round(qty * per_unit_risk, 2),
        notional=round(qty * entry, 2),
        notes=f"{lots} lots × {lot_size}",
    )


def option_structure_size(
    capital: float,
    per_trade_pct: float,
    max_loss_per_lot: float,
    lot_size: int,
) -> SizeResult:
    """max_loss_per_lot = ₹ max loss for 1 lot of the structure (already in rupees)."""
    risk_budget = capital * per_trade_pct
    if max_loss_per_lot <= 0:
        return SizeResult(0, 0.0, 0.0, "invalid max loss")
    lots = math.floor(risk_budget / max_loss_per_lot)
    qty = lots * lot_size
    return SizeResult(
        qty=qty,
        risk_rupees=round(lots * max_loss_per_lot, 2),
        notional=0.0,
        notes=f"{lots} lots of structure",
    )
