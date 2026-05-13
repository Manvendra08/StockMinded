    """
    Generate actionable NIFTY option-selling setups from signal data.
    
    Returns list of setups with full metadata for potential entry.
    """
    from signals.option_strategy import pick_nifty_strategy, resolve_nifty_structure
    from signals.options import chain_snapshot, atm_strike, is_within_entry_window
    
    if cfg is None:
        from config.loader import load_config
        cfg = load_config()
    
    setups = []
    nifty_cfg = cfg.get("nifty_options", {})
    if not nifty_cfg.get("enabled", False):
        return setups
    
    regime = data.get("regime", {})
    flows = data.get("flows", {})
    
    regime_name = regime.get("name", "")
    bias = flows.get("bias", "NEUTRAL")
    vix = regime.get("vix", 15)
    vix_change = regime.get("vix_5d_change_pct", 0)
    pcr = flows.get("pcr_oi")
    
    # Get NIFTY spot
    nifty_data = data.get("nifty", {})
    spot = nifty_data.get("close", 0)
    
    if spot <= 0:
        return setups
    
    # Check entry window
    in_window, window_reason = is_within_entry_window(cfg)
    
    # Pick strategy
    setup = pick_nifty_strategy(regime_name, bias, vix, vix_change, pcr, cfg)
    
    if setup is None:
        setups.append({
            "symbol": "NIFTY",
            "suitable": False,
            "skip_reason": "No strategy for current regime/bias",
            "regime": regime_name,
            "bias": bias,
            "vix": vix
        })
        return setups
    
    # Get option chain
    try:
        chain = chain_snapshot("NIFTY")
    except Exception:
        setups.append({
            "symbol": "NIFTY",
            "suitable": False,
            "skip_reason": "Could not fetch NIFTY option chain",
            "regime": regime_name,
            "bias": bias,
            "vix": vix
        })
        return setups
    
    if chain.empty:
        setups.append({
            "symbol": "NIFTY",
            "suitable": False,
            "skip_reason": "Empty option chain",
            "regime": regime_name,
            "bias": bias,
            "vix": vix
        })
        return setups
    
    # Resolve strikes
    lot_size = nifty_cfg.get("lot_size", {}).get("NIFTY", 75)
    strike_step = nifty_cfg.get("strike_step", {}).get("NIFTY", 50)
    
    setup = resolve_nifty_structure(setup, chain, spot, lot_size, strike_step)
    
    # Add entry window status
    setup.entry_window_ok = in_window
    
    # Convert to dict for JSON serialization
    setup_dict = {
        "symbol": setup.symbol,
        "mode": setup.mode,
        "strategy": setup.strategy,
        "regime": setup.regime,
        "bias": setup.bias,
        "vix": setup.vix,
        "vix_change_pct": setup.vix_change_pct,
        "pcr": setup.pcr,
        "entry_reason": setup.entry_reason,
        "entry_window_ok": setup.entry_window_ok,
        "entry_window_reason": window_reason,
        "suitable": setup.suitable and in_window,
        "skip_reason": setup.skip_reason if not setup.suitable else ("" if in_window else window_reason),
        "net_credit": setup.net_credit,
        "max_loss_rupees": setup.max_loss_rupees,
        "risk_pct": setup.risk_pct,
        "breakevens": setup.breakevens,
        "short_strikes": setup.short_strikes,
        "wing_width": setup.wing_width,
        "exit_rules": setup.exit_rules,
        "legs": [
            {
                "side": l.side,
                "type": l.type,
                "strike": l.strike,
                "expiry": l.expiry,
                "qty": l.lots * l.lot_size,
                "premium": l.premium
            } for l in setup.legs
        ] if setup.legs else []
