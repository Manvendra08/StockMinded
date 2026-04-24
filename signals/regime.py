"""Market regime classifier. Outputs one of 6 regimes + score dict."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum

import numpy as np
import pandas as pd

from data import feed


class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE_LOW_VOL = "RANGE_LOW_VOL"
    RANGE_HIGH_VOL = "RANGE_HIGH_VOL"
    VOL_EXPANSION = "VOL_EXPANSION"
    VOL_CONTRACTION = "VOL_CONTRACTION"


@dataclass
class RegimeSnapshot:
    regime: Regime
    trend_score: int
    vix: float
    vix_5d_change_pct: float
    adx: float
    breadth_pct_above_50dma: float | None
    notes: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["regime"] = self.regime.value
        return d


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _adx(df: pd.DataFrame, n: int = 14) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    dn = -low.diff()
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = pd.concat([
        (high - low),
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return float(dx.ewm(alpha=1 / n, adjust=False).mean().iloc[-1])


def _trend_score(close: pd.Series) -> int:
    e20 = _ema(close, 20).iloc[-1]
    e50 = _ema(close, 50).iloc[-1]
    e200 = _ema(close, 200).iloc[-1]
    px = close.iloc[-1]
    score = 0
    # Price vs EMAs
    score += 1 if px > e20 else -1
    score += 1 if px > e50 else -1
    score += 1 if px > e200 else -1
    # EMA alignment
    score += 1 if e20 > e50 else -1
    score += 1 if e50 > e200 else -1
    # EMA slopes (using last 5 days)
    e20_slope = 1 if len(close) > 5 and _ema(close, 20).iloc[-1] > _ema(close, 20).iloc[-5] else -1
    e50_slope = 1 if len(close) > 5 and _ema(close, 50).iloc[-1] > _ema(close, 50).iloc[-5] else -1
    score += e20_slope
    score += e50_slope
    return int(score)


def breadth_pct_above_50dma(stock_data: dict[str, pd.DataFrame]) -> float | None:
    if not stock_data:
        return None
    above = 0
    total = 0
    for _, df in stock_data.items():
        if df is None or df.empty or len(df) < 50:
            continue
        sma50 = df["close"].rolling(50).mean().iloc[-1]
        if pd.isna(sma50):
            continue
        total += 1
        if df["close"].iloc[-1] > sma50:
            above += 1
    return round(100 * above / total, 1) if total else None


def classify(index_symbol: str = "NIFTY", stock_universe_data: dict | None = None) -> RegimeSnapshot:
    idx = feed.ohlc_cached(index_symbol, period="2y")
    vix = feed.ohlc_cached("INDIAVIX", period="3mo")

    if idx is None or idx.empty or vix is None or vix.empty:
        return RegimeSnapshot(
            regime=Regime.RANGE_LOW_VOL,
            trend_score=0,
            vix=0.0,
            vix_5d_change_pct=0.0,
            adx=0.0,
            breadth_pct_above_50dma=0.0,
            notes="fallback due to missing data",
        )

    trend = _trend_score(idx["close"])
    adx = _adx(idx)
    vix_now = float(vix["close"].iloc[-1])
    vix_5d_ago = float(vix["close"].iloc[-6]) if len(vix) >= 6 else vix_now
    vix_chg = 100 * (vix_now - vix_5d_ago) / vix_5d_ago if vix_5d_ago else 0.0
    breadth = breadth_pct_above_50dma(stock_universe_data or {})

    # Rule stack
    notes = []
    if vix_chg > 25 and vix_now > 16:
        regime = Regime.VOL_EXPANSION
        notes.append(f"VIX +{vix_chg:.1f}% in 5d at {vix_now:.1f}")
    elif vix_chg < -20 and vix_now < 14:
        regime = Regime.VOL_CONTRACTION
        notes.append(f"VIX {vix_chg:.1f}% in 5d at {vix_now:.1f}")
    elif adx >= 25 and trend >= 2:
        regime = Regime.TREND_UP
    elif adx >= 25 and trend <= -2:
        regime = Regime.TREND_DOWN
    elif adx < 20 and vix_now < 14:
        regime = Regime.RANGE_LOW_VOL
    elif adx < 20 and vix_now >= 16:
        regime = Regime.RANGE_HIGH_VOL
    else:
        regime = Regime.RANGE_LOW_VOL if vix_now < 15 else Regime.RANGE_HIGH_VOL
        notes.append("transition zone")

    return RegimeSnapshot(
        regime=regime,
        trend_score=trend,
        vix=round(vix_now, 2),
        vix_5d_change_pct=round(vix_chg, 2),
        adx=round(adx, 2),
        breadth_pct_above_50dma=breadth,
        notes="; ".join(notes) or "ok",
    )
