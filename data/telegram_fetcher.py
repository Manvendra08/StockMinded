"""Public Web Preview scraper for the Telegram signal pipeline.

Reads public Telegram channels WITHOUT any API credentials by scraping the
public "preview" view at ``https://t.me/s/{username}``. This is a zero-auth,
no-phone, no-API-key approach: it works for any public channel and never
requires a bot or user account.

HTML is parsed with BeautifulSoup. Messages are identified by the
``data-post`` attribute (``channel/msg_id``), which doubles as a stable
dedupe key consumed by ``ops.telegram_state.TelegramState``.

The module is lazy about importing ``bs4`` / ``requests`` so the rest of the
codebase (and unit tests with a mocked response) does not require the
dependency at import time.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TelegramMessage:
    msg_id: int
    text: str
    date: Any
    channel: str
    views: int = 0
    forwards: int = 0


# Matches the data-post attribute, e.g. "RedBoxglobalIndia/1234"
_POST_RE = re.compile(r"^[^/]+/(\d+)$")


def _build_session() -> Any:
    """Build an HTTP session using plain requests or curl_cffi.

    Note: We do NOT use impersonate='chrome' because Telegram inspects Chrome TLS
    fingerprints and sends a 302 redirect from t.me/s/{username} to t.me/{username}.
    Standard HTTP requests without cookies return 200 OK with the full widget HTML.
    """
    try:
        import requests
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                )
            }
        )
        return s
    except ImportError:
        pass
    try:
        from curl_cffi import requests as curl_requests
        s = curl_requests.Session()
        s.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
                )
            }
        )
        return s
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "requests or curl_cffi is required for the Telegram Web Preview fetcher. "
            "Install with: pip install requests"
        ) from e


def _parse_message_html(html: str, username: str, last_msg_id: int, yesterday_start: Any) -> "list[TelegramMessage]":
    """Parse Telegram message HTML from either channel-history or single-message embed format.

    Handles two HTML layouts:
    - Channel history (t.me/s/{channel}): messages wrapped in .tgme_widget_message_wrap
    - Single-message embed (t.me/{channel}/{id}?embed=1): bare [data-post] element, no wrapper
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out: list[TelegramMessage] = []

    # --- Layout 1: channel history (has .tgme_widget_message_wrap wrappers) ---
    wraps = soup.select(".tgme_widget_message_wrap")
    if wraps:
        for wrap in reversed(wraps):
            msg_el = wrap.select_one(".tgme_widget_message")
            post_attr = (msg_el.get("data-post") or "") if msg_el else ""
            m = _POST_RE.match(post_attr)
            if not m:
                continue
            msg_id = int(m.group(1))
            if msg_id <= last_msg_id:
                continue
            time_el = wrap.select_one("time")
            date_val = time_el.get("datetime") if time_el else None
            if date_val:
                try:
                    if datetime.fromisoformat(date_val) < yesterday_start:
                        continue
                except Exception:
                    pass
            text_el = wrap.select_one(".tgme_widget_message_text")
            text = text_el.get_text(separator="\n", strip=True) if text_el else ""
            views_el = wrap.select_one(".tgme_widget_message_views")
            views = _parse_kmb(views_el.get_text(strip=True)) if views_el else 0
            out.append(TelegramMessage(msg_id=msg_id, text=text, date=date_val, channel=username, views=views))
        out.reverse()
        return out

    # --- Layout 2: single-message embed (bare [data-post] element) ---
    msg_els = soup.select("[data-post]")
    for msg_el in msg_els:
        post_attr = msg_el.get("data-post") or ""
        m = _POST_RE.match(post_attr)
        if not m:
            continue
        msg_id = int(m.group(1))
        if msg_id <= last_msg_id:
            continue
        # Look for time in the whole document
        time_el = soup.select_one("time")
        date_val = time_el.get("datetime") if time_el else None
        if date_val:
            try:
                if datetime.fromisoformat(date_val) < yesterday_start:
                    return out  # too old, stop walking
            except Exception:
                pass
        text_el = msg_el.select_one(".tgme_widget_message_text")
        if not text_el:
            text_el = soup.select_one(".tgme_widget_message_text")
        text = text_el.get_text(separator="\n", strip=True) if text_el else ""
        out.append(TelegramMessage(msg_id=msg_id, text=text, date=date_val, channel=username))

    return out


def _fetch_via_history(session: Any, username: str, timeout: int) -> tuple[str, bool]:
    """Try t.me/s/{username}?embed=1. Returns (html, success)."""
    url = f"https://t.me/s/{username}?embed=1"
    if hasattr(session, "cookies") and hasattr(session.cookies, "clear"):
        session.cookies.clear()
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=False)
        if resp.status_code == 200:
            resp.encoding = getattr(resp, "apparent_encoding", None) or "utf-8"
            return resp.text, True
        logger.debug("t.me/s/%s -> HTTP %s", username, resp.status_code)
        return "", False
    except Exception as e:
        logger.debug("t.me/s/%s fetch error: %s", username, e)
        return "", False


def _fetch_via_msg_embed(session: Any, username: str, start_msg_id: int,
                         last_msg_id: int, limit: int, timeout: int,
                         yesterday_start: Any) -> "list[TelegramMessage]":
    """Walk backwards through individual message embeds for channels where /s/ is disabled.

    Fetches t.me/{username}/{msg_id}?embed=1 for msg_id = start_msg_id, start_msg_id-1, …
    stopping when msg_id <= last_msg_id, limit is hit, or the message is too old.
    """
    out: list[TelegramMessage] = []
    msg_id = start_msg_id
    consecutive_misses = 0
    while msg_id > last_msg_id and len(out) < limit and consecutive_misses < 5:
        url = f"https://t.me/{username}/{msg_id}?embed=1&single=1"
        if hasattr(session, "cookies") and hasattr(session.cookies, "clear"):
            session.cookies.clear()
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            if resp.status_code != 200:
                msg_id -= 1
                consecutive_misses += 1
                continue
            resp.encoding = getattr(resp, "apparent_encoding", None) or "utf-8"
            parsed = _parse_message_html(resp.text, username, last_msg_id - 1, yesterday_start)
            if parsed:
                consecutive_misses = 0
                out.extend(parsed)
            else:
                consecutive_misses += 1
        except Exception as e:
            logger.debug("t.me/%s/%d embed error: %s", username, msg_id, e)
            consecutive_misses += 1
        msg_id -= 1

    out.sort(key=lambda x: x.msg_id)
    return out


def _get_latest_msg_id(session: Any, username: str, timeout: int) -> int:
    """Probe t.me/{username}?embed=1 to find the latest msg_id in the channel."""
    url = f"https://t.me/{username}?embed=1"
    if hasattr(session, "cookies") and hasattr(session.cookies, "clear"):
        session.cookies.clear()
    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True)
        if resp.status_code != 200:
            return 0
        from bs4 import BeautifulSoup
        resp.encoding = getattr(resp, "apparent_encoding", None) or "utf-8"
        soup = BeautifulSoup(resp.text, "html.parser")
        els = soup.select("[data-post]")
        ids = []
        for el in els:
            m = _POST_RE.match(el.get("data-post", ""))
            if m:
                ids.append(int(m.group(1)))
        return max(ids) if ids else 0
    except Exception:
        return 0


def fetch_public_messages(
    channel_username: str,
    last_msg_id: int = 0,
    limit: int = 20,
    session: Any | None = None,
    timeout: int = 20,
) -> list[TelegramMessage]:
    """Scrape public messages for ``channel_username`` with id > ``last_msg_id``.

    Strategy 1 (preferred): ``t.me/s/{username}?embed=1`` — channel history view.
      Works for channels that have web preview enabled. Returns up to ``limit`` recent messages.

    Strategy 2 (fallback): ``t.me/{username}/{msg_id}?embed=1`` — per-message embed.
      Used when strategy 1 returns HTTP 302 (channel has web preview disabled).
      Walks backwards from the latest known msg_id fetching individual message embeds.

    Both strategies restrict to messages from the current and previous day (UTC).
    Returns messages in chronological order (oldest first), up to ``limit`` items.
    """
    try:
        from bs4 import BeautifulSoup  # noqa: F401 – ensure available
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "beautifulsoup4 is required for the Telegram Web Preview fetcher. "
            "Install it with: pip install beautifulsoup4"
        ) from e

    username = channel_username.lstrip("@")
    own_session = session is None
    if session is None:
        session = _build_session()

    # Date cutoff: start of yesterday UTC
    now_utc = datetime.now(timezone.utc)
    yesterday_start = (now_utc - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    try:
        # --- Strategy 1: channel history ---
        html, ok = _fetch_via_history(session, username, timeout)
        if ok and html:
            msgs = _parse_message_html(html, username, last_msg_id, yesterday_start)
            if msgs:
                return msgs[:limit]
            # History page loaded but has 0 new messages (all already seen)
            return []

        # --- Strategy 2: per-message embed (channel has /s/ preview disabled) ---
        logger.info("t.me/s/%s unavailable, using per-message embed strategy", username)
        latest = _get_latest_msg_id(session, username, timeout)
        if not latest:
            logger.warning("Could not determine latest msg_id for %s", username)
            return []
        if latest <= last_msg_id:
            return []  # Nothing new

        return _fetch_via_msg_embed(
            session, username,
            start_msg_id=latest,
            last_msg_id=last_msg_id,
            limit=limit,
            timeout=timeout,
            yesterday_start=yesterday_start,
        )

    except Exception as e:  # pragma: no cover
        logger.error("fetch_public_messages(%s) failed: %s", username, e)
        return []
    finally:
        if own_session:
            try:
                session.close()
            except Exception:
                pass


def _parse_kmb(raw: str) -> int:
    raw = (raw or "").strip().replace(",", "")
    if not raw:
        return 0
    mult = 1
    if raw[-1] in "kK":
        mult = 1_000
        raw = raw[:-1]
    elif raw[-1] in "mM":
        mult = 1_000_000
        raw = raw[:-1]
    try:
        return int(float(raw) * mult)
    except ValueError:
        return 0


def fetch_all_channels(
    cfg: dict,
    state,
    limit: int = 20,
) -> list[TelegramMessage]:
    """Convenience: poll every enabled Telegram channel using shared ``state``.

    Honors ``telegram_pipeline.channels`` entries with keys:
      - name: logical key used for the ``telegram_state`` row (dedupe).
      - username: the @username without the leading @ (used in t.me/s URL).
      - enabled: optional, defaults True.
    """
    results: list[TelegramMessage] = []
    for ch in cfg.get("channels", []):
        if not ch.get("enabled", True):
            continue
        name = ch.get("name") or ch.get("username")
        username = ch.get("username")
        if not name or not username:
            continue
        last = state.get_last_msg_id(name)
        # Use fresh session per channel to prevent Telegram tracking preview cookies & 302 redirecting
        session = _build_session()
        try:
            msgs = fetch_public_messages(username, last, limit=limit, session=session)
            for m in msgs:
                results.append(m)
            if msgs:
                state.set_last_msg_id(name, max(m.msg_id for m in msgs))
        finally:
            try:
                session.close()
            except Exception:
                pass
    return results
