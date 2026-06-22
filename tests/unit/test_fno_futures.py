"""Tests for F&O stock futures entry and expiry exits."""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.paper_trader import auto_enter_from_alerts, check_and_close_trades, get_futures_expiry

@pytest.fixture(autouse=True)
def mock_atomic_db_update(tmp_path):
    """Globally patch atomic_db_update and DATA_FILE to avoid lock file issues."""
    import contextlib
    import dashboard.paper_trader

    test_db = tmp_path / "paper_trades_test.json"
    with patch("dashboard.paper_trader.DATA_FILE", test_db), \
         patch("dashboard.paper_trader.LOCK_FILE", test_db.with_suffix(".lock")), \
         patch("dashboard.paper_trader.BAK_FILE", test_db.with_suffix(".bak")), \
         patch("dashboard.paper_trader.TMP_FILE", test_db.with_suffix(".tmp")), \
         patch("dashboard.paper_trader.get_fno_lot_sizes", return_value={"RELIANCE": 250}):

        @contextlib.contextmanager
        def mock_update():
            db = dashboard.paper_trader._load_db()
            yield db
            dashboard.paper_trader._save_db(db)

        with patch("dashboard.paper_trader.atomic_db_update", side_effect=mock_update):
            yield


@pytest.fixture
def mock_db_empty():
    """Mock empty database with custom settings."""
    return {
        "trades": [],
        "option_trades": [],
        "daily_summaries": [],
        "strategy_notes": [],
        "settings": {
            "capital_per_trade": 500000.0,
            "sl_pct": 2.0,
            "tgt_pct": 4.0,
            "min_confidence": "HIGH",
            "trail_sl": False,
        },
        "cumulative_pnl": 0.0,
    }


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


@patch("dashboard.paper_trader._get_ltp")
@patch("dashboard.paper_trader.is_market_open", return_value=True)
def test_fno_future_entry(mock_market_open, mock_ltp, mock_db_empty, mock_config):
    """F&O stock alerts should enter as FUTURE contracts with lot-aligned quantities."""
    mock_ltp.return_value = 2500.0
    alert = {
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry_trigger": "Breakout",
        "entry_price": 2500.0,
        "stop": 2450.0,
        "target1": 2600.0,
        "confidence": "HIGH",
        "evidence": [],
        "source_regime": "TREND_UP",
        "flow_bias": "LONG",
    }

    mock_db_empty["settings"]["capital_per_trade"] = 800000.0

    with patch("dashboard.paper_trader._load_db", return_value=mock_db_empty), \
         patch("dashboard.paper_trader._save_db"), \
         patch("dashboard.paper_trader._now_ist") as mock_now:
        
        mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

        result = auto_enter_from_alerts([alert], cfg=mock_config)
        
        assert len(result) == 1
        trade = result[0]
        assert trade["symbol"] == "RELIANCE"
        assert trade["type"] == "FUTURE"
        assert trade["instrument"] == "RELIANCE FUT"
        assert trade["expiry_date"] is not None
        # Max notional cap is 500,000. 500,000 / 2500 = 200.
        # But wait! Sizing budget: 7,000,000 * 0.0075 = 52,500 risk budget.
        # per-unit-risk = 50. 52,500 / 50 = 1050 raw qty.
        # Cap is 200 (which is floored to lot_size 250 -> 0).
        # Ah, let's check: if cap is 200 and lot size is 250, then floor to lot size is 0!
        # Let's adjust the capital cap in settings so it doesn't floor to 0.
        # If capital_per_trade is 1,000,000: 1,000,000 / 2500 = 400.
        # 400 is floored to lot size 250 -> 250.
        # Let's verify this behavior.
        
@patch("dashboard.paper_trader._get_ltp")
@patch("dashboard.paper_trader.is_market_open", return_value=True)
def test_fno_future_entry_sizing_and_cap(mock_market_open, mock_ltp, mock_db_empty, mock_config):
    """Verify lot alignment and capital capping for F&O future entry."""
    mock_ltp.return_value = 2500.0
    alert = {
        "symbol": "RELIANCE",
        "direction": "LONG",
        "entry_price": 2500.0,
        "stop": 2450.0,
        "confidence": "HIGH",
    }
    
    # Increase capital cap so we can afford at least 1 lot (250 qty)
    mock_db_empty["settings"]["capital_per_trade"] = 800000.0  # 800,000 notional cap

    with patch("dashboard.paper_trader._load_db", return_value=mock_db_empty), \
         patch("dashboard.paper_trader._save_db"), \
         patch("dashboard.paper_trader._now_ist") as mock_now:
        
        mock_now.return_value = datetime(2026, 4, 28, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))

        result = auto_enter_from_alerts([alert], cfg=mock_config)
        
        assert len(result) == 1
        trade = result[0]
        # Notional cap: 800,000 / 2500 = 320 -> floored to 250 (1 lot)
        # Sizing budget: 7,000,000 * 0.0075 = 52,500 / 50 = 1050 -> 1000 (4 lots)
        # min(1000, 250) = 250 qty
        assert trade["qty"] == 250
        assert trade["type"] == "FUTURE"


@patch("dashboard.paper_trader._get_ltp_batch")
@patch("dashboard.paper_trader.is_market_open", return_value=True)
def test_fno_future_expiry_exit(mock_market_open, mock_ltp_batch):
    """Futures should close automatically on or after their expiry date."""
    mock_ltp_batch.return_value = {"RELIANCE": 2600.0}
    
    # 28th April is past 23rd April (assumed expiry)
    trade = {
        "id": 123,
        "symbol": "RELIANCE",
        "direction": "LONG",
        "type": "FUTURE",
        "instrument": "RELIANCE FUT",
        "entry_price": 2500.0,
        "qty": 250,
        "sl_price": 2400.0,
        "tgt_price": 2700.0,
        "entry_time": "2026-04-20 10:00:00",
        "entry_date": "2026-04-20",
        "expiry_date": "2026-04-23",
        "status": "OPEN",
    }
    
    mock_db = {
        "trades": [trade],
        "option_trades": [],
        "daily_summaries": [],
        "settings": {"trail_sl": False, "auto_close_eod": False},
    }
    
    with patch("dashboard.paper_trader._load_db", return_value=mock_db), \
         patch("dashboard.paper_trader._save_db"), \
         patch("dashboard.paper_trader._now_ist") as mock_now:
        
        # Today is April 24th, 2026 (past expiry date of April 23rd)
        mock_now.return_value = datetime(2026, 4, 24, 11, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        
        closed = check_and_close_trades()
        
        assert len(closed) == 1
        assert closed[0]["id"] == 123
        assert closed[0]["exit_reason"] == "EXPIRY"
        assert closed[0]["exit_price"] == 2600.0
        assert closed[0]["status"] == "CLOSED"
        assert closed[0]["pnl"] == 25000.0 # (2600 - 2500) * 250
