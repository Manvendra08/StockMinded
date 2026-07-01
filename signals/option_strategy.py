from dataclasses import dataclass, field
from typing import Callable, Literal, Optional

import pandas as pd

from signals.options import _next_expiry, atm_strike, delta_strike


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


def _resolve_cfg_key(cfg: dict, symbol: str = "NIFTY") -> tuple[str, dict]:
    """Return (cfg_key, section_dict) for the given symbol."""
    cfg_key = "banknifty_options" if symbol == "BANKNIFTY" else "nifty_options"
    return cfg_key, cfg.get(cfg_key, {})


def _build_setup_for_symbol(
    builder_name: str,
    symbol: str,
    regime: str,
    bias: str,
    vix: float,
    pcr: Optional[float],
    cfg: dict,
    mode: str,
    vol_exp_blocked: bool = False,
):
    """Dispatch to the right builder with the symbol-specific config."""
    cfg_key, sym_cfg = _resolve_cfg_key(cfg, symbol)
    wing_width_key = "iron_condor_wing_width"
    spread_width_key = "spread_width"
    wing = sym_cfg.get(wing_width_key, 300 if symbol == "NIFTY" else 600)
    width = sym_cfg.get(spread_width_key, 200 if symbol == "NIFTY" else 400)

    if builder_name == "NAKED_SELL":
        direction = "LONG" if bias in ("LONG", "BULL_FLOW") else "SHORT"
        strategy = "NAKED_PUT_SELL" if direction == "LONG" else "NAKED_CALL_SELL"
        return NiftyOptionSetup(
            symbol=symbol,
            mode=mode,
            strategy=strategy,
            regime=regime,
            bias=bias,
            vix=vix,
            pcr=pcr,
            wing_width=0,
            entry_reason=f"Directional bias {bias} -> Naked {strategy}",
            suitable=True,
            exit_rules={
                "profit_take_pct": sym_cfg.get("profit_take_pct", 0.50),
                "stop_loss_mult": sym_cfg.get("stop_loss_mult", 1.25),
                "vix_spike_exit_pct": sym_cfg.get("vix_spike_exit_pct", 10.0),
                "eod_exit": mode == "intraday",
                "expiry_exit": mode == "positional",
            },
        )
    elif builder_name == "IRON_CONDOR":
        return NiftyOptionSetup(
            symbol=symbol,
            mode=mode,
            strategy="IRON_CONDOR",
            regime=regime,
            bias=bias,
            vix=vix,
            pcr=pcr,
            wing_width=wing,
            entry_reason=f"RANGE_LOW_VOL Iron Condor, wing={wing}",
            vol_expansion_blocked=vol_exp_blocked,
            suitable=True,
            exit_rules={
                "profit_take_pct": sym_cfg.get("profit_take_pct", 0.50),
                "stop_loss_mult": sym_cfg.get("stop_loss_mult", 1.25),
                "vix_spike_exit_pct": sym_cfg.get("vix_spike_exit_pct", 10.0),
                "eod_exit": mode == "intraday",
                "expiry_exit": mode == "positional",
            },
        )
    elif builder_name == "IRON_CONDOR_WIDE":
        return NiftyOptionSetup(
            symbol=symbol,
            mode=mode,
            strategy="IRON_CONDOR_WIDE",
            regime=regime,
            bias=bias,
            vix=vix,
            pcr=pcr,
            wing_width=wing * 1.5,
            entry_reason=f"RANGE_HIGH_VOL wider IC, wing={wing * 1.5}",
            vol_expansion_blocked=vol_exp_blocked,
            suitable=True,
            exit_rules={
                "profit_take_pct": sym_cfg.get("profit_take_pct", 0.50),
                "stop_loss_mult": sym_cfg.get("stop_loss_mult", 1.25),
                "vix_spike_exit_pct": sym_cfg.get("vix_spike_exit_pct", 10.0),
                "eod_exit": mode == "intraday",
                "expiry_exit": mode == "positional",
            },
        )
    elif builder_name == "BULL_PUT_SPREAD":
        if vol_exp_blocked:
            return NiftyOptionSetup(
                symbol=symbol,
                regime=regime,
                bias=bias,
                vix=vix,
                suitable=False,
                skip_reason="Bull Put blocked: VIX expanding",
            )
        return NiftyOptionSetup(
            symbol=symbol,
            mode=mode,
            strategy="BULL_PUT_SPREAD",
            regime=regime,
            bias=bias,
            vix=vix,
            pcr=pcr,
            wing_width=width,
            entry_reason=f"TREND_UP Bull Put Spread, width={width}",
            vol_expansion_blocked=vol_exp_blocked,
            suitable=True,
            exit_rules={
                "profit_take_pct": sym_cfg.get("profit_take_pct", 0.50),
                "stop_loss_mult": sym_cfg.get("stop_loss_mult", 1.25),
                "vix_spike_exit_pct": sym_cfg.get("vix_spike_exit_pct", 10.0),
                "eod_exit": mode == "intraday",
                "expiry_exit": mode == "positional",
            },
        )
    elif builder_name == "BEAR_CALL_SPREAD":
        if vol_exp_blocked:
            return NiftyOptionSetup(
                symbol=symbol,
                regime=regime,
                bias=bias,
                vix=vix,
                suitable=False,
                skip_reason="Bear Call blocked: VIX expanding",
            )
        return NiftyOptionSetup(
            symbol=symbol,
            mode=mode,
            strategy="BEAR_CALL_SPREAD",
            regime=regime,
            bias=bias,
            vix=vix,
            pcr=pcr,
            wing_width=width,
            entry_reason=f"TREND_DOWN Bear Call Spread, width={width}",
            vol_expansion_blocked=vol_exp_blocked,
            suitable=True,
            exit_rules={
                "profit_take_pct": sym_cfg.get("profit_take_pct", 0.50),
                "stop_loss_mult": sym_cfg.get("stop_loss_mult", 1.25),
                "vix_spike_exit_pct": sym_cfg.get("vix_spike_exit_pct", 10.0),
                "eod_exit": mode == "intraday",
                "expiry_exit": mode == "positional",
            },
        )
    return None


def _pick_strategy(
    data: dict,
    regime: str,
    bias: str,
    vix: float,
    vix_change_pct: float,
    pcr: Optional[float] = None,
    cfg: dict = None,
    symbol: str = "NIFTY",
) -> Optional[NiftyOptionSetup]:
    """Select option-selling strategy for given symbol based on regime/bias/VIX."""
    if cfg is None:
        from config.loader import load_config

        cfg = load_config()

    cfg_key, sym_cfg = _resolve_cfg_key(cfg, symbol)
    if not sym_cfg.get("enabled", False):
        return None

    avoid_vol_exp = sym_cfg.get("avoid_vol_expansion", True)
    vol_exp_thresh = sym_cfg.get("vol_expansion_threshold", 5.0)
    vol_expansion_blocked = avoid_vol_exp and vix_change_pct > vol_exp_thresh

    if regime == "VOL_EXPANSION":
        return NiftyOptionSetup(
            symbol=symbol,
            regime=regime,
            vix=vix,
            vix_change_pct=vix_change_pct,
            suitable=False,
            skip_reason="VOL_EXPANSION",
        )

    if vol_expansion_blocked and regime in ("RANGE_LOW_VOL", "RANGE_HIGH_VOL"):
        return NiftyOptionSetup(
            symbol=symbol,
            regime=regime,
            vix=vix,
            vix_change_pct=vix_change_pct,
            suitable=False,
            skip_reason=f"VIX expanding {vix_change_pct:.1f}% > {vol_exp_thresh}%",
        )

    mode = sym_cfg.get("mode", "positional")
    verdict = data.get("verdict", {})

    if symbol == "NIFTY":
        symbol_v = verdict.get("nifty", {})
        v_action = symbol_v.get("action", "WAIT")
        if v_action == "NAKED_OPTION_SELL":
            return _build_setup_for_symbol(
                "NAKED_SELL", symbol, regime, bias, vix, pcr, cfg, mode
            )

    if regime in ("RANGE_LOW_VOL", "VOL_CONTRACTION"):
        return _build_setup_for_symbol(
            "IRON_CONDOR",
            symbol,
            regime,
            bias,
            vix,
            pcr,
            cfg,
            mode,
            vol_expansion_blocked,
        )
    elif regime == "RANGE_HIGH_VOL" and not vol_expansion_blocked:
        return _build_setup_for_symbol(
            "IRON_CONDOR_WIDE", symbol, regime, bias, vix, pcr, cfg, mode
        )
    elif regime == "TREND_UP" and bias in ("LONG", "BULL_FLOW"):
        return _build_setup_for_symbol(
            "BULL_PUT_SPREAD",
            symbol,
            regime,
            bias,
            vix,
            pcr,
            cfg,
            mode,
            vol_expansion_blocked,
        )
    elif regime == "TREND_DOWN" and bias in ("SHORT", "BEAR_FLOW"):
        return _build_setup_for_symbol(
            "BEAR_CALL_SPREAD",
            symbol,
            regime,
            bias,
            vix,
            pcr,
            cfg,
            mode,
            vol_expansion_blocked,
        )
    return None


def pick_nifty_strategy(
    data: dict,
    regime: str,
    bias: str,
    vix: float,
    vix_change_pct: float,
    pcr: Optional[float] = None,
    cfg: dict = None,
) -> Optional[NiftyOptionSetup]:
    """Select NIFTY option-selling strategy based on regime/bias/VIX."""
    return _pick_strategy(
        data, regime, bias, vix, vix_change_pct, pcr, cfg, symbol="NIFTY"
    )


def pick_banknifty_strategy(
    data: dict,
    regime: str,
    bias: str,
    vix: float,
    vix_change_pct: float,
    pcr: Optional[float] = None,
    cfg: dict = None,
) -> Optional[NiftyOptionSetup]:
    """Select BANKNIFTY option-selling strategy based on regime/bias/VIX."""
    return _pick_strategy(
        data, regime, bias, vix, vix_change_pct, pcr, cfg, symbol="BANKNIFTY"
    )


def _build_naked_selling(
    regime: str, bias: str, vix: float, pcr: Optional[float], cfg: dict, mode: str
) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    direction = "LONG" if bias in ("LONG", "BULL_FLOW") else "SHORT"
    strategy = "NAKED_PUT_SELL" if direction == "LONG" else "NAKED_CALL_SELL"
    return NiftyOptionSetup(
        symbol="NIFTY",
        mode=mode,
        strategy=strategy,
        regime=regime,
        bias=bias,
        vix=vix,
        pcr=pcr,
        wing_width=0,
        entry_reason=f"Directional bias {bias} -> Naked {strategy}",
        suitable=True,
        exit_rules={
            "profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
            "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
            "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
            "eod_exit": mode == "intraday",
            "expiry_exit": mode == "positional",
        },
    )


def _build_iron_condor(
    regime: str,
    bias: str,
    vix: float,
    pcr: Optional[float],
    cfg: dict,
    mode: str,
    vol_exp_blocked: bool,
) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    wing = nifty_cfg.get("iron_condor_wing_width", 300)
    return NiftyOptionSetup(
        symbol="NIFTY",
        mode=mode,
        strategy="IRON_CONDOR",
        regime=regime,
        bias=bias,
        vix=vix,
        pcr=pcr,
        wing_width=wing,
        entry_reason=f"RANGE_LOW_VOL Iron Condor, wing={wing}",
        vol_expansion_blocked=vol_exp_blocked,
        suitable=True,
        exit_rules={
            "profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
            "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
            "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
            "eod_exit": mode == "intraday",
            "expiry_exit": mode == "positional",
        },
    )


def _build_wider_iron_condor(
    regime: str,
    bias: str,
    vix: float,
    pcr: Optional[float],
    cfg: dict,
    mode: str,
    vol_exp_blocked: bool = False,
) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    wing = nifty_cfg.get("iron_condor_wing_width", 300) * 1.5
    return NiftyOptionSetup(
        symbol="NIFTY",
        mode=mode,
        strategy="IRON_CONDOR_WIDE",
        regime=regime,
        bias=bias,
        vix=vix,
        pcr=pcr,
        wing_width=wing,
        entry_reason=f"RANGE_HIGH_VOL wider IC, wing={wing}",
        vol_expansion_blocked=vol_exp_blocked,
        suitable=True,
        exit_rules={
            "profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
            "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
            "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
            "eod_exit": mode == "intraday",
            "expiry_exit": mode == "positional",
        },
    )


def _build_bull_put_spread(
    regime: str,
    bias: str,
    vix: float,
    pcr: Optional[float],
    cfg: dict,
    mode: str,
    vol_blocked: bool,
) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    width = nifty_cfg.get("spread_width", 200)
    if vol_blocked:
        return NiftyOptionSetup(
            symbol="NIFTY",
            regime=regime,
            bias=bias,
            vix=vix,
            suitable=False,
            skip_reason="Bull Put blocked: VIX expanding",
        )
    return NiftyOptionSetup(
        symbol="NIFTY",
        mode=mode,
        strategy="BULL_PUT_SPREAD",
        regime=regime,
        bias=bias,
        vix=vix,
        pcr=pcr,
        wing_width=width,
        entry_reason=f"TREND_UP Bull Put Spread, width={width}",
        vol_expansion_blocked=vol_blocked,
        suitable=True,
        exit_rules={
            "profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
            "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
            "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
            "eod_exit": mode == "intraday",
            "expiry_exit": mode == "positional",
        },
    )


def _build_bear_call_spread(
    regime: str,
    bias: str,
    vix: float,
    pcr: Optional[float],
    cfg: dict,
    mode: str,
    vol_blocked: bool,
) -> NiftyOptionSetup:
    nifty_cfg = cfg.get("nifty_options", {})
    width = nifty_cfg.get("spread_width", 200)
    if vol_blocked:
        return NiftyOptionSetup(
            symbol="NIFTY",
            regime=regime,
            bias=bias,
            vix=vix,
            suitable=False,
            skip_reason="Bear Call blocked: VIX expanding",
        )
    return NiftyOptionSetup(
        symbol="NIFTY",
        mode=mode,
        strategy="BEAR_CALL_SPREAD",
        regime=regime,
        bias=bias,
        vix=vix,
        pcr=pcr,
        wing_width=width,
        entry_reason=f"TREND_DOWN Bear Call Spread, width={width}",
        vol_expansion_blocked=vol_blocked,
        suitable=True,
        exit_rules={
            "profit_take_pct": nifty_cfg.get("profit_take_pct", 0.50),
            "stop_loss_mult": nifty_cfg.get("stop_loss_mult", 1.25),
            "vix_spike_exit_pct": nifty_cfg.get("vix_spike_exit_pct", 10.0),
            "eod_exit": mode == "intraday",
            "expiry_exit": mode == "positional",
        },
    )


def resolve_nifty_structure(
    setup: NiftyOptionSetup,
    chain: pd.DataFrame,
    spot: float,
    lot_size: int,
    strike_step: int,
    cfg: dict = None,
) -> NiftyOptionSetup:
    """Resolve NIFTY option structure with live premiums."""
    return _resolve_structure(
        setup, chain, spot, lot_size, strike_step, cfg, symbol="NIFTY"
    )


def resolve_banknifty_structure(
    setup: NiftyOptionSetup,
    chain: pd.DataFrame,
    spot: float,
    lot_size: int,
    strike_step: int,
    cfg: dict = None,
) -> NiftyOptionSetup:
    """Resolve BANKNIFTY option structure with live premiums."""
    return _resolve_structure(
        setup, chain, spot, lot_size, strike_step, cfg, symbol="BANKNIFTY"
    )


def _resolve_structure(
    setup: NiftyOptionSetup,
    chain: pd.DataFrame,
    spot: float,
    lot_size: int,
    strike_step: int,
    cfg: dict = None,
    symbol: str = "NIFTY",
) -> NiftyOptionSetup:
    if chain.empty or not setup.suitable:
        return setup

    chain = chain.copy()
    chain["ce_ltp"] = pd.to_numeric(chain["ce_ltp"], errors="coerce").fillna(0.0)
    chain["pe_ltp"] = pd.to_numeric(chain["pe_ltp"], errors="coerce").fillna(0.0)

    setup.spot = spot
    if cfg is None:
        from config.loader import load_config

        cfg = load_config()
    cfg_key = "banknifty_options" if symbol == "BANKNIFTY" else "nifty_options"
    min_lots = max(1, cfg.get(cfg_key, {}).get("min_lots_per_leg", 1))

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
        setup.skip_reason = (
            "No live option prices (chain source is OI-only — wait for market open)"
        )
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
            ResolvedLeg(
                "SELL",
                "PE",
                short_put,
                expiry,
                min_lots,
                lot_size,
                sr.iloc[0]["pe_ltp"],
            ),
            ResolvedLeg(
                "BUY", "PE", long_put, expiry, min_lots, lot_size, lr.iloc[0]["pe_ltp"]
            ),
            ResolvedLeg(
                "SELL",
                "CE",
                short_call,
                expiry,
                min_lots,
                lot_size,
                scr.iloc[0]["ce_ltp"],
            ),
            ResolvedLeg(
                "BUY",
                "CE",
                long_call,
                expiry,
                min_lots,
                lot_size,
                lcr.iloc[0]["ce_ltp"],
            ),
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
            ResolvedLeg(
                "SELL", leg_type, short_s, expiry, min_lots, lot_size, short_prem
            ),
            ResolvedLeg("BUY", leg_type, long_s, expiry, min_lots, lot_size, long_prem),
        ]
        credit_per_lot = net_credit / lot_size if lot_size else 0.0
        setup.breakevens = [
            short_s - credit_per_lot if leg_type == "PE" else short_s + credit_per_lot
        ]
        setup.short_strikes = [short_s]

    elif setup.strategy in ("NAKED_PUT_SELL", "NAKED_CALL_SELL"):
        leg_type = "PE" if setup.strategy == "NAKED_PUT_SELL" else "CE"
        # Sell OTM delta 20-30 or 1 strike OTM
        short_s = _nearest(
            atm - 100 if leg_type == "PE" else atm + 100,
            strikes,
            prefer_higher=(leg_type == "CE"),
        )

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

    # ── Premium Filter: enforce min & max bounds on short-leg premium ──
    #
    # Two-directional walking:
    #   max_short_premium (cap)  — if premium >= cap, walk FURTHER OTM to reduce risk
    #   min_short_premium (floor) — if premium < floor, walk TOWARDS ATM to get adequate credit
    #
    # If even ATM premium  < min_short_premium → reject (market too quiet).
    # If furthest OTM premium >= max_short_premium → reject (can't reduce risk enough).
    # Config conflict (min > max) → reject.
    sym_cfg = cfg.get(cfg_key, {})
    max_prem = float(sym_cfg.get("max_short_premium", 0.0))
    min_prem = float(sym_cfg.get("min_short_premium", 5.0))
    if min_prem > max_prem > 0:
        setup.suitable = False
        setup.skip_reason = f"Config conflict: min_short_premium ₹{min_prem:.0f} > max_short_premium ₹{max_prem:.0f}"
        return setup
    if resolved_legs and (max_prem > 0 or min_prem > 0):
        for i, leg in enumerate(resolved_legs):
            if leg.side != "SELL":
                continue
            current_strike = leg.strike
            current_prem = leg.premium

            # Phase 1: if premium is TOO LOW, walk TOWARDS ATM (more premium,
            # still OTM). If already AT or past ATM — reject (would sell ITM).
            # If even ATM premium < min_prem — reject (market too quiet).
            if min_prem > 0 and current_prem < min_prem:
                # Check if we're already ATM or ITM — can't get more OTM premium
                if (leg.type == "PE" and current_strike >= atm) or (
                    leg.type == "CE" and current_strike <= atm
                ):
                    # At ATM: check if ATM premium meets minimum
                    row = chain[chain["strike"] == atm]
                    if not row.empty:
                        atm_prem = float(row.iloc[0][f"{leg.type.lower()}_ltp"])
                        if atm_prem < min_prem:
                            setup.suitable = False
                            setup.skip_reason = f"{leg.type} ATM premium ₹{atm_prem:.1f} < min ₹{min_prem:.0f} — market too quiet"
                            return setup
                    # ATM is adequate but current position was already ATM/ITM
                    # This shouldn't happen (prem at ATM >= min > current_prem < min)
                    # but if it does, just accept it
                    pass
                else:
                    # OTM — walk towards ATM to get more premium
                    walk_count = 0
                    while current_prem < min_prem and walk_count < 5:
                        if leg.type == "PE":
                            # OTM PE: strike < ATM, walk UP towards ATM
                            next_strike = _nearest(
                                current_strike + strike_step,
                                strikes,
                                prefer_higher=True,
                            )
                        else:
                            # OTM CE: strike > ATM, walk DOWN towards ATM
                            next_strike = _nearest(
                                current_strike - strike_step,
                                strikes,
                                prefer_higher=False,
                            )
                        if next_strike == current_strike:
                            break
                        # Stop walking once we hit ATM
                        if leg.type == "PE" and next_strike >= atm:
                            next_strike = atm
                        elif leg.type == "CE" and next_strike <= atm:
                            next_strike = atm
                        current_strike = next_strike
                        row = chain[chain["strike"] == current_strike]
                        if row.empty:
                            break
                        current_prem = float(row.iloc[0][f"{leg.type.lower()}_ltp"])
                        walk_count += 1
                    if current_prem < min_prem:
                        setup.suitable = False
                        setup.skip_reason = f"{leg.type} short premium ₹{current_prem:.1f} < min ₹{min_prem:.0f} at ATM — market too quiet"
                        return setup

            # Phase 2: if premium is TOO HIGH, walk further OTM to reduce risk
            if max_prem > 0 and current_prem >= max_prem:
                walk_count = 0
                while current_prem >= max_prem and walk_count < 5:
                    if leg.type == "PE":
                        next_strike = _nearest(
                            current_strike - strike_step, strikes, prefer_higher=False
                        )
                    else:  # CE
                        next_strike = _nearest(
                            current_strike + strike_step, strikes, prefer_higher=True
                        )
                    if next_strike == current_strike:
                        break
                    current_strike = next_strike
                    row = chain[chain["strike"] == current_strike]
                    if row.empty:
                        break
                    current_prem = float(row.iloc[0][f"{leg.type.lower()}_ltp"])
                    walk_count += 1
                if current_prem >= max_prem:
                    setup.suitable = False
                    setup.skip_reason = f"{leg.type} short premium ₹{current_prem:.1f} >= max ₹{max_prem:.0f} at furthest OTM strike"
                    return setup

            # Update leg with new strike/premium
            leg.strike = current_strike
            leg.premium = current_prem
            # Also update related long leg(s) if this is a structured strategy
            if setup.strategy in ("IRON_CONDOR", "IRON_CONDOR_WIDE") and wing > 0:
                if leg.type == "PE":
                    long_target = _nearest(
                        current_strike - wing, strikes, prefer_higher=False
                    )
                else:
                    long_target = _nearest(
                        current_strike + wing, strikes, prefer_higher=True
                    )
                for j, ll in enumerate(resolved_legs):
                    if ll.side == "BUY" and ll.type == leg.type:
                        lr_row = chain[chain["strike"] == long_target]
                        if not lr_row.empty:
                            resolved_legs[j].strike = long_target
                            resolved_legs[j].premium = float(
                                lr_row.iloc[0][f"{leg.type.lower()}_ltp"]
                            )
                        break

        # Recalculate net_credit, max_loss, and breakevens after premium adjustments
        if resolved_legs:
            net_credit = 0.0
            for leg in resolved_legs:
                sign = 1 if leg.side == "SELL" else -1
                net_credit += sign * leg.premium * leg.lots * leg.lot_size
            net_credit = max(0, net_credit)
            setup.net_credit = net_credit

            # Recalculate max_loss from adjusted strikes
            short_legs = [l for l in resolved_legs if l.side == "SELL"]
            long_legs = [l for l in resolved_legs if l.side == "BUY"]
            if (
                setup.strategy in ("IRON_CONDOR", "IRON_CONDOR_WIDE")
                and len(short_legs) >= 2
            ):
                # IC: wing distance × lot_size − net_credit (per side, take max)
                put_short = [l for l in short_legs if l.type == "PE"]
                put_long = [l for l in long_legs if l.type == "PE"]
                call_short = [l for l in short_legs if l.type == "CE"]
                call_long = [l for l in long_legs if l.type == "CE"]
                max_loss = 0.0
                if put_short and put_long:
                    put_wing = abs(put_short[0].strike - put_long[0].strike)
                    put_risk = (
                        put_wing * lot_size * min_lots
                        - (put_short[0].premium - put_long[0].premium)
                        * lot_size
                        * min_lots
                    )
                    max_loss = max(max_loss, put_risk)
                if call_short and call_long:
                    call_wing = abs(call_short[0].strike - call_long[0].strike)
                    call_risk = (
                        call_wing * lot_size * min_lots
                        - (call_short[0].premium - call_long[0].premium)
                        * lot_size
                        * min_lots
                    )
                    max_loss = max(max_loss, call_risk)
            elif (
                setup.strategy in ("BULL_PUT_SPREAD", "BEAR_CALL_SPREAD")
                and short_legs
                and long_legs
            ):
                spread_wing = abs(short_legs[0].strike - long_legs[0].strike)
                max_loss = spread_wing * lot_size * min_lots - net_credit
            elif setup.strategy in ("NAKED_PUT_SELL", "NAKED_CALL_SELL"):
                # C3 FIX: Naked short max loss uses 50% of spot (was 20%)
                # and enforces a minimum floor to avoid underestimating risk.
                spot_for_risk = spot if spot > 0 else 25000.0
                naked_loss_pct = float(
                    sym_cfg.get("naked_loss_pct", 0.50)
                )  # C3 FIX: 0.20 → 0.50
                naked_loss_cap = float(
                    sym_cfg.get("naked_loss_cap", 250_000.0)
                )
                base_estimate = spot_for_risk * naked_loss_pct * lot_size * min_lots
                floor_total = naked_loss_cap * min_lots
                max_loss = max(base_estimate, floor_total)

            # Recalc breakevens if we have short strikes
            if short_legs:
                # C4 FIX: Sort short legs by strike to ensure correct ordering.
                # For Iron Condor, PE short (lower strike) must come before
                # CE short (higher strike) for correct breakeven assignment.
                short_legs_sorted = sorted(short_legs, key=lambda l: l.strike)
                credit_per_lot = (
                    net_credit / (short_legs_sorted[0].lot_size * short_legs_sorted[0].lots)
                    if short_legs_sorted[0].lot_size
                    else 0.0
                )
                if (
                    setup.strategy in ("IRON_CONDOR", "IRON_CONDOR_WIDE")
                    and len(short_legs_sorted) >= 2
                ):
                    setup.breakevens = [
                        short_legs_sorted[0].strike - credit_per_lot,
                        short_legs_sorted[1].strike + credit_per_lot,
                    ]
                    setup.short_strikes = [l.strike for l in short_legs_sorted]
                else:
                    leg_type = short_legs[0].type
                    s_strike = short_legs[0].strike
                    setup.breakevens = [
                        s_strike - credit_per_lot
                        if leg_type == "PE"
                        else s_strike + credit_per_lot
                    ]
                    setup.short_strikes = [s_strike]

    setup.legs = resolved_legs
    setup.net_credit = max(0, net_credit)
    setup.max_loss_rupees = max(0, max_loss)
    return setup


def _nearest(target: float, strikes: list, prefer_higher: bool = True) -> float:
    """Return the nearest strike to target.
    
    M4 FIX: When equidistant, respect prefer_higher parameter instead of
    always biasing toward higher strike. This prevents systematic placement
    bias in Iron Condors and spreads.
    """
    if not strikes:
        return target
    below = [s for s in strikes if s <= target]
    above = [s for s in strikes if s >= target]
    if not below:
        return min(above)
    if not above:
        return max(below)
    lower, upper = max(below), min(above)
    dist_lower = abs(lower - target)
    dist_upper = abs(upper - target)
    # M4 FIX: Strict < comparison — when equidistant, defer to prefer_higher
    if dist_upper < dist_lower:
        return upper
    elif dist_lower < dist_upper:
        return lower
    # Equidistant: use the preference flag to break the tie
    return upper if prefer_higher else lower


# Legacy compatibility
def pick_structure(regime, bias, iv_rank, vix) -> Optional[OptionStructure]:
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
                OptionLeg("SELL", "CE", "ATM+2", "WEEKLY", 1),
            ],
            entry_condition="TREND_UP & LONG",
            exit_rules={"pct_profit": 0.5, "pct_loss": 1.0},
            max_loss_formula=lambda d: d,
            structure_type="debit_spread",
        )
    elif regime == "TREND_DOWN" and bias in ("SHORT", "NEUTRAL"):
        return OptionStructure(
            name="BEAR_PUT_SPREAD",
            legs=[
                OptionLeg("BUY", "PE", "ATM", "WEEKLY", 1),
                OptionLeg("SELL", "PE", "ATM-2", "WEEKLY", 1),
            ],
            entry_condition="TREND_DOWN & SHORT",
            exit_rules={"pct_profit": 0.5, "pct_loss": 1.0},
            max_loss_formula=lambda d: d,
            structure_type="debit_spread",
        )
    elif regime == "VOL_EXPANSION" or (regime.startswith("RANGE_") and low_iv):
        return OptionStructure(
            name="LONG_STRADDLE",
            legs=[
                OptionLeg("BUY", "CE", "ATM", "WEEKLY", 1),
                OptionLeg("BUY", "PE", "ATM", "WEEKLY", 1),
            ],
            entry_condition="VOL_EXPANSION or LOW_IV",
            exit_rules={"pct_profit": 0.4, "pct_loss": 0.3},
            max_loss_formula=lambda d: d,
            structure_type="debit_spread",
        )
    elif regime in ("RANGE_LOW_VOL", "RANGE_HIGH_VOL") and high_iv:
        return OptionStructure(
            name="SHORT_STRANGLE_WINGED",
            legs=[
                OptionLeg("SELL", "CE", "DELTA_20", "WEEKLY", 1),
                OptionLeg("SELL", "PE", "DELTA_20", "WEEKLY", 1),
                OptionLeg("BUY", "CE", "DELTA_10", "WEEKLY", 1),
                OptionLeg("BUY", "PE", "DELTA_10", "WEEKLY", 1),
            ],
            entry_condition="RANGE & HIGH_IV",
            exit_rules={"pct_profit": 0.5, "pct_loss": 1.5},
            max_loss_formula=lambda c, w=0: w - c,
            structure_type="iron_condor",
        )
    return None


def resolve_legs(
    structure: OptionStructure,
    chain: pd.DataFrame,
    spot: float,
    lot_size: int,
    strike_step: int,
    num_lots: int = 1,
) -> list[ResolvedLeg]:
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
        resolved.append(
            ResolvedLeg(
                leg.side,
                leg.option_type,
                target,
                row.iloc[0].get("expiry"),
                leg.qty_ratio * num_lots,
                lot_size,
                premium,
            )
        )
    return resolved
