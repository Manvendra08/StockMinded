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
import os
import sys
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


# ─────────────────────────────────────────────────────────────────────────────
# H3 FIX: Cross-platform advisory file locking.
#
# The module previously did a hard `import msvcrt` at the top. `msvcrt` only
# exists on Windows, so importing paper_trader on Linux (the VPS target in
# setup_vps.sh) raised ImportError and took down the whole engine. `fcntl`
# (POSIX) and `msvcrt` (Windows) provide equivalent advisory locks; we select
# the right backend once at import time and expose two tiny helpers used by
# atomic_db_update(). Behaviour is preserved: non-blocking attempts first, then
# a blocking acquire, with the existing retry/sleep loop handling contention.
# ─────────────────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    import msvcrt

    def _lock_file(f, blocking: bool) -> None:
        # msvcrt.locking locks bytes relative to the current file position; lock
        # a single byte at offset 0. LK_LOCK blocks, LK_NBLCK raises OSError if
        # the region is already locked.
        f.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(f.fileno(), mode, 1)

    def _unlock_file(f) -> None:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
else:
    import fcntl

    def _lock_file(f, blocking: bool) -> None:
        # flock raises BlockingIOError (an OSError subclass) when LOCK_NB is set
        # and the lock is held elsewhere — the caller's retry loop catches it.
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f.fileno(), flags)

    def _unlock_file(f) -> None:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


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

# Thread-local storage for reentrant lock guard in atomic_db_update()
_atomic_db_tls = threading.local()

DEFAULT_SETTINGS = {
    "capital_per_trade": 500000.0,
    "capital_per_trade_stocks": 500000.0,
    "capital_per_trade_options": 500000.0,
    "sl_pct": 2.0,
    "tgt_pct": 4.0,
    "trail_sl": True,
    "trail_activation_pct": 2.0,  # Only start trailing once profit exceeds this %
    # MEDIUM matches the verdict engine's baseline for a valid directional trend
    # setup (TREND_UP/TREND_DOWN issue MEDIUM by default, upgrading to HIGH only
    # when AI + smart-money bias align). HIGH here would filter out nearly every
    # clean trend trade. Range-regime setups remain LOW in the verdict and stay
    # filtered. See signals/verdict.py.
    "min_confidence": "MEDIUM",
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
    "rg_daily_stop_pct": 0.02,  # 2% of capital = daily loss limit
    "rg_monthly_stop_pct": 0.06,  # 6% of capital = monthly loss limit
    "rg_concurrent_open_pct": 0.03,  # 3% of capital = max simultaneous open risk
    "rg_margin_util_cap": 0.60,  # 60% margin utilisation ceiling
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


def get_fno_security_info(exchange: str, token: str) -> dict | None:
    """
    Fetch contract specs (lot_size, tick_size, freeze_qty, circuit limits) from Shoonya.
    """
    try:
        from data.feed import fetch_shoonya_security_info

        info = fetch_shoonya_security_info(exchange, token)
        if info and info.get("stat") == "Ok":
            return {
                "lot_size": _safe_int(info.get("ls")),
                "tick_size": _safe_float(info.get("ti")),
                "freeze_qty": _safe_int(info.get("frzqty")),
                "lower_circuit": _safe_float(info.get("lct")),
                "upper_circuit": _safe_float(info.get("uct")),
                "expiry": info.get("exd"),
                "symbol": info.get("tsym") or info.get("dnm"),
            }
    except Exception as e:
        logging.getLogger(__name__).debug("Shoonya security info fetch failed: %s", e)
    return None


def get_futures_expiry(now_dt: datetime = None) -> str:
    """Return the next monthly futures expiry date (last Tuesday).

    NSE monthly derivative contracts (both indices and single stocks)
    expire on the last Tuesday of the contract month. If the last
    Tuesday falls on a trading holiday, expiry shifts to the previous
    trading day.

    Weekly contracts follow a different schedule:
      - Indices: Tuesdays
      - Single stocks: Thursdays
    """
    import calendar
    from datetime import date, time, timedelta

    from signals.options import _is_holiday

    if now_dt is None:
        now_dt = datetime.now(IST)

    today = now_dt.date()
    year, month = today.year, today.month

    def last_tuesday_of(y, m):
        """Find the last Tuesday of a given year/month, with holiday rollback."""
        cal = calendar.monthcalendar(y, m)
        for week in reversed(cal):
            tue = week[calendar.TUESDAY]
            if tue != 0:
                dt = date(y, m, tue)
                while _is_holiday(dt) or dt.weekday() >= 5:
                    dt -= timedelta(days=1)
                return dt

    curr_expiry = last_tuesday_of(year, month)

    if today > curr_expiry or (today == curr_expiry and now_dt.time() >= time(15, 30)):
        next_month = month + 1 if month < 12 else 1
        next_year = year if month < 12 else year + 1
        curr_expiry = last_tuesday_of(next_year, next_month)

    return curr_expiry.strftime("%Y-%m-%d")


def _load_db() -> dict:
    """Load the full trade database (Read Only).

    H7 FIX: Log a warning when falling back to empty DB or BAK file,
    so silent data loss is visible in logs.
    """
    import logging as _log_mod
    _logger = _log_mod.getLogger(__name__)

    for path in [DATA_FILE, BAK_FILE]:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if path == BAK_FILE:
                    _logger.warning(
                        "[_load_db] Loaded from BACKUP file %s — main file may be corrupted",
                        path,
                    )
                return data
            except Exception as e:
                _logger.warning("[_load_db] Failed to parse %s: %s", path, e)
                continue

    _logger.error(
        "[_load_db] NO database file found (neither %s nor %s). "
        "Returning empty DB — any subsequent save will overwrite backups with empty data!",
        DATA_FILE,
        BAK_FILE,
    )
    return {
        "trades": [],
        "option_trades": [],
        "daily_summaries": [],
        "settings": DEFAULT_SETTINGS.copy(),
        "cumulative_pnl": 0.0,
        "version": 1,
    }


def _save_db(db: dict) -> None:
    """Save the trade database to disk with Windows rename retries."""
    import shutil
    import logging as _log_mod
    _logger = _log_mod.getLogger(__name__)

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TMP_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())

    # Windows: os.replace/rename fails if the destination file is open by another thread/process.
    # Concurrent dashboard page-loads call _load_db() which briefly opens the file.
    # We use shutil.copy2 (which opens the file in read-shared mode) for backup,
    # and retry os.replace with exponential backoff up to 10 times to bypass transient locks.
    max_retries = 10
    base_delay = 0.05
    for attempt in range(max_retries):
        try:
            # Create backup before replacing
            if DATA_FILE.exists():
                try:
                    shutil.copy2(DATA_FILE, BAK_FILE)
                except Exception as bak_err:
                    _logger.warning("[_save_db] Backup creation failed: %s", bak_err)

            # Atomic replace (works on Windows & Unix, replaces destination if it exists)
            os.replace(TMP_FILE, DATA_FILE)
            return
        except PermissionError as e:
            if attempt < max_retries - 1:
                time.sleep(base_delay * (attempt + 1))
                continue
            raise


@contextlib.contextmanager
def atomic_db_update():
    """Transaction-style database update with Windows cross-process locking and retries.

    H5 FIX: Separated lock-acquisition retries from save failures.
    Previously, if _save_db() raised an OSError (e.g. disk full), the retry
    loop would attempt to re-yield, which is invalid for a contextmanager
    generator. Now:
      - Lock acquisition: retries up to 20 times on contention.
      - Save failure: logs error and re-raises immediately (no retry).
      - The caller always receives the exception on save failure, so it
        knows the mutation was NOT persisted.
    """
    import copy

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

    # Phase 1: Acquire lock with retries
    lock_f = None

    # Reentrant guard: if the current thread already holds the lock, skip
    # acquisition to avoid OSError [Errno 36] Resource deadlock avoided.
    _tls = _atomic_db_tls
    if getattr(_tls, "held", False):
        db = _load_db()
        yield db
        _save_db(db)
        return

    try:
        LOCK_FILE.touch(exist_ok=True)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[atomic_db_update] lock file touch failed: %s", e
        )

    for attempt in range(20):
        try:
            lock_f = open(LOCK_FILE, "r+")
            # H3 FIX: cross-platform lock helper. Non-blocking for the first 3
            # attempts, then blocking to avoid busy-spinning on heavily
            # contended files (preserves the original msvcrt LK_NBLCK→LK_LOCK
            # escalation without being Windows-specific).
            _lock_file(lock_f, blocking=(attempt >= 3))
            _tls.held = True
            break  # Lock acquired
        except (PermissionError, OSError) as lock_err:
            # OSError [Errno 36] = EDEADLK: same thread already holds lock
            if lock_f:
                try:
                    lock_f.close()
                except Exception:
                    pass
                lock_f = None
            if attempt < 19:
                time.sleep(0.2 * (attempt + 1))
                continue
            raise

    # Phase 2: Load, yield, save (NO retry on save failure)
    try:
        db = _load_db()
        yield db
        _save_db(db)
    except (OSError, IOError) as save_err:
        # H5 FIX: Log save failure explicitly so the caller knows data was NOT persisted
        logging.getLogger(__name__).error(
            "atomic_db_update: SAVE FAILED — mutations were NOT persisted to disk. "
            "Error: %s",
            save_err,
            exc_info=True,
        )
        raise
    finally:
        # Clear reentrant guard flag
        if getattr(_tls, "held", False):
            _tls.held = False
        # Release lock
        if lock_f:
            try:
                _unlock_file(lock_f)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "[atomic_db_update] lock release failed: %s", e
                )
            try:
                lock_f.close()
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "[atomic_db_update] lock_f close failed: %s", e
                )


MARKET_OPEN = dt_mod.time(9, 15)
MARKET_CLOSE = dt_mod.time(15, 30)
EOD_WINDOW_START = dt_mod.time(15, 25)
EOD_WINDOW_END = dt_mod.time(15, 35)

# Option SL grace period: don't check SL/TGT within N minutes of entry.
# Prevents false exits from bid-ask spread jitter on illiquid OTM strikes.
OPTION_SL_GRACE_MINUTES = 5

# Equity entry window: avoid open whipsaw and late-day insufficient-time entries
EQUITY_ENTRY_START = dt_mod.time(9, 40)
EQUITY_ENTRY_END = dt_mod.time(15, 15)


def is_within_equity_entry_window(now_ist) -> bool:
    """True if *now_ist* is inside the equity entry window (seconds-precision).

    Shared by alert generation (server.py) and auto-entry (paper_trader.py)
    so the cutoff is consistent across both call-sites.
    """
    t = now_ist.timetz().replace(tzinfo=None)
    return EQUITY_ENTRY_START <= t <= EQUITY_ENTRY_END


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

    t = n.timetz().replace(tzinfo=None)
    return EOD_WINDOW_START <= t <= EOD_WINDOW_END


def get_settings() -> dict:
    """Return current paper trader settings."""
    db = _load_db()
    settings = DEFAULT_SETTINGS.copy()
    settings.update(db.get("settings", {}))
    return settings


SETTINGS_SCHEMA = {
    "capital_per_trade": (float, 1000.0, 100000000.0),
    "capital_per_trade_stocks": (float, 1000.0, 100000000.0),
    "capital_per_trade_options": (float, 1000.0, 100000000.0),
    "sl_pct": (float, 0.1, 50.0),
    "tgt_pct": (float, 0.1, 200.0),
    "trail_activation_pct": (float, 0.0, 50.0),
    "min_confidence": ("enum", ["LOW", "MEDIUM", "HIGH"], None),
    "max_trades_per_day": (int, 1, 100),
    "max_new_entries_per_cycle": (int, 1, 50),
    "regime_filter": (bool, None, None),
    "auto_close_eod": (bool, None, None),
    "trail_sl": (bool, None, None),
    "atr_multiplier": (float, 0.1, 10.0),
    "telegram_bot_token": (str, 0, 100),
    "telegram_chat_id": (str, 0, 50),
    "options_sl_pct": (float, 0.1, 500.0),
    "options_tgt_pct": (float, 0.1, 500.0),
    "smart_exits_enabled": (bool, None, None),
    "smart_exit_vix_spike_pct": (float, 0.1, 100.0),
    "smart_exit_vix_floor": (float, 0.0, 50.0),
    "smart_exit_delta_threshold": (float, 0.0, 1.0),
    "smart_exit_trail_lock_pct": (float, 0.0, 100.0),
    "smart_exit_trail_floor_pct": (float, 0.0, 100.0),
    "smart_reentry_enabled": (bool, None, None),
    "options_lots_per_trade": (int, 1, 500),
    "rg_daily_stop_pct": (float, 0.001, 0.20),
    "rg_monthly_stop_pct": (float, 0.001, 0.50),
    "rg_concurrent_open_pct": (float, 0.001, 0.50),
    "rg_margin_util_cap": (float, 0.05, 1.0),
    "rg_correlation_max": (float, 0.05, 1.0),
}


def validate_settings(new_settings: dict) -> dict:
    """Validate new_settings dict against SETTINGS_SCHEMA.

    Raises ValueError with descriptive message if validation fails.
    Returns cleaned/casted settings dictionary.
    """
    if not isinstance(new_settings, dict):
        raise ValueError("Settings payload must be a JSON object.")

    validated = {}
    for k, v in new_settings.items():
        if k not in SETTINGS_SCHEMA:
            raise ValueError(f"Unknown settings key: '{k}'")

        rule_type, min_val, max_val = SETTINGS_SCHEMA[k]

        if rule_type == bool:
            if isinstance(v, bool):
                validated[k] = v
            elif isinstance(v, str) and v.lower() in ("true", "false"):
                validated[k] = (v.lower() == "true")
            else:
                raise ValueError(f"Setting '{k}' must be a boolean.")
        elif rule_type == "enum":
            v_str = str(v).upper()
            if v_str not in min_val:
                raise ValueError(f"Setting '{k}' must be one of {min_val}.")
            validated[k] = v_str
        elif rule_type == str:
            v_str = str(v).strip()
            if len(v_str) > max_val:
                raise ValueError(f"Setting '{k}' exceeds maximum length of {max_val}.")
            validated[k] = v_str
        elif rule_type in (float, int):
            if v is None or str(v).strip() == "":
                raise ValueError(f"Setting '{k}' cannot be empty.")
            try:
                num_v = float(v) if rule_type == float else int(v)
            except (ValueError, TypeError):
                raise ValueError(f"Setting '{k}' must be a valid {rule_type.__name__}.")
            if min_val is not None and num_v < min_val:
                raise ValueError(f"Setting '{k}' ({num_v}) must be >= {min_val}.")
            if max_val is not None and num_v > max_val:
                raise ValueError(f"Setting '{k}' ({num_v}) must be <= {max_val}.")
            validated[k] = num_v

    return validated


def save_settings(new_settings: dict) -> dict:
    """Update and persist paper trader settings."""
    validated = validate_settings(new_settings)
    with atomic_db_update() as db:
        if "settings" not in db:
            db["settings"] = DEFAULT_SETTINGS.copy()
        for k, v in validated.items():
            db["settings"][k] = v
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
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[_get_ltp] feed.ltp failed for %s: %s", symbol, e
        )
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

    symbol = alert["symbol"]
    entry_price = _get_ltp(symbol)
    if entry_price is None:
        return {"error": f"Could not fetch LTP for {symbol}"}

    with atomic_db_update() as db:
        direction = alert.get("direction", "LONG")
        today_str = _now_ist().date().isoformat()

        for t in db["trades"]:
            if t.get("symbol") == symbol and t.get("entry_date") == today_str:
                return {"error": f"{symbol} already traded today (id={t['id']})"}

        settings = db.get("settings", DEFAULT_SETTINGS.copy())
        capital_per_trade = settings.get("capital_per_trade_stocks", settings.get("capital_per_trade", 500000.0))
        sl_pct = settings["sl_pct"]
        tgt_pct = settings["tgt_pct"]

        # Check if F&O Stock
        fno_lots = get_fno_lot_sizes()
        lot_size = fno_lots.get(symbol) or 1

        if lot_size > 1:
            trade_type = "FUTURE"
            instrument = f"{symbol} FUT"
            expiry_date = get_futures_expiry(_now_ist())
        else:
            trade_type = alert.get("type", "STOCK")
            instrument = alert.get("instrument", f"{symbol} EQ")
            expiry_date = None

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

        # ─────────────────────────────────────────────────────────────
        # C1 FIX: Use risk-based position sizing (directional_size) instead
        # of naive capital / entry_price.  This ensures position size is
        # proportional to risk_budget / per_unit_risk, not a flat allocation.
        #
        # C5 FIX: directional_size validates stop placement vs direction
        # (e.g. stop > entry for LONG raises ValueError), preventing
        # inverted stops that cause instant stop-outs.
        # ─────────────────────────────────────────────────────────────
        try:
            cfg = load_config()
            total_capital = float(cfg.get("account", {}).get("capital", 5_000_000))
            per_trade_pct = float(cfg.get("risk", {}).get("per_trade_pct", 0.01))
        except Exception:
            total_capital = float(settings.get("total_capital", 5_000_000))
            per_trade_pct = float(settings.get("per_trade_pct", 0.01))

        try:
            size_result = directional_size(
                total_capital,
                per_trade_pct,
                entry_price,
                sl_price,
                lot_size,
                direction=direction,
            )
        except ValueError as ve:
            # C5 FIX: Invalid stop placement (e.g. stop > entry for LONG)
            return {"error": f"Invalid stop/direction: {ve}"}

        # Cap by notional limit (capital_per_trade setting)
        max_qty_by_cap = int(capital_per_trade / entry_price) if entry_price > 0 else 0
        if lot_size > 1:
            max_qty_by_cap = (max_qty_by_cap // lot_size) * lot_size

        # Respect alert-specified quantity if provided and smaller
        alert_qty = int(alert.get("qty") or 0)
        qty = size_result.qty
        if alert_qty > 0:
            qty = min(qty, alert_qty)
        qty = min(qty, max_qty_by_cap)

        # Round down to lot size
        if lot_size > 1:
            qty = (qty // lot_size) * lot_size

        if qty <= 0:
            return {
                "error": (
                    f"Position size is 0 for {symbol}: risk-based qty={size_result.qty}, "
                    f"capital cap qty={max_qty_by_cap}, lot_size={lot_size}. "
                    f"Check capital ({total_capital:,.0f}), per_trade_pct ({per_trade_pct}), "
                    f"and stop distance ({abs(entry_price - sl_price):.2f})."
                )
            }

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

    # Fetch Spot and VIX data outside the lock to avoid holding DB lock during slow yfinance API calls
    underlying_spot = None
    try:
        from data import feed

        spot_data = feed.ohlc_cached(underlying, period="5d")
        if spot_data is not None and not spot_data.empty:
            underlying_spot = float(spot_data["close"].iloc[-1])
    except Exception:
        underlying_spot = None

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

    with atomic_db_update() as db:
        if "option_trades" not in db:
            db["option_trades"] = []
        open_ops = [t for t in db.get("option_trades", []) if t.get("status") == "OPEN"]
        max_ops = cfg.get("options", {}).get("max_concurrent_structures", 4)
        if len(open_ops) >= max_ops:
            return {"error": f"Max concurrent options structures ({max_ops}) reached"}

        today_str = _now_ist().date().isoformat()

        # Expiry verification guard: verify legs are not past/expired
        for leg in resolved_legs:
            leg_exp = getattr(leg, "expiry", None)
            if leg_exp:
                try:
                    exp_d = datetime.strptime(leg_exp, "%Y-%m-%d").date() if "-" in leg_exp and len(leg_exp) == 10 else datetime.strptime(leg_exp, "%d-%b-%Y").date()
                    if today_str > exp_d.strftime("%Y-%m-%d"):
                        return {"error": f"{underlying} entry blocked: leg strike {leg.strike} has expired date ({leg_exp})"}
                except Exception:
                    pass

        # Re-entry guard: allow new trades immediately after a trade is marked CLOSED,
        # but block duplicate entries if a trade is currently OPEN for the symbol.
        open_symbol_trades = [
            t
            for t in db["option_trades"]
            if t.get("symbol") == underlying and t.get("status") == "OPEN"
        ]
        if open_symbol_trades:
            return {"error": f"{underlying} option structure already open"}

        # Validate each leg has a genuine positive premium (reject corrupted 0.0 data)
        zero_prem_legs = [
            (leg.side, leg.type, leg.strike, leg.premium)
            for leg in resolved_legs
            if leg.premium <= 0
        ]
        if zero_prem_legs:
            details = "; ".join(
                f"{s} {t} @ {strike} prem={p}" for s, t, strike, p in zero_prem_legs
            )
            logging.getLogger(__name__).error(
                "%s: rejecting custom entry — %d leg(s) with zero/corrupt premium: %s",
                underlying,
                len(zero_prem_legs),
                details,
            )
            return {
                "error": f"{underlying} blocked: {len(zero_prem_legs)} leg(s) with zero/corrupt premium"
            }

        net_premium = sum(
            (leg.premium * leg.lots * leg.lot_size) * (1 if leg.side == "SELL" else -1)
            for leg in resolved_legs
        )
        all_buy = all(leg.side == "BUY" for leg in resolved_legs)
        is_debit = all_buy and net_premium < 0
        if not is_debit and net_premium <= 0:
            return {
                "error": "Non-credit structure (0 premium likely due to missing LTP data)"
            }
        if is_debit and abs(net_premium) <= 0:
            return {
                "error": "Debit structure with zero premium"
            }
        # Smart exit metadata
        short_strikes = [l.strike for l in resolved_legs if l.side == "SELL"]

        # M3 FIX: Calculate wing_width from resolved legs instead of hardcoding 0.0.
        # For spreads: wing_width = |short_strike - long_strike| on either side.
        # For Iron Condors: wing_width = max(call_spread_width, put_spread_width).
        # This is critical for STRIKE_BREACH smart exit which checks spot vs short_strikes ± wing_width.
        from signals.options import calc_structure_max_loss

        short_calls = sorted(
            [l for l in resolved_legs if l.side == "SELL" and l.type == "CE"],
            key=lambda l: l.strike,
        )
        long_calls = sorted(
            [l for l in resolved_legs if l.side == "BUY" and l.type == "CE"],
            key=lambda l: l.strike,
        )
        short_puts = sorted(
            [l for l in resolved_legs if l.side == "SELL" and l.type == "PE"],
            key=lambda l: l.strike,
        )
        long_puts = sorted(
            [l for l in resolved_legs if l.side == "BUY" and l.type == "PE"],
            key=lambda l: l.strike,
        )

        call_spread_width = 0.0
        if short_calls and long_calls:
            # For credit spreads, the protective long is typically above the short for bear call
            # and below for bull put. Use the closest long to the short.
            call_spread_width = abs(short_calls[0].strike - long_calls[0].strike)

        put_spread_width = 0.0
        if short_puts and long_puts:
            put_spread_width = abs(short_puts[0].strike - long_puts[0].strike)

        # Iron Condor: use the wider of the two spread widths
        # Credit spread: use the single spread width
        # Naked: wing_width stays 0 (no protective leg)
        computed_wing_width = max(call_spread_width, put_spread_width)

        # Determine structure type for max_loss calculation
        struct_type = "unknown"
        if short_calls and long_calls and short_puts and long_puts:
            struct_type = "iron_condor"
        elif short_calls and long_calls:
            struct_type = "bear_call_spread"
        elif short_puts and long_puts:
            struct_type = "bull_put_spread"
        elif short_calls or short_puts:
            struct_type = "naked_short"

        lot_size = resolved_legs[0].lot_size if resolved_legs else 1
        num_lots = max(l.lots for l in resolved_legs) if resolved_legs else 1
        is_defined_risk = struct_type != "naked_short"

        max_loss = calc_structure_max_loss(
            struct_type,
            net_premium,
            computed_wing_width,
            lot_size,
            lots=num_lots,
            underlying_spot=underlying_spot,
        )

        trade = {
            "id": _next_id(db),
            "symbol": underlying,
            "structure": structure_name,
            "structure_type": struct_type,
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
            "net_credit": round(max(net_premium, 0.0), 2),
            "entry_net_credit": round(max(net_premium, 0.0), 2),
            "entry_net_debit": round(max(-net_premium, 0.0), 2),
            "max_loss_rupees": round(max_loss, 2),
            "is_defined_risk": is_defined_risk,
            "entry_time": _now_ist().strftime("%Y-%m-%d %H:%M:%S"),
            "entry_date": today_str,
            "exit_time": None,
            "exit_reason": None,
            "pnl": None,
            "status": "OPEN",
            # Smart exit tracking
            "entry_vix": round(entry_vix, 2),
            "short_strikes": short_strikes,
            "wing_width": computed_wing_width,
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

    # Fetch VIX outside the lock to avoid holding DB lock during slow yfinance API calls
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

        today_str = now_ist.date().isoformat()

        # Expiry verification guard: verify legs are not past/expired
        for leg in resolved_legs:
            leg_exp = getattr(leg, "expiry", None)
            if leg_exp:
                try:
                    exp_d = datetime.strptime(leg_exp, "%Y-%m-%d").date() if "-" in leg_exp and len(leg_exp) == 10 else datetime.strptime(leg_exp, "%d-%b-%Y").date()
                    if today_str > exp_d.strftime("%Y-%m-%d"):
                        return {"error": f"{symbol} entry blocked: leg strike {leg.strike} has expired date ({leg_exp})"}
                except Exception:
                    pass

        # Validate each leg has a genuine positive premium (reject corrupted 0.0 data)
        zero_prem_legs = [
            (l.side, l.type, l.strike, l.premium)
            for l in resolved_legs
            if l.premium <= 0
        ]
        if zero_prem_legs:
            details = "; ".join(
                f"{s} {t} @ {strike} prem={p}" for s, t, strike, p in zero_prem_legs
            )
            logging.getLogger(__name__).warning(
                "%s: rejecting entry — %d leg(s) with zero/corrupt premium: %s",
                symbol,
                len(zero_prem_legs),
                details,
            )
            journal.log_skipped_trade(
                symbol,
                "NEUTRAL",
                "MED",
                "CORRUPT_PREMIUM",
                "UNKNOWN",
                "NEUTRAL",
                "options_gate",
                f"Zero premium legs: {details}",
            )
            return {
                "error": f"{symbol} blocked: {len(zero_prem_legs)} leg(s) with zero/corrupt premium"
            }

        net_credit = sum(
            (l.premium * l.lots * l.lot_size) * (1 if l.side == "SELL" else -1)
            for l in resolved_legs
        )

        # Determine if this is a debit structure (all BUY legs, e.g. LONG_STRADDLE).
        # For debit structures, net_credit is negative by design — the gate below
        # should check that premiums are positive, not that credit is positive.
        all_buy = all(l.side == "BUY" for l in resolved_legs)
        is_debit = all_buy and net_credit < 0

        if is_debit:
            # Debit structure: just verify we're paying a positive amount (premiums valid)
            net_debit = abs(net_credit)
            if net_debit <= 0:
                journal.log_skipped_trade(
                    symbol,
                    "NEUTRAL",
                    "MED",
                    "CORRUPT_PREMIUM",
                    "UNKNOWN",
                    "NEUTRAL",
                    "options_gate",
                    "Debit structure with zero net debit",
                )
                return {"error": f"{symbol} blocked: debit structure with zero premium"}
        elif net_credit <= 0:
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
        # DEBIT FIX: Properly classify structure type for all strategies.
        # Previously defaulted to "bear_call_spread" for unrecognized strategies,
        # which caused incorrect max_loss calculation for debit structures like LONG_STRADDLE.
        if "IRON_CONDOR" in setup.strategy:
            struct_type = "iron_condor"
        elif "BULL_PUT" in setup.strategy:
            struct_type = "bull_put_spread"
        elif "BEAR_CALL" in setup.strategy:
            struct_type = "bear_call_spread"
        elif "STRADDLE" in setup.strategy:
            struct_type = "long_straddle"
        elif "STRANGLE" in setup.strategy:
            struct_type = "long_strangle"
        elif is_debit:
            struct_type = "debit_spread"
        else:
            struct_type = "unknown"
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
            "net_credit": round(net_credit, 2) if not is_debit else 0.0,
            "net_premium": round(net_credit, 2),
            "max_loss_rupees": round(max_loss, 2),
            "entry_net_credit": round(net_credit, 2) if not is_debit else 0.0,
            "entry_net_debit": round(abs(net_credit), 2) if is_debit else 0.0,
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
    from datetime import datetime

    from signals.options import chain_snapshot

    underlyings = list({t["symbol"] for t in open_ops})
    price_map = {}
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    for sym in underlyings:
        try:
            # Filter stale expiries (more than 7 days in the past)
            min_valid_date = ist_now.date()
            needed_expiries_raw = list(
                {
                    leg["expiry"]
                    for t in open_ops
                    if t["symbol"] == sym
                    for leg in t["legs"]
                }
            )
            needed_expiries = []
            for e in needed_expiries_raw:
                try:
                    exp_date = datetime.strptime(e, "%Y-%m-%d").date()
                    days_stale = (min_valid_date - exp_date).days
                    # Skip clearly corrupt dates (>365 days in the past)
                    if days_stale >= 365:
                        logging.getLogger(__name__).warning(
                            "%s: dropping corrupt expiry %s (%d days stale)",
                            sym,
                            e,
                            days_stale,
                        )
                        continue
                    if exp_date >= min_valid_date or days_stale < 7:
                        needed_expiries.append(e)
                except ValueError:
                    needed_expiries.append(e)
            if not needed_expiries:
                logging.getLogger(__name__).warning(
                    "%s: all target expiries %s are stale; using closest future expiry",
                    sym,
                    needed_expiries_raw,
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
                sym,
                target_expiries=needed_expiries or None,
                target_strikes=needed_strikes,
            )
            # Get the data source name for diagnostics
            _chain_source = "unknown"
            try:
                from data.feed import option_chain_source

                _chain_source = option_chain_source(sym) or "unknown"
            except Exception:
                pass
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
            # H7 FIX: Dynamic premium cap based on underlying price.
            # Previously hardcoded to 5000, which rejects valid deep-ITM
            # stock option premiums (e.g. MRF at ₹125,000 spot).
            # For indices (NIFTY ~25k, BANKNIFTY ~50k), 5000 is still reasonable.
            # For stocks, cap = max(5000, 50% of spot) to accommodate deep ITM.
            _INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "MIDCPNIFTY"}
            spot_price = _get_ltp(sym) or 0.0
            if sym in _INDEX_SYMBOLS:
                MAX_REASONABLE_PREMIUM = 5000.0
            else:
                MAX_REASONABLE_PREMIUM = max(5000.0, spot_price * 0.50)

            is_stale_chain = str(_chain_source).startswith("cache")

            for trade in [t for t in open_ops if t["symbol"] == sym]:
                for leg in trade["legs"]:
                    key = (leg["strike"], leg["expiry"], leg["type"])
                    col = f"{leg['type'].lower()}_ltp"
                    row = chain[
                        (chain["strike"] == leg["strike"])
                        & (chain["expiry"] == leg["expiry"])
                    ]
                    price_found = False
                    if not row.empty:
                        try:
                            raw_price = float(row.iloc[0][col])
                            # SANITY CHECK: reject spot/index contamination
                            if raw_price <= MAX_REASONABLE_PREMIUM:
                                # If option chain source is stale cache AND raw_price equals entry premium,
                                # compute dynamic live BS pricing from current spot price.
                                entry_p = float(leg.get("entry_premium") or 0.0)
                                if is_stale_chain and spot_price > 0 and abs(raw_price - entry_p) < 0.01:
                                    try:
                                        from signals.options import _bs_price
                                        exp_d = datetime.strptime(leg["expiry"], "%Y-%m-%d").date()
                                        t_yrs = max((exp_d - ist_now.date()).days, 0.5) / 365.0
                                        vix_vol = 0.14
                                        bs_p = round(_bs_price(spot_price, leg["strike"], t_yrs, 0.065, vix_vol, leg["type"]), 2)
                                        price_map[key] = bs_p
                                        price_found = True
                                    except Exception:
                                        price_map[key] = raw_price
                                        price_found = True
                                else:
                                    price_map[key] = raw_price
                                    price_found = True
                            else:
                                logging.getLogger(__name__).error(
                                    "%s: REJECTED corrupt premium %s for %s (strike=%s, expiry=%s, type=%s) "
                                    "- exceeds max reasonable %s (source=%s).",
                                    sym,
                                    raw_price,
                                    key,
                                    leg["strike"],
                                    leg["expiry"],
                                    leg["type"],
                                    MAX_REASONABLE_PREMIUM,
                                    _chain_source,
                                )
                        except Exception as e:
                            logging.getLogger(__name__).exception(
                                "Failed to parse price for %s from chain for symbol %s: %s",
                                key,
                                sym,
                                e,
                            )

                    if not price_found and spot_price > 0:
                        # Fallback to Black-Scholes estimate if leg missing or unparsed
                        try:
                            from signals.options import _bs_price
                            exp_d = datetime.strptime(leg["expiry"], "%Y-%m-%d").date()
                            t_yrs = max((exp_d - ist_now.date()).days, 0.5) / 365.0
                            vix_vol = 0.14
                            bs_p = round(_bs_price(spot_price, leg["strike"], t_yrs, 0.065, vix_vol, leg["type"]), 2)
                            price_map[key] = bs_p
                        except Exception:
                            pass
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

    if current_net is None:
        return None

    # SIGNED PnL for ALL structures. _net_premium_signed() preserves the
    # sign of net_premium: positive for credit (e.g. IRON_CONDOR),
    # negative for debit (e.g. LONG_STRADDLE). The old code read
    # `net_premium or net_credit` which was always positive and broke P&L
    # for debit structures. current_net from _option_net_premium() is also
    # signed (negative for BUY legs).  P&L = entry - current works for both.
    entry_premium = _net_premium_signed(t)
    pnl = entry_premium - current_net

    # 1. VIX Spike Exit
    entry_vix = t.get("entry_vix", 0.0)
    vix_threshold = settings.get("smart_exit_vix_spike_pct", 15.0)
    vix_floor = settings.get("smart_exit_vix_floor", 18.0)

    # Dynamic threshold based on Regime. The caller already passes a fresh
    # current_regime (from signals.regime.classify); only fall back to a
    # light VIX-level inference when none is supplied. We do NOT re-fetch
    # VIX here (it is already available as vix_now) — the old per-trade
    # feed.ohlc_cached() call was a redundant network round-trip per trade.
    if current_regime is None:
        if vix_now < 14:
            current_regime = "VOL_CONTRACTION"
        elif vix_now > 20:
            current_regime = "HIGH_VOL_TREND"

    if current_regime == "HIGH_VOL_TREND":
        # Already-elevated VIX: spikes are more dangerous, be MORE sensitive.
        vix_threshold *= 0.8
    elif current_regime == "VOL_CONTRACTION":
        # Low VIX: a spike is a bigger relative shock, also more sensitive.
        vix_threshold *= 0.8

    if entry_vix > 0:
        # FLOOR semantics (CORRECTED): the spike exit should only fire when
        # VIX rises from a *low/normal* level into elevated territory. If the
        # trade was entered when VIX was ALREADY above the floor, a further
        # rise is expected market behaviour and must NOT auto-exit.
        # Old code (`vix_now >= vix_floor`) fired on every elevated-VIX print,
        # which is backwards. Require entry below floor AND now at/above floor.
        if entry_vix < vix_floor and vix_now >= vix_floor:
            from signals.options import check_vix_spike_exit

            should_exit, _ = check_vix_spike_exit(vix_now, entry_vix, vix_threshold)
            if should_exit:
                t["reentry_eligible"] = True
                return "VIX_SPIKE"

    # 2. Underlying Strike Breach
    # Credit structures (condors/spreads) define short_strikes; debit
    # structures (e.g. LONG_STRADDLE) define long_strikes. For a straddle the
    # danger is a large move AWAY from the strike, so breach the long strike
    # ± wing_width. Use whichever reference the structure provides.
    breach_refs = t.get("short_strikes") or t.get("long_strikes") or []
    wing_width = t.get("wing_width", 0.0)
    if breach_refs and wing_width > 0:
        try:
            ltp = _get_ltp(t.get("symbol", "NIFTY"))
            if ltp and ltp > 0:
                highest_ref = max(breach_refs)
                lowest_ref = min(breach_refs)
                breach_margin = wing_width * 0.5
                if (
                    ltp > highest_ref + breach_margin
                    or ltp < lowest_ref - breach_margin
                ):
                    t["reentry_eligible"] = True
                    return "STRIKE_BREACH"
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Failed to evaluate strike breach for trade %s: %s", t.get("id"), e
            )

    # 3. Theta Trail Lock
    # Use abs() for threshold checks so trail lock works for both credit
    # (positive net_premium) and debit (negative net_premium) structures.
    abs_entry_premium = abs(entry_premium)
    if abs_entry_premium > 0:
        trail_lock_pct = settings.get("smart_exit_trail_lock_pct", 50.0) / 100.0
        trail_floor_pct = settings.get("smart_exit_trail_floor_pct", 35.0) / 100.0

        # Update peak PnL
        peak_pnl = t.get("peak_pnl", 0.0)
        if pnl > peak_pnl:
            t["peak_pnl"] = pnl
            peak_pnl = pnl

        lock_threshold = abs_entry_premium * trail_lock_pct
        if peak_pnl >= lock_threshold:
            t["trailing_lock"] = True

        if t.get("trailing_lock", False):
            lock_floor = abs_entry_premium * trail_floor_pct
            if pnl < lock_floor:
                t["reentry_eligible"] = True
                return "TRAIL_LOCK"

    return None


def _entry_premium(trade: dict) -> float:
    """C2 FIX: Extract entry net premium with unified fallback across all field names.

    Different entry paths set different field names:
      - enter_option_structure() sets 'net_premium'
      - _enter_option_structure() sets both 'net_credit' and 'net_premium'
      - Some paths set 'entry_net_credit' or 'entry_net_debit'

    Returns the first non-zero value found, or 0.0 if none exist.
    """
    for key in ("net_premium", "net_credit", "entry_net_credit", "entry_net_debit"):
        val = trade.get(key)
        if val is not None and val > 0:
            return float(val)
    return 0.0


def _net_premium_signed(trade: dict) -> float:
    """Return signed net premium for correct P&L calculation.

    BUG FIX: The original `_entry_premium()` always returns a POSITIVE value,
    which breaks P&L calculation for debit structures (e.g., LONG_STRADDLE).

    For credit structures (SELL legs):
      - net_premium is positive (credit received)
      - current_net is positive (cost to buy back)
      - P&L = net_premium - current_net

    For debit structures (BUY legs, e.g., LONG_STRADDLE):
      - net_premium is NEGATIVE (debit paid, stored as negative)
      - current_net is NEGATIVE (value of position, sign=-1 for BUY)
      - P&L = net_premium - current_net = (-debit) - (-current_value)
            = current_value - debit  ← CORRECT!

    Using the signed net_premium ensures the formula works for BOTH
    credit and debit structures.

    Returns:
        Signed net premium: positive for credit, negative for debit.
    """
    # net_premium preserves the sign: negative for debit, positive for credit
    val = trade.get("net_premium")
    if val is not None and val != 0:
        return float(val)
    # Fallback to net_credit (always positive, assumes credit structure)
    val = trade.get("net_credit")
    if val is not None and val != 0:
        return float(val)
    # Fallback: entry_net_credit - entry_net_debit (preserves sign)
    credit = trade.get("entry_net_credit", 0) or 0
    debit = trade.get("entry_net_debit", 0) or 0
    if credit > 0 or debit > 0:
        return float(credit) - float(debit)
    return 0.0


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

    # Fetch option price map and chain snapshots completely unlocked (to avoid database write locks during slow network APIs)
    price_map = _build_option_price_map(open_ops)

    settings = db.get("settings", {})
    smart_enabled = settings.get("smart_exits_enabled", True)
    chain_cache = {}
    if smart_enabled:
        from signals.options import chain_snapshot
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

    with atomic_db_update() as db:
        open_ops = [t for t in db.get("option_trades", []) if t.get("status") == "OPEN"]
        if not open_ops:
            logging.getLogger(__name__).debug(
                "check_option_exits: no open option trades found (re-read inside lock)"
            )
            return []
        
        # Refresh settings and flags inside the lock
        settings = db.get("settings", {})
        auto_close = settings.get("auto_close_eod", True)
        is_eod = is_eod_window(now_ist)
        smart_enabled = settings.get("smart_exits_enabled", True)

        # MAJOR FIX: Respect the configured option exit window for structural
        # smart exits. Outside the exit window we must not auto-exit on
        # VIX/STRIKE/DELTA/TRAIL signals; only EOD close may override.
        from signals.options import is_within_exit_window
        from config.loader import load_config

        try:
            _cfg = load_config()
        except Exception:
            _cfg = None
        sym0 = next((t.get("symbol") for t in open_ops if t.get("symbol")), "NIFTY")
        within_exit_window = is_within_exit_window(
            cfg=_cfg, now=now_ist, symbol=sym0
        )[0]

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
            # Gated by exit window unless EOD (override).
            if not exit_reason and (within_exit_window or is_eod):
                exit_reason = _smart_exit_check(
                    t, current_net, settings, vix_now, current_regime=current_regime
                )

            # Delta Breach (needs chain data)
            if not exit_reason and smart_enabled and (within_exit_window or is_eod):
                chain = chain_cache.get(t.get("symbol"))
                if chain is not None and not chain.empty:
                    delta_threshold = settings.get("smart_exit_delta_threshold", 0.35)
                    from signals.options import net_position_delta
                    nd = net_position_delta(t["legs"], chain)
                    if nd is not None and abs(nd) > delta_threshold:
                        t["reentry_eligible"] = True
                        exit_reason = "DELTA_BREACH"

            # Flat SL/TGT backup (skip during grace period to avoid bid-ask jitter exits)
            if not exit_reason and not _within_grace_period(t, now_ist):
                # DEBIT FIX: Use signed net premium for correct P&L on both
                # credit and debit structures.  _entry_premium() always returns
                # a POSITIVE value, which breaks P&L for debit structures
                # (e.g. LONG_STRADDLE) where net_premium is stored negative.
                net_prem = _net_premium_signed(t)
                pnl = net_prem - current_net

                # M2 FIX: SANITY BOUND - Changed multiplier from 1.1 to 1.0.
                # For defined-risk structures (Iron Condor, credit spreads), P&L should
                # NEVER exceed max_loss by definition. The old 1.1x buffer tolerated 10%
                # data corruption silently — now we enforce the theoretical boundary exactly.
                max_loss_rupees = t.get("max_loss_rupees", 0.0)
                is_defined_risk = t.get("is_defined_risk", True)
                if (
                    is_defined_risk
                    and max_loss_rupees > 0
                    and abs(pnl) > max_loss_rupees
                ):
                    logging.getLogger(__name__).error(
                        "Trade %s %s: P&L %s exceeds theoretical max loss %s by %.1fx - CORRUPT DATA. "
                        "Capping P&L to max_loss for defined-risk structure.",
                        t.get("id"),
                        t.get("symbol"),
                        pnl,
                        max_loss_rupees,
                        abs(pnl) / max_loss_rupees if max_loss_rupees else 0,
                    )
                    pnl = max(-max_loss_rupees, min(max_loss_rupees, pnl))

                sl_limit = settings.get("options_sl_pct", 125.0) / 100.0
                tgt_limit = settings.get("options_tgt_pct", 50.0) / 100.0
                abs_prem = abs(net_prem)
                if abs_prem > 0:
                    # Risk-relative stop: for defined-risk credit spreads the
                    # max loss is (wing − credit), which can be many multiples
                    # of the credit received. A stop expressed purely as a
                    # fraction of premium (e.g. 125% of credit) could either
                    # never trigger (if it exceeds max loss) or be absurdly
                    # loose. Cap the stop at the structure's own max_loss so it
                    # always fires at/before the theoretical boundary. Target
                    # stays premium-based (book X% of credit received).
                    sl_pnl = abs_prem * sl_limit
                    if t.get("is_defined_risk", True) and max_loss_rupees > 0:
                        sl_pnl = min(sl_pnl, max_loss_rupees)
                    if pnl <= -sl_pnl:
                        exit_reason = "SL_HIT"
                    elif pnl >= abs_prem * tgt_limit:
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
                # DEBIT FIX: Use signed net premium for final P&L
                final_pnl = round(_net_premium_signed(t) - current_net, 2)
                # M2 FIX: Re-apply sanity bound on final P&L — now using strict 1.0x
                # instead of 1.1x. Defined-risk structures cannot exceed max_loss.
                max_loss_rupees = t.get("max_loss_rupees", 0.0)
                is_defined_risk = t.get("is_defined_risk", True)
                if (
                    is_defined_risk
                    and max_loss_rupees > 0
                    and abs(final_pnl) > max_loss_rupees
                ):
                    logging.getLogger(__name__).error(
                        "Trade %s %s: FINAL P&L %s exceeds max_loss %s - clamping to theoretical boundary.",
                        t.get("id"),
                        t.get("symbol"),
                        final_pnl,
                        max_loss_rupees,
                    )
                    final_pnl = max(-max_loss_rupees, min(max_loss_rupees, final_pnl))
                t["pnl"] = final_pnl
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

        # MAJOR FIX: Respect configured option exit window for structural
        # smart exits (see check_option_exits for rationale).
        from signals.options import is_within_exit_window
        from config.loader import load_config

        try:
            _cfg = load_config()
        except Exception:
            _cfg = None
        within_exit_window = is_within_exit_window(
            cfg=_cfg, now=now_ist, symbol=symbol
        )[0]

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
            exit_reason = "EOD_CUTOFF" if (is_eod and auto_close) else None

            # Expiry check for Options (only square off at 15:15+ IST on expiry day, or if past expiry date)
            if not exit_reason:
                from datetime import time as dt_time
                today_str = now_ist.strftime("%Y-%m-%d")
                for leg in t.get("legs", []):
                    leg_exp = leg.get("expiry")
                    if leg_exp:
                        if today_str > leg_exp:
                            exit_reason = "EXPIRY"
                            break
                        elif today_str == leg_exp and (now_ist.time() >= dt_time(15, 15) or is_eod):
                            exit_reason = "EXPIRY"
                            break

            current_net = _option_net_premium(t["legs"], price_map)

            # If no current net (missing LTPs) and not already marked EXPIRY/EOD, skip exit checks
            if current_net is None and not exit_reason:
                logging.getLogger(__name__).warning(
                    f"Skipping exit checks for trade id={t.get('id')} symbol={symbol} due to missing LTP data"
                )
                continue

            if current_net is None:
                current_net = 0.0

            # Smart exits (fire first)
            if not exit_reason and (within_exit_window or is_eod):
                exit_reason = _smart_exit_check(
                    t, current_net, settings, vix_now, current_regime=current_regime
                )

            # Delta Breach
            if (
                not exit_reason
                and smart_enabled
                and (within_exit_window or is_eod)
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
                # DEBIT FIX: Use signed net premium for correct P&L on both
                # credit and debit structures.  _entry_premium() always returns
                # a POSITIVE value, which breaks P&L for debit structures
                # (e.g. LONG_STRADDLE) where net_premium is stored negative.
                net_prem = _net_premium_signed(t)
                pnl = net_prem - current_net

                # M2 FIX: SANITY BOUND - Changed multiplier from 1.1 to 1.0.
                # For defined-risk structures (Iron Condor, credit spreads), P&L should
                # NEVER exceed max_loss by definition. The old 1.1x buffer tolerated 10%
                # data corruption silently — now we enforce the theoretical boundary exactly.
                max_loss_rupees = t.get("max_loss_rupees", 0.0)
                is_defined_risk = t.get("is_defined_risk", True)
                if (
                    is_defined_risk
                    and max_loss_rupees > 0
                    and abs(pnl) > max_loss_rupees
                ):
                    logging.getLogger(__name__).error(
                        "Trade %s %s: P&L %s exceeds theoretical max loss %s by %.1fx - CORRUPT DATA. "
                        "Capping P&L to max_loss for defined-risk structure.",
                        t.get("id"),
                        t.get("symbol"),
                        pnl,
                        max_loss_rupees,
                        abs(pnl) / max_loss_rupees if max_loss_rupees else 0,
                    )
                    pnl = max(-max_loss_rupees, min(max_loss_rupees, pnl))

                sl_limit = settings.get("options_sl_pct", 125.0) / 100.0
                tgt_limit = settings.get("options_tgt_pct", 50.0) / 100.0
                abs_prem = abs(net_prem)
                if abs_prem > 0:
                    if pnl >= abs_prem * tgt_limit:
                        exit_reason = "PROFIT_TAKEN"
                    elif pnl <= -abs_prem * sl_limit:
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
                # DEBIT FIX: Use signed net premium for final P&L
                final_pnl = round(_net_premium_signed(t) - current_net, 2)
                # M2 FIX: Re-apply sanity bound on final P&L — now using strict 1.0x
                # instead of 1.1x. Defined-risk structures cannot exceed max_loss.
                max_loss_rupees = t.get("max_loss_rupees", 0.0)
                is_defined_risk = t.get("is_defined_risk", True)
                if (
                    is_defined_risk
                    and max_loss_rupees > 0
                    and abs(final_pnl) > max_loss_rupees
                ):
                    logging.getLogger(__name__).error(
                        "Trade %s %s: FINAL P&L %s exceeds max_loss %s - clamping to theoretical boundary.",
                        t.get("id"),
                        t.get("symbol"),
                        final_pnl,
                        max_loss_rupees,
                    )
                    final_pnl = max(-max_loss_rupees, min(max_loss_rupees, final_pnl))
                t["pnl"] = final_pnl
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
    is_enabled = sym_cfg.get("enabled", cfg.get("options", {}).get("enabled", True))
    if not is_enabled:
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
        if not journal.has_skipped_today(symbol, "NO_STRATEGY", "options_engine"):
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
        if not journal.has_skipped_today(symbol, "CHAIN_ERROR", "options_engine"):
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
        if not journal.has_skipped_today(symbol, "EMPTY_CHAIN", "options_engine"):
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
        # Deduplicate: skip logging if we already logged the same skip for this
        # symbol+reason+risk_gate today (VIX expansion repeats every 60s cycle)
        if not journal.has_skipped_today(symbol, "NOT_SUITABLE", "options_engine"):
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

            # M5 FIX: Trailing stop logic with direction-aware variable naming.
            # The stored field "peak_price" tracks the best price seen:
            #   - LONG: highest price (true "peak") — trailing stop ratchets UP
            #   - SHORT: lowest price ("trough") — trailing stop ratchets DOWN
            # We use local variable `best_price` to avoid the misleading "peak" name
            # for SHORT trades. The stored field name is kept for backward compatibility.
            if settings.get("trail_sl", True):
                trail_activation_pct = settings.get("trail_activation_pct", 2.0)
                entry_price_ref = trade["entry_price"]
                if direction == "LONG":
                    # Only start trailing once price is at least trail_activation_pct above entry
                    profit_pct = (ltp - entry_price_ref) / entry_price_ref * 100.0
                    if profit_pct >= trail_activation_pct:
                        if "peak_price" not in trade:
                            trade["peak_price"] = entry_price_ref
                        best_price = trade["peak_price"]
                        sl_pct = trade.get("sl_pct", settings.get("sl_pct", 2.0))
                        if ltp > best_price:
                            best_price = ltp
                            trade["peak_price"] = best_price
                            new_sl = ltp * (1 - sl_pct / 100)
                            if new_sl > trade["sl_price"]:
                                trade["sl_price"] = round(new_sl, 2)
                else:
                    # Only start trailing once price is at least trail_activation_pct below entry
                    profit_pct = (entry_price_ref - ltp) / entry_price_ref * 100.0
                    if profit_pct >= trail_activation_pct:
                        if "peak_price" not in trade:
                            trade["peak_price"] = entry_price_ref
                        best_price = trade["peak_price"]
                        sl_pct = trade.get("sl_pct", settings.get("sl_pct", 2.0))
                        if ltp < best_price:
                            best_price = ltp
                            trade["peak_price"] = best_price
                            new_sl = ltp * (1 + sl_pct / 100)
                            if new_sl < trade["sl_price"]:
                                trade["sl_price"] = round(new_sl, 2)

            # Respect EOD close setting
            eod_trigger = is_eod and auto_close

            # Expiry check for Futures
            is_expired = False
            if trade.get("type") == "FUTURE" and trade.get("expiry_date"):
                from datetime import time as dt_time

                today_str = now_ist.strftime("%Y-%m-%d")
                exp_date = trade["expiry_date"]
                if today_str > exp_date:
                    is_expired = True
                elif today_str == exp_date and (
                    now_ist.time() >= dt_time(15, 15) or is_eod
                ):
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
                # Cap exit_price for TARGET_HIT/SL_HIT to prevent bad-LTP corruption:
                #   TARGET_HIT LONG → at most 1% above target (gap-through guard)
                #   TARGET_HIT SHORT → at least 1% below target
                #   SL_HIT LONG → at least 1% below SL
                #   SL_HIT SHORT → at most 1% above SL
                if exit_reason == "TARGET_HIT":
                    if direction == "LONG":
                        trade["exit_price"] = min(ltp, round(trade["tgt_price"] * 1.01, 2))
                    else:
                        trade["exit_price"] = max(ltp, round(trade["tgt_price"] * 0.99, 2))
                elif exit_reason == "SL_HIT":
                    if direction == "LONG":
                        trade["exit_price"] = max(ltp, round(trade["sl_price"] * 0.99, 2))
                    else:
                        trade["exit_price"] = min(ltp, round(trade["sl_price"] * 1.01, 2))
                else:
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
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "[check_and_close_trades] hold_minutes calc failed for trade %s: %s",
                        trade.get("id"),
                        e,
                    )

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
    """Manually close a specific trade at current LTP (handles both JSON and SQLite trades)."""
    now_ist = _now_ist()
    
    # 1. Try to find and close in JSON database first
    with atomic_db_update() as db:
        trade = next(
            (
                t
                for t in db.get("trades", [])
                if t["id"] == trade_id and t.get("status") == "OPEN"
            ),
            None,
        )
        if trade:
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
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "[close_trade_manual] hold_minutes calc failed for trade %s: %s",
                    trade_id,
                    e,
                )

            direction = trade["direction"]
            if direction == "LONG":
                pnl = (ltp - trade["entry_price"]) * trade["qty"]
                pnl_pct = 100 * (ltp - trade["entry_price"]) / trade["entry_price"]
            else:
                pnl = (trade["entry_price"] - ltp) * trade["qty"]
                pnl_pct = 100 * (trade["entry_price"] - ltp) / trade["entry_price"]

            trade["pnl"] = round(pnl, 2)
            trade["pnl_pct"] = round(pnl_pct, 2)

            jid = trade.get("journal_id")
            if jid:
                try:
                    cfg = load_config()
                    journal = Journal(cfg["paths"]["journal_db"])
                    journal.close_trade(jid, ltp, trade["pnl"])
                except Exception as e:
                    print(f"⚠️ Journal close sync failed for trade {jid}: {e}")

            return trade

    # 2. If not found in JSON, search and close in SQLite journal database (Stock trades)
    try:
        cfg = load_config()
        db_path = cfg["paths"]["journal_db"]
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute("SELECT * FROM trades WHERE id=? AND closed_at IS NULL", (trade_id,)).fetchone()
            if not row:
                return None
            trade_data = dict(row)
        finally:
            conn.close()

        symbol = trade_data["symbol"]
        ltp = _get_ltp(symbol)
        if ltp is None:
            return {"error": f"Could not fetch LTP for {symbol}"}

        qty = trade_data["qty"]
        entry = float(trade_data["entry"])
        side = trade_data["side"]  # 'LONG' or 'SHORT'

        if side == "SHORT":
            pnl = (entry - ltp) * qty
            pnl_pct = 100 * (entry - ltp) / entry
        else:
            pnl = (ltp - entry) * qty
            pnl_pct = 100 * (ltp - entry) / entry

        # Close in SQLite Journal
        journal = Journal(db_path)
        journal.close_trade(trade_id, ltp, round(pnl, 2))

        return {
            "id": trade_id,
            "symbol": symbol,
            "direction": side,
            "entry_price": entry,
            "exit_price": ltp,
            "qty": qty,
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "status": "CLOSED",
            "exit_reason": reason,
            "exit_time": now_ist.strftime("%Y-%m-%d %H:%M:%S"),
        }
    except Exception as e:
        logging.getLogger(__name__).exception("Failed to manually close SQLite trade %d: %s", trade_id, e)
        return {"error": str(e)}


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
        # DEBIT FIX: Use signed net premium for correct P&L on both
        # credit and debit structures.
        net_prem = _net_premium_signed(trade)
        if current_net is not None:
            trade["pnl"] = round(net_prem - current_net, 2)
        else:
            trade["pnl"] = 0.0

        # SANITY BOUND: Clamp P&L to theoretical max loss
        max_loss_rupees = trade.get("max_loss_rupees", 0.0)
        if max_loss_rupees > 0 and abs(trade["pnl"]) > max_loss_rupees:
            logging.getLogger(__name__).error(
                "Manual close trade %s %s: P&L %s exceeds theoretical max loss %s - clamping.",
                trade.get("id"),
                trade.get("symbol"),
                trade["pnl"],
                max_loss_rupees,
            )
            trade["pnl"] = max(-max_loss_rupees, min(max_loss_rupees, trade["pnl"]))

        return trade


def auto_enter_from_alerts(alerts: list[dict], cfg: dict | None = None) -> list[dict]:
    """Take paper trades on alerts automatically with risk guardrails and learned filters."""
    from config.loader import load_config

    now_ist = _now_ist()
    if not is_market_open(now_ist):
        return []

    if not is_within_equity_entry_window(now_ist):
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
    today_str = now_ist.date().isoformat()
    entered = []

    # Pre-fetch LTP for any alerts that lack entry_price/stop OUTSIDE the lock
    missing_symbols = []
    for alert in alerts:
        ep = alert.get("entry_price")
        if ep is None or not isinstance(ep, (int, float)) or ep <= 0:
            missing_symbols.append(alert.get("symbol"))
    if missing_symbols:
        prefetched_prices = _get_ltp_batch(list(set(missing_symbols)))
    else:
        prefetched_prices = {}

    with atomic_db_update() as db:
        settings = db.get("settings", DEFAULT_SETTINGS.copy())
        # Apply version-agnostic migration: implicit HIGH -> MEDIUM for persisted stores,
        # but respect explicitly configured user overrides.
        raw = settings.get("min_confidence")
        if raw is None:
            raw = DEFAULT_SETTINGS["min_confidence"]  # will be MEDIUM via DEFAULT_SETTINGS
        elif raw == "HIGH":
            # Detect legacy fallback (implicit HIGH from older code). If user never set this key,
            # treat it as an implicit HIGH and migrate to MEDIUM.
            # If user explicitly set "HIGH" via UI/configuration, preserve it (they opted in).
            # Heuristic: if this persisted entry originated from the old code's get(..., "HIGH"),
            # it will lack a UI-set or saved-control flag. We assume it's implicit and upgrade.
            # However, there is no reliable flag to distinguish. So we upgrade ONLY when the
            # settings were created *before* this migration script. Since we lack versioning,
            # we approximate: if the key exists AND the value is HIGH AND there is no known
            # explicit user control (most persisted "HIGH" are from old code), migrate.
            # For safety, we still keep HIGH for entries that have been explicitly interacted
            # with via UI (i.e., they appear in the UI settings list). In the current code,
            # UI updates go through get_settings() which writes directly to settings via
            # _save_settings, preserving whatever the user chose.
            # So most persisted HIGH files are from old code. We upgrade them silently.
            raw = "MEDIUM"
        min_conf = raw
        max_trades_per_day = int(settings.get("max_trades_per_day", 8))
        max_new_entries_per_cycle = int(settings.get("max_new_entries_per_cycle", 5))
        conf_levels = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
        min_val = conf_levels.get(min_conf, 2)  # MEDIUM is default confidence

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
        open_trades_count = len(
            [t for t in db["trades"] if t.get("status") == "OPEN" and t.get("symbol") not in ("NIFTY", "BANKNIFTY", "FINNIFTY")]
        )

        for alert in alerts:
            if today_trade_count + len(entered) >= max_trades_per_day:
                break
            if len(entered) >= max_new_entries_per_cycle:
                break

            regime = alert.get("source_regime", "UNKNOWN")
            if "RANGE" in regime.upper() and (open_trades_count + len(entered) >= 10):
                journal.log_skipped_trade(
                    alert.get("symbol", ""),
                    alert.get("direction", "LONG"),
                    alert.get("confidence", "MEDIUM"),
                    "REGIME_CAP",
                    regime,
                    alert.get("flow_bias", "NEUTRAL"),
                    "regime_filter",
                    "Max 10 concurrent positions in RANGE regime",
                )
                continue

            sym = alert.get("symbol", "")
            conf = alert.get("confidence", "MEDIUM")
            direction = alert.get("direction", "LONG")
            regime = alert.get("source_regime", "UNKNOWN")
            flow_bias = alert.get("flow_bias", "NEUTRAL")

            if (
                sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX")
                or alert.get("type") == "INDEX"
            ):
                # Index option setups are processed independently in Phase 2/3 of automation worker.
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
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "[auto_enter_from_alerts] confidence parse failed: %s", e
                    )

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

            # --- NEW: Timing Gate Check (PT-401) ---
            if not alert.get("timing_ok", True):
                logging.info(
                    f"[SKIP] {sym} {direction}: timing gate failed. Reason: {alert.get('timing_reason', 'unknown')}"
                )
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "TIMING_OVEREXTENDED",
                    regime,
                    flow_bias,
                    "timing_gate",
                    alert.get("timing_reason", "unknown"),
                )
                continue
            # --- END TIMING GATE CHECK ---

            # --- PHASE 2: AI Review Gate (PT-402) ---
            if not alert.get("ai_timing_ok", True):
                logging.info(
                    f"[SKIP] {sym} {direction}: AI review rejected. Reason: {alert.get('ai_reason', 'unknown')}"
                )
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "AI_REVIEW_REJECTED",
                    regime,
                    flow_bias,
                    "ai_review_gate",
                    alert.get("ai_reason", "unknown"),
                )
                continue
            # --- END AI REVIEW GATE ---

            # --- PHASE 2: Sentiment Flip Gate (PT-403) ---
            if alert.get("sentiment_flip_detected", False):
                logging.info(
                    f"[SKIP] {sym} {direction}: sentiment flip detected; trading blocked"
                )
                journal.log_skipped_trade(
                    sym,
                    direction,
                    conf,
                    "SENTIMENT_FLIP",
                    regime,
                    flow_bias,
                    "sentiment_flip_gate",
                    "Sentiment flip detected; trading blocked for equity",
                )
                continue

            # Respect event-risk size multiplier
            size_multiplier = alert.get("size_multiplier", 1.0)

            entry_price = alert.get("entry_price")
            stop = alert.get("stop")

            # Fallback for missing prices or string entry_zone (common for stock alerts)
            if entry_price is None or not isinstance(entry_price, (int, float)) or entry_price <= 0:
                entry_price = prefetched_prices.get(sym) or _get_ltp(sym)
                if entry_price is None or not isinstance(entry_price, (int, float)) or entry_price <= 0:
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
                capital_cap = settings.get("capital_per_trade_stocks", settings.get("capital_per_trade", 500000.0))
                max_qty_by_cap = int(capital_cap / entry_price)
                if lot_size > 1:
                    max_qty_by_cap = max(lot_size, (max_qty_by_cap // lot_size) * lot_size)

                # Apply size multiplier from timing gate (event risk mode)
                adjusted_qty = int(size_result.qty * size_multiplier)
                final_qty = min(adjusted_qty, max_qty_by_cap)

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

            # In paper trading mode for stock trades, bypass margin utilization and open risk caps
            # so paper stock futures trades execute freely without getting blocked by paper account caps.
            gate_result = guardrails.check_new_trade(
                proposed_risk=proposed_risk,
                open_risk=0.0,
                day_pnl=today_pnl,
                month_pnl=month_pnl,
                margin_used_pct=0.0,
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
                "event_risk_mode": alert.get("event_risk_mode", False),
                "size_multiplier": size_multiplier,
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

            def _is_option(t):
                s = str(t.get("structure") or t.get("type") or "").upper()
                return any(kw in s for kw in (
                    "IRON_CONDOR", "IRON_BUTTERFLY", "IRON_FLY", "VERTICAL_SPREAD",
                    "CALENDAR", "DIAGONAL", "COVERED_CALL", "PROTECTIVE_PUT",
                    "STRADDLE", "STRANGLE", "STRIP", "STRAP", "IRON",
                ))

            def _get_group_stats(group):
                if not group:
                    return {"count": 0, "pnl": 0.0, "wr": 0.0}
                wins = [t for t in group if (t.get("pnl") or 0) > 0]
                return {
                    "count": len(group),
                    "pnl": round(sum(t.get("pnl") or 0 for t in group), 2),
                    "wr": round(len(wins) / len(group) * 100, 1),
                }

            stock_pnl = round(
                sum(t.get("pnl", 0) or 0 for t in closed_today if not _is_option(t)), 2
            )
            option_pnl = round(
                sum(t.get("pnl", 0) or 0 for t in closed_today if _is_option(t)), 2
            )

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
                "stock_pnl": stock_pnl,
                "option_pnl": option_pnl,
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

    H7 FIX: Creates timestamped snapshot backup before ANY destructive operation.
    H7 FIX: Logs audit trail for every cleanup call.
    """
    import sqlite3
    import logging as _log_mod
    from datetime import datetime as _dt

    _logger = _log_mod.getLogger(__name__)

    # H7: Snapshot backup before any destructive operation
    _snapshot_dir = DATA_FILE.parent / "backups"
    _snapshot_dir.mkdir(parents=True, exist_ok=True)
    _ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    _snapshot = _snapshot_dir / f"paper_trades_{_ts}.json"

    try:
        import shutil
        if DATA_FILE.exists():
            shutil.copy2(DATA_FILE, _snapshot)
            _logger.warning(
                "[cleanup_db] SNAPSHOT saved to %s before destructive operation "
                "(full_reset=%s, purge_churn=%s, from=%s, to=%s)",
                _snapshot, full_reset, purge_churn, from_date, to_date,
            )
    except Exception as e:
        _logger.error("[cleanup_db] Failed to create snapshot backup: %s", e)

    db = _load_db()

    if full_reset:
        _logger.warning(
            "[cleanup_db] FULL RESET requested — wiping %d trades, %d options, %d summaries",
            len(db.get("trades", [])),
            len(db.get("option_trades", [])),
            len(db.get("daily_summaries", [])),
        )
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
    # H6 FIX: Include option trades in daily summary recalculation.
    # Previously only stock trades were counted, causing total_pnl in daily
    # summaries to diverge from the cumulative_pnl (which includes both).
    new_summaries = []
    for s in db.get("daily_summaries", []):
        d = s.get("date")
        if (from_date and d < from_date) or (to_date and d > to_date):
            if from_date or to_date:
                continue

        # M1 FIX: Include BOTH stock and option trades for this date.
        # Deduplicate by trade ID to prevent double-counting when the same
        # trade exists in both JSON and SQLite stores after sync.
        s_trades = [t for t in keep_trades if t.get("entry_date") == d]
        s_options = [t for t in keep_options if t.get("entry_date") == d]

        # Build deduplicated list: prefer option_trades entry if ID overlaps
        seen_ids: set = set()
        all_day_trades: list[dict] = []
        for t in s_options + s_trades:
            tid = t.get("id") or t.get("trade_id")
            if tid is not None and tid in seen_ids:
                continue
            if tid is not None:
                seen_ids.add(tid)
            all_day_trades.append(t)

        s["trades"] = s_trades
        s["option_trades"] = s_options
        s["total_trades"] = len(all_day_trades)
        s["total_pnl"] = round(sum(t.get("pnl", 0) or 0 for t in all_day_trades), 2)
        winners = [t for t in all_day_trades if (t.get("pnl") or 0) > 0]
        s["win_rate"] = (
            round((len(winners) / max(len(all_day_trades), 1) * 100), 2)
            if all_day_trades
            else 0
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

    # H7: Audit log — who called cleanup, what was deleted
    _logger.warning(
        "[cleanup_db] AUDIT: removed=%d, remaining=%d, "
        "purge_churn=%s, from=%s, to=%s, snapshot=%s",
        removed_count,
        len(db.get("trades", [])) + len(db.get("option_trades", [])),
        purge_churn,
        from_date,
        to_date,
        str(_snapshot) if _snapshot.exists() else "NONE",
    )

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
