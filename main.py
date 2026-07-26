"""StockMinded — daily decision engine.

Usage:
  python main.py dashboard          # run the 4-signal dashboard now
  python main.py agot               # run the AGoT-enhanced dashboard
  python main.py agot-test          # quick AGoT system validation (no data fetch)
  python main.py schedule           # run APScheduler loop (IST times from config)
  python main.py health             # quick data connectivity check
  python main.py telegram-pipeline [--dry-run]  # run dual-engine Telegram → verdict pipeline
  python main.py investment-dashboard # run lightweight server for Investment Verdicts page only
"""
from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime
from typing import Any

from core.log import setup_logging
setup_logging()

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from pytz import timezone as pytz_timezone
    _SCHEDULER_AVAILABLE = True
except ImportError:
    _SCHEDULER_AVAILABLE = False

from config.loader import load_config, load_universe, load_sector_map
from data import feed
from ops.alerts import Alerter, format_dashboard
from ops.journal import Journal
from ops.telegram_state import TelegramState

# AGoT imports (lazy-loaded in functions to avoid startup overhead)


def run_dashboard(cfg: dict) -> None:
    # H1 fix: these signal modules were referenced below but never imported,
    # causing a NameError the moment `python main.py dashboard` ran. Lazy-import
    # them here (consistent with run_agot_dashboard) to avoid startup overhead.
    from signals import regime as regime_mod
    from signals import flows as flows_mod
    from signals import leadership as lead_mod
    from signals import structure_map as sm

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


def run_telegram_pipeline(cfg: dict, dry_run: bool = False, force_live: bool = False) -> dict:
    """Run one pass of the dual-engine Telegram → verdict pipeline.

    This is fully additive: it reads Telegram channels, parses tickers with an
    LLM, fetches fundamentals, applies hard filters + LLM fusion, persists
    verdicts to the journal, and (unless dry_run) sends alert cards. It does
    NOT touch any existing dashboard/engine code paths.

    Returns a run summary dict for logging/tests.
    """
    from datetime import datetime, timezone

    import uuid

    from data.ai_scraper import call_llm
    from data.screener_fetcher import fetch_fundamentals
    from data.telegram_fetcher import fetch_all_channels
    from signals.regime import classify as classify_regime
    from signals.telegram_fusion import run_fusion
    from signals.telegram_parser import parse_message, is_spam

    tp_cfg = cfg.get("telegram_pipeline", {})
    sahi_cfg = cfg.get("sahi_news", {})
    tp_enabled = tp_cfg.get("enabled", True)
    sahi_enabled = sahi_cfg.get("enabled", True)

    if not tp_enabled and not sahi_enabled and not dry_run and not force_live:
        logging.getLogger(__name__).info("telegram-pipeline and sahi-news disabled in config")
        return {"skipped": True, "reason": "disabled"}

    journal = Journal(cfg["paths"]["journal_db"])
    state = TelegramState(cfg["paths"]["journal_db"])

    universe = set(load_universe(cfg)) if tp_cfg.get("fusion", {}).get("universe_filter", True) else None

    # 1. Fetch new Telegram messages
    # H2 fix: removed redundant `from datetime import datetime` that shadowed the
    # `datetime` already imported at the top of this function (with timezone).
    dt_str = datetime.now().strftime('%H:%M:%S')
    print(f"\033[94m[{dt_str}] [Telegram Pipeline] Attempting to fetch new messages...\033[0m")
    try:
        messages = fetch_all_channels(tp_cfg, state, limit=tp_cfg.get("max_messages_per_run", 20))
        if messages:
            platform_map = {ch.get("username"): ch.get("platform", "telegram") for ch in tp_cfg.get("channels", [])}
            journal.log_raw_messages(messages, platform_map)
        print(f"\033[92m[{datetime.now().strftime('%H:%M:%S')}] [Telegram Pipeline] Status: SUCCESS. Fetched {len(messages)} messages.\033[0m")
    except Exception as e:
        print(f"\033[91m[{datetime.now().strftime('%H:%M:%S')}] [Telegram Pipeline] Status: FAILED. Error: {e}\033[0m")
        return {"messages": 0, "extracted": 0, "verdicts": 0, "error": str(e)}

    # 2. Parse tickers from each message (dedupe by symbol, keep strongest context)
    extracted_map: dict[str, Any] = {}
    llm_calls = 0
    if messages:
        for m in messages:
            tickers = parse_message(
                m.text,
                call_llm=call_llm,
                universe=universe,
                model=tp_cfg.get("parser", {}).get("model", "llama-3.3-70b-versatile"),
            )
            llm_calls += 1 if tickers or not is_spam(m.text) else 0
            for tk in tickers:
                tk.source_platform = "telegram"
                tk.telegram_msg_id = m.msg_id
                tk.telegram_channel = m.channel
                if tk.symbol not in extracted_map or tk.confidence > extracted_map[tk.symbol].confidence:
                    extracted_map[tk.symbol] = tk
        logging.getLogger(__name__).info(
            "Parsed %d unique symbols from %d messages (LLM calls: ~%d)",
            len(extracted_map), len(messages), llm_calls,
        )

    # 2b. Fetch sahi.com breaking news headlines (additive source)
    sahi_cfg = cfg.get("sahi_news", {})
    sahi_enabled = sahi_cfg.get("enabled", True)
    sahi_extracted = []
    if sahi_enabled:
        try:
            from data.sahi_news import extract_tickers_from_headlines, fetch_sahi_headlines
            # Collect the full recency window (default: past 1 hour) across
            # multiple listing pages so the bot does not miss headlines that
            # fall beyond the first page of the breaking-news feed.
            fetch_kwargs = {
                "limit": sahi_cfg.get("max_headlines", 60),
                "window_minutes": sahi_cfg.get("window_minutes", 60),
                "max_pages": sahi_cfg.get("max_pages", 4),
            }
            if force_live:
                fetch_kwargs["min_interval"] = 0
            sahi_headlines = fetch_sahi_headlines(**fetch_kwargs)
            if sahi_headlines:
                sahi_extracted = extract_tickers_from_headlines(
                    sahi_headlines,
                    call_llm=call_llm,
                    universe=universe,
                    min_confidence=sahi_cfg.get("min_confidence", 0.4),
                    journal=journal,
                )
                # Merge sahi tickers into extracted_map.
                # Rule: do not overwrite a Telegram ticker with a sahi ticker of
                # equal or lower confidence. Only upgrade to sahi when its
                # confidence is strictly higher.
                for tk in sahi_extracted:
                    tk.source_platform = "sahi"
                    existing = extracted_map.get(tk.symbol)
                    existing_conf = getattr(existing, "confidence", 0.0)
                    existing_src = getattr(existing, "source_platform", "")
                    if existing is None:
                        extracted_map[tk.symbol] = tk
                    elif tk.confidence > existing_conf and existing_src != "telegram":
                        extracted_map[tk.symbol] = tk
                    elif tk.confidence > existing_conf + 0.1:
                        extracted_map[tk.symbol] = tk
                    # else: keep existing ticker (preserves telegram-sourced ones)
                logging.getLogger(__name__).info(
                    "Sahi.com: %d headlines → %d symbols (merged into %d total)",
                    len(sahi_headlines), len(sahi_extracted), len(extracted_map),
                )
        except Exception as e:
            logging.getLogger(__name__).warning("Sahi.com pipeline failed: %s", e)

    extracted = list(extracted_map.values())
    if not extracted:
        return {"messages": len(messages), "extracted": 0, "verdicts": 0}

    # Filter extracted items to process only NEW news events/messages (incremental fusion)
    new_extracted = []
    for tk in extracted:
        news_ev = getattr(tk, "news_event", "") or getattr(tk, "context", "")
        msg_id = getattr(tk, "telegram_msg_id", None)
        src_plat = getattr(tk, "source_platform", "")
        if not journal.has_recent_verdict(tk.symbol, news_event=news_ev, msg_id=msg_id, max_age_hours=24, source_platform=src_plat):
            new_extracted.append(tk)
        else:
            logging.getLogger(__name__).info(
                "Skipping duplicate verdict pass for %s (news event already processed in last 24h)", tk.symbol
            )

    if not new_extracted:
        logging.getLogger(__name__).info("No new un-processed news events found for fusion scan.")
        return {"messages": len(messages), "extracted": len(extracted), "verdicts": 0, "status": "no_new_items"}

    # 3. Regime context
    try:
        regime_snap = classify_regime("NIFTY")
        regime_val = regime_snap.regime.value if hasattr(regime_snap.regime, "value") else str(regime_snap.regime)
    except Exception as e:
        logging.getLogger(__name__).warning("regime classify failed: %s", e)
        regime_val = "UNKNOWN"

    # 4. Hard filters + LLM fusion
    fusion_cfg = tp_cfg.get("fusion", {})
    logging.getLogger(__name__).info(
        "Running fusion on %d new symbols (total candidates: %d)...",
        len(new_extracted), len(extracted),
    )
    verdicts = run_fusion(
        extracted=new_extracted,
        fundamentals_fn=lambda s, c="": fetch_fundamentals(
            s,
            journal=journal if not dry_run else None,
            cache_ttl_hours=tp_cfg.get("screener", {}).get("cache_ttl_hours", 24),
            rate_limit_delay=tp_cfg.get("screener", {}).get("rate_limit_delay", 2.0),
            company_name=c,
            call_llm=call_llm,
        ),
        filters=fusion_cfg.get("hard_filters", {}),
        regime=regime_val,
        call_llm=call_llm,
        model=fusion_cfg.get("model", "llama-3.3-70b-versatile"),
        max_tokens=fusion_cfg.get("max_tokens", 2048),
        source_platform="mixed",
    )

    # Attach Telegram / Sahi source metadata
    for v in verdicts:
        src = extracted_map.get(v.symbol)
        if src is not None:
            v.telegram_msg_id = getattr(src, "telegram_msg_id", None)
            v.telegram_channel = getattr(src, "telegram_channel", None)
            v.source_platform = getattr(src, "source_platform", "telegram")

    if dry_run:
        summary = {
            "dry_run": True,
            "messages": len(messages),
            "extracted": len(extracted),
            "verdicts": len(verdicts),
            "sahi_headlines": len(sahi_extracted),
            "symbols": [v.symbol for v in verdicts],
            "details": [
                {
                    "symbol": v.symbol,
                    "verdict": v.verdict,
                    "confidence": v.confidence,
                    "news_event": getattr(v, "news_event", ""),
                    "event_type": getattr(v, "event_type", ""),
                    "sentiment": getattr(v, "sentiment_direction", ""),
                    "company": getattr(v, "company_name", ""),
                    "source": getattr(v, "source_platform", "telegram"),
                }
                for v in verdicts
            ],
        }
        logging.getLogger(__name__).info(
            "DRY-RUN complete: %d messages → %d extracted → %d verdicts (total LLM calls: ~%d)",
            len(messages), len(extracted), len(verdicts), llm_calls + len(extracted),
        )
        return summary

    # 5. Persist
    scan_id = uuid.uuid4().hex
    scan_ts = datetime.now(timezone.utc).isoformat()
    verdict_rows = []
    seen_symbols: dict[str, dict] = {}
    for v in verdicts:
        row = {
            "symbol": v.symbol,
            "verdict": v.verdict,
            "confidence": v.confidence,
            "rationale": v.rationale,
            "key_risks": v.key_risks,
            "entry_zone": v.entry_zone,
            "stop_loss": v.stop_loss,
            "target": v.target,
            "telegram_msg_id": getattr(v, "telegram_msg_id", None),
            "telegram_channel": getattr(v, "telegram_channel", None),
            "fundamentals_json": getattr(v, "fundamentals_json", None),
            "regime_at_scan": getattr(v, "regime_at_scan", None),
            "news_event": getattr(v, "news_event", ""),
            "event_type": getattr(v, "event_type", "general"),
            "sentiment_direction": getattr(v, "sentiment_direction", "NEUTRAL"),
            "company_name": getattr(v, "company_name", ""),
            "source_platform": getattr(v, "source_platform", "telegram"),
        }
        existing = seen_symbols.get(v.symbol)
        if existing is None or v.confidence > existing["confidence"]:
            seen_symbols[v.symbol] = row
    verdict_rows = list(seen_symbols.values())
    journal.save_investment_verdicts(scan_id, scan_ts, verdict_rows)
    journal.deduplicate_investment_verdicts()

    # 5b. Feed actionable stock verdicts to paper trading engine
    try:
        from dashboard import paper_trader as pt
        actionable_alerts = []
        for v in verdicts:
            v_str = str(getattr(v, "verdict", "")).upper()
            if any(k in v_str for k in ("BUY", "LONG", "ACCUMULATE")):
                direction = "LONG"
            elif any(k in v_str for k in ("SELL", "SHORT", "REDUCE")):
                direction = "SHORT"
            else:
                continue

            actionable_alerts.append({
                "symbol": v.symbol,
                "direction": direction,
                "confidence": getattr(v, "confidence", "MEDIUM"),
                "source_regime": getattr(v, "regime_at_scan", regime_val),
                "flow_bias": getattr(v, "sentiment_direction", "NEUTRAL"),
                "type": "STOCK",
                "entry_price": getattr(v, "entry_zone", None),
                "stop": getattr(v, "stop_loss", None),
                "target": getattr(v, "target", None),
                "entry_trigger": f"Telegram Verdict: {v.verdict}",
            })
        if actionable_alerts:
            entered_trades = pt.auto_enter_from_alerts(actionable_alerts, cfg=cfg)
            if entered_trades:
                print(f"\033[93m[{datetime.now().strftime('%H:%M:%S')}] [Telegram Pipeline] [+ ] Auto-entered {len(entered_trades)} stock paper trades: {', '.join(t['symbol'] for t in entered_trades)}\033[0m")
    except Exception as pt_err:
        logging.getLogger(__name__).warning("Failed to auto-enter Telegram paper trades: %s", pt_err)

    # 6. Prune old scans
    inv_cfg = cfg.get("investment_dashboard", {})
    keep = inv_cfg.get("max_history_scans", 50)
    journal.prune_investment_scans(keep)
    pruned = journal.prune_investment_verdicts(max_age_hours=24)
    if pruned:
        logging.getLogger(__name__).info("Pruned %d old/Avoid verdicts", pruned)

    # 7. Alerts are surfaced on the Investment dashboard page only
    #    (localhost-only deployment — no outbound Telegram alerts are sent).
    alerts_sent = 0

    logging.getLogger(__name__).info(
        "Pipeline complete: %d messages → %d extracted → %d verdicts (scan_id=%s)",
        len(messages), len(extracted), len(verdicts), scan_id,
    )
    print(f"\033[92m[{datetime.now().strftime('%H:%M:%S')}] [Telegram Pipeline] COMPLETE: {len(messages)} messages -> {len(extracted)} extracted -> {len(verdicts)} verdicts (scan_id: {scan_id[:8]}).\033[0m")

    return {
        "scan_id": scan_id,
        "messages": len(messages),
        "extracted": len(extracted),
        "verdicts": len(verdicts),
        "alerts_sent": alerts_sent,
    }


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

    # Dual-engine Telegram pipeline — hourly at :05 (additive, unique job id).
    tp_cfg = cfg.get("telegram_pipeline", {})
    if tp_cfg.get("enabled", False):
        sched.add_job(
            _h("telegram-pipeline", lambda c: run_telegram_pipeline(c)),
            CronTrigger(minute=5),
            id="telegram_pipeline",
            replace_existing=True,
        )
        print("Telegram pipeline scheduled hourly at :05 (enabled).")
    else:
        print("Telegram pipeline NOT scheduled (telegram_pipeline.enabled=false).")

    # Sahi.com news — hourly at :05, independent of telegram pipeline.
    sahi_cfg = cfg.get("sahi_news", {})
    if sahi_cfg.get("enabled", True):
        def _run_sahi_only(c):
            from data.sahi_news import run_sahi_pipeline
            run_sahi_pipeline(c)
        sched.add_job(
            _h("sahi-news", _run_sahi_only),
            CronTrigger(minute=5),
            id="sahi_news",
            replace_existing=True,
        )
        print("Sahi.com news scheduled hourly at :05 (enabled).")
    else:
        print("Sahi.com news NOT scheduled (sahi_news.enabled=false).")

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
    dry_run = "--dry-run" in sys.argv[2:]
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
    elif cmd == "telegram-pipeline":
        run_telegram_pipeline(cfg, dry_run=dry_run)
    elif cmd in ("investment-dashboard", "investment-page"):
        import subprocess
        print("🧠 Launching lightweight Investment Dashboard server...")
        try:
            subprocess.run([sys.executable, "dashboard/server.py", "--investment-only"])
        except KeyboardInterrupt:
            print("\nServer stopped.")
    else:
        print(f"unknown command: {cmd}")
        return 2
    return 0


if __name__ == "__main__":
    import io
    if sys.stdout.encoding != 'utf-8':
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.exit(main())
