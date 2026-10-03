"""
bot/reporting/morning_briefing.py

Assembles the morning briefing facts (see bot/reporting/data.py for exactly
how each number is computed), turns them into prose (bot/reporting/narrate.py),
and sends the result to Telegram. Run by the .github/workflows/morning_briefing.yml
schedule (7:00 AM IST = 01:30 UTC).

Run with:  python -m bot.reporting.morning_briefing
"""

import logging
import sys

import config
from bot.broker import AlpacaBroker
from bot.reporting import data
from bot.reporting.narrate import narrate_morning
from bot.reporting.telegram import send_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                     stream=sys.stdout)
logger = logging.getLogger("bot.reporting.morning_briefing")


def build_facts() -> dict:
    broker = AlpacaBroker()
    snapshot = data.gather_account_snapshot(broker)
    state = data.load_bot_state()
    trades = data.load_trades()

    y_start, y_end = data.utc_day_bounds(days_ago=1)
    yesterday_trades = data.trades_in_window(trades, y_start, y_end)

    from datetime import timedelta
    now = data.utc_day_bounds(days_ago=0)[0]
    week_start = now - timedelta(days=7)
    week_trades = data.trades_in_window(trades, week_start, now + timedelta(days=1))

    facts = {
        "equity": snapshot["equity"],
        "equity_market_open": snapshot["equity_market_open"],
        "positions": snapshot["positions"],
        "yesterday_total_pnl": sum(t["profit_loss"] for t in yesterday_trades),
        "yesterday_pnl_by_instrument": data.pnl_by_instrument(yesterday_trades),
        "market_conditions": data.market_conditions(broker),
        "win_rate_7d_pct": data.win_rate_pct(week_trades),
        "trades_7d_count": len(week_trades),
        "stop_proximity": data.position_stop_proximity(snapshot["positions"], state),
        "correlation_filter_blocking": data.correlation_filter_active(snapshot["positions"]),
        "drawdown_from_peak_pct": data.drawdown_from_peak_pct(
            snapshot["equity"], state.get("peak_equity"),
        ),
    }
    return facts


def main():
    facts = build_facts()
    text = narrate_morning(facts)
    logger.info("Morning briefing:\n%s", text)
    sent = send_telegram_message(text)
    if not sent:
        logger.error("Telegram send failed -- see TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID secrets.")
        sys.exit(1)


if __name__ == "__main__":
    main()
