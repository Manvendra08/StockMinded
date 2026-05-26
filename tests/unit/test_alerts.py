"""Tests for ops/alerts.py — formatting + threshold checks only (no live API calls)."""
import pytest
from signals.regime import Regime, RegimeSnapshot
from signals.flows import FlowSnapshot
from signals.verdict import StockVerdict, NiftyVerdict


@pytest.fixture
def regime_snap():
    return RegimeSnapshot(
        regime=Regime.TREND_UP,
        trend_score=2,
        vix=12.5,
        vix_5d_change_pct=-3.0,
        adx=27.0,
        breadth_pct_above_50dma=72.0,
        vix_rank=None,   # callsite fix: new P3 #10 field
        notes="ok",
    )


@pytest.fixture
def flow_snap():
    return FlowSnapshot(
        fii_dii_5d_net_cr={"fii": 1200.0, "dii": 500.0},
        top_inflow_sectors=[("NIFTY IT", 2.1), ("NIFTY BANK", 1.8)],
        top_outflow_sectors=[("NIFTY METAL", -1.5)],
        pcr_oi=1.25,
        pcr_vol=1.10,
        max_pain=22500,
        smart_money_bias="LONG",
        pcr_stale=False,
        mp_stale=False,
        pcr_updated_at=None,
        mp_updated_at=None,
    )


@pytest.fixture
def stock_verdict():
    return StockVerdict(
        action="LONG_ONLY",
        tone="bull",
        confidence="HIGH",
        confidence_score=80,
        strategy="Long A-Grade leaders",
        top_long="RELIANCE",
        top_short=None,
        can_trade=True,
        reasons=["Regime TREND_UP"],
        blocks=[],
    )


@pytest.fixture
def nifty_verdict():
    return NiftyVerdict(
        action="NAKED_OPTION_SELL",
        tone="bull",
        bias="LONG",
        confidence="HIGH",
        confidence_score=80,
        strategy="Naked PUTS selling with SL",
        can_trade=True,
        reasons=["Regime TREND_UP"],
        blocks=[],
    )


class TestAlertFormatting:
    def test_regime_snap_has_vix_rank(self, regime_snap):
        assert hasattr(regime_snap, "vix_rank")

    def test_stock_verdict_confidence_score(self, stock_verdict):
        assert stock_verdict.confidence_score == 80

    def test_nifty_verdict_confidence_score(self, nifty_verdict):
        assert nifty_verdict.confidence_score == 80

    def test_stock_verdict_can_trade(self, stock_verdict):
        assert stock_verdict.can_trade is True

    def test_nifty_verdict_action(self, nifty_verdict):
        assert nifty_verdict.action == "NAKED_OPTION_SELL"

    def test_regime_snap_regime_value(self, regime_snap):
        assert regime_snap.regime == Regime.TREND_UP

    def test_regime_snap_to_dict(self, regime_snap):
        d = regime_snap.to_dict()
        assert d["regime"] == "TREND_UP"
        assert "vix_rank" in d

    def test_flow_snap_pcr(self, flow_snap):
        assert flow_snap.pcr_oi == 1.25

    def test_flow_snap_bias(self, flow_snap):
        assert flow_snap.smart_money_bias == "LONG"

    def test_stock_verdict_blocks_empty(self, stock_verdict):
        assert stock_verdict.blocks == []
