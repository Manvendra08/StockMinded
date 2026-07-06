# StockMinded — System Architecture

> **Decision system for Indian equity & derivatives markets**
> Capital target: ₹70L · 5%/month · max −6% monthly / −2% daily drawdown

---

## 1. High-Level Overview

StockMinded is a **modular monolith** with a layered architecture that answers four questions every morning before 9:15 IST:

1. **What regime are we in?** (Trend Up/Down, Range, Volatility Expansion/Contraction)
2. **Where is the money flowing?** (FII/DII, sector rotation, PCR/max-pain)
3. **Who are the leaders?** (Relative strength ranking, A-grade stocks)
4. **What structure should we trade?** (Regime → primary/secondary trade structure)

The system produces a unified `CombinedVerdict` and optionally auto-executes paper trades via the dashboard.

---

## 2. Architecture Layers

```
┌─────────────────────────────────────────────────────────────────┐
│                    PRESENTATION LAYER                            │
│  dashboard/index.html · dashboard/paper.html · Flask UI         │
├─────────────────────────────────────────────────────────────────┤
│                   ORCHESTRATION LAYER                            │
│  dashboard/server.py (Flask API) · dashboard/paper_trader.py    │
│  main.py (CLI: dashboard | agot | schedule | health)            │
├─────────────────────────────────────────────────────────────────┤
│                     INTELLIGENCE LAYER                           │
│  intelligence/thought_graph.py · adaptive_regime.py              │
│  signal_ensemble.py · feedback_loop.py · learner.py             │
├─────────────────────────────────────────────────────────────────┤
│                      SIGNAL LAYER                                │
│  signals/regime.py · flows.py · leadership.py · verdict.py      │
│  signals/options.py · option_strategy.py · structure_map.py      │
│  signals/timing.py                                              │
├─────────────────────────────────────────────────────────────────┤
│                   RISK & GUARDRAILS LAYER                        │
│  risk/guardrails.py · risk/sizing.py                            │
├─────────────────────────────────────────────────────────────────┤
│                       DATA FEED LAYER                            │
│  data/feed.py · data/ai_scraper.py · data/shoonya_fetcher.py    │
│  data/cache/ohlc/ · data/journal.sqlite · data/iv_history.sqlite│
├─────────────────────────────────────────────────────────────────┤
│                     OPERATIONS LAYER                             │
│  ops/journal.py (SQLite audit) · ops/alerts.py (Telegram)       │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. Reasoning Graph (AGoT)

The **Adaptive Graph of Thoughts** (AGoT) system adds a graph-based reasoning layer on top of the deterministic signal pipeline.

### 3.1 Thought Graph Structure

```
                    ┌──────────────────┐
                    │   OBSERVATION    │
                    │ (Market State)   │
                    │  confidence: 0.95│
                    └────────┬─────────┘
                             │
          ┌──────────────────┼──────────────────┐
          │                  │                  │
    ┌─────▼─────┐    ┌──────▼──────┐    ┌──────▼──────┐
    │ TREND_UP  │    │RANGE_LOW_VOL│    │RANGE_HIGH_VOL│
    │ conf: 0.72│    │ conf: 0.58  │    │ conf: 0.45  │
    │ evidence: │    │ evidence:   │    │ evidence:   │
    │ trend=+4  │    │ adx=15      │    │ adx=18      │
    │ adx=28    │    │ vix=12      │    │ vix=17      │
    │ breadth=65│    │             │    │             │
    └───────────┘    └─────────────┘    └─────────────┘
          │
          ▼ (selected best)
    ┌──────────────────┐
    │   CONFIRMED      │
    │   TREND_UP       │
    │   conf: 0.72     │
    └──────────────────┘
```

### 3.2 Key Components

| Component | File | Purpose |
|-----------|------|---------|
| **ThoughtNode** | `thought_graph.py` | Hypothesis with confidence, evidence list, status |
| **ThoughtEdge** | `thought_graph.py` | Reasoning path (derivation/branch/revision) |
| **ThoughtGraph** | `thought_graph.py` | DAG with multi-branch reasoning, select_best() |
| **AdaptiveRegimeClassifier** | `adaptive_regime.py` | Evaluates ALL 6 regimes in parallel with evidence scoring |
| **SignalEnsemble** | `signal_ensemble.py` | Aggregates regime/flow/leadership/VIX/breadth into unified bias |
| **FeedbackLoop** | `feedback_loop.py` | Analyzes closed trades → generates correction rules |
| **Learner** | `learner.py` | Statistical rule extraction (Wilson bounds, segment analysis) |

### 3.3 Confidence Update Formula

```
confidence = base + (net_evidence × 0.4 × evidence_influence)
```

Where:
- `base = 0.5` (prior)
- `net_evidence = (support_sum - contradict_sum) / total_weight`
- `evidence_influence = min(total_weight / 5.0, 1.0)` (saturates at 5 pieces)
- Clamped to `[0.05, 0.95]` to avoid absolute certainty

---

## 4. Signal Pipeline

### 4.1 Regime Classification (`signals/regime.py`)

| Indicator | Computation | Weight |
|-----------|-------------|--------|
| **Trend Score** | EMA20/50/200 alignment + slope comparison (-10 to +10) | 3.0 |
| **ADX** | Directional Movement Index (14-period smoothed) | 2.5 |
| **VIX** | India VIX level + 5d change + 1-year percentile rank | 1.0–3.0 |
| **Breadth** | % stocks above 50 DMA | 2.0 |

**Regime Decision Tree:**
```
VIX +25% in 5d (2-session confirmed) → VOL_EXPANSION
VIX −20% and VIX < 14 → VOL_CONTRACTION
trend ≤ −3 and breadth ≥ 55% → RANGE (mixed tape)
trend ≥ 3 and breadth ≤ 40% → RANGE (mixed tape)
ADX ≥ 20 and trend ≥ 4 and breadth ≥ 50% → TREND_UP
ADX ≥ 20 and trend ≤ −4 and breadth ≤ 45% → TREND_DOWN
ADX < 20 and VIX < 14 → RANGE_LOW_VOL
ADX < 20 and VIX ≥ 16 → RANGE_HIGH_VOL
```

### 4.2 Flow Analysis (`signals/flows.py`)

**Smart Money Bias** — weighted signal scoring:

| Signal | Weight | Threshold |
|--------|--------|-----------|
| FII Index Futures 5D | 2.0 | >₹1000Cr LONG / <−₹1000Cr SHORT |
| PCR OI | 1.5 | >1.2 bull / <0.85 bear |
| FII Long-Short Ratio | 1.5 | >1.25 bull / <0.75 bear |
| FII Cash Net 5D | 1.0 | >₹500Cr LONG / <−₹500Cr SHORT |
| FII Stock Futures 5D | 1.0 | >₹2000Cr LONG / <−₹2000Cr SHORT |
| AI Sentiment | 1.0 | Confidence-scaled (HIGH=1.0, MED=0.6, LOW=0.3) |

**Conviction threshold:** weighted_score ≥ 2.0 → LONG, ≤ −2.0 → SHORT

### 4.3 Leadership Ranking (`signals/leadership.py`)

**Symmetric Quintile Scoring (Q1–Q5):**

| Quintile | Long Criteria | Short Criteria |
|----------|--------------|----------------|
| Q5 (A-grade) | RS₁₀>15, RS₂₀>5, 0%≤vs20≤6%, above 50DMA, RVOL>1.2 | RS₁₀<−15, RS₂₀<−5, −6%≤vs20≤0%, below 50DMA, RVOL>1.2 |
| Q4 | RS₁₀>10, RS₂₀>0, −1%≤vs20≤8%, above 50DMA, RVOL>1.0 | RS₁₀<−10, RS₂₀<0, −8%≤vs20≤1%, below 50DMA, RVOL>1.0 |
| Q3 | RS₁₀>5, −2%≤vs20≤10%, above 50DMA | RS₁₀<−5, −10%≤vs20≤2%, below 50DMA |
| Q2 | RS₁₀>0, above 50DMA | RS₁₀<0, below 50DMA |
| Q1 | Default | Default |

### 4.4 Verdict Engine (`signals/verdict.py`)

**Dual Verdict System:**

1. **StockVerdict** — Directional stock picking
   - Actions: `LONG_ONLY`, `SHORT_ONLY`, `LONG_AND_SHORT`, `WAIT`
   - Tone: `bull`, `bear`, `mixed`
   - Confidence: `HIGH` (80), `MEDIUM` (50), `LOW` (20)

2. **NiftyVerdict** — Index options selling
   - Actions: `OPTION_SELL_DEFINED_RISK`, `NAKED_OPTION_SELL`, `WAIT`
   - Requires: fresh option chain, VIX < 25, IV rank ≥ 40 for naked sells

---

## 5. Design Trade-offs

### 5.1 Storage: JSON vs SQLite

| Aspect | JSON (`paper_trades.json`) | SQLite (`journal.sqlite`) |
|--------|---------------------------|---------------------------|
| **Use** | Active paper trades | Historical audit log |
| **Concurrency** | File locks (msvcrt) with 20 retries | WAL mode |
| **Crash Safety** | Atomic write (tmp → bak → rename) | ACID transactions |
| **Scalability** | Limited (~100s of trades) | Unlimited |
| **Trade-off** | Simple, human-readable | Robust, queryable |

**Rationale:** JSON for active state (frequent read/write, small dataset), SQLite for audit (append-only, large dataset).

### 5.2 Data Freshness: Caching Strategy

```
data/cache/ohlc/{symbol}_{date}.pkl
```

- **Pros:** Eliminates yfinance rate limits, fast subsequent reads
- **Cons:** Day-scoped invalidation (old files accumulate), stale at market open
- **Mitigation:** `data_freshness` status (LIVE/STALE/OLD/MISSING) in API responses

### 5.3 Options Pricing: Synthetic Fallback

When NSE option chain returns zero LTPs:

```python
if ce_ltp <= 0:
    ce_ltp = _bs_price(spot, strike, t, r=0.065, sigma=ce_iv or 0.15, kind="CE")
    ce_synthetic = True
```

**Trade-off:** Enables signals during data gaps but skews risk if >50% synthetic.

### 5.4 AGoT Complexity vs Deterministic Rules

| Approach | Strengths | Weaknesses |
|----------|-----------|------------|
| **Deterministic** (signals/) | Predictable, debuggable, fast | Rigid, no ambiguity handling |
| **AGoT** (intelligence/) | Handles ambiguity, self-correcting, auditable | Complex, slower, harder to debug |

**Resolution:** Both coexist. AGoT is opt-in (`python main.py agot`), deterministic is default.

---

## 6. Entry Points

| Command | Description |
|---------|-------------|
| `python main.py dashboard` | Run 4-signal dashboard, send Telegram alert |
| `python main.py agot` | Run AGoT-enhanced dashboard with multi-hypothesis regime |
| `python main.py agot-test` | Validate AGoT components without data fetch |
| `python main.py schedule` | APScheduler loop (cron at configured IST times) |
| `python main.py health` | Check data connectivity (NIFTY, VIX, PCR, FII/DII) |
| `python dashboard/server.py` | Launch Flask web server on port 5050 |

---

## 7. Data Flow Diagram

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│  yfinance   │     │  NSE Option │     │  ScrapeGraph│
│  OHLC data  │     │    Chain    │     │     AI      │
└──────┬──────┘     └──────┬──────┘     └──────┬──────┘
       │                   │                   │
       ▼                   ▼                   ▼
┌─────────────────────────────────────────────────────┐
│                    data/feed.py                      │
│  ohlc_cached() · option_chain() · fii_dii_cash()    │
│  get_pcr_max_pain_cached() · india_vix()             │
└──────────────────────────┬──────────────────────────┘
                           │
       ┌───────────────────┼───────────────────┐
       ▼                   ▼                   ▼
┌─────────────┐    ┌─────────────┐    ┌─────────────┐
│  regime.py  │    │   flows.py  │    │leadership.py│
│  (classify) │    │ (snapshot)  │    │  (rank)     │
└──────┬──────┘    └──────┬──────┘    └──────┬──────┘
       │                  │                   │
       └──────────────────┼───────────────────┘
                          ▼
              ┌─────────────────────┐
              │    verdict.py       │
              │ build_trade_verdict │
              │ → CombinedVerdict   │
              └──────────┬──────────┘
                         │
              ┌──────────┼──────────┐
              ▼          ▼          ▼
        ┌──────────┐ ┌───────┐ ┌────────┐
        │structure │ │ risk/ │ │ ops/   │
        │ _map.py  │ │guards │ │alerts  │
        └──────────┘ └───────┘ └────────┘
```

---

## 8. Module Dependencies

```
main.py
├── config/loader.py
├── data/feed.py
├── signals/regime.py → data/feed.py
├── signals/flows.py → data/feed.py, data/ai_scraper.py
├── signals/leadership.py → data/feed.py
├── signals/structure_map.py → signals/regime.py
├── signals/verdict.py → (consumes regime + flows dicts)
├── ops/alerts.py
├── ops/journal.py
└── intelligence/agot_integration.py
    ├── intelligence/adaptive_regime.py → thought_graph.py
    ├── intelligence/signal_ensemble.py → thought_graph.py
    └── intelligence/feedback_loop.py → ops/journal.py
```

---

## 9. Configuration

**Primary:** `config/config.yaml`
- `account.capital`: Total account size (₹)
- `risk.*`: Per-trade %, daily/monthly stops, margin cap
- `schedule_ist.*`: Cron times for morning dashboard
- `nifty_options.*` / `banknifty_options.*`: Strategy parameters

**Environment:** `.env`
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `GOOGLE_API_KEY` (for AI sentiment)
- `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN` (optional broker)

---

## 10. Testing Strategy

| Type | Location | Pattern |
|------|----------|---------|
| Unit | `tests/unit/` | `test_*.py` |
| Integration | `tests/integration/` | `test_*.py` |
| Fixtures | `tests/conftest.py` | `:memory:` SQLite, mock configs |

**Key patterns:**
- Time patching: `patch("dashboard.paper_trader._now_ist")`
- Database isolation: `Journal(":memory:")`
- Market data mocking: `@patch("data.feed.ohlc_cached")`
