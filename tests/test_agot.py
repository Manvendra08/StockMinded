"""
AGoT (Adaptive Graph of Thoughts) System Tests
================================================

Tests for the intelligence module including:
- ThoughtGraph core engine
- AdaptiveRegimeClassifier
- SignalEnsemble
- FeedbackLoop
- AGoTPipeline integration
"""
from __future__ import annotations

import pytest
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestThoughtGraph:
    """Tests for the core ThoughtGraph engine."""

    def test_create_graph(self):
        from intelligence.thought_graph import ThoughtGraph
        graph = ThoughtGraph("test_graph")
        assert graph.name == "test_graph"
        assert len(graph.nodes) == 0

    def test_add_thought(self):
        from intelligence.thought_graph import ThoughtGraph
        graph = ThoughtGraph("test")
        node = graph.add_thought(
            thought_type="observation",
            label="NIFTY above 200 EMA",
            confidence=0.8,
        )
        assert node.label == "NIFTY above 200 EMA"
        assert node.confidence == 0.8
        assert node.id in graph.nodes

    def test_branch_from(self):
        from intelligence.thought_graph import ThoughtGraph, ThoughtStatus
        graph = ThoughtGraph("test")
        parent = graph.add_thought(label="Root", confidence=0.5)
        child = graph.branch_from(parent, "Branch A", branch_id="branch_a")
        
        assert child.branch_id == "branch_a"
        assert parent.status == ThoughtStatus.BRANCHED
        assert child.id in parent.children

    def test_evidence_updates_confidence(self):
        from intelligence.thought_graph import ThoughtGraph, Evidence
        graph = ThoughtGraph("test")
        node = graph.add_thought(label="Hypothesis", confidence=0.5)
        
        # Add supporting evidence
        node.add_evidence(Evidence(source="test", value=1, supports=True, weight=2.0))
        assert node.confidence > 0.5

    def test_contradicting_evidence(self):
        from intelligence.thought_graph import ThoughtGraph, Evidence
        graph = ThoughtGraph("test")
        node = graph.add_thought(label="Hypothesis", confidence=0.5)
        
        # Add contradicting evidence
        node.add_evidence(Evidence(source="test", value=1, supports=False, weight=2.0))
        assert node.confidence < 0.5

    def test_select_best(self):
        from intelligence.thought_graph import ThoughtGraph, Evidence
        graph = ThoughtGraph("test")
        
        root = graph.add_thought(label="Root", confidence=0.5)
        a = graph.branch_from(root, "Hypothesis A", branch_id="a", confidence=0.5)
        b = graph.branch_from(root, "Hypothesis B", branch_id="b", confidence=0.5)
        
        # Add strong evidence to A
        a.add_evidence(Evidence("test", 1, supports=True, weight=3.0))
        a.add_evidence(Evidence("test2", 1, supports=True, weight=2.0))
        
        best = graph.select_best()
        assert best is not None
        assert "A" in best.label

    def test_reasoning_trace(self):
        from intelligence.thought_graph import ThoughtGraph
        graph = ThoughtGraph("test")
        graph.add_thought(label="Node 1", confidence=0.6)
        graph.add_thought(label="Node 2", confidence=0.7)
        
        trace = graph.get_reasoning_trace()
        assert trace["node_count"] == 2
        assert "nodes" in trace
        assert "edges" in trace

    def test_revise_thought(self):
        from intelligence.thought_graph import ThoughtGraph, ThoughtStatus
        graph = ThoughtGraph("test")
        original = graph.add_thought(label="Original", confidence=0.8)
        
        revision = graph.revise(original.id, "Updated conclusion", 0.6, "new data")
        
        assert original.status == ThoughtStatus.REVISED
        assert revision.revision_of == original.id
        assert "REVISED" in revision.label


class TestAdaptiveRegime:
    """Tests for the AGoT regime classifier."""

    def test_classifier_creation(self):
        from intelligence.adaptive_regime import AdaptiveRegimeClassifier
        classifier = AdaptiveRegimeClassifier()
        assert classifier.min_confidence == 0.35

    def test_evidence_weights_defined(self):
        from intelligence.adaptive_regime import AdaptiveRegimeClassifier
        from signals.regime import Regime
        
        classifier = AdaptiveRegimeClassifier()
        # All regimes should have evidence weights
        for regime in Regime:
            assert regime in classifier.EVIDENCE_WEIGHTS

    def test_fallback_result(self):
        from intelligence.adaptive_regime import AdaptiveRegimeClassifier
        from signals.regime import Regime
        
        classifier = AdaptiveRegimeClassifier()
        result = classifier._fallback_result("test error")
        
        assert result.primary == Regime.RANGE_LOW_VOL
        assert result.ambiguity_score == 1.0
        assert "test error" in result.snapshot.notes

    def test_scoring_functions(self):
        from intelligence.adaptive_regime import AdaptiveRegimeClassifier
        from signals.regime import Regime
        
        classifier = AdaptiveRegimeClassifier()
        
        # Strong trend should support TREND_UP
        score = classifier._score_trend_for_regime(Regime.TREND_UP, 5)
        assert score > 0
        
        # Weak trend should not support TREND_UP
        score = classifier._score_trend_for_regime(Regime.TREND_UP, 0)
        assert score < 0
        
        # High ADX supports trend regimes
        score = classifier._score_adx_for_regime(Regime.TREND_UP, 30)
        assert score > 0


class TestSignalEnsemble:
    """Tests for the signal ensemble module."""

    def test_ensemble_creation(self):
        from intelligence.signal_ensemble import SignalEnsemble
        ensemble = SignalEnsemble()
        assert ensemble.min_agreement == 0.5

    def test_signal_weights_defined(self):
        from intelligence.signal_ensemble import SignalEnsemble
        
        ensemble = SignalEnsemble()
        assert "regime" in ensemble.SIGNAL_WEIGHTS
        assert "flow_bias" in ensemble.SIGNAL_WEIGHTS
        assert "leadership" in ensemble.SIGNAL_WEIGHTS

    def test_evaluate_with_minimal_input(self):
        from intelligence.signal_ensemble import SignalEnsemble
        
        ensemble = SignalEnsemble()
        result = ensemble.evaluate()  # No inputs
        
        assert result.overall_bias in ("LONG", "SHORT", "NEUTRAL")
        assert 0.0 <= result.confidence <= 1.0
        assert result.risk_adjustment in ("NORMAL", "REDUCE_SIZE", "SKIP")

    def test_evaluate_with_regime_only(self):
        from intelligence.signal_ensemble import SignalEnsemble
        from signals.regime import Regime, RegimeSnapshot
        
        ensemble = SignalEnsemble()
        
        # Create a mock regime snapshot
        snapshot = RegimeSnapshot(
            regime=Regime.TREND_UP,
            trend_score=4,
            vix=14.5,
            vix_5d_change_pct=-2.0,
            vix_rank=25.0,
            adx=28.0,
            breadth_pct_above_50dma=65.0,
            notes="test",
        )
        
        result = ensemble.evaluate(regime_result=snapshot, vix=14.5, breadth=65.0)
        
        assert result.overall_bias in ("LONG", "SHORT", "NEUTRAL")
        assert "regime" in result.signal_contributions

    def test_agreement_calculation(self):
        from intelligence.signal_ensemble import SignalEnsemble
        
        ensemble = SignalEnsemble()
        
        # All agree
        agreement = ensemble._calculate_agreement(
            ["LONG", "LONG", "LONG"],
            {"a": {"bias": "LONG", "confidence": 0.8, "weight": 1.0}}
        )
        assert agreement > 0.6

    def test_risk_adjustment(self):
        from intelligence.signal_ensemble import SignalEnsemble
        
        ensemble = SignalEnsemble()
        
        # High agreement + high confidence = NORMAL
        adj = ensemble._determine_risk_adjustment(0.8, 0.7)
        assert adj == "NORMAL"
        
        # Low agreement + low confidence = SKIP
        adj = ensemble._determine_risk_adjustment(0.3, 0.3)
        assert adj == "SKIP"


class TestFeedbackLoop:
    """Tests for the feedback loop module."""

    def test_loop_creation(self):
        from intelligence.feedback_loop import FeedbackLoop
        loop = FeedbackLoop("./data/journal.sqlite")
        assert loop.db_path == "./data/journal.sqlite"

    def test_empty_analysis(self):
        from intelligence.feedback_loop import FeedbackLoop
        
        loop = FeedbackLoop("./nonexistent_db.sqlite")
        analysis = loop._empty_analysis()
        
        assert analysis.total_trades == 0
        assert analysis.win_rate == 0.0
        assert analysis.corrections == []

    def test_verdict_from_win_rate(self):
        from intelligence.feedback_loop import FeedbackLoop
        
        loop = FeedbackLoop("./data/journal.sqlite")
        
        assert loop._verdict_from_win_rate(0.70) == "STRONG"
        assert loop._verdict_from_win_rate(0.55) == "OK"
        assert loop._verdict_from_win_rate(0.45) == "WEAK"
        assert loop._verdict_from_win_rate(0.30) == "AVOID"


class TestAGoTPipeline:
    """Tests for the AGoT integration pipeline."""

    def test_pipeline_creation(self):
        from intelligence.agot_integration import AGoTPipeline
        
        # Should handle missing config gracefully
        pipeline = AGoTPipeline(
            config={"paths": {}, "sectors": [], "alerts": {}},
            use_adaptive_regime=True,
            use_signal_ensemble=True,
            use_feedback_loop=False,  # Skip for test
        )
        
        assert pipeline.use_adaptive_regime == True
        assert pipeline.use_signal_ensemble == True

    def test_result_to_dict(self):
        from intelligence.agot_integration import AGoTDashboardResult
        from signals.regime import Regime, RegimeSnapshot
        
        snapshot = RegimeSnapshot(
            regime=Regime.TREND_UP,
            trend_score=4,
            vix=14.5,
            vix_5d_change_pct=-2.0,
            vix_rank=25.0,
            adx=28.0,
            breadth_pct_above_50dma=65.0,
            notes="test",
        )
        
        result = AGoTDashboardResult(
            regime_snapshot=snapshot,
            flow_snapshot=None,
            structure_plan=None,
            long_leaders=[],
            short_laggards=[],
            final_bias="LONG",
            final_confidence=0.75,
            risk_adjustment="NORMAL",
            recommendation="Test recommendation",
        )
        
        d = result.to_dict()
        assert "regime" in d
        assert "agot" in d
        assert d["agot"]["final_bias"] == "LONG"


class TestImports:
    """Verify all intelligence module imports work."""

    def test_import_thought_graph(self):
        from intelligence.thought_graph import (
            ThoughtGraph, ThoughtNode, ThoughtEdge,
            ThoughtStatus, Evidence,
            create_regime_graph, create_signal_graph,
        )
        assert ThoughtGraph is not None

    def test_import_adaptive_regime(self):
        from intelligence.adaptive_regime import (
            AdaptiveRegimeClassifier,
            AdaptiveRegimeResult,
            classify_adaptive,
        )
        assert AdaptiveRegimeClassifier is not None

    def test_import_signal_ensemble(self):
        from intelligence.signal_ensemble import (
            SignalEnsemble,
            EnsembleResult,
            evaluate_signals,
        )
        assert SignalEnsemble is not None

    def test_import_feedback_loop(self):
        from intelligence.feedback_loop import (
            FeedbackLoop,
            FeedbackAnalysis,
            get_agot_corrections,
        )
        assert FeedbackLoop is not None

    def test_import_agot_integration(self):
        from intelligence.agot_integration import (
            AGoTPipeline,
            AGoTDashboardResult,
            run_agot_dashboard,
        )
        assert AGoTPipeline is not None

    def test_import_from_intelligence(self):
        from intelligence import (
            ThoughtGraph,
            AdaptiveRegimeClassifier,
            SignalEnsemble,
            FeedbackLoop,
            AGoTPipeline,
            run_agot_dashboard,
        )
        assert ThoughtGraph is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
