r"""Paper Trading Engine for StockMinded.

Manages simulated trades with entry/exit criteria, tracks P&L,
and generates end-of-day analysis with strategy corrections.

Data persisted to dashboard/paper_trades.json.
"""
from __future__ import annotations

import json
import msvcrt
import contextlib
import threading
import traceback
from datetime import datetime, date, time, timezone, timedelta
from pathlib import Path
from typing import Any

import yfinance as yf
import pandas as pd

# Standardized IST timezone
IST = timezone(timedelta(hours=5, minutes=30))

def _now_ist() -> datetime:
    return datetime.now(IST)

# Import risk modules
from risk.guardrails import Guardrails
from risk.sizing import directional_size, SizeResult
from ops.journal import Journal
from config.loader import load_config

DATA_FILE = Path(__file__).parent / "paper_trades.json"

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

@contextlib.contextmanager
def atomic_db_update():
    """Transaction-style database update with Windows cross-process locking."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    if not DATA_FILE.exists():
        with open(DATA_FILE, "w", encoding='utf-8') as f:
            json.dump({
                "trades": [],
                "option_trades": [],
                "daily_summaries": [],
                "strategy_notes": [],
                "settings": DEFAULT_SETTINGS.copy(),
                "cumulative_pnl": 0.0,
                "version": 1,
            }, f, indent=2)

    with open(DATA_FILE, "r+", encoding='utf-8') as f:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            db = json.load(f)
            yield db
            f.seek(0)
            f.truncate()
            json.dump(db, f, indent=2, default=str)
            f.flush()
            import os
            os.fsync(f.fileno())
        finally:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
EOD_WINDOW_START = time(15, 25)
EOD_WINDOW_END = time(15, 35)

def is_market_open(now: datetime | None = None) -> bool:
    """True only on Mon-Fri between 09:15 and 15:30 IST."""
    n = now or _now_ist()
    if n.tzinfo is None:
        n = n.replace(tzinfo=IST)
    if n.weekday() >= 5:
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

def _load_db() -> dict:
    """Load the full trade database (Read Only)."""
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
    with atomic_db_update() as db:
        if "settings" not in db:
            db["settings"] = DEFAULT_SETTINGS.copy()
        for k, v in new_settings.items():
            if k in DEFAULT_SETTINGS:
                if k in ("capital_per_trade", "sl_pct", "tgt_pct"):
                    db["settings"][k] = float(v)
                elif k in ("max_trades_per_day", "max_new_entries_per_cycle"):
                    db["settings"][k] = int(v)
                elif k in ("trail_sl", "regime_filter"):
                    db["settings"][k] = bool(v)
                else:
                    db["settings"][k] = str(v)
        return db["settings"]

def _next_id(db: dict) -> int:
    """Get next trade ID."""
    t_ids = [t["id"] for t in db.get("trades", [])]
    o_ids = [t.get("id", 0) for t in db.get("option_trades", [])]
    all_ids = t_ids + o_ids
    return max(all_ids + [0]) + 1

def _get_ltp(symbol: str) -> float | None:
    """Fetch the latest trading price for a symbol."""
    try:
        from data import feed
        px = feed.ltp(symbol)
        if px: return px
    except Exception: pass
    try:
        yf_sym = f"{symbol}.NS" if not symbol.startswith("^") and "." not in symbol else symbol
        t = yf.Ticker(yf_sym)
        info = t.fast_info
        return round(float(info.last_price), 2) if info.last_price else None
    except Exception: return None

def _get_ltp_batch(symbols: list[str]) -> dict[str, float | None]:
    """Fetch LTPs for multiple symbols at once."""
    try:
        from data import feed
        quotes = feed.quote_batch(symbols)
        result = {s: (round(float(quotes[s]["ltp"]), 2) if quotes.get(s, {}).get("ltp") else None) for s in symbols}
        if any(v is not None for v in result.values()):
            missing = [s for s, v in result.items() if v is None]
            if not missing: return result
        else: missing = symbols
    except Exception:
        result = {}
        missing = symbols

    yf_syms = [f"{s}.NS" if not s.startswith("^") and "." not in s else s for s in missing]
    try:
        tickers = yf.Tickers(" ".join(yf_syms))
        for sym, yf_s in zip(missing, yf_syms):
            try:
                info = tickers.tickers[yf_s].fast_info
                result[sym] = round(float(info.last_price), 2) if info.last_price else None
            except Exception: result[sym] = None
    except Exception:
        for s in missing: result[s] = None
    return result

def enter_trade(alert: dict) -> dict:
    """Open a paper trade based on an alert signal."""
    if not is_market_open():
        return {"error": "Market closed (9:15-15:30 IST, Mon-Fri)"}

    with atomic_db_update() as db:
        symbol = alert["symbol"]
        direction = alert.get("direction", "LONG")
        today_str = date.today().isoformat()

        for t in db["trades"]:
            if t.get("symbol") == symbol and t.get("entry_date") == today_str:
                return {"error": f"{symbol} already traded today (id={t['id']})"}

        entry_price = _get_ltp(symbol)
        if entry_price is None:
            return {"error": f"Could not fetch LTP for {symbol}"}

        settings = db.get("settings", DEFAULT_SETTINGS.copy())
        capital_per_trade = settings["capital_per_trade"]
        sl_pct = settings["sl_pct"]
        tgt_pct = settings["tgt_pct"]

        # Calculate qty based on capital cap
        qty_cap = int(capital_per_trade / entry_price) if entry_price > 0 else 0
        alert_qty = int(alert.get("qty") or 0)
        
        if alert_qty > 0:
            qty = min(qty_cap, alert_qty)
        else:
            qty = qty_cap

        if qty == 0: 
            return {"error": f"Price too high for Rs {capital_per_trade:,.0f} allocation"}

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
            "entry_time": _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_date": today_str,
            "exit_price": None,
            "exit_time": None,
            "exit_reason": None,
            "pnl": None,
            "pnl_pct": None,
            "status": "OPEN",
            "evidence": alert.get("evidence", []),
            "source_regime": alert.get("source_regime", ""),
            "hold_minutes": None
        }

        # Sync with SQLite Journal for historical audit
        try:
            cfg = load_config()
            journal = Journal(cfg["paths"]["journal_db"])
            jid = journal.open_trade(
                symbol=symbol,
                structure=alert.get("type", "STOCK"),
                side=direction,
                qty=qty,
                entry=entry_price,
                stop=sl_price,
                target=tgt_price,
                risk_rupees=round(abs(entry_price - sl_price) * qty, 2),
                regime=alert.get("source_regime", "UNKNOWN"),
                notes=f"Manual entry: {alert.get('entry_trigger', '')}"
            )
            trade["journal_id"] = jid
        except Exception as e:
            # Non-blocking failure; log for investigation
            print(f"⚠️ Journal sync failed for {symbol}: {e}")

        db["trades"].append(trade)
        return trade

def enter_option_structure(structure_name: str, resolved_legs: list, underlying: str, cfg: dict) -> dict:
    """Enter a custom option structure."""
    if not is_market_open(): return {"error": "Market closed"}
    with atomic_db_update() as db:
        if "option_trades" not in db: db["option_trades"] = []
        open_ops = [t for t in db["option_trades"] if t["status"] == "OPEN"]
        max_ops = cfg.get("options", {}).get("max_concurrent_structures", 4)
        if len(open_ops) >= max_ops: return {"error": f"Max concurrent options structures ({max_ops}) reached"}
        
        net_premium = sum((leg.premium * leg.lots * leg.lot_size) * (1 if leg.side == "SELL" else -1) for leg in resolved_legs)
        trade = {
            "id": _next_id(db),
            "symbol": underlying,
            "structure": structure_name,
            "legs": [{
                "side": l.side, "type": l.type, "strike": l.strike, "expiry": l.expiry,
                "qty": l.lots * l.lot_size, "entry_premium": l.premium, "exit_premium": None
            } for l in resolved_legs],
            "net_premium": round(net_premium, 2),
            "entry_time": _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_date": date.today().isoformat(),
            "exit_time": None, "exit_reason": None, "pnl": None, "status": "OPEN"
        }
        db["option_trades"].append(trade)
        return trade

def enter_nifty_option_structure(setup, resolved_legs: list, cfg: dict) -> dict:
    """Enter a NIFTY specific option structure with metadata."""
    from signals.options import is_within_entry_window, check_naked_legs, calc_structure_max_loss
    now_ist = _now_ist()
    in_window, window_reason = is_within_entry_window(cfg, now_ist)
    journal = Journal(cfg["paths"]["journal_db"])
    
    if not in_window:
        journal.log_skipped_trade("NIFTY", "NEUTRAL", "MED", "WINDOW_CLOSED", "UNKNOWN", "NEUTRAL", "options_gate", window_reason)
        return {"error": f"NIFTY entry blocked: {window_reason}"}
    
    leg_dicts = [{"side": l.side, "type": l.type, "strike": l.strike, "expiry": l.expiry, "qty": l.lots * l.lot_size} for l in resolved_legs]
    no_naked, naked_reason = check_naked_legs(leg_dicts)
    if not no_naked:
        journal.log_skipped_trade("NIFTY", "NEUTRAL", "MED", "NAKED_RISK", "UNKNOWN", "NEUTRAL", "options_gate", naked_reason)
        return {"error": f"NIFTY entry blocked: {naked_reason}"}
    
    with atomic_db_update() as db:
        if "option_trades" not in db: db["option_trades"] = []
        nifty_cfg = cfg.get("nifty_options", {})
        max_nifty = nifty_cfg.get("max_nifty_structures", 2)
        open_nifty = [t for t in db["option_trades"] if t["status"] == "OPEN" and t.get("symbol") == "NIFTY"]
        if len(open_nifty) >= max_nifty:
            journal.log_skipped_trade("NIFTY", "NEUTRAL", "MED", "MAX_TRADES", "UNKNOWN", "NEUTRAL", "options_gate", "Max concurrent NIFTY structures reached")
            return {"error": f"Max concurrent NIFTY structures reached"}
        
        net_credit = sum((l.premium * l.lots * l.lot_size) * (1 if l.side == "SELL" else -1) for l in resolved_legs)
        if net_credit <= 0:
            journal.log_skipped_trade("NIFTY", "NEUTRAL", "MED", "NO_CREDIT", "UNKNOWN", "NEUTRAL", "options_gate", "Non-credit structure")
            return {"error": f"NIFTY blocked: non-credit"}
        
        lot_size = resolved_legs[0].lot_size if resolved_legs else 1
        struct_type = "iron_condor" if "IRON_CONDOR" in setup.strategy else ("bull_put_spread" if "BULL_PUT" in setup.strategy else "bear_call_spread")
        max_loss = calc_structure_max_loss(struct_type, net_credit, setup.wing_width, lot_size)
        
        trade = {
            "id": _next_id(db), "symbol": "NIFTY", "structure": setup.strategy, "mode": setup.mode,
            "legs": [{
                "side": l.side, "type": l.type, "strike": l.strike, "expiry": l.expiry,
                "qty": l.lots * l.lot_size, "entry_premium": l.premium, "exit_premium": None
            } for l in resolved_legs],
            "net_credit": round(net_credit, 2), "max_loss_rupees": round(max_loss, 2),
            "entry_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"), "entry_date": date.today().isoformat(),
            "status": "OPEN", "pnl": None, "exit_reason": None, "exit_time": None
        }
        db["option_trades"].append(trade)
        return trade

def _option_net_premium(legs: list[dict], price_map: dict) -> float | None:
    total = 0.0
    for leg in legs:
        key = (leg["strike"], leg["expiry"], leg["type"])
        price = price_map.get(key)
        if price is None: return None
        sign = 1 if leg["side"] == "SELL" else -1
        total += sign * price * leg["qty"]
    return round(total, 2)

def _build_option_price_map(open_ops: list[dict]) -> dict:
    from signals.options import chain_snapshot
    underlyings = list({t["symbol"] for t in open_ops})
    price_map = {}
    for sym in underlyings:
        try:
            chain = chain_snapshot(sym)
            if chain.empty: continue
            for trade in [t for t in open_ops if t["symbol"] == sym]:
                for leg in trade["legs"]:
                    key = (leg["strike"], leg["expiry"], leg["type"])
                    col = f"{leg['type'].lower()}_ltp"
                    row = chain[(chain["strike"] == leg["strike"]) & (chain["expiry"] == leg["expiry"])]
                    if not row.empty: price_map[key] = float(row.iloc[0][col])
        except Exception: continue
    return price_map

def check_option_exits() -> list[dict]:
    now_ist = _now_ist()
    if not is_market_open(now_ist) and not is_eod_window(now_ist): return []
    closed = []
    with atomic_db_update() as db:
        open_ops = [t for t in db.get("option_trades", []) if t["status"] == "OPEN"]
        if not open_ops: return []
        price_map = _build_option_price_map(open_ops)
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True)
        is_eod = is_eod_window(now_ist)
        
        for t in open_ops:
            current_net = _option_net_premium(t["legs"], price_map)
            exit_reason = "EOD_CLOSE" if (is_eod and auto_close) else None
            if not exit_reason and current_net is not None:
                pnl = t.get("net_premium", 0.0) - current_net
                if pnl <= -abs(t.get("net_premium", 1000)) * 2: exit_reason = "SL_HIT"
                elif pnl >= abs(t.get("net_premium", 1000)) * 0.5: exit_reason = "TGT_HIT"
            if exit_reason:
                t["status"], t["exit_reason"], t["exit_time"] = "CLOSED", exit_reason, now_ist.strftime("%Y-%m-%d %H:%M:%S")
                t["pnl"] = round(t.get("net_premium", 0.0) - current_net, 2) if current_net is not None else 0.0
                closed.append(t)
    return closed

def check_nifty_option_exits(vix_current: float = None, cfg: dict = None) -> list[dict]:
    from signals.options import is_expiry_day, is_within_exit_window
    now_ist = _now_ist()
    if not is_market_open(now_ist) and not is_eod_window(now_ist): return []
    closed = []
    with atomic_db_update() as db:
        open_nifty = [t for t in db.get("option_trades", []) if t["status"] == "OPEN" and t.get("symbol") == "NIFTY"]
        if not open_nifty: return []
        price_map = _build_option_price_map(open_nifty)
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True)
        is_eod = is_eod_window(now_ist)

        for t in open_nifty:
            exit_reason = None
            if is_eod and auto_close: exit_reason = "EOD_CUTOFF"
            current_net = _option_net_premium(t["legs"], price_map)
            if not exit_reason and current_net is not None:
                pnl = t.get("net_credit", 0.0) - current_net
                if pnl >= t.get("net_credit", 0) * 0.5: exit_reason = "PROFIT_TAKEN"
                elif pnl <= -t.get("net_credit", 0) * 1.25: exit_reason = "STOP_LOSS"
            if exit_reason:
                t["status"], t["exit_reason"], t["exit_time"] = "CLOSED", exit_reason, now_ist.strftime("%Y-%m-%d %H:%M:%S")
                t["pnl"] = round(t.get("net_credit", 0.0) - current_net, 2) if current_net is not None else 0.0
                closed.append(t)
    return closed

def get_nifty_option_setups(data: dict, cfg: dict = None) -> list[dict]:
    """
    Generate actionable NIFTY option-selling setups from signal data.
    """
    from signals.option_strategy import pick_nifty_strategy, resolve_nifty_structure
    from signals.options import chain_snapshot, is_within_entry_window
    
    if cfg is None:
        from config.loader import load_config
        cfg = load_config()
    
    journal = Journal(cfg["paths"]["journal_db"])
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
    spot = data.get("nifty", {}).get("close", 0)
    
    if spot <= 0: return setups
    
    # Check entry window
    in_window, window_reason = is_within_entry_window(cfg)
    
    # Pick strategy — pass full data dict as first arg (required for verdict lookup)
    setup = pick_nifty_strategy(data, regime_name, bias, vix, vix_change, pcr, cfg)
    
    if setup is None:
        journal.log_skipped_trade("NIFTY", "NEUTRAL", "LOW", "NO_STRATEGY", regime_name, bias, "options_engine", "No strategy for current regime/bias")
        setups.append({
            "symbol": "NIFTY", "suitable": False, "skip_reason": "No strategy for current regime/bias",
            "regime": regime_name, "bias": bias, "vix": vix
        })
        return setups
    
    # Get option chain
    try:
        chain = chain_snapshot("NIFTY")
    except Exception as e:
        journal.log_skipped_trade("NIFTY", "NEUTRAL", "MED", "CHAIN_ERROR", regime_name, bias, "options_engine", str(e))
        setups.append({
            "symbol": "NIFTY", "suitable": False, "skip_reason": f"Chain error: {e}",
            "regime": regime_name, "bias": bias, "vix": vix
        })
        return setups
    
    if chain.empty:
        journal.log_skipped_trade("NIFTY", "NEUTRAL", "MED", "EMPTY_CHAIN", regime_name, bias, "options_engine", "Empty option chain")
        return setups

    # Read lot_size/strike_step from the correct 'options' config section
    options_cfg = cfg.get("options", {})
    lot_size = (
        nifty_cfg.get("lot_size", {}).get("NIFTY")
        or options_cfg.get("lot_size", {}).get("NIFTY")
        or 75
    )
    strike_step = (
        nifty_cfg.get("strike_step", {}).get("NIFTY")
        or options_cfg.get("strike_step", {}).get("NIFTY")
        or 50
    )
    setup = resolve_nifty_structure(setup, chain, spot, lot_size, strike_step)
    
    setup.entry_window_ok = in_window
    if not in_window or not setup.suitable:
        reason = setup.skip_reason if not setup.suitable else window_reason
        journal.log_skipped_trade("NIFTY", "NEUTRAL", "MED", "NOT_SUITABLE", regime_name, bias, "options_engine", reason)

    setup_dict = {
        "symbol": setup.symbol, "mode": setup.mode, "strategy": setup.strategy,
        "regime": setup.regime, "bias": setup.bias, "vix": setup.vix,
        "vix_change_pct": setup.vix_change_pct, "pcr": setup.pcr,
        "entry_reason": setup.entry_reason, "entry_window_ok": setup.entry_window_ok,
        "entry_window_reason": window_reason, "suitable": setup.suitable and in_window,
        "skip_reason": setup.skip_reason if not setup.suitable else ("" if in_window else window_reason),
        "net_credit": setup.net_credit, "max_loss_rupees": setup.max_loss_rupees,
        "risk_pct": setup.risk_pct, "breakevens": setup.breakevens,
        "short_strikes": setup.short_strikes, "wing_width": setup.wing_width,
        "exit_rules": setup.exit_rules,
        "legs": [
            {"side": l.side, "type": l.type, "strike": l.strike, "expiry": l.expiry, "qty": l.lots * l.lot_size, "premium": l.premium}
            for l in setup.legs
        ] if setup.legs else []
    }
    setups.append(setup_dict)
    return setups

def check_and_close_trades() -> list[dict]:
    """Check all OPEN trades against current prices."""
    now_ist = _now_ist()
    market_open = is_market_open(now_ist)
    is_eod = is_eod_window(now_ist)

    if not market_open and not is_eod: return []

    closed = []
    with atomic_db_update() as db:
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True) # Default to True if missing
        
        open_trades = [t for t in db["trades"] if t["status"] == "OPEN"]
        if not open_trades: return []

        symbols = list(set(t["symbol"] for t in open_trades))
        prices = _get_ltp_batch(symbols)

        for trade in open_trades:
            ltp = prices.get(trade["symbol"])
            if ltp is None: continue

            exit_reason = None
            direction = trade["direction"]

            if any(k not in trade for k in ["sl_price", "tgt_price", "direction"]):
                continue

            # Respect EOD close setting
            eod_trigger = is_eod and auto_close

            if direction == "LONG":
                if ltp <= trade["sl_price"]: exit_reason = "SL_HIT"
                elif ltp >= trade["tgt_price"]: exit_reason = "TARGET_HIT"
                elif eod_trigger: exit_reason = "EOD_CLOSE"
            else:
                if ltp >= trade["sl_price"]: exit_reason = "SL_HIT"
                elif ltp <= trade["tgt_price"]: exit_reason = "TARGET_HIT"
                elif eod_trigger: exit_reason = "EOD_CLOSE"

            if exit_reason:
                trade["exit_price"] = ltp
                trade["exit_time"] = now_ist.strftime("%Y-%m-%d %H:%M:%S")
                trade["exit_reason"] = exit_reason
                trade["status"] = "CLOSED"
                
                # Duration tracking
                try:
                    entry_dt = datetime.strptime(trade["entry_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
                    trade["hold_minutes"] = int((now_ist - entry_dt).total_seconds() / 60)
                except Exception: pass

                if direction == "LONG":
                    pnl = (ltp - trade["entry_price"]) * trade["qty"]
                    pnl_pct = 100 * (ltp - trade["entry_price"]) / trade["entry_price"]
                else:
                    pnl = (trade["entry_price"] - ltp) * trade["qty"]
                    pnl_pct = 100 * (trade["entry_price"] - ltp) / trade["entry_price"]

                trade["pnl"] = round(pnl, 2)
                trade["pnl_pct"] = round(pnl_pct, 2)
                
                # Sync exit with Journal
                jid = trade.get("journal_id")
                if jid:
                    try:
                        cfg = load_config()
                        journal = Journal(cfg["paths"]["journal_db"])
                        journal.close_trade(jid, ltp, trade["pnl"])
                    except Exception as e:
                        print(f"⚠️ Journal close sync failed for trade {jid}: {e}")

                closed.append(trade)
    return closed

def close_trade_manual(trade_id: int, reason: str = "MANUAL") -> dict | None:
    """Manually close a specific trade at current LTP."""
    now_ist = _now_ist()
    with atomic_db_update() as db:
        trade = next((t for t in db["trades"] if t["id"] == trade_id and t["status"] == "OPEN"), None)
        if not trade: return None

        ltp = _get_ltp(trade["symbol"])
        if ltp is None: return {"error": f"Could not fetch LTP for {trade['symbol']}"}

        trade["exit_price"] = ltp
        trade["exit_time"] = now_ist.strftime("%Y-%m-%d %H:%M:%S")
        trade["exit_reason"] = reason
        trade["status"] = "CLOSED"
        
        try:
            entry_dt = datetime.strptime(trade["entry_time"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
            trade["hold_minutes"] = int((now_ist - entry_dt).total_seconds() / 60)
        except Exception: pass

        direction = trade["direction"]
        if direction == "LONG":
            pnl = (ltp - trade["entry_price"]) * trade["qty"]
            pnl_pct = 100 * (ltp - trade["entry_price"]) / trade["entry_price"]
        else:
            pnl = (trade["entry_price"] - ltp) * trade["qty"]
            pnl_pct = 100 * (trade["entry_price"] - ltp) / trade["entry_price"]

        trade["pnl"] = round(pnl, 2)
        trade["pnl_pct"] = round(pnl_pct, 2)

        # Sync manual exit with Journal
        jid = trade.get("journal_id")
        if jid:
            try:
                cfg = load_config()
                journal = Journal(cfg["paths"]["journal_db"])
                journal.close_trade(jid, ltp, trade["pnl"])
            except Exception as e:
                print(f"⚠️ Journal close sync failed for trade {jid}: {e}")

        return trade

def auto_enter_from_alerts(alerts: list[dict], cfg: dict | None = None) -> list[dict]:
    """Take paper trades on alerts automatically with risk guardrails and learned filters."""
    from config.loader import load_config
    now_ist = _now_ist()
    if not is_market_open(now_ist): return []
    if now_ist.hour == 15 and now_ist.minute >= 15: return []

    if cfg is None: cfg = load_config()
    guardrails = Guardrails(cfg)
    journal = Journal(cfg["paths"]["journal_db"])
    today_str = date.today().isoformat()
    entered = []

    with atomic_db_update() as db:
        settings = db.get("settings", DEFAULT_SETTINGS.copy())
        min_conf = settings.get("min_confidence", "HIGH")
        max_trades_per_day = int(settings.get("max_trades_per_day", 8))
        max_new_entries_per_cycle = int(settings.get("max_new_entries_per_cycle", 5))
        conf_levels = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        min_val = conf_levels.get(min_conf, 3)

        today_symbols = {t["symbol"] for t in db["trades"] if t.get("entry_date") == today_str}
        all_closed = [t for t in db["trades"] if t["status"] == "CLOSED"]
        today_pnl = sum(t.get("pnl", 0) or 0 for t in all_closed if t.get("entry_date") == today_str)
        month_pnl = sum(t.get("pnl", 0) or 0 for t in all_closed if t.get("entry_date", "")[:7] == today_str[:7])
        open_risk = sum(t.get("risk_rupees", 0) or 0 for t in db["trades"] if t["status"] == "OPEN")
        
        total_capital = cfg["account"]["capital"]
        deployed = sum(t.get("capital_deployed", 0) or 0 for t in db["trades"] if t["status"] == "OPEN")
        margin_used_pct = deployed / total_capital if total_capital > 0 else 0
        today_trade_count = len([t for t in db["trades"] if t.get("entry_date") == today_str])

        for alert in alerts:
            if today_trade_count + len(entered) >= max_trades_per_day: break
            if len(entered) >= max_new_entries_per_cycle: break

            sym = alert.get("symbol", "")
            conf = alert.get("confidence", "MEDIUM")
            direction = alert.get("direction", "LONG")
            regime = alert.get("source_regime", "UNKNOWN")
            flow_bias = alert.get("flow_bias", "NEUTRAL")

            if sym in ("NIFTY", "BANKNIFTY", "FINNIFTY") or alert.get("type") == "INDEX":
                journal.log_skipped_trade(sym, direction, conf, "INDEX_BYPASS", regime, flow_bias, "equity_worker", "Index handled by options worker")
                continue
            if direction not in ("LONG", "SHORT"):
                journal.log_skipped_trade(sym, direction, conf, "NOT_DIRECTIONAL", regime, flow_bias, "paper_equity_only", alert.get("entry_trigger", ""))
                continue
            
            if conf_levels.get(conf, 2) < min_val:
                journal.log_skipped_trade(sym, direction, conf, "CONFIDENCE_FILTER", regime, flow_bias, f"min_conf={min_conf}", f"Alert {conf} < {min_conf}")
                continue
                
            rules = db.get("learned_filters", {}).get("rules", [])
            if rules:
                try:
                    from intelligence.learner import apply_learned_filter
                    decision, reason = apply_learned_filter(alert, rules)
                    if decision == "BLOCK":
                        journal.log_skipped_trade(sym, direction, conf, "LEARNED_FILTER", regime, flow_bias, reason, "Blocked by intelligence")
                        continue
                    if decision == "DOWNGRADE":
                        old_conf = conf
                        if conf == "HIGH": conf = "MEDIUM"
                        elif conf == "MEDIUM": conf = "LOW"
                        if conf_levels.get(conf, 1) < min_val:
                            journal.log_skipped_trade(sym, direction, old_conf, "LEARNED_DOWNGRADE", regime, flow_bias, reason, f"Downgraded to {conf}")
                            continue
                        alert["confidence"] = conf
                except Exception: pass

            if sym in today_symbols:
                journal.log_skipped_trade(sym, direction, conf, "DUPLICATE_TODAY", regime, flow_bias, "daily_dedup", f"Already traded {sym}")
                continue

            proposed_risk = alert.get("risk_rupees", 0) or 0
            gate_result = guardrails.check_new_trade(
                proposed_risk=proposed_risk,
                open_risk=open_risk,
                day_pnl=today_pnl,
                month_pnl=month_pnl,
                margin_used_pct=margin_used_pct
            )
            if not gate_result.ok:
                journal.log_skipped_trade(sym, direction, conf, "RISK_GATE", regime, flow_bias, "; ".join(gate_result.reasons), f"Risk: ₹{proposed_risk:,.0f}")
                continue

            entry_price = alert.get("entry_price")
            stop = alert.get("stop")

            # Fallback for missing prices (common for stock alerts)
            if entry_price is None or entry_price <= 0:
                entry_price = _get_ltp(sym)
                if entry_price is None or entry_price <= 0:
                    journal.log_skipped_trade(sym, direction, conf, "PRICE_ERROR", regime, flow_bias, "LTP_FETCH_FAILED", f"Could not get LTP for {sym}")
                    continue
                alert["entry_price"] = entry_price

            if stop is None or stop <= 0:
                sl_pct = settings.get("sl_pct", 2.0)
                if direction == "LONG":
                    stop = round(entry_price * (1 - sl_pct / 100), 2)
                else:
                    stop = round(entry_price * (1 + sl_pct / 100), 2)
                alert["stop"] = stop

            if entry_price > 0 and stop > 0 and entry_price != stop:
                size_result = directional_size(total_capital, cfg["risk"]["per_trade_pct"], entry_price, stop, 1)
                
                # Cap quantity by Capital per Trade setting (Notional cap)
                capital_cap = settings.get("capital_per_trade", 500000.0)
                max_qty_by_cap = int(capital_cap / entry_price)
                
                final_qty = min(size_result.qty, max_qty_by_cap)
                
                if final_qty > 0:
                    alert["qty"] = final_qty
                    alert["risk_rupees"] = round(final_qty * abs(entry_price - stop), 2)
                else:
                    journal.log_skipped_trade(sym, direction, conf, "SIZE_ERROR", regime, flow_bias, "ZERO_QTY", f"Size calculation returned 0 for {sym}")
                    continue
            else:
                journal.log_skipped_trade(sym, direction, conf, "PRICE_ERROR", regime, flow_bias, "INVALID_PRICES", f"Entry: {entry_price}, Stop: {stop}")
                continue

            sl_pct = settings.get("sl_pct", 2.0)
            tgt_pct = settings.get("tgt_pct", 4.0)
            if direction == "LONG":
                tgt_price = round(entry_price * (1 + tgt_pct / 100), 2)
            else:
                tgt_price = round(entry_price * (1 - tgt_pct / 100), 2)

            new_id = max([t["id"] for t in db["trades"]] + [0]) + 1
            final_qty = alert.get("qty", 0)
            new_trade = {
                "id": new_id, "symbol": sym, "direction": direction, "status": "OPEN",
                "entry_price": entry_price, "qty": final_qty,
                "capital_deployed": round(entry_price * final_qty, 2),
                "sl_price": stop, "sl_pct": sl_pct,
                "tgt_price": tgt_price, "tgt_pct": tgt_pct,
                "entry_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                "entry_date": today_str, "confidence": conf, "risk_rupees": alert.get("risk_rupees", 0),
                "source_regime": regime, "flow_bias": flow_bias, "hold_minutes": None
            }

            # Sync with SQLite Journal
            try:
                journal = Journal(cfg["paths"]["journal_db"])
                jid = journal.open_trade(
                    symbol=sym,
                    structure="STOCK",
                    side=direction,
                    qty=final_qty,
                    entry=entry_price,
                    stop=stop,
                    target=tgt_price,
                    risk_rupees=new_trade["risk_rupees"],
                    regime=regime,
                    notes=f"Auto-entry: {alert.get('entry_trigger', '')}"
                )
                new_trade["journal_id"] = jid
            except Exception as e:
                print(f"⚠️ Journal sync failed for {sym}: {e}")

            db["trades"].append(new_trade)
            entered.append(new_trade)
            today_symbols.add(sym)
            open_risk += new_trade["risk_rupees"]
    return entered

def generate_eod_summary(target_date: str | None = None) -> dict:
    """Generate end-of-day P&L summary and strategy analysis."""
    today = target_date or date.today().isoformat()
    check_and_close_trades()

    with atomic_db_update() as db:
        day_trades = [t for t in db["trades"] if t.get("entry_date") == today]
        closed_today = [t for t in day_trades if t["status"] == "CLOSED"]
        
        winners = [t for t in closed_today if (t.get("pnl") or 0) > 0]
        losers = [t for t in closed_today if (t.get("pnl") or 0) < 0]
        total_pnl = sum(t.get("pnl", 0) or 0 for t in closed_today)
        
        summary = {
            "date": today,
            "generated_at": _now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
            "total_trades": len(day_trades),
            "closed": len(closed_today),
            "winners": len(winners),
            "losers": len(losers),
            "total_pnl": round(total_pnl, 2),
            "trades": closed_today,
            "cumulative_pnl": round(db.get("cumulative_pnl", 0) + total_pnl, 2)
        }
        db["cumulative_pnl"] = summary["cumulative_pnl"]
        db["daily_summaries"].append(summary)
        return summary

def get_stats() -> dict:
    db = _load_db()
    all_closed = [t for t in db["trades"] if t["status"] == "CLOSED"]
    resolved = [t for t in all_closed if (t.get("pnl") or 0) != 0]
    winners = [t for t in resolved if (t.get("pnl") or 0) > 0]
    return {
        "total_trades": len(db["trades"]),
        "open_trades": len([t for t in db["trades"] if t["status"] == "OPEN"]),
        "cumulative_pnl": round(sum(t.get("pnl", 0) or 0 for t in all_closed), 2),
        "overall_win_rate": round(len(winners) / max(len(resolved), 1) * 100, 1)
    }

def get_open_trades() -> list[dict]: return [t for t in _load_db()["trades"] if t["status"] == "OPEN"]
def get_all_trades(limit: int = 50) -> list[dict]: return list(reversed(_load_db()["trades"][-limit:]))
def get_daily_summaries(limit: int = 30) -> list[dict]: return list(reversed(_load_db()["daily_summaries"][-limit:]))
