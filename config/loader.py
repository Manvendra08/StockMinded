import os
import re
import logging
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv

# Load environment variables from .env file in the project root
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Env vars enforcement.
# Intentionally left empty: this project is designed to run in "alerts-only /
# paper trade" modes where missing broker/AI credentials should not crash the
# app (see fallbacks in ops/alerts.py and data/feed.py / data/ai_scraper.py).
_REQUIRED_ENV_VARS: list[str] = []


def _expand(value, _missing: list | None = None):
    """Expand ${VAR}/$VAR in config strings.

    Collects undefined vars into *_missing* list instead of silently
    returning empty string.  Caller decides whether to raise.
    """
    if isinstance(value, str):
        pattern = re.compile(r"\$(?:(\w+)|{(\w+)})")

        def replace(match):
            var_name = match.group(1) or match.group(2)
            val = os.environ.get(var_name)
            if val is None:
                if _missing is not None:
                    _missing.append(var_name)
                return ""
            return val

        return pattern.sub(replace, value)
    if isinstance(value, dict):
        return {k: _expand(v, _missing) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(x, _missing) for x in value]
    return value


# BUG-28 FIX: Default config used when config.yaml is missing.
# Provides sensible defaults so the application can start in
# alerts-only / paper-trade mode without a config file.
_DEFAULT_CONFIG: dict = {
    "universe_source": "fo_sample",
    "universe_fo_sample": [],
    "data_sources": {},
    "broker": {},
    "paths": {"journal_db": "data/trades.db"},
}


def load_config(path: str | None = None) -> dict:
    p = Path(path) if path else Path(__file__).parent / "config.yaml"
    # BUG-28 FIX: Fall back to default config if config.yaml doesn't exist
    # instead of crashing with FileNotFoundError.
    if not p.exists():
        logging.getLogger(__name__).warning(
            "Config file not found at %s; using built-in defaults", p
        )
        return dict(_DEFAULT_CONFIG)
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if cfg is None:
        cfg = {}

    missing: list[str] = []
    cfg = _expand(cfg, missing)

    # Only raise for vars that are genuinely required (not broker secrets
    # which may be intentionally absent in local/paper-trade mode).
    critical_missing = [v for v in missing if v in _REQUIRED_ENV_VARS]
    if critical_missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(critical_missing)}. "
            "Set them in your .env file or shell before starting."
        )

    # if missing:
    #     print(f"[config] WARNING: undefined env vars (non-critical): {', '.join(missing)}")

    return cfg


def fetch_dhan_public_universe() -> list[str]:
    import json
    import re

    import requests

    symbols = set()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    # Fetch page 1 from raw HTML
    try:
        # BUG-37 FIX: Increased timeout from 5s to 15s. Dhan can be slow
        # under load; 5s caused frequent timeouts and unnecessary CSV fallback.
        r = requests.get(
            "https://dhan.co/futures-stocks-list/", headers=headers, timeout=15
        )
        if r.status_code == 200:
            match = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL
            )
            if match:
                js = json.loads(match.group(1))
                data = (
                    js.get("props", {})
                    .get("pageProps", {})
                    .get("listData", {})
                    .get("data", [])
                )
                for item in data:
                    sym = item.get("Sym")
                    if sym:
                        symbols.add(sym.strip())
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[universe] Dhan HTML page 1 fetch failed: %s", e
        )

    # Query remaining pages of Nifty 200 from public customscan API
    post_url = "https://ow-scanx-analytics.dhan.co/customscan/fetchdt"
    post_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": "https://dhan.co/",
    }
    for page in range(1, 5):
        payload = {
            "data": {
                "sort": "Mcap",
                "sorder": "desc",
                "count": 50,
                "params": [
                    {"field": "idxlist.Indexid", "op": "", "val": "18"},
                    {"field": "Exch", "op": "", "val": "NSE"},
                    {"field": "OgInst", "op": "", "val": "ES"},
                ],
                "fields": ["Sym"],
                "pgno": page,
            }
        }
        try:
            # BUG-37 FIX: Increased timeout from 5s to 15s (see above).
            r = requests.post(post_url, headers=post_headers, json=payload, timeout=15)
            if r.status_code == 200:
                for item in r.json().get("data", []):
                    sym = item.get("Sym")
                    if sym:
                        symbols.add(sym.strip())
        except Exception as e:
            logging.getLogger(__name__).warning(
                "[universe] Dhan customscan page %s failed: %s", page, e
            )

    return sorted(list(symbols))


def load_universe(cfg: dict) -> list[str]:
    src = cfg.get("universe_source", "fo_sample")
    if src == "fno200":
        # Make Dhan public URL/API primary
        try:
            symbols = fetch_dhan_public_universe()
            if symbols:
                return symbols
        except Exception as e:
            print(f"[config] Failed to fetch Dhan public F&O universe: {e}")

        # Fallback to local csv
        csv_path = Path(__file__).parent / "fno200.csv"
        try:
            import csv

            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                symbols = []
                for row in reader:
                    sym = row.get("symbol", "").strip()
                    if sym and sym not in symbols:
                        symbols.append(sym)
            if not symbols:
                raise ValueError("fno200.csv loaded but contained no valid symbols")
            return symbols
        except Exception as e:
            print(f"Failed to load fno200.csv: {e}")
            fallback = cfg.get("universe_fo_sample", [])
            if not fallback:
                raise ValueError(
                    "Universe is empty. Check config 'universe_fo_sample' or 'fno200.csv'."
                ) from e
            return fallback
    else:
        symbols = cfg.get("universe_fo_sample", [])
        if not symbols:
            raise ValueError("Universe is empty. Check config 'universe_fo_sample'.")
        return symbols


def load_sector_map(cfg: dict | None = None) -> dict[str, str]:
    """Return {symbol: sector} from fno200.csv.

    Falls back to an empty dict if the file is unavailable so callers
    that treat sector_map as optional continue to work.
    """
    csv_path = Path(__file__).parent / "fno200.csv"
    try:
        import csv

        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            return {
                row["symbol"].strip(): row["sector"].strip()
                for row in reader
                if row.get("symbol", "").strip() and row.get("sector", "").strip()
            }
    except Exception as e:
        print(f"[config] WARNING: could not load sector map from fno200.csv: {e}")
        return {}
