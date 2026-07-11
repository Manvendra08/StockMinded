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
    def test_load_universe_fo_sample(self):
        """Test loading universe from fo_sample config"""
        cfg = {"universe_source": "fo_sample", "universe_fo_sample": ["FO1", "FO2", "FO3"]}
        from config.loader import load_universe
        res = load_universe(cfg)
        assert res == ["FO1", "FO2", "FO3"]

    def test_load_universe_fo_sample_empty_raises(self):
        """Test that empty fo_sample raises ValueError"""
        cfg = {"universe_source": "fo_sample", "universe_fo_sample": []}
        from config.loader import load_universe
        with pytest.raises(ValueError, match="Universe is empty"):
            load_universe(cfg)

    def test_load_universe_fno200_unknown_source_raises(self):
        """Test that unknown universe source falls back to fo_sample"""
        cfg = {"universe_source": "unknown", "universe_fo_sample": ["FB1", "FB2"]}
        from config.loader import load_universe
        # unknown source falls through to else branch which uses fo_sample
        res = load_universe(cfg)
        assert res == ["FB1", "FB2"]

