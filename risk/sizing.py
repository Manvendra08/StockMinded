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


def directional_size(capital: float, per_trade_pct: float, entry: float, stop: float,
                     lot_size: int = 1) -> SizeResult:
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


def option_structure_size(capital: float, per_trade_pct: float, max_loss_per_lot: float,
                          lot_size: int) -> SizeResult:
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
