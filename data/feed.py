"""Data feed: OHLC, option chain, FII/DII, VIX. yfinance + nsepython."""
from __future__ import annotations

import datetime as dt
import io
import json
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Optional

import pandas as pd
import requests


CACHE_DIR = Path(__file__).parent.parent / "data/cache"


# Persistent session for NSE to avoid hitting home page every time
_NSE_SESSION = None
_NSE_SESSION_TS = 0
_NSE_SESSION_LOCK = threading.Lock()
_DHAN_OC_CACHE: dict[str, tuple[float, dict]] = {}
_DHAN_MASTER_CACHE: pd.DataFrame | None = None
_OPTION_CHAIN_SOURCE: dict[str, str] = {}

def _get_nse_session():
    global _NSE_SESSION, _NSE_SESSION_TS
    # Use lock to prevent race condition when refreshing session
    with _NSE_SESSION_LOCK:
        now = time.time()
        # Refresh session every 10 minutes or if not exists
        if _NSE_SESSION is None or (now - _NSE_SESSION_TS) > 600:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive"
            }
            _NSE_SESSION = requests.Session()
            _NSE_SESSION.headers.update(headers)
            try:
                # Hit pages that set NSE cookies used by option-chain APIs.
                _NSE_SESSION.get("https://www.nseindia.com", timeout=10)
                _NSE_SESSION.get("https://www.nseindia.com/market-data/live-equity-market", timeout=10)
                _NSE_SESSION.get("https://www.nseindia.com/option-chain", timeout=10)
                _NSE_SESSION_TS = now
            except Exception as e:
                print(f"[_get_nse_session] failed: {e}")
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
    return cfg.get("provider") == "dhan" and ds.get("enabled", True) is not False and _dhan_headers() is not None


def _dhan_underlying(symbol: str) -> tuple[int, str] | None:
    cfg = _data_sources_cfg().get("dhan", {})
    underlyings = cfg.get("underlyings", {}) if isinstance(cfg.get("underlyings"), dict) else {}
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
    url = _data_sources_cfg().get("dhan", {}).get("instrument_master_url", "https://images.dhan.co/api-data/api-scrip-master.csv")
    df = pd.read_csv(url, low_memory=False)
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        df.to_csv(cache_file, index=False)
    except Exception:
        pass
    _DHAN_MASTER_CACHE = df
    return df


def _dhan_find_instrument(symbol: str) -> dict | None:
    symbol = symbol.upper().replace(".NS", "")
    under = _dhan_underlying(symbol)
    if under:
        return {"security_id": str(under[0]), "segment": under[1], "instrument": "INDEX"}
    try:
        df = _dhan_master()
    except Exception:
        return None
    sec_col = _dhan_col(df, "SEM_SMST_SECURITY_ID", "security_id", "SECURITY_ID")
    exch_col = _dhan_col(df, "SEM_EXM_EXCH_ID", "EXCH_ID")
    seg_col = _dhan_col(df, "SEM_SEGMENT", "SEGMENT")
    inst_col = _dhan_col(df, "SEM_INSTRUMENT_NAME", "INSTRUMENT")
    sym_col = _dhan_col(df, "SM_SYMBOL_NAME", "SYMBOL_NAME", "UNDERLYING_SYMBOL")
    disp_col = _dhan_col(df, "SEM_CUSTOM_SYMBOL", "DISPLAY_NAME")
    if not sec_col:
        return None
    work = df
    if exch_col:
        work = work[work[exch_col].astype(str).str.upper().eq("NSE")]
    if seg_col:
        work = work[work[seg_col].astype(str).str.upper().isin(["E", "D", "IDX_I", "NSE_EQ", "NSE_FNO"])]
    candidates = []
    for col in [sym_col, disp_col]:
        if col:
            hit = work[work[col].astype(str).str.upper().eq(symbol)]
            if not hit.empty:
                candidates.append(hit)
    if not candidates:
        return None
    row = candidates[0].iloc[0]
    seg_raw = str(row[seg_col]).upper() if seg_col else "E"
    inst_raw = str(row[inst_col]).upper() if inst_col else "EQUITY"
    segment = "NSE_EQ" if seg_raw in ("E", "NSE_EQ") else ("NSE_FNO" if seg_raw in ("D", "NSE_FNO") else seg_raw)
    instrument = "EQUITY" if "EQUITY" in inst_raw or seg_raw in ("E", "NSE_EQ") else inst_raw
    return {"security_id": str(row[sec_col]), "segment": segment, "instrument": instrument}


def _dhan_post(path: str, payload: dict) -> dict:
    headers = _dhan_headers()
    if not headers:
        return {}
    response = requests.post(f"https://api.dhan.co/v2/{path}", headers=headers, json=payload, timeout=15)
    response.raise_for_status()
    data = response.json()
    if data.get("status") not in (None, "success"):
        raise RuntimeError(f"Dhan {path} failed: {data}")
    return data


def _dhan_frame(raw: dict) -> pd.DataFrame:
    if not raw or not raw.get("timestamp"):
        return pd.DataFrame()
    df = pd.DataFrame({
        "open": raw.get("open", []),
        "high": raw.get("high", []),
        "low": raw.get("low", []),
        "close": raw.get("close", []),
        "volume": raw.get("volume", []),
    })
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
    """Broker-first LTP/OHLC snapshot. Falls back silently per symbol."""
    out: dict[str, dict] = {s: {} for s in symbols}
    if not _dhan_enabled():
        return out
    grouped: dict[str, list[int]] = {}
    reverse: dict[tuple[str, str], str] = {}
    for sym in symbols:
        inst = _dhan_find_instrument(sym)
        if not inst:
            continue
        seg = inst["segment"]
        sid = int(inst["security_id"])
        grouped.setdefault(seg, []).append(sid)
        reverse[(seg, str(sid))] = sym
    if not grouped:
        return out
    try:
        data = {}
        for seg, ids in grouped.items():
            try:
                raw = _dhan_post("marketfeed/quote", {seg: ids})
                data.update(raw.get("data") or {})
            except Exception as e:
                print(f"[dhan quote_batch] segment {seg} failed: {e}")
        for seg, rows in (data or {}).items():
            for sid, item in (rows or {}).items():
                sym = reverse.get((seg, str(sid)))
                if not sym:
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
                    "change_pct": round(100 * (ltp - prev) / prev, 2) if ltp and prev else None,
                    "source": "dhan_quote",
                }
    except Exception as e:
        print(f"[dhan quote_batch] failed: {e}")
    return out


def ltp(symbol: str) -> float | None:
    q = quote_batch([symbol]).get(symbol) or {}
    if q.get("ltp"):
        return round(float(q["ltp"]), 2)
    try:
        yf_sym = YF_SYMBOL.get(symbol) or (f"{symbol}.NS" if not symbol.startswith("^") and "." not in symbol else symbol)
        info = _yf().Ticker(yf_sym).fast_info
        return round(float(info.last_price), 2) if info.last_price else None
    except Exception:
        return None


def _dhan_expiry(symbol: str, underlying_scrip: int, underlying_seg: str) -> str | None:
    cache_file = CACHE_DIR / f"dhan_expiries_{symbol.upper()}.json"
    try:
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 3600:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            expiries = cached.get("data") or []
            return sorted(expiries)[0] if expiries else None
    except Exception:
        pass

    payload = {"UnderlyingScrip": underlying_scrip, "UnderlyingSeg": underlying_seg}
    raw = _dhan_post("optionchain/expirylist", payload)
    expiries = raw.get("data") or []
    if expiries:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"ts": time.time(), "data": expiries}), encoding="utf-8")
        except Exception:
            pass
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
        records.append({
            "strikePrice": strike,
            "expiryDate": expiry,
            "CE": {
                "openInterest": ce.get("oi", 0) or 0,
                "changeinOpenInterest": (ce.get("oi", 0) or 0) - (ce.get("previous_oi", 0) or 0),
                "totalTradedVolume": ce.get("volume", 0) or 0,
                "lastPrice": ce.get("last_price", 0) or 0,
                "impliedVolatility": ce.get("implied_volatility", 0) or 0,
                "bidprice": ce.get("top_bid_price", 0) or 0,
                "askPrice": ce.get("top_ask_price", 0) or 0,
                "identifier": ce.get("security_id"),
            },
            "PE": {
                "openInterest": pe.get("oi", 0) or 0,
                "changeinOpenInterest": (pe.get("oi", 0) or 0) - (pe.get("previous_oi", 0) or 0),
                "totalTradedVolume": pe.get("volume", 0) or 0,
                "lastPrice": pe.get("last_price", 0) or 0,
                "impliedVolatility": pe.get("implied_volatility", 0) or 0,
                "bidprice": pe.get("top_bid_price", 0) or 0,
                "askPrice": pe.get("top_ask_price", 0) or 0,
                "identifier": pe.get("security_id"),
            },
        })
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
    if _data_sources_cfg().get("dhan", {}).get("enabled", True) is False:
        return {"records": {"data": []}}
    underlying = _dhan_underlying(symbol)
    if not underlying or not _dhan_headers():
        return {"records": {"data": []}}
    cached = _DHAN_OC_CACHE.get(symbol)
    if cached and time.time() - cached[0] < 3:
        return cached[1]
    underlying_scrip, underlying_seg = underlying
    expiry = _dhan_expiry(symbol, underlying_scrip, underlying_seg)
    if not expiry:
        return {"records": {"data": []}}
    payload = {"UnderlyingScrip": underlying_scrip, "UnderlyingSeg": underlying_seg, "Expiry": expiry}
    raw = _dhan_post("optionchain", payload)
    data = _dhan_to_nse_chain(symbol, raw, expiry)
    if data.get("records", {}).get("data"):
        _DHAN_OC_CACHE[symbol] = (time.time(), data)
    return data


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
        underlying = underlying or _first(row, "underlyingValue", "spot", "nifty", default=None)
        ce_oi = _first(row, "CE_openInterest", "call_oi", "ceoi", default=0) or 0
        pe_oi = _first(row, "PE_openInterest", "put_oi", "peoi", default=0) or 0
        ce_vol = _first(row, "CE_totalTradedVolume", "call_volume", "cevolume", default=0) or 0
        pe_vol = _first(row, "PE_totalTradedVolume", "put_volume", "pevolume", default=0) or 0
        ce_ltp = _first(row, "CE_lastPrice", "call_ltp", "celtp", default=0) or 0
        pe_ltp = _first(row, "PE_lastPrice", "put_ltp", "peltp", default=0) or 0
        ce_iv = _first(row, "CE_impliedVolatility", "call_iv", "ceiv", default=0) or 0
        pe_iv = _first(row, "PE_impliedVolatility", "put_iv", "peiv", default=0) or 0
        rows.append({
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
        })
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

_R360_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.research360.in/future-and-options/option-chain",
    "Origin": "https://www.research360.in",
    "X-Requested-With": "XMLHttpRequest",
}

# Research360 uses specific internal names for indices
_R360_SYMBOL_MAP = {
    "NIFTY": "NIFTY50",
    "BANKNIFTY": "NIFTYBANK",
    "FINNIFTY": "NIFTYFINSERVICE",
    "MIDCPNIFTY": "NFTMIDSELE",
}


def _get_r360_session() -> requests.Session:
    global _R360_SESSION, _R360_SESSION_TS
    with _R360_SESSION_LOCK:
        now = time.time()
        if _R360_SESSION is None or (now - _R360_SESSION_TS) > 1800:
            s = requests.Session()
            s.headers.update(_R360_HEADERS)
            try:
                s.get(
                    "https://www.research360.in/future-and-options/option-chain",
                    timeout=15,
                )
                _R360_SESSION = s
                _R360_SESSION_TS = now
            except Exception as e:
                print(f"[r360 session] failed: {e}")
                _R360_SESSION = None
    return _R360_SESSION


def _r360_expiries(session: requests.Session, symbol: str) -> list[str]:
    import re as _re
    # Research360 expiries endpoint uses 'symbol' parameter for indices
    r = session.get(
        "https://www.research360.in/fno/option/ajax/optionChainExp.php",
        headers=_R360_HEADERS,
        params={"table_flag": "optionChain", "symbol": symbol},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json().get("data", "")
    return _re.findall(r'value="(\d{4}-\d{2}-\d{2})"', data)


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
        key = float(s)
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
        records.append({
            "strikePrice": strike,
            "expiryDate": expiry,
            "CE": {
                "openInterest": ce_oi,
                "changeinOpenInterest": ce_oi_chg,
                "totalTradedVolume": 0,
                "lastPrice": ltp_ce.get(strike, 0.0),
                "impliedVolatility": 0.0,
            },
            "PE": {
                "openInterest": pe_oi,
                "changeinOpenInterest": pe_oi_chg,
                "totalTradedVolume": 0,
                "lastPrice": ltp_pe.get(strike, 0.0),
                "impliedVolatility": 0.0,
            },
        })

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
    sym = symbol.upper()
    # Map to Research360 specific index names if needed
    r360_sym = _R360_SYMBOL_MAP.get(sym, sym)

    session = _get_r360_session()
    if session is None:
        return {"records": {"data": []}}
    expiries = _r360_expiries(session, r360_sym)
    if not expiries:
        return {"records": {"data": []}}
    expiry = expiries[0]
    r = session.post(
        "https://www.research360.in/fno/option/ajax/optionChainApi.php",
        headers=_R360_HEADERS,
        data={"stock": r360_sym, "expiry": expiry, "showall": "on", "showallnew": "on"},
        timeout=30,
    )
    r.raise_for_status()
    raw = r.json()
    data = _r360_to_nse_chain(sym, raw, expiry)
    if not data.get("records", {}).get("data"):
        return {"records": {"data": []}}
    return data


def _option_chain_from_local_file(symbol: str) -> dict:
    cfg = _data_sources_cfg().get("local_files", {})
    raw_path = _env_or_value(cfg.get(f"{symbol.lower()}_option_chain") or cfg.get("option_chain"))
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
        df.columns = [c[0].lower() if isinstance(c, tuple) else str(c).lower() for c in df.columns]
    else:
        df.columns = [str(c).lower() for c in df.columns]
    return df


_OHLC_CACHE = {}
_OHLC_CACHE_BUCKET = 0


def ohlc(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily/intraday OHLC. Dhan primary, yfinance fallback."""
    try:
        df = _dhan_ohlc(symbol, period=period, interval=interval)
        if not df.empty:
            df.attrs["source"] = "dhan_historical"
            return df
    except Exception as e:
        print(f"[dhan ohlc] failed for {symbol}: {e}")
    yf = _yf()
    tkr = YF_SYMBOL.get(symbol) or (symbol if "." in symbol or "=" in symbol or "^" in symbol else f"{symbol}.NS")
    df = yf.download(tkr, period=period, interval=interval, progress=False, auto_adjust=False)
    df = _flatten_columns(df)
    if df.empty:
        return df
    df.index.name = "date"
    return df


def india_vix(period: str = "3mo") -> pd.DataFrame:
    return ohlc("INDIAVIX", period=period)


def option_chain(symbol: str = "NIFTY") -> dict:
    """Live option chain via nsepython or direct robust fetch. Returns {'records': ..., 'filtered': ...}."""
    global _NSE_SESSION, _NSE_SESSION_TS
    symbol = symbol.upper()
    cache_file = CACHE_DIR / f"option_chain_{symbol}.json"

    def _save_chain(data: dict) -> dict:
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"ts": time.time(), "data": data}, default=str), encoding="utf-8")
        except Exception:
            pass
        _OPTION_CHAIN_SOURCE[symbol] = data.get("_source") or "unknown"
        return data

    def _load_cached_chain() -> dict:
        try:
            if cache_file.exists():
                cached = json.loads(cache_file.read_text(encoding="utf-8"))
                data = cached.get("data") or {}
                if data.get("records", {}).get("data"):
                    data.setdefault("_cache", {})
                    data["_cache"].update({"stale": True, "ts": cached.get("ts")})
                    _OPTION_CHAIN_SOURCE[symbol] = f"cache:{data.get('_source') or 'unknown'}"
                    return data
        except Exception:
            pass
        return {"records": {"data": []}}

    def _try_external_fallbacks() -> dict:
        for fn in (_option_chain_from_local_file,):
            try:
                data = fn(symbol)
                if data and data.get("records", {}).get("data"):
                    return _save_chain(data)
            except Exception as e:
                print(f"[option_chain {fn.__name__}] failed for {symbol}: {e}")
        return {"records": {"data": []}}
    
    # 1. Dhan (preferred: has full data including LTPs when user has API access).
    try:
        data = _option_chain_from_dhan(symbol)
        if data and data.get("records", {}).get("data"):
            return _save_chain(data)
    except Exception as e:
        print(f"[option_chain dhan] failed for {symbol}: {e}")

    # 2. Research360 — no auth required, provides OI for all strikes + LTP
    #    for ~10 near-ATM strikes via graphprice/graphc/graphp arrays.
    try:
        data = _option_chain_from_research360(symbol)
        if data and data.get("records", {}).get("data"):
            return _save_chain(data)
    except Exception as e:
        print(f"[option_chain research360] failed for {symbol}: {e}")

    # 3. Try robust direct fetch (NSE) as third option
    session = _get_nse_session()
    if session:
        for attempt in range(2):
            try:
                indices = ['NIFTY', 'FINNIFTY', 'BANKNIFTY', 'MIDCPNIFTY']
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
                print(f"[option_chain robust fetch] failed for {symbol}: {e}")
                break
        try:
            indices = ['NIFTY', 'FINNIFTY', 'BANKNIFTY', 'MIDCPNIFTY']
            api_type = "indices" if symbol in indices else "equities"
            url = f"https://www.nseindia.com/api/option-chain-{api_type}?symbol={symbol}"
            
            # Temporary headers for this specific call
            call_headers = {
                "Referer": f"https://www.nseindia.com/get-quotes/option-chain?symbol={symbol}",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01"
            }
            
            response = session.get(url, headers=call_headers, timeout=10)
            if response.status_code == 200:
                data = response.json()
                if data and data.get("records", {}).get("data"):
                    return _save_chain(data)
        except Exception as e:
            print(f"[option_chain robust fetch] failed for {symbol}: {e}")

    # 3. Fallback to nsepython (might work if our session logic failed but theirs somehow succeeds)
    try:
        from nsepython import nse_optionchain_scrapper
        result = nse_optionchain_scrapper(symbol)
        if result and result.get("records", {}).get("data"):
            return _save_chain(result)
    except Exception:
        pass

    try:
        from nsepython import option_chain as nse_oc
        result = nse_oc(symbol)
        if result and result.get("records", {}).get("data"):
            return _save_chain(result)
    except Exception:
        pass

    fallback = _try_external_fallbacks()
    if fallback.get("records", {}).get("data"):
        return fallback

    data = _load_cached_chain()
    
    # Enrichment step for Research360: If LTP is 0, try to patch with Dhan LTPs
    if data.get("_source") == "research360":
        try:
            # We only need LTPs for the near-ATM strikes usually.
            # Patching the whole chain is expensive, but paper trading needs it.
            # Try to get underlying price first.
            spot = data.get("records", {}).get("underlyingValue")
            if spot:
                # Use a secondary call to Dhan just for LTPs if available
                dhan_data = _option_chain_from_dhan(symbol)
                if dhan_data.get("records", {}).get("data"):
                    # Create a map of strike+type -> LTP
                    ltp_map = {}
                    for row in dhan_data["records"]["data"]:
                        s = row["strikePrice"]
                        ltp_map[f"{s}_CE"] = row["CE"].get("lastPrice", 0)
                        ltp_map[f"{s}_PE"] = row["PE"].get("lastPrice", 0)
                    
                    # Apply to Research360 data
                    for row in data["records"]["data"]:
                        s = row["strikePrice"]
                        row["CE"]["lastPrice"] = ltp_map.get(f"{s}_CE", 0)
                        row["PE"]["lastPrice"] = ltp_map.get(f"{s}_PE", 0)
                    data["_source"] = "research360+dhan_ltp"
        except Exception as e:
            print(f"[option_chain enrichment] failed for {symbol}: {e}")

    return data


def get_pcr_max_pain_cached(symbol: str = "NIFTY") -> tuple[float | None, float | None, float | None, bool, bool, float | None, float | None]:
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
                "source": raw.get("_source") or option_chain_source(symbol) or "unknown",
            }
            with open(cache_file, 'w') as f:
                json.dump(cache_data, f)

            return pcr_oi, pcr_vol, max_pain, False, False, cache_data["ts"], cache_data["ts"]
    except Exception as e:
        print(f"[get_pcr_max_pain_cached] live fetch failed: {e}")
        
    # Fallback to cache
    if cache_file.exists():
        try:
            with open(cache_file, 'r') as f:
                cache_data = json.load(f)
            if cache_data.get("source"):
                _OPTION_CHAIN_SOURCE[symbol.upper()] = f"cache:{cache_data.get('source')}"
            age = time.time() - cache_data.get("ts", 0)
            is_stale = age > 900 # 15 mins
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
            print(f"[get_pcr_max_pain_cached] cache read failed: {e}")
            
    # If we got here, both live and cache failed. Do not take down dashboard flows.
    return None, None, None, True, True, None, None


def fii_dii_cash(days: int = 10) -> pd.DataFrame:
    """FII/DII cash market net buy/sell, last N sessions."""
    try:
        from nsepython import nse_fiidii
    except ImportError as e:
        raise RuntimeError("nsepython not installed or nse_fiidii not available") from e

    raw = nse_fiidii()
    if isinstance(raw, pd.DataFrame):
        df = raw
    elif isinstance(raw, list):
        df = pd.DataFrame(raw)
    else:
        return pd.DataFrame()

    if df.empty:
        return df

    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], format="%d-%b-%Y", errors="coerce")
        # Fix #11: Filter by Segment if available to avoid double counting
        cols = [c.lower() for c in df.columns]
        if "segment" in cols:
            seg_col = df.columns[cols.index("segment")]
            df = df[df[seg_col].str.lower().str.contains("cash", na=False)]
        df = df.sort_values("date").tail(days).reset_index(drop=True)
    else:
        df = df.tail(days).reset_index(drop=True)
    return df


def _cached_ohlc(symbol: str, period: str, interval: str, cache_key: str) -> pd.DataFrame:
    key = f"{symbol}_{period}_{interval}_{cache_key}"
    if key in _OHLC_CACHE and not _OHLC_CACHE[key].empty:
        return _OHLC_CACHE[key].copy()
        
    df = ohlc(symbol, period=period, interval=interval)
    if not df.empty:
        _OHLC_CACHE[key] = df.copy()
    return df

def ohlc_cached(symbol: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Time-bucketed cache to ensure fresh data every 2 minutes without spamming yfinance."""
    global _OHLC_CACHE, _OHLC_CACHE_BUCKET
    current_bucket = int(time.time() / 120)
    
    if current_bucket != _OHLC_CACHE_BUCKET:
        _OHLC_CACHE.clear()
        _OHLC_CACHE_BUCKET = current_bucket
        
    return _cached_ohlc(symbol, period, interval, str(current_bucket))


def sector_ohlc(sectors: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    return {s: ohlc_cached(s, period=period) for s in sectors}


def universe_ohlc(tickers: list[str], period: str = "6mo") -> dict[str, pd.DataFrame]:
    import os
    import time
    import yfinance as yf
    from pathlib import Path
    from datetime import timezone, timedelta
    
    cache_dir = Path("data/cache/ohlc")
    cache_dir.mkdir(parents=True, exist_ok=True)
    today_str = dt.datetime.now().strftime("%Y-%m-%d")

    results = {}
    missing_tickers = []
    
    # Check market status for cache invalidation
    ist_now = dt.datetime.now(timezone(timedelta(hours=5, minutes=30)))
    is_weekday = ist_now.weekday() < 5
    tt = ist_now.time()
    market_open = is_weekday and (tt.hour, tt.minute) >= (9, 15) and (tt.hour, tt.minute) <= (15, 30)

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
            except Exception:
                missing_tickers.append(t)
        else:
            missing_tickers.append(t)
            
    if not missing_tickers:
        return results

    # 2. Dhan historical primary for symbols that can be mapped.
    dhan_failed = []
    if _dhan_enabled():
        for t in missing_tickers:
            try:
                sym_df = _dhan_ohlc(t, period=period, interval="1d")
                if not sym_df.empty:
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
    chunks = [yf_tickers[i:i + 100] for i in range(0, len(yf_tickers), 100)]
    fetched, failed, skipped = 0, 0, 0
    
    for chunk in chunks:
        try:
            df_dict = yf.download(tickers=" ".join(chunk), period=period, group_by='ticker', threads=True, progress=False)
            time.sleep(1) # rate limit spacing
        except Exception as e:
            print(f"yfinance batch download failed, retrying once: {e}")
            time.sleep(2)
            try:
                df_dict = yf.download(tickers=" ".join(chunk), period=period, group_by='ticker', threads=True, progress=False)
            except Exception:
                failed += len(chunk)
                continue
                
        if len(chunk) == 1:
            sym = chunk[0].replace('.NS', '')
            sym_df = df_dict.copy()
            sym_df = _flatten_columns(sym_df)
            if not sym_df.empty:
                sym_df.index.name = "date"
                sym_df.to_pickle(cache_dir / f"{sym}_{today_str}.pkl")
                results[sym] = sym_df
                fetched += 1
            else:
                skipped += 1
        elif isinstance(df_dict.columns, pd.MultiIndex):
            # Dynamic ticker level detection
            ticker_level = 0
            if 'Close' in df_dict.columns.levels[0] or 'close' in df_dict.columns.levels[0]:
                ticker_level = 1
            elif 'Close' in df_dict.columns.levels[1] or 'close' in df_dict.columns.levels[1]:
                ticker_level = 0
            else:
                # Default to looking for tickers in level 0
                ticker_level = 0
                
            for yf_t in chunk:
                sym = yf_t.replace('.NS', '')
                try:
                    if yf_t in df_dict.columns.levels[ticker_level]:
                        sym_df = df_dict.xs(yf_t, level=ticker_level, axis=1).copy()
                        sym_df = _flatten_columns(sym_df)
                        # Drop rows where all elements are NaN
                        sym_df = sym_df.dropna(how='all')
                        if not sym_df.empty:
                            sym_df.index.name = "date"
                            sym_df.to_pickle(cache_dir / f"{sym}_{today_str}.pkl")
                            results[sym] = sym_df
                            fetched += 1
                        else:
                            skipped += 1
                    else:
                        failed += 1
                except Exception as ex:
                    failed += 1
                    print(f"universe_ohlc: {yf_t} err: {type(ex).__name__}: {ex}")
        else:
            failed += len(chunk)

    print(f"universe_ohlc: fetched={fetched} failed={failed} skipped={skipped}")
    return results
