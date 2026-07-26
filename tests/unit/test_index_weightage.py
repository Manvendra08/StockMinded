from unittest.mock import MagicMock, patch
import pytest
from signals import index_weightage


@pytest.mark.unit
def test_load_index_weights_state_defaults() -> None:
    # Test fallback to baselines when state file does not exist
    with patch("os.path.exists", return_value=False):
        state = index_weightage.load_index_weights_state()
        assert state["status"] == "BASELINE"
        assert "NIFTY" in state["weights"]
        assert state["weights"]["NIFTY"]["HDFCBANK"] == 11.18


@pytest.mark.unit
def test_refresh_weights_calculations() -> None:
    # Test calculations from mocked yfinance responses
    mock_ticker_fast_info = MagicMock()
    mock_ticker_fast_info.market_cap = 1000.0  # mock market cap
    
    mock_ticker = MagicMock()
    mock_ticker.fast_info = mock_ticker_fast_info

    mock_tickers = MagicMock()
    mock_tickers.tickers = {f"{s}.NS": mock_ticker for s in index_weightage.FREE_FLOAT_FACTORS.keys()}

    from unittest.mock import mock_open
    # Mock open and json.dump to avoid writing files in unit tests
    with patch("yfinance.Tickers", return_value=mock_tickers), \
         patch("signals.index_weightage.load_index_weights_state", return_value={"last_refresh": None, "weights": {}}), \
         patch("os.makedirs"), \
         patch("builtins.open", mock_open()), \
         patch("json.dump") as mock_dump:
        
        res = index_weightage.refresh_weights_if_needed(force=True)
        assert res is True
        assert mock_dump.called
        # Check that saved state contains weights
        args, _ = mock_dump.call_args
        saved_state = args[0]
        assert "weights" in saved_state
        assert "NIFTY" in saved_state["weights"]


@pytest.mark.unit
def test_calculate_weighted_momentum() -> None:
    mock_quotes = {
        "HDFCBANK": {"change_pct": 1.0, "ltp": 1500.0},
        "ICICIBANK": {"change_pct": -2.0, "ltp": 900.0},
    }
    
    # We mock feed.quote_batch to return our quotes
    with patch("data.feed.quote_batch", return_value=mock_quotes), \
         patch("signals.index_weightage.load_index_weights_state", return_value={
             "last_refresh": "2026-07-06T00:00:00",
             "weights": {
                 "NIFTY": {
                     "HDFCBANK": 60.0,
                     "ICICIBANK": 40.0,
                 }
             }
         }):
        # Nifty top symbols mock
        with patch("signals.index_weightage.CONSTITUENTS", {"NIFTY": ["HDFCBANK", "ICICIBANK"]}):
            res = index_weightage.calculate_weighted_momentum("NIFTY")
            # weighted = (60 * 1.0 + 40 * -2.0) / 100 = (60 - 80) / 100 = -0.2
            assert res["weighted_momentum"] == -0.2
            assert res["bullish_count"] == 1
            assert res["bearish_count"] == 1
