r"""Paper Trading Engine for StockMinded.

Manages simulated trades with entry/exit criteria, tracks P&L,
and generates end-of-day analysis with strategy corrections.

Data persisted to dashboard/paper_trades.json.
"""
from __future__ import annotations

import json
import msvcrt
import os
import contextlib
import threading
import traceback
import time
from datetime import datetime, date, timezone, timedelta
import datetime as dt_mod # Use this for the time class if needed, or just datetime.time
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
LOCK_FILE = DATA_FILE.with_suffix(".json.lock")
BAK_FILE = DATA_FILE.with_suffix(".json.bak")
TMP_FILE = DATA_FILE.with_suffix(".json.tmp")

DEFAULT_SETTINGS = {
    "capital_per_trade": 500000.0,
    "sl_pct": 2.0,
    "tgt_pct": 4.0,
    "trail_sl": True,
    "min_confidence": "HIGH",
    "max_trades_per_day": 8,
    "max_new_entries_per_cycle": 5,
    "regime_filter": True,
    "auto_close_eod": True,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # ── Option Trades Target & Stoploss ───────────────────────────
    "options_sl_pct": 125.0,         # Default 125% of net credit
    "options_tgt_pct": 50.0,         # Default 50% of net credit
    # ── Smart Exits (Intelligent Option Exit Engine) ──────────────
    "smart_exits_enabled": True,             # Master toggle
    "smart_exit_vix_spike_pct": 10.0,        # VIX spike threshold %
    "smart_exit_delta_threshold": 0.35,      # Net delta danger zone
    "smart_exit_trail_lock_pct": 30.0,       # Trail lock activation (% of max profit)
    "smart_exit_trail_floor_pct": 20.0,      # Trail lock floor (% of max profit)
    "smart_reentry_enabled": False,          # Re-entry (off by default)
    # ── Risk Gate (Guardrails) overrides ──────────────────────────
    "rg_daily_stop_pct": 0.02,       # 2% of capital = daily loss limit
    "rg_monthly_stop_pct": 0.06,     # 6% of capital = monthly loss limit
    "rg_concurrent_open_pct": 0.03,  # 3% of capital = max simultaneous open risk
    "rg_margin_util_cap": 0.60,      # 60% margin utilisation ceiling
    "rg_correlation_max": 0.70,      # max RS correlation with existing position
}

def _load_db() -> dict:
    """Load the full trade database (Read Only)."""
    for path in [DATA_FILE, BAK_FILE]:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
    return {
        "trades": [],
        "option_trades": [],
        "daily_summaries": [],
        "strategy_notes": [],
        "settings": DEFAULT_SETTINGS.copy(),
        "cumulative_pnl": 0.0,
        "version": 1,
    }

def _save_db(db: dict) -> None:
    """Save the trade database to disk."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TMP_FILE, "w", encoding='utf-8') as f:
        json.dump(db, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    
    # Create backup before replacing
    if DATA_FILE.exists():
        if BAK_FILE.exists():
            os.remove(BAK_FILE)
        os.rename(DATA_FILE, BAK_FILE)
    
    # Atomic rename
    os.rename(TMP_FILE, DATA_FILE)

@contextlib.contextmanager
def atomic_db_update():
    """Transaction-style database update with Windows cross-process locking and retries."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    # Ensure lock file exists
    if not LOCK_FILE.exists():
        LOCK_FILE.touch()

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

    locked = False
    lock_f = None
    for attempt in range(10): # Increased retries
        try:
            lock_f = open(LOCK_FILE, "r+")
            # Try to acquire lock on the lock file
            msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
            
            db = _load_db()

            yield db
            
            _save_db(db)
            break # Success
        except (OSError, IOError) as e:
            if attempt < 9:
                time.sleep(0.1 * (attempt + 1))
                continue
            else:
                raise
        finally:
            if locked and lock_f:
                try:
                    lock_f.seek(0)
                    msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)
                except: pass
            if lock_f:
                try: lock_f.close()
                except: pass

MARKET_OPEN = dt_mod.time(9, 15)
MARKET_CLOSE = dt_mod.time(15, 30)
EOD_WINDOW_START = dt_mod.time(15, 25)
EOD_WINDOW_END = dt_mod.time(15, 35)

# Option SL grace period: don't check SL/TGT within N minutes of entry.
# Prevents false exits from bid-ask spread jitter on illiquid OTM strikes.
OPTION_SL_GRACE_MINUTES = 5

# Equity entry window: avoid open whipsaw and late-day insufficient-time entries
EQUITY_ENTRY_START = dt_mod.time(10, 0)
EQUITY_ENTRY_END = dt_mod.time(14, 15)


def _within_grace_period(trade: dict, now: datetime, grace_minutes: int = OPTION_SL_GRACE_MINUTES) -> bool:
    """True if trade was entered within the last `grace_minutes` — skip SL/TGT checks."""
    entry_time_str = trade.get("entry_time")
    if not entry_time_str:
        return False
    try:
        entry_dt = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)
        # Ensure now is also timezone-aware
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        elapsed = (now - entry_dt).total_seconds()
        return elapsed < grace_minutes * 60
    except (ValueError, TypeError):
        return False

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
                if k in ("capital_per_trade", "sl_pct", "tgt_pct",
                          "rg_daily_stop_pct", "rg_monthly_stop_pct",
                          "rg_concurrent_open_pct", "rg_margin_util_cap",
                          "rg_correlation_max", "options_sl_pct", "options_tgt_pct",
                          "smart_exit_vix_spike_pct", "smart_exit_delta_threshold",
                          "smart_exit_trail_lock_pct", "smart_exit_trail_floor_pct"):
                    if v is not None and str(v).strip() != "":
                        try:
                            db["settings"][k] = float(v)
                        except (ValueError, TypeError):
                            pass
                elif k in ("max_trades_per_day", "max_new_entries_per_cycle"):
                    if v is not None and str(v).strip() != "":
                        try:
                            db["settings"][k] = int(v)
                        except (ValueError, TypeError):
                            pass
                elif k in ("trail_sl", "regime_filter", "auto_close_eod",
                           "smart_exits_enabled", "smart_reentry_enabled"):
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
            "peak_price": entry_price,
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
        open_ops = [t for t in db.get("option_trades", []) if t.get("status") == "OPEN"]
        max_ops = cfg.get("options", {}).get("max_concurrent_structures", 4)
        if len(open_ops) >= max_ops: return {"error": f"Max concurrent options structures ({max_ops}) reached"}
        
        # Prevent SL infinite loops: max 1 entry per structure per symbol per day
        today_str = date.today().isoformat()
        todays_trades = [t for t in db["option_trades"] if t.get("symbol") == underlying and t.get("structure") == structure_name and t.get("entry_date") == today_str]
        if todays_trades: return {"error": f"Already traded {structure_name} for {underlying} today"}

        net_premium = sum((leg.premium * leg.lots * leg.lot_size) * (1 if leg.side == "SELL" else -1) for leg in resolved_legs)
        if net_premium <= 0:
            return {"error": "Non-credit structure (0 premium likely due to missing LTP data)"}
        # Smart exit metadata
        short_strikes = [l.strike for l in resolved_legs if l.side == "SELL"]
        try:
            from data import feed
            vix_df = feed.ohlc_cached("INDIAVIX", period="5d")
            entry_vix = float(vix_df["close"].iloc[-1]) if vix_df is not None and not vix_df.empty else 0.0
        except Exception:
            entry_vix = 0.0
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
            "exit_time": None, "exit_reason": None, "pnl": None, "status": "OPEN",
            # Smart exit tracking
            "entry_vix": round(entry_vix, 2),
            "short_strikes": short_strikes,
            "wing_width": 0.0,
            "peak_pnl": 0.0,
            "trailing_lock": False,
            "reentry_eligible": False,
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
        open_nifty = [t for t in db["option_trades"] if t.get("status") == "OPEN" and t.get("symbol") == "NIFTY"]
        if len(open_nifty) > 0: return {"error": "NIFTY option structure already open"}
        
        # Prevent SL infinite loops: max 1 entry per structure today
        today_str = date.today().isoformat()
        todays_trades = [t for t in db["option_trades"] if t.get("symbol") == "NIFTY" and t.get("structure") == setup.strategy and t.get("entry_date") == today_str]
        if todays_trades: return {"error": f"Already traded {setup.strategy} for NIFTY today"}
        
        net_credit = sum((l.premium * l.lots * l.lot_size) * (1 if l.side == "SELL" else -1) for l in resolved_legs)
        if net_credit <= 0:
            journal.log_skipped_trade("NIFTY", "NEUTRAL", "MED", "NO_CREDIT", "UNKNOWN", "NEUTRAL", "options_gate", "Non-credit structure")
            return {"error": f"NIFTY blocked: non-credit"}
        
        lot_size = resolved_legs[0].lot_size if resolved_legs else 1
        struct_type = "iron_condor" if "IRON_CONDOR" in setup.strategy else ("bull_put_spread" if "BULL_PUT" in setup.strategy else "bear_call_spread")
        # Pass current spot price for dynamic max-loss calculation (fixes hardcoded ₹250k naked short)
        underlying_spot = setup.spot if hasattr(setup, "spot") and setup.spot else None
        max_loss = calc_structure_max_loss(struct_type, net_credit, setup.wing_width, lot_size, underlying_spot=underlying_spot)
        # Smart exit metadata
        short_strikes = [l.strike for l in resolved_legs if l.side == "SELL"]
        try:
            from data import feed
            vix_df = feed.ohlc_cached("INDIAVIX", period="5d")
            entry_vix = float(vix_df["close"].iloc[-1]) if vix_df is not None and not vix_df.empty else setup.vix
        except Exception:
            entry_vix = setup.vix
        
        trade = {
            "id": _next_id(db), "symbol": "NIFTY", "structure": setup.strategy, "mode": setup.mode,
            "legs": [{
                "side": l.side, "type": l.type, "strike": l.strike, "expiry": l.expiry,
                "qty": l.lots * l.lot_size, "entry_premium": l.premium, "exit_premium": None
            } for l in resolved_legs],
            "net_credit": round(net_credit, 2), "net_premium": round(net_credit, 2), "max_loss_rupees": round(max_loss, 2),
            "entry_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"), "entry_date": date.today().isoformat(),
            "status": "OPEN", "pnl": None, "exit_reason": None, "exit_time": None,
            # Smart exit tracking
            "entry_vix": round(entry_vix, 2),
            "short_strikes": short_strikes,
            "wing_width": setup.wing_width,
            "peak_pnl": 0.0,
            "trailing_lock": False,
            "reentry_eligible": False,
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

def _get_current_vix() -> float:
    """Fetch current VIX value. Returns 0.0 on failure."""
    try:
        from data import feed
        vix_df = feed.ohlc_cached("INDIAVIX", period="5d")
        if vix_df is not None and not vix_df.empty:
            col = "close" if "close" in vix_df.columns else "Close"
            return float(vix_df[col].iloc[-1])
    except Exception:
        pass
    return 0.0

def _smart_exit_check(t: dict, current_net: float | None, settings: dict, vix_now: float = 0.0) -> str | None:
    """Run all smart exit signals on a single option trade.
    
    Returns exit_reason string or None if no exit triggered.
    Signals checked in priority order:
      1. VIX Spike Exit
      2. Underlying Strike Breach
      3. Delta Breach (skipped here — needs chain, done in caller)
      4. Theta Trail Lock
    """
    if not settings.get("smart_exits_enabled", True):
        return None
    
    entry_premium = t.get("net_premium") or t.get("net_credit") or 0.0
    if current_net is None:
        return None
    
    pnl = entry_premium - current_net
    
    # 1. VIX Spike Exit
    entry_vix = t.get("entry_vix", 0.0)
    vix_threshold = settings.get("smart_exit_vix_spike_pct", 10.0)
    if entry_vix > 0 and vix_now > 0:
        from signals.options import check_vix_spike_exit
        should_exit, _ = check_vix_spike_exit(vix_now, entry_vix, vix_threshold)
        if should_exit:
            t["reentry_eligible"] = True
            return "VIX_SPIKE"
    
    # 2. Underlying Strike Breach
    short_strikes = t.get("short_strikes", [])
    wing_width = t.get("wing_width", 0.0)
    if short_strikes and wing_width > 0:
        try:
            ltp = _get_ltp(t.get("symbol", "NIFTY"))
            if ltp and ltp > 0:
                highest_short = max(short_strikes)
                lowest_short = min(short_strikes)
                breach_margin = wing_width * 0.5
                if ltp > highest_short + breach_margin or ltp < lowest_short - breach_margin:
                    return "STRIKE_BREACH"
        except Exception:
            pass
    
    # 3. Theta Trail Lock
    trail_lock_pct = settings.get("smart_exit_trail_lock_pct", 30.0) / 100.0
    trail_floor_pct = settings.get("smart_exit_trail_floor_pct", 20.0) / 100.0
    
    # Update peak PnL
    peak_pnl = t.get("peak_pnl", 0.0)
    if pnl > peak_pnl:
        t["peak_pnl"] = pnl
        peak_pnl = pnl
    
    lock_threshold = abs(entry_premium) * trail_lock_pct
    if peak_pnl >= lock_threshold:
        t["trailing_lock"] = True
    
    if t.get("trailing_lock", False):
        lock_floor = abs(entry_premium) * trail_floor_pct
        if pnl < lock_floor:
            return "TRAIL_LOCK"
    
    return None

def check_option_exits() -> list[dict]:
    now_ist = _now_ist()
    if not is_market_open(now_ist) and not is_eod_window(now_ist): return []
    closed = []
    vix_now = _get_current_vix()
    with atomic_db_update() as db:
        open_ops = [t for t in db.get("option_trades", []) if t.get("status") == "OPEN"]
        if not open_ops: return []
        price_map = _build_option_price_map(open_ops)
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True)
        is_eod = is_eod_window(now_ist)
        smart_enabled = settings.get("smart_exits_enabled", True)
        
        # Fetch chain snapshots for delta breach check
        chain_cache = {}
        if smart_enabled:
            from signals.options import chain_snapshot, net_position_delta
            for sym in {t["symbol"] for t in open_ops}:
                try:
                    chain_cache[sym] = chain_snapshot(sym)
                except Exception:
                    pass
        
        for t in open_ops:
            current_net = _option_net_premium(t["legs"], price_map)
            exit_reason = "EOD_CLOSE" if (is_eod and auto_close) else None
            
            # Smart exits (fire first — structural danger)
            if not exit_reason:
                exit_reason = _smart_exit_check(t, current_net, settings, vix_now)
            
            # Delta Breach (needs chain data)
            if not exit_reason and smart_enabled and current_net is not None:
                chain = chain_cache.get(t.get("symbol"))
                if chain is not None and not chain.empty:
                    delta_threshold = settings.get("smart_exit_delta_threshold", 0.35)
                    nd = net_position_delta(t["legs"], chain)
                    if nd is not None and abs(nd) > delta_threshold:
                        t["reentry_eligible"] = True
                        exit_reason = "DELTA_BREACH"
            
            # Flat SL/TGT backup (skip during grace period to avoid bid-ask jitter exits)
            if not exit_reason and current_net is not None and not _within_grace_period(t, now_ist):
                pnl = (t.get("net_premium") or 0.0) - current_net
                sl_limit = settings.get("options_sl_pct", 125.0) / 100.0
                tgt_limit = settings.get("options_tgt_pct", 50.0) / 100.0
                if pnl <= -abs(t.get("net_premium", 1000)) * sl_limit: exit_reason = "SL_HIT"
                elif pnl >= abs(t.get("net_premium", 1000)) * tgt_limit: exit_reason = "TGT_HIT"
            
            if exit_reason:
                t["status"], t["exit_reason"], t["exit_time"] = "CLOSED", exit_reason, now_ist.strftime("%Y-%m-%d %H:%M:%S")
                t["pnl"] = round((t.get("net_premium") or 0.0) - current_net, 2) if current_net is not None else 0.0
                closed.append(t)
    return closed

def check_nifty_option_exits(vix_current: float = None, cfg: dict = None) -> list[dict]:
    from signals.options import is_expiry_day, is_within_exit_window
    now_ist = _now_ist()
    if not is_market_open(now_ist) and not is_eod_window(now_ist): return []
    closed = []
    vix_now = vix_current or _get_current_vix()
    with atomic_db_update() as db:
        open_nifty = [t for t in db.get("option_trades", []) if t.get("status") == "OPEN" and t.get("symbol") == "NIFTY"]
        if not open_nifty: return []
        price_map = _build_option_price_map(open_nifty)
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True)
        is_eod = is_eod_window(now_ist)
        smart_enabled = settings.get("smart_exits_enabled", True)

        # Fetch chain for delta breach
        chain = None
        if smart_enabled:
            from signals.options import chain_snapshot, net_position_delta
            try:
                chain = chain_snapshot("NIFTY")
            except Exception:
                pass

        for t in open_nifty:
            exit_reason = None
            if is_eod and auto_close: exit_reason = "EOD_CUTOFF"
            current_net = _option_net_premium(t["legs"], price_map)
            
            # Smart exits (fire first)
            if not exit_reason:
                exit_reason = _smart_exit_check(t, current_net, settings, vix_now)
            
            # Delta Breach
            if not exit_reason and smart_enabled and current_net is not None and chain is not None and not chain.empty:
                delta_threshold = settings.get("smart_exit_delta_threshold", 0.35)
                nd = net_position_delta(t["legs"], chain)
                if nd is not None and abs(nd) > delta_threshold:
                    t["reentry_eligible"] = True
                    exit_reason = "DELTA_BREACH"
            
            # Flat SL/TGT backup (skip during grace period to avoid bid-ask jitter exits)
            if not exit_reason and current_net is not None and not _within_grace_period(t, now_ist):
                pnl = t.get("net_credit", 0.0) - current_net
                sl_limit = settings.get("options_sl_pct", 125.0) / 100.0
                tgt_limit = settings.get("options_tgt_pct", 50.0) / 100.0
                if pnl >= t.get("net_credit", 0) * tgt_limit: exit_reason = "PROFIT_TAKEN"
                elif pnl <= -t.get("net_credit", 0) * sl_limit: exit_reason = "STOP_LOSS"
            
            if exit_reason:
                t["status"], t["exit_reason"], t["exit_time"] = "CLOSED", exit_reason, now_ist.strftime("%Y-%m-%d %H:%M:%S")
                t["pnl"] = round(t.get("net_credit", 0.0) - current_net, 2) if current_net is not None else 0.0
                closed.append(t)
    return closed

def scan_reentry_candidates(data: dict, cfg: dict = None) -> list[dict]:
    """Scan recently closed eligible trades and re-enter if conditions normalize."""
    if cfg is None:
        from config.loader import load_config
        cfg = load_config()
    now_ist = _now_ist()
    if not is_market_open(now_ist): return []
    
    db = _load_db()
    settings = db.get("settings", {})
    if not settings.get("smart_reentry_enabled", False): return []
    
    today_str = date.today().isoformat()
    eligible = [t for t in db.get("option_trades", []) 
                if t.get("reentry_eligible") and t.get("status") == "CLOSED" 
                and t.get("entry_date") == today_str
                and t.get("exit_reason") in ("VIX_SPIKE", "DELTA_BREACH")]
    
    if not eligible: return []
    
    # Check if VIX has stabilized
    vix_now = _get_current_vix()
    regime = data.get("regime", {})
    regime_name = regime.get("regime") or regime.get("name") or "UNKNOWN"
    if regime_name == "VOL_EXPANSION": return []
    
    reentered = []
    for t in eligible:
        entry_vix = t.get("entry_vix", 0.0)
        if entry_vix > 0 and vix_now > entry_vix * 1.05:
            continue  # VIX still elevated
        
        # Check we haven't already re-entered this structure today
        already_today = any(
            x.get("symbol") == t["symbol"] and x.get("structure") == t.get("structure") 
            and x.get("entry_date") == today_str and x.get("status") == "OPEN"
            for x in db.get("option_trades", [])
        )
        if already_today: continue
        
        # Mark as no longer eligible to prevent loops
        with atomic_db_update() as db2:
            for x in db2.get("option_trades", []):
                if x.get("id") == t["id"]:
                    x["reentry_eligible"] = False
        
        reentered.append({"symbol": t["symbol"], "structure": t.get("structure"), "original_id": t["id"]})
    
    return reentered

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

    # 1. READ open symbols first OUTSIDE the lock/transaction to avoid holding it during network calls
    db = _load_db()
    open_trades = [t for t in db.get("trades", []) if t.get("status") == "OPEN"]
    if not open_trades: return []

    symbols = list(set(t["symbol"] for t in open_trades))
    
    # 2. FETCH prices OUTSIDE the lock
    prices = _get_ltp_batch(symbols)

    closed = []
    # 3. Enter transaction lock ONLY for local fast DB mutations
    with atomic_db_update() as db:
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True) # Default to True if missing
        
        # Reload open trades from the locked db copy to ensure fresh transaction state
        open_trades = [t for t in db.get("trades", []) if t.get("status") == "OPEN"]
        if not open_trades: return []

        for trade in open_trades:
            ltp = prices.get(trade["symbol"])
            if ltp is None: continue

            exit_reason = None
            direction = trade["direction"]

            if any(k not in trade for k in ["sl_price", "tgt_price", "direction"]):
                continue

            # Trailing stop logic (lock in 50% max gains)
            if settings.get("trail_sl", True):
                if "peak_price" not in trade:
                    trade["peak_price"] = trade["entry_price"]
                if direction == "LONG":
                    if ltp > trade["peak_price"]:
                        trade["peak_price"] = ltp
                        new_sl = trade["entry_price"] + (ltp - trade["entry_price"]) * 0.5
                        if new_sl > trade["sl_price"]:
                            trade["sl_price"] = round(new_sl, 2)
                else:
                    if ltp < trade["peak_price"]:
                        trade["peak_price"] = ltp
                        new_sl = trade["entry_price"] - (trade["entry_price"] - ltp) * 0.5
                        if new_sl < trade["sl_price"]:
                            trade["sl_price"] = round(new_sl, 2)

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
        trade = next((t for t in db["trades"] if t["id"] == trade_id and t.get("status") == "OPEN"), None)
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

def close_option_trade_manual(trade_id: int, reason: str = "MANUAL") -> dict | None:
    """Manually close a specific option trade at current LTP."""
    now_ist = _now_ist()
    with atomic_db_update() as db:
        if "option_trades" not in db: return None
        trade = next((t for t in db["option_trades"] if t["id"] == trade_id and t.get("status") == "OPEN"), None)
        if not trade: return None

        trade["status"] = "CLOSED"
        trade["exit_time"] = now_ist.strftime("%Y-%m-%d %H:%M:%S")
        trade["exit_reason"] = reason

        # Fetch LTPs to get accurate final premiums
        price_map = _build_option_price_map([trade])
        current_net = _option_net_premium(trade["legs"], price_map)

        # Update exit_premium for each leg
        for leg in trade["legs"]:
            key = (leg["strike"], leg["expiry"], leg["type"])
            price = price_map.get(key) if price_map else None
            leg["exit_premium"] = price

        # Compute PnL
        entry_net = trade.get("net_premium") or trade.get("net_credit") or 0.0
        if current_net is not None:
            trade["pnl"] = round(entry_net - current_net, 2)
        else:
            trade["pnl"] = 0.0

        return trade

def auto_enter_from_alerts(alerts: list[dict], cfg: dict | None = None) -> list[dict]:
    """Take paper trades on alerts automatically with risk guardrails and learned filters."""
    from config.loader import load_config
    now_ist = _now_ist()
    if not is_market_open(now_ist): return []
    if now_ist.hour == 15 and now_ist.minute >= 15: return []

    # Equity entry time window: avoid open whipsaw (09:15-10:00) and
    # late-day entries with insufficient time for target (after 14:15).
    t_now = now_ist.timetz().replace(tzinfo=None)
    if t_now < EQUITY_ENTRY_START or t_now > EQUITY_ENTRY_END:
        return []

    if cfg is None: cfg = load_config()

    # Merge UI-saved risk gate overrides into a copy of cfg so Guardrails
    # picks them up without touching the global config file.
    _saved = get_settings()
    _risk_override = dict(cfg.get("risk", {}))
    _risk_override["daily_stop_pct"]      = _saved.get("rg_daily_stop_pct",      _risk_override.get("daily_stop_pct", 0.02))
    _risk_override["monthly_stop_pct"]    = _saved.get("rg_monthly_stop_pct",    _risk_override.get("monthly_stop_pct", 0.06))
    _risk_override["concurrent_open_pct"] = _saved.get("rg_concurrent_open_pct", _risk_override.get("concurrent_open_pct", 0.03))
    _risk_override["margin_util_cap"]     = _saved.get("rg_margin_util_cap",     _risk_override.get("margin_util_cap", 0.60))
    _risk_override["correlation_max"]     = _saved.get("rg_correlation_max",     _risk_override.get("correlation_max", 0.70))
    _cfg_override = {**cfg, "risk": _risk_override}
    guardrails = Guardrails(_cfg_override)
    journal = Journal(cfg["paths"]["journal_db"])
    today_str = date.today().isoformat()
    entered = []

    # Pre-fetch LTP for any alerts that lack entry_price/stop OUTSIDE the lock
    missing_symbols = []
    for alert in alerts:
        ep = alert.get("entry_price")
        if ep is None or ep <= 0:
            missing_symbols.append(alert.get("symbol"))
    if missing_symbols:
        prefetched_prices = _get_ltp_batch(list(set(missing_symbols)))
    else:
        prefetched_prices = {}

    with atomic_db_update() as db:
        settings = db.get("settings", DEFAULT_SETTINGS.copy())
        min_conf = settings.get("min_confidence", "HIGH")
        max_trades_per_day = int(settings.get("max_trades_per_day", 8))
        max_new_entries_per_cycle = int(settings.get("max_new_entries_per_cycle", 5))
        conf_levels = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        min_val = conf_levels.get(min_conf, 3)

        today_symbols = {t["symbol"] for t in db["trades"] if t.get("entry_date") == today_str}
        all_closed = [t for t in db["trades"] if t.get("status") == "CLOSED"]
        today_pnl = sum(t.get("pnl", 0) or 0 for t in all_closed if t.get("entry_date") == today_str)
        month_pnl = sum(t.get("pnl", 0) or 0 for t in all_closed if t.get("entry_date", "")[:7] == today_str[:7])
        open_risk = sum(t.get("risk_rupees", 0) or 0 for t in db["trades"] if t.get("status") == "OPEN")
        
        total_capital = cfg["account"]["capital"]
        deployed = sum(t.get("capital_deployed", 0) or 0 for t in db["trades"] if t.get("status") == "OPEN")
        margin_used_pct = deployed / total_capital if total_capital > 0 else 0
        today_trade_count = len([t for t in db["trades"] if t.get("entry_date") == today_str])
        open_trades_count = len([t for t in db["trades"] if t.get("status") == "OPEN"])

        for alert in alerts:
            if today_trade_count + len(entered) >= max_trades_per_day: break
            if len(entered) >= max_new_entries_per_cycle: break

            regime = alert.get("source_regime", "UNKNOWN")
            if "RANGE" in regime.upper() and (open_trades_count + len(entered) >= 3):
                journal.log_skipped_trade(alert.get("symbol", ""), alert.get("direction", "LONG"), alert.get("confidence", "MEDIUM"), "REGIME_CAP", regime, alert.get("flow_bias", "NEUTRAL"), "regime_filter", "Max 3 concurrent positions in RANGE regime")
                continue

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

            entry_price = alert.get("entry_price")
            stop = alert.get("stop")

            # Fallback for missing prices (common for stock alerts)
            if entry_price is None or entry_price <= 0:
                entry_price = prefetched_prices.get(sym) or _get_ltp(sym)
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
                try:
                    size_result = directional_size(
                        total_capital, 
                        cfg["risk"]["per_trade_pct"], 
                        entry_price, 
                        stop, 
                        1, 
                        direction=direction
                    )
                except ValueError as ve:
                    journal.log_skipped_trade(sym, direction, conf, "PRICE_ERROR", regime, flow_bias, "INVALID_STOP_RELATION", str(ve))
                    continue
                
                # Cap quantity by Capital per Trade setting (Notional cap)
                capital_cap = settings.get("capital_per_trade", 500000.0)
                max_qty_by_cap = int(capital_cap / entry_price)
                
                final_qty = min(size_result.qty, max_qty_by_cap)
                
                if final_qty > 0:
                    alert["qty"] = final_qty
                    proposed_risk = round(final_qty * abs(entry_price - stop), 2)
                    alert["risk_rupees"] = proposed_risk
                else:
                    journal.log_skipped_trade(sym, direction, conf, "SIZE_ERROR", regime, flow_bias, "ZERO_QTY", f"Size calculation returned 0 for {sym}")
                    continue
            else:
                journal.log_skipped_trade(sym, direction, conf, "PRICE_ERROR", regime, flow_bias, "INVALID_PRICES", f"Entry: {entry_price}, Stop: {stop}")
                continue

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

            sl_pct = settings.get("sl_pct", 2.0)
            tgt_pct = settings.get("tgt_pct", 4.0)
            if direction == "LONG":
                tgt_price = round(entry_price * (1 + tgt_pct / 100), 2)
            else:
                tgt_price = round(entry_price * (1 - tgt_pct / 100), 2)

            new_id = max([t["id"] for t in db["trades"]] + [0]) + 1
            new_trade = {
                "id": new_id, "symbol": sym, "direction": direction, "status": "OPEN",
                "entry_price": entry_price, "qty": final_qty,
                "capital_deployed": round(entry_price * final_qty, 2),
                "sl_price": stop, "sl_pct": sl_pct,
                "tgt_price": tgt_price, "tgt_pct": tgt_pct,
                "peak_price": entry_price,
                "entry_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                "entry_date": today_str, "confidence": conf, "risk_rupees": proposed_risk,
                "planned_risk": alert.get("planned_risk", proposed_risk),
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
                    risk_rupees=proposed_risk,
                    regime=regime,
                    notes=f"Auto-enter: {alert.get('entry_trigger', '')}"
                )
                new_trade["journal_id"] = jid
            except Exception as e:
                print(f"⚠️ Journal sync failed for auto-enter {sym}: {e}")

            db["trades"].append(new_trade)
            today_symbols.add(sym)
            open_risk += proposed_risk
            entered.append(new_trade)

    return entered
