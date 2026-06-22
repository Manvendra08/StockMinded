"""Tests for config/loader.py."""
import os
import pytest
import yaml
from config.loader import load_config


@pytest.fixture
def config_file(tmp_path):
    def _make(content: dict) -> str:
        path = tmp_path / "config.yaml"
        with open(path, "w") as f:
            yaml.dump(content, f)
        return str(path)
    return _make


class TestLoadConfig:
    def test_loads_basic_yaml(self, config_file):
        path = config_file({"account": {"capital": 1000000}})
        cfg = load_config(path)
        assert cfg["account"]["capital"] == 1000000

    def test_expands_env_var(self, config_file, monkeypatch):
        monkeypatch.setenv("MY_TOKEN", "secret123")
        path = config_file({"token": "${MY_TOKEN}"})
        cfg = load_config(path)
        assert cfg["token"] == "secret123"

    def test_missing_env_var_expands_to_empty_string(self, config_file, monkeypatch):
        monkeypatch.delenv("MISSING_VAR", raising=False)
        path = config_file({"key": "${MISSING_VAR}"})
        cfg = load_config(path)
        assert cfg["key"] == ""

    def test_expands_env_in_nested_dict(self, config_file, monkeypatch):
        monkeypatch.setenv("API_KEY", "abc")
        path = config_file({"broker": {"api_key": "${API_KEY}", "provider": "kite"}})
        cfg = load_config(path)
        assert cfg["broker"]["api_key"] == "abc"
        assert cfg["broker"]["provider"] == "kite"

    def test_expands_env_in_list(self, config_file, monkeypatch):
        monkeypatch.setenv("ITEM", "NIFTY")
        path = config_file({"indices": ["${ITEM}", "BANKNIFTY"]})
        cfg = load_config(path)
        assert cfg["indices"][0] == "NIFTY"

    def test_non_string_values_untouched(self, config_file):
        path = config_file({"risk": {"per_trade_pct": 0.0075, "daily_stop_pct": 0.02}})
        cfg = load_config(path)
        assert cfg["risk"]["per_trade_pct"] == 0.0075

    def test_default_config_loads(self):
        cfg = load_config()
        assert "account" in cfg
        assert "risk" in cfg
        assert "universe_fo_sample" in cfg


class TestLoadUniverse:
    def test_load_universe_dhan_primary_success(self, monkeypatch):
        mock_symbols = ["MOCK1", "MOCK2"]
        monkeypatch.setattr(
            "config.loader.fetch_dhan_public_universe",
            lambda: mock_symbols
        )
        cfg = {"universe_source": "fno200"}
        symbols = load_config() # dummy to get imports, or we can import load_universe
        from config.loader import load_universe
        res = load_universe(cfg)
        assert res == mock_symbols

    def test_load_universe_dhan_fallback_on_failure(self, monkeypatch, tmp_path):
        # Force fetch_dhan_public_universe to fail/return None
        monkeypatch.setattr(
            "config.loader.fetch_dhan_public_universe",
            lambda: None
        )
        
        # Mock Path in loader to point to a temporary csv file
        csv_content = "symbol,sector,lot_size\nFALLBACK1,BANK,100\nFALLBACK2,IT,200\n"
        csv_file = tmp_path / "fno200.csv"
        with open(csv_file, "w", encoding="utf-8") as f:
            f.write(csv_content)
            
        monkeypatch.setattr(
            "config.loader.Path",
            lambda *args, **kwargs: csv_file
        )
        
        cfg = {"universe_source": "fno200"}
        from config.loader import load_universe
        res = load_universe(cfg)
        assert "FALLBACK1" in res
        assert "FALLBACK2" in res

