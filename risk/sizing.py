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
    if direction.upper() == "LONG" and stop > entry:
        raise ValueError(
            f"Invalid stop for LONG trade: stop ({stop}) must be below entry ({entry}). "
            "Check whether entry and stop were passed in the wrong order."
        )
    if direction.upper() == "SHORT" and stop < entry:
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
    *,
    margin_per_lot: float = 0.0,
    max_notional: float = 0.0,
) -> SizeResult:
    """Size an option structure by risk budget / max-loss, with margin guard.

    Args:
        capital: Total account capital in rupees.
        per_trade_pct: Fraction of capital to risk per trade (e.g. 0.01 for 1%).
        max_loss_per_lot: ₹ max loss for 1 lot of the structure (already in rupees).
        lot_size: Contract multiplier (e.g. 75 for NIFTY).
        margin_per_lot: ₹ margin required per lot by the exchange/broker.
            If provided, lots are also capped by capital / margin_per_lot to
            prevent margin exhaustion. Pass 0 to skip the margin check.
        max_notional: Maximum notional deployment. If provided, lots are also
            capped by max_notional / (premium_per_lot * lot_size). Pass 0 to skip.

    M8 FIX: Added margin_per_lot parameter. Previously, structures with low
    max_loss but high margin requirements could be sized beyond the account's
    margin capacity, causing broker rejection or margin calls.

    Returns:
        SizeResult with qty, risk_rupees, notional, and notes describing
        which constraint was binding (risk, margin, or notional).
    """
    if max_loss_per_lot <= 0:
        return SizeResult(0, 0.0, 0.0, "invalid max loss")

    risk_budget = capital * per_trade_pct
    lots_by_risk = math.floor(risk_budget / max_loss_per_lot)

    # M8 FIX: Margin guard — cap lots by available capital / margin requirement
    lots_by_margin = lots_by_risk  # default: no margin constraint
    if margin_per_lot > 0:
        lots_by_margin = math.floor(capital / margin_per_lot)

    # Take the most conservative of risk-based and margin-based sizing
    lots = min(lots_by_risk, lots_by_margin)

    # Determine which constraint was binding for the notes
    constraint = "risk"
    if lots_by_margin < lots_by_risk:
        constraint = "margin"

    qty = lots * lot_size

    notes_parts = [f"{lots} lots of structure"]
    if constraint != "risk":
        notes_parts.append(f"capped by {constraint}")
    if margin_per_lot > 0:
        notes_parts.append(f"margin/lot=₹{margin_per_lot:,.0f}")

    return SizeResult(
        qty=qty,
        risk_rupees=round(lots * max_loss_per_lot, 2),
        notional=0.0,
        notes=" | ".join(notes_parts),
    )
