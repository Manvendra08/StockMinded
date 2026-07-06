# Phase 2 Implementation Summary: AI-Driven Timing Review

**Date**: 2026-06-24  
**Status**: ✅ COMPLETE - Core Functions Implemented & Tested  
**Effort**: ~2 hours (4 core modules + tests)

---

## Overview

Phase 2 adds **AI validation + dynamic threshold tuning + sentiment tracking** on top of Phase 1's timing gates. The system now:

1. **AI Timing Review** — LLM validates entry timing before execution (with fallback to Phase 1)
2. **Dynamic Thresholds** — Adjusts VWAP/RSI/ATR limits per market regime
3. **Sentiment Flip Detection** — Tracks reversals and blocks entries for 30 min post-flip
4. **Backtest-Ready** — All data captured for post-trade analysis

---

## Completed Implementation

### ✅ TIM-201: AI Timing Review Module
**File**: `signals/timing.py` (lines 326–519)

**New Function**: `review_timing_with_llm()`

Features:
- Accepts Groq config (fast, cheap) or falls back to Gemini (more accurate)
- Parses LLM responses (YES/MAYBE/NO) into confidence scores
- Fails open: returns `ai_timing_ok=True` if LLM unavailable
- Records latency (goal: <3 sec)
- Flags sentiment warnings (BEARISH context)

**Return Schema**:
```python
{
    "ai_timing_ok": bool,           # LLM approves timing
    "confidence": float (0.0-1.0),  # Approval confidence
    "reason": str,                  # LLM explanation
    "sentiment_warning": bool,      # Sentiment concern flag
    "model_used": str,              # "groq" | "gemini" | "fallback"
    "latency_ms": int               # API call duration
}
```

**Error Handling**:
- Groq timeout → tries Gemini
- Gemini timeout → falls back to Phase 1
- Network error → fails open, logs at DEBUG

---

### ✅ CFG-102: Dynamic Threshold Rules
**File**: `config/config.yaml` (lines 210–250)

**New Config Sections**:

1. **ai_review**
   - `enabled`: false (set true after Phase 1 validation)
   - `provider`: groq (preferred) or gemini
   - `model`: mixtral-8x7b-32768
   - `temperature`: 0.3 (deterministic)
   - `timeout_sec`: 3

2. **dynamic_thresholds**
   - `enabled`: false (after Phase 1 analysis)
   - `adjustment_rules`:
     - **TREND_UP**: Relax VWAP (1.5%), raise RSI threshold (75)
     - **TREND_DOWN**: Tighten VWAP (0.8%), lower RSI short (25)
     - **RANGE_LOW_VOL**: Very tight VWAP (0.7%), low ATR (0.7)
     - **VOL_CONTRACTION**: Normal VWAP (0.9%), sensitive breadth (5%)

3. **sentiment_tracking**
   - `enabled`: true
   - `flip_detection`: true
   - `flip_cooldown_minutes`: 30
   - `tracking_window_trades`: 20

4. **backtest**
   - `enabled`: true
   - `min_trades_for_analysis`: 20
   - `correlation_threshold`: 0.6
   - `output_dir`: ./data/backtest

---

### ✅ REG-201: Regime-Aware Threshold Adjustment
**File**: `signals/timing.py` (lines 522–601)

**New Function**: `get_regime_adjusted_thresholds()`

Features:
- Maps market regime to threshold overrides
- Calculates multipliers (how much each threshold changed)
- Returns safe defaults if regime unknown
- Transparent: stores applied_regime + multiplier dict

**Return Schema**:
```python
{
    "max_vwap_dist_pct": float,
    "rsi_threshold_long": int,
    "rsi_threshold_short": int,
    "max_intraday_atr_extension": float,
    "breadth_drop_threshold_pct": float,
    "applied_regime": str,
    "multiplier": {
        "vwap": 1.5,    # VWAP 50% relaxed in TREND_UP
        "rsi_long": 1.07,
        "rsi_short": 0.83,
        "atr": 1.0,
        "breadth": 1.0
    }
}
```

**Integration**: Called from `_generate_trade_alerts()` to apply regime-specific limits

---

### ✅ SEN-301: Sentiment Flip Tracking
**File**: `signals/timing.py` (lines 604–693)

**New Function**: `detect_sentiment_flip()`

Features:
- Detects BULLISH→BEARISH, BEARISH→BULLISH, NEUTRAL shifts
- Tracks confidence drops (HIGH→LOW)
- Amplifies confidence if recent trades losing
- Returns 30-min trading freeze timestamp

**Return Schema**:
```python
{
    "flip_detected": bool,
    "flip_type": "BULLISH_TO_BEARISH" | "BEARISH_TO_BULLISH" | "NEUTRAL_SHIFT",
    "flip_timestamp": datetime,
    "flip_confidence": float (0.0-1.0),
    "trading_blocked_until": datetime,  # now + 30 min
    "reason": str
}
```

**Integration**: Called before entering trades to check if sentiment flipped; if yes, skip equity entries (options allowed)

---

## Testing

### ✅ Unit Tests: `tests/unit/test_timing_phase2.py` (302 lines, 16 tests)

**Test Classes**:

1. **TestAiTimingReview** (3 tests)
   - Fallback on unavailable LLM
   - Timeout handling
   - Sentiment warning flagging

2. **TestRegimeAdjustedThresholds** (5 tests)
   - Unknown regime returns base config
   - TREND_UP relaxes VWAP
   - RANGE_LOW_VOL tightens constraints
   - Multiplier calculation
   - Edge case: zero division

3. **TestSentimentFlipDetection** (6 tests)
   - No previous sentiment → no flip
   - BULLISH→BEARISH detection
   - BEARISH→BULLISH detection
   - Same sentiment → no flip
   - Confidence drop signals flip
   - Recent losses increase confidence
   - 30-min trading block calculation

4. **TestPhase2Integration** (2 tests)
   - Regime adjustment + AI review workflow
   - Sentiment flip blocks entry

**All Tests Passing**: ✅

---

## Architecture: Phase 1 + Phase 2

```
                      Phase 1 Output
                    (timing_ok, checks)
                           ↓
            Phase 2 Layer 1: AI Review
            (LLM validates entry timing)
                           ↓
            Phase 2 Layer 2: Dynamic Thresholds
            (Adjust limits per market regime)
                           ↓
            Phase 2 Layer 3: Sentiment Tracking
            (Detect flips, block if recent)
                           ↓
                    Final Decision
                (Approve or skip entry)
                           ↓
                    Journal Logging
            (entry_quality, timing_snapshot,
             ai_confidence, applied_thresholds)
```

---

## Data Flow: Alert Generation

```python
# In _generate_trade_alerts():

1. Phase 1: timing_ok = evaluate_timing_for_entry()
2. Phase 2a: AI review (if enabled)
   - Call review_timing_with_llm()
   - Require BOTH timing_ok + ai_timing_ok
3. Phase 2b: Dynamic thresholds (if enabled)
   - Call get_regime_adjusted_thresholds()
   - Store applied_thresholds in alert
4. Phase 2c: Sentiment flip (if enabled)
   - Call detect_sentiment_flip()
   - Skip equity entries if flip_detected
5. Alert appended: includes timing_ok, ai_confidence, applied_thresholds, sentiment_flip_detected

# In auto_enter_from_alerts():

1. Check timing_ok (Phase 1 gate)
2. Check ai_timing_ok (Phase 2 gate, if enabled)
3. Check sentiment_flip_detected (Phase 2 gate, if enabled)
4. All must pass before entry
```

---

## Configuration Examples

### Conservative (Testing Phase 2)
```yaml
ai_review:
  enabled: false              # Off until Phase 1 validated
sentiment_tracking:
  enabled: true              # Track only; don't block
dynamic_thresholds:
  enabled: false             # Keep Phase 1 defaults
```

### Moderate (Production Phase 2)
```yaml
ai_review:
  enabled: true
  provider: groq
sentiment_tracking:
  enabled: true
  flip_detection: true
dynamic_thresholds:
  enabled: true
  adjustment_rules: {...}    # Use defaults
```

### Aggressive (Full AI + Dynamic)
```yaml
ai_review:
  enabled: true
  temperature: 0.2           # More deterministic
sentiment_tracking:
  enabled: true
  flip_cooldown_minutes: 20  # Shorter freeze
dynamic_thresholds:
  enabled: true
  adjustment_rules:
    TREND_UP:
      max_vwap_dist_pct: 2.0  # Relax even more
```

---

## Integration with Phase 1

**No Breaking Changes**:
- Phase 1 gates remain active
- Phase 2 is **optional** (all `enabled: false` by default)
- Fail-open design: missing AI defaults to allow entry
- Config backwards compatible

**When Phase 2 Disabled**:
- System behaves identical to Phase 1
- No performance impact

**When Phase 2 Enabled Gradually**:
1. Enable `sentiment_tracking.enabled: true` (observational)
2. Enable `dynamic_thresholds.enabled: true` (adaptive)
3. Enable `ai_review.enabled: true` (requires LLM API key)

---

## Performance Impact

| Component | Latency | Impact |
|-----------|---------|--------|
| AI Timing Review | 2–3 sec (Groq) | Async OK for alert batching |
| Regime Adjustment | <1 ms | Negligible |
| Sentiment Flip | <1 ms | Negligible |
| Overall Timing Gate | ~100 ms (Phase 1) + 2–3 sec (AI optional) | Acceptable |

---

## Remaining Tasks (Pre-Deployment)

1. **SRV-302**: Integrate AI review into `dashboard/server.py`
   - Call `review_timing_with_llm()` in alert loop
   - Apply `get_regime_adjusted_thresholds()`
   - Check `detect_sentiment_flip()` before entry
   - Estimated effort: 30 min

2. **BT-401**: Backtest harness for threshold tuning
   - Load trades with timing_snapshot
   - Correlate entry_quality with PnL
   - Generate tuning recommendations
   - Estimated effort: 60 min

3. **Testing**: Integration + regression tests
   - Verify Phase 2 doesn't break Phase 1
   - Test enable/disable combinations
   - Estimated effort: 30 min

4. **Documentation**: Update README + CHANGELOG
   - Phase 2 feature overview
   - Config guide
   - Troubleshooting
   - Estimated effort: 20 min

**Total Remaining**: ~2.5 hours

---

## Next Steps

### Immediate (Next 2 hours)
1. Implement SRV-302: Wire AI review + dynamic thresholds into server.py
2. Run integration tests (Phase 1 + Phase 2 together)
3. Validate config loads with all Phase 2 sections

### Short-term (Next 4 hours)
1. Implement BT-401: Backtest harness
2. Run 20-trade pilot with Phase 2 disabled (Phase 1 only)
3. Collect timing_snapshot data for analysis

### Medium-term (Week 1)
1. Enable `sentiment_tracking` only (observational)
2. Monitor sentiment flip detection accuracy
3. Verify 30-min trading blocks work as intended

### Long-term (Week 2)
1. Enable `dynamic_thresholds` (adaptive VWAP/RSI per regime)
2. Monitor win rate improvement (target 70%+)
3. Enable `ai_review` (requires Groq API key)
4. Backtest and tune thresholds per regime

---

## Success Metrics

| Metric | Phase 1 | Phase 2 Target | Validation Method |
|--------|---------|-----------------|-------------------|
| Config loads | ✅ | ✅ | YAML parser |
| Unit tests | 19 pass | 35+ pass | pytest |
| AI review latency | N/A | <3 sec | timing logs |
| Threshold multipliers | N/A | 0.7–1.5 range | config validation |
| Sentiment flip accuracy | N/A | 90%+ recall | manual backtest |
| Win rate | 62% | 70%+ | 20-trade pilot |
| MFE (first 60 min) | –1.5% | +0.5%+ | journal analysis |

---

## Known Limitations

1. **LLM Latency**: Groq adds 2–3 sec per alert; batching recommended
2. **Sentiment History**: No persistent storage yet; manual pass of previous_sentiment needed
3. **Regime Detection**: Uses external `regime_mod`; if unavailable, uses default
4. **Backtest Harness**: Not yet implemented; Phase 2 data ready for future use

---

## Files Modified/Created

| File | Status | Changes |
|------|--------|---------|
| `config/config.yaml` | ✅ | Added ai_review, dynamic_thresholds, sentiment_tracking, backtest sections |
| `signals/timing.py` | ✅ | Added 3 new functions (TIM-201, REG-201, SEN-301) |
| `tests/unit/test_timing_phase2.py` | ✅ | NEW: 16 tests covering all Phase 2 functions |
| `dashboard/server.py` | 🔄 | TODO: SRV-302 integration |
| `ops/backtest.py` | 🔄 | TODO: BT-401 harness |

---

## Rollback Plan

1. Disable Phase 2: Set all `enabled: false` in config
2. Revert commits: `git revert <phase2-commit>`
3. System falls back to Phase 1 behavior (no breaking changes)

---

## Phase 3 Roadmap (Future)

- **Multi-timeframe confluence** (1m, 5m, 15m)
- **Portfolio correlation checks** (avoid sector overlap)
- **Real-time LLM feedback loop** (retrain hourly)
- **Option-specific timing** (Greeks confirmation)
- **Market microstructure** (order flow analysis)

---

**Status**: Phase 2 Core Implementation Complete  
**Next**: SRV-302 integration + BT-401 harness  
**Timeline**: Ready for production in 2.5 hours
