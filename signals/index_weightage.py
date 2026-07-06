import json
import logging
import os
from datetime import datetime, timedelta, timezone
import yfinance as yf

logger = logging.getLogger(__name__)

# Output cache path
CACHE_DIR = "./data/cache"
STATE_FILE = os.path.join(CACHE_DIR, "index_weights_state.json")

# Fixed list of constituents for top 20 NIFTY, top 20 SENSEX, and all 14 BANKNIFTY
CONSTITUENTS = {
    "NIFTY": [
        "HDFCBANK", "ICICIBANK", "RELIANCE", "BHARTIARTL", "LT",
        "SBIN", "AXISBANK", "INFY", "KOTAKBANK", "ITC",
        "TCS", "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA",
        "HCLTECH", "M&M", "ADANIENT", "ULTRACEMCO", "POWERGRID"
    ],
    "SENSEX": [
        "HDFCBANK", "RELIANCE", "ICICIBANK", "BHARTIARTL", "LT",
        "AXISBANK", "INFY", "BAJFINANCE", "TCS", "ITC",
        "KOTAKBANK", "HINDUNILVR", "HCLTECH", "MARUTI", "SUNPHARMA",
        "M&M", "ULTRACEMCO", "NTPC", "POWERGRID", "ASIANPAINT"
    ],
    "BANKNIFTY": [
        "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK",
        "FEDERALBNK", "INDUSINDBK", "AUBANK", "IDFCFIRSTB", "BANKBARODA",
        "PNB", "CANBK", "BANDHANBNK", "UNIONBANK"
    ]
}

# Curated static free-float factors representing the percentage of float shares
FREE_FLOAT_FACTORS = {
    "HDFCBANK": 1.00,
    "ICICIBANK": 1.00,
    "RELIANCE": 0.50,
    "BHARTIARTL": 0.45,
    "LT": 1.00,
    "SBIN": 0.43,
    "AXISBANK": 0.90,
    "INFY": 0.85,
    "KOTAKBANK": 0.74,
    "ITC": 1.00,
    "TCS": 0.28,
    "HINDUNILVR": 0.38,
    "BAJFINANCE": 0.45,
    "MARUTI": 0.43,
    "SUNPHARMA": 0.46,
    "HCLTECH": 0.39,
    "M&M": 0.48,
    "ADANIENT": 0.35,
    "ULTRACEMCO": 0.40,
    "POWERGRID": 0.49,
    "NTPC": 0.49,
    "ASIANPAINT": 0.47,
    "FEDERALBNK": 1.00,
    "INDUSINDBK": 0.85,
    "AUBANK": 0.75,
    "IDFCFIRSTB": 0.60,
    "BANKBARODA": 0.36,
    "PNB": 0.27,
    "CANBK": 0.37,
    "BANDHANBNK": 0.60,
    "UNIONBANK": 0.25,
}

# Baseline weightages to fall back on if yfinance is entirely unreachable on first run
BASELINE_WEIGHTS = {
    "NIFTY": {
        "HDFCBANK": 11.18, "ICICIBANK": 9.01, "RELIANCE": 8.00, "BHARTIARTL": 5.15, "LT": 4.44,
        "SBIN": 3.88, "AXISBANK": 3.54, "INFY": 3.21, "KOTAKBANK": 2.64, "ITC": 2.53,
        "TCS": 2.50, "HINDUNILVR": 2.20, "BAJFINANCE": 2.10, "MARUTI": 1.90, "SUNPHARMA": 1.80,
        "HCLTECH": 1.70, "M&M": 1.60, "ADANIENT": 1.50, "ULTRACEMCO": 1.40, "POWERGRID": 1.30
    },
    "SENSEX": {
        "HDFCBANK": 13.40, "ICICIBANK": 10.80, "RELIANCE": 9.80, "BHARTIARTL": 6.30, "LT": 5.00,
        "AXISBANK": 4.10, "INFY": 3.80, "BAJFINANCE": 3.10, "TCS": 3.00, "ITC": 2.90,
        "KOTAKBANK": 2.80, "HINDUNILVR": 2.50, "HCLTECH": 2.20, "MARUTI": 2.00, "SUNPHARMA": 1.90,
        "M&M": 1.80, "ULTRACEMCO": 1.60, "NTPC": 1.50, "POWERGRID": 1.40, "ASIANPAINT": 1.30
    },
    "BANKNIFTY": {
        "HDFCBANK": 19.30, "ICICIBANK": 14.16, "SBIN": 10.02, "AXISBANK": 9.59, "KOTAKBANK": 9.31,
        "FEDERALBNK": 6.67, "INDUSINDBK": 4.99, "AUBANK": 4.64, "IDFCFIRSTB": 4.36, "BANKBARODA": 4.00,
        "PNB": 3.50, "CANBK": 3.00, "BANDHANBNK": 2.50, "UNIONBANK": 2.00
    }
}


def _now_ist() -> datetime:
    return datetime.now(timezone(timedelta(hours=5, minutes=30)))


def load_index_weights_state() -> dict:
    """Load cached weights state from state file, falling back to baselines."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                if "weights" in state and all(idx in state["weights"] for idx in CONSTITUENTS):
                    return state
        except Exception as e:
            logger.warning("Failed to load index weights state: %s", e)

    # Return default baseline state if cache is missing/corrupted
    return {
        "last_refresh": None,
        "status": "BASELINE",
        "weights": BASELINE_WEIGHTS
    }


def refresh_weights_if_needed(force: bool = False) -> bool:
    """Checks if weights need weekly refresh (every Monday) and triggers updates from yfinance."""
    now_ist = _now_ist()
    state = load_index_weights_state()
    last_refresh_str = state.get("last_refresh")

    need_refresh = force or (last_refresh_str is None)
    if not need_refresh and last_refresh_str:
        try:
            last_dt = datetime.fromisoformat(last_refresh_str)
            # Find the starting Monday of both weeks
            last_monday = (last_dt.date() - timedelta(days=last_dt.weekday()))
            current_monday = (now_ist.date() - timedelta(days=now_ist.weekday()))
            if current_monday > last_monday:
                need_refresh = True
        except Exception:
            need_refresh = True

    if not need_refresh:
        return False

    logger.info("Triggering weekly index components weightage refresh...")
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        # Collect all unique symbols to query in a single batch
        all_symbols = set()
        for idx_syms in CONSTITUENTS.values():
            all_symbols.update(idx_syms)

        # yfinance tickers expect .NS suffix for NSE stocks
        yf_symbols = [f"{s}.NS" for s in all_symbols]
        tickers = yf.Tickers(" ".join(yf_symbols))

        # Build a map of symbol -> market_cap
        mcap_map = {}
        for s in all_symbols:
            yf_sym = f"{s}.NS"
            try:
                ticker = tickers.tickers[yf_sym]
                mcap = ticker.fast_info.market_cap
                if mcap and mcap > 0:
                    mcap_map[s] = mcap
            except Exception as e:
                logger.warning("Could not fetch market cap for %s from yfinance: %s", s, e)

        # Calculate new weights
        new_weights = {}
        for index_name, symbols in CONSTITUENTS.items():
            ff_mcaps = {}
            for s in symbols:
                mcap = mcap_map.get(s)
                if not mcap:
                    # Fallback to a default proportional market cap from baseline if missing
                    baseline_wt = BASELINE_WEIGHTS[index_name].get(s, 1.0)
                    mcap = baseline_wt * 1e11  # scale it to some dummy high value

                factor = FREE_FLOAT_FACTORS.get(s, 0.50)
                ff_mcaps[s] = mcap * factor

            total_ff_mcap = sum(ff_mcaps.values())
            if total_ff_mcap > 0:
                new_weights[index_name] = {
                    s: round((ff_mcap / total_ff_mcap) * 100, 2)
                    for s, ff_mcap in ff_mcaps.items()
                }
            else:
                new_weights[index_name] = BASELINE_WEIGHTS[index_name]

        # Save to state file
        new_state = {
            "last_refresh": now_ist.isoformat(),
            "status": "SUCCESS",
            "weights": new_weights
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(new_state, f, indent=2)

        logger.info("Successfully refreshed index weightages.")
        return True

    except Exception as e:
        logger.exception("Failed to dynamically update index weights: %s", e)
        # Keep old weights but mark the attempt as failed in logs
        return False


def calculate_weighted_momentum(index_name: str) -> dict:
    """Fetch live returns for constituents and calculate weighted index momentum."""
    index_name = index_name.upper()
    if index_name not in CONSTITUENTS:
        return {
            "weighted_momentum": 0.0,
            "bullish_count": 0,
            "bearish_count": 0,
            "total_weight_captured": 0.0,
            "constituents": []
        }

    state = load_index_weights_state()
    weights = state["weights"].get(index_name, BASELINE_WEIGHTS[index_name])

    from data import feed
    symbols = CONSTITUENTS[index_name]
    
    # Single batch call to retrieve quotes for all constituents
    quotes = feed.quote_batch(symbols)

    weighted_sum = 0.0
    valid_weight_sum = 0.0
    bullish_count = 0
    bearish_count = 0
    details = []

    for s in symbols:
        weight = weights.get(s, 0.0)
        q = quotes.get(s, {})
        change_pct = q.get("change_pct")
        ltp = q.get("ltp")

        # Fallback to yfinance if primary feeds don't have change_pct
        if change_pct is None:
            try:
                # Attempt to get prev close and ltp
                prev = q.get("prev_close")
                if ltp and prev:
                    change_pct = round(100 * (ltp - prev) / prev, 2)
            except Exception:
                pass

        if change_pct is not None:
            weighted_sum += (weight * change_pct)
            valid_weight_sum += weight
            if change_pct > 0.05:
                bullish_count += 1
            elif change_pct < -0.05:
                bearish_count += 1

        details.append({
            "symbol": s,
            "weight": weight,
            "ltp": ltp,
            "change_pct": change_pct
        })

    # Sort details by weight descending
    details.sort(key=lambda x: x["weight"], reverse=True)

    weighted_momentum = 0.0
    if valid_weight_sum > 0:
        weighted_momentum = round(weighted_sum / valid_weight_sum, 2)

    return {
        "weighted_momentum": weighted_momentum,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
        "total_weight_captured": round(valid_weight_sum, 2),
        "constituents": details
    }
