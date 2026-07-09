import pytest
from signals.verdict import build_trade_verdict
from intelligence.adaptive_regime import AdaptiveRegimeClassifier
from signals.regime import Regime

def test_verdict_bearish_circuit_breaker(monkeypatch):
    # Mock index heavyweight momentum to represent a severe crash (-2.0%)
    monkeypatch.setattr(
        "signals.index_weightage.calculate_weighted_momentum",
        lambda idx: {"weighted_momentum": -2.0}
    )

    data = {
        "regime": {
            "name": "TREND_UP",
            "trend_score": 5,
            "adx": 25.0,
            "vix": 15.0,
            "breadth_pct_above_50dma": 70.0
        },
        "flows": {
            "smart_money_bias": "LONG",
            "ai_sentiment": {
                "overall_market_sentiment": "BEARISH",
                "confidence": "HIGH",
                "sentiment_score": -0.9
            },
            "pcr_oi": 1.1,
            "max_pain": 24000.0,
            "pcr_stale": False,
            "mp_stale": False
        },
        "structure": {
            "primary": "Iron Condor"
        },
        "leaders": [{"symbol": "RELIANCE", "quintile": 5}],
        "laggards": [],
        "data_freshness": {"status": "FRESH"},
        "source_errors": [],
        "iv_rank": 30.0
    }

    verdict = build_trade_verdict(data)

    # Bearish circuit breaker should override stock action (LONG_ONLY -> WAIT)
    assert verdict.stock.action == "WAIT"
    assert "Circuit Breaker" in verdict.stock.strategy
    assert "Bearish Circuit Breaker: Longs Blocked" in verdict.stock.blocks

    # Bearish circuit breaker should override nifty options action (defined risk -> WAIT)
    assert verdict.nifty.action == "WAIT"
    assert "Circuit Breaker" in verdict.nifty.strategy
    assert "Bearish Circuit Breaker: Option Selling Blocked" in verdict.nifty.blocks


def test_verdict_bullish_circuit_breaker(monkeypatch):
    # Mock index heavyweight momentum to represent a severe breakout (+2.0%)
    monkeypatch.setattr(
        "signals.index_weightage.calculate_weighted_momentum",
        lambda idx: {"weighted_momentum": 2.0}
    )

    data = {
        "regime": {
            "name": "TREND_DOWN",
            "trend_score": -5,
            "adx": 25.0,
            "vix": 15.0,
            "breadth_pct_above_50dma": 30.0
        },
        "flows": {
            "smart_money_bias": "SHORT",
            "ai_sentiment": {
                "overall_market_sentiment": "BULLISH",
                "confidence": "HIGH",
                "sentiment_score": 0.9
            },
            "pcr_oi": 0.7,
            "max_pain": 24000.0,
            "pcr_stale": False,
            "mp_stale": False
        },
        "structure": {
            "primary": "Iron Condor"
        },
        "leaders": [],
        "laggards": [{"symbol": "TCS", "quintile": 5}],
        "data_freshness": {"status": "FRESH"},
        "source_errors": [],
        "iv_rank": 30.0
    }

    verdict = build_trade_verdict(data)

    # Bullish circuit breaker should override stock action (SHORT_ONLY -> WAIT)
    assert verdict.stock.action == "WAIT"
    assert "Circuit Breaker" in verdict.stock.strategy
    assert "Bullish Circuit Breaker: Shorts Blocked" in verdict.stock.blocks

    # Bullish circuit breaker should override nifty options action -> WAIT
    assert verdict.nifty.action == "WAIT"
    assert "Circuit Breaker" in verdict.nifty.strategy
    assert "Bullish Circuit Breaker: Option Selling Blocked" in verdict.nifty.blocks


def test_adaptive_regime_scoring_functions():
    classifier = AdaptiveRegimeClassifier()

    # Heavyweight momentum scoring
    # TREND_UP: positive momentum should support, negative momentum should contradict
    assert classifier._score_heavyweight_momentum_for_regime(Regime.TREND_UP, 2.0) == 1.0
    assert classifier._score_heavyweight_momentum_for_regime(Regime.TREND_UP, -2.0) == -1.0

    # TREND_DOWN: negative momentum should support, positive momentum should contradict
    assert classifier._score_heavyweight_momentum_for_regime(Regime.TREND_DOWN, -2.0) == 1.0
    assert classifier._score_heavyweight_momentum_for_regime(Regime.TREND_DOWN, 2.0) == -1.0

    # VOL_EXPANSION: extreme momentum in either direction should support
    assert classifier._score_heavyweight_momentum_for_regime(Regime.VOL_EXPANSION, 2.0) == 1.0
    assert classifier._score_heavyweight_momentum_for_regime(Regime.VOL_EXPANSION, -2.0) == 1.0

    # RANGE_LOW_VOL / VOL_CONTRACTION: high momentum should contradict
    assert classifier._score_heavyweight_momentum_for_regime(Regime.RANGE_LOW_VOL, 1.5) == -0.8
    assert classifier._score_heavyweight_momentum_for_regime(Regime.RANGE_LOW_VOL, 0.15) == 0.6

    # AI Sentiment scoring
    # TREND_UP: bullish sentiment supports, bearish contradicts
    assert classifier._score_ai_sentiment_for_regime(Regime.TREND_UP, 0.9) == 1.0
    assert classifier._score_ai_sentiment_for_regime(Regime.TREND_UP, -0.9) == -1.0

    # TREND_DOWN: bearish sentiment supports, bullish contradicts
    assert classifier._score_ai_sentiment_for_regime(Regime.TREND_DOWN, -0.9) == 1.0
    assert classifier._score_ai_sentiment_for_regime(Regime.TREND_DOWN, 0.9) == -1.0
