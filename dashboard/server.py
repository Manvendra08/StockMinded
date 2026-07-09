r"""StockMinded visual dashboard -- Flask server.

Run:  .venv312\Scripts\python dashboard/server.py
Open: http://localhost:5050
"""

from __future__ import annotations

import concurrent.futures as _cfutures
import datetime as dt_mod
import json
import os
import sqlite3
import sys
import traceback
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Ensure project root is on sys.path so signal imports work
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".env")

import logging

# Suppress noisy external warnings
logging.getLogger("src.intelligence.ml_predictor").setLevel(logging.ERROR)
logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request, send_from_directory

from config.loader import load_config, load_universe
from data import feed
from ops.alerts import Alerter
from ops.journal import Journal
from signals import flows as flows_mod
from signals import leadership as lead_mod
from signals import regime as regime_mod
from signals import structure_map as sm
from signals import timing as timing_mod
from signals import verdict as verdict_mod

app = Flask(__name__, static_folder=str(Path(__file__).parent))
app.json.ensure_ascii = (
    False  # Allow native UTF-8 (like Rupee symbol) in JSON responses
)

# -- cache in memory so refresh is instant after first load --------
_cache: dict = {}
_cache_ts: datetime | None = None
_cache_lock = __import__("threading").Lock()
_engine_busy = False


def _get_ai_sentiment_ts() -> float:
    """Return the Unix timestamp of the last AI sentiment fetch (from persistent cache file)."""
    try:
        import json as _json
        _cache_file = PROJECT_ROOT / "data" / "cache" / "ai_sentiment_cache.json"
        if _cache_file.exists():
            with open(_cache_file, "r") as _f:
                return _json.load(_f).get("timestamp", 0.0)
    except Exception:
        pass
    return 0.0

# -- data pipeline health tracking -----------------------------------
_HEALTH: dict = {
    "shoonya": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
    "dhan": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
    "yfinance": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
    "news_icicidirect": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
    "news_livemint": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
    "news_moneycontrol": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
    "sentiment_llm": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
    "option_chain": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
    "journal_db": {
        "status": "unknown",
        "last_ok": None,
        "last_error": None,
        "error_count": 0,
    },
}
_HEALTH_LOCK = __import__("threading").Lock()


def _health_event(source: str, success: bool, detail: str = "") -> None:
    """Record a health event for a data pipeline source."""
    now_ts = datetime.now(timezone.utc).isoformat()
    with _HEALTH_LOCK:
        if source not in _HEALTH:
            _HEALTH[source] = {
                "status": "unknown",
                "last_ok": None,
                "last_error": None,
                "error_count": 0,
            }
        if success:
            _HEALTH[source]["status"] = "ok"
            _HEALTH[source]["last_ok"] = now_ts
        else:
            _HEALTH[source]["status"] = "error"
            _HEALTH[source]["last_error"] = f"{now_ts}: {detail}"
            _HEALTH[source]["error_count"] += 1
            logging.getLogger(__name__).warning(
                "[health] %s FAILED: %s (total errors: %s)",
                source,
                detail,
                _HEALTH[source]["error_count"],
            )


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

    def _to_ist(dt_str: str) -> str | None:
        if not dt_str:
            return None
        try:
            clean = dt_str.replace("Z", "").replace("T", " ")[:19]
            dt = datetime.strptime(clean, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            ist = timezone(timedelta(hours=5, minutes=30))
            return dt.astimezone(ist).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return dt_str.replace("T", " ")[:19]

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
        "entry_date": _to_ist(opened_at)[:10] if opened_at else None,
        "entry_time": _to_ist(opened_at),
        "exit_time": _to_ist(closed_at),
        "structure": row.get("structure"),
        "notes": row.get("notes"),
        "source": "journal",
        "risk_rupees": row.get("risk_rupees"),
        "sl_price": row.get("stop"),
        "tgt_price": row.get("target"),
    }


def _merged_paper_trades(limit: int = 100) -> tuple[list[dict], list[dict]]:
    """Return stock journal trades plus paper-trader trades in one feed."""
    trades = []
    try:
        journal_rows = _load_journal_trade_rows()
        trades.extend(_journal_trade_to_ui_trade(row) for row in journal_rows)
    except Exception:
        traceback.print_exc()
    try:
        trades.extend(pt.get_all_trades(limit=999999))
    except Exception:
        traceback.print_exc()

    # Deduplicate: paper trades store 'journal_id' which maps to journal trade 'id'
    seen_jids = set()
    for t in trades:
        if t.get("source") != "journal" and "journal_id" in t:
            seen_jids.add(t["journal_id"])

    deduped = [
        t
        for t in trades
        if not (t.get("source") == "journal" and t.get("id") in seen_jids)
    ]
    deduped.sort(key=lambda t: str(t.get("entry_time") or t.get("entry_date") or ""))

    full_history = list(reversed(deduped))
    return full_history, full_history[:limit]


def _merged_open_trades() -> list[dict]:
    """Return all open stock and paper trades."""
    trades = []
    try:
        journal_rows = [
            row for row in _load_journal_trade_rows() if not row.get("closed_at")
        ]
        trades.extend(_journal_trade_to_ui_trade(row) for row in journal_rows)
    except Exception:
        traceback.print_exc()
    try:
        trades.extend(pt.get_open_trades())
    except Exception:
        traceback.print_exc()

    # Deduplicate by journal_id
    seen_jids = set()
    for t in trades:
        if t.get("source") != "journal" and "journal_id" in t:
            seen_jids.add(t["journal_id"])

    return [
        t
        for t in trades
        if not (t.get("source") == "journal" and t.get("id") in seen_jids)
    ]


def _market_status_now() -> dict:
    """Compute live market status — never cached."""
    from datetime import timedelta, timezone

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
        "market_status": "OPEN"
        if market_open
        else ("WEEKEND" if not is_weekday else "CLOSED"),
        "session_date": ist_now.strftime("%Y-%m-%d"),
    }


def _calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Compute simple ATR from OHLC DataFrame."""
    if df.empty or len(df) < period + 1:
        return 0.0
    h = df["high"]
    l = df["low"]
    c_prev = df["close"].shift(1)
    tr = pd.concat([h - l, (h - c_prev).abs(), (l - c_prev).abs()], axis=1).max(axis=1)
    return float(tr.rolling(period).mean().iloc[-1])


def _run_engine() -> dict:
    """Execute the full 4-signal pipeline and return a JSON-safe dict."""
    global _cache, _cache_ts, _engine_busy

    with _cache_lock:
        cache_ts = _cache_ts
        cache_val = _cache
        if _engine_busy:
            return {**cache_val, "engine_status": "BUSY", **_market_status_now()}

    if cache_ts and (datetime.now() - cache_ts).total_seconds() < 300:
        return {**cache_val, **_market_status_now()}

    with _cache_lock:
        _engine_busy = True

    # _engine_busy is cleared after cache is written (happy path) or in the
    # automation worker's exception handler (error path).
    cfg = load_config()
    universe = load_universe(cfg)
    sectors = cfg["sectors"]

    source_errors = []

    # ── Parallel data fetch ─────────────────────────────────────────
    # Run the three slow I/O calls concurrently
    _stock_data = {}
    _sector_data = {}
    _fetched = 0
    _usable = 0

    def _fetch_universe():
        nonlocal _stock_data, _fetched, _usable
        try:
            result = feed.universe_ohlc(universe, period="6mo")
            if isinstance(result, dict) and "data" in result:
                _stock_data = result.get("data") or {}
                _fetched = result.get("fetched", 0) or 0
            else:
                _stock_data = result or {}
                _fetched = len(_stock_data) if hasattr(_stock_data, "__len__") else 0
            _usable = sum(
                1
                for df in (
                    _stock_data.values() if isinstance(_stock_data, dict) else []
                )
                if df is not None and hasattr(df, "empty") and not df.empty
            )
        except Exception as e:
            source_errors.append(f"Stock feed failed: {e}")

    def _fetch_sectors():
        nonlocal _sector_data
        try:
            _sector_data = feed.sector_ohlc(sectors, period="6mo")
        except Exception as e:
            source_errors.append(f"Sector feed failed: {e}")

    with _cfutures.ThreadPoolExecutor(max_workers=2) as _pool:
        _f_univ = _pool.submit(_fetch_universe)
        _f_sec = _pool.submit(_fetch_sectors)
        _cfutures.wait([_f_univ, _f_sec])

    stock_data = _stock_data
    sector_data = _sector_data
    fetched = _fetched
    usable_symbol_count = _usable

    # Hard early exit: stop the pipeline (and LLM) when no usable OHLC DataFrames exist.
    if usable_symbol_count == 0:
        market_now = _market_status_now()
        result = {
            **market_now,
            "source_errors": source_errors
            + [
                f"No usable OHLC data (usable_symbol_count=0, fetched={fetched}). Skipping Brain Audit."
            ],
            "nifty": {"close": 0, "change_pct": 0},
            "banknifty": {"close": 0, "change_pct": 0},
            "regime": {
                "name": "UNKNOWN",
                "trend_score": 0,
                "vix": 0,
                "vix_5d_change_pct": 0,
                "adx": 0,
                "breadth_pct_above_50dma": 0,
                "notes": "No fresh universe OHLC; engine short-circuited.",
            },
            "flows": {
                "fii_dii_5d": {},
                "top_inflow": [],
                "top_outflow": [],
                "pcr_oi": None,
                "pcr_vol": None,
                "max_pain": None,
                "bias": "NEUTRAL",
                "pcr_stale": True,
                "mp_stale": True,
                "pcr_updated_at": None,
                "mp_updated_at": None,
                "notes": "",
                "option_source": None,
                "ai_sentiment": None,
                "ai_sentiment_fetched_at": None,
                "fii_derivatives_5d": {},
                "fii_derivatives_stale": False,
                "trendlyne_kpis": {},
                "modified_max_pain": None,
                "iv_percentile": None,
            },
            "leaders": [],
            "laggards": [],
            "all_ranks": [],
            "sector_rs": [],
            "structure": {"primary": None, "secondary": None, "notes": "N/A"},
            "risk": {
                "capital": load_config().get("account", {}).get("capital", 0),
                "per_trade_pct": load_config().get("risk", {}).get("per_trade_pct", 0),
                "daily_stop_pct": load_config()
                .get("risk", {})
                .get("daily_stop_pct", 0),
                "monthly_stop_pct": load_config()
                .get("risk", {})
                .get("monthly_stop_pct", 0),
                "margin_util_cap": load_config()
                .get("risk", {})
                .get("margin_util_cap", 0),
            },
            "verdict": verdict_mod.build_trade_verdict(
                {"regime": {"name": "UNKNOWN"}, "flows": {"bias": "NEUTRAL"}}
            ).to_dict()
            if hasattr(verdict_mod, "build_trade_verdict")
            else {"action": "WAIT"},
            "skips": {
                "today": [],
                "summary": {"total": 0, "by_reason": {}, "by_gate": {}},
            },
            "verdict_trace": {
                "inputs": {"fetched": 0},
                "blocks": [],
                "reasons": [],
            },
            "signals_computed_at": market_now.get("ts"),
        }
        with _cache_lock:
            _cache = result
            _cache_ts = datetime.now()
            _engine_busy = False
        return result

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
            smart_money_bias="NEUTRAL",
        )
        source_errors.append(f"Flow snapshot failed: {e}")

    bench = feed.ohlc_cached("NIFTY", period="1y")
    ranks = lead_mod.rank_universe(stock_data, bench)
    inflow_syms = [s for s, _ in flow_snap.top_inflow_sectors]

    # --- MOMENTUM BREAKOUT ENRICHMENT ---
    # Add ATR and Breakout metrics to ranks
    enriched_ranks = []
    for r in ranks:
        df = stock_data.get(r.symbol, pd.DataFrame())
        if df.empty:
            continue

        atr = _calculate_atr(df)
        # Relative Volume (Today vs 20D Avg)
        vols = df["volume"].tail(21)
        rel_vol = vols.iloc[-1] / vols.iloc[:-1].mean() if len(vols) > 1 else 1.0
        # Breakout check: Price near or above 20-day high
        high_20d = df["high"].iloc[:-1].tail(20).max()
        is_breakout = df["close"].iloc[-1] >= (high_20d * 0.99)
        # Volatility Score: Annualized volatility (20D window) for beta analysis
        vol_score = round(df["close"].pct_change().tail(20).std() * (252**0.5) * 100, 2)

        # Momentum Score: Prioritize RS + Breakout + Volume
        # This moves breakout candidates to the top of the A-Grade list
        m_score = r.rs_slope_20d + (20.0 if is_breakout else 0.0) + (rel_vol * 5.0)

        # Inject enrichment (low-beta prioritization enabled via vol_score)
        r.momentum_score = m_score
        r.atr = atr
        r.volatility_score = vol_score
        r.rel_vol = rel_vol
        enriched_ranks.append(r)

    # Sort by momentum_score instead of default RS slope
    enriched_ranks.sort(key=lambda x: getattr(x, "momentum_score", 0), reverse=True)

    longs, shorts = lead_mod.a_grade(
        enriched_ranks, inflow_sectors=inflow_syms, sector_map=None
    )

    structure = sm.plan_for(regime_snap.regime)

    # ── Index quotes for the header bar ──
    # Use yfinance OHLC from cache (already fetched above) for prev_close / change_pct.
    # The live LTP from Shoonya is available via /api/intraday if needed.
    try:
        nifty_df = feed.ohlc_cached("NIFTY", period="1mo")
        if not nifty_df.empty:
            nifty_df = nifty_df.dropna(subset=["close"])
    except Exception as e:
        nifty_df = pd.DataFrame()
        source_errors.append(f"Nifty feed failed: {e}")

    nifty_close = float(nifty_df["close"].iloc[-1]) if not nifty_df.empty else 0
    nifty_prev = (
        float(nifty_df["close"].iloc[-2]) if len(nifty_df) >= 2 else nifty_close
    )
    nifty_chg_pct = (
        round(100 * (nifty_close - nifty_prev) / nifty_prev, 2) if nifty_prev else 0
    )

    # BankNifty — fallback to ^NSEBANK via yfinance if primary feed fails
    bn_df = pd.DataFrame()
    try:
        bn_df = feed.ohlc_cached("BANKNIFTY", period="1mo")
        if not bn_df.empty:
            bn_df = bn_df.dropna(subset=["close"])
    except Exception as e:
        source_errors.append(f"BankNifty primary feed failed: {e}")
    if bn_df.empty:
        try:
            import yfinance as yf

            bn_ticker = yf.Ticker("^NSEBANK")
            bn_df = bn_ticker.history(period="1mo")
            if not bn_df.empty:
                bn_df = bn_df.rename(
                    columns={
                        "Open": "open",
                        "High": "high",
                        "Low": "low",
                        "Close": "close",
                        "Volume": "volume",
                    }
                )
                source_errors.append("BankNifty: used yfinance fallback")
        except Exception as e2:
            source_errors.append(f"BankNifty fallback also failed: {e2}")

    bn_close = float(bn_df["close"].iloc[-1]) if not bn_df.empty else 0
    bn_prev = float(bn_df["close"].iloc[-2]) if len(bn_df) >= 2 else bn_close
    bn_chg_pct = round(100 * (bn_close - bn_prev) / bn_prev, 2) if bn_prev else 0

    # Risk params
    risk_cfg = cfg.get("risk", {})
    account_cfg = cfg.get("account", {})

    # Compute data freshness based on cache file ages
    cache_dir = Path("data/cache/ohlc")
    today_str = datetime.now().strftime("%Y-%m-%d")
    max_age_secs = 0
    checked = 0
    if cache_dir.exists():
        for t in universe[:5]:  # Spot check first 5 tickers
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
        freshness_status = (
            "LIVE"
            if max_age_secs < 900
            else ("STALE" if max_age_secs < 3600 else "OLD")
        )
    else:
        freshness_status = "EOD"
    data_freshness = {
        "status": freshness_status,
        "max_age_minutes": round(max_age_secs / 60, 1),
        "cache_files_checked": checked,
    }

    status = market_now
    last_trading_date = None
    if not nifty_df.empty:
        try:
            last_idx = nifty_df.index[-1]
            last_trading_date = (
                last_idx.strftime("%Y-%m-%d")
                if hasattr(last_idx, "strftime")
                else str(last_idx)[:10]
            )
        except Exception:
            last_trading_date = None

    result = {
        "ts": status["ts"],
        "market_open": status["market_open"],
        "market_status": status["market_status"],
        "session_date": status.get("session_date"),
        "last_trading_date": last_trading_date,
        "data_freshness": data_freshness,
        "data_sources": feed.get_data_sources(),
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
            "ai_sentiment_fetched_at": (
                lambda ts: datetime.fromtimestamp(ts).strftime('%d %b %Y, %H:%M') if ts else None
            )(_get_ai_sentiment_ts()),
            "fii_derivatives_5d": getattr(flow_snap, "fii_derivatives_5d", {}),
            "fii_derivatives_stale": getattr(flow_snap, "fii_derivatives_stale", False),
            "trendlyne_kpis": getattr(flow_snap, "trendlyne_kpis", {}),
            "modified_max_pain": getattr(flow_snap, "trendlyne_kpis", {}).get(
                "modified_max_pain"
            ),
            "iv_percentile": getattr(flow_snap, "trendlyne_kpis", {}).get(
                "iv_percentile"
            ),
        },
        "leaders": [
            {
                "symbol": r.symbol,
                "rs_slope": r.rs_slope_20d,
                "pct_vs_50dma": r.pct_vs_50dma,
                "quintile": r.quintile,
                "atr": getattr(r, "atr", 0),
                "vol_score": getattr(r, "volatility_score", 0),
            }
            for r in longs[:6]
        ],
        "laggards": [
            {
                "symbol": r.symbol,
                "rs_slope": r.rs_slope_20d,
                "pct_vs_50dma": r.pct_vs_50dma,
                "quintile": r.quintile,
                "atr": getattr(r, "atr", 0),
                "vol_score": getattr(r, "volatility_score", 0),
            }
            for r in shorts[:6]
        ],
        "all_ranks": [
            {
                "symbol": r.symbol,
                "rs_slope": max(-150.0, min(150.0, r.rs_slope_20d)),
                "pct_vs_50dma": r.pct_vs_50dma,
                "quintile": r.quintile,
                "above_50dma": r.above_50dma,
                "vol_score": getattr(r, "volatility_score", 0),
            }
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
    # Compute iv_rank — re-use chain snapshot from flows if already fetched
    _iv_rank_for_verdict = None
    try:
        from signals.options import atm_iv as _atm_iv
        from signals.options import chain_snapshot as _chain_snap
        from signals.options import iv_rank as _iv_rank_fn

        _db_path = cfg.get("options", {}).get(
            "iv_history_db", "./data/iv_history.sqlite"
        )
        # Cache the chain snapshot within this engine run to avoid redundant fetches
        _chain = _chain_snap("NIFTY")
        _spot = nifty_close
        if not _chain.empty and _spot > 0:
            _iv_rank_for_verdict = _iv_rank_fn(
                "NIFTY", _atm_iv(_chain, _spot), _db_path
            )
    except Exception as e:
        logging.getLogger(__name__).exception(
            "Failed computing iv_rank for verdict: %s", e
        )

    # Compute verdict using FULL data before slicing leaders/laggards for UI
    result_for_verdict = {
        **result,
        "leaders": [{"quintile": r.quintile, "symbol": r.symbol} for r in longs],
        "laggards": [{"quintile": r.quintile, "symbol": r.symbol} for r in shorts],
        "iv_rank": _iv_rank_for_verdict,
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
        skip_rows = journal.get_skipped_trades(
            limit=50, since_date=start_utc.isoformat()
        )

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
            },
        }
    except Exception as e:
        result["skips"] = {
            "today": [],
            "summary": {"total": 0, "by_reason": {}, "by_gate": {}},
            "error": str(e),
        }

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
    with _cache_lock:
        _cache = result
        _cache_ts = datetime.now()
        _engine_busy = False  # Clear busy flag only after successful cache write
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

    # Local OHLC cache to avoid re-fetching the same symbol's data
    # across LONG and SHORT timing checks (same symbol can appear in both).
    _local_ohlc_5m: dict[str, pd.DataFrame] = {}
    _local_ohlc_1d: dict[str, pd.DataFrame] = {}

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
    ai_sentiment = flows.get("ai_sentiment") or {}
    trade_verdict = (
        data.get("verdict") or verdict_mod.build_trade_verdict(data).to_dict()
    )
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

    # 2. Entry Window Tightening:
    # Avoid equity entries after 14:15 IST (EOD volatility). Options use their own
    # window from config (is_within_entry_window) which allows until 14:30.
    now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
    if (now_ist.hour, now_ist.minute) >= (14, 15):
        allow_longs = False
        allow_shorts = False
        # Do NOT block can_trade_options here — options entry window handles this

    # 2b. Expiry Day Restriction: no equity trades after 12:00 IST on expiry day
    try:
        from signals.options import is_symbol_expiry_today

        if is_symbol_expiry_today("NIFTY"):
            if (now_ist.hour, now_ist.minute) >= (12, 0):
                allow_longs = False
                allow_shorts = False
        if is_symbol_expiry_today("BANKNIFTY"):
            if (now_ist.hour, now_ist.minute) >= (12, 0):
                allow_longs = False
                allow_shorts = False
        if is_symbol_expiry_today("SENSEX"):
            if (now_ist.hour, now_ist.minute) >= (12, 0):
                allow_longs = False
                allow_shorts = False
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[_generate_trade_alerts] expiry check failed, fail-open: %s", e
        )  # fail-open if expiry check unavailable

    # 2. VIX Filter
    if vix > 24:
        allow_longs = False
        allow_shorts = False
        alerts.append(
            {
                "symbol": "NIFTY",
                "direction": "AVOID",
                "entry_trigger": "VIX > 24",
                "entry_price": None,
                "stop": None,
                "target1": None,
                "target2": None,
                "trail_rule": "",
                "qty": 0,
                "risk_rupees": 0,
                "confidence": "HIGH",
                "no_trade_reason": "Extreme volatility (VIX > 24). Stay flat.",
                "evidence": [f"VIX: {vix:.1f}", f"Regime: {regime_name}"],
            }
        )
        return alerts

    if (
        verdict_action in ("WAIT", "NO_TRADE_DATA_STALE")
        and not can_trade_equity
        and not can_trade_options
    ):
        alerts.append(
            {
                "symbol": "NIFTY",
                "direction": "AVOID",
                "entry_trigger": verdict_action,
                "entry_price": None,
                "stop": None,
                "target1": None,
                "target2": None,
                "trail_rule": "",
                "qty": 0,
                "risk_rupees": 0,
                "confidence": trade_verdict.get("confidence", "LOW"),
                "no_trade_reason": trade_verdict.get("strategy", "No clean edge."),
                "evidence": trade_verdict.get("reasons", [])
                + trade_verdict.get("blocks", []),
            }
        )
        return alerts

    # --- NIFTY OPTIONS ALERTS ---
    if can_trade_options:
        if nifty_action == "OPTION_SELL_DEFINED_RISK" and regime_name in (
            "RANGE_LOW_VOL",
            "RANGE_HIGH_VOL",
            "VOL_CONTRACTION",
        ):
            if max_pain:
                alerts.append(
                    {
                        "symbol": "NIFTY",
                        "direction": "NEUTRAL",
                        "entry_trigger": f"Iron Condor @ Max Pain {max_pain:.0f}",
                        "entry_price": nifty_px,
                        "stop": "Defined Risk",
                        "target1": "Theta Decay",
                        "target2": None,
                        "trail_rule": "Adjust wings if breached",
                        "qty": 50,
                        "risk_rupees": round(nifty_px * 0.01 * 50, 2),
                        "confidence": "MEDIUM",
                        "no_trade_reason": None,
                        "evidence": [
                            f"Regime: {regime_name}",
                            f"PCR: {pcr}",
                            f"Max Pain: {max_pain}",
                        ],
                        "verdict_action": "OPTION_SELL_DEFINED_RISK",
                    }
                )
        elif nifty_action == "NAKED_OPTION_SELL":
            direction = (
                "LONG" if (nifty_v.get("tone") == "bull" or bias == "LONG") else "SHORT"
            )
            side = "PUTS" if direction == "LONG" else "CALLS"
            alerts.append(
                {
                    "symbol": "NIFTY",
                    "direction": direction,
                    "entry_trigger": f"Naked {side} Sell ({nifty_v.get('confidence')} Conf)",
                    "entry_price": nifty_px,
                    "stop": "20% Premium SL",
                    "target1": "80% Premium Decay",
                    "target2": None,
                    "trail_rule": "Trail SL to cost after 50% decay",
                    "qty": 50,
                    "risk_rupees": 5000,
                    "confidence": nifty_v.get("confidence", "MEDIUM"),
                    "no_trade_reason": None,
                    "evidence": [
                        f"Regime: {regime_name}",
                        f"Trend: {trend_score}",
                        f"Bias: {bias}",
                    ],
                    "verdict_action": "NAKED_OPTION_SELL",
                }
            )

    # BankNifty Divergence
    if abs(bn_chg - nifty_chg) > 0.5:
        direction = "LONG" if bn_chg > nifty_chg else "SHORT"
        if (direction == "LONG" and allow_longs) or (
            direction == "SHORT" and allow_shorts
        ):
            sl_dist = bn_px * 0.005
            alerts.append(
                {
                    "symbol": "BANKNIFTY",
                    "direction": direction,
                    "entry_trigger": f"Divergence play ({bn_chg:+.2f}% vs Nifty {nifty_chg:+.2f}%)",
                    "entry_price": bn_px,
                    "stop": round(
                        bn_px - sl_dist if direction == "LONG" else bn_px + sl_dist, 2
                    ),
                    "target1": round(
                        bn_px + sl_dist * 2
                        if direction == "LONG"
                        else bn_px - sl_dist * 2,
                        2,
                    ),
                    "target2": None,
                    "trail_rule": "Fixed SL",
                    "qty": 30,
                    "risk_rupees": round(sl_dist * 30, 2),
                    "confidence": "LOW",
                    "no_trade_reason": None,
                    "evidence": [f"BN Divergence: {abs(bn_chg - nifty_chg):.2f}%"],
                }
            )

    # --- STOCK ALERTS ---
    risk_amt = capital * per_trade_risk_pct

    # --- AI Sentiment Direction Guidance ---
    # AI sentiment steers direction rather than blocking trades.
    # The verdict engine (verdict.py) already factors AI into direction + confidence.
    # Here we only:
    #   1. Caution in choppy + LOW confidence (avoid noise)
    #   2. Add AI ticker mentions as evidence to individual alerts
    # Handle case where ai_sentiment might be a list instead of dict
    if isinstance(ai_sentiment, list):
        # If it's a list, try to get confidence from first item if it's a dict
        ai_conf = str(ai_sentiment[0].get("confidence") if ai_sentiment and isinstance(ai_sentiment[0], dict) else "").upper()
        # For actionable_trade_ideas, use the list directly if it's a list of dicts
        actionable_ideas = ai_sentiment if all(isinstance(item, dict) for item in ai_sentiment) else []
    else:
        ai_conf = str(ai_sentiment.get("confidence") or "").upper()
        actionable_ideas = ai_sentiment.get("actionable_trade_ideas") or []
    # Build a lookup of AI-mentioned tickers: {SYMBOL: "LONG"|"SHORT"}
    ai_ideas: dict[str, str] = {
        str(idea.get("ticker", "")).upper(): str(idea.get("direction", "")).upper()
        for idea in actionable_ideas
        if idea.get("ticker") and idea.get("direction") in ("LONG", "SHORT")
    }

    # Safety filter: in choppy range when AI has LOW confidence, avoid noise trading
    # AI sentiment already factored into verdict direction — this is an extra safety net
    if ai_conf == "LOW" and regime_name == "RANGE_HIGH_VOL":
        allow_longs = False
        allow_shorts = False

    # --- TIMING ENGINE CONFIG ---
    cfg = load_config()
    timing_engine_cfg = cfg.get("timing_engine", {})

    if allow_longs:
        for stock in leaders[:8]:
            sym = stock["symbol"]
            q_val = stock.get("quintile")
            q = int(q_val) if pd.notna(q_val) else 0

            # Logic Flaw Fix: Avoid "Late Entry" by skipping stocks overextended from 50DMA (>12%)
            if stock.get("pct_vs_50dma", 0) > 12.0:
                continue

            # --- NEW: Timing Gate (SRV-301) ---
            timing_ok = True
            timing_reason = ""
            timing_result = {}
            price = stock.get("ltp", 0)
            if timing_engine_cfg.get("enabled", True):
                try:
                    if sym not in _local_ohlc_5m:
                        _local_ohlc_5m[sym] = feed.ohlc_cached(sym, interval="5m", period="1d")
                    if sym not in _local_ohlc_1d:
                        _local_ohlc_1d[sym] = feed.ohlc_cached(sym, interval="1d", period="6mo")
                    df_5m = _local_ohlc_5m[sym]
                    df_1d = _local_ohlc_1d[sym]
                    vwap_5m = None
                    if (
                        df_5m is not None
                        and not df_5m.empty
                        and "vwap" in df_5m.columns
                    ):
                        vwap_5m = df_5m["vwap"].iloc[-1]

                    market_breadth = {
                        "advances": len(leaders),
                        "declines": len(laggards),
                    }

                    timing_result = timing_mod.evaluate_timing_for_entry(
                        symbol=sym,
                        direction="LONG",
                        price=price,
                        config=timing_engine_cfg,
                        df_5m=df_5m,
                        df_1d=df_1d,
                        vwap_5m=vwap_5m,
                        ai_sentiment_current=ai_sentiment,
                        market_breadth=market_breadth,
                        vix_df=None,
                    )
                    timing_ok = timing_result.get("timing_ok", True)
                    timing_reason = timing_result.get("reason", "")

                    if not timing_ok:
                        logging.debug(f"[TIMING] {sym} LONG skipped: {timing_reason}")
                        continue
                except Exception as e:
                    logging.debug(f"[TIMING] {sym} check failed: {e}; failing open")
                    # Fail-open: continue with timing_ok=True
            # --- END TIMING GATE ---

            # --- PHASE 2: AI Review (SRV-302) ---
            ai_timing_ok = True
            ai_confidence = 0.0
            ai_reason = ""
            applied_thresholds = {}
            sentiment_flip_detected = False

            if timing_engine_cfg.get("enabled", True):
                # AI Review
                if cfg.get("timing_engine", {}).get("ai_review", {}).get("enabled"):
                    try:
                        ai_result = timing_mod.review_timing_with_llm(
                            symbol=sym,
                            direction="LONG",
                            price=price,
                            timing_snapshot=timing_result.get("checks", {}),
                            market_regime=regime.get("name", "UNKNOWN"),
                            ai_sentiment=ai_sentiment,
                            use_groq=cfg["timing_engine"]["ai_review"].get("provider")
                            == "groq",
                            groq_config=cfg["timing_engine"]["ai_review"],
                        )
                        ai_timing_ok = ai_result.get("ai_timing_ok", True)
                        ai_confidence = ai_result.get("confidence", 0.0)
                        ai_reason = ai_result.get("reason", "")

                        if not ai_timing_ok:
                            logging.info(
                                f"[AI_REVIEW] {sym} LONG: AI rejected. {ai_reason}"
                            )
                            continue
                    except Exception as e:
                        logging.debug(f"[AI_REVIEW] {sym}: error ({e}); failing open")
                        # Fail-open: continue with ai_timing_ok=True

                # Dynamic Thresholds
                if (
                    cfg.get("timing_engine", {})
                    .get("dynamic_thresholds", {})
                    .get("enabled")
                ):
                    try:
                        applied_thresholds = timing_mod.get_regime_adjusted_thresholds(
                            market_regime=regime.get("name", "UNKNOWN"),
                            base_config=cfg["timing_engine"].get(
                                "late_entry_filter", {}
                            ),
                            dynamic_rules=cfg["timing_engine"][
                                "dynamic_thresholds"
                            ].get("adjustment_rules", {}),
                        )
                    except Exception as e:
                        logging.debug(f"[DYNAMIC_THRESHOLDS] {sym}: error ({e})")

                # Sentiment Flip Detection
                if (
                    cfg.get("timing_engine", {})
                    .get("sentiment_tracking", {})
                    .get("enabled")
                ):
                    try:
                        flip_result = timing_mod.detect_sentiment_flip(
                            current_sentiment=ai_sentiment,
                            previous_sentiment=None,  # TODO: fetch from cache/journal
                            window_trades=[],  # TODO: fetch last 20 trades from journal
                        )
                        if flip_result.get("flip_detected") and cfg["timing_engine"][
                            "sentiment_tracking"
                        ].get("flip_detection"):
                            logging.warning(
                                f"[SENTIMENT_FLIP] {flip_result.get('flip_type')}: blocked until {flip_result.get('trading_blocked_until')}"
                            )
                            sentiment_flip_detected = True
                            if flip_result.get("block_type") == "equity":
                                # Block equity entries
                                continue
                    except Exception as e:
                        logging.debug(f"[SENTIMENT_FLIP] {sym}: error ({e})")

            conf = "HIGH" if q >= 5 else ("MEDIUM" if q >= 4 else "LOW")
            evidence = [
                f"RS Slope: {stock['rs_slope']}",
                f"Q: {q}",
                f"vs 50DMA: {stock['pct_vs_50dma']}%",
            ]
            if ai_ideas.get(sym) == "LONG":
                evidence.append("AI: LONG mentioned in news")
            alerts.append(
                {
                    "symbol": sym,
                    "direction": "LONG",
                    "entry_trigger": "A-Grade RS leader: pullback/breakout",
                    "entry_price": None,
                    "stop": None,
                    "target1": None,
                    "target2": None,
                    "trail_rule": "Move SL to cost at T1",
                    "qty": 0,
                    "risk_rupees": round(risk_amt, 2),
                    "confidence": conf,
                    "no_trade_reason": None,
                    "atr": stock.get("atr"),
                    "evidence": evidence,
                    "timing_ok": timing_ok,
                    "timing_reason": timing_reason,
                    "event_risk_mode": timing_result.get("event_risk_mode", False),
                    "size_multiplier": timing_result.get("size_multiplier", 1.0),
                    "ai_timing_ok": ai_timing_ok,
                    "ai_confidence": ai_confidence,
                    "ai_reason": ai_reason,
                    "applied_thresholds": applied_thresholds,
                    "sentiment_flip_detected": sentiment_flip_detected,
                }
            )

    if allow_shorts:
        for stock in laggards[:5]:
            sym = stock["symbol"]
            q_val = stock.get("quintile")
            q = int(q_val) if pd.notna(q_val) else 0

            # Logic Flaw Fix: Avoid "Late Entry" on shorts (already collapsed stocks)
            if stock.get("pct_vs_50dma", 0) < -10.0:
                continue

            # --- NEW: Timing Gate (SRV-301) ---
            timing_ok = True
            timing_reason = ""
            timing_result = {}
            price = stock.get("ltp", 0)
            if timing_engine_cfg.get("enabled", True):
                try:
                    if sym not in _local_ohlc_5m:
                        _local_ohlc_5m[sym] = feed.ohlc_cached(sym, interval="5m", period="1d")
                    if sym not in _local_ohlc_1d:
                        _local_ohlc_1d[sym] = feed.ohlc_cached(sym, interval="1d", period="6mo")
                    df_5m = _local_ohlc_5m[sym]
                    df_1d = _local_ohlc_1d[sym]
                    vwap_5m = None
                    if (
                        df_5m is not None
                        and not df_5m.empty
                        and "vwap" in df_5m.columns
                    ):
                        vwap_5m = df_5m["vwap"].iloc[-1]

                    market_breadth = {
                        "advances": len(leaders),
                        "declines": len(laggards),
                    }

                    timing_result = timing_mod.evaluate_timing_for_entry(
                        symbol=sym,
                        direction="SHORT",
                        price=price,
                        config=timing_engine_cfg,
                        df_5m=df_5m,
                        df_1d=df_1d,
                        vwap_5m=vwap_5m,
                        ai_sentiment_current=ai_sentiment,
                        market_breadth=market_breadth,
                        vix_df=None,
                    )
                    timing_ok = timing_result.get("timing_ok", True)
                    timing_reason = timing_result.get("reason", "")

                    if not timing_ok:
                        logging.debug(f"[TIMING] {sym} SHORT skipped: {timing_reason}")
                        continue
                except Exception as e:
                    logging.debug(f"[TIMING] {sym} check failed: {e}; failing open")
                    # Fail-open: continue with timing_ok=True
            # --- END TIMING GATE ---

            # --- PHASE 2: AI Review (SRV-302) for SHORT ---
            ai_timing_ok = True
            ai_confidence = 0.0
            ai_reason = ""
            applied_thresholds = {}
            sentiment_flip_detected = False

            if timing_engine_cfg.get("enabled", True):
                # AI Review
                if cfg.get("timing_engine", {}).get("ai_review", {}).get("enabled"):
                    try:
                        ai_result = timing_mod.review_timing_with_llm(
                            symbol=sym,
                            direction="SHORT",
                            price=price,
                            timing_snapshot=timing_result.get("checks", {}),
                            market_regime=regime.get("name", "UNKNOWN"),
                            ai_sentiment=ai_sentiment,
                            use_groq=cfg["timing_engine"]["ai_review"].get("provider")
                            == "groq",
                            groq_config=cfg["timing_engine"]["ai_review"],
                        )
                        ai_timing_ok = ai_result.get("ai_timing_ok", True)
                        ai_confidence = ai_result.get("confidence", 0.0)
                        ai_reason = ai_result.get("reason", "")

                        if not ai_timing_ok:
                            logging.info(
                                f"[AI_REVIEW] {sym} SHORT: AI rejected. {ai_reason}"
                            )
                            continue
                    except Exception as e:
                        logging.debug(f"[AI_REVIEW] {sym}: error ({e}); failing open")

                # Dynamic Thresholds
                if (
                    cfg.get("timing_engine", {})
                    .get("dynamic_thresholds", {})
                    .get("enabled")
                ):
                    try:
                        applied_thresholds = timing_mod.get_regime_adjusted_thresholds(
                            market_regime=regime.get("name", "UNKNOWN"),
                            base_config=cfg["timing_engine"].get(
                                "late_entry_filter", {}
                            ),
                            dynamic_rules=cfg["timing_engine"][
                                "dynamic_thresholds"
                            ].get("adjustment_rules", {}),
                        )
                    except Exception as e:
                        logging.debug(f"[DYNAMIC_THRESHOLDS] {sym}: error ({e})")

                # Sentiment Flip Detection
                if (
                    cfg.get("timing_engine", {})
                    .get("sentiment_tracking", {})
                    .get("enabled")
                ):
                    try:
                        flip_result = timing_mod.detect_sentiment_flip(
                            current_sentiment=ai_sentiment,
                            previous_sentiment=None,
                            window_trades=[],
                        )
                        if flip_result.get("flip_detected") and cfg["timing_engine"][
                            "sentiment_tracking"
                        ].get("flip_detection"):
                            logging.warning(
                                f"[SENTIMENT_FLIP] {flip_result.get('flip_type')}: blocked until {flip_result.get('trading_blocked_until')}"
                            )
                            sentiment_flip_detected = True
                            if flip_result.get("block_type") == "equity":
                                continue
                    except Exception as e:
                        logging.debug(f"[SENTIMENT_FLIP] {sym}: error ({e})")

            conf = "HIGH" if q >= 5 else ("MEDIUM" if q >= 4 else "LOW")
            evidence = [
                f"RS Slope: {stock['rs_slope']}",
                f"Q: {q}",
                f"vs 50DMA: {stock['pct_vs_50dma']}%",
            ]
            if ai_ideas.get(sym) == "SHORT":
                evidence.append("AI: SHORT mentioned in news")
            alerts.append(
                {
                    "symbol": sym,
                    "direction": "SHORT",
                    "entry_trigger": "A-Grade RS laggard: bounce/breakdown",
                    "entry_price": None,
                    "stop": None,
                    "target1": None,
                    "target2": None,
                    "trail_rule": "Move SL to cost at T1",
                    "qty": 0,
                    "risk_rupees": round(risk_amt, 2),
                    "confidence": conf,
                    "no_trade_reason": None,
                    "evidence": evidence,
                    "timing_ok": timing_ok,
                    "timing_reason": timing_reason,
                    "event_risk_mode": timing_result.get("event_risk_mode", False),
                    "size_multiplier": timing_result.get("size_multiplier", 1.0),
                    "ai_timing_ok": ai_timing_ok,
                    "ai_confidence": ai_confidence,
                    "ai_reason": ai_reason,
                    "applied_thresholds": applied_thresholds,
                    "sentiment_flip_detected": sentiment_flip_detected,
                }
            )

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
    lines.append(
        f"Regime: `{regime.get('name', '?')}`  |  Bias: `{flows.get('bias', '?')}`"
    )
    lines.append(
        f"NIFTY: {nifty.get('close', 0):.0f} ({nifty.get('change_pct', 0):+.2f}%)"
    )
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
            arrow = (
                "🟢 BUY"
                if a["direction"] == "LONG"
                else ("🔴 SELL" if a["direction"] == "SHORT" else "⚪ NEUTRAL")
            )
            conf = a.get("confidence", "")
            lines.append(f"  {arrow} *{a['symbol']}* [{conf}]")
            lines.append(f"    Trigger: {a.get('entry_trigger', 'N/A')}")
            lines.append(
                f"    Entry: {a.get('entry_price', 'N/A')} | SL: {a.get('stop', 'N/A')}"
            )
            lines.append(
                f"    T1: {a.get('target1', 'N/A')} | T2: {a.get('target2', 'N/A')}"
            )
            lines.append(
                f"    Risk: ₹{a.get('risk_rupees', 0):,.0f} | Qty: {a.get('qty', 0)}"
            )
            for ev in a.get("evidence", []):
                lines.append(f"    - {ev}")
            lines.append("")

    if not alerts:
        lines.append("No actionable trades right now.")
        lines.append(f"Regime `{regime.get('name', '')}` -- stay flat or wait.")

    lines.append("---")
    lines.append("_Risk: 0.75% per trade | Max 3% concurrent | Hard gates enforced_")
    return "\n".join(lines)


def _format_options_telegram_alert(
    trade: dict,
    regime_name: str,
    bias: str,
    ivr_disp: str,
    vix_disp: str,
    is_nifty: bool = False,
) -> str:
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
        lines.append(
            f"  {side_emoji} {side_text} {leg_type} {strike:.0f} ({expiry}) x {qty} @ ₹{premium:.2f}"
        )

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
@app.route("/favicon.ico")
def favicon():
    return "", 204


@app.route("/")
def index():
    # Serve dashboard UI at root so http://localhost:5050/ works.
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
            _json.dumps(raw, default=str), mimetype="application/json"
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# TTL cache for intraday endpoint to reduce redundant yfinance downloads
_INTRADAY_CACHE: dict[str, tuple[float, dict]] = {}
_INTRADAY_CACHE_TTL = 30  # seconds


@app.route("/api/intraday")
def api_intraday():
    """Live intraday snapshot: LTP, day OHLC, change%, volume for watchlist."""
    try:
        # Check cache
        now = time.time()
        if (
            _INTRADAY_CACHE.get("data")
            and (now - _INTRADAY_CACHE.get("ts", 0)) < _INTRADAY_CACHE_TTL
        ):
            return jsonify(_INTRADAY_CACHE["data"])

        cfg = load_config()
        top_n = cfg.get("intraday_top_n", 30)
        universe = load_universe(cfg)

        if _cache and _cache.get("all_ranks"):
            ranked_syms = [r["symbol"] for r in _cache["all_ranks"]]
            top_syms = ranked_syms[:top_n]
        else:
            top_syms = universe[:top_n]

        instruments = [
            f"{s}.NS" if not s.startswith("^") and "." not in s else s for s in top_syms
        ]
        broker_quotes = feed.quote_batch(top_syms)
        import yfinance as yf

        tickers = yf.Tickers(" ".join(instruments))

        # Fetch enough daily history for 20D average volume calculation
        try:
            hist_df = yf.download(
                " ".join(instruments),
                period="3mo",
                interval="1d",
                progress=False,
                group_by="ticker",
                auto_adjust=False,
            )
        except Exception:
            hist_df = pd.DataFrame()

        rows = []
        for sym, raw in zip(top_syms, instruments):
            try:
                q = broker_quotes.get(sym) or {}
                source = q.get("source", "")
                # Shoonya FNO futures quotes have full OHLC; shoonya_quote has ltp only
                if source in ("shoonya_fno", "shoonya_quote", "dhan_quote"):
                    ltp = round(float(q["ltp"]), 2) if q.get("ltp") else None
                    open_ = round(float(q["open"]), 2) if q.get("open") else None
                    high = round(float(q["high"]), 2) if q.get("high") else None
                    low = round(float(q["low"]), 2) if q.get("low") else None
                    prev = (
                        round(float(q["prev_close"]), 2)
                        if q.get("prev_close")
                        else None
                    )
                    avg_vol = int(q.get("volume") or 0)
                    chg_pct = q.get("change_pct")
                else:
                    info = tickers.tickers[raw].fast_info
                    ltp = (
                        round(float(info.last_price), 2)
                        if hasattr(info, "last_price") and info.last_price
                        else None
                    )
                    open_ = (
                        round(float(info.open), 2)
                        if hasattr(info, "open") and info.open
                        else None
                    )
                    high = (
                        round(float(info.day_high), 2)
                        if hasattr(info, "day_high") and info.day_high
                        else None
                    )
                    low = (
                        round(float(info.day_low), 2)
                        if hasattr(info, "day_low") and info.day_low
                        else None
                    )
                    prev = (
                        round(float(info.previous_close), 2)
                        if hasattr(info, "previous_close") and info.previous_close
                        else None
                    )
                    avg_vol = int(info.three_month_average_volume or 0)
                    chg_pct = (
                        round(100 * (ltp - prev) / prev, 2) if ltp and prev else None
                    )

                # Calculate today's volume and relative volume
                today_vol = 0
                rel_vol = None
                if not hist_df.empty and raw in hist_df.columns.get_level_values(0):
                    try:
                        sym_hist = hist_df[raw]
                        if not sym_hist.empty and "Volume" in sym_hist:
                            volumes = sym_hist["Volume"].dropna()
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
                                avg_20d = (
                                    float(hist_20.mean())
                                    if len(hist_20) >= 5
                                    else float(avg_vol)
                                )
                                if avg_20d > 0:
                                    rel_vol = round(today_vol / avg_20d, 2)
                    except Exception as e:
                        logging.getLogger(__name__).exception(
                            "Failed computing relative volume for %s: %s", raw, e
                        )

                rows.append(
                    {
                        "symbol": sym,
                        "ltp": ltp,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "prev_close": prev,
                        "chg_pct": chg_pct,
                        "vol": avg_vol,
                        "today_vol": today_vol,
                        "rel_vol": rel_vol,
                        "source": source or q.get("source", "yfinance"),
                    }
                )
            except Exception:
                rows.append(
                    {
                        "symbol": sym,
                        "ltp": None,
                        "open": None,
                        "high": None,
                        "low": None,
                        "prev_close": None,
                        "chg_pct": None,
                        "vol": None,
                        "today_vol": 0,
                        "rel_vol": None,
                        "source": "error",
                    }
                )

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
            nifty_intra = feed.ohlc_cached(
                "NIFTY", period=tf_cfg["period"], interval=tf_cfg["interval"]
            )
            if not nifty_intra.empty:
                # Flatten MultiIndex columns from newer yfinance
                if isinstance(nifty_intra.columns, __import__("pandas").MultiIndex):
                    nifty_intra.columns = [
                        c[0] if isinstance(c, tuple) else c for c in nifty_intra.columns
                    ]
                for ts, row in nifty_intra.iterrows():
                    candles.append(
                        {
                            "t": ts.strftime(
                                "%H:%M" if tf in ["5m", "15m", "1h"] else "%d-%m"
                            ),
                            "o": round(float(row.get("Open", row.get("open"))), 2),
                            "h": round(float(row.get("High", row.get("high"))), 2),
                            "l": round(float(row.get("Low", row.get("low"))), 2),
                            "c": round(float(row.get("Close", row.get("close"))), 2),
                        }
                    )
        except Exception as e:
            print(f"[intraday candle error] {e}")
            traceback.print_exc()

        response_data = {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
            "watchlist": rows,
            "nifty_candles": candles,
            "timeframe": tf,
        }
        _INTRADAY_CACHE["data"] = response_data
        _INTRADAY_CACHE["ts"] = time.time()
        return jsonify(response_data)
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

        token = p_settings.get("telegram_bot_token") or cfg.get("alerts", {}).get(
            "telegram_bot_token", ""
        )
        chat_id = p_settings.get("telegram_chat_id") or cfg.get("alerts", {}).get(
            "telegram_chat_id"
        )
        alerter = Alerter(token, chat_id)

        # We need to peek into ops/alerts.py for the status
        import ops.alerts as alerts_mod

        success = alerter.send("Test ping from StockMinded dashboard.")

        last = alerts_mod._last_send
        suffix = token[-4:] if token and len(token) >= 4 else None
        return jsonify(
            {
                "ok": success,
                "status_code": last.get("status"),
                "telegram_description": last.get("error"),
                "token_suffix": suffix,
            }
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/telegram/status")
def api_telegram_status():
    import ops.alerts as alerts_mod

    return jsonify(alerts_mod._last_send)


import importlib.util as _ilu

_pt_spec = _ilu.spec_from_file_location(
    "paper_trader", Path(__file__).parent / "paper_trader.py"
)
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
        full_history, display_trades = _merged_paper_trades(limit=100)
        closed_trades = [t for t in full_history if t.get("status") == "CLOSED"]
        winners = [t for t in closed_trades if (t.get("pnl") or 0) > 0]
        losers = [t for t in closed_trades if (t.get("pnl") or 0) < 0]
        resolved_trades = [t for t in closed_trades if (t.get("pnl") or 0) != 0]

        stats = {
            "total_trades": len(full_history),
            "open_trades": len([t for t in full_history if t.get("status") == "OPEN"]),
            "cumulative_pnl": round(
                sum(t.get("pnl", 0) or 0 for t in closed_trades), 2
            ),
            "overall_win_rate": round(
                len(winners) / max(len(resolved_trades), 1) * 100,
                1,
            ),
            "total_winners": len(winners),
            "total_losers": len(losers),
            "closed_trades": len(closed_trades),
            "avg_winner": round(
                sum(t.get("pnl", 0) or 0 for t in winners) / max(len(winners), 1), 2
            )
            if winners
            else 0.0,
            "avg_loser": round(
                sum(t.get("pnl", 0) or 0 for t in losers) / max(len(losers), 1), 2
            )
            if losers
            else 0.0,
        }
        return jsonify({"trades": display_trades, "stats": stats})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/open")
def api_paper_open():
    """Get only open trades with live P&L and prev-day change.

    Data source priority:
      1. Dhan public F&O page (no auth, scraped)
      2. yfinance (fallback for LTP only)
      3. Dhan API (fallback for prev_close/change_pct)
    """
    try:
        trades = _merged_open_trades()
        if trades:
            symbols = list(set(t["symbol"] for t in trades))

            # 1. Primary: Dhan public F&O page (no auth needed, has LTP + prev_close + change_pct)
            from data.feed import quote_batch, quote_batch_public

            quotes = quote_batch_public(symbols)

            # 2. Fallback LTP: yfinance via _get_ltp_batch for symbols still missing
            missing_ltp = [s for s in symbols if not quotes.get(s, {}).get("ltp")]
            if missing_ltp:
                yf_prices = pt._get_ltp_batch(missing_ltp)
                for s in missing_ltp:
                    ltp = yf_prices.get(s)
                    if ltp is not None:
                        if s not in quotes or not quotes[s]:
                            quotes[s] = {}
                        quotes[s]["ltp"] = ltp
                        # yfinance doesn't provide prev_close/change_pct reliably
                        # so leave those as-is

            def _needs_prev_close_refresh(q: dict) -> bool:
                """True when prev_close is missing or clearly inconsistent with LTP."""
                ltp = q.get("ltp")
                if ltp is None:
                    return False
                prev_close = q.get("prev_close")
                try:
                    ltp_f = float(ltp)
                    prev_f = float(prev_close) if prev_close is not None else None
                except (TypeError, ValueError):
                    return True
                if prev_f is None or prev_f <= 0:
                    return True
                # For NSE cash/F&O equities, >30% day move is almost always bad metadata.
                implied_move = abs(100 * (ltp_f - prev_f) / prev_f)
                return implied_move > 30

            def _get_robust_prev_close(sym: str, ltp: float) -> float | None:
                """Fetch previous close robustly from local EOD cache or yfinance history."""
                # 1. Try local time-bucketed EOD cache
                try:
                    from data.feed import ohlc_cached
                    df = ohlc_cached(sym, period="5d")
                    if df is not None and not df.empty:
                        import datetime as dt
                        today_str = dt.datetime.now().strftime("%Y-%m-%d")
                        last_date = df.index[-1]
                        last_date_str = (
                            last_date.strftime("%Y-%m-%d")
                            if hasattr(last_date, "strftime")
                            else str(last_date)[:10]
                        )
                        if last_date_str == today_str and len(df) >= 2:
                            p = float(df["close"].iloc[-2])
                        else:
                            p = float(df["close"].iloc[-1])
                        
                        if p > 0 and abs(100 * (ltp - p) / p) <= 40:
                            return round(p, 2)
                except Exception:
                    pass

                # 2. Try yfinance individually (avoiding batch failures)
                try:
                    import yfinance as yf
                    yf_s = f"{sym}.NS" if not sym.startswith("^") and "." not in sym else sym
                    t_obj = yf.Ticker(yf_s)
                    # Try fast_info
                    try:
                        p = float(t_obj.fast_info.previous_close)
                        if p > 0 and abs(100 * (ltp - p) / p) <= 40:
                            return round(p, 2)
                    except Exception:
                        pass
                    # Try history 5d
                    df = t_obj.history(period="5d")
                    if df is not None and not df.empty:
                        import datetime as dt
                        today_str = dt.datetime.now().strftime("%Y-%m-%d")
                        last_date = df.index[-1]
                        last_date_str = (
                            last_date.strftime("%Y-%m-%d")
                            if hasattr(last_date, "strftime")
                            else str(last_date)[:10]
                        )
                        if last_date_str == today_str and len(df) >= 2:
                            p = float(df["Close"].iloc[-2])
                        else:
                            p = float(df["Close"].iloc[-1])
                        if p > 0 and abs(100 * (ltp - p) / p) <= 40:
                            return round(p, 2)
                except Exception:
                    pass
                return None

            # 3. Fallback prev_close/change_pct: Dhan API/Shoonya for symbols
            # with missing or suspicious prev_close.
            missing_meta = [
                s
                for s in symbols
                if _needs_prev_close_refresh(quotes.get(s, {}))
            ]
            if missing_meta:
                dhan_q = quote_batch(missing_meta)
                for s in missing_meta:
                    dq = dhan_q.get(s, {})
                    prev = dq.get("prev_close")
                    ltp = quotes.get(s, {}).get("ltp")
                    # Sanity check incoming Shoonya/Dhan fallback close
                    if prev is not None and ltp is not None:
                        try:
                            prev_f = float(prev)
                            ltp_f = float(ltp)
                            if prev_f > 0 and abs(100 * (ltp_f - prev_f) / prev_f) <= 40:
                                if s not in quotes or not quotes[s]:
                                    quotes[s] = {}
                                quotes[s]["prev_close"] = prev_f
                                quotes[s]["change_pct"] = dq.get("change_pct")
                        except (TypeError, ValueError):
                            pass

            # 4. Final robust fallback: yfinance / local EOD cache
            still_missing = [
                s
                for s in symbols
                if _needs_prev_close_refresh(quotes.get(s, {}))
            ]
            for sym in still_missing:
                ltp = quotes.get(sym, {}).get("ltp")
                if ltp is not None:
                    prev = _get_robust_prev_close(sym, ltp)
                    if prev is not None:
                        if sym not in quotes or not quotes[sym]:
                            quotes[sym] = {}
                        quotes[sym]["prev_close"] = prev
                        quotes[sym]["change_pct"] = round(
                            100 * (ltp - prev) / prev, 2
                        )

            # Enrich each trade
            for t in trades:
                if t.get("legs"):
                    continue
                q = quotes.get(t["symbol"], {})
                ltp = q.get("ltp")
                if ltp is not None and t.get("entry_price"):
                    if t.get("direction") == "SHORT":
                        t["unrealized_pnl"] = round(
                            (t["entry_price"] - ltp) * t["qty"], 2
                        )
                        t["unrealized_pct"] = round(
                            100 * (t["entry_price"] - ltp) / t["entry_price"], 2
                        )
                    else:
                        t["unrealized_pnl"] = round(
                            (ltp - t["entry_price"]) * t["qty"], 2
                        )
                        t["unrealized_pct"] = round(
                            100 * (ltp - t["entry_price"]) / t["entry_price"], 2
                        )
                    t["current_price"] = ltp
                prev_close = q.get("prev_close")
                chg_pct = None
                # Derive the day change from ltp + prev_close whenever possible.
                if ltp is not None and prev_close not in (None, 0):
                    try:
                        ltp_f = float(ltp)
                        prev_f = float(prev_close)
                        implied_move = abs(100 * (ltp_f - prev_f) / prev_f)
                        if prev_f > 0 and implied_move <= 40:
                            chg_pct = round(100 * (ltp_f - prev_f) / prev_f, 2)
                        else:
                            prev_close = None
                    except (TypeError, ValueError, ZeroDivisionError):
                        chg_pct = None
                if chg_pct is None:
                    raw_chg = q.get("change_pct")
                    if raw_chg is not None:
                        try:
                            raw_chg_f = float(raw_chg)
                            if abs(raw_chg_f) <= 40:
                                chg_pct = round(raw_chg_f, 2)
                        except (TypeError, ValueError):
                            chg_pct = None
                if chg_pct is not None and abs(chg_pct) > 40:
                    chg_pct = None
                t["prev_close"] = prev_close
                t["chg_pct"] = chg_pct

        # Response-level timestamp for the CMP header
        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        fetched_at = ist_now.strftime("%d/%m %I:%M %p")
        return jsonify({"trades": trades, "fetched_at": fetched_at})
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
            result = pt.enter_nifty_option_structure(
                _dict_to_setup(s), _dict_legs_to_resolved(s["legs"]), cfg
            )
            if "error" not in result:
                entered_nifty_options.append(result)

        # BANKNIFTY options
        entered_banknifty_options = []
        banknifty_setups = pt.get_banknifty_option_setups(data, cfg)
        for s in banknifty_setups:
            if not s.get("suitable") or not s.get("legs"):
                continue
            result = pt.enter_banknifty_option_structure(
                _dict_to_setup(s), _dict_legs_to_resolved(s["legs"]), cfg
            )
            if "error" not in result:
                entered_banknifty_options.append(result)

        return jsonify(
            {
                "entered": entered,
                "entered_nifty_options": entered_nifty_options,
                "entered_banknifty_options": entered_banknifty_options,
                "alert_count": len(alerts),
                "nifty_setup_count": len(setups),
                "banknifty_setup_count": len(banknifty_setups),
            }
        )
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
        closed_opts = pt.check_nifty_option_exits(
            vix_current=data.get("regime", {}).get("vix", 15.0), cfg=cfg
        )
        closed_banknifty_opts = pt.check_banknifty_option_exits(
            vix_current=data.get("regime", {}).get("vix", 15.0), cfg=cfg
        )
        return jsonify(
            {
                "closed": closed,
                "count": len(closed),
                "closed_nifty_options": closed_opts,
                "nifty_option_count": len(closed_opts),
                "closed_banknifty_options": closed_banknifty_opts,
                "banknifty_option_count": len(closed_banknifty_opts),
            }
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/close/<int:trade_id>")
def api_paper_close(trade_id):
    """Manually close a specific trade."""
    try:
        result = pt.close_trade_manual(trade_id, reason="MANUAL")
        if result is None:
            return jsonify(
                {"error": f"Trade {trade_id} not found or already closed"}
            ), 404
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
            return jsonify(
                {"error": f"Option trade {trade_id} not found or already closed"}
            ), 404
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


@app.route("/api/paper/intelligence")
def api_paper_intelligence():
    """Use AI to analyze last 20 closed trades and suggest config updates."""
    try:
        from data.ai_scraper import call_llm

        # Get last 20 closed trades for performance analysis
        all_trades = pt.get_all_trades(limit=100)
        closed_trades = [t for t in all_trades if t.get("status") == "CLOSED"][:20]
        settings = pt.get_settings()

        if not closed_trades:
            return jsonify(
                {
                    "analysis": "No closed trades available for analysis yet.",
                    "suggestions": [],
                }
            )

        # Use centralized logic from paper_trader for context enrichment
        intel_context = pt.get_intelligence_context(closed_trades)

        # Extract new metrics for Smart Exits vs Standard Exits
        smart_metrics = intel_context.get("smart_exits_metrics", {})
        standard_metrics = intel_context.get("standard_exits_metrics", {})

        # Get past intelligence history to preserve memory
        try:
            db_data = pt._load_db()
            intel_history = db_data.get("ai_intelligence_history", [])
        except Exception:
            intel_history = []

        # Limit history to the last 5 runs to keep prompt size small and relevant
        recent_history = intel_history[-5:]

        history_context = ""
        if recent_history:
            history_context = "\n--- PREVIOUS AI OPTIMIZATION HISTORY & MEMORY ---\n"
            for i, hist in enumerate(recent_history):
                history_context += (
                    f"Run {i + 1} ({hist.get('timestamp', 'N/A')}):\n"
                    f"  Analysis Summary: {hist.get('analysis', '')}\n"
                    f"  Suggestions Made: {json.dumps(hist.get('suggestions', []))}\n\n"
                )

        prompt = (
            f"Analyze these 20 recent paper trades (Profit Factor: {intel_context['profit_factor']}, Drawdown: {intel_context['drawdown']}) to optimize intelligence-led trading.\n"
            f"Trades: {json.dumps(intel_context['summary'])}\n"
            f"Current Settings: {json.dumps(settings)}\n\n"
            f"Performance of Smart Exits (AI-driven): Count={smart_metrics.get('count')}, Win Rate={smart_metrics.get('win_rate')}%, Profit Factor={smart_metrics.get('profit_factor')}, Total PnL={smart_metrics.get('total_pnl')}\n"
            f"Performance of Standard Exits (SL/TGT): Count={standard_metrics.get('count')}, Win Rate={standard_metrics.get('win_rate')}%, Profit Factor={standard_metrics.get('profit_factor')}, Total PnL={standard_metrics.get('total_pnl')}\n\n"
            f"{history_context}"
            "Specifically evaluate if 'smart_exits_enabled' and 'smart_reentry_enabled' are performing optimally. "
            "If VIX spikes or Delta breaches caused large losses before exits, suggest enabling/disabling these intelligence features. "
            "If the 'Smart Exits' group shows a high win rate (e.g., >60%) but a low profit factor (e.g., <1.0), suggest enabling 'smart_reentry_enabled' to capitalize on potential reversals after smart exits. "
            "Also look for patterns like 'stop loss too tight' or 'overtrading'.\n"
            "Review the PREVIOUS AI OPTIMIZATION HISTORY & MEMORY above. Identify if previous suggestions were made, and if they were applied, evaluate their success against the new metrics. This is a continuous learning loop: build on your previous insights and self-improve. Do not repeat failed recommendations.\n"
            "Return JSON with 'analysis' (professional summary incorporating historical comparison) and 'suggestions' (list of {param, current, suggest, reason})."
        )

        result, model_used = call_llm(
            prompt,
            system_prompt="You are a senior hedge fund risk manager specializing in Indian Equities.",
            return_provider=True,
        )
        if result and isinstance(result, dict):
            result["model_used"] = model_used

            # Save the run to intelligence history to preserve the memory for next run
            try:
                new_run = {
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S IST"),
                    "model_used": model_used,
                    "analysis": result.get("analysis", ""),
                    "suggestions": result.get("suggestions", []),
                }
                with pt.atomic_db_update() as update_db:
                    if "ai_intelligence_history" not in update_db:
                        update_db["ai_intelligence_history"] = []
                    update_db["ai_intelligence_history"].append(new_run)
                    # Limit saved history to 10 runs in DB to prevent unbounded growth
                    update_db["ai_intelligence_history"] = update_db[
                        "ai_intelligence_history"
                    ][-10:]
            except Exception as save_err:
                app.logger.error("Failed to save intelligence history: %s", save_err)

        return jsonify(result or {"error": "Intelligence engine unavailable"})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/paper/intelligence/apply", methods=["POST"])
def api_paper_intelligence_apply():
    """Apply a suggested configuration change."""
    try:
        suggestion = request.json
        if not suggestion or "param" not in suggestion:
            return jsonify({"error": "Missing suggestion data"}), 400

        settings = pt.get_settings()
        param = suggestion["param"]
        if param in settings:
            settings[param] = suggestion["suggest"]
            pt.save_settings(settings)
            return jsonify({"success": True, "updated_param": param})
        return jsonify({"error": f"Parameter '{param}' not found in settings"}), 404
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

        return jsonify(
            {
                "date": target_date.isoformat(),
                "skipped": rows,
                "summary": {
                    "total": len(rows),
                    "by_reason": by_reason,
                    "by_gate": by_gate,
                },
            }
        )
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


def calculate_option_trade_margin(trade: dict) -> float:
    """Calculate SPAN + Exposure margin requirement for an option trade structure."""
    legs = trade.get("legs", [])
    if not legs:
        return 0.0

    symbol = trade.get("symbol", "NIFTY")
    
    # Standard lot sizes: NIFTY = 50 (or 75, or 25), BANKNIFTY = 15.
    # Let's dynamically infer lot size if possible or default to standard.
    lot_size = 50
    if symbol == "BANKNIFTY":
        lot_size = 15
    elif symbol == "NIFTY":
        qty_0 = legs[0].get("qty", 50) if legs else 50
        if qty_0 % 75 == 0:
            lot_size = 75
        elif qty_0 % 25 == 0:
            lot_size = 25
        else:
            lot_size = 50

    sell_ce = []
    buy_ce = []
    sell_pe = []
    buy_pe = []

    for leg in legs:
        side = leg.get("side", "BUY")
        ltype = leg.get("type", "CE")
        qty = leg.get("qty", lot_size)
        strike = leg.get("strike", 0.0)
        prem = leg.get("entry_premium") or leg.get("premium") or 0.0
        
        num_lots = max(1, qty // lot_size)
        for _ in range(num_lots):
            item = {"strike": strike, "premium": prem}
            if side == "SELL":
                if ltype == "CE":
                    sell_ce.append(item)
                else:
                    sell_pe.append(item)
            else:
                if ltype == "CE":
                    buy_ce.append(item)
                else:
                    buy_pe.append(item)

    sell_ce.sort(key=lambda x: x["strike"])
    buy_ce.sort(key=lambda x: x["strike"])
    sell_pe.sort(key=lambda x: x["strike"], reverse=True)
    buy_pe.sort(key=lambda x: x["strike"], reverse=True)

    margin = 0.0
    
    # Match Call Spreads
    hedged_ce_count = 0
    naked_sell_ce_count = 0
    while sell_ce:
        sell_ce.pop()
        if buy_ce:
            buy_ce.pop()
            hedged_ce_count += 1
        else:
            naked_sell_ce_count += 1

    # Match Put Spreads
    hedged_pe_count = 0
    naked_sell_pe_count = 0
    while sell_pe:
        sell_pe.pop()
        if buy_pe:
            buy_pe.pop()
            hedged_pe_count += 1
        else:
            naked_sell_pe_count += 1

    # Net debit for remaining buy legs (buyer paid premium)
    net_debit = 0.0
    for b in buy_ce + buy_pe:
        net_debit += b["premium"]

    # Exchange SPAN + Exposure estimate per lot
    naked_margin_per_lot = 120000.0
    spread_margin_per_lot = 30000.0

    call_spreads_margin = hedged_ce_count * spread_margin_per_lot
    put_spreads_margin = hedged_pe_count * spread_margin_per_lot
    
    if call_spreads_margin > 0 and put_spreads_margin > 0:
        hedged_margin = max(call_spreads_margin, put_spreads_margin) + min(call_spreads_margin, put_spreads_margin) * 0.15
    else:
        hedged_margin = call_spreads_margin + put_spreads_margin
        
    naked_margin = (naked_sell_ce_count + naked_sell_pe_count) * naked_margin_per_lot
    
    margin += hedged_margin + naked_margin + (net_debit * lot_size)
    
    return round(margin, 2)


@app.route("/api/options/structures")
def api_options_structures():
    try:
        db = pt._load_db()
        ops = db.get("option_trades", [])

        # Calculate margin requirement for each trade structure
        for t in ops:
            t["margin_req"] = calculate_option_trade_margin(t)

        # Enrich open option trades with live premiums and P&L
        open_ops = [t for t in ops if t.get("status") == "OPEN"]
        if open_ops:
            try:
                price_map = pt._build_option_price_map(open_ops)
                for t in open_ops:
                    current_net = pt._option_net_premium(t["legs"], price_map)

                    # Store current premium for each leg
                    for leg in t["legs"]:
                        key = (leg["strike"], leg["expiry"], leg["type"])
                        leg["current_premium"] = price_map.get(key)

                    if current_net is not None:
                        t["current_net_premium"] = current_net
                        entry_net = t.get("net_premium") or t.get("net_credit") or 0.0
                        t["pnl"] = round(entry_net - current_net, 2)
                        # SANITY BOUND: Clamp P&L to theoretical max loss
                        max_loss_rupees = t.get("max_loss_rupees", 0.0)
                        if (
                            max_loss_rupees > 0
                            and abs(t["pnl"]) > max_loss_rupees * 1.1
                        ):
                            logging.getLogger(__name__).error(
                                "Display enrich trade %s %s: P&L %s exceeds theoretical max loss %s - clamping.",
                                t.get("id"),
                                t.get("symbol"),
                                t["pnl"],
                                max_loss_rupees,
                            )
                            t["pnl"] = max(
                                -max_loss_rupees, min(max_loss_rupees, t["pnl"])
                            )
            except Exception as e:
                logging.getLogger(__name__).exception(
                    "Failed to enrich open option trades: %s", e
                )

        ist_now = datetime.now(timezone(timedelta(hours=5, minutes=30)))
        fetched_at = ist_now.strftime("%d/%m %I:%M %p")
        return jsonify({"option_trades": ops, "fetched_at": fetched_at})
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
    from config.loader import load_config
    from signals.option_strategy import ResolvedLeg

    cfg = load_config()
    min_lots = max(1, cfg.get("nifty_options", {}).get("min_lots_per_leg", 1))
    out = []
    for leg in legs:
        qty = max(1, int(leg.get("qty", 1)))
        lots = leg.get("lots")
        lot_size = leg.get("lot_size")
        if lots is None or lot_size is None:
            try:
                settings = pt.get_settings()
                lots = max(1, settings.get("options_lots_per_trade", 1))
            except Exception:
                lots = min_lots
            exchange_lot_size = (
                cfg.get("nifty_options", {}).get("lot_size", {}).get("NIFTY")
                or cfg.get("options", {}).get("lot_size", {}).get("NIFTY")
                or 75
            )
            if qty % lots == 0:
                lot_size = qty // lots
            else:
                lot_size = exchange_lot_size
                lots = max(1, qty // lot_size)
        premium_val = leg.get("premium", 0.0) or 0.0
        if premium_val <= 0:
            logging.getLogger(__name__).error(
                "REJECTED leg with zero/corrupt premium: %s %s strike=%s expiry=%s premium=%s",
                leg.get("side", ""),
                leg.get("type", ""),
                leg.get("strike", 0),
                leg.get("expiry", ""),
                premium_val,
            )
        out.append(
            ResolvedLeg(
                side=leg.get("side", ""),
                type=leg.get("type", ""),
                strike=leg.get("strike", 0),
                expiry=leg.get("expiry", ""),
                lots=lots,
                lot_size=lot_size,
                premium=premium_val,
            )
        )
    return out


@app.route("/api/options/alerts")
def api_options_alerts():
    """Generate NIFTY and BANKNIFTY option-selling setups."""
    try:
        cfg = load_config()
        data = _run_engine()
        setups = pt.get_nifty_option_setups(data, cfg)
        banknifty_setups = pt.get_banknifty_option_setups(data, cfg)
        return jsonify(
            {
                "ts": data.get("ts"),
                "setups": setups,
                "banknifty_setups": banknifty_setups,
            }
        )
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/options/auto-enter")
def api_options_auto_enter():
    """Auto-enter suitable NIFTY and BANKNIFTY option setups."""
    try:
        cfg = load_config()
        data = _run_engine()
        # NIFTY
        setups = pt.get_nifty_option_setups(data, cfg)
        entered = []
        for s in setups:
            if not s.get("suitable") or not s.get("legs"):
                continue
            result = pt.enter_nifty_option_structure(
                _dict_to_setup(s), _dict_legs_to_resolved(s["legs"]), cfg
            )
            if "error" not in result:
                entered.append(result)
        # BANKNIFTY
        banknifty_setups = pt.get_banknifty_option_setups(data, cfg)
        entered_banknifty = []
        for s in banknifty_setups:
            if not s.get("suitable") or not s.get("legs"):
                continue
            result = pt.enter_banknifty_option_structure(
                _dict_to_setup(s), _dict_legs_to_resolved(s["legs"]), cfg
            )
            if "error" not in result:
                entered_banknifty.append(result)
        return jsonify(
            {
                "entered": entered,
                "entered_banknifty": entered_banknifty,
                "setups": setups,
                "banknifty_setups": banknifty_setups,
            }
        )
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
            headers={
                "Content-disposition": f"attachment; filename=paper_trades_{date.today().isoformat()}.csv"
            },
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

# In-memory cache to prevent repeated LLM brain audits for the same
# (regime/bias/vix/pcr + alert basket). This stops token burn loops.
_brain_audit_cache: dict[str, bool] = {}
_brain_audit_cache_ts: dict[str, float] = {}


_VERDICT_REVIEW_HISTORY_FILE = Path("data/cache/verdict_review_history.json")


def _load_verdict_review_history() -> list[dict]:
    """Load past verdict review history for self-improvement context."""
    try:
        if _VERDICT_REVIEW_HISTORY_FILE.exists():
            with open(_VERDICT_REVIEW_HISTORY_FILE, "r") as f:
                data = json.load(f)
                return data if isinstance(data, list) else []
    except Exception as e:
        logging.getLogger(__name__).exception(
            "Failed to load verdict review history: %s", e
        )
    return []


def _save_verdict_review_run(run: dict) -> None:
    """Save a verdict review run to persistent history for self-improvement."""
    try:
        _VERDICT_REVIEW_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        history = _load_verdict_review_history()
        history.append(run)
        history = history[-20:]  # keep last 20
        with open(_VERDICT_REVIEW_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        logging.getLogger(__name__).exception(
            "Failed to save verdict review history: %s", e
        )


def _build_verdict_review_context() -> str:
    """Build self-improvement context from past verdict reviews."""
    history = _load_verdict_review_history()
    if not history:
        return ""
    recent = history[-5:]
    ctx = "\n\n--- PREVIOUS VERDICT REVIEW HISTORY (Self-Improvement Memory) ---\n"
    ctx += (
        "Review how your previous assessments performed. Learn from past decisions:\n"
    )
    for h in recent:
        ts = h.get("timestamp", "N/A")
        act = h.get("action", "N/A")
        appr = h.get("approved", "N/A")
        sug = h.get("suggested_action", "N/A")
        reas = h.get("reason", "")
        ctx += f"- [{ts}] Verdict: {act} | Approved: {appr} | Suggested: {sug} | Reason: {reas}\n"
    ctx += "\nUse this memory to avoid repeating past errors in your assessment.\n"
    return ctx


@app.route("/api/verdict-review")
def api_verdict_review():
    """On-demand LLM verdict review (manual trigger only, like Intelligence tab)."""
    try:
        data = _run_engine()
        review = _brain_verdict_review(data)
        return jsonify(review)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _brain_verdict_review(data: dict) -> dict:
    """LLM reviews the Final Trading Verdict for Directional Stock Strategy.

    The verdict engine already computes a data-driven verdict. This LLM review
    adds a second-opinion layer that considers:
    - Regime context
    - AI sentiment (from news)
    - Smart money bias
    - PCR, VIX, trend

    Self-Improvement: past reviews are loaded and injected as context so the
    LLM can learn from prior assessments and improve future decision-making.

    Returns: {"approved": bool, "reason": str, "suggested_action": str}
    Returns fail-open default if LLM unavailable.
    """
    import json as _json
    import time as _time

    from data.ai_scraper import call_llm

    verdict = data.get("verdict", {})
    stock_v = verdict.get("stock", {})
    regime = data.get("regime", {})
    flows = data.get("flows", {})

    regime_name = regime.get("name", "UNKNOWN")
    trend_score = regime.get("trend_score", 0)
    vix = regime.get("vix", 15)
    adx = regime.get("adx", 0)
    bias = flows.get("bias", "NEUTRAL")
    pcr = flows.get("pcr_oi")
    breadth = regime.get("breadth_pct_above_50dma", 50)
    ai_sentiment = flows.get("ai_sentiment", {})
    if isinstance(ai_sentiment, dict):
        ai_overall = ai_sentiment.get("overall_market_sentiment", "NEUTRAL")
        ai_conf = ai_sentiment.get("confidence", "LOW")
    else:
        ai_overall = "NEUTRAL"
        ai_conf = "LOW"

    stock_action = stock_v.get("action", "WAIT")
    stock_conf = stock_v.get("confidence", "LOW")
    stock_strategy = stock_v.get("strategy", "")
    stock_reasons = stock_v.get("reasons", [])

    # Load self-improvement context from past reviews
    _improvement_context = _build_verdict_review_context()

    prompt = (
        "You are a senior Indian equities risk manager. Review this trading verdict.\n\n"
        f"DIRECTIONAL STOCK VERDICT: {stock_action} (Confidence: {stock_conf})\n"
        f"Strategy: {stock_strategy}\n"
        f"Regime: {regime_name} | Trend: {trend_score}/10 | ADX: {adx:.1f} | VIX: {vix:.1f}\n"
        f"Breadth >50DMA: {breadth}% | Bias: {bias} | PCR: {pcr}\n"
        f"AI Sentiment: {ai_overall} ({ai_conf})\n"
        f"Reasons: {'; '.join(stock_reasons[-3:])}\n"
        f"{_improvement_context}"
        "Return ONLY valid JSON:\n"
        '{"approved": true/false, '
        '"reason": "<=15 words explaining concern or approval", '
        '"suggested_action": "PROCEED" | "CAUTION" | "SKIP"}'
    )

    try:
        decision, model_used = call_llm(
            prompt,
            system_prompt="You are a risk manager. Return strict JSON only.",
            json_mode=True,
            max_tokens=100,
            return_provider=True,
        )
        if decision is None:
            return {
                "approved": True,
                "reason": "LLM unavailable, proceeding",
                "model_used": "None",
            }

        approved = bool(decision.get("approved", True))
        reason = str(decision.get("reason", ""))
        suggested = str(decision.get("suggested_action", "PROCEED"))

        logging.getLogger(__name__).info(
            "🧠 Verdict Review (%s): %s | %s | %s",
            model_used,
            approved,
            suggested,
            reason,
        )

        # Build and save the review run for self-improvement
        review_run = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "model_used": model_used,
            "action": stock_action,
            "confidence": stock_conf,
            "approved": approved,
            "suggested_action": suggested,
            "reason": reason,
            "regime": regime_name,
            "trend_score": trend_score,
            "vix": vix,
            "bias": bias,
            "ai_overall_sentiment": ai_overall,
        }
        _save_verdict_review_run(review_run)

        return {
            "approved": approved,
            "reason": reason,
            "suggested_action": suggested,
            "model_used": model_used,
        }
    except Exception as e:
        logging.getLogger(__name__).error("🧠 Verdict Review ERROR: %s", e)
        return {
            "approved": True,
            "reason": "Review unavailable, proceeding",
            "model_used": "None",
        }


def _brain_audit(data: dict, alerts: list[dict]) -> bool:
    """Ask LLM Brain for final approval before auto-executing trades.

    Cost safeguards:
    - Cache LLM decision for 10 minutes per snapshot key.
    - Keep prompt short; request strict JSON only.
    """
    import json as _json
    import time as _time

    from data.ai_scraper import call_llm

    regime_name = data.get("regime", {}).get("name")
    trend_score = data.get("regime", {}).get("trend_score")
    vix = data.get("regime", {}).get("vix")
    vix_chg = data.get("regime", {}).get("vix_5d_change_pct")
    bias = data.get("flows", {}).get("bias")
    pcr = data.get("flows", {}).get("pcr_oi")

    symbols_sig = ",".join(
        sorted({str(a.get("symbol")) for a in alerts if a.get("symbol")})
    )
    directions_sig = ",".join(
        sorted({str(a.get("direction")).upper() for a in alerts if a.get("direction")})
    )

    vix_key = round(vix) if isinstance(vix, (int, float)) else 0
    pcr_key = round(pcr, 1) if isinstance(pcr, (int, float)) else 0.0
    cache_key = f"{regime_name}|{trend_score}|{vix_key}|{bias}|{pcr_key}|{symbols_sig}|{directions_sig}"
    ttl = 600.0

    ts = _brain_audit_cache_ts.get(cache_key)
    if ts is not None and (_time.time() - ts) < ttl:
        return _brain_audit_cache.get(cache_key, True)

    proposed = [
        {"symbol": a.get("symbol"), "direction": a.get("direction")} for a in alerts
    ]
    prompt = (
        f"Evaluate this trade signal. "
        f"Regime={regime_name}, TrendScore={trend_score}, VIX={vix}, Bias={bias}, PCR={pcr}. "
        f"Signals={_json.dumps(proposed)}. "
        "Return ONLY valid JSON: "
        '{"approved": true/false, "reason": "<=10 words"}.'
    )

    try:
        decision = call_llm(
            prompt,
            system_prompt="You are a risk manager for Indian trading. Reply with strict JSON only.",
            json_mode=True,
            max_tokens=50,
        )
        approved = True if decision is None else bool(decision.get("approved"))
        _brain_audit_cache[cache_key] = approved
        _brain_audit_cache_ts[cache_key] = _time.time()

        if not approved:
            reason = (
                decision.get("reason") if isinstance(decision, dict) else "Rejected"
            )
            logging.getLogger(__name__).warning("🧠 Brain Audit REJECTED: %s", reason)
        else:
            logging.getLogger(__name__).info("🧠 Brain Audit APPROVED")

        return approved
    except Exception as e:
        # Fail open to avoid blocking engine execution if LLM is down.
        logging.getLogger(__name__).error("🧠 Brain Audit ERROR: %s", e)
        _brain_audit_cache[cache_key] = True
        _brain_audit_cache_ts[cache_key] = _time.time()
        return True


def _automation_worker():
    """Background task to keep the engine fresh and automated."""
    import logging as _logging

    _log = _logging.getLogger(__name__)

    start_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # Cooldown for LLM brain audit to manage API costs.
    # This means the LLM is consulted at most once every `_brain_audit_cooldown_seconds`.
    _last_brain_audit_ts = 0.0
    _brain_audit_cooldown_seconds = 900  # Audit LLM at most once every 15 minutes
    _log.info("[%s] Background worker started...", start_time)

    last_engine_run = 0
    data = {}  # Maintain scope for exit checks

    while True:
        try:
            now = time.time()
            market_open = pt.is_market_open()

            # 1) Run engine every 2 min and auto-enter ONLY during market hours.
            if now - last_engine_run > 120:
                _log.info(
                    "[%s] Engine tick (market_open=%s)...",
                    datetime.now().strftime("%H:%M:%S"),
                    market_open,
                )
                try:
                    from signals.index_weightage import refresh_weights_if_needed
                    refresh_weights_if_needed()
                except Exception as e:
                    _log.warning("Failed background weights refresh: %s", e)

                data = _run_engine()

                if market_open:
                    cfg = load_config()

                    # ---- Phase 1: Equity auto-entry from alerts ----
                    alerts = _generate_trade_alerts(data)
                    actionable_alerts = [
                        a for a in alerts if a.get("direction") in ("LONG", "SHORT")
                    ]

                    # Apply cooldown to LLM brain audit to manage API costs.
                    # If actionable alerts are present, but the cooldown is active, we implicitly approve
                    # to avoid blocking trades, similar to how LLM errors are handled.
                    perform_audit = False
                    if actionable_alerts:
                        if now - _last_brain_audit_ts > _brain_audit_cooldown_seconds:
                            perform_audit = True
                        else:
                            _log.info(
                                f"🧠 Brain Audit skipped due to cooldown ({round(_brain_audit_cooldown_seconds - (now - _last_brain_audit_ts))}s remaining). Approving by default."
                            )

                    if actionable_alerts:
                        approved = True
                        if perform_audit:
                            approved = _brain_audit(data, actionable_alerts)
                            _last_brain_audit_ts = (
                                now  # Cooldown applies whether approved or rejected
                            )

                        if approved:
                            entered = pt.auto_enter_from_alerts(
                                actionable_alerts, cfg=cfg
                            )
                            if entered:
                                _log.info(
                                    "  > Auto-entered %s trades: %s",
                                    len(entered),
                                    ", ".join(e["symbol"] for e in entered),
                                )

                                # Fire Telegram alert so user is notified of auto-trades.
                                try:
                                    token = cfg.get("alerts", {}).get(
                                        "telegram_bot_token"
                                    )
                                    chat_id = cfg.get("alerts", {}).get(
                                        "telegram_chat_id"
                                    )

                                    entered_syms = {e["symbol"] for e in entered}
                                    entered_alerts = [
                                        a
                                        for a in actionable_alerts
                                        if a.get("symbol") in entered_syms
                                    ]

                                    msg = _format_telegram_alert(data, entered_alerts)
                                    msg = "*[AUTO-EXECUTED]*\n" + msg

                                    ok = Alerter(token, chat_id).send(msg)
                                    if ok:
                                        _log.info(
                                            "  > Telegram alert sent for %s auto-trades",
                                            len(entered),
                                        )
                                    else:
                                        import ops.alerts as alerts_mod

                                        _log.warning(
                                            "  > Telegram alert failed. Last error: %s",
                                            alerts_mod._last_send.get("error"),
                                        )
                                except Exception as te:
                                    _log.exception("  > Telegram send failed: %s", te)

                    # ---- Phase 2: NIFTY options auto-entry ----
                    try:
                        setups = pt.get_nifty_option_setups(data, cfg)
                        nifty_entered = []
                        for s in setups:
                            if not s.get("suitable") or not s.get("legs"):
                                continue

                            result = pt.enter_nifty_option_structure(
                                _dict_to_setup(s),
                                _dict_legs_to_resolved(s["legs"]),
                                cfg,
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
                                    res,
                                    regime_name,
                                    bias,
                                    "N/A",
                                    vix_disp,
                                    is_nifty=True,
                                )
                                Alerter(token, chat_id).send(msg)
                    except Exception as ne:
                        _log.exception("  > NIFTY options automation error: %s", ne)

                    # ---- Phase 3: BANKNIFTY options auto-entry ----
                    try:
                        banknifty_setups = pt.get_banknifty_option_setups(data, cfg)
                        banknifty_entered = []
                        for s in banknifty_setups:
                            if not s.get("suitable") or not s.get("legs"):
                                continue
                            result = pt.enter_banknifty_option_structure(
                                _dict_to_setup(s),
                                _dict_legs_to_resolved(s["legs"]),
                                cfg,
                            )
                            if "error" not in result:
                                banknifty_entered.append(result)
                        if banknifty_entered:
                            token = cfg.get("alerts", {}).get("telegram_bot_token")
                            chat_id = cfg.get("alerts", {}).get("telegram_chat_id")
                            for res in banknifty_entered:
                                msg = _format_options_telegram_alert(
                                    res,
                                    regime_name,
                                    bias,
                                    "N/A",
                                    vix_disp,
                                    is_nifty=False,
                                )
                                Alerter(token, chat_id).send(msg)
                    except Exception as bne:
                        _log.exception(
                            "  > BANKNIFTY options automation error: %s", bne
                        )

                    # ---- Phase 4: Multi-underlying options auto-execution ----
                    if cfg.get("options", {}).get("enabled"):
                        try:
                            from signals.option_strategy import (
                                pick_structure,
                                resolve_legs,
                            )
                            from signals.options import atm_iv, chain_snapshot, iv_rank

                            underlyings = [
                                u
                                for u in cfg.get("options", {}).get(
                                    "underlyings", ["NIFTY", "BANKNIFTY"]
                                )
                                if u != "NIFTY"
                            ]

                            token = cfg.get("alerts", {}).get("telegram_bot_token")
                            chat_id = cfg.get("alerts", {}).get("telegram_chat_id")

                            regime_name = data.get("regime", {}).get("name", "")
                            bias = data.get("flows", {}).get("bias", "NEUTRAL")
                            vix = data.get("regime", {}).get("vix", 15)
                            db_path = cfg.get("options", {}).get(
                                "iv_history_db", "./data/iv_history.sqlite"
                            )

                            for sym in underlyings:
                                try:
                                    chain = chain_snapshot(sym)
                                    spot = (
                                        data.get("nifty", {}).get("close")
                                        if sym == "NIFTY"
                                        else data.get("banknifty", {}).get("close")
                                    ) or 0

                                    if spot <= 0 or chain.empty:
                                        continue

                                    current_iv = atm_iv(chain, spot)
                                    ivr = iv_rank(sym, current_iv, db_path)

                                    struct = pick_structure(regime_name, bias, ivr, vix)
                                    if not struct:
                                        continue

                                    lot_sz = (
                                        cfg.get("options", {})
                                        .get("lot_size", {})
                                        .get(sym, 50)
                                    )
                                    step = (
                                        cfg.get("options", {})
                                        .get("strike_step", {})
                                        .get(sym, 50)
                                    )
                                    num_lots = pt.get_settings().get(
                                        "options_lots_per_trade", 1
                                    )

                                    legs = resolve_legs(
                                        struct,
                                        chain,
                                        spot,
                                        lot_sz,
                                        step,
                                        num_lots=num_lots,
                                    )
                                    if not legs:
                                        continue

                                    res = pt.enter_option_structure(
                                        struct.name, legs, sym, cfg
                                    )
                                    if "error" in res:
                                        continue

                                    _log.info(
                                        "  > Auto-entered Option Structure: %s %s",
                                        sym,
                                        struct.name,
                                    )

                                    ivr_disp = (
                                        f"{ivr:.0f}" if ivr is not None else "N/A"
                                    )
                                    vix_disp = (
                                        f"{vix:.1f}" if vix is not None else "N/A"
                                    )

                                    msg = _format_options_telegram_alert(
                                        res,
                                        regime_name,
                                        bias,
                                        ivr_disp,
                                        vix_disp,
                                        is_nifty=False,
                                    )
                                    Alerter(token, chat_id).send(msg)
                                except Exception as sym_err:
                                    _log.exception(
                                        "  > Underlying %s automation failed: %s",
                                        sym,
                                        sym_err,
                                    )
                        except Exception as oe:
                            _log.exception("  > Options automation core error: %s", oe)

                last_engine_run = now

            # 2) Exit checks (runs every minute; EOD flatten window handled inside pt methods)
            vix_now = data.get("regime", {}).get("vix", 15.0)
            regime_now = data.get("regime", {}).get("name")
            pt.check_and_close_trades()
            pt.check_option_exits(vix_current=vix_now, current_regime=regime_now)
            try:
                pt.check_nifty_option_exits(
                    vix_current=vix_now, cfg=load_config(), current_regime=regime_now
                )
            except Exception as ex:
                _log.exception("  > NIFTY exit check failed: %s", ex)
            try:
                pt.check_banknifty_option_exits(
                    vix_current=vix_now, cfg=load_config(), current_regime=regime_now
                )
            except Exception as ex:
                _log.exception("  > BANKNIFTY exit check failed: %s", ex)

        except Exception as e:
            ts = datetime.now().strftime("%H:%M:%S")
            _log.exception("[%s] CRITICAL: Automation worker loop error: %s", ts, e)
            # Ensure _engine_busy is never left True after an unhandled exception.
            with _cache_lock:
                _engine_busy = False

        time.sleep(60)  # Wake up every minute


# ── News Headlines ───────────────────────────────────────────────────
_NEWS_CACHE: dict | None = None
_NEWS_CACHE_TS: float = 0
_NEWS_CACHE_TTL: float = 300.0  # 5 minutes


@app.route("/api/news/headlines")
def api_news_headlines():
    """Return recent market news headlines from all sources."""
    global _NEWS_CACHE, _NEWS_CACHE_TS
    try:
        now = time.time()
        if _NEWS_CACHE is not None and (now - _NEWS_CACHE_TS) < _NEWS_CACHE_TTL:
            return jsonify(_NEWS_CACHE)

        from concurrent.futures import ThreadPoolExecutor, as_completed

        from data.ai_scraper import (
            _fetch_icicidirect_news,
            _fetch_livemint_news,
            _fetch_moneycontrol_news,
            get_market_news_sentiment,
        )

        headlines: list[dict] = []
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {
                executor.submit(_fetch_icicidirect_news): "icicidirect",
                executor.submit(_fetch_livemint_news): "livemint",
                executor.submit(_fetch_moneycontrol_news): "moneycontrol",
            }
            for future in as_completed(futures):
                src = futures[future]
                try:
                    result = future.result()
                    if result:
                        for title, pub in result:
                            headlines.append(
                                {
                                    "title": title,
                                    "published_at": pub,
                                    "source": src,
                                }
                            )
                        _health_event(f"news_{src}", True, f"{len(result)} headlines")
                    else:
                        _health_event(f"news_{src}", False, "empty result")
                except Exception as e:
                    _health_event(f"news_{src}", False, str(e))
                    logging.getLogger(__name__).warning(
                        "[api_news_headlines] source '%s' result failed: %s", src, e
                    )

        # Deduplicate by title
        seen: set = set()
        deduped = []
        for h in headlines:
            key = h["title"].lower().strip()
            if key not in seen:
                seen.add(key)
                deduped.append(h)

        # Sort by published_at descending (most recent first)
        deduped.sort(key=lambda x: x.get("published_at", ""), reverse=True)

        # Fetch AI sentiment verdict (non-blocking on failure)
        verdict = None
        try:
            # Build market context from cached engine data
            _market_ctx = {}
            if _cache:
                regime = _cache.get("regime", {})
                flows = _cache.get("flows", {})
                nifty = _cache.get("nifty", {})
                if nifty.get("close"):
                    _market_ctx["nifty_close"] = nifty["close"]
                    _market_ctx["nifty_change_pct"] = nifty.get("change_pct", 0)
                if regime.get("vix"):
                    _market_ctx["vix"] = regime["vix"]
                if regime.get("name"):
                    _market_ctx["regime"] = regime["name"]
                if regime.get("breadth_pct_above_50dma"):
                    _market_ctx["breadth_pct"] = regime["breadth_pct_above_50dma"]
                fii_dii = flows.get("fii_dii_5d", {})
                if fii_dii.get("fii") is not None:
                    _market_ctx["fii_net"] = fii_dii["fii"]
                if fii_dii.get("dii") is not None:
                    _market_ctx["dii_net"] = fii_dii["dii"]
                if flows.get("pcr_oi") is not None:
                    _market_ctx["pcr_oi"] = flows["pcr_oi"]
                if flows.get("max_pain") is not None:
                    _market_ctx["max_pain"] = flows["max_pain"]
                inflow = [s for s, _ in (flows.get("top_inflow") or [])]
                outflow = [s for s, _ in (flows.get("top_outflow") or [])]
                if inflow:
                    _market_ctx["sector_inflow"] = inflow
                if outflow:
                    _market_ctx["sector_outflow"] = outflow

            sentiment = get_market_news_sentiment(
                market_context=_market_ctx if _market_ctx else None
            )
            if sentiment:
                _health_event(
                    "sentiment_llm", True, sentiment.get("model_used", "unknown")
                )
                verdict = {
                    "overall_market_sentiment": sentiment.get(
                        "overall_market_sentiment"
                    ),
                    "sentiment_score": sentiment.get("sentiment_score"),
                    "sentiment_strength": sentiment.get("sentiment_strength"),
                    "justification": sentiment.get("justification"),
                    "key_drivers": (sentiment.get("key_drivers") or [])[:5],
                    "sector_outlook": sentiment.get("sector_outlook", {}),
                    "risk_factors": (sentiment.get("risk_factors") or [])[:3],
                    "support_resistance": sentiment.get("support_resistance"),
                    "actionable_trade_ideas": (
                        sentiment.get("actionable_trade_ideas") or []
                    )[:3],
                    "market_narrative": sentiment.get("market_narrative"),
                    "confidence": sentiment.get("confidence"),
                    "model_used": sentiment.get("model_used"),
                }
        except Exception as sent_err:
            _health_event("sentiment_llm", False, str(sent_err))
            logging.getLogger(__name__).warning(
                "Sentiment verdict fetch failed: %s", sent_err
            )

        result = {
            "headlines": deduped[:100],  # cap at 100
            "total": len(deduped),
            "verdict": verdict,
        }
        _NEWS_CACHE = result
        _NEWS_CACHE_TS = now
        return jsonify(result)

    except Exception as e:
        logging.getLogger(__name__).exception("News headlines failed: %s", e)
        return jsonify({"error": str(e), "headlines": []}), 500


@app.route("/api/intelligence/index_momentum")
def get_index_momentum():
    from signals.index_weightage import calculate_weighted_momentum, load_index_weights_state
    try:
        nifty_data = calculate_weighted_momentum("NIFTY")
        banknifty_data = calculate_weighted_momentum("BANKNIFTY")
        sensex_data = calculate_weighted_momentum("SENSEX")
        state = load_index_weights_state()
        return jsonify({
            "status": "success",
            "last_refresh": state.get("last_refresh"),
            "weights_status": state.get("status"),
            "data": {
                "NIFTY": nifty_data,
                "BANKNIFTY": banknifty_data,
                "SENSEX": sensex_data
            }
        })
    except Exception as e:
        logging.getLogger(__name__).exception("Failed to get index momentum: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/intelligence/refresh_weights", methods=["POST", "GET"])
def trigger_refresh_weights():
    from signals.index_weightage import refresh_weights_if_needed, load_index_weights_state
    try:
        success = refresh_weights_if_needed(force=True)
        state = load_index_weights_state()
        return jsonify({
            "status": "success" if success else "failed",
            "last_refresh": state.get("last_refresh"),
            "weights_status": state.get("status")
        })
    except Exception as e:
        logging.getLogger(__name__).exception("Failed to force refresh weights: %s", e)
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/health")
def api_health():
    """Return data pipeline health status for all sources."""
    with _HEALTH_LOCK:
        return jsonify(
            {
                "sources": dict(_HEALTH),
                "error_sources": [
                    k for k, v in _HEALTH.items() if v.get("status") == "error"
                ],
                "status": "degraded"
                if any(v.get("status") == "error" for v in _HEALTH.values())
                else "ok",
            }
        )


def _preamble():
    """Run heavy engine work in background so first HTTP request is instant."""
    import logging as _log
    _log.getLogger(__name__).info("[preamble] Warming engine cache in background...")
    try:
        _run_engine()
        _log.getLogger(__name__).info("[preamble] Engine cache warmed successfully.")
    except Exception as e:
        _log.getLogger(__name__).warning("[preamble] Engine warm-up failed: %s", e)


if __name__ == "__main__":
    # Start automation thread
    worker = threading.Thread(target=_automation_worker, daemon=True)
    worker.start()

    # Pre-warm engine cache in background so first dashboard load is instant
    threading.Thread(target=_preamble, daemon=True).start()

    __import__("logging").getLogger(__name__).info(
        "StockMinded Dashboard -> http://localhost:5050"
    )
    app.run(host="0.0.0.0", port=5050, debug=False)
