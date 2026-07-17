import calendar
import csv
import logging
import math
import os
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from data.feed import option_chain


def _norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _bs_delta(spot, strike, t, r, sigma, kind="CE"):
    if t <= 0 or sigma <= 0:
        if kind == "CE":
            return 1.0 if spot > strike else 0.0
        else:
            return -1.0 if spot < strike else 0.0
    d1 = (math.log(spot / strike) + (r + sigma**2 / 2.0) * t) / (sigma * math.sqrt(t))
    if kind == "CE":
        return _norm_cdf(d1)
    else:
        return _norm_cdf(d1) - 1.0


def _bs_price(spot, strike, t, r, sigma, kind="CE"):
    if t <= 0 or sigma <= 0:
        if kind == "CE":
            return max(0.0, spot - strike)
        else:
            return max(0.0, strike - spot)
    d1 = (math.log(spot / strike) + (r + sigma**2 / 2.0) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    if kind == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    else:
        return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


# ---------------------------------------------------------------------------
# H8 FIX: Cache holiday set at module level to avoid re-reading CSV on every call.
# The CSV is small (~20 rows) but _is_holiday() is called dozens of times per
# minute during market hours from chain_snapshot(), exit checks, etc.
# ---------------------------------------------------------------------------
_HOLIDAY_CACHE: set[str] | None = None
_HOLIDAY_CACHE_PATH: Path | None = None


def _load_holiday_set() -> set[str]:
    """Load NSE holiday dates from CSV into a cached set."""
    global _HOLIDAY_CACHE, _HOLIDAY_CACHE_PATH
    path = Path(__file__).parent.parent / "config" / "nse_holidays_2026.csv"
    if _HOLIDAY_CACHE is not None and _HOLIDAY_CACHE_PATH == path:
        return _HOLIDAY_CACHE
    holidays: set[str] = set()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    d = row.get("date", "").strip()
                    if d:
                        holidays.add(d)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Failed to load holiday CSV from %s: %s", path, exc
            )
    _HOLIDAY_CACHE = holidays
    _HOLIDAY_CACHE_PATH = path
    return holidays


def _is_holiday(dt_date: date) -> bool:
    """Check if a date is an NSE holiday. Uses module-level cache (H8 fix)."""
    holidays = _load_holiday_set()
    return dt_date.strftime("%Y-%m-%d") in holidays


def _next_expiry(symbol: str = "NIFTY", preference: str = "weekly") -> str:
    """Return the nearest (current or next) expiry date string for a symbol.

    H1 FIX: Changed `<= 0` to `< 0` so that on expiry day itself
    (days_ahead == 0) we return TODAY's expiry, not next week's.
    chain_snapshot() has its own 0-DTE avoidance logic for signal generation,
    so including today here is safe and correct for exit-check contexts.
    
    H9 FIX: Post-3:30pm on expiry day, roll to next week's expiry
    since today's contract has expired.
    """
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    today = now_ist.date()
    current_time = now_ist.time()
    market_close = time(15, 30)  # 3:30 PM IST
    
    if preference == "weekly":
        if symbol == "BANKNIFTY":
            # NSE weekly BANKNIFTY options expire on Wednesdays
            days_ahead = calendar.WEDNESDAY - today.weekday()
            # H9 FIX: If today is expiry day AND after 3:30pm, roll to next week
            if days_ahead == 0 and current_time >= market_close:
                days_ahead = 7
            elif days_ahead < 0:  # H1 FIX: < 0 instead of <= 0
                days_ahead += 7
            exp_date = today + timedelta(days=days_ahead)
        elif symbol == "SENSEX":
            # SENSEX weekly options expire on Thursdays
            days_ahead = calendar.THURSDAY - today.weekday()
            if days_ahead == 0 and current_time >= market_close:
                days_ahead = 7
            elif days_ahead < 0:  # H1 FIX: < 0 instead of <= 0
                days_ahead += 7
            exp_date = today + timedelta(days=days_ahead)
        else:
            # NIFTY and other indices: NSE changed NIFTY weekly expiry from Thursday → Tuesday (effective Apr 2025)
            days_ahead = calendar.TUESDAY - today.weekday()
            if days_ahead == 0 and current_time >= market_close:
                days_ahead = 7
            elif days_ahead < 0:  # H1 FIX: < 0 instead of <= 0
                days_ahead += 7
            exp_date = today + timedelta(days=days_ahead)
    else:
        # Monthly last-Tuesday expiry logic for both NIFTY and BANKNIFTY
        cal = calendar.monthcalendar(today.year, today.month)
        last_week = cal[-1]
        if last_week[calendar.TUESDAY] != 0:
            exp_date = date(today.year, today.month, last_week[calendar.TUESDAY])
        else:
            exp_date = date(today.year, today.month, cal[-2][calendar.TUESDAY])
        if exp_date < today:
            next_month = today.month + 1 if today.month < 12 else 1
            next_year = today.year if today.month < 12 else today.year + 1
            cal = calendar.monthcalendar(next_year, next_month)
            last_week = cal[-1]
            if last_week[calendar.TUESDAY] != 0:
                exp_date = date(next_year, next_month, last_week[calendar.TUESDAY])
            else:
                exp_date = date(next_year, next_month, cal[-2][calendar.TUESDAY])
    while _is_holiday(exp_date) or exp_date.weekday() >= 5:
        exp_date -= timedelta(days=1)
    return exp_date.strftime("%d-%b-%Y")


def atm_strike(spot, strikes):
    if not strikes:
        return None
    return min(strikes, key=lambda k: abs(k - spot))


def delta_strike(chain, target_delta, side="CE"):
    if chain.empty:
        return None
    delta_col = "ce_delta" if side == "CE" else "pe_delta"
    chain = chain.dropna(subset=[delta_col])
    if chain.empty:
        return None
    target = abs(target_delta)
    closest_idx = chain[delta_col].apply(lambda x: abs(abs(x) - target)).idxmin()
    return chain.loc[closest_idx, "strike"]


def atm_iv(chain, spot):
    if chain.empty:
        return 0.0
    strikes = chain["strike"].tolist()
    atm = atm_strike(spot, strikes)
    if not atm:
        return 0.0
    row = chain[chain["strike"] == atm]
    if row.empty:
        return 0.0
    ce_iv = row.iloc[0].get("ce_iv", 0)
    pe_iv = row.iloc[0].get("pe_iv", 0)
    if ce_iv > 0 and pe_iv > 0:
        return (ce_iv + pe_iv) / 2.0
    return ce_iv if ce_iv > 0 else pe_iv


def iv_rank(symbol, current_iv, db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "PRAGMA journal_mode=WAL"
    )  # Issue #9: WAL avoids reader/writer contention
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS iv_history (
                 symbol TEXT, date DATE, atm_iv REAL, PRIMARY KEY(symbol, date))""")
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
    if current_iv > 0:
        c.execute(
            "INSERT OR REPLACE INTO iv_history VALUES (?, ?, ?)",
            (symbol, today, current_iv),
        )
        conn.commit()
    # Compute the rank against PRIOR history only (exclude today's freshly
    # inserted value) so the rank is a true historical percentile and doesn't
    # include itself or drift intraday as today's row is overwritten.
    c.execute(
        "SELECT atm_iv FROM iv_history WHERE symbol=? AND date < ? ORDER BY date DESC LIMIT 252",
        (symbol, today),
    )
    rows = c.fetchall()
    conn.close()
    ivs = [r[0] for r in rows if r[0] > 0]
    if len(ivs) < 60:
        return None
    low = min(ivs)
    high = max(ivs)
    if high == low:
        return 50.0
    return ((current_iv - low) / (high - low)) * 100.0


def chain_snapshot(symbol, target_expiries=None, target_strikes=None) -> pd.DataFrame:
    # Skip ATM filtering when specific target strikes are requested,
    # e.g. for exit checks on existing trades with known legs.
    skip_filter = target_strikes is not None
    raw = option_chain(symbol, _skip_atm_filter=skip_filter)
    records = raw.get("records", {}).get("data", [])
    underlying_value = (
        raw.get("records", {}).get("underlyingValue")
        or raw.get("underlying_price")
        or raw.get("filtered", {}).get("underlying_price")
        or 0.0
    )
    if not records:
        return pd.DataFrame()
    expiries = list(set(r.get("expiryDate") for r in records if "expiryDate" in r))
    if not expiries:
        return pd.DataFrame()

    def parse_exp(s):
        for fmt in ("%d-%b-%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except:
                logging.getLogger(__name__).debug(f"Failed to parse expiry string: {s}")
        return datetime.max

    expiries.sort(key=parse_exp)
    # Issue #4: on expiry day, avoid 0-DTE chain (gamma risk / illiquidity);
    # prefer next week's expiry so signals are stable throughout the session.
    today_local = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()

    def _is_today(s):
        try:
            return parse_exp(s).date() == today_local
        except Exception:
            return False

    non_zero_dte = [e for e in expiries if not _is_today(e)]
    closest_expiry = non_zero_dte[0] if non_zero_dte else expiries[0]

    # Filter target_expiries: only keep future expiries (or today's)
    today_local_date = today_local
    filtered_targets = []
    if target_expiries:
        for e in target_expiries:
            parsed = parse_exp(e)
            if parsed != datetime.max:
                days_stale = (today_local_date - parsed.date()).days
                # Keep only today or future expiries; skip clearly stale ones
                # Near-stale (<7d) allowed for grace on recent closed trades;
                # dates >365d in the past are definitely data corruption.
                if days_stale < -365 or days_stale >= 365:
                    logging.getLogger(__name__).warning(
                        "%s: dropping corrupt expiry %s (%d days stale)",
                        symbol,
                        e,
                        days_stale,
                    )
                    continue
                if parsed.date() >= today_local_date or days_stale < 7:
                    filtered_targets.append(e)
        if filtered_targets != target_expiries:
            removed = [e for e in target_expiries if e not in filtered_targets]
            if removed:
                logging.getLogger(__name__).warning(
                    "%s: filtered stale target_expiries: removed %s, kept %s",
                    symbol,
                    removed,
                    filtered_targets,
                )

    standardized_valid = set()
    if filtered_targets:
        found_any = False
        for e in filtered_targets:
            parsed = parse_exp(e)
            if parsed != datetime.max:
                std = parsed.strftime("%Y-%m-%d")
                if std in [
                    parse_exp(x).strftime("%Y-%m-%d")
                    for x in expiries
                    if parse_exp(x) != datetime.max
                ]:
                    standardized_valid.add(std)
                    found_any = True
        if not found_any:
            # Fallback: target expiries not in chain (likely stale); use closest valid expiry
            logging.getLogger(__name__).warning(
                "%s: target expiries %s not in chain; falling back to %s",
                symbol,
                filtered_targets,
                closest_expiry,
            )
            parsed_closest = parse_exp(closest_expiry)
            if parsed_closest != datetime.max:
                standardized_valid.add(parsed_closest.strftime("%Y-%m-%d"))
    else:
        parsed_closest = parse_exp(closest_expiry)
        if parsed_closest != datetime.max:
            standardized_valid.add(parsed_closest.strftime("%Y-%m-%d"))

    rows = []
    r = 0.065
    vix_annual = 0.15

    for rec in records:
        if rec.get('strikePrice') is None:
            continue
        # BUG-01 FIX: lastPrice is nested inside CE/PE dicts, not at top level.
        # Check that at least one side has a valid premium before processing.
        ce_lp = rec.get("CE", {}).get("lastPrice")
        pe_lp = rec.get("PE", {}).get("lastPrice")
        if (ce_lp is None or ce_lp <= 0) and (pe_lp is None or pe_lp <= 0):
            continue
        exp_date_str = rec.get("expiryDate")
        if not exp_date_str:
            continue
        parsed_rec = parse_exp(exp_date_str)
        if parsed_rec == datetime.max:
            continue

        rec_exp_std = parsed_rec.strftime("%Y-%m-%d")
        if rec_exp_std not in standardized_valid:
            continue

        strike = rec.get("strikePrice")
        if target_strikes is not None and strike not in target_strikes:
            continue

        tte_days = (parsed_rec.date() - today_local).days
        t = max(tte_days, 0.5) / 365.0

        ce = rec.get("CE", {})
        pe = rec.get("PE", {})
        ce_iv = ce.get("impliedVolatility") or 0.0
        pe_iv = pe.get("impliedVolatility") or 0.0
        if ce_iv > 1.0:
            ce_iv /= 100.0
        if pe_iv > 1.0:
            pe_iv /= 100.0
        ce_ltp = ce.get("lastPrice") or 0.0
        pe_ltp = pe.get("lastPrice") or 0.0
        ce_delta = 0.0
        pe_delta = 0.0
        ce_synthetic = False
        pe_synthetic = False
        if underlying_value > 0:
            if ce_iv <= 0:
                ce_iv = vix_annual
            if pe_iv <= 0:
                pe_iv = vix_annual

            if ce_ltp <= 0:
                ce_ltp = round(
                    _bs_price(underlying_value, strike, t, r, ce_iv, "CE"), 2
                )
                ce_synthetic = True
            if pe_ltp <= 0:
                pe_ltp = round(
                    _bs_price(underlying_value, strike, t, r, pe_iv, "PE"), 2
                )
                pe_synthetic = True

            ce_delta = _bs_delta(underlying_value, strike, t, r, ce_iv, "CE")
            pe_delta = _bs_delta(underlying_value, strike, t, r, pe_iv, "PE")
        rows.append(
            {
                "strike": strike,
                "expiry": rec_exp_std,
                "ce_oi": ce.get("openInterest", 0),
                "ce_vol": ce.get("totalTradedVolume", 0),
                "ce_iv": ce_iv,
                "ce_ltp": ce_ltp,
                "ce_delta": ce_delta,
                "ce_synthetic": ce_synthetic,
                "pe_oi": pe.get("openInterest", 0),
                "pe_vol": pe.get("totalTradedVolume", 0),
                "pe_iv": pe_iv,
                "pe_ltp": pe_ltp,
                "pe_delta": pe_delta,
                "pe_synthetic": pe_synthetic,
            }
        )
    return pd.DataFrame(rows)


# =============================================================================
# NIFTY OPTION SELLING HELPERS
# =============================================================================


def is_within_entry_window(
    cfg: dict | None = None,
    now: datetime | None = None,
    symbol: str = "NIFTY",
) -> tuple[bool, str]:
    """
    Check if current time is within valid option entry window.

    Args:
        cfg: Config dict (loaded if None)
        now: Current IST datetime (auto if None)
        symbol: "NIFTY" or "BANKNIFTY"

    Returns: (is_valid, reason)
    - Intraday mode: 09:45-14:30
    - Positional mode: entries only during allowed weekdays
    - Expiry day: no entries after 12:00 IST
    """
    from datetime import timedelta, timezone

    if cfg is None:
        from config.loader import load_config

        cfg = load_config()

    # Pick the right config section based on symbol
    cfg_key = "banknifty_options" if symbol == "BANKNIFTY" else "nifty_options"
    sym_cfg = cfg.get(cfg_key, {})
    if not sym_cfg.get("enabled", False):
        return False, f"{symbol} options disabled"

    mode = sym_cfg.get("mode", "positional")
    now = now or datetime.now(timezone(timedelta(hours=5, minutes=30)))

    if now.weekday() >= 5:
        return False, "Weekend - market closed"

    # Block entries on NSE holidays
    if _is_holiday(now.date()):
        return False, "Holiday - market closed"

    current_time = now.time()

    # Get entry window times
    entry_start_str = sym_cfg.get("intraday_entry_start", "09:45")
    entry_end_str = sym_cfg.get("intraday_entry_end", "14:30")

    # BUG-15 FIX: Wrap time parsing in try/except to handle invalid config formats
    # like "945" instead of "09:45". Fall back to sensible defaults on error.
    try:
        h1, m1 = map(int, entry_start_str.split(":"))
        entry_start = time(h1, m1)
    except (ValueError, TypeError):
        entry_start = time(9, 45)

    try:
        h2, m2 = map(int, entry_end_str.split(":"))
        entry_end = time(h2, m2)
    except (ValueError, TypeError):
        entry_end = time(14, 30)

    if not (entry_start <= current_time <= entry_end):
        return False, f"Outside entry window ({entry_start_str}-{entry_end_str})"

    # Positional mode: check allowed days from config
    if mode == "positional":
        allowed_days = sym_cfg.get("positional_entry_days", [0, 1, 2, 3, 4])
        if now.weekday() not in allowed_days:
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
            return (
                False,
                f"Positional {symbol}: entry not allowed today ({day_names[now.weekday()]})",
            )

    # Expiry day cut-off: no entries after 12:00 IST on expiry day
    if is_symbol_expiry_today(symbol):
        expiry_cutoff = time(12, 0)
        if current_time >= expiry_cutoff:
            return (
                False,
                f"{symbol} expiry today — no entries after 12:00 IST",
            )

    return True, f"Valid {symbol} entry window"


def is_expiry_day(expiry_str: str) -> bool:
    """Check if today is expiry day for given expiry string."""
    try:
        exp_date = datetime.strptime(expiry_str, "%d-%b-%Y").date()
        today_ist = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
        return exp_date == today_ist
    except:
        return False


def _expiry_date_for_symbol(symbol: str) -> str | None:
    """Return the nearest (current or next) expiry date for a symbol.

    For weekly symbols (NIFTY Tue, SENSEX Thu): finds the most recent
    occurrence of the target weekday (including today), then applies
    holiday rollback (previous trading day).

    For monthly symbols (BANKNIFTY): last Tuesday of the month with
    holiday rollback.

    Used by is_symbol_expiry_today() to determine if today IS the
    effective expiry day.
    """
    try:
        from datetime import date as dt_date

        today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
        cal = calendar

        if symbol == "BANKNIFTY":
            # Last Tuesday of the month
            month_cal = cal.monthcalendar(today.year, today.month)
            # Find the last Tuesday (weekday=1)
            last_tuesday = max(
                week[cal.TUESDAY] for week in month_cal if week[cal.TUESDAY] != 0
            )
            exp_date = dt_date(today.year, today.month, last_tuesday)
        else:
            # Weekly expiry: determine target weekday from symbol
            target_weekday = (
                calendar.THURSDAY if symbol == "SENSEX" else calendar.TUESDAY
            )
            # Days since the most recent target weekday (0 if today is expiry day)
            days_since = (today.weekday() - target_weekday) % 7
            exp_date = today - timedelta(days=days_since)

        # Holiday rollback: if expiry lands on a holiday/weekend, move to previous trading day
        while _is_holiday(exp_date) or exp_date.weekday() >= 5:
            exp_date -= timedelta(days=1)
            if exp_date.month != today.month and symbol == "BANKNIFTY":
                # Monthly expiry rolled into previous month — find next month's
                next_month = today.month + 1 if today.month < 12 else 1
                next_year = today.year if today.month < 12 else today.year + 1
                month_cal = cal.monthcalendar(next_year, next_month)
                last_tuesday = max(
                    week[cal.TUESDAY] for week in month_cal if week[cal.TUESDAY] != 0
                )
                exp_date = dt_date(next_year, next_month, last_tuesday)
                while _is_holiday(exp_date) or exp_date.weekday() >= 5:
                    exp_date -= timedelta(days=1)
                break

        return exp_date.strftime("%d-%b-%Y")
    except Exception:
        return None


def is_symbol_expiry_today(symbol: str) -> bool:
    """Check if today is the expiry day for the given symbol."""
    exp_str = _expiry_date_for_symbol(symbol)
    if exp_str is None:
        return False
    return is_expiry_day(exp_str)


def is_within_exit_window(
    cfg: dict = None,
    now: datetime = None,
    mode: str = "positional",
    symbol: str = "NIFTY",
) -> Tuple[bool, str]:
    """
    Check if trade should be exited now based on mode and rules.

    M7 FIX: Added symbol parameter to use the correct config section.
    Previously always used nifty_options config even for BANKNIFTY trades,
    causing BANKNIFTY exits to be governed by NIFTY's timing rules.

    Intraday: exit by configured time (default 15:15)
    Positional: exit by expiry-day configured time ONLY; normal days remain in trade
    """
    from datetime import timedelta, timezone

    if cfg is None:
        from config.loader import load_config

        cfg = load_config()

    # M7 FIX: Pick the right config section based on symbol
    cfg_key = "banknifty_options" if symbol == "BANKNIFTY" else "nifty_options"
    sym_cfg = cfg.get(cfg_key, {})

    now = now or datetime.now(timezone(timedelta(hours=5, minutes=30)))

    if now.weekday() >= 5:
        return False, "Weekend"

    # Block forced exits on NSE holidays (no market activity)
    if _is_holiday(now.date()):
        return False, "Holiday - market closed"

    current_time = now.time()

    if mode == "intraday":
        exit_str = sym_cfg.get("intraday_exit_by", "15:15")
        h, m = map(int, exit_str.split(":"))
        exit_time = time(h, m)
        if current_time >= exit_time:
            return True, f"Intraday exit by {exit_str}"
        return False, "Not yet exit time"

    else:  # positional
        exit_str = sym_cfg.get("positional_exit_expiry_cutoff", "15:15")
        h, m = map(int, exit_str.split(":"))
        exit_time = time(h, m)
        # Only force-exit on EXPIRY DAY; on normal days stay in the trade
        # M7 FIX: Use symbol-specific expiry check
        on_expiry_day = is_symbol_expiry_today(symbol)
        if on_expiry_day and current_time >= exit_time:
            return True, f"Expiry day exit by {exit_str}"
        return False, "Not expiry cutoff"


def calc_structure_max_loss(
    structure_type: str,
    net_credit: float,
    wing_width: float,
    lot_size: int = 1,
    lots: int = 1,
    **kwargs,
) -> float:
    """
    Calculate max loss for an option structure.

    Iron Condor / Credit spread: max_loss = (wing_width * lot_size * lots) - net_credit
    Credit Spread: max_loss = (spread_width * lot_size * lots) - net_credit
    Debit structures (Long Straddle/Strangle/Spread): max_loss = debit paid = abs(net_credit)
    naked_short kwargs: underlying_spot, naked_loss_pct (default 0.20), naked_loss_cap (default 250_000)
    """
    # DEBIT FIX: For debit spreads (net_credit <= 0), max loss is the debit paid,
    # not the wing width. Wing width applies only to credit spreads.
    # This handles LONG_STRADDLE, LONG_STRANGLE, and other debit structures.
    # NOTE: net_credit is already the TOTAL premium (premium × lots × lot_size)
    # from all callers, so we must NOT multiply by lot_size × lots again.
    if net_credit <= 0:
        return max(0, abs(net_credit))

    if structure_type == "iron_condor":
        return max(0, (wing_width * lot_size * lots) - net_credit)
    elif structure_type in ("bull_put_spread", "bear_call_spread"):
        return max(0, (wing_width * lot_size * lots) - net_credit)
    elif structure_type == "credit_spread":
        return max(0, (wing_width * lot_size * lots) - net_credit)
    elif structure_type in ("long_straddle", "long_strangle", "debit_spread"):
        # Debit structure: max loss is the debit paid (already handled above
        # by the net_credit <= 0 check, but included for explicit clarity)
        return max(0, abs(net_credit))
    elif structure_type == "naked_short":
        # C3 FIX: Naked short has theoretically unlimited risk.  The previous
        # formula (spot * 0.20) underestimated max loss by 2-3× for gap-risk
        # scenarios.  We now:
        #   1. Use 50% of spot as the base estimate (covers ~3σ gap moves).
        #   2. Enforce a configurable FLOOR (naked_loss_cap) so sizing never
        #      underestimates risk, even for low-priced underlyings.
        #   3. Return MAX(base_estimate, floor) to be conservative.
        spot = kwargs.get("underlying_spot", 0.0)
        pct = kwargs.get("naked_loss_pct", 0.50)  # C3 FIX: 20% → 50%
        floor_cap = kwargs.get("naked_loss_cap", 250_000.0)
        if spot > 0:
            base_estimate = round(spot * pct * lot_size * lots, 2)
        else:
            base_estimate = 0.0
        floor_total = floor_cap * lots
        return max(base_estimate, floor_total)

    return wing_width * lot_size * lots


def calc_exit_levels(
    entry_net_credit: float, max_loss: float, cfg: dict = None
) -> Dict[str, float]:
    """
    Calculate profit target and stop loss levels based on entry credit.

    Profit target: 50% of max credit (configurable)
    Stop loss: 1.25x credit received (configurable)
    """
    if cfg is None:
        from config.loader import load_config

        cfg = load_config()

    nifty_cfg = cfg.get("nifty_options", {})
    profit_pct = nifty_cfg.get("profit_take_pct", 0.50)
    sl_mult = nifty_cfg.get("stop_loss_mult", 1.25)

    profit_target = entry_net_credit * profit_pct
    stop_loss = entry_net_credit * sl_mult

    return {
        "entry_net_credit": entry_net_credit,
        "max_loss": max_loss,
        "profit_target": profit_target,
        "stop_loss_level": stop_loss,
        "profit_pct": profit_pct,
        "sl_mult": sl_mult,
    }


def check_naked_legs(legs: list, allow_naked: bool = False) -> Tuple[bool, str]:
    """
    Verify that no leg is a naked short option, unless allow_naked is True.
    All short positions must have corresponding protective legs in defined-risk mode.

    M6 FIX: Now also validates strike ordering — protective legs must be on the
    correct side of the short leg to actually provide protection:
      - Bear Call Spread: long CE strike must be ABOVE short CE strike
      - Bull Put Spread: long PE strike must be BELOW short PE strike
    A protective leg at the wrong strike creates a debit spread, not a credit spread,
    and does NOT cap the risk.
    """
    if not legs:
        return True, "No legs"

    if allow_naked:
        return True, "Naked allowed for this strategy"

    def _get(leg, key):
        if isinstance(leg, dict):
            return leg.get(key)
        return getattr(leg, key, None)

    # Collect actual leg objects (not just counts) for strike validation
    short_call_legs = [l for l in legs if _get(l, "side") == "SELL" and _get(l, "type") == "CE"]
    long_call_legs = [l for l in legs if _get(l, "side") == "BUY" and _get(l, "type") == "CE"]
    short_put_legs = [l for l in legs if _get(l, "side") == "SELL" and _get(l, "type") == "PE"]
    long_put_legs = [l for l in legs if _get(l, "side") == "BUY" and _get(l, "type") == "PE"]

    short_calls = len(short_call_legs)
    long_calls = len(long_call_legs)
    short_puts = len(short_put_legs)
    long_puts = len(long_put_legs)

    # Naked if short without protection
    if short_calls > 0 and long_calls == 0:
        return False, "Naked short call detected"
    if short_puts > 0 and long_puts == 0:
        return False, "Naked short put detected"

    # For Iron Condor / Credit Spreads: need exactly 1 short + 1 long on each side
    if short_calls > 0 and long_calls > 0:
        if short_calls != long_calls:
            return False, "Unbalanced call legs"
        # M6 FIX: Validate strike ordering for call spreads
        # Bear Call Spread: short CE at lower strike, long CE at higher strike
        # The long CE caps the upside risk. If long CE < short CE, it's NOT protective.
        for sc in short_call_legs:
            sc_strike = _get(sc, "strike")
            if sc_strike is None:
                continue
            # Find a matching protective long call with strike > short strike
            protective_found = False
            for lc in long_call_legs:
                lc_strike = _get(lc, "strike")
                if lc_strike is not None and lc_strike > sc_strike:
                    protective_found = True
                    break
            if not protective_found:
                return False, (
                    f"Call spread strike ordering invalid: short CE at {sc_strike} "
                    f"has no protective long CE above it"
                )

    if short_puts > 0 and long_puts > 0:
        if short_puts != long_puts:
            return False, "Unbalanced put legs"
        # M6 FIX: Validate strike ordering for put spreads
        # Bull Put Spread: short PE at higher strike, long PE at lower strike
        # The long PE caps the downside risk. If long PE > short PE, it's NOT protective.
        for sp in short_put_legs:
            sp_strike = _get(sp, "strike")
            if sp_strike is None:
                continue
            # Find a matching protective long put with strike < short strike
            protective_found = False
            for lp in long_put_legs:
                lp_strike = _get(lp, "strike")
                if lp_strike is not None and lp_strike < sp_strike:
                    protective_found = True
                    break
            if not protective_found:
                return False, (
                    f"Put spread strike ordering invalid: short PE at {sp_strike} "
                    f"has no protective long PE below it"
                )

    return True, "No naked legs"


def calc_pnl_from_legs(
    legs: list, entry_prices: dict, current_prices: dict, lot_size: int = 1
) -> Dict:
    """
    Calculate P&L from option structure legs.

    Args:
        legs: list of leg dicts with keys: side, type, strike, expiry, qty
        entry_prices: {leg_key: entry_premium}
        current_prices: {leg_key: current_premium}
        lot_size: contract multiplier

    Returns: dict with pnl breakdown
    """
    total_pnl = 0.0
    leg_results = []

    for leg in legs:
        leg_key = (leg.get("strike"), leg.get("expiry"), leg.get("type"))
        entry_px = entry_prices.get(leg_key, 0)
        curr_px = current_prices.get(leg_key, 0)
        qty = leg.get("qty", lot_size)

        if leg.get("side") == "SELL":
            # Short: profit when premium decreases
            leg_pnl = (entry_px - curr_px) * qty
        else:
            # Long: profit when premium increases
            leg_pnl = (curr_px - entry_px) * qty

        total_pnl += leg_pnl
        leg_results.append(
            {"leg": leg, "entry_px": entry_px, "current_px": curr_px, "pnl": leg_pnl}
        )

    return {"total_pnl": round(total_pnl, 2), "leg_results": leg_results}


def check_vix_spike_exit(
    vix_current: float, vix_entry: float, threshold_pct: float = 10.0
) -> Tuple[bool, str]:
    """
    Check if VIX has spiked enough to trigger exit.

    Returns: (should_exit, reason)
    """
    if vix_entry <= 0:
        return False, ""

    change_pct = ((vix_current - vix_entry) / vix_entry) * 100

    if change_pct > threshold_pct:
        return True, f"VIX spiked {change_pct:.1f}% > {threshold_pct}% threshold"

    return False, ""


def net_position_delta(legs: list, chain: pd.DataFrame) -> Optional[float]:
    """Compute net delta across all legs of an option trade using live chain data.

    Args:
        legs: list of leg dicts with keys: side, type, strike, expiry, qty
        chain: DataFrame from chain_snapshot() with ce_delta, pe_delta columns

    Returns: net delta float (per-share normalized) or None if chain data insufficient
    """
    if chain is None or chain.empty or not legs:
        return None

    min_qty = min(leg.get("qty", 1) for leg in legs) if legs else 1
    if min_qty <= 0:
        min_qty = 1

    net_delta = 0.0
    matched = 0
    for leg in legs:
        strike = leg.get("strike")
        opt_type = leg.get("type", "CE")
        side = leg.get("side", "BUY")

        row = chain[chain["strike"] == strike]
        if row.empty:
            continue

        delta_col = "ce_delta" if opt_type == "CE" else "pe_delta"
        leg_delta = float(row.iloc[0].get(delta_col, 0.0))

        qty = leg.get("qty", 1)
        sign = 1 if side == "BUY" else -1
        net_delta += sign * leg_delta * qty
        matched += 1

    return round(net_delta / min_qty, 4) if matched > 0 else None
