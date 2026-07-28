"""
AGoT Standalone Test Runner
============================

Quick validation script for the AGoT system.
Run this without needing the full data pipeline.

Usage:
    python test_agot_standalone.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

# Force UTF-8 output on Windows consoles to prevent cp1252 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))


def print_section(title: str):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def test_thought_graph():
    """Test the core ThoughtGraph engine."""
    print_section("1. Testing ThoughtGraph Core Engine")

    from intelligence.thought_graph import ThoughtGraph, Evidence, ThoughtStatus

    graph = ThoughtGraph("test_market_analysis")
    print(f"✅ Created ThoughtGraph: {graph.name}")

    obs = graph.add_thought(
        thought_type="observation",
        label="NIFTY at 24,500 | VIX at 14.2 | ADX at 26",
        confidence=0.9,
    )
    print(f"✅ Added observation: {obs.label}")

    trend_up = graph.branch_from(obs, "Hypothesis: TREND_UP", branch_id="trend_up")
    trend_down = graph.branch_from(obs, "Hypothesis: TREND_DOWN", branch_id="trend_down")
    range_low = graph.branch_from(obs, "Hypothesis: RANGE_LOW_VOL", branch_id="range_low")
    print(f"✅ Created 3 hypothesis branches")

    trend_up.add_evidence(Evidence("ADX", 26, supports=True, weight=2.5))
    trend_up.add_evidence(Evidence("EMA_alignment", "bullish", supports=True, weight=2.0))
    trend_up.add_evidence(Evidence("breadth", "65%>50DMA", supports=True, weight=1.5))
    print(f"✅ Added 3 pieces of evidence to TREND_UP")

    trend_down.add_evidence(Evidence("flow_data", "FII selling", supports=True, weight=1.0))
    range_low.add_evidence(Evidence("VIX_level", "low", supports=True, weight=1.5))

    best = graph.select_best()
    print(f"✅ Selected best: {best.label} (confidence: {best.confidence:.2%})")

    top3 = graph.select_top_k(3)
    print(f"✅ Top 3 hypotheses:")
    for i, node in enumerate(top3, 1):
        print(f"   {i}. {node.label} - {node.confidence:.2%} ({len(node.evidence)} evidence)")

    trace = graph.get_reasoning_trace()
    print(f"✅ Reasoning trace: {trace['node_count']} nodes, {trace['edge_count']} edges")
    print(f"\n{graph.summary()}")

    return True


def test_adaptive_regime_unit():
    """Test AdaptiveRegimeClassifier unit tests."""
    print_section("2. Testing AdaptiveRegimeClassifier (Unit Tests)")

    from intelligence.adaptive_regime import AdaptiveRegimeClassifier
    from signals.regime import Regime

    classifier = AdaptiveRegimeClassifier()
    print(f"✅ Created classifier with min_confidence={classifier.min_confidence}")

    tests = [
        ("TREND_UP with strong trend (5)", Regime.TREND_UP,
         classifier._score_trend_for_regime(Regime.TREND_UP, 5), True),
        ("TREND_UP with weak trend (1)", Regime.TREND_UP,
         classifier._score_trend_for_regime(Regime.TREND_UP, 1), False),
        ("TREND_DOWN with bearish trend (-5)", Regime.TREND_DOWN,
         classifier._score_trend_for_regime(Regime.TREND_DOWN, -5), True),
        ("High ADX supports TREND_UP", Regime.TREND_UP,
         classifier._score_adx_for_regime(Regime.TREND_UP, 30), True),
        ("Low ADX supports RANGE_LOW_VOL", Regime.RANGE_LOW_VOL,
         classifier._score_adx_for_regime(Regime.RANGE_LOW_VOL, 15), True),
        ("High VIX contradicts RANGE_LOW_VOL", Regime.RANGE_LOW_VOL,
         classifier._score_vix_level_for_regime(Regime.RANGE_LOW_VOL, 22), False),
        ("VIX spike supports VOL_EXPANSION", Regime.VOL_EXPANSION,
         classifier._score_vix_change_for_regime(Regime.VOL_EXPANSION, 40), True),
    ]

    all_pass = True
    for desc, regime, score, should_support in tests:
        is_supporting = score > 0
        status = "✅" if is_supporting == should_support else "❌"
        support_str = "supports" if is_supporting else "contradicts"
        print(f"  {status} {desc}: {support_str} ({score:+.1f})")
        if is_supporting != should_support:
            all_pass = False

    fallback = classifier._fallback_result("test error")
    print(f"  ✅ Fallback result: {fallback.primary.value}, ambiguity={fallback.ambiguity_score}")

    for regime in Regime:
        assert regime in classifier.EVIDENCE_WEIGHTS, f"Missing weights for {regime}"
    print(f"  ✅ All {len(Regime)} regimes have evidence weights defined")

    return all_pass


def test_signal_ensemble_unit():
    """Test SignalEnsemble unit tests."""
    print_section("3. Testing SignalEnsemble (Unit Tests)")

    from intelligence.signal_ensemble import SignalEnsemble
    from signals.regime import Regime, RegimeSnapshot

    ensemble = SignalEnsemble()
    print(f"✅ Created ensemble with min_agreement={ensemble.min_agreement}")

    result = ensemble.evaluate()
    print(f"✅ Empty evaluation: bias={result.overall_bias}, confidence={result.confidence:.2%}")

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

    result = ensemble.evaluate(
        regime_result=snapshot,
        vix=14.5,
        breadth=65.0,
    )
    print(f"✅ TREND_UP evaluation:")
    print(f"   Bias: {result.overall_bias} ({result.confidence:.2%})")
    print(f"   Agreement: {result.agreement_score:.2%}")
    print(f"   Risk: {result.risk_adjustment}")
    print(f"   Signals: {list(result.signal_contributions.keys())}")

    agreement = ensemble._calculate_agreement(
        ["LONG", "LONG", "LONG"],
        {"a": {"bias": "LONG", "confidence": 0.8, "weight": 2.0}}
    )
    print(f"✅ Agreement (all LONG): {agreement:.2%}")

    risk_normal = ensemble._determine_risk_adjustment(0.8, 0.7)
    risk_reduce = ensemble._determine_risk_adjustment(0.5, 0.5)
    risk_skip = ensemble._determine_risk_adjustment(0.3, 0.3)
    print(f"✅ Risk adjustments: HIGH={risk_normal}, MED={risk_reduce}, LOW={risk_skip}")

    return True


def test_feedback_loop_unit():
    """Test FeedbackLoop unit tests."""
    print_section("4. Testing FeedbackLoop (Unit Tests)")

    from intelligence.feedback_loop import FeedbackLoop

    loop = FeedbackLoop("./data/journal.sqlite")
    print(f"✅ Created feedback loop")

    verdicts = {
        0.70: "STRONG",
        0.55: "OK",
        0.45: "WEAK",
        0.30: "AVOID",
    }
    for wr, expected in verdicts.items():
        actual = loop._verdict_from_win_rate(wr)
        status = "✅" if actual == expected else "❌"
        print(f"  {status} Win rate {wr:.0%} -> {actual} (expected {expected})")

    empty = loop._empty_analysis()
    print(f"✅ Empty analysis: {empty.total_trades} trades, {empty.win_rate:.0%} win rate")

    return True


def test_agot_pipeline_creation():
    """Test AGoTPipeline creation."""
    print_section("5. Testing AGoTPipeline (Creation)")

    from intelligence.agot_integration import AGoTPipeline, AGoTDashboardResult
    from signals.regime import Regime, RegimeSnapshot

    pipeline = AGoTPipeline(
        config={"paths": {}, "sectors": [], "alerts": {}},
        use_adaptive_regime=True,
        use_signal_ensemble=True,
        use_feedback_loop=False,
    )
    print(f"✅ Created pipeline:")
    print(f"   Adaptive regime: {pipeline.use_adaptive_regime}")
    print(f"   Signal ensemble: {pipeline.use_signal_ensemble}")
    print(f"   Feedback loop: {pipeline.use_feedback_loop}")

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
        recommendation="AGoT: LONG bias (75% confidence)",
    )

    d = result.to_dict()
    print(f"✅ Result serialization:")
    print(f"   Regime: {d['regime'].get('regime', 'N/A')}")
    print(f"   Final bias: {d['agot']['final_bias']}")
    print(f"   Final confidence: {d['agot']['final_confidence']}")

    return True


def test_module_imports():
    """Test all module imports."""
    print_section("6. Testing Module Imports")

    imports = [
        ("ThoughtGraph", "from intelligence.thought_graph import ThoughtGraph"),
        ("ThoughtNode", "from intelligence.thought_graph import ThoughtNode"),
        ("Evidence", "from intelligence.thought_graph import Evidence"),
        ("AdaptiveRegimeClassifier", "from intelligence.adaptive_regime import AdaptiveRegimeClassifier"),
        ("SignalEnsemble", "from intelligence.signal_ensemble import SignalEnsemble"),
        ("FeedbackLoop", "from intelligence.feedback_loop import FeedbackLoop"),
        ("AGoTPipeline", "from intelligence.agot_integration import AGoTPipeline"),
        ("run_agot_dashboard", "from intelligence.agot_integration import run_agot_dashboard"),
    ]

    all_ok = True
    for name, import_stmt in imports:
        try:
            exec(import_stmt)
            print(f"  ✅ {name}")
        except Exception as e:
            print(f"  ❌ {name}: {e}")
            all_ok = False

    try:
        from intelligence import (
            ThoughtGraph, AdaptiveRegimeClassifier,
            SignalEnsemble, FeedbackLoop, AGoTPipeline,
        )
        print(f"  ✅ Top-level intelligence imports")
    except Exception as e:
        print(f"  ❌ Top-level imports: {e}")
        all_ok = False

    return all_ok


def run_all_tests():
    """Run all standalone tests."""
    print("\n" + "=" * 60)
    print("  AGoT (Adaptive Graph of Thoughts) - Standalone Test Suite")
    print("=" * 60)

    t0 = time.time()
    results = {}

    tests = [
        ("ThoughtGraph", test_thought_graph),
        ("AdaptiveRegime", test_adaptive_regime_unit),
        ("SignalEnsemble", test_signal_ensemble_unit),
        ("FeedbackLoop", test_feedback_loop_unit),
        ("AGoTPipeline", test_agot_pipeline_creation),
        ("ModuleImports", test_module_imports),
    ]

    for name, test_fn in tests:
        try:
            results[name] = test_fn()
        except Exception as e:
            print(f"\n❌ {name} FAILED with exception: {e}")
            import traceback
            traceback.print_exc()
            results[name] = False

    elapsed = time.time() - t0
    print_section("Test Summary")

    passed = sum(1 for v in results.values() if v)
    total = len(results)

    for name, result in results.items():
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"  {status} - {name}")

    print(f"\n  Total: {passed}/{total} passed in {elapsed:.2f}s")

    if passed == total:
        print("\n  🎉 All tests passed! AGoT system is working correctly.")
    else:
        print(f"\n  ⚠️ {total - passed} test(s) failed.")

    return passed == total


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)
