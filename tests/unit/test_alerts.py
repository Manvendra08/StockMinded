"""Tests for ops/alerts.py."""
import pytest
from unittest.mock import patch, MagicMock
from ops.alerts import Alerter, format_dashboard
from signals.regime import Regime, RegimeSnapshot
from signals.flows import FlowSnapshot
from signals.structure_map import StructurePlan
from signals.leadership import StockRank


@pytest.fixture
def alerter_no_creds():
    return Alerter(bot_token=None, chat_id=None)


@pytest.fixture
def alerter_with_creds():
    return Alerter(bot_token="test-token", chat_id="test-chat")


@pytest.fixture
def regime_snap():
    return RegimeSnapshot(
        regime=Regime.TREND_UP,
        trend_score=2,
        vix=12.5,
        vix_5d_change_pct=-3.0,
        adx=27.0,
        breadth_pct_above_50dma=72.0,
        notes="ok",
    )


@pytest.fixture
def flow_snap():
    return FlowSnapshot(
        fii_dii_5d_net_cr={"fii": 1200.0, "dii": 500.0},
        top_inflow_sectors=[("NIFTY IT", 2.1), ("NIFTY BANK", 1.8)],
        top_outflow_sectors=[("NIFTY METAL", -1.5)],
        pcr_oi=1.25,
        pcr_vol=0.95,
        max_pain=22100.0,
        smart_money_bias="LONG",
    )


@pytest.fixture
def structure():
    return StructurePlan(
        primary="Long futures on A-grade leaders",
        secondary="Bull Call Spread",
        notes="Trail stops at +1R",
    )


class TestAlerter:
    def test_no_creds_prints_to_stdout(self, alerter_no_creds, capsys):
        alerter_no_creds.send("test message")
        captured = capsys.readouterr()
        assert "test message" in captured.out

    def test_with_creds_calls_telegram(self, alerter_with_creds):
        with patch("ops.alerts.requests.post") as mock_post:
            mock_post.return_value.status_code = 200
            alerter_with_creds.send("hello")
            mock_post.assert_called_once()
            call_kwargs = mock_post.call_args
            assert "test-token" in call_kwargs[0][0]
            assert call_kwargs[1]["json"]["chat_id"] == "test-chat"

    def test_telegram_failure_falls_back_to_stdout(self, alerter_with_creds, capsys):
        with patch("ops.alerts.requests.post", side_effect=Exception("connection error")):
            alerter_with_creds.send("fallback message")
        captured = capsys.readouterr()
        assert "fallback message" in captured.out


class TestFormatDashboard:
    def test_returns_string(self, regime_snap, flow_snap, structure):
        longs = [StockRank("RELIANCE", 5.0, 3.0, 5, True)]
        shorts = [StockRank("TATAMOTORS", -4.0, -2.0, 1, False)]
        result = format_dashboard(regime_snap, flow_snap, structure, longs, shorts)
        assert isinstance(result, str)

    def test_contains_regime(self, regime_snap, flow_snap, structure):
        result = format_dashboard(regime_snap, flow_snap, structure, [], [])
        assert "TREND_UP" in result

    def test_contains_vix(self, regime_snap, flow_snap, structure):
        result = format_dashboard(regime_snap, flow_snap, structure, [], [])
        assert "12.5" in result

    def test_contains_bias(self, regime_snap, flow_snap, structure):
        result = format_dashboard(regime_snap, flow_snap, structure, [], [])
        assert "LONG" in result

    def test_contains_structure(self, regime_snap, flow_snap, structure):
        result = format_dashboard(regime_snap, flow_snap, structure, [], [])
        assert "Long futures" in result

    def test_longs_appear_in_output(self, regime_snap, flow_snap, structure):
        longs = [StockRank("RELIANCE", 5.0, 3.0, 5, True)]
        result = format_dashboard(regime_snap, flow_snap, structure, longs, [])
        assert "RELIANCE" in result

    def test_shorts_appear_in_output(self, regime_snap, flow_snap, structure):
        shorts = [StockRank("TATAMOTORS", -4.0, -2.0, 1, False)]
        result = format_dashboard(regime_snap, flow_snap, structure, [], shorts)
        assert "TATAMOTORS" in result
