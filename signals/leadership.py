"""Leadership: RS-line ranking against Nifty, A-grade long/short lists."""
from __future__ import annotations

import datetime as dt
from datetime import timezone, timedelta
from dataclasses import dataclass

import numpy as np
import pandas as pd

from data import feed


def _projected_volume_multiplier() -> float:
    """
    Calculate the projection multiplier for today's volume based on the elapsed market time.
    Active market hours: 09:15 to 15:30 IST (375 minutes).
    Returns a multiplier >= 1.0 to scale today's partial volume.
    If the market is closed or not yet open, returns 1.0.

    NOTE: Early-session volume is NOT a reliable proxy for full-day volume. A
    naive 1/elapsed_fraction projection massively inflates RVOL in the first
    hour (e.g. a 2x multiplier before 10:00), which then manufactures false
    Q4/Q5 breakout quintiles. To avoid that, we only apply projection once a
    meaningful part of the session has elapsed; before that we return 1.0 so
    the raw (partial) volume is used and the stock simply does not qualify for
    a volume-confirmed quintile until volume is real.
    """
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = dt.datetime.now(ist)

    if now_ist.weekday() >= 5:  # Saturday or Sunday
        return 1.0

    market_start = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_end = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)

    if now_ist < market_start:
        return 1.0
    if now_ist >= market_end:
        return 1.0

    elapsed_minutes = (now_ist - market_start).total_seconds() / 60.0
    if elapsed_minutes <= 0:
        return 1.0

    total_minutes = 375.0
    elapsed_fraction = elapsed_minutes / total_minutes

    # Only project once at least 50% of the session has elapsed. Before that,
    # the partial-day volume is not a reliable full-day sample, so we keep the
    # raw volume (multiplier = 1.0) rather than extrapolating upward.
    if elapsed_fraction < 0.5:
        return 1.0

    # BUG-18 FIX preserved: clamp the elapsed fraction so the multiplier stays
    # bounded (max ~6.7x at the 0.15 floor), preventing runaway RVOL spikes.
    elapsed_fraction = max(0.15, min(1.0, elapsed_fraction))
    return 1.0 / elapsed_fraction


@dataclass
class StockRank:
    symbol: str
    rs_slope_20d: float
    pct_vs_50dma: float
    quintile: int          # 1 (worst) .. 5 (best)
    above_50dma: bool
    rs_slope_10d: float = 0.0
    rvol: float = 1.0
    pct_vs_20dma: float = 0.0


def _rs_line(stock_close: pd.Series, bench_close: pd.Series) -> pd.Series:
    aligned = pd.concat([stock_close, bench_close], axis=1, join="inner").dropna()
    aligned.columns = ["s", "b"]
    return aligned["s"] / aligned["b"]


def _slope(series: pd.Series, n: int = 20) -> float:
    if len(series) < n:
        return 0.0
    y = series.tail(n).values
    x = np.arange(len(y))
    m, _ = np.polyfit(x, y, 1)
    return float(m / y.mean()) if y.mean() else 0.0


def rank_universe(stock_data: dict[str, pd.DataFrame], bench_df: pd.DataFrame) -> list[StockRank]:
    ranks: list[StockRank] = []
    if bench_df is None or bench_df.empty or "close" not in bench_df.columns:
        return []
    bench_close = bench_df["close"].dropna()
    for sym, df in stock_data.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        valid_close = df["close"].dropna()
        if len(valid_close) < 50:
            continue
        rs = _rs_line(valid_close, bench_close)
        slope20 = _slope(rs, 20)
        slope10 = _slope(rs, 10)
        sma50 = valid_close.rolling(50).mean().iloc[-1]
        sma20 = valid_close.rolling(20).mean().iloc[-1]
        px = float(valid_close.iloc[-1])
        if pd.isna(sma50) or pd.isna(px) or pd.isna(sma20):
            continue
        pct_vs = 100 * (px / float(sma50) - 1)
        pct_vs_20 = 100 * (px / float(sma20) - 1)

        # Calculate Relative Volume (RVOL) = today_vol / 20-day mean
        rvol = 1.0
        if "volume" in df.columns:
            valid_vol = df["volume"].dropna()
            if len(valid_vol) >= 21:
                prev_20 = valid_vol.iloc[-21:-1]
                mean_v = prev_20.mean()
                if mean_v > 0:
                    today_vol = float(valid_vol.iloc[-1])
                    ist = timezone(timedelta(hours=5, minutes=30))
                    last_idx = valid_vol.index[-1]
                    if hasattr(last_idx, "tz_convert"):
                        try:
                            last_date = last_idx.tz_convert(ist).date()
                        except Exception:
                            last_date = last_idx.date()
                    elif hasattr(last_idx, "date"):
                        last_date = last_idx.date()
                    else:
                        last_date = last_idx
                    today_ist = dt.datetime.now(ist).date()
                    if last_date == today_ist:
                        today_vol *= _projected_volume_multiplier()
                    rvol = today_vol / mean_v

        ranks.append(
            StockRank(
                symbol=sym,
                rs_slope_20d=round(slope20 * 1e4, 2),
                pct_vs_50dma=round(pct_vs, 2),
                quintile=1,
                above_50dma=bool(px > sma50),
                rs_slope_10d=round(slope10 * 1e4, 2),
                rvol=round(rvol, 2),
                pct_vs_20dma=round(pct_vs_20, 2)
            )
        )

    if not ranks:
        return []
    
    # Redesigned Symmetric Scoring Bands for Early Breakout with Conviction (Q=1..5)
    for r in ranks:
        s10 = r.rs_slope_10d
        s20 = r.rs_slope_20d
        vs20 = r.pct_vs_20dma
        vs50 = r.pct_vs_50dma
        rvol = r.rvol
        
        # Determine position relative to 50 DMA:
        # - strictly_above: price > 50 DMA (bullish bias)
        # - at_or_below: price <= 50 DMA (bearish or neutral)
        # This fixes the edge case where price == exactly 50 DMA
        at_or_below_50dma = (r.pct_vs_50dma <= 0)
        strictly_above_50dma = (r.pct_vs_50dma > 0)
        
        # Long setups (require strictly above 50 DMA)
        if s10 > 15 and s20 > 5 and (0.0 <= vs20 <= 6.0) and strictly_above_50dma and rvol > 1.2:
            r.quintile = 5
        elif s10 > 10 and s20 > 0 and (-1.0 <= vs20 <= 8.0) and vs50 > -2.0 and strictly_above_50dma and rvol > 1.0:
            r.quintile = 4
        elif s10 > 5 and (-2.0 <= vs20 <= 10.0) and strictly_above_50dma:
            r.quintile = 3
        elif s10 > 0 and strictly_above_50dma:
            r.quintile = 2
        # Short setups (require at or below 50 DMA)
        elif s10 < -15 and s20 < -5 and (-6.0 <= vs20 <= 0.0) and at_or_below_50dma and rvol > 1.2:
            r.quintile = 5
        elif s10 < -10 and s20 < 0 and (-8.0 <= vs20 <= 1.0) and vs50 < 2.0 and at_or_below_50dma and rvol > 1.0:
            r.quintile = 4
        elif s10 < -5 and (-10.0 <= vs20 <= 2.0) and at_or_below_50dma:
            r.quintile = 3
        elif s10 < 0 and at_or_below_50dma:
            r.quintile = 2
        else:
            r.quintile = 1
            
    return ranks


def a_grade(ranks: list[StockRank], inflow_sectors: list[str] | None = None,
            sector_map: dict[str, str] | None = None, top_n: int = 10) -> tuple[list[StockRank], list[StockRank]]:
    """Leaders inside top-inflow sectors = A-grade longs; laggards inside outflow = A-grade shorts.

    If sector_map is not provided, returns raw quintile extremes.
    """
    # A-grade requires strong RS conviction (Q4/Q5). Q2/Q3 are watchlist only.
    leaders = [r for r in ranks if r.quintile >= 4 and r.above_50dma and r.rs_slope_20d > 0]
    if not leaders:
        # Fallback to Q3 if no Q4/Q5 leaders found
        leaders = [r for r in ranks if r.quintile >= 3 and r.above_50dma and r.rs_slope_20d > 0]

    laggards = [r for r in ranks if r.quintile >= 4 and not r.above_50dma and r.rs_slope_20d < 0]
    if not laggards:
        laggards = [r for r in ranks if r.quintile >= 3 and not r.above_50dma and r.rs_slope_20d < 0]

    if sector_map and inflow_sectors:
        inflow_set = set(inflow_sectors)
        # Attempt to filter by inflow, but keep the original candidates if none match the sector filter
        sector_leaders = [r for r in leaders if sector_map.get(r.symbol) in inflow_set]
        if sector_leaders: leaders = sector_leaders
        
        sector_laggards = [r for r in laggards if sector_map.get(r.symbol) not in inflow_set]
        if sector_laggards: laggards = sector_laggards

    # Sort: Higher quintile first, then by trend slope magnitude
    leaders.sort(key=lambda r: (r.quintile, r.rs_slope_20d), reverse=True)
    laggards.sort(key=lambda r: (r.quintile, -r.rs_slope_20d), reverse=True)
    return leaders[:top_n], laggards[:top_n]


def build(universe: list[str], index_symbol: str = "NIFTY") -> tuple[list[StockRank], list[StockRank], list[StockRank]]:
    bench = feed.ohlc_cached(index_symbol, period="1y")
    data = feed.universe_ohlc(universe, period="1y")
    ranks = rank_universe(data, bench)
    longs, shorts = a_grade(ranks)
    return ranks, longs, shorts
