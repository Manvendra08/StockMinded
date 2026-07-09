"""Shoonya (Finvasia) data fetcher — primary market data source.

OAuth 2.0 Authentication (from 1st April 2026) using Playwright browser automation.

Provides:
  - Option chain data (GetOptionChain API)
  - Quote/LTP data (GetQuotes API)
  - Underlying spot prices

Credentials (from .env):
  SHOONYA_USER_ID
  SHOONYA_PASSWORD
  SHOONYA_TOTP_KEY
  SHOONYA_API_SECRET
  SHOONYA_VENDOR_CODE (optional, defaults to <user_id>_U)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pyotp

logger = logging.getLogger(__name__)

_LOGIN_LOCK = threading.Lock()

IST = timezone(timedelta(hours=5, minutes=30))

_API_BASE = "https://api.shoonya.com/NorenWClientAPI"
_TOKEN_URL = "https://api.shoonya.com/NorenWClientAPI/GenAcsTok"

# Index → Shoonya exchange mapping
_INDEX_SPOT_NAMES = {
    "NIFTY": "Nifty 50",
    "BANKNIFTY": "Nifty Bank",
    "FINNIFTY": "Nifty Fin Services",
    "MIDCPNIFTY": "Nifty Midcap 100",
    "SENSEX": "S&P BSE SENSEX",
    "NIFTY IT": "Nifty IT",
    "NIFTY AUTO": "Nifty Auto",
    "NIFTY PHARMA": "Nifty Pharma",
    "NIFTY FMCG": "Nifty FMCG",
    "NIFTY METAL": "Nifty Metal",
    "NIFTY ENERGY": "Nifty Energy",
    "NIFTY REALTY": "Nifty Realty",
    "NIFTY INFRA": "Nifty Infra",
    "NIFTY PSE": "Nifty PSE",
    "NIFTY PVT BANK": "Nifty Pvt Bank",
}

# NSE index tokens for spot index queries (NSE cash exchange)
_INDEX_NSE_TOKENS: dict[str, str] = {
    "NIFTY": "26000",
    "BANKNIFTY": "26009",
    "FINNIFTY": "26037",
    "MIDCPNIFTY": "26074",
    "SENSEX": "1",  # Correct SENSEX token for BSE
    "NIFTY IT": "26008",
    "NIFTY AUTO": "26029",
    "NIFTY PHARMA": "26023",
    "NIFTY FMCG": "26021",
    "NIFTY METAL": "26030",
    "NIFTY ENERGY": "26020",
    "NIFTY REALTY": "26018",
    "NIFTY INFRA": "26019",
    "NIFTY PSE": "26024",
    "NIFTY PVT BANK": "26047",
}
_EXCHANGE_MAP: dict[str, str] = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO",
    "SENSEX": "BFO",
    "NIFTY IT": "NFO",
    "NIFTY AUTO": "NFO",
    "NIFTY PHARMA": "NFO",
    "NIFTY FMCG": "NFO",
    "NIFTY METAL": "NFO",
    "NIFTY ENERGY": "NFO",
    "NIFTY REALTY": "NFO",
    "NIFTY INFRA": "NFO",
    "NIFTY PSE": "NFO",
    "NIFTY PVT BANK": "NFO",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _post_jdata(
    url: str, payload: dict, access_token: str | None = None
) -> dict | None:
    """POST jData= encoded payload, return parsed JSON or None.

    Uses curl_cffi (libcurl-based, robust TLS) with fallback to stdlib urllib.
    """
    body_str = "jData=" + json.dumps(payload, separators=(",", ":"))
    if access_token:
        body_str += f"&jKey={access_token}"
    headers: dict[str, str] = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0",
    }

    # curl_cffi path — works around Windows SSL/TLS issues
    try:
        from curl_cffi import requests as curl_requests

        max_retries = 2
        last_exc = None
        for attempt in range(max_retries):
            try:
                resp = curl_requests.post(
                    url, data=body_str, headers=headers, timeout=30, impersonate="chrome120"
                )
                if resp.status_code >= 200 and resp.status_code < 300:
                    return resp.json()
                raw = resp.text
                try:
                    parsed = json.loads(raw)
                except Exception:
                    logger.error(
                        "[shoonya] POST %s -> HTTP %s: %s",
                        url,
                        resp.status_code,
                        raw[:200],
                    )
                    return None
                if _is_session_expired_response(parsed):
                    logger.info(
                        "[shoonya] POST %s -> HTTP %s: session expired",
                        url,
                        resp.status_code,
                    )
                else:
                    emsg = str(parsed.get("emsg") or parsed.get("Emsg") or "")
                    if "no data" in emsg.lower():
                        logger.debug(
                            "[shoonya] POST %s -> HTTP %s: %s",
                            url,
                            resp.status_code,
                            emsg[:100],
                        )
                    else:
                        logger.error(
                            "[shoonya] POST %s -> HTTP %s: %s",
                            url,
                            resp.status_code,
                            raw[:200],
                        )
                return parsed
            except Exception as exc:
                last_exc = exc
                error_str = str(exc)
                is_dns_error = "Could not resolve host" in error_str or "DNSError" in error_str
                if attempt < max_retries - 1:
                    logger.warning("[shoony] POST %s attempt %d failed: %s, retrying...", url, attempt + 1, exc)
                    import time
                    time.sleep(1)
                else:
                    if is_dns_error:
                        logger.warning("[shoonya] POST %s (curl_cffi) DNS failed after %d retries: %s. Falling back to urllib.", url, max_retries, exc)
                    else:
                        logger.error("[shoonya] POST %s (curl_cffi) failed after %d retries: %s", url, max_retries, exc)
                    break
    except ImportError:
        pass

    # Fallback: stdlib urllib
    try:
        body = body_str.encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            parsed = json.loads(raw)
        except Exception:
            logger.error("[shoonya] POST %s -> HTTP %s: %s", url, e.code, raw[:200])
            return None
        if _is_session_expired_response(parsed):
            logger.info("[shoonya] POST %s -> HTTP %s: session expired", url, e.code)
        else:
            emsg = str(parsed.get("emsg") or parsed.get("Emsg") or "")
            if "no data" in emsg.lower():
                logger.debug(
                    "[shoonya] POST %s -> HTTP %s: %s",
                    url,
                    e.code,
                    emsg[:100],
                )
            else:
                logger.error("[shoonya] POST %s -> HTTP %s: %s", url, e.code, raw[:200])
        return parsed
    except Exception as exc:
        logger.error("[shoonya] POST %s failed: %s", url, exc)
        return None


def _is_session_expired_response(response: dict | None) -> bool:
    """Return True when Shoonya reports an expired/invalid session token."""
    if not isinstance(response, dict):
        return False
    emsg = str(response.get("emsg") or response.get("Emsg") or "").lower()
    return (
        response.get("stat") == "Not_Ok"
        and "session" in emsg
        and ("expired" in emsg or "invalid session" in emsg or "invalid" in emsg)
    )


def _resolve_token_cache_dir() -> str:
    """Return project scratch directory for token caching."""
    # __file__ = data/shoonya_fetcher.py → parent.parent = project root
    scratch = Path(__file__).resolve().parent.parent / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    return str(scratch)


def _read_shared_token_file(filepath: str) -> dict | None:
    import json
    import time

    for _ in range(10):
        try:
            if not os.path.exists(filepath):
                return None
            with open(filepath, "r") as f:
                return json.load(f)
        except (PermissionError, OSError, json.JSONDecodeError):
            time.sleep(0.05)
    return None


def _write_shared_token_file(filepath: str, data: dict) -> bool:
    import json
    import time

    temp_filepath = filepath + ".tmp"
    for _ in range(10):
        try:
            os.makedirs(os.path.dirname(filepath), exist_ok=True)
            with open(temp_filepath, "w") as f:
                json.dump(data, f)
            if os.path.exists(filepath):
                os.remove(filepath)
            os.rename(temp_filepath, filepath)
            return True
        except (PermissionError, OSError):
            time.sleep(0.05)
            try:
                if os.path.exists(temp_filepath):
                    os.remove(temp_filepath)
            except Exception:
                pass
    return False


# ---------------------------------------------------------------------------
# ShoonyaFetcher
# ---------------------------------------------------------------------------


class ShoonyaFetcher:
    """Shoonya API client with OAuth login, option chain and quote retrieval."""

    name = "shoonya"
    _TOKEN_REFRESH_INTERVAL = 50400  # seconds (14 hours)

    def __init__(self):
        self.access_token: str | None = None
        self._token_created_at: float = 0.0

        # Read credentials from environment
        self.user_id = os.environ.get("SHOONYA_USER_ID")
        self.password = os.environ.get("SHOONYA_PASSWORD")
        self.totp_key = os.environ.get("SHOONYA_TOTP_KEY")
        self.secret_code = os.environ.get("SHOONYA_API_SECRET")
        vendor = os.environ.get("SHOONYA_VENDOR_CODE")
        self.vendor_code = vendor or (f"{self.user_id}_U" if self.user_id else "")

        # Shared Shoonya token JSON file in NSEBOT root folder
        self._token_cache = "C:/Users/manve/Downloads/NSEBOT/shoonya_shared_token.json"

        # Try loading cached token
        self._load_cached_token()

    # -- Token persistence ------------------------------------------------

    def _save_token(self) -> None:
        if not self.access_token:
            return
        data = {
            "susertoken": self.access_token,
            "access_token": self.access_token,
            "userid": self.user_id,
            "last_updated": self._token_created_at,
        }
        if _write_shared_token_file(self._token_cache, data):
            logger.debug("[shoonya] token cached to %s", self._token_cache)
        else:
            logger.warning("[shoonya] failed to cache token to %s", self._token_cache)

    def _load_cached_token(self) -> None:
        data = _read_shared_token_file(self._token_cache)
        if data and isinstance(data, dict):
            token = data.get("susertoken") or data.get("access_token")
            if token:
                self.access_token = token
                self._token_created_at = data.get("last_updated", time.time())
                logger.debug("[shoonya] loaded cached token from %s", self._token_cache)

    def _clear_cached_token(self) -> None:
        self.access_token = None
        for _ in range(10):
            try:
                if os.path.exists(self._token_cache):
                    os.remove(self._token_cache)
                    logger.debug("[shoonya] cleared cached token")
                break
            except (PermissionError, OSError):
                import time

                time.sleep(0.05)

    def _verify_token(self) -> bool:
        """Quick lightweight check: does the cached token still work?

        BUG-03 FIX: Use NSE exchange with a known equity symbol (RELIANCE)
        instead of NFO/NIFTY. NIFTY is an index and doesn't exist as a scrip
        on NFO, so SearchScrip always returned empty → token always 'failed'.
        """
        import urllib.parse

        payload = {
            "uid": self.user_id,
            "exch": "NSE",
            "stext": urllib.parse.quote_plus("RELIANCE"),
        }
        res = _post_jdata(
            f"{_API_BASE}/SearchScrip",
            payload,
            self.access_token,
        )
        if res and res.get("stat") == "Ok":
            logger.debug("[shoonya] cached token is still valid")
            return True
        if _is_session_expired_response(res):
            logger.info("[shoonya] cached token expired — will re-authenticate")
        else:
            logger.info(
                "[shoonya] cached token rejected — will re-authenticate: %s", res
            )
        self._clear_cached_token()
        return False

    # -- OAuth Login ------------------------------------------------------

    def _get_auth_code_playwright(self) -> str | None:
        """Headless browser OAuth login via Playwright.

        Retries once on transient failures (page didn't load, selector timeout)
        which are common when the network is flaky or the OAuth endpoint is slow.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.error(
                "[shoonya] playwright not installed. Run: pip install playwright && playwright install chromium"
            )
            return None

        authorize_url = f"https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={self.vendor_code}"

        for attempt in range(2):
            if attempt > 0:
                logger.info("[shoonya] Retrying OAuth login (attempt %d)...", attempt + 1)
                time.sleep(3)

            auth_code: str | None = None
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    page = browser.new_page()
                    captured_urls: list[str] = []

                    def handle_route(route):
                        req_type = route.request.resource_type
                        if req_type in ("image", "font"):
                            route.abort()
                        else:
                            route.continue_()

                    page.route("**/*", handle_route)

                    page.on(
                        "request",
                        lambda r: captured_urls.append(r.url) if "code=" in r.url else None,
                    )
                    page.on(
                        "response",
                        lambda r: captured_urls.append(r.url) if "code=" in r.url else None,
                    )

                    page.goto(authorize_url, wait_until="commit")
                    # Reduced timeout from 60s to 45s; retry handles slow loads
                    page.wait_for_selector("#lgnusrid", state="visible", timeout=45000)

                    totp = pyotp.TOTP(self.totp_key).now()
                    page.locator("#lgnusrid").fill(self.user_id)
                    page.locator("#lgnpwd").fill(self.password)
                    page.locator("#lgnotp").fill(totp)

                    try:
                        page.locator("button:has-text('LOGIN')").click()
                        page.wait_for_url("*code=*", timeout=45000)
                    except Exception as click_err:
                        logger.debug("[shoonya] Browser navigation error: %s", click_err)

                    final_url = page.url
                    browser.close()

                    for candidate in [final_url] + captured_urls:
                        m = re.search(r"[?&]code=([A-Za-z0-9_\-]+)", candidate)
                        if m:
                            auth_code = m.group(1)
                            logger.info("[shoonya] auth_code captured successfully")
                            break

                    if not auth_code:
                        logger.error(
                            "[shoonya] auth_code not found. Final URL: %s", final_url
                        )
            except Exception as exc:
                logger.warning("[shoonya] Playwright OAuth attempt %d failed: %s", attempt + 1, exc)
                continue

            if auth_code:
                return auth_code

        logger.error("[shoonya] All OAuth attempts exhausted")
        return None

    def _exchange_for_token(self, auth_code: str) -> str | None:
        checksum = _sha256(self.vendor_code + self.secret_code + auth_code)
        payload = {"uid": self.user_id, "code": auth_code, "checksum": checksum}
        res = _post_jdata(_TOKEN_URL, payload)
        if not res:
            return None
        if res.get("stat") != "Ok":
            emsg = res.get("emsg", "")
            logger.error("[shoonya] GenAcsTok failed: %s", res)
            if "INVALID_IP" in emsg:
                logger.error(
                    "[shoonya] INVALID_IP: Your machine IP is not whitelisted. "
                    "Contact Shoonya support to whitelist this IP, or set SHOONYA_VENDOR_CODE if not already configured."
                )
            return None
        token = res.get("access_token") or res.get("susertoken")
        if not token:
            logger.error("[shoonya] GenAcsTok: no token in response: %s", res)
        return token

    def login(self, force: bool = False) -> bool:
        """Authenticate with Shoonya. Uses cached token if valid."""
        global _SHOONYA_LOGIN_FAILURE_TS

        with _LOGIN_LOCK:
            if (
                _SHOONYA_LOGIN_FAILURE_TS
                and (time.time() - _SHOONYA_LOGIN_FAILURE_TS) < _SHOONYA_LOGIN_COOLDOWN
            ):
                logger.debug(
                    "[shoonya] login cooldown active (%d s remaining)",
                    int(
                        _SHOONYA_LOGIN_COOLDOWN
                        - (time.time() - _SHOONYA_LOGIN_FAILURE_TS)
                    ),
                )
                return False

            if force:
                self._clear_cached_token()

            # Load from shared cache first to see if another process updated it
            self._load_cached_token()

            if self.access_token:
                if self._verify_token():
                    logger.info("[shoonya] reused cached token \u2014 skipping OAuth")
                    return True

            missing = [
                k
                for k, v in [
                    ("SHOONYA_USER_ID", self.user_id),
                    ("SHOONYA_PASSWORD", self.password),
                    ("SHOONYA_TOTP_KEY", self.totp_key),
                    ("SHOONYA_API_SECRET", self.secret_code),
                ]
                if not v
            ]
            if missing:
                logger.warning(
                    "[shoonya] missing credentials: %s \u2014 skipping", missing
                )
                return False

            # BUG-32 NOTE: Intentionally set BEFORE the auth attempt so that if
            # the Playwright thread hangs or deadlocks, the cooldown still activates.
            # On success, _SHOONYA_LOGIN_FAILURE_TS is reset to 0.0 below.
            _SHOONYA_LOGIN_FAILURE_TS = time.time()

            try:
                # Run the full OAuth flow in a timed thread so we never hang forever.
                auth_code = None

                def _do_auth():
                    nonlocal auth_code
                    try:
                        auth_code = self._get_auth_code_playwright()
                    except Exception:
                        pass

                t = threading.Thread(target=_do_auth, daemon=True)
                start_ts = time.time()
                t.start()
                t.join(timeout=90)  # 90 second timeout for the full browser flow
                elapsed = time.time() - start_ts
                if t.is_alive():
                    logger.warning("[shoonya] OAuth timed out after %.0fs", elapsed)
                    _SHOONYA_LOGIN_FAILURE_TS = time.time()
                    return False

                if not auth_code:
                    logger.error(
                        "[shoonya] Failed to obtain auth_code (%.0fs)", elapsed
                    )
                    _SHOONYA_LOGIN_FAILURE_TS = time.time()
                    return False
                logger.info(
                    "[shoonya] Exchanging auth_code for access_token (%.0fs)...",
                    elapsed,
                )
                token = self._exchange_for_token(auth_code)
                if not token:
                    _SHOONYA_LOGIN_FAILURE_TS = time.time()
                    return False
                self.access_token = token
                self._save_token()
                _SHOONYA_LOGIN_FAILURE_TS = 0.0
                logger.info("[shoonya] OAuth login successful")
                return True
            except Exception as exc:
                logger.exception("[shoonya] login exception: %s", exc)
                _SHOONYA_LOGIN_FAILURE_TS = time.time()
                return False

    # -- API calls --------------------------------------------------------

    def _api_call(self, endpoint: str, payload: dict) -> dict | None:
        # Load the latest token from the shared JSON file
        self._load_cached_token()

        if not self.access_token:
            if not self.login():
                return None

        # Adjust/stagger call frequency to avoid affecting the NSEBOT scan
        # Sleeping 0.2s throttles STOCKMINDED to maximum 5 requests/sec
        time.sleep(0.2)

        payload.setdefault("uid", self.user_id)
        res = _post_jdata(f"{_API_BASE}/{endpoint}", payload, self.access_token)

        # Rotate token if a new one was returned on success
        if res and isinstance(res, dict) and res.get("stat") == "Ok":
            fresh_token = res.get("susertoken") or res.get("access_token")
            if fresh_token and fresh_token != self.access_token:
                self.access_token = fresh_token
                self._token_created_at = time.time()
                self._save_token()

        if not _is_session_expired_response(res):
            return res

        logger.warning(
            "[shoonya] session expired during %s. Checking shared token file...",
            endpoint,
        )

        # Check if the shared token was updated by another process
        old_token = self.access_token
        self._load_cached_token()
        if self.access_token and self.access_token != old_token:
            logger.info(
                "[shoonya] Retrying %s with newly loaded shared token...", endpoint
            )
            # Sleep again to respect staggering on retry
            time.sleep(0.2)
            res = _post_jdata(f"{_API_BASE}/{endpoint}", payload, self.access_token)

            # Rotate token if retry was successful
            if res and isinstance(res, dict) and res.get("stat") == "Ok":
                fresh_token = res.get("susertoken") or res.get("access_token")
                if fresh_token and fresh_token != self.access_token:
                    self.access_token = fresh_token
                    self._token_created_at = time.time()
                    self._save_token()

            if not _is_session_expired_response(res):
                return res

        # If it's still expired, re-authenticate as last resort
        logger.warning(
            "[shoonya] Shared token still expired. Clearing and performing full login...",
        )
        if not self.login(force=True):
            global _SHOONYA_LOGIN_FAILURE_TS
            _SHOONYA_LOGIN_FAILURE_TS = time.time()
            self._clear_cached_token()
            logger.warning(
                "[shoonya] re-authentication failed; Shoonya disabled for %ds",
                _SHOONYA_LOGIN_COOLDOWN,
            )
            return res

        # Retry once more with the brand new token we generated
        time.sleep(0.2)
        res = _post_jdata(f"{_API_BASE}/{endpoint}", payload, self.access_token)
        if res and isinstance(res, dict) and res.get("stat") == "Ok":
            fresh_token = res.get("susertoken") or res.get("access_token")
            if fresh_token and fresh_token != self.access_token:
                self.access_token = fresh_token
                self._token_created_at = time.time()
                self._save_token()
        return res

    def _search_scrip(self, exchange: str, searchtext: str) -> dict | None:
        import urllib.parse

        return self._api_call(
            "SearchScrip",
            {"exch": exchange, "stext": urllib.parse.quote_plus(searchtext)},
        )

    def _get_quotes(self, exchange: str, token: str) -> dict | None:
        return self._api_call("GetQuotes", {"exch": exchange, "token": token})

    def _get_option_chain(
        self, exchange: str, tsym: str, strikeprice: float, count: int = 15
    ) -> dict | None:
        return self._api_call(
            "GetOptionChain",
            {
                "exch": exchange,
                "tsym": tsym,
                "strprc": str(strikeprice),
                "cnt": str(count),
            },
        )

    def fetch_fno_quote(self, symbol: str) -> dict | None:
        """Fetch NFO futures quote for an F&O stock.

        Queries NFO exchange for the nearest-month FUTSTK contract and
        returns full OHLC + OI + prev_close + change_pct from GetQuotes.

        Returns dict with keys:
          ltp, open, high, low, close, volume, oi, prev_close, change_pct
        or None on failure / if no futures contract found.
        """
        if not self.login():
            return None

        base = symbol.upper().split()[0]
        try:
            scrip_res = self._search_scrip("NFO", base)
            if (
                not scrip_res
                or scrip_res.get("stat") != "Ok"
                or not scrip_res.get("values")
            ):
                return None

            values = scrip_res["values"]
            now = datetime.now()

            # Find nearest-month FUTSTK (stock futures) contract
            target_item = None
            target_diff = None
            for v in values:
                if v.get("instname") != "FUTSTK":
                    continue
                tsym = v.get("tsym", "")
                # Extract expiry from tsym pattern (e.g. RELIANCE26JUNFUT)
                # BUG-02 FIX: FUTSTK tsym format is e.g. RELIANCE26JUNFUT
                # (suffix is 'FUT', not 2-digit year). Match both formats.
                m = re.search(rf"^{base}(\d{{2}}[A-Z]{{3}})(\d{{2}}|FUT)", tsym)
                if not m:
                    continue
                try:
                    date_part = m.group(1)  # e.g. "26JUN"
                    suffix = m.group(2)     # e.g. "25" or "FUT"
                    if suffix == "FUT":
                        # For FUT suffix, parse month/day and assume current year
                        exp_dt = datetime.strptime(date_part, "%d%b").replace(year=now.year)
                        if exp_dt < now:
                            exp_dt = exp_dt.replace(year=now.year + 1)
                    else:
                        exp_dt = datetime.strptime(date_part + suffix, "%d%b%y")
                except ValueError:
                    continue
                diff = abs((exp_dt - now).days)
                if target_item is None or diff < target_diff:
                    target_item = v
                    target_diff = diff

            if not target_item:
                return None

            token = target_item.get("token")
            if not token:
                return None

            quote = self._get_quotes("NFO", token)
            if not quote or quote.get("stat") != "Ok":
                return None

            def _f(key: str) -> float | None:
                try:
                    return float(quote[key])
                except (ValueError, TypeError, KeyError):
                    return None

            ltp = _f("lp")
            close_price = _f("c")
            prev_close = close_price
            change_pct = (
                round(100 * (ltp - prev_close) / prev_close, 2)
                if ltp is not None and prev_close and prev_close != 0
                else None
            )

            return {
                "ltp": ltp,
                "open": _f("o"),
                "high": _f("h"),
                "low": _f("l"),
                "close": close_price,
                "volume": _f("v"),
                "oi": _f("oi"),
                "prev_close": prev_close,
                "change_pct": change_pct,
                "source": "shoonya_fno",
            }

        except Exception as exc:
            logger.warning("[shoonya] FNO quote fetch failed for %s: %s", symbol, exc)
            return None

    # -- Public interface -------------------------------------------------

    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
        """Fetch full option chain for a symbol.

        Returns normalized dict:
        {
          "symbol": str,
          "underlying_price": float,
          "expiry": str (YYYY-MM-DD),
          "strikes": [{"strike": float, "option_type": "CE"|"PE", "ltp": float, ...}, ...]
        }
        Returns None on failure.
        """
        if not self.login():
            return None

        base = symbol.upper().split()[0]
        try:
            is_index = base in _INDEX_SPOT_NAMES
            if is_index:
                exch = _EXCHANGE_MAP.get(base, "NFO")
                search_text = base
                instname = "FUTIDX"
                option_exch = exch
                if base == "SENSEX":
                    option_exch = "BFO"
                    exch = "BFO"
                    search_text = "SENSEX FUT"
            else:
                logger.warning("[shoonya] symbol %s not in index list; skipping", base)
                return None

            # 1. Resolve underlying futures contract
            search_res = self._search_scrip(exch, search_text)
            if (
                not search_res
                or search_res.get("stat") != "Ok"
                or not search_res.get("values")
            ):
                logger.warning("[shoonya] could not search scrip for %s", search_text)
                return None

            values = search_res["values"]
            underlying_token = underlying_tsym = None

            # Filter futures contracts by instname and tsym pattern
            def _is_not_option(val: dict) -> bool:
                tsym = val.get("tsym", "")
                return "CE" not in tsym.upper() and "PE" not in tsym.upper()

            futures = []
            for val in values:
                if not _is_not_option(val):
                    continue
                if val.get("instname") != instname:
                    continue
                pattern = rf"^{base}\d{{2}}[A-Z]{{3}}(?:\d{{2}}F?|FUT)?$"
                if re.match(pattern, val.get("tsym", "")):
                    futures.append(val)

            if not futures:
                for val in values:
                    if not _is_not_option(val):
                        continue
                    if val.get("instname") == instname:
                        futures.append(val)

            if futures:
                target_item = futures[0]
                underlying_token = target_item.get("token")
                underlying_tsym = target_item.get("tsym")

            if not underlying_token:
                logger.warning("[shoonya] underlying not resolved for %s", base)
                return None

            quote = self._get_quotes(exch, underlying_token)
            if not quote or quote.get("stat") != "Ok":
                logger.warning(
                    "[shoonya] failed quotes for underlying %s", underlying_tsym
                )
                return None

            try:
                underlying_price = float(quote.get("lp", 0))
            except (ValueError, TypeError):
                underlying_price = 0.0

            if underlying_price == 0.0:
                logger.warning(
                    "[shoonya] underlying price is 0 for %s", underlying_tsym
                )
                return None

            # For non-index symbols, return early with just the underlying price
            if not is_index:
                return {
                    "symbol": base,
                    "underlying_price": underlying_price,
                    "expiry": "",
                    "strikes": [],
                    "source": self.name,
                }

            # 2. Fetch option chain via GetOptionChain
            chain_tsym = underlying_tsym
            chain = self._get_option_chain(
                option_exch, chain_tsym, underlying_price, count=30
            )
            if not chain or chain.get("stat") != "Ok" or not chain.get("values"):
                logger.warning("[shoonya] empty option chain for %s", chain_tsym)
                return None

            scrip_list = chain["values"]

            # 3. Parse expiries from option chain
            expiry_dates: dict[str, str] = {}
            now = datetime.now()
            for item in scrip_list:
                exp_str = item.get("expiry")
                if not exp_str:
                    tsym = item.get("tsym", "")
                    m = re.search(r"(\d{2}[A-Z]{3}\d{2})[CP]", tsym)
                    if m:
                        candidate = m.group(1)
                        try:
                            dt = datetime.strptime(candidate, "%d%b%y")
                            if now.year - 5 <= dt.year <= now.year + 2:
                                exp_str = candidate
                                item["expiry_parsed"] = exp_str
                                expiry_dates[exp_str] = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass
                    if not item.get("expiry_parsed"):
                        m = re.search(r"(\d{2}[A-Z]{3})\d+[CP]", tsym)
                        if m:
                            exp_str = m.group(1)
                            item["expiry_parsed"] = exp_str
                            try:
                                exp_month_dt = datetime.strptime(exp_str[2:], "%b")
                                exp_month = exp_month_dt.month
                                year = now.year
                                if exp_month < now.month - 2:
                                    year += 1
                                dt = datetime(year, exp_month, int(exp_str[:2]))
                                expiry_dates[exp_str] = dt.strftime("%Y-%m-%d")
                            except ValueError:
                                pass
                else:
                    item["expiry_parsed"] = exp_str
                    if exp_str not in expiry_dates:
                        try:
                            dt = datetime.strptime(exp_str.title(), "%d-%b-%Y")
                            expiry_dates[exp_str] = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass

            all_expiries = sorted(set(expiry_dates.values()))
            if not all_expiries:
                logger.warning("[shoonya] no valid expiries for %s", base)
                return None

            target_expiry_iso = expiry
            if not target_expiry_iso:
                today = datetime.now(IST).date()
                future = [
                    e
                    for e in all_expiries
                    if datetime.strptime(e, "%Y-%m-%d").date() >= today
                ]
                target_expiry_iso = future[0] if future else all_expiries[0]

            target_expiry_shoonya = next(
                (sh for sh, iso in expiry_dates.items() if iso == target_expiry_iso),
                None,
            )
            if not target_expiry_shoonya:
                logger.warning(
                    "[shoonya] target expiry %s not found", target_expiry_iso
                )
                return None

            target_scrips = [
                s for s in scrip_list if s.get("expiry_parsed") == target_expiry_shoonya
            ]
            if not target_scrips:
                logger.warning(
                    "[shoonya] no contracts for expiry %s", target_expiry_iso
                )
                return None

            # 4. Determine LTP for each contract.
            #    PRIORITY: chain.lp (from GetOptionChain, has correct option premium)
            #          -> GetQuotes.lp (may return underlying price instead of premium)
            #          -> bid/ask mid-price (last resort before synthetic pricing)
            strikes = []
            for item in target_scrips:
                token = item.get("token")
                if not token:
                    continue

                ot = item.get("optt")
                if ot not in ("CE", "PE"):
                    continue

                try:
                    strike = float(item.get("strprc") or 0)
                except (ValueError, TypeError):
                    continue

                if strike <= 0:
                    continue

                # --- Helper: is this price a reasonable option premium? ---
                def _is_reasonable_premium(
                    candidate: float, _ot: str, _strike: float, _underlying: float
                ) -> bool:
                    """Return True if candidate looks like a genuine option premium (not spot leakage)."""
                    if candidate <= 0.0 or _underlying <= 0.0:
                        return False
                    if _ot == "CE":
                        intrinsic = max(0.0, _underlying - _strike)
                    else:
                        intrinsic = max(0.0, _strike - _underlying)
                    # Max reasonable: intrinsic + 15% of underlying for time value
                    max_allowed = intrinsic + 0.15 * _underlying
                    # The premium should never reach the underlying or spot itself
                    return candidate <= max_allowed and candidate < _underlying * 0.99

                # --- Attempt 1: Use lp directly from GetOptionChain item ---
                chain_lp = None
                try:
                    raw = item.get("lp") or item.get("LTP") or item.get("prc")
                    if raw is not None:
                        chain_lp = float(raw)
                except (ValueError, TypeError):
                    chain_lp = None

                ltp_val = None
                source_tag = "synthetic"

                if chain_lp is not None and _is_reasonable_premium(
                    chain_lp, ot, strike, underlying_price
                ):
                    ltp_val = chain_lp
                    source_tag = "chain_lp"
                    logger.debug(
                        "[shoonya] using chain.lp=%.1f for %s %s strike=%.0f",
                        ltp_val,
                        base,
                        ot,
                        strike,
                    )
                else:
                    # --- Attempt 2: GetQuotes with bid/ask fallback ---
                    q = self._get_quotes(option_exch, token)
                    if q and q.get("stat") == "Ok":

                        def _f(key: str, _q: dict = q) -> float:
                            try:
                                return float(_q.get(key) or 0.0)
                            except (ValueError, TypeError):
                                return 0.0

                        q_lp = _f("lp")
                        if _is_reasonable_premium(q_lp, ot, strike, underlying_price):
                            ltp_val = q_lp
                            source_tag = "quotes_lp"
                            logger.debug(
                                "[shoonya] using quotes.lp=%.1f for %s %s strike=%.0f",
                                ltp_val,
                                base,
                                ot,
                                strike,
                            )
                        else:
                            # Corrupt GetQuotes lp (underlying leaked) — try bid/ask
                            bid_val = _f("bp1")
                            ask_val = _f("sp1")
                            mid_val = (
                                (bid_val + ask_val) / 2
                                if bid_val > 0 and ask_val > 0
                                else 0.0
                            )

                            for fallback, label in [
                                (bid_val, "bid"),
                                (ask_val, "ask"),
                                (mid_val, "mid"),
                            ]:
                                if _is_reasonable_premium(
                                    fallback, ot, strike, underlying_price
                                ):
                                    ltp_val = fallback
                                    source_tag = f"quotes_{label}"
                                    logger.info(
                                        "[shoonya] GetQuotes lp corrupt (%.1f); using %s=%.1f for %s %s strike=%.0f",
                                        q_lp,
                                        label,
                                        ltp_val,
                                        base,
                                        ot,
                                        strike,
                                    )
                                    break

                            if ltp_val is None:
                                logger.debug(
                                    "[shoonya] all price sources corrupt for %s %s strike=%.0f "
                                    "(chain_lp=%.1f, quotes_lp=%.1f, bid=%.1f, ask=%.1f) "
                                    "— will use synthetic pricing downstream",
                                    base,
                                    ot,
                                    strike,
                                    chain_lp or 0.0,
                                    _f("lp"),
                                    bid_val,
                                    ask_val,
                                )
                                ltp_val = 0.0  # synthetic pricing downstream
                    else:
                        # GetQuotes failed entirely — use chain_lp even if slightly suspect
                        if chain_lp is not None and chain_lp > 0:
                            ltp_val = chain_lp
                            source_tag = "chain_lp_fallback"
                            logger.warning(
                                "[shoonya] GetQuotes failed; using chain.lp=%.1f for %s %s strike=%.0f "
                                "(may be stale)",
                                ltp_val,
                                base,
                                ot,
                                strike,
                            )
                        else:
                            ltp_val = 0.0
                            source_tag = "none"

                oi_val = 0
                oichg_val = 0
                vol_val = 0
                iv_val = 0.0
                bid_val_final = 0.0
                ask_val_final = 0.0
                if q and q.get("stat") == "Ok":

                    def _f2(key: str, _q: dict = q) -> float:
                        try:
                            return float(_q.get(key) or 0.0)
                        except (ValueError, TypeError):
                            return 0.0

                    def _i2(key: str, _q: dict = q) -> int:
                        try:
                            return int(_q.get(key) or 0)
                        except (ValueError, TypeError):
                            return 0

                    oi_val = _i2("oi")
                    oichg_val = _i2("oichg")
                    vol_val = _i2("v")
                    iv_val = _f2("iv")
                    bid_val_final = _f2("bp1")
                    ask_val_final = _f2("sp1")

                # Do not include strikes with 0 premium (corrupt or untraded data)
                if ltp_val is None or ltp_val <= 0.0:
                    continue

                strikes.append(
                    {
                        "strike": strike,
                        "option_type": ot,
                        "ltp": ltp_val,
                        "ltp_source": source_tag,
                        "oi": oi_val,
                        "oi_change": oichg_val,
                        "volume": vol_val,
                        "iv": iv_val,
                        "bid": bid_val_final,
                        "ask": ask_val_final,
                        "expiry": target_expiry_iso,
                    }
                )

            if not strikes:
                logger.warning("[shoonya] no valid strikes parsed (all had 0 premium or failed) for %s", base)
                return None

            strikes.sort(key=lambda x: (x["strike"], x["option_type"]))

            return {
                "symbol": base,
                "underlying_price": underlying_price,
                "expiry": target_expiry_iso,
                "strikes": strikes,
                "source": self.name,
            }

        except Exception as exc:
            logger.exception(
                "[shoonya] option chain fetch failed for %s: %s", symbol, exc
            )
            return None

    def fetch_quote(self, symbol: str) -> dict | None:
        """Fetch single quote (LTP, OHLC, prev close) for a symbol.

        Returns dict with keys: ltp, open, high, low, close, volume, prev_close, change_pct
        or None on failure.
        """
        if not self.login():
            return None

        base = symbol.upper().split()[0]
        try:
            if base in _INDEX_SPOT_NAMES:
                # Indices: use NSE spot exchange with known tokens
                # The Shoonya API has no prev_close field in GetQuotes response.
                token = _INDEX_NSE_TOKENS.get(base)
                if token:
                    quote = self._get_quotes("NSE", token)
                else:
                    # Fallback: search NSE for the index name
                    scrip_res = self._search_scrip("NSE", base)
                    if not scrip_res or scrip_res.get("stat") != "Ok":
                        return None
                    values = scrip_res.get("values", [])
                    item = None
                    for v in values:
                        if v.get("symname") == base:
                            item = v
                            break
                    if item is None:
                        return None
                    token = item.get("token") or item.get("tok")
                    if not token:
                        return None
                    quote = self._get_quotes("NSE", token)
            else:
                # Equity stocks on NSE
                scrip_res = self._search_scrip("NSE", base)
                if not scrip_res or scrip_res.get("stat") != "Ok":
                    return None
                values = scrip_res.get("values", [])
                if not values:
                    return None

                # BUG-13 FIX: Find exact match by tsym (e.g., PNB-EQ for PNB).
                # Previously fell back to values[0] which could be a different stock
                # (e.g., PNBHOUSING-EQ when searching for PNB). Now returns None
                # if no exact match is found, letting the caller use Dhan/yfinance.
                target_tsym = f"{base}-EQ"
                item = None
                for v in values:
                    if v.get("tsym") == target_tsym or v.get("symname") == base:
                        item = v
                        break
                if item is None:
                    return None  # No exact match; don't risk returning wrong stock
                token = item.get("token") or item.get("tok")
                if not token:
                    return None
                quote = self._get_quotes("NSE", token)

            if not quote or quote.get("stat") != "Ok":
                return None

            ltp = _safe_float(quote.get("lp"))
            if ltp is None:
                logger.debug(
                    "[shoonya] quote for %s has no usable lp field: %s", base, quote
                )
                return None
            # Shoonya GetQuotes does NOT provide prev_close, open/high/low/volume
            # for spot equities. Only lp (ltp) and c (current/close) are reliable.
            # prev_close is set to None so the caller can fallback to Dhan/yfinance.
            prev_close = None
            change_pct = None

            return {
                "ltp": ltp,
                "prev_close": prev_close,
                "change_pct": change_pct,
            }

        except Exception as exc:
            logger.warning("[shoonya] quote fetch failed for %s: %s", symbol, exc)
            return None


    # BUG-04 FIX: These were module-level functions with `self` parameter,
    # causing AttributeError when called as methods. Moved inside the class.
    def get_market_quote(self, exchange: str, token: str) -> dict | None:
        """
        Fetch full snap-quote details including LTP, OHLCV, and Best 5 Bid/Ask Market Depth.

        Parameters:
            exchange (str): Exchange identifier (e.g., "NSE", "NFO", "BFO", "MCX")
            token (str): Instrument numeric token ID (e.g., "2885" for Reliance)

        Returns:
            dict | None: The parsed market data JSON package from Shoonya, or None if failed.
        """
        if not exchange or not token:
            logger.error("[shoonya] Cannot fetch market quote. Missing exchange or token.")
            return None

        payload = {"uid": self.user_id, "exch": exchange.upper(), "token": str(token)}

        logger.debug("[shoonya] Fetching market quote for %s:%s", exchange, token)
        return self._api_call("GetQuotes", payload)

    def get_security_info(self, exchange: str, token: str) -> dict | None:
        """
        Fetch contract specifications, including lot size, tick size, freeze limits.

        Parameters:
            exchange (str): Exchange identifier (e.g., "NSE", "NFO", "BFO", "MCX")
            token (str): Instrument numeric token ID

        Returns:
            dict | None: The parsed security specifications dictionary, or None if failed.
        """
        if not exchange or not token:
            logger.error("[shoonya] Cannot fetch security info. Missing exchange or token.")
            return None

        payload = {"uid": self.user_id, "exch": exchange.upper(), "token": str(token)}

        logger.debug("[shoonya] Fetching security info for %s:%s", exchange, token)
        return self._api_call("GetSecurityInfo", payload)

    def get_index_list(self) -> dict | None:
        """
        Fetch the list of available indices and their tokens.

        Returns:
            dict | None: Response with index list, or None if failed.
        """
        if not self.login():
            return None

        payload = {"uid": self.user_id}

        logger.debug("[shoonya] Fetching index list")
        return self._api_call("GetIndexList", payload)

    def get_historical_candles(
        self, exchange: str, token: str, interval: int, start_time: str, end_time: str
    ) -> dict | None:
        """
        Fetch intraday/historical OHLCV candles (TPSeries / Time Price Series).

        Parameters:
            exchange (str): Exchange identifier
            token (str): Instrument numeric token ID
            interval (int): Candle interval in minutes
            start_time (str): Start timestamp "DD-MM-YYYY HH:MM:SS"
            end_time (str): End timestamp "DD-MM-YYYY HH:MM:SS"

        Returns:
            dict | None: Response with candle data, or None if failed.
        """
        if not exchange or not token:
            logger.error("[shoonya] Cannot fetch TPSeries. Missing exchange or token.")
            return None

        payload = {
            "uid": self.user_id,
            "exch": exchange.upper(),
            "token": str(token),
            "interval": str(interval),
            "start_time": start_time,
            "end_time": end_time,
        }

        logger.debug(
            "[shoonya] Fetching TPSeries for %s:%s interval=%s", exchange, token, interval
        )
        return self._api_call("TPSeries", payload)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _safe_float(val) -> float | None:
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    if val is None:
        return None
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Singleton instance (lazy init)
# ---------------------------------------------------------------------------

# BUG-07 FIX: Renamed from _SHONYAA_INSTANCE (typo) to _SHOONYA_INSTANCE
_SHOONYA_INSTANCE: ShoonyaFetcher | None = None

# Login failure cooldown: skip re-authentication for N seconds after failure
# to avoid repeatedly running the slow Playwright OAuth flow on every request.
_SHOONYA_LOGIN_COOLDOWN = 900  # 15 minutes
_SHOONYA_LOGIN_FAILURE_TS: float = 0.0


def get_shoonya() -> ShoonyaFetcher | None:
    """Return shared ShoonyaFetcher instance (lazy init).

    Returns None if credentials missing, cooldown is active, or the cached
    token is expired (handled transparently by login()).
    """
    global _SHOONYA_INSTANCE

    # If login recently failed, skip Shoonya entirely for the cooldown period
    if (
        _SHOONYA_LOGIN_FAILURE_TS
        and (time.time() - _SHOONYA_LOGIN_FAILURE_TS) < _SHOONYA_LOGIN_COOLDOWN
    ):
        logger.debug(
            "[shoonya] cooldown active (%d s remaining)",
            int(_SHOONYA_LOGIN_COOLDOWN - (time.time() - _SHOONYA_LOGIN_FAILURE_TS)),
        )
        return None

    if _SHOONYA_INSTANCE is None:
        # Check if credentials exist before attempting instantiation
        if not all(
            [
                os.environ.get("SHOONYA_USER_ID"),
                os.environ.get("SHOONYA_PASSWORD"),
                os.environ.get("SHOONYA_TOTP_KEY"),
                os.environ.get("SHOONYA_API_SECRET"),
            ]
        ):
            logger.warning(
                "[shoonya] credentials not configured in .env — Shoonya unavailable"
            )
            return None
        _SHOONYA_INSTANCE = ShoonyaFetcher()
    return _SHOONYA_INSTANCE
