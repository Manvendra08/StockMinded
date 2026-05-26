"""Leadership: RS-line ranking against Nifty, A-grade long/short lists."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from data import feed


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
                    rvol = float(valid_vol.iloc[-1] / mean_v)

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
        
        # Long setups
        if s10 > 15 and s20 > 5 and (0.0 <= vs20 <= 6.0) and r.above_50dma and rvol > 1.2:
            r.quintile = 5
        elif s10 > 10 and s20 > 0 and (-1.0 <= vs20 <= 8.0) and vs50 > -2.0 and rvol > 1.0:
            r.quintile = 4
        elif s10 > 5 and (-2.0 <= vs20 <= 10.0):
            r.quintile = 3
        elif s10 > 0:
            r.quintile = 2
        # Short setups
        elif s10 < -15 and s20 < -5 and (-6.0 <= vs20 <= 0.0) and (not r.above_50dma) and rvol > 1.2:
            r.quintile = 5
        elif s10 < -10 and s20 < 0 and (-8.0 <= vs20 <= 1.0) and vs50 < 2.0 and rvol > 1.0:
            r.quintile = 4
        elif s10 < -5 and (-10.0 <= vs20 <= 2.0):
            r.quintile = 3
        elif s10 < 0:
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
