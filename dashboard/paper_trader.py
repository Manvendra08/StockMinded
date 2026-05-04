r"""Paper Trading Engine for StockMinded.

Manages simulated trades with entry/exit criteria, tracks P&L,
and generates end-of-day analysis with strategy corrections.

Data persisted to dashboard/paper_trades.json.
"""
from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime, date, time, timezone, timedelta
from pathlib import Path
from typing import Any

import yfinance as yf
import pandas as pd

# Import risk modules
from risk.guardrails import Guardrails
from risk.sizing import directional_size, SizeResult
from ops.journal import Journal

DATA_FILE = Path(__file__).parent / "paper_trades.json"
IST = timezone(timedelta(hours=5, minutes=30))
DEFAULT_SETTINGS = {
    "capital_per_trade": 500000.0,
    "sl_pct": 2.0,
    "tgt_pct": 4.0,
    "trail_sl": False,
    "min_confidence": "HIGH",
    "max_trades_per_day": 8,
    "max_new_entries_per_cycle": 5,
    "regime_filter": True,
    "telegram_bot_token": "",
    "telegram_chat_id": ""
}

LOCK = threading.Lock()

IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
EOD_WINDOW_START = time(15, 25)
EOD_WINDOW_END = time(15, 35)


def _now_ist() -> datetime:
    return datetime.now(IST)


def is_market_open(now: datetime | None = None) -> bool:
    """True only on Mon-Fri between 09:15 and 15:30 IST."""
    n = now or _now_ist()
    if n.tzinfo is None:
        n = n.replace(tzinfo=IST)
    if n.weekday() >= 5:  # 5=Sat, 6=Sun
        return False
    t = n.timetz().replace(tzinfo=None)
    return MARKET_OPEN <= t <= MARKET_CLOSE


def is_eod_window(now: datetime | None = None) -> bool:
    """Narrow 15:25-15:35 IST window for EOD flatten."""
    n = now or _now_ist()
    if n.tzinfo is None:
        n = n.replace(tzinfo=IST)
    if n.weekday() >= 5:
        return False
    t = n.timetz().replace(tzinfo=None)
    return EOD_WINDOW_START <= t <= EOD_WINDOW_END


# ═══════════════════════════════════════════════════════════════
#  DATA LAYER — persist to JSON
# ═══════════════════════════════════════════════════════════════

def _load_db() -> dict:
    """Load the full trade database."""
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "trades": [],
        "option_trades": [],
        "daily_summaries": [],
        "strategy_notes": [],
        "settings": DEFAULT_SETTINGS.copy(),
        "cumulative_pnl": 0.0,
        "version": 1,
    }


def get_settings() -> dict:
    """Return current paper trader settings."""
    db = _load_db()
    settings = DEFAULT_SETTINGS.copy()
    settings.update(db.get("settings", {}))
    return settings


def save_settings(new_settings: dict) -> dict:
    """Update and persist paper trader settings."""
    db = _load_db()
    if "settings" not in db:
        db["settings"] = DEFAULT_SETTINGS.copy()

    # Update only valid keys
    for k, v in new_settings.items():
        if k in DEFAULT_SETTINGS:
            # Type casting/validation
            if k in ("capital_per_trade", "sl_pct", "tgt_pct"):
                db["settings"][k] = float(v)
            elif k in ("max_trades_per_day", "max_new_entries_per_cycle"):
                db["settings"][k] = int(v)
            elif k in ("trail_sl", "regime_filter"):
                db["settings"][k] = bool(v)
            else:
                db["settings"][k] = str(v)

    _save_db(db)
    return db["settings"]



def _save_db(db: dict) -> None:
    """Persist trade database to disk."""
    with LOCK:
        DATA_FILE.write_text(json.dumps(db, indent=2, default=str), encoding="utf-8")


def _next_id(db: dict) -> int:
    """Get next trade ID."""
    t_ids = [t["id"] for t in db.get("trades", [])]
    o_ids = [t.get("id", 0) for t in db.get("option_trades", [])]
    all_ids = t_ids + o_ids
    if not all_ids:
        return 1
    return max(all_ids) + 1


# ═══════════════════════════════════════════════════════════════
#  PRICE HELPERS
# ═══════════════════════════════════════════════════════════════

def _get_ltp(symbol: str) -> float | None:
    """Fetch the latest trading price for a symbol."""
    try:
        from data import feed
        px = feed.ltp(symbol)
        if px:
            return px
    except Exception:
        pass
    try:
        yf_sym = f"{symbol}.NS" if not symbol.startswith("^") and "." not in symbol else symbol
        t = yf.Ticker(yf_sym)
        info = t.fast_info
        return round(float(info.last_price), 2) if info.last_price else None
    except Exception:
        return None


def _get_ltp_batch(symbols: list[str]) -> dict[str, float | None]:
    """Fetch LTPs for multiple symbols at once."""
    try:
        from data import feed
        quotes = feed.quote_batch(symbols)
        result = {s: (round(float(quotes[s]["ltp"]), 2) if quotes.get(s, {}).get("ltp") else None) for s in symbols}
        if any(v is not None for v in result.values()):
            missing = [s for s, v in result.items() if v is None]
            if not missing:
                return result
        else:
            missing = symbols
    except Exception:
        result = {}
        missing = symbols

    yf_syms = []
    for s in missing:
        yf_s = f"{s}.NS" if not s.startswith("^") and "." not in s else s
        yf_syms.append(yf_s)

    try:
        tickers = yf.Tickers(" ".join(yf_syms))
        for sym, yf_s in zip(missing, yf_syms):
            try:
                info = tickers.tickers[yf_s].fast_info
                result[sym] = round(float(info.last_price), 2) if info.last_price else None
            except Exception:
                result[sym] = None
    except Exception:
        for s in missing:
            result[s] = None
    return result


# ═══════════════════════════════════════════════════════════════
#  TRADE OPERATIONS
# ═══════════════════════════════════════════════════════════════

def enter_trade(alert: dict) -> dict:
    """Open a paper trade based on an alert signal.

    alert dict expected keys:
      symbol, direction (LONG/SHORT), instrument, entry (price),
      sl, target, evidence, confidence, type (INDEX/STOCK)
    """
    # Market-hours gate — applies to paper AND future live broker trades.
    if not is_market_open():
        return {"error": "Market closed (9:15-15:30 IST, Mon-Fri)"}

    db = _load_db()

    symbol = alert["symbol"]
    direction = alert.get("direction", "LONG")
    today_str = date.today().isoformat()

    # Hard dedup: one trade per symbol per day regardless of caller.
    for t in db["trades"]:
        if t.get("symbol") == symbol and t.get("entry_date") == today_str:
            return {"error": f"{symbol} already traded today (id={t['id']})"}

    # Resolve entry price
    entry_price = _get_ltp(symbol)
    if entry_price is None:
        return {"error": f"Could not fetch LTP for {symbol}"}

    settings = get_settings()
    capital_per_trade = settings["capital_per_trade"]
    sl_pct = settings["sl_pct"]
    tgt_pct = settings["tgt_pct"]

    # Manual entries use fixed allocation; auto entries may pass risk-sized qty.
    qty = int(capital_per_trade / entry_price) if entry_price > 0 else 0

    alert_stop = alert.get("stop")
    alert_t1 = alert.get("target1")
    if isinstance(alert_stop, (int, float)) and alert_stop > 0:
        sl_price = round(float(alert_stop), 2)
    elif direction == "LONG":
        sl_price = round(entry_price * (1 - sl_pct / 100), 2)
    else:
        sl_price = round(entry_price * (1 + sl_pct / 100), 2)

    if isinstance(alert_t1, (int, float)) and alert_t1 > 0:
        tgt_price = round(float(alert_t1), 2)
    elif direction == "LONG":
        tgt_price = round(entry_price * (1 + tgt_pct / 100), 2)
    else:
        tgt_price = round(entry_price * (1 - tgt_pct / 100), 2)

    alert_qty = int(alert.get("qty") or 0)
    if alert_qty > 0:
        qty = alert_qty
    if qty == 0:
        return {"error": f"Price too high for Rs {capital_per_trade:,.0f} allocation"}


    trade = {
        "id": _next_id(db),
        "symbol": symbol,
        "direction": direction,
        "type": alert.get("type", "STOCK"),
        "instrument": alert.get("instrument", f"{symbol} EQ"),
        "entry_price": entry_price,
        "qty": qty,
        "capital_deployed": round(entry_price * qty, 2),
        "sl_price": sl_price,
        "sl_pct": sl_pct,
        "tgt_price": tgt_price,
        "tgt_pct": tgt_pct,
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_date": date.today().isoformat(),
        "exit_price": None,
        "exit_time": None,
        "exit_reason": None,  # SL_HIT, TARGET_HIT, EOD_CLOSE, MANUAL
        "pnl": None,
        "pnl_pct": None,
        "status": "OPEN",  # OPEN, CLOSED
        "evidence": alert.get("evidence", []),
        "confidence": alert.get("confidence", "MEDIUM"),
        "notes": "",
        # Enhanced fields for tracking
        "planned_risk": alert.get("planned_risk", round((entry_price - sl_price) * qty if direction == "LONG" else (sl_price - entry_price) * qty, 2)),
        "risk_rupees": alert.get("planned_risk", round((entry_price - sl_price) * qty if direction == "LONG" else (sl_price - entry_price) * qty, 2)),
        "entry_rule": alert.get("entry_trigger", ""),
        "trail_rule": alert.get("trail_rule", ""),
        "source_regime": alert.get("source_regime", ""),
        "skip_reason": None,  # Only populated if trade was initially skipped then re-entered
    }

    db["trades"].append(trade)
    _save_db(db)
    return trade

def enter_option_structure(structure_name: str, resolved_legs: list, underlying: str, cfg: dict) -> dict:
    if not is_market_open():
        return {"error": "Market closed"}
        
    db = _load_db()
    if "option_trades" not in db: db["option_trades"] = []
    
    # Check concurrent structures limit
    open_ops = [t for t in db["option_trades"] if t["status"] == "OPEN"]
    max_ops = cfg.get("options", {}).get("max_concurrent_structures", 4)
    if len(open_ops) >= max_ops:
        return {"error": f"Max concurrent options structures ({max_ops}) reached"}
        
    # Build trade
    net_premium = sum((leg.premium * leg.lots * leg.lot_size) * (1 if leg.side == "SELL" else -1) for leg in resolved_legs)
    
    trade = {
        "id": _next_id(db),
        "symbol": underlying,
        "structure": structure_name,
        "legs": [
            {
                "side": l.side,
                "type": l.type,
                "strike": l.strike,
                "expiry": l.expiry,
                "qty": l.lots * l.lot_size,
                "entry_premium": l.premium,
                "exit_premium": None
            } for l in resolved_legs
        ],
        "net_premium": round(net_premium, 2),
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_date": date.today().isoformat(),
        "exit_time": None,
        "exit_reason": None,
        "pnl": None,
        "status": "OPEN"
    }
    
    db["option_trades"].append(trade)
    _save_db(db)
    return trade


def check_and_close_trades() -> list[dict]:
    """Check all OPEN trades against current prices.
    Close if SL hit, target hit, or EOD cutoff.
    Returns list of trades that were closed this call.
    """
    now_ist = _now_ist()
    market_open = is_market_open(now_ist)
    is_eod = is_eod_window(now_ist)

    # Outside market hours and outside the EOD flatten window — do nothing.
    # Prevents SL/TGT/EOD triggers at stale post-close prices.
    if not market_open and not is_eod:
        return []

    db = _load_db()
    open_trades = [t for t in db["trades"] if t["status"] == "OPEN"]
    if not open_trades:
        return []

    symbols = list(set(t["symbol"] for t in open_trades))
    prices = _get_ltp_batch(symbols)
    closed = []
    today_str = date.today().isoformat()
    now = datetime.now()

    for trade in open_trades:
        ltp = prices.get(trade["symbol"])
        if ltp is None:
            continue

        exit_reason = None
        direction = trade["direction"]

        if direction == "LONG":
            if ltp <= trade["sl_price"]:
                exit_reason = "SL_HIT"
            elif ltp >= trade["tgt_price"]:
                exit_reason = "TARGET_HIT"
            elif is_eod:
                exit_reason = "EOD_CLOSE"
        else:  # SHORT
            if ltp >= trade["sl_price"]:
                exit_reason = "SL_HIT"
            elif ltp <= trade["tgt_price"]:
                exit_reason = "TARGET_HIT"
            elif is_eod:
                exit_reason = "EOD_CLOSE"

        if exit_reason:
            trade["exit_price"] = ltp
            trade["exit_time"] = now.strftime("%Y-%m-%d %H:%M:%S")
            trade["exit_reason"] = exit_reason
            trade["status"] = "CLOSED"

            if direction == "LONG":
                pnl = (ltp - trade["entry_price"]) * trade["qty"]
                pnl_pct = 100 * (ltp - trade["entry_price"]) / trade["entry_price"]
            else:
                pnl = (trade["entry_price"] - ltp) * trade["qty"]
                pnl_pct = 100 * (trade["entry_price"] - ltp) / trade["entry_price"]

            trade["pnl"] = round(pnl, 2)
            trade["pnl_pct"] = round(pnl_pct, 2)
            closed.append(trade)

    if closed:
        _save_db(db)

    return closed

def _option_net_premium(legs: list[dict], price_map: dict[str, dict[str, float]]) -> float | None:
    """Compute current net premium given a price_map keyed by (strike, expiry, type).
    Returns None if any leg price is unavailable."""
    total = 0.0
    for leg in legs:
        key = (leg["strike"], leg["expiry"], leg["type"])
        price = price_map.get(key)
        if price is None:
            return None
        sign = 1 if leg["side"] == "SELL" else -1
        qty = leg.get("qty", leg.get("lots", 1) * leg.get("lot_size", 1))
        total += sign * price * qty
    return round(total, 2)


def _build_option_price_map(open_ops: list[dict]) -> dict[str, dict[str, float]]:
    """Fetch current option premiums for all legs in open option trades.
    Returns dict keyed by (strike, expiry, type) → current_ltp."""
    try:
        from signals.options import chain_snapshot
    except ImportError:
        return {}

    underlyings = list({t["symbol"] for t in open_ops})
    chains: dict[str, pd.DataFrame] = {}
    for sym in underlyings:
        try:
            chains[sym] = chain_snapshot(sym)
        except Exception:
            chains[sym] = pd.DataFrame()

    price_map = {}
    for trade in open_ops:
        sym = trade["symbol"]
        chain = chains.get(sym, pd.DataFrame())
        if chain.empty:
            continue
        for leg in trade["legs"]:
            key = (leg["strike"], leg["expiry"], leg["type"])
            if key in price_map:
                continue
            col = f"{leg['type'].lower()}_ltp"
            row = chain[(chain["strike"] == leg["strike"]) & (chain["expiry"] == leg["expiry"])]
            if not row.empty and col in row.columns:
                price_map[key] = float(row.iloc[0][col])
    return price_map


def check_option_exits() -> list[dict]:
    now_ist = _now_ist()
    market_open = is_market_open(now_ist)
    is_eod = is_eod_window(now_ist)

    if not market_open and not is_eod:
        return []

    db = _load_db()
    open_ops = [t for t in db.get("option_trades", []) if t["status"] == "OPEN"]
    if not open_ops:
        return []

    price_map = _build_option_price_map(open_ops)
    closed = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for t in open_ops:
        current_net = _option_net_premium(t["legs"], price_map)
        exit_reason = None

        if is_eod:
            exit_reason = "EOD_CLOSE"
        elif current_net is not None:
            entry_net = t.get("net_premium", 0.0)
            pnl = entry_net - current_net  # credit: profit when current falls; debit: profit when current rises
            max_loss = abs(entry_net) if entry_net != 0 else abs(current_net)
            max_profit = abs(entry_net)  # simplified; refined by structure max_loss_formula

            if pnl <= -max_loss:
                exit_reason = "SL_HIT_NET"
            elif pnl >= max_profit * 0.5:
                exit_reason = "TGT_HIT_NET"

        if exit_reason:
            t["exit_time"] = now_str
            t["exit_reason"] = exit_reason
            t["status"] = "CLOSED"
            if current_net is not None:
                t["pnl"] = round(t.get("net_premium", 0.0) - current_net, 2)
                for leg in t["legs"]:
                    key = (leg["strike"], leg["expiry"], leg["type"])
                    if key in price_map:
                        leg["exit_premium"] = price_map[key]
            else:
                t["pnl"] = 0.0
            closed.append(t)

    if closed:
        _save_db(db)
    return closed


# ═══════════════════════════════════════════════════════════════
#  NIFTY OPTION SELLING - TRADE MANAGEMENT
# ═══════════════════════════════════════════════════════════════

def enter_nifty_option_structure(setup, resolved_legs: list, cfg: dict) -> dict:
    """
    Enter a NIFTY option-selling structure with full metadata.
    
    Args:
        setup: NiftyOptionSetup with strategy details
        resolved_legs: list of ResolvedLeg objects
        cfg: config dict
    
    Returns: trade dict or error
    """
    from signals.options import is_within_entry_window, check_naked_legs, calc_structure_max_loss
    
    now_ist = _now_ist()
    
    # Check entry window
    in_window, window_reason = is_within_entry_window(cfg, now_ist)
    if not in_window:
        return {"error": f"NIFTY entry blocked: {window_reason}"}
    
    # Check naked legs
    leg_dicts = [{"side": l.side, "type": l.type, "strike": l.strike, 
                  "expiry": l.expiry, "qty": l.lots * l.lot_size} for l in resolved_legs]
    no_naked, naked_reason = check_naked_legs(leg_dicts)
    if not no_naked:
        return {"error": f"NIFTY entry blocked: {naked_reason}"}
    
    db = _load_db()
    if "option_trades" not in db:
        db["option_trades"] = []
    
    # Check concurrent NIFTY structures limit
    nifty_cfg = cfg.get("nifty_options", {})
    max_nifty = nifty_cfg.get("max_nifty_structures", 2)
    open_nifty = [t for t in db["option_trades"] 
                  if t["status"] == "OPEN" and t.get("symbol") == "NIFTY"]
    if len(open_nifty) >= max_nifty:
        return {"error": f"Max concurrent NIFTY structures ({max_nifty}) reached"}
    
    # Calculate financials
    net_credit = sum((l.premium * l.lots * l.lot_size) * (1 if l.side == "SELL" else -1) 
                     for l in resolved_legs)
    if net_credit <= 0:
        return {"error": f"NIFTY entry blocked: non-credit structure (net={net_credit:.2f})"}
    wing_width = setup.wing_width
    lot_size = resolved_legs[0].lot_size if resolved_legs else 1
    
    # Determine structure type
    if setup.strategy.startswith("IRON_CONDOR"):
        struct_type = "iron_condor"
    elif "BULL_PUT" in setup.strategy:
        struct_type = "bull_put_spread"
    elif "BEAR_CALL" in setup.strategy:
        struct_type = "bear_call_spread"
    else:
        struct_type = "unknown"
    
    max_loss = calc_structure_max_loss(struct_type, net_credit, wing_width, lot_size)
    
    # Check max risk per structure
    capital = cfg.get("account", {}).get("capital", 7000000)
    options_cfg = cfg.get("options", {})
    max_risk_pct = nifty_cfg.get("max_risk_per_pct", options_cfg.get("max_risk_per_structure_pct", 0.01))
    max_allowed_risk = capital * max_risk_pct
    if max_loss > max_allowed_risk:
        return {"error": f"NIFTY risk check: max loss ₹{max_loss:,.0f} > allowed ₹{max_allowed_risk:,.0f}"}
    
    # Build trade record
    trade = {
        "id": _next_id(db),
        "symbol": "NIFTY",
        "structure": setup.strategy,
        "mode": setup.mode,
        "strategy": setup.strategy,
        "regime": setup.regime,
        "bias": setup.bias,
        "entry_reason": setup.entry_reason,
        "exit_rules": setup.exit_rules,
        "legs": [
            {
                "side": l.side,
                "type": l.type,
                "strike": l.strike,
                "expiry": l.expiry,
                "qty": l.lots * l.lot_size,
                "entry_premium": l.premium,
                "exit_premium": None
            } for l in resolved_legs
        ],
        "net_credit": round(net_credit, 2),
        "max_loss_rupees": round(max_loss, 2),
        "risk_pct": round(max_loss / capital * 100, 2),
        "breakevens": setup.breakevens,
        "short_strikes": setup.short_strikes,
        "wing_width": wing_width,
        "vix_entry": setup.vix,
        "vix_change_pct_entry": setup.vix_change_pct,
        "pcr_entry": setup.pcr,
        "entry_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "entry_date": date.today().isoformat(),
        "exit_time": None,
        "exit_reason": None,
        "pnl": None,
        "status": "OPEN"
    }
    
    db["option_trades"].append(trade)
    _save_db(db)
    return trade


def check_nifty_option_exits(vix_current: float = None, cfg: dict = None) -> list[dict]:
    """
    Check NIFTY option trades for exit conditions.
    
    Exit triggers:
    - Profit take at 50% of max credit
    - Stop loss at 1.25x credit received
    - Short strike breach
    - VIX spike > 10%
    - EOD cutoff (intraday mode)
    - Expiry day cutoff (positional mode)
    """
    from signals.options import check_vix_spike_exit, is_within_exit_window, is_expiry_day
    
    if cfg is None:
        from config.loader import load_config
        cfg = load_config()
    
    now_ist = _now_ist()
    if not is_market_open(now_ist) and not is_eod_window(now_ist):
        return []
    
    nifty_cfg = cfg.get("nifty_options", {})
    
    db = _load_db()
    open_nifty = [t for t in db.get("option_trades", []) 
                  if t["status"] == "OPEN" and t.get("symbol") == "NIFTY"]
    if not open_nifty:
        return []
    
    # Get current prices
    price_map = _build_option_price_map(open_nifty)
    closed = []
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Get VIX if not provided
    if vix_current is None:
        try:
            vix_data = yf.Ticker("^INDIAVIX")
            vix_current = vix_data.fast_info.last_price or 15.0
        except:
            vix_current = 15.0
    
    for t in open_nifty:
        current_net = None
        entry_credit = t.get("net_credit", 0.0)
        exit_reason = None
        mode = t.get("mode", "positional")
        structure = t.get("structure", "")
        
        # Check expiry day exit for positional trades
        if mode == "positional":
            leg_expiries = [l.get("expiry") for l in t.get("legs", [])]
            is_exp = any(is_expiry_day(e) for e in leg_expiries)
            if is_exp:
                in_exit, exit_msg = is_within_exit_window(cfg, now_ist, mode)
                if in_exit:
                    exit_reason = "EXPIRY_DAY_CUTOFF"
        
        # Check EOD exit for intraday trades
        if not exit_reason and mode == "intraday":
            in_exit, exit_msg = is_within_exit_window(cfg, now_ist, mode)
            if in_exit:
                exit_reason = "EOD_CUTOFF"
        
        # Check P&L based exits
        if not exit_reason:
            current_net = _option_net_premium(t.get("legs", []), price_map)
            
            if current_net is not None:
                # For credit spreads: profit when net decreases, loss when net increases
                pnl = entry_credit - current_net
                
                # Get exit levels
                profit_take_pct = t.get("exit_rules", {}).get("profit_take_pct", 0.50)
                sl_mult = t.get("exit_rules", {}).get("stop_loss_mult", 1.25)
                
                profit_target = entry_credit * profit_take_pct
                stop_loss = entry_credit * sl_mult
                
                # Check profit take
                if pnl >= profit_target:
                    exit_reason = "PROFIT_TAKEN"
                # Check stop loss (pnl is negative when losing)
                elif pnl <= -stop_loss:
                    exit_reason = "STOP_LOSS"
                
                # Check short strike breach for Iron Condor
                if not exit_reason and "IRON_CONDOR" in structure:
                    short_strikes = t.get("short_strikes", [])
                    try:
                        spot = _get_ltp("NIFTY")
                    except Exception:
                        spot = None
                    if spot is not None and len(short_strikes) == 2:
                        if spot <= min(short_strikes) or spot >= max(short_strikes):
                            exit_reason = "SHORT_STRIKE_BREACH"

                if not exit_reason and structure == "BULL_PUT_SPREAD":
                    short_strikes = t.get("short_strikes", [])
                    spot = _get_ltp("NIFTY")
                    if spot is not None and short_strikes and spot <= short_strikes[0]:
                        exit_reason = "SHORT_STRIKE_BREACH"

                if not exit_reason and structure == "BEAR_CALL_SPREAD":
                    short_strikes = t.get("short_strikes", [])
                    spot = _get_ltp("NIFTY")
                    if spot is not None and short_strikes and spot >= short_strikes[0]:
                        exit_reason = "SHORT_STRIKE_BREACH"
        
        # Check VIX spike exit
        if not exit_reason:
            vix_entry = t.get("vix_entry", 15)
            vix_thresh = nifty_cfg.get("vix_spike_exit_pct", 10.0)
            should_exit, exit_msg = check_vix_spike_exit(vix_current, vix_entry, vix_thresh)
            if should_exit:
                exit_reason = f"VIX_SPIKE: {exit_msg}"
        
        # Execute close
        if exit_reason:
            t["exit_time"] = now_str
            t["exit_reason"] = exit_reason
            t["status"] = "CLOSED"
            
            if current_net is not None:
                t["pnl"] = round(entry_credit - current_net, 2)
                for leg in t.get("legs", []):
                    key = (leg["strike"], leg["expiry"], leg["type"])
                    if key in price_map:
                        leg["exit_premium"] = price_map[key]
            else:
                t["pnl"] = 0.0
            
            closed.append(t)
    
    if closed:
        _save_db(db)
    
    return closed


def get_nifty_option_setups(data: dict, cfg: dict = None) -> list[dict]:
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
    }
    
    setups.append(setup_dict)
    return setups


def close_trade_manual(trade_id: int, reason: str = "MANUAL") -> dict | None:
    """Manually close a specific trade at current LTP."""
    db = _load_db()
    trade = next((t for t in db["trades"] if t["id"] == trade_id and t["status"] == "OPEN"), None)
    if not trade:
        return None

    ltp = _get_ltp(trade["symbol"])
    if ltp is None:
        return {"error": f"Could not fetch LTP for {trade['symbol']}"}

    trade["exit_price"] = ltp
    trade["exit_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    trade["exit_reason"] = reason
    trade["status"] = "CLOSED"

    direction = trade["direction"]
    if direction == "LONG":
        pnl = (ltp - trade["entry_price"]) * trade["qty"]
        pnl_pct = 100 * (ltp - trade["entry_price"]) / trade["entry_price"]
    else:
        pnl = (trade["entry_price"] - ltp) * trade["qty"]
        pnl_pct = 100 * (trade["entry_price"] - ltp) / trade["entry_price"]

    trade["pnl"] = round(pnl, 2)
    trade["pnl_pct"] = round(pnl_pct, 2)
    _save_db(db)
    return trade


# ═══════════════════════════════════════════════════════════════
#  AUTO-TRADE FROM ALERTS
# ═══════════════════════════════════════════════════════════════

def auto_enter_from_alerts(alerts: list[dict], cfg: dict | None = None) -> list[dict]:
    """Take paper trades on HIGH confidence alerts automatically.
    
    Applies risk guardrails before entering:
    - Daily/monthly loss limits
    - Concurrent open risk cap
    - Margin utilization cap
    - Correlation filter
    
    Skips if same symbol already has any trade (OPEN or CLOSED) today.
    Hard cutoff: No entries after 15:15 IST (15 min before close to avoid EOD churn).
    Logs all skipped trades to journal for learning.
    """
    from config.loader import load_config
    
    now_ist = _now_ist()
    if not is_market_open(now_ist):
        return []
    # Block last 15 minutes of session to avoid EOD churn
    if now_ist.hour == 15 and now_ist.minute >= 15:
        return []

    # Load config for risk parameters
    if cfg is None:
        cfg = load_config()
    
    # Initialize guardrails
    guardrails = Guardrails(cfg)
    
    # Initialize journal for logging skipped trades
    journal = Journal(cfg["paths"]["journal_db"])
    
    today_str = date.today().isoformat()
    db = _load_db()
    # Block symbols already traded today (open OR closed)
    today_symbols = {t["symbol"] for t in db["trades"] if t.get("entry_date") == today_str}

    settings = get_settings()
    min_conf = settings["min_confidence"]
    max_trades_per_day = int(settings.get("max_trades_per_day", 8))
    max_new_entries_per_cycle = int(settings.get("max_new_entries_per_cycle", 5))
    conf_levels = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
    min_val = conf_levels.get(min_conf, 3)

    # Get current P&L state for guardrail checks
    all_closed = [t for t in db["trades"] if t["status"] == "CLOSED"]
    today_pnl = sum(t.get("pnl", 0) or 0 for t in all_closed if t.get("entry_date") == today_str)
    month_pnl = sum(t.get("pnl", 0) or 0 for t in all_closed 
                   if t.get("entry_date", "")[:7] == today_str[:7])  # YYYY-MM
    open_risk = sum(t.get("risk_rupees", 0) or 0 for t in db["trades"] if t["status"] == "OPEN")
    
    # Estimate margin utilization (simplified)
    total_capital = cfg["account"]["capital"]
    deployed = sum(t.get("capital_deployed", 0) or 0 for t in db["trades"] if t["status"] == "OPEN")
    margin_used_pct = deployed / total_capital if total_capital > 0 else 0
    today_trade_count = len([t for t in db["trades"] if t.get("entry_date") == today_str])

    entered = []
    for alert in alerts:
        if today_trade_count + len(entered) >= max_trades_per_day:
            break
        if len(entered) >= max_new_entries_per_cycle:
            break

        sym = alert.get("symbol", "")
        conf = alert.get("confidence", "MEDIUM")
        direction = alert.get("direction", "LONG")
        
        # Get regime and flow info from alert for logging
        regime = alert.get("source_regime", "UNKNOWN")
        flow_bias = alert.get("flow_bias", "NEUTRAL")

        if direction not in ("LONG", "SHORT"):
            journal.log_skipped_trade(
                symbol=sym, direction=direction, alert_confidence=conf,
                skip_reason="NOT_DIRECTIONAL", regime=regime, flow_bias=flow_bias,
                risk_gate="paper_equity_only",
                notes=alert.get("no_trade_reason") or alert.get("entry_trigger", "")
            )
            continue
        
        # Stricter confidence filter from settings
        if conf_levels.get(conf, 2) < min_val:
            journal.log_skipped_trade(
                symbol=sym, direction=direction, alert_confidence=conf,
                skip_reason="CONFIDENCE_FILTER", regime=regime, flow_bias=flow_bias,
                risk_gate=f"min_conf={min_conf}",
                notes=f"Alert confidence {conf} < required {min_conf}"
            )
            continue
            
        # Regime filter if enabled
        if settings.get("regime_filter"):
            # Skip if regime/flow mismatch (already handled by alert generator,
            # but double-check here as safety)
            pass

        # Skip if already traded today
        if sym in today_symbols:
            journal.log_skipped_trade(
                symbol=sym, direction=direction, alert_confidence=conf,
                skip_reason="DUPLICATE_TODAY", regime=regime, flow_bias=flow_bias,
                risk_gate="daily_dedup",
                notes=f"Symbol {sym} already traded today"
            )
            continue

        # === RISK GATE CHECKS ===
        # Calculate proposed risk from alert
        proposed_risk = alert.get("risk_rupees", 0) or 0
        
        # Check guardrails
        gate_result = guardrails.check_new_trade(
            proposed_risk=proposed_risk,
            open_risk=open_risk,
            day_pnl=today_pnl,
            month_pnl=month_pnl,
            margin_used_pct=margin_used_pct,
        )
        
        if not gate_result.ok:
            journal.log_skipped_trade(
                symbol=sym, direction=direction, alert_confidence=conf,
                skip_reason="RISK_GATE", regime=regime, flow_bias=flow_bias,
                risk_gate="; ".join(gate_result.reasons),
                notes=f"Proposed risk: ₹{proposed_risk:,.0f}"
            )
            continue

        # === SIZE CALCULATION ===
        # Use sizing module to calculate proper position size
        entry_price = alert.get("entry_price", 0)
        stop = alert.get("stop", 0)
        if entry_price > 0 and stop > 0 and entry_price != stop:
            capital = cfg["account"]["capital"]
            per_trade_pct = cfg["risk"]["per_trade_pct"]
            size_result = directional_size(
                capital=capital, per_trade_pct=per_trade_pct,
                entry=entry_price, stop=stop, lot_size=1
            )
            # Override alert qty with calculated size
            if size_result.qty > 0:
                alert["qty"] = size_result.qty
                alert["planned_risk"] = size_result.risk_rupees
                alert["risk_rupees"] = size_result.risk_rupees
            else:
                journal.log_skipped_trade(
                    symbol=sym, direction=direction, alert_confidence=conf,
                    skip_reason="INVALID_STOP", regime=regime, flow_bias=flow_bias,
                    risk_gate="sizing_failed",
                    notes="Could not size trade from entry/stop"
                )
                continue
        elif entry_price <= 0 or stop <= 0:
            journal.log_skipped_trade(
                symbol=sym, direction=direction, alert_confidence=conf,
                skip_reason="INVALID_ENTRY_DATA", regime=regime, flow_bias=flow_bias,
                risk_gate="missing_entry_stop",
                notes=f"entry={entry_price}, stop={stop}"
            )
            continue

        # === ENTER TRADE ===
        result = enter_trade(alert)
        if "error" not in result:
            entered.append(result)
            today_symbols.add(sym)
            # Update open_risk for next iteration
            open_risk += result.get("risk_rupees", 0) or 0
        else:
            journal.log_skipped_trade(
                symbol=sym, direction=direction, alert_confidence=conf,
                skip_reason="ENTER_FAILED", regime=regime, flow_bias=flow_bias,
                risk_gate="enter_trade_error",
                notes=result.get("error", "Unknown error")
            )

    return entered


# ═══════════════════════════════════════════════════════════════
#  EOD ANALYSIS — generates P&L summary + strategy corrections
# ═══════════════════════════════════════════════════════════════

def generate_eod_summary(target_date: str | None = None) -> dict:
    """Generate end-of-day P&L summary and strategy analysis.

    Call this at 3:30 PM IST.
    """
    db = _load_db()
    today = target_date or date.today().isoformat()

    # Close any remaining open trades for today
    closed_now = check_and_close_trades()

    # Reload after closing
    db = _load_db()

    # Filter trades for the target date
    day_trades = [t for t in db["trades"] if t.get("entry_date") == today]
    closed_today = [t for t in day_trades if t["status"] == "CLOSED"]

    total_trades = len(day_trades)
    closed_count = len(closed_today)

    winners = [t for t in closed_today if (t.get("pnl") or 0) > 0]
    losers = [t for t in closed_today if (t.get("pnl") or 0) < 0]
    breakeven = [t for t in closed_today if (t.get("pnl") or 0) == 0]
    resolved = [t for t in closed_today if (t.get("pnl") or 0) != 0]

    settings = get_settings()
    capital_per_trade = settings["capital_per_trade"]
    total_pnl = sum(t.get("pnl", 0) or 0 for t in closed_today)
    total_pnl_pct = (total_pnl / (capital_per_trade * max(total_trades, 1))) * 100


    # Categorize by exit reason
    sl_hits = [t for t in closed_today if t.get("exit_reason") == "SL_HIT"]
    tgt_hits = [t for t in closed_today if t.get("exit_reason") == "TARGET_HIT"]
    eod_exits = [t for t in closed_today if t.get("exit_reason") == "EOD_CLOSE"]

    # Analyze what went right / wrong
    analysis = _analyze_trades(closed_today)

    # Strategy corrections
    corrections = _generate_corrections(closed_today, db.get("daily_summaries", []))

    summary = {
        "date": today,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
        "total_trades": total_trades,
        "closed": closed_count,
        "winners": len(winners),
        "losers": len(losers),
        "breakeven": len(breakeven),
        "resolved": len(resolved),
        "win_rate": round(len(winners) / max(len(resolved), 1) * 100, 1),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "avg_winner_pnl": round(sum(t["pnl"] for t in winners) / max(len(winners), 1), 2),
        "avg_loser_pnl": round(sum(t["pnl"] for t in losers) / max(len(losers), 1), 2),
        "best_trade": max(closed_today, key=lambda t: t.get("pnl", 0)) if closed_today else None,
        "worst_trade": min(closed_today, key=lambda t: t.get("pnl", 0)) if closed_today else None,
        "sl_hits": len(sl_hits),
        "target_hits": len(tgt_hits),
        "eod_exits": len(eod_exits),
        "trades": closed_today,
        "analysis": analysis,
        "corrections": corrections,
        "capital_per_trade": capital_per_trade,

    }

    # Update cumulative
    db["cumulative_pnl"] = round(db.get("cumulative_pnl", 0) + total_pnl, 2)
    summary["cumulative_pnl"] = db["cumulative_pnl"]

    # Save daily summary
    # Remove existing summary for same date if re-running
    db["daily_summaries"] = [s for s in db["daily_summaries"] if s.get("date") != today]
    db["daily_summaries"].append(summary)

    # Save strategy notes
    if corrections:
        db["strategy_notes"].append({
            "date": today,
            "corrections": corrections,
        })

    _save_db(db)
    return summary


def _analyze_trades(trades: list[dict]) -> dict:
    """Analyze what went right and wrong."""
    if not trades:
        return {
            "what_went_right": ["No trades taken today."],
            "what_went_wrong": [],
            "patterns": [],
        }

    right = []
    wrong = []
    patterns = []

    winners = [t for t in trades if (t.get("pnl") or 0) > 0]
    losers = [t for t in trades if (t.get("pnl") or 0) < 0]
    sl_hits = [t for t in trades if t.get("exit_reason") == "SL_HIT"]
    tgt_hits = [t for t in trades if t.get("exit_reason") == "TARGET_HIT"]

    # Winners analysis
    if winners:
        high_conf_winners = [t for t in winners if t.get("confidence") == "HIGH"]
        if high_conf_winners:
            right.append(f"HIGH confidence signals delivered: {len(high_conf_winners)}/{len(winners)} winners were HIGH conf")
        if tgt_hits:
            right.append(f"{len(tgt_hits)} trade(s) hit full target — R:R framework working")
        long_winners = [t for t in winners if t["direction"] == "LONG"]
        short_winners = [t for t in winners if t["direction"] == "SHORT"]
        if long_winners:
            right.append(f"Long bias correct: {len(long_winners)} profitable long(s)")
        if short_winners:
            right.append(f"Short bias correct: {len(short_winners)} profitable short(s)")

    # Losers analysis
    if losers:
        low_conf_losers = [t for t in losers if t.get("confidence") in ("LOW", "MEDIUM")]
        if low_conf_losers:
            wrong.append(f"{len(low_conf_losers)} loser(s) were LOW/MEDIUM confidence — filter stricter")
        if len(sl_hits) > len(tgt_hits):
            wrong.append(f"SL hits ({len(sl_hits)}) > Target hits ({len(tgt_hits)}) — SL too tight or entries poorly timed")
        # Check if losses concentrated in one direction
        long_losers = [t for t in losers if t["direction"] == "LONG"]
        short_losers = [t for t in losers if t["direction"] == "SHORT"]
        if len(long_losers) > 2:
            wrong.append(f"Multiple long losses ({len(long_losers)}) — bull signal may be false")
        if len(short_losers) > 2:
            wrong.append(f"Multiple short losses ({len(short_losers)}) — bearish signal may be false")

    # Patterns
    # Only count trades that actually had a movement (exclude churn/0-PNL entries)
    resolved_trades = [t for t in trades if (t.get("pnl") or 0) != 0]
    total = len(resolved_trades)
    if total > 0:
        win_rate = len(winners) / total * 100
        if win_rate >= 60:
            patterns.append(f"Win rate {win_rate:.0f}% — system performing well")
        elif win_rate <= 30:
            patterns.append(f"Win rate {win_rate:.0f}% — system underperforming, widen SL or reduce position count")

        index_trades = [t for t in trades if t.get("type") == "INDEX"]
        stock_trades = [t for t in trades if t.get("type") == "STOCK"]
        if index_trades:
            idx_pnl = sum(t.get("pnl", 0) for t in index_trades)
            patterns.append(f"Index trades P&L: Rs {idx_pnl:,.0f}")
        if stock_trades:
            stk_pnl = sum(t.get("pnl", 0) for t in stock_trades)
            patterns.append(f"Stock trades P&L: Rs {stk_pnl:,.0f}")

    if not right:
        right.append("No winning trades today.")
    if not wrong:
        wrong.append("No losing trades today — clean session!")

    return {
        "what_went_right": right,
        "what_went_wrong": wrong,
        "patterns": patterns,
    }


def _generate_corrections(today_trades: list[dict], history: list[dict]) -> list[str]:
    """Generate strategy corrections based on today's result + recent history."""
    corrections = []

    if not today_trades:
        return ["No trades to analyze. Consider lowering confidence threshold if signals were present but not taken."]

    winners = [t for t in today_trades if (t.get("pnl") or 0) > 0]
    losers = [t for t in today_trades if (t.get("pnl") or 0) < 0]
    sl_hits = [t for t in today_trades if t.get("exit_reason") == "SL_HIT"]

    win_rate = len(winners) / len(today_trades) * 100 if today_trades else 0

    # SL too tight?
    if len(sl_hits) >= 3:
        corrections.append("WIDEN SL: 3+ SL hits today. Consider 2.5% SL instead of 2%.")

    # Win rate corrections
    if win_rate < 40 and len(today_trades) >= 3:
        corrections.append("REDUCE TRADES: Low win rate. Only take HIGH confidence signals tomorrow.")
    elif win_rate >= 70:
        corrections.append("MAINTAIN: Strategy working. Keep current parameters.")

    # Check for missed targets (EOD exits that were in profit)
    eod_profitable = [t for t in today_trades if t.get("exit_reason") == "EOD_CLOSE" and (t.get("pnl") or 0) > 0]
    if eod_profitable:
        corrections.append(f"TRAIL STOP: {len(eod_profitable)} trade(s) exited at EOD with profit — implement trailing SL to lock gains.")

    # Directional bias check
    long_trades = [t for t in today_trades if t["direction"] == "LONG"]
    short_trades = [t for t in today_trades if t["direction"] == "SHORT"]
    long_losses = sum(1 for t in long_trades if (t.get("pnl") or 0) < 0)
    short_losses = sum(1 for t in short_trades if (t.get("pnl") or 0) < 0)

    if long_losses >= 2 and len(long_trades) >= 2:
        corrections.append("REDUCE LONGS: Long bias failing. Check if regime has shifted bearish.")
    if short_losses >= 2 and len(short_trades) >= 2:
        corrections.append("REDUCE SHORTS: Short bias failing. Check if regime has shifted bullish.")

    # Check recent history for systemic issues
    if len(history) >= 3:
        last_3 = history[-3:]
        losing_days = sum(1 for s in last_3 if s.get("total_pnl", 0) < 0)
        if losing_days >= 3:
            corrections.append("PAUSE TRADING: 3 consecutive losing days. Review strategy fundamentals before continuing.")
        avg_win_rate = sum(s.get("win_rate", 0) for s in last_3) / 3
        if avg_win_rate < 35:
            corrections.append("OVERHAUL SIGNALS: 3-day average win rate below 35%. Tighten entry criteria aggressively.")

    if not corrections:
        corrections.append("No corrections needed. System performing within parameters.")

    return corrections


# ═══════════════════════════════════════════════════════════════
#  MAINTENANCE
# ═══════════════════════════════════════════════════════════════

def cleanup_db(from_date: str | None = None, to_date: str | None = None, purge_churn: bool = False, full_reset: bool = False) -> dict:
    """Clean up trade database based on criteria.
    - full_reset: Clears ALL trades and summaries.
    - purge_churn: Removes trades with 0 P&L (spam entries).
    - from_date/to_date: Removes ALL trades in this range (inclusive).
    """
    db = _load_db()
    
    if full_reset:
        db["trades"] = []
        db["option_trades"] = []
        db["daily_summaries"] = []
        db["strategy_notes"] = []
        db["cumulative_pnl"] = 0.0
        _save_db(db)
        return {"status": "success", "message": "Database fully reset"}

    original_count = len(db["trades"])

    keep_trades = []
    removed_count = 0

    for t in db["trades"]:
        entry_date = t.get("entry_date", "")
        pnl = t.get("pnl") or 0.0
        
        # 1. Check Date Range
        in_range = True
        if from_date and entry_date < from_date:
            in_range = False
        if to_date and entry_date > to_date:
            in_range = False
            
        if from_date or to_date:
            if in_range:
                removed_count += 1
                continue # Delete it
        
        # 2. Check Churn (Spam)
        # ID <= 5 are usually the "golden" test trades, keep them
        if purge_churn and t["id"] > 5 and pnl == 0 and t["status"] == "CLOSED":
            removed_count += 1
            continue # Delete it

        keep_trades.append(t)

    # Re-sync database state
    db["trades"] = keep_trades
    
    # Recalculate cumulative P&L
    db["cumulative_pnl"] = sum(t.get("pnl", 0) for t in keep_trades if t["status"] == "CLOSED")
    
    # Clean up daily summaries that might now be empty or inaccurate
    new_summaries = []
    for s in db.get("daily_summaries", []):
        d = s["date"]
        # If we deleted everything in a range, we might want to delete the summary too
        if (from_date and d < from_date) or (to_date and d > to_date):
            # If we are strictly deleting a range, remove summary
            if from_date or to_date:
                continue
        
        # Update trade list in summary
        s_trades = [t for t in keep_trades if t.get("entry_date") == d]
        s["trades"] = s_trades
        s["total_trades"] = len(s_trades)
        s["total_pnl"] = sum(t.get("pnl", 0) for t in s_trades)
        # Recalculate win rate
        winners = [t for t in s_trades if (t.get("pnl") or 0) > 0]
        s["win_rate"] = round((len(winners) / len(s_trades) * 100), 2) if s_trades else 0
        
        new_summaries.append(s)
    
    db["daily_summaries"] = new_summaries
    _save_db(db)

    return {
        "original_count": original_count,
        "removed_count": removed_count,
        "final_count": len(db["trades"]),
        "cumulative_pnl": db["cumulative_pnl"]
    }


def get_open_trades() -> list[dict]:
    """Return all currently open trades."""
    db = _load_db()
    return [t for t in db["trades"] if t["status"] == "OPEN"]


def get_all_trades(limit: int = 50) -> list[dict]:
    """Return recent trades (newest first)."""
    db = _load_db()
    return list(reversed(db["trades"][-limit:]))


def get_daily_summaries(limit: int = 30) -> list[dict]:
    """Return recent daily summaries."""
    db = _load_db()
    return list(reversed(db["daily_summaries"][-limit:]))


def get_strategy_notes() -> list[dict]:
    """Return all strategy correction notes."""
    db = _load_db()
    return list(reversed(db.get("strategy_notes", [])))


def get_stats() -> dict:
    """Global stats across all paper trading.

    Distinguishes resolved trades (pnl != 0) from churn (same-price EOD closes).
    Win rate / avg winner / avg loser are computed on RESOLVED trades only
    so that breakeven churn does not dilute performance metrics.
    """
    db = _load_db()
    trades = db["trades"]
    all_closed = [t for t in trades if t["status"] == "CLOSED"]
    resolved = [t for t in all_closed if (t.get("pnl") or 0) != 0]
    winners = [t for t in resolved if (t.get("pnl") or 0) > 0]
    losers = [t for t in resolved if (t.get("pnl") or 0) < 0]
    breakeven = [t for t in all_closed if (t.get("pnl") or 0) == 0]
    all_pnl = sum(t.get("pnl", 0) or 0 for t in all_closed)

    return {
        "total_trades": len(trades),
        "open_trades": len([t for t in trades if t["status"] == "OPEN"]),
        "closed_trades": len(all_closed),
        "resolved_trades": len(resolved),
        "breakeven_trades": len(breakeven),
        "cumulative_pnl": round(all_pnl, 2),
        "total_winners": len(winners),
        "total_losers": len(losers),
        "overall_win_rate": round(len(winners) / max(len(resolved), 1) * 100, 1),
        "avg_winner": round(sum(t["pnl"] for t in winners) / max(len(winners), 1), 2),
        "avg_loser": round(sum(t["pnl"] for t in losers) / max(len(losers), 1), 2),
        "best_trade": max(all_closed, key=lambda t: t.get("pnl", 0) or 0) if all_closed else None,
        "worst_trade": min(all_closed, key=lambda t: t.get("pnl", 0) or 0) if all_closed else None,
        "capital_per_trade": get_settings()["capital_per_trade"],
        "strategy_corrections": len(db.get("strategy_notes", [])),
    }


def export_trades_to_csv() -> str:
    """Return trade history as a CSV string."""
    db = _load_db()
    trades = db.get("trades", [])
    if not trades:
        return "No trades found"

    df = pd.DataFrame(trades)
    # Reorder columns for readability
    cols = [
        "id", "symbol", "direction", "status", "entry_price", "exit_price",
        "qty", "capital_deployed", "pnl", "pnl_pct", "exit_reason",
        "entry_time", "exit_time", "confidence"
    ]
    # Filter only available columns
    available_cols = [c for c in cols if c in df.columns]
    return df[available_cols].to_csv(index=False)
