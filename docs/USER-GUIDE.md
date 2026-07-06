# StockMinded — User Guide

> Complete guide for operating the StockMinded decision system.

---

## 1. Getting Started

### 1.1 Prerequisites

- **Python 3.12** (64-bit)
- **Windows OS** (PowerShell) or Linux
- **4GB RAM** minimum (8GB recommended for 200-stock universe)
- **Internet connection** for market data APIs

### 1.2 Installation

```bash
# Clone or copy the project
cd StockMinded

# Create virtual environment
python -m venv .venv312

# Activate (Windows)
.venv312\Scripts\activate

# Activate (Linux/Mac)
source .venv312/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy environment template
cp .env.example .env
```

### 1.3 Configuration

Edit `.env` with your credentials:

```bash
# Telegram alerts (required)
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here

# AI sentiment (optional but recommended)
GOOGLE_API_KEY=your_google_api_key_here
SCRAPEGRAPHAI_API_KEY=your_scrapegraph_key_here

# Broker integration (optional, Phase 3)
DHAN_CLIENT_ID=your_dhan_client_id
DHAN_ACCESS_TOKEN=your_dhan_access_token
```

Edit `config/config.yaml` for your account settings:

```yaml
account:
  capital: 7000000    # Your total capital in ₹

risk:
  per_trade_pct: 0.0075     # 0.75% risk per trade
  daily_stop_pct: 0.02       # 2% daily stop loss
  monthly_stop_pct: 0.06    # 6% monthly stop loss

schedule_ist:
  morning_dashboard: "08:45"  # Time for daily alert
```

### 1.4 First Run

```bash
# Check connectivity
python main.py health

# Expected output:
# ✅ NIFTY OHLC: 22 rows, last close 24567.80
# ✅ India VIX: 13.45
# ✅ PCR (OI): 1.15 (Updated: 2026-06-30 15:30)
# ✅ FII/DII: 5 rows
```

---

## 2. Daily Operations

### 2.1 Morning Dashboard (Manual)

Run the 4-signal analysis and receive a Telegram alert:

```bash
python main.py dashboard
```

**Output (Telegram message):**
```
📊 Morning Dashboard

Regime: TREND_UP
  trend=+4  VIX=13.45 (+2.1% 5d)  ADX=28.5
  breadth>50DMA: 62.5%
  notes: ok

Flows — bias: LONG
  FII/DII 5d (₹Cr): {'fii': 1250.5, 'dii': -320.2}
  PCR OI=1.18  Vol=0.95  MaxPain=24600
  🟢 in: IT(+2.1%), PHARMA(+1.5%), AUTO(+1.2%)
  🔴 out: METAL(-1.8%), REALTY(-1.2%), MEDIA(-0.9%)

Leaders (A-grade long): TCS, INFY, SUNPHARMA, RELIANCE, HDFCBANK
Laggards (A-grade short): TATASTEEL, HINDALCO, VEDL

Structure: Long futures/stock on A-grade leaders; Bull Call Spread Nifty
  alt: Debit call spreads on leader stocks
  notes: Trail stops on 3-EMA 15m after +1R
```

### 2.2 AGoT-Enhanced Dashboard

For deeper analysis with multi-hypothesis reasoning:

```bash
python main.py agot
```

**Additional output:**
```
🧠 AGoT Morning Dashboard

Regime: TREND_UP
  AGoT confidence: 72%
  Ambiguity: 15%
  alternatives: RANGE_LOW_VOL(35%), VOL_CONTRACTION(25%)

AGoT Ensemble — LONG (68%)
  Risk: NORMAL
  Agreement: 75%

Corrections (1 active):
  🚫 REGIME_AVOID: RANGE_HIGH_VOL — 33% win rate (n=12)

Structure: Long futures/stock on A-grade leaders
Longs: TCS, INFY, SUNPHARMA
Shorts: TATASTEEL, HINDALCO
```

### 2.3 Scheduled Mode (Production)

Run as a background service with APScheduler:

```bash
python main.py schedule
```

This starts a blocking scheduler that runs the dashboard at configured IST times (default: 08:45 Mon-Fri).

**To run as a Windows service:**
```powershell
# Create a batch file: run_schedule.bat
@echo off
cd C:\path\to\StockMinded
call .venv312\Scripts\activate
python main.py schedule
pause

# Or use Task Scheduler to run at startup
```

**To run as a Linux service:**
```bash
# Create systemd service
sudo nano /etc/systemd/system/stockminded.service

[Unit]
Description=StockMinded Scheduler
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/StockMinded
ExecStart=/path/to/.venv312/bin/python main.py schedule
Restart=always

[Install]
WantedBy=multi-user.target

# Enable and start
sudo systemctl enable stockminded
sudo systemctl start stockminded
```

---

## 3. Web Dashboard

### 3.1 Starting the Server

```bash
python dashboard/server.py
```

Open http://localhost:5050 in your browser.

### 3.2 Dashboard Sections

| Section | Purpose |
|---------|---------|
| **Header Bar** | NIFTY/BANKNIFTY live quotes, market status, data freshness |
| **Regime Panel** | Current regime, trend score, VIX, ADX, breadth |
| **Flows Panel** | FII/DII flows, PCR, max-pain, sector rotation |
| **Leadership Table** | A-grade longs and shorts with RS metrics |
| **Structure Panel** | Recommended trade structure for current regime |
| **Verdict Panel** | Combined verdict with confidence and blocks |
| **Active Trades** | Currently open paper trades with live P&L |
| **Trade History** | Closed trades with exit reasons and P&L |
| **Skip Reasons** | Why trades were skipped today |
| **Settings** | Paper trader configuration |

### 3.3 Paper Trader Controls

From the web dashboard:

1. **Auto-Enter** — Click to scan alerts and auto-enter qualifying trades
2. **Check Exits** — Click to evaluate SL/TGT for all open trades
3. **EOD Summary** — Click to generate end-of-day report
4. **Manual Entry** — Enter a trade manually with custom parameters
5. **Settings** — Adjust risk parameters, SL/TGT percentages

### 3.4 Understanding the Verdict

The verdict panel shows:

```
┌─────────────────────────────────────────┐
│ VERDICT                                  │
├─────────────────────────────────────────┤
│ Stock: LONG_ONLY (HIGH confidence)       │
│   Strategy: Long A-Grade leaders        │
│   Top Long: TCS                         │
│                                          │
│ Nifty: OPTION_SELL_DEFINED_RISK (MED)   │
│   Strategy: Iron Condor @ Max Pain      │
│                                          │
│ Blocks: None                            │
│ Reasons: Regime TREND_UP, Trend +4/10   │
│          AI sentiment BULLISH (HIGH)    │
└─────────────────────────────────────────┘
```

**Confidence Levels:**
- **HIGH (80):** Strong signal alignment — proceed with full sizing
- **MEDIUM (50):** Moderate alignment — consider reduced sizing
- **LOW (20):** Weak signals — avoid or minimal sizing

**Blocks:** Conditions that prevent trading (e.g., "VIX extreme", "Data stale")

---

## 4. Paper Trading

### 4.1 Auto-Entry Workflow

1. **Signal generation** runs (via scheduler or manual refresh)
2. **Alerts** are created for qualifying setups
3. **Auto-enter** endpoint is called (via dashboard or scheduler)
4. **Risk gates** check each alert against guardrails
5. **Position sizing** calculates quantity based on risk budget
6. **Trade** is recorded in `paper_trades.json`

### 4.2 Monitoring Open Trades

Trades are checked every 5 minutes during market hours:

| Condition | Action |
|-----------|--------|
| Price hits stop loss | Close trade, log as STOP_LOSS |
| Price hits target | Close trade, log as TARGET |
| Trailing stop triggered | Close trade, log as TRAILING_STOP |
| EOD and auto-close enabled | Close at LTP, log as EOD_AUTO_CLOSE |
| LTP unavailable | Skip check (fail-safe) |

### 4.3 Option Trade Management

Options have additional exit conditions:

| Exit Type | Trigger |
|-----------|---------|
| VIX Spike | VIX increases >15% from entry AND VIX > 18 |
| Delta Breach | Net position delta exceeds ±0.35 |
| Profit Target | P&L reaches 50% of net credit |
| Stop Loss | Loss reaches 125% of net credit |
| Trailing Lock | Profit locked at 50%, exits if drops 30% |
| Expiry Day | Forced exit at 15:15 IST on expiry |

### 4.4 Understanding Trade Records

**Equity Trade:**
```json
{
  "id": 42,
  "symbol": "TCS",
  "direction": "LONG",
  "type": "STOCK",
  "entry_price": 3850.00,
  "qty": 130,
  "sl_price": 3773.00,
  "tgt_price": 3927.00,
  "peak_price": 3875.00,
  "entry_time": "2026-06-30 10:15:00",
  "exit_price": 3927.00,
  "exit_time": "2026-06-30 14:30:00",
  "exit_reason": "TARGET",
  "pnl": 10010.00,
  "pnl_pct": 2.0,
  "status": "CLOSED",
  "hold_minutes": 255,
  "journal_id": 156
}
```

**Option Trade:**
```json
{
  "id": 15,
  "symbol": "NIFTY",
  "structure": "IRON_CONDOR",
  "mode": "positional",
  "legs": [
    {"side": "SELL", "type": "PE", "strike": 24300, "expiry": "2026-07-01", "qty": 75, "entry_premium": 45.0},
    {"side": "BUY", "type": "PE", "strike": 24000, "expiry": "2026-07-01", "qty": 75, "entry_premium": 18.0},
    {"side": "SELL", "type": "CE", "strike": 24900, "expiry": "2026-07-01", "qty": 75, "entry_premium": 38.0},
    {"side": "BUY", "type": "CE", "strike": 25200, "expiry": "2026-07-01", "qty": 75, "entry_premium": 15.0}
  ],
  "net_credit": 3750.00,
  "max_loss_rupees": 18750.00,
  "entry_vix": 13.5,
  "peak_pnl": 2100.00,
  "trailing_lock": true,
  "status": "OPEN"
}
```

---

## 5. Risk Management

### 5.1 Guardrails in Action

The system enforces these non-negotiable rules:

| Rule | Default | Effect |
|------|---------|--------|
| Daily Stop | −2% of capital (₹1.4L on ₹70L) | All new entries blocked for the day |
| Monthly Stop | −6% of capital (₹4.2L on ₹70L) | Position size halved for next month |
| Per-Trade Risk | 0.75% of capital (₹52,500) | Maximum loss per trade |
| Concurrent Risk | 3% of capital (₹2.1L) | Max total open risk at any time |
| Margin Cap | 60% utilization | Block new entries if margin > 60% |
| Correlation | 0.70 max with existing | Block highly correlated new positions |

### 5.2 Viewing Risk Status

From the web dashboard, the **Risk Panel** shows:
- Current capital deployed
- Open risk (sum of all position risks)
- Daily P&L (realized today)
- Monthly P&L (realized this month)
- Margin utilization estimate

### 5.3 Adjusting Risk Parameters

Via the web dashboard **Settings** panel or by editing `paper_trades.json`:

```json
{
  "settings": {
    "capital_per_trade": 500000,
    "sl_pct": 2.0,
    "tgt_pct": 4.0,
    "rg_daily_stop_pct": 0.02,
    "rg_monthly_stop_pct": 0.06,
    "rg_concurrent_open_pct": 0.03
  }
}
```

---

## 6. Troubleshooting

### 6.1 Common Issues

| Problem | Cause | Solution |
|---------|-------|----------|
| "No usable OHLC data" | yfinance rate limited or network issue | Wait 15 min, check cache files in `data/cache/ohlc/` |
| "Option chain stale" | NSE API temporarily down | System uses synthetic pricing; wait for NSE recovery |
| "Could not fetch LTP" | Symbol not found or market closed | Verify symbol name; check market hours |
| "Daily stop hit" | Today's realized losses exceeded 2% | System blocks new entries until tomorrow |
| "Telegram send failed" | Invalid bot token or chat ID | Verify `.env` credentials |
| "JSON parse error" | `paper_trades.json` corrupted | System loads from `.bak` file automatically |
| "AGoT components failed" | Intelligence module error | System falls back to deterministic pipeline |

### 6.2 Log Files

| Log | Location | Content |
|-----|----------|---------|
| Server output | `dashboard/server_out.txt` | Flask server stdout |
| Server errors | `dashboard/server_log.txt` | Flask server stderr |
| Dashboard log | `dashboard_log.txt` | Main process output |
| Paper trader | Inline in server logs | Trade entry/exit events |

### 6.3 Data Freshness Check

```bash
python main.py health
```

If any check shows ❌, investigate:
- **NIFTY OHLC:** Check internet, yfinance status
- **VIX:** Same as above
- **PCR:** NSE option chain may be down
- **FII/DII:** NSE RSS feed may be unavailable

### 6.4 Resetting State

**Clear paper trades (start fresh):**
```bash
# Backup first
cp dashboard/paper_trades.json dashboard/paper_trades_backup.json

# Reset
echo '{"trades":[],"option_trades":[],"daily_summaries":[],"settings":{},"cumulative_pnl":0,"version":1}' > dashboard/paper_trades.json
```

**Clear OHLC cache:**
```bash
rm -rf data/cache/ohlc/*.pkl
```

**Reset journal database:**
```bash
# WARNING: This deletes all historical trade records
rm data/journal.sqlite
```

---

## 7. Best Practices

### 7.1 Daily Routine

1. **Before 9:00 AM:** Run `python main.py health` to verify data connectivity
2. **At 8:45 AM:** Scheduler runs `python main.py dashboard` automatically (or run manually)
3. **Review Telegram alert:** Check regime, bias, leaders, and structure recommendation
4. **Open web dashboard:** Verify signals match alert, check for any blocks
5. **During market hours:** Paper trader auto-checks exits every 5 minutes
6. **After 3:30 PM:** Review EOD summary, check closed trades
7. **Weekly:** Review feedback loop corrections, adjust if needed

### 7.2 When to Override

The system may occasionally produce signals that conflict with your market view. Consider overriding when:

- **Major event risk:** RBI policy, budget day, global crisis — system doesn't account for known events
- **Data quality issues:** If you know option chain data is wrong
- **Regime transition:** System may lag during regime changes

**How to override:**
- Simply don't execute the alert (manual trading in Phase 1)
- Adjust settings in the web dashboard to tighten/loosen filters
- Block specific symbols via the skip reasons API

### 7.3 Interpreting Low Confidence

When AGoT shows low confidence or high ambiguity:

| Signal | Meaning | Action |
|--------|---------|--------|
| Confidence < 50% | Market regime is unclear | Reduce position size or stay flat |
| Ambiguity > 60% | Multiple regimes equally likely | Wait for clarity (next session) |
| Agreement < 50% | Signals disagree with each other | Reduce size or skip trades |
| Corrections present | Historical pattern has failed | Apply the correction (block or reduce) |

### 7.4 Monitoring the Feedback Loop

Check active corrections weekly:

```python
from intelligence.feedback_loop import FeedbackLoop

loop = FeedbackLoop("./data/journal.sqlite")
analysis = loop.analyze_outcomes(lookback_days=30)

print(f"Win rate: {analysis.win_rate:.1%}")
print(f"Profit factor: {analysis.profit_factor:.2f}")
print(f"\nRegime accuracy:")
for regime, stats in analysis.regime_accuracy.items():
    print(f"  {regime}: {stats['win_rate']:.1%} ({stats['count']} trades)")
print(f"\nActive corrections: {len(analysis.corrections)}")
for c in analysis.corrections:
    print(f"  {c['type']}: {c['target']} — {c['reason']}")
```

---

## 8. File Reference

### 8.1 Key Files

| File | Purpose |
|------|---------|
| `main.py` | CLI entry point |
| `dashboard/server.py` | Flask web server |
| `dashboard/paper_trader.py` | Paper trading engine |
| `dashboard/paper_trades.json` | Active trade state |
| `data/journal.sqlite` | Historical trade audit |
| `data/feed.py` | Market data fetching |
| `config/config.yaml` | System configuration |
| `config/fno200.csv` | F&O stock lot sizes |
| `.env` | Secrets (tokens, API keys) |

### 8.2 Cache Files

| Location | Content | Cleanup |
|----------|---------|---------|
| `data/cache/ohlc/*.pkl` | Daily OHLC per symbol | Day-scoped, auto-invalidated |
| `data/cache/option_chain_*.json` | Option chain snapshots | Overwritten each fetch |
| `data/cache/fii_dii_cache.json` | FII/DII data | Overwritten each fetch |
| `data/cache/ai_sentiment_cache.json` | AI sentiment | Cached for 4 hours |

### 8.3 Database Tables

| Table | Purpose |
|-------|---------|
| `trades` | Historical trade log (open/close/P&L) |
| `skipped_trades` | Reasons for not entering trades |
| `regime_snapshots` | Regime classification history |
| `flow_snapshots` | Flow analysis history |
| `trade_exit_analysis` | Post-trade root cause analysis |
| `iv_history` | Daily ATM IV per symbol |

---

## 9. FAQ

**Q: Can I run this on a VPS?**
A: Yes. Use `setup_vps.sh` for Linux deployment. Ensure the dashboard is behind a reverse proxy with authentication.

**Q: How do I add more stocks to the universe?**
A: Edit `config/fno200.csv` or change `universe.source` in `config.yaml` to a custom list.

**Q: Why are some option premiums showing ₹0?**
A: The NSE option chain may be returning OI-only data (outside market hours). The system falls back to Black-Scholes synthetic pricing.

**Q: How do I disable paper trading?**
A: Set `nifty_options.enabled: false` and `banknifty_options.enabled: false` in `config.yaml`.

**Q: Can I use this for live trading?**
A: Not yet. Phase 1-2 are alerts-only and paper trading. Phase 3 (broker integration) is planned but requires extensive testing.

**Q: How often should I review the system?**
A: Daily: check alerts and paper trades. Weekly: review feedback loop corrections. Monthly: verify risk parameters still match your capital.

**Q: What if the system crashes mid-trade?**
A: The atomic write mechanism (`tmp → bak → rename`) ensures `paper_trades.json` is never corrupted. On restart, the system resumes from the last consistent state.
