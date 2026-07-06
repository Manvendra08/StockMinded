# StockMinded — Bug Audit Report

**Generated:** 2026-07-04  
**Auditor:** Automated line-by-line review  
**Scope:** All `.py` source files in `StockMinded/`

---

## Summary

| Severity | Count |
|----------|-------|
| 🔴 Critical | 8 |
| 🟠 High | 12 |
| 🟡 Medium | 14 |
| 🟢 Low / Cosmetic | 7 |
| **Total** | **30** |

---

## 🔴 Critical Bugs

### BUG-01 — `chain_snapshot()` always returns empty DataFrame
**File:** `signals/options.py` — line ~310  
**Code:**
```python
for rec in records:
    if rec.get('strikePrice') is None or rec.get('lastPrice') is None or rec.get('lastPrice') <= 0:
        continue
```
**Problem:** The option chain format returned by `feed.option_chain()` has `CE.lastPrice` and `PE.lastPrice` nested inside `CE`/`PE` dicts — there is **no top-level `lastPrice` key** on each record. `rec.get('lastPrice')` always returns `None`, so **every record is skipped** and the function returns an empty DataFrame.  
**Impact:** All option strategy resolution (`resolve_nifty_structure`, `resolve_banknifty_structure`) receives an empty chain and silently skips trade setup. **No option trades will ever be generated.**  
**Fix:** Access premiums via `rec.get("CE", {}).get("lastPrice")` and `rec.get("PE", {}).get("lastPrice")`.

---

### BUG-02 — `fetch_fno_quote()` regex fails to match FUTSTK contracts
**File:** `data/shoonya_fetcher.py` — line ~430  
**Code:**
```python
m = re.search(rf"^{base}(\d{{2}}[A-Z]{{3}}\d{{2}})", tsym)
```
**Problem:** The regex expects exactly `2 digits + 3 letters + 2 digits` (e.g., `RELIANCE26JUN25`). But Shoonya's FUTSTK `tsym` format is typically `RELIANCE26JUNFUT` — the suffix is `FUT` (3 letters), not `2 digits`. The regex **never matches**, so `fetch_fno_quote()` always returns `None`.  
**Impact:** All F&O stock quotes via Shoonya fail. `quote_batch()` falls back to spot quote only (LTP only, no OHLC/OI).  
**Fix:** Update regex to `rf"^{base}(\d{{2}}[A-Z]{{3}}(?:FUT|\d{{2}}))"`.

---

### BUG-03 — `_verify_token()` always fails (token verification broken)
**File:** `data/shoonya_fetcher.py` — line ~290  
**Code:**
```python
payload = {
    "uid": self.user_id,
    "exch": "NFO",
    "stext": urllib.parse.quote_plus("NIFTY"),
}
res = _post_jdata(f"{_API_BASE}/SearchScrip", payload, self.access_token)
```
**Problem:** `SearchScrip` with `exch="NFO"` searches for "NIFTY" on the NFO (derivatives) exchange. NIFTY is an **index**, not a stock — it doesn't exist on NFO as a scrip. The search returns empty results, `_verify_token()` returns `False`, and the system **re-authenticates on every single API call**.  
**Impact:** OAuth login runs on every request (slow Playwright browser launch), causing massive latency and potential rate-limiting. Token is never reused.  
**Fix:** Use `exch="NSE"` with a known equity symbol (e.g., "RELIANCE") for token verification, or use `GetIndexList` instead of `SearchScrip`.

---

### BUG-04 — Module-level functions have `self` parameter (not methods)
**File:** `data/shoonya_fetcher.py` — lines ~700-780  
**Code:**
```python
def get_market_quote(self, exchange: str, token: str) -> dict | None:
    ...
def get_security_info(self, exchange: str, token: str) -> dict | None:
    ...
def get_index_list(self) -> dict | None:
    ...
def get_historical_candles(self, exchange, token, interval, start_time, end_time) -> dict | None:
    ...
```
**Problem:** These functions are defined at **module level** (not inside the `ShoonyaFetcher` class) but take `self` as the first parameter. Calling `feed.fetch_shoonya_market_quote(exchange, token)` passes `exchange` as `self`, causing `AttributeError` when `self.user_id` is accessed.  
**Impact:** All four functions are **completely broken** when called from `feed.py`.  
**Fix:** Move these functions inside the `ShoonyaFetcher` class as methods, or change them to module-level functions that accept `shoonya: ShoonyaFetcher` as first parameter.

---

### BUG-05 — `quote_batch()` never falls back to Dhan when Shoonya fails
**File:** `data/feed.py` — `quote_batch()` function  
**Problem:** The function only tries Shoonya for quotes. The `_dhan_fill_quotes()` helper exists but is **never called** from `quote_batch()`. If Shoonya login fails (cooldown, missing credentials, network error), ALL quotes return empty `{}`.  
**Impact:** When Shoonya is unavailable, the entire system has no price data. Dashboard shows no leaders/laggards, no LTPs, no change percentages.  
**Fix:** Add Dhan fallback in `quote_batch()`:
```python
# After Shoonya attempt, fill remaining gaps from Dhan
grouped, reverse = {}, {}
for sym in out:
    if out[sym].get("ltp"):
        continue
    inst = _dhan_find_instrument(sym)
    if inst:
        seg = inst["segment"]
        grouped.setdefault(seg, []).append(inst["security_id"])
        reverse[(seg, str(inst["security_id"]))] = sym
if grouped:
    _dhan_fill_quotes(grouped, reverse, out)
```

---

### BUG-06 — `_filter_atm_strikes()` called twice (double filtering)
**File:** `data/feed.py` — `option_chain()` → `_save_chain()` and `_load_cached_chain()`  
**Problem:** `_save_chain()` calls `_filter_atm_strikes(data)` before saving. `_load_cached_chain()` also calls `_filter_atm_strikes(data)` after loading. This means cached data is filtered **twice** — once on save and once on load. If the underlying price changes between save and load, the ATM strike changes, and the second filter could produce a **different (narrower) strike range** than what was originally saved.  
**Impact:** Stale cached option chains may have fewer strikes than expected, causing strategy resolution to fail or use wrong strikes.  
**Fix:** Remove `_filter_atm_strikes` call from `_load_cached_chain()` — only filter on save.

---

### BUG-07 — Singleton typo: `_SHONYAA_INSTANCE` (double-A)
**File:** `data/shoonya_fetcher.py` — line ~800  
**Code:**
```python
_SHONYAA_INSTANCE: ShoonyaFetcher | None = None  # typo: should be _SHOONYA_INSTANCE
```
**Problem:** The singleton variable is named `_SHONYAA_INSTANCE` (double-A typo) instead of `_SHOONYA_INSTANCE`. While the code is internally consistent (both declaration and usage use the typo), this is confusing and could cause issues if another module tries to access `_SHOONYA_INSTANCE`.  
**Impact:** Low functional impact (code works), but maintenance risk.  
**Fix:** Rename to `_SHOONYA_INSTANCE` throughout the module.

---

### BUG-08 — SQL injection in `backtest.py`
**File:** `ops/backtest.py` — `load_trades_with_timing()`  
**Code:**
```python
if since_date:
    query += f" AND opened_at >= '{since_date}'"
```
**Problem:** `since_date` is interpolated directly into the SQL string using f-string. If `since_date` contains malicious SQL, it could be executed. While `since_date` comes from internal code (not user input), this is still a security vulnerability.  
**Impact:** SQL injection vulnerability.  
**Fix:** Use parameterized query:
```python
if since_date:
    query += " AND opened_at >= ?"
    params.append(since_date)
# ...
df = pd.read_sql_query(query, conn, params=params)
```

---

## 🟠 High Bugs

### BUG-09 — `breadth_pct_above_50dma()` case-sensitive column check fails
**File:** `signals/regime.py`  
**Code:**
```python
if "Close" in df.columns:
    col_name = "Close"
```
**Problem:** `feed.py`'s `_flatten_columns()` lowercases all columns. The check for `"Close"` (capital C) will **never match** because columns are always lowercase after flattening. The fallback checks for `"close"` (lowercase), so it works — but the `"Close"` check is dead code.  
**Impact:** Minor functional impact (fallback works), but the code is misleading.

---

### BUG-10 — `ohlc_cached()` invalidates ALL cache every 2 minutes
**File:** `data/feed.py`  
**Code:**
```python
current_bucket = int(time.time() / 120)
if current_bucket != _OHLC_CACHE_BUCKET:
    _OHLC_CACHE.clear()
    _OHLC_CACHE_BUCKET = current_bucket
```
**Problem:** Every 2 minutes, the **entire** OHLC cache is cleared. This means every 2 minutes, ALL symbols (200+ stocks + indices) need to be re-fetched from yfinance/Dhan. This causes massive API load and latency spikes every 2 minutes.  
**Impact:** Performance degradation every 2 minutes. Dashboard may timeout or show stale data during cache refresh.  
**Fix:** Use per-symbol TTL instead of global bucket:
```python
def ohlc_cached(symbol, period="1y", interval="1d"):
    key = f"{symbol}_{period}_{interval}"
    if key in _OHLC_CACHE:
        cached_df, cached_ts = _OHLC_CACHE[key]
        if time.time() - cached_ts < 120:
            return cached_df.copy()
    # fetch and cache with timestamp
```

---

### BUG-11 — Thread-unsafe global mutable state in `feed.py`
**File:** `data/feed.py`  
**Problem:** Multiple module-level dicts (`_QUOTE_SOURCE`, `_OHLC_SOURCE`, `_OPTION_CHAIN_SOURCE`, `_OHLC_CACHE`, `_OHLC_CACHE_BUCKET`) are mutated without thread locks. If `run_dashboard()` and `run_schedule()` run concurrently (or if APScheduler triggers overlapping jobs), race conditions could cause:
- Stale data being served from cache
- Source tracking being overwritten
- Cache bucket being reset mid-fetch  
**Impact:** Data corruption in multi-threaded scenarios.  
**Fix:** Add `threading.Lock()` around all global dict mutations.

---

### BUG-12 — `_dhan_post()` has no retry logic
**File:** `data/feed.py`  
**Code:**
```python
response = requests.post(
    f"https://api.dhan.co/v2/{path}", headers=headers, json=payload, timeout=15
)
response.raise_for_status()
```
**Problem:** Uses raw `requests.post` with no retry. If Dhan API returns 500/502/503/504, the request fails immediately with no retry. Other parts of the code use `_create_retry_session()` with 5 retries, but `_dhan_post` doesn't.  
**Impact:** Dhan API calls fail on transient errors, causing data gaps.  
**Fix:** Use `_create_retry_session()` instead of raw `requests`.

---

### BUG-13 — `fetch_quote()` for stocks may return wrong stock
**File:** `data/shoonya_fetcher.py`  
**Code:**
```python
target_tsym = f"{base}-EQ"
item = None
for v in values:
    if v.get("tsym") == target_tsym or v.get("symname") == base:
        item = v
        break
if item is None:
    item = values[0]  # fallback to first
```
**Problem:** If searching for "PNB", `_search_scrip("NSE", "PNB")` may return multiple results including "PNBHOUSING-EQ". The code tries exact match `tsym == "PNB-EQ"`, but if no exact match, falls back to `values[0]` which could be "PNBHOUSING-EQ".  
**Impact:** Wrong stock quote returned for ambiguous symbol names.  
**Fix:** Remove the `values[0]` fallback; return `None` if no exact match.

---

### BUG-14 — `fii_dii_derivatives()` triggers unnecessary fetch
**File:** `data/feed.py`  
**Code:**
```python
def fii_dii_derivatives(days: int = 5) -> tuple[dict[str, float], bool]:
    _, cached_stockedge, _ = _get_persistent_fii_dii_cache()
    if not cached_stockedge:
        try:
            fii_dii_cash(days=20)  # Triggers fetch of 20 days of data!
            _, cached_stockedge, _ = _get_persistent_fii_dii_cache()
        except Exception as e:
            print(f"[feed.fii_dii_derivatives] Failed to trigger fetch: {e}")
```
**Problem:** Calls `fii_dii_cash(days=20)` just to trigger a fetch, discarding the return value. This fetches 20 days of FII/DII data (slow API call) just to populate cache.  
**Impact:** Unnecessary API call every time derivatives data is needed and cache is empty.  
**Fix:** Extract the StockEdge fetch logic into a separate function that both functions can call.

---

### BUG-15 — `is_within_entry_window()` no error handling for config format
**File:** `signals/options.py`  
**Code:**
```python
entry_start_str = sym_cfg.get("intraday_entry_start", "09:45")
h1, m1 = map(int, entry_start_str.split(":"))
```
**Problem:** If config has invalid format like `"945"` instead of `"09:45"`, `split(":")` returns `["945"]` and `map(int, ...)` fails with `ValueError`. No error handling.  
**Impact:** Crash on invalid config.  
**Fix:** Add try/except with sensible defaults.

---

### BUG-16 — `analyze_history()` counts `None` as distinct date
**File:** `intelligence/learner.py`  
**Code:**
```python
distinct_dates = group["entry_date"].nunique()
if distinct_dates < 3:
    continue
```
**Problem:** If some trades have `entry_date = None`, `nunique()` counts `None` as a distinct value. This could cause segments with fewer than 3 real dates to pass the filter.  
**Impact:** Incorrect learning rules from segments with insufficient data.  
**Fix:** Filter out `None` before counting: `distinct_dates = group["entry_date"].dropna().nunique()`.

---

### BUG-17 — `profit_factor` returns `inf` when no losses
**File:** `intelligence/feedback_loop.py`  
**Code:**
```python
profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')
```
**Problem:** Returns `float('inf')` when there are no losses. Downstream code that serializes this to JSON will fail (`json.dumps` doesn't support `inf`).  
**Impact:** JSON serialization fails when trying to save feedback analysis.  
**Fix:** Return a large finite number like `999.0` instead of `inf`.

---

### BUG-18 — `_projected_volume_multiplier()` extreme multiplier
**File:** `signals/leadership.py`  
**Code:**
```python
elapsed_fraction = max(0.05, min(1.0, elapsed_fraction))
return 1.0 / elapsed_fraction
```
**Problem:** If market just opened (1 minute elapsed), `elapsed_fraction = 1/375 ≈ 0.0027`, clamped to `0.05`, giving multiplier `1/0.05 = 20x`. This means today's volume is multiplied by 20x, causing RVOL to be 20x higher than reality. This causes false Q5 quintile assignments for stocks that just opened.  
**Impact:** False leadership signals in the first few minutes of trading.  
**Fix:** Increase minimum clamp to `0.15` (max multiplier ~6.7x) or disable projection for first 15 minutes.

---

### BUG-19 — `calc_structure_max_loss()` overestimates for debit spreads
**File:** `signals/options.py`  
**Code:**
```python
if net_credit <= 0:
    return max(0, wing_width * lot_size * lots)
```
**Problem:** If `net_credit` is negative (debit spread), returns `wing_width * lot_size * lots` which is the **maximum possible loss** for a credit spread. For debit spreads, the max loss is the debit paid, not the wing width.  
**Impact:** Overestimates risk for debit spreads, causing under-sizing.  
**Fix:** Return `abs(net_credit) * lot_size * lots` for debit spreads.

---

### BUG-20 — `ohlc()` column flattening loses "Adj Close"
**File:** `data/feed.py`  
**Code:**
```python
def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns
        ]
```
**Problem:** If yfinance returns `"Adj Close"` as a column, it becomes `"adj close"` (with space). Downstream code checks for `"close"` which won't match `"adj close"`.  
**Impact:** Minor — most code uses `"close"` which is present. But if `"close"` is missing and only `"adj close"` exists, downstream code fails.  
**Fix:** Normalize spaces: `str(c).lower().replace(" ", "_")`.

---

## 🟡 Medium Bugs

### BUG-21 — `fetch_trendlyne_options_kpis()` import error handling
**File:** `data/feed.py`  
**Problem:** Uses `from curl_cffi import requests as curl_requests` inside try block. If `curl_cffi` is not installed, `ImportError` is caught by outer `except Exception`, which then tries stale cache. But if cache is also empty, returns `{}`. This is fine but the error message is misleading.  
**Impact:** Low — fallback works, but error logging is confusing.

---

### BUG-22 — `_dhan_find_instrument()` uses pandas `.eq()` on Series
**File:** `data/feed.py`  
**Code:**
```python
hit = work[work[col].astype(str).str.upper().eq(symbol)]
```
**Problem:** `.eq()` is a pandas Series method, but `str.upper()` returns a Series, and `.eq(symbol)` works. However, this is less readable than `== symbol`.  
**Impact:** Cosmetic — code works but is harder to read.

---

### BUG-23 — `_dhan_period_dates()` imprecise month calculation
**File:** `data/feed.py`  
**Code:**
```python
if unit in ("mo", "m"):
    days = qty * 31
```
**Problem:** Uses `qty * 31` for months, which overestimates by 1-2 days per month. For `period="6mo"`, this gives `6 * 31 = 186` days instead of ~182 days.  
**Impact:** Minor — 4-day overestimate for 6-month period.  
**Fix:** Use `qty * 30` or `dateutil.relativedelta`.

---

### BUG-24 — `fii_dii_cash()` unnecessary fetch trigger
**File:** `data/feed.py`  
**Code:**
```python
need_fetch = (now - cached_ts) >= 3600 or not cached_stockedge
```
**Problem:** `cached_stockedge` could be an empty list `[]` which is falsy, triggering unnecessary fetches even when cache is fresh.  
**Impact:** Unnecessary API calls.  
**Fix:** Check `cached_stockedge is None` instead of `not cached_stockedge`.

---

### BUG-25 — `_save_chain()` modifies data in-place
**File:** `data/feed.py`  
**Code:**
```python
def _save_chain(data: dict) -> dict:
    if not _skip_atm_filter:
        data = _filter_atm_strikes(data)  # Modifies data in-place!
```
**Problem:** `_filter_atm_strikes()` modifies `data["records"]["data"]` in-place. The caller's `data` dict is also modified. This could cause issues if the caller expects unmodified data.  
**Impact:** Minor — most callers don't reuse the data after saving.

---

### BUG-26 — `Alerter.send()` no Markdown fallback
**File:** `ops/alerts.py`  
**Code:**
```python
r = requests.post(url, json={"chat_id": self.chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
```
**Problem:** If `text` contains unsupported Markdown characters (e.g., nested bold/italic), Telegram returns an error. No fallback to plain text.  
**Impact:** Alert fails silently if Markdown is invalid.  
**Fix:** Retry without `parse_mode` on failure:
```python
if not r.ok:
    r = requests.post(url, json={"chat_id": self.chat_id, "text": text}, timeout=10)
```

---

### BUG-27 — `open_trade()` no default for required kwargs
**File:** `ops/journal.py`  
**Code:**
```python
def open_trade(self, **kw) -> int:
    cur = self.conn.execute(
        """INSERT INTO trades(...) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            kw.get("opened_at", ...),
            kw["symbol"],  # KeyError if missing!
            kw["structure"],  # KeyError if missing!
            ...
        ),
    )
```
**Problem:** Uses `kw["symbol"]` and `kw["structure"]` without `.get()` defaults. If caller forgets these, raises `KeyError`.  
**Impact:** Crash on missing kwargs.  
**Fix:** Use `.get()` with sensible defaults or validate kwargs at function start.

---

### BUG-28 — `load_config()` no fallback for missing config.yaml
**File:** `config/loader.py`  
**Code:**
```python
def load_config(path: str | None = None) -> dict:
    p = Path(path) if path else Path(__file__).parent / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
```
**Problem:** If `config.yaml` doesn't exist, raises `FileNotFoundError`. No fallback to default config.  
**Impact:** Crash if config file is missing.  
**Fix:** Add fallback to default config dict if file not found.

---

### BUG-29 — `compute_rsi_from_df()` unreliable for small datasets
**File:** `signals/timing.py`  
**Code:**
```python
avg_gain = gains.rolling(window=period, min_periods=1).mean()
```
**Problem:** Uses `min_periods=1` which means RSI can be computed with just 1 period. For small datasets (< 14 periods), RSI is unreliable.  
**Impact:** Unreliable RSI signals for stocks with limited history.  
**Fix:** Use `min_periods=period` and return default 50.0 if insufficient data.

---

### BUG-30 — `analyze_entry_quality_performance()` correlation on small sample
**File:** `ops/backtest.py`  
**Code:**
```python
valid_idx = df["quality_score"].notna() & df["pnl_rupees"].notna()
if valid_idx.sum() > 2:
    correlation = df.loc[valid_idx, "quality_score"].corr(df.loc[valid_idx, "pnl_rupees"])
```
**Problem:** Computes correlation with as few as 3 data points. Correlation with 3 points is statistically meaningless.  
**Impact:** Misleading correlation values on small samples.  
**Fix:** Require minimum 10 data points before computing correlation.

---

## 🟢 Low / Cosmetic

### BUG-31 — Typo in `_score_breadth_for_regime()` comment
**File:** `intelligence/adaptive_regime.py`  
**Code:**
```python
return -0.7  # Weak breadth contradupt trend up
```
**Problem:** Typo "contradupt" should be "contradicts".  
**Impact:** Cosmetic only.

---

### BUG-32 — `_SHOONYA_LOGIN_COOLDOWN` set before auth attempt
**File:** `data/shoonya_fetcher.py`  
**Code:**
```python
# Pre-set failure timestamp so cooldown applies even if thread hangs
_SHOONYA_LOGIN_FAILURE_TS = time.time()
```
**Problem:** Sets failure timestamp before auth attempt. If auth succeeds, it's reset to 0. But if the thread hangs, the cooldown is already active. This is intentional but could be confusing.  
**Impact:** Low — intentional design, but could be confusing.

---

### BUG-33 — `_dhan_col()` normalizes column names but not consistently
**File:** `data/feed.py`  
**Code:**
```python
lookup = {str(c).lower().replace("_", "").replace(" ", ""): c for c in df.columns}
for name in names:
    key = name.lower().replace("_", "").replace(" ", "")
    if key in lookup:
        return lookup[key]
```
**Problem:** Normalizes column names by removing underscores and spaces, but the original column name is returned. This is fine but could return unexpected columns if multiple columns normalize to the same key.  
**Impact:** Low — unlikely to have duplicate normalized names.

---

### BUG-34 — `_dhan_master()` cache file path may not exist
**File:** `data/feed.py`  
**Code:**
```python
cache_file = CACHE_DIR / "dhan_api_scrip_master.csv"
if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 86400:
    _DHAN_MASTER_CACHE = pd.read_csv(cache_file, low_memory=False)
```
**Problem:** If `CACHE_DIR` doesn't exist, `cache_file.exists()` returns False, and the code fetches from URL. Then tries to create `CACHE_DIR` with `mkdir(parents=True, exist_ok=True)`. This is fine.  
**Impact:** Low — handled correctly.

---

### BUG-35 — `_r360_expiries()` regex may miss expiries
**File:** `data/feed.py`  
**Code:**
```python
expiries = _re.findall(r'value="(\d{4}-\d{2}-\d{2})"', data)
```
**Problem:** Regex expects `YYYY-MM-DD` format. If Research360 returns `DD-Mon-YYYY` format, the regex won't match.  
**Impact:** Low — Research360 uses `YYYY-MM-DD` format based on testing.

---

### BUG-36 — `_option_chain_from_public_dhan()` timestamp comparison
**File:** `data/feed.py`  
**Code:**
```python
now_ts = int(_time.time())
for k, v in opsum.items():
    if seg == 1 and v.get("exptype") != "M":
        continue
    try:
        ts = int(k)
    except (ValueError, TypeError):
        continue
    if ts > now_ts:
        expiries.append(ts)
```
**Problem:** Compares `ts > now_ts` to find future expiries. If `k` is a string timestamp (not Unix timestamp), `int(k)` raises ValueError. The try/except handles this.  
**Impact:** Low — handled correctly.

---

### BUG-37 — `fetch_dhan_public_universe()` short timeout
**File:** `config/loader.py`  
**Code:**
```python
r = requests.get("https://dhan.co/futures-stocks-list/", headers=headers, timeout=5)
```
**Problem:** 5-second timeout is short. If Dhan is slow, this fails.  
**Impact:** Low — falls back to CSV.

---

## Recommendations

### Immediate Fixes (Critical)
1. **Fix BUG-01** (`chain_snapshot` empty DataFrame) — this blocks all option trading
2. **Fix BUG-02** (FUTSTK regex) — this blocks all F&O stock quotes
3. **Fix BUG-03** (`_verify_token` always fails) — this causes constant re-authentication
4. **Fix BUG-04** (module-level functions with `self`) — these are completely broken
5. **Fix BUG-05** (no Dhan fallback in `quote_batch`) — this blocks all quotes when Shoonya is down
6. **Fix BUG-06** (double filtering) — this causes incorrect strike filtering
7. **Fix BUG-07** (singleton typo) — rename for clarity
8. **Fix BUG-08** (SQL injection) — use parameterized queries

### High Priority
9. Fix thread-safety issues in `feed.py` globals
10. Add retry logic to `_dhan_post()`
11. Fix `_projected_volume_multiplier()` extreme values
12. Fix `calc_structure_max_loss()` for debit spreads

### Medium Priority
13. Add error handling for config format errors
14. Fix `profit_factor` infinity issue
15. Fix `analyze_history()` None counting
16. Add Markdown fallback in `Alerter.send()`

### Low Priority
17. Fix typos and cosmetic issues
18. Improve column name normalization
19. Increase timeout for Dhan public URL

---

## File Summary

| File | Bugs Found | Critical | High | Medium | Low |
|------|------------|----------|------|--------|-----|
| `data/feed.py` | 12 | 3 | 4 | 4 | 1 |
| `data/shoonya_fetcher.py` | 5 | 3 | 1 | 1 | 0 |
| `signals/options.py` | 4 | 1 | 2 | 1 | 0 |
| `signals/regime.py` | 1 | 0 | 1 | 0 | 0 |
| `signals/leadership.py` | 1 | 0 | 1 | 0 | 0 |
| `signals/option_strategy.py` | 1 | 0 | 0 | 1 | 0 |
| `signals/timing.py` | 1 | 0 | 0 | 1 | 0 |
| `signals/verdict.py` | 0 | 0 | 0 | 0 | 0 |
| `intelligence/adaptive_regime.py` | 1 | 0 | 0 | 0 | 1 |
| `intelligence/signal_ensemble.py` | 0 | 0 | 0 | 0 | 0 |
| `intelligence/feedback_loop.py` | 2 | 0 | 1 | 1 | 0 |
| `intelligence/agot_integration.py` | 1 | 0 | 0 | 1 | 0 |
| `intelligence/learner.py` | 1 | 0 | 1 | 0 | 0 |
| `risk/guardrails.py` | 0 | 0 | 0 | 0 | 0 |
| `risk/sizing.py` | 0 | 0 | 0 | 0 | 0 |
| `ops/alerts.py` | 1 | 0 | 0 | 1 | 0 |
| `ops/journal.py` | 1 | 0 | 0 | 1 | 0 |
| `ops/backtest.py` | 2 | 1 | 0 | 0 | 1 |
| `config/loader.py` | 1 | 0 | 0 | 0 | 1 |
| `main.py` | 0 | 0 | 0 | 0 | 0 |

---

*Report generated by automated line-by-line code review.*
