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
    confidence_score: int  # Issue #11: numeric 0-100 backing the label
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
    confidence_score: int  # Issue #11: numeric 0-100 backing the label
    strategy: str
    can_trade: bool
    reasons: list[str]
    blocks: list[str]


@dataclass
class CombinedVerdict:
    stock: StockVerdict
    nifty: NiftyVerdict
    nifty_heavyweight_momentum: float | None = None
    banknifty_heavyweight_momentum: float | None = None

    def to_dict(self) -> dict:
        d = {
            "stock": asdict(self.stock),
            "nifty": asdict(self.nifty),
            # Keep legacy top-level fields for dashboard compatibility
            "action": self.stock.action if self.stock.can_trade else self.nifty.action,
            "strategy": f"Stocks: {self.stock.strategy} | Nifty: {self.nifty.strategy}",
            "can_trade_equity": self.stock.can_trade,
            "can_trade_options": self.nifty.can_trade,
            "blocks": list(set(self.stock.blocks + self.nifty.blocks)),
            "reasons": list(set(self.stock.reasons + self.nifty.reasons)),
            "confidence": self.stock.confidence
            if self.stock.can_trade
            else self.nifty.confidence,
        }
        if self.nifty_heavyweight_momentum is not None:
            d["nifty_heavyweight_momentum"] = self.nifty_heavyweight_momentum
        if self.banknifty_heavyweight_momentum is not None:
            d["banknifty_heavyweight_momentum"] = self.banknifty_heavyweight_momentum
        return d


def _num(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _get_val(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def build_trade_verdict(data: dict) -> CombinedVerdict:
    # Fetch index heavyweight momentum
    nifty_momentum = None
    banknifty_momentum = None
    try:
        from signals.index_weightage import calculate_weighted_momentum
        nifty_mom_data = calculate_weighted_momentum("NIFTY")
        banknifty_mom_data = calculate_weighted_momentum("BANKNIFTY")
        nifty_momentum = nifty_mom_data.get("weighted_momentum")
        banknifty_momentum = banknifty_mom_data.get("weighted_momentum")
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("Failed to calculate heavyweight momentum in verdict: %s", e)

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

    # --- AI Sentiment Extraction (dict + legacy string-safe) ---
    ai_sentiment = flows.get("ai_sentiment")
    ai_overall = "NEUTRAL"
    ai_conf_lbl = "LOW"
    ai_score_raw = 0.0
    if isinstance(ai_sentiment, dict):
        ai_overall = str(
            ai_sentiment.get("overall_market_sentiment") or "NEUTRAL"
        ).upper()
        ai_conf_lbl = str(ai_sentiment.get("confidence") or "LOW").upper()
        ai_score_raw = _num(ai_sentiment.get("sentiment_score"), 0.0)
    elif isinstance(ai_sentiment, str):
        s = ai_sentiment.strip().upper()
        if s in ("BULLISH", "POSITIVE", "LONG"):
            ai_overall = "BULLISH"
        elif s in ("BEARISH", "NEGATIVE", "SHORT"):
            ai_overall = "BEARISH"
        else:
            ai_overall = "NEUTRAL"

    # AI direction score: +1 BULLISH, -1 BEARISH, else sign(sentiment_score) fallback
    if ai_overall == "BULLISH":
        ai_dir = 1
    elif ai_overall == "BEARISH":
        ai_dir = -1
    elif ai_score_raw > 0.15:
        ai_dir = 1
    elif ai_score_raw < -0.15:
        ai_dir = -1
    else:
        ai_dir = 0

    # AI confidence weight: HIGH=1.0, MEDIUM=0.5, LOW=0.2
    ai_weight = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2}.get(ai_conf_lbl, 0.0)
    # Combined AI influence: direction strength * confidence weight, range [-1, 1]
    ai_influence = ai_dir * ai_weight

    pcr = flows.get("pcr_oi")
    max_pain = flows.get("max_pain")
    option_stale = bool(flows.get("pcr_stale")) or bool(flows.get("mp_stale"))
    option_ok = pcr is not None and max_pain is not None and not option_stale
    data_stale = freshness.get("status") in ("OLD", "MISSING")

    common_reasons = [
        f"Regime {regime_name}",
        f"Trend {trend:+.0f}/10",
        f"ADX {adx:.1f}",
        f"VIX {vix:.1f}",
    ]
    common_blocks = []
    if data_stale:
        common_blocks.append("Market data stale")
    if source_errors:
        common_blocks.append("Source errors present")
    if vix >= 25:
        common_blocks.append("VIX extreme (>25)")

    top_long = _get_val(leaders[0], "symbol") if leaders else None
    top_short = _get_val(laggards[0], "symbol") if laggards else None

    # --- 1. DIRECTIONAL STOCK PICKING SYSTEM ---
    stock_action = "WAIT"
    stock_strategy = "No clear directional edge for individual stocks."
    stock_can_trade = False
    stock_tone = "unclear"
    stock_conf = "LOW"
    stock_blocks = list(common_blocks)
    stock_reasons_extra: list[str] = []

    if not data_stale and vix < 25:
        if regime_name == "TREND_UP" and trend >= 3 and breadth >= 45:
            stock_action = "LONG_ONLY"
            stock_tone = "bull"
            stock_can_trade = True
            # AI boost: if AI agrees (BULLISH + HIGH/MED conf), upgrade to HIGH
            if ai_influence > 0.4:
                stock_conf = "HIGH"
                stock_reasons_extra = [
                    f"AI sentiment {ai_overall} ({ai_conf_lbl}) aligns with trend up"
                ]
            elif bias == "LONG":
                stock_conf = "HIGH"
                stock_reasons_extra = []
            else:
                stock_conf = "MEDIUM"
                stock_reasons_extra = []
            # If AI is strongly bearish, note caution but don't block
            if ai_influence < -0.4:
                stock_conf = "MEDIUM"
                stock_reasons_extra = [
                    f"AI sentiment BEARISH ({ai_conf_lbl}) — caution on long entries"
                ]
            # Heavyweight Momentum adjustment
            if nifty_momentum is not None:
                if nifty_momentum < -0.25:
                    stock_conf = "MEDIUM" if stock_conf == "HIGH" else "LOW"
                    stock_reasons_extra.append(
                        f"Nifty heavyweight momentum bearish ({nifty_momentum:+.2f}%) — caution on longs"
                    )
                elif nifty_momentum > 0.25:
                    stock_reasons_extra.append(
                        f"Nifty heavyweight momentum bullish ({nifty_momentum:+.2f}%) aligns with longs"
                    )
            stock_strategy = "Long A-Grade leaders with RS Slope > 50."
        elif regime_name == "TREND_DOWN" and trend <= -3 and breadth <= 55:
            stock_action = "SHORT_ONLY"
            stock_tone = "bear"
            stock_can_trade = True
            # AI boost: if AI agrees (BEARISH + HIGH/MED conf), upgrade to HIGH
            if ai_influence < -0.4:
                stock_conf = "HIGH"
                stock_reasons_extra = [
                    f"AI sentiment {ai_overall} ({ai_conf_lbl}) aligns with trend down"
                ]
            elif bias == "SHORT":
                stock_conf = "HIGH"
                stock_reasons_extra = []
            else:
                stock_conf = "MEDIUM"
                stock_reasons_extra = []
            # If AI is strongly bullish, note caution but don't block
            if ai_influence > 0.4:
                stock_conf = "MEDIUM"
                stock_reasons_extra = [
                    f"AI sentiment BULLISH ({ai_conf_lbl}) — caution on short entries"
                ]
            # Heavyweight Momentum adjustment
            if nifty_momentum is not None:
                if nifty_momentum > 0.25:
                    stock_conf = "MEDIUM" if stock_conf == "HIGH" else "LOW"
                    stock_reasons_extra.append(
                        f"Nifty heavyweight momentum bullish ({nifty_momentum:+.2f}%) — caution on shorts"
                    )
                elif nifty_momentum < -0.25:
                    stock_reasons_extra.append(
                        f"Nifty heavyweight momentum bearish ({nifty_momentum:+.2f}%) aligns with shorts"
                    )
            stock_strategy = "Short A-Grade laggards with RS Slope < -50."
        elif regime_name in ("RANGE_HIGH_VOL", "RANGE_LOW_VOL", "VOL_CONTRACTION"):
            # Range regimes: directional stock picking only when leadership is
            # exceptionally strong.  Marginal setups bleed via EOD close in
            # choppy conditions.  Prefer option premium selling instead.
            q5_longs = sum(1 for l in leaders if _get_val(l, "quintile", 0) == 5)
            q5_shorts = sum(1 for l in laggards if _get_val(l, "quintile", 0) == 5)

            # Gate: need >=5 Q5 names on at least one side to even consider
            if q5_longs >= 5 or q5_shorts >= 5:
                long_str = (
                    q5_longs
                    + (2 if bias == "LONG" else 0)
                    + (4 if trend > 0 else 0)
                )
                short_str = (
                    q5_shorts
                    + (2 if bias == "SHORT" else 0)
                    + (4 if trend < 0 else 0)
                )

                # Evaluate strength first to avoid overlap bug
                # If one side has clear edge, take that direction
                if long_str > short_str + 4:
                    stock_action = "LONG_ONLY"
                elif short_str > long_str + 4:
                    stock_action = "SHORT_ONLY"
                elif q5_longs >= 5 and q5_shorts >= 5:
                    # Both sides qualify but no clear edge - mixed play
                    stock_action = "LONG_AND_SHORT"
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
                    # Confidence: AI alignment can boost from LOW to MEDIUM
                    ai_range_boost = abs(ai_influence) > 0.4
                    base_high = q5_longs >= 7 or q5_shorts >= 7
                    if ai_range_boost or base_high:
                        stock_conf = "MEDIUM"
                    else:
                        stock_conf = "LOW"
                    stock_strategy = f"Range leadership {stock_action}: Only Q5 RS candidates (high bar)."
                    # Log AI influence on range direction decision
                    if ai_influence != 0:
                        direction = "longs" if ai_influence > 0 else "shorts"
                        stock_reasons_extra.append(
                            f"AI sentiment {ai_overall} ({ai_conf_lbl}) favors {direction} (confidence boost)"
                        )

            # Intraday Momentum Override: fires in low-vol regimes when heavyweight
            # momentum is strongly directional. ADX/breadth are lagging indicators
            # that miss same-day rallies. This catches genuine momentum days.
            if stock_action == "WAIT" and vix < 18:
                if regime_name in ("RANGE_LOW_VOL", "VOL_CONTRACTION"):
                    if nifty_momentum is not None and nifty_momentum > 0.40:
                        stock_action = "LONG_ONLY"
                        stock_tone = "bull"
                        stock_can_trade = True
                        stock_conf = "MEDIUM"
                        stock_strategy = "Intraday momentum play: heavyweights driving LONG in low-vol regime."
                        stock_reasons_extra.append(
                            f"Nifty heavyweight momentum {nifty_momentum:+.2f}% signals bullish intraday"
                        )
                    elif nifty_momentum is not None and nifty_momentum < -0.40:
                        stock_action = "SHORT_ONLY"
                        stock_tone = "bear"
                        stock_can_trade = True
                        stock_conf = "MEDIUM"
                        stock_strategy = "Intraday momentum play: heavyweights driving SHORT in low-vol regime."
                        stock_reasons_extra.append(
                            f"Nifty heavyweight momentum {nifty_momentum:+.2f}% signals bearish intraday"
                        )
                    # AI bearish override: downgrade confidence if AI disagrees
                    if stock_action == "LONG_ONLY" and ai_influence < -0.4:
                        stock_conf = "LOW"
                        stock_reasons_extra.append(
                            f"AI sentiment {ai_overall} ({ai_conf_lbl}) contradicts intraday long"
                        )
                    elif stock_action == "SHORT_ONLY" and ai_influence > 0.4:
                        stock_conf = "LOW"
                        stock_reasons_extra.append(
                            f"AI sentiment {ai_overall} ({ai_conf_lbl}) contradicts intraday short"
                        )

    stock_reasons = list(common_reasons)
    if ai_weight > 0 and ai_dir != 0:
        stock_reasons.append(
            f"AI sentiment {ai_overall} ({ai_conf_lbl}, influence {ai_influence:+.1f})"
        )
    for extra in stock_reasons_extra:
        if extra not in stock_reasons:
            stock_reasons.append(extra)

    # --- 2. NIFTY OPTIONS SELLING SYSTEM ---
    nifty_action = "WAIT"
    nifty_strategy = "Waiting for fresh option chain / low VIX."
    nifty_can_trade = False
    nifty_tone = "range"
    nifty_conf = "LOW"
    nifty_blocks = list(common_blocks)
    nifty_reasons = list(common_reasons) + [f"PCR {pcr}", f"MaxPain {max_pain}"]
    if not option_ok:
        nifty_blocks.append("Option chain stale/missing")

    if not data_stale and vix < 25 and option_ok:
        nifty_can_trade = True
        iv_rank_val = data.get("iv_rank")
        if iv_rank_val is not None:
            try:
                iv_rank_val = float(iv_rank_val)
            except (ValueError, TypeError):
                iv_rank_val = None
        # Issue #6: IV rank check for naked sells
        # None = unknown IV history -> require defined-risk (safer default)
        # Only allow naked sells when we have confirmed IV rank >= 40
        iv_rank_ok = iv_rank_val is not None and iv_rank_val >= 40

        if regime_name in ("TREND_UP", "TREND_DOWN") and abs(trend) >= 4:
            # Issue #6: require IV rank >= 40 before naked sell; thin-premium environments
            # produce inadequate compensation for the open-ended risk.
            if not iv_rank_ok:
                nifty_action = "OPTION_SELL_DEFINED_RISK"
                nifty_conf = "LOW" if iv_rank_val is None else "MEDIUM"
                nifty_tone = "bull" if trend > 0 else "bear"
                if iv_rank_val is None:
                    nifty_strategy = "IV rank unknown - downgrade to defined-risk spread (safety first)."
                else:
                    nifty_strategy = f"IV rank {iv_rank_val:.0f} < 40 - downgrade naked -> defined-risk spread."
            else:
                nifty_action = "NAKED_OPTION_SELL"
                nifty_conf = (
                    "HIGH"
                    if (trend > 0 and bias == "LONG") or (trend < 0 and bias == "SHORT")
                    else "MEDIUM"
                )
                nifty_tone = "bull" if trend > 0 else "bear"
                side = "PUTS" if trend > 0 else "CALLS"
                nifty_strategy = f"Naked {side} selling with SL (Aggressive Trend, IVR {iv_rank_val:.0f})."
        elif regime_name in ("RANGE_HIGH_VOL", "RANGE_LOW_VOL", "VOL_CONTRACTION"):
            # Range-bound -> Defined risk (Iron Condor / Iron Fly)
            nifty_action = "OPTION_SELL_DEFINED_RISK"
            nifty_conf = "MEDIUM"
            nifty_strategy = (
                structure.get("primary") or "Sell premium via Iron Condor/Fly (Range)."
            )
        else:
            # Issue #6: fallback naked sell also requires IV rank >= 40
            if bias in ("LONG", "SHORT") and iv_rank_ok:
                nifty_action = "NAKED_OPTION_SELL"
                nifty_conf = "MEDIUM"
                side = "PUTS" if bias == "LONG" else "CALLS"
                nifty_strategy = (
                    f"Naked {side} selling basis Smart Money Bias (IVR {iv_rank_val:.0f})."
                )

    # --- 3. CIRCUIT BREAKER OVERRIDES ---
    # Threshold 2.5%: normal bullish/bearish days (0.5-1.5%) should not trigger;
    # only extreme momentum (>2.5%) or strong AI conviction (>0.8) blocks trades.
    bearish_cb = (nifty_momentum is not None and nifty_momentum <= -2.5) or (banknifty_momentum is not None and banknifty_momentum <= -2.5) or (ai_influence <= -0.8)
    bullish_cb = (nifty_momentum is not None and nifty_momentum >= 2.5) or (banknifty_momentum is not None and banknifty_momentum >= 2.5) or (ai_influence >= 0.8)

    if bearish_cb:
        if stock_action in ("LONG_ONLY", "LONG_AND_SHORT"):
            stock_action = "WAIT"
            stock_strategy = "Circuit Breaker: Severe bearish momentum/sentiment. Longs blocked."
            stock_reasons.append(f"Bearish Circuit Breaker (Nifty Mom: {nifty_momentum:+.2f}%, BN Mom: {banknifty_momentum:+.2f}%, AI Infl: {ai_influence:+.1f})")
            stock_blocks.append("Bearish Circuit Breaker: Longs Blocked")
        if nifty_action in ("OPTION_SELL_DEFINED_RISK", "NAKED_OPTION_SELL"):
            nifty_action = "WAIT"
            nifty_strategy = "Circuit Breaker: Severe bearish momentum/sentiment. Option selling blocked."
            nifty_reasons.append(f"Bearish Circuit Breaker (Nifty Mom: {nifty_momentum:+.2f}%, BN Mom: {banknifty_momentum:+.2f}%, AI Infl: {ai_influence:+.1f})")
            nifty_blocks.append("Bearish Circuit Breaker: Option Selling Blocked")
    elif bullish_cb:
        if stock_action in ("SHORT_ONLY", "LONG_AND_SHORT"):
            stock_action = "WAIT"
            stock_strategy = "Circuit Breaker: Severe bullish momentum/sentiment. Shorts blocked."
            stock_reasons.append(f"Bullish Circuit Breaker (Nifty Mom: {nifty_momentum:+.2f}%, BN Mom: {banknifty_momentum:+.2f}%, AI Infl: {ai_influence:+.1f})")
            stock_blocks.append("Bullish Circuit Breaker: Shorts Blocked")
        if nifty_action in ("OPTION_SELL_DEFINED_RISK", "NAKED_OPTION_SELL"):
            nifty_action = "WAIT"
            nifty_strategy = "Circuit Breaker: Severe bullish momentum/sentiment. Option selling blocked."
            nifty_reasons.append(f"Bullish Circuit Breaker (Nifty Mom: {nifty_momentum:+.2f}%, BN Mom: {banknifty_momentum:+.2f}%, AI Infl: {ai_influence:+.1f})")
            nifty_blocks.append("Bullish Circuit Breaker: Option Selling Blocked")

    stock_v = StockVerdict(
        action=stock_action,
        tone=stock_tone,
        confidence=stock_conf,
        confidence_score=_conf_score(stock_conf),  # Issue #11
        strategy=stock_strategy,
        top_long=top_long,
        top_short=top_short,
        can_trade=stock_can_trade,
        reasons=stock_reasons,
        blocks=stock_blocks,
    )

    nifty_v = NiftyVerdict(
        action=nifty_action,
        tone=nifty_tone,
        bias=bias,
        confidence=nifty_conf,
        confidence_score=_conf_score(nifty_conf),  # Issue #11
        strategy=nifty_strategy,
        can_trade=nifty_can_trade,
        reasons=nifty_reasons,
        blocks=nifty_blocks,
    )

    return CombinedVerdict(
        stock=stock_v,
        nifty=nifty_v,
        nifty_heavyweight_momentum=nifty_momentum,
        banknifty_heavyweight_momentum=banknifty_momentum
    )
