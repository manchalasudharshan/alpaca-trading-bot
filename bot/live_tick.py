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
  4. Writes positions_snapshot.json: Alpaca's own current positions,
     account equity, and market-open status. This is what lets the
     morning/evening Telegram reports -- written by a scheduled Claude
     Cowork session, not by this workflow -- know "current positions" and
     "current equity" without needing their own network route to Alpaca
     (the dev sandbox those sessions run in can reach GitHub but not
     api.alpaca.markets directly; this repo file is the bridge).

The workflow then commits bot_state.json, positions_snapshot.json,
trades.csv, and daily_pnl.csv back into the git repo, so the record is
durable and inspectable between runs (same pattern as bot/backtest.py's
results/ commit). bot.log is uploaded as a workflow artifact instead.

Run with:  python -m bot.live_tick
"""

import json
import logging
import sys
from datetime import datetime, timezone

import config
from bot.main import TradingBot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(config.BOT_LOG_PATH)],
)
logger = logging.getLogger("bot.live_tick")

POSITIONS_SNAPSHOT_PATH = "positions_snapshot.json"


def write_positions_snapshot(bot: TradingBot):
    try:
        positions = bot.broker.get_positions()
        equity = bot.broker.get_equity()
        market_open = bot.broker.is_equity_market_open()
    except Exception as e:
        logger.error("Failed to fetch account snapshot for positions_snapshot.json: %s", e)
        return

    snapshot = {
        "snapshot_at": datetime.now(timezone.utc).isoformat(),
        "equity": equity,
        "peak_equity": bot.peak_equity,
        "equity_market_open": market_open,
        "positions": positions,
    }
    with open(POSITIONS_SNAPSHOT_PATH, "w") as f:
        json.dump(snapshot, f, indent=2)
    logger.info("Wrote %s (%d open position(s), equity=%.2f)",
                POSITIONS_SNAPSHOT_PATH, len(positions), equity)


def main():
    logger.info("=== live tick start ===")
    bot = TradingBot()
    bot.load_state(config.STATE_FILE_PATH)
    bot.run_once()
    bot.save_state(config.STATE_FILE_PATH)
    write_positions_snapshot(bot)
    logger.info("=== live tick end (open positions: %s) ===",
                list(bot.portfolio.positions.keys()))


if __name__ == "__main__":
    main()
