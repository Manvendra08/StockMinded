"""Money flow: FII/DII, sector rotation, option chain PCR + max pain."""

from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from data import feed


@dataclass
class FlowSnapshot:
    fii_dii_5d_net_cr: dict[str, float]
    top_inflow_sectors: list[tuple[str, float]]
    top_outflow_sectors: list[tuple[str, float]]
    pcr_oi: float | None
    pcr_vol: float | None
    max_pain: float | None
    smart_money_bias: str  # LONG | SHORT | NEUTRAL
    pcr_stale: bool = False
    mp_stale: bool = False
    fii_dii_stale: bool = False  # True when FII/DII data fetch failed
    pcr_updated_at: float | None = None
    mp_updated_at: float | None = None
    notes: str = ""
    option_source: str | None = None
    ai_sentiment: str | None = None
    fii_derivatives_5d: dict[str, float] = field(default_factory=dict)
    fii_derivatives_stale: bool = False
    trendlyne_kpis: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "fii_dii_5d_net_cr": self.fii_dii_5d_net_cr,
            "top_inflow_sectors": self.top_inflow_sectors,
            "top_outflow_sectors": self.top_outflow_sectors,
            "pcr_oi": self.pcr_oi,
            "pcr_vol": self.pcr_vol,
            "max_pain": self.max_pain,
            "smart_money_bias": self.smart_money_bias,
            "pcr_stale": self.pcr_stale,
            "mp_stale": self.mp_stale,
            "fii_dii_stale": self.fii_dii_stale,
            "pcr_updated_at": self.pcr_updated_at,
            "mp_updated_at": self.mp_updated_at,
            "notes": self.notes,
            "option_source": self.option_source,
            "ai_sentiment": self.ai_sentiment,
            "fii_derivatives_5d": self.fii_derivatives_5d,
            "fii_derivatives_stale": self.fii_derivatives_stale,
            "trendlyne_kpis": self.trendlyne_kpis,
        }


def fii_dii_5d_net() -> tuple[dict[str, float], bool]:
    """Return (net_cr_dict, stale).

    *stale* is True whenever the fetch failed or returned no usable rows,
    meaning the zeros in the dict are placeholders, not real data.
    """
    zero = {"fii": 0.0, "dii": 0.0}
    try:
        df = feed.fii_dii_cash(days=20)
    except Exception as e:
        print(f"[flows] FII/DII fetch failed: {e}")
        return zero, True

    if df.empty:
        return zero, True

    if "date" not in df.columns:
        return zero, True

    unique_dates = sorted(df["date"].unique())
    last_5_dates = unique_dates[-5:]
    df = df[df["date"].isin(last_5_dates)]

    out = {"fii": 0.0, "dii": 0.0}
    cols = {c.lower(): c for c in df.columns}
    cat_col = cols.get("category") or cols.get("clienttype")
    net_col = cols.get("netvalue") or cols.get("net")
    if not cat_col or not net_col:
        return zero, True

    for _, row in df.iterrows():
        c = str(row[cat_col]).strip().lower()
        raw_val = row[net_col]
        if isinstance(raw_val, str):
            raw_val = raw_val.replace(",", "")
        v = float(raw_val) if pd.notna(raw_val) else 0.0
        if "fii" in c or "fpi" in c:
            out["fii"] += v
        elif "dii" in c:
            out["dii"] += v

    return {k: round(v, 2) for k, v in out.items()}, False


def fii_dii_2d_trend(days: int = 20) -> tuple[float, float, bool]:
    """Return (fii_last2d_net, fii_prev3d_net, stale).

    Splits the 5-day FII cash window into the most-recent 2 days vs the
    prior 3 days so callers can detect intra-period reversals.
    A rising trajectory (last2d > prev3d_avg * 2/3) signals institutional
    re-entry even when the 5d sum is still negative.
    """
    zero = (0.0, 0.0, True)
    try:
        df = feed.fii_dii_cash(days=days)
    except Exception:
        return zero

    if df.empty or "date" not in df.columns:
        return zero

    cols = {c.lower(): c for c in df.columns}
    cat_col = cols.get("category") or cols.get("clienttype")
    net_col = cols.get("netvalue") or cols.get("net")
    if not cat_col or not net_col:
        return zero

    unique_dates = sorted(df["date"].unique())
    if len(unique_dates) < 5:
        return zero

    last2 = unique_dates[-2:]
    prev3 = unique_dates[-5:-2]

    def _sum(dates):
        sub = df[df["date"].isin(dates)]
        total = 0.0
        for _, row in sub.iterrows():
            c = str(row[cat_col]).strip().lower()
            if "fii" not in c and "fpi" not in c:
                continue
            raw = row[net_col]
            if isinstance(raw, str):
                raw = raw.replace(",", "")
            total += float(raw) if pd.notna(raw) else 0.0
        return round(total, 2)

    return _sum(last2), _sum(prev3), False


def sector_relative_strength(
    sector_data: dict[str, pd.DataFrame], lookback: int = 5
) -> list[tuple[str, float]]:
    out = []
    for name, df in sector_data.items():
        if df is None or df.empty or "close" not in df.columns:
            continue
        valid_close = df["close"].dropna()
        if len(valid_close) < lookback + 1:
            continue
        ret = 100 * (valid_close.iloc[-1] / valid_close.iloc[-lookback - 1] - 1)
        if pd.isna(ret) or np.isinf(ret):
            continue
        out.append((name, round(float(ret), 2)))
    return sorted(out, key=lambda x: x[1], reverse=True)


def pcr_and_max_pain(
    symbol: str = "NIFTY",
) -> tuple[float | None, float | None, float | None]:
    """Compute PCR (OI), PCR (volume) and max-pain from raw option chain.

    .. deprecated::
        This function duplicates logic already handled by
        ``feed.get_pcr_max_pain_cached()``, which is the canonical source
        used by :func:`snapshot`.  New code should call
        ``feed.get_pcr_max_pain_cached()`` directly and this function will
        be removed in a future cleanup.
    """
    warnings.warn(
        "pcr_and_max_pain() is deprecated. Use feed.get_pcr_max_pain_cached() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    try:
        raw = feed.option_chain(symbol)
        data = raw.get("records", {}).get("data", [])
    except Exception:
        return None, None, None
    if not data:
        return None, None, None

    ce_oi = pe_oi = ce_vol = pe_vol = 0.0
    strike_ce_oi: dict[float, float] = {}
    strike_pe_oi: dict[float, float] = {}
    for row in data:
        strike = row.get("strikePrice")
        ce = row.get("CE") or {}
        pe = row.get("PE") or {}
        ce_oi += ce.get("openInterest", 0) or 0
        pe_oi += pe.get("openInterest", 0) or 0
        ce_vol += ce.get("totalTradedVolume", 0) or 0
        pe_vol += pe.get("totalTradedVolume", 0) or 0
        if strike is not None:
            strike_ce_oi[strike] = ce.get("openInterest", 0) or 0
            strike_pe_oi[strike] = pe.get("openInterest", 0) or 0

    pcr_oi = round(pe_oi / ce_oi, 2) if ce_oi else None
    pcr_vol = round(pe_vol / ce_vol, 2) if ce_vol else None

    max_pain = None
    if strike_ce_oi and strike_pe_oi:
        strikes = sorted(set(strike_ce_oi) | set(strike_pe_oi))
        pain = {}
        for k in strikes:
            p = 0.0
            for s in strikes:
                if s < k:
                    p += strike_pe_oi.get(s, 0) * (k - s)
                elif s > k:
                    p += strike_ce_oi.get(s, 0) * (s - k)
            pain[k] = p
        max_pain = float(min(pain, key=pain.get))
    return pcr_oi, pcr_vol, max_pain


def _bias(
    fii_dii: dict[str, float],
    pcr_oi: float | None,
    fii_dii_stale: bool = False,
    derivatives: dict[str, float] | None = None,
    derivatives_stale: bool = False,
    ai_sentiment: dict | str | None = None,
    trendlyne: dict | None = None,
) -> str:
    """Compute smart-money bias with weighted, calibrated signal scoring.

    Signal weights and thresholds (NSE-calibrated):
    -----------------------------------------------
    FII Index Futures 5D net  : weight 2.0  — most reliable institutional lead
    PCR OI                    : weight 1.5  — NSE bands: >1.2 bull / <0.85 bear
    FII Cash Net 5D           : weight 1.0  — T+1 delayed, directional only
    FII Stock Futures 5D net  : weight 1.0
    FII Index Options 5D net  : weight 0.5  — noisy; low weight
    FII Index Long-Short Ratio: weight 1.5  — >1.25 bull / <0.75 bear
    AI Sentiment              : weight up to 1.0, confidence-scaled, applied when
                                score in (-3, 3)

    Conviction threshold: weighted_score >= 2.0 → LONG, <= -2.0 → SHORT.
    Raising from ±1 prevents single weak signals from declaring bias.

    2-day trend delta:
    If fii_last2d and fii_prev3d have opposite signs, the 5d net is
    misleading. We apply +0.5 weight boost in the direction of the 2d trend.
    """
    score = 0.0

    # --- FII Cash (weight 1.0) with 2-day recency delta ---
    if not fii_dii_stale:
        net5d = fii_dii.get("fii", 0.0)
        # Base score from 5d net
        if net5d > 500:
            score += 1.0
        elif net5d < -500:
            score -= 1.0

        # Recency delta: detect intra-period reversals
        try:
            last2, prev3, trend_stale = fii_dii_2d_trend()
            if not trend_stale:
                # Reversal: last 2d direction conflicts with prior 3d
                if prev3 < -200 and last2 > 100:
                    # Was selling, now buying — bullish reversal boost
                    score += 0.5
                elif prev3 > 200 and last2 < -100:
                    # Was buying, now selling — bearish reversal signal
                    score -= 0.5
        except Exception as e:
            logging.getLogger(__name__).exception("fii_dii_2d_trend() failed: %s", e)

    # --- PCR OI (weight 1.5, NSE-calibrated bands) ---
    if pcr_oi is not None:
        if pcr_oi > 1.2:
            score += 1.5
        elif pcr_oi < 0.85:
            score -= 1.5

    # --- Trendlyne FII Long-Short Ratio (weight 1.5) ---
    if trendlyne and trendlyne.get("fii_index_long_short_ratio"):
        try:
            ratio = float(trendlyne["fii_index_long_short_ratio"])
            if ratio > 1.25:
                score += 1.5
            elif ratio < 0.75:
                score -= 1.5
        except (ValueError, TypeError):
            pass

    # --- FII Derivatives (only when fresh) ---
    if not derivatives_stale and derivatives is not None:
        # FII Index Futures 5D Net (weight 2.0 — highest quality signal)
        idx_fut = derivatives.get("fii_index_futures_5d", 0.0)
        if idx_fut > 1000:
            score += 2.0
        elif idx_fut < -1000:
            score -= 2.0

        # FII Index Options 5D Net (weight 0.5 — noisy, low threshold)
        idx_opt = derivatives.get("fii_index_options_5d", 0.0)
        if idx_opt > 2000:
            score += 0.5
        elif idx_opt < -2000:
            score -= 0.5

        # FII Stock Futures 5D Net (weight 1.0)
        stk_fut = derivatives.get("fii_stock_futures_5d", 0.0)
        if stk_fut > 2000:
            score += 1.0
        elif stk_fut < -2000:
            score -= 1.0

    # --- AI Sentiment direction influence (weight 1.0, range (-3, 3)) ---
    # Wider range than before: AI can now tip bias when core signals are indecisive.
    # The verdict engine also uses AI independently, so this is a secondary influence.
    if ai_sentiment is not None and -3.0 < score < 3.0:
        # ai_sentiment may arrive as a full dict (from get_market_news_sentiment)
        # or as a plain string. Extract the relevant field when it's a dict.
        if isinstance(ai_sentiment, dict):
            _sentiment_str = str(
                ai_sentiment.get("overall_market_sentiment") or ""
            ).upper()
            _ai_conf = str(ai_sentiment.get("confidence") or "LOW").upper()
        else:
            _sentiment_str = str(ai_sentiment).upper()
            _ai_conf = "MEDIUM"
        # Scale weight by confidence: HIGH=1.0, MEDIUM=0.6, LOW=0.3
        _conf_mult = {"HIGH": 1.0, "MEDIUM": 0.6, "LOW": 0.3}.get(_ai_conf, 0.3)
        _ai_weight = 1.0 * _conf_mult
        if _sentiment_str in ("BULLISH", "POSITIVE", "LONG"):
            score += _ai_weight
        elif _sentiment_str in ("BEARISH", "NEGATIVE", "SHORT"):
            score -= _ai_weight

    # --- Conviction threshold: ±2.0 on a max possible ~6.5 scale ---
    if score >= 2.0:
        return "LONG"
    if score <= -2.0:
        return "SHORT"
    return "NEUTRAL"


def snapshot(
    sector_data: dict[str, pd.DataFrame], index_symbol: str = "NIFTY"
) -> FlowSnapshot:
    fii_dii, fii_dii_stale = fii_dii_5d_net()

    # Fetch FII derivatives data
    try:
        fii_derivs, fii_derivs_stale = feed.fii_dii_derivatives(days=5)
    except Exception as e:
        print(f"[flows.snapshot] FII derivatives fetch failed: {e}")
        fii_derivs, fii_derivs_stale = {}, True

    rs = sector_relative_strength(sector_data, lookback=5)
    top_in = rs[:3]
    top_out = rs[-3:][::-1]
    notes_parts = []
    if fii_dii_stale:
        notes_parts.append("FII/DII data unavailable")
    if fii_derivs_stale:
        notes_parts.append("FII derivatives data unavailable")

    try:
        pcr_oi, pcr_vol, mp, pcr_stale, mp_stale, pcr_updated_at, mp_updated_at = (
            feed.get_pcr_max_pain_cached(index_symbol)
        )
        if pcr_oi is None and mp is None and (pcr_stale or mp_stale):
            notes_parts.append("Option chain unavailable; PCR/max-pain unavailable")
    except Exception as e:
        pcr_oi, pcr_vol, mp = None, None, None
        pcr_stale, mp_stale = True, True
        pcr_updated_at, mp_updated_at = None, None
        notes_parts.append(f"Option chain unavailable: {e}")

    # AI Sentiment Analysis
    ai_sentiment = None
    try:
        from data import ai_scraper

        ai_sentiment = ai_scraper.get_market_news_sentiment()
    except Exception as e:
        print(f"[flows.snapshot] AI sentiment failed: {e}")

    # Trendlyne KPIs
    try:
        trendlyne_kpis = feed.fetch_trendlyne_options_kpis(index_symbol)
    except Exception:
        trendlyne_kpis = {}

    return FlowSnapshot(
        fii_dii_5d_net_cr=fii_dii,
        top_inflow_sectors=top_in,
        top_outflow_sectors=top_out,
        pcr_oi=pcr_oi,
        pcr_vol=pcr_vol,
        max_pain=mp,
        smart_money_bias=_bias(
            fii_dii,
            pcr_oi,
            fii_dii_stale=fii_dii_stale,
            derivatives=fii_derivs,
            derivatives_stale=fii_derivs_stale,
            ai_sentiment=ai_sentiment,
            trendlyne=trendlyne_kpis,
        ),
        pcr_stale=pcr_stale,
        mp_stale=mp_stale,
        fii_dii_stale=fii_dii_stale,
        pcr_updated_at=pcr_updated_at,
        mp_updated_at=mp_updated_at,
        notes="; ".join([n for n in notes_parts if n]),
        option_source=feed.option_chain_source(index_symbol),
        ai_sentiment=ai_sentiment,
        fii_derivatives_5d=fii_derivs,
        fii_derivatives_stale=fii_derivs_stale,
        trendlyne_kpis=trendlyne_kpis,
    )
