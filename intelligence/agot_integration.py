"""
AGoT Integration Layer
=======================

Provides a unified API for using the Adaptive Graph of Thoughts system
within StockMinded's existing workflow.

This module bridges the existing signal pipeline with AGoT enhancements,
allowing gradual adoption without major refactoring.

Usage:
    from intelligence.agot_integration import AGoTPipeline
    
    pipeline = AGoTPipeline(config)
    result = pipeline.run_dashboard()
    
    # result includes:
    #   - regime_result (AGoT-enhanced)
    #   - flow_result (enhanced with ensemble scoring)
    #   - leadership_result
    #   - ensemble_result (AGoT aggregation)
    #   - corrections (from feedback loop)
    #   - recommendation (final AGoT recommendation)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from config.loader import load_config, load_universe, load_sector_map
from data import feed
from signals import regime as regime_mod
from signals import flows as flows_mod
from signals import leadership as lead_mod
from signals import structure_map as sm

from intelligence.adaptive_regime import AdaptiveRegimeClassifier, AdaptiveRegimeResult
from intelligence.signal_ensemble import SignalEnsemble, EnsembleResult
from intelligence.feedback_loop import FeedbackLoop


@dataclass
class AGoTDashboardResult:
    """Complete dashboard result with AGoT enhancements."""
    # Traditional signals (backward compatible)
    regime_snapshot: Any
    flow_snapshot: Any
    structure_plan: Any
    long_leaders: list
    short_laggards: list
    
    # AGoT enhancements
    regime_agot: AdaptiveRegimeResult | None = None
    ensemble_result: EnsembleResult | None = None
    corrections: list[dict] = field(default_factory=list)
    
    # Final AGoT recommendation
    final_bias: str = "NEUTRAL"
    final_confidence: float = 0.5
    risk_adjustment: str = "NORMAL"
    recommendation: str = ""
    
    # Metadata
    compute_time_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "regime": self.regime_snapshot.to_dict() if hasattr(self.regime_snapshot, "to_dict") else {},
            "flow": self.flow_snapshot.to_dict() if hasattr(self.flow_snapshot, "to_dict") else {},
            "structure": {
                "primary": self.structure_plan.primary,
                "secondary": self.structure_plan.secondary,
                "notes": self.structure_plan.notes,
            } if self.structure_plan else {},
            "longs": [
                {"symbol": l.symbol, "quintile": l.quintile, "rs_slope_20d": l.rs_slope_20d}
                for l in self.long_leaders
            ],
            "shorts": [
                {"symbol": s.symbol, "quintile": s.quintile, "rs_slope_20d": s.rs_slope_20d}
                for s in self.short_laggards
            ],
            "agot": {
                "regime_confidence": round(self.regime_agot.primary_confidence, 3) if self.regime_agot else None,
                "regime_alternatives": self.regime_agot.alternatives if self.regime_agot else [],
                "ambiguity_score": round(self.regime_agot.ambiguity_score, 3) if self.regime_agot else None,
                "ensemble": self.ensemble_result.to_dict() if self.ensemble_result else {},
                "corrections": self.corrections,
                "final_bias": self.final_bias,
                "final_confidence": round(self.final_confidence, 3),
                "risk_adjustment": self.risk_adjustment,
            },
            "recommendation": self.recommendation,
            "compute_time_ms": round(self.compute_time_ms, 2),
            "timestamp": self.timestamp,
        }


class AGoTPipeline:
    """
    AGoT-enhanced dashboard pipeline.
    
    Runs the traditional 4-signal analysis and adds AGoT layers:
    1. Adaptive regime classification (multi-hypothesis)
    2. Signal ensemble scoring (confidence-weighted)
    3. Feedback loop corrections (learn from history)
    4. Final recommendation with risk adjustment
    """

    def __init__(
        self,
        config: dict | None = None,
        use_adaptive_regime: bool = True,
        use_signal_ensemble: bool = True,
        use_feedback_loop: bool = True,
    ):
        """
        Args:
            config: Configuration dict (loads default if not provided)
            use_adaptive_regime: Use AGoT regime classifier
            use_signal_ensemble: Use AGoT signal ensemble
            use_feedback_loop: Use AGoT feedback corrections
        """
        self.cfg = config or load_config()
        self.use_adaptive_regime = use_adaptive_regime
        self.use_signal_ensemble = use_signal_ensemble
        self.use_feedback_loop = use_feedback_loop

    def run_dashboard(self) -> AGoTDashboardResult:
        """
        Run the full AGoT-enhanced dashboard pipeline.
        
        Returns a comprehensive result with traditional signals + AGoT enhancements.
        """
        t0 = time.time()

        # --- Step 1: Fetch Data ---
        universe = load_universe(self.cfg)
        sectors = self.cfg.get("sectors", [])
        sector_map = load_sector_map(self.cfg)

        try:
            stock_data = feed.universe_ohlc(universe, period="6mo")
            sector_data = feed.sector_ohlc(sectors, period="6mo")
        except Exception as e:
            raise RuntimeError(f"Data fetch failed: {e}")

        # --- Step 2: Regime Classification ---
        regime_agot = None
        if self.use_adaptive_regime:
            classifier = AdaptiveRegimeClassifier()
            regime_agot = classifier.classify(
                index_symbol="NIFTY",
                stock_universe_data=stock_data,
            )
            regime_snapshot = regime_agot.snapshot
        else:
            regime_snapshot = regime_mod.classify("NIFTY", stock_universe_data=stock_data)

        # --- Step 3: Flow Analysis ---
        flow_snapshot = flows_mod.snapshot(sector_data, index_symbol="NIFTY")

        # --- Step 4: Leadership Ranking ---
        bench = feed.ohlc_cached("NIFTY", period="1y")
        ranks = lead_mod.rank_universe(stock_data, bench)
        inflow_syms = [s for s, _ in flow_snapshot.top_inflow_sectors]
        longs, shorts = lead_mod.a_grade(
            ranks,
            inflow_sectors=inflow_syms,
            sector_map=sector_map or None,
        )

        # --- Step 5: Structure Plan ---
        structure_plan = sm.plan_for(regime_snapshot.regime)

        # --- Step 6: AGoT Signal Ensemble ---
        ensemble_result = None
        final_bias = "NEUTRAL"
        final_confidence = 0.5
        risk_adjustment = "NORMAL"

        if self.use_signal_ensemble:
            ensemble = SignalEnsemble()
            ensemble_result = ensemble.evaluate(
                regime_result=regime_agot or regime_snapshot,
                flow_snapshot=flow_snapshot,
                leaders=longs,
                laggards=shorts,
                vix=regime_snapshot.vix,
                breadth=regime_snapshot.breadth_pct_above_50dma,
            )
            final_bias = ensemble_result.overall_bias
            final_confidence = ensemble_result.confidence
            risk_adjustment = ensemble_result.risk_adjustment

        # --- Step 7: Feedback Loop Corrections ---
        corrections = []
        if self.use_feedback_loop:
            try:
                journal_path = self.cfg.get("paths", {}).get("journal_db", "./data/journal.sqlite")
                loop = FeedbackLoop(journal_path)
                corrections = loop.get_corrections(lookback_days=30)
            except Exception:
                pass  # Feedback loop is optional

        # --- Step 8: Build Recommendation ---
        recommendation = self._build_recommendation(
            regime_snapshot, flow_snapshot, structure_plan,
            longs, shorts, final_bias, final_confidence,
            risk_adjustment, corrections, regime_agot,
        )

        elapsed_ms = (time.time() - t0) * 1000

        return AGoTDashboardResult(
            regime_snapshot=regime_snapshot,
            flow_snapshot=flow_snapshot,
            structure_plan=structure_plan,
            long_leaders=longs,
            short_laggards=shorts,
            regime_agot=regime_agot,
            ensemble_result=ensemble_result,
            corrections=corrections,
            final_bias=final_bias,
            final_confidence=final_confidence,
            risk_adjustment=risk_adjustment,
            recommendation=recommendation,
            compute_time_ms=elapsed_ms,
        )

    def _build_recommendation(
        self,
        regime_snapshot,
        flow_snapshot,
        structure_plan,
        longs,
        shorts,
        final_bias,
        final_confidence,
        risk_adjustment,
        corrections,
        regime_agot,
    ) -> str:
        """Build a human-readable recommendation string."""
        parts = []

        # Header with AGoT confidence
        conf_pct = f"{final_confidence:.0%}"
        if regime_agot:
            ambiguity = f"{regime_agot.ambiguity_score:.0%}"
            parts.append(
                f"🎯 AGoT Analysis: {final_bias} bias ({conf_pct} confidence, "
                f"ambiguity: {ambiguity})"
            )
        else:
            parts.append(f"🎯 Analysis: {final_bias} bias ({conf_pct} confidence)")

        # Regime
        parts.append(f"📊 Regime: {regime_snapshot.regime.value} | VIX: {regime_snapshot.vix:.1f}")

        # AGoT regime alternatives (if available)
        if regime_agot and regime_agot.alternatives:
            alt_strs = [f"{a['regime']}({a['confidence']:.0%})" for a in regime_agot.alternatives[:2]]
            parts.append(f"   Alternatives: {', '.join(alt_strs)}")

        # Flow bias
        parts.append(f"💰 Flow: {flow_snapshot.smart_money_bias}")

        # Risk adjustment
        if risk_adjustment == "REDUCE_SIZE":
            parts.append("⚠️ RISK: Reduce position size (signals disagree)")
        elif risk_adjustment == "SKIP":
            parts.append("🚫 RISK: Skip new trades (low confidence/agreement)")

        # Corrections from feedback
        block_corrections = [c for c in corrections if c.get("action") == "BLOCK"]
        if block_corrections:
            blocked = [c.get("target", "") for c in block_corrections[:2]]
            parts.append(f"🚫 BLOCKED: {', '.join(blocked)} (poor historical performance)")

        # Structure recommendation
        parts.append(f"📐 {structure_plan.primary}")

        # Stock picks
        if longs:
            top3 = [l.symbol for l in longs[:3]]
            parts.append(f"📈 Longs: {', '.join(top3)}")
        if shorts:
            top3 = [s.symbol for s in shorts[:3]]
            parts.append(f"📉 Shorts: {', '.join(top3)}")

        return " | ".join(parts)


# --- Convenience function ---

def run_agot_dashboard(config: dict | None = None) -> AGoTDashboardResult:
    """
    Quick function to run the AGoT-enhanced dashboard.
    
    Usage:
        result = run_agot_dashboard()
        print(result.recommendation)
    """
    pipeline = AGoTPipeline(config)
    return pipeline.run_dashboard()
