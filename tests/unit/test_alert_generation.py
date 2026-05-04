"""Tests for alert generation by regime in dashboard/server.py."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

# Import the function under test
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.server import _generate_trade_alerts


@pytest.fixture
def base_data():
    """Base signal data for testing."""
    return {
        "regime": {
            "name": "TREND_UP",
            "trend_score": 4,
            "vix": 12.5,
            "vix_5d_change_pct": -3.0,
            "adx": 27.0,
            "breadth_pct_above_50dma": 72.0,
        },
        "flows": {
            "fii_dii_5d": {"fii": 1200.0, "dii": 500.0},
            "top_inflow": [("NIFTY IT", 2.1)],
            "top_outflow": [("NIFTY METAL", -1.5)],
            "pcr_oi": 1.25,
            "pcr_vol": 0.95,
            "max_pain": 22100.0,
            "bias": "LONG",
        },
        "leaders": [
            {"symbol": "RELIANCE", "rs_slope": 5.0, "pct_vs_50dma": 3.0, "quintile": 5},
            {"symbol": "INFY", "rs_slope": 4.5, "pct_vs_50dma": 2.5, "quintile": 5},
        ],
        "laggards": [
            {"symbol": "TATAMOTORS", "rs_slope": -4.0, "pct_vs_50dma": -2.0, "quintile": 1},
        ],
        "risk": {
            "capital": 7_000_000,
            "per_trade_pct": 0.0075,
        },
        "nifty": {"close": 22000.0, "change_pct": 0.5},
        "banknifty": {"close": 48000.0, "change_pct": 0.8},
    }


class TestAlertGenerationByRegime:
    """Test that alerts are generated correctly based on market regime."""

    def test_long_alerts_in_trend_up(self, base_data):
        """TREND_UP regime should generate LONG alerts for leaders."""
        base_data["regime"]["name"] = "TREND_UP"
        base_data["regime"]["trend_score"] = 4
        base_data["flows"]["bias"] = "LONG"
        
        alerts = _generate_trade_alerts(base_data)
        
        # Should have NIFTY long alert
        nifty_longs = [a for a in alerts if a["symbol"] == "NIFTY" and a["direction"] == "LONG"]
        assert len(nifty_longs) >= 1
        
        # Should have stock long alerts for leaders
        stock_longs = [a for a in alerts if a["direction"] == "LONG" and a["symbol"] != "NIFTY"]
        assert len(stock_longs) >= 1
        assert stock_longs[0]["symbol"] in ["RELIANCE", "INFY"]

    def test_short_alerts_in_trend_down(self, base_data):
        """TREND_DOWN regime should generate SHORT alerts for laggards."""
        base_data["regime"]["name"] = "TREND_DOWN"
        base_data["regime"]["trend_score"] = -4
        base_data["regime"]["breadth_pct_above_50dma"] = 35.0
        base_data["flows"]["bias"] = "SHORT"
        
        alerts = _generate_trade_alerts(base_data)
        
        # Should have NIFTY short alert
        nifty_shorts = [a for a in alerts if a["symbol"] == "NIFTY" and a["direction"] == "SHORT"]
        assert len(nifty_shorts) >= 1
        
        # Should have stock short alerts for laggards
        stock_shorts = [a for a in alerts if a["direction"] == "SHORT"]
        assert len(stock_shorts) >= 1

    def test_no_longs_in_trend_down(self, base_data):
        """TREND_DOWN should not generate LONG stock alerts."""
        base_data["regime"]["name"] = "TREND_DOWN"
        base_data["regime"]["trend_score"] = -3
        base_data["regime"]["breadth_pct_above_50dma"] = 35.0
        base_data["flows"]["bias"] = "SHORT"
        
        alerts = _generate_trade_alerts(base_data)
        
        # Should not have stock long alerts
        stock_longs = [a for a in alerts if a["direction"] == "LONG" and a["symbol"] != "NIFTY"]
        assert len(stock_longs) == 0

    def test_no_shorts_in_trend_up(self, base_data):
        """TREND_UP should not generate SHORT stock alerts."""
        base_data["regime"]["name"] = "TREND_UP"
        base_data["regime"]["trend_score"] = 4
        base_data["flows"]["bias"] = "LONG"
        
        alerts = _generate_trade_alerts(base_data)
        
        # Should not have stock short alerts
        stock_shorts = [a for a in alerts if a["direction"] == "SHORT" and a["symbol"] != "NIFTY"]
        assert len(stock_shorts) == 0

    def test_range_regime_prefers_options(self, base_data):
        """RANGE regimes should prefer defined-risk options strategies."""
        base_data["regime"]["name"] = "RANGE_LOW_VOL"
        base_data["regime"]["trend_score"] = 0
        base_data["flows"]["max_pain"] = 22000.0
        
        alerts = _generate_trade_alerts(base_data)
        
        # Should have neutral/options alert
        neutral = [a for a in alerts if a["direction"] == "NEUTRAL"]
        assert len(neutral) >= 1
        assert "Iron Condor" in neutral[0].get("entry_trigger", "") or "Max Pain" in neutral[0].get("entry_trigger", "")

    def test_vix_filter_blocks_trades(self, base_data):
        """High VIX (>24) should block all trades."""
        base_data["regime"]["vix"] = 25.0
        
        alerts = _generate_trade_alerts(base_data)
        
        # Should have AVOID alert
        avoid = [a for a in alerts if a["direction"] == "AVOID"]
        assert len(avoid) >= 1
        assert "VIX" in avoid[0].get("no_trade_reason", "")

    def test_late_day_filter_blocks_entries(self, base_data):
        """Entries after 14:45 IST should be blocked."""
        # Mock datetime to be after 14:45
        with patch("dashboard.server.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 4, 28, 14, 50, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            mock_dt.side_effect = lambda *args, **kw: datetime(*args, **kw) if args else datetime.now()
            
            alerts = _generate_trade_alerts(base_data)
            
            # Should not have actionable stock/index trades
            actionable = [a for a in alerts if a["direction"] in ("LONG", "SHORT") and a["symbol"] in ("NIFTY", "RELIANCE", "INFY")]
            assert len(actionable) == 0

    def test_alert_structure_has_required_fields(self, base_data):
        """All alerts should have the required structured fields."""
        alerts = _generate_trade_alerts(base_data)
        
        required_fields = [
            "symbol", "direction", "entry_trigger", "entry_price", "stop",
            "target1", "target2", "trail_rule", "qty", "risk_rupees",
            "confidence", "no_trade_reason"
        ]
        
        for alert in alerts:
            for field in required_fields:
                assert field in alert, f"Missing field {field} in alert: {alert}"

    def test_alert_has_metadata_fields(self, base_data):
        """Actionable alerts should have tracking metadata."""
        alerts = _generate_trade_alerts(base_data)
        actionable = [a for a in alerts if a["direction"] in ("LONG", "SHORT")]
        
        if actionable:
            alert = actionable[0]
            # Check for enhanced metadata fields
            assert "planned_risk" in alert
            assert "entry_rule" in alert
            assert "trail_rule" in alert
            assert "source_regime" in alert
            assert "flow_bias" in alert


class TestBankNiftyDivergence:
    """Test BankNifty divergence alerts."""

    def test_bn_divergence_generates_alert(self, base_data):
        """Large BN/Nifty divergence should generate divergence alert."""
        base_data["banknifty"]["change_pct"] = 2.0  # BN up 2%
        base_data["nifty"]["change_pct"] = 0.5  # Nifty up 0.5%
        
        alerts = _generate_trade_alerts(base_data)
        
        bn_alerts = [a for a in alerts if a["symbol"] == "BANKNIFTY"]
        assert len(bn_alerts) >= 1
        assert "Divergence" in bn_alerts[0].get("entry_trigger", "")

    def test_small_divergence_no_alert(self, base_data):
        """Small divergence (<0.5%) should not generate alert."""
        base_data["banknifty"]["change_pct"] = 0.7
        base_data["nifty"]["change_pct"] = 0.5
        
        alerts = _generate_trade_alerts(base_data)
        
        bn_alerts = [a for a in alerts if a["symbol"] == "BANKNIFTY" and "Divergence" in a.get("entry_trigger", "")]
        assert len(bn_alerts) == 0
