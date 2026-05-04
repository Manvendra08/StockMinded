# StockMinded Improvement Plan - Test Plan

## Overview
This document outlines the test plan for the StockMinded improvement plan to make the tool behave like a disciplined Indian market paper trader.

## Test Categories

### 1. Unit Tests for Alert Generation by Regime
**File**: `tests/unit/test_alert_generation.py`

Tests verify that:
- ✅ LONG alerts generated in TREND_UP regime with bullish flow
- ✅ SHORT alerts generated in TREND_DOWN regime with bearish flow  
- ✅ No LONG stock alerts in TREND_DOWN regime
- ✅ No SHORT stock alerts in TREND_UP regime
- ✅ RANGE regimes prefer defined-risk options strategies
- ✅ High VIX (>24) blocks all trades with AVOID alert
- ✅ Late-day entries (after 14:45 IST) are blocked
- ✅ All alerts have required structured fields
- ✅ Alerts include tracking metadata (planned_risk, entry_rule, etc.)
- ✅ BankNifty divergence alerts generated when appropriate

**Run**: `pytest tests/unit/test_alert_generation.py -v`

### 2. Unit Tests for Paper Auto-Entry Risk Rejection
**File**: `tests/unit/test_paper_auto_entry.py`

Tests verify that:
- ✅ Trades enter when all risk gates pass
- ✅ Daily stop loss blocks new entries
- ✅ Monthly stop loss blocks new entries
- ✅ Concurrent open risk cap blocks excessive exposure
- ✅ Margin utilization cap blocks over-leveraged entries
- ✅ Correlation filter blocks highly correlated positions
- ✅ Duplicate symbol trades (same day) are blocked
- ✅ Low confidence alerts are filtered per settings
- ✅ Late-day entries (after 15:15 IST) are blocked
- ✅ Skipped trades are logged to journal with reason

**Run**: `pytest tests/unit/test_paper_auto_entry.py -v`

### 3. Unit Tests for Sizing from Stop Distance
**File**: `tests/unit/test_sizing_integration.py`

Tests verify that:
- ✅ Wider stop distance reduces position size
- ✅ Higher entry price reduces quantity for same risk budget
- ✅ Lot size flooring works correctly
- ✅ Zero/negative risk budget returns zero quantity
- ✅ Calculated risk never exceeds budget
- ✅ Option sizing uses max loss per lot correctly

**Run**: `pytest tests/unit/test_sizing_integration.py -v`

### 4. Unit Tests for Duplicate Trade and Late-Entry Blocking
**File**: `tests/unit/test_duplicate_late_entry.py`

Tests verify that:
- ✅ Same symbol traded today blocks duplicate entry
- ✅ Different symbols can be traded same day
- ✅ Closed trades today still block re-entry (one per symbol per day)
- ✅ Entries before 15:15 IST are allowed
- ✅ Entries at/after 15:15 IST are blocked
- ✅ Entries outside market hours are blocked

**Run**: `pytest tests/unit/test_duplicate_late_entry.py -v`

### 5. Manual Integration Checks

#### `/api/dashboard`
```bash
curl http://localhost:5050/api/dashboard | jq .
```
**Verify**:
- Regime classification is correct
- Flow bias matches PCR/max-pain data
- Leaders/laggards ranked correctly
- Structure plan matches regime

#### `/api/trade-alerts`
```bash
curl http://localhost:5050/api/trade-alerts | jq .
```
**Verify**:
- Alerts are structured objects (not text)
- Required fields present: symbol, direction, entry_trigger, entry_price, stop, target1, target2, trail_rule, qty, risk_rupees, confidence, no_trade_reason
- Metadata fields present: planned_risk, entry_rule, source_regime, flow_bias
- Regime/flow alignment enforced (no longs in downtrend, etc.)
- VIX filter working (>24 blocks trades)
- Time filter working (no entries after 14:45)

#### `/paper` (Paper Trading UI)
**Navigate to**: http://localhost:5050/paper

**Verify**:
- Open trades show planned_risk, entry_rule, trail_rule, source_regime
- Trade history includes skip_reason for rejected alerts
- Auto-enter respects risk gates (daily/monthly stops, concurrent risk)
- Skipped trades logged in database with detailed reasons

## Running All Tests
```bash
# Run all unit tests
pytest tests/unit/ -v

# Run with coverage
pytest tests/unit/ --cov=dashboard --cov=ops --cov=risk --cov-report=html

# Run specific test file
pytest tests/unit/test_alert_generation.py::TestAlertGenerationByRegime::test_long_alerts_in_trend_up -v
```

## Expected Outcomes
After running the test suite:
1. All unit tests pass (100% pass rate)
2. Code coverage >80% for modified modules
3. Manual API checks return properly structured responses
4. Paper trading UI shows enhanced trade metadata
5. Skipped trades visible in journal with learning reasons

## Regression Testing
Before deploying changes:
1. Run existing test suite to ensure no regressions
2. Verify dashboard still loads without errors
3. Confirm paper trading still functions with new risk gates
4. Test Telegram alerts still send correctly

## Continuous Integration
Add to CI pipeline:
```yaml
# .github/workflows/test.yml
- name: Run unit tests
  run: pytest tests/unit/ -v --cov=dashboard --cov=ops --cov=risk
  
- name: Check coverage
  run: pytest tests/unit/ --cov-fail-under=80
```
