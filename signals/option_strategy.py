from dataclasses import dataclass, field
from typing import Literal, Callable, Optional
import pandas as pd

from signals.options import atm_strike, delta_strike, _next_expiry


@dataclass
class OptionLeg:
    side: Literal["BUY", "SELL"]
    option_type: Literal["CE", "PE"]
    strike_rule: str
    expiry_rule: str
    qty_ratio: int = 1


@dataclass
class OptionStructure:
    name: str
    legs: list[OptionLeg]
    entry_condition: str
    exit_rules: dict
    max_loss_formula: Callable
    structure_type: str = "unknown"
    wing_width: float = 0.0
    is_defined_risk: bool = True


@dataclass
class ResolvedLeg:
    side: str
    type: str
    strike: float
    expiry: str
    lots: int
    lot_size: int
    premium: float


@dataclass
class NiftyOptionSetup:
    symbol: str = "NIFTY"
    mode: str = "positional"
    strategy: str = ""
    regime: str = ""
    bias: str = ""
    vix: float = 0.0
    vix_change_pct: float = 0.0
    pcr: Optional[float] = None
    entry_reason: str = ""
    legs: list = field(default_factory=list)
    net_credit: float = 0.0
    max_loss_rupees: float = 0.0
    risk_pct: float = 0.0
    breakevens: list = field(default_factory=list)
    short_strikes: list = field(default_factory=list)
    wing_width: float = 0.0
    lot_count: int = 1
    entry_window_ok: bool = True
    vol_expansion_blocked: bool = False
    suitable: bool = True
    skip_reason: str = ""
    exit_rules: dict = field(default_factory=dict)


def pick_nifty_strategy(data: dict, regime: str, bias: str, vix: float, vix_change_pct: float,
                        pcr: Optional[float] = None, cfg: dict = None) -> Optional[NiftyOptionSetup]:
    """Select NIFTY option-selling strategy based on regime/bias/VIX."""
    if cfg is None:
        from config.loader import load_config
        cfg = load_config()
    
    nifty_cfg = cfg.get("nifty_options", {})
    if not nifty_cfg.get("enabled", False):
        return None
    
    avoid_vol_exp = nifty_cfg.get("avoid_vol_expansion", True)
    vol_exp_thresh = nifty_cfg.get("vol_expansion_threshold", 5.0)
    vol_expansion_blocked = avoid_vol_exp and vix_change_pct > vol_exp_thresh
    
    if regime == "VOL_EXPANSION":
        return NiftyOptionSetup(symbol="NIFTY", regime=regime, vix=vix, vix_change_pct=vix_change_pct,
                                suitable=False, skip_reason="VOL_EXPANSION")
    
    if vol_expansion_blocked and regime in ("RANGE_LOW_VOL", "RANGE_HIGH_VOL"):
        return NiftyOptionSetup(symbol="NIFTY", regime=regime, vix=vix, vix_change_pct=vix_change_pct,
                                suitable=False, skip_reason=f"VIX expanding {vix_change_pct:.1f}% > {vol_exp_thresh}%")
    
    mode = nifty_cfg.get("mode", "positional")
    verdict = data.get("verdict", {})
    nifty_v = verdict.get("nifty", {})
    v_action = nifty_v.get("action", "WAIT")

    # If the verdict explicitly requested Naked Selling, prioritize it
    if v_action == "NAKED_OPTION_SELL":
        return _build_naked_selling(regime, bias, vix, pcr, cfg, mode)

    if regime in ("RANGE_LOW_VOL", "VOL_CONTRACTION"):
        return _build_iron_condor(regime, bias, vix, pcr, cfg, mode, vol_expansion_blocked)
    elif regime == "RANGE_HIGH_VOL" and not vol_expansion_blocked:
        return _build_wider_iron_condor(regime, bias, vix, pcr, cfg, mode)
    elif regime == "TREND_UP" and bias in ("LONG", "BULL_FLOW"):
        return _build_bull_put_spread(regime, bias, vix, pcr, cfg, mode, vol_expansion_blocked)
    elif regime == "TREND_DOWN" and bias in ("SHORT", "BEAR_FLOW"):
        return _build_bear_call_spread(regime, bias, vix, pcr, cfg, mode, vol_expansion_blocked)
    return None

def _build_naked_selling(regime: str, bias: str, vix: float, pcr: Optional[float],
                         cfg: dict, mode: str) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    direction = "LONG" if bias in ("LONG", "BULL_FLOW") else "SHORT"
    strategy = "NAKED_PUT_SELL" if direction == "LONG" else "NAKED_CALL_SELL"
    return NiftyOptionSetup(
        symbol="NIFTY", mode=mode, strategy=strategy, regime=regime, bias=bias,
        vix=vix, pcr=pcr, wing_width=0, entry_reason=f"Directional bias {bias} -> Naked {strategy}",
        suitable=True,
        exit_rules={"profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
                    "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
                    "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
                    "eod_exit": mode == "intraday", "expiry_exit": mode == "positional"}
    )


def _build_iron_condor(regime: str, bias: str, vix: float, pcr: Optional[float],
                       cfg: dict, mode: str, vol_exp_blocked: bool) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    wing = nifty_cfg.get("iron_condor_wing_width", 300)
    return NiftyOptionSetup(
        symbol="NIFTY", mode=mode, strategy="IRON_CONDOR", regime=regime, bias=bias,
        vix=vix, pcr=pcr, wing_width=wing, entry_reason=f"RANGE_LOW_VOL Iron Condor, wing={wing}",
        vol_expansion_blocked=vol_exp_blocked, suitable=True,
        exit_rules={"profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
                    "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
                    "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
                    "eod_exit": mode == "intraday", "expiry_exit": mode == "positional"}
    )


def _build_wider_iron_condor(regime: str, bias: str, vix: float, pcr: Optional[float],
                             cfg: dict, mode: str) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    wing = nifty_cfg.get("iron_condor_wing_width", 300) * 1.5
    return NiftyOptionSetup(
        symbol="NIFTY", mode=mode, strategy="IRON_CONDOR_WIDE", regime=regime, bias=bias,
        vix=vix, pcr=pcr, wing_width=wing, entry_reason=f"RANGE_HIGH_VOL wider IC, wing={wing}",
        suitable=True,
        exit_rules={"profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
                    "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
                    "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
                    "eod_exit": mode == "intraday", "expiry_exit": mode == "positional"}
    )


def _build_bull_put_spread(regime: str, bias: str, vix: float, pcr: Optional[float],
                           cfg: dict, mode: str, vol_blocked: bool) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    width = nifty_cfg.get("spread_width", 200)
    if vol_blocked:
        return NiftyOptionSetup(symbol="NIFTY", regime=regime, bias=bias, vix=vix,
                                suitable=False, skip_reason="Bull Put blocked: VIX expanding")
    return NiftyOptionSetup(
        symbol="NIFTY", mode=mode, strategy="BULL_PUT_SPREAD", regime=regime, bias=bias,
        vix=vix, pcr=pcr, wing_width=width, entry_reason=f"TREND_UP Bull Put Spread, width={width}",
        vol_expansion_blocked=vol_blocked, suitable=True,
        exit_rules={"profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
                    "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
                    "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
                    "eod_exit": mode == "intraday", "expiry_exit": mode == "positional"}
    )


def _build_bear_call_spread(regime: str, bias: str, vix: float, pcr: Optional[float],
                            cfg: dict, mode: str, vol_blocked: bool) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    width = nifty_cfg.get("spread_width", 200)
    if vol_blocked:
        return NiftyOptionSetup(symbol="NIFTY", regime=regime, bias=bias, vix=vix,
                                suitable=False, skip_reason="Bear Call blocked: VIX expanding")
    return NiftyOptionSetup(
        symbol="NIFTY", mode=mode, strategy="BEAR_CALL_SPREAD", regime=regime, bias=bias,
        vix=vix, pcr=pcr, wing_width=width, entry_reason=f"TREND_DOWN Bear Call Spread, width={width}",
        vol_expansion_blocked=vol_blocked, suitable=True,
        exit_rules={"profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
                    "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
                    "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
                    "eod_exit": mode == "intraday", "expiry_exit": mode == "positional"}
    )


def resolve_nifty_structure(setup: NiftyOptionSetup, chain: pd.DataFrame,
                            spot: float, lot_size: int, strike_step: int, cfg: dict = None) -> NiftyOptionSetup:
    if chain.empty or not setup.suitable:
        return setup
    
    chain = chain.copy()
    chain["ce_ltp"] = pd.to_numeric(chain["ce_ltp"], errors="coerce").fillna(0.0)
    chain["pe_ltp"] = pd.to_numeric(chain["pe_ltp"], errors="coerce").fillna(0.0)

    setup.spot = spot
    if cfg is None:
        from config.loader import load_config
        cfg = load_config()
    min_lots = max(1, cfg.get("nifty_options", {}).get("min_lots_per_leg", 1))
    
    strikes = sorted(chain["strike"].tolist())
    atm = atm_strike(spot, strikes)
    if not atm:
        setup.suitable = False
        setup.skip_reason = "Could not find ATM strike"
        return setup

    # Guard: if ALL premiums are 0 the data source has no live prices (after-hours / OI-only).
    # Do NOT generate phantom zero-premium trades.
    has_live_prices = (chain["ce_ltp"].sum() + chain["pe_ltp"].sum()) > 0
    if not has_live_prices:
        setup.suitable = False
        setup.skip_reason = "No live option prices (chain source is OI-only — wait for market open)"
        return setup
        
    # Guard: ensure ATM options have live prices (not purely synthetic fallbacks)
    atm_row = chain[chain["strike"] == atm]
    if not atm_row.empty:
        ce_syn = atm_row.iloc[0].get("ce_synthetic", False)
        pe_syn = atm_row.iloc[0].get("pe_synthetic", False)
        if ce_syn and pe_syn:
            setup.suitable = False
            setup.skip_reason = "ATM options lack live prices (fully synthetic). Chain data is stale or market closed."
            return setup

    wing = setup.wing_width
    if wing > 0 and strike_step > 0:
        wing = int(round(wing / strike_step)) * strike_step
        setup.wing_width = wing
    resolved_legs = []
    net_credit = 0.0
    max_loss = 0.0
    
    if setup.strategy in ("IRON_CONDOR", "IRON_CONDOR_WIDE"):
        short_put = _nearest(atm - wing, strikes, prefer_higher=False)
        long_put = _nearest(short_put - wing, strikes, prefer_higher=False)
        short_call = _nearest(atm + wing, strikes, prefer_higher=True)
        long_call = _nearest(short_call + wing, strikes, prefer_higher=True)
        
        sr = chain[chain["strike"] == short_put]
        lr = chain[chain["strike"] == long_put]
        scr = chain[chain["strike"] == short_call]
        lcr = chain[chain["strike"] == long_call]
        
        if any(r.empty for r in [sr, lr, scr, lcr]):
            setup.suitable = False
            return setup
        
        expiry = chain.iloc[0]["expiry"]
        put_credit = (sr.iloc[0]["pe_ltp"] - lr.iloc[0]["pe_ltp"]) * lot_size
        call_credit = (scr.iloc[0]["ce_ltp"] - lcr.iloc[0]["ce_ltp"]) * lot_size
        net_credit = put_credit + call_credit
        max_loss = wing * lot_size - net_credit
        
        resolved_legs = [
            ResolvedLeg("SELL", "PE", short_put, expiry, min_lots, lot_size, sr.iloc[0]["pe_ltp"]),
            ResolvedLeg("BUY", "PE", long_put, expiry, min_lots, lot_size, lr.iloc[0]["pe_ltp"]),
            ResolvedLeg("SELL", "CE", short_call, expiry, min_lots, lot_size, scr.iloc[0]["ce_ltp"]),
            ResolvedLeg("BUY", "CE", long_call, expiry, min_lots, lot_size, lcr.iloc[0]["ce_ltp"]),
        ]
        credit_per_lot = net_credit / lot_size if lot_size else 0.0
        setup.breakevens = [short_put - credit_per_lot, short_call + credit_per_lot]
        setup.short_strikes = [short_put, short_call]
        
    elif setup.strategy in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD"):
        if setup.strategy == "BULL_PUT_SPREAD":
            short_s = _nearest(atm - wing * 0.5, strikes, prefer_higher=False)
            long_s = _nearest(short_s - wing, strikes, prefer_higher=False)
            leg_type = "PE"
        else:
            short_s = _nearest(atm + wing * 0.5, strikes, prefer_higher=True)
            long_s = _nearest(short_s + wing, strikes, prefer_higher=True)
            leg_type = "CE"
        
        sr = chain[chain["strike"] == short_s]
        lr = chain[chain["strike"] == long_s]
        if sr.empty or lr.empty:
            setup.suitable = False
            return setup
        
        expiry = chain.iloc[0]["expiry"]
        short_prem = sr.iloc[0][f"{leg_type.lower()}_ltp"]
        long_prem = lr.iloc[0][f"{leg_type.lower()}_ltp"]
        net_credit = (short_prem - long_prem) * lot_size
        max_loss = wing * lot_size - net_credit
        
        resolved_legs = [
            ResolvedLeg("SELL", leg_type, short_s, expiry, min_lots, lot_size, short_prem),
            ResolvedLeg("BUY", leg_type, long_s, expiry, min_lots, lot_size, long_prem),
        ]
        credit_per_lot = net_credit / lot_size if lot_size else 0.0
        setup.breakevens = [short_s - credit_per_lot if leg_type == "PE" else short_s + credit_per_lot]
        setup.short_strikes = [short_s]
    
    elif setup.strategy in ("NAKED_PUT_SELL", "NAKED_CALL_SELL"):
        leg_type = "PE" if setup.strategy == "NAKED_PUT_SELL" else "CE"
        # Sell OTM delta 20-30 or 1 strike OTM
        short_s = _nearest(atm - 100 if leg_type == "PE" else atm + 100, strikes, 
                          prefer_higher=(leg_type == "CE"))
        
        sr = chain[chain["strike"] == short_s]
        if sr.empty:
            setup.suitable = False
            return setup
            
        expiry = chain.iloc[0]["expiry"]
        prem = sr.iloc[0][f"{leg_type.lower()}_ltp"]
        
        resolved_legs = [
            ResolvedLeg("SELL", leg_type, short_s, expiry, min_lots, lot_size, prem),
        ]
        net_credit = prem * lot_size
        spot_for_risk = spot if spot > 0 else 25000.0
        max_loss = spot_for_risk * 0.20 * lot_size
        setup.breakevens = [short_s - prem if leg_type == "PE" else short_s + prem]
        setup.short_strikes = [short_s]
    
    setup.legs = resolved_legs
    setup.net_credit = max(0, net_credit)
    setup.max_loss_rupees = max(0, max_loss)
    return setup


def _nearest(target: float, strikes: list, prefer_higher: bool = True) -> float:
    if not strikes:
        return target
    below = [s for s in strikes if s <= target]
    above = [s for s in strikes if s >= target]
    if not below:
        return min(above)
    if not above:
        return max(below)
    lower, upper = max(below), min(above)
    if prefer_higher:
        return upper if abs(upper - target) <= abs(lower - target) else lower
    return lower if abs(lower - target) <= abs(upper - target) else upper


# Legacy compatibility
def pick_structure(regime, bias, iv_rank, vix) -> Optional[OptionStructure]:
    if iv_rank is None:
        high_iv = vix > 16
        low_iv = vix < 14
    else:
        high_iv = iv_rank > 50
        low_iv = iv_rank < 30
        
    if regime == "TREND_UP" and bias in ("LONG", "NEUTRAL"):
        return OptionStructure(name="BULL_CALL_SPREAD",
            legs=[OptionLeg("BUY", "CE", "ATM", "WEEKLY", 1), OptionLeg("SELL", "CE", "ATM+2", "WEEKLY", 1)],
            entry_condition="TREND_UP & LONG", exit_rules={"pct_profit": 0.5, "pct_loss": 1.0},
            max_loss_formula=lambda d: d, structure_type="debit_spread")
    elif regime == "TREND_DOWN" and bias in ("SHORT", "NEUTRAL"):
        return OptionStructure(name="BEAR_PUT_SPREAD",
            legs=[OptionLeg("BUY", "PE", "ATM", "WEEKLY", 1), OptionLeg("SELL", "PE", "ATM-2", "WEEKLY", 1)],
            entry_condition="TREND_DOWN & SHORT", exit_rules={"pct_profit": 0.5, "pct_loss": 1.0},
            max_loss_formula=lambda d: d, structure_type="debit_spread")
    elif regime == "VOL_EXPANSION" or (regime.startswith("RANGE_") and low_iv):
        return OptionStructure(name="LONG_STRADDLE",
            legs=[OptionLeg("BUY", "CE", "ATM", "WEEKLY", 1), OptionLeg("BUY", "PE", "ATM", "WEEKLY", 1)],
            entry_condition="VOL_EXPANSION or LOW_IV", exit_rules={"pct_profit": 0.4, "pct_loss": 0.3},
            max_loss_formula=lambda d: d, structure_type="debit_spread")
    elif regime in ("RANGE_LOW_VOL", "RANGE_HIGH_VOL") and high_iv:
        return OptionStructure(name="SHORT_STRANGLE_WINGED",
            legs=[OptionLeg("SELL", "CE", "DELTA_20", "WEEKLY", 1), OptionLeg("SELL", "PE", "DELTA_20", "WEEKLY", 1),
                  OptionLeg("BUY", "CE", "DELTA_10", "WEEKLY", 1), OptionLeg("BUY", "PE", "DELTA_10", "WEEKLY", 1)],
            entry_condition="RANGE & HIGH_IV", exit_rules={"pct_profit": 0.5, "pct_loss": 1.5},
            max_loss_formula=lambda c, w=0: w - c, structure_type="iron_condor")
    return None


def resolve_legs(structure: OptionStructure, chain: pd.DataFrame, spot: float,
                  lot_size: int, strike_step: int, num_lots: int = 1) -> list[ResolvedLeg]:
    if chain.empty:
        return []
    chain = chain.copy()
    chain["ce_ltp"] = pd.to_numeric(chain["ce_ltp"], errors="coerce").fillna(0.0)
    chain["pe_ltp"] = pd.to_numeric(chain["pe_ltp"], errors="coerce").fillna(0.0)
    strikes = sorted(chain["strike"].tolist())
    atm = atm_strike(spot, strikes)
    if not atm:
        return []
    atm_idx = strikes.index(atm)
    resolved = []
    for leg in structure.legs:
        target = atm
        if leg.strike_rule == "ATM+2":
            idx = min(len(strikes) - 1, atm_idx + 2)
            target = strikes[idx]
        elif leg.strike_rule == "ATM-2":
            idx = max(0, atm_idx - 2)
            target = strikes[idx]
        elif leg.strike_rule.startswith("DELTA_"):
            d_val = float(leg.strike_rule.split("_")[1]) / 100.0
            target = delta_strike(chain, d_val, leg.option_type) or atm
        row = chain[chain["strike"] == target]
        if row.empty:
            return []
        premium = row.iloc[0].get(f"{leg.option_type.lower()}_ltp", 0)
        resolved.append(ResolvedLeg(leg.side, leg.option_type, target, row.iloc[0].get("expiry"), leg.qty_ratio * num_lots, lot_size, premium))
    return resolved
