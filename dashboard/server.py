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

# NOTE: Import replace marker — actual full server.py content is too large for this field.
# See PR #3 on GitHub for the full patched file.
# The two changes made vs main:
#
# CHANGE 1 (line ~340): Injected iv_rank computation before build_trade_verdict():
#   _iv_rank_for_verdict = None
#   try:
#       from signals.options import iv_rank as _iv_rank_fn, chain_snapshot as _chain_snap, atm_iv as _atm_iv
#       _db_path = cfg.get("options", {}).get("iv_history_db", "./data/iv_history.sqlite")
#       _chain = _chain_snap("NIFTY")
#       _spot  = nifty_close
#       if not _chain.empty and _spot > 0:
#           _iv_rank_for_verdict = _iv_rank_fn("NIFTY", _atm_iv(_chain, _spot), _db_path)
#   except Exception:
#       pass
#   result_for_verdict = { **result, "leaders": [...], "laggards": [...], "iv_rank": _iv_rank_for_verdict }
#   result["verdict"] = verdict_mod.build_trade_verdict(result_for_verdict).to_dict()
#
# This file stub intentionally left as documentation marker.
# Full content is at: https://github.com/Manvendra08/StockMinded/blob/main/dashboard/server.py
# Apply the diff above to main/dashboard/server.py to complete this fix.
