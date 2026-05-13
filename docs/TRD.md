# Technical Requirements Document (TRD) - StockMinded

## 1. System Architecture
StockMinded follows a **Modular Layered Architecture** with a clear separation between data, signals, risk, and operations.

### 1.1 Tech Stack
- **Language**: Python 3.12+
- **Framework**: Flask (Dashboard API)
- **Database**: 
    - SQLite (Trade Journal, Signal History, IV History)
    - JSON (Atomic, File-backed Paper Trading State)
- **Data Feeds**: `yfinance` (Global/OHLC), `nsepython` (NSE Specific Data), `pandas` (Analysis)
- **Scheduler**: `APScheduler` for IST-aligned cron jobs.

## 2. Technical Components
### 2.1 Signal Engine (`signals/`)
- **Regime**: ADX and EMA-based trend/volatility scoring.
- **Leadership**: Polynomial fit of RS-lines (Stock vs Benchmark) for quintile ranking.
- **Options**: Custom Black-Scholes implementation for Delta/IV without external scientific libraries.

### 2.2 Data Layer (`data/`)
- **Hybrid Caching**: 120s in-memory bucket for OHLC data.
- **Persistent Cache**: Pickle-based day-scoped storage for F&O universe candles to mitigate rate-limiting.

### 2.3 Operations Layer (`ops/`)
- **Alerter**: Multi-channel (Telegram/Stdout) notification system.
- **Journal**: Thread-safe SQLite persistence for auditability.

## 3. Reliability & Performance
- **Atomic Updates**: Paper trading JSON uses Write-Ahead-Logging (WAL) logic (.tmp -> sync -> rename) with file locks for Windows compatibility.
- **Self-Healing**: Automatic backup (.bak) restoration if JSON corruption is detected.
- **Rate-Limit Management**: Unified option chain fetches per ticker per worker tick.

## 4. Security
- **Secret Management**: Environment variable expansion (`${VAR}`) within `config.yaml` to prevent secret leakage in version control.
- **Local First**: Data remains on local storage with optional Telegram delivery.
