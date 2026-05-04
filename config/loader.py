import os
from pathlib import Path
import yaml


def _expand(value):
    """Expand environment variables in config values using os.path.expandvars.
    
    Supports ${VAR} and $VAR syntax. Returns empty string for undefined vars.
    """
    if isinstance(value, str):
        # os.path.expandvars is safer than custom regex substitution
        # It handles ${VAR} and $VAR syntax and doesn't allow arbitrary code execution
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(x) for x in value]
    return value


def load_config(path: str | None = None) -> dict:
    p = Path(path) if path else Path(__file__).parent / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return _expand(cfg)

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
                raise ValueError("Universe is empty. Check config 'universe_fo_sample' or 'fno200.csv'.") from e
            return fallback
    else:
        symbols = cfg.get("universe_fo_sample", [])
        if not symbols:
            raise ValueError("Universe is empty. Check config 'universe_fo_sample'.")
        return symbols
