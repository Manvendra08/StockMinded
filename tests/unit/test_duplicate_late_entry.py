"""Tests for duplicate trade blocking and late-entry prevention."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.paper_trader import auto_enter_from_alerts, _load_db


@pytest.fixture
def mock_db_with_trade(tmp_path):
    """Mock database with an existing trade today."""
    today = datetime.now().date().isoformat()
    mock_data = {
        "trades": [
            {
                "id": 1, "symbol": "RELIANCE", "direction": "LONG",
                "status": "OPEN", "entry_date": today,
                "entry_price": 2500.0, "qty": 100, "risk_rupees": 5000.0,
            }
        ],
        "option_trades": [],
        "daily_summaries": [],
        "strategy_notes": [],
        "settings": {"capital_per_trade": 500000.0, "sl_pct": 2.0, "tgt_pct": 4.0, "min_confidence": "HIGH"},
        "cumulative_pnl": 0.0,
    }
    return mock_data


@pytest.fixture
def mock_config():
    """Mock config."""
    return {
        "account": {"capital": 7_000_000},
        "risk": {
            "per_trade_pct": 0.0075,
            "concurrent_open_pct": 0.03,
            "daily_stop_pct": 0.02,
            "monthly_stop_pct": 0.06,
            "margin_util_cap": 0.60,
            "correlation_max": 0.70,
        },
        "paths": {"journal_db": ":memory:"},
        "alerts": {},
    }


@pytest.fixture
def alert_reliance():
    """Alert for RELIANCE."""
    return {
        "symbol": "RELIANCE", "direction": "LONG", "entry_trigger": "Breakout",
        "entry_price": 2500.0, "stop": 2450.0, "target1": 2600.0, "target2": 2700.0,
        "trail_rule": "Trail", "qty": 100, "risk_rupees": 5000.0,
        "confidence": "HIGH", "no_trade_reason": None, "evidence": [],
        "planned_risk": 5000.0, "entry_rule": "", "source_regime": "TREND_UP",
        "flow_bias": "LONG",
    }


class TestDuplicateTradeBlocking:
    """Test that duplicate trades for same symbol on same day are blocked."""

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_same_symbol_today_blocked(self, mock_market_open, mock_ltp, mock_db_with_trade, mock_config, alert_reliance):
        """Should block trade if symbol already traded today."""
        mock_ltp.return_value = 2500.0
        
        with patch("dashboard.paper_trader._load_db", return_value=mock_db_with_trade):
            with patch("dashboard.paper_trader._save_db"):
                with patch("dashboard.paper_trader._now_ist") as mock_now:
                    mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
                    
                    result = auto_enter_from_alerts([alert_reliance], cfg=mock_config)
                    
                    # Should not enter duplicate
                    assert len(result) == 0

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_different_symbol_allowed(self, mock_market_open, mock_ltp, mock_db_with_trade, mock_config, alert_reliance):
        """Should allow trade for different symbol."""
        mock_ltp.return_value = 2600.0
        alert_reliance["symbol"] = "INFY"  # Different symbol
        
        with patch("dashboard.paper_trader._load_db", return_value=mock_db_with_trade):
            with patch("dashboard.paper_trader._save_db"):
                with patch("dashboard.paper_trader._now_ist") as mock_now:
                    mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
                    
                    result = auto_enter_from_alerts([alert_reliance], cfg=mock_config)
                    
                    # Should enter different symbol
                    assert len(result) == 1
                    assert result[0]["symbol"] == "INFY"

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_closed_trade_today_still_blocks(self, mock_market_open, mock_ltp, mock_config, alert_reliance):
        """Should block even if earlier trade was closed today."""
        mock_ltp.return_value = 2500.0
        today = datetime.now().date().isoformat()
        
        mock_db = {
            "trades": [
                {
                    "id": 1, "symbol": "RELIANCE", "direction": "LONG",
                    "status": "CLOSED", "entry_date": today,  # Closed today
                    "exit_reason": "TARGET_HIT", "pnl": 2000.0,
                }
            ],
            "option_trades": [],
            "daily_summaries": [],
            "strategy_notes": [],
            "settings": {"capital_per_trade": 500000.0, "min_confidence": "HIGH"},
            "cumulative_pnl": 2000.0,
        }
        
        with patch("dashboard.paper_trader._load_db", return_value=mock_db):
            with patch("dashboard.paper_trader._save_db"):
                with patch("dashboard.paper_trader._now_ist") as mock_now:
                    mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
                    
                    result = auto_enter_from_alerts([alert_reliance], cfg=mock_config)
                    
                    # Should still block (one trade per symbol per day)
                    assert len(result) == 0


class TestLateEntryBlocking:
    """Test that entries after market cutoff are blocked."""

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_entry_before_1515_allowed(self, mock_market_open, mock_ltp, mock_db_with_trade, mock_config, alert_reliance):
        """Entries before 15:15 should be allowed."""
        mock_ltp.return_value = 2500.0
        alert_reliance["symbol"] = "INFY"  # Different symbol to avoid dup check
        
        with patch("dashboard.paper_trader._load_db", return_value=mock_db_with_trade):
            with patch("dashboard.paper_trader._save_db"):
                with patch("dashboard.paper_trader._now_ist") as mock_now:
                    # 15:14 - just before cutoff
                    mock_now.return_value = datetime(2026, 4, 28, 15, 14, tzinfo=timezone(timedelta(hours=5, minutes=30)))
                    
                    result = auto_enter_from_alerts([alert_reliance], cfg=mock_config)
                    
                    assert len(result) == 1

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_entry_at_1515_blocked(self, mock_market_open, mock_ltp, mock_db_with_trade, mock_config, alert_reliance):
        """Entries at or after 15:15 should be blocked."""
        mock_ltp.return_value = 2500.0
        
        with patch("dashboard.paper_trader._load_db", return_value=mock_db_with_trade):
            with patch("dashboard.paper_trader._save_db"):
                with patch("dashboard.paper_trader._now_ist") as mock_now:
                    # 15:15 - at cutoff
                    mock_now.return_value = datetime(2026, 4, 28, 15, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
                    
                    result = auto_enter_from_alerts([alert_reliance], cfg=mock_config)
                    
                    assert len(result) == 0

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_entry_after_1515_blocked(self, mock_market_open, mock_ltp, mock_db_with_trade, mock_config, alert_reliance):
        """Entries well after 15:15 should be blocked."""
        mock_ltp.return_value = 2500.0
        
        with patch("dashboard.paper_trader._load_db", return_value=mock_db_with_trade):
            with patch("dashboard.paper_trader._save_db"):
                with patch("dashboard.paper_trader._now_ist") as mock_now:
                    # 15:30 - market close
                    mock_now.return_value = datetime(2026, 4, 28, 15, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
                    
                    result = auto_enter_from_alerts([alert_reliance], cfg=mock_config)
                    
                    assert len(result) == 0

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=False)
    def test_outside_market_hours_blocked(self, mock_market_open, mock_ltp, mock_db_with_trade, mock_config, alert_reliance):
        """Entries outside market hours should be blocked."""
        mock_ltp.return_value = 2500.0
        
        with patch("dashboard.paper_trader._load_db", return_value=mock_db_with_trade):
            with patch("dashboard.paper_trader._save_db"):
                with patch("dashboard.paper_trader._now_ist") as mock_now:
                    # Weekend
                    mock_now.return_value = datetime(2026, 4, 26, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))  # Saturday
                    
                    result = auto_enter_from_alerts([alert_reliance], cfg=mock_config)
                    
                    assert len(result) == 0
