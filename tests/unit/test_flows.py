"""Tests for signals/flows.py — pure computation functions only (no feed calls)."""
import pytest
import pandas as pd
from unittest.mock import patch

from signals.flows import (
    sector_relative_strength,
    pcr_and_max_pain,
    _bias,
    FlowSnapshot,
)


def _make_sector_df(n: int, prices: list[float] | None = None) -> pd.DataFrame:
    if prices is None:
        prices = list(range(100, 100 + n))
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="B")
    return pd.DataFrame({"close": prices, "open": prices, "high": prices, "low": prices}, index=idx)


class TestSectorRelativeStrength:
    def test_ascending_sector_has_positive_return(self):
        df = _make_sector_df(10, [100, 101, 102, 103, 104, 105, 106, 107, 108, 110])
        result = sector_relative_strength({"IT": df}, lookback=5)
        assert result[0][0] == "IT"
        assert result[0][1] > 0

    def test_descending_sector_has_negative_return(self):
        df = _make_sector_df(10, [110, 108, 107, 106, 105, 104, 103, 102, 101, 100])
        result = sector_relative_strength({"IT": df}, lookback=5)
        assert result[0][1] < 0

    def test_sorted_by_return_descending(self):
        high = _make_sector_df(10, [100, 100, 100, 100, 100, 110, 115, 120, 125, 130])
        low = _make_sector_df(10, [100, 100, 100, 100, 100, 101, 102, 103, 104, 105])
        result = sector_relative_strength({"A": low, "B": high}, lookback=5)
        assert result[0][0] == "B"
        assert result[1][0] == "A"

    def test_insufficient_data_skipped(self):
        df = _make_sector_df(3, [100, 101, 102])
        result = sector_relative_strength({"X": df}, lookback=5)
        assert result == []

    def test_empty_input_returns_empty(self):
        result = sector_relative_strength({}, lookback=5)
        assert result == []


class TestPcrAndMaxPain:
    def _make_raw(self, strikes_ce: dict, strikes_pe: dict) -> dict:
        data = []
        for k in sorted(set(strikes_ce) | set(strikes_pe)):
            data.append({
                "strikePrice": k,
                "CE": {"openInterest": strikes_ce.get(k, 0), "totalTradedVolume": strikes_ce.get(k, 0) * 2},
                "PE": {"openInterest": strikes_pe.get(k, 0), "totalTradedVolume": strikes_pe.get(k, 0) * 2},
            })
        return {"records": {"data": data}}

    def test_pcr_oi_calculation(self):
        raw = self._make_raw({22000: 100, 22100: 100}, {22000: 80, 22100: 120})
        with patch("signals.flows.feed.option_chain", return_value=raw):
            pcr_oi, pcr_vol, _ = pcr_and_max_pain("NIFTY")
        assert pcr_oi == pytest.approx(200 / 200)

    def test_empty_option_chain_returns_nones(self):
        with patch("signals.flows.feed.option_chain", return_value={"records": {"data": []}}):
            pcr_oi, pcr_vol, mp = pcr_and_max_pain("NIFTY")
        assert pcr_oi is None
        assert pcr_vol is None
        assert mp is None

    def test_feed_exception_returns_nones(self):
        with patch("signals.flows.feed.option_chain", side_effect=Exception("network")):
            pcr_oi, pcr_vol, mp = pcr_and_max_pain("NIFTY")
        assert all(x is None for x in [pcr_oi, pcr_vol, mp])

    def test_max_pain_is_a_valid_strike(self):
        raw = self._make_raw(
            {22000: 500, 22100: 200, 22200: 100},
            {22000: 100, 22100: 200, 22200: 500},
        )
        with patch("signals.flows.feed.option_chain", return_value=raw):
            _, _, mp = pcr_and_max_pain("NIFTY")
        assert mp in [22000.0, 22100.0, 22200.0]


class TestBias:
    def test_bullish_fii_and_high_pcr(self):
        assert _bias({"fii": 1000, "dii": 0}, pcr_oi=1.4) == "LONG"

    def test_bearish_outflow_and_low_pcr(self):
        assert _bias({"fii": -1000, "dii": 0}, pcr_oi=0.6) == "SHORT"

    def test_neutral_when_mixed(self):
        assert _bias({"fii": 0, "dii": 0}, pcr_oi=1.0) == "NEUTRAL"

    def test_neutral_when_pcr_none(self):
        assert _bias({"fii": 0, "dii": 0}, pcr_oi=None) == "NEUTRAL"

    def test_bullish_fii_alone_with_neutral_pcr(self):
        result = _bias({"fii": 1000, "dii": 0}, pcr_oi=1.0)
        assert result == "NEUTRAL"

    def test_bearish_fii_alone_with_neutral_pcr(self):
        result = _bias({"fii": -1000, "dii": 0}, pcr_oi=1.0)
        assert result == "NEUTRAL"

    def test_derivatives_bullish_futures_and_stock_futures(self):
        derivs = {
            "fii_index_futures_5d": 1200.0,
            "fii_index_options_5d": 0.0,
            "fii_stock_futures_5d": 2500.0,
        }
        # Score: cash (0) + pcr (0) + futures (+1) + stk_fut (+1) = +2 -> LONG
        assert _bias({"fii": 0, "dii": 0}, pcr_oi=1.0, derivatives=derivs) == "LONG"

    def test_derivatives_bearish_options_and_futures(self):
        derivs = {
            "fii_index_futures_5d": -1500.0,
            "fii_index_options_5d": -6000.0,
            "fii_stock_futures_5d": 0.0,
        }
        # Score: cash (0) + pcr (0) + futures (-1) + options (-1) = -2 -> SHORT
        assert _bias({"fii": 0, "dii": 0}, pcr_oi=1.0, derivatives=derivs) == "SHORT"

    def test_derivatives_stale_ignored(self):
        derivs = {
            "fii_index_futures_5d": 2000.0,
            "fii_index_options_5d": 8000.0,
            "fii_stock_futures_5d": 4000.0,
        }
        # Since stale=True, derivatives score should not count, so result is NEUTRAL
        assert _bias({"fii": 0, "dii": 0}, pcr_oi=1.0, derivatives=derivs, derivatives_stale=True) == "NEUTRAL"


class TestFlowSnapshot:
    def test_to_dict_roundtrip(self):
        snap = FlowSnapshot(
            fii_dii_5d_net_cr={"fii": 500.0, "dii": 200.0},
            top_inflow_sectors=[("NIFTY IT", 2.3)],
            top_outflow_sectors=[("NIFTY METAL", -1.1)],
            pcr_oi=1.2,
            pcr_vol=0.9,
            max_pain=22100.0,
            smart_money_bias="LONG",
            notes="test",
        )
        d = snap.to_dict()
        assert d["smart_money_bias"] == "LONG"
        assert d["pcr_oi"] == 1.2
        assert d["fii_dii_5d_net_cr"]["fii"] == 500.0
