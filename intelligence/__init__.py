"""
Intelligence Module - AGoT (Adaptive Graph of Thoughts)
=======================================================

This module implements the Adaptive Graph of Thoughts framework for
StockMinded's market analysis and decision-making.

Core Components:
- thought_graph: Core graph-based reasoning engine
- adaptive_regime: Multi-hypothesis regime classification
- signal_ensemble: Confidence-weighted signal aggregation
- feedback_loop: Learn from trade outcomes
- learner: Legacy rule-based learning (backward compatible)

Quick Start:
    from intelligence import (
        AdaptiveRegimeClassifier,
        SignalEnsemble,
        FeedbackLoop,
    )
    
    # 1. Classify regime with AGoT
    classifier = AdaptiveRegimeClassifier()
    regime_result = classifier.classify()
    
    # 2. Evaluate all signals
    ensemble = SignalEnsemble()
    signal_result = ensemble.evaluate(
        regime_result=regime_result,
        flow_snapshot=flow_snapshot,
        leaders=long_stocks,
        laggards=short_stocks,
    )
    
    # 3. Get learning corrections
    loop = FeedbackLoop()
    corrections = loop.get_corrections()
"""
from __future__ import annotations

# Core AGoT Engine
from intelligence.thought_graph import (
    ThoughtGraph,
    ThoughtNode,
    ThoughtEdge,
    ThoughtStatus,
    Evidence,
    create_regime_graph,
    create_signal_graph,
)

# Adaptive Regime Classifier
from intelligence.adaptive_regime import (
    AdaptiveRegimeClassifier,
    AdaptiveRegimeResult,
    classify_adaptive,
)

# Signal Ensemble
from intelligence.signal_ensemble import (
    SignalEnsemble,
    EnsembleResult,
    evaluate_signals,
)

# Feedback Loop
from intelligence.feedback_loop import (
    FeedbackLoop,
    FeedbackAnalysis,
    get_agot_corrections,
)

# Legacy learner (backward compatible)
from intelligence.learner import (
    analyze_history,
    build_correction_strings,
    apply_learned_filter,
)

# Integration Layer
from intelligence.agot_integration import (
    AGoTPipeline,
    AGoTDashboardResult,
    run_agot_dashboard,
)


__all__ = [
    # Core AGoT
    "ThoughtGraph",
    "ThoughtNode",
    "ThoughtEdge",
    "ThoughtStatus",
    "Evidence",
    "create_regime_graph",
    "create_signal_graph",
    # Adaptive Regime
    "AdaptiveRegimeClassifier",
    "AdaptiveRegimeResult",
    "classify_adaptive",
    # Signal Ensemble
    "SignalEnsemble",
    "EnsembleResult",
    "evaluate_signals",
    # Feedback Loop
    "FeedbackLoop",
    "FeedbackAnalysis",
    "get_agot_corrections",
    # Legacy
    "analyze_history",
    "build_correction_strings",
    "apply_learned_filter",
    # Integration
    "AGoTPipeline",
    "AGoTDashboardResult",
    "run_agot_dashboard",
]
