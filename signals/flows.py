"""Money flow: FII/DII, sector rotation, option chain PCR + max pain."""
from __future__ import annotations

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
    notes: str = ""

    def to_dict(self) -> dict:
        return {
            "fii_dii_5d_net_cr": self.fii_dii_5d_net_cr,
            "top_inflow_sectors": self.top_inflow_sectors,
            "top_outflow_sectors": self.top_outflow_sectors,
            "pcr_oi": self.pcr_oi,
            "pcr_vol": self.pcr_vol,
            "max_pain": self.max_pain,
            "smart_money_bias": self.smart_money_bias,
            "notes": self.notes,
        }


def fii_dii_5d_net() -> dict[str, float]:
    try:
        df = feed.fii_dii_cash(days=10)  # get more rows since each date has 2 rows (FII + DII)
    except Exception:
        return {"fii": 0.0, "dii": 0.0}
    out = {"fii": 0.0, "dii": 0.0}
    if df.empty:
        return out
    # nse_fiidii returns: category, date, buyValue, sellValue, netValue
    # category values: 'DII', 'FII/FPI'
    cols = {c.lower(): c for c in df.columns}
    cat_col = cols.get("category") or cols.get("clienttype")
    net_col = cols.get("netvalue") or cols.get("net")
    if not cat_col or not net_col:
        return out
    for _, row in df.iterrows():
        c = str(row[cat_col]).strip().lower()
        raw_val = row[net_col]
        # netValue may be string with commas like "2,966.89"
        if isinstance(raw_val, str):
            raw_val = raw_val.replace(",", "")
        v = float(raw_val) if pd.notna(raw_val) else 0.0
        if "fii" in c or "fpi" in c:
            out["fii"] += v
        elif "dii" in c:
            out["dii"] += v
    return {k: round(v, 2) for k, v in out.items()}


def sector_relative_strength(sector_data: dict[str, pd.DataFrame], lookback: int = 5) -> list[tuple[str, float]]:
    out = []
    for name, df in sector_data.items():
        if df is None or df.empty or len(df) < lookback + 1:
            continue
        ret = 100 * (df["close"].iloc[-1] / df["close"].iloc[-lookback - 1] - 1)
        out.append((name, round(float(ret), 2)))
    return sorted(out, key=lambda x: x[1], reverse=True)


def pcr_and_max_pain(symbol: str = "NIFTY") -> tuple[float | None, float | None, float | None]:
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


def _bias(fii_dii: dict[str, float], pcr_oi: float | None) -> str:
    score = 0
    net = fii_dii.get("fii", 0) + fii_dii.get("dii", 0)
    if net > 500:
        score += 1
    elif net < -500:
        score -= 1
    if pcr_oi is not None:
        if pcr_oi > 1.3:
            score += 1       # put writers dominant → bullish
        elif pcr_oi < 0.7:
            score -= 1
    if score >= 1:
        return "LONG"
    if score <= -1:
        return "SHORT"
    return "NEUTRAL"


def snapshot(sector_data: dict[str, pd.DataFrame], index_symbol: str = "NIFTY") -> FlowSnapshot:
    fii_dii = fii_dii_5d_net()
    rs = sector_relative_strength(sector_data, lookback=5)
    top_in = rs[:3]
    top_out = rs[-3:][::-1] if len(rs) >= 3 else []
    pcr_oi, pcr_vol, mp = pcr_and_max_pain(index_symbol)
    return FlowSnapshot(
        fii_dii_5d_net_cr=fii_dii,
        top_inflow_sectors=top_in,
        top_outflow_sectors=top_out,
        pcr_oi=pcr_oi,
        pcr_vol=pcr_vol,
        max_pain=mp,
        smart_money_bias=_bias(fii_dii, pcr_oi),
    )
