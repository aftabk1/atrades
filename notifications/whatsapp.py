"""
WhatsApp notifications via CallMeBot (free, no paid account required).

Setup (one-time):
  1. Add +34 644 59 57 88 to your WhatsApp contacts.
  2. Send: "I allow callmebot to send me messages"
  3. You will receive your API key by WhatsApp within a few minutes.
  4. Set WHATSAPP_PHONE and WHATSAPP_APIKEY in .env (or via the Config tab).

All errors are swallowed — a notification failure must never crash the trading loop.
"""

from __future__ import annotations

import urllib.parse
import urllib.request

from loguru import logger

_API_URL = "https://api.callmebot.com/whatsapp.php"


def notify(message: str) -> None:
    """Send a WhatsApp message. No-op if credentials are not configured."""
    try:
        import config
        phone  = getattr(config, "WHATSAPP_PHONE",  "").strip()
        apikey = getattr(config, "WHATSAPP_APIKEY", "").strip()
    except Exception:
        return

    if not phone or not apikey:
        return

    try:
        params = urllib.parse.urlencode({"phone": phone, "text": message, "apikey": apikey})
        url    = f"{_API_URL}?{params}"
        with urllib.request.urlopen(url, timeout=10) as resp:
            if not (200 <= resp.status < 300):
                logger.warning(f"WhatsApp notify: HTTP {resp.status}")
    except Exception as exc:
        logger.warning(f"WhatsApp notify failed (non-critical): {exc}")
