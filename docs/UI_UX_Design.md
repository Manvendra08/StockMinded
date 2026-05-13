# UI/UX Design Specification - StockMinded

## 1. Design Philosophy
- **High Signal-to-Noise**: Only display actionable data.
- **Color Coding**: 
    - 🟢 Bullish / Profit / Inflow
    - 🔴 Bearish / Loss / Outflow
    - ⚪ Neutral / Stale
- **Mobile First**: Telegram alerts are optimized for one-handed mobile scrolling.

## 2. Telegram Alert Structure
### 2.1 The Morning Dashboard
1. **Header**: Date, Time (IST), Global VIX Status.
2. **Regime Block**: 🧠 Current Regime (e.g., `TREND_UP`) + Trend Score (0-3).
3. **Flows Block**: 🌊 FII/DII Net, PCR (OI/Vol), Smart Money Bias.
4. **Leadership Tables**: Top 5 A-Grade Longs and Top 5 A-Grade Shorts.
5. **Trade Plan**: Primary and Secondary structure suggestions.

## 3. Web Dashboard (Server UI)
### 3.1 Main Dashboard
- **Market Status Bar**: Live Nifty/BankNifty LTP.
- **Signal Cards**: Large cards for Regime and Flows.
- **Leaderboard Grid**: Interactive table with RS-slope and quintile filtering.

### 3.2 Paper Trading Tab (New)
- **Trade Cards**: 
    - Header: Symbol, Structure, Entry Date.
    - Body: Multi-leg breakdown (Collapsible).
    - Footer: Unrealized P&L, Status (OPEN/CLOSED), Exit Reason.
- **Automation Toggle**: Controls the background `auto_enter` worker.

## 4. Micro-Interactions
- **Copy-to-Clipboard**: One-click copy for symbols and strike prices.
- **Progressive Disclosure**: Detailed leg data hidden behind "Show Legs" buttons to keep the UI clean.
