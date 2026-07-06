# Phase 2 Plan: AI-Driven Timing Review & Dynamic Threshold Tuning

**Version**: 1.0  
**Date**: 2026-06-24  
**Status**: Planning → Implementation  
**Effort**: ~4–5 hours

---

## Overview

Phase 2 builds on Phase 1's timing gates by adding:

1. **AI Timing Review** — LLM validates entry timing quality before execution
2. **Dynamic Thresholds** — Adjust VWAP/RSI/distance limits per market regime
3. **Sentiment Flip Detection** — Track mid-session sentiment reversals
4. **Backtesting Harness** — Correlate entry_quality with PnL outcomes for tuning

**Goal**: Move from static rules to **adaptive, AI-validated timing** that learns from Phase 1 results.

---

## Phase 2 Architecture

```
Phase 1 Output (timing_ok, entry_quality, timing_snapshot)
    ↓
Phase 2 Layer 1: AI Review
├─ LLM prompt: "Given these market conditions + timing snapshot, is this entry OK?"
├─ Groq API call (2-3 sec latency)
├─ Fallback to Phase 1 if LLM unavailable
└─ Result: ai_timing_ok, confidence, reason
    ↓
Phase 2 Layer 2: Dynamic Thresholds
├─ Detect market regime (TREND_UP, RANGE_LOW_VOL, VOL_CONTRACTION, etc.)
├─ Adjust thresholds based on regime (e.g., relax VWAP in trends)
├─ Store applied_thresholds in journal
└─ Result: regime_aware_limits
    ↓
Phase 2 Layer 3: Sentiment Tracking
├─ Monitor sentiment changes (BULLISH → NEUTRAL → BEARISH)
├─ Track flip timestamps + confidence
├─ Trigger cold-startup mode if sentiment flipped in last 30 min
└─ Result: sentiment_trend, recent_flip
    ↓
Phase 2 Layer 4: Backtest Harness
├─ Read Phase 1 trades + timing_snapshots
├─ Correlate entry_quality with final PnL
├─ Suggest threshold adjustments
└─ Result: tuning_recommendations.json
```

---

## Ticket Breakdown (7 tickets, sequential order)

### **TIM-201: AI Timing Review Module**
**File**: `signals/timing.py` (extension)

**New Function**:
```python
def review_timing_with_llm(
    symbol: str,
    direction: str,
    price: float,
    timing_snapshot: dict,
    market_regime: str,
    ai_sentiment: dict,
    use_groq: bool = True
) -> dict:
    """
    LLM review of entry timing.
    
    Args:
        symbol: Stock ticker
        direction: LONG or SHORT
        price: Current LTP
        timing_snapshot: From Phase 1 evaluate_timing_for_entry()
        market_regime: Detected regime (TREND_UP, RANGE_LOW_VOL, etc.)
        ai_sentiment: Current AI sentiment dict
        use_groq: Try Groq first (fast); fallback to Gemini
    
    Returns:
        {
            "ai_timing_ok": bool,
            "confidence": float (0.0-1.0),
            "reason": str,
            "sentiment_warning": bool,
            "model_used": "groq" | "gemini" | "fallback",
            "latency_ms": int
        }
    
    Prompt Template:
        "Market: {regime}. Stock: {symbol}. Direction: {direction}.
         VWAP: {vwap}%, RSI: {rsi}, Breadth: {breadth}.
         Sentiment: {sentiment}. Is entry timing sound? Reply: YES|MAYBE|NO."
    
    Logic:
    - Parse LLM response (YES → ai_timing_ok=True, NO → False)
    - Extract confidence from response tone
    - Flag if sentiment recently flipped
    - Time the call; log if >3 sec
    - Fallback to Phase 1 if exception
    """
```

**Integration Points**:
- Import Groq, Gemini APIs from config
- Call from `evaluate_timing_for_entry()` if `ai_review_enabled`
- Store `ai_timing_ok` + `ai_confidence` in alert dict

**Fail-Safe**: If LLM unavailable, return `ai_timing_ok=True` (allow entry)

**Effort**: ~40 min

---

### **CFG-102: Dynamic Threshold Rules**
**File**: `config/config.yaml` (extension)

**Addition** (after timing_engine section):

```yaml
timing_engine:
  # ... existing Phase 1 config ...
  
  # [NEW] AI Review
  ai_review:
    enabled: false  # Set to true after validation
    provider: groq  # groq | gemini | openrouter
    model: "mixtral-8x7b-32768"  # Groq model
    temperature: 0.3  # Deterministic
    timeout_sec: 3
    fallback_on_error: true
  
  # [NEW] Dynamic Threshold Adjustment
  dynamic_thresholds:
    enabled: false  # Set to true after Phase 1 analysis
    adjustment_rules:
      TREND_UP:
        # In uptrends, be more lenient on VWAP deviation
        max_vwap_dist_pct: 1.5  # Relaxed from default 1.0
        rsi_threshold_long: 75   # Higher tolerance for momentum
      TREND_DOWN:
        max_vwap_dist_pct: 0.8   # Stricter on shorts
        rsi_threshold_short: 25
      RANGE_LOW_VOL:
        # In ranges, tight around VWAP
        max_vwap_dist_pct: 0.7
        max_intraday_atr_extension: 0.7
      VOL_CONTRACTION:
        max_vwap_dist_pct: 0.9
        breadth_drop_threshold_pct: 5  # Sensitive
  
  # [NEW] Sentiment Tracking
  sentiment_tracking:
    enabled: true
    flip_detection: true
    flip_cooldown_minutes: 30  # Don't trade 30 min after flip
    tracking_window_trades: 20  # Monitor last 20 trades
  
  # [NEW] Backtest Configuration
  backtest:
    enabled: true
    min_trades_for_analysis: 20
    correlation_threshold: 0.6  # Min R² for tuning suggestion
    output_dir: "./data/backtest"
```

**Validation**:
- `ai_review.timeout_sec` in [1, 10]
- `dynamic_thresholds.adjustment_rules` keys match known regimes
- Adjusted thresholds within reasonable bounds

**Effort**: ~10 min

---

### **REG-201: Market Regime-Aware Thresholds**
**File**: `signals/timing.py` (new function)

**New Function**:
```python
def get_regime_adjusted_thresholds(
    market_regime: str,
    base_config: dict,
    dynamic_rules: dict
) -> dict:
    """
    Adjust timing thresholds based on detected market regime.
    
    Args:
        market_regime: TREND_UP, TREND_DOWN, RANGE_LOW_VOL, VOL_CONTRACTION, etc.
        base_config: Base timing_engine config from CFG-102
        dynamic_rules: dynamic_thresholds.adjustment_rules from config
    
    Returns:
        {
            "max_vwap_dist_pct": float,
            "rsi_threshold_long": int,
            "rsi_threshold_short": int,
            "max_intraday_atr_extension": float,
            "breadth_drop_threshold_pct": float,
            "applied_regime": str,
            "multiplier": dict  # How much each threshold was adjusted
        }
    
    Logic:
    - If regime in dynamic_rules: apply overrides
    - Otherwise: return base config
    - Store multiplier for transparency
    - Log applied regime
    """
```

**Integration Points**:
- Call from `evaluate_timing_for_entry()` with current regime
- Pass adjusted limits to VWAP/RSI/distance checks
- Store `applied_thresholds` in journal

**Effort**: ~20 min

---

### **SEN-301: Sentiment Flip Tracking**
**File**: `signals/timing.py` (new function)

**New Function**:
```python
def detect_sentiment_flip(
    current_sentiment: dict,
    previous_sentiment: dict | None,
    window_trades: list[dict]
) -> dict:
    """
    Detect recent sentiment reversals (BULLISH → BEARISH, etc.).
    
    Args:
        current_sentiment: Latest AI sentiment from flows_mod.snapshot()
        previous_sentiment: Last recorded sentiment (from journal)
        window_trades: Last N trades (for context)
    
    Returns:
        {
            "flip_detected": bool,
            "flip_type": "BULLISH_TO_BEARISH" | "BEARISH_TO_BULLISH" | "NEUTRAL_SHIFT",
            "flip_timestamp": datetime,
            "flip_confidence": float,
            "trading_blocked_until": datetime,  # 30 min freeze
            "reason": str
        }
    
    Logic:
    - Compare current vs previous sentiment.overall
    - If changed + recent trades show losses: high confidence flip
    - If confidence.drop > 20% in last 20 min: likely flip
    - Return trading_blocked_until = now + 30 min
    """
```

**Integration Points**:
- Call before entering trade
- Store in alert dict: `sentiment_flip_detected`
- Block equity entries if flip detected (but allow options)

**Effort**: ~25 min

---

### **SRV-302: Integrate AI Review in Alerts**
**File**: `dashboard/server.py` (extension)

**Changes** (in `_generate_trade_alerts()`):

```python
# After Phase 1 timing gate (line 1037):

# --- NEW: AI Timing Review (Phase 2) ---
if cfg.get("timing_engine", {}).get("ai_review", {}).get("enabled"):
    try:
        ai_result = timing_mod.review_timing_with_llm(
            symbol=sym,
            direction=direction,
            price=price,
            timing_snapshot=timing_result.get("checks", {}),
            market_regime=regime_name,
            ai_sentiment=ai_sentiment,
            use_groq=cfg["timing_engine"]["ai_review"].get("provider") == "groq"
        )
        alert["ai_timing_ok"] = ai_result["ai_timing_ok"]
        alert["ai_confidence"] = ai_result["confidence"]
        alert["ai_reason"] = ai_result["reason"]
        
        # Require BOTH Phase 1 + AI approval
        if not ai_result["ai_timing_ok"]:
            logging.info(f"[AI_REVIEW] {sym} {direction}: AI rejected. Reason: {ai_result['reason']}")
            continue  # Skip alert
    except Exception as e:
        logging.debug(f"[AI_REVIEW] {sym}: LLM check failed ({e}); failing open")
        # Fail-open: continue with timing_ok from Phase 1
# --- END AI REVIEW ---

# --- NEW: Dynamic Thresholds (Phase 2) ---
if cfg.get("timing_engine", {}).get("dynamic_thresholds", {}).get("enabled"):
    adjusted_thresholds = timing_mod.get_regime_adjusted_thresholds(
        market_regime=regime_name,
        base_config=cfg["timing_engine"]["late_entry_filter"],
        dynamic_rules=cfg["timing_engine"]["dynamic_thresholds"].get("adjustment_rules", {})
    )
    alert["applied_thresholds"] = adjusted_thresholds
# --- END DYNAMIC THRESHOLDS ---

# --- NEW: Sentiment Flip Detection (Phase 2) ---
if cfg.get("timing_engine", {}).get("sentiment_tracking", {}).get("enabled"):
    flip_result = timing_mod.detect_sentiment_flip(
        current_sentiment=ai_sentiment,
        previous_sentiment=None,  # TODO: fetch from journal
        window_trades=[]  # TODO: fetch recent trades
    )
    if flip_result["flip_detected"] and cfg["timing_engine"]["sentiment_tracking"].get("flip_detection"):
        logging.warning(f"[SENTIMENT_FLIP] {flip_result['flip_type']}: Trading blocked until {flip_result['trading_blocked_until']}")
        # Optional: skip all equity entries for 30 min
        # For now: flag in alert for manual review
        alert["sentiment_flip_detected"] = True
# --- END SENTIMENT FLIP ---
```

**Effort**: ~30 min

---

### **BT-401: Backtest Harness**
**File**: `ops/backtest.py` (new)

**New Class**:
```python
class TimingBacktester:
    """Correlate entry_quality with outcomes; suggest threshold tuning."""
    
    def __init__(self, journal_db: str, output_dir: str):
        pass
    
    def load_trades_with_timing(self, since_date: str | None = None) -> pd.DataFrame:
        """Load trades + entry_quality + timing_snapshot."""
    
    def analyze_entry_quality_performance(self) -> dict:
        """
        Correlate entry_quality with PnL.
        
        Returns:
            {
                "GOOD": {"win_rate": 0.72, "avg_pnl": +1.2, "count": 15},
                "LATE": {"win_rate": 0.45, "avg_pnl": -0.8, "count": 5},
                "CHASING": {"win_rate": 0.20, "avg_pnl": -2.1, "count": 3},
                "correlation_r2": 0.68
            }
        """
    
    def suggest_threshold_adjustments(self) -> dict:
        """
        Based on analysis, recommend config changes.
        
        Example:
            {
                "max_vwap_dist_pct": {
                    "current": 1.0,
                    "suggested": 0.8,
                    "reason": "LATE entries have 55% higher losses",
                    "confidence": 0.75
                }
            }
        """
    
    def generate_report(self) -> str:
        """Generate HTML/Markdown backtest report."""
```

**Effort**: ~60 min (complex statistical analysis)

---

### **Testing (Phase 2)**
**File**: `tests/unit/test_timing_phase2.py` (new)

**Tests**:
- `TestAiTimingReview`: Mock Groq calls, verify fallback
- `TestRegimeAdjustedThresholds`: Verify threshold overrides per regime
- `TestSentimentFlipDetection`: Detect flips, verify cooldown
- `TestBacktestHarness`: Load trades, compute correlations

**Effort**: ~45 min

---

### **Documentation & Commit**
**Files**:
- `PHASE2_IMPLEMENTATION_SUMMARY.md` — Executive summary
- `PHASE2_READINESS_CHECKLIST.md` — Validation checklist
- Git commit with Phase 2 message

**Effort**: ~20 min

---

## Implementation Sequence & Dependencies

```
TIM-201 (AI module)
├─ Independent; tests in isolation
├─ Can deploy without CFG-102/SRV-302
└─ ~40 min

CFG-102 (config)
├─ Independent; no code changes
├─ Add config keys for Phase 2 features
└─ ~10 min

REG-201 (regime thresholds)
├─ Depends on: regime_mod available
├─ Enhances: evaluate_timing_for_entry()
└─ ~20 min

SEN-301 (sentiment tracking)
├─ Independent; uses flows_mod.snapshot()
├─ Integrates into alert flow
└─ ~25 min

SRV-302 (integration)
├─ Depends on: TIM-201, CFG-102, REG-201, SEN-301
├─ Wires everything into alert generation
└─ ~30 min

BT-401 (backtest harness)
├─ Depends on: Phase 1 trades in journal
├─ Post-deployment analysis tool
└─ ~60 min

Testing & Docs
├─ Depends on: All modules ready
├─ ~65 min

TOTAL: ~250 min (~4.2 hours)
```

---

## Configuration Scenarios

### Conservative (Testing Mode)
```yaml
ai_review:
  enabled: false  # Off until Phase 1 validated
sentiment_tracking:
  enabled: true   # Just track; don't block
dynamic_thresholds:
  enabled: false  # Keep Phase 1 defaults
```

### Moderate (Production Phase 2)
```yaml
ai_review:
  enabled: true
  temperature: 0.3
sentiment_tracking:
  enabled: true
  flip_detection: true
dynamic_thresholds:
  enabled: true
```

### Aggressive (Full AI)
```yaml
ai_review:
  enabled: true
  temperature: 0.2  # More deterministic
sentiment_tracking:
  enabled: true
  flip_detection: true
dynamic_thresholds:
  enabled: true
  # Relax thresholds in trends to maximize capture
```

---

## Success Metrics

| Metric | Phase 1 | Phase 2 Target | Timeline |
|--------|---------|-----------------|----------|
| Win rate (equity) | 62% | 70%+ | 2 weeks |
| Avg MFE (first 60 min) | –1.5% | +0.5%+ | 2 weeks |
| AI review accuracy | N/A | >75% | After 30 trades |
| Threshold tuning precision | N/A | 0.8+ R² | After 50 trades |
| Sentiment flip detection | N/A | 90%+ recall | 1 week |

---

## Rollback Plan

1. **Disable AI**: Set `ai_review.enabled: false` in config
2. **Disable dynamic thresholds**: Set `dynamic_thresholds.enabled: false`
3. **Revert sentiment blocking**: Set `sentiment_tracking.flip_detection: false`
4. **Fall back to Phase 1**: All gates still active; AI optional

---

## Open Questions for User

1. **LLM Provider**: Prefer Groq (fast, cheap) or Gemini (more accurate)?
2. **AI Review Trigger**: Review EVERY alert or only QUESTIONABLE ones (e.g., RSI near threshold)?
3. **Sentiment Freeze**: Block entries for full 30 min post-flip, or apply size reduction instead?
4. **Backtest Frequency**: Run daily, weekly, or on-demand?

---

## Phase 2 → Phase 3 (Future)

- **Multi-timeframe analysis** (1m, 5m, 15m candles for confluence)
- **Portfolio correlation** (avoid overlapping sector exposure)
- **Real-time LLM feedback loop** (retrain thresholds hourly)
- **Option-specific timing** (Greeks confirmation for spreads)

---

**Next Step**: Approve Phase 2 scope, then execute TIM-201 → Testing → Deployment
