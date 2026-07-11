"""Unit tests for signals/timing.py module."""

import pandas as pd
import pytest

from signals import timing as timing_mod


class TestIsOverextendedFromVwap:
    """Test VWAP overextension check."""

    def test_long_within_vwap_threshold(self):
        """LONG at +0.5% from VWAP with 1.0% threshold should pass."""
        is_over, reason = timing_mod.is_overextended_from_vwap(
            price=1005, vwap=1000, max_dist_pct=1.0
        )
        assert is_over is False
        assert "VWAP" in reason or reason == ""

    def test_long_beyond_vwap_threshold(self):
        """LONG at +1.5% from VWAP with 1.0% threshold should fail."""
        is_over, reason = timing_mod.is_overextended_from_vwap(
            price=1015, vwap=1000, max_dist_pct=1.0
        )
        assert is_over is True
        assert "Overextended" in reason or "%" in reason

    def test_short_beyond_vwap_threshold(self):
        """SHORT at -1.5% from VWAP with 1.0% threshold should fail."""
        is_over, reason = timing_mod.is_overextended_from_vwap(
            price=985, vwap=1000, max_dist_pct=1.0
        )
        assert is_over is True

    def test_vwap_none_fails_open(self):
        """Missing VWAP should fail-open (allow entry)."""
        is_over, reason = timing_mod.is_overextended_from_vwap(
            price=1010, vwap=None, max_dist_pct=1.0
        )
        assert is_over is False
        assert "unavailable" in reason.lower()


class TestIsRsiOverextended:
    """Test RSI overextension check."""

    def test_long_rsi_overbought(self):
        """LONG with RSI > threshold should be overextended."""
        # Create uptrend
        close_vals = [100 + i * 0.5 for i in range(20)]
        df = pd.DataFrame({"close": close_vals})
        is_over, reason = timing_mod.is_rsi_overextended(
            df, threshold_long=70, threshold_short=30, direction="LONG"
        )
        assert is_over is True
        assert "RSI" in reason or "overbought" in reason

    def test_long_rsi_healthy(self):
        """LONG with RSI in middle should pass."""
        # Create sideways market
        close_vals = [100] * 20
        df = pd.DataFrame({"close": close_vals})
        is_over, reason = timing_mod.is_rsi_overextended(
            df, threshold_long=70, threshold_short=30, direction="LONG"
        )
        assert is_over is False

    def test_rsi_insufficient_data(self):
        """RSI with < 15 candles should fail-open."""
        df = pd.DataFrame({"close": [100, 101, 102, 103, 104]})
        is_over, reason = timing_mod.is_rsi_overextended(
            df, threshold_long=70, threshold_short=30, direction="LONG"
        )
        assert is_over is False
        assert "insufficient" in reason.lower() or "unavailable" in reason.lower()


class TestIsPriceOverextended:
    """Test price distance from open check."""

    def test_long_within_atr_limit(self):
        """LONG +0.5 ATR from open with 1.0 limit should pass."""
        is_over, reason = timing_mod.is_price_overextended(
            price=1005, open_price=1000, atr=10, max_atr_extension=1.0, direction="LONG"
        )
        assert is_over is False

    def test_long_beyond_atr_limit(self):
        """LONG +1.5 ATR from open with 1.0 limit should fail."""
        is_over, reason = timing_mod.is_price_overextended(
            price=1015, open_price=1000, atr=10, max_atr_extension=1.0, direction="LONG"
        )
        assert is_over is True
        assert "ATR" in reason

    def test_short_within_atr_limit(self):
        """SHORT -0.5 ATR from open with 1.0 limit should pass."""
        is_over, reason = timing_mod.is_price_overextended(
            price=995, open_price=1000, atr=10, max_atr_extension=1.0, direction="SHORT"
        )
        assert is_over is False

    def test_atr_zero_fails_open(self):
        """ATR=0 should fail-open."""
        is_over, reason = timing_mod.is_price_overextended(
            price=1010, open_price=1000, atr=0, max_atr_extension=1.0, direction="LONG"
        )
        assert is_over is False


class TestMarketExhaustionScore:
    """Test market exhaustion calculation."""

    def test_healthy_market(self):
        """Healthy breadth + no VIX spike should score 0."""
        nifty_df = pd.DataFrame({"close": [17000 + i * 10 for i in range(25)]})
        score, reason = timing_mod.market_exhaustion_score(
            nifty_df,
            advances=180,
            declines=50,
            breadth_drop_threshold_pct=8,
            vix_df=None,
        )
        assert score == 0.0
        assert "healthy" in reason.lower() or "no" in reason.lower()

    def test_weak_breadth(self):
        """Weak breadth (< 0.55 A/D ratio) should increase score."""
        nifty_df = pd.DataFrame({"close": [17000 + i * 10 for i in range(25)]})
        score, reason = timing_mod.market_exhaustion_score(
            nifty_df,
            advances=80,
            declines=180,
            breadth_drop_threshold_pct=8,
            vix_df=None,
        )
        assert score > 0  # Should have some exhaustion
        assert "Breadth" in reason or "weak" in reason.lower()

    def test_insufficient_nifty_data(self):
        """< 20 NIFTY candles should fail-open (score 0)."""
        nifty_df = pd.DataFrame({"close": [17000, 17010, 17020]})
        score, reason = timing_mod.market_exhaustion_score(
            nifty_df,
            advances=100,
            declines=100,
            breadth_drop_threshold_pct=8,
            vix_df=None,
        )
        assert score == 0.0


class TestEvaluateTimingForEntry:
    """Test unified timing evaluation."""

    def test_timing_disabled_returns_ok(self):
        """Disabled timing engine should return timing_ok=True."""
        config = {"enabled": False}
        result = timing_mod.evaluate_timing_for_entry(
            symbol="TEST",
            direction="LONG",
            price=1000,
            config=config,
            df_5m=None,
            df_1d=None,
            vwap_5m=None,
            ai_sentiment_current=None,
            market_breadth=None,
        )
        assert result["timing_ok"] is True

    def test_all_checks_pass(self):
        """All timing checks pass should return timing_ok=True."""
        config = {
            "enabled": True,
            "late_entry_filter": {
                "enabled": True,
                "max_vwap_dist_pct": 2.0,
                "rsi_threshold_long": 70,
            },
        }

        # Create mock data: mid-range RSI (oscillating prices), within VWAP threshold
        # Use oscillating prices to keep RSI near 50 (neutral)
        prices = [100.0]
        for i in range(20):
            if i % 2 == 0:
                prices.append(prices[-1] + 0.1)
            else:
                prices.append(prices[-1] - 0.1)
        df_5m = pd.DataFrame({"close": prices})
        df_1d = pd.DataFrame(
            {
                "open": [1000],
                "high": [1010],
                "low": [990],
                "close": [1005],
            }
        )

        result = timing_mod.evaluate_timing_for_entry(
            symbol="TEST",
            direction="LONG",
            price=1001,  # Near VWAP
            config=config,
            df_5m=df_5m,
            df_1d=df_1d,
            vwap_5m=1000,
            ai_sentiment_current={},
            market_breadth={"advances": 150, "declines": 50},
        )
        assert result["timing_ok"] is True
        assert result["size_multiplier"] == 1.0

    def test_overextended_from_vwap_fails(self):
        """Price far from VWAP should fail."""
        config = {
            "enabled": True,
            "late_entry_filter": {
                "enabled": True,
                "max_vwap_dist_pct": 1.0,
            },
        }

        result = timing_mod.evaluate_timing_for_entry(
            symbol="TEST",
            direction="LONG",
            price=1020,  # 2% above VWAP
            config=config,
            df_5m=None,
            df_1d=None,
            vwap_5m=1000,
            ai_sentiment_current=None,
            market_breadth=None,
        )
        assert result["timing_ok"] is False
        assert "Overextended" in result["reason"]

    def test_event_risk_mode_activated(self):
        """High exhaustion should activate event_risk_mode."""
        config = {
            "enabled": True,
            "late_entry_filter": {"enabled": False},
            "market_exhaustion": {
                "enabled": True,
                "breadth_drop_threshold_pct": 2,  # Low threshold
            },
            "event_risk_mode": {"enabled": True, "size_multiplier": 0.5},
        }

        nifty_df = pd.DataFrame({"close": [17000 + i * 10 for i in range(25)]})
        # VIX data with spike: yesterday close 15, current 20 (33% spike > 5% threshold)
        vix_df = pd.DataFrame(
            {"close": [15.0, 20.0]}, 
            index=pd.date_range("2024-01-01", periods=2, freq="B")
        )

        result = timing_mod.evaluate_timing_for_entry(
            symbol="TEST",
            direction="LONG",
            price=1000,
            config=config,
            df_5m=None,
            df_1d=nifty_df,
            vwap_5m=1000,
            ai_sentiment_current=None,
            market_breadth={"advances": 80, "declines": 200},  # Weak breadth
            vix_df=vix_df,
        )

        # High exhaustion should activate event risk mode
        assert result["event_risk_mode"] is True
        assert result["size_multiplier"] == 0.5
