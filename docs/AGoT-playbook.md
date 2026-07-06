# AGoT Playbook — Adaptive Graph of Thoughts

> Graph-based reasoning system for dynamic market analysis with multi-hypothesis evaluation, self-correction, and learning from outcomes.

---

## 1. What is AGoT?

The **Adaptive Graph of Thoughts** (AGoT) is a reasoning framework that replaces rigid if-else pipelines with a **directed acyclic graph** (DAG) of hypotheses. Each node represents a thought (observation, hypothesis, or decision) with a dynamically updated confidence score based on accumulated evidence.

### 1.1 Why AGoT?

Traditional signal pipelines suffer from:
- **Binary classification:** "It IS trend up" — no ambiguity handling
- **No self-correction:** Once classified, regime doesn't update mid-session
- **No audit trail:** Hard to trace WHY a decision was made
- **No learning:** Same mistakes repeated across sessions

AGoT solves these by:
1. Evaluating ALL hypotheses in parallel (not just one path)
2. Assigning confidence scores (not binary labels)
3. Maintaining reasoning traces for audit
4. Learning from trade outcomes via feedback loops

---

## 2. Core Concepts

### 2.1 Thought Node

```python
@dataclass
class ThoughtNode:
    id: str                      # unique identifier
    label: str                   # human-readable description
    thought_type: str            # "hypothesis" | "decision" | "observation"
    confidence: float            # 0.05 to 0.95 (never absolute)
    status: ThoughtStatus        # ACTIVE | CONFIRMED | REVISED | REJECTED | BRANCHED | STALE
    evidence: list[Evidence]     # supporting/contradicting evidence
    children: list[str]          # derived thought IDs
    parents: list[str]           # parent thought IDs
    branch_id: str | None        # which hypothesis branch this belongs to
    revision_of: str | None      # if this revises a previous thought
```

### 2.2 Evidence

```python
@dataclass
class Evidence:
    source: str          # e.g., "ADX", "VIX_level", "breadth_pct_above_50dma"
    value: Any           # the raw data value
    supports: bool       # True = supports hypothesis, False = contradicts
    weight: float        # importance (0.0 to 5.0)
    timestamp: str       # when evidence was added
```

### 2.3 Thought Edge

```python
@dataclass
class ThoughtEdge:
    from_id: str         # parent thought
    to_id: str           # child thought
    reasoning: str       # description of the reasoning step
    weight: float        # connection strength
    edge_type: str       # "derivation" | "branch" | "revision" | "contradiction"
```

### 2.4 Status Lifecycle

```
    ACTIVE ──────▶ CONFIRMED (selected as best)
       │
       ├──────────▶ REVISED (new evidence contradicts)
       │
       ├──────────▶ REJECTED (disproven)
       │
       ├──────────▶ BRANCHED (spawned alternatives)
       │
       └──────────▶ STALE (superseded by newer data)
```

---

## 3. Using AGoT in StockMinded

### 3.1 Running the AGoT Dashboard

```bash
# Full AGoT-enhanced dashboard
python main.py agot

# Quick validation (no data fetch)
python main.py agot-test
```

### 3.2 Programmatic Usage

```python
from intelligence.agot_integration import AGoTPipeline

# Initialize pipeline
pipeline = AGoTPipeline(
    config=cfg,
    use_adaptive_regime=True,    # multi-hypothesis regime
    use_signal_ensemble=True,    # weighted signal voting
    use_feedback_loop=True,      # learn from history
)

# Run
result = pipeline.run_dashboard()

# Access results
print(f"Regime: {result.regime_snapshot.regime.value}")
print(f"AGoT Confidence: {result.regime_agot.primary_confidence:.1%}")
print(f"Ambiguity: {result.regime_agot.ambiguity_score:.1%}")
print(f"Alternatives: {result.regime_agot.alternatives}")
print(f"Final Bias: {result.final_bias} ({result.final_confidence:.1%})")
print(f"Risk: {result.risk_adjustment}")
print(f"Corrections: {result.corrections}")
```

### 3.3 Output Structure

```python
AGoTDashboardResult:
├── regime_snapshot        # Traditional RegimeSnapshot (backward compatible)
├── flow_snapshot          # Traditional FlowSnapshot
├── structure_plan         # Traditional StructurePlan
├── long_leaders           # list[StockRank]
├── short_laggards         # list[StockRank]
├── regime_agot            # AdaptiveRegimeResult (NEW)
│   ├── primary: Regime
│   ├── primary_confidence: float
│   ├── alternatives: list[{regime, confidence, evidence_count}]
│   ├── ambiguity_score: float
│   └── evidence_breakdown: dict
├── ensemble_result        # EnsembleResult (NEW)
│   ├── overall_bias: str
│   ├── confidence: float
│   ├── agreement_score: float
│   └── signal_contributions: dict
├── corrections            # list[{type, target, action, reason}]
├── final_bias             # str (LONG/SHORT/NEUTRAL)
├── final_confidence       # float (0.0–1.0)
├── risk_adjustment        # str (NORMAL/REDUCE_SIZE/SKIP)
├── recommendation         # str (human-readable)
└── compute_time_ms        # float
```

---

## 4. Adaptive Regime Classification

### 4.1 Multi-Hypothesis Evaluation

Instead of a single rule stack, the adaptive classifier evaluates ALL 6 regimes simultaneously:

```
Observation (trend=+4, ADX=28, VIX=14, breadth=65%)
    │
    ├── Hypothesis: TREND_UP      (conf: 0.72) ← BEST
    ├── Hypothesis: TREND_DOWN    (conf: 0.15)
    ├── Hypothesis: RANGE_LOW_VOL (conf: 0.35)
    ├── Hypothesis: RANGE_HIGH_VOL(conf: 0.20)
    ├── Hypothesis: VOL_EXPANSION (conf: 0.10)
    └── Hypothesis: VOL_CONTRACTION(conf: 0.25)
```

### 4.2 Evidence Weights per Regime

| Indicator | TREND_UP | TREND_DOWN | RANGE_LOW | RANGE_HIGH | VOL_EXP | VOL_CONT |
|-----------|----------|------------|-----------|------------|---------|----------|
| trend_score | 3.0 | 3.0 | 1.5 | 1.0 | 0.5 | — |
| ADX | 2.5 | 2.5 | 2.0 | 1.5 | — | 1.5 |
| VIX level | 1.0 | 1.0 | 2.5 | 2.0 | 2.0 | 2.0 |
| VIX change | — | — | — | — | 3.0 | 2.5 |
| Breadth | 2.0 | 2.0 | 1.0 | 1.5 | 1.0 | 0.5 |
| EMA alignment | 2.0 | 2.0 | — | — | — | — |
| Realized vol | — | — | 2.0 | 2.0 | — | — |

### 4.3 Scoring Functions

Each indicator produces a score from −1.0 to +1.0 for each regime:

```python
# Example: trend_score scoring for TREND_UP
if trend >= 4:   score = 1.0   # strongly supports
elif trend >= 3: score = 0.6   # moderately supports
elif trend >= 2: score = 0.2   # weakly supports
elif trend <= 0: score = -0.5  # contradicts
else:            score = 0.0   # neutral
```

### 4.4 Ambiguity Detection

```python
# Ambiguity = 1 - (gap between top 2 hypotheses × 5)
gap = top_1.confidence - top_2.confidence
ambiguity = max(0.0, 1.0 - (gap * 5))

# gap = 0.20 → ambiguity = 0.0 (clear winner)
# gap = 0.05 → ambiguity = 0.75 (highly ambiguous)
# gap = 0.00 → ambiguity = 1.0  (perfect tie)
```

**When ambiguity is high:**
- `risk_adjustment` becomes "REDUCE_SIZE" or "SKIP"
- Telegram alert includes alternative hypotheses
- Dashboard UI highlights ambiguity warning

---

## 5. Signal Ensemble

### 5.1 Architecture

```
              Market Direction Assessment
                      │
        ┌─────────────┼─────────────┐
        │             │             │
   LONG Bias    SHORT Bias      NEUTRAL
        │             │             │
   ┌────┴────┐   ┌────┴────┐   ┌────┴────┐
   │ Regime  │   │ Regime  │   │ Regime  │
   │ Flow    │   │ Flow    │   │ Flow    │
   │ Lead.   │   │ Lead.   │   │ Lead.   │
   │ VIX     │   │ VIX     │   │ VIX     │
   │ Breadth │   │ Breadth │   │ Breadth │
   └─────────┘   └─────────┘   └─────────┘
        │             │             │
        └─────────────┼─────────────┘
                      ▼
              Best Hypothesis
              (confidence-weighted)
```

### 5.2 Signal Weights

| Signal | Weight | Rationale |
|--------|--------|-----------|
| **Regime** | 2.5 | Market regime sets the tone for everything |
| **Flow Bias** | 2.0 | Smart money direction is highly predictive |
| **VIX** | 1.5 | Volatility is a critical risk filter |
| **Leadership** | 1.5 | Quality of stock selection matters |
| **Structure** | 1.0 | Trade structure fit is secondary |
| **Breadth** | 1.0 | Market internals are confirming, not leading |

### 5.3 Agreement Score

```python
def _calculate_agreement(biases, contributions):
    # Raw agreement: majority direction / total signals
    max_count = max(long_count, short_count, neutral_count)
    raw_agreement = max_count / total
    
    # Weighted agreement: confidence-weighted for majority direction
    weighted_agreement = sum(
        c["confidence"] * c["weight"]
        for sig, c in contributions.items()
        if c["bias"] == majority_direction
    )
    weighted_ratio = weighted_agreement / total_weight
    
    # Blend: 40% raw + 60% weighted
    return 0.4 * raw_agreement + 0.6 * weighted_ratio
```

### 5.4 Risk Adjustment

| Agreement | Confidence | Adjustment |
|-----------|------------|------------|
| ≥ 0.70 | ≥ 0.60 | `NORMAL` — full size allowed |
| ≥ 0.50 | ≥ 0.45 | `REDUCE_SIZE` — halve position |
| < 0.50 | < 0.45 | `SKIP` — no new trades |

---

## 6. Feedback Loop

### 6.1 How It Works

```
Trade Closes → Journal Records Outcome
        │
        ▼
Feedback Loop Analyzes (lookback 30d)
        │
        ├── Win rate by regime
        ├── Confidence calibration
        ├── Signal performance
        │
        ▼
Generate Corrections
        │
        ├── BLOCK: regime/signal with <40% win rate
        ├── REDUCE_SIZE: regime with <50% win rate
        └── ADJUST_THRESHOLD: overconfident bucket
        │
        ▼
Apply to Future Decisions
```

### 6.2 Regime Accuracy Tracking

```python
# After 30 days of trades:
regime_accuracy = {
    "TREND_UP": {
        "win_rate": 0.62,
        "count": 15,
        "avg_pnl": 3200.0,
        "verdict": "STRONG"
    },
    "RANGE_HIGH_VOL": {
        "win_rate": 0.33,
        "count": 12,
        "avg_pnl": -1800.0,
        "verdict": "AVOID"
    }
}

# Generates correction:
correction = {
    "type": "REGIME_AVOID",
    "target": "RANGE_HIGH_VOL",
    "action": "BLOCK",
    "reason": "RANGE_HIGH_VOL trades have 33% win rate (n=12)",
    "priority": "HIGH"
}
```

### 6.3 Confidence Calibration

Checks if confidence scores are honest:

| Bucket | Expected Win Rate | Actual Win Rate | Verdict |
|--------|-------------------|-----------------|---------|
| LOW (30-50%) | 40% | 38% | ✅ Calibrated |
| MED (50-70%) | 60% | 52% | ⚠️ Overestimated |
| HIGH (70-90%) | 80% | 65% | ❌ Severely overestimated |

**Correction:** When high-confidence trades underperform, the system raises the confidence threshold for HIGH labels.

### 6.4 Correction Rules

```python
# Generated by feedback loop
corrections = [
    {
        "type": "REGIME_AVOID",
        "target": "RANGE_HIGH_VOL",
        "action": "BLOCK",
        "reason": "33% win rate (n=12)",
        "priority": "HIGH"
    },
    {
        "type": "CONFIDENCE_OVERESTIMATE",
        "target": "high_70-90",
        "action": "ADJUST_THRESHOLD",
        "reason": "HIGH confidence wins 65% vs expected 80%",
        "priority": "MEDIUM"
    },
    {
        "type": "SIGNAL_BLOCK",
        "target": "bias_NEUTRAL",
        "action": "BLOCK",
        "reason": "NEUTRAL bias has 35% win rate (n=8)",
        "priority": "HIGH"
    }
]
```

---

## 7. Learner Module

### 7.1 Statistical Rule Extraction

The `intelligence/learner.py` module uses **Wilson score intervals** to identify losing segments:

```python
def _wilson_lower_bound(pos, n, confidence=0.95):
    """Lower bound of Wilson score interval for binomial proportion."""
    z = 1.96  # 95% confidence
    phat = pos / n
    return (phat + z²/(2n) - z*sqrt((phat*(1-phat) + z²/(4n))/n)) / (1 + z²/n)
```

### 7.2 Dimensions Analyzed

| Dimension | Buckets |
|-----------|---------|
| Confidence | HIGH, MEDIUM, LOW |
| Source Regime | TREND_UP, TREND_DOWN, RANGE_LOW_VOL, etc. |
| Direction | LONG, SHORT |
| Entry Hour | OPEN (≤10), MID (11-13), CLOSE (≥14) |
| Sector | Energy, IT, Banks, etc. |
| Flow Bias | LONG, SHORT, NEUTRAL |
| VIX | LOW (<14), MED (14-20), HIGH (>20) |
| ADX | WEAK (<20), MID (20-30), STRONG (>30) |

### 7.3 Rule Generation

```python
# If a segment has:
# - n ≥ 5 trades
# - ≥ 3 distinct trading days
# - Wilson lower bound < 0.40

rule = {
    "id": "a3f8c2d1",
    "segment": {"dim": "source_regime", "value": "RANGE_HIGH_VOL"},
    "action": "BLOCK",
    "evidence": "RANGE_HIGH_VOL trades lost 67% (n=12) over 30d",
    "sample_size": 12,
    "win_rate": 0.333,
    "wilson_low": 0.138,
    "expires_at": "2026-07-14T..."
}
```

### 7.4 Applying Learned Filters

```python
from intelligence.learner import apply_learnized_filter

action, evidence = apply_learnized_filter(alert, rules)

if action == "BLOCK":
    # Skip this trade entirely
    journal.log_skipped_trade(..., skip_reason=evidence)
elif action == "DOWNGRADE":
    # Reduce confidence
    alert["confidence"] = "LOW"
```

---

## 8. Reasoning Trace

### 8.1 Full Trace Export

```python
graph = ThoughtGraph("regime_classification")
# ... add thoughts and evidence ...

trace = graph.get_reasoning_trace()

# trace contains:
{
    "name": "regime_classification",
    "session_id": "abc12345",
    "node_count": 7,
    "edge_count": 6,
    "branch_count": 6,
    "nodes": {
        "node_001": {
            "label": "Market State Observation",
            "confidence": 0.95,
            "status": "BRANCHED",
            "evidence": [...],
            "children": ["node_002", "node_003", ...],
        },
        "node_002": {
            "label": "Hypothesis: TREND_UP",
            "confidence": 0.72,
            "status": "CONFIRMED",
            "evidence": [
                {"source": "trend_score", "value": 4, "supports": true, "weight": 3.0},
                {"source": "ADX", "value": 28, "supports": true, "weight": 2.5},
                {"source": "breadth", "value": 65, "supports": true, "weight": 2.0},
            ],
        },
        # ... more nodes
    },
    "edges": [...],
    "aggregated_confidence": 0.68,
    "best_hypothesis": {...}
}
```

### 8.2 Human-Readable Summary

```python
print(graph.summary())

# Output:
# ThoughtGraph: regime_classification (session: abc12345)
#   Nodes: 7 | Edges: 6 | Branches: 6
#   ● [0.95] Market State Observation
#   ● [0.72] Hypothesis: TREND_UP
#   ○ [0.35] Hypothesis: RANGE_LOW_VOL
#   ○ [0.20] Hypothesis: RANGE_HIGH_VOL
#   ✗ [0.15] Hypothesis: TREND_DOWN
#   ○ [0.10] Hypothesis: VOL_EXPANSION
#   ○ [0.25] Hypothesis: VOL_CONTRACTION
```

---

## 9. Integration with Deterministic Pipeline

### 9.1 Coexistence Strategy

AGoT runs **alongside** the deterministic pipeline, not replacing it:

```python
# main.py
def run_dashboard(cfg):
    # Deterministic pipeline (always runs)
    regime_snap = regime_mod.classify("NIFTY", stock_data)
    flow_snap = flows_mod.snapshot(sector_data)
    # ... etc
    
def run_agot_dashboard(cfg):
    # AGoT-enhanced pipeline (opt-in)
    pipeline = AGoTPipeline(cfg, use_adaptive_regime=True, ...)
    result = pipeline.run_dashboard()
    # result.regime_snapshot is backward-compatible RegimeSnapshot
```

### 9.2 Backward Compatibility

`AdaptiveRegimeResult` contains a `.snapshot` field that is a standard `RegimeSnapshot`:

```python
# This works identically whether using deterministic or AGoT regime:
regime = regime_result.regime          # Regime enum
trend = regime_result.trend_score      # int
vix = regime_result.vix                # float
```

### 9.3 Gradual Adoption

Teams can adopt AGoT incrementally:

1. **Phase 1:** Use `run_dashboard()` (deterministic) — current state
2. **Phase 2:** Use `run_agot_dashboard()` with `use_adaptive_regime=True` only
3. **Phase 3:** Enable `use_signal_ensemble=True`
4. **Phase 4:** Enable `use_feedback_loop=True`
5. **Phase 5:** Full AGoT with all layers active

---

## 10. Performance Characteristics

| Metric | Deterministic | AGoT-Enhanced |
|--------|--------------|---------------|
| Compute time | ~2-5s | ~5-15s |
| Data fetches | Same | Same (shared) |
| Memory | Low | Moderate (graph nodes) |
| Debuggability | Rule-based trace | Full reasoning trace |
| Ambiguity handling | None | Explicit scoring |
| Self-correction | None | Feedback loop |

---

## 11. Best Practices

### 11.1 When to Use AGoT

- **Ambiguous market conditions** — when regime isn't clear-cut
- **High-stakes decisions** — when you need audit trails
- **Strategy research** — when exploring which signals work
- **Post-mortem analysis** — when tracing why a trade was taken

### 11.2 When to Stick with Deterministic

- **Fast iteration** — when testing new signal ideas
- **Simple markets** — when regime is obvious (strong trend)
- **Resource-constrained** — when compute time matters
- **Known-good conditions** — when deterministic has proven track record

### 11.3 Interpreting AGoT Output

| AGoT Metric | Meaning | Action |
|-------------|---------|--------|
| `primary_confidence > 0.70` | Strong conviction | Proceed with normal sizing |
| `primary_confidence 0.50-0.70` | Moderate conviction | Consider reducing size |
| `primary_confidence < 0.50` | Low conviction | Skip or minimal size |
| `ambiguity_score > 0.60` | Market is unclear | Wait for clarity |
| `agreement_score > 0.75` | All signals agree | Full conviction |
| `agreement_score < 0.40` | Signals disagree | Reduce size or skip |
| `corrections` present | Historical pattern failed | Apply correction |
