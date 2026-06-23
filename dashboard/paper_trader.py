r"""Paper Trading Engine for StockMinded.

Manages simulated trades with entry/exit criteria, tracks P&L,
and generates end-of-day analysis with strategy corrections.

Data persisted to dashboard/paper_trades.json.
"""

from __future__ import annotations

import contextlib
import datetime as dt_mod  # Use this for the time class if needed, or just datetime.time
import json
import logging
import msvcrt
import os
import threading
import time
import traceback
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

# Standardized IST timezone
IST = timezone(timedelta(hours=5, minutes=30))


def _now_ist() -> datetime:
    return datetime.now(IST)


# Import risk modules
from config.loader import load_config
from ops.journal import Journal
from risk.guardrails import Guardrails
from risk.sizing import SizeResult, directional_size

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
    "atr_multiplier": 2.0,  # Dynamic SL: multiplier for ATR
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # ── Option Trades Target & Stoploss ───────────────────────────
    "options_sl_pct": 125.0,  # Default 125% of net credit
    "options_tgt_pct": 50.0,  # Default 50% of net credit
    # ── Smart Exits (Intelligent Option Exit Engine) ──────────────
    "smart_exits_enabled": True,  # Master toggle
    "smart_exit_vix_spike_pct": 15.0,  # VIX spike threshold %
    "smart_exit_vix_floor": 18.0,  # Only exit if VIX > floor
    "smart_exit_delta_threshold": 0.35,  # Net delta danger zone
    "smart_exit_trail_lock_pct": 50.0,  # Trail lock activation (% of max profit)
    "smart_exit_trail_floor_pct": 35.0,  # Trail lock floor (% of max profit)
    "smart_reentry_enabled": True,  # Re-entry (off by default)
    "options_lots_per_trade": 10,  # Number of lots to trade per options order
    # ── Risk Gate (Guardrails) overrides ──────────────────────────
    "rg_daily_stop_pct": 0.05,  # 2% of capital = daily loss limit
    "rg_monthly_stop_pct": 0.08,  # 6% of capital = monthly loss limit
    "rg_concurrent_open_pct": 0.05,  # 3% of capital = max simultaneous open risk
    "rg_margin_util_cap": 0.80,  # 60% margin utilisation ceiling
    "rg_correlation_max": 0.70,  # max RS correlation with existing position
}

# F&O Stocks Lot Sizes Cache & Helpers
_fno_lot_sizes_cache = None


def get_fno_lot_sizes() -> dict[str, int]:
    global _fno_lot_sizes_cache
    if _fno_lot_sizes_cache is not None:
        return _fno_lot_sizes_cache

    csv_path = Path(__file__).parent.parent / "config" / "fno200.csv"
    res = {}
    try:
        import csv

        if csv_path.exists():
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    sym = row.get("symbol", "").strip()
                    lot = row.get("lot_size", "").strip()
                    if sym and lot:
                        res[sym] = int(lot)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "Could not load lot sizes from fno200.csv: %s", e
        )
    _fno_lot_sizes_cache = res
    return res


def get_futures_expiry(now_dt: datetime = None) -> str:
    import calendar
    from datetime import date, time, timedelta

    from signals.options import _is_holiday

    if now_dt is None:
        now_dt = datetime.now(IST)

    today = now_dt.date()
    year, month = today.year, today.month

    def last_thursday_of(y, m):
        cal = calendar.monthcalendar(y, m)
        for week in reversed(cal):
            thurs = week[calendar.THURSDAY]
            if thurs != 0:
                dt = date(y, m, thurs)
                while _is_holiday(dt) or dt.weekday() >= 5:
                    dt -= timedelta(days=1)
                return dt

    curr_expiry = last_thursday_of(year, month)

    if today > curr_expiry or (today == curr_expiry and now_dt.time() >= time(15, 30)):
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1
        curr_expiry = last_thursday_of(next_year, next_month)

    return curr_expiry.strftime("%Y-%m-%d")


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
        "settings": DEFAULT_SETTINGS.copy(),
        "cumulative_pnl": 0.0,
        "version": 1,
    }


def _save_db(db: dict) -> None:
    """Save the trade database to disk."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TMP_FILE, "w", encoding="utf-8") as f:
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
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "trades": [],
                    "option_trades": [],
                    "daily_summaries": [],
                    "settings": DEFAULT_SETTINGS.copy(),
                    "cumulative_pnl": 0.0,
                    "version": 1,
                },
                f,
                indent=2,
            )

    locked = False
    lock_f = None
    for attempt in range(10):  # Increased retries
        try:
            lock_f = open(LOCK_FILE, "r+")
            # Try to acquire lock on the lock file
            msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True

            db = _load_db()

            yield db

            _save_db(db)
            break  # Success
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
                except:
                    pass
            if lock_f:
                try:
                    lock_f.close()
                except:
                    pass


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


def _within_grace_period(
    trade: dict, now: datetime, grace_minutes: int = OPTION_SL_GRACE_MINUTES
) -> bool:
    """True if trade was entered within the last `grace_minutes` — skip SL/TGT checks."""
    entry_time_str = trade.get("entry_time")
    if not entry_time_str:
        return False
    try:
        entry_dt = datetime.strptime(entry_time_str, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=IST
        )
        # Ensure now is also timezone-aware
        if now.tzinfo is None:
            now = now.replace(tzinfo=IST)
        elapsed = (now - entry_dt).total_seconds()
        return elapsed < grace_minutes * 60
    except (ValueError, TypeError):
        return False


def is_market_open(now: datetime | None = None) -> bool:
    """True only on Mon-Fri between 09:15 and 15:30 IST, avoiding holidays."""
    n = now or _now_ist()
    if n.tzinfo is None:
        n = n.replace(tzinfo=IST)
    if n.weekday() >= 5:
        return False
    from signals.options import _is_holiday

    if _is_holiday(n.date()):
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
    from signals.options import _is_holiday

    if _is_holiday(n.date()):
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
                if k in (
                    "capital_per_trade",
                    "sl_pct",
                    "tgt_pct",
                    "rg_daily_stop_pct",
                    "rg_monthly_stop_pct",
                    "rg_concurrent_open_pct",
                    "rg_margin_util_cap",
                    "rg_correlation_max",
                    "options_sl_pct",
                    "options_tgt_pct",
                    "smart_exit_vix_spike_pct",
                    "smart_exit_delta_threshold",
                    "atr_multiplier",
                    "smart_exit_trail_lock_pct",
                    "smart_exit_trail_floor_pct",
                    "smart_exit_vix_floor",
                ):
                    if v is not None and str(v).strip() != "":
                        try:
                            db["settings"][k] = float(v)
                        except (ValueError, TypeError):
                            pass
                elif k in (
                    "max_trades_per_day",
                    "max_new_entries_per_cycle",
                    "options_lots_per_trade",
                ):
                    if v is not None and str(v).strip() != "":
                        try:
                            db["settings"][k] = int(v)
                        except (ValueError, TypeError):
                            pass
                elif k in (
                    "trail_sl",
                    "regime_filter",
                    "auto_close_eod",
                    "smart_exits_enabled",
                    "smart_reentry_enabled",
                ):
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
        if px:
            return px
    except Exception:
        pass
    try:
        yf_sym = (
            f"{symbol}.NS"
            if not symbol.startswith("^") and "." not in symbol
            else symbol
        )
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
        result = {
            s: (
                round(float(quotes[s]["ltp"]), 2)
                if quotes.get(s, {}).get("ltp")
                else None
            )
            for s in symbols
        }
        if any(v is not None for v in result.values()):
            missing = [s for s, v in result.items() if v is None]
            if not missing:
                return result
        else:
            missing = symbols
    except Exception:
        result = {}
        missing = symbols

    yf_syms = [
        f"{s}.NS" if not s.startswith("^") and "." not in s else s for s in missing
    ]
    try:
        tickers = yf.Tickers(" ".join(yf_syms))
        for sym, yf_s in zip(missing, yf_syms):
            try:
                info = tickers.tickers[yf_s].fast_info
                result[sym] = (
                    round(float(info.last_price), 2) if info.last_price else None
                )
            except Exception:
                result[sym] = None
    except Exception:
        for s in missing:
            result[s] = None
    return result


def enter_trade(alert: dict) -> dict:
    """Open a paper trade based on an alert signal."""
    if not is_market_open():
        return {"error": "Market closed (9:15-15:30 IST, Mon-Fri)"}

    with atomic_db_update() as db:
        symbol = alert["symbol"]
        direction = alert.get("direction", "LONG")
        today_str = _now_ist().date().isoformat()

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

        # Check if F&O Stock
        fno_lots = get_fno_lot_sizes()
        lot_size = fno_lots.get(symbol)

        if lot_size:
            trade_type = "FUTURE"
            instrument = f"{symbol} FUT"
            expiry_date = get_futures_expiry(_now_ist())
            qty_cap = int(capital_per_trade / entry_price) if entry_price > 0 else 0
            alert_qty = int(alert.get("qty") or 0)
            if alert_qty > 0:
                qty = min(qty_cap, alert_qty)
            else:
                qty = qty_cap
            lots = qty // lot_size
            qty = lots * lot_size
            if qty == 0:
                return {
                    "error": f"Price or lot size too high for Rs {capital_per_trade:,.0f} allocation (minimum {lot_size} shares needed)"
                }
        else:
            trade_type = alert.get("type", "STOCK")
            instrument = alert.get("instrument", f"{symbol} EQ")
            expiry_date = None
            qty_cap = int(capital_per_trade / entry_price) if entry_price > 0 else 0
            alert_qty = int(alert.get("qty") or 0)
            if alert_qty > 0:
                qty = min(qty_cap, alert_qty)
            else:
                qty = qty_cap
            if qty == 0:
                return {
                    "error": f"Price too high for Rs {capital_per_trade:,.0f} allocation"
                }

        alert_stop = alert.get("stop")
        alert_t1 = alert.get("target1")
        atr_val = alert.get("atr")
        atr_mult = settings.get("atr_multiplier", 2.0)

        if isinstance(alert_stop, (int, float)) and alert_stop > 0:
            sl_price = round(float(alert_stop), 2)
        elif atr_val and atr_val > 0:
            # Dynamic ATR-based Stop Loss
            dist = atr_val * atr_mult
            sl_price = round(
                entry_price - dist if direction == "LONG" else entry_price + dist, 2
            )
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
            "type": trade_type,
            "instrument": instrument,
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
            "hold_minutes": None,
        }
        if expiry_date:
            trade["expiry_date"] = expiry_date

        # Sync with SQLite Journal for historical audit
        try:
            cfg = load_config()
            journal = Journal(cfg["paths"]["journal_db"])
            jid = journal.open_trade(
                symbol=symbol,
                structure=trade_type,
                side=direction,
                qty=qty,
                entry=entry_price,
                stop=sl_price,
                target=tgt_price,
                risk_rupees=round(abs(entry_price - sl_price) * qty, 2),
                regime=alert.get("source_regime", "UNKNOWN"),
                notes=f"Manual entry: {alert.get('entry_trigger', '')}",
            )
            trade["journal_id"] = jid
        except Exception as e:
            # Non-blocking failure; log for investigation
            print(f"⚠️ Journal sync failed for {symbol}: {e}")

        db["trades"].append(trade)
        return trade


def enter_option_structure(
    structure_name: str, resolved_legs: list, underlying: str, cfg: dict
) -> dict:
    """Enter a custom option structure."""
    if not is_market_open():
        return {"error": "Market closed"}
    with atomic_db_update() as db:
        if "option_trades" not in db:
            db["option_trades"] = []
        open_ops = [t for t in db.get("option_trades", []) if t.get("status") == "OPEN"]
        max_ops = cfg.get("options", {}).get("max_concurrent_structures", 4)
        if len(open_ops) >= max_ops:
            return {"error": f"Max concurrent options structures ({max_ops}) reached"}

        # Prevent SL infinite loops: max 1 entry per structure per symbol per day
        today_str = _now_ist().date().isoformat()
        todays_trades = [
            t
            for t in db["option_trades"]
            if t.get("symbol") == underlying
            and t.get("structure") == structure_name
            and t.get("entry_date") == today_str
        ]
        if todays_trades:
            return {"error": f"Already traded {structure_name} for {underlying} today"}

        net_premium = sum(
            (leg.premium * leg.lots * leg.lot_size) * (1 if leg.side == "SELL" else -1)
            for leg in resolved_legs
        )
        if net_premium <= 0:
            return {
                "error": "Non-credit structure (0 premium likely due to missing LTP data)"
            }
        # Smart exit metadata
        short_strikes = [l.strike for l in resolved_legs if l.side == "SELL"]
        try:
            from data import feed

            vix_df = feed.ohlc_cached("INDIAVIX", period="5d")
            entry_vix = (
                float(vix_df["close"].iloc[-1])
                if vix_df is not None and not vix_df.empty
                else 0.0
            )
        except Exception:
            entry_vix = 0.0
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
                    "exit_premium": None,
                }
                for l in resolved_legs
            ],
            "net_premium": round(net_premium, 2),
            "entry_net_credit": round(net_premium, 2),
            "entry_time": _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_date": today_str,
            "exit_time": None,
            "exit_reason": None,
            "pnl": None,
            "status": "OPEN",
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


def _enter_option_structure(
    setup, resolved_legs: list, cfg: dict, symbol: str = "NIFTY"
) -> dict:
    """Enter an option structure with metadata. Shared by NIFTY and BANKNIFTY."""
    from signals.options import (
        calc_structure_max_loss,
        check_naked_legs,
        is_within_entry_window,
    )

    cfg_key = "banknifty_options" if symbol == "BANKNIFTY" else "nifty_options"

    now_ist = _now_ist()
    in_window, window_reason = is_within_entry_window(cfg, now_ist, symbol=symbol)
    journal = Journal(cfg["paths"]["journal_db"])

    if not in_window:
        journal.log_skipped_trade(
            symbol,
            "NEUTRAL",
            "MED",
            "WINDOW_CLOSED",
            "UNKNOWN",
            "NEUTRAL",
            "options_gate",
            window_reason,
        )
        return {"error": f"{symbol} entry blocked: {window_reason}"}

    leg_dicts = [
        {
            "side": l.side,
            "type": l.type,
            "strike": l.strike,
            "expiry": l.expiry,
            "qty": l.lots * l.lot_size,
        }
        for l in resolved_legs
    ]
    no_naked, naked_reason = check_naked_legs(leg_dicts)
    if not no_naked:
        journal.log_skipped_trade(
            symbol,
            "NEUTRAL",
            "MED",
            "NAKED_RISK",
            "UNKNOWN",
            "NEUTRAL",
            "options_gate",
            naked_reason,
        )
        return {"error": f"{symbol} entry blocked: {naked_reason}"}

    with atomic_db_update() as db:
        if "option_trades" not in db:
            db["option_trades"] = []
        sym_cfg = cfg.get(cfg_key, {})
        open_sym = [
            t
            for t in db["option_trades"]
            if t.get("status") == "OPEN" and t.get("symbol") == symbol
        ]
        if len(open_sym) > 0:
            return {"error": f"{symbol} option structure already open"}

        # Prevent SL infinite loops: max 1 entry per structure today
        today_str = now_ist.date().isoformat()
        todays_trades = [
            t
            for t in db["option_trades"]
            if t.get("symbol") == symbol
            and t.get("structure") == setup.strategy
            and t.get("entry_date") == today_str
        ]
        if todays_trades:
            return {"error": f"Already traded {setup.strategy} for {symbol} today"}

        net_credit = sum(
            (l.premium * l.lots * l.lot_size) * (1 if l.side == "SELL" else -1)
            for l in resolved_legs
        )
        if net_credit <= 0:
            journal.log_skipped_trade(
                symbol,
                "NEUTRAL",
                "MED",
                "NO_CREDIT",
                "UNKNOWN",
                "NEUTRAL",
                "options_gate",
                "Non-credit structure",
            )
            return {"error": f"{symbol} blocked: non-credit"}

        lot_size = resolved_legs[0].lot_size if resolved_legs else 1
        struct_type = (
            "iron_condor"
            if "IRON_CONDOR" in setup.strategy
            else (
                "bull_put_spread"
                if "BULL_PUT" in setup.strategy
                else "bear_call_spread"
            )
        )
        underlying_spot = setup.spot if hasattr(setup, "spot") and setup.spot else None
        num_lots = max(l.lots for l in resolved_legs) if resolved_legs else 1
        max_loss = calc_structure_max_loss(
            struct_type,
            net_credit,
            setup.wing_width,
            lot_size,
            lots=num_lots,
            underlying_spot=underlying_spot,
        )
        # Smart exit metadata
        short_strikes = [l.strike for l in resolved_legs if l.side == "SELL"]
        try:
            from data import feed

            vix_df = feed.ohlc_cached("INDIAVIX", period="5d")
            entry_vix = (
                float(vix_df["close"].iloc[-1])
                if vix_df is not None and not vix_df.empty
                else setup.vix
            )
        except Exception:
            entry_vix = setup.vix

        trade = {
            "id": _next_id(db),
            "symbol": symbol,
            "structure": setup.strategy,
            "mode": setup.mode,
            "legs": [
                {
                    "side": l.side,
                    "type": l.type,
                    "strike": l.strike,
                    "expiry": l.expiry,
                    "qty": l.lots * l.lot_size,
                    "entry_premium": l.premium,
                    "exit_premium": None,
                }
                for l in resolved_legs
            ],
            "net_credit": round(net_credit, 2),
            "net_premium": round(net_credit, 2),
            "max_loss_rupees": round(max_loss, 2),
            "entry_net_credit": round(net_credit, 2),
            "entry_net_debit": 0.0,
            "entry_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_date": today_str,
            "status": "OPEN",
            "pnl": None,
            "exit_reason": None,
            "exit_time": None,
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


def enter_nifty_option_structure(setup, resolved_legs: list, cfg: dict) -> dict:
    """Enter a NIFTY specific option structure with metadata."""
    return _enter_option_structure(setup, resolved_legs, cfg, symbol="NIFTY")


def enter_banknifty_option_structure(setup, resolved_legs: list, cfg: dict) -> dict:
    """Enter a BANKNIFTY specific option structure with metadata."""
    return _enter_option_structure(setup, resolved_legs, cfg, symbol="BANKNIFTY")


def _option_net_premium(legs: list[dict], price_map: dict) -> float | None:
    total = 0.0
    for leg in legs:
        key = (leg["strike"], leg["expiry"], leg["type"])
        price = price_map.get(key)
        if price is None:
            return None
        sign = 1 if leg["side"] == "SELL" else -1
        total += sign * price * leg["qty"]
    return round(total, 2)


def _build_option_price_map(open_ops: list[dict]) -> dict:
    from signals.options import chain_snapshot

    underlyings = list({t["symbol"] for t in open_ops})
    price_map = {}
    for sym in underlyings:
        try:
            needed_expiries = list(
                {
                    leg["expiry"]
                    for t in open_ops
                    if t["symbol"] == sym
                    for leg in t["legs"]
                }
            )
            needed_strikes = list(
                {
                    leg["strike"]
                    for t in open_ops
                    if t["symbol"] == sym
                    for leg in t["legs"]
                }
            )
            chain = chain_snapshot(
                sym, target_expiries=needed_expiries, target_strikes=needed_strikes
            )
            if chain.empty:
                logging.getLogger(__name__).warning(
                    "%s: chain_snapshot returned empty DataFrame for need_strikes=%s expiries=%s",
                    sym,
                    needed_strikes,
                    needed_expiries,
                )
                continue
            found_strikes = set(chain["strike"].tolist())
            missing = [s for s in needed_strikes if s not in found_strikes]
            if missing:
                logging.getLogger(__name__).warning(
                    "%s: strikes %s not found in chain (found %d of %d)",
                    sym,
                    missing,
                    len(found_strikes),
                    len(needed_strikes),
                )
            for trade in [t for t in open_ops if t["symbol"] == sym]:
                for leg in trade["legs"]:
                    key = (leg["strike"], leg["expiry"], leg["type"])
                    col = f"{leg['type'].lower()}_ltp"
                    row = chain[
                        (chain["strike"] == leg["strike"])
                        & (chain["expiry"] == leg["expiry"])
                    ]
                    if not row.empty:
                        try:
                            price_map[key] = float(row.iloc[0][col])
                        except Exception as e:
                            logging.getLogger(__name__).exception(
                                "Failed to parse price for %s from chain for symbol %s: %s",
                                key,
                                sym,
                                e,
                            )
                            continue
                    else:
                        logging.getLogger(__name__).debug(
                            "%s: leg %s not found in chain (row empty)", sym, key
                        )
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Failed to build price map for %s: %s", sym, e
            )
            continue
    return price_map


def _get_current_vix() -> float:
    """Fetch current VIX value. Returns 0.0 on failure."""
    try:
        from data import feed

        vix_df = feed.ohlc_cached("INDIAVIX", period="5d")
        if vix_df is not None and not vix_df.empty:
            col = "close" if "close" in vix_df.columns else "Close"
            return float(vix_df[col].iloc[-1])
    except Exception as e:
        logging.getLogger(__name__).exception("Failed to fetch current VIX: %s", e)
    return 0.0


def _smart_exit_check(
    t: dict,
    current_net: float | None,
    settings: dict,
    vix_now: float = 0.0,
    current_regime: str | None = None,
) -> str | None:
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
    vix_threshold = settings.get("smart_exit_vix_spike_pct", 20.0)
    vix_floor = settings.get("smart_exit_vix_floor", 16.0)

    # Dynamic threshold based on Regime
    if current_regime == "TREND_UP":
        vix_threshold *= 1.5  # 50% more lenient during uptrends
    elif current_regime == "VOL_CONTRACTION":
        vix_threshold *= 0.8  # 20% more sensitive during vol contraction

    if entry_vix > 0 and vix_now > vix_floor:
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
                if (
                    ltp > highest_short + breach_margin
                    or ltp < lowest_short - breach_margin
                ):
                    return "STRIKE_BREACH"
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Failed to evaluate strike breach for trade %s: %s", t.get("id"), e
            )

    # 3. Theta Trail Lock
    if entry_premium > 0:
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


def check_option_exits(
    vix_current: float | None = None, current_regime: str | None = None
) -> list[dict]:
    now_ist = _now_ist()
    if not is_market_open(now_ist) and not is_eod_window(now_ist):
        return []
    db = _load_db()
    open_ops = [t for t in db.get("option_trades", []) if t.get("status") == "OPEN"]
    if not open_ops:
        return []

    closed = []
    vix_now = vix_current if vix_current is not None else _get_current_vix()
    with atomic_db_update() as db:
        open_ops = [t for t in db.get("option_trades", []) if t.get("status") == "OPEN"]
        if not open_ops:
            logging.getLogger(__name__).debug(
                "check_option_exits: no open option trades found (re-read inside lock)"
            )
            return []
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
                    needed_strikes = list(
                        {
                            leg["strike"]
                            for t in open_ops
                            if t["symbol"] == sym
                            for leg in t["legs"]
                        }
                    )
                    chain_cache[sym] = chain_snapshot(
                        sym, target_strikes=needed_strikes
                    )
                except Exception as e:
                    logging.getLogger(__name__).exception(
                        "Failed to fetch chain_snapshot for %s: %s", sym, e
                    )

        for t in open_ops:
            # Guard: Check for valid premium setup before enforcing PnL targets
            entry_net_credit = t.get("entry_net_credit")
            entry_net_debit = t.get("entry_net_debit")
            fallback_net = t.get("net_credit") or t.get("net_premium") or 0.0
            has_entry_premium = (
                (entry_net_credit or 0.0) > 0
                or (entry_net_debit or 0.0) > 0
                or fallback_net > 0
            )
            if not has_entry_premium:
                # Some synthetic test entries might lack credit/debit keys entirely, mark as invalid entry
                if "synthetic" in t.get("structure", "").lower() or fallback_net <= 0:
                    t["status"] = "CLOSED"
                    t["exit_time"] = now_ist.strftime("%Y-%m-%d %H:%M:%S")
                    t["exit_reason"] = "INVALID_ZERO_PREMIUM"
                    t["pnl"] = 0
                    closed.append(t["symbol"])
                    msg = f"{t['symbol']} {t.get('structure', '')}: Closed invalid entry (zero or missing net premium)"
                    print(msg)
                    logging.getLogger(__name__).warning("PaperTrader: %s", msg)
                    continue

            current_net = _option_net_premium(t["legs"], price_map)

            # If we don't have current net premium (missing LTPs), skip automated exits for safety.
            if current_net is None:
                logging.getLogger(__name__).warning(
                    f"Skipping exit checks for trade id={t.get('id')} symbol={t.get('symbol')} due to missing LTP data"
                )
                continue

            exit_reason = "EOD_CLOSE" if (is_eod and auto_close) else None

            # Smart exits (fire first — structural danger)
            if not exit_reason:
                exit_reason = _smart_exit_check(
                    t, current_net, settings, vix_now, current_regime=current_regime
                )

            # Delta Breach (needs chain data)
            if not exit_reason and smart_enabled:
                chain = chain_cache.get(t.get("symbol"))
                if chain is not None and not chain.empty:
                    delta_threshold = settings.get("smart_exit_delta_threshold", 0.35)
                    nd = net_position_delta(t["legs"], chain)
                    if nd is not None and abs(nd) > delta_threshold:
                        t["reentry_eligible"] = True
                        exit_reason = "DELTA_BREACH"

            # Flat SL/TGT backup (skip during grace period to avoid bid-ask jitter exits)
            if not exit_reason and not _within_grace_period(t, now_ist):
                pnl = (t.get("net_premium") or 0.0) - current_net
                sl_limit = settings.get("options_sl_pct", 125.0) / 100.0
                tgt_limit = settings.get("options_tgt_pct", 50.0) / 100.0
                net_prem = t.get("net_premium")
                if net_prem and net_prem > 0:
                    if pnl <= -abs(net_prem) * sl_limit:
                        exit_reason = "SL_HIT"
                    elif pnl >= abs(net_prem) * tgt_limit:
                        exit_reason = "TGT_HIT"

            if exit_reason:
                t["status"], t["exit_reason"], t["exit_time"] = (
                    "CLOSED",
                    exit_reason,
                    now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                )
                # Populate exit_premium for each leg
                for leg in t["legs"]:
                    key = (leg["strike"], leg["expiry"], leg["type"])
                    price = price_map.get(key) if price_map else None
                    leg["exit_premium"] = price
                # current_net guaranteed non-None here
                t["pnl"] = round((t.get("net_premium") or 0.0) - current_net, 2)
                closed.append(t)
    return closed


def _check_option_exits(
    vix_current: float = None,
    cfg: dict = None,
    current_regime: str | None = None,
    symbol: str = "NIFTY",
) -> list[dict]:
    """Check option exits for a given symbol. Shared by NIFTY and BANKNIFTY."""
    from signals.options import is_expiry_day, is_within_exit_window

    now_ist = _now_ist()
    if not is_market_open(now_ist) and not is_eod_window(now_ist):
        return []
    db = _load_db()
    open_trades = [
        t
        for t in db.get("option_trades", [])
        if t.get("status") == "OPEN" and t.get("symbol") == symbol
    ]
    if not open_trades:
        return []

    closed = []
    vix_now = vix_current or _get_current_vix()
    with atomic_db_update() as db:
        open_trades = [
            t
            for t in db.get("option_trades", [])
            if t.get("status") == "OPEN" and t.get("symbol") == symbol
        ]
        if not open_trades:
            logging.getLogger(__name__).debug(
                f"check_option_exits({symbol}): no open trades found (re-read inside lock)"
            )
            return []
        price_map = _build_option_price_map(open_trades)
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True)
        is_eod = is_eod_window(now_ist)
        smart_enabled = settings.get("smart_exits_enabled", True)

        # Fetch chain for delta breach
        chain = None
        if smart_enabled:
            from signals.options import chain_snapshot, net_position_delta

            try:
                needed_strikes = list(
                    {leg["strike"] for t in open_trades for leg in t["legs"]}
                )
                chain = chain_snapshot(symbol, target_strikes=needed_strikes)
            except Exception as e:
                logging.getLogger(__name__).exception(
                    "Failed to fetch %s chain_snapshot: %s", symbol, e
                )

        for t in open_trades:
            current_net = _option_net_premium(t["legs"], price_map)

            # If no current net (missing LTPs), skip exit checks for safety
            if current_net is None:
                logging.getLogger(__name__).warning(
                    f"Skipping exit checks for trade id={t.get('id')} symbol={symbol} due to missing LTP data"
                )
                continue

            exit_reason = "EOD_CUTOFF" if (is_eod and auto_close) else None

            # Smart exits (fire first)
            if not exit_reason:
                exit_reason = _smart_exit_check(
                    t, current_net, settings, vix_now, current_regime=current_regime
                )

            # Delta Breach
            if (
                not exit_reason
                and smart_enabled
                and chain is not None
                and not chain.empty
            ):
                delta_threshold = settings.get("smart_exit_delta_threshold", 0.35)
                nd = net_position_delta(t["legs"], chain)
                if nd is not None and abs(nd) > delta_threshold:
                    t["reentry_eligible"] = True
                    exit_reason = "DELTA_BREACH"

            # Flat SL/TGT backup (skip during grace period to avoid bid-ask jitter exits)
            if not exit_reason and not _within_grace_period(t, now_ist):
                pnl = t.get("net_credit", 0.0) - current_net
                sl_limit = settings.get("options_sl_pct", 125.0) / 100.0
                tgt_limit = settings.get("options_tgt_pct", 50.0) / 100.0
                net_credit = t.get("net_credit")
                if net_credit and net_credit > 0:
                    if pnl >= net_credit * tgt_limit:
                        exit_reason = "PROFIT_TAKEN"
                    elif pnl <= -net_credit * sl_limit:
                        exit_reason = "STOP_LOSS"

            if exit_reason:
                t["status"], t["exit_reason"], t["exit_time"] = (
                    "CLOSED",
                    exit_reason,
                    now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                )
                # Populate exit_premium for each leg
                for leg in t["legs"]:
                    key = (leg["strike"], leg["expiry"], leg["type"])
                    price = price_map.get(key) if price_map else None
                    leg["exit_premium"] = price
                t["pnl"] = round(t.get("net_credit", 0.0) - current_net, 2)
                closed.append(t)
    return closed


def check_nifty_option_exits(
    vix_current: float = None, cfg: dict = None, current_regime: str | None = None
) -> list[dict]:
    """Check NIFTY option exits."""
    return _check_option_exits(vix_current, cfg, current_regime, symbol="NIFTY")


def check_banknifty_option_exits(
    vix_current: float = None, cfg: dict = None, current_regime: str | None = None
) -> list[dict]:
    """Check BANKNIFTY option exits."""
    return _check_option_exits(vix_current, cfg, current_regime, symbol="BANKNIFTY")


def scan_reentry_candidates(data: dict, cfg: dict = None) -> list[dict]:
    """Scan recently closed eligible trades and re-enter if conditions normalize."""
    if cfg is None:
        from config.loader import load_config

        cfg = load_config()
    now_ist = _now_ist()
    if not is_market_open(now_ist):
        return []

    db = _load_db()
    settings = db.get("settings", {})
    if not settings.get("smart_reentry_enabled", False):
        return []

    today_str = _now_ist().date().isoformat()
    eligible = [
        t
        for t in db.get("option_trades", [])
        if t.get("reentry_eligible")
        and t.get("status") == "CLOSED"
        and t.get("entry_date") == today_str
        and t.get("exit_reason") in ("VIX_SPIKE", "DELTA_BREACH")
    ]

    if not eligible:
        return []

    # Check if VIX has stabilized
    vix_now = _get_current_vix()
    regime = data.get("regime", {})
    regime_name = regime.get("regime") or regime.get("name") or "UNKNOWN"
    if regime_name == "VOL_EXPANSION":
        return []

    reentered = []
    for t in eligible:
        entry_vix = t.get("entry_vix", 0.0)
        if entry_vix > 0 and vix_now > entry_vix * 1.05:
            continue  # VIX still elevated

        # Check we haven't already re-entered this structure today
        already_today = any(
            x.get("symbol") == t["symbol"]
            and x.get("structure") == t.get("structure")
            and x.get("entry_date") == today_str
            and x.get("status") == "OPEN"
            for x in db.get("option_trades", [])
        )
        if already_today:
            continue

        # Mark as no longer eligible to prevent loops
        with atomic_db_update() as db2:
            for x in db2.get("option_trades", []):
                if x.get("id") == t["id"]:
                    x["reentry_eligible"] = False

        reentered.append(
            {
                "symbol": t["symbol"],
                "structure": t.get("structure"),
                "original_id": t["id"],
            }
        )

    return reentered


def _get_option_setups(
    data: dict, cfg: dict = None, symbol: str = "NIFTY"
) -> list[dict]:
    """
    Generate actionable option-selling setups for a given symbol.
    Internal shared implementation for NIFTY and BANKNIFTY.
    """
    from signals.option_strategy import (
        pick_banknifty_strategy,
        pick_nifty_strategy,
        resolve_banknifty_structure,
        resolve_nifty_structure,
    )
    from signals.options import chain_snapshot, is_within_entry_window

    if cfg is None:
        from config.loader import load_config

        cfg = load_config()

    cfg_key = "banknifty_options" if symbol == "BANKNIFTY" else "nifty_options"

    # Inject options lot size from UI settings
    db = _load_db()
    settings = db.get("settings", {})
    if cfg_key not in cfg:
        cfg[cfg_key] = {}
    cfg[cfg_key]["min_lots_per_leg"] = max(1, settings.get("options_lots_per_trade", 1))

    journal = Journal(cfg["paths"]["journal_db"])
    setups = []
    sym_cfg = cfg.get(cfg_key, {})
    if not sym_cfg.get("enabled", False):
        return setups

    regime = data.get("regime", {})
    flows = data.get("flows", {})
    regime_name = regime.get("name", "")
    bias = flows.get("bias", "NEUTRAL")
    vix = regime.get("vix", 15)
    vix_change = regime.get("vix_5d_change_pct", 0)
    pcr = flows.get("pcr_oi")

    if symbol == "BANKNIFTY":
        spot = data.get("banknifty", {}).get("close", 0)
    else:
        spot = data.get("nifty", {}).get("close", 0)

    if spot <= 0:
        return setups

    # Check entry window with symbol
    in_window, window_reason = is_within_entry_window(cfg, symbol=symbol)

    # Pick strategy
    if symbol == "BANKNIFTY":
        setup = pick_banknifty_strategy(
            data, regime_name, bias, vix, vix_change, pcr, cfg
        )
    else:
        setup = pick_nifty_strategy(data, regime_name, bias, vix, vix_change, pcr, cfg)

    if setup is None:
        journal.log_skipped_trade(
            symbol,
            "NEUTRAL",
            "LOW",
            "NO_STRATEGY",
            regime_name,
            bias,
            "options_engine",
            "No strategy for current regime/bias",
        )
        setups.append(
            {
                "symbol": symbol,
                "suitable": False,
                "skip_reason": "No strategy for current regime/bias",
                "regime": regime_name,
                "bias": bias,
                "vix": vix,
            }
        )
        return setups

    # Get option chain
    try:
        chain = chain_snapshot(symbol)
    except Exception as e:
        journal.log_skipped_trade(
            symbol,
            "NEUTRAL",
            "MED",
            "CHAIN_ERROR",
            regime_name,
            bias,
            "options_engine",
            str(e),
        )
        setups.append(
            {
                "symbol": symbol,
                "suitable": False,
                "skip_reason": f"Chain error: {e}",
                "regime": regime_name,
                "bias": bias,
                "vix": vix,
            }
        )
        return setups

    if chain.empty:
        journal.log_skipped_trade(
            symbol,
            "NEUTRAL",
            "MED",
            "EMPTY_CHAIN",
            regime_name,
            bias,
            "options_engine",
            "Empty option chain",
        )
        return setups

    # Read lot_size/strike_step from the correct config section
    options_cfg = cfg.get("options", {})
    lot_size = (
        sym_cfg.get("lot_size", {}).get(symbol)
        or options_cfg.get("lot_size", {}).get(symbol)
        or (30 if symbol == "BANKNIFTY" else 75)
    )
    strike_step = (
        sym_cfg.get("strike_step", {}).get(symbol)
        or options_cfg.get("strike_step", {}).get(symbol)
        or (100 if symbol == "BANKNIFTY" else 50)
    )

    if symbol == "BANKNIFTY":
        setup = resolve_banknifty_structure(
            setup, chain, spot, lot_size, strike_step, cfg
        )
    else:
        setup = resolve_nifty_structure(setup, chain, spot, lot_size, strike_step, cfg)

    setup.entry_window_ok = in_window
    if not in_window or not setup.suitable:
        reason = setup.skip_reason if not setup.suitable else window_reason
        journal.log_skipped_trade(
            symbol,
            "NEUTRAL",
            "MED",
            "NOT_SUITABLE",
            regime_name,
            bias,
            "options_engine",
            reason,
        )

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
        "skip_reason": setup.skip_reason
        if not setup.suitable
        else ("" if in_window else window_reason),
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
                "lots": l.lots,
                "lot_size": l.lot_size,
                "premium": l.premium,
            }
            for l in setup.legs
        ]
        if setup.legs
        else [],
    }
    setups.append(setup_dict)
    return setups


def get_nifty_option_setups(data: dict, cfg: dict = None) -> list[dict]:
    """Generate actionable NIFTY option-selling setups from signal data."""
    return _get_option_setups(data, cfg, symbol="NIFTY")


def get_banknifty_option_setups(data: dict, cfg: dict = None) -> list[dict]:
    """Generate actionable BANKNIFTY option-selling setups from signal data."""
    return _get_option_setups(data, cfg, symbol="BANKNIFTY")


def check_and_close_trades() -> list[dict]:
    """Check all OPEN trades against current prices."""
    now_ist = _now_ist()
    market_open = is_market_open(now_ist)
    is_eod = is_eod_window(now_ist)

    if not market_open and not is_eod:
        return []

    # 1. READ open symbols first OUTSIDE the lock/transaction to avoid holding it during network calls
    db = _load_db()
    open_trades = [t for t in db.get("trades", []) if t.get("status") == "OPEN"]
    if not open_trades:
        return []

    symbols = list(set(t["symbol"] for t in open_trades))

    # 2. FETCH prices OUTSIDE the lock
    prices = _get_ltp_batch(symbols)

    closed = []
    # 3. Enter transaction lock ONLY for local fast DB mutations
    with atomic_db_update() as db:
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True)  # Default to True if missing

        # Reload open trades from the locked db copy to ensure fresh transaction state
        open_trades = [t for t in db.get("trades", []) if t.get("status") == "OPEN"]
        if not open_trades:
            logging.getLogger(__name__).debug(
                "check_and_close_trades: no open trades found (re-read inside lock)"
            )
            return []

        for trade in open_trades:
            ltp = prices.get(trade["symbol"])
            if ltp is None:
                continue

            exit_reason = None
            direction = trade["direction"]

            if any(k not in trade for k in ["sl_price", "tgt_price", "direction"]):
                continue

            # Trailing stop logic (Standard trailing stop maintaining sl_pct buffer behind peak)
            if settings.get("trail_sl", True):
                if "peak_price" not in trade:
                    trade["peak_price"] = trade["entry_price"]
                sl_pct = trade.get("sl_pct", settings.get("sl_pct", 2.0))
                if direction == "LONG":
                    if ltp > trade["peak_price"]:
                        trade["peak_price"] = ltp
                        new_sl = ltp * (1 - sl_pct / 100)
                        if new_sl > trade["sl_price"]:
                            trade["sl_price"] = round(new_sl, 2)
                else:
                    if ltp < trade["peak_price"]:
                        trade["peak_price"] = ltp
                        new_sl = ltp * (1 + sl_pct / 100)
                        if new_sl < trade["sl_price"]:
                            trade["sl_price"] = round(new_sl, 2)

            # Respect EOD close setting
            eod_trigger = is_eod and auto_close

            # Expiry check for Futures
            is_expired = False
            if trade.get("type") == "FUTURE" and trade.get("expiry_date"):
                today_str = now_ist.strftime("%Y-%m-%d")
                if today_str >= trade["expiry_date"]:
                    is_expired = True

            if is_expired:
                exit_reason = "EXPIRY"
            elif direction == "LONG":
                if ltp <= trade["sl_price"]:
                    exit_reason = "SL_HIT"
                elif ltp >= trade["tgt_price"]:
                    exit_reason = "TARGET_HIT"
                elif eod_trigger:
                    exit_reason = "EOD_CLOSE"
            else:
                if ltp >= trade["sl_price"]:
                    exit_reason = "SL_HIT"
                elif ltp <= trade["tgt_price"]:
                    exit_reason = "TARGET_HIT"
                elif eod_trigger:
                    exit_reason = "EOD_CLOSE"

            if exit_reason:
                trade["exit_price"] = ltp
                trade["exit_time"] = now_ist.strftime("%Y-%m-%d %H:%M:%S")
                trade["exit_reason"] = exit_reason
                trade["status"] = "CLOSED"

                # Duration tracking
                try:
                    entry_dt = datetime.strptime(
                        trade["entry_time"], "%Y-%m-%d %H:%M:%S"
                    ).replace(tzinfo=IST)
                    trade["hold_minutes"] = int(
                        (now_ist - entry_dt).total_seconds() / 60
                    )
                except Exception:
                    pass

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
        trade = next(
            (
                t
                for t in db["trades"]
                if t["id"] == trade_id and t.get("status") == "OPEN"
            ),
            None,
        )
        if not trade:
            return None

        ltp = _get_ltp(trade["symbol"])
        if ltp is None:
            return {"error": f"Could not fetch LTP for {trade['symbol']}"}

        trade["exit_price"] = ltp
        trade["exit_time"] = now_ist.strftime("%Y-%m-%d %H:%M:%S")
        trade["exit_reason"] = reason
        trade["status"] = "CLOSED"

        try:
            entry_dt = datetime.strptime(
                trade["entry_time"], "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=IST)
            trade["hold_minutes"] = int((now_ist - entry_dt).total_seconds() / 60)
        except Exception:
            pass

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
        if "option_trades" not in db:
            return None
        trade = next(
            (
                t
                for t in db["option_trades"]
                if t["id"] == trade_id and t.get("status") == "OPEN"
            ),
            None,
        )
        if not trade:
            return None

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
    if not is_market_open(now_ist):
        return []
    if now_ist.hour == 15 and now_ist.minute >= 15:
        return []

    # Equity entry time window: avoid open whipsaw (09:15-10:00) and
    # late-day entries with insufficient time for target (after 14:15).
    t_now = now_ist.timetz().replace(tzinfo=None)
    if t_now < EQUITY_ENTRY_START or t_now > EQUITY_ENTRY_END:
        return []

    if cfg is None:
        cfg = load_config()

    # Merge UI-saved risk gate overrides into a copy of cfg so Guardrails
    # picks them up without touching the global config file.
    _saved = get_settings()
    _risk_override = dict(cfg.get("risk", {}))
    _risk_override["daily_stop_pct"] = _saved.get(
        "rg_daily_stop_pct", _risk_override.get("daily_stop_pct", 0.02)
    )
    _risk_override["monthly_stop_pct"] = _saved.get(
        "rg_monthly_stop_pct", _risk_override.get("monthly_stop_pct", 0.06)
    )
    _risk_override["concurrent_open_pct"] = _saved.get(
        "rg_concurrent_open_pct", _risk_override.get("concurrent_open_pct", 0.03)
    )
    _risk_override["margin_util_cap"] = _saved.get(
        "rg_margin_util_cap", _risk_override.get("margin_util_cap", 0.60)
    )
    _risk_override["correlation_max"] = _saved.get(
        "rg_correlation_max", _risk_override.get("correlation_max", 0.70)
    )
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

        today_symbols = {
            t["symbol"] for t in db["trades"] if t.get("entry_date") == today_str
        }
        all_closed = [t for t in db["trades"] if t.get("status") == "CLOSED"]
        today_pnl = sum(
            t.get("pnl", 0) or 0 for t in all_closed if t.get("entry_date") == today_str
        )
        month_pnl = sum(
            t.get("pnl", 0) or 0
            for t in all_closed
            if t.get("entry_date", "")[:7] == today_str[:7]
        )
        open_risk = sum(
            t.get("risk_rupees", 0) or 0
            for t in db["trades"]
            if t.get("status") == "OPEN"
        )

        total_capital = cfg["account"]["capital"]
        deployed = sum(
            t.get("capital_deployed", 0) or 0
            for t in db["trades"]
            if t.get("status") == "OPEN"
        )
        margin_used_pct = deployed / total_capital if total_capital > 0 else 0
        today_trade_count = len(
            [t for t in db["trades"] if t.get("entry_date") == today_str]
        )
        open_trades_count = len([t for t in db["trades"] if t.get("status") == "OPEN"])

        for alert in alerts:
            if today_trade_count + len(entered) >= max_trades_per_day:
                break
            if len(entered) >= max_new_entries_per_cycle:
                break

            regime = alert.get("source_regime", "UNKNOWN")
            if "RANGE" in regime.upper() and (open_trades_count + len(entered) >= 5):
                journal.log_skipped_trade(
                    alert.get("symbol", ""),
                    alert.get("direction", "LONG"),
                    alert.get("confidence", "MEDIUM"),
                    "REGIME_CAP",
                    regime,
                    alert.get("flow_bias", "NEUTRAL"),
                    "regime_filter",
                    "Max 5 concurrent positions in RANGE regime",
                )
                continue

            sym = alert.get("symbol", "")
            conf = alert.get("confidence", "MEDIUM")
            direction = alert.get("direction", "LONG")
            regime = alert.get("source_regime", "UNKNOWN")
            flow_bias = alert.get("flow_bias", "NEUTRAL")

            if (
                sym in ("NIFTY", "BANKNIFTY", "FINNIFTY")
                or alert.get("type") == "INDEX"
            ):
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "INDEX_BYPASS",
                    regime,
                    flow_bias,
                    "equity_worker",
                    "Index handled by options worker",
                )
                continue
            if direction not in ("LONG", "SHORT"):
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "NOT_DIRECTIONAL",
                    regime,
                    flow_bias,
                    "paper_equity_only",
                    alert.get("entry_trigger", ""),
                )
                continue

            if conf_levels.get(conf, 2) < min_val:
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "CONFIDENCE_FILTER",
                    regime,
                    flow_bias,
                    f"min_conf={min_conf}",
                    f"Alert {conf} < {min_conf}",
                )
                continue

            rules = db.get("learned_filters", {}).get("rules", [])
            if rules:
                try:
                    from intelligence.learner import apply_learned_filter

                    decision, reason = apply_learned_filter(alert, rules)
                    if decision == "BLOCK":
                        journal.log_skipped_trade(
                            sym,
                            direction,
                            conf,
                            "LEARNED_FILTER",
                            regime,
                            flow_bias,
                            reason,
                            "Blocked by intelligence",
                        )
                        continue
                    if decision == "DOWNGRADE":
                        old_conf = conf
                        if conf == "HIGH":
                            conf = "MEDIUM"
                        elif conf == "MEDIUM":
                            conf = "LOW"
                        if conf_levels.get(conf, 1) < min_val:
                            journal.log_skipped_trade(
                                sym,
                                direction,
                                old_conf,
                                "LEARNED_DOWNGRADE",
                                regime,
                                flow_bias,
                                reason,
                                f"Downgraded to {conf}",
                            )
                            continue
                        alert["confidence"] = conf
                except Exception:
                    pass

            if sym in today_symbols:
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "DUPLICATE_TODAY",
                    regime,
                    flow_bias,
                    "daily_dedup",
                    f"Already traded {sym}",
                )
                continue

            entry_price = alert.get("entry_price")
            stop = alert.get("stop")

            # Fallback for missing prices (common for stock alerts)
            if entry_price is None or entry_price <= 0:
                entry_price = prefetched_prices.get(sym) or _get_ltp(sym)
                if entry_price is None or entry_price <= 0:
                    journal.log_skipped_trade(
                        sym,
                        direction,
                        conf,
                        "PRICE_ERROR",
                        regime,
                        flow_bias,
                        "LTP_FETCH_FAILED",
                        f"Could not get LTP for {sym}",
                    )
                    continue
                alert["entry_price"] = entry_price

            if stop is None or stop <= 0:
                sl_pct = settings.get("sl_pct", 2.0)
                if direction == "LONG":
                    stop = round(entry_price * (1 - sl_pct / 100), 2)
                else:
                    stop = round(entry_price * (1 + sl_pct / 100), 2)
                alert["stop"] = stop

            # Check if F&O Stock
            fno_lots = get_fno_lot_sizes()
            lot_size = fno_lots.get(sym, 1)

            if lot_size > 1:
                trade_type = "FUTURE"
                instrument = f"{sym} FUT"
                expiry_date = get_futures_expiry(now_ist)
            else:
                trade_type = alert.get("type", "STOCK")
                instrument = alert.get("instrument", f"{sym} EQ")
                expiry_date = None

            if entry_price > 0 and stop > 0 and entry_price != stop:
                try:
                    size_result = directional_size(
                        total_capital,
                        cfg["risk"]["per_trade_pct"],
                        entry_price,
                        stop,
                        lot_size,
                        direction=direction,
                    )
                except ValueError as ve:
                    journal.log_skipped_trade(
                        sym,
                        direction,
                        conf,
                        "PRICE_ERROR",
                        regime,
                        flow_bias,
                        "INVALID_STOP_RELATION",
                        str(ve),
                    )
                    continue

                # Cap quantity by Capital per Trade setting (Notional cap)
                capital_cap = settings.get("capital_per_trade", 500000.0)
                max_qty_by_cap = int(capital_cap / entry_price)
                if lot_size > 1:
                    max_qty_by_cap = (max_qty_by_cap // lot_size) * lot_size

                final_qty = min(size_result.qty, max_qty_by_cap)

                if final_qty > 0:
                    alert["qty"] = final_qty
                    proposed_risk = round(final_qty * abs(entry_price - stop), 2)
                    alert["risk_rupees"] = proposed_risk
                else:
                    journal.log_skipped_trade(
                        sym,
                        direction,
                        conf,
                        "SIZE_ERROR",
                        regime,
                        flow_bias,
                        "ZERO_QTY",
                        f"Size calculation returned 0 for {sym} (lot size {lot_size})",
                    )
                    continue
            else:
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "PRICE_ERROR",
                    regime,
                    flow_bias,
                    "INVALID_PRICES",
                    f"Entry: {entry_price}, Stop: {stop}",
                )
                continue

            gate_result = guardrails.check_new_trade(
                proposed_risk=proposed_risk,
                open_risk=open_risk,
                day_pnl=today_pnl,
                month_pnl=month_pnl,
                margin_used_pct=margin_used_pct,
            )
            if not gate_result.ok:
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "RISK_GATE",
                    regime,
                    flow_bias,
                    "; ".join(gate_result.reasons),
                    f"Risk: ₹{proposed_risk:,.0f}",
                )
                continue

            sl_pct = settings.get("sl_pct", 2.0)
            tgt_pct = settings.get("tgt_pct", 4.0)
            if direction == "LONG":
                tgt_price = round(entry_price * (1 + tgt_pct / 100), 2)
            else:
                tgt_price = round(entry_price * (1 - tgt_pct / 100), 2)

            new_id = max([t["id"] for t in db["trades"]] + [0]) + 1
            new_trade = {
                "id": new_id,
                "symbol": sym,
                "direction": direction,
                "status": "OPEN",
                "type": trade_type,
                "instrument": instrument,
                "entry_price": entry_price,
                "qty": final_qty,
                "capital_deployed": round(entry_price * final_qty, 2),
                "sl_price": stop,
                "sl_pct": sl_pct,
                "tgt_price": tgt_price,
                "tgt_pct": tgt_pct,
                "peak_price": entry_price,
                "entry_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
                "entry_date": today_str,
                "confidence": conf,
                "risk_rupees": proposed_risk,
                "planned_risk": alert.get("planned_risk", proposed_risk),
                "source_regime": regime,
                "flow_bias": flow_bias,
                "hold_minutes": None,
            }
            if expiry_date:
                new_trade["expiry_date"] = expiry_date

            # Sync with SQLite Journal
            try:
                journal = Journal(cfg["paths"]["journal_db"])
                jid = journal.open_trade(
                    symbol=sym,
                    structure=trade_type,
                    side=direction,
                    qty=final_qty,
                    entry=entry_price,
                    stop=stop,
                    target=tgt_price,
                    risk_rupees=new_trade["risk_rupees"],
                    regime=regime,
                    notes=f"Auto-entry: {alert.get('entry_trigger', '')}",
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
        if "daily_summaries" not in db:
            db["daily_summaries"] = []

        # Gather all trades across SQLite and paper_trades JSON
        all_trades = []

        # 1. Load from SQLite
        try:
            import sqlite3

            from config.loader import load_config

            cfg = load_config()
            db_path = cfg["paths"]["journal_db"]
            if Path(db_path).exists():
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                try:
                    rows = conn.execute("SELECT * FROM trades").fetchall()
                    for r in rows:
                        row = dict(r)
                        opened_at = str(row.get("opened_at") or "")
                        closed_at = str(row.get("closed_at") or "")
                        side = str(row.get("side") or "LONG").upper()
                        entry = row.get("entry")
                        exit_price = row.get("exit_price")
                        qty = int(row.get("qty") or 0)
                        pnl = row.get("pnl_rupees")
                        if (
                            pnl is None
                            and entry not in (None, 0)
                            and exit_price is not None
                        ):
                            if side == "SHORT":
                                pnl = (float(entry) - float(exit_price)) * qty
                            else:
                                pnl = (float(exit_price) - float(entry)) * qty

                        notes = str(row.get("notes") or "").upper()
                        exit_reason = "CLOSED"
                        if "SL_HIT" in notes or "SL HIT" in notes:
                            exit_reason = "SL_HIT"
                        elif (
                            "TARGET_HIT" in notes
                            or "TARGET HIT" in notes
                            or "TGT HIT" in notes
                        ):
                            exit_reason = "TARGET_HIT"
                        elif "EOD" in notes:
                            exit_reason = "EOD_CLOSE"
                        elif closed_at:
                            exit_reason = "CLOSED"
                        else:
                            exit_reason = "OPEN"

                        all_trades.append(
                            {
                                "id": row.get("id"),
                                "symbol": row.get("symbol"),
                                "direction": "SHORT" if side == "SHORT" else "LONG",
                                "entry_price": entry,
                                "exit_price": exit_price,
                                "qty": qty,
                                "pnl": pnl,
                                "status": "CLOSED" if closed_at else "OPEN",
                                "exit_reason": exit_reason,
                                "entry_date": opened_at[:10] if opened_at else None,
                                "source": "sqlite",
                            }
                        )
                finally:
                    conn.close()
        except Exception as e:
            print(f"Error loading SQLite trades for EOD: {e}")

        # 2. Load from JSON db["trades"]
        for t in db.get("trades", []):
            all_trades.append(
                {
                    "id": t.get("id"),
                    "symbol": t.get("symbol"),
                    "direction": t.get("direction", "LONG"),
                    "entry_price": t.get("entry_price"),
                    "exit_price": t.get("exit_price"),
                    "qty": t.get("qty"),
                    "pnl": t.get("pnl"),
                    "status": t.get("status"),
                    "exit_reason": t.get("exit_reason")
                    or ("CLOSED" if t.get("status") == "CLOSED" else "OPEN"),
                    "entry_date": t.get("entry_date"),
                    "source": "json_trades",
                }
            )

        # 3. Load from JSON db["option_trades"]
        for t in db.get("option_trades", []):
            # Skip invalid synthetic/zero-premium closures — they are not real executed P&L events
            exit_reason = t.get("exit_reason") or (
                "CLOSED" if t.get("status") == "CLOSED" else "OPEN"
            )
            pnl_val = t.get("pnl")
            if exit_reason == "INVALID_ZERO_PREMIUM" and (
                pnl_val is None or pnl_val == 0
            ):
                # Don't include in EOD metrics; continue to next trade
                continue

            all_trades.append(
                {
                    "id": t.get("id"),
                    "symbol": t.get("symbol"),
                    "direction": "LONG"
                    if (t.get("net_premium", 0) or 0) < 0
                    else "SHORT",
                    "entry_price": t.get("net_premium"),
                    "exit_price": t.get("exit_premium"),
                    "qty": sum(leg.get("qty", 1) for leg in t.get("legs", [])),
                    "pnl": t.get("pnl"),
                    "status": t.get("status"),
                    "exit_reason": exit_reason,
                    "entry_date": t.get("entry_date"),
                    "source": "json_options",
                }
            )

        def _calc_day_summary(day: str) -> dict:
            day_trades = [t for t in all_trades if t.get("entry_date") == day]
            closed_today = [t for t in day_trades if t["status"] == "CLOSED"]
            winners = [t for t in closed_today if (t.get("pnl") or 0) > 0]
            losers = [t for t in closed_today if (t.get("pnl") or 0) < 0]
            total_pnl = sum(t.get("pnl", 0) or 0 for t in closed_today)

            sl_hits = len([t for t in closed_today if t.get("exit_reason") == "SL_HIT"])
            target_hits = len(
                [t for t in closed_today if t.get("exit_reason") == "TARGET_HIT"]
            )
            eod_exits = len(
                [t for t in closed_today if t.get("exit_reason") == "EOD_CLOSE"]
            )
            win_rate = round((len(winners) / max(len(closed_today), 1)) * 100, 1)

            # Intelligence Performance Comparison
            smart_reasons = {"VIX_SPIKE", "STRIKE_BREACH", "TRAIL_LOCK", "DELTA_BREACH"}
            standard_reasons = {"SL_HIT", "TARGET_HIT", "STOP_LOSS", "PROFIT_TAKEN"}

            smart_group = [
                t for t in closed_today if t.get("exit_reason") in smart_reasons
            ]
            std_group = [
                t for t in closed_today if t.get("exit_reason") in standard_reasons
            ]

            def _get_group_stats(group):
                if not group:
                    return {"count": 0, "pnl": 0.0, "wr": 0.0}
                wins = [t for t in group if (t.get("pnl") or 0) > 0]
                return {
                    "count": len(group),
                    "pnl": round(sum(t.get("pnl") or 0 for t in group), 2),
                    "wr": round(len(wins) / len(group) * 100, 1),
                }

            return {
                "date": day,
                "generated_at": _now_ist().strftime("%Y-%m-%d %H:%M:%S IST"),
                "total_trades": len(day_trades),
                "closed": len(closed_today),
                "winners": len(winners),
                "losers": len(losers),
                "win_rate": win_rate,
                "sl_hits": sl_hits,
                "target_hits": target_hits,
                "eod_exits": eod_exits,
                "total_pnl": round(total_pnl, 2),
                "trades": closed_today,
                "cumulative_pnl": 0.0,
                "analysis": {
                    "what_went_right": [],
                    "what_went_wrong": [],
                    "patterns": [],
                },
                "corrections": _generate_corrections(
                    closed_today, db.get("daily_summaries", [])
                ),
                "performance_comparison": {
                    "smart": _get_group_stats(smart_group),
                    "standard": _get_group_stats(std_group),
                },
            }

        # 1. Identify all unique trade dates in all_trades
        all_trade_dates = set()
        for t in all_trades:
            if t.get("entry_date"):
                all_trade_dates.add(t["entry_date"])

        # Ensure 'today' is in the set
        all_trade_dates.add(today)

        # 2. Check for missing summaries and generate them
        existing_dates = {s["date"] for s in db["daily_summaries"]}
        for d in sorted(all_trade_dates):
            if d not in existing_dates or d == today:
                summary = _calc_day_summary(d)
                db["daily_summaries"] = [
                    s for s in db["daily_summaries"] if s["date"] != d
                ]
                db["daily_summaries"].append(summary)

        # 3. Sort daily_summaries chronologically
        db["daily_summaries"].sort(key=lambda x: x["date"])

        # 4. Recompute cumulative_pnl sequentially across all summaries
        running_pnl = 0.0
        for s in db["daily_summaries"]:
            running_pnl += s["total_pnl"]
            s["cumulative_pnl"] = round(running_pnl, 2)

        db["cumulative_pnl"] = round(running_pnl, 2)

        # Return the summary for target today
        today_summary = next(
            (s for s in db["daily_summaries"] if s["date"] == today), None
        )
        if not today_summary:
            today_summary = _calc_day_summary(today)
            today_summary["cumulative_pnl"] = db["cumulative_pnl"]

        return today_summary


def _generate_corrections(today_trades: list[dict], history: list[dict]) -> list[str]:
    """Generate strategy corrections based on today's result + recent history."""
    corrections = []

    if not today_trades:
        return [
            "No trades to analyze. Consider lowering confidence threshold if signals were present but not taken."
        ]

    winners = [t for t in today_trades if (t.get("pnl") or 0) > 0]
    losers = [t for t in today_trades if (t.get("pnl") or 0) < 0]
    sl_hits = [t for t in today_trades if t.get("exit_reason") == "SL_HIT"]

    win_rate = len(winners) / len(today_trades) * 100 if today_trades else 0

    # SL too tight?
    if len(sl_hits) >= 3:
        corrections.append(
            "WIDEN SL: 3+ SL hits today. Consider 2.5% SL instead of 2%."
        )

    # Win rate corrections
    if win_rate < 40 and len(today_trades) >= 3:
        corrections.append(
            "REDUCE TRADES: Low win rate. Only take HIGH confidence signals tomorrow."
        )
    elif win_rate >= 70:
        corrections.append("MAINTAIN: Strategy working. Keep current parameters.")

    # Check for missed targets (EOD exits that were in profit)
    eod_profitable = [
        t
        for t in today_trades
        if t.get("exit_reason") == "EOD_CLOSE" and (t.get("pnl") or 0) > 0
    ]
    if eod_profitable:
        corrections.append(
            f"TRAIL STOP: {len(eod_profitable)} trade(s) exited at EOD with profit — implement trailing SL to lock gains."
        )

    # Directional bias check
    long_trades = [t for t in today_trades if t["direction"] == "LONG"]
    short_trades = [t for t in today_trades if t["direction"] == "SHORT"]
    long_losses = sum(1 for t in long_trades if (t.get("pnl") or 0) < 0)
    short_losses = sum(1 for t in short_trades if (t.get("pnl") or 0) < 0)

    if long_losses >= 2 and len(long_trades) >= 2:
        corrections.append(
            "REDUCE LONGS: Long bias failing. Check if regime has shifted bearish."
        )
    if short_losses >= 2 and len(short_trades) >= 2:
        corrections.append(
            "REDUCE SHORTS: Short bias failing. Check if regime has shifted bullish."
        )

    # Check recent history for systemic issues
    if len(history) >= 3:
        last_3 = history[-3:]
        losing_days = sum(1 for s in last_3 if s.get("total_pnl", 0) < 0)
        if losing_days >= 3:
            corrections.append(
                "PAUSE TRADING: 3 consecutive losing days. Review strategy fundamentals before continuing."
            )
        avg_win_rate = sum(s.get("win_rate", 0) for s in last_3) / 3
        if avg_win_rate < 35:
            corrections.append(
                "OVERHAUL SIGNALS: 3-day average win rate below 35%. Tighten entry criteria aggressively."
            )

    if not corrections:
        corrections.append(
            "No corrections needed. System performing within parameters."
        )

    return corrections


def get_stats() -> dict:
    db = _load_db()
    all_closed = [t for t in db.get("trades", []) if t.get("status") == "CLOSED"]
    all_closed.extend(
        [t for t in db.get("option_trades", []) if t.get("status") == "CLOSED"]
    )
    resolved = [t for t in all_closed if (t.get("pnl") or 0) != 0]
    winners = [t for t in resolved if (t.get("pnl") or 0) > 0]
    total_trades = len(db.get("trades", [])) + len(db.get("option_trades", []))
    open_trades = len(
        [t for t in db.get("trades", []) if t.get("status") == "OPEN"]
    ) + len([t for t in db.get("option_trades", []) if t.get("status") == "OPEN"])
    return {
        "total_trades": total_trades,
        "open_trades": open_trades,
        "cumulative_pnl": round(sum(t.get("pnl", 0) or 0 for t in all_closed), 2),
        "overall_win_rate": round(len(winners) / max(len(resolved), 1) * 100, 1),
    }


def _normalize_option_trade_for_ui(trade: dict, settings: dict | None = None) -> dict:
    if not trade.get("legs"):
        return trade

    settings = settings or {}
    normalized = dict(trade)
    entry_price = normalized.get("net_premium")
    if entry_price is None:
        entry_price = normalized.get("entry_net_credit")
    if entry_price is None:
        entry_price = normalized.get("entry_net_debit")
    if entry_price is None:
        entry_price = normalized.get("entry_price")

    qty = sum(max(1, int(leg.get("qty", 1) or 1)) for leg in normalized.get("legs", []))
    direction = normalized.get("direction") or (
        "SHORT" if (normalized.get("net_premium") or 0) >= 0 else "LONG"
    )
    confidence = normalized.get("confidence") or "MEDIUM"

    normalized["entry_price"] = entry_price
    normalized["qty"] = qty
    normalized["direction"] = direction
    normalized["confidence"] = confidence

    if entry_price not in (None, 0):
        sl_pct = (
            settings.get("options_sl_pct", DEFAULT_SETTINGS["options_sl_pct"]) / 100.0
        )
        tgt_pct = (
            settings.get("options_tgt_pct", DEFAULT_SETTINGS["options_tgt_pct"]) / 100.0
        )
        if normalized["direction"] == "SHORT":
            normalized.setdefault(
                "sl_price", round(float(entry_price) * (1 + sl_pct), 2)
            )
            normalized.setdefault(
                "tgt_price", round(float(entry_price) * (1 - tgt_pct), 2)
            )
        else:
            normalized.setdefault(
                "sl_price", round(float(entry_price) * (1 - sl_pct), 2)
            )
            normalized.setdefault(
                "tgt_price", round(float(entry_price) * (1 + tgt_pct), 2)
            )

    pnl = normalized.get("pnl")
    if pnl is not None and entry_price is not None:
        normalized.setdefault("exit_premium", round(entry_price - pnl, 2))

    return normalized


def get_open_trades() -> list[dict]:
    db = _load_db()
    open_t = [t for t in db.get("trades", []) if t.get("status") == "OPEN"]
    return open_t


def get_all_trades(limit: int = 50) -> list[dict]:
    db = _load_db()
    settings = db.get("settings", {})
    all_t = db.get("trades", []) + [
        _normalize_option_trade_for_ui(t, settings) for t in db.get("option_trades", [])
    ]
    # Re-sort by id or entry time to match original behavior where newer is at end
    all_t.sort(key=lambda x: str(x.get("entry_time", "")))
    return list(reversed(all_t[-limit:]))


def get_daily_summaries(limit: int = 30) -> list[dict]:
    return list(reversed(_load_db()["daily_summaries"][-limit:]))


def cleanup_db(
    from_date: str | None = None,
    to_date: str | None = None,
    purge_churn: bool = False,
    full_reset: bool = False,
) -> dict:
    """Clean up trade database based on criteria.
    - full_reset: Clears ALL trades and summaries.
    - purge_churn: Removes trades with 0 P&L (spam entries).
    - from_date/to_date: Removes ALL trades in this range (inclusive).
    """
    import sqlite3

    db = _load_db()

    if full_reset:
        db["trades"] = []
        db["option_trades"] = []
        db["daily_summaries"] = []
        db["cumulative_pnl"] = 0.0
        _save_db(db)

        # Sync full reset with SQLite Journal database
        try:
            cfg = load_config()
            conn = sqlite3.connect(cfg["paths"]["journal_db"])
            conn.execute("DELETE FROM trades")
            conn.execute("DELETE FROM skipped_trades")
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"⚠️ Failed to reset SQLite journal: {e}")

        return {"status": "success", "message": "Database fully reset"}

    original_count = len(db.get("trades", []))
    removed_count = 0

    # 1. Filter Stock Trades
    keep_trades = []
    for t in db.get("trades", []):
        entry_date = t.get("entry_date", "")
        pnl = t.get("pnl") or 0.0

        in_range = True
        if from_date and entry_date < from_date:
            in_range = False
        if to_date and entry_date > to_date:
            in_range = False

        if from_date or to_date:
            if in_range:
                removed_count += 1
                continue  # Delete it

        if (
            purge_churn
            and t.get("id", 0) > 5
            and pnl == 0
            and t.get("status") == "CLOSED"
        ):
            removed_count += 1
            continue  # Delete it

        keep_trades.append(t)
    db["trades"] = keep_trades

    # 2. Filter Option Trades
    keep_options = []
    for t in db.get("option_trades", []):
        entry_date = t.get("entry_date", "")
        pnl = t.get("pnl") or 0.0

        in_range = True
        if from_date and entry_date < from_date:
            in_range = False
        if to_date and entry_date > to_date:
            in_range = False

        if from_date or to_date:
            if in_range:
                removed_count += 1
                continue  # Delete it

        keep_options.append(t)
    db["option_trades"] = keep_options

    # Recalculate cumulative P&L
    db["cumulative_pnl"] = sum(
        t.get("pnl", 0) for t in keep_trades if t.get("status") == "CLOSED"
    ) + sum(t.get("pnl", 0) for t in keep_options if t.get("status") == "CLOSED")

    # 3. Clean up daily summaries
    new_summaries = []
    for s in db.get("daily_summaries", []):
        d = s.get("date")
        if (from_date and d < from_date) or (to_date and d > to_date):
            if from_date or to_date:
                continue

        # Update trade list in summary
        s_trades = [t for t in keep_trades if t.get("entry_date") == d]
        s["trades"] = s_trades
        s["total_trades"] = len(s_trades)
        s["total_pnl"] = sum(t.get("pnl", 0) for t in s_trades)
        winners = [t for t in s_trades if (t.get("pnl") or 0) > 0]
        s["win_rate"] = (
            round((len(winners) / max(len(s_trades), 1) * 100), 2) if s_trades else 0
        )
        new_summaries.append(s)
    db["daily_summaries"] = new_summaries

    _save_db(db)

    # Sync range deletion with SQLite Journal database
    try:
        cfg = load_config()
        conn = sqlite3.connect(cfg["paths"]["journal_db"])
        if from_date and to_date:
            conn.execute(
                "DELETE FROM trades WHERE substr(opened_at, 1, 10) >= ? AND substr(opened_at, 1, 10) <= ?",
                (from_date, to_date),
            )
            conn.execute(
                "DELETE FROM skipped_trades WHERE substr(ts, 1, 10) >= ? AND substr(ts, 1, 10) <= ?",
                (from_date, to_date),
            )
        elif from_date:
            conn.execute(
                "DELETE FROM trades WHERE substr(opened_at, 1, 10) >= ?", (from_date,)
            )
            conn.execute(
                "DELETE FROM skipped_trades WHERE substr(ts, 1, 10) >= ?", (from_date,)
            )
        elif to_date:
            conn.execute(
                "DELETE FROM trades WHERE substr(opened_at, 1, 10) <= ?", (to_date,)
            )
            conn.execute(
                "DELETE FROM skipped_trades WHERE substr(ts, 1, 10) <= ?", (to_date,)
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"⚠️ Failed to clean journal SQLite db range: {e}")

    return {
        "original_count": original_count,
        "removed_count": removed_count,
        "final_count": len(db["trades"]),
        "cumulative_pnl": db["cumulative_pnl"],
    }


def export_trades_to_csv() -> str:
    """Return trade history as a CSV string."""
    db = _load_db()
    trades = db.get("trades", []) + db.get("option_trades", [])
    if not trades:
        return "No trades found"

    df = pd.DataFrame(trades)
    cols = [
        "id",
        "symbol",
        "direction",
        "status",
        "entry_price",
        "exit_price",
        "qty",
        "capital_deployed",
        "pnl",
        "pnl_pct",
        "exit_reason",
        "entry_time",
        "exit_time",
        "confidence",
    ]
    available_cols = [c for c in cols if c in df.columns]
    return df[available_cols].to_csv(index=False)


def get_intelligence_context(trades: list[dict]) -> dict:
    """Calculate advanced metrics (Drawdown, Profit Factor) for LLM summary."""
    if not trades:
        return {"drawdown": 0, "profit_factor": 0, "summary": []}

    # 1. Profit Factor calculation
    wins = [t.get("pnl", 0) for t in trades if (t.get("pnl") or 0) > 0]
    losses = [abs(t.get("pnl", 0)) for t in trades if (t.get("pnl") or 0) < 0]
    pf = (
        round(sum(wins) / sum(losses), 2)
        if losses and sum(losses) > 0
        else (round(sum(wins), 2) if wins else 0)
    )

    # 2. Drawdown calculation (Sequence peak-to-valley)
    equity = 0
    max_equity = 0
    max_dd = 0
    summary = []
    for t in reversed(trades):  # Newest first in input; process chronological
        pnl = t.get("pnl") or 0
        equity += pnl
        max_equity = max(max_equity, equity)
        dd = max_equity - equity
        max_dd = max(max_dd, dd)
        summary.append(
            {
                "sym": t.get("symbol"),
                "dir": t.get("direction"),
                "pnl": pnl,
                "pnl_pct": t.get("pnl_pct"),
                "exit": t.get("exit_reason"),
                "dur": t.get("hold_minutes"),
            }
        )

    # --- New: Performance comparison for Smart Exits vs Standard Exits ---
    smart_reasons = {"VIX_SPIKE", "STRIKE_BREACH", "TRAIL_LOCK", "DELTA_BREACH"}
    # Added more standard reasons for completeness
    standard_reasons = {
        "SL_HIT",
        "TARGET_HIT",
        "STOP_LOSS",
        "PROFIT_TAKEN",
        "EOD_CLOSE",
        "MANUAL",
        "EXPIRY",
        "INVALID_ZERO_PREMIUM",
    }

    smart_group = [t for t in trades if t.get("exit_reason") in smart_reasons]
    std_group = [t for t in trades if t.get("exit_reason") in standard_reasons]

    def _calculate_group_metrics(group_trades: list[dict]) -> dict:
        if not group_trades:
            return {"count": 0, "win_rate": 0.0, "profit_factor": 0.0, "total_pnl": 0.0}

        group_wins = [t.get("pnl", 0) for t in group_trades if (t.get("pnl") or 0) > 0]
        group_losses_abs = [
            abs(t.get("pnl", 0)) for t in group_trades if (t.get("pnl") or 0) < 0
        ]

        group_win_rate = (
            round((len(group_wins) / len(group_trades)) * 100, 1)
            if group_trades
            else 0.0
        )

        sum_group_wins = sum(group_wins)
        sum_group_losses_abs = sum(group_losses_abs)

        if sum_group_losses_abs > 0:
            group_profit_factor = round(sum_group_wins / sum_group_losses_abs, 2)
        elif sum_group_wins > 0:  # All wins, no losses
            group_profit_factor = float("inf")  # Represent as infinite profit factor
        else:  # No trades or all breakeven
            group_profit_factor = 0.0

        group_total_pnl = round(sum(t.get("pnl", 0) for t in group_trades), 2)

        return {
            "count": len(group_trades),
            "win_rate": group_win_rate,
            "profit_factor": group_profit_factor,
            "total_pnl": group_total_pnl,
        }

    smart_metrics = _calculate_group_metrics(smart_group)
    standard_metrics = _calculate_group_metrics(std_group)

    return {
        "drawdown": round(max_dd, 2),
        "profit_factor": pf,
        "summary": list(reversed(summary)),
        "smart_exits_metrics": smart_metrics,
        "standard_exits_metrics": standard_metrics,
    }
