"""Money flow: FII/DII, sector rotation, option chain PCR + max pain."""
from __future__ import annotations

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
    smart_money_bias: str   # LONG | SHORT | NEUTRAL
    pcr_stale: bool = False
    mp_stale: bool = False
    fii_dii_stale: bool = False   # True when FII/DII data fetch failed
    pcr_updated_at: float | None = None
    mp_updated_at: float | None = None
    notes: str = ""
    option_source: str | None = None
    ai_sentiment: str | None = None
    fii_derivatives_5d: dict[str, float] = field(default_factory=dict)
    fii_derivatives_stale: bool = False

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
) -> str:
    """Compute smart-money bias. Stale FII/DII data does not contribute a score."""
    score = 0
    if not fii_dii_stale:
        net = fii_dii.get("fii", 0) + fii_dii.get("dii", 0)
        if net > 500:
            score += 1
        elif net < -500:
            score -= 1
            
    if pcr_oi is not None:
        if pcr_oi > 1.3:
            score += 1
        elif pcr_oi < 0.7:
            score -= 1

    if not derivatives_stale and derivatives is not None:
        # FII Index Futures 5D Net
        idx_fut = derivatives.get("fii_index_futures_5d", 0.0)
        if idx_fut > 1000:
            score += 1
        elif idx_fut < -1000:
            score -= 1
            
        # FII Index Options 5D Net
        idx_opt = derivatives.get("fii_index_options_5d", 0.0)
        if idx_opt > 5000:
            score += 1
        elif idx_opt < -5000:
            score -= 1
            
        # FII Stock Futures 5D Net
        stk_fut = derivatives.get("fii_stock_futures_5d", 0.0)
        if stk_fut > 2000:
            score += 1
        elif stk_fut < -2000:
            score -= 1

    if score >= 1:
        return "LONG"
    if score <= -1:
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
    top_out = rs[-3:][::-1] if len(rs) >= 3 else []
    notes_parts = []
    if fii_dii_stale:
        notes_parts.append("FII/DII data unavailable")
    if fii_derivs_stale:
        notes_parts.append("FII derivatives data unavailable")

    try:
        pcr_oi, pcr_vol, mp, pcr_stale, mp_stale, pcr_updated_at, mp_updated_at = feed.get_pcr_max_pain_cached(index_symbol)
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

    return FlowSnapshot(
        fii_dii_5d_net_cr=fii_dii,
        top_inflow_sectors=top_in,
        top_outflow_sectors=top_out,
        pcr_oi=pcr_oi,
        pcr_vol=pcr_vol,
        max_pain=mp,
        smart_money_bias=_bias(
            fii_dii, pcr_oi, fii_dii_stale=fii_dii_stale,
            derivatives=fii_derivs, derivatives_stale=fii_derivs_stale
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
    )
