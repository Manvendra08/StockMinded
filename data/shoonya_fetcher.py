"""
Shoonya (Finvasia) Option Chain Fetcher
OAuth 2.0 Authentication (from 1st April 2026) using Playwright for browser automation:

  Step 1: Headless browser navigates OAuth authorize page
  Step 2: Auto-fills uid, password, TOTP and submits
  Step 3: Captures auth_code from redirect URL
  Step 4: POST /NorenWClientAPI/GenAcsTok  (auth_code + SHA256(uid+secret+code))  -> access_token
  Step 5: Bearer access_token for all subsequent API calls
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

import pyotp

STRIKES_AROUND_ATM = int(os.environ.get("STRIKES_AROUND_ATM", "10"))


def _optional_env(key: str, default: str = "") -> str | None:
    val = os.environ.get(key)
    return val if val else default or None

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

_API_BASE = "https://api.shoonya.com/NorenWClientAPI"
_TOKEN_URL = "https://api.shoonya.com/NorenWClientAPI/GenAcsTok"
_SHOONYA_LOGIN_FAILURE_TS = 0.0


def _is_session_expired_response(response: dict | None) -> bool:
    if not response or not isinstance(response, dict):
        return False
    emsg = str(response.get("emsg") or "")
    return response.get("stat") == "Not_Ok" and "Session Expired" in emsg


_INDEX_SPOT_NAMES = {
    "NIFTY": "Nifty 50",
    "BANKNIFTY": "Nifty Bank",
    "FINNIFTY": "Nifty Fin Services",
    "MIDCPNIFTY": "Nifty Midcap 100",
    "SENSEX": "S&P BSE SENSEX",
}

# Shoonya exchange codes for index derivatives.
# NFO = NSE F&O (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY)
# BFO = BSE F&O (SENSEX)
# MCX = MCX Commodities
_EXCHANGE_MAP: dict[str, str] = {
    "NIFTY": "NFO",
    "BANKNIFTY": "NFO",
    "FINNIFTY": "NFO",
    "MIDCPNIFTY": "NFO",
    "SENSEX": "BFO",
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _post_jdata(
    url: str, payload: dict, access_token: str | None = None
) -> dict | None:
    """POST jData= encoded payload, return parsed JSON or None.

    Cleans payload structures to pass authorization details via standard form
    bodies instead of conflicting Bearer headers, resolving the duplicate-auth bug.

    Retries up to 2 times on transient 5xx errors (502/503/504) with exponential
    backoff (1.5s, 3s). Does NOT retry on 4xx (auth errors handled by caller).
    Also retries on DNS resolution failures with IP fallback.
    """
    import socket
    import time as _time

    json_str = json.dumps(payload, separators=(",", ":"))
    # Shoonya's API expects raw JSON (not fully urlencoded) but the web server splits
    # the POST body on '&' and '='. We minimally encode only these characters in the JSON value.
    jdata_val = json_str.replace("&", "%26").replace("=", "%3D")
    body_str = "jData=" + jdata_val
    if access_token:
        body_str += f"&jKey={access_token}"
    body = body_str.encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "Mozilla/5.0",
    }

    _TRANSIENT_5XX = {502, 503, 504}
    _MAX_RETRIES = 1
    _BACKOFF_BASE = 1.5  # seconds
    # Known IPv4 addresses for api.shoonya.com (fallback if DNS fails)
    _SHOONYA_IPS = ["13.202.119.185"]

    for attempt in range(1, _MAX_RETRIES + 2):  # attempts: 1, 2
        req = urllib.request.Request(url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            if e.code in _TRANSIENT_5XX and attempt <= _MAX_RETRIES:
                wait = _BACKOFF_BASE * attempt
                log.warning(
                    "[shoonya] POST %s -> HTTP %s (transient), retry %d/%d in %.1fs",
                    url, e.code, attempt, _MAX_RETRIES, wait,
                )
                _time.sleep(wait)
                continue
            if "Session Expired" in raw:
                log.info("[shoonya] POST %s -> Session Expired (HTTP %s)", url, e.code)
            elif "No Data" in raw:
                log.debug("[shoonya] POST %s -> HTTP %s: %s", url, e.code, raw[:200])
            else:
                log.error("[shoonya] POST %s -> HTTP %s: %s", url, e.code, raw[:200])
            try:
                return json.loads(raw)
            except Exception:
                return None
        except (urllib.error.URLError, socket.gaierror, socket.timeout, TimeoutError, ConnectionError) as exc:
            # Network failure: DNS, timeout, connection reset/refused
            exc_str = str(exc).lower()
            is_dns = isinstance(exc, socket.gaierror) or "getaddrinfo failed" in exc_str or "name or service not known" in exc_str
            is_timeout = isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in exc_str
            is_transient_conn = isinstance(exc, ConnectionError) or "connection reset" in exc_str or "connection refused" in exc_str

            if (is_dns or is_timeout or is_transient_conn) and attempt <= _MAX_RETRIES:
                log.warning("[shoonya] Network error %s for %s (attempt %d/%d): %s", 
                            "DNS" if is_dns else "Timeout/Conn", url, attempt, _MAX_RETRIES + 1, exc)
                
                # Try IP-based fallback on last DNS retry
                if is_dns and attempt == _MAX_RETRIES and _SHOONYA_IPS:
                    for ip in _SHOONYA_IPS:
                        try:
                            ip_url = url.replace("api.shoonya.com", ip)
                            req = urllib.request.Request(ip_url, data=body, headers={**headers, "Host": "api.shoonya.com"})
                            with urllib.request.urlopen(req, timeout=6) as resp:
                                log.info("[shoonya] DNS fallback succeeded via IP %s", ip)
                                return json.loads(resp.read().decode())
                        except Exception as ip_exc:
                            log.debug("[shoonya] IP fallback %s failed: %s", ip, ip_exc)
                            continue
                
                wait = _BACKOFF_BASE * attempt
                _time.sleep(wait)
                continue
            
            log.error("[shoonya] POST %s failed: %s", url, exc)
            return None


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
            try:
                os.chmod(filepath, 0o600)
            except Exception:
                pass
            return True
        except (PermissionError, OSError):
            time.sleep(0.05)
            try:
                if os.path.exists(temp_filepath):
                    os.remove(temp_filepath)
            except Exception:
                pass
    return False


class ShoonyaFetcher:
    name = "shoonya"
    # Shared Shoonya token JSON file in NSEBOT root folder
    _TOKEN_CACHE = "C:/Users/manve/Downloads/NSEBOT/shoonya_shared_token.json"
    _TOKEN_REFRESH_INTERVAL = (
        50400  # seconds (14 hours — session token is valid all day)
    )

    # Token lifetimes are long (24h+ for Shoonya OAuth).  Genuine expiry is
    # handled by the Session Expired retry mechanism in _api_call().

    def __init__(self):
        self.access_token: str | None = None
        self._token_created_at: float = 0.0  # monotonic time when token was obtained

        self.user_id = _optional_env("SHOONYA_USER_ID")
        self.password = _optional_env("SHOONYA_PASSWORD")
        self.totp_key = _optional_env("SHOONYA_TOTP_KEY")
        self.secret_code = _optional_env("SHOONYA_API_SECRET")
        self.vendor_code = _optional_env(
            "SHOONYA_VENDOR_CODE", f"{self.user_id}_U" if self.user_id else ""
        )

        # Cache for resolved MCX futures tokens: symbol -> (token, exchange, expires_at)
        self._futures_token_cache: dict[str, tuple[str, str, float]] = {}

        self._login_lock = threading.RLock()
        # Serialises Shoonya API calls so token rotation from one call's response
        # is saved before the next call reads `access_token`.  Eliminates the race
        # that caused "Session Expired" after ~1-2 calls under concurrent MCX fetches.
        self._api_lock = threading.Lock()
        self._login_failed_until = 0.0

        # Try to load cached token to avoid repeated OAuth browser launches.
        self._load_cached_token()

    def _token_cache_path(self) -> str:
        return self._TOKEN_CACHE

    def _save_token(self) -> None:
        """Persist the current access_token to disk."""
        if not self.access_token:
            return
        data = {
            "susertoken": self.access_token,
            "access_token": self.access_token,
            "userid": self.user_id,
            "last_updated": self._token_created_at,
        }
        if _write_shared_token_file(self._TOKEN_CACHE, data):
            log.debug("[shoonya] token cached to %s", self._TOKEN_CACHE)
        else:
            log.warning("[shoonya] failed to cache token: %s", self._TOKEN_CACHE)

    def _load_cached_token(self) -> None:
        """Load a previously cached access_token from disk."""
        data = _read_shared_token_file(self._TOKEN_CACHE)
        if data and isinstance(data, dict):
            token = data.get("susertoken") or data.get("access_token")
            if token:
                self.access_token = token
                self._token_created_at = data.get("last_updated", time.time())
                log.debug("[shoonya] loaded cached token from %s", self._TOKEN_CACHE)

    def _clear_cached_token(self) -> None:
        """Remove the cached token file (e.g. after expiry)."""
        self.access_token = None
        for _ in range(10):
            try:
                if os.path.exists(self._TOKEN_CACHE):
                    os.remove(self._TOKEN_CACHE)
                    log.debug("[shoonya] cleared cached token")
                break
            except (PermissionError, OSError):
                import time

                time.sleep(0.05)

    def _check_session_quota(self) -> None:
        """Check if the session is approaching its call quota and re-auth if needed.

        Shoonya API sessions have a finite call quota (~2000 calls/session).
        This method checks the approximate usage and triggers a re-auth at 80%.
        """
        # Track approximate call count within the session
        if not hasattr(self, "_session_call_count"):
            self._session_call_count = 0
        if not hasattr(self, "_max_session_calls"):
            self._max_session_calls = 2000

        self._session_call_count += 1

        # Re-auth when approaching the quota limit
        if self._session_call_count >= int(self._max_session_calls * 0.8):
            log.info(
                "[shoonya] Session call count %d approaching quota (%d) — re-authenticating",
                self._session_call_count,
                self._max_session_calls,
            )
            self._clear_cached_token()
            self.login()
            self._session_call_count = 0

    def _verify_token(self) -> bool:
        """
        Trust the cached token — skip network probe.
        The Shoonya API (api.shoonya.com) is frequently unreachable from here
        (WinError 10060), so we rely on the shared NSEBOT token file.
        Only clears the cached token on explicit 'Session Expired' from a real call.
        """
        return True

    # ------------------------------------------------------------------
    # Authentication — Playwright OAuth flow
    # ------------------------------------------------------------------

    def _get_auth_code_playwright(self) -> str | None:
        """
        Headless browser OAuth login:
        1. Navigate to authorize URL → auto-redirects to /investor-entry-level/login
        2. Fill: #lgnusrid, #lgnpwd (raw password), #lgnotp (TOTP), click LOGIN button
        3. Capture auth_code from post-login redirect URL
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            log.error(
                "[shoonya] playwright not installed. Run: .venv\\Scripts\\pip install playwright && .venv\\Scripts\\playwright install chromium"
            )
            return None

        authorize_url = f"https://api.shoonya.com/OAuthlogin/authorize/oauth?client_id={self.vendor_code}"
        log.info("[shoonya] Launching headless browser for OAuth login...")
        auth_code: str | None = None

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()
                captured_urls: list[str] = []

                # Block only images and fonts to save bandwidth and speed up page load
                def handle_route(route):
                    req_type = route.request.resource_type
                    if req_type in ("image", "font"):
                        route.abort()
                    else:
                        route.continue_()

                page.route("**/*", handle_route)

                # Listen to requests and responses to capture code= even on failed page navigations (e.g. net::ERR_NAME_NOT_RESOLVED)
                page.on(
                    "request",
                    lambda r: captured_urls.append(r.url) if "code=" in r.url else None,
                )
                page.on(
                    "response",
                    lambda r: captured_urls.append(r.url) if "code=" in r.url else None,
                )

                # Step 1: Navigate — will redirect to /investor-entry-level/login
                page.goto(authorize_url, wait_until="commit", timeout=15000)
                log.debug("[shoonya] Landed on: %s", page.url)

                # Wait for React to render the login form (allow up to 15s for 9.5MB JS load)
                page.wait_for_selector("#lgnusrid", state="visible", timeout=15000)

                # Generate fresh TOTP right before filling (avoids expiry during navigation)
                totp = pyotp.TOTP(self.totp_key).now()

                # Step 2: Fill credentials using confirmed field selectors
                page.locator("#lgnusrid").fill(self.user_id)
                page.locator("#lgnpwd").fill(self.password)
                page.locator("#lgnotp").fill(totp)

                # Step 3: Click Login (wrap in try-except in case the redirect target domain does not resolve)
                try:
                    page.locator("button:has-text('LOGIN')").click()
                    # Step 4: Wait for redirect containing auth_code
                    page.wait_for_url("*code=*", timeout=10000)
                except Exception as click_err:
                    log.debug(
                        "[shoonya] Browser encountered navigation or redirect error: %s",
                        click_err,
                    )

                final_url = page.url
                body_text = ""
                try:
                    body_text = page.locator("body").inner_text()
                except Exception:
                    pass

                browser.close()

                if "blocked" in body_text.lower() or "unsuccessful login attempts" in body_text.lower():
                    log.error(
                        "[shoonya] ACCOUNT BLOCKED: Shoonya user '%s' is blocked by broker due to unsuccessful login attempts. Unblock via PAN + DOB on Shoonya portal: https://api.shoonya.com/OAuthlogin/investor-entry-level/login?api_key=%s",
                        self.user_id,
                        self.vendor_code,
                    )
                    self._login_failed_until = time.time() + 900
                    return None

                log.debug("[shoonya] Post-login URL: %s", final_url)

                # Extract auth_code from URL candidates (both final URL and any intermediate requests)
                for candidate in [final_url] + captured_urls:
                    m = re.search(r"[?&]code=([A-Za-z0-9_\-]+)", candidate)
                    if m:
                        auth_code = m.group(1)
                        log.info("[shoonya] auth_code captured successfully")
                        break

                if not auth_code:
                    log.error(
                        "[shoonya] auth_code not found. Final URL: %s, Captured URLs: %s",
                        final_url,
                        captured_urls,
                    )

        except Exception as exc:
            log.exception("[shoonya] Playwright OAuth login failed: %s", exc)

        return auth_code

    def _exchange_for_token(self, auth_code: str) -> str | None:
        """Exchange auth_code for access_token via GenAcsTok."""
        checksum = _sha256(self.vendor_code + self.secret_code + auth_code)
        payload = {
            "uid": self.user_id,
            "code": auth_code,
            "checksum": checksum,
            "source": "API",
            "imei": "127.0.0.1",
        }
        res = _post_jdata(_TOKEN_URL, payload)
        if not res:
            return None
        if res.get("stat") != "Ok":
            emsg = str(res.get("emsg", ""))
            log.error("[shoonya] GenAcsTok failed: %s", res)
            if "INVALID_IP" in emsg.upper():
                log.warning("[shoonya] INVALID_IP error from Shoonya API (registered API IP does not match current network IP). Pausing OAuth login for 10 minutes.")
                self._login_failed_until = time.time() + 600
            else:
                self._login_failed_until = time.time() + 60
            return None
        # Prefer susertoken (legacy session token for jKey auth) over access_token
        # (OAuth Bearer token). The Noren REST API endpoints (SearchScrip, GetQuotes,
        # GetOptionChain) authenticate via jKey=susertoken in the POST body.
        token = res.get("susertoken") or res.get("access_token")
        if not token:
            log.error("[shoonya] GenAcsTok: no token in response: %s", res)
        else:
            log.debug(
                "[shoonya] GenAcsTok response keys: %s, token length: %d",
                list(res.keys()),
                len(token),
            )
        return token

    def login(self) -> bool:
        with self._login_lock:
            # Load from shared cache first to see if another process updated it
            self._load_cached_token()
            # If we have a cached token, trust it and let _api_call() handle expiry.
            if self.access_token:
                log.debug("[shoonya] using cached token — skipping OAuth")
                return True

            if time.time() < getattr(self, "_login_failed_until", 0.0):
                rem_sec = int(self._login_failed_until - time.time())
                log.debug("[shoonya] Login paused due to active failure cooldown (%ds remaining)", rem_sec)
                return False

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
                log.warning("[shoonya] missing credentials: %s — skipping", missing)
                return False

            try:
                auth_code = self._get_auth_code_playwright()
                if not auth_code:
                    log.error("[shoonya] Failed to obtain auth_code")
                    return False
                log.info("[shoonya] Exchanging auth_code for access_token...")
                token = self._exchange_for_token(auth_code)
                if not token:
                    return False
                self.access_token = token
                self._token_created_at = time.time()
                self._save_token()
                log.info("[shoonya] OAuth login successful")
                return True
            except Exception as exc:
                log.exception("[shoonya] login exception: %s", exc)
                return False

    # ------------------------------------------------------------------
    # API helpers (Bearer auth)
    # ------------------------------------------------------------------

    def _api_call(
        self, endpoint: str, payload: dict, retry_on_expiry: bool = True
    ) -> dict | None:
        # Acquire the serialisation lock so concurrent callers (e.g. MCX
        # ThreadPoolExecutor) never race on token rotation.  Each call
        # completes — including token save — before the next starts.
        with self._api_lock:
            return self._api_call_impl(endpoint, payload, retry_on_expiry)

    def _api_call_impl(
        self, endpoint: str, payload: dict, retry_on_expiry: bool = True
    ) -> dict | None:
        payload.setdefault("uid", self.user_id)

        # Reload the token from the shared JSON file to ensure we use the latest rotated token
        self._load_cached_token()

        # If token is cleared/None, login first to avoid guaranteed 401 response
        if not self.access_token:
            if not self.login():
                return None

        log.debug(
            "[shoonya] API call to %s with uid=%s, token_len=%d",
            endpoint,
            self.user_id,
            len(self.access_token or ""),
        )
        res = _post_jdata(f"{_API_BASE}/{endpoint}", payload, self.access_token)

        if res and isinstance(res, dict):
            log.debug("[shoonya] %s response keys: %s", endpoint, list(res.keys()))

        # ── Extract fresh token from response ──────────────────────────────────
        # Shoonya rotates the session token on every successful API response.
        # If we don't extract the new token, the next call will use a stale
        # token and get "Session Expired (HTTP 401)".
        if res and isinstance(res, dict) and res.get("stat") == "Ok":
            fresh_token = (
                res.get("susertoken")
                or res.get("access_token")
                or res.get("uname")  # Some Shoonya endpoints return token as uname
            )
            if fresh_token and fresh_token != self.access_token:
                log.debug(
                    "[shoonya] Token rotated on %s (len: %d → %d)",
                    endpoint,
                    len(self.access_token or ""),
                    len(fresh_token),
                )
                self.access_token = fresh_token
                self._token_created_at = time.time()
                self._save_token()

        is_expired = _is_session_expired_response(res)

        if is_expired:
            log.warning(
                "[shoonya] Session expired on endpoint %s. Reloading shared token...",
                endpoint,
            )
            old_token = self.access_token
            self._load_cached_token()

            # If the shared token was updated by another process, retry once with the new token
            if self.access_token and self.access_token != old_token:
                log.info(
                    "[shoonya] Retrying API call to %s with newly loaded shared token...",
                    endpoint,
                )
                res = _post_jdata(f"{_API_BASE}/{endpoint}", payload, self.access_token)
                if res and isinstance(res, dict):
                    # Handle successful retry token rotation
                    if res.get("stat") == "Ok":
                        fresh_token = res.get("susertoken") or res.get("access_token")
                        if fresh_token and fresh_token != self.access_token:
                            self.access_token = fresh_token
                            self._token_created_at = time.time()
                            self._save_token()
                        return res
                    is_expired = _is_session_expired_response(res)

            if is_expired:
                log.warning(
                    "[shoonya] Token still expired. Clearing and performing login..."
                )
                self._clear_cached_token()
                if retry_on_expiry:
                    log.info(
                        "[shoonya] Retrying API call to %s after re-authentication...",
                        endpoint,
                    )
                    login_ok = self.login(force=True)
                    if not login_ok:
                        global _SHOONYA_LOGIN_FAILURE_TS
                        _SHOONYA_LOGIN_FAILURE_TS = time.time()
                    if login_ok:
                        # ═══════════════════════════════════════════════════════
                        # FIX: Extract rotated token from retry response too.
                        # Without this, the new token from the retry call would be
                        # consumed but not saved, making the very next call fail
                        # with "Session Expired".
                        # ═══════════════════════════════════════════════════════
                        res = _post_jdata(
                            f"{_API_BASE}/{endpoint}", payload, self.access_token
                        )
                        if res and isinstance(res, dict) and res.get("stat") == "Ok":
                            fresh_token = res.get("susertoken") or res.get(
                                "access_token"
                            )
                            if fresh_token and fresh_token != self.access_token:
                                self.access_token = fresh_token
                                self._token_created_at = time.time()
                                self._save_token()
                        return res
        return res

    def _search_scrip(self, exchange: str, searchtext: str) -> dict | None:
        # Shoonya SearchScrip expects raw search text (not URL-encoded).
        # quote_plus() would corrupt multi-word queries like "SENSEX FUT" -> "SENSEX+FUT".
        # The JSON payload is already HTTP-encoded by _post_jdata.
        return self._api_call(
            "SearchScrip",
            {"exch": exchange, "stext": searchtext},
            retry_on_expiry=True,
        )

    def _get_quotes(self, exchange: str, token: str) -> dict | None:
        return self._api_call("GetQuotes", {"exch": exchange, "token": token})

    def _get_option_chain(
        self, exchange: str, tsym: str, strikeprice: float, count: int = STRIKES_AROUND_ATM
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

    # ------------------------------------------------------------------
    # OHLC candle data — GetTimePriceSeries
    # ------------------------------------------------------------------

    def resolve_futures_token(self, base_symbol: str) -> tuple[str, str] | None:
        """
        Resolve the near-month futures token for an MCX commodity.
        Returns (exchange, token) or None on failure.
        Results are cached for 6 hours to minimise SearchScrip calls.
        """

        entry = self._futures_token_cache.get(base_symbol)
        if entry:
            token, exchange, expires_at = entry
            if time.time() < expires_at:
                return (exchange, token)

        if not self.login():
            return None

        try:
            search_res = self._search_scrip("MCX", base_symbol)
            if (
                not search_res
                or search_res.get("stat") != "Ok"
                or not search_res.get("values")
            ):
                log.warning(
                    "[shoonya] resolve_futures_token: SearchScrip failed for %s",
                    base_symbol,
                )
                return None

            futures = []
            for val in search_res["values"]:
                tsym = val.get("tsym", "")
                if "CE" in tsym.upper() or "PE" in tsym.upper():
                    continue
                if val.get("instname") != "FUTCOM":
                    continue
                pattern = (
                    rf"^{re.escape(base_symbol)}\d{{2}}[A-Z]{{3}}(?:\d{{2}}F?|FUT)?$"
                )
                if re.match(pattern, tsym):
                    futures.append(val)

            if not futures and search_res.get("values"):
                # Fallback: any FUTCOM for this symbol
                futures = [
                    v
                    for v in search_res["values"]
                    if v.get("instname") == "FUTCOM"
                    and "CE" not in v.get("tsym", "").upper()
                    and "PE" not in v.get("tsym", "").upper()
                ]

            if not futures:
                log.warning(
                    "[shoonya] resolve_futures_token: no FUTCOM contracts found for %s",
                    base_symbol,
                )
                return None

            target = futures[0]
            token = target.get("token")
            if not token:
                return None

            self._futures_token_cache[base_symbol] = (
                token,
                "MCX",
                time.time() + 21600,
            )
            log.info(
                "[shoonya] resolved futures token for %s: %s (tsym=%s)",
                base_symbol,
                token,
                target.get("tsym"),
            )
            return ("MCX", token)

        except Exception as exc:
            log.warning(
                "[shoonya] resolve_futures_token failed for %s: %s", base_symbol, exc
            )
            return None

    def fetch_candles(
        self,
        exchange: str,
        token: str,
        interval_minutes: int,
        start_epoch: int,
        end_epoch: int,
    ) -> list[dict] | None:
        """
        Fetch OHLC candle data via GetTimePriceSeries.
        Returns a list of bar dicts with keys: Open, High, Low, Close, _ts (epoch seconds).
        Returns None if the API call fails or returns no data.

        Includes retry logic for temporary network/gateway errors (HTTP 502, 503, 504, timeouts)
        and session expiry.
        """
        import time

        max_attempts = 3
        delay = 1.5

        payload = {
            "uid": self.user_id,
            "exch": exchange,
            "token": str(token),
            "st": str(start_epoch),
            "et": str(end_epoch),
            "intrv": str(interval_minutes),
        }
        url = "https://api.shoonya.com/NorenWClientTP/TPSeries"

        for attempt in range(1, max_attempts + 1):
            # Acquire the same serialisation lock as _api_call() so token
            # state is never mutated by _api_call while fetch_candles runs,
            # and vice versa.
            with self._api_lock:
                if not self.login():
                    log.warning("[shoonya] Candle fetch aborted: Login failed")
                    return None

                # ── Proactively refresh if token nearing expiry ──────────────────
                if (
                    self._token_created_at > 0
                    and time.time() - self._token_created_at
                    > self._TOKEN_REFRESH_INTERVAL
                ):
                    log.info(
                        "[shoonya] Token age %.0fs exceeds refresh interval — re-authenticating before candle fetch",
                        time.time() - self._token_created_at,
                    )
                    self._clear_cached_token()
                    self.login()

                # Proactively re-auth if approaching the session call quota
                self._check_session_quota()

                # ── Use jKey-only auth (no Bearer header) ──────────────────
                # Dual auth (jKey + Bearer) was found to double the session
                # quota burn rate, causing premature Session Expired errors
                # after ~45-50 effective API calls.  See _post_jdata() docstring.
                body_str = "jData=" + json.dumps(payload, separators=(",", ":"))
                body_str += f"&jKey={self.access_token}"
                body = body_str.encode()
                headers: dict[str, str] = {
                    "Content-Type": "application/x-www-form-urlencoded"
                }
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers=headers,
                )
                try:
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        raw = json.loads(resp.read().decode())
                except urllib.error.HTTPError as e:
                    raw_body = ""
                    try:
                        raw_body = e.read().decode()
                    except Exception:
                        pass

                    log.warning(
                        "[shoonya] chart-candles HTTP %s for %s token=%s (attempt %d/%d): %s",
                        e.code,
                        exchange,
                        token,
                        attempt,
                        max_attempts,
                        raw_body[:100].strip(),
                    )

                    # Retry on typical gateway or rate-limiting/server errors
                    if e.code in (502, 503, 504, 429) and attempt < max_attempts:
                        # Exit lock so the sleep + retry don't block other callers
                        pass  # will sleep and continue below
                    else:
                        return None
                except Exception as exc:
                    log.warning(
                        "[shoonya] chart-candles failed %s token=%s (attempt %d/%d): %s",
                        exchange,
                        token,
                        attempt,
                        max_attempts,
                        exc,
                    )
                    if attempt < max_attempts:
                        pass  # will sleep and continue below
                    else:
                        return None
                else:
                    # ── Successful HTTP response — process result ────────────

                    # Handle auth-failure response (single dict with stat != Ok)
                    if isinstance(raw, dict):
                        emsg = raw.get("emsg", str(raw))
                        if (
                            "session" in emsg.lower()
                            or "token" in emsg.lower()
                            or "invalid" in emsg.lower()
                        ):
                            log.info(
                                "[shoonya] chart-candles: session expired (attempt %d/%d) — clearing token cache",
                                attempt,
                                max_attempts,
                            )
                            self._clear_cached_token()
                            if attempt < max_attempts:
                                # Re-authenticate on next loop iteration
                                time.sleep(0.5)
                                continue
                        else:
                            log.warning(
                                "[shoonya] chart-candles unexpected response: %s", emsg
                            )
                        return None

                    if not isinstance(raw, list):
                        log.warning(
                            "[shoonya] chart-candles: unexpected response type %s",
                            type(raw),
                        )
                        return None

                    bars: list[dict] = []
                    for item in raw:
                        try:
                            # Shoonya returns: ssboe (bar start epoch), into/inth/intl/intc (OHLC)
                            # Some API versions use 'o'/'h'/'l'/'c' — handle both.
                            ts = float(item.get("ssboe") or item.get("ts") or 0)
                            o = float(item.get("into") or item.get("o") or 0)
                            h = float(item.get("inth") or item.get("h") or 0)
                            l = float(item.get("intl") or item.get("l") or 0)
                            c = float(item.get("intc") or item.get("c") or 0)
                            if ts > 0 and all(x > 0 for x in (o, h, l, c)):
                                bars.append(
                                    {
                                        "Open": o,
                                        "High": h,
                                        "Low": l,
                                        "Close": c,
                                        "_ts": ts,
                                    }
                                )
                        except (ValueError, KeyError, TypeError):
                            continue

                    if not bars:
                        log.debug(
                            "[shoonya] GetTimePriceSeries: zero valid bars for %s token=%s",
                            exchange,
                            token,
                        )
                        return None

                    # ═══════════════════════════════════════════════════════════
                    # Token rotation: TPSeries returns a list (not a dict with
                    # susertoken), but the call still counts toward the session
                    # quota.  Saving the current token at least ensures the
                    # in-memory / on-disk state matches, preventing drift.
                    # ═══════════════════════════════════════════════════════════
                    self._token_created_at = time.time()
                    self._save_token()
                    self._increment_and_save_call_count()

                    log.debug(
                        "[shoonya] GetTimePriceSeries: %d bars for %s token=%s",
                        len(bars),
                        exchange,
                        token,
                    )
                    log.info(
                        "[shoonya] chart-candles | %s token=%s | %d bars fetched",
                        exchange,
                        token,
                        len(bars),
                    )
                    return bars

            # If we reach here, an HTTP/gateway error occurred and we should
            # retry — sleep outside the lock so concurrent callers are not blocked.
            time.sleep(delay)
            delay *= 2

        return None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def _ensure_mcx_symbols(self) -> str | None:
        """Ensure MCX symbols file is present and up to date."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        dest_dir = os.path.join(project_root, "scratch", "MCX_symbols")
        dest_file = os.path.join(dest_dir, "MCX_symbols.txt")

        import time

        if os.path.exists(dest_file):
            mtime = os.path.getmtime(dest_file)
            if (time.time() - mtime) < 86400:  # 24 hours
                return dest_file

        try:
            os.makedirs(dest_dir, exist_ok=True)
            url = "https://api.shoonya.com/MCX_symbols.txt.zip"
            zip_path = os.path.join(dest_dir, "MCX_symbols.txt.zip")
            log.info("[shoonya] Downloading MCX symbols master from %s...", url)

            import urllib.request

            urllib.request.urlretrieve(url, zip_path)

            import zipfile

            log.info("[shoonya] Extracting MCX symbols...")
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                zip_ref.extractall(dest_dir)

            if os.path.exists(zip_path):
                os.remove(zip_path)

            log.info("[shoonya] MCX symbols updated successfully at %s", dest_file)
            return dest_file
        except Exception as e:
            log.exception("[shoonya] Failed to download/extract MCX symbols: %s", e)
            if os.path.exists(dest_file):
                log.warning("[shoonya] Using existing MCX symbols file")
                return dest_file
            return None

    def fetch_option_chain(self, symbol: str, expiry: str | None = None) -> dict | None:
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
                # SENSEX derivatives trade on BFO (BSE F&O).
                # SearchScrip on BFO needs "SENSEX FUT" as search text to
                # find regular SENSEX futures (avoids SENSEX50 mini contracts).
                # Futures tsym format: SENSEX26JUNFUT (NFO uses NIFTY25JUN26F).
                if base == "SENSEX":
                    option_exch = "BFO"
                    exch = "BFO"
                    search_text = "SENSEX FUT"
            else:
                exch = "MCX"
                search_text = base
                instname = "FUTCOM"
                option_exch = "MCX"

            # 1. Resolve underlying futures contract
            search_res = self._search_scrip(exch, search_text)
            if (
                not search_res
                or search_res.get("stat") != "Ok"
                or not search_res.get("values")
            ):
                log.warning("[shoonya] could not search scrip for %s", search_text)
                return None

            values = search_res["values"]
            underlying_token = underlying_tsym = None

            # Filter futures contracts
            def _is_not_option(val: dict) -> bool:
                tsym = val.get("tsym", "")
                return "CE" not in tsym.upper() and "PE" not in tsym.upper()

            futures = []
            for val in values:
                if not _is_not_option(val):
                    continue
                if val.get("instname") != instname:
                    continue
                # NFO format: NIFTY25JUN26F  (base + ddMMMyy + optional F)
                # BFO format: SENSEX26JUNFUT  (base + ddMMM + FUT)
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
                # If nearest expires today (e.g. 25JUN26), select next one if available
                if len(futures) > 1 and "25JUN26" in target_item.get("tsym", ""):
                    target_item = futures[1]
                underlying_token = target_item.get("token")
                underlying_tsym = target_item.get("tsym")

            if not underlying_token:
                log.warning("[shoonya] underlying not resolved for %s", base)
                return None

            quote = self._get_quotes(exch, underlying_token)
            if not quote or quote.get("stat") != "Ok":
                log.warning(
                    "[shoonya] failed quotes for underlying %s", underlying_tsym
                )
                return None

            underlying_price = 0.0
            # Try last traded price ('lp') first, then fall back to close price ('c')
            for key in ("lp", "c"):
                try:
                    val = quote.get(key)
                    if val is not None:
                        underlying_price = float(val)
                        if underlying_price > 0.0:
                            if key == "c":
                                log.warning(
                                    "[shoonya] LTP (lp) is 0/unavailable for %s, falling back to close price (c): %s",
                                    underlying_tsym,
                                    underlying_price,
                                )
                            break
                except (ValueError, TypeError):
                    pass

            if underlying_price == 0.0:
                log.warning("[shoonya] underlying price is 0 for %s", underlying_tsym)
                return None

            # Handle Non-index (MCX Commodities) using local symbol file fallback
            if not is_index:
                symbol_file = self._ensure_mcx_symbols()
                if not symbol_file:
                    log.warning("[shoonya] MCX symbols file not available")
                    return None

                import csv

                all_options = []
                with open(symbol_file, "r") as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if (
                            row.get("Instrument") == "OPTFUT"
                            and row.get("Symbol") == base
                        ):
                            all_options.append(row)

                if not all_options:
                    log.warning(
                        "[shoonya] No option contracts found in master for %s", base
                    )
                    return None

                # Unique expiries sorted chronologically
                try:
                    expiries = sorted(
                        list(set(row["Expiry"] for row in all_options)),
                        key=lambda x: datetime.strptime(x, "%d-%b-%Y"),
                    )
                except Exception as e:
                    log.warning("[shoonya] Failed to parse expiries: %s", e)
                    return None

                if not expiries:
                    log.warning("[shoonya] No expiries found for %s", base)
                    return None

                target_expiry_shoonya = None
                target_expiry_iso = None

                if expiry:
                    for exp in expiries:
                        iso = datetime.strptime(exp, "%d-%b-%Y").strftime("%Y-%m-%d")
                        if iso == expiry:
                            target_expiry_shoonya = exp
                            target_expiry_iso = expiry
                            break
                    if not target_expiry_shoonya:
                        log.warning(
                            "[shoonya] Target expiry %s not found in MCX master for %s",
                            expiry,
                            base,
                        )
                        return None
                else:
                    target_expiry_shoonya = expiries[0]
                    target_expiry_iso = datetime.strptime(
                        target_expiry_shoonya, "%d-%b-%Y"
                    ).strftime("%Y-%m-%d")

                expiry_options = [
                    row for row in all_options if row["Expiry"] == target_expiry_shoonya
                ]
                if not expiry_options:
                    log.warning(
                        "[shoonya] No contracts found for expiry %s",
                        target_expiry_shoonya,
                    )
                    return None

                for row in expiry_options:
                    try:
                        row["strike_val"] = float(row["StrikePrice"])
                    except (ValueError, TypeError):
                        row["strike_val"] = 0.0

                expiry_options = [
                    row for row in expiry_options if row["strike_val"] > 0
                ]
                if not expiry_options:
                    log.warning(
                        "[shoonya] No valid strikes parsed for %s options", base
                    )
                    return None

                unique_strikes = sorted(
                    list(set(row["strike_val"] for row in expiry_options))
                )
                atm_strike = min(
                    unique_strikes, key=lambda s: abs(s - underlying_price)
                )
                atm_idx = unique_strikes.index(atm_strike)

                start_idx = max(0, atm_idx - STRIKES_AROUND_ATM)
                end_idx = min(len(unique_strikes), atm_idx + STRIKES_AROUND_ATM + 1)
                selected_strikes = set(unique_strikes[start_idx:end_idx])

                contracts_to_fetch = [
                    row
                    for row in expiry_options
                    if row["strike_val"] in selected_strikes
                ]

                import time
                from concurrent.futures import ThreadPoolExecutor

                quotes = {}

                def fetch_quote(row):
                    token = row["Token"]
                    q = self._get_quotes(exch, token)
                    if q and q.get("stat") == "Ok":
                        return token, q
                    return token, None

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = []
                    for row in contracts_to_fetch:
                        futures.append(executor.submit(fetch_quote, row))
                        time.sleep(
                            0.12
                        )  # Pace to stay strictly under the 10/sec rate limit

                    for fut in futures:
                        try:
                            token, q = fut.result()
                            if q:
                                quotes[token] = q
                        except Exception:
                            pass

                strikes = []
                for row in contracts_to_fetch:
                    token = row["Token"]
                    q = quotes.get(token)
                    if not q:
                        continue

                    ot = row["OptionType"]
                    if ot not in ("CE", "PE"):
                        continue

                    def _f(key: str, _q: dict = q) -> float:
                        try:
                            return float(_q.get(key) or 0.0)
                        except (ValueError, TypeError):
                            return 0.0

                    def _i(key: str, _q: dict = q) -> int:
                        try:
                            return int(_q.get(key) or 0)
                        except (ValueError, TypeError):
                            return 0

                    strikes.append(
                        {
                            "strike": row["strike_val"],
                            "option_type": ot,
                            "ltp": _f("lp"),
                            "oi": _i("oi"),
                            "oi_change": _i("oichg"),
                            "volume": _i("v"),
                            "iv": _f("iv"),
                            "bid": _f("bp1"),
                            "ask": _f("sp1"),
                        }
                    )

                if not strikes:
                    log.warning("[shoonya] No quotes fetched for %s options", base)
                    return None

                return {
                    "symbol": base,
                    "underlying_price": underlying_price,
                    "expiry": target_expiry_iso,
                    "strikes": strikes,
                    "source": self.name,
                    "all_expiries": [
                        datetime.strptime(e, "%d-%b-%Y").strftime("%Y-%m-%d")
                        for e in expiries
                    ],
                }

            # Handle standard NSE/BSE indices using GetOptionChain
            chain_tsym = underlying_tsym

            if option_exch == "BFO":
                # SENSEX/BANKEX weekly options chain in Shoonya is not grouped under the monthly futures contract.
                # We need to find an active weekly option contract symbol (e.g. SENSEX2670966500CE) and use it as chain_tsym.
                try:
                    now_ist = datetime.now(IST)
                    today_ist = now_ist.date()
                    # Find first Thursday >= today
                    cand_dt = today_ist
                    while cand_dt.weekday() != 3:  # 3 is Thursday
                        cand_dt += timedelta(days=1)
                    
                    # If today is Thursday and past 15:30 IST, roll to next Thursday
                    from datetime import time as dt_time
                    if cand_dt == today_ist and now_ist.time() > dt_time(15, 30):
                        cand_dt += timedelta(days=7)
                    
                    # Check next 3 Thursdays
                    candidates = [cand_dt, cand_dt + timedelta(days=7), cand_dt + timedelta(days=14)]
                    resolved_weekly_tsym = None
                    for c_date in candidates:
                        yy = c_date.strftime("%y")
                        m_val = c_date.month
                        m_str = "O" if m_val == 10 else ("N" if m_val == 11 else ("D" if m_val == 12 else str(m_val)))
                        dd = c_date.strftime("%d")
                        prefix = f"{base}{yy}{m_str}{dd}"
                        
                        log.info("[shoonya] Searching BFO for weekly prefix %s to resolve option chain...", prefix)
                        res = self._search_scrip("BFO", prefix)
                        if res and res.get("stat") == "Ok" and res.get("values"):
                            for val in res["values"]:
                                tsym_opt = val.get("tsym", "")
                                if "CE" in tsym_opt or "PE" in tsym_opt:
                                    resolved_weekly_tsym = tsym_opt
                                    log.info("[shoonya] Resolved BFO weekly option symbol: %s", resolved_weekly_tsym)
                                    break
                        if resolved_weekly_tsym:
                            break
                    
                    if resolved_weekly_tsym:
                        chain_tsym = resolved_weekly_tsym
                except Exception as ex_bfo:
                    log.warning("[shoonya] failed to resolve BFO weekly option symbol: %s. Using default: %s", ex_bfo, chain_tsym)

            chain = self._get_option_chain(
                option_exch, chain_tsym, underlying_price, count=STRIKES_AROUND_ATM
            )
            if not chain or chain.get("stat") != "Ok" or not chain.get("values"):
                log.warning("[shoonya] empty option chain for %s", chain_tsym)
                return None

            scrip_list = chain["values"]

            expiry_dates: dict[str, str] = {}
            now = datetime.now()
            for item in scrip_list:
                exp_str = item.get("expiry")
                if not exp_str:
                    tsym = item.get("tsym", "")
                    # Try NFO format: NIFTY25JUN2677100CE → captures "25JUN26"
                    m = re.search(r"(\d{2}[A-Z]{3}\d{2})[CP]", tsym)
                    if m:
                        candidate = m.group(1)
                        try:
                            dt = datetime.strptime(candidate, "%d%b%y")
                            # Sanity check: year should be within ~5 years of current
                            if now.year - 5 <= dt.year <= now.year + 2:
                                exp_str = candidate
                                item["expiry_parsed"] = exp_str
                                expiry_dates[exp_str] = dt.strftime("%Y-%m-%d")
                        except ValueError:
                            pass
                    # If NFO format failed, try BFO monthly format:
                    # SENSEX26JUN77100CE → captures "26JUN" (no year digits)
                    if not item.get("expiry_parsed"):
                        m = re.search(r"(\d{2}[A-Z]{3})\d+[CP]", tsym)
                        if m:
                            exp_str = m.group(1)
                            item["expiry_parsed"] = exp_str
                            try:
                                exp_month = datetime.strptime(exp_str[2:], "%b").month
                                year = now.year
                                # Infer year: if month is Dec and current is Jan, use prev year
                                # If month is Jan and current is Dec, use next year
                                if exp_month < now.month - 2:
                                    year += 1
                                dt = datetime(year, exp_month, int(exp_str[:2]))
                                expiry_dates[exp_str] = dt.strftime("%Y-%m-%d")
                            except ValueError:
                                pass

                    # If NFO and BFO monthly failed, try BFO weekly format:
                    # SENSEX2670266500CE → captures "26702" (YY + M + DD)
                    if not item.get("expiry_parsed"):
                        m = re.search(rf"^{base}(\d{{2}})([1-9OND])(\d{{2}})\d+(?:CE|PE)$", tsym)
                        if m:
                            yy_str = m.group(1)
                            m_str = m.group(2)
                            dd_str = m.group(3)
                            month_map = {"O": 10, "N": 11, "D": 12}
                            try:
                                year = 2000 + int(yy_str)
                                month = month_map.get(m_str) or int(m_str)
                                day = int(dd_str)
                                dt = datetime(year, month, day)
                                iso_date = dt.strftime("%Y-%m-%d")
                                
                                exp_str = f"{dd_str}{dt.strftime('%b').upper()}{yy_str}"  # e.g., "02JUL26"
                                item["expiry_parsed"] = exp_str
                                expiry_dates[exp_str] = iso_date
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

            all_expiries = sorted(expiry_dates.values())
            if not all_expiries:
                log.warning("[shoonya] no valid expiries for %s", base)
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
                log.warning("[shoonya] target expiry %s not found", target_expiry_iso)
                return None

            target_scrips = [
                s for s in scrip_list if s.get("expiry_parsed") == target_expiry_shoonya
            ]
            if not target_scrips:
                log.warning("[shoonya] no contracts for expiry %s", target_expiry_iso)
                return None

            # ── NSE index: read LTP/OI/IV directly from GetOptionChain items ──────
            # Shoonya's GetOptionChain response already contains lp, oi, oichg, v, iv,
            # bp1, sp1 per item.  Previously this code made one _get_quotes() call per
            # strike (42 calls per symbol), which exhausted the ~45-call session quota
            # on a single NSE index fetch.  Now we extract these fields directly from the
            # chain response, eliminating 42 redundant API calls per NSE symbol.
            #
            # If any item is missing critical fields, we fall back to _get_quotes()
            # for that specific strike only (not all 42).
            strikes = []
            for item in target_scrips:
                ot = item.get("optt")
                if ot not in ("CE", "PE"):
                    continue

                try:
                    strike = float(item.get("strprc") or 0)
                except (ValueError, TypeError):
                    continue

                # Determine data source: GetOptionChain item first, fall back to GetQuotes
                # if LTP or OI is missing.
                data = item
                ltp_raw = item.get("lp")
                oi_raw = item.get("oi")
                if ltp_raw is None or oi_raw is None:
                    token = item.get("token")
                    if token:
                        q = self._get_quotes(option_exch, token)
                        if q and q.get("stat") == "Ok":
                            data = q

                def _fq(key, _src=data):
                    try:
                        v = _src.get(key)
                        return float(v) if v is not None else None
                    except (ValueError, TypeError):
                        return None

                def _iq(key, _src=data):
                    try:
                        v = _src.get(key)
                        return int(v) if v is not None else None
                    except (ValueError, TypeError):
                        return None

                parsed_ltp = _fq("lp") or 0.0
                if parsed_ltp > 0 and underlying_price > 0:
                    intrinsic = (underlying_price - strike) if ot == "CE" else (strike - underlying_price)
                    intrinsic = max(0.0, intrinsic)
                    max_allowed = intrinsic + (0.15 * underlying_price)
                    if parsed_ltp > max_allowed:
                        log.warning(
                            "[shoonya] Rejecting corrupt option LTP %.2f for %s %s strike %.1f (underlying %.2f, max allowed %.2f)",
                            parsed_ltp, base, ot, strike, underlying_price, max_allowed,
                        )
                        parsed_ltp = 0.0

                strikes.append(
                    {
                        "strike": strike,
                        "option_type": ot,
                        "ltp": parsed_ltp,
                        "oi": _iq("oi") or 0,
                        "oi_change": _iq("oichg") or 0,
                        "volume": _iq("v") or 0,
                        "iv": _fq("iv") or 0.0,
                        "bid": _fq("bp1") or 0.0,
                        "ask": _fq("sp1") or 0.0,
                    }
                )

            if not strikes:
                log.warning("[shoonya] no strikes parsed for %s", base)
                return None

            return {
                "symbol": base,
                "underlying_price": underlying_price,
                "expiry": target_expiry_iso,
                "strikes": strikes,
                "source": self.name,
                "all_expiries": all_expiries,
            }

        except Exception as exc:
            log.exception("[shoonya] option chain fetch failed for %s: %s", symbol, exc)
            return None

    # ------------------------------------------------------------------
    # Quote methods expected by data/feed.py quote_batch()
    # ------------------------------------------------------------------

    def fetch_fno_quote(self, symbol: str) -> dict | None:
        """Fetch NFO futures quote for an F&O stock.

        Returns dict with keys: ltp, open, high, low, close, volume, oi,
        prev_close, change_pct or None on failure.
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

            target_item = None
            target_diff = None
            for v in values:
                if v.get("instname") != "FUTSTK":
                    continue
                tsym = v.get("tsym", "")
                m = re.search(rf"^{base}(\d{{2}}[A-Z]{{3}})(\d{{2}})(F|FUT)?$", tsym)
                if not m:
                    continue
                try:
                    date_part = m.group(1)  # e.g. "28JUL"
                    year_part = m.group(2)   # e.g. "26"
                    fut_tag = m.group(3)     # "F", "FUT", or None
                    if fut_tag == "FUT":
                        exp_dt = datetime.strptime(date_part, "%d%b").replace(year=now.year)
                        if exp_dt < now:
                            exp_dt = exp_dt.replace(year=now.year + 1)
                    else:
                        exp_dt = datetime.strptime(date_part + year_part, "%d%b%y")
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

            def _f(key):
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
            log.warning("[shoonya] FNO quote fetch failed for %s: %s", symbol, exc)
            return None

    def fetch_quote(self, symbol: str) -> dict | None:
        """Fetch single quote (LTP) for a symbol.

        Returns dict with keys: ltp, prev_close, change_pct or None.
        """
        if not self.login():
            return None

        base = symbol.upper().split()[0]
        try:
            if base in _INDEX_SPOT_NAMES:
                token = _INDEX_NSE_TOKENS.get(base)
                if token:
                    quote = self._get_quotes("NSE", token)
                else:
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
                scrip_res = self._search_scrip("NSE", base)
                if not scrip_res or scrip_res.get("stat") != "Ok":
                    return None
                values = scrip_res.get("values", [])
                if not values:
                    return None
                target_tsym = f"{base}-EQ"
                item = None
                for v in values:
                    if v.get("tsym") == target_tsym or v.get("symname") == base:
                        item = v
                        break
                if item is None:
                    return None
                token = item.get("token") or item.get("tok")
                if not token:
                    return None
                quote = self._get_quotes("NSE", token)

            if not quote or quote.get("stat") != "Ok":
                return None

            def _f(key):
                try:
                    return float(quote.get(key) or 0.0)
                except (ValueError, TypeError):
                    return None

            ltp = _f("lp")
            if ltp is None or ltp <= 0:
                return None

            return {
                "ltp": ltp,
                "prev_close": None,
                "change_pct": None,
            }

        except Exception as exc:
            log.warning("[shoonya] quote fetch failed for %s: %s", symbol, exc)
            return None


# ------------------------------------------------------------------
# Module-level singleton — shared by chart_fetcher and option chain router.
# Using a single instance reuses the cached OAuth token across both callers.
# ------------------------------------------------------------------

_shoonya_instance: ShoonyaFetcher | None = None
_shoonya_lock = threading.Lock()


def get_shoonya_fetcher() -> ShoonyaFetcher:
    """Return (or lazily create) the process-wide ShoonyaFetcher singleton."""
    global _shoonya_instance
    if _shoonya_instance is None:
        with _shoonya_lock:
            if _shoonya_instance is None:
                _shoonya_instance = ShoonyaFetcher()
    return _shoonya_instance


# ------------------------------------------------------------------
# Compatibility shims imported by data/feed.py and other modules.
# ------------------------------------------------------------------

get_shoonya = get_shoonya_fetcher

_SHOONYA_API_FAILURE_TS: float = 0.0
_SHOONYA_API_COOLDOWN: int = 120  # seconds

_INDEX_NSE_TOKENS: dict[str, str] = {
    "NIFTY": "26000",
    "BANKNIFTY": "26009",
    "FINNIFTY": "26037",
    "MIDCPNIFTY": "26074",
    "SENSEX": "1",
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
