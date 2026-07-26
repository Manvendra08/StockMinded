"""Tests for standard percentage-buffered trailing stop loss logic."""
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

import sys
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.paper_trader import check_and_close_trades, _load_db, _save_db, DEFAULT_SETTINGS

@pytest.fixture(autouse=True)
def mock_atomic_db_update(tmp_path):
    """Globally patch atomic_db_update and DATA_FILE to prevent DB file locks/leaks."""
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

class TestTrailingStopLoss:
    """Test standard trailing stop loss logic in paper_trader.py."""

    @patch("dashboard.paper_trader._get_ltp_batch")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_trailing_sl_does_not_choke_on_small_gain(self, mock_market_open, mock_ltp_batch, tmp_path):
        """Trailing SL must NOT move below the trail_activation_pct threshold."""
        # Entry price = 100, sl_pct = 3%, trail_activation_pct = 2.0%
        # A tiny gain of 0.10% is below the 2% threshold → SL must stay at initial 97.0
        trade = {
            "id": 1,
            "symbol": "TEST",
            "direction": "LONG",
            "status": "OPEN",
            "entry_price": 100.0,
            "peak_price": 100.0,
            "sl_price": 97.0,
            "sl_pct": 3.0,
            "tgt_price": 105.0,
            "tgt_pct": 5.0,
            "qty": 100,
            "entry_time": "2026-06-09 10:00:00",
            "entry_date": "2026-06-09"
        }

        mock_data = {
            "trades": [trade],
            "option_trades": [],
            "daily_summaries": [],
            "strategy_notes": [],
            "settings": {"trail_sl": True, "trail_activation_pct": 2.0, "sl_pct": 3.0, "auto_close_eod": False},
            "cumulative_pnl": 0.0,
            "version": 1
        }

        with patch("dashboard.paper_trader._load_db", return_value=mock_data), \
             patch("dashboard.paper_trader._save_db") as mock_save:
            
            # Step 1: Price ticks up slightly to 100.10 (+0.10%, below 2% threshold)
            mock_ltp_batch.return_value = {"TEST": 100.10}
            closed = check_and_close_trades()
            
            # Trade must stay open; SL must NOT have moved (still 97.0)
            assert len(closed) == 0
            assert trade["status"] == "OPEN"
            assert trade["sl_price"] == 97.0, "SL must not trail below trail_activation_pct"
            
            # Step 2: Price ticks back down to 100.00 — still safe at SL=97.0
            mock_ltp_batch.return_value = {"TEST": 100.00}
            closed = check_and_close_trades()
            assert len(closed) == 0
            assert trade["status"] == "OPEN"
            assert trade["sl_price"] == 97.0

    @patch("dashboard.paper_trader._get_ltp_batch")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_trailing_sl_activates_after_threshold(self, mock_market_open, mock_ltp_batch):
        """Trailing SL should only activate once profit exceeds trail_activation_pct."""
        trade = {
            "id": 1,
            "symbol": "TEST",
            "direction": "LONG",
            "status": "OPEN",
            "entry_price": 100.0,
            "peak_price": 100.0,
            "sl_price": 97.0,
            "sl_pct": 3.0,
            "tgt_price": 120.0,
            "tgt_pct": 20.0,
            "qty": 100,
            "entry_time": "2026-06-09 10:00:00",
            "entry_date": "2026-06-09"
        }

        mock_data = {
            "trades": [trade],
            "option_trades": [],
            "daily_summaries": [],
            "strategy_notes": [],
            "settings": {"trail_sl": True, "trail_activation_pct": 2.0, "sl_pct": 3.0, "auto_close_eod": False},
            "cumulative_pnl": 0.0,
            "version": 1
        }

        with patch("dashboard.paper_trader._load_db", return_value=mock_data), \
             patch("dashboard.paper_trader._save_db") as mock_save:
            
            # Step 1: Price goes to 101.50 (+1.50%) — still BELOW 2% threshold → no trail
            mock_ltp_batch.return_value = {"TEST": 101.50}
            closed = check_and_close_trades()
            assert len(closed) == 0
            assert trade["status"] == "OPEN"
            assert trade["sl_price"] == 97.0, "SL must not trail below trail_activation_pct"

            # Step 2: Price goes to 102.50 (+2.50%) — ABOVE 2% threshold → trail activates
            mock_ltp_batch.return_value = {"TEST": 102.50}
            closed = check_and_close_trades()
            assert len(closed) == 0
            assert trade["status"] == "OPEN"
            assert trade["peak_price"] == 102.50
            assert trade["sl_price"] == round(102.50 * 0.97, 2)  # 99.43
            
            # Step 2: Price ticks back down to 100.00 (below activation threshold)
            # Trail SL holds at last trailed value; does NOT regress to original SL.
            mock_ltp_batch.return_value = {"TEST": 100.00}
            closed = check_and_close_trades()
            assert len(closed) == 0
            assert trade["status"] == "OPEN"
            assert trade["sl_price"] == round(102.50 * 0.97, 2)  # 99.42 — lock held below threshold

    @patch("dashboard.paper_trader._get_ltp_batch")
    @patch("dashboard.paper_trader.is_market_open", return_value=True)
    def test_trailing_sl_updates_and_triggers_stop_out(self, mock_market_open, mock_ltp_batch):
        """Stop loss should rise with major gains and hit if price falls back by sl_pct."""
        trade = {
            "id": 1,
            "symbol": "TEST",
            "direction": "LONG",
            "status": "OPEN",
            "entry_price": 100.0,
            "peak_price": 100.0,
            "sl_price": 97.0,
            "sl_pct": 3.0,
            "tgt_price": 120.0,
            "tgt_pct": 20.0,
            "qty": 100,
            "entry_time": "2026-06-09 10:00:00",
            "entry_date": "2026-06-09"
        }

        mock_data = {
            "trades": [trade],
            "option_trades": [],
            "daily_summaries": [],
            "strategy_notes": [],
            "settings": {"trail_sl": True, "trail_activation_pct": 2.0, "sl_pct": 3.0, "auto_close_eod": False},
            "cumulative_pnl": 0.0,
            "version": 1
        }

        with patch("dashboard.paper_trader._load_db", return_value=mock_data), \
             patch("dashboard.paper_trader._save_db") as mock_save:
            
            # Step 1: Price goes up to 110.00 (large gain)
            mock_ltp_batch.return_value = {"TEST": 110.00}
            closed = check_and_close_trades()
            assert len(closed) == 0
            assert trade["status"] == "OPEN"
            assert trade["peak_price"] == 110.00
            # 110.00 * (1 - 0.03) = 110 * 0.97 = 106.7
            assert trade["sl_price"] == 106.7
            
            # Step 2: Price retraces to 106.50 (hits trailed stop loss)
            mock_ltp_batch.return_value = {"TEST": 106.50}
            closed = check_and_close_trades()
            assert len(closed) == 1
            assert closed[0]["status"] == "CLOSED"
            assert closed[0]["exit_reason"] == "SL_HIT"
            assert closed[0]["exit_price"] == 106.50
            # P&L = (106.50 - 100.0) * 100 = +650.00
            assert closed[0]["pnl"] == 650.00
            assert closed[0]["pnl_pct"] == 6.50
