"""
Adaptive Regime Classifier using AGoT
======================================

Replaces the static rule-based regime classifier with an Adaptive Graph of Thoughts
approach that:

1. Generates MULTIPLE competing regime hypotheses (not just one)
2. Scores each hypothesis based on weighted evidence
3. Allows revision when new evidence contradicts previous conclusions
4. Outputs confidence levels, not just hard classifications
5. Provides reasoning trace for debugging

The key insight: market regime is rarely clear-cut. AGoT handles ambiguity by
maintaining multiple interpretations and selecting the highest-confidence one,
while keeping alternatives available for rapid re-classification.

Usage:
    from intelligence.adaptive_regime import AdaptiveRegimeClassifier
    
    classifier = AdaptiveRegimeClassifier()
    result = classifier.classify(index_data, vix_data, stock_universe_data)
    
    # result includes:
    #   - primary regime with confidence
    #   - alternative hypotheses ranked
    #   - reasoning trace
    #   - evidence breakdown
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from signals.regime import Regime, RegimeSnapshot, _trend_score, _adx, _vix_rank, breadth_pct_above_50dma
from intelligence.thought_graph import ThoughtGraph, ThoughtNode, Evidence, ThoughtStatus


@dataclass
class AdaptiveRegimeResult:
    """Result from the AGoT-enhanced regime classifier."""
    primary: Regime
    primary_confidence: float
    alternatives: list[dict]           # [{regime, confidence, reason}]
    snapshot: RegimeSnapshot           # Compatible with existing system
    graph_trace: dict                  # Full reasoning trace
    ambiguity_score: float             # 0=clear, 1=highly ambiguous
    evidence_breakdown: dict[str, float]  # regime -> total evidence score

    def to_dict(self) -> dict:
        d = self.snapshot.to_dict()
        d["agot"] = {
            "primary_confidence": round(self.primary_confidence, 3),
            "alternatives": self.alternatives,
            "ambiguity_score": round(self.ambiguity_score, 3),
            "evidence_breakdown": {k: round(v, 3) for k, v in self.evidence_breakdown.items()},
        }
        return d


class AdaptiveRegimeClassifier:
    """
    AGoT-powered regime classifier that evaluates all 6 regime hypotheses
    simultaneously and selects the highest-confidence one.
    
    Unlike the static rule stack in signals/regime.py, this classifier:
    - Evaluates ALL regimes in parallel
    - Assigns confidence scores based on evidence strength
    - Handles ambiguous conditions gracefully
    - Provides full reasoning trace
    """

    # Evidence weights for each indicator per regime
    # Higher = more important for that regime
    EVIDENCE_WEIGHTS = {
        Regime.TREND_UP: {
            "trend_score": 3.0,
            "adx": 2.5,
            "breadth": 2.0,
            "vix": 1.0,
            "ema_alignment": 2.0,
            "heavyweight_momentum": 2.5,
            "ai_sentiment": 2.0,
        },
        Regime.TREND_DOWN: {
            "trend_score": 3.0,
            "adx": 2.5,
            "breadth": 2.0,
            "vix": 1.0,
            "ema_alignment": 2.0,
            "heavyweight_momentum": 2.5,
            "ai_sentiment": 2.0,
        },
        Regime.RANGE_LOW_VOL: {
            "adx": 2.0,
            "vix": 2.5,
            "trend_score": 1.5,
            "breadth": 1.0,
            "volatility": 2.0,
            "heavyweight_momentum": 1.5,
            "ai_sentiment": 1.0,
        },
        Regime.RANGE_HIGH_VOL: {
            "adx": 1.5,
            "vix": 2.0,
            "trend_score": 1.0,
            "breadth": 1.5,
            "volatility": 2.0,
            "heavyweight_momentum": 1.5,
            "ai_sentiment": 1.5,
        },
        Regime.VOL_EXPANSION: {
            "vix_change": 3.0,
            "vix_level": 2.0,
            "breadth": 1.0,
            "trend_score": 0.5,
            "heavyweight_momentum": 3.0,
            "ai_sentiment": 2.5,
        },
        Regime.VOL_CONTRACTION: {
            "vix_change": 2.5,
            "vix_level": 2.0,
            "adx": 1.5,
            "breadth": 0.5,
            "heavyweight_momentum": 1.5,
            "ai_sentiment": 1.0,
        },
    }

    def __init__(self, min_confidence: float = 0.35):
        self.min_confidence = min_confidence

    def classify(
        self,
        index_symbol: str = "NIFTY",
        stock_universe_data: dict | None = None,
        idx_df: pd.DataFrame | None = None,
        vix_df: pd.DataFrame | None = None,
    ) -> AdaptiveRegimeResult:
        """
        Classify market regime using AGoT multi-hypothesis evaluation.
        
        Returns both the primary regime AND ranked alternatives with confidences.
        """
        t0 = time.time()

        # Fetch data if not provided
        if idx_df is None:
            from data import feed
            idx_df = feed.ohlc_cached(index_symbol, period="2y")
        if vix_df is None:
            from data import feed
            vix_df = feed.ohlc_cached("INDIAVIX", period="3mo")

        # Handle missing data
        if idx_df is None or idx_df.empty or vix_df is None or vix_df.empty:
            return self._fallback_result("data unavailable")

        idx_df = idx_df.dropna(subset=["close"])
        vix_df = vix_df.dropna(subset=["close"])

        # Compute indicators
        indicators = self._compute_indicators(idx_df, vix_df, stock_universe_data, index_symbol=index_symbol)

        # Build thought graph with all regime hypotheses
        graph = ThoughtGraph("adaptive_regime")

        # Root observation node
        obs = graph.add_thought(
            thought_type="observation",
            label="Market State Observation",
            confidence=0.95,
            metadata={
                "trend_score": indicators["trend_score"],
                "adx": indicators["adx"],
                "vix": indicators["vix_now"],
                "breadth": indicators["breadth"],
            },
        )

        # Create hypothesis branches for ALL 6 regimes
        regime_nodes: dict[Regime, ThoughtNode] = {}
        for regime in Regime:
            node = graph.branch_from(
                parent=obs,
                label=f"Hypothesis: {regime.value}",
                branch_id=f"regime_{regime.value}",
                confidence=0.5,  # Start neutral
            )
            regime_nodes[regime] = node

        # Score each hypothesis with evidence
        evidence_scores = {}
        for regime, node in regime_nodes.items():
            score = self._evaluate_hypothesis(regime, node, indicators, graph)
            evidence_scores[regime.value] = score

        # Select best hypothesis
        best = graph.select_best(min_confidence=self.min_confidence)

        # Get alternatives (top 3)
        top_k = graph.select_top_k(k=3, min_confidence=0.0)

        # Build alternatives list
        alternatives = []
        for node in top_k:
            regime_name = node.label.replace("Hypothesis: ", "")
            if regime_name != (best.label.replace("Hypothesis: ", "") if best else ""):
                alternatives.append({
                    "regime": regime_name,
                    "confidence": round(node.confidence, 3),
                    "evidence_count": len(node.evidence),
                })

        # Determine primary regime
        if best:
            primary_name = best.label.replace("Hypothesis: ", "")
            try:
                primary_regime = Regime(primary_name)
            except ValueError:
                primary_regime = Regime.RANGE_LOW_VOL
            primary_conf = best.confidence
        else:
            primary_regime = Regime.RANGE_LOW_VOL
            primary_conf = 0.3

        # Calculate ambiguity (1 - gap between top 2)
        if len(top_k) >= 2:
            gap = top_k[0].confidence - top_k[1].confidence
            ambiguity = max(0.0, 1.0 - (gap * 5))  # Scale: 0.2 gap = 0 ambiguity
        else:
            ambiguity = 0.0

        # Build traditional RegimeSnapshot for backward compatibility
        snapshot = self._build_snapshot(primary_regime, indicators, primary_conf)

        elapsed_ms = (time.time() - t0) * 1000

        return AdaptiveRegimeResult(
            primary=primary_regime,
            primary_confidence=primary_conf,
            alternatives=alternatives,
            snapshot=snapshot,
            graph_trace=graph.get_reasoning_trace(),
            ambiguity_score=ambiguity,
            evidence_breakdown=evidence_scores,
        )

    def _compute_indicators(
        self,
        idx_df: pd.DataFrame,
        vix_df: pd.DataFrame,
        stock_universe_data: dict | None,
        index_symbol: str = "NIFTY",
    ) -> dict[str, Any]:
        """Compute all market indicators needed for regime evaluation."""
        trend = _trend_score(idx_df["close"])
        adx_val = _adx(idx_df)
        breadth = breadth_pct_above_50dma(stock_universe_data or {})

        vix_now = float(vix_df["close"].iloc[-1])
        vix_5d_ago = float(vix_df["close"].iloc[-6]) if len(vix_df) >= 6 else vix_now
        vix_chg = 100 * (vix_now - vix_5d_ago) / vix_5d_ago if vix_5d_ago else 0.0

        # EMA alignment check
        close = idx_df["close"]
        ema20 = close.ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = close.ewm(span=50, adjust=False).mean().iloc[-1]
        ema200 = close.ewm(span=200, adjust=False).mean().iloc[-1]
        px = float(close.iloc[-1])

        bullish_alignment = px > ema20 > ema50 > ema200
        bearish_alignment = px < ema20 < ema50 < ema200

        # Volatility: 20-day realized vol
        returns = close.pct_change().dropna()
        realized_vol = float(returns.tail(20).std() * np.sqrt(252) * 100) if len(returns) >= 20 else 0.0

        vix_rank_val = _vix_rank(vix_df["close"])

        # Dynamic/intraday/qualitative enhancements
        heavyweight_momentum = 0.0
        try:
            from signals.index_weightage import calculate_weighted_momentum
            mom_data = calculate_weighted_momentum(index_symbol)
            heavyweight_momentum = mom_data.get("weighted_momentum") or 0.0
        except Exception:
            pass

        ai_influence = 0.0
        ai_overall = "NEUTRAL"
        try:
            from data import ai_scraper
            ai_sentiment = ai_scraper.get_market_news_sentiment()
            if isinstance(ai_sentiment, dict):
                ai_overall = str(ai_sentiment.get("overall_market_sentiment") or "NEUTRAL").upper()
                ai_conf_lbl = str(ai_sentiment.get("confidence") or "LOW").upper()
                ai_score_raw = float(ai_sentiment.get("sentiment_score") or 0.0)
            elif isinstance(ai_sentiment, str):
                s = ai_sentiment.strip().upper()
                if s in ("BULLISH", "POSITIVE", "LONG"):
                    ai_overall = "BULLISH"
                elif s in ("BEARISH", "NEGATIVE", "SHORT"):
                    ai_overall = "BEARISH"
                ai_conf_lbl = "LOW"
                ai_score_raw = 0.0
            
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
            
            ai_weight = {"HIGH": 1.0, "MEDIUM": 0.5, "LOW": 0.2}.get(ai_conf_lbl, 0.0)
            ai_influence = ai_dir * ai_weight
        except Exception:
            pass

        return {
            "trend_score": trend,
            "adx": adx_val,
            "vix_now": vix_now,
            "vix_5d_change": vix_chg,
            "breadth": breadth,
            "breadth_val": 50.0 if breadth is None else float(breadth),
            "bullish_alignment": bullish_alignment,
            "bearish_alignment": bearish_alignment,
            "realized_vol": realized_vol,
            "vix_rank": vix_rank_val,
            "price": px,
            "ema20": float(ema20),
            "ema50": float(ema50),
            "ema200": float(ema200),
            "heavyweight_momentum": heavyweight_momentum,
            "ai_influence": ai_influence,
            "ai_overall": ai_overall,
        }

    def _evaluate_hypothesis(
        self,
        regime: Regime,
        node: ThoughtNode,
        indicators: dict,
        graph: ThoughtGraph,
    ) -> float:
        """
        Evaluate a single regime hypothesis by adding evidence.
        Returns total evidence score for the hypothesis.
        """
        weights = self.EVIDENCE_WEIGHTS[regime]
        total_score = 0.0

        # --- Trend Score Evidence ---
        if "trend_score" in weights:
            ts = indicators["trend_score"]
            score = self._score_trend_for_regime(regime, ts)
            if score != 0:
                node.add_evidence(Evidence(
                    source="trend_score",
                    value=ts,
                    supports=score > 0,
                    weight=weights["trend_score"] * abs(score),
                ))
                total_score += score * weights["trend_score"]

        # --- ADX Evidence ---
        if "adx" in weights:
            adx_val = indicators["adx"]
            score = self._score_adx_for_regime(regime, adx_val)
            if score != 0:
                node.add_evidence(Evidence(
                    source="ADX",
                    value=round(adx_val, 2),
                    supports=score > 0,
                    weight=weights["adx"] * abs(score),
                ))
                total_score += score * weights["adx"]

        # --- VIX Level Evidence ---
        if "vix_level" in weights:
            vix = indicators["vix_now"]
            score = self._score_vix_level_for_regime(regime, vix)
            if score != 0:
                node.add_evidence(Evidence(
                    source="VIX_level",
                    value=round(vix, 2),
                    supports=score > 0,
                    weight=weights["vix_level"] * abs(score),
                ))
                total_score += score * weights["vix_level"]

        # --- VIX Change Evidence ---
        if "vix_change" in weights:
            vix_chg = indicators["vix_5d_change"]
            score = self._score_vix_change_for_regime(regime, vix_chg)
            if score != 0:
                node.add_evidence(Evidence(
                    source="VIX_5d_change",
                    value=round(vix_chg, 2),
                    supports=score > 0,
                    weight=weights["vix_change"] * abs(score),
                ))
                total_score += score * weights["vix_change"]

        # --- Breadth Evidence ---
        if "breadth" in weights:
            breadth = indicators["breadth_val"]
            score = self._score_breadth_for_regime(regime, breadth)
            if score != 0:
                node.add_evidence(Evidence(
                    source="breadth_pct_above_50dma",
                    value=round(breadth, 1),
                    supports=score > 0,
                    weight=weights["breadth"] * abs(score),
                ))
                total_score += score * weights["breadth"]

        # --- EMA Alignment Evidence ---
        if "ema_alignment" in weights:
            bull = indicators["bullish_alignment"]
            bear = indicators["bearish_alignment"]
            score = self._score_ema_alignment(regime, bull, bear)
            if score != 0:
                node.add_evidence(Evidence(
                    source="EMA_alignment",
                    value="bullish" if bull else ("bearish" if bear else "mixed"),
                    supports=score > 0,
                    weight=weights["ema_alignment"] * abs(score),
                ))
                total_score += score * weights["ema_alignment"]

        # --- Volatility Evidence ---
        if "volatility" in weights:
            rvol = indicators["realized_vol"]
            score = self._score_volatility_for_regime(regime, rvol)
            if score != 0:
                node.add_evidence(Evidence(
                    source="realized_volatility",
                    value=round(rvol, 2),
                    supports=score > 0,
                    weight=weights["volatility"] * abs(score),
                ))
                total_score += score * weights["volatility"]

        # --- Heavyweight Momentum Evidence ---
        if "heavyweight_momentum" in weights:
            mom = indicators["heavyweight_momentum"]
            score = self._score_heavyweight_momentum_for_regime(regime, mom)
            if score != 0:
                node.add_evidence(Evidence(
                    source="heavyweight_momentum",
                    value=round(mom, 2),
                    supports=score > 0,
                    weight=weights["heavyweight_momentum"] * abs(score),
                ))
                total_score += score * weights["heavyweight_momentum"]

        # --- AI Sentiment Evidence ---
        if "ai_sentiment" in weights:
            ai_infl = indicators["ai_influence"]
            score = self._score_ai_sentiment_for_regime(regime, ai_infl)
            if score != 0:
                node.add_evidence(Evidence(
                    source="ai_sentiment",
                    value=round(ai_infl, 2),
                    supports=score > 0,
                    weight=weights["ai_sentiment"] * abs(score),
                ))
                total_score += score * weights["ai_sentiment"]

        return total_score

    def _score_heavyweight_momentum_for_regime(self, regime: Regime, mom: float) -> float:
        """How well does index heavyweight momentum support this regime?"""
        if regime == Regime.TREND_UP:
            if mom >= 1.5: return 1.0
            if mom >= 0.5: return 0.5
            if mom <= -1.5: return -1.0
            if mom <= -0.5: return -0.5
        elif regime == Regime.TREND_DOWN:
            if mom <= -1.5: return 1.0
            if mom <= -0.5: return 0.5
            if mom >= 1.5: return -1.0
            if mom >= 0.5: return -0.5
        elif regime == Regime.VOL_EXPANSION:
            if abs(mom) >= 1.5: return 1.0
            if abs(mom) >= 0.75: return 0.5
        elif regime in (Regime.RANGE_LOW_VOL, Regime.VOL_CONTRACTION):
            if abs(mom) >= 1.0: return -0.8
            if abs(mom) < 0.3: return 0.6
        elif regime == Regime.RANGE_HIGH_VOL:
            if abs(mom) >= 1.0: return 0.6
        return 0.0

    def _score_ai_sentiment_for_regime(self, regime: Regime, ai_infl: float) -> float:
        """How well does AI news sentiment support this regime?"""
        if regime == Regime.TREND_UP:
            if ai_infl >= 0.8: return 1.0
            if ai_infl >= 0.4: return 0.5
            if ai_infl <= -0.8: return -1.0
            if ai_infl <= -0.4: return -0.5
        elif regime == Regime.TREND_DOWN:
            if ai_infl <= -0.8: return 1.0
            if ai_infl <= -0.4: return 0.5
            if ai_infl >= 0.8: return -1.0
            if ai_infl >= 0.4: return -0.5
        elif regime == Regime.VOL_EXPANSION:
            if abs(ai_infl) >= 0.8: return 0.8
        elif regime in (Regime.RANGE_LOW_VOL, Regime.VOL_CONTRACTION):
            if abs(ai_infl) >= 0.6: return -0.6
            if abs(ai_infl) < 0.2: return 0.5
        elif regime == Regime.RANGE_HIGH_VOL:
            if abs(ai_infl) >= 0.5: return 0.5
        return 0.0

    # --- Scoring functions: return -1.0 to +1.0 ---

    def _score_trend_for_regime(self, regime: Regime, trend: int) -> float:
        """How well does trend score support this regime?"""
        if regime == Regime.TREND_UP:
            if trend >= 4: return 1.0
            if trend >= 3: return 0.6
            if trend >= 2: return 0.2
            return -0.5 if trend <= 0 else 0.0
        elif regime == Regime.TREND_DOWN:
            if trend <= -4: return 1.0
            if trend <= -3: return 0.6
            if trend <= -2: return 0.2
            return -0.5 if trend >= 0 else 0.0
        elif regime in (Regime.RANGE_LOW_VOL, Regime.RANGE_HIGH_VOL):
            if -2 <= trend <= 2: return 0.6
            return -0.3
        elif regime == Regime.VOL_EXPANSION:
            return 0.0  # Trend-neutral
        elif regime == Regime.VOL_CONTRACTION:
            if -1 <= trend <= 1: return 0.4
            return -0.2
        return 0.0

    def _score_adx_for_regime(self, regime: Regime, adx: float) -> float:
        """How well does ADX support this regime?"""
        if regime in (Regime.TREND_UP, Regime.TREND_DOWN):
            if adx >= 25: return 1.0
            if adx >= 20: return 0.5
            return -0.5  # Low ADX contradicts trend
        elif regime in (Regime.RANGE_LOW_VOL, Regime.RANGE_HIGH_VOL):
            if adx < 20: return 0.7
            if adx < 25: return 0.3
            return -0.3
        return 0.0

    def _score_vix_level_for_regime(self, regime: Regime, vix: float) -> float:
        """How well does VIX level support this regime?"""
        if regime == Regime.RANGE_LOW_VOL:
            if vix < 14: return 1.0
            if vix < 16: return 0.4
            return -0.5
        elif regime == Regime.RANGE_HIGH_VOL:
            if vix >= 16: return 0.8
            if vix >= 14: return 0.3
            return -0.4
        elif regime == Regime.VOL_EXPANSION:
            if vix > 18: return 0.8
            if vix > 16: return 0.4
            return -0.3
        elif regime == Regime.VOL_CONTRACTION:
            if vix < 14: return 0.8
            if vix < 16: return 0.4
            return -0.5
        return 0.0

    def _score_vix_change_for_regime(self, regime: Regime, vix_chg: float) -> float:
        """How well does VIX 5d change support this regime?"""
        if regime == Regime.VOL_EXPANSION:
            if vix_chg > 35: return 1.0
            if vix_chg > 25: return 0.7
            if vix_chg > 15: return 0.3
            return -0.5
        elif regime == Regime.VOL_CONTRACTION:
            if vix_chg < -20: return 1.0
            if vix_chg < -10: return 0.5
            return -0.3 if vix_chg > 10 else 0.0
        elif regime in (Regime.RANGE_LOW_VOL, Regime.TREND_UP):
            if vix_chg < -5: return 0.3
            if vix_chg > 20: return -0.5
            return 0.0
        return 0.0

    def _score_breadth_for_regime(self, regime: Regime, breadth: float) -> float:
        """How well does market breadth support this regime?"""
        if regime == Regime.TREND_UP:
            if breadth >= 60: return 1.0
            if breadth >= 50: return 0.5
            if breadth >= 40: return 0.0
            return -0.7  # Weak breadth contradicts trend up
        elif regime == Regime.TREND_DOWN:
            if breadth <= 40: return 1.0
            if breadth <= 50: return 0.5
            if breadth >= 60: return -0.5
            return 0.0
        elif regime in (Regime.RANGE_LOW_VOL, Regime.RANGE_HIGH_VOL):
            if 40 <= breadth <= 60: return 0.5
            return -0.2  # Extreme breadth suggests trend, not range
        return 0.0

    def _score_ema_alignment(self, regime: Regime, bull: bool, bear: bool) -> float:
        """How well does EMA alignment support this regime?"""
        if regime == Regime.TREND_UP:
            if bull: return 1.0
            if bear: return -0.8
            return -0.2
        elif regime == Regime.TREND_DOWN:
            if bear: return 1.0
            if bull: return -0.8
            return -0.2
        elif regime in (Regime.RANGE_LOW_VOL, Regime.RANGE_HIGH_VOL):
            if not bull and not bear: return 0.5  # Mixed = range
            return -0.2
        return 0.0

    def _score_volatility_for_regime(self, regime: Regime, rvol: float) -> float:
        """How well does realized volatility support this regime?"""
        if regime == Regime.RANGE_LOW_VOL:
            if rvol < 12: return 0.8
            if rvol < 16: return 0.3
            return -0.3
        elif regime == Regime.RANGE_HIGH_VOL:
            if rvol >= 16: return 0.6
            if rvol >= 12: return 0.2
            return -0.2
        elif regime == Regime.VOL_EXPANSION:
            if rvol > 20: return 0.7
            return 0.0
        elif regime == Regime.VOL_CONTRACTION:
            if rvol < 10: return 0.7
            if rvol < 14: return 0.3
            return -0.2
        return 0.0

    def _build_snapshot(
        self,
        regime: Regime,
        indicators: dict,
        confidence: float,
    ) -> RegimeSnapshot:
        """Build a backward-compatible RegimeSnapshot."""
        notes_parts = [f"AGoT confidence: {confidence:.1%}"]

        breadth_val = indicators.get("breadth")
        if indicators.get("bullish_alignment"):
            notes_parts.append("EMA bullish alignment")
        elif indicators.get("bearish_alignment"):
            notes_parts.append("EMA bearish alignment")

        return RegimeSnapshot(
            regime=regime,
            trend_score=indicators["trend_score"],
            vix=round(indicators["vix_now"], 2),
            vix_5d_change_pct=round(indicators["vix_5d_change"], 2),
            vix_rank=indicators.get("vix_rank"),
            adx=round(indicators["adx"], 2),
            breadth_pct_above_50dma=breadth_val,
            notes="; ".join(notes_parts),
        )

    def _fallback_result(self, reason: str) -> AdaptiveRegimeResult:
        """Return a safe fallback when data is unavailable."""
        snapshot = RegimeSnapshot(
            regime=Regime.RANGE_LOW_VOL,
            trend_score=0,
            vix=0.0,
            vix_5d_change_pct=0.0,
            vix_rank=None,
            adx=0.0,
            breadth_pct_above_50dma=None,
            notes=f"AGoT fallback: {reason}",
        )
        return AdaptiveRegimeResult(
            primary=Regime.RANGE_LOW_VOL,
            primary_confidence=0.3,
            alternatives=[],
            snapshot=snapshot,
            graph_trace={"error": reason},
            ambiguity_score=1.0,
            evidence_breakdown={},
        )


# --- Integration with existing system ---

def classify_adaptive(
    index_symbol: str = "NIFTY",
    stock_universe_data: dict | None = None,
) -> AdaptiveRegimeResult:
    """
    Drop-in replacement for signals.regime.classify() that uses AGoT.
    
    Returns AdaptiveRegimeResult which contains a .snapshot field
    compatible with the existing RegimeSnapshot dataclass.
    """
    classifier = AdaptiveRegimeClassifier()
    return classifier.classify(
        index_symbol=index_symbol,
        stock_universe_data=stock_universe_data,
    )
