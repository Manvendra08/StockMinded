"""Tests for option exit guardrails."""
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager
from unittest.mock import patch

from dashboard.paper_trader import check_option_exits


IST = timezone(timedelta(hours=5, minutes=30))


@contextmanager
def _db_context(db):
    yield db


def test_check_option_exits_keeps_valid_credit_trade():
    now = datetime(2026, 6, 4, 10, 0, 0, tzinfo=IST)
    trade = {
        "id": 1,
        "symbol": "BANKNIFTY",
        "structure": "SHORT_STRANGLE_WINGED",
        "status": "OPEN",
        "entry_time": "2026-06-04 10:00:00",
        "entry_date": "2026-06-04",
        "net_premium": 300.0,
        "legs": [
            {"side": "SELL", "type": "CE", "strike": 56000, "expiry": "27-Jun-2026", "qty": 30}
        ],
    }
    db = {"option_trades": [trade], "settings": {"smart_exits_enabled": False}}
    price_map = {(56000, "27-Jun-2026", "CE"): 10.0}

    with patch("dashboard.paper_trader.atomic_db_update", side_effect=lambda: _db_context(db)), \
         patch("dashboard.paper_trader._load_db", return_value=db), \
         patch("dashboard.paper_trader.is_market_open", return_value=True), \
         patch("dashboard.paper_trader.is_eod_window", return_value=False), \
         patch("dashboard.paper_trader._build_option_price_map", return_value=price_map), \
         patch("dashboard.paper_trader._now_ist", return_value=now), \
         patch("dashboard.paper_trader._get_current_vix", return_value=0.0):
        closed = check_option_exits()

    assert closed == []
    assert trade["status"] == "OPEN"
    assert trade.get("exit_reason") is None


def test_check_option_exits_closes_missing_premium_trade():
    now = datetime(2026, 6, 4, 10, 0, 0, tzinfo=IST)
    trade = {
        "id": 2,
        "symbol": "BANKNIFTY",
        "structure": "SYNTHETIC_TEST",
        "status": "OPEN",
        "entry_time": "2026-06-04 10:00:00",
        "entry_date": "2026-06-04",
        "net_premium": 0.0,
        "legs": [
            {"side": "SELL", "type": "CE", "strike": 56000, "expiry": "27-Jun-2026", "qty": 30}
        ],
    }
    db = {"option_trades": [trade], "settings": {"smart_exits_enabled": False}}

    with patch("dashboard.paper_trader.atomic_db_update", side_effect=lambda: _db_context(db)), \
         patch("dashboard.paper_trader._load_db", return_value=db), \
         patch("dashboard.paper_trader.is_market_open", return_value=True), \
         patch("dashboard.paper_trader.is_eod_window", return_value=False), \
         patch("dashboard.paper_trader._build_option_price_map", return_value={}), \
         patch("dashboard.paper_trader._now_ist", return_value=now), \
         patch("dashboard.paper_trader._get_current_vix", return_value=0.0):
        closed = check_option_exits()

    assert len(closed) == 1
    assert trade["status"] == "CLOSED"
    assert trade.get("exit_reason") == "INVALID_ZERO_PREMIUM"
