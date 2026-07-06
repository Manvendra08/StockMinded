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
  ├── data/feed.py            → Data pipeline (Shoonya + yfinance + nsepython + AI fallback)
  ├── data/shoonya_fetcher.py → Shoonya (Finvasia) PRIMARY data source: option chain, quotes, LTP
  ├── data/ai_scraper.py      → AI extraction + LLM provider chain + news sentiment
  ├── data/notifier.py        → Telegram + Discord notification utility
  ├── signals/                → 4-signal computation
  │   ├── regime.py           → Market state classifier
  │   ├── flows.py            → FII/DII, PCR, sector analysis
  │   ├── leadership.py       → RS-line quintile ranking
  │   ├── structure_map.py    → Regime → trade structure mapping
  │   ├── timing.py           → Entry timing gates (VWAP/RSI/ATR overextension, market exhaustion)
  │   ├── verdict.py          → Split verdict builder (stock directional vs. Nifty option selling)
  │   ├── options.py          → Option chain snapshot, IV rank, expiry helpers
  │   └── option_strategy.py  → Option structure picker + leg resolver
  ├── intelligence/           → AGoT (Adaptive Graph of Thoughts) framework
  │   ├── thought_graph.py    → Core graph-based reasoning engine
  │   ├── adaptive_regime.py  → Multi-hypothesis regime classification
  │   ├── signal_ensemble.py  → Confidence-weighted signal aggregation
  │   ├── feedback_loop.py    → Learn from trade outcomes (self-improvement)
  │   ├── agot_integration.py → AGoT pipeline integration layer
  │   └── learner.py          → Legacy rule-based learning (backward compatible)
  ├── risk/                   → Risk enforcement
  │   ├── guardrails.py       → Position checks (daily stop, correlation, margin)
  │   └── sizing.py           → Position sizing (Kelly fraction based)
  └── ops/                    → Operations
      ├── journal.py          → SQLite trade + snapshot + skip-log logging
      ├── alerts.py           → Telegram fallback to stdout
      └── backtest.py         → Timing backtest harness (entry_quality vs PnL correlation)
```

**Phase 3 (not yet implemented)**:
- `execution/broker.py` → Kite/Dhan/Fyers live order placement
- `manage/monitor.py` → Intraday P&L tracking, trail-stops, auto-exits

### Data Flow

1. **Data ingestion** (`data/feed.py`):
   - OHLC: yfinance (via `YF_SYMBOL` mapping for NSE indices/stocks)
   - Option chain: Shoonya (PRIMARY, OAuth-authenticated) → direct NSE/nsepython → local files/ScrapeGraphAI fallback
   - Quotes/LTP: Shoonya NFO futures (for F&O stocks) → Dhan fallback → yfinance
   - FII/DII, VIX: nsepython
   - AI Scraper & LLM (`data/ai_scraper.py`): `call_llm()` provider chain: OpenCode Zen (big-pickle) → Groq (llama-3.3-70b-versatile) → Gemini. Each provider has rate limiters and dead-provider caching. Uses `curl_cffi` + TLS 1.2 adapter for Windows SSL compatibility. ScrapeGraphAI hybrid (SaaS + Local) used when structured APIs fail.
   - News sentiment: RSS feeds (ICICIDirect, Livemint, Moneycontrol) → LLM summarization → `ai_sentiment` dict with `overall_market_sentiment`, `confidence`, `sentiment_score`. History persisted at `data/cache/ai_sentiment_history.json` for self-improvement.
   - Caching: `_OHLC_CACHE` with 120s bucket window

2. **Signal computation** (`signals/*`):
   - `regime.classify()` returns `RegimeSnapshot` (enum + metrics)
   - `flows.snapshot()` returns flow data (FII/DII, inflows, PCR, max-pain, `ai_sentiment`)
   - `leadership.rank_universe()` and `.a_grade()` for long/short lists
   - `structure_map.plan_for()` returns trade structure (primary + secondary)
   - `timing.evaluate_timing_for_entry()` gates entries on VWAP/RSI/ATR overextension + market exhaustion
   - `verdict.build_trade_verdict()` splits decisions: `StockVerdict` (directional) + `NiftyVerdict` (option selling). AI sentiment steers confidence without blocking.

3. **Intelligence layer** (`intelligence/`, optional):
   - `AGoTPipeline` / `run_agot_dashboard()` enhances the 4-signal pipeline with graph-based multi-hypothesis reasoning.
   - `FeedbackLoop` analyzes closed trades to update evidence weights and calibrate confidence.

4. **Logging** (`ops/journal.py`):
   - SQLite schema: 5 tables (regime_snapshots, flow_snapshots, trades, skipped_trades, trade_exit_analysis)
   - `Journal` class handles insert/update operations + skip-log deduplication

5. **Alerting** (`ops/alerts.py`, `data/notifier.py`):
   - `Alerter` class: sends to Telegram or falls back to stdout
   - `format_dashboard()` formats all 4 signals for Telegram markdown
   - `notifier.py`: Telegram + Discord webhook utility with retry logic

### Configuration

**File**: `config/config.yaml`
**Key sections**:
- `account`: capital, currency
- `risk`: daily/monthly stops, per-trade risk %, concurrent open cap, margin/correlation limits
- `targets`: monthly/weekly net % targets
- `schedule_ist`: cron times (IST) for morning dashboard, entry window, midday check, EOD review
- `broker`: provider (dhan) + credentials (env vars: `${BROKER_API_KEY}`, `${DHAN_CLIENT_ID}`, etc.)
- `universe_source`: `fno200` (default) or `fo_sample`
- `universe_fo_sample`: 19 liquid F&O stocks for fallback universe
- `sectors`: 11 NIFTY sector indices
- `data_sources`: Dhan API config (disabled, not subscribed), local files, ScrapeGraphAI
- `scrapegraphai`: enabled flag, model (`google_genai/gemini-2.5-flash-lite`), `model_tokens` (8192), `api_key` (Google), `groq_api_key`, `openrouter_api_key`, `sambanova_api_key`, `opencode_api_key`, `saas_api_key`
- `options`: enabled, underlyings, lot_size (NIFTY: 65, BANKNIFTY: 30, FINNIFTY: 60), strike_step, expiry_preference, max_risk_per_structure_pct, max_concurrent_structures (10), iv_history_db, enabled_structures
- `nifty_options`: NIFTY-specific option selling config — min_lots_per_leg, mode (intraday/positional), entry/exit windows, max structures, iron_condor_wing_width, spread_width, max_risk_per_pct, avoid_vol_expansion, profit_take_pct, stop_loss_mult, vix_spike_exit_pct, min_short_premium, max_short_premium
- `banknifty_options`: BANKNIFTY-specific option selling config (same structure as nifty_options with different thresholds)
- `timing_engine`: late_entry_filter (VWAP/RSI/ATR gates), market_exhaustion (breadth/VIX), event_risk_mode (size multiplier, sentiment flip cooldown), ai_review (Groq/Gemini), dynamic_thresholds (per-regime adjustment rules), sentiment_tracking (flip detection), backtest (min_trades, correlation_threshold)

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

# Run AGoT-enhanced dashboard (graph-based reasoning)
python main.py agot

# Quick AGoT system validation (no data fetch)
python main.py agot-test

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

**`signals/options.py`** (~864 lines):
- Black-Scholes delta/price via `math.erf` (no scipy)
- `chain_snapshot(symbol, target_expiries, target_strikes)` normalizes nsepython/Shoonya CE/PE into flat DataFrame [strike, expiry, ce_oi, ce_vol, ce_iv, ce_ltp, ce_delta, pe_oi, pe_vol, pe_iv, pe_ltp, pe_delta]
- `atm_strike`, `delta_strike`, `atm_iv` helpers
- `iv_rank(symbol, current_iv, db_path)` percentile in last 252 days, returns None if <60 days (bootstrap guard)
- `_next_expiry(symbol)` NSE expiry calendar (NIFTY weekly Thu, BANKNIFTY monthly last-Thu only post Jan-2025)
- 0-DTE avoidance: on expiry day, prefers next week's expiry for stable signals

**`signals/option_strategy.py`** (~1085 lines):
- `OptionLeg`, `OptionStructure`, `ResolvedLeg` dataclasses
- `pick_structure(regime, bias, iv_rank, vix) -> OptionStructure | None` maps regime to executable structure (BULL_CALL_SPREAD, BEAR_PUT_SPREAD, LONG_STRADDLE, SHORT_STRANGLE_WINGED)
- `resolve_legs(structure, chain, spot, lot_size)` converts strike rules (ATM, ATM±N, DELTA_X) to concrete strikes
- Builder functions for `NAKED_PUT_SELL`, `NAKED_CALL_SELL`, `IRON_CONDOR`, `IRON_CONDOR_WIDE` strategies (driven by verdict engine)
- `setup_structure_from_verdict()` — maps verdict actions to option structures with premium floor/cap enforcement

**`signals/timing.py`** (~723 lines):
- `is_overextended_from_vwap()` — price vs VWAP deviation check
- `is_rsi_overextended()` — RSI overbought/oversold gate (configurable thresholds)
- `is_price_overextended()` — ATR-based price extension from open
- `market_exhaustion_score()` — breadth drop + VIX spike → 0.0–1.0 severity
- `evaluate_timing_for_entry()` — main entry point combining all gates; optional Groq/Gemini AI review
- Dynamic threshold adjustment per regime (TREND_UP, TREND_DOWN, RANGE_LOW_VOL, VOL_CONTRACTION)

**`signals/verdict.py`** (~362 lines):
- `StockVerdict`, `NiftyVerdict`, `CombinedVerdict` dataclasses
- `build_trade_verdict(data)` — splits decisions into directional stock picking (LONG_ONLY/SHORT_ONLY/LONG_AND_SHORT/WAIT) and Nifty option selling (OPTION_SELL_DEFINED_RISK/NAKED_OPTION_SELL/WAIT)
- AI sentiment steers confidence: BULLISH/BEARISH alignment boosts confidence, opposition downgrades but doesn't block
- Confidence labels (HIGH/MEDIUM/LOW) with numeric scores (80/50/20)
- IV rank ≥40 required for naked selling; <40 downgrades to defined-risk spread

**`data/shoonya_fetcher.py`** (~1373 lines):
- `ShoonyaFetcher` class — PRIMARY market data source
- OAuth 2.0 authentication via Playwright browser automation (from April 2026)
- `login()` — session management with thread lock
- `fetch_option_chain(symbol, expiry)` — GetOptionChain API
- `fetch_fno_quote(symbol)` — NFO futures quote (for F&O stocks)
- `fetch_quote(symbol)` — general LTP/quote
- `get_shoonya()` — singleton accessor
- Index→exchange mapping: NIFTY/BANKNIFTY/FINNIFTY → NFO, SENSEX → BFO

**`dashboard/paper_trader.py`** (~3440 lines):
- `is_market_open()`, `is_eod_window()` IST helpers (9:15–15:30 Mon–Fri)
- `enter_option_structure()` validates max-loss vs risk cap, checks concurrent limit, dedupes per (underlying, structure, expiry, date)
- `check_option_exits()` market-hours gate + mark-to-market via fresh chain snapshot, exits on SL_HIT_NET / TGT_HIT_NET / GAMMA_SQOFF (≤30 min to 15:30) / EXPIRY_SETTLE
- P&L formula: `entry_net_debit - current_net_premium` (both sign convention: BUY=-qty*price, SELL=+qty*price)
- Trade schema: legs array, entry/current/exit premiums per leg, net P&L
- **Smart Exits**: VIX spike exits, delta breach checks, strike breaches, theta trail locks
- **Risk Guardrails**: Honors real-time config updates (from UI) for daily limits, concurrent risk, and margin
- **Premium Filters**: `min_short_premium` floor rejects inadequate credit (walks TOWARDS ATM); `max_short_premium` cap rejects excessive risk (walks further OTM). Zero-premium leg guards.
- **Skip Log**: Blocked trades logged to `skipped_trades` table with dedup via `has_skipped_today()`

**`dashboard/server.py`** updates:
- Routes: `/api/options/chain/<symbol>`, `/api/options/structures`, `/api/options/trades`, `POST /api/options/auto-enter`, `/api/paper/skipped`, verdict tracing
- `_automation_worker` auto-enters structures during market hours if enabled in config
- Per-leg Telegram alerts via `_format_option_alert(trade)`
- EOD summary includes option P&L
- AI timing review integration (Groq/Gemini) for entry validation
- Data source tracking: `data_freshness` reports primary source (shoonya/dhan/yfinance) for quotes and option chain

**`dashboard/paper.html`**:
- "OPTION TRADES" tab with collapsible legs, live mark, unrealized P&L, SL/TGT display, age.
- "DIAGNOSTICS" tab featuring the Verdict Engine Trace and Skip Log (blocks/reasons).
- "MAINTENANCE" tab for dynamic Trading Settings, Smart Exits, and Risk Gate configuration.

**`config/config.yaml`** additions:
```yaml
options:
  enabled: true
  underlyings: [NIFTY, BANKNIFTY]
  lot_size: {NIFTY: 65, BANKNIFTY: 30, FINNIFTY: 60}
  strike_step: {NIFTY: 50, BANKNIFTY: 100}
  expiry_preference: weekly
  max_risk_per_structure_pct: 0.0075
  max_concurrent_structures: 10
  iv_history_db: ./data/iv_history.sqlite
  enabled_structures: [BULL_CALL_SPREAD, BEAR_PUT_SPREAD, LONG_STRADDLE, SHORT_STRANGLE_WINGED]

nifty_options:
  enabled: true
  min_lots_per_leg: 10
  mode: positional  # intraday | positional
  intraday_entry_start: "09:45"
  intraday_entry_end: "14:30"
  intraday_exit_by: "15:25"
  max_nifty_structures: 2
  iron_condor_wing_width: 300
  spread_width: 200
  max_risk_per_pct: 0.01
  avoid_vol_expansion: true
  vol_expansion_threshold: 5.0
  profit_take_pct: 0.50
  stop_loss_mult: 1.0
  vix_spike_exit_pct: 10.0
  min_short_premium: 15.0
  max_short_premium: 150.0

timing_engine:
  enabled: true
  late_entry_filter: {max_vwap_dist_pct, rsi_threshold_long/short, max_move_from_open_pct, max_intraday_atr_extension}
  market_exhaustion: {breadth_drop_threshold_pct, vix_intraday_spike_pct}
  event_risk_mode: {size_multiplier, no_fresh_equity_after, sentiment_flip_cooldown_minutes}
  ai_review: {provider, model, temperature, timeout_sec}
  dynamic_thresholds: {per-regime adjustment rules}
  sentiment_tracking: {flip_detection, flip_cooldown_minutes, tracking_window_trades}
  backtest: {min_trades_for_analysis, correlation_threshold, output_dir}
```

**`config/fno200.csv`**:
- 175 F&O-eligible stocks with lot_size, sector columns. Single source of truth for universe + lot sizing.

**`config/loader.py`**:
- `load_universe(cfg)` reads fno200.csv if `universe_source==fno200`, else falls back to `universe_fo_sample` (19 stocks).

### Critical Implementation Notes

1. **IV rank bootstrap**: First 60 trading days return `None`; `pick_structure` falls back to VIX thresholds
2. **NSE holidays**: `_is_holiday(date)` reads `config/nse_holidays_2026.csv`; expiry rolls back one day if lands on holiday
3. **Option chain rate limit**: ONE fetch per underlying per worker tick, shared across picker + mark-to-market (NOT per-leg)
4. **Gamma risk**: `GAMMA_SQOFF` triggers ≤30 min before 15:30 IST to avoid gamma whip on short-gamma structures
5. **Credit vs debit SL**: Debit structures SL on net premium increase; credit structures SL = 1.0–1.25× entry credit (per underlying config)
6. **Premium floor/cap**: `min_short_premium` walks TOWARDS ATM for adequate credit; `max_short_premium` walks further OTM to reduce risk. Reject if can't satisfy either.
7. **Shoonya LTP corruption**: Source-level fix for corrupt option LTPs; Shoonya fetcher validates and rejects bad data
8. **0-DTE avoidance**: On expiry day, `chain_snapshot` prefers next week's expiry for signal stability
9. **AI sentiment steering**: AI sentiment influences confidence (boost/penalize) but never blocks trades; direction shifts range-regime long/short balance

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

CREATE TABLE skipped_trades (
    id PRIMARY KEY,
    ts TEXT,             -- UTC ISO timestamp
    symbol TEXT,
    direction TEXT,
    alert_confidence TEXT,
    skip_reason TEXT,
    engine TEXT          -- "options_engine" | "stock_engine" | etc.
);

CREATE TABLE trade_exit_analysis (
    id PRIMARY KEY,
    trade_id INTEGER,
    exit_quality TEXT,
    loss_root_cause TEXT,
    timing_snapshot JSON,
    analysis_ts TEXT
);
```

## Critical Points

1. **IST scheduling**: All cron times in `config.yaml` are IST (Asia/Kolkata). Main alert window: 08:45 IST, entry opens 09:20 IST, markets close 15:30 IST.

2. **Risk is non-negotiable**: If any guardrail is violated, trading halts. This is by design for capital preservation.

3. **Paper trading active (Phase 2)**: Options paper trading is live with auto-entry, smart exits, and skip logging. No real broker integration yet — Phase 3 will add `execution/broker.py`.

4. **Sector universe is fixed**: 11 NIFTY sector indices. Changing this requires updates to `data.YF_SYMBOL` mapping and `feed.sector_ohlc()`.

5. **Data source priority**: Shoonya (PRIMARY) → nsepython/NSE direct → ScrapeGraphAI/yfinance fallback. Monitor `data_freshness` in dashboard for source health.

6. **Correlation check**: Guardrails enforce 0.70 max correlation between any new trade and existing open positions. This prevents portfolio concentration.

7. **Verdict split**: Stock directional picking and Nifty option selling are evaluated independently. AI sentiment steers confidence but never blocks trades.

8. **Premium filters**: `min_short_premium` / `max_short_premium` per underlying reject inadequate or excessive risk strikes. Zero-premium leg guards prevent worthless entries.

## Testing

A comprehensive unit test suite (30+ files in `tests/unit/`) plus integration tests:
- Run full unit suite: `pytest tests/unit/ -q`
- Run AI Scraper tests: `pytest tests/test_ai_scraper.py -v`
- Run integration tests: `pytest -m integration -q`
- Run Shoonya tests: `pytest tests/unit/test_shoonya_session.py tests/unit/test_shoonya_fno_quote.py tests/unit/test_shoonya_corrupt_ltp.py -v`
- Run timing/verdict tests: `pytest tests/unit/test_timing.py tests/unit/test_timing_phase2.py tests/unit/test_smart_exits.py -v`
- Manual smoke tests:
  - `python main.py dashboard` → 4 signals compute, check terminal output for data fetch counts
  - `python main.py agot` → AGoT-enhanced dashboard runs
  - `python dashboard/server.py` → server starts on :5050, test endpoints manually or via `/api/dashboard`
  - Verify A-GRADE LONGS / SHORTS tables populate on dashboard (requires ≥50-bar data for each symbol)
  - Telegram: `/api/test-telegram` sends ping, Alerter prints `[ALERT:SENT]` or `[ALERT:FAIL]`

**Test coverage includes**: Shoonya session/quotes/option chain, smart exits, timing gates, trailing SL, ATM filtering, duplicate late entry, skipped trades API, guardrails, sizing, regime, flows, leadership, structure map, journal, alerts, config loader, paper auto-entry, EOD summary, bugfix regressions.

**Common issues**:
- A-Grade empty: `universe_ohlc` returned 0 symbols (see `stdout: fetched=0`). Check network, yfinance rate limit, or cache corruption.
- Options empty: `chain_snapshot` failed (Shoonya down? NSE blocked?) or `pick_structure` returned None (IV history <60 days?).
- Shoonya login fail: Check `SHOONYA_USER_ID`, `SHOONYA_PASSWORD`, `SHOONYA_TOTP_KEY`, `SHOONYA_API_SECRET` env vars. OAuth requires Playwright browser.
- LLM all providers exhausted: Check `OPENCODE_API_KEY`, `GROQ_API_KEY`, `GOOGLE_API_KEY`. See `test_llm_providers()` diagnostic.
- Telegram silent: `Alerter.send()` returned False; check token, chat_id, network. See `_last_send` dict in `/api/telegram/status`.

## Known Limitations

- **No API auth**: Dashboard endpoints public, no password. Add auth before production.
- **Parquet → Pickle**: Switched cache format to avoid pyarrow dep; old `.parquet` files ignored. Delete `data/cache/ohlc/` to force fresh download.
- **Shoonya OAuth dependency**: Primary data source requires Playwright browser for OAuth login. If Shoonya is down or credentials expire, falls back to nsepython/yfinance (may be less reliable).
- **nsepython fragile**: Depends on NSE HTML structure. Used as secondary fallback after Shoonya.
- **LLM provider dependency**: AI sentiment and timing review depend on external LLM APIs (OpenCode Zen, Groq, Gemini). If all providers fail, sentiment falls back to local RSS analysis.
- **AI Latency**: LLM and ScrapeGraphAI calls are significantly slower than direct REST API calls. Only invoked as fallback/enhancement paths.
- **IST-only**: All times hardcoded to Asia/Kolkata. Multi-timezone would need refactor.
- **Paper trades only**: No real broker integration yet. Dhan API config exists but disabled (not subscribed). Phase 3 will add live execution.

## Future Phases

- **Phase 3**: Add `execution/broker.py` (Kite/Dhan/Fyers) + live order placement
- **Phase 4**: Add `manage/monitor.py` for intraday P&L tracking, trail-stops, auto-exits, position squaring
