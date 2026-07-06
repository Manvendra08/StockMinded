# StockMinded — Functional Specification

> Comprehensive specification of system capabilities, interfaces, and behaviors.

**Version:** 2.0
**Date:** 2026-06-30
**Status:** Active (Phase 1–2 operational, Phase 3 planned)

---

## 1. System Purpose

StockMinded is a **decision-support and paper-trading system** for Indian equity and derivatives markets. It processes market data through four independent signal engines to produce actionable trade recommendations, and optionally simulates trade execution with full P&L tracking.

### 1.1 Core Objectives

1. **Answer 4 questions every morning before 9:15 IST:**
   - What market regime are we in?
   - Where is institutional money flowing?
   - Which stocks show relative strength leadership?
   - What trade structure fits the current environment?

2. **Deliver alerts via Telegram** with sufficient detail for manual execution.

3. **Simulate trades** in a paper trading environment with realistic entry/exit logic.

4. **Learn from outcomes** via feedback loops that adjust future decision weights.

### 1.2 Non-Objectives

- **Not a live trading system** (Phase 1–2 are alerts-only and paper trading)
- **Not a high-frequency system** (daily/positional timeframe)
- **Not a multi-user platform** (single-operator dashboard)
- **Not a backtesting framework** (forward-testing via paper trades)

---

## 2. Functional Requirements

### 2.1 Signal Generation (FR-SIG)

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-SIG-01 | Classify market into 1 of 6 regimes | P0 | ✅ Implemented |
| FR-SIG-02 | Compute FII/DII 5-day net flows | P0 | ✅ Implemented |
| FR-SIG-03 | Compute sector relative strength | P0 | ✅ Implemented |
| FR-SIG-04 | Compute PCR (OI and Volume) | P0 | ✅ Implemented |
| FR-SIG-05 | Compute max-pain from option chain | P0 | ✅ Implemented |
| FR-SIG-06 | Rank universe stocks by RS slope | P0 | ✅ Implemented |
| FR-SIG-07 | Produce A-grade long/short lists | P0 | ✅ Implemented |
| FR-SIG-08 | Map regime to trade structure | P0 | ✅ Implemented |
| FR-SIG-09 | Build combined verdict (stock + nifty) | P0 | ✅ Implemented |
| FR-SIG-10 | AI sentiment analysis via ScrapeGraph | P1 | ✅ Implemented |
| FR-SIG-11 | FII derivatives breakdown | P1 | ✅ Implemented |
| FR-SIG-12 | Trendlyne KPI integration | P2 | ✅ Implemented |

### 2.2 Options Engine (FR-OPT)

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-OPT-01 | Iron Condor structure resolution | P0 | ✅ Implemented |
| FR-OPT-02 | Bull Put Spread structure | P0 | ✅ Implemented |
| FR-OPT-03 | Bear Call Spread structure | P0 | ✅ Implemented |
| FR-OPT-04 | Naked option sell (with IV rank gate) | P1 | ✅ Implemented |
| FR-OPT-05 | Entry window validation | P0 | ✅ Implemented |
| FR-OPT-06 | Premium walking (min/max bounds) | P1 | ✅ Implemented |
| FR-OPT-07 | Synthetic Black-Scholes pricing | P1 | ✅ Implemented |
| FR-OPT-08 | IV rank calculation (1-year rolling) | P1 | ✅ Implemented |
| FR-OPT-09 | Expiry day management | P0 | ✅ Implemented |
| FR-OPT-10 | Smart exits (VIX spike, delta, trailing) | P1 | ✅ Implemented |

### 2.3 Risk Management (FR-RSK)

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-RSK-01 | Daily drawdown stop (−2%) | P0 | ✅ Implemented |
| FR-RSK-02 | Monthly drawdown stop (−6%) | P0 | ✅ Implemented |
| FR-RSK-03 | Concurrent risk cap (3%) | P0 | ✅ Implemented |
| FR-RSK-04 | Margin utilization cap (60%) | P0 | ✅ Implemented |
| FR-RSK-05 | Correlation check with open positions | P1 | ✅ Implemented |
| FR-RSK-06 | Directional position sizing | P0 | ✅ Implemented |
| FR-RSK-07 | Option structure sizing | P0 | ✅ Implemented |
| FR-RSK-08 | Per-trade risk limit (0.75%) | P0 | ✅ Implemented |

### 2.4 Paper Trading (FR-PT)

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-PT-01 | Auto-enter from alerts | P0 | ✅ Implemented |
| FR-PT-02 | Stop loss monitoring | P0 | ✅ Implemented |
| FR-PT-03 | Target price monitoring | P0 | ✅ Implemented |
| FR-PT-04 | Trailing stop loss | P1 | ✅ Implemented |
| FR-PT-05 | EOD auto-close | P1 | ✅ Implemented |
| FR-PT-06 | EOD summary generation | P1 | ✅ Implemented |
| FR-PT-07 | Options trade management | P0 | ✅ Implemented |
| FR-PT-08 | Smart exit engine | P1 | ✅ Implemented |
| FR-PT-09 | F&O lot size detection | P0 | ✅ Implemented |
| FR-PT-10 | Futures expiry tracking | P1 | ✅ Implemented |

### 2.5 Intelligence Layer (FR-INT)

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-INT-01 | Thought graph reasoning engine | P1 | ✅ Implemented |
| FR-INT-02 | Adaptive regime classification | P1 | ✅ Implemented |
| FR-INT-03 | Signal ensemble voting | P1 | ✅ Implemented |
| FR-INT-04 | Feedback loop from outcomes | P1 | ✅ Implemented |
| FR-INT-05 | Statistical learner (Wilson bounds) | P1 | ✅ Implemented |
| FR-INT-06 | Regime accuracy tracking | P2 | ✅ Implemented |
| FR-INT-07 | Confidence calibration | P2 | ✅ Implemented |

### 2.6 Dashboard & UI (FR-UI)

| ID | Requirement | Priority | Status |
|----|-------------|----------|--------|
| FR-UI-01 | Signal summary display | P0 | ✅ Implemented |
| FR-UI-02 | Active trades table | P0 | ✅ Implemented |
| FR-UI-03 | Trade history | P0 | ✅ Implemented |
| FR-UI-04 | Settings panel | P1 | ✅ Implemented |
| FR-UI-05 | Verdict trace viewer | P2 | ✅ Implemented |
| FR-UI-06 | Skip reasons display | P1 | ✅ Implemented |
| FR-UI-07 | Brain Verdict Review (LLM) | P2 | ✅ Implemented |
| FR-UI-08 | Paper trader control panel | P1 | ✅ Implemented |

---

## 3. Non-Functional Requirements

### 3.1 Performance

| Metric | Target | Current |
|--------|--------|---------|
| Signal generation time | < 30 seconds | ~10-20s (with cache) |
| Dashboard page load | < 3 seconds | ~1-2s |
| LTP fetch (single) | < 2 seconds | ~0.5-1s |
| Paper trade check cycle | < 60 seconds | ~30s |
| AGoT dashboard | < 60 seconds | ~15-30s |

### 3.2 Reliability

| Requirement | Implementation |
|-------------|----------------|
| Data fetch failure | Graceful degradation (skip symbol, log error) |
| Database corruption | Atomic writes + backup rotation |
| Network timeout | Retry with exponential backoff |
| Process crash | APScheduler restart, state recovery from JSON |

### 3.3 Security

| Requirement | Implementation |
|-------------|----------------|
| Secret management | `.env` file + `${ENV_VAR}` expansion in config |
| Dashboard access | Localhost only (no auth — single-user) |
| Data integrity | SQLite WAL mode, JSON file locks |
| Git hygiene | `.gitignore` excludes `.env`, `*.sqlite`, `paper_trades.json` |

### 3.4 Maintainability

| Requirement | Implementation |
|-------------|----------------|
| Code style | PEP 8, type hints, docstrings |
| Testing | pytest with unit + integration markers |
| Documentation | `.planning/codebase/` + `docs/` |
| Module boundaries | Clean imports, no circular dependencies |

---

## 4. System Interfaces

### 4.1 External APIs

| Interface | Protocol | Auth | Purpose |
|-----------|----------|------|---------|
| Yahoo Finance | HTTPS REST | None | OHLC price data |
| NSE Option Chain | HTTPS REST | None | Option chain, PCR |
| NSE FII/DII | HTTPS RSS | None | Institutional flows |
| ScrapeGraphAI | HTTPS REST | API Key | AI sentiment |
| Google Gemini | HTTPS REST | API Key | AI fallback |
| Telegram Bot | HTTPS REST | Bot Token | Alert delivery |
| Dhan API | HTTPS REST | Client ID + Token | Broker data (optional) |
| Shoonya API | WebSocket/REST | Session Token | LTP, security info |

### 4.2 Internal APIs (Flask)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/signals` | GET | Current signal snapshot |
| `/api/signals/refresh` | POST | Trigger signal recomputation |
| `/api/paper/trades` | GET | List active paper trades |
| `/api/paper/trades` | POST | Manual trade entry |
| `/api/paper/auto-enter` | POST | Auto-enter from alerts |
| `/api/paper/check` | POST | Check SL/TGT for open trades |
| `/api/paper/settings` | GET/PUT | Paper trader settings |
| `/api/paper/eod-summary` | POST | Generate EOD summary |
| `/api/intraday` | GET | Live LTP via Shoonya |
| `/api/journal/trades` | GET | Historical trade log |
| `/api/journal/skips` | GET | Skipped trade reasons |
| `/api/health` | GET | System health check |

### 4.3 CLI Interface

```
Usage:
  python main.py dashboard    Run 4-signal dashboard, send Telegram alert
  python main.py agot         Run AGoT-enhanced dashboard
  python main.py agot-test    Validate AGoT components (no data fetch)
  python main.py schedule     Start APScheduler cron loop
  python main.py health       Check data connectivity

Additional:
  python dashboard/server.py  Start Flask web server on port 5050
```

---

## 5. Data Model

### 5.1 Core Entities

```
RegimeSnapshot
├── regime: Regime (enum)
├── trend_score: int (-10 to +10)
├── vix: float
├── vix_5d_change_pct: float
├── vix_rank: float | None
├── adx: float
├── breadth_pct_above_50dma: float | None
└── notes: str

FlowSnapshot
├── fii_dii_5d_net_cr: dict
├── top_inflow_sectors: list[tuple]
├── top_outflow_sectors: list[tuple]
├── pcr_oi: float | None
├── pcr_vol: float | None
├── max_pain: float | None
├── smart_money_bias: str (LONG|SHORT|NEUTRAL)
├── ai_sentiment: dict | None
└── trendlyne_kpis: dict

CombinedVerdict
├── stock: StockVerdict
│   ├── action: str (LONG_ONLY|SHORT_ONLY|LONG_AND_SHORT|WAIT)
│   ├── confidence: str (HIGH|MEDIUM|LOW)
│   └── can_trade: bool
└── nifty: NiftyVerdict
    ├── action: str (OPTION_SELL_DEFINED_RISK|NAKED_OPTION_SELL|WAIT)
    ├── confidence: str
    └── can_trade: bool

PaperTrade
├── id: int
├── symbol: str
├── direction: str (LONG|SHORT)
├── entry_price: float
├── qty: int
├── sl_price: float
├── tgt_price: float
├── status: str (OPEN|CLOSED)
├── pnl: float | None
└── journal_id: int
```

### 5.2 Storage Formats

| Data | Format | Location |
|------|--------|----------|
| Active paper trades | JSON | `dashboard/paper_trades.json` |
| Historical journal | SQLite | `data/journal.sqlite` |
| IV history | SQLite | `data/iv_history.sqlite` |
| OHLC cache | Pickle | `data/cache/ohlc/{symbol}_{date}.pkl` |
| Option chain cache | JSON | `data/cache/option_chain_{symbol}.json` |
| Configuration | YAML | `config/config.yaml` |
| Environment secrets | dotenv | `.env` |

---

## 6. State Machines

### 6.1 Paper Trade Lifecycle

```
                    ┌──────────┐
                    │  ALERT   │
                    └────┬─────┘
                         │ auto_enter_from_alerts()
                         ▼
               ┌───────────────────┐
               │   RISK GATE       │
               │ (guardrails check)│
               └────────┬──────────┘
                   pass │    fail
                  ┌─────┘    └──────┐
                  ▼                  ▼
          ┌──────────────┐    ┌───────────┐
          │    OPEN       │    │ SKIPPED   │
          │ status=OPEN   │    │ (logged)  │
          └──────┬───────┘    └───────────┘
                 │
        ┌────────┼────────┬─────────────┐
        ▼        ▼        ▼             ▼
   ┌────────┐┌────────┐┌──────────┐┌──────────┐
   │SL HIT  ││TGT HIT ││TRAIL HIT ││EOD CLOSE │
   └───┬────┘└───┬────┘└────┬─────┘└────┬─────┘
       │         │          │            │
       └─────────┴──────────┴────────────┘
                         │
                         ▼
                 ┌──────────────┐
                 │   CLOSED     │
                 │ pnl computed │
                 │ journal sync │
                 └──────────────┘
```

### 6.2 Option Trade Lifecycle

```
     ┌──────────────┐
     │ SETUP        │
     │ (strategy    │
     │  selected)   │
     └──────┬───────┘
            │ resolve_structure()
            ▼
     ┌──────────────┐
     │ RESOLVED     │
     │ (legs +      │
     │  premiums)   │
     └──────┬───────┘
            │ enter_option_structure()
            ▼
     ┌──────────────┐
     │    OPEN      │◄──── grace period (5 min)
     │              │
     └──────┬───────┘
            │
    ┌───────┼────────┬──────────┬──────────┐
    ▼       ▼        ▼          ▼          ▼
┌───────┐┌───────┐┌────────┐┌────────┐┌──────────┐
│VIX    ││DELTA  ││PROFIT  ││STOP    ││EXPIRY    │
│SPIKE  ││BREACH ││TARGET  ││LOSS    ││DAY EXIT  │
└───┬───┘└───┬───┘└───┬────┘└───┬────┘└────┬─────┘
    │        │        │         │           │
    └────────┴────────┴─────────┴───────────┘
                         │
                         ▼
                  ┌──────────────┐
                  │   CLOSED     │
                  └──────────────┘
```

---

## 7. Error Handling Specification

### 7.1 Failure Modes

| Failure | Detection | Response | Recovery |
|---------|-----------|----------|----------|
| yfinance timeout | Exception caught | Skip symbol, log | Next cycle retries |
| NSE option chain down | Empty response | Use synthetic BS pricing | Auto-resumes when available |
| Telegram send fails | HTTP error | Fallback to stdout | Retry on next alert |
| SQLite locked | OSError | 20× retry with backoff | Log warning if exhausted |
| JSON corrupt | JSONDecodeError | Load from .bak file | Regenerate if both fail |
| AGoT component fails | Exception | Fall back to deterministic | Continue pipeline |

### 7.2 Data Freshness States

| Status | Condition | Impact |
|--------|-----------|--------|
| LIVE | Cache < 15 min old during market hours | Full signal generation |
| STALE | Cache 15–60 min old | Warning flag, signals still computed |
| OLD | Cache > 60 min old | Block trading, alert user |
| MISSING | No cache files | Block all operations, critical alert |
| EOD | Market closed | Read-only mode, no entries |

---

## 8. Configuration Schema

### 8.1 Required Configuration

```yaml
# config/config.yaml (required sections)
account:
  capital: 7000000          # Total capital in ₹

risk:
  per_trade_pct: 0.0075     # 0.75% per trade
  daily_stop_pct: 0.02       # 2% daily stop
  monthly_stop_pct: 0.06    # 6% monthly stop
  concurrent_open_pct: 0.03 # 3% max concurrent risk
  margin_util_cap: 0.60     # 60% margin ceiling
  correlation_max: 0.70     # Max correlation with open

alerts:
  telegram_bot_token: ${TELEGRAM_BOT_TOKEN}
  telegram_chat_id: ${TELEGRAM_CHAT_ID}

paths:
  journal_db: ./data/journal.sqlite

schedule_ist:
  morning_dashboard: "08:45"
```

### 8.2 Optional Configuration

```yaml
# Universe definition
universe:
  source: "fno200"          # or "index37" or explicit list
  
# Sector definitions
sectors:
  - "NIFTY IT"
  - "NIFTY BANK"
  - "NIFTY PHARMA"
  # ... etc

# Options configuration
nifty_options:
  enabled: true
  mode: "positional"
  lot_size: 75
  iron_condor_wing_width: 300
  # ... (see options-engine.md)

# AI configuration
ai:
  scrapegraph_api_key: ${SCRAPEGRAPHAI_API_KEY}
  google_api_key: ${GOOGLE_API_KEY}
```

---

## 9. Acceptance Criteria

### 9.1 Signal Accuracy

- Regime classification matches manual assessment ≥ 80% of the time
- Smart Money Bias correlates with next-day NIFTY direction ≥ 55% of the time
- A-grade leaders outperform NIFTY by ≥ 2% over 5-day holding period

### 9.2 Paper Trading Accuracy

- P&L calculations match manual spreadsheet verification
- Stop losses trigger within 1 tick of configured price
- No duplicate entries for same symbol on same day
- EOD summaries reconcile with individual trade P&Ls

### 9.3 System Reliability

- 99% uptime during market hours (Mon-Fri 09:15-15:30 IST)
- No data loss on crash (atomic writes + backup)
- Graceful degradation when any single data source fails

---

## 10. Future Requirements (Phase 3+)

| ID | Requirement | Target Phase |
|----|-------------|--------------|
| FR-BRK-01 | Live broker integration (Kite/Dhan/Fyers) | Phase 3 |
| FR-BRK-02 | Order execution with retry logic | Phase 3 |
| FR-BRK-03 | Position reconciliation | Phase 3 |
| FR-MON-01 | Intraday P&L monitoring | Phase 3 |
| FR-MON-02 | Multi-leg adjustment | Phase 3 |
| FR-MON-03 | Auto-exit on broker signals | Phase 3 |
| FR-ML-01 | ML-based regime prediction | Phase 4 |
| FR-ML-02 | Reinforcement learning for sizing | Phase 4 |
| FR-ML-03 | NLP news sentiment enhancement | Phase 4 |
