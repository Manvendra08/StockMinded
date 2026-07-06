# Options Strategy Engine

> NIFTY and BANKNIFTY options selling system with defined-risk structures.

---

## 1. Overview

The options engine selects and executes option-selling strategies based on market regime, flow bias, and volatility conditions. It supports both **defined-risk** (Iron Condor, spreads) and **naked** (aggressive) structures.

### 1.1 Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                  option_strategy.py                          │
│  pick_nifty_structure() · pick_banknifty_structure()         │
│  resolve_nifty_structure() · resolve_banknifty_structure()   │
├─────────────────────────────────────────────────────────────┤
│                      options.py                              │
│  chain_snapshot() · iv_rank() · _bs_price() · _bs_delta()   │
│  is_within_entry_window() · is_within_exit_window()         │
│  calc_structure_max_loss() · calc_exit_levels()              │
│  check_naked_legs() · check_vix_spike_exit()                │
│  net_position_delta() · calc_pnl_from_legs()                │
├─────────────────────────────────────────────────────────────┤
│                  paper_trader.py                             │
│  enter_nifty_option_structure() · check_option_exits()       │
│  _enter_option_structure() (shared NIFTY/BANKNIFTY)          │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. Strategy Selection

### 2.1 Decision Matrix

| Regime | Bias | Strategy | Risk Profile |
|--------|------|----------|--------------|
| **TREND_UP** | LONG | Bull Put Spread | Defined risk, bullish |
| **TREND_DOWN** | SHORT | Bear Call Spread | Defined risk, bearish |
| **RANGE_LOW_VOL** | NEUTRAL | Iron Condor (standard wing) | Defined risk, neutral |
| **RANGE_HIGH_VOL** | NEUTRAL | Iron Condor Wide (1.5× wing) | Defined risk, neutral |
| **VOL_CONTRACTION** | NEUTRAL | Iron Condor | Defined risk, neutral |
| **VOL_EXPANSION** | ANY | **BLOCKED** | Too risky for selling |
| Any | LONG (naked allowed) | Naked Put Sell | Undefined risk, bullish |
| Any | SHORT (naked allowed) | Naked Call Sell | Undefined risk, bearish |

### 2.2 Selection Logic

```python
def _pick_strategy(data, regime, bias, vix, vix_change_pct, pcr, cfg, symbol):
    sym_cfg = cfg[f"{symbol.lower()}_options"]
    
    if not sym_cfg.get("enabled"):
        return None
    
    # Vol expansion filter
    avoid_vol_exp = sym_cfg.get("avoid_vol_expansion", True)
    vol_exp_thresh = sym_cfg.get("vol_expansion_threshold", 5.0)
    vol_expansion_blocked = avoid_vol_exp and vix_change_pct > vol_exp_thresh
    
    # Hard block: VOL_EXPANSION regime
    if regime == "VOL_EXPANSION":
        return NiftyOptionSetup(suitable=False, skip_reason="VOL_EXPANSION")
    
    # Range regimes with expanding VIX → block
    if vol_expansion_blocked and regime in ("RANGE_LOW_VOL", "RANGE_HIGH_VOL"):
        return NiftyOptionSetup(suitable=False, skip_reason=f"VIX expanding")
    
    # Naked sell (from verdict NAKED_OPTION_SELL action)
    if symbol == "NIFTY" and verdict.nifty.action == "NAKED_OPTION_SELL":
        return _build_setup("NAKED_SELL", ...)
    
    # Range regimes → Iron Condor
    if regime in ("RANGE_LOW_VOL", "VOL_CONTRACTION"):
        return _build_setup("IRON_CONDOR", ...)
    elif regime == "RANGE_HIGH_VOL" and not vol_expansion_blocked:
        return _build_setup("IRON_CONDOR_WIDE", ...)
    
    # Trend regimes → directional spreads
    elif regime == "TREND_UP" and bias in ("LONG", "BULL_FLOW"):
        if vol_expansion_blocked:
            return NiftyOptionSetup(suitable=False, skip_reason="Bull Put blocked")
        return _build_setup("BULL_PUT_SPREAD", ...)
    elif regime == "TREND_DOWN" and bias in ("SHORT", "BEAR_FLOW"):
        if vol_expansion_blocked:
            return NiftyOptionSetup(suitable=False, skip_reason="Bear Call blocked")
        return _build_setup("BEAR_CALL_SPREAD", ...)
```

---

## 3. Structure Resolution

### 3.1 Strike Selection

```python
def _resolve_structure(setup, chain, spot, lot_size, strike_step, cfg, symbol):
    strikes = sorted(chain["strike"].tolist())
    atm = atm_strike(spot, strikes)  # nearest strike to spot
    
    # Iron Condor: sell OTM, buy further OTM for protection
    if setup.strategy in ("IRON_CONDOR", "IRON_CONDOR_WIDE"):
        wing = setup.wing_width  # e.g., 300 points for NIFTY
        short_put  = nearest(atm - wing, strikes)
        long_put   = nearest(short_put - wing, strikes)
        short_call = nearest(atm + wing, strikes)
        long_call  = nearest(short_call + wing, strikes)
    
    # Bull Put Spread: sell OTM put, buy further OTM put
    elif setup.strategy == "BULL_PUT_SPREAD":
        width = setup.wing_width  # e.g., 200 points
        short_strike = nearest(atm - width * 0.5, strikes)
        long_strike  = nearest(short_strike - width, strikes)
    
    # Naked Sell: sell single OTM option
    elif setup.strategy in ("NAKED_PUT_SELL", "NAKED_CALL_SELL"):
        short_strike = nearest(atm ± 100, strikes)
```

### 3.2 Premium Walking (Risk Management)

When option premiums fall outside acceptable bounds, the engine "walks" strikes to find better prices:

```
min_short_premium = ₹5 (floor)
max_short_premium = configurable (cap)

If premium < min → walk TOWARDS ATM (more premium, still OTM)
If premium > max → walk FURTHER OTM (less premium, less risk)
```

```python
# Phase 1: premium too LOW → walk towards ATM
if current_prem < min_prem:
    while current_prem < min_prem and walk_count < 5:
        next_strike = nearest(current_strike ± strike_step, strikes)
        current_prem = chain[strike == next_strike].ltp

# Phase 2: premium too HIGH → walk further OTM
if current_prem >= max_prem:
    while current_prem >= max_prem and walk_count < 5:
        next_strike = nearest(current_strike ∓ strike_step, strikes)
        current_prem = chain[strike == next_strike].ltp
```

### 3.3 Live Price Validation

```python
# Guard: reject if ALL premiums are 0 (market closed / OI-only data)
has_live_prices = (chain["ce_ltp"].sum() + chain["pe_ltp"].sum()) > 0
if not has_live_prices:
    return Setup(suitable=False, skip_reason="No live option prices")

# Guard: ATM must have live prices (not fully synthetic)
atm_row = chain[chain["strike"] == atm]
if atm_row.iloc[0]["ce_synthetic"] and atm_row.iloc[0]["pe_synthetic"]:
    return Setup(suitable=False, skip_reason="ATM options lack live prices")

# Guard: reject corrupt premiums (spot contamination)
MAX_REASONABLE_PREMIUM = 5000  # NIFTY/BANKNIFTY options rarely exceed this
if raw_price > MAX_REASONABLE_PREMIUM:
    log.error("REJECTED corrupt premium %s — spot contamination suspected")
```

---

## 4. Risk Calculations

### 4.1 Max Loss by Structure

```python
def calc_structure_max_loss(structure_type, net_credit, wing_width, lot_size, lots, **kwargs):
    if structure_type == "iron_condor":
        # Max loss = wing width × lot_size × lots - net credit received
        return max(0, (wing_width * lot_size * lots) - net_credit)
    
    elif structure_type in ("bull_put_spread", "bear_call_spread"):
        # Max loss = spread width × lot_size × lots - net credit
        return max(0, (wing_width * lot_size * lots) - net_credit)
    
    elif structure_type == "naked_short":
        # Max loss = 20% of underlying × lot_size × lots (estimated)
        spot = kwargs.get("underlying_spot", 25000)
        return round(spot * 0.20 * lot_size * lots, 2)
```

### 4.2 Exit Levels

```python
def calc_exit_levels(entry_net_credit, max_loss, cfg):
    nifty_cfg = cfg["nifty_options"]
    
    profit_target = entry_net_credit × profit_take_pct   # default: 50%
    stop_loss     = entry_net_credit × stop_loss_mult     # default: 125%
    
    return {
        "profit_target": profit_target,
        "stop_loss_level": stop_loss,
    }
```

### 4.3 Naked Legs Verification

```python
def check_naked_legs(legs, allow_naked=False):
    """Ensure all short positions have protective legs."""
    short_calls = count(side="SELL", type="CE")
    long_calls  = count(side="BUY", type="CE")
    short_puts  = count(side="SELL", type="PE")
    long_puts   = count(side="BUY", type="PE")
    
    # Naked = short without protection
    if short_calls > 0 and long_calls == 0:
        return False, "Naked short call detected"
    if short_puts > 0 and long_puts == 0:
        return False, "Naked short put detected"
    
    # Iron Condor: need balanced legs
    if short_calls != long_calls:
        return False, "Unbalanced call legs"
    if short_puts != long_puts:
        return False, "Unbalanced put legs"
    
    return True, "No naked legs"
```

---

## 5. Entry Conditions

### 5.1 Entry Window

```python
def is_within_entry_window(cfg, now, symbol):
    mode = cfg[f"{symbol.lower()}_options"]["mode"]  # "intraday" or "positional"
    
    # Weekend / holiday check
    if now.weekday() >= 5 or _is_holiday(now.date()):
        return False, "Market closed"
    
    # Time window: 09:45–14:30 IST
    entry_start = time(9, 45)
    entry_end   = time(14, 30)
    if not (entry_start <= now.time() <= entry_end):
        return False, "Outside entry window"
    
    # Positional: check allowed days
    if mode == "positional":
        allowed_days = cfg.get("positional_entry_days", [0,1,2,3,4])
        if now.weekday() not in allowed_days:
            return False, f"Not allowed on {day_name}"
    
    # Expiry day cutoff: no entries after 12:00 IST
    if is_symbol_expiry_today(symbol) and now.time() >= time(12, 0):
        return False, "Expiry day — no entries after 12:00"
    
    return True, "Valid entry window"
```

### 5.2 Expiry Management

| Symbol | Weekly Expiry | Monthly Expiry |
|--------|--------------|----------------|
| NIFTY | Tuesday (from Apr 2025) | Last Tuesday |
| BANKNIFTY | Wednesday | Last Tuesday |
| SENSEX | Thursday | — |

**Holiday rollback:** If expiry falls on holiday/weekend, shift to previous trading day.

**0-DTE avoidance:** On expiry day, the engine prefers next week's expiry to avoid gamma risk.

---

## 6. Exit Conditions (Smart Exits)

### 6.1 Exit Hierarchy

```
1. Grace Period    → skip all checks within 5 minutes of entry
2. VIX Spike       → exit if VIX spikes >15% AND VIX > 18 floor
3. Delta Breach    → exit if |net delta| > 0.35 (position too directional)
4. Profit Target   → exit at 50% of net credit received
5. Stop Loss       → exit at 125% of net credit (loss exceeds credit)
6. Trailing Lock   → lock profit at 50%, exit if drops below 70% of peak
7. Expiry Day      → forced exit at 15:15 IST on expiry day
```

### 6.2 VIX Spike Exit

```python
def check_vix_spike_exit(vix_current, vix_entry, threshold_pct=10.0):
    if vix_entry <= 0:
        return False, ""
    
    change_pct = ((vix_current - vix_entry) / vix_entry) * 100
    
    if change_pct > threshold_pct:
        return True, f"VIX spiked {change_pct:.1f}%"
    
    return False, ""
```

### 6.3 Net Delta Monitoring

```python
def net_position_delta(legs, chain):
    """Compute net delta across all legs using live chain data."""
    net_delta = 0.0
    for leg in legs:
        row = chain[chain["strike"] == leg["strike"]]
        delta_col = "ce_delta" if leg["type"] == "CE" else "pe_delta"
        leg_delta = row.iloc[0][delta_col]
        
        sign = 1 if leg["side"] == "BUY" else -1
        net_delta += sign * leg_delta * leg["qty"]
    
    return round(net_delta / min_qty, 4)
```

### 6.4 Trailing Lock Mechanism

```python
# Activate trailing lock when profit reaches trail_lock_pct (50%)
if current_pnl >= entry_credit * trail_lock_pct:
    trade["trailing_lock"] = True
    trade["peak_pnl"] = max(trade["peak_pnl"], current_pnl)

# Exit if profit drops below trail_floor_pct (35%) of peak
if trade["trailing_lock"]:
    if current_pnl < trade["peak_pnl"] * (trail_floor_pct / trail_lock_pct):
        return "TRAILING_LOCK_EXIT"
```

---

## 7. Synthetic Pricing (Black-Scholes)

When NSE option chain returns missing or zero LTPs, the engine falls back to Black-Scholes:

```python
def _bs_price(spot, strike, t, r, sigma, kind="CE"):
    """Black-Scholes option pricing."""
    if t <= 0 or sigma <= 0:
        return max(0, spot - strike) if kind == "CE" else max(0, strike - spot)
    
    d1 = (log(spot/strike) + (r + sigma²/2)*t) / (sigma*sqrt(t))
    d2 = d1 - sigma*sqrt(t)
    
    if kind == "CE":
        return spot * N(d1) - strike * exp(-r*t) * N(d2)
    else:
        return strike * exp(-r*t) * N(-d2) - spot * N(-d1)

def _bs_delta(spot, strike, t, r, sigma, kind="CE"):
    """Black-Scholes delta."""
    d1 = (log(spot/strike) + (r + sigma²/2)*t) / (sigma*sqrt(t))
    return N(d1) if kind == "CE" else N(d1) - 1
```

**Parameters:**
- `r = 0.065` (risk-free rate, ~6.5% RBI repo)
- `sigma = ce_iv or pe_iv or 0.15` (VIX fallback when IV missing)
- `t = max(tte_days, 0.5) / 365` (time to expiry in years)

**Warning:** If >50% of chain is synthetic, signals may be skewed. The engine flags this in logs.

---

## 8. IV Rank Calculation

```python
def iv_rank(symbol, current_iv, db_path):
    """Rolling 1-year percentile of ATM IV."""
    conn = sqlite3.connect(db_path)  # data/iv_history.sqlite
    
    # Store today's IV
    conn.execute(
        "INSERT OR REPLACE INTO iv_history VALUES (?, ?, ?)",
        (symbol, today_iso, current_iv)
    )
    
    # Fetch last 252 trading days
    rows = conn.execute(
        "SELECT atm_iv FROM iv_history WHERE symbol=? ORDER BY date DESC LIMIT 252",
        (symbol,)
    ).fetchall()
    
    ivs = [r[0] for r in rows if r[0] > 0]
    if len(ivs) < 60:
        return None  # insufficient history
    
    low, high = min(ivs), max(ivs)
    return ((current_iv - low) / (high - low)) * 100
```

**Usage:** Naked option sells require IV rank ≥ 40 (adequate premium compensation).

---

## 9. Configuration Parameters

### 9.1 NIFTY Options (`config.yaml`)

```yaml
nifty_options:
  enabled: true
  mode: "positional"                    # intraday | positional
  lot_size: 75
  
  # Strategy parameters
  iron_condor_wing_width: 300           # points
  spread_width: 200                     # points
  min_short_premium: 5                  # floor
  max_short_premium: 0                  # 0 = no cap
  
  # Exit parameters
  profit_take_pct: 0.50                 # 50% of credit
  stop_loss_mult: 1.25                  # 125% of credit
  vix_spike_exit_pct: 10.0
  vix_spike_exit_floor: 18.0
  
  # Entry window
  intraday_entry_start: "09:45"
  intraday_entry_end: "14:30"
  positional_entry_days: [0, 1, 2, 3, 4]  # Mon-Fri
  
  # Vol expansion filter
  avoid_vol_expansion: true
  vol_expansion_threshold: 5.0
  
  # Risk limits
  max_concurrent_structures: 2
  min_lots_per_leg: 1
```

### 9.2 BANKNIFTY Options

```yaml
banknifty_options:
  enabled: true
  mode: "positional"
  lot_size: 30
  iron_condor_wing_width: 600           # wider wings for BN
  spread_width: 400
  # ... (same structure as NIFTY)
```

---

## 10. P&L Calculation

### 10.1 Per-Leg P&L

```python
def calc_pnl_from_legs(legs, entry_prices, current_prices, lot_size):
    total_pnl = 0.0
    for leg in legs:
        key = (leg["strike"], leg["expiry"], leg["type"])
        entry_px = entry_prices[key]
        curr_px  = current_prices[key]
        qty = leg["qty"]
        
        if leg["side"] == "SELL":
            # Short: profit when premium decreases
            leg_pnl = (entry_px - curr_px) * qty
        else:
            # Long: profit when premium increases
            leg_pnl = (curr_px - entry_px) * qty
        
        total_pnl += leg_pnl
    
    return {"total_pnl": round(total_pnl, 2)}
```

### 10.2 Breakeven Calculation

```python
# Iron Condor:
credit_per_lot = net_credit / (lot_size * lots)
breakeven_low  = short_put_strike  - credit_per_lot
breakeven_high = short_call_strike + credit_per_lot

# Bull Put Spread:
breakeven = short_put_strike - credit_per_lot

# Naked Put Sell:
breakeven = short_strike - premium_received
```

---

## 11. Lot Size Mapping

| Symbol | Lot Size | Source |
|--------|----------|--------|
| NIFTY | 75 | Hardcoded / config |
| BANKNIFTY | 30 | Hardcoded / config |
| FINNIFTY | 65 | Hardcoded / config |
| SENSEX | 10 | Hardcoded / config |
| F&O Stocks | Variable | `config/fno200.csv` |

**Critical:** Lot size mismatches between Python backend, JSON API, and JavaScript UI cause P&L distortion. The `fno200.csv` file is the single source of truth for stock lot sizes.
