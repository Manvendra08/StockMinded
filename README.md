# StockMinded

Decision system for Indian markets. Answers 4 questions every morning before 9:15 IST:
**Regime · Flows · Leadership · Structure** — then sends a Telegram alert.

Capital target: ₹70L · 5%/month · max −6% monthly / −2% daily drawdown.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # fill Telegram, broker, and GOOGLE_API_KEY
```

Edit `config/config.yaml` for capital, universe, schedule.

## Run

```bash
python main.py health          # check data feeds
python main.py dashboard       # run 4-signal dashboard now
python main.py schedule        # cron loop: daily 08:45 IST alert
```

## Layout

```
config/      config.yaml + loader
data/        feed.py (yfinance + nsepython) + ai_scraper.py (ScrapeGraphAI SaaS/Local)
signals/     regime · flows · leadership · structure_map
risk/        sizing · guardrails
ops/         journal (SQLite) · alerts (Telegram)
main.py      CLI entry
```

## What each signal does

| Signal | File | Output |
|---|---|---|
| Regime | `signals/regime.py` | 1 of 6 regimes + VIX/ADX/trend/breadth |
| Flows | `signals/flows.py` | FII/DII 5d, top inflow/outflow sectors, PCR, max-pain, bias, AI news sentiment |
| Leadership | `signals/leadership.py` | A-grade long/short lists via 20d RS-line quintiles |
| Structure | `signals/structure_map.py` | Regime → primary/secondary trade structure |

## Phase plan

1. **Phase 1 (now)**: alerts-only. You execute trades manually from Telegram.
2. **Phase 2**: add `execution/broker.py` (Kite/Dhan/Fyers) + auto-trade after 2 weeks of clean alerts.
3. **Phase 3**: add `manage/monitor.py` intraday P&L + trail-stop + auto-exit.

## Risk contract (non-negotiable)

- per-trade risk 0.75% · concurrent open 3%
- daily stop −2% → flat everything
- monthly stop −6% → halve size next month
- margin util cap 60% · correlation cap 0.70

See `risk/guardrails.py`.
