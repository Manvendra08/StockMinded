import os
import re
from pathlib import Path
import yaml

_ENV_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _expand(value):
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), value)
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
                return symbols
        except Exception as e:
            print(f"Failed to load fno200.csv: {e}")
            return cfg.get("universe_fo_sample", [])
    else:
        return cfg.get("universe_fo_sample", [])
