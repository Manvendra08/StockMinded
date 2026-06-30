"""
Signal Ensemble using AGoT
===========================

Aggregates multiple signal sources (regime, flows, leadership, structure)
into a unified decision using graph-based reasoning.

Instead of a fixed pipeline (Regime → Flows → Leadership → Structure),
this module treats each signal as a "voter" in a thought graph and
produces a confidence-weighted ensemble decision.

Key features:
1. Each signal contributes evidence to hypothesis branches
2. Conflicting signals are flagged as "disagreement" 
3. Consensus signals get boosted confidence
4. The output includes a confidence score and reasoning trace

Usage:
    from intelligence.signal_ensemble import SignalEnsemble
    
    ensemble = SignalEnsemble()
    result = ensemble.evaluate(
        regime_result=regime_result,
        flow_result=flow_snapshot,
        leaders=long_stocks,
        laggards=short_stocks,
    )
    
    # result includes:
    #   - overall_bias (LONG | SHORT | NEUTRAL)
    #   - confidence (0.0 to 1.0)
    #   - agreement_score (how much signals agree)
    #   - action_recommendation
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from intelligence.thought_graph import ThoughtGraph, ThoughtNode, Evidence, ThoughtStatus
from signals.regime import Regime


@dataclass
class EnsembleResult:
    """Result from the signal ensemble evaluation."""
    overall_bias: str                  # LONG | SHORT | NEUTRAL
    confidence: float                  # 0.0 to 1.0
    agreement_score: float             # 0.0 (disagree) to 1.0 (full consensus)
    signal_contributions: dict[str, dict]  # signal -> {bias, confidence, weight}
    action_recommendation: str         # Human-readable recommendation
    risk_adjustment: str               # "NORMAL" | "REDUCE_SIZE" | "SKIP"
    reasoning_trace: dict              # Full AGoT trace
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "overall_bias": self.overall_bias,
            "confidence": round(self.confidence, 3),
            "agreement_score": round(self.agreement_score, 3),
            "signal_contributions": self.signal_contributions,
            "action_recommendation": self.action_recommendation,
            "risk_adjustment": self.risk_adjustment,
            "timestamp": self.timestamp,
        }


class SignalEnsemble:
    """
    AGoT-based signal ensemble that aggregates multiple market signals
    into a unified decision with confidence scoring.
    """

    # Signal weights (higher = more influence on final decision)
    SIGNAL_WEIGHTS = {
        "regime": 2.5,      # Market regime sets the tone
        "flow_bias": 2.0,   # Smart money direction
        "leadership": 1.5,  # Stock selection quality
        "structure": 1.0,   # Trade structure fit
        "vix": 1.5,         # Volatility filter
        "breadth": 1.0,     # Market internals
    }

    # Regime-to-bias mapping
    REGIME_BIAS = {
        Regime.TREND_UP: "LONG",
        Regime.TREND_DOWN: "SHORT",
        Regime.RANGE_LOW_VOL: "NEUTRAL",
        Regime.RANGE_HIGH_VOL: "NEUTRAL",
        Regime.VOL_EXPANSION: "NEUTRAL",
        Regime.VOL_CONTRACTION: "NEUTRAL",
    }

    def __init__(self, min_agreement: float = 0.5):
        """
        Args:
            min_agreement: Minimum agreement score to proceed with a trade.
                          Below this, risk_adjustment becomes "REDUCE_SIZE" or "SKIP".
        """
        self.min_agreement = min_agreement

    def evaluate(
        self,
        regime_result: Any = None,
        flow_snapshot: Any = None,
        leaders: list = None,
        laggards: list = None,
        structure_plan: Any = None,
        vix: float = None,
        breadth: float = None,
    ) -> EnsembleResult:
        """
        Evaluate all signals and produce an ensemble decision.
        
        Each signal is treated as a "voter" that contributes evidence
        for LONG, SHORT, or NEUTRAL hypotheses.
        """
        graph = ThoughtGraph("signal_ensemble")

        # Root node: market assessment request
        root = graph.add_thought(
            thought_type="decision",
            label="Market Direction Assessment",
            confidence=0.5,
        )

        # Create three hypothesis branches
        long_node = graph.branch_from(root, "LONG Bias", branch_id="bias_LONG", confidence=0.5)
        short_node = graph.branch_from(root, "SHORT Bias", branch_id="bias_SHORT", confidence=0.5)
        neutral_node = graph.branch_from(root, "NEUTRAL", branch_id="bias_NEUTRAL", confidence=0.5)

        contributions = {}
        signal_biases = []

        # --- 1. Regime Signal ---
        if regime_result is not None:
            regime_bias, regime_conf = self._evaluate_regime(regime_result)
            contributions["regime"] = {
                "bias": regime_bias,
                "confidence": regime_conf,
                "weight": self.SIGNAL_WEIGHTS["regime"],
            }
            signal_biases.append(regime_bias)
            self._add_bias_evidence(graph, long_node, short_node, neutral_node,
                                     regime_bias, regime_conf, self.SIGNAL_WEIGHTS["regime"])

        # --- 2. Flow Bias Signal ---
        if flow_snapshot is not None:
            flow_bias, flow_conf = self._evaluate_flows(flow_snapshot)
            contributions["flow_bias"] = {
                "bias": flow_bias,
                "confidence": flow_conf,
                "weight": self.SIGNAL_WEIGHTS["flow_bias"],
            }
            signal_biases.append(flow_bias)
            self._add_bias_evidence(graph, long_node, short_node, neutral_node,
                                     flow_bias, flow_conf, self.SIGNAL_WEIGHTS["flow_bias"])

        # --- 3. Leadership Signal ---
        if leaders is not None and laggards is not None:
            lead_bias, lead_conf = self._evaluate_leadership(leaders, laggards)
            contributions["leadership"] = {
                "bias": lead_bias,
                "confidence": lead_conf,
                "weight": self.SIGNAL_WEIGHTS["leadership"],
            }
            signal_biases.append(lead_bias)
            self._add_bias_evidence(graph, long_node, short_node, neutral_node,
                                     lead_bias, lead_conf, self.SIGNAL_WEIGHTS["leadership"])

        # --- 4. VIX Signal ---
        if vix is not None:
            vix_bias, vix_conf = self._evaluate_vix(vix)
            contributions["vix"] = {
                "bias": vix_bias,
                "confidence": vix_conf,
                "weight": self.SIGNAL_WEIGHTS["vix"],
            }
            signal_biases.append(vix_bias)
            self._add_bias_evidence(graph, long_node, short_node, neutral_node,
                                     vix_bias, vix_conf, self.SIGNAL_WEIGHTS["vix"])

        # --- 5. Breadth Signal ---
        if breadth is not None:
            breadth_bias, breadth_conf = self._evaluate_breadth(breadth)
            contributions["breadth"] = {
                "bias": breadth_bias,
                "confidence": breadth_conf,
                "weight": self.SIGNAL_WEIGHTS["breadth"],
            }
            signal_biases.append(breadth_bias)
            self._add_bias_evidence(graph, long_node, short_node, neutral_node,
                                     breadth_bias, breadth_conf, self.SIGNAL_WEIGHTS["breadth"])

        # Select best hypothesis
        best = graph.select_best(min_confidence=0.2)

        # Determine overall bias
        if best:
            overall_bias = best.branch_id.replace("bias_", "") if best.branch_id else "NEUTRAL"
            confidence = best.confidence
        else:
            overall_bias = "NEUTRAL"
            confidence = 0.3

        # Calculate agreement score
        agreement = self._calculate_agreement(signal_biases, contributions)

        # Determine risk adjustment
        risk_adj = self._determine_risk_adjustment(agreement, confidence)

        # Build recommendation
        recommendation = self._build_recommendation(
            overall_bias, confidence, agreement, contributions
        )

        return EnsembleResult(
            overall_bias=overall_bias,
            confidence=confidence,
            agreement_score=agreement,
            signal_contributions=contributions,
            action_recommendation=recommendation,
            risk_adjustment=risk_adj,
            reasoning_trace=graph.get_reasoning_trace(),
        )

    def _evaluate_regime(self, regime_result: Any) -> tuple[str, float]:
        """Extract bias and confidence from regime result."""
        # Handle both AdaptiveRegimeResult and RegimeSnapshot
        if hasattr(regime_result, "primary"):
            regime = regime_result.primary
            conf = getattr(regime_result, "primary_confidence", 0.6)
        elif hasattr(regime_result, "regime"):
            regime = regime_result.regime
            conf = 0.6
        else:
            return "NEUTRAL", 0.3

        bias = self.REGIME_BIAS.get(regime, "NEUTRAL")
        return bias, conf

    def _evaluate_flows(self, flow_snapshot: Any) -> tuple[str, float]:
        """Extract bias and confidence from flow snapshot."""
        bias = getattr(flow_snapshot, "smart_money_bias", "NEUTRAL")
        
        # Confidence based on data freshness
        stale_count = 0
        if getattr(flow_snapshot, "pcr_stale", False): stale_count += 1
        if getattr(flow_snapshot, "fii_dii_stale", False): stale_count += 1
        if getattr(flow_snapshot, "fii_derivatives_stale", False): stale_count += 1
        
        if stale_count == 0:
            conf = 0.75
        elif stale_count == 1:
            conf = 0.55
        else:
            conf = 0.35

        return bias, conf

    def _evaluate_leadership(self, leaders: list, laggards: list) -> tuple[str, float]:
        """Extract bias from leadership quality."""
        n_leaders = len(leaders) if leaders else 0
        n_laggards = len(laggards) if laggards else 0

        if n_leaders == 0 and n_laggards == 0:
            return "NEUTRAL", 0.3

        # Quality check: Q4/Q5 leaders are high conviction
        high_q_leaders = sum(1 for l in (leaders or []) if getattr(l, "quintile", 0) >= 4)
        high_q_laggards = sum(1 for l in (laggards or []) if getattr(l, "quintile", 0) >= 4)

        if high_q_leaders > high_q_laggards and n_leaders >= 3:
            bias = "LONG"
            conf = min(0.8, 0.5 + (high_q_leaders * 0.1))
        elif high_q_laggards > high_q_leaders and n_laggards >= 3:
            bias = "SHORT"
            conf = min(0.8, 0.5 + (high_q_laggards * 0.1))
        else:
            bias = "NEUTRAL"
            conf = 0.4

        return bias, conf

    def _evaluate_vix(self, vix: float) -> tuple[str, float]:
        """VIX as a risk filter."""
        if vix < 14:
            return "LONG", 0.6   # Low VIX = risk-on
        elif vix < 18:
            return "NEUTRAL", 0.5
        elif vix < 22:
            return "SHORT", 0.5  # Elevated VIX = caution
        else:
            return "SHORT", 0.7  # High VIX = risk-off

    def _evaluate_breadth(self, breadth: float) -> tuple[str, float]:
        """Market breadth as internal confirmation."""
        if breadth >= 60:
            return "LONG", 0.7
        elif breadth >= 50:
            return "LONG", 0.4
        elif breadth >= 40:
            return "NEUTRAL", 0.4
        elif breadth >= 30:
            return "SHORT", 0.5
        else:
            return "SHORT", 0.7

    def _add_bias_evidence(
        self,
        graph: ThoughtGraph,
        long_node: ThoughtNode,
        short_node: ThoughtNode,
        neutral_node: ThoughtNode,
        bias: str,
        confidence: float,
        weight: float,
    ) -> None:
        """Add evidence to the appropriate bias node."""
        ev = Evidence(
            source=f"signal_{bias}",
            value=bias,
            supports=True,
            weight=weight * confidence,
        )

        if bias == "LONG":
            long_node.add_evidence(ev)
            # Add contradicting evidence to SHORT
            short_node.add_evidence(Evidence(
                source=f"signal_{bias}", value=bias, supports=False, weight=weight * confidence * 0.3,
            ))
        elif bias == "SHORT":
            short_node.add_evidence(ev)
            long_node.add_evidence(Evidence(
                source=f"signal_{bias}", value=bias, supports=False, weight=weight * confidence * 0.3,
            ))
        else:
            neutral_node.add_evidence(ev)

    def _calculate_agreement(
        self,
        biases: list[str],
        contributions: dict[str, dict],
    ) -> float:
        """
        Calculate how much signals agree with each other.
        
        1.0 = all signals agree
        0.0 = signals are evenly split
        """
        if not biases:
            return 0.5

        long_count = biases.count("LONG")
        short_count = biases.count("SHORT")
        neutral_count = biases.count("NEUTRAL")
        total = len(biases)

        if total == 0:
            return 0.5

        # Weighted agreement: majority direction / total
        max_count = max(long_count, short_count, neutral_count)
        raw_agreement = max_count / total

        # Boost if high-confidence signals agree
        weighted_agreement = 0.0
        total_weight = 0.0
        for sig_name, contrib in contributions.items():
            if contrib["bias"] == (
                "LONG" if long_count >= short_count and long_count >= neutral_count
                else "SHORT" if short_count >= neutral_count
                else "NEUTRAL"
            ):
                weighted_agreement += contrib["confidence"] * contrib["weight"]
            total_weight += contrib["weight"]

        if total_weight > 0:
            weighted_ratio = weighted_agreement / total_weight
        else:
            weighted_ratio = 0.5

        # Blend raw and weighted agreement
        return round(0.4 * raw_agreement + 0.6 * weighted_ratio, 3)

    def _determine_risk_adjustment(self, agreement: float, confidence: float) -> str:
        """Determine risk adjustment based on agreement and confidence."""
        if agreement >= 0.7 and confidence >= 0.6:
            return "NORMAL"
        elif agreement >= 0.5 and confidence >= 0.45:
            return "REDUCE_SIZE"
        else:
            return "SKIP"

    def _build_recommendation(
        self,
        bias: str,
        confidence: float,
        agreement: float,
        contributions: dict,
    ) -> str:
        """Build a human-readable recommendation."""
        parts = []

        if bias == "NEUTRAL":
            parts.append("⏸️ NEUTRAL — No clear directional bias.")
        elif confidence < 0.45:
            parts.append(f"⚠️ {bias} bias detected but LOW CONFIDENCE ({confidence:.0%}).")
        else:
            parts.append(f"{'📈' if bias == 'LONG' else '📉'} {bias} bias ({confidence:.0%} confidence).")

        if agreement < 0.5:
            parts.append("Signals disagree — reduce position size or skip.")
        elif agreement >= 0.75:
            parts.append("Strong signal consensus — full size allowed.")

        # Highlight key signal contributions
        key_signals = sorted(
            contributions.items(),
            key=lambda x: x[1]["confidence"] * x[1]["weight"],
            reverse=True,
        )[:3]
        if key_signals:
            sig_strs = [f"{name}={c['bias']}({c['confidence']:.0%})" for name, c in key_signals]
            parts.append(f"Key signals: {', '.join(sig_strs)}")

        return " | ".join(parts)


# --- Convenience function ---

def evaluate_signals(
    regime_result: Any = None,
    flow_snapshot: Any = None,
    leaders: list = None,
    laggards: list = None,
    vix: float = None,
    breadth: float = None,
) -> EnsembleResult:
    """
    Convenience function to evaluate all signals using AGoT ensemble.
    """
    ensemble = SignalEnsemble()
    return ensemble.evaluate(
        regime_result=regime_result,
        flow_snapshot=flow_snapshot,
        leaders=leaders,
        laggards=laggards,
        vix=vix,
        breadth=breadth,
    )
