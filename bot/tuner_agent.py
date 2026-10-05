"""
bot/tuner_agent.py

An OPT-IN (config.TUNER_AGENT_ENABLED, default False), advisory-only layer
on top of bot/auto_tune.py's existing deterministic 90-day backtest/
grid-search tuner.

Unlike a typical "LLM agent" wired into this script via an API key, the
actual reasoning here is done by a SCHEDULED CLAUDE SESSION (the same kind
of session this bot's README already uses for the Telegram morning/evening
reports) -- not by this script calling an LLM API itself. That session:
  1. Pulls the latest repo.
  2. Reads tuning_history.csv, strategy_params.json, and trades.csv itself
     (and may do its own web research on the traded symbols).
  3. Reasons about what extra parameter values might be worth trying, or
     flags a strategy that looks structurally broken.
  4. Writes a small raw JSON object (see RAW_SCHEMA_EXAMPLE below) to a
     file and runs `python3 -m bot.tuner_agent --apply <path>`.

This script's only job is to be the safety boundary that raw JSON has to
pass through before it can ever influence anything:
  - `apply_suggestions()` sanitizes it (`_sanitize_suggestions`): unknown
    strategy names, non-numeric values, and malformed flags are dropped,
    never raised.
  - It writes the sanitized result to tuner_agent_suggestions.json and
    appends an audit row to tuner_agent_log.csv, then commits/pushes both.
  - `load_suggested_candidates()` (called by bot/auto_tune.py) reads that
    file back. Every value it returns still gets re-clipped to the
    relevant parameter's hardcoded [lo, hi] bounds by auto_tune.py's
    `_clip()` before it can ever be backtested -- this script doesn't even
    need to know what those bounds are.
  - Nothing here can write strategy_params.json, touch
    bot/risk_manager.py, the correlation filter, the hard-stop logic, or
    the circuit breaker, or run while the circuit breaker is tripped.

With TUNER_AGENT_ENABLED=false (the default), or if no suggestions file
has ever been written, bot/auto_tune.py behaves byte-for-byte identically
to a world where this module doesn't exist.

Run standalone with:   python3 -m bot.tuner_agent --apply <path-to-raw.json>

RAW_SCHEMA_EXAMPLE (what the scheduled session writes before calling
--apply; see README "Tuner agent (LLM advisory layer)" for the full
tunable-parameter list):
{
  "strategy_suggestions": {
    "momentum_breakout": {"volume_multiple": [1.5, 1.8]},
    "mean_reversion": {"entry_std_dev.SPY": [2.4, 3.0]},
    "trend_following": {"fast_ema.GLD": [30, 40], "trailing_stop_atr_multiple.GLD": [2.0, 2.5]}
  },
  "flags": [
    {"strategy": "momentum_breakout", "concern": "score stayed deeply negative across 3 cycles"}
  ],
  "note": "one short sentence summarizing the reasoning"
}

IMPORTANT for per-symbol parameters -- mean_reversion's entry_std_dev
({"entry_std_dev": {"SPY": ..., "QQQ": ...}}) AND, as of 2026-10-05,
trend_following's fast_ema/slow_ema/trailing_stop_atr_multiple
({"fast_ema": {"GLD": ..., "USO": ...}}, etc. -- split because GLD and USO
had opposite-sign expectancy on the same shared params; see
config.TREND_FOLLOWING_PARAMS for the full writeup): the key here is NOT
the bare parameter name. It's the dotted path auto_tune.py's TUNE_SPECS
actually tunes -- "entry_std_dev.SPY", "fast_ema.GLD", "fast_ema.USO",
"slow_ema.GLD", "slow_ema.USO", "trailing_stop_atr_multiple.GLD",
"trailing_stop_atr_multiple.USO" -- because load_suggested_candidates()
below looks up `".".join(param_path)` against whatever auto_tune.py
passes it (e.g. `("entry_std_dev", "SPY")` -> "entry_std_dev.SPY",
`("fast_ema", "GLD")` -> "fast_ema.GLD"). A suggestion filed under a bare
key like "fast_ema" (no symbol suffix) is not malformed -- it parses fine
-- it just never matches any lookup auto_tune.py actually makes for these
per-symbol parameters, so it silently never gets used. momentum_breakout's
params (lookback, volume_multiple, trailing_stop_atr_multiple) remain the
only single-valued (not per-symbol) ones in this codebase, since it trades
just one symbol (BTC/USD) -- their key is just the bare name, e.g.
"volume_multiple".
"""

import csv
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import config
from bot.backtest import STRATEGY_CLASSES
from bot.git_utils import commit_and_push_files, timestamped_commit_message
from bot.state_utils import is_trading_halted

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot.tuner_agent")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_FIELDS = ["timestamp", "suggestions_kept", "flags_kept", "note"]

KNOWN_STRATEGIES = set(STRATEGY_CLASSES.keys())


# ===========================================================================
# Public loader -- the only function bot/auto_tune.py calls
# ===========================================================================

def load_suggested_candidates(strategy_name: str, param_path: Tuple[str, ...],
                               suggestions_path: Optional[str] = None) -> List[float]:
    """
    Returns whatever numeric candidate values were last applied for this
    exact (strategy, parameter path), or [] on ANY failure condition: the
    feature is disabled, the file doesn't exist, it's malformed JSON, the
    strategy/param isn't in it, or a value isn't numeric. This function
    must never raise -- a broken suggestions file should degrade to "no
    suggestions," never to a crashed tuning run.

    Deliberately does not validate `strategy_name`/`param_path` against
    bot.auto_tune.TUNE_SPECS (that would require importing auto_tune.py,
    which imports this module -- a cycle). It doesn't need to: auto_tune.py
    only ever calls this with its own known slot paths, so a suggestion for
    anything else is simply never looked up, and every value returned here
    still gets clipped to that slot's hardcoded bounds by auto_tune.py's
    _clip() before it can ever be evaluated.
    """
    if not config.TUNER_AGENT_ENABLED:
        return []

    path = suggestions_path or config.TUNER_AGENT_SUGGESTIONS_PATH
    try:
        with open(path, "r") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []

    if not isinstance(data, dict):
        return []

    strategy_suggestions = data.get("strategy_suggestions")
    if not isinstance(strategy_suggestions, dict):
        return []

    per_strategy = strategy_suggestions.get(strategy_name)
    if not isinstance(per_strategy, dict):
        return []

    key = ".".join(param_path)
    values = per_strategy.get(key)
    if not isinstance(values, list):
        return []

    result = []
    for v in values:
        if isinstance(v, bool):  # bool is an int subclass; exclude explicitly
            continue
        if isinstance(v, (int, float)):
            result.append(float(v))
    return result


# ===========================================================================
# Sanitization: the untrusted-input boundary. Anything that doesn't pass
# this is silently dropped (with a log line), never raised. The raw JSON
# handed to apply_suggestions() came from a scheduled Claude session's own
# reasoning over repo data (and possibly web research) -- still untrusted
# input from this script's point of view, sanitized the same way an LLM
# API response would be.
# ===========================================================================

def _sanitize_suggestions(raw: Optional[dict]) -> dict:
    clean_suggestions: Dict[str, Dict[str, List[float]]] = {}
    clean_flags: List[dict] = []

    if not isinstance(raw, dict):
        return {"strategy_suggestions": clean_suggestions, "flags": clean_flags, "note": ""}

    raw_suggestions = raw.get("strategy_suggestions")
    if isinstance(raw_suggestions, dict):
        for strategy_name, params in raw_suggestions.items():
            if strategy_name not in KNOWN_STRATEGIES or not isinstance(params, dict):
                continue
            clean_params: Dict[str, List[float]] = {}
            for param_key, values in params.items():
                if not isinstance(param_key, str) or not isinstance(values, list):
                    continue
                numeric = [
                    float(v) for v in values
                    if isinstance(v, (int, float)) and not isinstance(v, bool)
                ]
                if numeric:
                    clean_params[param_key] = numeric
            if clean_params:
                clean_suggestions[strategy_name] = clean_params

    raw_flags = raw.get("flags")
    if isinstance(raw_flags, list):
        for flag in raw_flags:
            if (isinstance(flag, dict)
                    and isinstance(flag.get("strategy"), str)
                    and flag.get("strategy") in KNOWN_STRATEGIES
                    and isinstance(flag.get("concern"), str)):
                clean_flags.append({"strategy": flag["strategy"], "concern": flag["concern"]})

    note = raw.get("note") if isinstance(raw.get("note"), str) else ""

    return {"strategy_suggestions": clean_suggestions, "flags": clean_flags, "note": note}


# ===========================================================================
# Output writing
# ===========================================================================

def _write_suggestions_file(path: str, sanitized: dict) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **sanitized,
    }
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, path)


def _append_log_row(log_path: str, row: dict) -> None:
    file_exists = os.path.exists(log_path)
    with open(log_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


# ===========================================================================
# Orchestration: apply a raw suggestions dict (produced by a scheduled
# Claude session's own reasoning, not by this script)
# ===========================================================================

def apply_suggestions(
    raw: dict,
    suggestions_path: Optional[str] = None,
    log_path: Optional[str] = None,
    skip_git: bool = False,
) -> dict:
    """
    Sanitizes `raw` and, if the feature is enabled and trading isn't
    halted, writes it to tuner_agent_suggestions.json + appends an audit
    row to tuner_agent_log.csv, then commits/pushes both. Returns a summary
    dict describing what happened. Never raises on malformed `raw` --
    worst case, zero suggestions get written.
    """
    suggestions_path = suggestions_path or config.TUNER_AGENT_SUGGESTIONS_PATH
    log_path = log_path or config.TUNER_AGENT_LOG_PATH

    if not config.TUNER_AGENT_ENABLED:
        logger.info("TUNER_AGENT_ENABLED is false; tuner agent apply is a no-op.")
        return {"ran": False, "reason": "disabled"}

    if is_trading_halted():
        logger.warning(
            "Circuit breaker is tripped (bot_state.json trading_halted=true); "
            "tuner agent refusing to apply suggestions. No-op."
        )
        return {"ran": False, "reason": "circuit_breaker_tripped"}

    sanitized = _sanitize_suggestions(raw)
    suggestions_kept = sum(
        len(values)
        for params in sanitized["strategy_suggestions"].values()
        for values in params.values()
    )
    flags_kept = len(sanitized["flags"])

    _write_suggestions_file(suggestions_path, sanitized)
    _append_log_row(log_path, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "suggestions_kept": suggestions_kept,
        "flags_kept": flags_kept,
        "note": sanitized["note"],
    })

    logger.info(
        "Tuner agent suggestions applied: suggestions_kept=%d, flags_kept=%d.",
        suggestions_kept, flags_kept,
    )
    for flag in sanitized["flags"]:
        logger.warning("Tuner agent flag [%s]: %s", flag["strategy"], flag["concern"])

    if not skip_git:
        commit_and_push_files(
            [os.path.relpath(suggestions_path, REPO_ROOT), os.path.relpath(log_path, REPO_ROOT)],
            timestamped_commit_message("Tuner agent suggestions"),
        )

    return {
        "ran": True,
        "suggestions_kept": suggestions_kept,
        "flags": sanitized["flags"],
    }


def main():
    if len(sys.argv) != 3 or sys.argv[1] != "--apply":
        print("Usage: python3 -m bot.tuner_agent --apply <path-to-raw-suggestions.json>",
              file=sys.stderr)
        sys.exit(2)

    raw_path = sys.argv[2]
    try:
        with open(raw_path, "r") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
        logger.error("Could not read/parse %s: %s", raw_path, e)
        sys.exit(1)

    logger.info("=== tuner_agent apply start (%s) ===", raw_path)
    result = apply_suggestions(raw)
    logger.info("=== tuner_agent apply end: %s ===", result)


if __name__ == "__main__":
    main()
