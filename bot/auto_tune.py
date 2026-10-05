"""
bot/auto_tune.py

Fully automated, bounded re-tuning of each strategy's own SIGNAL/INDICATOR
parameters -- and *only* those. This script never touches, imports for
writing, or in any way modifies:

  - bot/risk_manager.py (1%-of-equity ATR position sizing, hard-stop-never-
    widens logic, the correlation filter), or
  - config.RISK_PARAMS / config.MAX_DRAWDOWN_PCT / the circuit-breaker code
    in bot/main.py.

Those five things are the user's own "non-negotiable" rules and stay fixed
regardless of what this script finds. The only thing this script is allowed
to change is strategy_params.json -- each strategy's own tunable
lookback/threshold/multiplier-style parameters (see bot/params_store.py),
each bounded per-parameter below so a bad backtest window can't walk a
parameter to a degenerate/overfit extreme (e.g. a 1-bar lookback or a
near-zero ATR multiple).

Run standalone with:   python3 -m bot.auto_tune

What one run does:
  1. Refuses to run at all if bot_state.json's "trading_halted" flag is set
     (the max-drawdown circuit breaker has tripped) -- no re-tuning while
     trading is halted; a human needs to look at that first.
  2. Per strategy, refuses to run again the same UTC calendar day (guarded
     by tuning_history.csv's last timestamp for that strategy) -- at most
     once/day.
  3. Pulls a trailing ~90-day window of historical bars by reusing
     bot.backtest.fetch_all_bars()/AlpacaBroker (no re-implemented data
     fetching), then backtests every candidate parameter set through
     bot.backtest.simulate_instrument()/compute_metrics() -- the exact same
     strategy, risk-sizing, and hard-stop code paths the live bot and
     bot/backtest.py already use.
  4. Does a small, bounded, per-parameter coordinate search (not a full
     grid product -- see TUNE_SPECS/_candidates_for): starting from the
     strategy's current live params, each tunable parameter is nudged
     through a handful of candidate values (others held fixed), keeping
     whichever single-parameter change scores best before moving to the
     next parameter.
  5. Scores every backtest with `score_metrics()` -- a simple, documented
     combination of the Sharpe ratio, total return, and a max-drawdown
     penalty, all of which bot/backtest.py's compute_metrics() already
     reports (no new risk framework invented here).
  6. Only adopts a new parameter set if it (a) produced at least MIN_TRADES
     trades across the strategy's instruments in this window, and (b) beats
     the current live params' score on the same window by at least
     MIN_IMPROVEMENT_MARGIN -- avoiding needless thrash on backtest noise.
  7. Always appends one audit row per strategy to tuning_history.csv
     (timestamp, old/new params, old/new metrics, whether it was applied),
     and writes any adopted change to strategy_params.json.
  8. Commits + pushes strategy_params.json/tuning_history.csv if either
     changed, using the same git identity and pull-rebase-retry-once
     pattern as scripts/vps_tick.sh.
"""

import copy
import csv
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import pandas as pd

import config
from bot import params_store, tuner_agent
from bot.backtest import (
    STRATEGY_CLASSES,
    BacktestMetrics,
    compute_metrics,
    fetch_all_bars,
    simulate_instrument,
)
from bot.broker import AlpacaBroker
from bot.git_utils import commit_and_push_files, timestamped_commit_message
from bot.state_utils import is_trading_halted

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot.auto_tune")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HISTORY_FILE_PATH = os.path.join(REPO_ROOT, "tuning_history.csv")
HISTORY_FIELDS = [
    "timestamp", "strategy", "applied", "old_params", "new_params",
    "old_score", "new_score", "old_trades", "new_trades",
]

# How far back to pull bars for the tuning backtest. ~90 days, matching
# bot.backtest.fetch_all_bars's `months` knob (months * 30 days).
TUNE_WINDOW_MONTHS = 3

# Minimum number of trades (summed across the strategy's instruments) a
# candidate parameter set must produce in the tuning window before it is
# even considered -- rejects degenerate/overfit candidates that "look"
# great purely because they almost never traded.
MIN_TRADES = 10

# A candidate must beat the current live params' score by at least this
# much (an absolute score margin, or this fraction of the baseline score's
# magnitude, whichever is larger) to be adopted -- avoids thrashing
# parameters on backtest noise for a marginal/no real improvement.
MIN_IMPROVEMENT_ABS_FLOOR = 1.0
MIN_IMPROVEMENT_REL = 0.10


# ===========================================================================
# Per-parameter bounds (hardcoded; never derived from a backtest result)
# ===========================================================================
#
# NOTE: all 5 instruments now trade on 5Min bars (config.INSTRUMENTS; see
# README "5-minute timeframe" note). The bar-count rationale below was
# originally written for the old per-strategy timeframes (15Min for mean
# reversion, 1Hour for momentum breakout, 4Hour for trend following) --
# e.g. "60 bars = 15 trading hours" assumed 15Min bars, and
# momentum_breakout.lookback was described "(hours)" because it used to be
# 1Hour bars. The bounds (the actual [lo, hi] numbers) are left unchanged
# here -- changing them is a tuning decision, not a mechanical consequence
# of the timeframe change -- but the number of *bars* each bound represents
# now covers a much shorter wall-clock span than when these comments were
# written (e.g. slow_ema=300 is 1500 minutes =~ 1 trading day at 5Min,
# vs. 1200 hours =~ 50 days at 4Hour). A 90-day tuning window (below)
# comfortably warms up every bound at 5Min granularity; it did not
# necessarily do so at the old granularity for the largest bounds.
#
# Rationale, briefly (full detail in README "Automated parameter tuning"):
#   - mean_reversion.lookback [10,60]: below 10 bars the SMA/stddev window is
#     statistically meaningless noise; above 60 bars it starts blending into
#     trend_filter_period, collapsing the strategy's two-timeframe design
#     into one.
#   - entry_std_dev (both symbols) [1.2,3.5]: below 1.2 the bands sit inside
#     ordinary price noise (overtrades); above 3.5 entries become so rare
#     the strategy barely trades at all -- a classic overfit-to-one-window
#     value.
#   - trend_filter_period [50,300]: below 50 it's not meaningfully slower
#     than the 20-period fast SMA; above 300 it barely updates within a
#     90-day tuning window (functionally frozen/overfit to one regime).
#   - momentum_breakout.lookback [10,60] (bars): below 10 loses the
#     "significant breakout" selectivity the strategy depends on; above 60
#     trade count collapses toward zero in a 90-day window.
#   - volume_multiple [1.2,3.5]: below 1.2x isn't really a confirmation
#     filter; above 3.5x almost never fires live.
#   - trailing_stop_atr_multiple: breakout [1.0,4.0], trend [1.5,5.0]: the
#     floor keeps the trailing stop meaningfully wider than the risk
#     manager's own 1-ATR hard stop (so it isn't a degenerate near-zero
#     stop); the ceiling keeps it from drifting so wide it stops behaving
#     like a stop.
#   - trend_following.fast_ema [10,120] / slow_ema [120,300], with fast
#     always kept strictly below slow for EACH symbol independently
#     (_params_valid below): keeps "fast" meaningfully faster than "slow";
#     at 5Min granularity even slow_ema=300 warms up well within the 90-day
#     tuning window (see NOTE above).
#   - trend_following params are per-symbol (GLD, USO) as of 2026-10-05: a
#     same-params/same-code timeframe comparison showed GLD losing money at
#     BOTH its original 4Hour design and the current 5Min live timeframe,
#     while USO on the exact same shared params was profitable at 5Min --
#     a params-fit problem specific to gold, not a timeframe one. With a
#     single shared scalar per parameter, this coordinate search could only
#     ever find one compromise value across both symbols, so fixing GLD's
#     negative expectancy risked dragging USO's good one down with it. See
#     config.TREND_FOLLOWING_PARAMS for the full writeup.


@dataclass(frozen=True)
class ParamSlot:
    path: Tuple[str, ...]      # e.g. ("lookback",) or ("entry_std_dev", "SPY")
    bounds: Tuple[float, float]
    is_int: bool


TUNE_SPECS: Dict[str, List[ParamSlot]] = {
    "mean_reversion": [
        ParamSlot(("lookback",), (10, 60), True),
        ParamSlot(("entry_std_dev", "SPY"), (1.2, 3.5), False),
        ParamSlot(("entry_std_dev", "QQQ"), (1.2, 3.5), False),
        ParamSlot(("trend_filter_period",), (50, 300), True),
    ],
    "momentum_breakout": [
        ParamSlot(("lookback",), (10, 60), True),
        ParamSlot(("volume_multiple",), (1.2, 3.5), False),
        ParamSlot(("trailing_stop_atr_multiple",), (1.0, 4.0), False),
    ],
    "trend_following": [
        ParamSlot(("fast_ema", "GLD"), (10, 120), True),
        ParamSlot(("fast_ema", "USO"), (10, 120), True),
        ParamSlot(("slow_ema", "GLD"), (120, 300), True),
        ParamSlot(("slow_ema", "USO"), (120, 300), True),
        ParamSlot(("trailing_stop_atr_multiple", "GLD"), (1.5, 5.0), False),
        ParamSlot(("trailing_stop_atr_multiple", "USO"), (1.5, 5.0), False),
    ],
}

# The subset of each strategy's params dict that is actually tunable/stored
# in strategy_params.json -- everything else (timeframe, atr_period, ...)
# stays wherever config.py puts it and is never written here.
TUNABLE_KEYS: Dict[str, List[str]] = {
    "mean_reversion": ["lookback", "entry_std_dev", "trend_filter_period"],
    "momentum_breakout": ["lookback", "volume_multiple", "trailing_stop_atr_multiple"],
    "trend_following": ["fast_ema", "slow_ema", "trailing_stop_atr_multiple"],
}

CONFIG_DEFAULTS = {
    "mean_reversion": config.MEAN_REVERSION_PARAMS,
    "momentum_breakout": config.MOMENTUM_BREAKOUT_PARAMS,
    "trend_following": config.TREND_FOLLOWING_PARAMS,
}


def _get(d: dict, path: Tuple[str, ...]):
    for k in path[:-1]:
        d = d[k]
    return d[path[-1]]


def _set(d: dict, path: Tuple[str, ...], value) -> None:
    for k in path[:-1]:
        d = d[k]
    d[path[-1]] = value


def _params_valid(strategy_name: str, params: dict) -> bool:
    """Rejects structurally-degenerate candidates the per-slot bounds alone
    don't catch (e.g. fast EMA >= slow EMA). trend_following's fast_ema/
    slow_ema are per-symbol dicts (GLD, USO independently tunable -- see
    config.TREND_FOLLOWING_PARAMS), so this constraint is checked per
    symbol: each symbol's own fast EMA must stay strictly below its own
    slow EMA, independent of the other symbol's values."""
    if strategy_name == "trend_following":
        fast = params.get("fast_ema", {})
        slow = params.get("slow_ema", {})
        if not isinstance(fast, dict) or not isinstance(slow, dict):
            return fast < slow if isinstance(fast, (int, float)) and isinstance(slow, (int, float)) else True
        for symbol in set(fast) | set(slow):
            if fast.get(symbol, 0) >= slow.get(symbol, 1):
                return False
    return True


def _clip(value, bounds: Tuple[float, float], is_int: bool):
    """Clamp `value` into `bounds` (inclusive), rounding/typing the same way
    _candidates_for() does. This is the single choke point every candidate
    -- mechanically generated or agent-suggested -- passes through before
    it can ever be evaluated, so nothing (including an LLM suggestion) can
    land outside a slot's hardcoded [lo, hi] range."""
    lo, hi = bounds
    v = int(round(value)) if is_int else round(float(value), 4)
    return max(lo, min(hi, v))


def _candidates_for(value, bounds: Tuple[float, float], is_int: bool,
                     n_each_side: int = 2, rel_step: float = 0.15) -> List[float]:
    """A small, bounded grid of candidate values around `value`: up to
    `n_each_side` steps in each direction, each step ~`rel_step` of the
    current value, clipped to `bounds` (which is also the hard floor/ceiling
    -- see the per-parameter rationale above)."""
    lo, hi = bounds
    step = abs(value) * rel_step if value else (1 if is_int else 0.1)
    step = max(step, 1) if is_int else max(step, 0.01)

    raw = set()
    for k in range(-n_each_side, n_each_side + 1):
        v = value + k * step
        v = int(round(v)) if is_int else round(v, 4)
        v = max(lo, min(hi, v))
        raw.add(v)
    return sorted(raw)


# ===========================================================================
# Scoring: a risk-adjusted combination of metrics bot.backtest already
# computes -- Sharpe ratio (primary), total return, and a max-drawdown
# penalty. No new risk framework; these three numbers come straight out of
# bot.backtest.compute_metrics().
# ===========================================================================

def score_metrics(metrics: BacktestMetrics) -> float:
    return metrics.sharpe_ratio * 100.0 + metrics.total_return_pct - abs(metrics.max_drawdown_pct) * 0.5


def _evaluate(strategy_name: str, params: dict, bars_by_symbol: Dict[str, pd.DataFrame],
              starting_equity: float, slippage_pct: float) -> Tuple[float, int, List[BacktestMetrics]]:
    """Backtests `params` for every instrument this strategy trades (reusing
    bot.backtest.simulate_instrument/compute_metrics verbatim) and returns
    (average score across instruments, total trades, per-instrument metrics).
    """
    strategy_instance = STRATEGY_CLASSES[strategy_name](params=params)
    scores, metrics_list = [], []
    total_trades = 0

    for inst in config.INSTRUMENTS:
        if inst.strategy != strategy_name:
            continue
        bars = bars_by_symbol.get(inst.symbol)
        if bars is None or bars.empty:
            continue
        trades, curve = simulate_instrument(inst, bars, starting_equity, slippage_pct,
                                             strategy=strategy_instance)
        metrics = compute_metrics(
            label=inst.symbol, trades=trades, equity_curve=curve,
            starting_equity=starting_equity,
            trading_days_per_year=config.BACKTEST_PARAMS["trading_days_per_year"],
        )
        total_trades += metrics.total_trades
        scores.append(score_metrics(metrics))
        metrics_list.append(metrics)

    avg_score = sum(scores) / len(scores) if scores else float("-inf")
    return avg_score, total_trades, metrics_list


def _tune_strategy(strategy_name: str, baseline_params: dict,
                    bars_by_symbol: Dict[str, pd.DataFrame],
                    starting_equity: float, slippage_pct: float
                    ) -> Tuple[dict, float, int, List[BacktestMetrics]]:
    """Bounded per-parameter coordinate search. Returns the best params found
    (which may just be `baseline_params` unchanged) plus its score/trades/metrics."""
    best_params = copy.deepcopy(baseline_params)
    best_score, best_trades, best_metrics = _evaluate(
        strategy_name, best_params, bars_by_symbol, starting_equity, slippage_pct,
    )

    for slot in TUNE_SPECS.get(strategy_name, []):
        current_value = _get(best_params, slot.path)
        candidates = _candidates_for(current_value, slot.bounds, slot.is_int)

        # Optional LLM-advisory layer (bot/tuner_agent.py, opt-in via
        # config.TUNER_AGENT_ENABLED): if it suggested extra values to try
        # for this exact strategy/parameter, merge them in -- but ALWAYS
        # re-clipped to this slot's own hardcoded bounds, the same bounds
        # every other candidate here is clipped to. This is the safety
        # invariant: the agent can only ever widen the search within
        # already-approved bounds, never choose a value outside them, and
        # every merged-in candidate still has to survive the exact same
        # MIN_TRADES / improvement-margin adoption gate below as any other
        # candidate -- the agent proposes, it never adopts.
        agent_values = tuner_agent.load_suggested_candidates(strategy_name, slot.path)
        for raw_value in agent_values:
            clipped = _clip(raw_value, slot.bounds, slot.is_int)
            if clipped not in candidates:
                candidates.append(clipped)

        for candidate_value in candidates:
            if candidate_value == current_value:
                continue
            trial = copy.deepcopy(best_params)
            _set(trial, slot.path, candidate_value)
            if not _params_valid(strategy_name, trial):
                continue

            score, trades, metrics = _evaluate(
                strategy_name, trial, bars_by_symbol, starting_equity, slippage_pct,
            )
            if trades >= MIN_TRADES and score > best_score:
                best_params, best_score, best_trades, best_metrics = trial, score, trades, metrics

    return best_params, best_score, best_trades, best_metrics


# ===========================================================================
# Guards: circuit breaker + once-per-day
# ===========================================================================

def _load_last_run_dates(history_file_path: str) -> Dict[str, "datetime.date"]:
    """Per-strategy UTC date of its most recent tuning-history row, however
    that run turned out (applied or not) -- used for the once-per-day guard."""
    last_dates: Dict[str, "datetime.date"] = {}
    if not os.path.exists(history_file_path):
        return last_dates
    try:
        with open(history_file_path, "r", newline="") as f:
            for row in csv.DictReader(f):
                ts = row.get("timestamp")
                strat = row.get("strategy")
                if not ts or not strat:
                    continue
                try:
                    d = datetime.fromisoformat(ts).date()
                except ValueError:
                    continue
                if strat not in last_dates or d > last_dates[strat]:
                    last_dates[strat] = d
    except (OSError, csv.Error) as e:
        logger.warning("Could not read %s (%s); treating as no prior runs.", history_file_path, e)
    return last_dates


def _append_history_rows(history_file_path: str, rows: List[dict]) -> None:
    if not rows:
        return
    file_exists = os.path.exists(history_file_path)
    with open(history_file_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if not file_exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _improvement_margin(baseline_score: float) -> float:
    return max(MIN_IMPROVEMENT_ABS_FLOOR, abs(baseline_score) * MIN_IMPROVEMENT_REL)


# ===========================================================================
# strategy_params.json read/write (only the tunable subset per strategy)
# ===========================================================================

def _load_raw_params_file() -> dict:
    try:
        with open(params_store.PARAMS_FILE_PATH, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _write_raw_params_file(data: dict) -> None:
    tmp_path = params_store.PARAMS_FILE_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, params_store.PARAMS_FILE_PATH)


# ===========================================================================
# Git commit + push (bot/git_utils.py -- same identity/pattern as
# scripts/vps_tick.sh, shared with bot/tuner_agent.py)
# ===========================================================================

def _git_commit_and_push() -> None:
    commit_and_push_files(
        ["strategy_params.json", "tuning_history.csv"],
        timestamped_commit_message("Auto-tune strategy params"),
    )


# ===========================================================================
# Orchestration
# ===========================================================================

def run_auto_tune(bars_by_symbol: Optional[Dict[str, pd.DataFrame]] = None,
                   skip_git: bool = False,
                   history_file_path: Optional[str] = None) -> dict:
    """
    Runs one full auto-tune pass. Separated from main() so tests can inject
    synthetic `bars_by_symbol` (no network) and skip the git step.

    Returns a summary dict: {"halted": bool, "results": [per-strategy dicts]}.
    """
    history_file_path = history_file_path or HISTORY_FILE_PATH

    if is_trading_halted():
        logger.warning(
            "Circuit breaker is tripped (bot_state.json trading_halted=true); "
            "refusing to re-tune while trading is halted. No-op."
        )
        return {"halted": True, "results": []}

    last_run_dates = _load_last_run_dates(history_file_path)
    today = datetime.now(timezone.utc).date()

    if bars_by_symbol is None:
        logger.info("Connecting to Alpaca to fetch %d months of history for tuning...",
                    TUNE_WINDOW_MONTHS)
        broker = AlpacaBroker()
        bars_by_symbol = fetch_all_bars(broker, months=TUNE_WINDOW_MONTHS)

    starting_equity = config.BACKTEST_PARAMS["starting_equity"]
    slippage_pct = config.BACKTEST_PARAMS["slippage_pct"]

    raw_params_file = _load_raw_params_file()
    file_changed = False
    history_rows: List[dict] = []
    results: List[dict] = []

    for strategy_name in STRATEGY_CLASSES:
        last_run_date = last_run_dates.get(strategy_name)
        if last_run_date is not None and last_run_date >= today:
            logger.info("%s was already tuned today (%s); skipping (max once/day).",
                        strategy_name, last_run_date)
            results.append({"strategy": strategy_name, "skipped": "already_ran_today"})
            continue

        baseline_params = params_store.load_strategy_params(strategy_name, CONFIG_DEFAULTS[strategy_name])
        baseline_score, baseline_trades, _ = _evaluate(
            strategy_name, baseline_params, bars_by_symbol, starting_equity, slippage_pct,
        )

        best_params, best_score, best_trades, _ = _tune_strategy(
            strategy_name, baseline_params, bars_by_symbol, starting_equity, slippage_pct,
        )

        margin = _improvement_margin(baseline_score)
        changed_from_baseline = any(
            _get(best_params, slot.path) != _get(baseline_params, slot.path)
            for slot in TUNE_SPECS.get(strategy_name, [])
        )
        adopt = (
            changed_from_baseline
            and best_trades >= MIN_TRADES
            and best_score >= baseline_score + margin
        )

        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "strategy": strategy_name,
            "applied": adopt,
            "old_params": json.dumps({k: baseline_params[k] for k in TUNABLE_KEYS[strategy_name]}, sort_keys=True),
            "new_params": json.dumps(
                {k: (best_params if adopt else baseline_params)[k] for k in TUNABLE_KEYS[strategy_name]},
                sort_keys=True,
            ),
            "old_score": round(baseline_score, 4),
            "new_score": round(best_score if adopt else baseline_score, 4),
            "old_trades": baseline_trades,
            "new_trades": best_trades if adopt else baseline_trades,
        }
        history_rows.append(row)
        results.append({"strategy": strategy_name, "adopted": adopt, **row})

        if adopt:
            logger.info(
                "%s: ADOPTING new params (score %.3f -> %.3f, margin required %.3f, trades=%d).",
                strategy_name, baseline_score, best_score, margin, best_trades,
            )
            raw_params_file[strategy_name] = {k: best_params[k] for k in TUNABLE_KEYS[strategy_name]}
            file_changed = True
        else:
            logger.info(
                "%s: keeping current params (best candidate score %.3f vs baseline %.3f, "
                "margin required %.3f, trades=%d) -- no change.",
                strategy_name, best_score, baseline_score, margin, best_trades,
            )

    _append_history_rows(history_file_path, history_rows)

    if file_changed:
        _write_raw_params_file(raw_params_file)
        if not skip_git:
            _git_commit_and_push()
    else:
        logger.info("No strategy params changed; strategy_params.json left untouched.")

    return {"halted": False, "results": results}


def main():
    logger.info("=== auto_tune start ===")
    run_auto_tune()
    logger.info("=== auto_tune end ===")


if __name__ == "__main__":
    main()
