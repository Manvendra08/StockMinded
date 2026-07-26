"""Fundamental data fetcher for the Telegram pipeline.

Two scraping strategies:
1. **Structured parse** (default): extracts specific fields from known HTML
   selectors (#top-ratios, #quarters, #ratios). Fast and token-free.
2. **Adaptive LLM** (optional): grabs ALL tables as raw Markdown and passes
   them to the LLM with a sector-agnostic prompt. The LLM deduces the
   business model from row labels (e.g. "Financing Profit" → banking,
   "Operating Profit" → corporate). Zero maintenance — new metrics or
   exotic sectors (REITs, InvITs) require no code changes.

The adaptive path is triggered when ``call_llm`` is passed to
``fetch_fundamentals``. Without it, the structured parse is used as before.

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


# ---------------------------------------------------------------------------
# Adaptive LLM-based fundamental analysis
# ---------------------------------------------------------------------------

def scrape_raw_tables(symbol: str, timeout: int = 20) -> str | None:
    """Scrape core financial tables from the Screener page as Markdown.

    Uses pandas to grab <table> tags, then filters to only core financial
    tables (P&L, Balance Sheet, Cash Flows, Ratios, Shareholding) to avoid
    token bloat from Peer Comparison, Documents, etc.

    The Markdown string preserves the exact row labels Screener provides
    (which vary by sector — e.g. "Financing Profit" for banks, "Operating
    Profit" for corporates). Returns None on fetch failure.
    """
    try:
        from curl_cffi import requests as curl_requests
        url = _BASE.format(symbol=symbol)
        r = curl_requests.get(url, impersonate="chrome", timeout=timeout)
        if r.status_code != 200:
            logger.warning("Screener raw tables %s -> HTTP %s", symbol, r.status_code)
            return None
        html = r.text
    except Exception:
        try:
            import requests as _req
            r = _req.get(_BASE.format(symbol=symbol), headers=_HEADERS, timeout=timeout)
            if r.status_code != 200:
                return None
            r.encoding = r.apparent_encoding or "utf-8"
            html = r.text
        except Exception as e:
            logger.warning("Screener raw tables %s failed: %s", symbol, e)
            return None

    try:
        import pandas as pd
        from io import StringIO

        tables = pd.read_html(StringIO(html))
        if not tables:
            return None

        # Keywords that indicate a core financial table worth sending to LLM.
        # Matches in header row or first column of each table.
        _KEEP_KEYWORDS = {
            "sales", "revenue", "profit", "loss", "ebitda", "eps",
            "operating", "financing", "interest", "depreciation",
            "dividend", "equity", "assets", "liabilities", "cash",
            "debt", "roce", "roe", "ronw", "npa", "nim", "casa",
            "capital adequacy", "promoter", "pledge", "holding",
            "ratio", "mcap", "price", "book", "yield",
            # Screener section headers (appear as first row)
            "quarterly results", "profit & loss", "balance sheet",
            "cash flow", "ratios", "shareholding",
        }

        # Dates in header → likely a financial table (e.g. "Mar 2024", "Jun 2025")
        _DATE_PATTERN = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{4}\b")

        def _is_financial_table(df: pd.DataFrame) -> bool:
            """Return True if the DataFrame looks like core financial data."""
            if df.empty or len(df.columns) < 2:
                return False
            # Sample header row and first column values
            header_text = " ".join(str(c).lower() for c in df.columns)
            first_col_text = " ".join(str(v).lower() for v in df.iloc[:, 0].head(20).tolist())
            combined = header_text + " " + first_col_text
            # Check for financial keywords
            if any(kw in combined for kw in _KEEP_KEYWORDS):
                return True
            # Check for date-heavy headers (annual/quarterly tables)
            if _DATE_PATTERN.search(header_text):
                return True
            return False

        sections = []
        for idx, df in enumerate(tables):
            df = df.dropna(how="all")
            if df.empty:
                continue
            if not _is_financial_table(df):
                continue
            sections.append(f"Table {idx + 1}:\n{df.to_string(index=False)}")

        return "\n\n".join(sections) if sections else None
    except Exception as e:
        logger.warning("Screener raw tables parse %s failed: %s", symbol, e)
        return None


_ADAPTIVE_SYSTEM_PROMPT = """\
You are an expert fundamental equity analyst specializing in Indian markets.

I will provide raw financial tables scraped from Screener.in for a company.
The tables use Screener's native labels which vary by sector:
- Banks/NBFCs: "Financing Profit", "Interest", "NPA", "Capital Adequacy"
- Corporate/Manufacturing: "Operating Profit", "Material Cost", "ROCE"
- IT/Services: "Revenue", "EBITDA Margin", "Employee Cost"
- REITs/InvITs: "Distributable Income", "Distribution Yield"

Your task:
1. IDENTIFY the business model from row labels (do NOT assume — deduce from vocabulary)
2. EXTRACT the latest 4 quarters and latest 3 years of key metrics
3. COMPUTE:
   - Sales/Revenue growth trend (quarterly YoY and multi-year)
   - Profitability trend (margin direction, not just absolute level)
   - Balance sheet health using ONLY metrics present in the data
     (skip metrics that are absent — do NOT flag as "missing data")
4. FLAG risks visible in the numbers (margin decay, debt trajectory, pledge %, etc.)
5. Return a JSON object with EXACTLY these keys:
{
  "business_model": "banking|nbfc|corporate|it_services|reit|other",
  "confidence": "HIGH|MEDIUM|LOW",
  "key_metrics": {
    "metric_name": {"latest": value, "trend": "improving|stable|declining"}
  },
  "thesis": "2-3 sentence investment thesis",
  "risk": "Biggest fundamental risk visible in the numbers",
  "verdict": "STRONG_BUY|BUY|HOLD|AVOID"
}

Rules:
- Use ONLY data present in the tables. Never hallucinate missing metrics.
- If a metric is not available, omit it from key_metrics.
- For banks: focus on NIM, ROA, NPA trend, CASA ratio — ignore Debt/Equity.
- For corporates: focus on ROCE, OPM trend, D/E, promoter pledge.
- Reply with ONLY the JSON object. No preamble, no markdown fences."""


def adaptive_fundamental_analysis(
    symbol: str,
    call_llm: callable,
    company_name: str = "",
    timeout: int = 20,
) -> dict | None:
    """Scrape raw Screener tables and use LLM for sector-agnostic analysis.

    Args:
        symbol: NSE/BSE ticker
        call_llm: callable(prompt, system_prompt, json_mode, max_tokens) -> (str, str)
        company_name: optional company name for Screener search fallback
        timeout: HTTP timeout in seconds

    Returns:
        dict with keys: business_model, confidence, key_metrics, thesis, risk, verdict
        or None on failure.
    """
    raw = scrape_raw_tables(symbol, timeout=timeout)
    if not raw:
        return None

    # Truncate to ~12k chars (~3k tokens) to stay within provider limits.
    # The LLM only needs the latest 4 quarters + latest 3 years for analysis;
    # full peer comparison/shareholding bloat is already filtered out.
    _MAX_CHARS = 12000
    if len(raw) > _MAX_CHARS:
        raw = raw[:_MAX_CHARS] + f"\n\n[truncated at {_MAX_CHARS} chars]"

    label = company_name or symbol
    prompt = (
        f"Analyze the fundamentals of {label} ({symbol}.NS) based on the "
        f"following Screener.in data:\n\n{raw}"
    )

    try:
        resp, provider = call_llm(
            prompt=prompt,
            system_prompt=_ADAPTIVE_SYSTEM_PROMPT,
            json_mode=True,
            max_tokens=512,
            return_provider=True,
        )
    except Exception as e:
        logger.warning("Adaptive analysis LLM call failed for %s: %s", symbol, e)
        return None

    if not resp:
        return None

    # Handle both dict (pre-parsed) and string responses
    if isinstance(resp, dict):
        resp["_provider"] = provider
        logger.debug("Adaptive analysis OK for %s (%s): model=%s", symbol, resp.get("business_model"), resp.get("verdict"))
        return resp

    # Parse JSON string response
    try:
        import json
        text = str(resp).strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text[:-3]
        # Handle Python repr strings (single quotes → double quotes)
        if text.startswith("{") and "'" in text and '"' not in text:
            text = text.replace("'", '"')
        result = json.loads(text)
        if isinstance(result, dict):
            result["_provider"] = provider
            logger.debug("Adaptive analysis OK for %s (%s): model=%s", symbol, result.get("business_model"), result.get("verdict"))
            return result
    except (json.JSONDecodeError, IndexError):
        logger.warning("Adaptive analysis LLM returned non-JSON for %s: %s", symbol, str(resp)[:100])

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
    call_llm: callable | None = None,
) -> dict | None:
    """Return fundamentals for ``symbol``: cache -> Screener HTML -> yfinance.

    When ``call_llm`` is provided, the adaptive LLM analysis is also run
    and its output is included as ``adaptive_analysis`` in the result dict.

    When ``journal`` is provided, results are cached in the shared SQLite DB.
    """
    if journal is not None:
        cached = journal.get_cached_fundamentals(symbol, max_age_hours=cache_ttl_hours)
        if cached and cached.get("available_fields"):
            # Even for cached data, run adaptive analysis if call_llm is available
            if call_llm is not None and "adaptive_analysis" not in cached:
                adaptive = adaptive_fundamental_analysis(symbol, call_llm, company_name)
                if adaptive:
                    cached["adaptive_analysis"] = adaptive
                    try:
                        journal.cache_fundamentals(symbol, cached, datetime.now(timezone.utc).isoformat())
                    except Exception:
                        pass
            return cached

    data = fetch_screener(symbol, company_name=company_name)
    if data is None:
        if rate_limit_delay:
            time.sleep(rate_limit_delay)
        data = fetch_yfinance(symbol)

    if data is None:
        logger.warning("Fundamentals unavailable for %s", symbol)
        return None

    # Run adaptive LLM analysis if provider available
    if call_llm is not None:
        try:
            adaptive = adaptive_fundamental_analysis(symbol, call_llm, company_name)
            if adaptive:
                data["adaptive_analysis"] = adaptive
        except Exception as e:
            logger.debug("Adaptive analysis skipped for %s: %s", symbol, e)

    if journal is not None:
        journal.cache_fundamentals(symbol, data, datetime.now(timezone.utc).isoformat())
    return data
