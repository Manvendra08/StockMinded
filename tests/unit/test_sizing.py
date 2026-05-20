"""Tests for risk/sizing.py — pure functions, no external dependencies."""
import pytest
from risk.sizing import directional_size, option_structure_size, SizeResult


class TestDirectionalSize:
    def test_basic_sizing(self):
        result = directional_size(capital=1_000_000, per_trade_pct=0.0075, entry=500.0, stop=490.0)
        assert result.qty == 750
        assert result.risk_rupees == pytest.approx(7500.0)
        assert result.notional == pytest.approx(750 * 500.0)

    def test_lot_size_floors_to_full_lots(self):
        result = directional_size(capital=1_000_000, per_trade_pct=0.0075, entry=500.0, stop=490.0, lot_size=50)
        assert result.qty % 50 == 0
        assert result.qty == 750

    def test_zero_stop_distance_returns_invalid(self):
        result = directional_size(capital=1_000_000, per_trade_pct=0.0075, entry=500.0, stop=500.0)
        assert result.qty == 0
        assert result.notes == "invalid stop"

    def test_negative_stop_distance_returns_invalid(self):
        # stop above entry (short side) — abs handles it
        result = directional_size(capital=1_000_000, per_trade_pct=0.0075, entry=490.0, stop=500.0, direction="SHORT")
        assert result.qty > 0

    def test_risk_rupees_matches_capital_pct(self):
        capital = 7_000_000
        pct = 0.0075
        result = directional_size(capital=capital, per_trade_pct=pct, entry=1000.0, stop=995.0)
        budget = capital * pct
        assert result.risk_rupees <= budget

    def test_notes_format(self):
        result = directional_size(capital=1_000_000, per_trade_pct=0.0075, entry=500.0, stop=490.0, lot_size=50)
        assert "lots" in result.notes


class TestOptionStructureSize:
    def test_basic_sizing(self):
        result = option_structure_size(
            capital=7_000_000, per_trade_pct=0.0075, max_loss_per_lot=5000.0, lot_size=50
        )
        budget = 7_000_000 * 0.0075
        lots = int(budget // 5000)
        assert result.qty == lots * 50
        assert result.risk_rupees == pytest.approx(lots * 5000.0)

    def test_invalid_max_loss_returns_zero(self):
        result = option_structure_size(
            capital=1_000_000, per_trade_pct=0.0075, max_loss_per_lot=0.0, lot_size=50
        )
        assert result.qty == 0
        assert result.notes == "invalid max loss"

    def test_negative_max_loss_returns_zero(self):
        result = option_structure_size(
            capital=1_000_000, per_trade_pct=0.0075, max_loss_per_lot=-100.0, lot_size=50
        )
        assert result.qty == 0

    def test_notional_is_zero(self):
        result = option_structure_size(
            capital=1_000_000, per_trade_pct=0.0075, max_loss_per_lot=500.0, lot_size=50
        )
        assert result.notional == 0.0

    def test_risk_does_not_exceed_budget(self):
        capital = 7_000_000
        pct = 0.0075
        result = option_structure_size(
            capital=capital, per_trade_pct=pct, max_loss_per_lot=4321.0, lot_size=25
        )
        assert result.risk_rupees <= capital * pct
