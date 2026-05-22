# Dashboard Data Sources

## Overview

The StockMinded dashboard integrates data from multiple sources with fallback chains. Here's which data comes from where:

---

## 1. OHLC Data (Daily & Intraday)

| Data Point | Primary Source | Fallback(s) | Cache |
|-----------|---|---|---|
| **Daily OHLC (Stocks & Indices)** | yfinance (fast, reliable) | nsepython (institutional) | `data/cache/ohlc/{symbol}_{date}.pkl` |
| **Intraday Candles (5m, 15m, 1h)** | yfinance (1D, 5m, 15m, 60m) | Research360 charts (limited) | In-memory + cache fallback |
| **NIFTY 50 Close** | yfinance (`^NSEI`) | nsepython | Cached pickle, auto-refreshed daily |
| **BANKNIFTY Close** | yfinance | nsepython | Cached pickle, auto-refreshed daily |
| **NIFTY 50 EMA (20, 50, 200)** | Calculated from NIFTY OHLC | — | Computed on-demand |
| **INDIA VIX** | yfinance (`^INDIAVIX`) | nsepython | Cached same as NIFTY |

**Note:** Dhan broker API is optional; system works without it. If Dhan disabled/unavailable, defaults to yfinance → nsepython chain.

**Cache Details:**
- Location: `data/cache/ohlc/`
- Format: Pickle (`.pkl`), day-scoped (one file per {symbol}_{YYYY-MM-DD}.pkl)
- Freshness: Dashboard checks age of first 10 universe symbols; flags as LIVE (<15 min), STALE (15-60 min), or OLD (>60 min)
- Persistence: Intentionally day-scoped; old files ignored (not auto-cleaned)

---

## 2. Flows: FII/DII, Sectors, PCR, Max-Pain

| Data Point | Primary Source | Fallback(s) | Update Frequency |
|-----------|---|---|---|
| **FII/DII (5-day net)** | `nsepython.nse_fiidii()` (NSE API via library) | NSE direct HTTPS (session-based) | Every 2 min (cached 10-day history) |
| **Sector OI/Returns** | yfinance for 11 NIFTY sector indices | nsepython sector data | Daily, cached 6-month |
| **PCR (Open Interest)** | Research360 (pre-computed) | nsepython | Computed from option chain |
| **PCR (Volume)** | Research360 (pre-computed) | nsepython | Computed from option chain |
| **Max Pain Level** | Research360 (pre-computed) | nsepython | Computed from option chain |
| **Option Chain** | **Research360** → **nsepython** → **NSE direct** → **AI Scraper** | Cached JSON | < 3 min cache (Research360), ~5 min staleness flag |
| **Option Chain Source** | Logged in `_OPTION_CHAIN_SOURCE` dict | — | Updated per fetch |
| **AI Sentiment** | `data/ai_scraper.py` → ScrapeGraphAI (SaaS) + Gemini fallback | — | On-demand (slow path) |

**Option Chain Fetch Order (Revised, no Dhan):**
1. **Research360** (no auth; PHP scrape; OI all strikes, LTP ~10 near-ATM)
2. **nsepython** (library wrapper; institutional data)
3. **NSE Direct** (robust session-based fetch; may fail during high load)
4. **AI Scraper** (ScrapeGraphAI SaaS/Local; very slow, resilience only)
5. **Local File** (CSV/JSON if configured)
6. **Cached JSON** (stale flag set if > 3–5 min old)

**Data Freshness Flags:**
- `pcr_stale`: True if PCR unavailable or uncomputed
- `mp_stale`: True if max-pain unavailable
- `fii_dii_stale`: True if NSE FII/DII fetch failed
- `option_source`: String indicating which source returned the option chain (e.g., `"dhan_optionchain"`, `"research360+dhan_ltp"`, `"cache:research360"`)

---

## 3. Regime: Trend, Volatility, Breadth

| Metric | Calculation | Data Source |
|--------|---|---|
| **Trend Score** | +1 for each: price > EMA20, EMA20 > EMA50, EMA50 > EMA200 (range: 0–3) | NIFTY OHLC (via `feed.ohlc_cached()`) |
| **ADX (14)** | Average Directional Index | NIFTY OHLC |
| **VIX** | Last close of India VIX | yfinance `^INDIAVIX` |
| **VIX 5-day % change** | (Today - 5d ago) / 5d ago | INDIAVIX history (6-month) |
| **Breadth (% above 50-DMA)** | Count of universe stocks above their 50-day MA | Universe OHLC (6-month for each stock) |
| **Regime Classification** | One of 6 states: TREND_UP, TREND_DOWN, RANGE_LOW_VOL, RANGE_HIGH_VOL, VOL_EXPANSION, VOL_CONTRACTION | Calculated from trend_score + VIX bands |

**See:** [signals/regime.py](../signals/regime.py)

---

## 4. Leadership: RS-Line Rankings, A-Grade Lists

| Data Point | Calculation | Data Source |
|-----------|---|---|
| **RS-Line (Relative Strength)** | stock_close / nifty_close | Universe OHLC (6-month) vs NIFTY OHLC |
| **RS Slope (20-day)** | Polyfit slope of RS-line (last 20 bars) normalized by mean | RS-line series |
| **% vs 50-DMA** | (Current price / 50-DMA) - 1, in % | Stock OHLC |
| **Quintile Scoring** | 1–5 based on RS-slope + %vs50dma thresholds (symmetric for long/short) | Derived metrics |
| **A-Grade Long List** | Top stocks: quintile=5 + above_50dma + slope>0 (or loose: quintile=5 only) | Ranked universe + inflow sectors |
| **A-Grade Short List** | Bottom stocks: quintile=5 + below_50dma + slope<0 (or loose: quintile=5 only) | Ranked universe + outflow sectors |

**See:** [signals/leadership.py](../signals/leadership.py)

---

## 5. Trade Structure & Verdict

| Data Point | Source | Logic |
|-----------|--------|-------|
| **Primary & Secondary Structures** | `signals/structure_map.py` | Maps 6-regime states to trade tactics (e.g., "trend-following long" for TREND_UP) |
| **Verdict** | `signals/verdict.py` | Combines all 4 signals (regime, flows, leadership, structure) into a confidence score & trade direction |

---

## 6. Paper Trading State

| Data Point | Storage | Format | Update |
|-----------|---|---|---|
| **Open/Closed Trades** | `dashboard/paper_trades.json` | JSON array of trade objects | Real-time on entry/exit |
| **Trade Legs (Options)** | `dashboard/paper_trades.json` | Nested under `option_trades[].legs[]` | Per-structure updates |
| **P&L Snapshots** | SQLite journal (`config.yaml:paths.journal_db`) | Trades table (optional historical log) | End-of-day |

---

## 7. Intraday Market Data (Real-time)

| Endpoint | Source | Response |
|----------|--------|----------|
| `/api/intraday?symbol=NIFTY&minutes=1` | Dhan intraday API | 1m, 5m, 15m OHLCV candles |
| `/api/option-chain?symbol=NIFTY` | Dhan / Research360 / NSE | Full option chain with OI, IV, LTP |
| `/api/dashboard` | Computed signals | Cached 2-minute window |

---

## 8. External Data Sources

### A. **Research360 (research360.in)** — PRIMARY OPTION CHAIN
- **What:** Option chain (all strikes OI + ~10 near-ATM LTPs), pre-computed PCR, max-pain
- **Auth:** None (public AJAX endpoints)
- **Cost:** Free
- **Reliability:** Moderate (subject to rate limits & site structure changes)
- **Caching:** ~5 min staleness check; fallback to Research360+nsepython enrichment
- **Replace Dhan with this** ✅

### B. **nsepython Library** — PRIMARY FALLBACK
- **What:** Wrapper around NSE website; FII/DII, daily OHLC, option chain, quotes
- **Auth:** None
- **Cost:** Free
- **Reliability:** High for institutional data; fragile when NSE site structure changes
- **Installation:** `pip install nsepython`
- **Usage:**
  ```python
  from nsepython import optionchain_data, nse_fiidii
  chain = optionchain_data('NIFTY', '31-05-2026')
  fiidii = nse_fiidii(10)  # 10 days
  ```

### C. **NSE Official API** — DIRECT FALLBACK
- **What:** Option chain, live quotes, intraday data
- **Auth:** None (public API, but requires session cookie handling)
- **Cost:** Free
- **Reliability:** Variable (often blocked during high load)
- **Session Management:** Warm-up via curl_cffi (JA3 fingerprinting) to bypass browser detection
- **Advantage over nsepython:** Faster, direct HTTPS (bypasses library bugs)

### D. **yfinance** — OHLC PRIMARY
- **What:** Daily OHLC for NSE stocks/indices (via Yahoo Finance)
- **Auth:** None
- **Cost:** Free
- **Reliability:** High for daily data; intraday limited to 1m candles
- **Symbol Mapping:** Configured in `data.feed.YF_SYMBOL` dict (e.g., `"NIFTY" → "^NSEI"`)
- **Installation:** `pip install yfinance`
- **Usage:**
  ```python
  import yfinance as yf
  ohlc = yf.download('NIFTY50.NS', period='6mo')
  intra = yf.download('NIFTY50.NS', period='7d', interval='1m')
  ```

### E. **ScrapeGraphAI (AI Scraper)** — LAST RESORT FALLBACK
- **What:** AI-powered web scraping fallback for option chain (if all else fails)
- **Auth:** `GOOGLE_API_KEY` for local Gemini fallback; `SCRAPEGRAPH_SAAS_API_KEY` for SaaS
- **Cost:** SaaS API has usage-based pricing; local Gemini requires API quota
- **Reliability:** Moderate (slower, but resilient to site changes)
- **Path:** `data/ai_scraper.py`

### F. **Dhan Broker API** — OPTIONAL (DEPRECATED FOR NO-COST SETUP)
- **What:** Real-time OHLC, option chain, market quotes, intraday charts
- **Auth:** `DHAN_API_KEY`, `DHAN_CLIENT_ID` (from `.env`)
- **Cost:** Included with broker account (may have request limits)
- **Reliability:** High (when session is valid)
- **Caching:** 3-min in-memory for option chains
- **Status:** System works without Dhan (falls back to yfinance + Research360 + nsepython)

---

## 9. Configuration & Secrets

| Setting | Location | Type | Required | Notes |
|---------|----------|------|----------|-------|
| `DHAN_API_KEY` | `.env` | String | **No** | Optional (intraday disabled if missing; daily OHLC via yfinance) |
| `DHAN_CLIENT_ID` | `.env` | String | **No** | Optional (same as above) |
| `GOOGLE_API_KEY` | `.env` + `config/config.yaml:scrapegraphai.api_key` | String | Optional | Optional (AI fallback only) |
| `SCRAPEGRAPH_SAAS_API_KEY` | `.env` + `config/config.yaml:scrapegraphai.saas_api_key` | String | Optional | Optional (AI SaaS fallback) |
| `TELEGRAM_BOT_TOKEN` | `.env` | String | Optional | Optional (alerts disabled if missing) |
| `TELEGRAM_CHAT_ID` | `.env` | String | Optional | Optional |
| `universe_source` | `config/config.yaml:universe_source` | String (`fno200` \| `sample`) | Default: `fno200` | — |
| `dhan.enabled` | `config/config.yaml:data_sources.dhan.enabled` | Boolean | Default: `false` | Set to `false` to skip Dhan entirely |

---

## 10. Data Flow Diagram (No Dhan Edition)

```
Dashboard (/api/dashboard)
    ├── _run_engine()
    │   ├── feed.universe_ohlc()  
    │   │   ├── yfinance         (primary - fast, free)
    │   │   └── nsepython        (fallback - institutional)
    │   │   └── cache (pkl)
    │   │
    │   ├── feed.sector_ohlc()
    │   │   └── yfinance (11 sectors)
    │   │
    │   ├── regime_mod.classify()
    │   │   └── NIFTY OHLC (yfinance) → trend_score, VIX (^INDIAVIX), ADX, breadth
    │   │
    │   ├── flows_mod.snapshot()
    │   │   ├── feed.fii_dii_cash()
    │   │   │   └── nsepython (NSE API)  
    │   │   │       └── NSE direct fallback
    │   │   │
    │   │   ├── sector_relative_strength()
    │   │   │   └── sector OHLC (yfinance)
    │   │   │
    │   │   └── feed.get_pcr_max_pain_cached()
    │   │       └── feed.option_chain()
    │   │           ├── Research360 (PHP scrape, no auth)
    │   │           ├── nsepython (library wrapper)
    │   │           ├── NSE direct (session-based)
    │   │           ├── AI Scraper (ScrapeGraphAI)
    │   │           └── cache (stale flag)
    │   │
    │   ├── lead_mod.rank_universe()
    │   │   └── RS-line (stock/NIFTY via yfinance) → quintile scoring
    │   │
    │   └── sm.plan_for(regime)
    │       └── Regime → structure (primary/secondary)
    │
    ├── Market Status (IST time, weekday check)
    │
    └── Cache check: last_age_max(cache files) for freshness flag
        └── LIVE (<15min) | STALE (15-60min) | OLD (>60min) | MISSING
```

---

## 11. Cache Locations & Refresh Rates

| Cache | Path | TTL | Format |
|-------|------|-----|--------|
| OHLC (daily) | `data/cache/ohlc/{sym}_{YYYY-MM-DD}.pkl` | End of day | Pickle |
| Option chain | `data/cache/option_chain_{SYM}.json` | 3–5 min | JSON |
| FII/DII | `data/cache/fii_dii_cache.json` | 10-day rolling | JSON |
| PCR/Max-Pain | `data/cache/pcr_mp_cache.json` | On fetch | JSON |
| AI Sentiment | `data/cache/ai_sentiment_cache.json` | On fetch | JSON |
| Dashboard | In-memory `_cache` dict | 2 min | Python dict |

---

## 12. Stale Data Handling

1. **Option Chain:** If all fetches fail, return cached JSON with `_cache.stale=True` flag.
2. **FII/DII:** If NSE fetch fails, return `{"fii": 0, "dii": 0}` with `fii_dii_stale=True`.
3. **PCR/Max-Pain:** If option chain is empty, return `(None, None, None, pcr_stale=True, mp_stale=True)`.
4. **Regime:** Always computes with available NIFTY data (falls back to yfinance if Dhan fails).
5. **Freshness Status:** Dashboard UI shows `LIVE`, `STALE`, `OLD`, or `MISSING` based on cache file ages.

---

## 13. Data Validation & Error Tracking

- **Source Errors Array:** Captured in `_cache["source_errors"]` and returned in `/api/dashboard`.
- **Stale Flags:** Each snapshot dataclass includes `_stale` boolean for missing data.
- **Option Source Logging:** `feed._OPTION_CHAIN_SOURCE` dict stores which source was used (e.g., `"research360+dhan_ltp"`).

---

## Summary Table

| Signal | Primary Data | Secondary Data | Tertiary Data |
|--------|---|---|---|
| **Regime** | NIFTY OHLC (yfinance) | INDIAVIX (yfinance) | Breadth (universe OHLC) |
| **Flows** | Option chain (Research360/nsepython) | FII/DII (nsepython) | Sector OHLC (yfinance) |
| **Leadership** | Universe OHLC (6mo, yfinance) | Sector returns (optional filter) | — |
| **Structure** | Regime state | — | — |
| **Paper Trades** | User actions (UI) | Real-time quotes (yfinance/nsepython) | Option chain (Research360 for exits) |

---

## Notes for Developers (No Dhan Edition)

1. **yfinance as OHLC primary:** Fast, reliable, no auth. Replaces Dhan daily OHLC.
2. **Research360 as option chain primary:** Free, no auth, all strikes OI. May lack LTPs for far strikes (enrichable with nsepython).
3. **nsepython as strong fallback:** Institutional-grade data but slower (~2-5 sec per call).
4. **AI Scraper only as last resort:** 10x–100x slower; use only if Research360 + nsepython both fail.
5. **Removed complexity:** No Dhan session management, no Dhan broker auth, simpler codebase.
6. **Timezone:** All IST times (UTC+5:30). Cron schedule in `config.yaml` uses IST.
7. **Session Management:** NSE and Research360 require persistent HTTP sessions with proper headers/cookies to avoid blocking.
8. **Data Cost:** Fully free tier possible (yfinance + Research360 + nsepython all have no-cost access).

---

## Migration Path (If Currently Using Dhan)

1. Remove or comment out all `_dhan_*` functions in [data/feed.py](../data/feed.py)
2. Modify `ohlc()` to use yfinance first, nsepython fallback
3. Modify `option_chain()` to start with Research360 (already secondary, move to primary)
4. Verify `fii_dii_cash()` uses nsepython first (already does)
5. Update [config/config.yaml](../config/config.yaml) to set `dhan.enabled=false`
6. Test `/api/dashboard` and `/api/intraday` endpoints
7. Monitor logs for data source fallback behavior
