"""
bot/log_setup.py

Single shared place to configure the root logger, used by every entrypoint
(bot/main.py's standalone `python -m bot.main` loop, and bot/live_tick.py's
single-cycle cron invocation).

Why this exists: Python's logging.basicConfig() is a no-op on every call
after the first one actually attaches handlers to the root logger. Before
this module existed, bot/main.py had its own hardcoded
logging.basicConfig(level=logging.INFO, ...) at import time. Since
live_tick.py does `from bot.main import TradingBot`, that import ran
bot/main.py's basicConfig() call FIRST -- which silently made
live_tick.py's own LOG_LEVEL-aware basicConfig() call do nothing at all,
no matter what LOG_LEVEL was set to. DEBUG logging added to the strategy
modules never appeared under LOG_LEVEL=DEBUG because of this.

Fix: both entrypoints now call setup_logging() from here instead of calling
logging.basicConfig() themselves. This function always passes force=True,
which tears down and replaces any existing root handlers -- so whichever
entrypoint runs (or whichever one calls this last) always wins, and
LOG_LEVEL is always honored regardless of import order.
"""

import logging
import os
import sys

import config


def setup_logging(logger_name: str) -> logging.Logger:
    """Configure the root logger from the LOG_LEVEL env var and return a
    named logger for the calling module.

    LOG_LEVEL defaults to INFO. An unset or invalid value (anything that
    isn't a real logging level name) falls back to INFO and logs a warning
    once the logger exists.
    """
    log_level_name = os.environ.get("LOG_LEVEL", "INFO").strip().upper()
    log_level = getattr(logging, log_level_name, None)
    invalid = not isinstance(log_level, int)
    if invalid:
        log_level = logging.INFO
        log_level_name = "INFO"

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(config.BOT_LOG_PATH),
        ],
        force=True,
    )

    logger = logging.getLogger(logger_name)
    if invalid:
        raw = os.environ.get("LOG_LEVEL")
        if raw:
            logger.warning("Invalid LOG_LEVEL=%r; falling back to INFO.", raw)
    return logger
