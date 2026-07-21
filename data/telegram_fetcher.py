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
    """Build an HTTP session using curl_cffi (preferred) or plain requests.

    curl_cffi bundles its own libcurl + cert bundle and impersonates a real
    browser TLS fingerprint, which bypasses the Windows OpenSSL SSL bugs that
    cause plain requests to hang on t.me.
    """
    try:
        from curl_cffi import requests as curl_requests
        s = curl_requests.Session(impersonate="chrome")
        return s
    except ImportError:
        logger.debug("curl_cffi unavailable; falling back to plain requests for Telegram fetcher")
    try:
        import requests
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "requests or curl_cffi is required for the Telegram Web Preview fetcher. "
            "Install with: pip install curl_cffi"
        ) from e
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


def fetch_public_messages(
    channel_username: str,
    last_msg_id: int = 0,
    limit: int = 100,
    session: Any | None = None,
    timeout: int = 20,
) -> list[TelegramMessage]:
    """Scrape public messages for ``channel_username`` with id > ``last_msg_id``.

    Uses ``https://t.me/s/{username}``. Returns messages newest-last, only those
    whose numeric id exceeds ``last_msg_id``. Returns an empty list on any
    network/parse failure so the pipeline can continue with other channels.
    """
    username = channel_username.lstrip("@")
    url = f"https://t.me/s/{username}"
    own_session = session is None
    if session is None:
        session = _build_session()

    try:
        from bs4 import BeautifulSoup
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "beautifulsoup4 is required for the Telegram Web Preview fetcher. "
            "Install it with: pip install beautifulsoup4"
        ) from e

    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code != 200:
            logger.warning("t.me/s/%s -> HTTP %s", username, resp.status_code)
            return []
        # encoding: curl_cffi response may not have apparent_encoding
        resp.encoding = getattr(resp, "apparent_encoding", None) or "utf-8"
        html = resp.text
    except Exception as e:  # pragma: no cover - network edge cases
        logger.error("Failed to fetch t.me/s/%s: %s", username, e)
        return []

    try:
        soup = BeautifulSoup(html, "html.parser")
        wraps = soup.select(".tgme_widget_message_wrap")
        out: list[TelegramMessage] = []
        for wrap in wraps:
            # data-post lives on the inner .tgme_widget_message element,
            # not on the wrapping container.
            msg_el = wrap.select_one(".tgme_widget_message")
            post_attr = (msg_el.get("data-post") or "") if msg_el else ""
            m = _POST_RE.match(post_attr)
            if not m:
                continue
            msg_id = int(m.group(1))
            if msg_id <= last_msg_id:
                continue

            text_el = wrap.select_one(".tgme_widget_message_text")
            text = text_el.get_text(separator="\n", strip=True) if text_el else ""

            time_el = wrap.select_one("time")
            date_val = time_el.get("datetime") if time_el else None

            views = 0
            views_el = wrap.select_one(".tgme_widget_message_views")
            if views_el:
                raw = views_el.get_text(strip=True)
                views = _parse_kmb(raw)

            forwards = 0
            fr_el = wrap.select_one(".tgme_widget_message_forwarded")
            if fr_el:
                raw = fr_el.get_text(strip=True)
                forwards = _parse_kmb(raw)

            out.append(
                TelegramMessage(
                    msg_id=msg_id,
                    text=text,
                    date=date_val,
                    channel=username,
                    views=views,
                    forwards=forwards,
                )
            )
            if len(out) >= limit:
                break
        return out
    except Exception as e:  # pragma: no cover - parse edge cases
        logger.error("Failed to parse t.me/s/%s: %s", username, e)
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
    limit: int = 100,
) -> list[TelegramMessage]:
    """Convenience: poll every enabled Telegram channel using shared ``state``.

    Honors ``telegram_pipeline.channels`` entries with keys:
      - name: logical key used for the ``telegram_state`` row (dedupe).
      - username: the @username without the leading @ (used in t.me/s URL).
      - enabled: optional, defaults True.
    """
    results: list[TelegramMessage] = []
    session = _build_session()
    try:
        for ch in cfg.get("channels", []):
            if not ch.get("enabled", True):
                continue
            name = ch.get("name") or ch.get("username")
            username = ch.get("username")
            if not name or not username:
                continue
            last = state.get_last_msg_id(name)
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
