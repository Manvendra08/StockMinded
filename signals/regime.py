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
    vix_rank: float | None          # Issue #10: VIX percentile (0-100) over rolling 1-year window
    adx: float
    breadth_pct_above_50dma: float | None
    notes: str

    def to_dict(self) -> dict:
        d = asdict(self)
        d["regime"] = self.regime.value
        return d


def _vix_rank(vix_series: pd.Series, window: int = 252) -> float | None:
    """Issue #10: rolling percentile rank of the latest VIX value over `window` sessions."""
    if len(vix_series) < 30:
        return None
    tail = vix_series.iloc[-window:].dropna()
    if tail.empty:
        return None
    current = float(tail.iloc[-1])
    low, high = float(tail.min()), float(tail.max())
    if high == low:
        return 50.0
    percentile = (tail <= current).sum() / len(tail) * 100.0
    return round(float(percentile), 1)


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _adx(df: pd.DataFrame, n: int = 14) -> float:
    """Compute Average Directional Index.

    H2 FIX: Replaced np.where() with pandas .where() to preserve index
    alignment. np.where() converts Series to numpy arrays, losing the
    pandas index, which can cause row-shift errors when the DataFrame
    has been filtered or sliced.
    """
    high, low, close = df["high"], df["low"], df["close"]
    up = high.diff()
    dn = -low.diff()
    # H2 FIX: Use pandas .where() to keep Series index intact
    cond_plus = (up > dn) & (up > 0)
    cond_minus = (dn > up) & (dn > 0)
    plus_dm = up.where(cond_plus, 0.0)
    minus_dm = dn.where(cond_minus, 0.0)
    tr = pd.concat([
        (high - low),
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / n, adjust=False).mean()
    # H2 FIX: No need for pd.Series(..., index=df.index) — already a Series
    plus_di = 100 * plus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1 / n, adjust=False).mean() / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, 1e-9)
    return float(dx.ewm(alpha=1 / n, adjust=False).mean().fillna(0).iloc[-1])


def _cmp(a: float, b: float, neutral_band: float = 0.0015) -> int:
    if not b:
        return 0
    diff = (a - b) / abs(b)
    if diff > neutral_band:
        return 1
    if diff < -neutral_band:
        return -1
    return 0


def _trend_score(close: pd.Series) -> int:
    ema20 = _ema(close, 20)
    ema50 = _ema(close, 50)
    ema200 = _ema(close, 200)
    e20 = float(ema20.iloc[-1])
    e50 = float(ema50.iloc[-1])
    e200 = float(ema200.iloc[-1])
    px = float(close.iloc[-1])
    score = 0
    score += _cmp(px, e20)
    score += _cmp(px, e50)
    score += _cmp(px, e200)
    score += _cmp(e20, e50)
    score += _cmp(e50, e200)
    if len(close) > 20:
        score += _cmp(e20, float(ema20.iloc[-5]))
        score += _cmp(e50, float(ema50.iloc[-5]))
        score += _cmp(px, float(close.iloc[-6]), neutral_band=0.003)
        score += _cmp(px, float(close.iloc[-21]), neutral_band=0.006)
        score += _cmp(float(close.iloc[-6]), float(close.iloc[-21]), neutral_band=0.003)
    return int(score)


def breadth_pct_above_50dma(stock_data: dict[str, pd.DataFrame]) -> float | None:
    if not stock_data:
        return None
    above = 0
    total = 0
    for _, df in stock_data.items():
        if df is None or df.empty or len(df) < 50:
            continue
        try:
            col_name = None
            # BUG-09 FIX: Columns are always lowercased by _flatten_columns(),
            # so checking "Close" (capital C) is dead code. Only check "close".
            if "close" in df.columns:
                col_name = "close"
            elif isinstance(df.columns, pd.MultiIndex):
                flat_cols = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
                if "close" in flat_cols:
                    col_idx = flat_cols.index("close")
                    col_name = df.columns[col_idx]
            
            if col_name is None:
                continue
                
            close_series = df[col_name]
            sma50 = close_series.rolling(50).mean().iloc[-1]
            if pd.isna(sma50):
                continue
            total += 1
            if close_series.iloc[-1] > sma50:
                above += 1
        except Exception:
            continue
    return round(100 * above / total, 1) if total else None


def classify(index_symbol: str = "NIFTY", stock_universe_data: dict | None = None) -> RegimeSnapshot:
    idx = feed.ohlc_cached(index_symbol, period="2y")
    vix = feed.ohlc_cached("INDIAVIX", period="3mo")

    if idx is not None and not idx.empty:
        idx = idx.dropna(subset=["close"])
    if vix is not None and not vix.empty:
        vix = vix.dropna(subset=["close"])

    if idx is None or idx.empty or vix is None or vix.empty:
        return RegimeSnapshot(
            regime=Regime.RANGE_LOW_VOL,
            trend_score=0,
            vix=0.0,
            vix_5d_change_pct=0.0,
            vix_rank=None,
            adx=0.0,
            breadth_pct_above_50dma=None,
            notes="fallback due to missing data",
        )

    trend = _trend_score(idx["close"])
    adx = _adx(idx)
    vix_now = float(vix["close"].iloc[-1])
    # H3 FIX: Adaptive lookback for VIX change calculation.
    # When VIX data has fewer than 6 rows, use whatever is available
    # (minimum 2 rows) instead of falling back to vix_now (which zeroes
    # the change and silently suppresses VOL_EXPANSION detection).
    vix_len = len(vix)
    if vix_len >= 6:
        vix_5d_ago = float(vix["close"].iloc[-6])
    elif vix_len >= 2:
        vix_5d_ago = float(vix["close"].iloc[0])  # use earliest available
    else:
        vix_5d_ago = vix_now
    vix_chg = 100 * (vix_now - vix_5d_ago) / vix_5d_ago if vix_5d_ago else 0.0
    breadth = breadth_pct_above_50dma(stock_universe_data or {})

    breadth_val = 50.0 if breadth is None else float(breadth)

    # Rule stack. Breadth confirms direction; mixed breadth blocks clean trend calls.
    notes = []
    # Issue #7: VOL_EXPANSION requires VIX spike sustained across >= 2 consecutive sessions
    # (single-day spikes are too noisy to justify regime change)
    vix_prev = float(vix["close"].iloc[-2]) if len(vix) >= 2 else vix_now
    vix_prev_close = float(vix["close"].iloc[-3]) if len(vix) >= 3 else vix_prev
    prev_day_chg = 100 * (vix_prev - vix_prev_close) / vix_prev_close if vix_prev_close else 0.0
    two_session_spike = (vix_chg > 25 and vix_now > 16) and (prev_day_chg > 20 or vix_chg > 35)
    if two_session_spike:
        regime = Regime.VOL_EXPANSION
        notes.append(f"VIX +{vix_chg:.1f}% in 5d (2-session confirmed) at {vix_now:.1f}")
    elif vix_chg < -20 and vix_now < 14:
        regime = Regime.VOL_CONTRACTION
        notes.append(f"VIX {vix_chg:.1f}% in 5d at {vix_now:.1f}")
    elif trend <= -3 and breadth_val >= 55:
        # Issue #3: lower mixed-tape breadth floor from 60->55 to match the new 45 cap above
        regime = Regime.RANGE_HIGH_VOL if vix_now >= 16 else Regime.RANGE_LOW_VOL
        notes.append("mixed tape: index weak, breadth strong (>=55%)")
    elif trend >= 3 and breadth_val <= 40:
        regime = Regime.RANGE_HIGH_VOL if vix_now >= 16 else Regime.RANGE_LOW_VOL
        notes.append("mixed tape: index firm, breadth weak")
    elif adx >= 20 and trend >= 4 and breadth_val >= 50:
        regime = Regime.TREND_UP
    elif adx >= 20 and trend <= -4 and breadth_val <= 45:
        # Issue #3: raised breadth cap from 50->45; 46-50% was a ~40% dead zone
        # where TREND_DOWN fired in essentially mixed-market conditions.
        regime = Regime.TREND_DOWN
    elif adx < 20 and vix_now < 14:
        regime = Regime.RANGE_LOW_VOL
    elif adx < 20 and vix_now >= 16:
        regime = Regime.RANGE_HIGH_VOL
    else:
        regime = Regime.RANGE_LOW_VOL if vix_now < 15 else Regime.RANGE_HIGH_VOL
        notes.append("transition zone")

    vix_rank_val = _vix_rank(vix["close"])

    return RegimeSnapshot(
        regime=regime,
        trend_score=trend,
        vix=round(vix_now, 2),
        vix_5d_change_pct=round(vix_chg, 2),
        vix_rank=vix_rank_val,           # Issue #10
        adx=round(adx, 2),
        breadth_pct_above_50dma=breadth,
        notes="; ".join(notes) or "ok",
    )
