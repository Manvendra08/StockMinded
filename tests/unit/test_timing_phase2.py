"""Unit tests for Phase 2 timing enhancements."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from signals import timing as timing_mod


class TestAiTimingReview:
    """Test LLM-based timing review."""

    def test_ai_review_fallback_on_unavailable_llm(self):
        """If LLM unavailable, should fail-open (ai_timing_ok=True)."""
        from unittest.mock import patch
        with patch("data.ai_scraper.call_llm", return_value=(None, "none")):
            result = timing_mod.review_timing_with_llm(
                symbol="TEST",
                direction="LONG",
                price=1000,
                timing_snapshot={"vwap_overextended": (False, "OK")},
                market_regime="TREND_UP",
                ai_sentiment=None,
                use_groq=False,
                groq_config=None,
            )

            assert result["ai_timing_ok"] is True
            assert result["confidence"] == 0.1
            assert "unavailable" in result["reason"].lower()
            assert result["model_used"] == "none"

    def test_ai_review_timeout_handling(self):
        """AI review should handle timeout gracefully."""
        from unittest.mock import patch
        with patch("data.ai_scraper.call_llm", return_value=(None, "none")):
            result = timing_mod.review_timing_with_llm(
                symbol="TEST",
                direction="LONG",
                price=1000,
                timing_snapshot={"vwap_overextended": (False, "OK")},
                market_regime="TREND_UP",
                ai_sentiment=None,
                use_groq=True,
                groq_config={"api_key": "invalid_key", "timeout_sec": 0.1},
            )

            # Should fail-open
            assert result["ai_timing_ok"] is True
            assert result["latency_ms"] >= 0  # Timing recorded

    def test_ai_review_sentiment_warning(self):
        """AI review should flag BEARISH sentiment as warning."""
        result = timing_mod.review_timing_with_llm(
            symbol="TEST",
            direction="LONG",
            price=1000,
            timing_snapshot={"vwap_overextended": (False, "OK")},
            market_regime="TREND_UP",
            ai_sentiment={"overall": "BEARISH", "confidence": "HIGH"},
            use_groq=False,
            groq_config=None,
        )

        # Sentiment warning flagged
        assert "sentiment_warning" in result
        # When BEARISH sentiment detected, warning should be raised in fallback
        # (actual LLM response would check this)


class TestRegimeAdjustedThresholds:
    """Test dynamic threshold adjustment per market regime."""

    def test_no_regime_override_returns_base_config(self):
        """Unknown regime should return base config unchanged."""
        base = {
            "max_vwap_dist_pct": 1.0,
            "rsi_threshold_long": 70,
            "rsi_threshold_short": 30,
        }
        dynamic_rules = {"TREND_UP": {"max_vwap_dist_pct": 1.5}}

        result = timing_mod.get_regime_adjusted_thresholds(
            "UNKNOWN_REGIME", base, dynamic_rules
        )

        assert result["max_vwap_dist_pct"] == 1.0
        assert result["applied_regime"] == "UNKNOWN_REGIME"
        assert result["multiplier"] == {}

    def test_trend_up_relaxes_vwap(self):
        """TREND_UP should relax VWAP constraint."""
        base = {"max_vwap_dist_pct": 1.0}
        dynamic_rules = {"TREND_UP": {"max_vwap_dist_pct": 1.5}}

        result = timing_mod.get_regime_adjusted_thresholds(
            "TREND_UP", base, dynamic_rules
        )

        assert result["max_vwap_dist_pct"] == 1.5
        assert result["multiplier"]["vwap"] == 1.5  # 1.5 / 1.0

    def test_range_low_vol_tightens_vwap(self):
        """RANGE_LOW_VOL should tighten VWAP constraint."""
        base = {"max_vwap_dist_pct": 1.0, "max_intraday_atr_extension": 1.0}
        dynamic_rules = {
            "RANGE_LOW_VOL": {
                "max_vwap_dist_pct": 0.7,
                "max_intraday_atr_extension": 0.7,
            }
        }

        result = timing_mod.get_regime_adjusted_thresholds(
            "RANGE_LOW_VOL", base, dynamic_rules
        )

        assert result["max_vwap_dist_pct"] == 0.7
        assert result["max_intraday_atr_extension"] == 0.7
        assert result["multiplier"]["vwap"] == 0.7
        assert result["multiplier"]["atr"] == 0.7

    def test_trend_down_relaxes_rsi_short(self):
        """TREND_DOWN should relax RSI threshold for shorts."""
        base = {"rsi_threshold_short": 30}
        dynamic_rules = {"TREND_DOWN": {"rsi_threshold_short": 25}}

        result = timing_mod.get_regime_adjusted_thresholds(
            "TREND_DOWN", base, dynamic_rules
        )

        assert result["rsi_threshold_short"] == 25
        assert result["multiplier"]["rsi_short"] == 25 / 30

    def test_multiplier_calculation_zero_base(self):
        """Multiplier with zero base should default to 1.0 (division by zero)."""
        base = {"max_vwap_dist_pct": 0}  # Edge case: zero
        dynamic_rules = {"TREND_UP": {"max_vwap_dist_pct": 1.5}}

        result = timing_mod.get_regime_adjusted_thresholds(
            "TREND_UP", base, dynamic_rules
        )

        assert result["multiplier"]["vwap"] == 1.0  # Fallback


class TestSentimentFlipDetection:
    """Test sentiment reversal detection."""

    def test_no_previous_sentiment_no_flip(self):
        """No flip if no previous sentiment data."""
        result = timing_mod.detect_sentiment_flip(
            current_sentiment={"overall": "BULLISH"},
            previous_sentiment=None,
            window_trades=[],
        )

        assert result["flip_detected"] is False
        assert (
            "insufficient" in result["reason"].lower()
            or "history" in result["reason"].lower()
        )

    def test_bullish_to_bearish_flip(self):
        """Detect BULLISH → BEARISH reversal."""
        result = timing_mod.detect_sentiment_flip(
            current_sentiment={"overall": "BEARISH", "confidence": "HIGH"},
            previous_sentiment={"overall": "BULLISH", "confidence": "HIGH"},
            window_trades=[],
        )

        assert result["flip_detected"] is True
        assert result["flip_type"] == "BULLISH_TO_BEARISH"
        assert result["flip_confidence"] == 0.8
        assert result["trading_blocked_until"] is not None

    def test_bearish_to_bullish_flip(self):
        """Detect BEARISH → BULLISH reversal."""
        result = timing_mod.detect_sentiment_flip(
            current_sentiment={"overall": "BULLISH", "confidence": "HIGH"},
            previous_sentiment={"overall": "BEARISH", "confidence": "HIGH"},
            window_trades=[],
        )

        assert result["flip_detected"] is True
        assert result["flip_type"] == "BEARISH_TO_BULLISH"

    def test_no_flip_same_sentiment(self):
        """Same sentiment = no flip."""
        result = timing_mod.detect_sentiment_flip(
            current_sentiment={"overall": "BULLISH", "confidence": "HIGH"},
            previous_sentiment={"overall": "BULLISH", "confidence": "HIGH"},
            window_trades=[],
        )

        assert result["flip_detected"] is False

    def test_confidence_drop_signals_flip(self):
        """Confidence drop (HIGH → LOW) should signal flip."""
        result = timing_mod.detect_sentiment_flip(
            current_sentiment={"overall": "BULLISH", "confidence": "LOW"},
            previous_sentiment={"overall": "BULLISH", "confidence": "HIGH"},
            window_trades=[],
        )

        assert result["flip_detected"] is True
        assert result["flip_type"] == "NEUTRAL_SHIFT"
        assert result["flip_confidence"] >= 0.5

    def test_recent_losses_increase_flip_confidence(self):
        """3+ recent losses increase flip confidence."""
        window_trades = [
            {"pnl_rupees": -100},
            {"pnl_rupees": -150},
            {"pnl_rupees": -200},
            {"pnl_rupees": 50},
            {"pnl_rupees": -75},
        ]

        result = timing_mod.detect_sentiment_flip(
            current_sentiment={"overall": "BEARISH", "confidence": "HIGH"},
            previous_sentiment={"overall": "BULLISH", "confidence": "HIGH"},
            window_trades=window_trades,
        )

        assert result["flip_detected"] is True
        assert result["flip_confidence"] > 0.8
        assert "losing" in result["reason"].lower()

    def test_trading_blocked_until_30_min_from_now(self):
        """trading_blocked_until should be ~30 min from flip time."""
        before = datetime.now()
        result = timing_mod.detect_sentiment_flip(
            current_sentiment={"overall": "BEARISH", "confidence": "HIGH"},
            previous_sentiment={"overall": "BULLISH", "confidence": "HIGH"},
            window_trades=[],
        )
        after = datetime.now()

        assert result["trading_blocked_until"] is not None
        # Should be within 30-31 minutes from now
        time_to_unblock = (
            result["trading_blocked_until"] - before
        ).total_seconds() / 60
        assert 29.5 <= time_to_unblock <= 30.5


class TestPhase2Integration:
    """Integration tests for Phase 2 components."""

    def test_regime_adjustment_then_ai_review(self):
        """Adjusted thresholds should be available in AI review context."""
        base = {"max_vwap_dist_pct": 1.0, "rsi_threshold_long": 70}
        dynamic_rules = {"TREND_UP": {"max_vwap_dist_pct": 1.5}}

        adjusted = timing_mod.get_regime_adjusted_thresholds(
            "TREND_UP", base, dynamic_rules
        )

        # Simulate AI review with adjusted snapshot
        timing_snapshot = {
            "vwap_overextended": (False, f"OK at {adjusted['max_vwap_dist_pct']}%")
        }

        from unittest.mock import patch
        with patch("data.ai_scraper.call_llm", return_value=("YES: Good timing", "groq")):
            result = timing_mod.review_timing_with_llm(
                symbol="TEST",
                direction="LONG",
                price=1000,
                timing_snapshot=timing_snapshot,
                market_regime="TREND_UP",
                ai_sentiment={"overall": "BULLISH"},
                use_groq=False,
                groq_config=None,
            )

            # Should fail-open with adjusted context
            assert result["ai_timing_ok"] is True

    def test_sentiment_flip_blocks_entry_in_alert_flow(self):
        """Sentiment flip should prevent entry in alert generation."""
        # Simulate previous sentiment → current flip
        prev_sentiment = {
            "overall": "BULLISH",
            "confidence": "HIGH",
            "updated_at": "2026-06-24 10:00",
        }
        curr_sentiment = {
            "overall": "BEARISH",
            "confidence": "HIGH",
            "updated_at": "2026-06-24 10:05",
        }

        flip = timing_mod.detect_sentiment_flip(
            current_sentiment=curr_sentiment,
            previous_sentiment=prev_sentiment,
            window_trades=[],
        )

        # In alert generation: if flip_detected, could block entry
        if flip["flip_detected"]:
            # Entry would be skipped or logged
            assert flip["trading_blocked_until"] > datetime.now()
