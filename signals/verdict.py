"""Trade verdict builder from regime, flow, breadth, and data quality."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class TradeVerdict:
    action: str
    tone: str
    bias: str
    confidence: str
    strategy: str
    top_long: str | None
    top_short: str | None
    can_trade_equity: bool
    can_trade_options: bool
    reasons: list[str]
    blocks: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_trade_verdict(data: dict) -> TradeVerdict:
    regime = data.get("regime", {}) or {}
    flows = data.get("flows", {}) or {}
    structure = data.get("structure", {}) or {}
    leaders = data.get("leaders", []) or []
    laggards = data.get("laggards", []) or []
    freshness = data.get("data_freshness", {}) or {}
    source_errors = data.get("source_errors", []) or []

    regime_name = str(regime.get("name") or "UNKNOWN")
    bias = str(flows.get("bias") or "NEUTRAL")
    trend = _num(regime.get("trend_score"))
    adx = _num(regime.get("adx"))
    vix = _num(regime.get("vix"))
    breadth_raw = regime.get("breadth_pct_above_50dma")
    breadth = _num(breadth_raw, 50.0)

    pcr = flows.get("pcr_oi")
    max_pain = flows.get("max_pain")
    option_stale = bool(flows.get("pcr_stale")) or bool(flows.get("mp_stale"))
    option_ok = pcr is not None and max_pain is not None and not option_stale
    data_stale = freshness.get("status") in ("OLD", "MISSING")

    reasons: list[str] = [
        f"Regime {regime_name}",
        f"Trend {trend:+.0f}/10",
        f"ADX {adx:.1f}",
        f"Breadth {breadth:.1f}%",
        f"Bias {bias}",
    ]
    blocks: list[str] = []

    if data_stale:
        blocks.append("Market data old")
    if source_errors:
        blocks.append("Data issues present")
    if not option_ok:
        blocks.append("Option chain/PCR/max-pain unavailable or stale")
    if vix >= 24:
        blocks.append("VIX extreme")

    top_long = (
        leaders[0].get("symbol")
        if leaders and isinstance(leaders[0], dict)
        else None
    )
    top_short = (
        laggards[0].get("symbol")
        if laggards and isinstance(laggards[0], dict)
        else None
    )
    strategy = structure.get("primary") or "No clear setup"

    if data_stale:
        return TradeVerdict(
            action="NO_TRADE_DATA_STALE",
            tone="unclear",
            bias=bias,
            confidence="LOW",
            strategy="No trade until market data refreshes.",
            top_long=None,
            top_short=None,
            can_trade_equity=False,
            can_trade_options=False,
            reasons=reasons,
            blocks=blocks,
        )

    if vix >= 24:
        return TradeVerdict(
            action="WAIT",
            tone="bear",
            bias=bias,
            confidence="HIGH",
            strategy="Stay flat; volatility is too high for fresh paper entries.",
            top_long=None,
            top_short=None,
            can_trade_equity=False,
            can_trade_options=False,
            reasons=reasons,
            blocks=blocks,
        )

    mixed_bear = trend <= -3 and breadth >= 60
    mixed_bull = trend >= 3 and breadth <= 40
    if mixed_bear or mixed_bull:
        side = "index weak, breadth strong" if mixed_bear else "index firm, breadth weak"
        return TradeVerdict(
            action="WAIT",
            tone="range",
            bias=bias,
            confidence="MEDIUM",
            strategy=f"Mixed tape ({side}). Skip index direction; take only manual A-grade stock setups.",
            top_long=top_long,
            top_short=None if mixed_bear else top_short,
            can_trade_equity=False,
            can_trade_options=False,
            reasons=reasons + [side],
            blocks=blocks,
        )

    if regime_name == "TREND_UP" and trend >= 4 and breadth >= 50 and bias != "SHORT":
        return TradeVerdict(
            action="LONG_ONLY",
            tone="bull",
            bias=bias,
            confidence="HIGH" if bias == "LONG" else "MEDIUM",
            strategy="Long A-grade leaders only; no shorts.",
            top_long=top_long,
            top_short=None,
            can_trade_equity=True,
            can_trade_options=False,
            reasons=reasons,
            blocks=blocks,
        )

    if regime_name == "TREND_DOWN" and trend <= -4 and breadth <= 50 and bias != "LONG":
        return TradeVerdict(
            action="SHORT_ONLY",
            tone="bear",
            bias=bias,
            confidence="HIGH" if bias == "SHORT" else "MEDIUM",
            strategy="Short A-grade laggards only; no longs.",
            top_long=None,
            top_short=top_short,
            can_trade_equity=True,
            can_trade_options=False,
            reasons=reasons,
            blocks=blocks,
        )

    if regime_name in ("RANGE_LOW_VOL", "RANGE_HIGH_VOL", "VOL_CONTRACTION"):
        if option_ok:
            return TradeVerdict(
                action="OPTION_SELL_DEFINED_RISK",
                tone="range",
                bias=bias,
                confidence="HIGH" if regime_name == "VOL_CONTRACTION" else "MEDIUM",
                strategy=strategy,
                top_long=None,
                top_short=None,
                can_trade_equity=False,
                can_trade_options=True,
                reasons=reasons + [f"PCR {pcr}", f"Max pain {max_pain}"],
                blocks=[b for b in blocks if not b.startswith("Option chain")],
            )
        return TradeVerdict(
            action="WAIT",
            tone="range",
            bias=bias,
            confidence="MEDIUM",
            strategy="Wait. Range regime needs fresh option chain before selling premium.",
            top_long=None,
            top_short=None,
            can_trade_equity=False,
            can_trade_options=False,
            reasons=reasons,
            blocks=blocks,
        )

    return TradeVerdict(
        action="WAIT",
        tone="unclear",
        bias=bias,
        confidence="LOW",
        strategy="No clean edge. Wait.",
        top_long=top_long,
        top_short=top_short,
        can_trade_equity=False,
        can_trade_options=False,
        reasons=reasons,
        blocks=blocks,
    )
