from dataclasses import dataclass
from typing import Literal, Callable
import pandas as pd

from signals.options import atm_strike, delta_strike, _next_expiry

@dataclass
class OptionLeg:
    side: Literal["BUY", "SELL"]
    option_type: Literal["CE", "PE"]
    strike_rule: str      # "ATM", "ATM+2", "DELTA_25", "DELTA_10", "ATM±1%"
    expiry_rule: str      # "WEEKLY" | "MONTHLY"
    qty_ratio: int = 1

@dataclass
class OptionStructure:
    name: str
    legs: list[OptionLeg]
    entry_condition: str
    exit_rules: dict       # {pct_profit: 0.5, pct_loss: 1.0, gamma_sqoff_min: 30}
    max_loss_formula: Callable

@dataclass
class ResolvedLeg:
    side: str
    type: str
    strike: float
    expiry: str
    lots: int
    lot_size: int
    premium: float

def pick_structure(regime, bias, iv_rank, vix) -> OptionStructure | None:
    # Handle bootstrap None IVR
    if iv_rank is None:
        high_iv = vix > 16
        low_iv = vix < 14
    else:
        high_iv = iv_rank > 50
        low_iv = iv_rank < 30
        
    if regime == "TREND_UP" and bias in ("LONG", "NEUTRAL"):
        return OptionStructure(
            name="BULL_CALL_SPREAD",
            legs=[
                OptionLeg("BUY", "CE", "ATM", "WEEKLY", 1),
                OptionLeg("SELL", "CE", "ATM+2", "WEEKLY", 1)
            ],
            entry_condition="TREND_UP & LONG",
            exit_rules={"pct_profit": 0.5, "pct_loss": 1.0, "gamma_sqoff_min": 30},
            max_loss_formula=lambda net_debit: net_debit
        )
    elif regime == "TREND_DOWN" and bias in ("SHORT", "NEUTRAL"):
        return OptionStructure(
            name="BEAR_PUT_SPREAD",
            legs=[
                OptionLeg("BUY", "PE", "ATM", "WEEKLY", 1),
                OptionLeg("SELL", "PE", "ATM-2", "WEEKLY", 1)
            ],
            entry_condition="TREND_DOWN & SHORT",
            exit_rules={"pct_profit": 0.5, "pct_loss": 1.0, "gamma_sqoff_min": 30},
            max_loss_formula=lambda net_debit: net_debit
        )
    elif regime == "VOL_EXPANSION" or (regime.startswith("RANGE_") and low_iv):
        return OptionStructure(
            name="LONG_STRADDLE",
            legs=[
                OptionLeg("BUY", "CE", "ATM", "WEEKLY", 1),
                OptionLeg("BUY", "PE", "ATM", "WEEKLY", 1)
            ],
            entry_condition="VOL_EXPANSION or LOW_IV",
            exit_rules={"pct_profit": 0.4, "pct_loss": 0.3, "gamma_sqoff_min": 30},
            max_loss_formula=lambda net_debit: net_debit
        )
    elif regime in ("RANGE_LOW_VOL", "RANGE_HIGH_VOL"):
        if high_iv:
            delta_short = "DELTA_20" if regime == "RANGE_HIGH_VOL" else "DELTA_25"
            return OptionStructure(
                name="SHORT_STRANGLE_WINGED",
                legs=[
                    OptionLeg("SELL", "CE", delta_short, "WEEKLY", 1),
                    OptionLeg("SELL", "PE", delta_short, "WEEKLY", 1),
                    OptionLeg("BUY", "CE", "DELTA_10", "WEEKLY", 1),
                    OptionLeg("BUY", "PE", "DELTA_10", "WEEKLY", 1)
                ],
                entry_condition="RANGE & HIGH_IV",
                exit_rules={"pct_profit": 0.5, "pct_loss": 1.5, "gamma_sqoff_min": 30},
                max_loss_formula=lambda net_credit, wing_dist=0: wing_dist - net_credit # Simplified
            )
            
    return None

def resolve_legs(structure: OptionStructure, chain: pd.DataFrame, spot: float, lot_size: int, strike_step: int) -> list[ResolvedLeg]:
    if chain.empty: return []
    
    strikes = sorted(chain["strike"].tolist())
    atm = atm_strike(spot, strikes)
    if not atm: return []
    
    atm_idx = strikes.index(atm)
    
    resolved = []
    
    for leg in structure.legs:
        target_strike = None
        if leg.strike_rule == "ATM":
            target_strike = atm
        elif leg.strike_rule == "ATM+2":
            idx = min(len(strikes) - 1, atm_idx + 2)
            target_strike = strikes[idx]
        elif leg.strike_rule == "ATM-2":
            idx = max(0, atm_idx - 2)
            target_strike = strikes[idx]
        elif leg.strike_rule.startswith("DELTA_"):
            d_val = float(leg.strike_rule.split("_")[1]) / 100.0
            target_strike = delta_strike(chain, d_val, leg.option_type)
            
        if target_strike is None:
            # Fallback
            target_strike = atm
            
        row = chain[chain["strike"] == target_strike]
        if row.empty: return []
        
        premium = row.iloc[0].get(f"{leg.option_type.lower()}_ltp", 0)
        expiry = row.iloc[0].get("expiry")
        
        resolved.append(ResolvedLeg(
            side=leg.side,
            type=leg.option_type,
            strike=target_strike,
            expiry=expiry,
            lots=leg.qty_ratio,
            lot_size=lot_size,
            premium=premium
        ))
        
    return resolved
