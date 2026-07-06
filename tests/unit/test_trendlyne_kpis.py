import time
from unittest.mock import MagicMock, patch
import pytest
from data import feed


@pytest.fixture(autouse=True)
def clear_trendlyne_cache():
    feed._trendlyne_cache.clear()
    yield
    feed._trendlyne_cache.clear()


@pytest.mark.unit
def test_fetch_trendlyne_options_kpis_success():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = 'window.__INITIAL_STATE__ = {"optionsDashboard": {"latest": {"fiiLongShortRatio": "1.35", "modifiedMaxPain": 24000, "ivPercentile": 65, "pcr": 1.15}}};'

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    with patch("curl_cffi.requests.Session", return_value=mock_session):
        res = feed.fetch_trendlyne_options_kpis("NIFTY")
        assert res.get("fii_index_long_short_ratio") == "1.35"
        assert res.get("modified_max_pain") == 24000
        assert res.get("source") == "trendlyne"
        assert "NIFTY" in feed._trendlyne_cache


@pytest.mark.unit
def test_fetch_trendlyne_options_kpis_caching():
    feed._trendlyne_cache["NIFTY"] = (time.time(), {"fii_index_long_short_ratio": "1.20", "source": "trendlyne"})

    with patch("curl_cffi.requests.Session") as mock_session_cls:
        res = feed.fetch_trendlyne_options_kpis("NIFTY")
        assert res.get("fii_index_long_short_ratio") == "1.20"
        mock_session_cls.assert_not_called()


@pytest.mark.unit
def test_fetch_trendlyne_options_kpis_error_500_fallback_to_cache(caplog):
    # Pre-populate cache
    feed._trendlyne_cache["NIFTY"] = (time.time() - 1000, {"fii_index_long_short_ratio": "1.10", "source": "trendlyne"})

    mock_resp = MagicMock()
    mock_resp.raise_for_status.side_effect = Exception("HTTP Error 500: Internal Server Error")
    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp

    import logging
    with patch("curl_cffi.requests.Session", return_value=mock_session), caplog.at_level(logging.DEBUG):
        res = feed.fetch_trendlyne_options_kpis("NIFTY")
        # Should return stale cache instead of failing or spamming warnings
        assert res.get("fii_index_long_short_ratio") == "1.10"
        # Verify DEBUG log was emitted rather than WARNING
        assert any("Trendlyne fetch failed for NIFTY" in r.message and r.levelname == "DEBUG" for r in caplog.records)
        assert not any("Trendlyne fetch failed for NIFTY" in r.message and r.levelname == "WARNING" for r in caplog.records)
