# Phase 1 Implementation Summary: Fast-Market Timing Upgrade

**Date**: 2026-06-24  
**Status**: ✅ COMPLETE - Ready for Testing & Deployment  
**Total Effort**: ~3.5 hours

---

## Overview

Phase 1 implements the **late-entry and VWAP-based timing gates** to reduce bot losses from chasing/exhaustion entries. The system validates trade entry timing against:

1. **VWAP overextension** (max 1.0% deviation)
2. **RSI exhaustion** (no LONG at RSI > 70)
3. **Price distance from open** (max 1.0 ATR movement)
4. **Market breadth/VIX** (exhaustion scoring)
5. **Event-risk mode** (size reduction on high exhaustion)

---

## Completed Tickets

### ✅ CFG-101: Config Schema Extension
**File**: `config/config.yaml` (lines 176-206)

- Added `timing_engine` section with configurable thresholds
- Includes: late_entry_filter, market_exhaustion, event_risk_mode subsections
- All values have sensible defaults (VWAP 1.0%, RSI 70/30, etc.)
- Backward compatible: optional with `enabled: true` flag

**Sample Config**:
```yaml
timing_engine:
  enabled: true
  late_entry_filter:
    max_vwap_dist_pct: 1.0
    rsi_threshold_long: 70
    rsi_threshold_short: 30
    max_intraday_atr_extension: 1.0
  market_exhaustion:
    breadth_drop_threshold_pct: 8
    vix_intraday_spike_pct: 5
  event_risk_mode:
    size_multiplier: 0.5
```

---

### ✅ SIG-201: Timing Module Implementation
**File**: `signals/timing.py` (complete rewrite)

**Functions Implemented**:
1. `is_overextended_from_vwap()` - Check price deviation from VWAP
2. `compute_atr_from_df()` - Calculate 14-period ATR
3. `compute_rsi_from_df()` - Calculate RSI(14)
4. `is_rsi_overextended()` - Check RSI overbought/oversold
5. `is_price_overextended()` - Check distance from day open in ATRs
6. `market_exhaustion_score()` - Calculate market stress (0.0–1.0)
7. `evaluate_timing_for_entry()` - **Unified entry point** integrating all checks

**Key Features**:
- ✅ Fail-open design: missing data defaults to `timing_ok=True`
- ✅ Comprehensive return tuples: `(bool_or_score, reason_str)`
- ✅ Event-risk mode activation on high exhaustion (>0.6 score)
- ✅ Size multiplier applied when market stressed

---

### ✅ SRV-301: Alert Gating in Trade Alerts
**File**: `dashboard/server.py`

**Changes**:
- Line 42: Added `from signals import timing as timing_mod` import
- Lines 982-984: Load timing engine config
- Lines 1000-1037 (LONG alerts): Added timing gate check before appending alert
  - Fetches 5m/1d OHLC for candidate stock
  - Calls `evaluate_timing_for_entry()` with LONG direction
  - Skips alert if `timing_ok=False` (logs reason)
- Lines 1080-1125 (SHORT alerts): Applied same timing gate for SHORT direction
- Updated alert dict: Added `timing_ok`, `timing_reason`, `event_risk_mode`, `size_multiplier`

**Fail-Safe**: Exception handling around timing checks—fails open if data unavailable

---

### ✅ PT-401: Trade Entry Gating
**File**: `dashboard/paper_trader.py`

**Changes**:
- Lines 1998-2014 (in `auto_enter_from_alerts()`): Check `timing_ok` flag before entering trade
  - Logs skipped trade via `journal.log_skipped_trade(..., "TIMING_OVEREXTENDED")`
  - Continues to next alert if timing gate fails
- Line 2017: Extract `size_multiplier` from alert
- Line 2090: Apply size multiplier to position qty: `adjusted_qty = int(size_result.qty * size_multiplier)`
- Lines 2173-2174: Store `event_risk_mode` flag in trade record for backtest analysis

**Effect**: Overextended trades are logged as WATCHLIST, not entered

---

### ✅ JRN-601: Journal Schema Extension
**File**: `ops/journal.py`

**New Columns Added to `trades` Table**:
- `entry_quality` TEXT: GOOD | LATE | CHASING | REVERSAL
- `loss_root_cause` TEXT: LATE_ENTRY | MARKET_REVERSAL | SENTIMENT_FLIP | OVEREXTENDED | NORMAL
- `timing_snapshot` JSON: {"vwap": 12.34, "rsi": 72, "breadth": 0.4, ...}
- `event_risk_mode` INTEGER: 1 if position entered during elevated market stress

**New Table: `trade_exit_analysis`**:
- Stores post-trade root cause analysis
- Links to `trades` table via foreign key
- Supports backtest labeling workflow

**New Methods**:
- `log_entry_quality()`: Record entry timing quality + snapshot at entry
- `log_loss_root_cause()`: Assign root cause post-trade (during exit or backtest analysis)

**Backward Compat**: All new columns nullable (NULL for existing trades); no breaking changes

---

## Testing

### ✅ Unit Tests Created
**File**: `tests/unit/test_timing.py` (260 lines)

**Test Coverage**:
- `TestIsOverextendedFromVwap`: 4 tests (threshold, fail-open)
- `TestIsRsiOverextended`: 3 tests (overbought, healthy, insufficient data)
- `TestIsPriceOverextended`: 4 tests (within/beyond limits, fail-open)
- `TestMarketExhaustionScore`: 3 tests (healthy, weak breadth, insufficient data)
- `TestEvaluateTimingForEntry`: 5 tests (disabled, all pass, overextended, event-risk)

**All tests passing** (verified local run)

### Integration Tests
- Manual: Config loads without error
- Manual: Timing module imports successfully
- Manual: Alert gating applied to LONG/SHORT alerts
- Manual: Trade entry respects timing_ok flag

---

## Data Flow Verification

```
1. Verdict Engine generates direction signal (LONG_ONLY, SHORT_ONLY, NEUTRAL)
   ↓
2. Alert Generation (_generate_trade_alerts)
   - Loads timing_engine config
   - For each stock candidate: calls timing_mod.evaluate_timing_for_entry()
   - Returns timing_ok, timing_reason, event_risk_mode
   - Skips alert if timing_ok = False
   ↓
3. Auto-Entry (auto_enter_from_alerts)
   - Checks alert.timing_ok before _enter_option_structure()
   - Applies size_multiplier if event_risk_mode = True
   ↓
4. Journal Logging (ops/journal.py)
   - Stores entry_quality ("GOOD" if timing_ok, "LATE" if blocked)
   - Stores timing_snapshot (VWAP, RSI, breadth at entry)
   - Stores event_risk_mode flag for backtest
   ↓
5. Backtest Analysis
   - Reads timing_snapshot + loss_root_cause labels
   - Tunes thresholds based on entry_quality correlation with outcomes
```

---

## Configuration Tuning Scenarios

### Conservative (Low-Risk) Settings
```yaml
late_entry_filter:
  max_vwap_dist_pct: 0.5  # Very tight VWAP gate
  rsi_threshold_long: 60   # Earlier overbought detection
  max_atr_extension: 0.7
market_exhaustion:
  breadth_drop_threshold_pct: 5  # Sensitive to breadth weakness
```

### Aggressive (High-Volume) Settings
```yaml
late_entry_filter:
  max_vwap_dist_pct: 2.0   # Relaxed VWAP tolerance
  rsi_threshold_long: 75   # Less sensitive to overbought
  max_atr_extension: 1.5
market_exhaustion:
  breadth_drop_threshold_pct: 12  # Loose breadth tolerance
```

---

## Success Metrics (Phase 1 Acceptance)

| Criterion | Status |
|-----------|--------|
| Config loads without validation error | ✅ YES |
| Timing functions return (bool/score, reason_str) tuples | ✅ YES |
| Alerts include `timing_ok` boolean field | ✅ YES |
| Overextended trades logged as WATCHLIST, not entered | ✅ YES |
| No regression in option setup generation | ✅ YES (no changes to verdict/setup logic) |
| Journal captures `entry_quality` on each trade | ✅ YES |
| Unit tests passing | ✅ YES |
| Committed to main branch | ✅ Ready |

---

## Key Implementation Decisions

### 1. Fail-Open Philosophy
- Missing VWAP, RSI, breadth, or VIX data → `timing_ok=True` (allow entry)
- Never block trades due to data unavailability
- Logged at DEBUG level for later analysis

### 2. Multi-Check Consolidation
- All 4 timing checks (VWAP, RSI, distance, exhaustion) must pass for `timing_ok=True`
- ANY single failing check blocks the entry
- Reason field consolidates all failures

### 3. Event-Risk Mode
- Activated when market_exhaustion > 0.6
- Applies size_multiplier (default 0.5) to position qty
- Allows trade to proceed but with reduced risk

### 4. Backward Compatibility
- New journal columns nullable (don't break existing reads)
- Timing gate is configurable and can be disabled
- Alert dict has safe defaults (timing_ok=True if timing engine disabled)

---

## Next Steps (Phase 2)

1. **Deploy Phase 1 to production** (paper trading)
2. **Monitor next 20 trades** for timing_ok field + entry_quality labels
3. **Validate MFE improvement** (target: > 0.5% in first 60 mins)
4. **Phase 2 items**:
   - AI-driven timing review (LLM: "Does this entry timing make sense?")
   - Dynamic threshold adjustment per market regime
   - Sentiment flip detection + cold startup mode
   - Comprehensive backtesting harness

---

## Files Modified

| File | Lines | Changes |
|------|-------|---------|
| `config/config.yaml` | 176-206 | Added `timing_engine` section |
| `signals/timing.py` | 1-319 | Complete rewrite with 7 functions |
| `dashboard/server.py` | 42, 983, 1000-1037, 1080-1125 | Import + timing gate for alerts |
| `dashboard/paper_trader.py` | 1998-2014, 2017, 2090, 2173-2174 | Entry gating + size multiplier |
| `ops/journal.py` | 24-45, 60-72, 195-272 | Schema extension + 2 new methods |
| `tests/unit/test_timing.py` | NEW | 260-line test suite |

---

## Rollback Plan (If Needed)

1. `git revert <commit-sha>` to undo all Phase 1 changes
2. Set `timing_engine.enabled: false` in config.yaml (graceful disable)
3. No database migration needed (new columns are nullable)

---

## Quick Reference: Timing Config Defaults

| Setting | Default | Range | Purpose |
|---------|---------|-------|---------|
| `max_vwap_dist_pct` | 1.0 | 0.5–3.0 | Max % deviation from VWAP |
| `rsi_threshold_long` | 70 | 50–100 | LONG overbought threshold |
| `rsi_threshold_short` | 30 | 0–50 | SHORT oversold threshold |
| `max_intraday_atr_extension` | 1.0 | 0.5–2.0 | Max ATRs from day open |
| `breadth_drop_threshold_pct` | 8 | 2–15 | Breadth weakness % threshold |
| `vix_intraday_spike_pct` | 5 | 2–20 | VIX spike % threshold |
| `size_multiplier` | 0.5 | 0.1–1.0 | Position size reduction (event risk) |

---

**Status**: Phase 1 ready for production deployment  
**Approved for**: Paper trading validation  
**Next Review**: After 20 trades or 1 week, whichever comes first
