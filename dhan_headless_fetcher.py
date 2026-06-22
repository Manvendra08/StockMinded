"""
Dhan Headless Option Chain Fetcher — Playwright persistent-context interceptor.

Flow:
  1. Load persistent browser context from DHAN_PROFILE_DIR (survives login).
  2. Navigate to https://options-trader.dhan.co/advanceoptionchain
  3. Intercept XHR/fetch responses matching the option-chain API endpoint.
  4. Parse and normalise the JSON payload to base_fetcher schema.

First-run / session-expired: falls back gracefully; use `tools/authenticate_dhan.py`
to perform the interactive QR-scan login and persist the session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from src.fetchers.dhan_commodity_fetcher import DhanCommodityFetcher

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_DHAN_OC_URL = "https://options-trader.dhan.co/advanceoptionchain"

# Known API path fragments for option-chain endpoint on dhan options-trader
_OC_PATH_FRAGMENTS = (
    "/optionchain",
    "/option-chain",
    "/optchain",
    "/oc-data",
)

_PROFILE_DIR = Path(
    os.getenv("DHAN_PROFILE_DIR", Path.home() / ".nsebot" / "dhan_profile")
)

_DHAN_SYMBOL_MAP: dict[str, str] = {
    "NATURALGAS": "NATURALGAS",
    "CRUDEOIL": "CRUDEOIL",
    "GOLD": "GOLD",
    "SILVER": "SILVER",
    "NIFTY": "NIFTY",
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY": "FINNIFTY",
}

_WAIT_TIMEOUT_MS = 15_000
_PAGE_LOAD_WAIT_MS = 4_000
_INTERCEPT_TIMEOUT_MS = 12_000


def _normalise_dhan_payload(raw: dict, symbol: str) -> dict | None:
    """
    Normalise raw Dhan option chain JSON to base_fetcher schema.
    Handles multiple known Dhan API response shapes.
    """
    try:
        # Shape 1: {data: {optionChain: [...], underlyingValue: float, expiryDate: str}}
        data = raw.get("data") or raw
        expiry = (
            data.get("expiryDate")
            or data.get("expiry_date")
            or data.get("expiry")
            or ""
        )
        underlying = (
            data.get("underlyingValue")
            or data.get("underlying_value")
            or data.get("ltp")
            or data.get("spot_price")
        )

        chain_list = (
            data.get("optionChain")
            or data.get("option_chain")
            or data.get("strikes")
            or data.get("oc_data")
            or []
        )

        strikes: list[dict] = []
        for row in chain_list:
            # Each row may contain both CE and PE legs
            for leg_key, opt_type in (("CE", "CE"), ("PE", "PE")):
                leg = row.get(leg_key) or {}
                if not leg:
                    # Some shapes are flat per row with option_type field
                    if row.get("option_type", "").upper() == opt_type:
                        leg = row
                    else:
                        continue

                strike_price = (
                    row.get("strike_price")
                    or row.get("strikePrice")
                    or leg.get("strike_price")
                    or leg.get("strikePrice")
                )
                if strike_price is None:
                    continue

                strikes.append({
                    "strike": float(strike_price),
                    "option_type": opt_type,
                    "ltp": float(leg.get("last_price") or leg.get("ltp") or 0),
                    "oi": int(leg.get("open_interest") or leg.get("oi") or 0),
                    "oi_change": int(
                        leg.get("oi_change")
                        or leg.get("change_in_oi")
                        or leg.get("changeInOI")
                        or 0
                    ),
                    "volume": int(leg.get("volume") or leg.get("vol") or 0),
                    "iv": float(leg["implied_volatility"]) if leg.get("implied_volatility") else None,
                    "bid": float(leg["bid_price"]) if leg.get("bid_price") else None,
                    "ask": float(leg["ask_price"]) if leg.get("ask_price") else None,
                    "delta": float(leg["delta"]) if leg.get("delta") else None,
                    "theta": float(leg["theta"]) if leg.get("theta") else None,
                    "gamma": float(leg["gamma"]) if leg.get("gamma") else None,
                    "vega": float(leg["vega"]) if leg.get("vega") else None,
                })

        if not strikes:
            log.warning("[dhan_headless] parsed 0 strikes — raw keys: %s", list(raw.keys())[:10])
            return None

        strikes.sort(key=lambda r: (r["strike"], r["option_type"]))

        return {
            "symbol": symbol,
            "underlying_price": float(underlying) if underlying else None,
            "expiry": str(expiry),
            "strikes": strikes,
            "source": "dhan_headless",
            "fetched_at": datetime.now(IST).isoformat(),
        }
    except Exception as exc:
        log.error("[dhan_headless] normalisation error: %s", exc, exc_info=True)
        return None


async def _intercept_option_chain_async(symbol: str) -> dict | None:
    """
    Launch persistent context, navigate to Dhan advanced option chain,
    intercept the option chain API response, and return normalised data.
    """
    try:
        from playwright.async_api import async_playwright, Response
    except ImportError:
        log.error("[dhan_headless] playwright not installed")
        return None

    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    intercepted: dict | None = None
    intercept_event = asyncio.Event()

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            str(_PROFILE_DIR),
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def _on_response(response: Response):
            nonlocal intercepted
            if intercept_event.is_set():
                return
            url = response.url
            if any(frag in url.lower() for frag in _OC_PATH_FRAGMENTS):
                try:
                    body = await response.json()
                    intercepted = body
                    intercept_event.set()
                    log.info("[dhan_headless] intercepted: %s", url)
                except Exception as exc:
                    log.debug("[dhan_headless] json parse failed on %s: %s", url, exc)

        page.on("response", _on_response)

        try:
            log.info("[dhan_headless] navigating to Dhan option chain for %s", symbol)
            await page.goto(_DHAN_OC_URL, wait_until="domcontentloaded", timeout=15_000)

            # Try to select the symbol from the UI if a dropdown is available
            try:
                sym_input = await page.wait_for_selector(
                    "input[placeholder*='symbol' i], input[placeholder*='Symbol' i], "
                    ".symbol-selector input, #symbolSearch",
                    timeout=5_000,
                )
                await sym_input.fill(symbol)
                await page.keyboard.press("Enter")
                await page.wait_for_timeout(2_000)
            except Exception:
                log.debug("[dhan_headless] no symbol selector found; using default loaded symbol")

            # Wait for intercept or timeout
            try:
                await asyncio.wait_for(intercept_event.wait(), timeout=_INTERCEPT_TIMEOUT_MS / 1000)
            except asyncio.TimeoutError:
                log.warning("[dhan_headless] intercept timeout after %ds for %s", _INTERCEPT_TIMEOUT_MS // 1000, symbol)

        except Exception as exc:
            log.error("[dhan_headless] page navigation error: %s", exc)
        finally:
            await ctx.close()

    if not intercepted:
        return None

    return _normalise_dhan_payload(intercepted, symbol)


def _fetch_sync(symbol: str) -> dict | None:
    """Run async interceptor in isolated event loop."""
    import sys
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_intercept_option_chain_async(symbol))
    finally:
        loop.close()


class DhanHeadlessFetcher:
    """Playwright persistent-context option chain interceptor for Dhan."""

    name = "dhan_headless"

    def fetch_option_chain(self, symbol: str) -> dict | None:
        base = symbol.upper().split()[0]
        if base not in _DHAN_SYMBOL_MAP:
            log.warning("[dhan_headless] unsupported symbol: %s", symbol)
            return None

        if base in {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER"}:
            log.warning(
                "[dhan_headless] MCX commodity %s routed to public commodity scrape",
                base,
            )
            fallback = DhanCommodityFetcher().fetch_option_chain(base)
            if fallback and fallback.get("strikes"):
                return fallback

        result = _fetch_sync(base)
        if result and result.get("strikes"):
            log.info(
                "[dhan_headless] %s → %d strikes (expiry %s)",
                base, len(result["strikes"]), result.get("expiry"),
            )
            return result

        else:
            log.warning(
                "[dhan_headless] no data for %s — session may be expired; "
                "run tools/authenticate_dhan.py to re-login",
                base,
            )
        return result
