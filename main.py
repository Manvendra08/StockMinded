"""StockMinded — daily decision engine.

Usage:
  python main.py dashboard          # run the 4-signal dashboard now
  python main.py agot               # run the AGoT-enhanced dashboard
  python main.py agot-test          # quick AGoT system validation (no data fetch)
  python main.py schedule           # run APScheduler loop (IST times from config)
  python main.py health             # quick data connectivity check
"""
from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from pytz import timezone as pytz_timezone
    _SCHEDULER_AVAILABLE = True
except ImportError:
    _SCHEDULER_AVAILABLE = False

from config.loader import load_config, load_universe, load_sector_map
from data import feed
from signals import regime as regime_mod
from signals import flows as flows_mod
from signals import leadership as lead_mod
from signals import structure_map as sm
from ops.alerts import Alerter, format_dashboard
from ops.journal import Journal

# AGoT imports (lazy-loaded in functions to avoid startup overhead)


def run_dashboard(cfg: dict) -> None:
    alerter = Alerter(cfg["alerts"].get("telegram_bot_token"), cfg["alerts"].get("telegram_chat_id"))
    journal = Journal(cfg["paths"]["journal_db"])

    universe = load_universe(cfg)
    sectors = cfg["sectors"]
    sector_map = load_sector_map(cfg)

    try:
        stock_data = feed.universe_ohlc(universe, period="6mo")
        sector_data = feed.sector_ohlc(sectors, period="6mo")
    except Exception as e:
        alerter.send(f"⚠️ Data fetch failed: {e}")
        raise

    regime_snap = regime_mod.classify("NIFTY", stock_universe_data=stock_data)
    flow_snap = flows_mod.snapshot(sector_data, index_symbol="NIFTY")

    bench = feed.ohlc_cached("NIFTY", period="1y")
    ranks = lead_mod.rank_universe(stock_data, bench)
    inflow_syms = [s for s, _ in flow_snap.top_inflow_sectors]
    # Pass sector_map so a_grade() can filter leaders/laggards by inflow sectors.
    # load_sector_map() returns {} on failure; log warning to avoid silent disable.
    if not sector_map:
        logging.getLogger(__name__).warning("sector_map is empty — inflow sector filtering disabled")
    longs, shorts = lead_mod.a_grade(ranks, inflow_sectors=inflow_syms, sector_map=sector_map if sector_map else None)

    structure = sm.plan_for(regime_snap.regime)

    journal.log_regime(regime_snap.to_dict())
    journal.log_flow(flow_snap.to_dict())

    msg = format_dashboard(regime_snap, flow_snap, structure, longs, shorts)
    alerter.send(msg)


def run_agot_dashboard(cfg: dict) -> None:
    """Run the AGoT-enhanced dashboard with multi-hypothesis regime classification,
    signal ensemble voting, and feedback loop corrections."""
    from intelligence.agot_integration import AGoTPipeline

    alerter = Alerter(cfg["alerts"].get("telegram_bot_token"), cfg["alerts"].get("telegram_chat_id"))
    journal = Journal(cfg["paths"]["journal_db"])

    print("🧠 Running AGoT-enhanced dashboard...")

    pipeline = AGoTPipeline(
        config=cfg,
        use_adaptive_regime=True,
        use_signal_ensemble=True,
        use_feedback_loop=True,
    )

    try:
        result = pipeline.run_dashboard()
    except Exception as e:
        alerter.send(f"⚠️ AGoT dashboard failed: {e}")
        raise

    # Log to journal
    journal.log_regime(result.regime_snapshot.to_dict())
    journal.log_flow(result.flow_snapshot.to_dict())

    # Format AGoT message for Telegram
    msg = _format_agot_message(result)
    alerter.send(msg)

    # Also print to console
    print(f"\n{'='*60}")
    print(f"  AGoT Dashboard Complete ({result.compute_time_ms:.0f}ms)")
    print(f"{'='*60}")
    print(f"  Regime: {result.regime_snapshot.regime.value}")
    if result.regime_agot:
        print(f"  AGoT Confidence: {result.regime_agot.primary_confidence:.1%}")
        print(f"  Ambiguity: {result.regime_agot.ambiguity_score:.1%}")
        if result.regime_agot.alternatives:
            print(f"  Alternatives: {result.regime_agot.alternatives}")
    print(f"  Flow Bias: {result.flow_snapshot.smart_money_bias}")
    print(f"  Final Bias: {result.final_bias} ({result.final_confidence:.1%})")
    print(f"  Risk: {result.risk_adjustment}")
    if result.corrections:
        print(f"  Corrections: {len(result.corrections)} active")
    print(f"  Longs: {[l.symbol for l in result.long_leaders[:5]]}")
    print(f"  Shorts: {[s.symbol for s in result.short_laggards[:5]]}")
    print(f"\n  Recommendation:\n  {result.recommendation}")


def _format_agot_message(result) -> str:
    """Format AGoT result for Telegram."""
    lines = []
    lines.append("*🧠 AGoT Morning Dashboard*")
    lines.append("")

    # Header with AGoT confidence
    regime_val = result.regime_snapshot.regime.value if hasattr(result.regime_snapshot.regime, 'value') else str(result.regime_snapshot.regime)
    lines.append(f"*Regime:* `{regime_val}`")
    if result.regime_agot:
        lines.append(f"  AGoT confidence: `{result.regime_agot.primary_confidence:.1%}`")
        lines.append(f"  Ambiguity: `{result.regime_agot.ambiguity_score:.1%}`")

    # Regime details
    snap = result.regime_snapshot
    lines.append(f"  trend={snap.trend_score:+d}  VIX={snap.vix} ({snap.vix_5d_change_pct:+.1f}% 5d)  ADX={snap.adx}")
    if snap.breadth_pct_above_50dma is not None:
        lines.append(f"  breadth>50DMA: {snap.breadth_pct_above_50dma}%")

    # Alternatives
    if result.regime_agot and result.regime_agot.alternatives:
        alt_strs = [f"{a['regime']}({a['confidence']:.0%})" for a in result.regime_agot.alternatives[:2]]
        lines.append(f"  alternatives: {', '.join(alt_strs)}")

    lines.append("")

    # Flow
    flow = result.flow_snapshot
    lines.append(f"*Flows* — bias: `{flow.smart_money_bias}`")
    lines.append(f"  FII/DII 5d (₹Cr): {flow.fii_dii_5d_net_cr}")
    lines.append(f"  PCR OI={flow.pcr_oi}  Vol={flow.pcr_vol}  MaxPain={flow.max_pain}")

    lines.append("")

    # AGoT Ensemble
    lines.append(f"*AGoT Ensemble* — `{result.final_bias}` ({result.final_confidence:.0%})")
    lines.append(f"  Risk: `{result.risk_adjustment}`")
    if result.ensemble_result:
        lines.append(f"  Agreement: `{result.ensemble_result.agreement_score:.0%}`")

    # Corrections
    if result.corrections:
        lines.append("")
        lines.append(f"*Corrections* ({len(result.corrections)} active):")
        for c in result.corrections[:3]:
            lines.append(f"  🚫 {c.get('type')}: {c.get('target')} — {c.get('reason', '')[:60]}")

    lines.append("")

    # Structure
    if result.structure_plan:
        lines.append(f"*Structure:* {result.structure_plan.primary}")
        if result.structure_plan.secondary:
            lines.append(f"  {result.structure_plan.secondary}")

    lines.append("")

    # Stock picks
    if result.long_leaders:
        top_longs = [l.symbol for l in result.long_leaders[:5]]
        lines.append(f"*Longs:* {', '.join(top_longs)}")
    if result.short_laggards:
        top_shorts = [s.symbol for s in result.short_laggards[:5]]
        lines.append(f"*Shorts:* {', '.join(top_shorts)}")

    return "\n".join(lines)


def run_agot_test(cfg: dict) -> None:
    """Quick validation of the AGoT system without fetching market data."""
    print("\n" + "=" * 60)
    print("  AGoT System Validation Test")
    print("=" * 60)

    from signals.regime import Regime, RegimeSnapshot

    # 1. Test ThoughtGraph
    print("\n[1/5] ThoughtGraph Engine...")
    from intelligence.thought_graph import ThoughtGraph, Evidence
    graph = ThoughtGraph("validation_test")
    obs = graph.add_thought("observation", "Market data", 0.9)
    h1 = graph.branch_from(obs, "TREND_UP", branch_id="up")
    h2 = graph.branch_from(obs, "TREND_DOWN", branch_id="down")
    h1.add_evidence(Evidence("test", 1, True, 3.0))
    h1.add_evidence(Evidence("test2", 1, True, 2.0))
    best = graph.select_best()
    assert best is not None and best.label.upper() == Regime.TREND_UP.value, "ThoughtGraph failed"
    print(f"  ✅ ThoughtGraph works (best={best.label}, conf={best.confidence:.2f})")

    # 2. Test AdaptiveRegime (unit-level, no data fetch)
    print("\n[2/5] AdaptiveRegimeClassifier (unit tests)...")
    from intelligence.adaptive_regime import AdaptiveRegimeClassifier
    classifier = AdaptiveRegimeClassifier()
    score = classifier._score_trend_for_regime(Regime.TREND_UP, 5)
    assert score > 0, "TREND_UP scoring failed"
    fallback = classifier._fallback_result("test")
    assert fallback.primary == Regime.RANGE_LOW_VOL, "Fallback failed"
    print(f"  ✅ AdaptiveRegime works (trend_score→TREND_UP={score:+.1f})")

    # 3. Test SignalEnsemble
    print("\n[3/5] SignalEnsemble...")
    from intelligence.signal_ensemble import SignalEnsemble
    ensemble = SignalEnsemble()
    snapshot = RegimeSnapshot(
        regime=Regime.TREND_UP, trend_score=4, vix=14.5,
        vix_5d_change_pct=-2.0, vix_rank=25.0, adx=28.0,
        breadth_pct_above_50dma=65.0, notes="test",
    )
    result = ensemble.evaluate(regime_result=snapshot, vix=14.5, breadth=65.0)
    assert result.overall_bias in ("LONG", "SHORT", "NEUTRAL"), "Ensemble failed"
    print(f"  ✅ SignalEnsemble works (bias={result.overall_bias}, conf={result.confidence:.2f})")

    # 4. Test FeedbackLoop
    print("\n[4/5] FeedbackLoop...")
    from intelligence.feedback_loop import FeedbackLoop
    loop = FeedbackLoop(cfg.get("paths", {}).get("journal_db", "./data/journal.sqlite"))
    empty = loop._empty_analysis()
    assert empty.total_trades == 0, "FeedbackLoop failed"
    verdict = loop._verdict_from_win_rate(0.65)
    assert verdict == "STRONG", f"Verdict wrong: {verdict}"
    corrections = loop.get_corrections(lookback_days=30)
    print(f"  ✅ FeedbackLoop works ({len(corrections)} active corrections)")

    # 5. Test AGoTPipeline
    print("\n[5/5] AGoTPipeline (creation)...")
    from intelligence.agot_integration import AGoTPipeline, AGoTDashboardResult
    pipeline = AGoTPipeline(
        config={"paths": {}, "sectors": [], "alerts": {}},
        use_adaptive_regime=True,
        use_signal_ensemble=True,
        use_feedback_loop=False,
    )
    assert pipeline.use_adaptive_regime is True, "Pipeline failed"
    print(f"  ✅ AGoTPipeline works")

    print("\n" + "=" * 60)
    print("  🎉 All AGoT components validated successfully!")
    print("=" * 60)
    print("\n  Next steps:")
    print("    python main.py agot          # Run AGoT dashboard with live data")
    print("    python main.py dashboard     # Run original dashboard")
    print("")


def run_health(cfg: dict) -> None:
    checks = []
    try:
        df = feed.ohlc_cached("NIFTY", period="1mo")
        checks.append(f"✅ NIFTY OHLC: {len(df)} rows, last close {df['close'].iloc[-1]:.2f}")
    except Exception as e:
        checks.append(f"❌ NIFTY OHLC: {e}")

    try:
        v = feed.india_vix(period="1mo")
        checks.append(f"✅ India VIX: {v['close'].iloc[-1]:.2f}")
    except Exception as e:
        checks.append(f"❌ VIX: {e}")

    try:
        pcr_oi, pcr_vol, mp, pcr_stale, mp_stale, pcr_updated_at, mp_updated_at = (
            feed.get_pcr_max_pain_cached("NIFTY")
        )
        checks.append(f"✅ PCR (OI): {pcr_oi} stale={pcr_stale} (Updated: {pcr_updated_at})")
    except Exception as e:
        checks.append(f"❌ Option chain/PCR feed: {e}")

    try:
        df = feed.fii_dii_cash(days=3)
        checks.append(f"✅ FII/DII: {len(df)} rows")
    except Exception as e:
        checks.append(f"❌ FII/DII: {e}")

    print("\n".join(checks))


def run_schedule(cfg: dict) -> None:
    if not _SCHEDULER_AVAILABLE:
        raise RuntimeError(
            "APScheduler / pytz not installed. Run: pip install apscheduler pytz"
        )

    alerter = Alerter(cfg["alerts"].get("telegram_bot_token"), cfg["alerts"].get("telegram_chat_id"))

    ist = pytz_timezone("Asia/Kolkata")
    sched = BlockingScheduler(timezone=ist)

    s = cfg["schedule_ist"]

    def _h(name, fn):
        def wrapped():
            try:
                fn(cfg)
            except Exception as e:
                logging.getLogger(__name__).exception("[%s] failed", name)
                alerter.send(f"⚠️ Scheduler job [{name}] failed: {e}")
        return wrapped

    hh, mm = map(int, s["morning_dashboard"].split(":"))
    sched.add_job(_h("dashboard", run_dashboard), CronTrigger(hour=hh, minute=mm, day_of_week="mon-fri"))
    print(f"Scheduler started (IST). Morning dashboard @ {s['morning_dashboard']}.  Ctrl-C to stop.")

    def _shutdown_handler(signum, frame):
        print("\nShutting down scheduler gracefully...")
        sched.shutdown(wait=False)
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    signal.signal(signal.SIGTERM, _shutdown_handler)

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        print("\nScheduler stopped.")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cfg = load_config()
    cmd = sys.argv[1].lower()
    if cmd == "dashboard":
        run_dashboard(cfg)
    elif cmd == "agot":
        run_agot_dashboard(cfg)
    elif cmd == "agot-test":
        run_agot_test(cfg)
    elif cmd == "health":
        run_health(cfg)
    elif cmd == "schedule":
        run_schedule(cfg)
    else:
        print(f"unknown command: {cmd}")
        return 2
    return 0


if __name__ == "__main__":
    import io
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.exit(main())
