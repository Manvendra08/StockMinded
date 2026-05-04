"""StockMinded — daily decision engine.

Usage:
  python main.py dashboard          # run the 4-signal dashboard now
  python main.py schedule           # run APScheduler loop (IST times from config)
  python main.py health             # quick data connectivity check
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime

from config.loader import load_config, load_universe
from data import feed
from signals import regime as regime_mod
from signals import flows as flows_mod
from signals import leadership as lead_mod
from signals import structure_map as sm
from ops.alerts import Alerter, format_dashboard
from ops.journal import Journal


def run_dashboard(cfg: dict) -> None:
    alerter = Alerter(cfg["alerts"].get("telegram_bot_token"), cfg["alerts"].get("telegram_chat_id"))
    journal = Journal(cfg["paths"]["journal_db"])

    universe = load_universe(cfg)
    sectors = cfg["sectors"]

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
    longs, shorts = lead_mod.a_grade(ranks, inflow_sectors=inflow_syms, sector_map=None)

    structure = sm.plan_for(regime_snap.regime)

    journal.log_regime(regime_snap.to_dict())
    journal.log_flow(flow_snap.to_dict())

    msg = format_dashboard(regime_snap, flow_snap, structure, longs, shorts)
    alerter.send(msg)


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
        # Use the cached version which now raises RuntimeError on total failure
        pcr_oi, pcr_vol, mp, stale, _, updated_at, _ = feed.get_pcr_max_pain_cached("NIFTY")
        checks.append(f"✅ PCR (OI): {pcr_oi} (Updated: {updated_at})")
    except Exception as e:
        checks.append(f"❌ Option chain/PCR feed: {e}")

    try:
        df = feed.fii_dii_cash(days=3)
        checks.append(f"✅ FII/DII: {len(df)} rows")
    except Exception as e:
        checks.append(f"❌ FII/DII: {e}")

    print("\n".join(checks))


def run_schedule(cfg: dict) -> None:
    import signal
    import sys
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger
    from pytz import timezone

    ist = timezone("Asia/Kolkata")
    sched = BlockingScheduler(timezone=ist)

    s = cfg["schedule_ist"]

    def _h(name, fn):
        def wrapped():
            try:
                fn(cfg)
            except Exception as e:
                print(f"[{name}] failed: {e}")
                traceback.print_exc()
        return wrapped

    hh, mm = map(int, s["morning_dashboard"].split(":"))
    sched.add_job(_h("dashboard", run_dashboard), CronTrigger(hour=hh, minute=mm, day_of_week="mon-fri"))
    print(f"Scheduler started (IST). Morning dashboard @ {s['morning_dashboard']}.  Ctrl-C to stop.")
    
    # Add graceful shutdown handler
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
