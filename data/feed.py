"""Data feed: OHLC, option chain, FII/DII, VIX. yfinance + nsepython."""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

CACHE_DIR = Path(__file__).parent.parent / "data/cache"


# Persistent session for NSE to avoid hitting home page every time
_NSE_SESSION = None
_NSE_SESSION_TS = 0
_NSE_SESSION_LOCK = threading.Lock()
_DHAN_OC_CACHE: dict[str, tuple[float, dict]] = {}
_DHAN_MASTER_CACHE: pd.DataFrame | None = None
_OPTION_CHAIN_SOURCE: dict[str, str] = {}

# Track which data source served each symbol for dashboard visibility.
# Populated by quote_batch(), ohlc(), universe_ohlc().
_QUOTE_SOURCE: dict[str, str] = {}
_OHLC_SOURCE: dict[str, str] = {}


def _create_retry_session(
    retries=5, backoff_factor=1, status_forcelist=(500, 502, 503, 504)
):
    session = requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        allowed_methods=["HEAD", "GET", "OPTIONS", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def _get_nse_session():
    global _NSE_SESSION, _NSE_SESSION_TS
    with _NSE_SESSION_LOCK:
        now = time.time()
        if _NSE_SESSION is None or (now - _NSE_SESSION_TS) > 600:
            success = False
            for attempt in range(3):
                try:
                    # Leverage curl_cffi for robust browser TLS/JA3 impersonation
                    try:
                        from curl_cffi import requests as curl_requests

                        session = curl_requests.Session(impersonate="chrome120")
                    except ImportError:
                        session = _create_retry_session(retries=5, backoff_factor=1)

                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "*/*",
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Connection": "keep-alive",
                        "Referer": "https://www.nseindia.com/",
                    }
                    session.headers.update(headers)

                    # Some versions of NSE block if you don't have the cookies from the main page.
                    # We hit the main page first.
                    r = session.get("https://www.nseindia.com", timeout=15)
                    # If home page fails, try a slightly different approach
                    if r.status_code != 200:
                        headers["User-Agent"] = (
                            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                        )
                        session.headers.update(headers)
                        r = session.get("https://www.nseindia.com", timeout=15)

                    r.raise_for_status()
                    # Optional: hit a market data page to solidify the session
                    session.get(
                        "https://www.nseindia.com/market-data/live-equity-market",
                        timeout=10,
                    )

                    _NSE_SESSION = session
                    _NSE_SESSION_TS = now
                    success = True
                    logging.getLogger(__name__).debug(
                        "[_get_nse_session] session warmed up"
                    )
                    break
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "[_get_nse_session] warm-up attempt %s failed: %s: %s",
                        attempt + 1,
                        type(e).__name__,
                        e,
                    )
                    time.sleep(2 * (attempt + 1))

            if not success:
                _NSE_SESSION = None
    return _NSE_SESSION


YF_SYMBOL = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "INDIAVIX": "^INDIAVIX",
    "NIFTY IT": "^CNXIT",
    "NIFTY BANK": "^NSEBANK",
    "NIFTY AUTO": "^CNXAUTO",
    "NIFTY PHARMA": "^CNXPHARMA",
    "NIFTY FMCG": "^CNXFMCG",
    "NIFTY METAL": "^CNXMETAL",
    "NIFTY ENERGY": "^CNXENERGY",
    "NIFTY REALTY": "^CNXREALTY",
    "NIFTY INFRA": "^CNXINFRA",
    "NIFTY PSE": "^CNXPSE",
    "NIFTY PVT BANK": "NIFTY_PVT_BANK.NS",
    "USDINR": "INR=X",
    "CRUDE": "CL=F",
    "GOLD": "GC=F",
}


def _data_sources_cfg() -> dict:
    try:
        from config.loader import load_config

        return load_config().get("data_sources", {})
    except Exception:
        return {}


def _env_or_value(value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("${") and value.endswith("}"):
        import os

        return os.getenv(value[2:-1])
    return value


def _broker_cfg() -> dict:
    try:
        from config.loader import load_config

        return load_config().get("broker", {})
    except Exception:
        return {}


def _dhan_credentials() -> tuple[str | None, str | None]:
    cfg = _broker_cfg()
    dhan = cfg.get("dhan", {}) if isinstance(cfg.get("dhan"), dict) else {}
    client_id = _env_or_value(dhan.get("client_id") or cfg.get("client_id"))
    access_token = _env_or_value(dhan.get("access_token") or cfg.get("access_token"))
    return client_id, access_token


def _dhan_headers() -> dict | None:
    client_id, access_token = _dhan_credentials()
    if not client_id or not access_token:
        return None
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "client-id": str(client_id),
        "access-token": str(access_token),
    }


def _dhan_enabled() -> bool:
    cfg = _broker_cfg()
    ds = _data_sources_cfg().get("dhan", {})
    enabled = ds.get("enabled", False) is True
    return cfg.get("provider") == "dhan" and enabled and _dhan_headers() is not None


def _dhan_underlying(symbol: str) -> tuple[int, str] | None:
    cfg = _data_sources_cfg().get("dhan", {})
    underlyings = (
        cfg.get("underlyings", {}) if isinstance(cfg.get("underlyings"), dict) else {}
    )
    raw = underlyings.get(symbol.upper())
    if isinstance(raw, dict):
        return int(raw.get("security_id")), str(raw.get("segment", "IDX_I"))
    defaults = {
        "NIFTY": (13, "IDX_I"),
        "BANKNIFTY": (25, "IDX_I"),
        "FINNIFTY": (27, "IDX_I"),
        "MIDCPNIFTY": (442, "IDX_I"),
    }
    return defaults.get(symbol.upper())


def _dhan_period_dates(period: str) -> tuple[str, str]:
    today = dt.date.today()
    qty = int("".join(ch for ch in period if ch.isdigit()) or "1")
    unit = "".join(ch for ch in period if ch.isalpha()).lower()
    days = qty
    if unit in ("mo", "m"):
        days = qty * 31
    elif unit == "y":
        days = qty * 366
    elif unit in ("wk", "w"):
        days = qty * 7
    start = today - dt.timedelta(days=days)
    return start.isoformat(), (today + dt.timedelta(days=1)).isoformat()


def _dhan_interval(interval: str) -> str:
    if interval.endswith("m"):
        val = interval[:-1]
        return val if val in ("1", "5", "15", "25", "60") else ""
    if interval.endswith("min"):
        val = interval[:-3]
        return val if val in ("1", "5", "15", "25", "60") else ""
    if interval in ("1", "5", "15", "25", "60"):
        return interval
    return ""


def _dhan_col(df: pd.DataFrame, *names: str) -> str | None:
    lookup = {str(c).lower().replace("_", "").replace(" ", ""): c for c in df.columns}
    for name in names:
        key = name.lower().replace("_", "").replace(" ", "")
        if key in lookup:
            return lookup[key]
    return None


def _dhan_master() -> pd.DataFrame:
    global _DHAN_MASTER_CACHE
    if _DHAN_MASTER_CACHE is not None:
        return _DHAN_MASTER_CACHE
    cache_file = CACHE_DIR / "dhan_api_scrip_master.csv"
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 86400:
        _DHAN_MASTER_CACHE = pd.read_csv(cache_file, low_memory=False)
        return _DHAN_MASTER_CACHE
    url = (
        _data_sources_cfg()
        .get("dhan", {})
        .get(
            "instrument_master_url",
            "https://images.dhan.co/api-data/api-scrip-master.csv",
        )
    )
    df = pd.read_csv(url, low_memory=False)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_file, index=False)
    except Exception as e:
        logging.getLogger(__name__).exception(
            "Failed to write Dhan master cache: %s", e
        )
    _DHAN_MASTER_CACHE = df
    return df


def _dhan_find_instrument(symbol: str) -> dict | None:
    symbol = symbol.upper().replace(".NS", "")
    under = _dhan_underlying(symbol)
    if under:
        return {
            "security_id": str(under[0]),
            "segment": under[1],
            "instrument": "INDEX",
        }
    try:
        df = _dhan_master()
    except Exception as e:
        logging.getLogger(__name__).exception("Failed to load Dhan master data: %s", e)
        return None
    sec_col = _dhan_col(df, "SEM_SMST_SECURITY_ID", "security_id", "SECURITY_ID")
    exch_col = _dhan_col(df, "SEM_EXM_EXCH_ID", "EXCH_ID")
    seg_col = _dhan_col(df, "SEM_SEGMENT", "SEGMENT")
    inst_col = _dhan_col(df, "SEM_INSTRUMENT_NAME", "INSTRUMENT")
    sym_col = _dhan_col(df, "SM_SYMBOL_NAME", "SYMBOL_NAME", "UNDERLYING_SYMBOL")
    disp_col = _dhan_col(df, "SEM_CUSTOM_SYMBOL", "DISPLAY_NAME")
    trad_col = _dhan_col(df, "SEM_TRADING_SYMBOL", "TRADING_SYMBOL")
    if not sec_col:
        return None
    work = df
    if exch_col:
        work = work[work[exch_col].astype(str).str.upper().eq("NSE")]
    if seg_col:
        work = work[
            work[seg_col]
            .astype(str)
            .str.upper()
            .isin(["E", "D", "IDX_I", "NSE_EQ", "NSE_FNO"])
        ]
    candidates = []
    for col in [sym_col, disp_col, trad_col]:
        if col:
            hit = work[work[col].astype(str).str.upper().eq(symbol)]
            if not hit.empty:
                candidates.append(hit)
    if not candidates:
        return None
    row = candidates[0].iloc[0]
    seg_raw = str(row[seg_col]).upper() if seg_col else "E"
    inst_raw = str(row[inst_col]).upper() if inst_col else "EQUITY"
    segment = (
        "NSE_EQ"
        if seg_raw in ("E", "NSE_EQ")
        else ("NSE_FNO" if seg_raw in ("D", "NSE_FNO") else seg_raw)
    )
    instrument = (
        "EQUITY" if "EQUITY" in inst_raw or seg_raw in ("E", "NSE_EQ") else inst_raw
    )
    return {
        "security_id": str(row[sec_col]),
        "segment": segment,
        "instrument": instrument,
    }


def _dhan_post(path: str, payload: dict) -> dict:
    headers = _dhan_headers()
    if not headers:
        return {}
    response = requests.post(
        f"https://api.dhan.co/v2/{path}", headers=headers, json=payload, timeout=15
    )
    response.raise_for_status()
    data = response.json()
    if data.get("status") not in (None, "success"):
        raise RuntimeError(f"Dhan {path} failed: {data}")
    return data


def _dhan_frame(raw: dict) -> pd.DataFrame:
    if not raw or not raw.get("timestamp"):
        return pd.DataFrame()
    df = pd.DataFrame(
        {
            "open": raw.get("open", []),
            "high": raw.get("high", []),
            "low": raw.get("low", []),
            "close": raw.get("close", []),
            "volume": raw.get("volume", []),
        }
    )
    ts = pd.to_datetime(raw.get("timestamp", []), unit="s", errors="coerce")
    df.index = ts
    df.index.name = "date"
    if raw.get("open_interest") is not None:
        df["open_interest"] = raw.get("open_interest", [])
    return df.dropna(how="all")


def _dhan_ohlc(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    if not _dhan_enabled():
        return pd.DataFrame()
    inst = _dhan_find_instrument(symbol)
    if not inst:
        return pd.DataFrame()
    start, end = _dhan_period_dates(period)
    if interval in ("1d", "1D", "day"):
        payload = {
            "securityId": str(inst["security_id"]),
            "exchangeSegment": inst["segment"],
            "instrument": inst["instrument"],
            "expiryCode": 0,
            "oi": False,
            "fromDate": start,
            "toDate": end,
        }
        return _dhan_frame(_dhan_post("charts/historical", payload))
    if _dhan_interval(interval) not in ("1", "5", "15", "25", "60"):
        return pd.DataFrame()
    payload = {
        "securityId": str(inst["security_id"]),
        "exchangeSegment": inst["segment"],
        "instrument": inst["instrument"],
        "interval": _dhan_interval(interval),
        "oi": False,
        "fromDate": f"{start} 09:15:00",
        "toDate": f"{end} 15:30:00",
    }
    return _dhan_frame(_dhan_post("charts/intraday", payload))


def quote_batch(symbols: list[str]) -> dict[str, dict]:
    """LTP/OHLC snapshot. Shoonya primary (NFO futures for F&O stocks),
    Dhan fallback, per symbol."""
    out: dict[str, dict] = {s: {} for s in symbols}

    # Phase 0: Try Shoonya for each symbol (primary source)
    try:
        from data.shoonya_fetcher import get_shoonya

        shoonya = get_shoonya()
        if shoonya and shoonya.login():
            for sym in list(out.keys()):
                if out[sym].get("ltp"):
                    continue  # already populated
                try:
                    # Try NFO futures quote first (gives full OHLC+OI for F&O stocks)
                    q = shoonya.fetch_fno_quote(sym)
                    if q and q.get("ltp"):
                        out[sym] = q
                        out[sym]["source"] = "shoonya_fno"
                        _QUOTE_SOURCE[sym] = "shoonya"
                        continue
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "[quote_batch] shoonya fetch_fno_quote failed for %s: %s",
                        sym,
                        e,
                    )
                try:
                    # Fall back to spot quote (ltp only)
                    q = shoonya.fetch_quote(sym)
                    if q and q.get("ltp"):
                        out[sym] = q
                        out[sym]["source"] = "shoonya_quote"
                        _QUOTE_SOURCE[sym] = "shoonya"
                except Exception as e:
                    logging.getLogger(__name__).warning(
                        "[quote_batch] shoonya fetch_quote failed for %s: %s", sym, e
                    )
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[quote_batch] shoonya initialization failed: %s", e
        )

    return out


def _dhan_fill_quotes(
    grouped: dict[str, list[int]],
    reverse: dict[tuple[str, str], str],
    out: dict[str, dict],
) -> None:
    """Fill quote data from Dhan API for symbols in grouped/reverse."""
    try:
        data = {}
        for seg, ids in grouped.items():
            try:
                raw = _dhan_post("marketfeed/quote", {seg: ids})
                data.update(raw.get("data") or {})
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "[dhan quote_batch] segment %s failed: %s", seg, e
                )
        for seg, rows in (data or {}).items():
            for sid, item in (rows or {}).items():
                sym = reverse.get((seg, str(sid)))
                if not sym or out.get(sym, {}).get("ltp"):
                    continue
                ohlc_data = item.get("ohlc") or {}
                prev = ohlc_data.get("close")
                ltp = item.get("last_price")
                out[sym] = {
                    "ltp": ltp,
                    "open": ohlc_data.get("open"),
                    "high": ohlc_data.get("high"),
                    "low": ohlc_data.get("low"),
                    "prev_close": prev,
                    "volume": item.get("volume"),
                    "oi": item.get("oi"),
                    "change_pct": round(100 * (ltp - prev) / prev, 2)
                    if ltp and prev
                    else None,
                    "source": "dhan_quote",
                }
    except Exception as e:
        print(f"[dhan quote_batch] failed: {e}")


def get_data_sources() -> dict:
    """Return aggregated data source info for dashboard display.

    Returns:
        {
            "primary": str,          # overall primary source label
            "quotes": str | None,    # "shoonya" | "dhan" | "yfinance" | None
            "ohlc": str | None,      # "dhan_historical" | "yfinance" | None
            "option_chain": str | None,  # per-symbol or global source
            "option_chain_detail": dict  # {symbol: source}
        }
    """
    # Determine primary quote source from _QUOTE_SOURCE
    quote_sources = set(_QUOTE_SOURCE.values())
    quote_primary = None
    if "shoonya" in quote_sources:
        quote_primary = "shoonya"
    elif "dhan" in quote_sources:
        quote_primary = "dhan"

    # Determine primary OHLC source from _OHLC_SOURCE
    ohlc_sources = set(_OHLC_SOURCE.values())
    ohlc_primary = None
    if "dhan_historical" in ohlc_sources:
        ohlc_primary = "dhan"
    elif "yfinance" in ohlc_sources:
        ohlc_primary = "yfinance"
    elif "cache" in ohlc_sources:
        ohlc_primary = "cache"

    # Option chain sources — normalize to simple names for dashboard display
    _OC_SOURCE_MAP = {
        "shoonya": "shoonya",
        "dhan_optionchain": "dhan",
        "public_dhan_optionchain": "dhan",
        "research360": "research360",
        "research360+dhan_ltp": "dhan",
        "ai_scraper": "ai",
        "local_json": "local",
    }
    oc_sources = dict(_OPTION_CHAIN_SOURCE)
    oc_primary = None
    for sym, src in oc_sources.items():
        if src and not oc_primary:
            # Extract base source name (strip suffixes like :shoonya, /something)
            base = src.split(":")[0].split("/")[0]
            oc_primary = _OC_SOURCE_MAP.get(base, base)
            break

    # Compute overall primary label
    if quote_primary == "shoonya":
        overall = "shoonya"
    elif oc_primary == "shoonya":
        overall = "shoonya"
    elif quote_primary:
        overall = quote_primary
    elif oc_primary:
        overall = oc_primary
    elif ohlc_primary:
        overall = ohlc_primary
    else:
        overall = "yfinance"

    return {
        "primary": overall,
        "quotes": quote_primary,
        "ohlc": ohlc_primary,
        "option_chain": oc_primary,
        "option_chain_detail": oc_sources,
        "quote_detail": dict(_QUOTE_SOURCE),
        "ohlc_detail": dict(_OHLC_SOURCE),
    }


def ltp(symbol: str) -> float | None:
    q = quote_batch([symbol]).get(symbol) or {}
    if q.get("ltp"):
        return round(float(q["ltp"]), 2)
    try:
        yf_sym = YF_SYMBOL.get(symbol) or (
            f"{symbol}.NS"
            if not symbol.startswith("^") and "." not in symbol
            else symbol
        )
        info = _yf().Ticker(yf_sym).fast_info
        return round(float(info.last_price), 2) if info.last_price else None
    except Exception:
        return None


def quote_batch_public(symbols: list[str]) -> dict[str, dict]:
    """Fetch LTP/prev_close/change_pct from Dhan public F&O page (no auth needed).

    Scrapes https://dhan.co/futures-stocks-list/ which has a complete
    list of all F&O stocks with live prices. No API key required.
    Returns dict[symbol, {ltp, prev_close, change_pct, open, volume, source}]
    or {} for symbols not found on the page.
    """
    import json
    import re

    out: dict[str, dict] = {s: {} for s in symbols}
    sym_set = {s.upper().replace(".NS", "") for s in symbols}

    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        }
        r = requests.get(
            "https://dhan.co/futures-stocks-list/",
            headers=headers,
            timeout=10,
        )
        if r.status_code != 200:
            return out

        match = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            r.text,
            re.DOTALL,
        )
        if not match:
            return out

        js = json.loads(match.group(1))
        items = (
            js.get("props", {}).get("pageProps", {}).get("listData", {}).get("data", [])
        )

        for item in items:
            raw_sym = (item.get("Sym") or "").upper().strip()
            if not raw_sym or raw_sym not in sym_set:
                continue

            ltp = item.get("Ltp")
            prev_close = item.get("BcClose")
            chg_pct = item.get("PPerchange")  # already in percent (0.313 = 0.313%)

            out[raw_sym] = {
                "ltp": ltp,
                "prev_close": prev_close,
                "change_pct": chg_pct,
                "open": item.get("Open"),
                "volume": item.get("Volume"),
                "source": "dhan_public",
            }
    except Exception as e:
        logging.getLogger(__name__).warning("[quote_batch_public] failed: %s", e)

    return out


def _dhan_expiry(symbol: str, underlying_scrip: int, underlying_seg: str) -> str | None:
    cache_file = CACHE_DIR / f"dhan_expiries_{symbol.upper()}.json"
    try:
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 3600:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            expiries = cached.get("data") or []
            return sorted(expiries)[0] if expiries else None
    except Exception as e:
        logging.getLogger(__name__).exception("Failed to read Dhan expiry cache: %s", e)

    payload = {"UnderlyingScrip": underlying_scrip, "UnderlyingSeg": underlying_seg}
    raw = _dhan_post("optionchain/expirylist", payload)
    expiries = raw.get("data") or []
    if expiries:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"ts": time.time(), "data": expiries}), encoding="utf-8"
            )
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Failed to write Dhan expiry cache: %s", e
            )
        return sorted(expiries)[0]
    return None


def _dhan_to_nse_chain(symbol: str, raw: dict, expiry: str) -> dict:
    data = raw.get("data") or {}
    oc = data.get("oc") or {}
    records = []
    for strike_raw, row in oc.items():
        try:
            strike = float(strike_raw)
        except (TypeError, ValueError):
            continue
        ce = row.get("ce") if isinstance(row.get("ce"), dict) else {}
        pe = row.get("pe") if isinstance(row.get("pe"), dict) else {}
        records.append(
            {
                "strikePrice": strike,
                "expiryDate": expiry,
                "CE": {
                    "openInterest": ce.get("oi", 0) or 0,
                    "changeinOpenInterest": (ce.get("oi", 0) or 0)
                    - (ce.get("previous_oi", 0) or 0),
                    "totalTradedVolume": ce.get("volume", 0) or 0,
                    "lastPrice": ce.get("last_price", 0) or 0,
                    "impliedVolatility": ce.get("implied_volatility", 0) or 0,
                    "bidprice": ce.get("top_bid_price", 0) or 0,
                    "askPrice": ce.get("top_ask_price", 0) or 0,
                    "identifier": ce.get("security_id"),
                },
                "PE": {
                    "openInterest": pe.get("oi", 0) or 0,
                    "changeinOpenInterest": (pe.get("oi", 0) or 0)
                    - (pe.get("previous_oi", 0) or 0),
                    "totalTradedVolume": pe.get("volume", 0) or 0,
                    "lastPrice": pe.get("last_price", 0) or 0,
                    "impliedVolatility": pe.get("implied_volatility", 0) or 0,
                    "bidprice": pe.get("top_bid_price", 0) or 0,
                    "askPrice": pe.get("top_ask_price", 0) or 0,
                    "identifier": pe.get("security_id"),
                },
            }
        )
    return {
        "records": {
            "data": records,
            "expiryDates": [expiry],
            "underlyingValue": data.get("last_price"),
        },
        "filtered": {"data": records},
        "_source": "dhan_optionchain",
    }


def _option_chain_from_dhan(symbol: str) -> dict:
    if not _dhan_enabled():
        return {"records": {"data": []}}
    underlying = _dhan_underlying(symbol)
    if not underlying:
        return {"records": {"data": []}}
    cached = _DHAN_OC_CACHE.get(symbol)
    if cached and time.time() - cached[0] < 30:
        return cached[1]
    underlying_scrip, underlying_seg = underlying
    expiry = _dhan_expiry(symbol, underlying_scrip, underlying_seg)
    if not expiry:
        return {"records": {"data": []}}
    payload = {
        "UnderlyingScrip": underlying_scrip,
        "UnderlyingSeg": underlying_seg,
        "Expiry": expiry,
    }
    raw = _dhan_post("optionchain", payload)
    data = _dhan_to_nse_chain(symbol, raw, expiry)
    if data.get("records", {}).get("data"):
        _DHAN_OC_CACHE[symbol] = (time.time(), data)
    return data


def _public_dhan_to_nse_chain(symbol: str, raw: dict, expiry: str) -> dict:
    data = raw.get("data") or {}
    oc = data.get("oc") or {}
    records = []
    for strike_raw, row in oc.items():
        try:
            strike = float(strike_raw)
        except (TypeError, ValueError):
            continue
        ce = row.get("ce") if isinstance(row.get("ce"), dict) else {}
        pe = row.get("pe") if isinstance(row.get("pe"), dict) else {}
        records.append(
            {
                "strikePrice": strike,
                "expiryDate": expiry,
                "CE": {
                    "openInterest": ce.get("OI", 0) or 0,
                    "changeinOpenInterest": ce.get("oichng", 0) or 0,
                    "totalTradedVolume": ce.get("vol", 0) or 0,
                    "lastPrice": ce.get("ltp", 0) or 0,
                    "impliedVolatility": ce.get("iv", 0) or 0,
                    "bidprice": ce.get("bid", 0) or 0,
                    "askPrice": ce.get("ask", 0) or 0,
                    "identifier": ce.get("sid"),
                },
                "PE": {
                    "openInterest": pe.get("OI", 0) or 0,
                    "changeinOpenInterest": pe.get("oichng", 0) or 0,
                    "totalTradedVolume": pe.get("vol", 0) or 0,
                    "lastPrice": pe.get("ltp", 0) or 0,
                    "impliedVolatility": pe.get("iv", 0) or 0,
                    "bidprice": pe.get("bid", 0) or 0,
                    "askPrice": pe.get("ask", 0) or 0,
                    "identifier": pe.get("sid"),
                },
            }
        )
    return {
        "records": {
            "data": records,
            "expiryDates": [expiry],
            "underlyingValue": data.get("sltp"),
        },
        "filtered": {"data": records},
        "_source": "public_dhan_optionchain",
    }


def _option_chain_from_public_dhan(symbol: str) -> dict:
    symbol = symbol.upper()
    public_urls = {
        "NIFTY": "https://dhan.co/indices/nifty-50-option-chain/",
        "BANKNIFTY": "https://dhan.co/indices/nifty-bank-option-chain/",
        "FINNIFTY": "https://dhan.co/indices/nifty-financial-services-option-chain/",
        "MIDCPNIFTY": "https://dhan.co/indices/nifty-midcap-select-option-chain/",
    }
    url = public_urls.get(symbol)
    seg = 0
    sid = None
    if url:
        defaults = {
            "NIFTY": 13,
            "BANKNIFTY": 25,
            "FINNIFTY": 27,
            "MIDCPNIFTY": 442,
        }
        sid = defaults.get(symbol)
    else:
        url = "https://dhan.co/indices/nifty-50-option-chain/"
        seg = 1
        inst = _dhan_find_instrument(symbol)
        if inst:
            sid = int(inst["security_id"])
    if not sid:
        return {"records": {"data": []}}
    try:
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate="chrome120")
    except ImportError:
        session = _create_retry_session(retries=3, backoff_factor=0.5)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Connection": "keep-alive",
    }
    r = session.get(url, headers=headers, timeout=15)
    r.raise_for_status()
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(r.text, "html.parser")
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return {"records": {"data": []}}
    data = json.loads(script.string)
    page_props = data.get("props", {}).get("pageProps", {})
    fno_data = page_props.get("fnoData", {})
    opsum = fno_data.get("opsum", {})
    if not opsum:
        return {"records": {"data": []}}
    import time as _time

    expiries = []
    now_ts = int(_time.time())
    for k, v in opsum.items():
        if seg == 1 and v.get("exptype") != "M":
            continue
        try:
            ts = int(k)
        except (ValueError, TypeError):
            continue
        if ts > now_ts:
            expiries.append(ts)
    expiries.sort()
    if not expiries:
        logging.getLogger(__name__).debug(
            "[option_chain public dhan] %s: no future expiries found in opsum; returning empty",
            symbol,
        )
        return {"records": {"data": []}}
    target_exp = expiries[0]
    api_url = "https://open-web-scanx.dhan.co/scanx/optchainactive"
    api_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Content-Type": "application/json; charset=UTF-8",
        "Referer": "https://dhan.co/",
        "Origin": "https://dhan.co",
    }
    payload = {"Data": {"Seg": int(seg), "Sid": int(sid), "Exp": int(target_exp)}}
    api_resp = session.post(api_url, headers=api_headers, json=payload, timeout=15)
    api_resp.raise_for_status()
    raw_response = api_resp.json()
    expiry_date_str = dt.datetime.fromtimestamp(target_exp).strftime("%d-%b-%Y")
    return _public_dhan_to_nse_chain(symbol, raw_response, expiry_date_str)


def option_chain_source(symbol: str = "NIFTY") -> str | None:
    return _OPTION_CHAIN_SOURCE.get(symbol.upper())


def _option_chain_from_csv_text(text: str, source: str) -> dict:
    df = pd.read_csv(io.StringIO(text))
    if df.empty:
        return {"records": {"data": []}}

    def _to_float(value, default=0.0) -> float:
        if value is None or pd.isna(value):
            return default
        if isinstance(value, str):
            value = value.replace(",", "").strip()
            if not value or value == "-":
                return default
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _first(row, *names, default=None):
        cols = {str(c).lower().replace(" ", "").replace("_", ""): c for c in row.index}
        for name in names:
            key = name.lower().replace(" ", "").replace("_", "")
            if key in cols:
                val = row[cols[key]]
                if pd.notna(val):
                    return val
        return default

    rows = []
    underlying = None
    expiry_dates = set()
    for _, row in df.iterrows():
        strike = _first(row, "strikePrice", "strike")
        if pd.isna(strike):
            continue
        expiry = _first(row, "expiryDate", "expiry", default="")
        if expiry:
            expiry_dates.add(str(expiry))
        underlying = underlying or _first(
            row, "underlyingValue", "spot", "nifty", default=None
        )
        ce_oi = _first(row, "CE_openInterest", "call_oi", "ceoi", default=0) or 0
        pe_oi = _first(row, "PE_openInterest", "put_oi", "peoi", default=0) or 0
        ce_vol = (
            _first(row, "CE_totalTradedVolume", "call_volume", "cevolume", default=0)
            or 0
        )
        pe_vol = (
            _first(row, "PE_totalTradedVolume", "put_volume", "pevolume", default=0)
            or 0
        )
        ce_ltp = _first(row, "CE_lastPrice", "call_ltp", "celtp", default=0) or 0
        pe_ltp = _first(row, "PE_lastPrice", "put_ltp", "peltp", default=0) or 0
        ce_iv = _first(row, "CE_impliedVolatility", "call_iv", "ceiv", default=0) or 0
        pe_iv = _first(row, "PE_impliedVolatility", "put_iv", "peiv", default=0) or 0
        rows.append(
            {
                "strikePrice": _to_float(strike),
                "expiryDate": str(expiry),
                "CE": {
                    "openInterest": _to_float(ce_oi),
                    "totalTradedVolume": _to_float(ce_vol),
                    "lastPrice": _to_float(ce_ltp),
                    "impliedVolatility": _to_float(ce_iv),
                },
                "PE": {
                    "openInterest": _to_float(pe_oi),
                    "totalTradedVolume": _to_float(pe_vol),
                    "lastPrice": _to_float(pe_ltp),
                    "impliedVolatility": _to_float(pe_iv),
                },
            }
        )
    return {
        "records": {
            "data": rows,
            "expiryDates": sorted(expiry_dates),
            "underlyingValue": _to_float(underlying, None),
        },
        "_source": source,
    }


# ---------------------------------------------------------------------------
# Research360 (research360.in) option chain — no-auth PHP AJAX scrape
# ---------------------------------------------------------------------------
_R360_SESSION: requests.Session | None = None
_R360_SESSION_TS: float = 0
_R360_SESSION_LOCK = threading.Lock()

_R360_BASE_URLS = (
    "https://www.research360.in",
    "https://beta.research360.in",
)
_R360_BASE_URL: str | None = None
_R360_EXPIRY_PATHS = (
    "/fno/option/ajax/optionChainExp.php",
    "/ajax/optionChainExp.php",
)
_R360_CHAIN_PATHS = (
    "/fno/option/ajax/optionChainApi.php",
    "/ajax/optionChainApi.php",
)


def _r360_headers(base_url: str) -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"{base_url}/future-and-options/option-chain",
        "Origin": base_url,
        "X-Requested-With": "XMLHttpRequest",
    }


# Research360 uses specific internal names for indices
_R360_SYMBOL_MAP = {
    "NIFTY": "NIFTY50",
    "BANKNIFTY": "NIFTYBANK",
    "FINNIFTY": "NIFTYFINSERVICE",
    "MIDCPNIFTY": "NFTMIDSELE",
}


def _create_r360_session() -> requests.Session:
    try:
        from curl_cffi import requests as curl_requests

        return curl_requests.Session(impersonate="chrome120")
    except ImportError:
        return _create_retry_session(retries=5, backoff_factor=0.5)


def _get_r360_session() -> tuple[requests.Session | None, str | None]:
    global _R360_SESSION, _R360_SESSION_TS, _R360_BASE_URL
    with _R360_SESSION_LOCK:
        now = time.time()
        if _R360_SESSION is None or (now - _R360_SESSION_TS) > 1800:
            success = False
            for attempt in range(3):
                for base_url in _R360_BASE_URLS:
                    try:
                        session = _create_r360_session()
                        session.headers.update(_r360_headers(base_url))
                        session.get(
                            f"{base_url}/future-and-options/option-chain",
                            timeout=15,
                        )
                        _R360_SESSION = session
                        _R360_BASE_URL = base_url
                        _R360_SESSION_TS = now
                        success = True
                        break
                    except Exception as e:
                        print(
                            f"[r360 session] attempt {attempt + 1} {base_url} failed: "
                            f"{type(e).__name__}: {e}"
                        )
                        time.sleep(2 * (attempt + 1))
                if success:
                    break
            if not success:
                _R360_SESSION = None
                _R360_BASE_URL = None
    return _R360_SESSION, _R360_BASE_URL


def _r360_expiries(session: requests.Session, base_url: str, symbol: str) -> list[str]:
    import re as _re

    # Research360 expiries endpoint uses 'symbol' parameter for indices
    last_error: Exception | None = None
    for path in _R360_EXPIRY_PATHS:
        try:
            r = session.get(
                f"{base_url}{path}",
                headers=_r360_headers(base_url),
                params={"table_flag": "optionChain", "symbol": symbol},
                timeout=15,
            )
            if r.status_code == 404:
                continue
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data", "")
            expiries = _re.findall(r'value="(\d{4}-\d{2}-\d{2})"', data)
            if expiries:
                return expiries
            return []
        except Exception as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    return []


def _r360_to_nse_chain(symbol: str, raw: dict, expiry: str) -> dict:
    """
    Map Research360 datahc array to NSE-format option chain.

    Confirmed column layout (verified against live ATM data):
        col0 = strikePrice
        col1 = CE openInterest
        col2 = PE openInterest
        col3 = PE_OI / CE_OI per strike (PCR ratio)
        col4 = CE OI change (vs previous day)
        col5 = PE OI change (vs previous day)

    LTP for ~10 near-ATM strikes is available via graphprice/graphc/graphp
    arrays returned in the same response. We extract these and patch the
    matching records so that strategy resolution gets real premiums.
    """
    datahc = raw.get("datahc") or []

    # Build LTP lookup: strike -> CE/PE last price
    # graphprice: list of ~10 near-ATM strikes
    # graphc:     CE LTP for each strike in graphprice (same index)
    # graphp:     PE LTP for each strike in graphprice (same index)
    graphprice = raw.get("graphprice") or []
    graphc_arr = raw.get("graphc") or []
    graphp_arr = raw.get("graphp") or []
    ltp_ce: dict[float, float] = {}
    ltp_pe: dict[float, float] = {}
    for i, s in enumerate(graphprice):
        try:
            key = float(s)
        except (TypeError, ValueError):
            continue

        if i < len(graphc_arr) and graphc_arr[i] is not None:
            try:
                ltp_ce[key] = float(graphc_arr[i])
            except (TypeError, ValueError):
                pass
        if i < len(graphp_arr) and graphp_arr[i] is not None:
            try:
                ltp_pe[key] = float(graphp_arr[i])
            except (TypeError, ValueError):
                pass

    records = []
    for row in datahc:
        if len(row) < 3:
            continue
        strike = float(row[0])
        ce_oi = int(row[1]) if row[1] else 0
        pe_oi = int(row[2]) if row[2] else 0
        ce_oi_chg = int(row[4]) if len(row) > 4 and row[4] else 0
        pe_oi_chg = int(row[5]) if len(row) > 5 and row[5] else 0
        # Patch LTP from graphprice/graphc/graphp arrays (~10 near-ATM strikes)
        ce_ltp = ltp_ce.get(strike, 0.0)
        pe_ltp = ltp_pe.get(strike, 0.0)
        records.append(
            {
                "strikePrice": strike,
                "expiryDate": expiry,
                "CE": {
                    "openInterest": ce_oi,
                    "changeinOpenInterest": ce_oi_chg,
                    "totalTradedVolume": 0,
                    "lastPrice": ce_ltp,
                    "impliedVolatility": 0.0,
                },
                "PE": {
                    "openInterest": pe_oi,
                    "changeinOpenInterest": pe_oi_chg,
                    "totalTradedVolume": 0,
                    "lastPrice": pe_ltp,
                    "impliedVolatility": 0.0,
                },
            }
        )

    underlying = raw.get("spot_price")

    # PCR comes pre-computed; may be '-' if market is closed
    pcr_raw = raw.get("pcr")
    try:
        pcr_value = float(pcr_raw) if pcr_raw and pcr_raw != "-" else None
    except (TypeError, ValueError):
        pcr_value = None

    max_pain = raw.get("max_pain")
    lot_size = raw.get("lot_size")  # R360 returns the actual exchange lot size

    return {
        "records": {
            "data": records,
            "expiryDates": [expiry],
            "underlyingValue": underlying,
        },
        "filtered": {"data": records},
        "_source": "research360",
        "_r360_pcr": pcr_value,
        "_r360_max_pain": max_pain,
        "_r360_spot": underlying,
        "_r360_lot_size": lot_size,
    }


def _option_chain_from_research360(symbol: str) -> dict:
    """Scrape option chain from Research360 PHP AJAX endpoint."""
    global _R360_SESSION, _R360_BASE_URL
    sym = symbol.upper()
    r360_sym = _R360_SYMBOL_MAP.get(sym, sym)

    for attempt in range(3):
        session, base_url = _get_r360_session()
        if session is None or base_url is None:
            return {"records": {"data": []}}

        try:
            last_error: Exception | None = None
            base_candidates = [base_url] + [
                url for url in _R360_BASE_URLS if url != base_url
            ]
            for candidate_base in base_candidates:
                expiries = _r360_expiries(session, candidate_base, r360_sym)
                if not expiries:
                    continue

                expiry = expiries[0]
                for path in _R360_CHAIN_PATHS:
                    try:
                        r = session.post(
                            f"{candidate_base}{path}",
                            headers=_r360_headers(candidate_base),
                            data={
                                "stock": r360_sym,
                                "expiry": expiry,
                                "showall": "on",
                                "showallnew": "on",
                            },
                            timeout=30,
                        )
                        if r.status_code == 404:
                            continue
                        r.raise_for_status()
                        raw = r.json()
                        data = _r360_to_nse_chain(sym, raw, expiry)
                        if data.get("records", {}).get("data"):
                            with _R360_SESSION_LOCK:
                                _R360_BASE_URL = candidate_base
                            return data
                    except Exception as exc:
                        last_error = exc
                        continue
            if last_error is not None:
                raise last_error
        except Exception as e:
            print(
                f"[option_chain research360] attempt {attempt + 1} failed: {type(e).__name__}: {e}"
            )
            with _R360_SESSION_LOCK:
                _R360_SESSION = None  # Force new session on next attempt
                _R360_BASE_URL = None
            time.sleep(1 * (attempt + 1))

    return {"records": {"data": []}}


def _option_chain_from_local_file(symbol: str) -> dict:
    cfg = _data_sources_cfg().get("local_files", {})
    specific_key = f"{symbol.lower()}_option_chain"
    raw_path = _env_or_value(cfg.get(specific_key))
    if not raw_path and symbol.upper() == "NIFTY":
        raw_path = _env_or_value(cfg.get("option_chain"))
    if not raw_path:
        return {"records": {"data": []}}
    path = Path(raw_path)
    if not path.exists():
        return {"records": {"data": []}}
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_source"] = "local_json"
        return data
    data = _option_chain_from_csv_text(path.read_text(encoding="utf-8"), "local_csv")
    return data if data.get("records", {}).get("data") else {"records": {"data": []}}


def _yf():
    import yfinance as yf

    return yf


def _flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Flatten MultiIndex columns from newer yfinance (e.g. ('Close', '^NSEI') -> 'close')."""
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [
            c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns
        ]
    else:
        df.columns = [str(c).lower() for c in df.columns]
    return df


_OHLC_CACHE = {}
_OHLC_CACHE_BUCKET = 0


def ohlc(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily/intraday OHLC. yfinance primary."""
    import contextlib
    import io

    yf = _yf()
    tkr = YF_SYMBOL.get(symbol) or (
        symbol if "." in symbol or "=" in symbol or "^" in symbol else f"{symbol}.NS"
    )
    # Suppress yfinance's noisy "possibly delisted" stdout messages
    with contextlib.redirect_stdout(io.StringIO()):
        df = yf.download(
            tkr, period=period, interval=interval, progress=False, auto_adjust=False
        )
    df = _flatten_columns(df)
    if df.empty:
        return df
    df.index.name = "date"
    df.attrs["source"] = "yfinance"
    return df


def india_vix(period: str = "3mo") -> pd.DataFrame:
    df = ohlc("INDIAVIX", period=period)
    try:
        if df is None or df.empty:
            return df
        # Normalize column name
        col = "close" if "close" in df.columns else None
        if col is None:
            # Attempt to coerce last column as close
            if len(df.columns) > 0:
                col = df.columns[-1]
            else:
                return df
        # Coerce to numeric and clamp to sane range
        df[col] = pd.to_numeric(df[col], errors="coerce")
        # Replace outliers or negative with NaN, then forward-fill a conservative value
        df[col] = df[col].apply(
            lambda x: float(x) if pd.notna(x) and x > 0 and x < 1000 else float("nan")
        )
        if df[col].isna().all():
            logging.getLogger(__name__).warning(
                "India VIX feed returned no valid 'close' values"
            )
        return df
    except Exception as e:
        logging.getLogger(__name__).exception(f"Error sanitizing India VIX data: {e}")
        return df


def _filter_atm_strikes(data: dict) -> dict:
    """Filter records and filtered data to ATM +/- 15 strikes."""
    if not data or not isinstance(data, dict):
        return data

    records = data.get("records")
    if not records or not isinstance(records, dict):
        return data

    underlying_value = records.get("underlyingValue")
    if not underlying_value:
        underlying_value = data.get("filtered", {}).get("underlyingValue")

    if not underlying_value:
        rows = records.get("data") or []
        for r in rows:
            if r.get("CE", {}).get("underlyingValue"):
                underlying_value = r["CE"]["underlyingValue"]
                break
            if r.get("PE", {}).get("underlyingValue"):
                underlying_value = r["PE"]["underlyingValue"]
                break

    try:
        underlying_value = float(underlying_value)
    except (TypeError, ValueError):
        underlying_value = 0.0

    if underlying_value <= 0:
        return data

    rows = records.get("data") or []
    if not rows:
        return data

    # Extract unique strikes
    strikes = sorted(
        list({r["strikePrice"] for r in rows if r.get("strikePrice") is not None})
    )
    if not strikes:
        return data

    # Find the ATM strike (closest to underlying_value)
    atm_strike = min(strikes, key=lambda x: abs(x - underlying_value))
    atm_idx = strikes.index(atm_strike)

    # Slice the strikes to ATM +/- 15 strikes
    start_idx = max(0, atm_idx - 15)
    end_idx = min(len(strikes) - 1, atm_idx + 15)
    allowed_strikes = set(strikes[start_idx : end_idx + 1])

    # Filter data rows
    filtered_rows = [r for r in rows if r.get("strikePrice") in allowed_strikes]
    records["data"] = filtered_rows
    if "strikePrices" in records:
        records["strikePrices"] = sorted(list(allowed_strikes))

    # Filter filtered['data']
    if "filtered" in data and isinstance(data["filtered"], dict):
        filt_rows = data["filtered"].get("data") or []
        data["filtered"]["data"] = [
            r for r in filt_rows if r.get("strikePrice") in allowed_strikes
        ]
        if "strikePrices" in data["filtered"]:
            data["filtered"]["strikePrices"] = sorted(list(allowed_strikes))

    return data


def option_chain(symbol: str = "NIFTY", _skip_atm_filter: bool = False) -> dict:
    """Live option chain via nsepython or direct robust fetch. Returns {'records': ..., 'filtered': ...}.

    Args:
        symbol: Underlying symbol (e.g., NIFTY, BANKNIFTY).
        _skip_atm_filter: If True, skip ATM +/-15 strike filtering.
                          Used internally by chain_snapshot when specific
                          target strikes are needed for exit checks.
    """
    global _NSE_SESSION, _NSE_SESSION_TS
    symbol = symbol.upper()
    cache_file = CACHE_DIR / f"option_chain_{symbol}.json"

    def _save_chain(data: dict) -> dict:
        if not _skip_atm_filter:
            data = _filter_atm_strikes(data)
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps({"ts": time.time(), "data": data}, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Failed to save option chain cache for %s: %s", symbol, e
            )
        _OPTION_CHAIN_SOURCE[symbol] = data.get("_source") or "unknown"
        return data

    def _load_cached_chain() -> dict:
        try:
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                data = cached.get("data") or {}
                if data.get("records", {}).get("data"):
                    if not _skip_atm_filter:
                        data = _filter_atm_strikes(data)
                    data.setdefault("_cache", {})
                    data["_cache"].update({"stale": True, "ts": cached.get("ts")})
                    _OPTION_CHAIN_SOURCE[symbol] = (
                        f"cache:{data.get('_source') or 'unknown'}"
                    )
                    return data
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Failed to load option chain cache for %s: %s", symbol, e
            )
        return {"records": {"data": []}}

    def _try_shoonya() -> dict:
        """Fetch option chain via Shoonya API (primary source)."""
        try:
            from data.shoonya_fetcher import get_shoonya

            shoonya = get_shoonya()
            if shoonya:
                result = shoonya.fetch_option_chain(symbol)
                if result and result.get("strikes"):
                    # Convert Shoonya format to feed.py format (records.data)
                    records = []
                    for s in result["strikes"]:
                        records.append(
                            {
                                "strikePrice": s["strike"],
                                "expiryDate": s.get("expiry", result["expiry"]),
                                "CE": {
                                    "strikePrice": s["strike"],
                                    "expiryDate": s.get("expiry", result["expiry"]),
                                    "underlying": result["symbol"],
                                    "identifier": f"{result['symbol']}{s['expiry']}{s['strike']}CE",
                                    "openInterest": s.get("oi", 0),
                                    "changeinOpenInterest": 0,
                                    "totalTradedVolume": s.get("volume", 0),
                                    "lastPrice": s.get("ltp", 0),
                                    "bidPrice": s.get("bid", 0),
                                    "askPrice": s.get("ask", 0),
                                    "impliedVolatility": s.get("iv", 0),
                                }
                                if s["option_type"] == "CE"
                                else None,
                                "PE": {
                                    "strikePrice": s["strike"],
                                    "expiryDate": s.get("expiry", result["expiry"]),
                                    "underlying": result["symbol"],
                                    "identifier": f"{result['symbol']}{s['expiry']}{s['strike']}PE",
                                    "openInterest": s.get("oi", 0),
                                    "changeinOpenInterest": 0,
                                    "totalTradedVolume": s.get("volume", 0),
                                    "lastPrice": s.get("ltp", 0),
                                    "bidPrice": s.get("bid", 0),
                                    "askPrice": s.get("ask", 0),
                                    "impliedVolatility": s.get("iv", 0),
                                }
                                if s["option_type"] == "PE"
                                else None,
                            }
                        )
                    # Remove None entries (where CE/PE wasn't the matching type)
                    # Actually we need to merge CE/PE at each strike
                    merged = {}
                    for r in records:
                        sk = r["strikePrice"]
                        if sk not in merged:
                            merged[sk] = {
                                "strikePrice": sk,
                                "expiryDate": r["expiryDate"],
                            }
                        if r["CE"]:
                            merged[sk]["CE"] = r["CE"]
                        if r["PE"]:
                            merged[sk]["PE"] = r["PE"]
                    data_out = {
                        "records": {"data": list(merged.values())},
                        "underlying_price": result["underlying_price"],
                        "_source": "shoonya",
                        "filtered": {
                            "underlying_price": result["underlying_price"],
                            "atm_strike": round(result["underlying_price"] / 50) * 50,
                            "strikes": [s["strike"] for s in result["strikes"]],
                        },
                    }
                    return data_out
        except Exception as e:
            logging.getLogger(__name__).warning(
                "[option_chain shoonya] failed for %s: %s", symbol, e
            )
        return {"records": {"data": []}}

    def _try_external_fallbacks() -> dict:
        for fn in (_option_chain_from_local_file,):
            try:
                data = fn(symbol)
                if data and data.get("records", {}).get("data"):
                    return _save_chain(data)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "[option_chain %s] failed for %s: %s",
                    fn.__name__,
                    symbol,
                    e,
                )
        return {"records": {"data": []}}

    # 0. Shoonya (PRIMARY: OAuth authenticated, has full data including LTPs).
    try:
        data = _try_shoonya()
        if data and data.get("records", {}).get("data"):
            return _save_chain(data)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[option_chain shoonya] failed for %s: %s", symbol, e
        )

    # 1. Public Dhan Scraper (fallback: bypass-safe, unauthenticated, full data).
    try:
        data = _option_chain_from_public_dhan(symbol)
        if data and data.get("records", {}).get("data"):
            return _save_chain(data)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[option_chain public dhan] failed for %s: %s", symbol, e
        )

    # 2. Research360 — no auth required, provides OI for all strikes + LTP
    #    for ~10 near-ATM strikes via graphprice/graphc/graphp arrays.
    try:
        data = _option_chain_from_research360(symbol)
        if data and data.get("records", {}).get("data"):
            return _save_chain(data)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[option_chain research360] failed for %s: %s", symbol, e
        )

    # 3. Try robust direct fetch (NSE) as third option
    session = _get_nse_session()
    if session:
        for attempt in range(2):
            try:
                indices = ["NIFTY", "FINNIFTY", "BANKNIFTY", "MIDCPNIFTY"]
                api_type = "indices" if symbol in indices else "equities"
                url = f"https://www.nseindia.com/api/option-chain-{api_type}?symbol={symbol}"

                call_headers = {
                    "Referer": f"https://www.nseindia.com/get-quotes/option-chain?symbol={symbol}",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }

                response = session.get(url, headers=call_headers, timeout=10)
                if response.status_code in (401, 403) and attempt == 0:
                    _NSE_SESSION = None
                    _NSE_SESSION_TS = 0
                    session = _get_nse_session()
                    if session is None:
                        break
                    continue
                if response.status_code == 200:
                    data = response.json()
                    if data and data.get("records", {}).get("data"):
                        return _save_chain(data)
            except Exception as e:
                logging.getLogger(__name__).warning(
                    "[option_chain robust fetch] failed for %s: %s", symbol, e
                )
                break
        try:
            indices = ["NIFTY", "FINNIFTY", "BANKNIFTY", "MIDCPNIFTY"]
            api_type = "indices" if symbol in indices else "equities"
            url = (
                f"https://www.nseindia.com/api/option-chain-{api_type}?symbol={symbol}"
            )

            # Temporary headers for this specific call
            call_headers = {
                "Referer": f"https://www.nseindia.com/get-quotes/option-chain?symbol={symbol}",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            }

            response = session.get(url, headers=call_headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data and data.get("records", {}).get("data"):
                    return _save_chain(data)
        except Exception as e:
            logging.getLogger(__name__).warning(
                "[option_chain robust fetch] failed for %s: %s", symbol, e
            )

    # 3. Fallback to nsepython (might work if our session logic failed but theirs somehow succeeds)
    try:
        from nsepython import nse_optionchain_scrapper

        result = nse_optionchain_scrapper(symbol)
        if result and result.get("records", {}).get("data"):
            return _save_chain(result)
    except Exception as e:
        logging.getLogger(__name__).exception(
            "nse_optionchain_scrapper failed for %s: %s", symbol, e
        )

    try:
        from nsepython import option_chain as nse_oc

        result = nse_oc(symbol)
        if result and result.get("records", {}).get("data"):
            return _save_chain(result)
    except Exception as e:
        logging.getLogger(__name__).exception(
            "nsepython.option_chain failed for %s: %s", symbol, e
        )

    fallback = _try_external_fallbacks()
    if fallback.get("records", {}).get("data"):
        return fallback

    # AI Fallback (Resilient but slower)
    try:
        from data import ai_scraper

        ai_data = ai_scraper.get_option_chain_fallback(symbol)
        if ai_data and ai_data.get("records", {}).get("data"):
            ai_data["_source"] = "ai_scraper"
            return _save_chain(ai_data)
    except Exception as e:
        logging.getLogger(__name__).exception(
            "AI option_chain fallback failed for %s: %s", symbol, e
        )

    data = _load_cached_chain()

    # Enrichment step for Research360: If LTP is 0, try to patch with Dhan Public LTPs
    if data.get("_source") == "research360":
        try:
            spot = data.get("records", {}).get("underlyingValue")
            if spot is not None and spot != 0:
                # Use Dhan Public scraper for LTPs (free, no auth needed)
                public_data = _option_chain_from_public_dhan(symbol)
                if public_data.get("records", {}).get("data"):
                    ltp_map = {}
                    for row in public_data["records"]["data"]:
                        s = row["strikePrice"]
                        ltp_map[f"{s}_CE"] = row["CE"].get("lastPrice", 0)
                        ltp_map[f"{s}_PE"] = row["PE"].get("lastPrice", 0)

                    for row in data["records"]["data"]:
                        s = row["strikePrice"]
                        row["CE"]["lastPrice"] = ltp_map.get(f"{s}_CE", 0)
                        row["PE"]["lastPrice"] = ltp_map.get(f"{s}_PE", 0)
                    data["_source"] = "research360+public_dhan_ltp"
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Option chain enrichment failed for %s: %s", symbol, e
            )

    return data


def get_pcr_max_pain_cached(
    symbol: str = "NIFTY",
) -> tuple[
    float | None, float | None, float | None, bool, bool, float | None, float | None
]:
    """Fetch PCR and Max Pain with caching fallback.
    Returns: (pcr_oi, pcr_vol, max_pain, pcr_stale, mp_stale, pcr_updated_at, mp_updated_at)
    """
    cache_dir = CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "pcr_mp_cache.json"

    # Try live fetch
    try:
        raw = option_chain(symbol)
        data = raw.get("records", {}).get("data", [])
        if data:
            # Research360 provides pre-computed PCR and max_pain — use them directly.
            r360_pcr = raw.get("_r360_pcr")
            r360_max_pain = raw.get("_r360_max_pain")

            ce_oi = pe_oi = ce_vol = pe_vol = 0.0
            strike_ce_oi: dict[float, float] = {}
            strike_pe_oi: dict[float, float] = {}
            for row in data:
                strike = row.get("strikePrice")
                ce = row.get("CE") or {}
                pe = row.get("PE") or {}
                ce_oi += ce.get("openInterest", 0) or 0
                pe_oi += pe.get("openInterest", 0) or 0
                ce_vol += ce.get("totalTradedVolume", 0) or 0
                pe_vol += pe.get("totalTradedVolume", 0) or 0
                if strike is not None:
                    strike_ce_oi[strike] = ce.get("openInterest", 0) or 0
                    strike_pe_oi[strike] = pe.get("openInterest", 0) or 0

            # Prefer server-side PCR from Research360; fall back to computed.
            if r360_pcr is not None:
                pcr_oi = round(float(r360_pcr), 2)
            else:
                pcr_oi = round(pe_oi / ce_oi, 2) if ce_oi else None
            pcr_vol = round(pe_vol / ce_vol, 2) if ce_vol else None

            # Prefer server-side max_pain from Research360; fall back to computed.
            if r360_max_pain is not None:
                max_pain = float(r360_max_pain)
            elif strike_ce_oi and strike_pe_oi:
                strikes = sorted(set(strike_ce_oi) | set(strike_pe_oi))
                pain = {}
                for k in strikes:
                    p = 0.0
                    for s in strikes:
                        if s < k:
                            p += strike_pe_oi.get(s, 0) * (k - s)
                        elif s > k:
                            p += strike_ce_oi.get(s, 0) * (s - k)
                    pain[k] = p
                max_pain = float(min(pain, key=pain.get))
            else:
                max_pain = None

            # Save to cache
            cache_data = {
                "pcr_oi": pcr_oi,
                "pcr_vol": pcr_vol,
                "max_pain": max_pain,
                "ts": time.time(),
                "source": raw.get("_source")
                or option_chain_source(symbol)
                or "unknown",
            }
            with open(cache_file, "w", encoding="utf-8") as f:
                json.dump(cache_data, f)

            return (
                pcr_oi,
                pcr_vol,
                max_pain,
                False,
                False,
                cache_data["ts"],
                cache_data["ts"],
            )
    except Exception as e:
        print(f"[get_pcr_max_pain_cached] live fetch failed: {e}")

    # Fallback to cache
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                cache_data = json.load(f)
            if cache_data.get("source"):
                _OPTION_CHAIN_SOURCE[symbol.upper()] = (
                    f"cache:{cache_data.get('source')}"
                )
            age = time.time() - cache_data.get("ts", 0)
            is_stale = age > 900  # 15 mins
            return (
                cache_data.get("pcr_oi"),
                cache_data.get("pcr_vol"),
                cache_data.get("max_pain"),
                is_stale,
                is_stale,
                cache_data.get("ts"),
                cache_data.get("ts"),
            )
        except Exception as e:
            logging.getLogger(__name__).exception(
                "Failed to read PCR/MaxPain cache: %s", e
            )

    # If we got here, both live and cache failed. Do not take down dashboard flows.
    return None, None, None, True, True, None, None


def _get_persistent_fii_dii_cache() -> tuple[
    Optional[list[dict]], Optional[list[dict]], float
]:
    """Retrieve cached FII/DII data, stockedge data, and its timestamp from a persistent local file."""
    import json

    cache_file = CACHE_DIR / "fii_dii_cache.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r") as f:
                data = json.load(f)
                return (
                    data.get("data"),
                    data.get("stockedge_data"),
                    data.get("timestamp", 0.0),
                )
        except Exception as e:
            logging.getLogger(__name__).exception("Failed to read FII/DII cache: %s", e)
    return None, None, 0.0


def _set_persistent_fii_dii_cache(
    data: list[dict], stockedge_data: list[dict] | None, timestamp: float
) -> None:
    """Save FII/DII data, stockedge data, and its timestamp to a persistent local file."""
    import json

    cache_file = CACHE_DIR / "fii_dii_cache.json"
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump(
                {
                    "data": data,
                    "stockedge_data": stockedge_data,
                    "timestamp": timestamp,
                },
                f,
            )
    except Exception as e:
        logging.getLogger(__name__).exception("Failed to write FII/DII cache: %s", e)


def fii_dii_cash(days: int = 10) -> pd.DataFrame:
    """FII/DII cash market net buy/sell, last N sessions."""
    import time

    now = time.time()

    # Try to load existing cached data
    cached_data, cached_stockedge, cached_ts = _get_persistent_fii_dii_cache()
    if cached_data is None:
        cached_data = []
    if cached_stockedge is None:
        cached_stockedge = []

    # Check if we need to fetch live (only if cache is stale > 1 hour)
    need_fetch = (now - cached_ts) >= 3600 or not cached_stockedge

    raw_stockedge = None
    if need_fetch:
        print("[feed.fii_dii_cash] Fetching FII/DII activities from StockEdge API...")
        for attempt in range(3):
            try:
                url = "https://api.stockedge.com/Api/FIIDashboardApi/GetLatestFIIActivities?lang=en"
                try:
                    from curl_cffi import requests as curl_requests

                    session = curl_requests.Session(impersonate="chrome120")
                    response = session.get(url, timeout=15)
                except ImportError:
                    headers = {
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                        "Accept": "application/json, text/plain, */*",
                        "Origin": "https://web.stockedge.com",
                        "Referer": "https://web.stockedge.com/",
                    }
                    response = requests.get(url, headers=headers, timeout=15)

                if response.status_code == 200:
                    raw_stockedge = response.json()
                    if raw_stockedge:
                        break
            except Exception as e:
                print(
                    f"[feed.fii_dii_cash] StockEdge API attempt {attempt + 1} failed: {e}"
                )
                time.sleep(1)

        if raw_stockedge:
            cached_stockedge = raw_stockedge
            new_cash_records = []
            for day in raw_stockedge:
                date_str = day.get("Date", "").split("T")[0]
                try:
                    dt_obj = dt.datetime.strptime(date_str, "%Y-%m-%d")
                    formatted_date = dt_obj.strftime("%d-%b-%Y")
                except Exception:
                    formatted_date = date_str

                for item in day.get("FIIDIIData", []):
                    short_name = item.get("ShortName")
                    val = item.get("Value")
                    if short_name == "FII CM*":
                        new_cash_records.append(
                            {
                                "category": "FII/FPI",
                                "date": formatted_date,
                                "netValue": val,
                                "buyValue": 0.0,
                                "sellValue": 0.0,
                            }
                        )
                    elif short_name == "DII CM*":
                        new_cash_records.append(
                            {
                                "category": "DII",
                                "date": formatted_date,
                                "netValue": val,
                                "buyValue": 0.0,
                                "sellValue": 0.0,
                            }
                        )
            cached_data = new_cash_records
            try:
                _set_persistent_fii_dii_cache(cached_data, cached_stockedge, now)
            except Exception as e:
                print(f"[feed.fii_dii_cash] Failed to cache StockEdge data: {e}")
        else:
            print(
                "[feed.fii_dii_cash] StockEdge API failed. Falling back to legacy/AI..."
            )
            # Fallback to legacy
            raw = None
            nse_fiidii_fn = None
            try:
                import nsepython.rahu as nse_rahu
                from nsepython import nse_fiidii as nse_fiidii_fn

                if not hasattr(nse_rahu, "logger"):
                    nse_rahu.logger = logging.getLogger("nsepython")
            except ImportError as e:
                pass

            if nse_fiidii_fn is not None:
                for attempt in range(5):
                    try:
                        raw = nse_fiidii_fn()
                        if raw is not None and not (
                            isinstance(raw, pd.DataFrame) and raw.empty
                        ):
                            break
                    except Exception as e:
                        print(
                            f"[feed.fii_dii_cash] legacy attempt {attempt + 1} failed: {e}"
                        )
                        time.sleep(2)

            # AI Fallback
            if raw is None or (isinstance(raw, pd.DataFrame) and raw.empty):
                try:
                    from data import ai_scraper

                    ai_raw = ai_scraper.get_fii_dii_fallback()
                    if ai_raw:
                        raw = pd.DataFrame(ai_raw)
                except Exception as e:
                    print(f"[feed.fii_dii_cash] AI fallback failed: {e}")

            if raw is not None:
                if isinstance(raw, pd.DataFrame):
                    new_df = raw.copy()
                elif isinstance(raw, list):
                    new_df = pd.DataFrame(raw)
                else:
                    new_df = pd.DataFrame()

                if not new_df.empty:
                    new_records = new_df.to_dict(orient="records")
                    merged_map = {}
                    for r in cached_data:
                        d_str = str(r.get("date", ""))
                        cat = str(r.get("category", "")).strip()
                        merged_map[(d_str, cat)] = r

                    for r in new_records:
                        d_val = r.get("date")
                        if hasattr(d_val, "strftime"):
                            d_str = d_val.strftime("%d-%b-%Y")
                        else:
                            d_str = str(d_val)
                        r["date"] = d_str
                        cat = str(r.get("category", "")).strip()
                        merged_map[(d_str, cat)] = r

                    cached_data = list(merged_map.values())
                    try:
                        _set_persistent_fii_dii_cache(
                            cached_data, cached_stockedge, now
                        )
                    except Exception as e:
                        print(f"[feed.fii_dii_cash] Failed to cache FII/DII data: {e}")

    if not cached_data:
        return pd.DataFrame()

    df = pd.DataFrame(cached_data)

    if "date" in df.columns:
        parsed_dates = pd.to_datetime(df["date"], format="%d-%b-%Y", errors="coerce")
        mask_failed = parsed_dates.isna() & df["date"].notna()
        if mask_failed.any():
            iso_dates = pd.to_datetime(df.loc[mask_failed, "date"], errors="coerce")
            parsed_dates[mask_failed] = iso_dates
        df["date"] = parsed_dates
        # Fix #11: Filter by Segment if available to avoid double counting
        cols = [c.lower() for c in df.columns]
        if "segment" in cols:
            seg_col = df.columns[cols.index("segment")]
            df = df[df[seg_col].astype(str).str.lower().str.contains("cash", na=False)]

        # Sort chronologically
        df = df.sort_values("date").reset_index(drop=True)
    else:
        df = df.reset_index(drop=True)

    return df


def fii_dii_derivatives(days: int = 5) -> tuple[dict[str, float], bool]:
    """Return the cumulative sum of FII derivatives activity over the last N sessions.

    Returns (derivatives_dict, stale).
    """
    _, cached_stockedge, _ = _get_persistent_fii_dii_cache()
    if not cached_stockedge:
        try:
            fii_dii_cash(days=20)
            _, cached_stockedge, _ = _get_persistent_fii_dii_cache()
        except Exception as e:
            print(f"[feed.fii_dii_derivatives] Failed to trigger fetch: {e}")

    if not cached_stockedge:
        return {
            "fii_index_futures_5d": 0.0,
            "fii_index_options_5d": 0.0,
            "fii_stock_futures_5d": 0.0,
            "fii_stock_options_5d": 0.0,
        }, True

    # Sort chronologically by Date
    try:
        sorted_data = sorted(cached_stockedge, key=lambda x: x.get("Date", ""))
    except Exception:
        sorted_data = cached_stockedge

    # Take the last N days
    last_n = sorted_data[-days:] if len(sorted_data) >= days else sorted_data

    out = {
        "fii_index_futures_5d": 0.0,
        "fii_index_options_5d": 0.0,
        "fii_stock_futures_5d": 0.0,
        "fii_stock_options_5d": 0.0,
    }

    for day in last_n:
        for item in day.get("FIIDIIData", []):
            short_name = item.get("ShortName")
            val = item.get("Value", 0.0) or 0.0
            if short_name == "FII Idx Fut":
                out["fii_index_futures_5d"] += val
            elif short_name == "FII Idx Opt":
                out["fii_index_options_5d"] += val
            elif short_name == "FII Stk Fut":
                out["fii_stock_futures_5d"] += val
            elif short_name == "FII Stk Opt":
                out["fii_stock_options_5d"] += val

    return {k: round(v, 2) for k, v in out.items()}, False


def _cached_ohlc(
    symbol: str, period: str, interval: str, cache_key: str
) -> pd.DataFrame:
    key = f"{symbol}_{period}_{interval}_{cache_key}"
    if key in _OHLC_CACHE and not _OHLC_CACHE[key].empty:
        return _OHLC_CACHE[key].copy()

    # Try Shoonya TPSeries for intraday intervals (1m, 5m, 15m, 30m, 60m)
    if interval in ("1m", "5m", "15m", "30m", "60m", "1h", "1H"):
        try:
            from data.shoonya_fetcher import get_shoonya

            shoonya = get_shoonya()
            if shoonya and shoonya.login():
                # Need to resolve symbol to exchange + token
                # For indices, use NSE; for stocks, try NSE spot
                exchange = "NSE"
                token = _resolve_shoonya_token(shoonya, symbol, exchange)
                if token:
                    from datetime import datetime, timedelta

                    end_dt = datetime.now()
                    start_dt = end_dt - timedelta(days=_period_to_days(period))
                    start_str = start_dt.strftime("%d-%m-%Y %H:%M:%S")
                    end_str = end_dt.strftime("%d-%m-%Y %H:%M:%S")
                    interval_min = _interval_to_minutes(interval)
                    if interval_min:
                        resp = shoonya.get_historical_candles(
                            exchange, token, interval_min, start_str, end_str
                        )
                        if resp and resp.get("stat") == "Ok" and resp.get("values"):
                            df = _shoonya_candles_to_df(resp["values"])
                            if not df.empty:
                                _OHLC_CACHE[key] = df.copy()
                                return df
        except Exception as e:
            import logging

            logging.getLogger(__name__).debug(
                "Shoonya TPSeries failed for %s: %s", symbol, e
            )

    df = ohlc(symbol, period=period, interval=interval)
    if not df.empty:
        _OHLC_CACHE[key] = df.copy()
    return df


def _resolve_shoonya_token(shoonya, symbol: str, exchange: str) -> str | None:
    """Resolve a symbol to its Shoonya token."""
    try:
        # Try known index tokens first
        from data.shoonya_fetcher import _INDEX_NSE_TOKENS, _INDEX_SPOT_NAMES

        base = symbol.upper().split()[0]
        if base in _INDEX_NSE_TOKENS:
            return _INDEX_NSE_TOKENS[base]
        # Search via SearchScrip
        res = shoonya._search_scrip(exchange, base)
        if res and res.get("stat") == "Ok":
            values = res.get("values", [])
            if values:
                return values[0].get("token") or values[0].get("tok")
    except Exception as e:
        logging.getLogger(__name__).warning(
            "[_resolve_shoonya_token] failed for %s on %s: %s", symbol, exchange, e
        )
    return None


def _period_to_days(period: str) -> int:
    period = period.lower()
    if period.endswith("d"):
        return int(period[:-1])
    if period.endswith("mo") or period.endswith("m"):
        return int(period[:-2]) * 30
    if period.endswith("y"):
        return int(period[:-1]) * 365
    return 30  # default


def _interval_to_minutes(interval: str) -> int | None:
    interval = interval.lower()
    if interval.endswith("m"):
        return int(interval[:-1])
    if interval.endswith("h"):
        return int(interval[:-1]) * 60
    if interval == "1d" or interval == "1D":
        return 1440
    return None


def _shoonya_candles_to_df(values: list) -> pd.DataFrame:
    """Convert Shoonya TPSeries values to DataFrame.
    Shoonya returns: [timestamp, open, high, low, close, volume, oi]
    """
    if not values:
        return pd.DataFrame()
    rows = []
    for v in values:
        if len(v) >= 6:
            try:
                ts = datetime.strptime(v[0], "%d-%m-%Y %H:%M:%S")
                rows.append(
                    {
                        "timestamp": ts,
                        "open": float(v[1]),
                        "high": float(v[2]),
                        "low": float(v[3]),
                        "close": float(v[4]),
                        "volume": float(v[5]),
                    }
                )
            except Exception:
                continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df.set_index("timestamp", inplace=True)
    return df


from datetime import datetime, timedelta, timezone


def ohlc_cached(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Time-bucketed cache to ensure fresh data every 2 minutes without spamming yfinance."""
    global _OHLC_CACHE, _OHLC_CACHE_BUCKET
    current_bucket = int(time.time() / 120)

    if current_bucket != _OHLC_CACHE_BUCKET:
        _OHLC_CACHE.clear()
        _OHLC_CACHE_BUCKET = current_bucket

    return _cached_ohlc(symbol, period, interval, str(current_bucket))


def sector_ohlc(sectors: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    """Fetch OHLC for sector indices, gracefully handling individual failures."""
    result = {}
    for s in sectors:
        try:
            df = ohlc_cached(s, period=period)
            if df is not None and not df.empty:
                result[s] = df
        except Exception:
            continue
    return result


def universe_ohlc(tickers: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    import contextlib
    import io
    import os
    import time
    from datetime import timedelta, timezone
    from pathlib import Path

    import yfinance as yf

    _quiet = contextlib.redirect_stdout(io.StringIO())

    cache_dir = Path("data/cache/ohlc")
    cache_dir.mkdir(parents=True, exist_ok=True)
    today_str = dt.datetime.now().strftime("%Y-%m-%d")

    results: dict[str, pd.DataFrame] = {}
    missing_tickers: list[str] = []

    # Check market status for cache invalidation
    ist_now = dt.datetime.now(timezone(timedelta(hours=5, minutes=30)))
    is_weekday = ist_now.weekday() < 5
    tt = ist_now.time()
    market_open = (
        is_weekday
        and (tt.hour, tt.minute) >= (9, 15)
        and (tt.hour, tt.minute) <= (15, 30)
    )

    # 1. Load from cache if today's file exists
    for t in tickers:
        cache_file = cache_dir / f"{t}_{today_str}.pkl"
        if cache_file.exists():
            # Invalidate if market is open and file is >15 mins old
            if market_open and (time.time() - cache_file.stat().st_mtime > 900):
                missing_tickers.append(t)
                continue
            try:
                results[t] = pd.read_pickle(cache_file)
                _OHLC_SOURCE[t] = results[t].attrs.get("source", "cache")
            except Exception:
                missing_tickers.append(t)
        else:
            missing_tickers.append(t)

    # Quick return if everything was cached
    if not missing_tickers:
        return results

    if not missing_tickers:
        return results

    # 2. Dhan historical primary for symbols that can be mapped.
    dhan_failed = []
    if _dhan_enabled():
        for t in missing_tickers:
            try:
                sym_df = _dhan_ohlc(t, period=period, interval="1d")
                if not sym_df.empty:
                    _OHLC_SOURCE[t] = "dhan_historical"
                    sym_df.attrs["source"] = "dhan_historical"
                    sym_df.to_pickle(cache_dir / f"{t}_{today_str}.pkl")
                    results[t] = sym_df
                    continue
            except Exception as e:
                print(f"universe_ohlc dhan: {t} err: {type(e).__name__}: {e}")
            dhan_failed.append(t)
            time.sleep(0.22)
        missing_tickers = dhan_failed
        if not missing_tickers:
            return results

    # 3. Batch fetch remaining from yfinance fallback.
    yf_tickers = [f"{s}.NS" if "." not in s else s for s in missing_tickers]
    chunks = [yf_tickers[i : i + 100] for i in range(0, len(yf_tickers), 100)]
    fetched, failed, skipped = 0, 0, 0

    # If yfinance batch returns a structure but we extract zero symbols,
    # re-try per-symbol to avoid poisoning the whole universe run.
    PER_SYMBOL_RETRY_CAP = 60

    def _persist_symbol(sym: str, sym_df: pd.DataFrame) -> bool:
        """Persist only if non-empty and has a close column after flattening."""
        if sym_df is None or sym_df.empty:
            return False
        # Flatten might produce 'close' or 'Close' depending on source; normalize to lowercase.
        sym_df = _flatten_columns(sym_df)
        if sym_df.empty:
            return False
        if "close" not in sym_df.columns:
            return False
        sym_df.index.name = "date"
        _OHLC_SOURCE[sym] = "yfinance"
        sym_df.attrs["source"] = "yfinance"
        sym_df.to_pickle(cache_dir / f"{sym}_{today_str}.pkl")
        results[sym] = sym_df
        return True

    for chunk in chunks:
        try:
            with _quiet:
                df_dict = yf.download(
                    tickers=" ".join(chunk),
                    period=period,
                    group_by="ticker",
                    threads=False,
                    progress=False,
                )
            time.sleep(1)  # rate limit spacing
        except Exception as e:
            print(f"yfinance batch download failed, retrying once: {e}")
            time.sleep(2)
            try:
                with _quiet:
                    df_dict = yf.download(
                        tickers=" ".join(chunk),
                        period=period,
                        group_by="ticker",
                        threads=False,
                        progress=False,
                    )
            except Exception:
                failed += len(chunk)
                continue

        fetched_in_batch = 0

        if len(chunk) == 1:
            sym = chunk[0].replace(".NS", "")
            sym_df = df_dict.copy()
            if _persist_symbol(sym, sym_df):
                fetched_in_batch = 1
                fetched += 1
            else:
                skipped += 1

        elif isinstance(df_dict.columns, pd.MultiIndex):
            # Robust ticker level detection:
            # Identify which MultiIndex level contains the ticker symbols from this chunk.
            levels = list(df_dict.columns.levels)
            chunk_set = set(chunk)

            level_candidates: list[int] = []
            for i in (0, 1):
                try:
                    if any(x in levels[i] for x in chunk_set):
                        level_candidates.append(i)
                except Exception:
                    pass

            if len(level_candidates) == 1:
                ticker_level = level_candidates[0]
            else:
                # If ambiguous, try both levels by picking the one with higher overlap
                overlaps = {}
                for i in (0, 1):
                    try:
                        overlaps[i] = sum(1 for x in chunk_set if x in levels[i])
                    except Exception:
                        overlaps[i] = 0
                ticker_level = max(overlaps, key=lambda k: overlaps.get(k, 0))

            for yf_t in chunk:
                sym = yf_t.replace(".NS", "")
                extracted = False
                for lvl in (ticker_level, 1 - ticker_level):
                    try:
                        if yf_t in df_dict.columns.levels[lvl]:
                            sym_df = df_dict.xs(yf_t, level=lvl, axis=1).copy()
                            sym_df = sym_df.dropna(how="all")
                            if _persist_symbol(sym, sym_df):
                                fetched_in_batch += 1
                                extracted = True
                                break
                    except Exception:
                        continue

                if not extracted:
                    # Last-chance: try xs even if membership checks failed
                    try:
                        sym_df = df_dict.xs(yf_t, level=ticker_level, axis=1).copy()
                        sym_df = sym_df.dropna(how="all")
                        if _persist_symbol(sym, sym_df):
                            fetched_in_batch += 1
                            extracted = True
                    except Exception:
                        pass

                if not extracted:
                    failed += 1

            fetched += fetched_in_batch

        else:
            failed += len(chunk)

        # Per-symbol retry only when we extracted zero tickers from the whole batch.
        if fetched_in_batch == 0 and isinstance(df_dict, (pd.DataFrame, type(None))):
            # Cap to keep dashboard responsive.
            tried = 0
            for yf_t in chunk:
                if tried >= PER_SYMBOL_RETRY_CAP:
                    break
                sym = yf_t.replace(".NS", "")
                try:
                    # Retry individual ticker for parsing sanity.
                    with _quiet:
                        one = yf.download(
                            tickers=yf_t,
                            period=period,
                            group_by="ticker",
                            threads=False,
                            progress=False,
                        )
                    one = _flatten_columns(one)
                    if _persist_symbol(sym, one):
                        fetched += 1
                        fetched_in_batch += 1
                    else:
                        skipped += 1
                except Exception:
                    failed += 1
                tried += 1

    print(f"universe_ohlc: fetched={fetched} failed={failed} skipped={skipped}")
    return results


_trendlyne_cache: dict[str, tuple[float, dict]] = {}


def fetch_trendlyne_options_kpis(symbol: str = "NIFTY") -> dict:
    """
    Fetch high-level KPIs from Trendlyne Smart Options dashboard.
    Uses curl_cffi for Cloudflare bypass and extracts data from the SPA state or regex.
    Caches results to prevent rate limiting and handles HTTP 500 gracefully.
    """
    import time

    t_symbol = symbol.upper()
    now = time.time()
    if t_symbol in _trendlyne_cache:
        cached_ts, cached_data = _trendlyne_cache[t_symbol]
        if now - cached_ts < 900 and cached_data:
            return cached_data

    slug_map = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "FINNIFTY": "FINNIFTY"}
    slug = slug_map.get(t_symbol, t_symbol)
    url = f"https://smartoptions.trendlyne.com/dashboard/options/latest/{slug}/"

    headers = {
        "Referer": "https://smartoptions.trendlyne.com/",
    }

    try:
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate="chrome120")
        resp = session.get(url, headers=headers, timeout=12)
        resp.raise_for_status()

        import json
        import re

        kpis = {}
        match = re.search(
            r"window\.__INITIAL_STATE__\s*=\s*({.*?});", resp.text, re.DOTALL
        )
        if match:
            state = json.loads(match.group(1))
            dashboard = state.get("optionsDashboard", {}).get("latest", {})
            kpis = {
                "fii_index_long_short_ratio": dashboard.get("fiiLongShortRatio"),
                "modified_max_pain": dashboard.get("modifiedMaxPain"),
                "iv_percentile": dashboard.get("ivPercentile"),
                "sentiment": dashboard.get("sentiment"),
                "pcr_oi": dashboard.get("pcr"),
                "source": "trendlyne",
            }
        else:
            # Fallback regex extraction if Trendlyne SPA layout changed
            def _extract_val(pattern: str):
                m = re.search(pattern, resp.text, re.IGNORECASE)
                if m:
                    try:
                        return float(m.group(1))
                    except ValueError:
                        return m.group(1)
                return None

            kpis = {
                "fii_index_long_short_ratio": _extract_val(
                    r'["\']?fiiLongShortRatio["\']?\s*[:=]\s*["\']?([0-9\.\-]+)'
                ),
                "modified_max_pain": _extract_val(
                    r'["\']?modifiedMaxPain["\']?\s*[:=]\s*["\']?([0-9\.\-]+)'
                ),
                "iv_percentile": _extract_val(
                    r'["\']?ivPercentile["\']?\s*[:=]\s*["\']?([0-9\.\-]+)'
                ),
                "sentiment": _extract_val(
                    r'["\']?sentiment["\']?\s*[:=]\s*["\']?([A-Za-z]+)'
                ),
                "pcr_oi": _extract_val(r'["\']?pcr["\']?\s*[:=]\s*["\']?([0-9\.\-]+)'),
                "source": "trendlyne",
            }

        # Filter out None values
        kpis = {k: v for k, v in kpis.items() if v is not None}
        if len(kpis) > 1:
            _trendlyne_cache[t_symbol] = (now, kpis)
            return kpis

    except Exception as e:
        logging.getLogger(__name__).debug(f"Trendlyne fetch failed for {symbol}: {e}")

    # On error or missing data, return stale cache if available
    if t_symbol in _trendlyne_cache:
        return _trendlyne_cache[t_symbol][1]

    return {}


def fetch_shoonya_security_info(exchange: str, token: str) -> dict | None:
    """
    Fetch contract specifications from Shoonya (GetSecurityInfo).
    Returns dict with: ls (lot_size), ti (tick_size), frzqty (freeze_qty),
    lct/uct (circuit limits), exd (expiry).
    """
    try:
        from data.shoonya_fetcher import get_shoonya

        shoonya = get_shoonya()
        if shoonya and shoonya.login():
            return shoonya.get_security_info(exchange, token)
    except Exception as e:
        logging.getLogger(__name__).debug("Shoonya security info fetch failed: %s", e)
    return None


def fetch_shoonya_index_list() -> list[dict]:
    """
    Fetch list of indices from Shoonya (GetIndexList).
    Returns list of {symname, token, exch} dicts.
    """
    try:
        from data.shoonya_fetcher import get_shoonya

        shoonya = get_shoonya()
        if shoonya and shoonya.login():
            resp = shoonya.get_index_list()
            if resp and resp.get("stat") == "Ok":
                return resp.get("values", [])
    except Exception as e:
        logging.getLogger(__name__).debug("Shoonya index list fetch failed: %s", e)
    return []


def fetch_shoonya_historical_candles(
    exchange: str, token: str, interval: int, start_time: str, end_time: str
) -> dict | None:
    """
    Fetch OHLCV candles from Shoonya (TPSeries).
    """
    try:
        from data.shoonya_fetcher import get_shoonya

        shoonya = get_shoonya()
        if shoonya and shoonya.login():
            return shoonya.get_historical_candles(
                exchange, token, interval, start_time, end_time
            )
    except Exception as e:
        logging.getLogger(__name__).debug("Shoonya TPSeries fetch failed: %s", e)
    return None


def fetch_shoonya_market_quote(exchange: str, token: str) -> dict | None:
    """
    Fetch full market quote with depth from Shoonya (GetQuotes).
    Returns dict with lp, o,h,l,c,v, oi, poi, and Best 5 bid/ask arrays.
    """
    try:
        from data.shoonya_fetcher import get_shoonya

        shoonya = get_shoonya()
        if shoonya and shoonya.login():
            return shoonya.get_market_quote(exchange, token)
    except Exception as e:
        logging.getLogger(__name__).debug("Shoonya market quote fetch failed: %s", e)
    return None
