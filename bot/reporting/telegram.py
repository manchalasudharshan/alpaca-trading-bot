"""
bot/reporting/telegram.py

Minimal Telegram Bot API client -- just enough to push a text message to
one chat. No SDK dependency; this is a single POST to the bot's
sendMessage endpoint.

Setup (one-time, by the account owner):
  1. Message @BotFather on Telegram, send /newbot, follow the prompts.
     BotFather replies with a token like "123456789:AAFk...". That's
     TELEGRAM_BOT_TOKEN.
  2. Send any message to your new bot from the Telegram account/group that
     should receive reports.
  3. Visit https://api.telegram.org/bot<token>/getUpdates in a browser (or
     curl it) and find "chat":{"id": ...} in the JSON -- that's
     TELEGRAM_CHAT_ID (a negative number for a group chat).
  4. Add both as GitHub Actions repo secrets (same place as the Alpaca
     keys): Settings -> Secrets and variables -> Actions.
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"


def send_telegram_message(text: str, token: str = None, chat_id: str = None) -> bool:
    token = token or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.error(
            "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set -- cannot send Telegram "
            "message. Printing report to stdout instead:\n%s", text,
        )
        return False

    url = f"{TELEGRAM_API_BASE}/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=15,
        )
        resp.raise_for_status()
        ok = resp.json().get("ok", False)
        if ok:
            logger.info("Sent Telegram message (%d chars).", len(text))
        else:
            logger.error("Telegram API returned ok=False: %s", resp.text)
        return ok
    except Exception as e:
        logger.error("Failed to send Telegram message: %s", e)
        return False
