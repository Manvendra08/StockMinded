"""Tests for paper auto-entry with risk guardrails."""
import pytest
import json
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open
from datetime import datetime, timezone, timedelta

# Setup path
import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.paper_trader import auto_enter_from_alerts, _load_db, _save_db, DATA_FILE


@pytest.fixture(autouse=True)
def mock_atomic_db_update(tmp_path):
    """Globally patch atomic_db_update and DATA_FILE to avoid msvcrt lock file issues in tests and prevent leaks."""
    import contextlib
    import dashboard.paper_trader

    test_db = tmp_path / "paper_trades_test.json"
    with patch("dashboard.paper_trader.DATA_FILE", test_db), \
         patch("dashboard.paper_trader.LOCK_FILE", test_db.with_suffix(".lock")), \
         patch("dashboard.paper_trader.BAK_FILE", test_db.with_suffix(".bak")), \
         patch("dashboard.paper_trader.TMP_FILE", test_db.with_suffix(".tmp")):

        @contextlib.contextmanager
        def mock_update():
            db = dashboard.paper_trader._load_db()
            yield db
            dashboard.paper_trader._save_db(db)

        with patch("dashboard.paper_trader.atomic_db_update", side_effect=mock_update):
            yield


@pytest.fixture
def mock_db_empty(tmp_path):
    """Mock empty database."""
    mock_data = {
        "trades": [],
        "option_trades": [],
        "daily_summaries": [],
        "strategy_notes": [],
        "settings": {"capital_per_trade": 500000.0, "sl_pct": 2.0, "tgt_pct": 4.0, "min_confidence": "HIGH"},
        "cumulative_pnl": 0.0,
    }
    with patch("dashboard.paper_trader._load_db", return_value=mock_data):
        with patch("dashboard.paper_trader._save_db"):
            yield mock_data


@pytest.fixture
def mock_config():
    """Mock config with risk parameters."""
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
def valid_alert():
    """Valid alert that should pass all checks."""
    return {
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry_trigger": "Breakout",
        "entry_price": 2500.0,
        "stop": 2450.0,
        "target1": 2600.0,
        "target2": 2700.0,
        "trail_rule": "Trail after T1",
        "qty": 100,
        "risk_rupees": 5000.0,
        "confidence": "HIGH",
        "no_trade_reason": None,
        "evidence": ["RS slope: 5.0"],
        "planned_risk": 5000.0,
        "entry_rule": "Breakout in TREND_UP",
        "source_regime": "TREND_UP",
        "flow_bias": "LONG",
    }


class TestAutoEntryRiskGates:
    """Test that auto-entry respects risk guardrails."""

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_all_clear_enters_trade(self, mock_market_open, mock_ltp, mock_db_empty, mock_config, valid_alert):
        """Trade should enter when all risk gates pass."""
        mock_ltp.return_value = 2500.0
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            assert len(result) == 1
            assert result[0]["symbol"] == "RELIANCE"
            assert result[0]["status"] == "OPEN"

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_daily_stop_blocks_trade(self, mock_market_open, mock_ltp, mock_db_empty, mock_config, valid_alert):
        """Trade should be blocked if daily stop is hit."""
        mock_ltp.return_value = 2500.0
        
        # Set up database with losing trades that hit daily stop
        mock_db_empty["trades"] = [
            {
                "id": 1, "symbol": "TEST", "status": "CLOSED", "entry_date": datetime.now().date().isoformat(),
                "pnl": -140000,  # -2% of 7M capital
            }
        ]
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            assert len(result) == 0

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_concurrent_risk_cap_blocks_trade(self, mock_market_open, mock_ltp, mock_db_empty, mock_config, valid_alert):
        """Trade should be blocked if adding it exceeds concurrent risk cap."""
        mock_ltp.return_value = 2500.0
        
        # Set up database with open trades near risk cap
        # 3% of 7M = 210,000 concurrent risk cap
        mock_db_empty["trades"] = [
            {
                "id": 1, "symbol": "TEST1", "status": "OPEN", "risk_rupees": 208000,
            }
        ]
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            assert len(result) == 0

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_duplicate_today_blocks_trade(self, mock_market_open, mock_ltp, mock_db_empty, mock_config, valid_alert):
        """Trade should be blocked if symbol already traded today."""
        mock_ltp.return_value = 2500.0
        
        # Set up database with same symbol traded today
        mock_db_empty["trades"] = [
            {
                "id": 1, "symbol": "RELIANCE", "status": "CLOSED",
                "entry_date": datetime.now().date().isoformat(),
            }
        ]
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            assert len(result) == 0

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_low_confidence_filtered(self, mock_market_open, mock_ltp, mock_db_empty, mock_config, valid_alert):
        """LOW confidence alerts should be filtered by default."""
        mock_ltp.return_value = 2500.0
        valid_alert["confidence"] = "LOW"
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            assert len(result) == 0

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_late_day_blocks_entry(self, mock_market_open, mock_ltp, mock_db_empty, mock_config, valid_alert):
        """Entries after 15:15 should be blocked."""
        mock_ltp.return_value = 2500.0
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 15, 20, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            assert len(result) == 0

    @patch("dashboard.paper_trader._get_ltp")
    def test_holiday_blocks_auto_entry(self, mock_ltp, mock_db_empty, mock_config, valid_alert):
        """Auto-entry should not occur on NSE holidays listed in config."""
        mock_ltp.return_value = 2500.0

        from datetime import timezone, timedelta
        # Use a known holiday from config/nse_holidays_2026.csv -> 2026-05-01
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 5, 1, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)

            assert len(result) == 0


class TestSizingIntegration:
    """Test that sizing module is used correctly."""

    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_sizing_calculates_qty_from_stop(self, mock_market_open, mock_db_empty, mock_config, valid_alert):
        """Position size should be calculated from stop distance."""
        # Alert with wide stop should result in smaller position
        valid_alert["entry_price"] = 2500.0
        valid_alert["stop"] = 2400.0  # 4% stop
        valid_alert["risk_rupees"] = 10000.0  # 4% of 250k
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            assert len(result) == 1
            # Risk should be within budget (0.75% of 7M = 52,500)
            assert result[0]["risk_rupees"] <= 52500.0
            assert result[0]["planned_risk"] <= 52500.0

    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_invalid_stop_rejected(self, mock_market_open, mock_ltp, mock_db_empty, mock_config, valid_alert):
        """Alert with invalid stop (same as entry) should be rejected."""
        mock_ltp.return_value = 2500.0
        valid_alert["stop"] = 2500.0  # Same as entry
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            result = auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            # Should either not enter or enter with qty=0
            if result:
                assert result[0]["qty"] == 0 or result[0]["status"] != "OPEN"


class TestSkippedTradeLogging:
    """Test that skipped trades are logged to journal."""

    @patch("dashboard.paper_trader.Journal")
    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_low_confidence_logged_as_skipped(self, mock_market_open, mock_ltp, mock_journal_cls, mock_db_empty, mock_config, valid_alert):
        """LOW confidence alerts should be logged as skipped."""
        mock_ltp.return_value = 2500.0
        valid_alert["confidence"] = "LOW"
        mock_journal = MagicMock()
        mock_journal_cls.return_value = mock_journal
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            # Should have logged skipped trade
            mock_journal.log_skipped_trade.assert_called_once()
            call_args = mock_journal.log_skipped_trade.call_args
            assert call_args[0][3] == "CONFIDENCE_FILTER"

    @patch("dashboard.paper_trader.Journal")
    @patch("dashboard.paper_trader._get_ltp")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_risk_gate_violation_logged(self, mock_market_open, mock_ltp, mock_journal_cls, mock_db_empty, mock_config, valid_alert):
        """Risk gate violations should be logged with reason."""
        mock_ltp.return_value = 2500.0
        
        # Set up to trigger daily stop
        mock_db_empty["trades"] = [
            {
                "id": 1, "symbol": "TEST", "status": "CLOSED", "entry_date": datetime.now().date().isoformat(),
                "pnl": -140000,
            }
        ]
        mock_journal = MagicMock()
        mock_journal_cls.return_value = mock_journal
        
        with patch("dashboard.paper_trader._now_ist") as mock_now:
            mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
            
            auto_enter_from_alerts([valid_alert], cfg=mock_config)
            
            mock_journal.log_skipped_trade.assert_called_once()
            call_args = mock_journal.log_skipped_trade.call_args
            assert call_args[0][3] == "RISK_GATE"
            assert "Daily stop" in call_args[0][6]
