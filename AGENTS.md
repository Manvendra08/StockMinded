# AGENTS.md

This file provides guidance to agents when working with code in this repository.

- `config/loader.py` expands `${ENV_VAR}` inside YAML recursively; prefer putting secrets in [`config/config.yaml`](config/config.yaml) as placeholders instead of reading env vars ad hoc.
- Universe selection is not fixed: use [`config.loader.load_universe()`](config/loader.py:25) because `universe_source: fno200` switches runtime behavior away from `universe_fo_sample`.
- Cached market data is intentionally pickle-based, day-scoped, and stored under [`data/cache/ohlc`](data/cache/ohlc); [`data/feed.py`](data/feed.py:247) ignores old days instead of cleaning them.
- Dashboard and CLI do not share the exact same universe path: [`main.py`](main.py:28) still uses `universe_fo_sample`, while [`dashboard/server.py`](dashboard/server.py:70) uses `load_universe(cfg)`.
- Signal modules return dataclass snapshots with custom [`to_dict()`](signals/flows.py:25)-style serializers; preserve that boundary when adding fields.
- Paper trading state is file-backed at [`dashboard/paper_trades.json`](dashboard/paper_trades.json), not SQLite; scratch reset scripts hardcode Windows paths and are not portable.
- Journal persistence is SQLite-backed at [`paths.journal_db`](config/config.yaml:74) via [`ops/journal.py`](ops/journal.py:1); dashboard cache/freshness logic assumes this file coexists with pickle caches.
- Relative volume in intraday API is computed from 3 months of daily candles with `today_vol / mean(previous 20 sessions)` in [`dashboard/server.py`](dashboard/server.py:488), not from `fast_info.three_month_average_volume` alone.
- Q score is project-defined in [`signals/leadership.py`](signals/leadership.py:54) and now uses symmetric long/short threshold bands; do not replace with percentile quintiles without updating dashboard semantics.
- **AI Scraper & Sentiment Fallback**: Integrated **ScrapeGraphAI** in `data/ai_scraper.py` and `data/feed.py`.
  - Supports hybrid mode: manages calls through **SaaS API** (preferred, via `scrapegraph-py` using `saas_api_key`) and falls back to **Local execution** (`SmartScraperGraph`, `SearchGraph` using Gemini).
  - Environment: Requires `GOOGLE_API_KEY` for local fallback. Configured in `config/config.yaml` under `scrapegraphai`.
  - Flow snapshot captures `ai_sentiment` using news sentiment summarization.
- Test discovery is constrained by [`pytest.ini`](pytest.ini:1): `tests/`, `test_*.py`, `Test*`, `test_*`, with markers `unit` and `integration`.
- Useful commands discovered from files:
  - `python main.py dashboard`
  - `python main.py schedule`
  - `python main.py health`
  - `python dashboard/server.py`
  - `pytest tests/test_ai_scraper.py -v`
  - `pytest tests/unit/test_alerts.py -q`
  - `pytest -m integration -q`
  - `python -m py_compile dashboard\server.py signals\flows.py signals\leadership.py data\feed.py data\ai_scraper.py`
