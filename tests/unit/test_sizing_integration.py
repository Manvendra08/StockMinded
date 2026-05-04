"""Integration tests for sizing module with paper trader."""
import pytest
from risk.sizing import directional_size, SizeResult


class TestSizingFromStopDistance:
    """Test that position sizing correctly uses stop distance."""

    def test_wider_stop_reduces_position_size(self):
        """Wider stop distance should result in smaller position."""
        capital = 1_000_000
        per_trade_pct = 0.0075  # 0.75%
        
        # Tight stop: 2%
        result_tight = directional_size(
            capital=capital, per_trade_pct=per_trade_pct,
            entry=1000.0, stop=980.0  # 2% stop
        )
        
        # Wide stop: 5%
        result_wide = directional_size(
            capital=capital, per_trade_pct=per_trade_pct,
            entry=1000.0, stop=950.0  # 5% stop
        )
        
        # Wider stop should give smaller position
        assert result_wide.qty < result_tight.qty
        
        # But risk should be similar (within budget)
        budget = capital * per_trade_pct
        assert result_tight.risk_rupees <= budget
        assert result_wide.risk_rupees <= budget

    def test_higher_price_reduces_qty_for_same_risk(self):
        """Higher entry price should reduce quantity for same risk budget."""
        capital = 1_000_000
        per_trade_pct = 0.0075
        stop_pct = 0.02  # 2% stop
        
        # Low price stock
        result_low = directional_size(
            capital=capital, per_trade_pct=per_trade_pct,
            entry=100.0, stop=100.0 * (1 - stop_pct)
        )
        
        # High price stock
        result_high = directional_size(
            capital=capital, per_trade_pct=per_trade_pct,
            entry=1000.0, stop=1000.0 * (1 - stop_pct)
        )
        
        # Higher price should have lower quantity
        assert result_high.qty < result_low.qty
        
        # But risk should be similar
        assert abs(result_low.risk_rupees - result_high.risk_rupees) < 100  # Within rounding

    def test_lot_size_floors_to_valid_lots(self):
        """Position size should be floored to valid lot multiples."""
        result = directional_size(
            capital=1_000_000, per_trade_pct=0.0075,
            entry=500.0, stop=490.0, lot_size=50
        )
        
        assert result.qty % 50 == 0
        assert result.qty > 0

    def test_zero_risk_budget_returns_zero_qty(self):
        """Zero or negative risk budget should return zero quantity."""
        result = directional_size(
            capital=0, per_trade_pct=0.0075,
            entry=1000.0, stop=990.0
        )
        
        assert result.qty == 0
        assert result.risk_rupees == 0.0

    def test_risk_never_exceeds_budget(self):
        """Calculated risk should never exceed the risk budget."""
        capital = 7_000_000
        per_trade_pct = 0.0075
        budget = capital * per_trade_pct
        
        # Test various entry/stop combinations
        test_cases = [
            (1000.0, 990.0),   # 1% stop
            (2500.0, 2450.0),  # 2% stop
            (500.0, 475.0),    # 5% stop
            (10000.0, 9500.0), # 5% stop on high price
        ]
        
        for entry, stop in test_cases:
            result = directional_size(
                capital=capital, per_trade_pct=per_trade_pct,
                entry=entry, stop=stop
            )
            assert result.risk_rupees <= budget, f"Risk {result.risk_rupees} > budget {budget} for entry={entry}, stop={stop}"


class TestOptionSizing:
    """Test option structure sizing."""
    
    def test_option_sizing_uses_max_loss(self):
        """Option sizing should use max loss per lot, not stop distance."""
        from risk.sizing import option_structure_size
        
        capital = 7_000_000
        per_trade_pct = 0.0075
        max_loss_per_lot = 5000.0
        lot_size = 50
        
        result = option_structure_size(
            capital=capital, per_trade_pct=per_trade_pct,
            max_loss_per_lot=max_loss_per_lot, lot_size=lot_size
        )
        
        # Should calculate lots based on max loss, not entry/stop
        budget = capital * per_trade_pct
        expected_lots = int(budget // max_loss_per_lot)
        
        assert result.qty == expected_lots * lot_size
        assert result.risk_rupees == expected_lots * max_loss_per_lot
        assert result.notional == 0.0  # Options don't have notional in same way
