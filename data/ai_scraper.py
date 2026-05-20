"""AI-driven web scraper using ScrapeGraphAI (Local and SaaS)."""
from __future__ import annotations

import os
import logging
from typing import Any, Optional

from scrapegraphai.graphs import SmartScraperGraph, SearchGraph
try:
    from scrapegraph_py import ScrapeGraphAI as ScrapeGraphSaaS
except ImportError:
    ScrapeGraphSaaS = None

from config.loader import load_config

logger = logging.getLogger(__name__)

def _get_ai_config() -> Optional[dict]:
    """Retrieve ScrapeGraphAI configuration from config.yaml."""
    try:
        full_cfg = load_config()
        cfg = full_cfg.get("data_sources", {}).get("scrapegraphai", {})
        if not cfg.get("enabled"):
            return None
        
        # Local model config
        api_key = cfg.get("api_key")
        if isinstance(api_key, str) and api_key.startswith("${") and api_key.endswith("}"):
            api_key = os.getenv(api_key[2:-1])
        
        # SaaS config
        saas_api_key = cfg.get("saas_api_key")
        if isinstance(saas_api_key, str) and saas_api_key.startswith("${") and saas_api_key.endswith("}"):
            saas_api_key = os.getenv(saas_api_key[2:-1])
        
        # Self-healing Fallback:
        # If the user accidentally set GOOGLE_API_KEY to their ScrapeGraphAI key (starts with 'sgai-')
        # we route it to saas_api_key and clear api_key so the local Gemini LLM doesn't get initialized with it.
        if isinstance(api_key, str) and api_key.startswith("sgai-"):
            if not saas_api_key:
                saas_api_key = api_key
            api_key = None

        return {
            "local": {
                "llm": {
                    "api_key": api_key,
                    "model": cfg.get("model", "google_genai/gemini-1.5-flash"),
                },
                "verbose": False,
                "headless": True,
            } if api_key else None,
            "saas_api_key": saas_api_key
        }
    except Exception as e:
        logger.error(f"Failed to load ScrapeGraphAI config: {e}")
        return None

def scrape_url(url: str, prompt: str) -> Any:
    """Extract structured data from a single URL using SaaS (preferred) or Local graph."""
    config = _get_ai_config()
    if not config:
        return None
    
    # Try SaaS first if configured
    if config["saas_api_key"] and ScrapeGraphSaaS:
        try:
            logger.info(f"AI SaaS Extract started for URL: {url}")
            sgai = ScrapeGraphSaaS(api_key=config["saas_api_key"])
            # In SaaS, extract() is used for structured data from URL
            result = sgai.extract(prompt=prompt, url=url)
            if result.status == "success":
                return result.data
            else:
                logger.warning(f"SaaS Extract failed for {url}: {result.error}")
        except Exception as e:
            logger.error(f"SaaS Extract failed for {url}: {e}")

    # Fallback to local execution
    if config["local"]:
        try:
            logger.info(f"AI Local Scrape fallback started for URL: {url}")
            graph = SmartScraperGraph(
                prompt=prompt,
                source=url,
                config=config["local"]
            )
            return graph.run()
        except Exception as e:
            logger.error(f"Local SmartScraperGraph failed for {url}: {e}")
    
    return None

def search_and_scrape(query: str, prompt: str) -> Any:
    """Search for information across multiple pages and summarize."""
    config = _get_ai_config()
    if not config:
        return None
    
    # Try SaaS first
    if config["saas_api_key"] and ScrapeGraphSaaS:
        try:
            logger.info(f"AI SaaS Search started for query: {query}")
            sgai = ScrapeGraphSaaS(api_key=config["saas_api_key"])
            # 1. Perform the search
            result = sgai.search(query=query)
            if result.status == "success":
                search_data = result.data
                results_list = []
                if isinstance(search_data, dict):
                    results_list = search_data.get("results") or []
                elif hasattr(search_data, "results"):
                    results_list = getattr(search_data, "results") or []
                elif isinstance(search_data, list):
                    results_list = search_data
                
                text_parts = []
                for item in results_list:
                    title = ""
                    content = ""
                    if isinstance(item, dict):
                        title = item.get("title") or ""
                        content = item.get("content") or ""
                    else:
                        title = getattr(item, "title", "") or ""
                        content = getattr(item, "content", "") or ""
                    if title or content:
                        text_parts.append(f"Title: {title}\nContent: {content}\n")
                
                if text_parts:
                    combined_text = "\n".join(text_parts)
                    logger.info(f"AI SaaS Extract started for search results of query: {query}")
                    summary_res = sgai.extract(prompt=prompt, html=combined_text)
                    if summary_res.status == "success":
                        data = summary_res.data
                        if hasattr(data, "json_data") and data.json_data:
                            return data.json_data
                        if isinstance(data, dict) and "json_data" in data:
                            return data["json_data"]
                        return data
                    else:
                        logger.warning(f"SaaS Extract for search results failed: {summary_res.error}")
                else:
                    logger.warning("No search results found to extract.")
            else:
                logger.warning(f"SaaS Search failed for query '{query}': {result.error}")
        except Exception as e:
            logger.error(f"SaaS Search failed for query '{query}': {e}")

    # Fallback to local execution (Lightweight RSS + Gemini API)
    if config.get("local") and config["local"]["llm"].get("api_key"):
        try:
            logger.info(f"AI Local RSS Fallback started for query: {query}")
            import urllib.request
            import urllib.parse
            import xml.etree.ElementTree as ET
            import json

            # 1. Fetch RSS from Google News
            rss_query = urllib.parse.quote(query)
            rss_url = f"https://news.google.com/rss/search?q={rss_query}&hl=en-IN&gl=IN&ceid=IN:en"
            req = urllib.request.Request(rss_url, headers={'User-Agent': 'Mozilla/5.0'})
            resp = urllib.request.urlopen(req, timeout=10)
            root = ET.fromstring(resp.read())

            news_items = []
            for item in root.findall('.//item')[:15]:
                title = item.find('title').text if item.find('title') is not None else ''
                pub_date = item.find('pubDate').text if item.find('pubDate') is not None else ''
                news_items.append(f"- {pub_date}: {title}")

            news_text = '\n'.join(news_items)
            if not news_text.strip():
                logger.warning("No RSS news found.")
                return None

            # 2. Call Gemini API directly (1 request total)
            api_key = config["local"]["llm"]["api_key"]
            model_raw = config["local"]["llm"].get("model", "gemini-1.5-flash")
            model_name = model_raw.split("/")[-1] if "/" in model_raw else model_raw
            
            gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            full_prompt = f"{prompt}\n\nHere are the latest news headlines:\n{news_text}"
            
            payload = {
                "contents": [{"parts": [{"text": full_prompt}]}],
                "generationConfig": {"response_mime_type": "application/json"}
            }
            
            greio = urllib.request.Request(gemini_url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json'})
            gresp = urllib.request.urlopen(greio, timeout=15)
            res = json.loads(gresp.read())
            
            text_resp = res.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            if text_resp:
                try:
                    return json.loads(text_resp)
                except json.JSONDecodeError:
                    return text_resp
            return None
        except Exception as e:
            logger.error(f"Local RSS + Gemini fallback failed for query '{query}': {e}")
            
    return None

def get_option_chain_fallback(symbol: str) -> Optional[dict]:
    """Fallback extraction of option chain data using AI."""
    url = f"https://www.research360.in/future-and-options/option-chain?symbol={symbol}"
    prompt = (
        "Extract the current option chain table. "
        "Return a JSON object with 'records' containing a list of 'data' points. "
        "Each data point should have 'strikePrice', 'CE' (openInterest, lastPrice), "
        "and 'PE' (openInterest, lastPrice). "
        "Also include 'underlyingValue' if found."
    )
    return scrape_url(url, prompt)

def get_fii_dii_fallback() -> Optional[list[dict]]:
    """Fallback extraction of FII/DII cash data using AI."""
    url = "https://www.moneycontrol.com/stocks/marketstats/fii_dii_activity/index.php"
    prompt = (
        "Extract the latest FII and DII cash market net investment data. "
        "Return a list of objects, each with 'category' (FII or DII), 'date', and 'netValue' in Crores."
    )
    return scrape_url(url, prompt)

def _get_persistent_sentiment_cache() -> tuple[Optional[Any], float, float]:
    """Retrieve cached sentiment, timestamp, and expires_at from a persistent local file."""
    import json
    from pathlib import Path
    
    cache_file = Path("data/cache/ai_sentiment_cache.json")
    if cache_file.exists():
        try:
            with open(cache_file, "r") as f:
                data = json.load(f)
                return data.get("sentiment"), data.get("timestamp", 0.0), data.get("expires_at", 0.0)
        except Exception:
            pass
    return None, 0.0, 0.0

def _set_persistent_sentiment_cache(sentiment: Any, timestamp: float, ttl: float = 3600.0) -> None:
    """Save sentiment, timestamp, and expires_at to a persistent local file."""
    import json
    from pathlib import Path
    
    cache_file = Path("data/cache/ai_sentiment_cache.json")
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        with open(cache_file, "w") as f:
            json.dump({
                "sentiment": sentiment,
                "timestamp": timestamp,
                "expires_at": timestamp + ttl
            }, f)
    except Exception:
        pass

def _parse_rss_date(date_str: str) -> float:
    """Parse RFC 2822 date string to Unix timestamp. Returns 0.0 on failure."""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(date_str).timestamp()
    except Exception:
        return 0.0


# Noise phrases — skip headlines that are generic market outlooks / predictions
_NOISE_PATTERNS = {
    "prediction", "preview", "market today", "what to expect", "how indian",
    "stock market is expected", "nifty 50, sensex", "nifty 50, bank nifty",
    "nifty prediction", "banknifty prediction", "bank nifty prediction",
    "share market today", "sensex today", "outlook", "weekly recap",
    "week ahead", "market wrap", "roundup", "watch list", "stocks to watch",
    "things to know", "pre-market", "premarket", "gift nifty",
}

def _is_noise_headline(title: str) -> bool:
    t = title.lower()
    return any(p in t for p in _NOISE_PATTERNS)


def analyze_sentiment_locally(headlines: list[tuple[str, str]]) -> dict:
    """Analyze news sentiment locally using a curated financial lexicon.

    Args:
        headlines: List of (title, pub_date_str) tuples.

    Returns:
        Sentiment dict matching the Gemini output schema.
    """
    # --- Curated lexicon: removed noisy words (hit, high, up, down, support,
    # sell, pressure) that fire on neutral headlines. Added India-specific terms.
    pos_words = {
        "rally", "rebound", "gain", "gains", "rise", "rises", "surge", "surges",
        "soar", "soars", "upbeat", "bull", "bullish", "optimistic", "optimism",
        "recovery", "breakout", "beat", "beats", "growth", "strong", "positive",
        "buying", "inflow", "stabilize", "stabilizing", "upgrade", "upgraded",
        "gaining", "soaring", "outperform", "record", "boom", "booming",
        "accumulate", "accumulation", "overweight",
    }
    neg_words = {
        "drop", "drops", "fall", "falls", "plunge", "plunges", "crash",
        "decline", "declines", "selling", "downgrade", "downgraded", "loss",
        "losses", "slump", "slumps", "bear", "bearish", "fears", "worry",
        "worries", "correction", "slashed", "underperform", "weak", "weakness",
        "sink", "drag", "outflow", "slowdown", "falling", "plunging",
        "declining", "worried", "selloff", "panic", "turmoil", "underweight",
    }
    # Simple negation prefixes that flip the next word's polarity
    negators = {"no", "not", "fails", "failed", "unlikely", "without", "never"}

    pos_score = 0
    neg_score = 0
    catalysts: list[str] = []
    # Accumulate per-ticker net sentiment: {ticker: int} (+ = bullish mentions, - = bearish)
    ticker_sentiment: dict[str, int] = {}

    # Major Indian stock tickers — includes common headline name variants
    known_tickers = {
        "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN",
        "BHARTIARTL", "LT", "ITC", "TATASTEEL", "TATAMOTORS", "MARUTI",
        "AXISBANK", "KOTAKBANK", "ADANIENT", "WIPRO", "HCLTECH", "SUNPHARMA",
        "BAJFINANCE", "COALINDIA", "NIFTY", "BANKNIFTY",
    }
    # Tickers that need substring match because they contain special characters
    # or appear as company names rather than ticker symbols
    substring_tickers = {"M&M": "M&M", "MAHINDRA": "M&M"}

    for title, _pub_date in headlines:
        lower_title = title.lower()
        words = [w.strip(".,;:?!()\"'") for w in lower_title.split()]

        item_pos = 0
        item_neg = 0
        for i, w in enumerate(words):
            prev_word = words[i - 1] if i > 0 else ""
            is_negated = prev_word in negators

            if w in pos_words:
                if is_negated:
                    item_neg += 1
                else:
                    item_pos += 1
            elif w in neg_words:
                if is_negated:
                    item_pos += 1
                else:
                    item_neg += 1

        pos_score += item_pos
        neg_score += item_neg

        # Extract catalyst headlines — skip generic prediction/outlook noise
        if not _is_noise_headline(title):
            clean_title = title.split(" - ")[0].strip()
            if item_pos > item_neg and len(catalysts) < 3:
                catalysts.append(f"Bullish: {clean_title}")
            elif item_neg > item_pos and len(catalysts) < 3:
                catalysts.append(f"Bearish: {clean_title}")

        # Detect tickers — accumulate net sentiment per ticker
        headline_direction = 1 if item_pos >= item_neg else -1
        upper_title = title.upper()
        title_words_upper = {w.strip(".,;:?!()\"'") for w in upper_title.split()}

        for ticker in known_tickers:
            if ticker in title_words_upper:
                ticker_sentiment[ticker] = ticker_sentiment.get(ticker, 0) + headline_direction
        for substr, canonical in substring_tickers.items():
            if substr in upper_title:
                ticker_sentiment[canonical] = ticker_sentiment.get(canonical, 0) + headline_direction

    # Build deduplicated trade ideas: one direction per ticker
    trade_ideas: list[str] = []
    for ticker, net in sorted(ticker_sentiment.items(), key=lambda x: abs(x[1]), reverse=True):
        if len(trade_ideas) >= 3:
            break
        if net == 0:
            continue
        side = "LONG" if net > 0 else "SHORT"
        mentions = abs(net)
        strength = "strong" if mentions >= 3 else "moderate" if mentions == 2 else "mild"
        trade_ideas.append(f"{ticker} — {strength} {side.lower()} bias ({mentions} headline{'s' if mentions > 1 else ''})")

    # Fallback catalysts: only use non-noisy headlines
    if not catalysts:
        fallback_titles = [
            title.split(" - ")[0].strip()
            for title, _ in headlines
            if title and not _is_noise_headline(title)
        ][:3]
        catalysts = fallback_titles

    # Final fallback if all headlines were noise
    if not catalysts:
        if pos_score > neg_score:
            catalysts = ["Broad market sentiment positive across recent news cycle."]
        elif neg_score > pos_score:
            catalysts = ["Caution signals detected across recent news cycle."]
        else:
            catalysts = ["Mixed signals — no dominant directional catalyst identified."]

    # Ratio-based scoring: avoids the absolute-threshold scaling problem
    total_signals = pos_score + neg_score
    if total_signals > 0:
        ratio = pos_score / total_signals
        if ratio > 0.60:
            sentiment = "BULLISH"
        elif ratio < 0.40:
            sentiment = "BEARISH"
        else:
            sentiment = "NEUTRAL"
    else:
        sentiment = "NEUTRAL"
        ratio = 0.5

    justification = (
        f"Local Sentiment Engine: analyzed {len(headlines)} headlines. "
        f"{pos_score} bullish vs {neg_score} bearish signals "
        f"(ratio {ratio:.0%})."
    )

    return {
        "overall_market_sentiment": sentiment,
        "justification": justification,
        "top_catalysts": catalysts[:3],
        "actionable_trade_ideas": trade_ideas,
    }


def get_market_news_sentiment(query: str = "Nifty BankNifty stock market India today") -> Optional[dict]:
    """Fetch market news via RSS, summarize sentiment via Gemini or local lexicon fallback.

    Pipeline:
      1. Fetch Google News RSS (focused query, filtered to last 36 hours).
      2. Attempt Gemini REST API summarization (1 API call).
      3. If Gemini fails/rate-limited → local lexicon analysis (0 API calls).
    """
    import time
    now = time.time()

    # Check persistent cache
    cached_val, cached_ts, expires_at = _get_persistent_sentiment_cache()
    if expires_at == 0.0 and cached_ts > 0.0:
        expires_at = cached_ts + 3600.0

    if cached_val is not None and now < expires_at:
        logger.info(
            f"Returning cached sentiment (age: {round(now - cached_ts)}s, "
            f"expires in {round(expires_at - now)}s)"
        )
        return cached_val

    # 1. Fetch RSS from Google News
    headlines: list[tuple[str, str]] = []
    recency_cutoff = now - 36 * 3600  # Last 36 hours only
    try:
        import urllib.request
        import urllib.parse
        import xml.etree.ElementTree as ET

        rss_query = urllib.parse.quote(query)
        rss_url = (
            f"https://news.google.com/rss/search?q={rss_query}"
            "&hl=en-IN&gl=IN&ceid=IN:en"
        )
        req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        root = ET.fromstring(resp.read())

        for item in root.findall(".//item"):
            title_el = item.find("title")
            date_el = item.find("pubDate")
            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            pub_date = date_el.text.strip() if date_el is not None and date_el.text else ""
            if not title or _is_noise_headline(title):
                continue

            # Filter stale headlines
            if pub_date:
                pub_ts = _parse_rss_date(pub_date)
                if pub_ts > 0 and pub_ts < recency_cutoff:
                    continue

            headlines.append((title, pub_date))
            if len(headlines) >= 20:
                break

    except Exception as e:
        logger.error(f"Failed to fetch Google News RSS: {e}")

    # Self-healing Fallback: If primary daily-wrap query is 100% filtered out as noise,
    # try high-quality business, corporate, and macroeconomic queries.
    if not headlines:
        fallback_queries = [
            "Indian stock market corporate news earnings",
            "Indian economy OR corporate earnings OR RBI OR SEBI"
        ]
        import urllib.request
        import urllib.parse
        import xml.etree.ElementTree as ET
        
        for f_query in fallback_queries:
            try:
                logger.info(f"Primary news query yielded 0 headlines. Trying fallback query: '{f_query}'")
                rss_query = urllib.parse.quote(f_query)
                rss_url = f"https://news.google.com/rss/search?q={rss_query}&hl=en-IN&gl=IN&ceid=IN:en"
                req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
                resp = urllib.request.urlopen(req, timeout=10)
                root = ET.fromstring(resp.read())
                
                for item in root.findall(".//item"):
                    title_el = item.find("title")
                    date_el = item.find("pubDate")
                    title = title_el.text.strip() if title_el is not None and title_el.text else ""
                    pub_date = date_el.text.strip() if date_el is not None and date_el.text else ""
                    if not title or _is_noise_headline(title):
                        continue
                    if pub_date:
                        pub_ts = _parse_rss_date(pub_date)
                        if pub_ts > 0 and pub_ts < recency_cutoff:
                            continue
                    headlines.append((title, pub_date))
                    if len(headlines) >= 20:
                        break
                if headlines:
                    logger.info(f"Successfully recovered {len(headlines)} headlines using fallback query '{f_query}'")
                    break
            except Exception as fe:
                logger.error(f"Fallback news query '{f_query}' failed: {fe}")

    if not headlines:
        fallback = {
            "overall_market_sentiment": "NEUTRAL",
            "justification": "Google News RSS feed temporarily unavailable or returned no recent results.",
            "top_catalysts": ["RSS Feed unavailable or no recent articles found"],
            "actionable_trade_ideas": [],
        }
        _set_persistent_sentiment_cache(fallback, now, ttl=900.0)
        return fallback

    logger.info(f"Fetched {len(headlines)} recent headlines from Google News RSS")

    # 2. Attempt Gemini API summarization
    config = _get_ai_config()
    gemini_success = False
    sentiment = None

    if config and config.get("local") and config["local"]["llm"].get("api_key"):
        try:
            logger.info("Attempting Gemini API sentiment analysis on RSS news...")
            import json

            api_key = config["local"]["llm"]["api_key"]
            model_raw = config["local"]["llm"].get("model", "gemini-1.5-flash")
            model_name = model_raw.split("/")[-1] if "/" in model_raw else model_raw

            gemini_url = (
                f"https://generativelanguage.googleapis.com/v1beta/models/"
                f"{model_name}:generateContent?key={api_key}"
            )
            news_text = "\n".join(
                f"- {pub}: {title}" for title, pub in headlines
            )

            prompt = (
                "Analyze the latest Indian stock market news from top financial portals. "
                "Identify high-conviction market developments, key catalysts, and momentum trade ideas. "
                "Your response MUST be a JSON object with the following keys:\n"
                "1. 'overall_market_sentiment': Must be one of: BULLISH, BEARISH, or NEUTRAL.\n"
                "2. 'justification': A sharp, 1-2 sentence market strategist summary explaining the primary driver of today's price action.\n"
                "3. 'top_catalysts': List of 2-3 specific macro or micro-economic drivers (e.g., earnings beats, FII buying, global cues).\n"
                "4. 'actionable_trade_ideas': List of 2-3 concrete trading ideas based on broker upgrades, breakout patterns, or corporate actions mentioned in the news, specifying tickers if available."
            )
            full_prompt = f"{prompt}\n\nHere are the latest news headlines:\n{news_text}"

            payload = {
                "contents": [{"parts": [{"text": full_prompt}]}],
                "generationConfig": {"response_mime_type": "application/json"},
            }

            greio = urllib.request.Request(
                gemini_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            gresp = urllib.request.urlopen(greio, timeout=12)
            res = json.loads(gresp.read())

            text_resp = (
                res.get("candidates", [{}])[0]
                .get("content", {})
                .get("parts", [{}])[0]
                .get("text", "")
            )
            if text_resp:
                sentiment = json.loads(text_resp)
                gemini_success = True
                logger.info("Successfully generated sentiment via Gemini API!")
        except Exception as e:
            logger.warning(f"Gemini API analysis failed: {e}")

    # 3. Local lexicon fallback if Gemini failed or not configured
    if not gemini_success:
        logger.info("Triggering Local Financial Sentiment Analysis (lexicon-based)...")
        sentiment = analyze_sentiment_locally(headlines)

    _set_persistent_sentiment_cache(sentiment, now, ttl=3600.0)
    return sentiment

