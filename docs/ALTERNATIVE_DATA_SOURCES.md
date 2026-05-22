# Alternative Data Sources (No Dhan)

## Problem Statement
Dhan broker API is unavailable or user lacks plan. Need reliable alternatives for:
1. Daily & intraday OHLC (stocks, indices, sectors)
2. Option chains (all strikes, OI, IV, LTP)
3. FII/DII flows
4. VIX / market breadth

---

## Research Summary

| Source | Type | OHLC | Option Chain | FII/DII | Intraday | Ease | Status |
|--------|------|------|--------------|---------|----------|------|--------|
| **NSE (nsepython)** | Institutional API | ✅ 9/10 | ✅ 9/10 | ✅ 8/10 | ❌ | ⭐⭐⭐⭐⭐ | **PRIMARY** |
| **yfinance** | Web scrape | ✅ 9/10 | ❌ | ❌ | ✅ Limited | ⭐⭐⭐⭐⭐ | **BACKUP OHLC** |
| **Research360** | PHP scrape | ⚠️ Limited | ✅ 7/10 | ❌ | ⚠️ Charts only | ⭐⭐⭐⭐ | **IN USE** |
| **Moneycontrol** | HTML scrape + JS | ⚠️ 7/10 | ⚠️ 6/10 | ✅ 9/10 | ⚠️ Difficult | ⭐⭐ | NOT VIABLE |
| **Economic Times** | Editorial | ⚠️ 3/10 | ❌ | ⚠️ 4/10 | ❌ | ⭐ | NOT VIABLE |
| **TradingView** | Web (blocked) | ✅ 9/10 | ⚠️ 5/10 | ❌ | ✅ | ❌ | **BLOCKED** |

---

## Recommended Stack (No Dhan)

### Tier 1: NSE via `nsepython` (Primary)
**Installation:**
```bash
pip install nsepython
```

**Data Available:**
- Daily OHLC (bhavcopy)
- Option chains (all strikes, OI, IV, Greeks)
- FII/DII cash flows (5-year history)
- Index data (NIFTY, BANKNIFTY, FINNIFTY, etc.)
- VIX

**Example:**
```python
from nsepython import optionchain_data, nse_fiidii, nse_quotes

# Option chain
chain = optionchain_data('NIFTY', '31-05-2026')

# FII/DII
fiidii = nse_fiidii(10)  # Last 10 days

# Live quote
quote = nse_quotes('ITC')
```

**Pros:**
- Institutional-grade accuracy
- Complete coverage (options, FII/DII, indices)
- No auth required
- No rate limits (reasonable usage)

**Cons:**
- Depends on NSE.com site structure (breaks on redesigns)
- Slower than broker APIs (~2–5 sec per call)
- Intraday data not available (daily only)

**Status:** Already used by `data/feed.py`

---

### Tier 2: yfinance (OHLC Backup)
**Installation:**
```bash
pip install yfinance
```

**Data Available:**
- Daily OHLC (all NSE stocks, use `.NS` suffix)
- 1-minute intraday candles
- Sector indices (NIFTY 50, BANKNIFTY, etc.)
- Historical 10+ years

**Example:**
```python
import yfinance as yf

# Daily OHLC
nifty = yf.download('NIFTY50.NS', period='6mo', interval='1d')

# Intraday 1m
intra = yf.download('NIFTY50.NS', period='7d', interval='1m')
```

**Pros:**
- Fast, reliable
- Free tier unlimited
- Supports Indian symbols
- Already in use for missing Dhan data

**Cons:**
- No option chains
- No FII/DII
- No Greek or IV data
- Delayed quotes (15–20 min)

**Status:** Fallback in `data/feed.py`

---

### Tier 3: Research360 (Option Chain Backup)
**Already integrated in `data/feed.py`**

**Data Available:**
- Full option chain (all strikes OI, volume)
- Pre-computed PCR and max-pain
- ~10 near-ATM strike LTPs (via graphprice/graphc/graphp arrays)

**URL:**
```
POST https://beta.research360.in/fno/option/ajax/optionChainApi.php
```

**Pros:**
- No auth required
- All strikes available (unlike broker APIs with depth limits)
- Pre-computed PCR/max-pain

**Cons:**
- Web scrape (HTML structure fragile)
- LTP only for ~10 near-ATM strikes (not all strikes)
- Subject to rate limiting (~3–5 requests/min safe)

**Status:** Secondary in current code; promoted to primary for options

---

### Tier 4: NSE Direct (Robust Fetch)
**Already integrated in `data/feed.py`**

**Data Available:**
- Same as nsepython wrapper, but direct HTTPS API
- More stable than nsepython during NSE site changes

**Pros:**
- Bypass nsepython library failures
- Lower latency than nsepython scrape

**Cons:**
- Requires session management (cookie + referer headers)
- Subject to session timeouts

**Status:** Already in code as NSE session backup

---

### Tier 5: AI Scraper (Last Resort)
**Already integrated in `data/ai_scraper.py`**

**Data Available:**
- Fallback for option chains if all else fails
- Uses ScrapeGraphAI (SaaS API or local Gemini)

**Pros:**
- Resilient to site structure changes (AI-based)
- Works when other scrapers break

**Cons:**
- 10x–100x slower (~30–60 sec per call)
- Requires API key (Google Gemini or ScrapeGraphAI SaaS)
- Cost if using SaaS tier

**Status:** Emergency fallback only

---

## Revised Data Source Priority (No Dhan)

### OHLC (Daily)
```
1. yfinance          (fast, reliable)
2. nsepython         (institutional accuracy)
3. NSE direct        (fallback if library fails)
4. Cache             (stale flag if all fail)
```

### OHLC (Intraday 5m/15m)
```
1. yfinance          (only viable option for intraday)
2. Research360 charts (limited, unreliable)
3. Cache + manual    (not real-time)
```

### Option Chain
```
1. Research360       (no auth, all strikes OI)
2. nsepython         (institutional data)
3. NSE direct        (direct API)
4. AI Scraper        (30–60 sec delay)
5. Cache             (stale flag)
```

### FII/DII
```
1. nsepython         (5-year history)
2. NSE direct        (live via API)
3. Cache (10-day)    (if NSE down)
```

### VIX
```
1. yfinance ^INDIAVIX (1-year history)
2. nsepython         (live)
```

---

## Implementation Changes Required

### 1. Disable Dhan-Specific Code

**File:** `data/feed.py`

Wrap all Dhan-related functions in a check:
```python
def _dhan_enabled() -> bool:
    """Check if Dhan is configured and available."""
    cfg = _data_sources_cfg()
    return cfg.get("dhan", {}).get("enabled", False) and _dhan_headers() is not None
```

Modify `ohlc()` to skip Dhan:
```python
def ohlc(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily/intraday OHLC. yfinance primary, nsepython fallback."""
    # Remove: Dhan call (always skip if not enabled)
    
    # yfinance primary
    yf = _yf()
    tkr = YF_SYMBOL.get(symbol) or (symbol if "." in symbol else f"{symbol}.NS")
    df = yf.download(tkr, period=period, interval=interval, progress=False)
    return df
```

### 2. Promote Research360 for Option Chains

Already done in current code; ensure it's first after skipping Dhan:
```python
def option_chain(symbol: str = "NIFTY") -> dict:
    """Live option chain via Research360 (primary), then NSE/nsepython."""
    
    # 1. Research360 (primary, no auth)
    try:
        data = _option_chain_from_research360(symbol)
        if data and data.get("records", {}).get("data"):
            return _save_chain(data)
    except Exception as e:
        print(f"[option_chain research360] failed: {e}")
    
    # 2. NSE direct
    try:
        session = _get_nse_session()
        if session:
            data = _fetch_nse_direct(session, symbol)
            if data.get("records", {}).get("data"):
                return _save_chain(data)
    except Exception as e:
        print(f"[option_chain NSE direct] failed: {e}")
    
    # 3. nsepython
    try:
        from nsepython import optionchain_data
        raw = optionchain_data(symbol)
        data = _nsepython_to_nse_chain(symbol, raw)
        if data.get("records", {}).get("data"):
            return _save_chain(data)
    except Exception as e:
        print(f"[option_chain nsepython] failed: {e}")
    
    # 4. AI fallback
    try:
        from data import ai_scraper
        ai_data = ai_scraper.get_option_chain_fallback(symbol)
        if ai_data and ai_data.get("records", {}).get("data"):
            return _save_chain(ai_data)
    except Exception as e:
        print(f"[option_chain AI] failed: {e}")
    
    # 5. Cache + stale flag
    return _load_cached_chain()
```

### 3. FII/DII Priority

Ensure nsepython is primary:
```python
def fii_dii_cash(days: int = 10) -> pd.DataFrame:
    """FII/DII cash flows via nsepython."""
    try:
        from nsepython import nse_fiidii
        return nse_fiidii(days)
    except Exception as e:
        print(f"[fii_dii nsepython] failed: {e}")
    
    # NSE direct fallback
    try:
        session = _get_nse_session()
        if session:
            return _fetch_nse_fiidii_direct(session, days)
    except Exception as e:
        print(f"[fii_dii NSE direct] failed: {e}")
    
    return pd.DataFrame()  # Stale flag in caller
```

### 4. Dashboard Data Sources Doc Update

Replace Dhan with yfinance + Research360 in primary column.

---

## Moneycontrol & Economic Times: Why Not?

### **Moneycontrol**
**Would require:**
- Selenium for JS rendering (charts are JS-rendered)
- Proxy rotation (blocks scrapers)
- Cookie jar management
- Parsing dynamic HTML

**Example (not recommended):**
```python
from selenium import webdriver
browser = webdriver.Chrome()
browser.get("https://www.moneycontrol.com/...")
# ... JS rendering, selector parsing ... (fragile)
```

**Issues:**
- Slower than nsepython by 10x
- High maintenance (site changes break it)
- Blocks aggressively after few requests
- Complex setup (browser driver required)

**Verdict:** Not worth implementing when nsepython exists.

### **Economic Times**
**Issues:**
- Mostly editorial (news, analysis)
- No structured data endpoints
- Historical charts are embedded (JS-rendered)
- No API, pure HTML
- Lacks institutional data (option chains, FII/DII)

**Verdict:** Not a viable data source.

---

## Summary: Recommended Fallback Chain (No Dhan)

```
┌─ OHLC (Daily)
│  1. yfinance (fast, reliable, free)
│  2. nsepython (institutional, slower)
│  3. Cache
│
├─ Option Chain
│  1. Research360 (web scrape, no auth)
│  2. nsepython (library wrapper)
│  3. NSE direct (robust fetch)
│  4. AI Scraper (slow but resilient)
│  5. Cache (stale flag)
│
├─ FII/DII
│  1. nsepython (5-year history)
│  2. NSE direct (live)
│  3. Cache (10-day rolling)
│
└─ VIX
   1. yfinance ^INDIAVIX
   2. nsepython
```

**Action Items:**
1. Disable Dhan in `data/feed.py` (wrap with `_dhan_enabled()` check)
2. Update `ohlc()` to skip Dhan, use yfinance primary
3. Promote Research360 to option chain primary (already done)
4. Add nsepython to FII/DII primary (already done)
5. Update [docs/DASHBOARD_DATA_SOURCES.md](../docs/DASHBOARD_DATA_SOURCES.md) to reflect no Dhan
6. Test fallback chain end-to-end

**No changes needed to:**
- Research360 integration (already working)
- AI Scraper (already fallback-only)
- NSE session management (already in place)
- yfinance (already used as backup)
