"""Split Trade verdict builder: Directional Stock Picking vs. Nifty Option Selling."""
from __future__ import annotations
from dataclasses import asdict, dataclass

# Issue #11: confidence label -> numeric score mapping (0-100)
_CONF_SCORE: dict[str, int] = {"HIGH": 80, "MEDIUM": 50, "LOW": 20}

def _conf_score(label: str) -> int:
    """Return numeric confidence score (0-100) backing the label."""
    return _CONF_SCORE.get(label.upper(), 0)

@dataclass
class StockVerdict:
    action: str  # LONG_ONLY, SHORT_ONLY, LONG_AND_SHORT, WAIT
    tone: str
    confidence: str
    confidence_score: int           # Issue #11: numeric 0-100 backing the label
    strategy: str
    top_long: str | None
    top_short: str | None
    can_trade: bool
    reasons: list[str]
    blocks: list[str]

@dataclass
class NiftyVerdict:
    action: str  # OPTION_SELL_DEFINED_RISK, NAKED_OPTION_SELL, WAIT
    tone: str
    bias: str
    confidence: str
    confidence_score: int           # Issue #11: numeric 0-100 backing the label
    strategy: str
    can_trade: bool
    reasons: list[str]
    blocks: list[str]

@dataclass
class CombinedVerdict:
    stock: StockVerdict
    nifty: NiftyVerdict
    
    def to_dict(self) -> dict:
        return {
            "stock": asdict(self.stock),
            "nifty": asdict(self.nifty),
            # Keep legacy top-level fields for dashboard compatibility
            "action": self.stock.action if self.stock.can_trade else self.nifty.action,
            "strategy": f"Stocks: {self.stock.strategy} | Nifty: {self.nifty.strategy}",
            "can_trade_equity": self.stock.can_trade,
            "can_trade_options": self.nifty.can_trade,
            "blocks": list(set(self.stock.blocks + self.nifty.blocks)),
            "reasons": list(set(self.stock.reasons + self.nifty.reasons)),
            "confidence": self.stock.confidence if self.stock.can_trade else self.nifty.confidence,
        }

def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def build_trade_verdict(data: dict) -> CombinedVerdict:
    regime = data.get("regime", {}) or {}
    flows = data.get("flows", {}) or {}
    structure = data.get("structure", {}) or {}
    leaders = data.get("leaders", []) or []
    laggards = data.get("laggards", []) or []
    freshness = data.get("data_freshness", {}) or {}
    source_errors = data.get("source_errors", []) or []

    regime_name = str(regime.get("name") or regime.get("regime") or "UNKNOWN")
    bias = str(flows.get("bias") or flows.get("smart_money_bias") or "NEUTRAL")
    trend = _num(regime.get("trend_score"))
    adx = _num(regime.get("adx"))
    vix = _num(regime.get("vix"))
    breadth = _num(regime.get("breadth_pct_above_50dma"), 50.0)

    pcr = flows.get("pcr_oi")
    max_pain = flows.get("max_pain")
    option_stale = bool(flows.get("pcr_stale")) or bool(flows.get("mp_stale"))
    option_ok = pcr is not None and max_pain is not None and not option_stale
    data_stale = freshness.get("status") in ("OLD", "MISSING") or freshness.get("status") == "MISSING"

    common_reasons = [
        f"Regime {regime_name}",
        f"Trend {trend:+.0f}/10",
        f"ADX {adx:.1f}",
        f"VIX {vix:.1f}",
    ]
    common_blocks = []
    if data_stale: common_blocks.append("Market data stale")
    if source_errors: common_blocks.append("Source errors present")
    if vix >= 25: common_blocks.append("VIX extreme (>25)")

    top_long = leaders[0].get("symbol") if leaders and isinstance(leaders[0], dict) else None
    top_short = laggards[0].get("symbol") if laggards and isinstance(laggards[0], dict) else None

    # --- 1. DIRECTIONAL STOCK PICKING SYSTEM ---
    stock_action = "WAIT"
    stock_strategy = "No clear directional edge for individual stocks."
    stock_can_trade = False
    stock_tone = "unclear"
    stock_conf = "LOW"
    stock_blocks = list(common_blocks)
    
    if not data_stale and vix < 25:
        if regime_name == "TREND_UP" and trend >= 3 and breadth >= 45:
            stock_action = "LONG_ONLY"
            stock_tone = "bull"
            stock_can_trade = True
            stock_conf = "HIGH" if bias == "LONG" else "MEDIUM"
            stock_strategy = "Long A-Grade leaders with RS Slope > 50."
        elif regime_name == "TREND_DOWN" and trend <= -3 and breadth <= 55:
            stock_action = "SHORT_ONLY"
            stock_tone = "bear"
            stock_can_trade = True
            stock_conf = "HIGH" if bias == "SHORT" else "MEDIUM"
            stock_strategy = "Short A-Grade laggards with RS Slope < -50."
        elif regime_name in ("RANGE_HIGH_VOL", "RANGE_LOW_VOL", "VOL_CONTRACTION"):
            # Range regimes: directional stock picking only when leadership is
            # exceptionally strong.  Marginal setups bleed via EOD close in
            # choppy conditions.  Prefer option premium selling instead.
            q5_longs = sum(1 for l in leaders if l.get("quintile", 0) == 5)
            q5_shorts = sum(1 for l in laggards if l.get("quintile", 0) == 5)

            # Gate: need >=5 Q5 names on at least one side to even consider
            if q5_longs >= 5 or q5_shorts >= 5:
                long_str = q5_longs + (2 if bias == "LONG" else 0) + (4 if trend > 0 else 0)
                short_str = q5_shorts + (2 if bias == "SHORT" else 0) + (4 if trend < 0 else 0)

                if q5_longs >= 5 and q5_shorts >= 5:
                    stock_action = "LONG_AND_SHORT"
                elif long_str > short_str + 4:
                    stock_action = "LONG_ONLY"
                elif short_str > long_str + 4:
                    stock_action = "SHORT_ONLY"
                else:
                    # Ambiguous - stay out in range regimes
                    pass

                if stock_action != "WAIT":
                    # Issue #8: "mixed" tone is now a first-class value; downstream
                    # consumers must handle it explicitly (e.g. present as "range play").
                    if stock_action == "LONG_AND_SHORT":
                        stock_tone = "mixed"
                    elif stock_action == "LONG_ONLY":
                        stock_tone = "bull"
                    else:
                        stock_tone = "bear"
                    stock_can_trade = True
                    # Confidence stays LOW unless leadership is overwhelming
                    stock_conf = "MEDIUM" if (q5_longs >= 7 or q5_shorts >= 7) else "LOW"
                    stock_strategy = f"Range leadership {stock_action}: Only Q5 RS candidates (high bar)."
    
    stock_v = StockVerdict(
        action=stock_action, tone=stock_tone,
        confidence=stock_conf, confidence_score=_conf_score(stock_conf),  # Issue #11
        strategy=stock_strategy, top_long=top_long, top_short=top_short,
        can_trade=stock_can_trade, reasons=list(common_reasons), blocks=stock_blocks
    )

    # --- 2. NIFTY OPTIONS SELLING SYSTEM ---
    nifty_action = "WAIT"
    nifty_strategy = "Waiting for fresh option chain / low VIX."
    nifty_can_trade = False
    nifty_tone = "range"
    nifty_conf = "LOW"
    nifty_blocks = list(common_blocks)
    if not option_ok: nifty_blocks.append("Option chain stale/missing")

    if not data_stale and vix < 25 and option_ok:
        nifty_can_trade = True
        iv_rank_val = _num(data.get("iv_rank"))   # Issue #6: caller must pass current IV rank
        iv_rank_ok  = (iv_rank_val is None) or (iv_rank_val >= 40)  # None = unknown -> allow but flag

        if regime_name in ("TREND_UP", "TREND_DOWN") and abs(trend) >= 4:
            # Issue #6: require IV rank >= 40 before naked sell; thin-premium environments
            # produce inadequate compensation for the open-ended risk.
            if not iv_rank_ok:
                nifty_action = "OPTION_SELL_DEFINED_RISK"
                nifty_conf = "LOW"
                nifty_tone = "bull" if trend > 0 else "bear"
                nifty_strategy = (
                    f"IV rank {iv_rank_val:.0f} < 40 - downgrade naked -> defined-risk spread."
                )
            else:
                nifty_action = "NAKED_OPTION_SELL"
                nifty_conf = "HIGH" if (trend > 0 and bias == "LONG") or (trend < 0 and bias == "SHORT") else "MEDIUM"
                nifty_tone = "bull" if trend > 0 else "bear"
                side = "PUTS" if trend > 0 else "CALLS"
                ivr_display = f"{iv_rank_val:.0f}" if iv_rank_val is not None else "N/A"
                nifty_strategy = f"Naked {side} selling with SL (Aggressive Trend, IVR {ivr_display})."
        elif regime_name in ("RANGE_HIGH_VOL", "RANGE_LOW_VOL", "VOL_CONTRACTION"):
            # Range-bound -> Defined risk (Iron Condor / Iron Fly)
            nifty_action = "OPTION_SELL_DEFINED_RISK"
            nifty_conf = "MEDIUM"
            nifty_strategy = structure.get("primary") or "Sell premium via Iron Condor/Fly (Range)."
        else:
            # Issue #6: fallback naked sell also requires IV rank >= 40
            if bias in ("LONG", "SHORT") and iv_rank_ok:
                nifty_action = "NAKED_OPTION_SELL"
                nifty_conf = "MEDIUM"
                side = "PUTS" if bias == "LONG" else "CALLS"
                ivr_display = f"{iv_rank_val:.0f}" if iv_rank_val is not None else "N/A"
                nifty_strategy = f"Naked {side} selling basis Smart Money Bias (IVR {ivr_display})."

    nifty_v = NiftyVerdict(
        action=nifty_action, tone=nifty_tone, bias=bias,
        confidence=nifty_conf, confidence_score=_conf_score(nifty_conf),  # Issue #11
        strategy=nifty_strategy, can_trade=nifty_can_trade,
        reasons=list(common_reasons) + [f"PCR {pcr}", f"MaxPain {max_pain}"],
        blocks=nifty_blocks
    )

    return CombinedVerdict(stock=stock_v, nifty=nifty_v)
