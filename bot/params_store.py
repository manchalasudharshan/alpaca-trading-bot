"""
bot/params_store.py

Tiny, defensive loader for `strategy_params.json` -- the externalized,
auto-tunable SIGNAL/INDICATOR parameters for each strategy (see
bot/auto_tune.py, which is the only writer of that file).

This module intentionally knows nothing about risk sizing, the correlation
filter, hard-stop logic, or the max-drawdown circuit breaker: those stay
hardcoded in risk_manager.py / config.py (RISK_PARAMS, MAX_DRAWDOWN_PCT) and
are never read from or written to strategy_params.json.

Design goals:
  - The bot must NEVER crash because of a missing, corrupt, or partially
    filled-in strategy_params.json. Any failure mode (file missing, invalid
    JSON, missing strategy key, missing individual param key) falls back to
    the matching config.py default for just the affected value(s).
  - Only keys that already exist in the config.py default dict are ever
    applied from the file, so a stray/unexpected key in the JSON can't
    inject something a strategy class doesn't expect.
  - Nested dicts (e.g. mean_reversion's per-symbol `entry_std_dev`) are
    merged key-by-key, not replaced wholesale, so a partial retune (e.g.
    only SPY's threshold changed) can't silently drop QQQ's value.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)

# strategy_params.json lives at the repo root (one level above bot/). Kept
# as a plain module attribute (not resolved inside a function with a cached
# default) so tests can monkeypatch it the same way tests/test_circuit_breaker.py
# monkeypatches config.STATE_FILE_PATH.
PARAMS_FILE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "strategy_params.json"
)


def load_strategy_params(strategy_name: str, defaults: dict) -> dict:
    """
    Returns a dict with the same keys as `defaults` (typically one of
    config.py's MEAN_REVERSION_PARAMS / MOMENTUM_BREAKOUT_PARAMS /
    TREND_FOLLOWING_PARAMS dicts), with any matching, well-formed values
    from strategy_params.json's `strategy_name` section applied on top.

    Never raises. On any problem, logs a warning and falls back to
    `defaults` for the affected value(s).
    """
    result = dict(defaults)

    try:
        with open(PARAMS_FILE_PATH, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.info(
            "%s not found; using config.py defaults for '%s'.",
            PARAMS_FILE_PATH, strategy_name,
        )
        return result
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(
            "Could not read/parse %s (%s); using config.py defaults for '%s'.",
            PARAMS_FILE_PATH, e, strategy_name,
        )
        return result

    if not isinstance(data, dict):
        logger.warning(
            "%s does not contain a JSON object; using config.py defaults for '%s'.",
            PARAMS_FILE_PATH, strategy_name,
        )
        return result

    strat_params = data.get(strategy_name)
    if not isinstance(strat_params, dict):
        logger.warning(
            "%s has no valid '%s' section; using config.py defaults.",
            PARAMS_FILE_PATH, strategy_name,
        )
        return result

    for key, default_value in defaults.items():
        if key not in strat_params:
            continue
        candidate = strat_params[key]
        try:
            if isinstance(default_value, dict) and isinstance(candidate, dict):
                merged = dict(default_value)
                merged.update(candidate)
                result[key] = merged
            else:
                result[key] = candidate
        except Exception as e:  # noqa: BLE001 -- never let a bad value crash strategy init
            logger.warning(
                "Ignoring invalid value for %s.%s in %s (%s); keeping config.py default.",
                strategy_name, key, PARAMS_FILE_PATH, e,
            )

    return result
