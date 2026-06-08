r"""StockMinded visual dashboard -- Flask server.

Run:  .venv312\Scripts\python dashboard/server.py
Open: http://localhost:5050
"""
from __future__ import annotations

import json
import os
import sys
import sqlite3
import traceback
from dataclasses import asdict
from datetime import datetime, date, timezone, timedelta
import datetime as dt_mod
from pathlib import Path

# Ensure project root is on sys.path so signal imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from flask import Flask, jsonify, request, send_from_directory
import logging
import numpy as np
import pandas as pd

from config.loader import load_config, load_universe
from data import feed
from signals import regime as regime_mod
from signals import flows as flows_mod
from signals import leadership as lead_mod
from signals import structure_map as sm
from signals import verdict as verdict_mod
from ops.alerts import Alerter
from ops.journal import Journal

app = Flask(__name__, static_folder=str(Path(__file__).parent))
app.json.ensure_ascii = False  # Allow native UTF-8 (like Rupee symbol) in JSON responses

# -- cache in memory so refresh is instant after first load --------
_cache: dict = {}
_cache_ts: datetime | None = None


def _load_journal_trade_rows() -> list[dict]:
    """Load stock trades from the SQLite journal."""
    cfg = load_config()
    db_path = cfg["paths"]["journal_db"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM trades ORDER BY id ASC").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _journal_trade_to_ui_trade(row: dict) -> dict:
    opened_at = str(row.get("opened_at") or "")
    closed_at = str(row.get("closed_at") or "")
    side = str(row.get("side") or "LONG").upper()
    entry = row.get("entry")
    exit_price = row.get("exit_price")
    qty = int(row.get("qty") or 0)
    pnl = row.get("pnl_rupees")
    pnl_pct = None
    if pnl is None and entry not in (None, 0) and exit_price is not None:
        if side == "SHORT":
            pnl = round((float(entry) - float(exit_price)) * qty, 2)
            pnl_pct = round(100 * (float(entry) - float(exit_price)) / float(entry), 2)
        else:
            pnl = round((float(exit_price) - float(entry)) * qty, 2)
            pnl_pct = round(100 * (float(exit_price) - float(entry)) / float(entry), 2)
    elif pnl is not None and entry not in (None, 0) and exit_price is not None and qty:
        pnl_pct = round((float(pnl) / (float(entry) * qty)) * 100, 2)

    return {
        "id": row.get("id"),
        "symbol": row.get("symbol"),
        "direction": "SHORT" if side == "SHORT" else "LONG",
        "entry_price": entry,
        "exit_price": exit_price,
        "qty": qty,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "status": "CLOSED" if closed_at else "OPEN",
        "exit_reason": "CLOSED" if closed_at else "OPEN",
        "confidence": "MEDIUM",
        "entry_date": opened_at[:10] if opened_at else None,
        "entry_time": opened_at.replace("T", " ")[:19] if opened_at else None,
        "exit_time": closed_at.replace("T", " ")[:19] if closed_at else None,
        "structure": row.get("structure"),
        "notes": row.get("notes"),
        "source": "journal",
        "risk_rupees": row.get("risk_rupees"),
        "sl_price": row.get("stop"),
        "tgt_price": row.get("target"),
    }


def _merged_paper_trades(limit: int = 100) -> list[dict]:
    """Return stock journal trades plus paper-trader trades in one feed."""
    trades = []
    try:
        journal_rows = _load_journal_trade_rows()
        trades.extend(_journal_trade_to_ui_trade(row) for row in journal_rows)
    except Exception:
        traceback.print_exc()
    try:
        trades.extend(pt.get_all_trades(limit=limit))
    except Exception:
        traceback.print_exc()

    trades.sort(key=lambda t: str(t.get("entry_time") or t.get("entry_date") or ""))
    return list(reversed(trades[-limit:]))


def _merged_open_trades() -> list[dict]:
    """Return all open stock and paper trades."""
    trades = []
    try:
        journal_rows = [row for row in _load_journal_trade_rows() if not row.get("closed_at")]
        trades.extend(_journal_trade_to_ui_trade(row) for row in journal_rows)
    except Exception:
        traceback.print_exc()
    try:
        trades.extend(pt.get_open_trades())
    except Exception:
        traceback.print_exc()
    return trades


def _market_status_now() -> dict:
    """Compute live market status — never cached."""
    from datetime import timezone, timedelta
    ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    is_weekday = ist_now.weekday() < 5
    tt = ist_now.time()
    market_open = (
        is_weekday
        and (tt.hour, tt.minute) >= (9, 15)
        and (tt.hour, tt.minute) <= (15, 30)
    )
    return {
        "ts": ist_now.strftime("%Y-%m-%d %H:%M:%S IST"),
        "market_open": market_open,
        "market_status": "OPEN" if market_open else ("WEEKEND" if not is_weekday else "CLOSED"),
        "session_date": ist_now.strftime("%Y-%m-%d"),
    }


def _run_engine() -> dict:
    """Execute the full 4-signal pipeline and return a JSON-safe dict."""
    global _cache, _cache_ts

    # Re-use if less than 2 min old
    if _cache_ts and (datetime.now() - _cache_ts).total_seconds() < 120:
        # Always refresh live fields so UI never shows stale ts / market flag.
        return {**_cache, **_market_status_now()}

    cfg = load_config()
    universe = load_universe(cfg)
    sectors = cfg["sectors"]

    source_errors = []
    
    # Fetch stock data with error tracking
    try:
        stock_data = feed.universe_ohlc(universe, period="6mo")
    except Exception as e:
        stock_data = {}
        source_errors.append(f"Stock feed failed: {e}")
        
    # Fetch sector data with error tracking
    try:
        sector_data = feed.sector_ohlc(sectors, period="6mo")
    except Exception as e:
        sector_data = {}
        source_errors.append(f"Sector feed failed: {e}")

    regime_snap = regime_mod.classify("NIFTY", stock_universe_data=stock_data)
    try:
        flow_snap = flows_mod.snapshot(sector_data, index_symbol="NIFTY")
        if getattr(flow_snap, "notes", ""):
            source_errors.append(flow_snap.notes)
    except Exception as e:
        # Create a dummy flow_snap so the rest of the engine can continue
        from signals.flows import FlowSnapshot
        flow_snap = FlowSnapshot(
            fii_dii_5d_net_cr={"fii": 0.0, "dii": 0.0},
            top_inflow_sectors=[],
            top_outflow_sectors=[],
            pcr_oi=None,
            pcr_vol=None,
            max_pain=None,
            smart_money_bias="NEUTRAL"
        )
        source_errors.append(f"Option chain feed failed: {e}")

    bench = feed.ohlc_cached("NIFTY", period="1y")
    ranks = lead_mod.rank_universe(stock_data, bench)
    inflow_syms = [s for s, _ in flow_snap.top_inflow_sectors]
    longs, shorts = lead_mod.a_grade(ranks, inflow_sectors=inflow_syms, sector_map=None)

    structure = sm.plan_for(regime_snap.regime)

    # NIFTY close for header
    try:
        nifty_df = feed.ohlc_cached("NIFTY", period="1mo")
        if not nifty_df.empty:
            nifty_df = nifty_df.dropna(subset=["close"])
    except Exception as e:
        nifty_df = pd.DataFrame()
        source_errors.append(f"Nifty feed failed: {e}")
        
    nifty_close = float(nifty_df["close"].iloc[-1]) if not nifty_df.empty else 0
    nifty_prev = float(nifty_df["close"].iloc[-2]) if len(nifty_df) >= 2 else nifty_close
    nifty_chg_pct = round(100 * (nifty_close - nifty_prev) / nifty_prev, 2) if nifty_prev else 0

    # BankNifty
    try:
        bn_df = feed.ohlc_cached("BANKNIFTY", period="1mo")
        if not bn_df.empty:
            bn_df = bn_df.dropna(subset=["close"])
    except Exception as e:
        bn_df = pd.DataFrame()
        source_errors.append(f"BankNifty feed failed: {e}")
        
    bn_close = float(bn_df["close"].iloc[-1]) if not bn_df.empty else 0
    bn_prev = float(bn_df["close"].iloc[-2]) if len(bn_df) >= 2 else bn_close
    bn_chg_pct = round(100 * (bn_close - bn_prev) / bn_prev, 2) if bn_prev else 0

    # Risk params
    risk_cfg = cfg.get("risk", {})
    account_cfg = cfg.get("account", {})

    # Compute data freshness based on cache file ages
    import time
    cache_dir = Path("data/cache/ohlc")
    today_str = datetime.now().strftime("%Y-%m-%d")
    max_age_secs = 0
    checked = 0
    if cache_dir.exists():
        for t in universe[:10]:  # Spot check first 10 tickers
            p = cache_dir / f"{t}_{today_str}.pkl"
            if p.exists():
                age = time.time() - p.stat().st_mtime
                if age > max_age_secs:
                    max_age_secs = age
                checked += 1
                
    market_now = _market_status_now()
    if checked == 0:
        freshness_status = "MISSING"
    elif market_now["market_open"]:
        freshness_status = "LIVE" if max_age_secs < 900 else ("STALE" if max_age_secs < 3600 else "OLD")
    else:
        freshness_status = "EOD"
    data_freshness = {
        "status": freshness_status,
        "max_age_minutes": round(max_age_secs / 60, 1),
        "cache_files_checked": checked
    }

    status = market_now
    last_trading_date = None
    if not nifty_df.empty:
        try:
            last_idx = nifty_df.index[-1]
            last_trading_date = last_idx.strftime("%Y-%m-%d") if hasattr(last_idx, "strftime") else str(last_idx)[:10]
        except Exception:
            last_trading_date = None

    result = {
        "ts": status["ts"],
        "market_open": status["market_open"],
        "market_status": status["market_status"],
        "session_date": status.get("session_date"),
        "last_trading_date": last_trading_date,
        "data_freshness": data_freshness,
        "source_errors": source_errors,
        "nifty": {"close": round(nifty_close, 2), "change_pct": nifty_chg_pct},
        "banknifty": {"close": round(bn_close, 2), "change_pct": bn_chg_pct},
        "regime": {
            "name": regime_snap.regime.value,
            "trend_score": regime_snap.trend_score,
            "vix": regime_snap.vix,
            "vix_5d_change_pct": regime_snap.vix_5d_change_pct,
            "adx": regime_snap.adx,
            "breadth_pct_above_50dma": regime_snap.breadth_pct_above_50dma,
            "notes": regime_snap.notes,
        },
        "flows": {
            "fii_dii_5d": flow_snap.fii_dii_5d_net_cr,
            "top_inflow": flow_snap.top_inflow_sectors,
            "top_outflow": flow_snap.top_outflow_sectors,
            "pcr_oi": flow_snap.pcr_oi,
            "pcr_vol": flow_snap.pcr_vol,
            "max_pain": flow_snap.max_pain,
            "bias": flow_snap.smart_money_bias,
            "pcr_stale": flow_snap.pcr_stale,
            "mp_stale": flow_snap.mp_stale,
            "pcr_updated_at": flow_snap.pcr_updated_at,
            "mp_updated_at": flow_snap.mp_updated_at,
            "notes": getattr(flow_snap, "notes", ""),
            "option_source": getattr(flow_snap, "option_source", None),
            "ai_sentiment": getattr(flow_snap, "ai_sentiment", None),
            "fii_derivatives_5d": getattr(flow_snap, "fii_derivatives_5d", {}),
            "fii_derivatives_stale": getattr(flow_snap, "fii_derivatives_stale", False),
        },
        "leaders": [
            {"symbol": r.symbol, "rs_slope": max(-150.0, min(150.0, r.rs_slope_20d)), "pct_vs_50dma": r.pct_vs_50dma, "quintile": r.quintile}
            for r in longs[:6]
        ],
        "laggards": [
            {"symbol": r.symbol, "rs_slope": max(-150.0, min(150.0, r.rs_slope_20d)), "pct_vs_50dma": r.pct_vs_50dma, "quintile": r.quintile}
            for r in shorts[:6]
        ],
        "all_ranks": [
            {"symbol": r.symbol, "rs_slope": max(-150.0, min(150.0, r.rs_slope_20d)), "pct_vs_50dma": r.pct_vs_50dma, "quintile": r.quintile, "above_50dma": r.above_50dma}
            for r in sorted(ranks, key=lambda x: (-x.quintile, -x.rs_slope_20d))
        ],
        "sector_rs": flow_snap.top_inflow_sectors + flow_snap.top_outflow_sectors,
        "structure": {
            "primary": structure.primary,
            "secondary": structure.secondary,
            "notes": structure.notes,
        },
        "risk": {
            "capital": account_cfg.get("capital", 0),
            "per_trade_pct": risk_cfg.get("per_trade_pct", 0),
            "daily_stop_pct": risk_cfg.get("daily_stop_pct", 0),
            "monthly_stop_pct": risk_cfg.get("monthly_stop_pct", 0),
            "margin_util_cap": risk_cfg.get("margin_util_cap", 0),
        },
    }
    # Issue #6 callsite fix: compute iv_rank and pass it so naked-sell gate works
    _iv_rank_for_verdict = None
    try:
        from signals.options import iv_rank as _iv_rank_fn, chain_snapshot as _chain_snap, atm_iv as _atm_iv
        _db_path = cfg.get("options", {}).get("iv_history_db", "./data/iv_history.sqlite")
        _chain = _chain_snap("NIFTY")
        _spot  = nifty_close
        if not _chain.empty and _spot > 0:
            _iv_rank_for_verdict = _iv_rank_fn("NIFTY", _atm_iv(_chain, _spot), _db_path)
    except Exception as e:
        logging.getLogger(__name__).exception("Failed computing iv_rank for verdict: %s", e)

    # Compute verdict using FULL data before slicing leaders/laggards for UI
    result_for_verdict = {
        **result,
        "leaders": [{"quintile": r.quintile, "symbol": r.symbol} for r in longs],
        "laggards": [{"quintile": r.quintile, "symbol": r.symbol} for r in shorts],
        "iv_rank": _iv_rank_for_verdict
    }
    result["verdict"] = verdict_mod.build_trade_verdict(result_for_verdict).to_dict()

    # --- Skip Reasons (Today's) ---
    try:
        journal = Journal(cfg["paths"]["journal_db"])
        ist = timezone(timedelta(hours=5, minutes=30))
        now_ist = datetime.now(ist)
        # Shift logical day so that 'today' rolls over at 6:00 AM IST instead of midnight
        logical_date = (now_ist - timedelta(hours=6)).date()
        start_ist = datetime.combine(logical_date, dt_mod.time(6, 0), tzinfo=ist)
        start_utc = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
        skip_rows = journal.get_skipped_trades(limit=50, since_date=start_utc.isoformat())

        by_reason: dict[str, int] = {}
        by_gate: dict[str, int] = {}
        for row in skip_rows:
            reason = row.get("skip_reason") or "UNKNOWN"
            gate = row.get("risk_gate") or "UNKNOWN"
            by_reason[reason] = by_reason.get(reason, 0) + 1
            by_gate[gate] = by_gate.get(gate, 0) + 1

        result["skips"] = {
            "today": skip_rows[:10],
            "summary": {
                "total": len(skip_rows),
                "by_reason": by_reason,
                "by_gate": by_gate,
            }
        }
    except Exception as e:
        result["skips"] = {"today": [], "summary": {"total": 0, "by_reason": {}, "by_gate": {}}, "error": str(e)}

    # --- Verdict Engine Trace ---
    verdict = result["verdict"]
    flows = result.get("flows", {})
    regime = result.get("regime", {})
    freshness = result.get("data_freshness", {})
    result["verdict_trace"] = {
        "inputs": {
            "regime": regime.get("name") or regime.get("regime"),
            "trend_score": regime.get("trend_score"),
            "adx": regime.get("adx"),
            "vix": regime.get("vix"),
            "breadth_pct_above_50dma": regime.get("breadth_pct_above_50dma"),
            "pcr_oi": flows.get("pcr_oi"),
            "max_pain": flows.get("max_pain"),
            "pcr_stale": flows.get("pcr_stale"),
            "mp_stale": flows.get("mp_stale"),
            "data_freshness_status": freshness.get("status"),
            "bias": flows.get("bias"),
            "source_errors": result.get("source_errors", []),
        },
        "blocks": verdict.get("blocks", []),
        "reasons": verdict.get("reasons", []),
        "action": verdict.get("action"),
        "strategy": verdict.get("strategy"),
        "confidence": verdict.get("confidence"),
    }

    def _make_safe(obj):
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating, float)):
            val = float(obj)
            import math
            if math.isnan(val) or math.isinf(val):
                return None
            return val
        if isinstance(obj, (np.bool_,)):
            return bool(obj)
        if isinstance(obj, (list, tuple)):
            return [_make_safe(x) for x in obj]
        if isinstance(obj, dict):
            return {k: _make_safe(v) for k, v in obj.items()}
        return obj

    # Tag the heavy signal computation time so the UI can show freshness
    # distinct from wall-clock ts (which updates every request).
    result["signals_computed_at"] = status["ts"]
    result = _make_safe(result)
    _cache = result
    _cache_ts = datetime.now()
    return result


# -- Trade Alert Generation ----------------------------------------

def _generate_trade_alerts(data: dict) -> list[dict]:
    """Generate structured, actionable trade objects based on signal evidence.
    
    Returns list of trade dicts with:
      symbol, direction, entry_trigger, entry_price, stop, target1, target2,
      trail_rule, qty, risk_rupees, confidence, no_trade_reason, evidence
    """
    alerts = []
    regime = data.get("regime", {})
    flows = data.get("flows", {})
    leaders = data.get("leaders", [])
    laggards = data.get("laggards", [])
    risk = data.get("risk", {})
    nifty = data.get("nifty", {})
    banknifty = data.get("banknifty", {})

    regime_name = str(regime.get("name") or regime.get("regime") or "")
    bias = flows.get("bias", "NEUTRAL")
    capital = risk.get("capital", 7000000)
    per_trade_risk_pct = risk.get("per_trade_pct", 0.0075)
    
    nifty_px = nifty.get("close", 0)
    nifty_chg = nifty.get("change_pct", 0)
    bn_px = banknifty.get("close", 0)
    bn_chg = banknifty.get("change_pct", 0)
    pcr = flows.get("pcr_oi")
    max_pain = flows.get("max_pain")
    vix = regime.get("vix", 0)
    breadth = regime.get("breadth_pct_above_50dma", 0)
    trend_score = regime.get("trend_score", 0)
    trade_verdict = data.get("verdict") or verdict_mod.build_trade_verdict(data).to_dict()
    # --- VERDICT EXTRACTION ---
    stock_v = trade_verdict.get("stock", {})
    nifty_v = trade_verdict.get("nifty", {})
    
    stock_action = stock_v.get("action", "WAIT")
    nifty_action = nifty_v.get("action", "WAIT")
    
    can_trade_equity = bool(stock_v.get("can_trade"))
    can_trade_options = bool(nifty_v.get("can_trade"))

    # Derive verdict_action for backward compatibility in filters
    if can_trade_equity:
        verdict_action = stock_action
    elif can_trade_options:
        verdict_action = nifty_action
    else:
        verdict_action = "WAIT"

    # --- PRO FILTERS & GATING ---
    allow_longs = can_trade_equity and stock_action in ("LONG_ONLY", "LONG_AND_SHORT")
    allow_shorts = can_trade_equity and stock_action in ("SHORT_ONLY", "LONG_AND_SHORT")
    
    # 2. Time filter: avoid entries after 14:45 to reduce EOD churn
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    if (now_ist.hour, now_ist.minute) >= (14, 45):
        allow_longs = False
        allow_shorts = False
        can_trade_options = False
    
    # 2. VIX Filter
    if vix > 24:
        allow_longs = False
        allow_shorts = False
        alerts.append({
            "symbol": "NIFTY", "direction": "AVOID", "entry_trigger": "VIX > 24",
            "entry_price": None, "stop": None, "target1": None, "target2": None,
            "trail_rule": "", "qty": 0, "risk_rupees": 0, "confidence": "HIGH",
            "no_trade_reason": "Extreme volatility (VIX > 24). Stay flat.",
            "evidence": [f"VIX: {vix:.1f}", f"Regime: {regime_name}"]
        })
        return alerts

    if verdict_action in ("WAIT", "NO_TRADE_DATA_STALE") and not can_trade_equity and not can_trade_options:
        alerts.append({
            "symbol": "NIFTY", "direction": "AVOID", "entry_trigger": verdict_action,
            "entry_price": None, "stop": None, "target1": None, "target2": None,
            "trail_rule": "", "qty": 0, "risk_rupees": 0, "confidence": trade_verdict.get("confidence", "LOW"),
            "no_trade_reason": trade_verdict.get("strategy", "No clean edge."),
            "evidence": trade_verdict.get("reasons", []) + trade_verdict.get("blocks", [])
        })
        return alerts

    # --- NIFTY OPTIONS ALERTS ---
    if can_trade_options:
        if nifty_action == "OPTION_SELL_DEFINED_RISK" and regime_name in ("RANGE_LOW_VOL", "RANGE_HIGH_VOL", "VOL_CONTRACTION"):
            if max_pain:
                alerts.append({
                    "symbol": "NIFTY", "direction": "NEUTRAL", "entry_trigger": f"Iron Condor @ Max Pain {max_pain:.0f}",
                    "entry_price": nifty_px, "stop": "Defined Risk", "target1": "Theta Decay", "target2": None,
                    "trail_rule": "Adjust wings if breached", "qty": 50, "risk_rupees": round(nifty_px * 0.01 * 50, 2),
                    "confidence": "MEDIUM", "no_trade_reason": None,
                    "evidence": [f"Regime: {regime_name}", f"PCR: {pcr}", f"Max Pain: {max_pain}"],
                    "verdict_action": "OPTION_SELL_DEFINED_RISK"
                })
        elif nifty_action == "NAKED_OPTION_SELL":
            direction = "LONG" if (nifty_v.get("tone") == "bull" or bias == "LONG") else "SHORT"
            side = "PUTS" if direction == "LONG" else "CALLS"
            alerts.append({
                "symbol": "NIFTY", "direction": direction, "entry_trigger": f"Naked {side} Sell ({nifty_v.get('confidence')} Conf)",
                "entry_price": nifty_px, "stop": "20% Premium SL", "target1": "80% Premium Decay", "target2": None,
                "trail_rule": "Trail SL to cost after 50% decay", "qty": 50, "risk_rupees": 5000,
                "confidence": nifty_v.get("confidence", "MEDIUM"), "no_trade_reason": None,
                "evidence": [f"Regime: {regime_name}", f"Trend: {trend_score}", f"Bias: {bias}"],
                "verdict_action": "NAKED_OPTION_SELL"
            })

    # BankNifty Divergence
    if abs(bn_chg - nifty_chg) > 0.5:
        direction = "LONG" if bn_chg > nifty_chg else "SHORT"
        if (direction == "LONG" and allow_longs) or (direction == "SHORT" and allow_shorts):
            sl_dist = bn_px * 0.005
            alerts.append({
                "symbol": "BANKNIFTY", "direction": direction, "entry_trigger": f"Divergence play ({bn_chg:+.2f}% vs Nifty {nifty_chg:+.2f}%)",
                "entry_price": bn_px, "stop": round(bn_px - sl_dist if direction == "LONG" else bn_px + sl_dist, 2),
                "target1": round(bn_px + sl_dist * 2 if direction == "LONG" else bn_px - sl_dist * 2, 2), "target2": None,
                "trail_rule": "Fixed SL", "qty": 30, "risk_rupees": round(sl_dist * 30, 2),
                "confidence": "LOW", "no_trade_reason": None,
                "evidence": [f"BN Divergence: {abs(bn_chg - nifty_chg):.2f}%"]
            })

    # --- STOCK ALERTS ---
    risk_amt = capital * per_trade_risk_pct
    
    if allow_longs:
        for stock in leaders[:8]:
            sym = stock["symbol"]
            q = int(stock.get("quintile", 0))
            conf = "HIGH" if q >= 5 else ("MEDIUM" if q >= 4 else "LOW")
            alerts.append({
                "symbol": sym, "direction": "LONG", "entry_trigger": "A-Grade RS leader: pullback/breakout",
                "entry_price": None, "stop": None,
                "target1": None, "target2": None,
                "trail_rule": "Move SL to cost at T1", "qty": 0, "risk_rupees": round(risk_amt, 2),
                "confidence": conf,
                "no_trade_reason": None,
                "evidence": [f"RS Slope: {stock['rs_slope']}", f"Q: {q}", f"vs 50DMA: {stock['pct_vs_50dma']}%"]
            })

    if allow_shorts:
        for stock in laggards[:5]:
            sym = stock["symbol"]
            q = int(stock.get("quintile", 0))
            conf = "HIGH" if q >= 5 else ("MEDIUM" if q >= 4 else "LOW")
            alerts.append({
                "symbol": sym, "direction": "SHORT", "entry_trigger": "A-Grade RS laggard: bounce/breakdown",
                "entry_price": None, "stop": None,
                "target1": None, "target2": None,
                "trail_rule": "Move SL to cost at T1", "qty": 0, "risk_rupees": round(risk_amt, 2),
                "confidence": conf,
                "no_trade_reason": None,
                "evidence": [f"RS Slope: {stock['rs_slope']}", f"Q: {q}", f"vs 50DMA: {stock['pct_vs_50dma']}%"]
            })

    for alert in alerts:
        if alert.get("direction") not in ("LONG", "SHORT", "NEUTRAL"):
            continue
        alert.setdefault("planned_risk", alert.get("risk_rupees", 0))
        alert.setdefault("entry_rule", alert.get("entry_trigger", ""))
        alert.setdefault("source_regime", regime_name)
        alert.setdefault("flow_bias", bias)
        alert.setdefault("verdict_action", verdict_action)

    return alerts


def _format_telegram_alert(data: dict, alerts: list[dict]) -> str:
    """Format trade alerts for Telegram message."""
    lines = []
    ts = data.get("ts", "")
    regime = data.get("regime", {})
    flows = data.get("flows", {})
    nifty = data.get("nifty", {})

    lines.append("*STOCKMINDED TRADE ALERT*")
    lines.append(f"`{ts}`")
    lines.append("")
    lines.append(f"Regime: `{regime.get('name', '?')}`  |  Bias: `{flows.get('bias', '?')}`")
    lines.append(f"NIFTY: {nifty.get('close', 0):.0f} ({nifty.get('change_pct', 0):+.2f}%)")
    lines.append("")

    actionable = [a for a in alerts if a.get("direction") != "AVOID"]
    avoid = [a for a in alerts if a.get("direction") == "AVOID"]

    if avoid:
        lines.append("*-- NO TRADE --*")
        for a in avoid:
            lines.append(f"  ⛔ {a.get('no_trade_reason', 'Avoid')}")
        lines.append("")

    if actionable:
        lines.append("*-- ACTIONABLE TRADES --*")
        for a in actionable:
            arrow = "🟢 BUY" if a["direction"] == "LONG" else ("🔴 SELL" if a["direction"] == "SHORT" else "⚪ NEUTRAL")
            conf = a.get("confidence", "")
            lines.append(f"  {arrow} *{a['symbol']}* [{conf}]")
            lines.append(f"    Trigger: {a.get('entry_trigger', 'N/A')}")
            lines.append(f"    Entry: {a.get('entry_price', 'N/A')} | SL: {a.get('stop', 'N/A')}")
            lines.append(f"    T1: {a.get('target1', 'N/A')} | T2: {a.get('target2', 'N/A')}")
            lines.append(f"    Risk: ₹{a.get('risk_rupees', 0):,.0f} | Qty: {a.get('qty', 0)}")
            for ev in a.get("evidence", []):
                lines.append(f"    - {ev}")
            lines.append("")

    if not alerts:
        lines.append("No actionable trades right now.")
        lines.append(f"Regime `{regime.get('name', '')}` -- stay flat or wait.")

    lines.append("---")
    lines.append("_Risk: 0.75% per trade | Max 3% concurrent | Hard gates enforced_")
    return "\n".join(lines)


def _format_options_telegram_alert(trade: dict, regime_name: str, bias: str, ivr_disp: str, vix_disp: str, is_nifty: bool = False) -> str:
    """Format options trade execution details for Telegram."""
    lines = []
    header = "AUTO-EXECUTED NIFTY OPTIONS" if is_nifty else "AUTO-EXECUTED OPTIONS"
    lines.append(f"⚡ *[{header}]*")
    lines.append(f"*{trade.get('symbol')}* - `{trade.get('structure')}`")
    lines.append("")
    lines.append("*Market Context:*")
    lines.append(f"• Regime: `{regime_name}`")
    lines.append(f"• Bias: `{bias}` | IVR: `{ivr_disp}` | VIX: `{vix_disp}`")
    lines.append("")
    lines.append("*Executed Legs:*")
    for leg in trade.get("legs", []):
        side_emoji = "🟢" if leg.get("side") == "BUY" else "🔴"
        side_text = leg.get("side")
        leg_type = leg.get("type")
        strike = leg.get("strike")
        expiry = leg.get("expiry")
        qty = leg.get("qty")
        premium = leg.get("entry_premium")
        lines.append(f"  {side_emoji} {side_text} {leg_type} {strike:.0f} ({expiry}) x {qty} @ ₹{premium:.2f}")
    
    lines.append("")
    lines.append("*Position Summary:*")
    net_prem = trade.get("net_premium", 0)
    pnl_sign = "Credit" if net_prem >= 0 else "Debit"
    lines.append(f"• Net {pnl_sign}: *₹{abs(net_prem):,.0f}*")
    if "max_loss_rupees" in trade:
        max_loss = trade.get("max_loss_rupees", 0)
        lines.append(f"• Max Risk/Loss: *₹{max_loss:,.0f}*")
    return "\n".join(lines)



# -- Routes --------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(str(Path(__file__).parent), "index.html")


@app.route("/api/dashboard")
def api_dashboard():
    try:
        data = _run_engine()
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/refresh")
def api_refresh():
    global _cache_ts
    _cache_ts = None  # force re-fetch
    try:
        data = _run_engine()
        return jsonify(data)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/option-chain")
def api_option_chain():
    symbol = request.args.get("symbol", "NIFTY")
    try:
        raw = feed.option_chain(symbol)
        # Inject timestamp if the source didn't provide one (e.g. Research360)
        records = raw.get("records", {})
        if not records.get("timestamp"):
            records["timestamp"] = datetime.now().strftime("%d-%b-%Y %H:%M:%S")
        # Use default=str to handle any non-serializable edge cases
        import json as _json
        return app.response_class(
            _json.dumps(raw, default=str),
            mimetype="application/json"
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/intraday")
def api_intraday():
    """Live intraday snapshot: LTP, day OHLC, change%, volume for watchlist."""
    try:
        cfg = load_config()
        top_n = cfg.get("intraday_top_n", 30)
        universe = load_universe(cfg)
        
        if _cache and _cache.get("all_ranks"):
            ranked_syms = [r["symbol"] for r in _cache["all_ranks"]]
            top_syms = ranked_syms[:top_n]
        else:
            top_syms = universe[:top_n]
            
        instruments = [f"{s}.NS" if not s.startswith("^") and "." not in s else s for s in top_syms]
        broker_quotes = feed.quote_batch(top_syms)
        import yfinance as yf
        tickers = yf.Tickers(" ".join(instruments))
        
        # Fetch enough daily history for 20D average volume calculation
        try:
            hist_df = yf.download(" ".join(instruments), period="3mo", interval="1d", progress=False, group_by='ticker', auto_adjust=False)
        except Exception:
            hist_df = pd.DataFrame()
            
        rows = []
        for sym, raw in zip(top_syms, instruments):
            try:
                q = broker_quotes.get(sym) or {}
                if q.get("source") == "dhan_quote":
                    ltp = round(float(q["ltp"]), 2) if q.get("ltp") else None
                    open_ = round(float(q["open"]), 2) if q.get("open") else None
                    high = round(float(q["high"]), 2) if q.get("high") else None
                    low = round(float(q["low"]), 2) if q.get("low") else None
                    prev = round(float(q["prev_close"]), 2) if q.get("prev_close") else None
                    avg_vol = int(q.get("volume") or 0)
                    chg_pct = q.get("change_pct")
                else:
                    info = tickers.tickers[raw].fast_info
                    ltp   = round(float(info.last_price), 2) if hasattr(info, "last_price") and info.last_price else None
                    open_ = round(float(info.open),       2) if hasattr(info, "open")       and info.open       else None
                    high  = round(float(info.day_high),   2) if hasattr(info, "day_high")   and info.day_high   else None
                    low   = round(float(info.day_low),    2) if hasattr(info, "day_low")    and info.day_low    else None
                    prev  = round(float(info.previous_close), 2) if hasattr(info, "previous_close") and info.previous_close else None
                    avg_vol = int(info.three_month_average_volume or 0)
                    chg_pct = round(100 * (ltp - prev) / prev, 2) if ltp and prev else None
                
                # Calculate today's volume and relative volume
                today_vol = 0
                rel_vol = None
                if not hist_df.empty and raw in hist_df.columns.get_level_values(0):
                    try:
                        sym_hist = hist_df[raw]
                        if not sym_hist.empty and 'Volume' in sym_hist:
                            volumes = sym_hist['Volume'].dropna()
                            if not volumes.empty:
                                today_vol = float(volumes.iloc[-1])
                                last_date = volumes.index[-1]
                                if hasattr(last_date, "date"):
                                    last_date = last_date.date()
                                ist = timezone(timedelta(hours=5, minutes=30))
                                today_ist = datetime.now(ist).date()
                                if last_date == today_ist:
                                    today_vol *= lead_mod._projected_volume_multiplier()
                                today_vol = int(today_vol)
                                hist_20 = volumes.iloc[:-1].tail(20)
                                avg_20d = float(hist_20.mean()) if len(hist_20) >= 5 else float(avg_vol)
                                if avg_20d > 0:
                                    rel_vol = round(today_vol / avg_20d, 2)
                    except Exception as e:
                        logging.getLogger(__name__).exception("Failed computing relative volume for %s: %s", raw, e)
                        
                rows.append({"symbol": sym, "ltp": ltp, "open": open_, "high": high,
                             "low": low, "prev_close": prev, "chg_pct": chg_pct, 
                             "vol": avg_vol, "today_vol": today_vol, "rel_vol": rel_vol})
            except Exception as e:
                logging.getLogger(__name__).exception("Failed to fetch ticker data for %s: %s", sym, e)
                rows.append({"symbol": sym, "ltp": None, "open": None, "high": None,
                             "low": None, "prev_close": None, "chg_pct": None, "vol": None, "today_vol": 0, "rel_vol": None})

        # Nifty intraday candles (dynamic timeframe)
        tf = request.args.get("tf", "5m")
        tf_map = {
            "5m": {"interval": "5m", "period": "1d"},
            "15m": {"interval": "15m", "period": "5d"},
            "1h": {"interval": "60m", "period": "5d"},
            "1d": {"interval": "1d", "period": "3mo"},
            "1w": {"interval": "1wk", "period": "1y"},
            "1mo": {"interval": "1mo", "period": "2y"},
        }
        tf_cfg = tf_map.get(tf, tf_map["5m"])
        
        candles = []
        try:
            nifty_intra = feed.ohlc_cached("NIFTY", period=tf_cfg["period"], interval=tf_cfg["interval"])
            if not nifty_intra.empty:
                # Flatten MultiIndex columns from newer yfinance
                if isinstance(nifty_intra.columns, __import__('pandas').MultiIndex):
                    nifty_intra.columns = [c[0] if isinstance(c, tuple) else c for c in nifty_intra.columns]
                for ts, row in nifty_intra.iterrows():
                    candles.append({
                        "t": ts.strftime("%H:%M" if tf in ["5m", "15m", "1h"] else "%d-%m"),
                        "o": round(float(row.get("Open", row.get("open"))),  2),
                        "h": round(float(row.get("High", row.get("high"))),  2),
                        "l": round(float(row.get("Low", row.get("low"))),   2),
                        "c": round(float(row.get("Close", row.get("close"))), 2),
                    })
        except Exception as e:
            print(f"[intraday candle error] {e}")
            traceback.print_exc()

        return jsonify({
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
            "watchlist": rows,
            "nifty_candles": candles,
            "timeframe": tf,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/trade-alerts")
def api_trade_alerts():
    """Generate and return trade alerts based on current signal data."""
    try:
        data = _run_engine()
        alerts = _generate_trade_alerts(data)
        return jsonify({"ts": data.get("ts"), "alerts": alerts})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/send-alerts")
def api_send_alerts():
    """Generate trade alerts and send to Telegram."""
    try:
        data = _run_engine()
        alerts = _generate_trade_alerts(data)
        msg = _format_telegram_alert(data, alerts)

        cfg = load_config()
        token = cfg.get("alerts", {}).get("telegram_bot_token")
        chat_id = cfg.get("alerts", {}).get("telegram_chat_id")
        alerter = Alerter(token, chat_id)
        alerter.send(msg)

        return jsonify({"status": "sent", "alert_count": len(alerts), "message": msg})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/test-telegram")
def api_test_telegram():
    try:
        # Prioritize settings from paper_trades.json
        p_settings = pt.get_settings()
        cfg = load_config()

        token = p_settings.get("telegram_bot_token") or cfg.get("alerts", {}).get("telegram_bot_token", "")
        chat_id = p_settings.get("telegram_chat_id") or cfg.get("alerts", {}).get("telegram_chat_id")
        alerter = Alerter(token, chat_id)

        
        # We need to peek into ops/alerts.py for the status
        import ops.alerts as alerts_mod
        success = alerter.send("Test ping from StockMinded dashboard.")
        
        last = alerts_mod._last_send
        suffix = token[-4:] if token and len(token) >= 4 else None
        return jsonify({
            "ok": success,
            "status_code": last.get("status"),
            "telegram_description": last.get("error"),
            "token_suffix": suffix
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/status")
def api_telegram_status():
    import ops.alerts as alerts_mod
    return jsonify(alerts_mod._last_send)


import importlib.util as _ilu
_pt_spec = _ilu.spec_from_file_location("paper_trader", Path(__file__).parent / "paper_trader.py")
pt = _ilu.module_from_spec(_pt_spec)
_pt_spec.loader.exec_module(pt)

# ── Paper Trading Routes ─────────────────────────────────────────

@app.route("/paper")
def paper_page():
    return send_from_directory(str(Path(__file__).parent), "paper.html")


@app.route("/api/paper/trades")
def api_paper_trades():
    """Get all trades (open first, then recent closed)."""
    try:
        trades = _merged_paper_trades(limit=100)
        closed_trades = [t for t in trades if t.get("status") == "CLOSED"]
        winners = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
        losers = [t for t in closed_trades if (t.get("pnl") or 0) < 0]
        resolved_trades = [t for t in closed_trades if (t.get("pnl") or 0) != 0]

        stats = {
            "total_trades": len(trades),
            "open_trades": len([t for t in trades if t.get("status") == "OPEN"]),
            "cumulative_pnl": round(sum(t.get("pnl", 0) or 0 for t in closed_trades), 2),
            "overall_win_rate": round(
                len(winners)
                / max(len(resolved_trades), 1)
                * 100,
                1,
            ),
            "total_winners": len(winners),
            "total_losers": len(losers),
            "closed_trades": len(closed_trades),
            "avg_winner": round(sum(t.get("pnl", 0) or 0 for t in winners) / max(len(winners), 1), 2) if winners else 0.0,
            "avg_loser": round(sum(t.get("pnl", 0) or 0 for t in losers) / max(len(losers), 1), 2) if losers else 0.0,
        }
        return jsonify({"trades": trades, "stats": stats})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/learned-filters")
def api_paper_learned_filters():
    """Get active learned filters."""
    try:
        filters = pt.get_learned_filters()
        return jsonify({"filters": filters})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/open")
def api_paper_open():
    """Get only open trades with live P&L."""
    try:
        trades = _merged_open_trades()
        # Enrich with live unrealized P&L
        if trades:
            symbols = list(set(t["symbol"] for t in trades))
            prices = pt._get_ltp_batch(symbols)
            for t in trades:
                if t.get("legs"):
                    continue
                ltp = prices.get(t["symbol"])
                if ltp is not None and t.get("entry_price"):
                    if t.get("direction") == "SHORT":
                        t["unrealized_pnl"] = round((t["entry_price"] - ltp) * t["qty"], 2)
                        t["unrealized_pct"] = round(100 * (t["entry_price"] - ltp) / t["entry_price"], 2)
                    else:
                        t["unrealized_pnl"] = round((ltp - t["entry_price"]) * t["qty"], 2)
                        t["unrealized_pct"] = round(100 * (ltp - t["entry_price"]) / t["entry_price"], 2)
                    t["current_price"] = ltp
        return jsonify({"trades": trades})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/enter", methods=["POST"])
def api_paper_enter():
    """Manually enter a paper trade from an alert object."""
    try:
        if not pt.is_market_open():
            return jsonify({"error": "Market closed (9:15-15:30 IST, Mon-Fri)"}), 403
        alert = request.get_json()
        if not alert or "symbol" not in alert:
            return jsonify({"error": "Missing symbol in request body"}), 400
        result = pt.enter_trade(alert)
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/auto-enter")
def api_paper_auto_enter():
    """Auto-enter paper trades from generated alerts."""
    try:
        cfg = load_config()
        data = _run_engine()
        alerts = _generate_trade_alerts(data)
        # Pass config for risk guardrails
        entered = pt.auto_enter_from_alerts(alerts, cfg=cfg)
        entered_nifty_options = []
        setups = pt.get_nifty_option_setups(data, cfg)
        for s in setups:
            if not s.get("suitable") or not s.get("legs"):
                continue
            result = pt.enter_nifty_option_structure(_dict_to_setup(s), _dict_legs_to_resolved(s["legs"]), cfg)
            if "error" not in result:
                entered_nifty_options.append(result)

        return jsonify({
            "entered": entered,
            "entered_nifty_options": entered_nifty_options,
            "alert_count": len(alerts),
            "nifty_setup_count": len(setups),
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/check")
def api_paper_check():
    """Check open trades against SL/TGT/EOD and close if triggered."""
    try:
        cfg = load_config()
        data = _run_engine()
        closed = pt.check_and_close_trades()
        closed_opts = pt.check_nifty_option_exits(vix_current=data.get("regime", {}).get("vix", 15.0), cfg=cfg)
        return jsonify({"closed": closed, "count": len(closed), "closed_nifty_options": closed_opts, "nifty_option_count": len(closed_opts)})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/close/<int:trade_id>")
def api_paper_close(trade_id):
    """Manually close a specific trade."""
    try:
        result = pt.close_trade_manual(trade_id, reason="MANUAL")
        if result is None:
            return jsonify({"error": f"Trade {trade_id} not found or already closed"}), 404
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/options/close/<int:trade_id>")
def api_options_close(trade_id):
    """Manually close a specific options trade."""
    try:
        result = pt.close_option_trade_manual(trade_id, reason="MANUAL")
        if result is None:
            return jsonify({"error": f"Option trade {trade_id} not found or already closed"}), 404
        return jsonify(result)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/eod-summary")
def api_paper_eod_summary():
    """Generate EOD P&L summary and analysis."""
    try:
        target_date = request.args.get("date")
        summary = pt.generate_eod_summary(target_date)
        return jsonify(summary)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/history")
def api_paper_history():
    """Get daily summary history."""
    try:
        summaries = pt.get_daily_summaries(limit=30)
        return jsonify({"summaries": summaries})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/strategy-notes")
def api_paper_strategy_notes():
    """Get strategy correction notes."""
    try:
        notes = pt.get_strategy_notes()
        return jsonify({"notes": notes})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500
@app.route("/api/paper/skipped")
def api_paper_skipped():
    """Return skipped trade reasons for an IST date (default: today)."""
    try:
        cfg = load_config()
        journal = Journal(cfg["paths"]["journal_db"])
        limit = int(request.args.get("limit", 200))
        date_str = request.args.get("date")
        ist = timezone(timedelta(hours=5, minutes=30))

        if date_str:
            try:
                target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                return jsonify({"error": "Invalid date format. Use YYYY-MM-DD."}), 400
        else:
            target_date = datetime.now(ist).date()

        start_ist = datetime.combine(target_date, datetime.min.time(), tzinfo=ist)
        start_utc = start_ist.astimezone(timezone.utc).replace(tzinfo=None)
        rows = journal.get_skipped_trades(limit=limit, since_date=start_utc.isoformat())

        by_reason: dict[str, int] = {}
        by_gate: dict[str, int] = {}
        for row in rows:
            reason = row.get("skip_reason") or "UNKNOWN"
            gate = row.get("risk_gate") or "UNKNOWN"
            by_reason[reason] = by_reason.get(reason, 0) + 1
            by_gate[gate] = by_gate.get(gate, 0) + 1

        return jsonify({
            "date": target_date.isoformat(),
            "skipped": rows,
            "summary": {
                "total": len(rows),
                "by_reason": by_reason,
                "by_gate": by_gate,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/paper/skipped/clear", methods=["POST"])
def api_paper_skipped_clear():
    """Clear skipped trades older than N days."""
    try:
        data = request.json or {}
        days = data.get("days")
        if days is None:
            return jsonify({"error": "Missing days"}), 400
        
        cfg = load_config()
        journal = Journal(cfg["paths"]["journal_db"])
        count = journal.clear_skipped_trades(int(days))
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/api/options/structures")
def api_options_structures():
    try:
        db = pt._load_db()
        ops = db.get("option_trades", [])
        return jsonify({"option_trades": ops})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _dict_to_setup(d: dict):
    from signals.option_strategy import NiftyOptionSetup
    return NiftyOptionSetup(
        symbol=d.get("symbol", "NIFTY"),
        mode=d.get("mode", "positional"),
        strategy=d.get("strategy", ""),
        regime=d.get("regime", ""),
        bias=d.get("bias", ""),
        vix=d.get("vix", 0.0),
        vix_change_pct=d.get("vix_change_pct", 0.0),
        pcr=d.get("pcr"),
        entry_reason=d.get("entry_reason", ""),
        net_credit=d.get("net_credit", 0.0),
        max_loss_rupees=d.get("max_loss_rupees", 0.0),
        risk_pct=d.get("risk_pct", 0.0),
        breakevens=d.get("breakevens", []),
        short_strikes=d.get("short_strikes", []),
        wing_width=d.get("wing_width", 0.0),
        entry_window_ok=d.get("entry_window_ok", True),
        suitable=d.get("suitable", False),
        skip_reason=d.get("skip_reason", ""),
        exit_rules=d.get("exit_rules", {}),
    )


def _dict_legs_to_resolved(legs: list[dict]):
    from signals.option_strategy import ResolvedLeg
    from config.loader import load_config
    cfg = load_config()
    min_lots = cfg.get("nifty_options", {}).get("min_lots_per_leg", 10)
    out = []
    for leg in legs:
        qty = max(1, int(leg.get("qty", 1)))
        out.append(ResolvedLeg(
            side=leg.get("side", ""),
            type=leg.get("type", ""),
            strike=leg.get("strike", 0),
            expiry=leg.get("expiry", ""),
            lots=min_lots,
            lot_size=qty,
            premium=leg.get("premium", 0.0),
        ))
    return out


@app.route("/api/options/alerts")
def api_options_alerts():
    """Generate NIFTY option-selling setups."""
    try:
        cfg = load_config()
        data = _run_engine()
        setups = pt.get_nifty_option_setups(data, cfg)
        return jsonify({"ts": data.get("ts"), "setups": setups})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/options/auto-enter")
def api_options_auto_enter():
    """Auto-enter suitable NIFTY option setups."""
    try:
        cfg = load_config()
        data = _run_engine()
        setups = pt.get_nifty_option_setups(data, cfg)
        entered = []
        for s in setups:
            if not s.get("suitable") or not s.get("legs"):
                continue
            result = pt.enter_nifty_option_structure(_dict_to_setup(s), _dict_legs_to_resolved(s["legs"]), cfg)
            if "error" not in result:
                entered.append(result)
        return jsonify({"entered": entered, "setups": setups})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500



@app.route("/api/paper/settings", methods=["GET", "POST"])
def api_paper_settings():
    """Get or update paper trader settings."""
    try:
        if request.method == "POST":
            new_settings = request.json
            if not new_settings:
                return jsonify({"error": "Missing settings in request"}), 400
            settings = pt.save_settings(new_settings)
            return jsonify(settings)
        else:
            return jsonify(pt.get_settings())
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/export")
def api_paper_export():
    """Download trade history as CSV."""
    try:
        csv_data = pt.export_trades_to_csv()
        from flask import Response
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-disposition": f"attachment; filename=paper_trades_{date.today().isoformat()}.csv"}
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/cleanup", methods=["POST"])

def api_paper_cleanup():
    """Clean up DB with filters."""
    try:
        data = request.json or {}
        from_date = data.get("from_date")
        to_date = data.get("to_date")
        purge_churn = data.get("purge_churn", False)
        full_reset = data.get("full_reset", False)

        result = pt.cleanup_db(from_date, to_date, purge_churn, full_reset)
        return jsonify(result)

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


import threading
import time

def _automation_worker():
    """Background task to keep the engine fresh and automated."""
    start_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{start_time}] Background worker started...")
    last_engine_run = 0
    data = {} # Maintain scope for exit checks
    
    while True:
        try:
            now = time.time()
            
            market_open = pt.is_market_open()

            # 1. Run engine every 2 min and auto-enter ONLY during market hours.
            if now - last_engine_run > 120:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Engine tick (market_open={market_open})...")
                data = _run_engine()
                if market_open:
                    alerts = _generate_trade_alerts(data)
                    cfg = load_config()
                    entered = pt.auto_enter_from_alerts(alerts, cfg=cfg)
                    if entered:
                        print(f"  > Auto-entered {len(entered)} trades: {', '.join(e['symbol'] for e in entered)}")
                        # Fire Telegram alert so user is notified of auto-trades.
                        try:
                            token = cfg.get("alerts", {}).get("telegram_bot_token")
                            chat_id = cfg.get("alerts", {}).get("telegram_chat_id")
                            # Filter original alerts to only those that were actually entered
                            entered_syms = {e["symbol"] for e in entered}
                            entered_alerts = [a for a in alerts if a.get("symbol") in entered_syms]
                            msg = _format_telegram_alert(data, entered_alerts)
                            msg = "*[AUTO-EXECUTED]*\n" + msg
                            ok = Alerter(token, chat_id).send(msg)
                            if ok:
                                print(f"  > Telegram alert sent for {len(entered)} auto-trades")
                            else:
                                import ops.alerts as alerts_mod
                                print(f"  > Telegram alert failed. Last error: {alerts_mod._last_send.get('error')}")
                        except Exception as te:
                            print(f"  > Telegram send failed: {te}")

                    try:
                        setups = pt.get_nifty_option_setups(data, cfg)
                        nifty_entered = []
                        for s in setups:
                            if not s.get("suitable") or not s.get("legs"):
                                continue
                            result = pt.enter_nifty_option_structure(
                                _dict_to_setup(s), _dict_legs_to_resolved(s["legs"]), cfg
                            )
                            if "error" not in result:
                                nifty_entered.append(result)
                        if nifty_entered:
                            token = cfg.get("alerts", {}).get("telegram_bot_token")
                            chat_id = cfg.get("alerts", {}).get("telegram_chat_id")
                            regime_name = data.get("regime", {}).get("name", "")
                            bias = data.get("flows", {}).get("bias", "NEUTRAL")
                            vix = data.get("regime", {}).get("vix", 15)
                            vix_disp = f"{vix:.1f}" if vix is not None else "N/A"
                            for res in nifty_entered:
                                msg = _format_options_telegram_alert(
                                    res, regime_name, bias, "N/A", vix_disp, is_nifty=True
                                )
                                Alerter(token, chat_id).send(msg)
                    except Exception as ne:
                        print(f"  > NIFTY options automation error: {ne}")

                    # Phase 3: Options Auto-Execution
                    if cfg.get("options", {}).get("enabled"):
                        try:
                            from signals.options import iv_rank, chain_snapshot
                            from signals.option_strategy import pick_structure, resolve_legs
                            
                            underlyings = [u for u in cfg.get("options", {}).get("underlyings", ["NIFTY", "BANKNIFTY"]) if u != "NIFTY"]
                            token = cfg.get("alerts", {}).get("telegram_bot_token")
                            chat_id = cfg.get("alerts", {}).get("telegram_chat_id")
                            
                            regime_name = data.get("regime", {}).get("name", "")
                            bias = data.get("flows", {}).get("bias", "NEUTRAL")
                            vix = data.get("regime", {}).get("vix", 15)
                            db_path = cfg.get("options", {}).get("iv_history_db", "./data/iv_history.sqlite")

                            for sym in underlyings:
                                try:
                                    chain = chain_snapshot(sym)
                                    spot = (data.get("nifty", {}).get("close") if sym == "NIFTY"
                                            else data.get("banknifty", {}).get("close")) or 0
                                    if spot <= 0 or chain.empty:
                                        continue
                                    from signals.options import atm_iv
                                    current_iv = atm_iv(chain, spot)
                                    ivr = iv_rank(sym, current_iv, db_path)

                                    struct = pick_structure(regime_name, bias, ivr, vix)
                                    if struct:
                                        lot_sz = cfg.get("options", {}).get("lot_size", {}).get(sym, 50)
                                        step = cfg.get("options", {}).get("strike_step", {}).get(sym, 50)
                                        legs = resolve_legs(struct, chain, spot, lot_sz, step)
                                        if legs:
                                            res = pt.enter_option_structure(struct.name, legs, sym, cfg)
                                            if "error" not in res:
                                                print(f"  > Auto-entered Option Structure: {sym} {struct.name}")
                                                ivr_disp = f"{ivr:.0f}" if ivr is not None else "N/A"
                                                vix_disp = f"{vix:.1f}" if vix is not None else "N/A"
                                                msg = _format_options_telegram_alert(
                                                    res, regime_name, bias, ivr_disp, vix_disp, is_nifty=False
                                                )
                                                Alerter(token, chat_id).send(msg)
                                except Exception as sym_err:
                                    print(f"  > Underlying {sym} automation failed: {sym_err}")
                        except Exception as oe:
                            print(f"  > Options automation core error: {oe}")
                last_engine_run = now

            # except for the 15:25-15:35 EOD flatten window.
            pt.check_and_close_trades()
            pt.check_option_exits()
            try:
                vix_now = data.get("regime", {}).get("vix", 15.0)
                pt.check_nifty_option_exits(vix_current=vix_now, cfg=load_config())
            except Exception as ex:
                print(f"  > NIFTY exit check failed: {ex}")
            
        except Exception as e:
            ts = datetime.now().strftime('%H:%M:%S')
            print(f"[{ts}] CRITICAL: Automation worker loop error: {e}")
            traceback.print_exc()
            
        time.sleep(60) # Wake up every minute

if __name__ == "__main__":
    # Start automation thread
    worker = threading.Thread(target=_automation_worker, daemon=True)
    worker.start()
    
    print("StockMinded Dashboard -> http://localhost:5050")
    app.run(host="0.0.0.0", port=5050, debug=False)
