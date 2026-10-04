"""
bot/state_utils.py

Tiny shared helper for reading bot_state.json's "trading_halted" flag,
factored out of bot/auto_tune.py so bot/tuner_agent.py can apply the exact
same guard (never do anything while the max-drawdown circuit breaker is
tripped) without duplicating the read/parse logic.
"""

import json
import logging
import os

import config

logger = logging.getLogger(__name__)


def is_trading_halted(state_file_path: str = None) -> bool:
    state_file_path = state_file_path or config.STATE_FILE_PATH
    if not os.path.exists(state_file_path):
        return False
    try:
        with open(state_file_path, "r") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Could not read %s (%s); assuming NOT halted.", state_file_path, e)
        return False
    return bool(state.get("trading_halted", False))
