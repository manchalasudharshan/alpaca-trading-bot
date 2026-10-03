"""
bot/live_tick.py

A single-cycle entry point, meant to be invoked by a scheduled process
(the "live-trading" GitHub Actions workflow runs this every few minutes)
rather than looping forever like bot/main.py's `run()`.

Why this exists: GitHub-hosted Actions runners cap a single job at a few
hours, far short of "run continuously for weeks." Instead, each invocation:
  1. Loads bot_state.json (open positions, last-seen bars, peak equity) --
     without this, a fresh process every tick would forget it has an open
     position and could double up.
  2. Runs exactly one trading cycle (TradingBot.run_once()).
  3. Saves bot_state.json back for the next tick.

The workflow then commits bot_state.json, trades.csv, daily_pnl.csv, and
bot.log back into the git repo, so the record is durable and inspectable
between runs (same pattern as bot/backtest.py's results/ commit).

Run with:  python -m bot.live_tick
"""

import logging
import sys

import config
from bot.main import TradingBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(config.BOT_LOG_PATH)],
)
logger = logging.getLogger("bot.live_tick")


def main():
    logger.info("=== live tick start ===")
    bot = TradingBot()
    bot.load_state(config.STATE_FILE_PATH)
    bot.run_once()
    bot.save_state(config.STATE_FILE_PATH)
    logger.info("=== live tick end (open positions: %s) ===",
                list(bot.portfolio.positions.keys()))


if __name__ == "__main__":
    main()
