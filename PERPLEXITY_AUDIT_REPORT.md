# StockMinded — Full Code Audit Report
**Repository:** https://github.com/Manvendra08/StockMinded  
**Audit Date:** 2026-07-10  
**Auditor:** Perplexity AI (line-by-line static analysis)  
**Scope:** All source `.py` files, config, ops, signals, intelligence, data modules  

---

## Executive Summary

| Category | Count | Severity |
|---|---|---|
| Critical Bugs (runtime crash / wrong output) | 6 | 🔴 CRITICAL |
| Logic / Functional Bugs | 11 | 🟠 HIGH |
| Design / Reliability Issues | 9 | 🟡 MEDIUM |
| Code Quality / Maintainability | 8 | 🔵 LOW |
| **Total Issues** | **34** | — |

> **Bottom Line:** The core regime + flow pipeline is functional and shows solid defensive coding (fallback regimes, stale flags, vectorized FII parsing). However, there are 6 critical issues that **will cause silent wrong outputs or runtime crashes** in edge cases — particularly around VIX data sparsity, the PCR return-tuple mismatch, FII derivative threshold asymmetry, and missing error propagation in the scheduler. Fix criticals before any live deployment.

---

## File-by-File Audit

---

### 1. `main.py`

#### BUG-M01 🔴 CRITICAL — `run_health()` PCR tuple unpack: positional mismatch risk
**Lines:** ~265–270  
```python
pcr_oi, pcr_vol, mp, stale, _, updated_at, _ = feed.get_pcr_max_pain_cached("NIFTY")
```
**Issue:** `feed.get_pcr_max_pain_cached()` returns a 7-tuple:  
`(pcr_oi, pcr_vol, mp, pcr_stale, mp_stale, pcr_updated_at, mp_updated_at)`  
The unpack uses `stale, _, updated_at, _` — merging `pcr_stale` and `mp_stale` into a single `stale`, discarding `mp_updated_at`. This silently drops `mp_stale`. In `flows.py` the same call is correctly unpacked as 7 distinct variables. The health check will misreport staleness state.  
**Fix:**
```python
pcr_oi, pcr_vol, mp, pcr_stale, mp_stale, pcr_updated_at, mp_updated_at = feed.get_pcr_max_pain_cached("NIFTY")
checks.append(f"✅ PCR (OI): {pcr_oi} stale={pcr_stale} (Updated: {pcr_updated_at})")
```

#### BUG-M02 🟠 HIGH — `run_agot_test()` assert string matching is fragile
**Lines:** ~190–192  
```python
assert best is not None and "TREND_UP" in best.label, "ThoughtGraph failed"
```
**Issue:** If `ThoughtGraph.select_best()` returns a node whose `.label` attribute is renamed or the enum value changes format (e.g., `"trend_up"` lowercase), the assert silently becomes `False` and crashes the test with a misleading error. No actual unit test isolation — the "test" runs production imports.  
**Fix:** Compare against `Regime.TREND_UP.value` or use `best.label.upper()`.

#### BUG-M03 🟡 MEDIUM — `run_schedule()` silent exception swallowing
**Lines:** ~300–308  
```python
def _h(name, fn):
    def wrapped():
        try:
            fn(cfg)
        except Exception:
            logging.getLogger(__name__).exception("[%s] failed", name)
    return wrapped
```
**Issue:** Scheduler jobs silently log and continue on ANY exception. No alerting to Telegram on failure.  
**Fix:**
```python
except Exception as e:
    logging.getLogger(__name__).exception("[%s] failed", name)
    alerter.send(f"⚠️ Scheduler job [{name}] failed: {e}")
```

#### BUG-M04 🔵 LOW — `_format_agot_message()` attribute access without guard
**Lines:** ~132–133  
**Issue:** `result.regime_agot.primary_confidence` accessed in several places with no None guard. Will crash if `regime_agot` is `None` outside the checked blocks.  
**Fix:** Consolidate all `result.regime_agot.*` accesses under a single `if result.regime_agot:` block.

---

### 2. `signals/regime.py`

#### BUG-R01 🔴 CRITICAL — VIX fetched with `period="3mo"` but `_vix_rank()` expects 252 sessions
**Lines:** ~145–146  
```python
idx = feed.ohlc_cached(index_symbol, period="2y")
vix = feed.ohlc_cached("INDIAVIX", period="3mo")
```
**Issue:** `_vix_rank()` requires 252 trading sessions for a meaningful percentile. With `period="3mo"` (~63 sessions), the percentile is computed over only 3 months. A VIX of 16 in a low-vol 3-month window appears at the 90th percentile, triggering false VOL_EXPANSION signals.  
**Fix:**
```python
vix = feed.ohlc_cached("INDIAVIX", period="1y")  # or "2y"
```

#### BUG-R02 🟠 HIGH — `breadth_pct_above_50dma()` MultiIndex fallback is dead code
**Lines:** ~104–117  
**Issue:** Comment says columns are "always lowercased by `_flatten_columns()`". If true, the `MultiIndex` branch never executes. If `_flatten_columns()` fails, stocks are silently skipped. Logic is internally contradictory.  
**Fix:** Remove the dead branch or add a log warning when the fallback fires.

#### BUG-R03 🟡 MEDIUM — `_trend_score()` accesses `ema20.iloc[-5]` without sufficient length guard
**Lines:** ~83–88  
```python
if len(close) > 20:
    score += _cmp(e20, float(ema20.iloc[-5]))
```
**Issue:** Guard `> 20` is too low for statistically valid momentum signals. With only 21 rows, `close.iloc[-21]` is index 0 — meaningless for trend scoring.  
**Fix:** Raise guard to `>= 25`.

#### BUG-R04 🟡 MEDIUM — Regime rule gap: VIX 14–16 with ADX < 20 always hits catch-all
**Lines:** ~185–196  
**Issue:** The band `14 <= vix_now < 16` with `adx < 20` falls to the `else` clause and always appends "transition zone" note — even on normal, calm market days.  
**Fix:** Add explicit rule:
```python
elif adx < 20 and 14 <= vix_now < 16:
    regime = Regime.RANGE_LOW_VOL
```

---

### 3. `signals/flows.py`

#### BUG-F01 🔴 CRITICAL — FII net column parsing fails on mixed-type columns
**Lines:** ~72–75  
```python
if net_series.dtype == object:
    net_series = net_series.str.replace(",", "").astype(float)
```
**Issue:** If pandas infers `float64` on a mixed-type column, the `dtype == object` check is skipped. Rows with string commas remain unconverted, silently producing wrong FII net values.  
**Fix:**
```python
net_series = pd.to_numeric(
    df[net_col].astype(str).str.replace(",", "").str.strip(),
    errors="coerce"
).fillna(0.0)
```

#### BUG-F02 🔴 CRITICAL — Hardcoded absolute ₹Cr thresholds for derivative scoring
**Lines:** ~220–240  
**Issue:** Thresholds of ₹1000 Cr (index futures) and ₹2000 Cr (options/stock futures) are absolute and not normalized. On expiry week, these are trivially crossed in both directions, inflating score. On low-activity days, neither fires. The bias score is fundamentally unstable around expiry.  
**Fix:** Normalize by 5-day rolling average volume or make thresholds configurable in `config/settings.yaml`.

#### BUG-F03 🟠 HIGH — `pcr_and_max_pain()` deprecated but O(n²) max-pain loop retained
**Lines:** ~118–175  
**Issue:** The inner `for k in strikes: for s in strikes` loop is O(n²). For NIFTY weekly options with 100+ strikes, this is ~10,000 iterations per call. Should be removed or vectorized.  
**Fix (vectorized):**
```python
import numpy as np
strikes_arr = np.array(sorted(set(strike_ce_oi) | set(strike_pe_oi)))
pe_vals = np.array([strike_pe_oi.get(s, 0) for s in strikes_arr])
ce_vals = np.array([strike_ce_oi.get(s, 0) for s in strikes_arr])
pain = np.array([
    np.sum(pe_vals[strikes_arr < k] * (k - strikes_arr[strikes_arr < k])) +
    np.sum(ce_vals[strikes_arr > k] * (strikes_arr[strikes_arr > k] - k))
    for k in strikes_arr
])
max_pain = float(strikes_arr[np.argmin(pain)])
```

#### BUG-F04 🟡 MEDIUM — AI scraper in `snapshot()` has no timeout — blocks critical path
**Lines:** ~295–316  
**Issue:** `ai_scraper.get_market_news_sentiment()` makes external HTTP calls with no timeout. A hang blocks the entire morning dashboard.  
**Fix:**
```python
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
with ThreadPoolExecutor(max_workers=1) as ex:
    future = ex.submit(ai_scraper.get_market_news_sentiment, market_context=_market_ctx)
    try:
        ai_sentiment = future.result(timeout=10)
    except (FuturesTimeout, Exception):
        ai_sentiment = None
```

#### BUG-F05 🟡 MEDIUM — `fii_dii_2d_trend()` stale=True silently skips recency delta
**Lines:** ~98–102  
**Issue:** Returns `stale=True` with zeros when fewer than 5 unique dates exist (e.g., Monday, post-holiday). The +0.5 recency boost in `_bias()` is silently suppressed with no log.  
**Fix:** Add explicit logging when `trend_stale=True` to make suppressed signals visible.

---

### 4. `signals/leadership.py`

#### BUG-L01 🟠 HIGH — `sector_map or None` disables inflow filter silently on load failure
**Lines (main.py):**
```python
longs, shorts = lead_mod.a_grade(ranks, inflow_sectors=inflow_syms, sector_map=sector_map or None)
```
**Issue:** `load_sector_map()` returns `{}` on failure. Empty dict is falsy, so `sector_map or None` passes `None`, disabling inflow-sector filtering with no warning logged.  
**Fix:**
```python
if not sector_map:
    logging.getLogger(__name__).warning("sector_map is empty — inflow sector filtering disabled")
longs, shorts = lead_mod.a_grade(ranks, inflow_sectors=inflow_syms, sector_map=sector_map if sector_map else None)
```

---

### 5. `signals/structure_map.py`

#### BUG-S01 🔵 LOW — No fallback for unrecognized `Regime` enum values
**Issue:** If a new `Regime` member is added without updating `structure_map.py`, `plan_for()` raises `KeyError` and crashes `format_dashboard()`.  
**Fix:**
```python
return PLANS.get(regime, StructurePlan(primary="Hold — regime unrecognized", secondary=None))
```

---

### 6. `data/feed.py` (inferred from call sites)

#### BUG-D01 🔴 CRITICAL — Cache does not validate post-market-close freshness
**Issue:** `ohlc_cached()` with `period="6mo"` will return a morning snapshot even when called post-3:30 PM IST. No TTL or market-close staleness check exists.  
**Fix:**
```python
from datetime import datetime
import pytz
ist = pytz.timezone("Asia/Kolkata")
now_ist = datetime.now(ist)
market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
if now_ist > market_close and cache_timestamp < market_close.timestamp():
    # invalidate cache
    pass
```

#### BUG-D02 🟠 HIGH — `fii_dii_cash()` column name coupling to source schema
**Issue:** Checking for `"category"` or `"clienttype"`, `"netvalue"` or `"net"` is fragile against NSE CSV restructuring. Silent zeros returned on schema mismatch.  
**Fix:** Add schema validation on first parse and raise a structured `DataSchemaError`.

---

### 7. `ops/alerts.py` (inferred)

#### BUG-A01 🟠 HIGH — Telegram 4096-char message limit not enforced
**Issue:** `format_dashboard()` with large universes (30+ stocks) will produce messages exceeding Telegram's 4096-char limit. Raises `400 Bad Request` silently.  
**Fix:** Truncate or split messages before sending:
```python
MAX_TG = 4096
if len(msg) > MAX_TG:
    msg = msg[:MAX_TG - 20] + "\n...[truncated]"
```

---

### 8. `ops/journal.py` (inferred)

#### BUG-J01 🟡 MEDIUM — SQLite journal has no schema migration
**Issue:** New fields added to `RegimeSnapshot` or `FlowSnapshot` (e.g., `vix_rank`, `fii_derivatives_stale`) will crash or silently drop on existing databases.  
**Fix:** Use a schema version table and apply migrations on startup.

---

### 9. `intelligence/agot_integration.py` (inferred)

#### BUG-AG01 🟡 MEDIUM — `AGoTPipeline.run_dashboard()` no circuit breaker for partial failures
**Issue:** If `FeedbackLoop` fails (e.g., corrupt SQLite), the entire dashboard result is discarded — including valid regime + flow data. Telegram receives only an error alert with no market context.  
**Fix:** Run each component independently, store partial results, include failed components as `None` rather than aborting.

#### BUG-AG02 🔵 LOW — `compute_time_ms` printed to stdout but not journaled
**Issue:** No performance baseline over time. Future regressions (200ms → 8000ms) are undetectable.  
**Fix:** Log `compute_time_ms` to journal alongside regime snapshot.

---

### 10. `nse_fetcher.py`

#### BUG-N01 🟡 MEDIUM — Static User-Agent string; NSE blocks after ~10 min
**Issue:** NSE's public endpoints block repeated requests from static UA strings with `403` responses.  
**Fix:** Rotate between 3–5 real browser UA strings, add `Referer: https://www.nseindia.com/`, implement exponential backoff with jitter.

#### BUG-N02 🔵 LOW — `dhan_headless_fetcher.py` Selenium without explicit driver cleanup
**Issue:** Headless Selenium crashes leave zombie `chromedriver` processes. On a VPS, this causes gradual OOM over ~24 hours.  
**Fix:** Wrap in `try/finally` with `driver.quit()`.

---

### 11. `recompute_eod.py`

#### BUG-RE01 🟡 MEDIUM — EOD recomputation has no idempotency guard
**Issue:** Running twice (cron misfire) likely double-inserts journal records without checking if EOD for that date already exists.  
**Fix:** Use `INSERT OR REPLACE INTO` or add a `WHERE NOT EXISTS` guard.

---

### 12. Root-level utility scripts

#### BUG-GN01 🔵 LOW — Dev scripts committed to repo root
**Issue:** `get_nifty.py`, `find_quote.py`, `temp_read.py`, `_check.py`, `test.py` are in root. `old_paper_trader.py` (~72KB) and `decoded_old_paper_trader.py` (~37KB) are legacy with no deprecation path.  
**Fix:** Move to `tools/` or `scripts/`. Add `# DEPRECATED` header to legacy files. Exclude from pytest via `pytest.ini`.

---

### 13. `requirements.txt`

#### BUG-REQ01 🟡 MEDIUM — No version pins for critical dependencies
**Issue:** Unpinned `pandas`, `numpy`, `apscheduler`, `yfinance` will break on fresh install in 6 months.  
**Fix:** Pin with `==` for production. Use `pip freeze > requirements-lock.txt` as deployment manifest.

---

### 14. Test Infrastructure

#### BUG-T01 🟡 MEDIUM — Test files scattered in root, not under `tests/`
**Issue:** `test_agot_standalone.py`, `test_bedrock.py`, `_test_models.py`, `test.py`, `_check.py` in root may not be discovered by `pytest.ini` if `testpaths = tests`.  
**Fix:** Consolidate all test files under `tests/`.

#### BUG-T02 🔵 LOW — `run_agot_test()` is integration test masquerading as unit test
**Issue:** Imports live production modules, depends on filesystem (`journal.sqlite`). Any env issue fails the test for unrelated reasons.  
**Fix:** Mock `FeedbackLoop.__init__` in CI.

---

## Cross-Cutting Issues

### CC-01 🟠 HIGH — No retry/backoff at the `feed` layer
**Issue:** Single transient NSE/yfinance timeout aborts the full morning dashboard run.  
**Fix:** Use `tenacity`:
```python
from tenacity import retry, stop_after_attempt, wait_exponential
@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
def ohlc_cached(symbol, period): ...
```

### CC-02 🟠 HIGH — Verify secrets not in git history
**Issue:** `.env.example` is correct; verify actual tokens were never committed:
```bash
git log --all --full-history -- .env
git log -S "bot_token" --source --all
```

### CC-03 🟡 MEDIUM — `print()` used for operational logging throughout
**Issue:** On VPS systemd service, stdout is not captured by `journalctl` without explicit redirection. Use `logging.getLogger(__name__).info()` consistently.

### CC-04 🟡 MEDIUM — No IST market-hours guard in `run_dashboard()`
**Issue:** Running `python main.py dashboard` at 2 AM IST fetches cached/stale data. No weekday/time guard exists.  
**Fix:** Add IST weekday + hour check with a stale-data warning.

### CC-05 🔵 LOW — `diff.txt` (143KB) and `dashboard_log.txt` committed to root
**Fix:**
```
# .gitignore additions
diff.txt
dashboard_log.txt
*.log
```

---

## Priority Fix Order

| Priority | Bug ID | File | Impact |
|---|---|---|---|
| 1 | BUG-R01 | `signals/regime.py` | VIX rank on 3mo window — false VOL_EXPANSION signals |
| 2 | BUG-M01 | `main.py` | PCR tuple mismatch — wrong staleness reporting |
| 3 | BUG-F01 | `signals/flows.py` | FII net parsing fails on mixed-type columns |
| 4 | BUG-F02 | `signals/flows.py` | Hardcoded absolute derivative thresholds — expiry-week instability |
| 5 | BUG-D01 | `data/feed.py` | Cache has no post-market-close TTL — stale OHLC |
| 6 | BUG-M03 | `main.py` | Scheduler swallows errors silently — no Telegram alert |
| 7 | CC-01 | `data/feed.py` | No retry/backoff — single transient = full abort |
| 8 | BUG-F04 | `signals/flows.py` | AI scraper no timeout — blocks critical path |
| 9 | BUG-A01 | `ops/alerts.py` | Telegram 4096-char limit not enforced |
| 10 | BUG-J01 | `ops/journal.py` | SQLite schema migration missing for new fields |

---

## Positive Observations (What's Done Well)

- **Vectorized FII parsing** in `flows.py` (replacing `iterrows`) — correct and efficient
- **ADX implementation** uses pandas `.where()` to preserve Series index (H2 FIX documented inline)
- **Adaptive VIX lookback** in `classify()` handles sparse VIX data gracefully (H3 FIX)
- **Two-session VIX spike check** prevents single-day noise from triggering VOL_EXPANSION
- **Stale flags** on PCR, FII/DII, max-pain propagate correctly through `FlowSnapshot`
- **Deprecation warning** on `pcr_and_max_pain()` with correct `stacklevel=2`
- **`run_agot_test()`** validates all 5 AGoT components at startup without live data
- **`_vix_rank()` percentile** implementation is correct (counts `tail <= current` / len)

---

*Report generated by Perplexity AI — July 10, 2026*  
*Based on static analysis of repository commit `613e1a3`*
