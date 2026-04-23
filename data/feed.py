"""Data feed: OHLC, option chain, FII/DII, VIX. yfinance + nsepython."""
from __future__ import annotations

import datetime as dt
import time
from functools import lru_cache
from typing import Optional

import pandas as pd


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
    """Daily/intraday OHLC. symbol = index name or NSE ticker (appends .NS for stocks)."""
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
    """Live option chain via nsepython. Returns {'records': ..., 'filtered': ...}."""
    # Try nse_optionchain_scrapper first, then option_chain fallback
    try:
        from nsepython import nse_optionchain_scrapper
        result = nse_optionchain_scrapper(symbol)
        if result and result.get("records", {}).get("data"):
            return result
    except Exception:
        pass

    try:
        from nsepython import option_chain as nse_oc
        result = nse_oc(symbol)
        if result and result.get("records", {}).get("data"):
            return result
    except Exception:
        pass

    return {"records": {"data": []}}


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
    import yfinance as yf
    from pathlib import Path
    
    cache_dir = Path("data/cache/ohlc")
    cache_dir.mkdir(parents=True, exist_ok=True)
    today_str = dt.datetime.now().strftime("%Y-%m-%d")

    results = {}
    missing_tickers = []

    # 1. Load from cache if today's file exists
    for t in tickers:
        cache_file = cache_dir / f"{t}_{today_str}.pkl"
        if cache_file.exists():
            try:
                results[t] = pd.read_pickle(cache_file)
            except Exception:
                missing_tickers.append(t)
        else:
            missing_tickers.append(t)
            
    if not missing_tickers:
        return results
        
    # 2. Batch fetch missing ones
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
            ticker_level = 0
            # Newer yf might ignore group_by='ticker' sometimes and put tickers in level 1
            if 'Close' in df_dict.columns.levels[0] or 'close' in df_dict.columns.levels[0]:
                ticker_level = 1
                
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
