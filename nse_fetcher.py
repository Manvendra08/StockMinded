"""
NSE India Public JSON API — fallback fetcher.
No auth required. Session warm-up needed (cookie handshake).
Rate-limited: use only as fallback, not primary.
"""
import logging
import time
from datetime import datetime, timedelta, timezone
from config.settings import NSE_BASE_URL, NSE_OPTION_CHAIN_URL, NSE_EQUITY_OC_URL, NSE_HEADERS
from src.fetchers.base_fetcher import BaseFetcher

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"}
COMMODITY_SYMBOLS = {"NATURALGAS", "CRUDEOIL", "GOLD", "SILVER", "COPPER"}


class NSEPublicFetcher(BaseFetcher):
    name = "nse_public"
    _session_warmed = False
    _last_warmed_time = 0.0

    def __init__(self):
        super().__init__()
        self.session.headers.update(NSE_HEADERS)

    def _warm_session(self):
        """Hit NSE homepage to acquire cookies — required for API calls."""
        now = time.time()
        if NSEPublicFetcher._session_warmed and (now - NSEPublicFetcher._last_warmed_time < 300):
            return
        
        self.session.cookies.clear()
        
        # Add basic retry for session warm-up
        for attempt in range(3):
            try:
                self.session.get(NSE_BASE_URL, timeout=10)
                # Hit the option-chain page to ensure cookies are set for APIs
                self.session.get(f"{NSE_BASE_URL}/option-chain", timeout=10)
                NSEPublicFetcher._session_warmed = True
                NSEPublicFetcher._last_warmed_time = time.time()
                log.debug("[nse_public] session warmed")
                return
            except Exception as exc:
                exc_str = str(exc).lower()
                if "nameresolutionerror" in exc_str or "getaddrinfo failed" in exc_str or "failed to resolve" in exc_str:
                    log.warning("[nse_public] session warm-up failed: Name resolution failed. Skipping retries.")
                    break
                if attempt == 2:
                    log.warning("[nse_public] session warm-up failed after %d attempts: %s", attempt+1, exc)
                else:
                    time.sleep(2)

    def fetch_option_chain(self, symbol: str) -> dict | None:
        self._warm_session()
        symbol = symbol.upper().split()[0]

        if symbol in COMMODITY_SYMBOLS:
            log.warning("[nse_public] commodity option chain disabled for %s", symbol)
            return None

        raw = None
        try:
            if symbol in INDEX_SYMBOLS:
                expiry = self._nearest_contract_expiry(symbol)
                if not expiry:
                    return None
                url = f"{NSE_BASE_URL}/api/option-chain-v3?type=Indices&symbol={symbol}&expiry={expiry}"
                raw = self._get(url)
            else:
                expiry = self._nearest_contract_expiry(symbol)
                url = f"{NSE_BASE_URL}/api/option-chain-v3?type=Equity&symbol={symbol}"
                if expiry:
                    url += f"&expiry={expiry}"
                raw = self._get(url)
        except Exception as exc:
            log.warning("[nse_public] fetch error for %s: %s", symbol, exc)
            NSEPublicFetcher._session_warmed = False

        if not raw:
            NSEPublicFetcher._session_warmed = False
            return None
        return self._normalise(symbol, raw)

    def _commodity_spot(self, symbol: str) -> float:
        raw = self._get(f"{NSE_BASE_URL}/api/refrates?index=commodityspotrates")
        if not raw:
            return 0.0
        for item in raw.get("data", []):
            if str(item.get("symbol", "")).upper() == symbol:
                val = item.get("lastSpotPrice") or item.get("spotPrice") or 0
                try:
                    return float(str(val).replace(",", ""))
                except (TypeError, ValueError):
                    return 0.0
        return 0.0

    def _nearest_contract_expiry(self, symbol: str) -> str:
        url = f"{NSE_BASE_URL}/api/option-chain-contract-info?symbol={symbol}"
        raw = self._get(url)
        if not raw:
            return ""
        return self._nearest_expiry(raw.get("expiryDates", []), out_format="%d-%b-%Y")

    def _nearest_expiry(self, expiry_dates: list[str], out_format: str = "%Y-%m-%d") -> str:
        today = datetime.now(IST).date()
        parsed: list[tuple] = []
        for exp in expiry_dates:
            try:
                dt = datetime.strptime(exp, "%d-%b-%Y").date()
                parsed.append((dt, exp))
            except ValueError:
                continue

        future = sorted((item for item in parsed if item[0] >= today), key=lambda x: x[0])
        if future:
            return future[0][0].strftime(out_format)

        if parsed:
            return sorted(parsed, key=lambda x: x[0])[0][0].strftime(out_format)

        return ""

    def _normalise(self, symbol: str, raw: dict) -> dict | None:
        try:
            records_root = raw.get("records", {})
            filtered = raw.get("filtered", {})
            records = filtered.get("data") or records_root.get("data") or []
            underlying = float(
                records_root.get("underlyingValue", 0) or 0
            )
            expiry_dates = records_root.get("expiryDates", [])
            expiry = self._nearest_expiry(expiry_dates)

            strikes = []
            for record in records:
                if expiry and record.get("expiryDate"):
                    try:
                        record_expiry = datetime.strptime(record["expiryDate"], "%d-%b-%Y").date().strftime("%Y-%m-%d")
                    except ValueError:
                        record_expiry = ""
                    if record_expiry and record_expiry != expiry:
                        continue
                strike = float(record.get("strikePrice", 0))
                for ot in ("CE", "PE"):
                    opt = record.get(ot)
                    if not opt:
                        continue
                    strikes.append({
                        "strike":      strike,
                        "option_type": ot,
                        "ltp":         float(opt.get("lastPrice", 0) or 0),
                        "oi":          int(opt.get("openInterest", 0) or 0),
                        "oi_change":   int(opt.get("changeinOpenInterest", 0) or 0),
                        "volume":      int(opt.get("totalTradedVolume", 0) or 0),
                        "iv":          float(opt.get("impliedVolatility", 0) or 0),
                        "bid":         float(opt.get("bidPrice", opt.get("bidprice", 0)) or 0),
                        "ask":         float(opt.get("askPrice", 0) or 0),
                    })

            return {
                "symbol":           symbol,
                "underlying_price": underlying,
                "expiry":           expiry,
                "strikes":          strikes,
                "source":           self.name,
            }
        except Exception as exc:
            log.error("[nse_public] normalise failed for %s: %s", symbol, exc)
            return None
