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
    bench_close = bench_df["close"].dropna()
    for sym, df in stock_data.items():
        if df is None or df.empty:
            continue
        valid_close = df["close"].dropna()
        if len(valid_close) < 50:
            continue
        rs = _rs_line(valid_close, bench_close)
        slope = _slope(rs, 20)
        sma50 = valid_close.rolling(50).mean().iloc[-1]
        px = float(valid_close.iloc[-1])
        if pd.isna(sma50) or pd.isna(px):
            continue
        pct_vs = 100 * (px / float(sma50) - 1)
        ranks.append(StockRank(sym, round(slope * 1e4, 2), round(pct_vs, 2), 0, bool(px > sma50)))

    if not ranks:
        return []
    
    # Explicit threshold scoring for Q (1-5). Use symmetric downside bands
    # so short table also gets full intermediate grades.
    for r in ranks:
        slope = r.rs_slope_20d
        vs_dma = r.pct_vs_50dma
        if slope > 20 and vs_dma > 10:
            r.quintile = 5
        elif slope > 10 and vs_dma > 5:
            r.quintile = 4
        elif slope > 5 and vs_dma > 2:
            r.quintile = 3
        elif slope > 0 and vs_dma > 0:
            r.quintile = 2
        elif slope < -20 and vs_dma < -10:
            r.quintile = 5
        elif slope < -10 and vs_dma < -5:
            r.quintile = 4
        elif slope < -5 and vs_dma < -2:
            r.quintile = 3
        elif slope < 0 and vs_dma < 0:
            r.quintile = 2
        else:
            r.quintile = 1
    return ranks


def a_grade(ranks: list[StockRank], inflow_sectors: list[str] | None = None,
            sector_map: dict[str, str] | None = None, top_n: int = 10) -> tuple[list[StockRank], list[StockRank]]:
    """Leaders inside top-inflow sectors = A-grade longs; laggards inside outflow = A-grade shorts.

    If sector_map is not provided, returns raw quintile extremes.
    """
    leaders = [r for r in ranks if r.quintile >= 2 and r.above_50dma and r.rs_slope_20d > 0]
    if not leaders:
        leaders = [r for r in ranks if r.rs_slope_20d > 0]

    laggards = [r for r in ranks if r.quintile >= 2 and not r.above_50dma and r.rs_slope_20d < 0]
    if not laggards:
        laggards = [r for r in ranks if r.rs_slope_20d < 0]

    if sector_map and inflow_sectors:
        inflow_set = set(inflow_sectors)
        # Attempt to filter by inflow, but keep the original candidates if none match the sector filter
        sector_leaders = [r for r in leaders if sector_map.get(r.symbol) in inflow_set]
        if sector_leaders: leaders = sector_leaders
        
        sector_laggards = [r for r in laggards if sector_map.get(r.symbol) not in inflow_set]
        if sector_laggards: laggards = sector_laggards

    leaders.sort(key=lambda r: r.rs_slope_20d, reverse=True)
    laggards.sort(key=lambda r: r.rs_slope_20d)
    return leaders[:top_n], laggards[:top_n]


def build(universe: list[str], index_symbol: str = "NIFTY") -> tuple[list[StockRank], list[StockRank], list[StockRank]]:
    bench = feed.ohlc_cached(index_symbol, period="1y")
    data = feed.universe_ohlc(universe, period="1y")
    ranks = rank_universe(data, bench)
    longs, shorts = a_grade(ranks)
    return ranks, longs, shorts
