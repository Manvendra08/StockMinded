"""Notification utility for sending alerts via Telegram and Discord."""
from __future__ import annotations

import logging
import os
import requests
from typing import Optional
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

try:
    from config.loader import load_config
except ImportError:
    def load_config(): return {}

logger = logging.getLogger(__name__)

# Discord Color Constants (Decimal)
COLORS = {
    "BULLISH": 3066993,   # Emerald Green
    "BEARISH": 15158332,  # Alizarin Red
    "NEUTRAL": 9807270    # Asbestos Gray
}

def _env_or_value(value: str | None) -> str | None:
    if not value:
        return None
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1])
    return value

def _create_retry_session(retries=3, backoff_factor=1):
    session = requests.Session()
    retry = Retry(
        total=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=["POST"]
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    return session

def get_notifier_config() -> dict:
    """Retrieve notification settings from config.yaml."""
    cfg = load_config().get("notifications", {})
    return {
        "telegram": {
            "enabled": cfg.get("telegram", {}).get("enabled", False),
            "token": _env_or_value(cfg.get("telegram", {}).get("bot_token")),
            "chat_id": _env_or_value(cfg.get("telegram", {}).get("chat_id")),
        },
        "discord": {
            "enabled": cfg.get("discord", {}).get("enabled", False),
            "webhook_url": _env_or_value(cfg.get("discord", {}).get("webhook_url")),
        }
    }

def send_telegram_alert(message: str) -> bool:
    """Send a message via Telegram Bot API."""
    config = get_notifier_config()["telegram"]
    if not config["enabled"] or not config["token"] or not config["chat_id"]:
        return False

    url = f"https://api.telegram.org/bot{config['token']}/sendMessage"
    payload = {
        "chat_id": config["chat_id"],
        "text": message,
        "parse_mode": "HTML"
    }
    
    try:
        session = _create_retry_session()
        response = session.post(url, json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram alert failed: {e}")
        return False

def send_discord_alert(message: str, subject: Optional[str] = None, sentiment: str = "NEUTRAL") -> bool:
    """Send a message via Discord Webhook."""
    config = get_notifier_config()["discord"]
    if not config["enabled"] or not config["webhook_url"]:
        return False

    # Determine color based on sentiment
    color = COLORS.get(sentiment.upper(), COLORS["NEUTRAL"])
    
    # Create an Embed payload
    payload = {
        "embeds": [{
            "title": subject or "StockMinded Alert",
            "description": message,
            "color": color,
            "timestamp": None # Could add ISO timestamp here
        }]
    }

    try:
        session = _create_retry_session()
        response = session.post(config["webhook_url"], json=payload, timeout=10)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Discord alert failed: {e}")
        return False

def broadcast_alert(message: str, subject: Optional[str] = None, sentiment: str = "NEUTRAL"):
    """
    Send alert to all enabled channels.
    
    Args:
        message: The alert body text.
        subject: Optional title for the alert.
        sentiment: BULLISH, BEARISH, or NEUTRAL for color coding.
    """
    formatted_msg = f"<b>{subject}</b>\n\n{message}" if subject else message
    
    tg_status = send_telegram_alert(formatted_msg)
    
    # Pass raw text to Discord as Embeds handle formatting differently
    ds_status = send_discord_alert(message, subject=subject, sentiment=sentiment)
    
    if tg_status or ds_status:
        logger.info(f"Broadcast sent successfully (TG: {tg_status}, DS: {ds_status})")
    else:
        logger.warning("No alerts were sent. Check configuration.")