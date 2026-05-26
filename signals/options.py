import math
import calendar
import sqlite3
import pandas as pd
from datetime import datetime, date, timedelta, time
from pathlib import Path
import os
import csv
from typing import Optional, Tuple, List, Dict

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


def _is_holiday(dt_date):
    path = Path(__file__).parent.parent / "config" / "nse_holidays_2026.csv"
    if not path.exists():
        return False
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("date") == dt_date.strftime("%Y-%m-%d"):
                return True
    return False


def _next_expiry(symbol="NIFTY", preference="weekly"):
    today = date.today()
    if symbol == "BANKNIFTY":
        cal = calendar.monthcalendar(today.year, today.month)
        last_week = cal[-1]
        if last_week[calendar.THURSDAY] != 0:
            exp_date = date(today.year, today.month, last_week[calendar.THURSDAY])
        else:
            exp_date = date(today.year, today.month, cal[-2][calendar.THURSDAY])
        if exp_date < today:
            next_month = today.month + 1 if today.month < 12 else 1
            next_year = today.year if today.month < 12 else today.year + 1
            cal = calendar.monthcalendar(next_year, next_month)
            last_week = cal[-1]
            if last_week[calendar.THURSDAY] != 0:
                exp_date = date(next_year, next_month, last_week[calendar.THURSDAY])
            else:
                exp_date = date(next_year, next_month, cal[-2][calendar.THURSDAY])
    else:
        # NSE changed NIFTY weekly expiry from Thursday → Monday (effective Oct 2024)
        days_ahead = calendar.MONDAY - today.weekday()
        if days_ahead <= 0:           # already Monday or past it this week
            days_ahead += 7
        exp_date = today + timedelta(days=days_ahead)
    while _is_holiday(exp_date):
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
    conn.execute("PRAGMA journal_mode=WAL")  # Issue #9: WAL avoids reader/writer contention
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS iv_history (
                 symbol TEXT, date DATE, atm_iv REAL, PRIMARY KEY(symbol, date))''')
    today = date.today().isoformat()
    if current_iv > 0:
        c.execute("INSERT OR REPLACE INTO iv_history VALUES (?, ?, ?)", (symbol, today, current_iv))
        conn.commit()
    c.execute("SELECT atm_iv FROM iv_history WHERE symbol=? ORDER BY date DESC LIMIT 252", (symbol,))
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


def chain_snapshot(symbol) -> pd.DataFrame:
    raw = option_chain(symbol)
    records = raw.get("records", {}).get("data", [])
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
                pass
        return datetime.max
    expiries.sort(key=parse_exp)
    # Issue #4: on expiry day, avoid 0-DTE chain (gamma risk / illiquidity);
    # prefer next week's expiry so signals are stable throughout the session.
    today_local = date.today()
    def _is_today(s):
        try:
            return parse_exp(s).date() == today_local
        except Exception:
            return False
    non_zero_dte = [e for e in expiries if not _is_today(e)]
    closest_expiry = non_zero_dte[0] if non_zero_dte else expiries[0]
    underlying_value = raw.get("records", {}).get("underlyingValue", 0)
    rows = []
    tte_days = (parse_exp(closest_expiry).date() - date.today()).days
    t = max(tte_days, 0.5) / 365.0
    r = 0.065
    vix_annual = 0.15

    for rec in records:
        if rec.get("expiryDate") != closest_expiry:
            continue
        strike = rec.get("strikePrice")
        ce = rec.get("CE", {})
        pe = rec.get("PE", {})
        ce_iv = ce.get("impliedVolatility", 0) / 100.0
        pe_iv = pe.get("impliedVolatility", 0) / 100.0
        ce_ltp = ce.get("lastPrice", 0)
        pe_ltp = pe.get("lastPrice", 0)
        ce_delta = 0.0
        pe_delta = 0.0
        if underlying_value > 0:
            if ce_iv <= 0:
                ce_iv = vix_annual
            if pe_iv <= 0:
                pe_iv = vix_annual
            
            if ce_ltp <= 0:
                ce_ltp = round(_bs_price(underlying_value, strike, t, r, ce_iv, "CE"), 2)
            if pe_ltp <= 0:
                pe_ltp = round(_bs_price(underlying_value, strike, t, r, pe_iv, "PE"), 2)

            ce_delta = _bs_delta(underlying_value, strike, t, r, ce_iv, "CE")
            pe_delta = _bs_delta(underlying_value, strike, t, r, pe_iv, "PE")
        rows.append({
            "strike": strike,
            "expiry": closest_expiry,
            "ce_oi": ce.get("openInterest", 0),
            "ce_vol": ce.get("totalTradedVolume", 0),
            "ce_iv": ce_iv,
            "ce_ltp": ce_ltp,
            "ce_delta": ce_delta,
            "pe_oi": pe.get("openInterest", 0),
            "pe_vol": pe.get("totalTradedVolume", 0),
            "pe_iv": pe_iv,
            "pe_ltp": pe_ltp,
            "pe_delta": pe_delta,
        })
    return pd.DataFrame(rows)


# =============================================================================
# NIFTY OPTION SELLING HELPERS
# =============================================================================

def is_within_entry_window(cfg: dict = None, now: datetime = None) -> Tuple[bool, str]:
    """
    Check if current time is within valid NIFTY option entry window.
    
    Returns: (is_valid, reason)
    - Intraday mode: 09:45-14:30
    - Positional mode: entries only during first 3 days of contract (Wed-Fri for Tuesday expiry)
    """
    from datetime import timezone, timedelta
    if cfg is None:
        from config.loader import load_config
        cfg = load_config()
    
    nifty_cfg = cfg.get("nifty_options", {})
    if not nifty_cfg.get("enabled", False):
        return False, "NIFTY options disabled"
    
    mode = nifty_cfg.get("mode", "positional")
    now = now or datetime.now(timezone(timedelta(hours=5, minutes=30)))
    
    if now.weekday() >= 5:
        return False, "Weekend - market closed"
    
    current_time = now.time()
    
    # Get entry window times
    entry_start_str = nifty_cfg.get("intraday_entry_start", "09:45")
    entry_end_str = nifty_cfg.get("intraday_entry_end", "14:30")
    
    h1, m1 = map(int, entry_start_str.split(":"))
    h2, m2 = map(int, entry_end_str.split(":"))
    entry_start = time(h1, m1)
    entry_end = time(h2, m2)
    
    if not (entry_start <= current_time <= entry_end):
        return False, f"Outside entry window ({entry_start_str}-{entry_end_str})"
    
    # Positional mode: check allowed days from config (all weekdays enabled for paper trading)
    if mode == "positional":
        allowed_days = nifty_cfg.get("positional_entry_days", [0, 1, 2, 3, 4])
        if now.weekday() not in allowed_days:
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
            return False, f"Positional mode: entry not allowed today ({day_names[now.weekday()]})"
    
    return True, "Valid entry window"


def is_expiry_day(expiry_str: str) -> bool:
    """Check if today is expiry day for given expiry string."""
    try:
        exp_date = datetime.strptime(expiry_str, "%d-%b-%Y").date()
        return exp_date == date.today()
    except:
        return False


def is_within_exit_window(cfg: dict = None, now: datetime = None, mode: str = "positional") -> Tuple[bool, str]:
    """
    Check if trade should be exited now based on mode and rules.
    
    Intraday: exit by 15:15
    Positional: exit by expiry-day 15:15 ONLY; normal days remain in trade
    """
    from datetime import timezone, timedelta
    if cfg is None:
        from config.loader import load_config
        cfg = load_config()
    
    nifty_cfg = cfg.get("nifty_options", {})
    now = now or datetime.now(timezone(timedelta(hours=5, minutes=30)))
    
    if now.weekday() >= 5:
        return False, "Weekend"
    
    current_time = now.time()
    
    if mode == "intraday":
        exit_str = nifty_cfg.get("intraday_exit_by", "15:15")
        h, m = map(int, exit_str.split(":"))
        exit_time = time(h, m)
        if current_time >= exit_time:
            return True, f"Intraday exit by {exit_str}"
        return False, "Not yet exit time"
    
    else:  # positional
        exit_str = nifty_cfg.get("positional_exit_expiry_cutoff", "15:15")
        h, m = map(int, exit_str.split(":"))
        exit_time = time(h, m)
        # Only force-exit on EXPIRY DAY; on normal days stay in the trade
        expiry_str = nifty_cfg.get("current_expiry", "")
        on_expiry_day = is_expiry_day(expiry_str) if expiry_str else False
        if on_expiry_day and current_time >= exit_time:
            return True, f"Expiry day exit by {exit_str}"
        return False, "Not expiry cutoff"


def calc_structure_max_loss(structure_type: str, net_credit: float, wing_width: float,
                            lot_size: int = 1, **kwargs) -> float:
    """
    Calculate max loss for an option structure.
    
    Iron Condor / Credit spread: max_loss = (wing_width * lot_size) - net_credit
    Credit Spread: max_loss = (spread_width * lot_size) - net_credit
    naked_short kwargs: underlying_spot, naked_loss_pct (default 0.20), naked_loss_cap (default 250_000)
    """
    if net_credit <= 0:
        return max(0, wing_width * lot_size)

    if structure_type == "iron_condor":
        return max(0, (wing_width * lot_size) - net_credit)
    elif structure_type in ("bull_put_spread", "bear_call_spread"):
        return max(0, (wing_width * lot_size) - net_credit)
    elif structure_type == "credit_spread":
        return max(0, (wing_width * lot_size) - net_credit)
    elif structure_type == "naked_short":
        # Issue #5: derive max-loss proxy dynamically from a configurable % of underlying.
        # Caller should pass underlying_spot via kwargs; fallback keeps behaviour safe.
        # Default: 20 % of spot × lot_size  (e.g. 24500 × 0.20 × 75 ≈ ₹3.68 L/lot)
        spot = kwargs.get("underlying_spot", 0.0)
        pct  = kwargs.get("naked_loss_pct",  0.20)
        if spot > 0:
            return round(spot * pct * lot_size, 2)
        # Fallback: use configured absolute cap or ₹250k
        cap = kwargs.get("naked_loss_cap", 250_000.0)
        return cap * lot_size
    
    return wing_width * lot_size


def calc_exit_levels(entry_net_credit: float, max_loss: float, cfg: dict = None) -> Dict[str, float]:
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
        "sl_mult": sl_mult
    }


def check_naked_legs(legs: list, allow_naked: bool = False) -> Tuple[bool, str]:
    """
    Verify that no leg is a naked short option, unless allow_naked is True.
    All short positions must have corresponding protective legs in defined-risk mode.
    """
    if not legs:
        return True, "No legs"
    
    if allow_naked:
        return True, "Naked allowed for this strategy"

    def _get(leg, key):
        if isinstance(leg, dict):
            return leg.get(key)
        return getattr(leg, key, None)

    # Count short and long positions by type
    short_calls = sum(1 for l in legs if _get(l, "side") == "SELL" and _get(l, "type") == "CE")
    long_calls = sum(1 for l in legs if _get(l, "side") == "BUY" and _get(l, "type") == "CE")
    short_puts = sum(1 for l in legs if _get(l, "side") == "SELL" and _get(l, "type") == "PE")
    long_puts = sum(1 for l in legs if _get(l, "side") == "BUY" and _get(l, "type") == "PE")
    
    # Naked if short without protection
    if short_calls > 0 and long_calls == 0:
        return False, "Naked short call detected"
    if short_puts > 0 and long_puts == 0:
        return False, "Naked short put detected"
    
    # For Iron Condor: need exactly 1 short + 1 long on each side
    if short_calls > 0 and long_calls > 0:
        if short_calls != long_calls:
            return False, "Unbalanced call legs"
    if short_puts > 0 and long_puts > 0:
        if short_puts != long_puts:
            return False, "Unbalanced put legs"
    
    return True, "No naked legs"


def calc_pnl_from_legs(legs: list, entry_prices: dict, current_prices: dict, 
                       lot_size: int = 1) -> Dict:
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
        leg_results.append({
            "leg": leg,
            "entry_px": entry_px,
            "current_px": curr_px,
            "pnl": leg_pnl
        })
    
    return {
        "total_pnl": round(total_pnl, 2),
        "leg_results": leg_results
    }


def check_vix_spike_exit(vix_current: float, vix_entry: float, threshold_pct: float = 10.0) -> Tuple[bool, str]:
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
    
    return round(net_delta, 4) if matched > 0 else None
