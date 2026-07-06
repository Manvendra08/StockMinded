# StockMinded

Decision system for Indian markets. Answers 4 questions every morning before 9:15 IST:
**Regime · Flows · Leadership · Structure** — then sends a Telegram alert.

Capital target: ₹70L · 5%/month · max −6% monthly / −2% daily drawdown.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
cp .env.example .env          # fill Telegram, broker, Shoonya, and LLM API keys
```

Edit `config/config.yaml` for capital, universe, schedule, options, and timing engine config.

**Key env vars**: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SHOONYA_USER_ID`, `SHOONYA_PASSWORD`, `SHOONYA_TOTP_KEY`, `SHOONYA_API_SECRET`, `GOOGLE_API_KEY`, `OPENCODE_API_KEY`, `GROQ_API_KEY`.

## Run

```bash
python main.py health          # check data feeds
python main.py dashboard       # run 4-signal dashboard now
python main.py agot            # AGoT-enhanced dashboard (graph-based reasoning)
python main.py agot-test       # quick AGoT system validation (no data fetch)
python main.py schedule        # cron loop: daily 08:45 IST alert
python dashboard/server.py     # Flask dashboard + paper trading on :5050
```

## Layout

```
config/        config.yaml + loader + fno200.csv (universe)
data/          feed.py (Shoonya + yfinance + nsepython) + ai_scraper.py (LLM chain + sentiment)
               shoonya_fetcher.py (PRIMARY data source) + notifier.py (Telegram/Discord)
signals/       regime · flows · leadership · structure_map · timing · verdict
               options · option_strategy (option chain, IV rank, structure picker)
intelligence/  AGoT framework: thought_graph · adaptive_regime · signal_ensemble
               feedback_loop · agot_integration · learner
risk/          sizing · guardrails
ops/           journal (SQLite) · alerts (Telegram) · backtest (timing analysis)
dashboard/     server.py (Flask) + paper_trader.py + paper.html
main.py        CLI entry
```

## What each signal does

| Signal | File | Output |
|---|---|---|
| Regime | `signals/regime.py` | 1 of 6 regimes + VIX/ADX/trend/breadth |
| Flows | `signals/flows.py` | FII/DII 5d, top inflow/outflow sectors, PCR, max-pain, bias, AI news sentiment |
| Leadership | `signals/leadership.py` | A-grade long/short lists via 20d RS-line quintiles |
| Structure | `signals/structure_map.py` | Regime → primary/secondary trade structure |
| Timing | `signals/timing.py` | Entry gates: VWAP/RSI/ATR overextension + market exhaustion score |
| Verdict | `signals/verdict.py` | Split verdict: StockVerdict (directional) + NiftyVerdict (option selling) |
| Options | `signals/options.py` | Option chain snapshot, IV rank, expiry helpers, 0-DTE avoidance |
| Option Strategy | `signals/option_strategy.py` | Structure picker + leg resolver (spreads, straddles, iron condors, naked sells) |

## Data sources

| Source | Use | Priority |
|---|---|---|
| Shoonya (Finvasia) | Option chain, quotes, LTP | PRIMARY (OAuth via Playwright) |
| NSE direct / nsepython | Option chain, FII/DII, VIX | Fallback 1 |
| yfinance | OHLC, sector data | Fallback for OHLC |
| ScrapeGraphAI | Web scraping fallback | Fallback 2 (SaaS → Local/Gemini) |
| LLM chain | News sentiment, timing AI review | OpenCode Zen → Groq → Gemini |

## Phase plan

1. **Phase 1**: alerts-only. You execute trades manually from Telegram.
2. **Phase 2 (active)**: options paper trading — auto-entry, smart exits, skip logging, verdict engine, timing gates, AGoT intelligence layer. Dashboard on `:5050`.
3. **Phase 3**: add `execution/broker.py` (Kite/Dhan/Fyers) + live order placement.
4. **Phase 4**: add `manage/monitor.py` intraday P&L + trail-stop + auto-exit.

## Testing

```bash
pytest tests/unit/ -q              # 30+ unit tests
pytest -m integration -q           # integration tests
pytest tests/test_ai_scraper.py -v # AI scraper + LLM tests
```

## Risk contract (non-negotiable)

- per-trade risk 0.75% · concurrent open 3%
- daily stop −2% → flat everything
- monthly stop −6% → halve size next month
- margin util cap 60% · correlation cap 0.70

See `risk/guardrails.py`.
