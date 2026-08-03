from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# Sensibull oxide API base
_OXIDE_BASE = "https://oxide.sensibull.com/v1/compute/cache"

# Symbol → token mapping for NSE/BSE indices
_TOKEN_MAP: dict[str, str] = {
    "NIFTY": "256265",
    "BANKNIFTY": "260105",
    "FINNIFTY": "257801",
    "MIDCPNIFTY": "288009",
    "SENSEX": "265",
}

# Strike intervals per symbol
_INTERVAL_MAP: dict[str, int] = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCPNIFTY": 100,
    "SENSEX": 100,
}

_REQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://web.sensibull.com",
    "Referer": "https://web.sensibull.com/",
}

_SESSION_LOCK = threading.Lock()
_SESSION = None
_SESSION_TS = 0.0


def _get_session():
    """Get or initialize a persistent warmed-up session with platform identify."""
    global _SESSION, _SESSION_TS
    with _SESSION_LOCK:
        now = time.time()
        # Refresh session if it is older than 30 minutes
        if _SESSION is None or (now - _SESSION_TS) > 1800:
            try:
                from curl_cffi import requests as curl_requests
                session = curl_requests.Session(impersonate="chrome120")
            except ImportError:
                import requests
                session = requests.Session()

            session.headers.update(_REQ_HEADERS)
            try:
                # Warmed up request: hit platform identify first to set the HttpOnly access_token cookie
                r = session.get("https://oxide.sensibull.com/v1/pluto/auth/web/session/a/platform/identify", timeout=15)
                r.raise_for_status()
                _SESSION = session
                _SESSION_TS = now
                log.info("[sensibull] session successfully warmed up")
            except Exception as e:
                log.warning("[sensibull] session warm-up failed: %s", e)
                # Fallback to the session object anyway
                _SESSION = session
                _SESSION_TS = now
        return _SESSION


def _sensibull_get(url: str, timeout: int = 15) -> dict | None:
    """GET request to Sensibull oxide API using persistent warmed-up session, with retry.

    Handles 401 / invalid platform access token by fully clearing session cookies
    and forcing a fresh warm-up on retry.
    """
    global _SESSION
    session = _get_session()
    try:
        resp = session.get(url, timeout=timeout)
        # Detect 401 explicitly — token may have expired or be invalid
        if resp.status_code == 401:
            log.warning(
                "[sensibull] 401 on %s — forcing full session re-warmup", url
            )
            with _SESSION_LOCK:
                _SESSION = None
            # Create a completely fresh session (skip warm-up cache)
            try:
                from curl_cffi import requests as curl_requests
                session = curl_requests.Session(impersonate="chrome120")
            except ImportError:
                import requests
                session = requests.Session()
            session.headers.update(_REQ_HEADERS)
            # Warm up and assign
            r = session.get(
                "https://oxide.sensibull.com/v1/pluto/auth/web/session/a/platform/identify",
                timeout=timeout,
            )
            r.raise_for_status()
            with _SESSION_LOCK:
                _SESSION = session
                _SESSION_TS = time.time()
            # Retry once with the brand-new session
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            return resp.json()

        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(
            "[sensibull] GET request to %s failed: %s. Clearing session and retrying.",
            url,
            e,
        )
        # Clear the session so the next call to _get_session() warms up a new one
        with _SESSION_LOCK:
            _SESSION = None

        # Re-initialize and retry once
        session = _get_session()
        resp = session.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()


_SB_CACHE: dict[str, tuple[float, dict]] = {}
_SB_CACHE_LOCK = threading.Lock()
_SB_TTL = 60  # seconds


def fetch_option_chain(symbol: str, expiry: str | None = None) -> dict | None:
    """Fetch option chain from Sensibull oxide API (no auth required).

    Returns normalized format matching ShoonyaFetcher output:
    {
        "symbol": str,
        "underlying_price": float,
        "expiry": str,
        "strikes": [{"strike", "option_type", "ltp", "oi", "volume", "iv",
                      "delta", "theta", "gamma", "vega"}, ...],
        "all_expiries": list[str],
        "source": "sensibull",
    }
    """
    sym = symbol.upper().strip()

    now = time.time()
    cache_key = f"{sym}:{expiry or 'default'}"
    with _SB_CACHE_LOCK:
        if cache_key in _SB_CACHE:
            cached_time, cached_res = _SB_CACHE[cache_key]
            if now - cached_time < _SB_TTL:
                return cached_res

    token = _TOKEN_MAP.get(sym)
    if not token:
        log.warning("[sensibull] no token for '%s'", sym)
        return None

    try:
        raw = _sensibull_get(f"{_OXIDE_BASE}/live_derivative_prices/{token}")
    except Exception as e:
        log.warning("[sensibull] request failed for %s: %s", sym, e)
        return None

    if not raw or not isinstance(raw, dict):
        log.warning("[sensibull] empty or invalid response for %s", sym)
        return None

    data = raw.get("data")
    if not data:
        log.warning("[sensibull] no data in response for %s", sym)
        return None

    underlying = data.get("underlying_price")
    if underlying is None:
        log.warning("[sensibull] no underlying price for %s", sym)
        return None

    per_expiry = data.get("per_expiry_data", {})
    if not per_expiry:
        log.warning("[sensibull] no expiry data for %s", sym)
        return None

    # Pick target expiry: user-provided or nearest (chronologically)
    all_expiries = sorted(per_expiry.keys())
    if expiry:
        target_expiry = expiry
    else:
        target_expiry = all_expiries[0]

    exp_data = per_expiry.get(target_expiry)
    if not exp_data:
        log.warning("[sensibull] expiry '%s' not found; available: %s", target_expiry, all_expiries)
        return None

    opts = exp_data.get("options", [])
    if not opts:
        log.warning("[sensibull] no options for %s/%s", sym, target_expiry)
        return None

    atm_strike_num = exp_data.get("atm_strike") or round(underlying)
    interval = _INTERVAL_MAP.get(sym, 50)

    # Build token map
    token_map = {o["token"]: o for o in opts}
    used: set[int] = set()
    pairs: list[dict] = []

    # Phase 1: pair by token ±256, verify with delta
    for o in opts:
        t = o["token"]
        if t in used:
            continue
        partner = None
        for delta in (256, -256):
            pt = t + delta
            if pt in token_map and pt not in used:
                partner = token_map[pt]
                break
        if not partner:
            continue

        g1 = o.get("greeks_with_iv") or {}
        g2 = partner.get("greeks_with_iv") or {}
        d1, d2 = g1.get("delta", 0) or 0, g2.get("delta", 0) or 0

        if d1 >= d2:
            ce, pe = o, partner
        else:
            ce, pe = partner, o

        used.add(t)
        used.add(partner["token"])

        ceg = ce.get("greeks_with_iv") or {}
        peg = pe.get("greeks_with_iv") or {}
        pairs.append({
            "ce_ltp": ce.get("last_price", 0) or 0,
            "pe_ltp": pe.get("last_price", 0) or 0,
            "ce_delta": ceg.get("delta", 0) or 0,
            "pe_delta": peg.get("delta", 0) or 0,
            "ce_theta": ceg.get("theta", 0) or 0,
            "pe_theta": peg.get("theta", 0) or 0,
            "ce_gamma": ceg.get("gamma", 0) or 0,
            "pe_gamma": peg.get("gamma", 0) or 0,
            "ce_vega": ceg.get("vega", 0) or 0,
            "pe_vega": peg.get("vega", 0) or 0,
            "ce_iv": ceg.get("iv", 0) or 0,
            "pe_iv": peg.get("iv", 0) or 0,
            "ce_oi": ce.get("oi", 0) or 0,
            "pe_oi": pe.get("oi", 0) or 0,
            "ce_volume": ce.get("volume", 0) or 0,
            "pe_volume": pe.get("volume", 0) or 0,
        })

    # Phase 2: theta + opposite delta fallback for remaining
    remaining = [o for o in opts if o["token"] not in used]
    for o in remaining:
        t = o["token"]
        if t in used:
            continue
        g = o.get("greeks_with_iv") or {}
        theta = g.get("theta")
        delta = g.get("delta", 0) or 0
        if theta is None:
            used.add(t)
            continue
        best = None
        for pt, po in token_map.items():
            if pt in used or pt == t:
                continue
            pg = po.get("greeks_with_iv") or {}
            ptheta = pg.get("theta")
            pdelta = pg.get("delta", 0) or 0
            if ptheta == theta and (delta >= 0) != (pdelta >= 0):
                best = (pt, po)
                break
        if best:
            pt, po = best
            pg = po.get("greeks_with_iv") or {}
            pdelta = pg.get("delta", 0) or 0
            if delta >= 0:
                ce, pe = o, po
            else:
                ce, pe = po, o
            ceg2 = ce.get("greeks_with_iv") or {}
            peg2 = pe.get("greeks_with_iv") or {}
            pairs.append({
                "ce_ltp": ce.get("last_price", 0) or 0,
                "pe_ltp": pe.get("last_price", 0) or 0,
                "ce_delta": ceg2.get("delta", 0) or 0,
                "pe_delta": peg2.get("delta", 0) or 0,
                "ce_theta": ceg2.get("theta", 0) or 0,
                "pe_theta": peg2.get("theta", 0) or 0,
                "ce_gamma": ceg2.get("gamma", 0) or 0,
                "pe_gamma": peg2.get("gamma", 0) or 0,
                "ce_vega": ceg2.get("vega", 0) or 0,
                "pe_vega": peg2.get("vega", 0) or 0,
                "ce_iv": ceg2.get("iv", 0) or 0,
                "pe_iv": peg2.get("iv", 0) or 0,
                "ce_oi": ce.get("oi", 0) or 0,
                "pe_oi": pe.get("oi", 0) or 0,
                "ce_volume": ce.get("volume", 0) or 0,
                "pe_volume": pe.get("volume", 0) or 0,
            })
            used.add(t)
            used.add(pt)

    if not pairs:
        log.warning("[sensibull] no pairs constructed for %s/%s", sym, target_expiry)
        return None

    # Sort by CE LTP descending → increasing strike
    pairs.sort(key=lambda x: x["ce_ltp"], reverse=True)

    # Locate ATM: CE delta closest to 0.5
    valid_indices = [i for i, p in enumerate(pairs) if p["ce_delta"] is not None]
    if not valid_indices:
        log.warning("[sensibull] no valid deltas for %s", sym)
        return None
    atm_pair_idx = min(valid_indices, key=lambda i: abs(pairs[i]["ce_delta"] - 0.5))
    first_strike = round(atm_strike_num - atm_pair_idx * interval)

    # Build normalized strikes list
    strikes_out: list[dict] = []
    for i, p in enumerate(pairs):
        strike_price = round(first_strike + i * interval)
        strikes_out.append({
            "strike": float(strike_price),
            "option_type": "CE",
            "ltp": p["ce_ltp"],
            "oi": p["ce_oi"],
            "volume": p["ce_volume"],
            "iv": p["ce_iv"],
            "delta": p["ce_delta"],
            "theta": p["ce_theta"],
            "gamma": p["ce_gamma"],
            "vega": p["ce_vega"],
        })
        strikes_out.append({
            "strike": float(strike_price),
            "option_type": "PE",
            "ltp": p["pe_ltp"],
            "oi": p["pe_oi"],
            "volume": p["pe_volume"],
            "iv": p["pe_iv"],
            "delta": p["pe_delta"],
            "theta": p["pe_theta"],
            "gamma": p["pe_gamma"],
            "vega": p["pe_vega"],
        })

    if not strikes_out:
        log.warning("[sensibull] no strikes in normalized output for %s", sym)
        return None

    total_oi = sum(s.get("oi", 0) for s in strikes_out)
    total_ltp = sum(s.get("ltp", 0) for s in strikes_out)
    if total_oi == 0 and total_ltp == 0:
        log.warning("[sensibull] all-zero strikes for %s — discarding", sym)
        return None

    log.info(
        "[sensibull] %s | underlying=%.2f expiry=%s strikes=%d pairs=%d",
        sym, underlying, target_expiry, len(strikes_out), len(pairs),
    )

    res_dict = {
        "symbol": sym,
        "underlying_price": float(underlying),
        "expiry": str(target_expiry),
        "strikes": strikes_out,
        "all_expiries": all_expiries,
        "source": "sensibull",
    }
    with _SB_CACHE_LOCK:
        _SB_CACHE[cache_key] = (now, res_dict)

    return res_dict


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO)
    for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX"):
        r = fetch_option_chain(sym)
        if r:
            strikes = r["strikes"]
            n = len(strikes) // 2
            print(f"\n{sym}: {n} pairs, {len(strikes)} strikes")
            underlying = r["underlying_price"]
            atm_strike = min(strikes, key=lambda s: abs(s["strike"] - underlying))
            atm_ce = next(s for s in strikes if s["strike"] == atm_strike["strike"] and s["option_type"] == "CE")
            atm_pe = next(s for s in strikes if s["strike"] == atm_strike["strike"] and s["option_type"] == "PE")
            print(f"  ATM={atm_strike['strike']:.0f}: CE ltp={atm_ce['ltp']:.2f} d={atm_ce.get('delta',0):.2f} | PE ltp={atm_pe['ltp']:.2f} d={atm_pe.get('delta',0):.2f}")
        else:
            print(f"\n{sym}: FAILED")
