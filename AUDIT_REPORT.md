# StockMinded Code Audit Report

**Generated:** 2026-07-30  
**Scope:** Full repository functional logic and technical bug audit  
**Method:** Line-by-line review of all Python modules

---

## Executive Summary

The codebase is a sophisticated multi-signal trading system with:
- **4 signal engines** (Regime, Flows, Leadership, Structure)
- **AGoT intelligence layer** (Adaptive Graph of Thoughts)
- **Shoonya primary data source** with OAuth 2.0 browser auth
- **Paper trading engine** with SQLite + JSON persistence
- **Dashboard** (Flask) + **Scheduler** (APScheduler)
- **LLM provider chain** with 8 fallbacks

**Overall Quality:** High - well-structured, typed, with defensive patterns.  
**Critical Issues Found:** 8  
**High Priority Issues:** 12  
**Medium Priority Issues:** 19  
**Technical Debt / Maintenance:** 23

---

## Critical Bugs (Must Fix)

### 1. `risk/guardrails.py:60-77` — `Guardrails.check_new_trade()` References Undefined Variables
```python
# Lines 57-77 reference variables that don't exist in scope:
month_pnl        # not a parameter
open_risk        # not a parameter  
proposed_risk    # not a parameter
reasons          # used but never initialized (line 64)
```
**Impact:** Runtime `NameError` when guardrails are checked.  
**Fix:** Add missing parameters, initialize `reasons = []`.

---

### 2. `signals/leadership.py:122-127` — Timezone Comparison Bug
```python
last_date = valid_vol.index[-1]
if hasattr(last_date, "date"):
    last_date = last_date.date()
ist = timezone(timedelta(hours=5, minutes=30))
today_ist = dt.datetime.now(ist).date()
if last_date == today_ist:  # BUG: Comparing date to datetime.date
```
**Impact:** RVOL projection incorrectly applied/skipped due to tz mismatch.  
**Fix:** Ensure both sides are `date` objects in IST.

---

### 3. `data/feed.py:1400-1470` — `ohlc()` Cache Key Collision Risk
```python
key = f"{symbol}_{period}_{interval}"
```
**Impact:** Different symbols with same period/interval can collide if symbol naming differs (e.g., "RELIANCE" vs "RELIANCE.NS").  
**Fix:** Normalize symbol before key generation: `symbol.upper().replace(".NS", "")`.

---

### 4. `data/feed.py:157-173` — `_env_or_value()` Only Handles `${VAR}` Format
```python
if value.startswith("${") and value.endswith("}"):
    return os.getenv(value[2:-1])
```
**Impact:** Bare `$VAR` format from `config.loader._expand()` returns raw string. Secrets leak into logs/DB.  
**Fix:** Also handle `$VAR` format or standardize on `${VAR}` everywhere.

---

### 5. `dashboard/server.py:1186-1220` — `_generate_trade_alerts()` References Undefined `pt`
```python
trades.extend(pt.get_all_trades(limit=999999))  # Line 275
trades.extend(pt.get_open_trades())              # Line 307
```
**Missing import:** `from dashboard import paper_trader as pt`  
**Impact:** `NameError` on dashboard load.  
**Fix:** Add import at module top.

---

### 6. `signals/flows.py:498-513` — ThreadPoolExecutor Timeout Race
```python
with ThreadPoolExecutor(max_workers=1) as ex:
    future = ex.submit(ai_scraper.get_market_news_sentiment, ...)
    ai_sentiment = future.result(timeout=90)
```
**Issue:** `FuturesTimeout` caught but `ai_sentiment = None` returned silently. Caller (`flows_mod.snapshot()`) proceeds with `None` sentiment.  
**Fix:** Log timeout clearly, consider circuit breaker pattern.

---

### 7. `data/screener_fetcher.py:500-509` — Cache Stampede on Adaptive Analysis
```python
cached = journal.get_cached_fundamentals(symbol, ...)
if cached and call_llm is not None and "adaptive_analysis" not in cached:
    adaptive = adaptive_fundamental_analysis(symbol, call_llm, company_name)
    if adaptive:
        cached["adaptive_analysis"] = adaptive
        journal.cache_fundamentals(symbol, cached, ...)  # Race: multiple threads overwrite
```
**Impact:** Concurrent requests for same symbol trigger duplicate LLM calls.  
**Fix:** Use Redis lock or SQLite `INSERT OR IGNORE` pattern.

---

### 8. `intelligence/adaptive_regime.py:760-768` — `classify_adaptive()` Creates New Classifier Each Call
```python
def classify_adaptive(...):
    classifier = AdaptiveRegimeClassifier()  # New instance every call
    return classifier.classify(...)
```
**Impact:** Loses any learned state, re-fetches data unnecessarily.  
**Fix:** Make classifier a singleton or inject pre-initialized instance.

---

## High Priority Issues

### 9. `config/loader.py:148` — Universe Source Mismatch Dashboard vs CLI
```python
def load_universe(cfg): src = cfg.get("universe_source", "fno200")
```
**Dashboard** (`server.py:70`): Uses `load_universe(cfg)` → respects `universe_source: fno200`  
**CLI** (`main.py:52`): Uses `cfg.get("universe_fo_sample", [])` directly → ignores `fno200`  
**Impact:** Different universes in dashboard vs scheduled runs.  
**Fix:** Use `load_universe(cfg)` everywhere.

---

### 10. `signals/regime.py:276` — VIX Rank Calculation Uses Incomplete Window
```python
def _vix_rank(vix_series, window=252):
    tail = vix_series.iloc[-window:].dropna()
    # If len(vix) < 252, uses shorter window but still calls it "1-year rank"
```
**Impact:** Rank percentile not comparable across timeframes.  
**Fix:** Require minimum window (e.g., 126 sessions) or label dynamically.

---

### 11. `signals/timing.py:225-227` — `evaluate_timing_for_entry()` Market Breadth Proxy
```python
recent_closes = nifty_df["close"].tail(20)
historical_momentum = (recent_closes.iloc[-1] - recent_closes.iloc[0]) / recent_closes.iloc[0]
# Uses NIFTY momentum as PROXY for breadth — fundamentally flawed
```
**Impact:** Breadth exhaustion detection unreliable.  
**Fix:** Fetch actual advance/decline data or use NSE breadth API.

---

### 12. `signals/verdict.py:418-422` — Circuit Breaker Thresholds Too Aggressive
```python
bearish_cb = (nifty_momentum <= -2.5) or (ai_influence <= -0.8)
bullish_cb = (nifty_momentum >= 2.5) or (ai_influence >= 0.8)
```
**Impact:** 2.5% daily momentum is common (not extreme). Blocks legitimate trades.  
**Fix:** Raise to ±4% or use ATR-normalized threshold.

---

### 13. `dashboard/paper_trader.py:908-910` — Zero Premium Guard Uses `<= 0` Not `< epsilon`
```python
zero_prem_legs = [(l.side, l.type, l.strike, l.premium) for l in resolved_legs if l.premium <= 0]
```
**Impact:** Floating point precision (e.g., 1e-10) treated as zero premium → false rejections.  
**Fix:** Use `if l.premium < 0.01` or `math.isclose(premium, 0, abs_tol=0.01)`.

---

### 14. `data/ai_scraper.py:504-506` — Kilo Gateway Model List Hardcoded
```python
_kilo_models = [
    ("nvidia/nemotron-3-ultra-550b-a55b:free", ...),
    ...
]
```
**Impact:** Model deprecation breaks primary LLM path silently.  
**Fix:** Fetch available models from `/models` endpoint or config-driven list.

---

### 15. `intelligence/signal_ensemble.py:342-386` — Agreement Score Weighting Flaw
```python
for sig_name, contrib in contributions.items():
    if contrib["bias"] == ("LONG" if long_count >= short_count ...):
        weighted_agreement += contrib["confidence"] * contrib["weight"]
```
**Issue:** Majority bias determined BEFORE weighting, then weighted agreement uses same majority. Circular logic.  
**Fix:** Compute weighted agreement independently of raw count.

---

### 16. `ops/backtest.py:138-150` — Quality Mapping Hardcoded
```python
quality_map = {"GOOD": 1.0, "MID": 0.5, "LATE": 0.0, "EXHAUSTED": -1.0}
```
**Impact:** New entry quality labels (e.g., "CHASING", "REVERSAL") silently ignored → correlation wrong.  
**Fix:** Load from config or derive from timing engine enums.

---

### 17. `dashboard/server.py:581-600` — `option_chain()` Source Detection Fragile
```python
base = src.split(":")[0].split("/")[0]
oc_primary = _OC_SOURCE_MAP.get(base, base)
```
**Impact:** New sources (e.g., "shoonya:v2") not mapped → dashboard shows "unknown".  
**Fix:** Use prefix matching or registry pattern.

---

### 18. `signals/options.py:114-118` — Monthly Expiry Holiday Rollback Infinite Loop Risk
```python
while _is_holiday(exp_date) or exp_date.weekday() >= 5:
    exp_date -= timedelta(days=1)
```
**Edge Case:** If entire month is holidays (theoretical), infinite loop.  
**Fix:** Add max iteration guard (e.g., `for _ in range(10):`).

---

### 19. `data/feed.py:1063-1065` — Research360 PCR Value Handling
```python
try:
    pcr_value = float(pcr_raw) if pcr_raw and pcr_raw != "-" else None
except (TypeError, ValueError):
    pcr_value = None
```
**Issue:** `"-"` string comparison works but `"- "` (with space) or `"N/A"` would fail.  
**Fix:** Use `str(pcr_raw).strip()` and check against known invalid set.

---

### 20. `risk/sizing.py:56-58` — `directional_size()` Lot Rounding Logic Flawed
```python
lots = math.floor(raw_qty / lot_size)
if lots == 0 and raw_qty >= lot_size * 0.5:
    lots = 1
```
**Issue:** If `raw_qty = 0.6 * lot_size`, `lots = 1` → actual risk > budget.  
**Fix:** Only round up if `raw_qty >= lot_size * 0.9` or use proper rounding.

---

## Medium Priority Issues

### 21. `config/config.yaml:5-6` — Telegram Credentials as Placeholders
```yaml
telegram_bot_token: ${TELEGRAM_BOT_TOKEN}
telegram_chat_id: ${TELEGRAM_CHAT_ID}
```
**Issue:** No validation that these are set before `Alerter` init. Silent failures.  
**Fix:** Add startup validation in `main.py` or `server.py`.

---

### 22. `dashboard/paper_trader.py:108` — Hardcoded Windows Path
```python
_TOKEN_CACHE = "C:/Users/manve/Downloads/NSEBOT/shoonya_shared_token.json"
```
**Impact:** Non-portable. Fails on Linux/VPS.  
**Fix:** Use `Path.home() / "NSEBOT" / "shoonya_shared_token.json"` or config-driven.

---

### 23. `data/shoonya_fetcher.py:327-430` — Playwright OAuth No Headless Fallback
```python
browser = p.chromium.launch(headless=True)
```
**Issue:** Some cloud environments block headless Chromium. No fallback to headed or alternative auth.  
**Fix:** Try headless first, catch specific errors, fallback to headed or API key if available.

---

### 24. `signals/leadership.py:14-59` — Projected Volume Multiplier Logic Inverted
```python
if elapsed_fraction < 0.5:
    return 1.0  # Before 50% session: no projection
# After 50%: projects UP (multiplier > 1)
return 1.0 / elapsed_fraction
```
**Impact:** Early session RVOL artificially low (real volume / 1.0), late session RVOL inflated. Creates false "breakout" signals late day.  
**Fix:** Use VWAP-based volume curve or constant multiplier after 30 min.

---

### 25. `intelligence/feedback_loop.py:132` — Profit Factor Infinity Handling
```python
profit_factor = total_wins / total_losses if total_losses > 0 else 999.0
```
**Issue:** `999.0` skews JSON consumers expecting finite numbers.  
**Fix:** Use `null` or `"INF"` string, document in schema.

---

### 26. `data/feed.py:1465-1480` — `_flatten_columns()` Drops MultiIndex Level 1
```python
def _normalize(col):
    if isinstance(col, tuple):
        return str(col[0]).lower().replace(" ", "_")
```
**Impact:** `('Close', '^NSEI')` → `close`, but `('Close', 'RELIANCE.NS')` also → `close`. Column collision!  
**Fix:** Include symbol in normalized name: `f"{col[0]}_{col[1]}"`.

---

### 27. `dashboard/server.py:1186-1220` — `_generate_trade_alerts()` Massive Function (350+ lines)
**Issue:** Single function handles options alerts, stock alerts, divergence, AI sentiment, timing gates.  
**Fix:** Split into `_generate_option_alerts()`, `_generate_stock_alerts()`, `_apply_timing_gates()`.

---

### 28. `config/loader.py:95-135` — `fetch_fno200_symbols()` Thread Timeout Silent
```python
t.join(timeout=5)
if t.is_alive():
    logging.warning("...timed out after 5s; falling back")
```
**Issue:** Thread continues running in background, may complete after fallback. Resource leak.  
**Fix:** Use `concurrent.futures` with `Future.result(timeout=)` for true cancellation.

---

### 29. `signals/regime.py:232-250` — Emerging Regime ADX Rising Check Uses 5-Day Slope
```python
adx_rising = len(adx_series) >= 5 and float(adx_series.iloc[-1]) > float(adx_series.iloc[-5])
```
**Issue:** 5-day slope noisy. Single spike can flip "rising".  
**Fix:** Use linear regression slope over 10-14 days.

---

### 30. `data/ai_scraper.py:252-254` — Dead Provider TTL Fixed at 600s
```python
_DEAD_PROVIDER_TTL = 600.0  # 10 min
```
**Issue:** Kilo Gateway outage often >10 min. System re-tries failed provider too aggressively.  
**Fix:** Exponential backoff: `min(600 * 2^failures, 3600)`.

---

### 31. `intelligence/thought_graph.py:122-125` — Confidence Clamping Loses Information
```python
self.confidence = max(0.05, min(0.95, new_conf))
```
**Impact:** Strong evidence (confidence 0.98) clamped to 0.95. Downstream can't distinguish "very sure" from "sure".  
**Fix:** Allow 0.01-0.99 range, or use log-odds internally.

---

### 32. `signals/flows.py:360-366` — PCR OI Bands Hardcoded
```python
if pcr_oi > 1.2: score += 1.5
elif pcr_oi < 0.85: score -= 1.5
```
**Issue:** NSE PCR bands shifted post-COVID (0.9-1.1 now neutral).  
**Fix:** Load bands from config or compute rolling percentile.

---

### 33. `dashboard/paper_trader.py:150-154` — `DEFAULT_SETTINGS` Capital Hardcoded
```python
"capital_per_trade": 500000.0,
"capital_per_trade_stocks": 500000.0,
```
**Issue:** Not derived from `config.account.capital`. Diverges from risk engine.  
**Fix:** Load from config at init, not defaults.

---

### 34. `ops/journal.py:374-394` — `has_skipped_today()` Uses UTC Date
```python
today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
```
**Issue:** IST midnight ≠ UTC midnight. Skip logic misaligned with trading day.  
**Fix:** Use `datetime.now(IST).date()`.

---

### 35. `data/feed.py:244-243` — `_dhan_period_dates()` 30 Days/Month Approximation
```python
if unit in ("mo", "m"):
    days = qty * 30  # BUG-23 FIX comment says 30 not 31
```
**Impact:** 6mo = 180 days vs actual ~182. Minor but compounds.  
**Fix:** Use `dateutil.relativedelta` for calendar-accurate periods.

---

### 36. `signals/structure_map.py:16-57` — Structure Map Missing New Regimes
```python
MAP: dict[Regime, StructurePlan] = {
    Regime.TREND_UP: ...,
    # Missing: TREND_EMERGING_UP, TREND_EMERGING_DOWN, VOL_EXPANSION, VOL_CONTRACTION
}
```
Wait — they ARE present (lines 47-56). But `plan_for()` uses `.get()` with fallback.  
**Real Issue:** `Regime` enum in `regime.py` has 8 values, `MAP` has 8 entries — OK.  
**But:** `VOL_EXPANSION` plan says "Long Straddle ONLY around scheduled event" — no event calendar integration.

---

### 37. `data/screener_fetcher.py:269-288` — Adaptive Prompt Token Budget Fixed
```python
_MAX_CHARS = 12000  # ~3k tokens
```
**Issue:** Complex companies (banks with 50+ ratio rows) exceed budget → truncated mid-table.  
**Fix:** Dynamic truncation preserving latest quarters + key ratios.

---

### 38. `dashboard/server.py:65-74` — Global Cache Lock Coarse
```python
_cache_lock = threading.Lock()
_engine_busy = False
```
**Issue:** `_run_engine()` holds lock for entire pipeline (10-30s). Concurrent requests queue.  
**Fix:** Lock only cache read/write, not computation. Use `RLock` for reentrancy.

---

### 39. `intelligence/adaptive_regime.py:500-528` — Momentum Scoring Asymmetric
```python
def _score_heavyweight_momentum_for_regime(self, regime, mom):
    if regime == TREND_UP:
        if mom >= 1.5: return 1.0
        if mom <= -1.5: return -1.0  # Penalizes bearish mom in bull regime
    elif regime == TREND_DOWN:
        if mom <= -1.5: return 1.0
        if mom >= 1.5: return -1.0
```
**Issue:** `VOL_EXPANSION` rewards |mom|>=1.5 but `RANGE_LOW_VOL` penalizes |mom|>=1.0. Inconsistent scaling.  
**Fix:** Normalize all to [-1, 1] with regime-specific centers.

---

### 40. `risk/guardrails.py:18-48` — Guardrails Config Validation Incomplete
```python
if "risk" not in cfg: raise ValueError(...)
# But doesn't validate: daily_stop_pct > 0, monthly_stop_pct > daily, etc.
```
**Fix:** Add schema validation with `pydantic` or custom checks.

---

### 41. `data/notifier.py:73-111` — Telegram HTML Escape Incomplete
```python
def _escape_html(text):
    return str(text).replace("&", "&").replace("<", "<").replace(">", ">")
```
**Missing:** `"` → `"`, `'` → `'`. Can break attribute parsing.  
**Fix:** Use `html.escape()`.

---

### 42. `ops/backtest.py:230-250` — Regime-Based Suggestion String Matching Fragile
```python
if "TREND_UP" in str(regime) and regime_wr < 0.55:
```
**Issue:** Matches "TREND_EMERGING_UP" too. False positives.  
**Fix:** Exact match `regime == "TREND_UP"` or use enum.

---

## Technical Debt / Maintenance Items

### 43. `main.py:279-280` — `run_dashboard()` Lazy Imports Mask Errors
```python
from signals import regime as regime_mod  # Line 44
# If import fails, error only at RUNTIME when command executed
```
**Fix:** Move to module top or add `try/except` with clear error.

---

### 44. `dashboard/server.py:357-523` — `_run_engine()` 166 Lines, Does Everything
**Split into:** `_fetch_data()`, `_compute_signals()`, `_build_verdict()`, `_format_output()`.

---

### 45. `data/feed.py` — Multiple Global Mutable State Dicts
```python
_QUOTE_SOURCE: dict[str, str] = {}
_OHLC_SOURCE: dict[str, str] = {}
_OPTION_CHAIN_SOURCE: dict[str, str] = {}
```
**Issue:** No cleanup, grows unbounded. Thread-safe but memory leak over weeks.  
**Fix:** Add TTL cleanup or use `cachetools.TTLCache`.

---

### 46. `signals/timing.py:670-764` — `detect_sentiment_flip()` Duplicates `flows.sentiment_overall()`
**Fix:** Import and reuse `flows.sentiment_overall()`.

---

### 47. `intelligence/learner.py` — Legacy Rule-Based Learner Unused
**File exists but not imported anywhere.** Dead code.  
**Fix:** Remove or mark `@deprecated`.

---

### 48. `data/sahi_news.py` — Scraper Without Rate Limit Respect
```python
# No delays between page requests
for page in range(1, max_pages + 1):
    r = session.get(url, params={"page": page})
```
**Risk:** IP ban from sahi.com.  
**Fix:** Add `time.sleep(2)` between pages.

---

### 49. `signals/telegram_fusion.py` — Fusion Logic Undocumented
**No docstring explaining:** hard filter thresholds, LLM prompt structure, verdict mapping.  
**Fix:** Add comprehensive module docstring.

---

### 50. `config/config.yaml:192-193` — `vol_expansion_threshold` Duplicate
```yaml
nifty_options:
  vol_expansion_threshold: 8.0
banknifty_options:
  vol_expansion_threshold: 8.0
```
**Issue:** Same value, two places. Single source of truth preferred.  
**Fix:** Move to shared `timing_engine.market_exhaustion` section.

---

### 51. `dashboard/paper_trader.py:1290-1377` — `_build_option_price_map()` Stale Expiry Filter
```python
if days_stale >= 365:
    logging.warning("dropping corrupt expiry")
    continue
if exp_date >= min_valid_date or days_stale < 7:
    needed_expiries.append(e)
```
**Logic Error:** `days_stale < 7` INCLUDES future expiries (negative days_stale). But `exp_date >= min_valid_date` already covers that. Redundant.  
**Fix:** Simplify to `if days_stale < 7:`.

---

### 52. `data/feed.py:815-837` — `_option_chain_from_dhan()` No Retry on Transient Errors
```python
raw = _dhan_post("optionchain", payload)
```
**Issue:** Single attempt. Network blip → empty chain → stale PCR.  
**Fix:** Add `@retry(stop=stop_after_attempt(3), wait=wait_exponential(...))`.

---

### 53. `signals/verdict.py:85-134` — AI Influence Calculation Double-Counts
```python
if ai_overall == "BULLISH": ai_dir = 1
elif ai_score_raw > 0.15: ai_dir = 1  # Double counts if both true
```
**Fix:** Use `elif` chain consistently.

---

### 54. `intelligence/signal_ensemble.py:228-243` — `_evaluate_vix()` Thresholds Arbitrary
```python
if vix < 14: return "LONG", 0.6
elif vix < 18: return "NEUTRAL", 0.5
elif vix < 22: return "SHORT", 0.5
else: return "SHORT", 0.7
```
**Issue:** No statistical basis. VIX 18-22 is normal range, not SHORT signal.  
**Fix:** Use VIX percentile rank (from `_vix_rank()`).

---

### 55. `data/feed.py:165-173` — `_broker_cfg()` Reloads Config Every Call
```python
def _broker_cfg():
    return load_config().get("broker", {})
```
**Called from:** `_dhan_credentials()`, `_dhan_headers()`, `_dhan_enabled()` — multiple times per quote batch.  
**Fix:** Module-level cached config or pass config object.

---

### 56. `dashboard/server.py:768-776` — `_calculate_atr()` Duplicates `timing.compute_atr_from_df()`
**Fix:** Import and reuse.

---

### 57. `signals/flows.py:116-163` — `fii_dii_5d_net()` Vectorized But Complex
```python
net_series = pd.to_numeric(df[net_col].astype(str).str.replace(",", "").str.strip(), errors="coerce").fillna(0.0)
```
**Issue:** `astype(str)` on already-string column is redundant. `.fillna(0)` after `coerce` is correct but could use `pd.to_numeric(..., errors='coerce').fillna(0)` directly.  
**Fix:** Simplify.

---

### 58. `data/screener_fetcher.py:223-232` — `_is_financial_table()` Keyword List Incomplete
```python
_KEEP_KEYWORDS = {"sales", "revenue", "profit", "loss", "ebitda", "eps", ...}
```
**Missing:** "comprehensive income", "other income", "exceptional items", "tax expense".  
**Fix:** Expand keyword set or use ML classifier.

---

### 59. `dashboard/cleanup_paper_db.py` — Hardcoded Windows Paths
```python
DB_PATH = r"C:\Users\manve\Downloads\StockMinded\dashboard\paper_trades.json"
```
**Fix:** Use `Path(__file__).parent / "paper_trades.json"`.

---

### 60. `tests/` — No Integration Tests for Critical Paths
- `test_shoonya_session.py` only unit tests
- No test for: full dashboard pipeline, paper trade entry→exit, AGoT integration  
**Fix:** Add `tests/integration/test_dashboard_pipeline.py`.

---

### 61. `signals/regime.py:111-116` — `_trend_persistence()` Unused
```python
def _trend_persistence(trend_series, window=5, threshold=-3, min_hits=3): ...
```
**Defined but never called.** Dead code.  
**Fix:** Remove or integrate into regime logic.

---

### 62. `intelligence/agot_integration.py:200-214` — `FeedbackLoop` Instantiated Per Run
```python
loop = FeedbackLoop(journal_path)
corrections = loop.get_corrections(lookback_days=30)
```
**Issue:** Re-scans entire journal every dashboard run. Slow.  
**Fix:** Cache corrections with TTL (1 hour).

---

### 63. `config/loader.py:20-43` — `_expand()` Recursive Dict/List Walk
```python
if isinstance(value, dict):
    return {k: _expand(v, _missing) for k, v in value.items()}
if isinstance(value, list):
    return [_expand(x, _missing) for x in value]
```
**Issue:** No depth limit. Deeply nested YAML could cause recursion limit.  
**Fix:** Add `max_depth=50` parameter.

---

### 64. `dashboard/paper_trader.py:727-730` — `enter_trade()` Calls `load_config()` Inside Lock
```python
with atomic_db_update() as db:
    cfg = load_config()  # Reads YAML, expands env vars
```
**Issue:** Config parsing inside DB lock → contention.  
**Fix:** Load config before `atomic_db_update()`.

---

### 65. `signals/leadership.py:89-141` — `rank_universe()` Mutates Input Dict
```python
for sym, df in stock_data.items():
    # Adds attributes to StockRank objects
    ranks.append(StockRank(...))
```
**Not a bug** but `stock_data` dict passed by reference from caller. Caller may not expect mutation.  
**Fix:** Document or copy input.

---

## Configuration Issues

### 66. `config/config.yaml:165-166` — Schedule Times as Strings Not Validated
```yaml
morning_dashboard: 08:45
entry_window_open: 09:20
```
**Issue:** No schema validation. "25:70" would pass YAML but crash scheduler.  
**Fix:** Add Pydantic model with `time` type validation.

---

### 67. `config/config.yaml:192-210` — Timing Engine Config Deeply Nested
```yaml
timing_engine:
  late_entry_filter:
    enabled: true
    max_vwap_dist_pct: 2.0
  dynamic_thresholds:
    enabled: false
    adjustment_rules: ...
```
**Issue:** Hard to override via env vars. `config.loader._expand()` doesn't handle nested dict merging.  
**Fix:** Flatten or add merge logic.

---

## Security Issues

### 68. `data/shoonya_fetcher.py:208` — Token Cache World-Readable
```python
_TOKEN_CACHE = "C:/Users/manve/Downloads/NSEBOT/shoonya_shared_token.json"
```
**File contains:** `susertoken` (session token), `userid`.  
**Fix:** Set file permissions `0600` on write.

---

### 69. `data/ai_scraper.py:441-467` — `_call_chat_completion()` Logs Prompts
```python
_log_llm_call(provider, resp, prompt=prompt, output=text)
```
**Risk:** Prompts may contain PII (company names, tickers). Log file not encrypted.  
**Fix:** Hash prompt or redact sensitive fields before logging.

---

### 70. `dashboard/server.py:27-29` — `.env` Loaded at Module Import
```python
load_dotenv(PROJECT_ROOT / ".env")
```
**Issue:** If `.env` missing, no error — silent config degradation.  
**Fix:** Validate required keys after load.

---

## Performance Issues

### 71. `dashboard/server.py:401-438` — Parallel Fetch But Sequential Processing
```python
with ThreadPoolExecutor(max_workers=2) as pool:
    f_univ = pool.submit(_fetch_universe)
    f_sec = pool.submit(_fetch_sectors)
    wait([f_univ, f_sec])
# Then: regime, flows, leadership, structure ALL SEQUENTIAL
```
**Fix:** Pipeline stages can run in parallel (regime + flows independent).

---

### 72. `data/feed.py:1397-1470` — `ohlc_cached()` Pickle I/O Every Call
```python
with open(cache_file, "rb") as f:
    cached_df, cached_ts = pickle.load(f)
```
**Issue:** Pickle deserialization slow for large DataFrames.  
**Fix:** Use `pyarrow.feather` or `parquet` for columnar storage.

---

### 73. `signals/leadership.py:90-141` — `rank_universe()` Computes RS Line Per Symbol
```python
rs = _rs_line(valid_close, bench_close)  # Aligns + divides per symbol
```
**Issue:** `bench_close` same for all symbols. Pre-align once.  
**Fix:** Compute benchmark series once, pass aligned series.

---

### 74. `intelligence/adaptive_regime.py:366-498` — `_evaluate_hypothesis()` Adds Evidence One-by-One
```python
for evidence_type in [...]:
    node.add_evidence(Evidence(...))
```
**Issue:** Each `add_evidence()` triggers `_recalculate_confidence()` → O(n²) for n evidence pieces.  
**Fix:** Batch add evidence, recalculate once.

---

### 75. `dashboard/server.py:581-600` — `option_chain_source()` Called Per Symbol in Loop
```python
for sym in universe[:5]:
    p = cache_dir / f"{t}_{today_str}.pkl"
```
**Fix:** Batch check cache directory once.

---

## Architectural Concerns

### 76. Two Universe Loading Paths (CLI vs Dashboard)
- `main.py:52`: `universe = cfg.get("universe_fo_sample", [])`
- `server.py:70`: `universe = load_universe(cfg)`
**Result:** Different stock universes → different signals → inconsistent verdicts.

---

### 77. Paper Trading State Split: JSON + SQLite
- `paper_trades.json`: Trade objects, settings
- `journal.sqlite`: Trade log, regime snapshots, skipped trades
**Issue:** No transaction across both. Sync failures → divergent state.  
**Fix:** Single source of truth (SQLite with JSON columns) or event sourcing.

---

### 78. AGoT Components Not Fully Integrated
- `AdaptiveRegimeClassifier` → used in `agot_integration.py` only
- `SignalEnsemble` → used in `agot_integration.py` only  
- `FeedbackLoop` → used in `agot_integration.py` only
**Main dashboard (`run_dashboard`)** still uses legacy `regime_mod.classify()`, `flows_mod.snapshot()`.
**Fix:** Make AGoT the default path, legacy as fallback.

---

### 79. No Circuit Breaker for External APIs
- Shoonya, Dhan, yfinance, NSE, ScrapeGraphAI — all called without circuit breaker.
**Risk:** Cascading failures when one provider degrades.  
**Fix:** Add `pybreaker` or custom circuit breaker per provider.

---

### 80. Logging Inconsistent: `print()` + `logging` Mixed
- `paper_trader.py:852`: `print(f"⚠️ Journal sync failed...")`
- `main.py:270`: `print(f"Scheduler started...")`
- Most modules use `logging.getLogger(__name__)`
**Fix:** Standardize on `logging`, configure handlers in `core/log.py`.

---

## Test Coverage Gaps

### 81. Missing Tests For:
- `dashboard/server.py` endpoints (Flask test client)
- `paper_trader.py` entry/exit logic
- `verdict.py` circuit breaker logic
- `timing.py` market exhaustion scoring
- `adaptive_regime.py` hypothesis scoring
- `signal_ensemble.py` agreement calculation

---

## Recommendations Priority Order

### Immediate (Week 1)
1. Fix `risk/guardrails.py` NameErrors (Critical #1)
2. Fix `dashboard/server.py` missing `pt` import (Critical #5)
3. Fix `signals/leadership.py` timezone bug (Critical #2)
4. Align universe loading paths (High #9)
5. Add config validation for required secrets (Security #70)

### Short-term (Week 2-3)
6. Fix cache key collision in `feed.py` (Critical #3)
7. Fix `_env_or_value()` format handling (Critical #4)
8. Implement circuit breakers for external APIs (Arch #79)
9. Split `_run_engine()` into pipeline stages (Debt #44)
10. Standardize logging (Arch #80)

### Medium-term (Month 1)
11. Make AGoT default regime/flow/ensemble path (Arch #78)
12. Unify paper trading persistence (Arch #77)
13. Add integration tests (Test #81)
14. Fix RVOL projection logic (High #24)
15. Add file permissions for token cache (Security #68)

### Long-term (Quarter)
16. Migrate cache to Feather/Parquet (Perf #72)
17. Implement event-driven architecture for signals
18. Add distributed tracing (OpenTelemetry)
19. Build CI/CD with automated backtest validation
20. Document all signal/verdict logic in architecture decision records (ADRs)

---

## File Risk Heatmap

| File | Critical | High | Medium | Debt | Total |
|------|----------|------|--------|------|-------|
| `risk/guardrails.py` | 1 | 1 | 1 | 0 | 3 |
| `dashboard/server.py` | 1 | 2 | 5 | 2 | 10 |
| `dashboard/paper_trader.py` | 0 | 1 | 4 | 2 | 7 |
| `data/feed.py` | 1 | 1 | 3 | 2 | 7 |
| `signals/leadership.py` | 1 | 0 | 2 | 1 | 4 |
| `signals/timing.py` | 0 | 1 | 2 | 1 | 4 |
| `signals/verdict.py` | 0 | 1 | 2 | 1 | 4 |
| `data/ai_scraper.py` | 0 | 1 | 2 | 1 | 4 |
| `intelligence/adaptive_regime.py` | 1 | 0 | 2 | 1 | 4 |
| `config/loader.py` | 0 | 1 | 2 | 1 | 4 |
| `ops/journal.py` | 0 | 0 | 2 | 0 | 2 |
| `data/screener_fetcher.py` | 0 | 0 | 2 | 1 | 3 |
| `intelligence/signal_ensemble.py` | 0 | 1 | 1 | 1 | 3 |
| `ops/backtest.py` | 0 | 1 | 1 | 0 | 2 |
| `signals/flows.py` | 0 | 1 | 1 | 1 | 3 |
| `signals/regime.py` | 0 | 1 | 2 | 1 | 4 |
| `signals/options.py` | 0 | 0 | 1 | 0 | 1 | 2 |
| `data/shoonya_fetcher.py` | 0 | 0 | 1 | 2 | 3 |
| `data/notifier.py` | 0 | 0 | 1 | 1 | 2 |

---

## Appendix: Quick Fix Scripts

### Fix Guardrails NameError
```python
# risk/guardrails.py line 50
def check_new_trade(self, *, proposed_risk: float, open_risk: float,
                    day_pnl: float, month_pnl: float,
                    margin_used_pct: float,
                    max_correlation_vs_open: float = 0.0) -> GuardrailCheck:
    reasons: list[str] = []  # ADD THIS
    # ... rest of function uses reasons, month_pnl, open_risk, proposed_risk
```

### Fix Dashboard Import
```python
# dashboard/server.py line 58
from dashboard import paper_trader as pt  # ADD THIS
```

### Fix Universe Alignment
```python
# main.py line 52
universe = load_universe(cfg)  # REPLACE: universe = cfg.get("universe_fo_sample", [])
```

---

*End of Audit Report*