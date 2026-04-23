"""Tests for risk/guardrails.py."""
import pytest
from risk.guardrails import Guardrails, GuardrailCheck

CAPITAL = 7_000_000

CFG = {
    "account": {"capital": CAPITAL},
    "risk": {
        "per_trade_pct": 0.0075,
        "concurrent_open_pct": 0.03,
        "daily_stop_pct": 0.02,
        "monthly_stop_pct": 0.06,
        "margin_util_cap": 0.60,
        "correlation_max": 0.70,
    },
}


@pytest.fixture
def g():
    return Guardrails(CFG)


class TestGuardrailCheckNewTrade:
    def test_all_clear(self, g):
        result = g.check_new_trade(
            proposed_risk=5_000,
            open_risk=100_000,
            day_pnl=0,
            month_pnl=0,
            margin_used_pct=0.3,
        )
        assert result.ok
        assert result.reasons == []

    def test_daily_stop_blocks(self, g):
        daily_loss = -(CAPITAL * 0.02)
        result = g.check_new_trade(
            proposed_risk=5_000,
            open_risk=0,
            day_pnl=daily_loss,
            month_pnl=0,
            margin_used_pct=0.1,
        )
        assert not result.ok
        assert any("Daily stop" in r for r in result.reasons)

    def test_monthly_stop_blocks(self, g):
        monthly_loss = -(CAPITAL * 0.06)
        result = g.check_new_trade(
            proposed_risk=5_000,
            open_risk=0,
            day_pnl=0,
            month_pnl=monthly_loss,
            margin_used_pct=0.1,
        )
        assert not result.ok
        assert any("Monthly stop" in r for r in result.reasons)

    def test_concurrent_risk_cap_blocks(self, g):
        result = g.check_new_trade(
            proposed_risk=100_000,
            open_risk=200_000,  # already 200k, adding 100k = 300k > 3% of 7M (210k)
            day_pnl=0,
            month_pnl=0,
            margin_used_pct=0.1,
        )
        assert not result.ok
        assert any("Concurrent" in r for r in result.reasons)

    def test_margin_cap_blocks(self, g):
        result = g.check_new_trade(
            proposed_risk=5_000,
            open_risk=0,
            day_pnl=0,
            month_pnl=0,
            margin_used_pct=0.65,
        )
        assert not result.ok
        assert any("Margin" in r for r in result.reasons)

    def test_correlation_cap_blocks(self, g):
        result = g.check_new_trade(
            proposed_risk=5_000,
            open_risk=0,
            day_pnl=0,
            month_pnl=0,
            margin_used_pct=0.1,
            max_correlation_vs_open=0.80,
        )
        assert not result.ok
        assert any("orrelation" in r for r in result.reasons)

    def test_multiple_violations_all_in_reasons(self, g):
        result = g.check_new_trade(
            proposed_risk=5_000,
            open_risk=0,
            day_pnl=-(CAPITAL * 0.02),
            month_pnl=-(CAPITAL * 0.06),
            margin_used_pct=0.65,
        )
        assert not result.ok
        assert len(result.reasons) >= 3

    def test_bool_conversion(self, g):
        ok = g.check_new_trade(
            proposed_risk=1_000, open_risk=0, day_pnl=0, month_pnl=0, margin_used_pct=0.1
        )
        assert bool(ok) is True


class TestEodFlatten:
    def test_triggered_at_threshold(self, g):
        assert g.eod_flatten_required(-(CAPITAL * 0.02)) is True

    def test_not_triggered_above_threshold(self, g):
        assert g.eod_flatten_required(-(CAPITAL * 0.01)) is False

    def test_positive_pnl_not_triggered(self, g):
        assert g.eod_flatten_required(10_000) is False


class TestSizeHalveNextMonth:
    def test_triggered(self, g):
        assert g.size_halve_next_month(-(CAPITAL * 0.06)) is True

    def test_not_triggered(self, g):
        assert g.size_halve_next_month(-(CAPITAL * 0.03)) is False
