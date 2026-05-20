"""Tests for Paper Trader daily summary and EOD generation."""
import pytest
import json
from datetime import date
from unittest.mock import patch
from dashboard.paper_trader import generate_eod_summary, _load_db, _save_db, DATA_FILE


@pytest.fixture(autouse=True)
def mock_atomic_db_update(tmp_path):
    """Globally patch atomic_db_update and DATA_FILE to avoid msvcrt lock file issues in tests and prevent leaks."""
    import contextlib
    import dashboard.paper_trader

    test_db = tmp_path / "paper_trades_test.json"
    
    # Initialize the test db with default structure
    mock_data = {
        "trades": [
            {
                "id": 1, "symbol": "RELIANCE", "direction": "LONG", "status": "CLOSED",
                "entry_price": 2400.0, "qty": 10, "entry_date": "2026-05-18",
                "pnl": 100.0, "exit_reason": "TARGET_HIT"
            },
            {
                "id": 2, "symbol": "TCS", "direction": "SHORT", "status": "CLOSED",
                "entry_price": 3200.0, "qty": 5, "entry_date": "2026-05-18",
                "pnl": -50.0, "exit_reason": "SL_HIT"
            },
            {
                "id": 3, "symbol": "INFY", "direction": "LONG", "status": "CLOSED",
                "entry_price": 1500.0, "qty": 20, "entry_date": "2026-05-19",
                "pnl": 200.0, "exit_reason": "TARGET_HIT"
            }
        ],
        "option_trades": [],
        "daily_summaries": [],
        "cumulative_pnl": 0.0,
        "settings": {}
    }
    with open(test_db, "w") as f:
        json.dump(mock_data, f)

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


def test_generate_eod_summary_creates_missing_reports():
    """Verify that generate_eod_summary generates reports for all unique trade dates and today."""
    today = date.today().isoformat()
    
    with patch("dashboard.paper_trader.check_and_close_trades"):
        # Generate EOD summaries
        summary = generate_eod_summary(today)
    
    db = _load_db()
    summaries = db["daily_summaries"]
    dates = [s["date"] for s in summaries]
    
    assert "2026-05-18" in dates
    assert "2026-05-19" in dates
    assert today in dates
    
    # Check 2026-05-18 trade statistics
    s_18 = next(s for s in summaries if s["date"] == "2026-05-18")
    assert s_18["total_trades"] == 2
    assert s_18["winners"] == 1
    assert s_18["losers"] == 1
    assert s_18["total_pnl"] == 50.0  # 100.0 + (-50.0)
    
    # Check 2026-05-19 trade statistics
    s_19 = next(s for s in summaries if s["date"] == "2026-05-19")
    assert s_19["total_trades"] == 1
    assert s_19["winners"] == 1
    assert s_19["total_pnl"] == 200.0
    
    # Chronological sort check
    assert dates == sorted(dates)
    
    # Cumulative P&L checks
    # 2026-05-18 cum_pnl = 50.0
    # 2026-05-19 cum_pnl = 50.0 + 200.0 = 250.0
    # Today cum_pnl = 250.0 + 0 = 250.0
    assert s_18["cumulative_pnl"] == 50.0
    assert s_19["cumulative_pnl"] == 250.0
    s_today = next(s for s in summaries if s["date"] == today)
    assert s_today["cumulative_pnl"] == 250.0
    
    # Verify top-level cumulative_pnl is updated on the database
    assert db["cumulative_pnl"] == 250.0
