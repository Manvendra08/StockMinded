import math
import calendar
import sqlite3
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path
import os
import csv

from data.feed import option_chain

def _norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def _bs_delta(spot, strike, t, r, sigma, kind="CE"):
    if t <= 0 or sigma <= 0:
        if kind == "CE": return 1.0 if spot > strike else 0.0
        else: return -1.0 if spot < strike else 0.0
    
    d1 = (math.log(spot / strike) + (r + sigma**2 / 2.0) * t) / (sigma * math.sqrt(t))
    
    if kind == "CE":
        return _norm_cdf(d1)
    else:
        return _norm_cdf(d1) - 1.0

def _bs_price(spot, strike, t, r, sigma, kind="CE"):
    if t <= 0 or sigma <= 0:
        if kind == "CE": return max(0.0, spot - strike)
        else: return max(0.0, strike - spot)
        
    d1 = (math.log(spot / strike) + (r + sigma**2 / 2.0) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)
    
    if kind == "CE":
        return spot * _norm_cdf(d1) - strike * math.exp(-r * t) * _norm_cdf(d2)
    else:
        return strike * math.exp(-r * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)

def _is_holiday(dt_date):
    path = Path(__file__).parent.parent / "config" / "nse_holidays_2026.csv"
    if not path.exists(): return False
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("date") == dt_date.strftime("%Y-%m-%d"):
                return True
    return False

def _next_expiry(symbol="NIFTY", preference="weekly"):
    # Rough approximation for weekly (Thursday) vs monthly (Last Thursday)
    # Roll over on holidays
    today = date.today()
    
    if symbol == "BANKNIFTY":
        # Monthly last-Thursday only (post Jan-2025 rule change).
        # We find the last Thursday of the current month.
        cal = calendar.monthcalendar(today.year, today.month)
        last_week = cal[-1]
        if last_week[calendar.THURSDAY] != 0:
            exp_date = date(today.year, today.month, last_week[calendar.THURSDAY])
        else:
            exp_date = date(today.year, today.month, cal[-2][calendar.THURSDAY])
            
        if exp_date < today:
            # Move to next month
            next_month = today.month + 1 if today.month < 12 else 1
            next_year = today.year if today.month < 12 else today.year + 1
            cal = calendar.monthcalendar(next_year, next_month)
            last_week = cal[-1]
            if last_week[calendar.THURSDAY] != 0:
                exp_date = date(next_year, next_month, last_week[calendar.THURSDAY])
            else:
                exp_date = date(next_year, next_month, cal[-2][calendar.THURSDAY])
    else:
        # Weekly Thursday
        days_ahead = calendar.THURSDAY - today.weekday()
        if days_ahead < 0: # Target day already happened this week
            days_ahead += 7
        exp_date = today + timedelta(days=days_ahead)
        
    # Check for holidays and roll back
    while _is_holiday(exp_date):
        exp_date -= timedelta(days=1)
        
    return exp_date.strftime("%d-%b-%Y")

def atm_strike(spot, strikes):
    if not strikes: return None
    return min(strikes, key=lambda k: abs(k - spot))

def delta_strike(chain, target_delta, side="CE"):
    if chain.empty: return None
    delta_col = "ce_delta" if side == "CE" else "pe_delta"
    chain = chain.dropna(subset=[delta_col])
    if chain.empty: return None
    
    # Target delta should be positive if evaluating absolute magnitude
    target = abs(target_delta)
    # Find strike where absolute delta is closest to target
    closest_idx = chain[delta_col].apply(lambda x: abs(abs(x) - target)).idxmin()
    return chain.loc[closest_idx, "strike"]

def atm_iv(chain, spot):
    if chain.empty: return 0.0
    strikes = chain["strike"].tolist()
    atm = atm_strike(spot, strikes)
    if not atm: return 0.0
    row = chain[chain["strike"] == atm]
    if row.empty: return 0.0
    ce_iv = row.iloc[0].get("ce_iv", 0)
    pe_iv = row.iloc[0].get("pe_iv", 0)
    if ce_iv > 0 and pe_iv > 0:
        return (ce_iv + pe_iv) / 2.0
    return ce_iv if ce_iv > 0 else pe_iv

def iv_rank(symbol, current_iv, db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS iv_history (
                 symbol TEXT, date DATE, atm_iv REAL, PRIMARY KEY(symbol, date))''')
    
    today = date.today().isoformat()
    # Insert or replace today's IV
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
    if high == low: return 50.0
    
    return ((current_iv - low) / (high - low)) * 100.0

def chain_snapshot(symbol) -> pd.DataFrame:
    raw = option_chain(symbol)
    records = raw.get("records", {}).get("data", [])
    if not records:
        return pd.DataFrame()
        
    # Find closest expiry
    expiries = list(set(r.get("expiryDate") for r in records if "expiryDate" in r))
    if not expiries:
        return pd.DataFrame()
        
    # Use the nearest expiry
    def parse_exp(s):
        try: return datetime.strptime(s, "%d-%b-%Y")
        except: return datetime.max
    expiries.sort(key=parse_exp)
    closest_expiry = expiries[0]
    
    # We also need spot price to compute DTE properly
    underlying_value = raw.get("records", {}).get("underlyingValue", 0)
    
    rows = []
    tte_days = (parse_exp(closest_expiry).date() - date.today()).days
    t = max(tte_days, 0.5) / 365.0
    r = 0.065
    
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
            if ce_iv <= 0 and ce_ltp > 0:
                # very crude fallback
                ce_iv = 0.15 
            if pe_iv <= 0 and pe_ltp > 0:
                pe_iv = 0.15
                
            if ce_iv > 0:
                ce_delta = _bs_delta(underlying_value, strike, t, r, ce_iv, "CE")
            if pe_iv > 0:
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
