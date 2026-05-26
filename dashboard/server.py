import os
import sys
import json
import time
import calendar
import datetime as dt_mod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Bootstrap: ensure project root is on sys.path regardless of CWD
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.loader import load_config
from data import feed
from signals import flows as flows_mod
from signals import regime as regime_mod
from signals import verdict as verdict_mod
from signals import leadership as leadership_mod
from signals.structure_map import build_structure_map
from signals.option_strategy import pick_nifty_strategy, NiftyOptionSetup
from signals.flows import FlowSnapshot
from signals.regime import RegimeSnapshot, Regime
from signals.verdict import CombinedVerdict
from ops.alerts import Alerter
from ops.journal import Journal
from risk.guardrails import check_guardrails
from risk.sizing import position_size
from dashboard.paper_trader import PaperTrader

app = Flask(__name__)
CORS(app)

cfg = load_config()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ist_now():
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def _market_open(now=None):
    now = now or _ist_now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt_mod.time(9, 15) <= t <= dt_mod.time(15, 30)


# ---------------------------------------------------------------------------
# Core signal computation (cached per logical day)
# ---------------------------------------------------------------------------

_cache: dict = {"ts": None, "data": None}


def _logical_date(now=None):
    """Day rolls over at 06:00 IST so overnight runs see the same logical date."""
    now = now or _ist_now()
    return (now - timedelta(hours=6)).date()


def _build_result(force: bool = False) -> dict:
    now = _ist_now()
    ld = _logical_date(now)

    if not force and _cache["ts"] == ld and _cache["data"] is not None:
        return _cache["data"]

    result: dict = {}

    # --- Flows ---
    try:
        flow_snap: FlowSnapshot = flows_mod.compute_flows(cfg)
        result["flows"] = flow_snap.to_dict() if hasattr(flow_snap, "to_dict") else vars(flow_snap)
    except Exception as e:
        result["flows"] = {"error": str(e)}

    # --- Nifty price ---
    nifty_close = 0.0
    try:
        nifty_df = feed.ohlc_cached("NIFTY", period="2d")
        if nifty_df is not None and not nifty_df.empty:
            nifty_close = float(nifty_df["close"].iloc[-1])
    except Exception:
        pass
    result["nifty_close"] = nifty_close

    # --- Regime ---
    try:
        regime_snap: RegimeSnapshot = regime_mod.compute_regime(cfg)
        result["regime"] = regime_snap.to_dict() if hasattr(regime_snap, "to_dict") else vars(regime_snap)
    except Exception as e:
        result["regime"] = {"error": str(e)}
        regime_snap = None

    # --- Leadership ---
    longs, shorts = [], []
    try:
        longs, shorts = leadership_mod.compute_leadership(cfg)
        result["leaders_raw"] = [{"quintile": r.quintile, "symbol": r.symbol} for r in longs]
        result["laggards_raw"] = [{"quintile": r.quintile, "symbol": r.symbol} for r in shorts]
    except Exception as e:
        result["leaders_raw"] = []
        result["laggards_raw"] = []

    # --- Nifty strategy ---
    try:
        regime_name = (regime_snap.regime.value if regime_snap and hasattr(regime_snap, "regime") else "NEUTRAL")
        bias = result.get("flows", {}).get("bias", "neutral")
        vix = result.get("regime", {}).get("vix", 15.0) or 15.0
        iv_rank_val = result.get("regime", {}).get("iv_rank")
        setup: NiftyOptionSetup = pick_nifty_strategy(
            result, regime_name, bias, vix, cfg=cfg, iv_rank=iv_rank_val
        )
        result["nifty_setup"] = setup.to_dict() if hasattr(setup, "to_dict") else vars(setup)
    except Exception as e:
        result["nifty_setup"] = {"error": str(e)}
        setup = None

    # --- Structure map ---
    try:
        structure_map = build_structure_map(cfg)
        result["structure_map"] = structure_map
    except Exception as e:
        result["structure_map"] = {}

    # --- Guardrails ---
    try:
        gr = check_guardrails(cfg)
        result["guardrails"] = gr
    except Exception as e:
        result["guardrails"] = {"error": str(e)}

    # --- Position sizing ---
    try:
        ps = position_size(cfg)
        result["position_size"] = ps
    except Exception as e:
        result["position_size"] = {"error": str(e)}

    # --- Compute verdict using FULL data before slicing leaders/laggards for UI
    # Issue #6 callsite fix: compute iv_rank and pass it so naked-sell gate works
    _iv_rank_for_verdict = None
    try:
        from signals.options import iv_rank as _iv_rank_fn, chain_snapshot as _chain_snap, atm_iv as _atm_iv
        _db_path = cfg.get("options", {}).get("iv_history_db", "./data/iv_history.sqlite")
        _chain = _chain_snap("NIFTY")
        _spot  = nifty_close
        if not _chain.empty and _spot > 0:
            _iv_rank_for_verdict = _iv_rank_fn("NIFTY", _atm_iv(_chain, _spot), _db_path)
    except Exception:
        pass  # iv_rank unavailable -> verdict defaults to None -> gate passes with flag

    _iv_rank_for_verdict = None
    try:
        from signals.options import iv_rank as _iv_rank_fn, chain_snapshot as _chain_snap, atm_iv as _atm_iv
        _db_path = cfg.get("options", {}).get("iv_history_db", "./data/iv_history.sqlite")
        _chain = _chain_snap("NIFTY")
        _spot  = nifty_close
        if not _chain.empty and _spot > 0:
            _iv_rank_for_verdict = _iv_rank_fn("NIFTY", _atm_iv(_chain, _spot), _db_path)
    except Exception:
        pass
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
        skip_rows = journal.get_skipped_trades(limit=50, since_date=start_utc.isoformat())

        by_reason: dict[str, int] = {}
        by_gate: dict[str, int] = {}
        for row in skip_rows:
            r_key = row.get("skip_reason") or "unknown"
            g_key = row.get("gate") or "unknown"
            by_reason[r_key] = by_reason.get(r_key, 0) + 1
            by_gate[g_key] = by_gate.get(g_key, 0) + 1

        result["skip_log"] = {
            "total_today": len(skip_rows),
            "by_reason": by_reason,
            "by_gate": by_gate,
            "recent": skip_rows[:10],
        }
    except Exception as e:
        result["skip_log"] = {"error": str(e)}

    # --- Slice leaders/laggards for UI (top 5 each) ---
    result["leaders"] = [{"quintile": r.quintile, "symbol": r.symbol} for r in longs[:5]]
    result["laggards"] = [{"quintile": r.quintile, "symbol": r.symbol} for r in shorts[:5]]

    _cache["ts"] = ld
    _cache["data"] = result
    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/signals")
def api_signals():
    force = request.args.get("force", "false").lower() == "true"
    try:
        data = _build_result(force=force)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/regime")
def api_regime():
    try:
        snap = regime_mod.compute_regime(cfg)
        return jsonify({"ok": True, "data": snap.to_dict() if hasattr(snap, "to_dict") else vars(snap)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/flows")
def api_flows():
    try:
        snap = flows_mod.compute_flows(cfg)
        return jsonify({"ok": True, "data": snap.to_dict() if hasattr(snap, "to_dict") else vars(snap)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/leadership")
def api_leadership():
    try:
        longs, shorts = leadership_mod.compute_leadership(cfg)
        return jsonify({
            "ok": True,
            "leaders": [{"quintile": r.quintile, "symbol": r.symbol} for r in longs],
            "laggards": [{"quintile": r.quintile, "symbol": r.symbol} for r in shorts],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/verdict")
def api_verdict():
    try:
        data = _build_result()
        return jsonify({"ok": True, "data": data.get("verdict", {})})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/option-chain")
def api_option_chain():
    from signals.options import chain_snapshot
    symbol = request.args.get("symbol", "NIFTY")
    try:
        df = chain_snapshot(symbol)
        if df.empty:
            return jsonify({"ok": True, "data": []})
        records = json.loads(df.to_json(orient="records"))
        return jsonify({"ok": True, "data": records})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/paper-trades")
def api_paper_trades():
    try:
        pt_obj = PaperTrader(cfg)
        trades = pt_obj.get_open_trades()
        return jsonify({"ok": True, "data": trades})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/paper-trades/history")
def api_paper_trades_history():
    try:
        pt_obj = PaperTrader(cfg)
        limit = int(request.args.get("limit", 50))
        trades = pt_obj.get_closed_trades(limit=limit)
        return jsonify({"ok": True, "data": trades})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/paper-trades/enter", methods=["POST"])
def api_paper_trade_enter():
    try:
        data = _build_result()
        pt_obj = PaperTrader(cfg)
        trade_id = pt_obj.enter_trade(data)
        return jsonify({"ok": True, "trade_id": trade_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/paper-trades/exit", methods=["POST"])
def api_paper_trade_exit():
    try:
        body = request.get_json(force=True, silent=True) or {}
        trade_id = body.get("trade_id")
        reason = body.get("reason", "manual")
        pt_obj = PaperTrader(cfg)
        result = pt_obj.exit_trade(trade_id, reason=reason)
        return jsonify({"ok": True, "data": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/paper-trades/check-exits", methods=["POST"])
def api_check_exits():
    try:
        data = _build_result()
        pt_obj = PaperTrader(cfg)
        exited = pt_obj.check_and_exit_trades(data)
        return jsonify({"ok": True, "exited": exited})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/paper-trades/pnl")
def api_paper_pnl():
    try:
        pt_obj = PaperTrader(cfg)
        summary = pt_obj.get_pnl_summary()
        return jsonify({"ok": True, "data": summary})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/alerts/history")
def api_alerts_history():
    try:
        alerter = Alerter(cfg)
        limit = int(request.args.get("limit", 50))
        alerts = alerter.get_alert_history(limit=limit)
        return jsonify({"ok": True, "data": alerts})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/alerts/send", methods=["POST"])
def api_alerts_send():
    try:
        data = _build_result()
        alerter = Alerter(cfg)
        sent = alerter.send_signal_alert(data)
        return jsonify({"ok": True, "sent": sent})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/journal")
def api_journal():
    try:
        journal = Journal(cfg["paths"]["journal_db"])
        limit = int(request.args.get("limit", 100))
        entries = journal.get_entries(limit=limit)
        return jsonify({"ok": True, "data": entries})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/journal/add", methods=["POST"])
def api_journal_add():
    try:
        body = request.get_json(force=True, silent=True) or {}
        journal = Journal(cfg["paths"]["journal_db"])
        entry_id = journal.add_entry(**body)
        return jsonify({"ok": True, "entry_id": entry_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/skip-log")
def api_skip_log():
    try:
        data = _build_result()
        return jsonify({"ok": True, "data": data.get("skip_log", {})})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/skip-log/add", methods=["POST"])
def api_skip_log_add():
    try:
        body = request.get_json(force=True, silent=True) or {}
        journal = Journal(cfg["paths"]["journal_db"])
        skip_id = journal.add_skipped_trade(**body)
        return jsonify({"ok": True, "skip_id": skip_id})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/strategy-notes")
def api_strategy_notes():
    try:
        journal = Journal(cfg["paths"]["journal_db"])
        notes = journal.get_strategy_notes()
        return jsonify({"ok": True, "data": notes})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/strategy-notes/save", methods=["POST"])
def api_strategy_notes_save():
    try:
        body = request.get_json(force=True, silent=True) or {}
        journal = Journal(cfg["paths"]["journal_db"])
        journal.save_strategy_notes(body.get("notes", ""))
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/eod-analysis")
def api_eod_analysis():
    try:
        journal = Journal(cfg["paths"]["journal_db"])
        days = int(request.args.get("days", 30))
        analysis = journal.get_eod_analysis(days=days)
        return jsonify({"ok": True, "data": analysis})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/smart-money-bias")
def api_smart_money_bias():
    try:
        data = _build_result()
        flows_data = data.get("flows", {})
        return jsonify({"ok": True, "data": {
            "bias": flows_data.get("bias", "neutral"),
            "fii_net": flows_data.get("fii_net"),
            "dii_net": flows_data.get("dii_net"),
            "smart_money_score": flows_data.get("smart_money_score"),
        }})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/health")
def api_health():
    return jsonify({"ok": True, "ts": _ist_now().isoformat()})


@app.route("/api/cache/clear", methods=["POST"])
def api_cache_clear():
    _cache["ts"] = None
    _cache["data"] = None
    return jsonify({"ok": True, "message": "Cache cleared"})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
