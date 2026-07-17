# Scan Sentinel Knowledge Base

Welcome to the Scan Sentinel grounded codebase knowledge base. This document serves as the single source of truth for codebase architecture, pipeline flows, features, and diagnostics.

---

## 1. Pipeline Flow & Architecture

### News & Sentiment Analysis
- **Flow:** Real-time headlines are scraped from three sources: **ICICI Direct**, **Livemint**, and **Moneycontrol**.
- **Normalization:** Headlines are parsed with actual timezone-aware UTC timestamps rather than hardcoded now timestamps.
- **Sorting:** All news headlines are combined, deduped, and sorted chronologically (newest first) before being sent to the AI Sentiment Engine.
- **LLM Pipeline:** News sentiment is evaluated via LLM (Groq -> Gemini -> OpenRouter) to generate the "AI Market Sentiment" narrative, with a fallback to local lexicon analysis.

---

## 2. Key Hotfixes & Enhancements

### Manual Close for Stock Trades (July 2026)
- **Problem:** Stock paper trades were stored in the SQLite `data/journal.sqlite` database, but the UI close endpoint was only searching in the JSON `dashboard/paper_trades.json` file, causing manual closures to fail.
- **Fix:** Extended `close_trade_manual` in `dashboard/paper_trader.py` to check the SQLite database if a trade is not found in the JSON file. It now fetches the stock's current LTP, computes P&L, closes it in SQLite, and returns the closed trade back to the UI.

### Option Greek Delta Breach Bug (July 2026)
- **Problem:** Sensibull option chains return Implied Volatility (IV) as a decimal fraction (e.g. `0.15` for 15% IV), whereas other sources return it as a percentage (e.g. `15.0`). The code unconditionally divided all IV values by `100.0`, rendering Sensibull's IV to near-zero (e.g. `0.0015`), breaking Black-Scholes Greek calculation and causing instant false `DELTA_BREACH` exits on Straddle trades.
- **Fix:** Adjusted `chain_snapshot` in `signals/options.py` to only divide IV by `100.0` if the value is represented as a percentage (> 1.0).

### AI Sentiment News Chronology Bug (July 2026)
- **Problem:** Scrapers hardcoded all headline timestamps to `now()`, making it impossible to distinguish between current news and yesterday's stale commentary. Stale news was weighted heavily due to layout/thread execution order.
- **Fix:** Implemented parsed publication datetimes for ICICI Direct and Livemint, and relative chronology offsets for Moneycontrol. Headlines are sorted chronologically, prioritizing fresh market events at the top of the AI input window.
