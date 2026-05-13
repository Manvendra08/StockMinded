import os
import re
from pathlib import Path
from typing import Optional
import yaml

# Required env vars that must be non-empty for production use.
# Keys are the var names; values are the config dot-paths for context.
_REQUIRED_ENV_VARS = []


def _expand(value, _missing: list | None = None):
    """Expand ${VAR}/$VAR in config strings.

    Collects undefined vars into *_missing* list instead of silently
    returning empty string.  Caller decides whether to raise.
    """
    if isinstance(value, str):
        pattern = re.compile(r'\$(?:(\w+)|{(\w+)})')

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


def load_config(path: str | None = None) -> dict:
    p = Path(path) if path else Path(__file__).parent / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

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


def load_universe(cfg: dict) -> list[str]:
    src = cfg.get("universe_source", "fo_sample")
    if src == "fno200":
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
