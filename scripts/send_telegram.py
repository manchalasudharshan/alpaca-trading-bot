"""
scripts/send_telegram.py

A thin, dumb delivery pipe: takes a message and posts it to Telegram. This
exists ONLY because this project's dev sandbox has no outbound network
access to api.telegram.org (or to Alpaca) -- GitHub Actions runners do.

The actual morning/evening report *content* is written by a scheduled
Claude Cowork session (see README "Telegram reports" section), which reads
trades.csv / daily_pnl.csv / positions_snapshot.json straight out of this
git repo and composes the briefing itself. Once it has the finished text,
it triggers the "Send Telegram Message" GitHub Actions workflow
(.github/workflows/send_telegram.yml) with that text as the `message`
input, and this script does the one thing the sandbox can't: the actual
POST to Telegram.

Run with:  python scripts/send_telegram.py "message text"
       or: TELEGRAM_MESSAGE="message text" python scripts/send_telegram.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot.reporting.telegram import send_telegram_message  # noqa: E402


def main():
    message = sys.argv[1] if len(sys.argv) > 1 else os.getenv("TELEGRAM_MESSAGE", "")
    if not message.strip():
        print("No message provided (arg or TELEGRAM_MESSAGE env var).", file=sys.stderr)
        sys.exit(2)

    ok = send_telegram_message(message)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
