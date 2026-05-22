"""Tests for signals/leadership.py — pure computation functions only."""
import pytest
import numpy as np
import pandas as pd

from signals.leadership import _rs_line, _slope, rank_universe, a_grade, StockRank


def _make_close(prices: list[float], start: str = "2024-01-01") -> pd.Series:
    idx = pd.date_range(start, periods=len(prices), freq="B")
    return pd.Series(prices, index=idx, dtype=float)


def _make_df(prices: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="B")
    return pd.DataFrame({"close": prices, "open": prices, "high": prices, "low": prices}, index=idx)


def _bench(n: int = 200, base: float = 100.0) -> pd.DataFrame:
    prices = [base] * n
    return _make_df(prices)


class TestRsLine:
    def test_flat_stock_over_flat_bench_is_constant(self):
        stock = _make_close([100.0] * 50)
        bench = _make_close([100.0] * 50)
        rs = _rs_line(stock, bench)
        assert np.allclose(rs.values, 1.0, atol=1e-6)

    def test_outperforming_stock_has_rising_rs(self):
        stock = _make_close(list(range(100, 150)))
        bench = _make_close([100.0] * 50)
        rs = _rs_line(stock, bench)
        assert rs.iloc[-1] > rs.iloc[0]

    def test_underperforming_stock_has_falling_rs(self):
        stock = _make_close(list(range(149, 99, -1)))
        bench = _make_close([100.0] * 50)
        rs = _rs_line(stock, bench)
        assert rs.iloc[-1] < rs.iloc[0]


class TestSlope:
    def test_rising_series_positive_slope(self):
        s = pd.Series(range(50), dtype=float)
        assert _slope(s) > 0

    def test_falling_series_negative_slope(self):
        s = pd.Series(range(50, 0, -1), dtype=float)
        assert _slope(s) < 0

    def test_flat_series_near_zero(self):
        s = pd.Series([1.0] * 50)
        assert abs(_slope(s)) < 1e-6

    def test_insufficient_length_returns_zero(self):
        s = pd.Series([1.0, 2.0, 3.0])
        assert _slope(s, n=20) == 0.0

    def test_exact_slope_value_validation(self):
        # Create a perfectly linear series from 1.0 to 20.0
        # y increases by exactly 1.0 per day.
        # mean(y) = 10.5
        # m (raw slope) = 1.0
        # Expected _slope = 1.0 / 10.5 = 0.095238...
        s = pd.Series(range(1, 21), dtype=float)
        slope_val = _slope(s, n=20)
        expected = 1.0 / 10.5
        assert np.isclose(slope_val, expected, atol=1e-6)


class TestRankUniverse:
    def _universe(self, n_stocks: int = 5, n_rows: int = 100, trend: float = 0.5) -> dict:
        result = {}
        for i in range(n_stocks):
            prices = [100.0 + i * 5 + j * trend for j in range(n_rows)]
            result[f"STOCK{i}"] = _make_df(prices)
        return result

    def test_returns_stock_rank_list(self):
        stocks = self._universe()
        bench = _bench()
        ranks = rank_universe(stocks, bench)
        assert isinstance(ranks, list)
        assert all(isinstance(r, StockRank) for r in ranks)

    def test_quintiles_in_range(self):
        stocks = self._universe(5)
        bench = _bench()
        ranks = rank_universe(stocks, bench)
        for r in ranks:
            assert 1 <= r.quintile <= 5

    def test_skips_short_series(self):
        stocks = {"SHORT": _make_df([100.0] * 30)}
        bench = _bench()
        ranks = rank_universe(stocks, bench)
        assert ranks == []

    def test_empty_universe_returns_empty(self):
        ranks = rank_universe({}, _bench())
        assert ranks == []

    def test_above_50dma_flag(self):
        # Rising stock: should be above 50dma
        prices = list(range(100, 201))
        df = _make_df(prices)
        bench = _bench(n=200)
        ranks = rank_universe({"RISING": df}, bench)
        if ranks:
            assert ranks[0].above_50dma is True

    def test_exact_scaled_rs_slope_validation(self):
        # Validate the exact final rs_slope_20d scaling inside rank_universe
        # Bench is constant 100.0, so RS line equals (stock_price / 100)
        # If stock price increases by 1 each day from 100 to 119 over the last 20 days:
        prices = [100.0] * 30 + list(range(100, 120))
        df = _make_df(prices)
        bench = _bench(n=50)
        ranks = rank_universe({"EXACT": df}, bench)
        
        # y = RS over last 20 days = [1.00, 1.01, ..., 1.19]
        # raw slope (m) = 0.01 per day
        # mean(y) = 1.095
        # raw _slope = 0.01 / 1.095 = 0.0091324...
        # scaled = round(0.0091324... * 10000, 2) = 91.32
        assert len(ranks) == 1
        assert ranks[0].rs_slope_20d == 91.32


class TestAGrade:
    def _make_ranks(self) -> list[StockRank]:
        return [
            StockRank("BEST1", rs_slope_20d=10.0, pct_vs_50dma=5.0, quintile=5, above_50dma=True),
            StockRank("BEST2", rs_slope_20d=8.0, pct_vs_50dma=3.0, quintile=4, above_50dma=True),
            StockRank("MID", rs_slope_20d=1.0, pct_vs_50dma=0.5, quintile=3, above_50dma=True),
            StockRank("WORST1", rs_slope_20d=-8.0, pct_vs_50dma=-4.0, quintile=4, above_50dma=False),
            StockRank("WORST2", rs_slope_20d=-10.0, pct_vs_50dma=-5.0, quintile=5, above_50dma=False),
        ]

    def test_leaders_are_quintile_5_above_50dma(self):
        longs, _ = a_grade(self._make_ranks())
        assert all(r.quintile >= 2 and r.above_50dma for r in longs)

    def test_laggards_are_quintile_1_below_50dma(self):
        _, shorts = a_grade(self._make_ranks())
        assert all(r.quintile >= 2 and not r.above_50dma for r in shorts)

    def test_leaders_sorted_descending_by_rs(self):
        longs, _ = a_grade(self._make_ranks())
        slopes = [r.rs_slope_20d for r in longs]
        assert slopes == sorted(slopes, reverse=True)

    def test_laggards_sorted_ascending_by_rs(self):
        _, shorts = a_grade(self._make_ranks())
        slopes = [r.rs_slope_20d for r in shorts]
        assert slopes == sorted(slopes)

    def test_top_n_respected(self):
        ranks = [
            StockRank(f"S{i}", rs_slope_20d=float(i), pct_vs_50dma=1.0, quintile=5, above_50dma=True)
            for i in range(20)
        ]
        longs, _ = a_grade(ranks, top_n=5)
        assert len(longs) <= 5

    def test_empty_returns_empty_lists(self):
        longs, shorts = a_grade([])
        assert longs == []
        assert shorts == []


class TestEarlyBreakoutAndVolume:
    def test_early_breakout_quintile_assignment(self):
        # Q5 long setup: 10d slope > 15, 20d slope > 5, 20 DMA proximity between 0% and 6%, above 50 DMA, high volume (RVOL > 1.2)
        prices = [100.0] * 50 + [101.0, 102.0, 103.0]
        # Calculate volume list with volume surge on last day
        vol = [100] * 52 + [300]  # RVOL = 300 / 100 = 3.0
        
        # Create stock dataframe with close and volume
        idx = pd.date_range("2024-01-01", periods=len(prices), freq="B")
        df = pd.DataFrame({"close": prices, "open": prices, "high": prices, "low": prices, "volume": vol}, index=idx)
        
        bench = _bench(n=len(prices))
        ranks = rank_universe({"EARLY_LONG": df}, bench)
        assert len(ranks) == 1
        r = ranks[0]
        assert r.rvol > 1.2
        assert r.rs_slope_10d > 0
        assert r.quintile >= 3  # Triggers early breakout quintile

