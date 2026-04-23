"""Tests for signals/regime.py — pure computation functions only (no feed calls)."""
import pytest
import numpy as np
import pandas as pd

from signals.regime import (
    Regime,
    RegimeSnapshot,
    _ema,
    _trend_score,
    _adx,
    breadth_pct_above_50dma,
)


def _make_close(prices: list[float]) -> pd.Series:
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="B")
    return pd.Series(prices, index=idx, dtype=float)


def _make_ohlc(n: int, base: float = 100.0, trend: float = 0.0) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    prices = [base + i * trend for i in range(n)]
    prices = np.array(prices, dtype=float)
    return pd.DataFrame({
        "open": prices - 0.5,
        "high": prices + 1.0,
        "low": prices - 1.0,
        "close": prices,
    }, index=idx)


class TestEma:
    def test_flat_series_returns_constant(self):
        s = _make_close([10.0] * 50)
        result = _ema(s, 20)
        assert pytest.approx(result.iloc[-1], abs=0.01) == 10.0

    def test_ema_converges_toward_new_level(self):
        s = _make_close([0.0] * 50 + [100.0] * 50)
        result = _ema(s, 20)
        assert result.iloc[-1] > 50.0


class TestTrendScore:
    def test_strong_uptrend_returns_positive(self):
        # Continuously rising prices — px > EMA20 > EMA50 > EMA200
        prices = list(range(1, 251))
        close = _make_close(prices)
        score = _trend_score(close)
        assert score > 0

    def test_strong_downtrend_returns_negative(self):
        prices = list(range(250, 0, -1))
        close = _make_close(prices)
        score = _trend_score(close)
        assert score < 0

    def test_score_range(self):
        prices = [100.0] * 250
        close = _make_close(prices)
        score = _trend_score(close)
        assert -3 <= score <= 3


class TestAdx:
    def test_trending_market_has_high_adx(self):
        df = _make_ohlc(100, base=100.0, trend=1.0)
        adx = _adx(df)
        assert adx > 0

    def test_returns_float(self):
        df = _make_ohlc(60, base=100.0, trend=0.5)
        result = _adx(df)
        assert isinstance(result, float)
        assert not np.isnan(result)


class TestBreadthPctAbove50dma:
    def test_empty_dict_returns_none(self):
        assert breadth_pct_above_50dma({}) is None

    def test_all_above_50dma(self):
        # Create stocks with rising prices so they're above 50-DMA
        stocks = {}
        for sym in ["A", "B", "C"]:
            df = _make_ohlc(100, base=100.0, trend=0.5)
            stocks[sym] = df
        result = breadth_pct_above_50dma(stocks)
        assert result == 100.0

    def test_none_above_50dma(self):
        # Falling prices → last price below 50-DMA
        stocks = {}
        for sym in ["A", "B", "C"]:
            df = _make_ohlc(100, base=200.0, trend=-1.0)
            stocks[sym] = df
        result = breadth_pct_above_50dma(stocks)
        assert result == 0.0

    def test_skips_stocks_with_insufficient_data(self):
        stocks = {
            "SHORT": pd.DataFrame({"close": [100.0] * 10}),  # < 50 rows
        }
        result = breadth_pct_above_50dma(stocks)
        assert result is None

    def test_partial_above(self):
        stocks = {}
        # 2 rising (above 50dma), 2 falling (below 50dma)
        for sym in ["A", "B"]:
            stocks[sym] = _make_ohlc(100, base=100.0, trend=0.5)
        for sym in ["C", "D"]:
            stocks[sym] = _make_ohlc(100, base=200.0, trend=-1.0)
        result = breadth_pct_above_50dma(stocks)
        assert result == 50.0


class TestRegimeSnapshot:
    def test_to_dict_serializes_regime_as_string(self):
        snap = RegimeSnapshot(
            regime=Regime.TREND_UP,
            trend_score=3,
            vix=12.5,
            vix_5d_change_pct=-2.0,
            adx=28.0,
            breadth_pct_above_50dma=75.0,
            notes="ok",
        )
        d = snap.to_dict()
        assert d["regime"] == "TREND_UP"
        assert isinstance(d["regime"], str)

    def test_to_dict_contains_all_fields(self):
        snap = RegimeSnapshot(
            regime=Regime.RANGE_LOW_VOL,
            trend_score=0,
            vix=11.0,
            vix_5d_change_pct=0.0,
            adx=18.0,
            breadth_pct_above_50dma=60.0,
            notes="ok",
        )
        d = snap.to_dict()
        for key in ["regime", "trend_score", "vix", "vix_5d_change_pct", "adx", "breadth_pct_above_50dma", "notes"]:
            assert key in d
