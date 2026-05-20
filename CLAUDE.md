# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**StockMinded** is a daily decision engine for Indian equities/derivatives trading. It answers 4 questions every morning before 9:15 IST:
- **Regime**: Market trend/volatility classification (6 states: TREND_UP, TREND_DOWN, RANGE_LOW_VOL, RANGE_HIGH_VOL, VOL_EXPANSION, VOL_CONTRACTION)
- **Flows**: FII/DII behavior, sector inflows/outflows, PCR, max-pain, smart-money bias
- **Leadership**: A-grade long/short lists via relative strength (RS-line quintiles)
- **Structure**: Trade plan mapped to regime (primary/secondary structures)

Results are logged to SQLite journal and sent as Telegram alerts. Optionally drives an auto-trader (Phase 2+).

**Capital target**: ₹70L with 5%/month returns. Rigid risk guardrails: ±2% daily stop, ±6% monthly stop, 0.75% per-trade risk, 3% concurrent open cap.

## Architecture

### Modular Layer Design

```
main.py
  ├── config/loader.py        → YAML loading with env var expansion
  ├── data/feed.py            → Data pipeline (yfinance + nsepython + AI fallback)
  ├── data/ai_scraper.py      → AI extraction (SaaS + Local ScrapeGraphAI)
  ├── signals/                → 4-signal computation
  │   ├── regime.py           → Market state classifier
  │   ├── flows.py            → FII/DII, PCR, sector analysis
  │   ├── leadership.py       → RS-line quintile ranking
  │   └── structure_map.py    → Regime → trade structure mapping
  ├── risk/                   → Risk enforcement
  │   ├── guardrails.py       → Position checks (daily stop, correlation, margin)
  │   └── sizing.py           → Position sizing (Kelly fraction based)
  └── ops/                    → Operations
      ├── journal.py          → SQLite trade + snapshot logging
      └── alerts.py           → Telegram fallback to stdout
```

**Phase 2 addition (not yet implemented)**:
- `execution/broker.py` → Kite/Dhan/Fyers order placement
- `manage/monitor.py` → Intraday P&L tracking, trail-stops, auto-exits

### Data Flow

1. **Data ingestion** (`data/feed.py`):
   - OHLC: yfinance (via `YF_SYMBOL` mapping for NSE indices/stocks)
   - Option chain, FII/DII, VIX: nsepython
   - AI Scraper Fallbacks (`data/ai_scraper.py`): ScrapeGraphAI hybrid integration using SaaS API (scrapegraph-py) with a local fallback (SmartScraperGraph/SearchGraph using Gemini). Used when nsepython/Research360 feeds fail.
   - Caching: `_OHLC_CACHE` with 120s bucket window

2. **Signal computation** (`signals/*`):
   - `regime.classify()` returns `RegimeSnapshot` (enum + metrics)
   - `flows.snapshot()` returns flow data (FII/DII, inflows, PCR, max-pain)
   - `leadership.rank_universe()` and `.a_grade()` for long/short lists
   - `structure_map.plan_for()` returns trade structure (primary + secondary)

3. **Logging** (`ops/journal.py`):
   - SQLite schema: 3 tables (regime_snapshots, flow_snapshots, trades)
   - `Journal` class handles insert/update operations

4. **Alerting** (`ops/alerts.py`):
   - `Alerter` class: sends to Telegram or falls back to stdout
   - `format_dashboard()` formats all 4 signals for Telegram markdown

### Configuration

**File**: `config/config.yaml`
**Key sections**:
- `account`: capital, currency
- `risk`: daily/monthly stops, per-trade risk %, concurrent open cap, margin/correlation limits
- `schedule_ist`: cron times (IST) for morning dashboard, entry window, midday check, EOD review
- `broker`: provider + credentials (env vars: `${BROKER_API_KEY}`, etc.)
- `universe_fo_sample`: 20 liquid F&O stocks for analysis
- `sectors`: 11 NIFTY sector indices
- `scrapegraphai`: enabled flag, model ("google_genai/gemini-1.5-flash"), api_key, and saas_api_key

**Loader** (`config/loader.py`): replaces `${ENV_VAR}` in YAML via `_expand()` from `os.getenv()`.

## Development Commands

### Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows; or source .venv/bin/activate (Unix)
pip install -r requirements.txt
cp .env.example .env
# Edit .env with BROKER_API_KEY, TELEGRAM_BOT_TOKEN, etc.
```

### Run

```bash
# Execute 4-signal pipeline now
python main.py dashboard

# Run APScheduler loop (IST schedule from config.yaml)
python main.py schedule

# Health check: test all data feeds
python main.py health
```

### Dashboard Server (Phase 2)

```bash
python dashboard/server.py
# Opens on http://localhost:5050 — Flask + paper trading simulator
```

### Cleanup Scripts

Located in `scratch/`:
- `cleanup_db.py` — Remove stale journal entries
- `reset_db.py` — Full SQLite wipe
- `reset_db_2.py` — Alternative reset

## Key Design Patterns

### Dataclass Snapshots

Signals return immutable dataclass snapshots (e.g., `RegimeSnapshot`, return-type contract):
```python
@dataclass
class RegimeSnapshot:
    regime: Regime
    trend_score: int
    vix: float
    ...
    def to_dict(self) -> dict:
        d = asdict(self)
        d["regime"] = self.regime.value  # Serialize enum
        return d
```
**Benefit**: type safety + JSON serializability.

### Enum for Regime States

```python
class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    ...
```
Inherit from `str` so enum values are JSON-safe without custom serialization.

### Config Expansion

Environment variables in YAML are expanded at load time via regex substitution:
```python
# config.yaml: api_key: "${BROKER_API_KEY}"
# Loader replaces with os.getenv("BROKER_API_KEY")
```
Allows sensitive secrets to stay out of version control.

### Caching Strategy

`data/feed.py`:
- **In-memory**: `_OHLC_CACHE` dict + 120s bucket window (`ohlc_cached`)
- **Disk**: `universe_ohlc` caches per-symbol as `.pkl` files (no external deps). Uses parquet initially but switched to pickle for zero-dependency reliability. Today's cache auto-loads; old files ignored.

### Risk Guardrails (Hard Constraints)

`risk/guardrails.py` enforces:
- Daily P&L ≤ −2% → flatten all positions
- Monthly P&L ≤ −6% → halve position size next month
- Concurrent open risk ≤ 3% of capital
- Margin utilization ≤ 60%
- Correlation vs. open positions ≤ 0.70

**Non-negotiable**: These limits are hardcoded and centralized. Do not bypass.

## Phase 2 Implementation (Options Trading)

### New Modules Added

**`signals/options.py`** (~216 lines):
- Black-Scholes delta/price via `math.erf` (no scipy)
- `chain_snapshot(symbol)` normalizes nsepython CE/PE into flat DataFrame [strike, expiry, ce_oi, ce_vol, ce_iv, ce_ltp, ce_delta, pe_oi, pe_vol, pe_iv, pe_ltp, pe_delta]
- `atm_strike`, `delta_strike`, `atm_iv` helpers
- `iv_rank(symbol, current_iv, db_path)` percentile in last 252 days, returns None if <60 days (bootstrap guard)
- `_next_expiry(symbol)` NSE expiry calendar (NIFTY weekly Thu, BANKNIFTY monthly last-Thu only post Jan-2025)

**`signals/option_strategy.py`** (~139 lines):
- `OptionLeg`, `OptionStructure`, `ResolvedLeg` dataclasses
- `pick_structure(regime, bias, iv_rank, vix) -> OptionStructure | None` maps regime to executable structure (BULL_CALL_SPREAD, BEAR_PUT_SPREAD, LONG_STRADDLE, SHORT_STRANGLE_WINGED)
- `resolve_legs(structure, chain, spot, lot_size)` converts strike rules (ATM, ATM±N, DELTA_X) to concrete strikes

**`dashboard/paper_trader.py`** (~400+ lines):
- `is_market_open()`, `is_eod_window()` IST helpers (9:15–15:30 Mon–Fri)
- `enter_option_structure()` validates max-loss vs risk cap, checks concurrent limit (default 4), dedupes per (underlying, structure, expiry, date)
- `check_option_exits()` market-hours gate + mark-to-market via fresh chain snapshot, exits on SL_HIT_NET / TGT_HIT_NET / GAMMA_SQOFF (≤30 min to 15:30) / EXPIRY_SETTLE
- P&L formula: `entry_net_debit - current_net_premium` (both sign convention: BUY=-qty*price, SELL=+qty*price)
- Trade schema: legs array, entry/current/exit premiums per leg, net P&L

**`dashboard/server.py`** updates:
- Routes: `/api/options/chain/<symbol>`, `/api/options/structures`, `/api/options/trades`, `POST /api/options/auto-enter`
- `_automation_worker` auto-enters structures during market hours if enabled in config
- Per-leg Telegram alerts via `_format_option_alert(trade)`
- EOD summary includes option P&L

**`dashboard/paper.html`**:
- NEW "OPTION TRADES" tab with collapsible legs, live mark, unrealized P&L, SL/TGT display, age

**`config/config.yaml`** additions:
```yaml
options:
  enabled: true
  underlyings: [NIFTY, BANKNIFTY]
  lot_size: {NIFTY: 75, BANKNIFTY: 30, FINNIFTY: 65}
  strike_step: {NIFTY: 50, BANKNIFTY: 100}
  expiry_preference: weekly
  max_risk_per_structure_pct: 0.0075
  max_concurrent_structures: 4
  iv_history_db: ./data/iv_history.sqlite
  enabled_structures: [BULL_CALL_SPREAD, BEAR_PUT_SPREAD, LONG_STRADDLE, SHORT_STRANGLE_WINGED]
```

**`config/fno200.csv`** (new):
- 175 F&O-eligible stocks with lot_size, sector columns. Single source of truth for universe + lot sizing.

**`config/loader.py`**:
- `load_universe(cfg)` reads fno200.csv if `universe_source==fno200`, else falls back to `universe_fo_sample` (19 stocks).

### Critical Implementation Notes

1. **IV rank bootstrap**: First 60 trading days return `None`; `pick_structure` falls back to VIX thresholds
2. **NSE holidays**: `_is_holiday(date)` reads `config/nse_holidays_2026.csv`; expiry rolls back one day if lands on holiday
3. **Option chain rate limit**: ONE fetch per underlying per worker tick, shared across picker + mark-to-market (NOT per-leg)
4. **Gamma risk**: `GAMMA_SQOFF` triggers ≤30 min before 15:30 IST to avoid gamma whip on short-gamma structures
5. **Credit vs debit SL**: Debit structures SL on net premium increase; credit structures SL = 1.5× entry credit
6. **Quadrant mapping**: IV rank [0–100] × Regime [6 states] → 60 structure paths; only 4 implemented initially (BULL_CALL_SPREAD, BEAR_PUT_SPREAD, LONG_STRADDLE, SHORT_STRANGLE_WINGED)

## Signal Module Details

### regime.py

**Key functions**:
- `classify(symbol, stock_universe_data)` → `RegimeSnapshot`
- `_trend_score()`: +1 for each of [px > EMA20, EMA20 > EMA50, EMA50 > EMA200]
- `_adx()`: Average Directional Index (custom calculation, not indicator library)
- `breadth_pct_above_50dma()`: % of universe stocks above 50-day MA

**Dependencies**: yfinance for NIFTY data, universe OHLC.

### flows.py

**Key fields** (returned in `FlowSnapshot`):
- `fii_dii_5d_net_cr`: 5-day net FII/DII (rupees crore)
- `pcr_oi`, `pcr_vol`: Put-call ratio (open interest and volume)
- `max_pain`: Level where most option holders lose money
- `smart_money_bias`: 'BULLISH' | 'BEARISH' | 'NEUTRAL'
- `top_inflow_sectors`, `top_outflow_sectors`: lists of (sector, pct_change)

**Data source**: nsepython (live, not cached).

### leadership.py

**Key functions**:
- `rank_universe(stock_data, benchmark)` → list[StockRank] with RS slope + quintile. Skips symbols with <50 bars. 20-day RS-line slope = (stock/benchmark polyfit m-coefficient) / mean, scaled 1e4.
- `a_grade(ranks, inflow_sectors, sector_map, top_n=10)` → tuple (longs, shorts). Strict: quintile 5 + above_50dma + slope>0 (else loose: quintile 5 only). Optional sector filter if sector_map provided.

**Logic**: Quintile 5 = strongest 20%; Quintile 1 = weakest 20%. RS-slope ±ve = relative strength trend. Sector filter biases toward inflow sectors for longs, outflow for shorts.

### structure_map.py

**Key function**: `plan_for(regime)` → `StructurePlan(primary, secondary, notes)`.

Maps each of the 6 regimes to a trade structure (e.g., "trend-following long", "range trading"). Helps traders decide entry tactics.

## Journal Schema (SQLite)

```sql
CREATE TABLE regime_snapshots (
    id PRIMARY KEY,
    ts TEXT,            -- UTC ISO timestamp
    regime TEXT,        -- Enum string value
    payload JSON        -- Full RegimeSnapshot dict
);

CREATE TABLE flow_snapshots (
    id PRIMARY KEY,
    ts TEXT,            -- UTC ISO timestamp
    payload JSON        -- Full FlowSnapshot dict
);

CREATE TABLE trades (
    id PRIMARY KEY,
    opened_at TEXT,
    closed_at TEXT,
    symbol TEXT,
    structure TEXT,     -- "primary" | "secondary"
    side TEXT,          -- "long" | "short"
    qty INTEGER,
    entry, stop, target, exit_price REAL,
    risk_rupees, pnl_rupees REAL,
    regime TEXT,        -- Regime when trade opened
    notes TEXT
);
```

## Critical Points

1. **IST scheduling**: All cron times in `config.yaml` are IST (Asia/Kolkata). Main alert window: 08:45 IST, entry opens 09:20 IST, markets close 15:30 IST.

2. **Risk is non-negotiable**: If any guardrail is violated, trading halts. This is by design for capital preservation.

3. **No auto-trading yet**: Phase 1 (current) sends alerts via Telegram; human executes trades. Phase 2 will add `execution/broker.py` after 2 weeks of validated alerts.

4. **Sector universe is fixed**: 11 NIFTY sector indices. Changing this requires updates to `data.YF_SYMBOL` mapping and `feed.sector_ohlc()`.

5. **NSE data feeds**: Rely on nsepython library. If NSE site structure changes, feed functions may break. Monitor for exceptions in `run_health()`.

6. **Correlation check**: Guardrails enforce 0.70 max correlation between any new trade and existing open positions. This prevents portfolio concentration.

## Testing

No formal integration tests yet, but a unit test suite has been established:
- Run AI Scraper unit tests: `pytest tests/test_ai_scraper.py -v`
- Run regime/alert/etc:
  - `python main.py dashboard` → 4 signals compute, check terminal output for data fetch counts
- `python dashboard/server.py` → server starts on :5050, test endpoints manually or via `/api/dashboard`
- Verify A-GRADE LONGS / SHORTS tables populate on dashboard (requires ≥50-bar data for each symbol)
- Telegram: `/api/test-telegram` sends ping, Alerter prints `[ALERT:SENT]` or `[ALERT:FAIL]`

**Common issues**:
- A-Grade empty: `universe_ohlc` returned 0 symbols (see `stdout: fetched=0`). Check network, yfinance rate limit, or cache corruption.
- Options empty: `chain_snapshot` failed (nsepython down?) or `pick_structure` returned None (IV history <60 days?).
- Telegram silent: `Alerter.send()` returned False; check token, chat_id, network. See `_last_send` dict in `/api/telegram/status`.

## Known Limitations

- **No API auth**: Dashboard endpoints public, no password. Add auth before production.
- **Parquet → Pickle**: Switched cache format to avoid pyarrow dep; old `.parquet` files ignored. Delete `data/cache/ohlc/` to force fresh download.
- **nsepython fragile**: Depends on NSE HTML structure. If feeds break, checks falls back to ScrapeGraphAI SaaS/Local extraction.
- **AI Latency**: ScrapeGraphAI calls (SaaS/Local) are significantly slower than direct REST API calls. They are only invoked as secondary fallback paths.
- **IST-only**: All times hardcoded to Asia/Kolkata. Multi-timezone would need refactor.
- **Paper trades only**: No real broker integration yet. Phase 3 will add live execution via Kite/Dhan/Fyers.

## Future Phases

- **Phase 3**: Add `execution/broker.py` (Kite/Dhan/Fyers) + live order placement
- **Phase 4**: Add `manage/monitor.py` for intraday P&L tracking, trail-stops, auto-exits, position squaring
