# AGENTS.md

This file provides guidance to agents when working with code in this repository.

- `config/loader.py` expands `${ENV_VAR}` inside YAML recursively; prefer putting secrets in [`config/config.yaml`](config/config.yaml) as placeholders instead of reading env vars ad hoc.
- Universe selection is not fixed: use [`config.loader.load_universe()`](config/loader.py:25) because `universe_source: fno200` switches runtime behavior away from `universe_fo_sample`.
- Cached market data is intentionally pickle-based, day-scoped, and stored under [`data/cache/ohlc`](data/cache/ohlc); [`data/feed.py`](data/feed.py:247) ignores old days instead of cleaning them.
- Dashboard and CLI do not share the exact same universe path: [`main.py`](main.py:28) still uses `universe_fo_sample`, while [`dashboard/server.py`](dashboard/server.py:70) uses `load_universe(cfg)`.
- Signal modules return dataclass snapshots with custom [`to_dict()`](signals/flows.py:25)-style serializers; preserve that boundary when adding fields.
- Paper trading state is file-backed at [`dashboard/paper_trades.json`](dashboard/paper_trades.json), not SQLite; scratch reset scripts hardcode Windows paths and are not portable.
- Journal persistence is SQLite-backed at [`paths.journal_db`](config/config.yaml:74) via [`ops/journal.py`](ops/journal.py:1); dashboard cache/freshness logic assumes this file coexists with pickle caches.
- Relative volume in intraday API is computed from 3 months of daily candles with `today_vol / mean(previous 20 sessions)` in [`dashboard/server.py`](dashboard/server.py:488), not from `fast_info.three_month_average_volume` alone.
- Q score is project-defined in [`signals/leadership.py`](signals/leadership.py:54) and now uses symmetric long/short threshold bands; do not replace with percentile quintiles without updating dashboard semantics.
- **Shoonya Primary Data Source**: [`data/shoonya_fetcher.py`](data/shoonya_fetcher.py:240) (`ShoonyaFetcher` class) is the PRIMARY source for option chains and quotes. OAuth 2.0 auth via Playwright browser automation (from April 2026). Option chain fallback order in [`data/feed.py`](data/feed.py:1630): Shoonya → direct NSE/nsepython → local files/ScrapeGraphAI. Quote batch: Shoonya (NFO futures for F&O stocks) → Dhan fallback → yfinance.
- **LLM Provider Chain**: [`data/ai_scraper.py`](data/ai_scraper.py:345) `call_llm()` fallback order: OpenCode Zen (big-pickle, primary) → Groq (llama-3.3-70b-versatile) → Gemini. Each provider has its own rate limiter and dead-provider caching (skip for 600s after failure). Uses `curl_cffi` with TLS 1.2 adapter to bypass Windows OpenSSL SSL bugs. Config keys under `scrapegraphai`: `opencode_api_key`, `groq_api_key`, `openrouter_api_key`, `sambanova_api_key`, `saas_api_key`.
- **AI Scraper & Sentiment Fallback**: Integrated **ScrapeGraphAI** in `data/ai_scraper.py` and `data/feed.py`.
  - Supports hybrid mode: manages calls through **SaaS API** (preferred, via `scrapegraph-py` using `saas_api_key`) and falls back to **Local execution** (`SmartScraperGraph`, `SearchGraph` using Gemini).
  - Environment: Requires `GOOGLE_API_KEY` for local fallback. Configured in `config/config.yaml` under `scrapegraphai`.
  - Flow snapshot captures `ai_sentiment` using news sentiment summarization.
  - Sentiment history persisted at `data/cache/ai_sentiment_history.json` for the self-improvement loop.
- **Intelligence Module (AGoT)**: [`intelligence/`](intelligence/__init__.py) implements Adaptive Graph of Thoughts framework.
  - `thought_graph.py` — Core graph-based reasoning engine (`ThoughtGraph`, `ThoughtNode`, `Evidence`).
  - `adaptive_regime.py` — Multi-hypothesis regime classification (`AdaptiveRegimeClassifier`).
  - `signal_ensemble.py` — Confidence-weighted signal aggregation (`SignalEnsemble`).
  - `feedback_loop.py` — Learns from trade outcomes (`FeedbackLoop`); updates evidence weights, tracks regime classification accuracy, calibrates confidence.
  - `agot_integration.py` — Integration layer (`AGoTPipeline`, `run_agot_dashboard()`).
  - `learner.py` — Legacy rule-based learning (backward compatible).
- **Timing Engine**: [`signals/timing.py`](signals/timing.py:1) gates entries on overextension and market exhaustion.
  - `is_overextended_from_vwap()`, `is_rsi_overextended()`, `is_price_overextended()` (ATR-based).
  - `market_exhaustion_score()` — breadth drop + VIX spike → 0.0–1.0 severity.
  - `evaluate_timing_for_entry()` — main entry point; optional Groq/Gemini AI review.
  - Config under `timing_engine` in `config/config.yaml` (late_entry_filter, market_exhaustion, event_risk_mode, ai_review, dynamic_thresholds, sentiment_tracking, backtest).
- **Verdict Engine (Split)**: [`signals/verdict.py`](signals/verdict.py:1) `build_trade_verdict()` splits decisions into `StockVerdict` (directional: LONG_ONLY/SHORT_ONLY/LONG_AND_SHORT/WAIT) and `NiftyVerdict` (option selling: OPTION_SELL_DEFINED_RISK/NAKED_OPTION_SELL/WAIT). AI sentiment steers confidence (boost/penalize) without blocking. Confidence labels HIGH/MEDIUM/LOW map to numeric scores 80/50/20.
- **Skip Log**: Blocked trades logged to `skipped_trades` SQLite table via [`ops/journal.py`](ops/journal.py:137) `log_skipped_trade()`. Deduped per (symbol, reason, engine) per day via `has_skipped_today()`. Dashboard exposes `/api/paper/skipped`.
- **Backtest Harness**: [`ops/backtest.py`](ops/backtest.py:1) `TimingBacktester` correlates `entry_quality` with PnL and suggests threshold adjustments. Output dir: `./data/backtest`.
- **Notifier**: [`data/notifier.py`](data/notifier.py:1) sends alerts via Telegram and Discord webhooks with retry logic.
- **Option Premium Filters**: `nifty_options` and `banknifty_options` config sections enforce `min_short_premium` / `max_short_premium` per leg. Walks TOWARDS ATM for adequate credit; rejects if ATM also below floor or can't get below cap. Zero-premium leg guards in [`dashboard/paper_trader.py`](dashboard/paper_trader.py:1).
- Test discovery is constrained by [`pytest.ini`](pytest.ini:1): `tests/`, `test_*.py`, `Test*`, `test_*`, with markers `unit` and `integration`. 30+ unit test files in `tests/unit/` covering Shoonya, smart exits, timing, verdict, skip log, guardrails, sizing, etc.
- Useful commands discovered from files:
  - `python main.py dashboard` — run the 4-signal dashboard now
  - `python main.py agot` — run the AGoT-enhanced dashboard
  - `python main.py agot-test` — quick AGoT system validation (no data fetch)
  - `python main.py schedule` — run APScheduler loop (IST times from config)
  - `python main.py health` — quick data connectivity check
  - `python dashboard/server.py` — Flask dashboard + paper trading on :5050
  - `pytest tests/test_ai_scraper.py -v`
  - `pytest tests/unit/ -q` — run full unit test suite
  - `pytest -m integration -q`
  - `python -m py_compile dashboard\server.py signals\flows.py signals\leadership.py data\feed.py data\ai_scraper.py`

Follow below instructions before starting work:

1. **NO GUESSING** — If I lack information or a library function is uncertain, do not invent syntax. State clearly what is missing.

2. **THINK BEFORE WRITING** — Wrap step-by-step logic, edge-case analysis, and architectural plan in `<thinking>` tags before outputting code.

3. **VERIFY EXAMPLES** — Ensure all code snippets use exact syntax of the specific version requested. Never mix versions.

4. **TYPE SAFETY** — Always write strictly typed code with explicit error handling and input validation.

5. **NO SHORTCUTS** — Provide full, runnable code blocks. No placeholders like `// implement here`.

6. **Use all necessary MCP servers before starting any work.

7. **Review previous code line-by-line** for deprecated methods, unhandled edge cases, or logic bugs before fixing.

## Input Token Optimization (Prompts & Context)

### 1. Compress System Prompts
- Remove filler: "Please", "I want you to", "Make sure to", "Your job is to"
- Replace prose instructions with imperative bullets
- Merge redundant rules into one authoritative statement

**Before:** "Please make sure that when you respond to the user, you always try to be as concise as possible and avoid unnecessary verbosity."
**After:** "Be concise. No filler."

### 2. Trim Conversation History
- Summarize resolved turns instead of keeping full transcripts
- Drop intermediate reasoning steps once conclusions are confirmed
- Keep only: current task context + unresolved decisions + key constraints
- Prune user messages that are now irrelevant (e.g., "thanks", "ok got it")

### 3. Reference, Don't Repeat
- If a document is already in context, cite it — don't re-paste it
- Use a short label: "See Schema A above" instead of re-including the schema
- For structured data, send only relevant fields, not full objects

### 4. Compress Few-Shot Examples
- Use 1–2 tight examples, not 5 verbose ones
- Strip all commentary from examples — just input → output pairs
- If the pattern is simple, skip examples entirely; describe the rule instead

### 5. Chunk Long Documents
- Don't inject an entire document if only a section is relevant
- Pre-filter before sending: extract the relevant rows, paragraphs, or fields
- For RAG pipelines: retrieve narrowly, summarize before injecting
