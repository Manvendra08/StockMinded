# Order Flow — Signal to Execution

> Complete lifecycle of a trade from signal generation to journal entry.

---

## 1. Overview

```
┌────────────┐    ┌────────────┐    ┌────────────┐    ┌────────────┐
│   SIGNAL   │───▶│   ALERT    │───▶│ RISK GATE  │───▶│   ENTRY    │
│ Generation │    │ Generation │    │   Check    │    │ Execution  │
└────────────┘    └────────────┘    └────────────┘    └──────┬─────┘
                                                             │
       ┌─────────────────────────────────────────────────────┘
       ▼
┌────────────┐    ┌────────────┐    ┌────────────┐    ┌────────────┐
│  MONITOR   │◀───│    EXIT    │◀───│   EXIT     │◀───│   JOURNAL  │
│  (loop)    │    │  Trigger   │    │ Execution  │    │    Log     │
└────────────┘    └────────────┘    └────────────┘    └────────────┘
```

---

## 2. Signal Generation Phase

### 2.1 Trigger

| Trigger | Command | Frequency |
|---------|---------|-----------|
| Manual | `python main.py dashboard` | On-demand |
| Scheduled | `python main.py schedule` | Daily at configured IST time |
| Web UI | Flask API `/api/signals/refresh` | On dashboard refresh |
| Auto-entry | `/api/paper/auto-enter` | Every N minutes during market hours |

### 2.2 Data Fetch (Parallel)

```python
# dashboard/server.py: _run_engine()
with ThreadPoolExecutor(max_workers=2) as pool:
    pool.submit(_fetch_universe)  # feed.universe_ohlc(universe, period="6mo")
    pool.submit(_fetch_sectors)   # feed.sector_ohlc(sectors, period="6mo")
```

**Data sources:**
- **OHLC prices:** yfinance → `data/cache/ohlc/{symbol}_{date}.pkl`
- **Option chain:** NSE API / Dhan API → `data/cache/option_chain_{symbol}.json`
- **FII/DII:** NSE RSS → parsed DataFrame
- **AI sentiment:** ScrapeGraphAI SaaS or Google Gemini fallback

### 2.3 Signal Computation

| Step | Module | Output |
|------|--------|--------|
| 1 | `signals/regime.py` | `RegimeSnapshot` (regime, trend, VIX, ADX, breadth) |
| 2 | `signals/flows.py` | `FlowSnapshot` (FII/DII, PCR, max-pain, bias) |
| 3 | `signals/leadership.py` | `list[StockRank]` (RS slope, quintile, RVOL) |
| 4 | `signals/structure_map.py` | `StructurePlan` (primary/secondary structure) |
| 5 | `signals/verdict.py` | `CombinedVerdict` (StockVerdict + NiftyVerdict) |

---

## 3. Alert Generation Phase

### 3.1 Alert Structure

```python
{
    "symbol": "RELIANCE",
    "direction": "LONG",
    "entry_trigger": "A-grade leader, RS slope +12.5",
    "entry_price": 2850.00,        # fetched via _get_ltp()
    "stop": 2793.00,               # ATR-based or fixed %
    "target1": 2964.00,            # 2:1 reward-to-risk
    "target2": None,
    "trail_rule": "3-EMA 15m after +1R",
    "qty": 175,                    # from risk/sizing.py
    "risk_rupees": 9975.00,
    "confidence": "HIGH",
    "evidence": ["Regime: TREND_UP", "RS quintile: 5", "Sector: ENERGY (inflow)"],
    "verdict_action": "LONG_ONLY"
}
```

### 3.2 Alert Types

| Type | Condition | Confidence |
|------|-----------|------------|
| **Stock Long** | TREND_UP + Q4/Q5 leader + inflow sector | HIGH/MEDIUM |
| **Stock Short** | TREND_DOWN + Q4/Q5 laggard + outflow sector | HIGH/MEDIUM |
| **NIFTY Option** | Regime + bias + fresh option chain | MEDIUM/LOW |
| **BANKNIFTY Divergence** | BN% vs NIFTY% > 0.5% gap | LOW |
| **AVOID** | VIX > 24, data stale, WAIT verdict | N/A |

### 3.3 Pro Filters (Pre-Alert)

```python
# dashboard/server.py: _generate_trade_alerts()

# 1. Verdict gate
if verdict_action == "WAIT":
    return AVOID alert

# 2. Entry window
if time >= 14:15 IST:
    block equity entries (options window handled separately)

# 3. Expiry day cutoff
if is_expiry_today and time >= 12:00 IST:
    block equity entries

# 4. VIX filter
if VIX > 24:
    block all entries, return AVOID alert

# 5. AI sentiment influence
if AI confidence == LOW and regime == RANGE:
    caution flag (don't block, but note)
```

---

## 4. Risk Gate Phase

### 4.1 Guardrails Check (`risk/guardrails.py`)

```python
class Guardrails:
    def check_new_trade(
        self,
        proposed_risk: float,      # ₹ risk of new trade
        open_risk: float,          # ₹ risk of existing trades
        day_pnl: float,            # today's realized P&L
        month_pnl: float,          # month's realized P&L
        margin_used_pct: float,    # current margin utilization
        max_correlation_vs_open: float  # RS correlation with open positions
    ) -> GuardrailCheck:
```

**Checks performed:**

| Check | Rule | Default |
|-------|------|---------|
| Daily Stop | `day_pnl <= -capital × daily_stop_pct` | −2% |
| Monthly Stop | `month_pnl <= -capital × monthly_stop_pct` | −6% |
| Concurrent Risk | `open_risk + proposed > capital × concurrent_cap` | 3% |
| Margin Cap | `margin_used_pct > margin_util_cap` | 60% |
| Correlation | `correlation > correlation_max` | 0.70 |

### 4.2 Position Sizing (`risk/sizing.py`)

**Directional trades:**
```
risk_budget = capital × per_trade_pct (0.75%)
per_unit_risk = |entry - stop|
raw_qty = risk_budget / per_unit_risk
qty = floor(raw_qty / lot_size) × lot_size
```

**Option structures:**
```
risk_budget = capital × per_trade_pct
lots = floor(risk_budget / max_loss_per_lot)
qty = lots × lot_size
```

---

## 5. Entry Execution Phase

### 5.1 Equity Entry (`paper_trader.py: enter_trade()`)

```python
def enter_trade(alert: dict) -> dict:
    # 1. Market hours check
    if not is_market_open():
        return {"error": "Market closed"}
    
    # 2. Duplicate check (same symbol, same day)
    for t in db["trades"]:
        if t.symbol == alert.symbol and t.entry_date == today:
            return {"error": f"{symbol} already traded today"}
    
    # 3. Fetch LTP
    entry_price = _get_ltp(symbol)
    
    # 4. Lot size detection (F&O vs equity)
    lot_size = get_fno_lot_sizes().get(symbol)
    trade_type = "FUTURE" if lot_size else "STOCK"
    
    # 5. Stop loss calculation
    if alert.stop:
        sl_price = alert.stop
    elif alert.atr:
        sl_price = entry - (atr × atr_multiplier)  # dynamic ATR stop
    else:
        sl_price = entry × (1 - sl_pct/100)       # fixed % stop
    
    # 6. Target calculation
    if alert.target1:
        tgt_price = alert.target1
    else:
        tgt_price = entry × (1 + tgt_pct/100)
    
    # 7. Create trade record
    trade = {
        "id": _next_id(db),
        "symbol": symbol,
        "direction": direction,
        "entry_price": entry_price,
        "qty": qty,
        "sl_price": sl_price,
        "tgt_price": tgt_price,
        "status": "OPEN",
        "entry_time": now_ist(),
        "journal_id": journal.open_trade(...)  # sync to SQLite
    }
    
    db["trades"].append(trade)
    return trade
```

### 5.2 Option Entry (`paper_trader.py: enter_nifty_option_structure()`)

```python
def _enter_option_structure(setup, resolved_legs, cfg, symbol):
    # 1. Entry window check
    in_window, reason = is_within_entry_window(cfg, now, symbol)
    if not in_window:
        journal.log_skipped_trade(...)
        return {"error": reason}
    
    # 2. Naked legs check
    no_naked, reason = check_naked_legs(leg_dicts)
    if not no_naked:
        return {"error": reason}
    
    # 3. Duplicate structure check (1 per symbol per day)
    if already_traded_today(symbol, setup.strategy):
        return {"error": "Already traded today"}
    
    # 4. Net credit validation
    if net_credit <= 0:
        return {"error": "Non-credit structure"}
    
    # 5. Max loss calculation
    max_loss = calc_structure_max_loss(struct_type, net_credit, wing_width, ...)
    
    # 6. Create trade with smart-exit metadata
    trade = {
        "symbol": symbol,
        "structure": setup.strategy,
        "legs": [...],
        "net_credit": net_credit,
        "max_loss_rupees": max_loss,
        "entry_vix": vix_at_entry,      # for VIX spike exit
        "short_strikes": [...],          # for delta monitoring
        "peak_pnl": 0.0,                # for trailing lock
        "trailing_lock": False,
    }
```

### 5.3 Database Transaction

```python
@contextlib.contextmanager
def atomic_db_update():
    """Transaction-style update with Windows cross-process locking."""
    lock_f = open(LOCK_FILE, "r+")
    msvcrt.locking(lock_f.fileno(), msvcrt.LK_NBLCK, 1)  # acquire lock
    
    db = _load_db()      # read current state
    yield db             # caller modifies db
    _save_db(db)         # atomic write (tmp → bak → rename)
    
    msvcrt.locking(lock_f.fileno(), msvcrt.LK_UNLCK, 1)  # release lock
```

---

## 6. Monitor Phase

### 6.1 Check Cycle

**Trigger:** APScheduler or cron calls `/api/paper/check` every 5 minutes during market hours.

```python
def check_trades():
    open_trades = [t for t in db["trades"] if t["status"] == "OPEN"]
    
    # Batch LTP fetch for efficiency
    prices = _get_ltp_batch([t["symbol"] for t in open_trades])
    
    for trade in open_trades:
        ltp = prices.get(trade["symbol"])
        if ltp is None:
            continue  # skip if price unavailable
        
        # Update peak price (for trailing stop)
        if trade["direction"] == "LONG":
            trade["peak_price"] = max(trade["peak_price"], ltp)
        else:
            trade["peak_price"] = min(trade["peak_price"], ltp)
        
        # Check stop loss
        if _hit_stop_loss(trade, ltp):
            _close_trade(trade, ltp, "STOP_LOSS")
        
        # Check target
        elif _hit_target(trade, ltp):
            _close_trade(trade, ltp, "TARGET")
        
        # Check trailing stop
        elif trade.get("trail_sl") and _hit_trailing_stop(trade, ltp):
            _close_trade(trade, ltp, "TRAILING_STOP")
```

### 6.2 Smart Exits (Option Trades)

```python
def check_option_exits(trade, chain, vix_current):
    # Grace period: skip checks within 5 minutes of entry
    if _within_grace_period(trade, now, grace_minutes=5):
        return
    
    # 1. VIX spike exit
    if vix_spike > smart_exit_vix_spike_pct (15%) AND vix > floor (18):
        return "VIX_SPIKE"
    
    # 2. Delta threshold exit
    net_delta = net_position_delta(trade["legs"], chain)
    if abs(net_delta) > delta_threshold (0.35):
        return "DELTA_BREACH"
    
    # 3. Profit target (50% of net credit)
    current_pnl = _calc_current_pnl(trade, price_map)
    if current_pnl >= entry_credit × profit_take_pct:
        return "PROFIT_TARGET"
    
    # 4. Stop loss (125% of net credit)
    if current_pnl <= -(entry_credit × stop_loss_mult):
        return "STOP_LOSS"
    
    # 5. Trailing lock
    if current_pnl >= entry_credit × trail_lock_pct (50%):
        trade["trailing_lock"] = True
        trade["peak_pnl"] = max(trade["peak_pnl"], current_pnl)
    
    if trade["trailing_lock"] and current_pnl < trade["peak_pnl"] × 0.7:
        return "TRAILING_LOCK_EXIT"
    
    # 6. Expiry day exit (15:15 IST)
    if is_expiry_day(trade["expiry"]) and time >= 15:15:
        return "EXPIRY_EXIT"
```

---

## 7. Exit Execution Phase

### 7.1 Close Trade

```python
def _close_trade(trade, exit_price, reason):
    # Calculate P&L
    if trade["direction"] == "LONG":
        pnl = (exit_price - trade["entry_price"]) × trade["qty"]
    else:
        pnl = (trade["entry_price"] - exit_price) × trade["qty"]
    
    pnl_pct = (pnl / (trade["entry_price"] × trade["qty"])) × 100
    
    # Update trade record
    trade["exit_price"] = exit_price
    trade["exit_time"] = now_ist()
    trade["exit_reason"] = reason
    trade["pnl"] = round(pnl, 2)
    trade["pnl_pct"] = round(pnl_pct, 2)
    trade["status"] = "CLOSED"
    trade["hold_minutes"] = (exit_time - entry_time).total_seconds() / 60
    
    # Update cumulative P&L
    db["cumulative_pnl"] += pnl
    
    # Sync to SQLite journal
    journal.close_trade(trade["journal_id"], exit_price, pnl)
    
    # Log loss root cause (for feedback loop)
    if pnl < 0:
        journal.log_loss_root_cause(
            trade_id=trade["journal_id"],
            loss_root_cause=_classify_loss(trade),  # LATE_ENTRY, REVERSAL, NORMAL
            timing_at_exit=timing_snapshot
        )
```

### 7.2 EOD Summary

**Window:** 15:25–15:35 IST

```python
def generate_eod_summary():
    today_trades = [t for t in db["trades"] if t["entry_date"] == today]
    closed = [t for t in today_trades if t["status"] == "CLOSED"]
    
    summary = {
        "date": today,
        "total_trades": len(today_trades),
        "closed": len(closed),
        "open": len(today_trades) - len(closed),
        "wins": sum(1 for t in closed if t["pnl"] > 0),
        "losses": sum(1 for t in closed if t["pnl"] <= 0),
        "total_pnl": sum(t["pnl"] for t in closed),
        "win_rate": wins / closed if closed else 0,
        "avg_win": ...,
        "avg_loss": ...,
        "best_trade": max(closed, key=lambda t: t["pnl"]),
        "worst_trade": min(closed, key=lambda t: t["pnl"]),
        "regime_at_open": regime_snap.regime.value,
        "vix_at_open": regime_snap.vix,
    }
    
    db["daily_summaries"].append(summary)
    
    # Auto-close EOD trades (if enabled)
    if settings["auto_close_eod"]:
        for trade in open_trades:
            _close_trade(trade, ltp, "EOD_AUTO_CLOSE")
```

---

## 8. Journal Phase

### 8.1 SQLite Schema

```sql
-- Historical trade audit log
CREATE TABLE trades (
    id INTEGER PRIMARY KEY,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    symbol TEXT,
    structure TEXT,
    side TEXT,
    qty INTEGER,
    entry REAL,
    stop REAL,
    target REAL,
    exit_price REAL,
    risk_rupees REAL,
    pnl_rupees REAL,
    regime TEXT,
    notes TEXT,
    entry_quality TEXT,           -- GOOD, LATE, CHASING, REVERSAL
    loss_root_cause TEXT,         -- LATE_ENTRY, MARKET_REVERSAL, NORMAL
    timing_snapshot JSON,
    event_risk_mode INTEGER
);

-- Skip reasons for learning
CREATE TABLE skipped_trades (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    symbol TEXT,
    direction TEXT,
    alert_confidence TEXT,
    skip_reason TEXT,
    regime TEXT,
    flow_bias TEXT,
    risk_gate TEXT,
    notes TEXT
);

-- Exit analysis
CREATE TABLE trade_exit_analysis (
    id INTEGER PRIMARY KEY,
    trade_id INTEGER UNIQUE,
    ts TEXT NOT NULL,
    loss_root_cause TEXT,
    timing_at_exit JSON,
    notes TEXT
);
```

### 8.2 Feedback Loop Integration

```python
# After trade closes, feedback loop analyzes patterns
loop = FeedbackLoop("./data/journal.sqlite")
corrections = loop.get_corrections(lookback_days=30)

# Corrections are applied to future decisions:
for correction in corrections:
    if correction["action"] == "BLOCK":
        # Skip trades in this regime/signal combination
    elif correction["action"] == "REDUCE_SIZE":
        # Halve position size for this pattern
```

---

## 9. Error Handling & Fallbacks

| Scenario | Handling |
|----------|----------|
| **Data fetch fails** | Return empty DataFrame, log error, skip symbol |
| **LTP unavailable** | Skip exit check for this cycle (fail-safe) |
| **Option chain stale** | Use synthetic Black-Scholes pricing (flagged) |
| **Database locked** | Retry 20× with exponential backoff |
| **Telegram send fails** | Fall back to stdout logging |
| **AGoT components fail** | Continue with deterministic pipeline |

---

## 10. Timing Constraints

| Window | Time (IST) | Action |
|--------|------------|--------|
| Pre-market | Before 09:15 | Data fetch, signal generation |
| Equity entry | 10:00–14:15 | Avoid open whipsaw, ensure sufficient time |
| Option entry | 09:45–14:30 | Configurable per symbol |
| Expiry cutoff | 12:00 on expiry day | No new entries after 12:00 |
| Exit check | Every 5 min during market hours | SL/TGT/trailing evaluation |
| EOD flatten | 15:25–15:35 | Auto-close intraday positions |
| EOD summary | After 15:30 | Generate daily report |
