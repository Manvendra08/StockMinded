"""Fundamental data fetcher for the Telegram pipeline.

Screener.in does NOT expose a free, unauthenticated JSON financials API
(only a lightweight search-suggest endpoint). Therefore this module scrapes
the public company page HTML:

  * ``#quarters`` <section> for the quarterly P&L (Sales, OPM %, Net Profit)
    — used to compute Qtr Sales/Profit YoY variance and the OPM trend.
  * ``#top-ratios`` / ``#ratios`` for whatever consolidated ratios are present
    in the free page (typically ROCE %; Debt/Equity, Pledged %, and 3Y growth
    are often absent on the unauthenticated page).

Because the free page frequently omits core ratios, MISSING fields are treated
as "no opinion" (the corresponding hard filter is skipped) rather than an
automatic AVOID. The margin-compression checks (which rely only on the
quarterly table) remain fully enforced.

FALLBACK: yfinance key ratios when HTML scraping fails.
Caching: SQLite ``fundamentals_cache`` table via ``ops.journal.Journal`` with
a configurable TTL (reuses the existing journal DB — no new files).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

_BASE = "https://www.screener.in/company/{symbol}/"
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}

# Sectors for which the Debt/Equity hard filter is bypassed (plan §2.4).
_DE_FILTER_BYPASS_SECTORS = {"BANKS", "FINANCIALS", "NBFC"}


def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_HEADERS)
    return s


def _to_num(value: object) -> float | None:
    if value is None:
        return None
    s = str(value).strip().replace(",", "")
    if s in ("", "—", "-", "NA", "N/A"):
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg = True
        s = s[1:-1]
    s = s.replace("%", "").strip()
    try:
        v = float(s)
        return -v if neg else v
    except ValueError:
        return None


def fetch_screener(symbol: str, timeout: int = 20, company_name: str = "") -> dict | None:
    """Scrape the Screener.in company page for one symbol.

    Returns a normalized fundamentals dict, or None on failure.
    """
    url = _BASE.format(symbol=symbol)
    html = ""
    
    def _do_search_fallback(fetcher, kwargs):
        q = company_name if company_name else symbol
        if not q: return None
        try:
            s_url = f"https://www.screener.in/api/company/search/?q={q}"
            s_r = fetcher(s_url, **kwargs)
            if s_r.status_code == 200:
                data = s_r.json()
                if data and len(data) > 0 and data[0].get("url"):
                    new_url = f"https://www.screener.in{data[0]['url']}"
                    logger.info("Screener 404 for %s, redirecting to %s", symbol, new_url)
                    return fetcher(new_url, **kwargs)
        except Exception as e:
            logger.debug("Screener search failed for %s: %s", symbol, e)
        return None

    try:
        try:
            from curl_cffi import requests as curl_requests
            r = curl_requests.get(url, impersonate="chrome", timeout=timeout)
            if r.status_code == 404:
                fallback_r = _do_search_fallback(curl_requests.get, {"impersonate": "chrome", "timeout": timeout})
                if fallback_r: r = fallback_r
            if r.status_code != 200:
                logger.warning("Screener page %s -> HTTP %s", symbol, r.status_code)
                return None
            html = r.text
        except Exception as e_curl:
            logger.debug("curl_cffi failed for screener %s, trying requests: %s", symbol, e_curl)
            r = requests.get(url, headers=_HEADERS, timeout=timeout)
            if r.status_code == 404:
                fallback_r = _do_search_fallback(requests.get, {"headers": _HEADERS, "timeout": timeout})
                if fallback_r: r = fallback_r
            if r.status_code != 200:
                logger.warning("Screener page %s -> HTTP %s", symbol, r.status_code)
                return None
            r.encoding = r.apparent_encoding or "utf-8"
            html = r.text
    except Exception as e:
        logger.warning("Screener page %s failed: %s", symbol, e)
        return None

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        ratios: dict[str, float | None] = {}
        for sel in ("ul#top-ratios", "#ratios"):
            for li in soup.select(sel + " li"):
                name_el = li.select_one("span.name")
                num_el = li.select_one("span.number")
                if name_el and num_el:
                    ratios[name_el.get_text(strip=True)] = _to_num(
                        num_el.get_text(strip=True)
                    )
        # Also pull <td class="text">Label</td><td>Value</td> ratio rows.
        for row in soup.select("#ratios tr"):
            tds = row.find_all("td")
            if len(tds) >= 2:
                lab = tds[0].get_text(strip=True)
                val = _to_num(tds[1].get_text(strip=True))
                if lab and val is not None:
                    ratios.setdefault(lab, val)

        # Quarterly P&L from #quarters
        q = _extract_quarterly(soup)

        sector = _extract_sector(soup)

        return {
            # Core ratios (may be None on the free page)
            "debt_to_equity": ratios.get("Debt to Equity"),
            "roce_pct": ratios.get("ROCE %") or ratios.get("Return on Capital Employed"),
            "promoter_pledge_pct": ratios.get("Pledged percentage"),
            "sales_growth_3y_pct": ratios.get("Sales growth 3Years")
            or ratios.get("Growth in Revenue 3Years %"),
            "profit_growth_3y_pct": ratios.get("Profit growth 3Years")
            or ratios.get("Growth in Net Profit 3Years %"),
            # Derived from quarterly table
            "qtr_sales_var_pct": q["sales_yoy"],
            "qtr_profit_var_pct": q["profit_yoy"],
            "opm_series": q["opm"],
            "margin_decay_3q": _compute_margin_decay(q["opm"]),
            "sector": sector,
            "available_fields": sorted(
                k
                for k, v in {
                    "debt_to_equity": ratios.get("Debt to Equity"),
                    "roce_pct": ratios.get("ROCE %"),
                    "promoter_pledge_pct": ratios.get("Pledged percentage"),
                    "sales_growth_3y_pct": ratios.get("Sales growth 3Years"),
                    "profit_growth_3y_pct": ratios.get("Profit growth 3Years"),
                }.items()
                if v is not None
            ),
            "source": "screener",
        }
    except Exception as e:  # pragma: no cover - parse edge cases
        logger.warning("Screener parse %s failed: %s", symbol, e)
        return None


def _extract_quarterly(soup) -> dict:
    """Return dict with sales/profit/opm series (newest-first) + YoY vars."""
    out = {"sales": [], "profit": [], "opm": []}
    section = soup.select_one("#quarters")
    if not section:
        return {"sales_yoy": None, "profit_yoy": None, **out, "opm": []}

    # Map header row labels to column index for the data rows.
    header_cells = [
        c.get_text(strip=True).lower() for c in section.select("thead th, thead td")
    ]
    # Identify which data rows correspond to each metric.
    rows = section.select("tbody tr")
    for tr in rows:
        cells = tr.find_all(["td", "th"])
        if not cells:
            continue
        label = cells[0].get_text(strip=True).lower()
        vals = [_to_num(c.get_text(strip=True)) for c in cells[1:]]
        vals = [v for v in vals if v is not None]
        if not vals:
            continue
        vals.reverse()  # newest-first
        if "sales" in label and "opm" not in label:
            out["sales"] = vals
        elif "net profit" in label:
            out["profit"] = vals
        elif "opm" in label:
            out["opm"] = vals

    def yoy(series: list[float | None]) -> float | None:
        if len(series) >= 5 and series[0] is not None and series[4]:
            return round((series[0] / series[4] - 1) * 100, 2)
        return None

    return {
        "sales_yoy": yoy(out["sales"]),
        "profit_yoy": yoy(out["profit"]),
        "sales": out["sales"],
        "profit": out["profit"],
        "opm": out["opm"],
    }


def _compute_margin_decay(opm: list[float | None]) -> bool:
    """True if OPM declined in each of the last 3 sequential quarters."""
    if len(opm) < 3:
        return False
    last3 = opm[:3]  # newest-first
    for newer, older in zip(last3, last3[1:]):
        if newer is None or older is None:
            return False
        if newer >= older:
            return False
    return True


def _extract_sector(soup) -> str | None:
    try:
        tag = soup.select_one("#company-info .sub")
        if tag:
            return tag.get_text(strip=True)
    except Exception:
        return None
    return None


def fetch_yfinance(symbol: str, timeout: int = 15) -> dict | None:
    """Fallback fundamentals via yfinance (no OPM/margin-decay data)."""
    try:
        import yfinance as yf
    except ImportError:
        logger.warning("yfinance not installed; cannot use fallback")
        return None
    try:
        info = yf.Ticker(f"{symbol}.NS").info or {}
    except Exception as e:
        logger.warning("yfinance %s failed: %s", symbol, e)
        return None

    return {
        "debt_to_equity": _to_num(info.get("debtToEquity")),
        "roce_pct": _to_num(info.get("returnOnCapitalEmployed")),
        "promoter_pledge_pct": _to_num(info.get("heldPercentInsiders")),
        "sales_growth_3y_pct": _to_num(info.get("revenueGrowth")),
        "profit_growth_3y_pct": _to_num(info.get("earningsGrowth")),
        "qtr_sales_var_pct": None,
        "qtr_profit_var_pct": None,
        "opm_series": [],
        "margin_decay_3q": False,
        "sector": info.get("sector"),
        "available_fields": [],
        "source": "yfinance",
    }


def fetch_fundamentals(
    symbol: str,
    journal=None,
    cache_ttl_hours: int = 24,
    rate_limit_delay: float = 2.0,
    company_name: str = "",
) -> dict | None:
    """Return fundamentals for ``symbol``: cache -> Screener HTML -> yfinance.

    When ``journal`` is provided, results are cached in the shared SQLite DB.
    """
    if journal is not None:
        cached = journal.get_cached_fundamentals(symbol, max_age_hours=cache_ttl_hours)
        if cached and cached.get("available_fields"):
            return cached

    data = fetch_screener(symbol, company_name=company_name)
    if data is None:
        if rate_limit_delay:
            time.sleep(rate_limit_delay)
        data = fetch_yfinance(symbol)

    if data is None:
        logger.warning("Fundamentals unavailable for %s", symbol)
        return None

    if journal is not None:
        journal.cache_fundamentals(symbol, data, datetime.now(timezone.utc).isoformat())
    return data
