"""
tests/test_auto_tune.py

Covers bot/auto_tune.py:

  1. Per-parameter candidate generation respects the hardcoded bounds, even
     for wildly out-of-range starting values.
  2. Refuses to run (no-op) when bot_state.json's circuit breaker flag is
     tripped.
  3. Refuses to re-tune a strategy already tuned earlier the same UTC day
     (tuning_history.csv guard).
  4. Only adopts a new parameter set when it beats the baseline by at least
     the required margin -- a sub-margin improvement is found but not
     applied; a margin-beating improvement is applied.
  5. Never touches risk_manager.py, config.RISK_PARAMS, or
     config.MAX_DRAWDOWN_PCT -- the auto-tuner can only ever change
     strategy_params.json / tuning_history.csv.

None of these tests touch the network or git: bars_by_symbol is injected
directly (bypassing AlpacaBroker/fetch_all_bars) and skip_git=True bypasses
the git commit/push step.
"""

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

import config
from bot import auto_tune


REPO_FILES = {
    "risk_manager": "bot/risk_manager.py",
    "config": "config.py",
}


def _sha256(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture
def isolated_files(tmp_path, monkeypatch):
    """Redirects strategy_params.json / bot_state.json / tuning_history.csv
    to tmp_path so these tests never touch the real repo's files."""
    params_file = tmp_path / "strategy_params.json"
    params_file.write_text(json.dumps({
        "mean_reversion": dict(config.MEAN_REVERSION_PARAMS),
        "momentum_breakout": dict(config.MOMENTUM_BREAKOUT_PARAMS),
        "trend_following": dict(config.TREND_FOLLOWING_PARAMS),
    }))
    state_file = tmp_path / "bot_state.json"
    history_file = tmp_path / "tuning_history.csv"

    monkeypatch.setattr(auto_tune.params_store, "PARAMS_FILE_PATH", str(params_file))
    monkeypatch.setattr(config, "STATE_FILE_PATH", str(state_file))

    return {"params_file": params_file, "state_file": state_file, "history_file": history_file}


# ===========================================================================
# 1. Candidate generation respects bounds
# ===========================================================================

@pytest.mark.parametrize("value,bounds,is_int", [
    (20, (10, 60), True),
    (1000, (10, 60), True),       # way above bound
    (-50, (10, 60), True),        # way below bound
    (2.2, (1.2, 3.5), False),
    (0.0001, (1.0, 4.0), False),  # way below bound
    (999.0, (1.5, 5.0), False),   # way above bound
])
def test_candidates_respect_bounds(value, bounds, is_int):
    lo, hi = bounds
    candidates = auto_tune._candidates_for(value, bounds, is_int)
    assert len(candidates) >= 1
    for c in candidates:
        assert lo <= c <= hi


def test_tune_strategy_output_never_exceeds_bounds(isolated_files, monkeypatch):
    """End-to-end through _tune_strategy: even a scoring function that
    always rewards a larger trailing-stop multiple can't push the chosen
    value past its hardcoded upper bound."""
    def fake_evaluate(strategy_name, params, bars_by_symbol, starting_equity, slippage_pct):
        # Monotonically reward a larger trailing_stop_atr_multiple.
        return params["trailing_stop_atr_multiple"], 50, []

    monkeypatch.setattr(auto_tune, "_evaluate", fake_evaluate)

    baseline = dict(config.TREND_FOLLOWING_PARAMS)
    best_params, best_score, best_trades, _ = auto_tune._tune_strategy(
        "trend_following", baseline, bars_by_symbol={}, starting_equity=100_000.0, slippage_pct=0.0005,
    )

    lo, hi = dict((s.path, s.bounds) for s in auto_tune.TUNE_SPECS["trend_following"])[("trailing_stop_atr_multiple",)]
    assert lo <= best_params["trailing_stop_atr_multiple"] <= hi
    assert best_params["fast_ema"] < best_params["slow_ema"]  # structural constraint also held


# ===========================================================================
# 2. Circuit breaker gate
# ===========================================================================

def test_refuses_to_run_when_circuit_breaker_tripped(isolated_files):
    isolated_files["state_file"].write_text(json.dumps({"trading_halted": True}))

    before = isolated_files["params_file"].read_text()
    result = auto_tune.run_auto_tune(
        bars_by_symbol={}, skip_git=True, history_file_path=str(isolated_files["history_file"]),
    )

    assert result["halted"] is True
    assert result["results"] == []
    assert isolated_files["params_file"].read_text() == before  # untouched
    assert not isolated_files["history_file"].exists()  # no history row written either


def test_runs_normally_when_not_halted(isolated_files, monkeypatch):
    isolated_files["state_file"].write_text(json.dumps({"trading_halted": False}))
    monkeypatch.setattr(auto_tune, "_evaluate", lambda *a, **k: (0.0, 0, []))

    result = auto_tune.run_auto_tune(
        bars_by_symbol={}, skip_git=True, history_file_path=str(isolated_files["history_file"]),
    )

    assert result["halted"] is False
    assert {r["strategy"] for r in result["results"]} == set(auto_tune.STRATEGY_CLASSES)


# ===========================================================================
# 3. Once-per-day guard
# ===========================================================================

def test_refuses_to_run_twice_same_utc_day(isolated_files, monkeypatch):
    isolated_files["state_file"].write_text(json.dumps({"trading_halted": False}))
    today = datetime.now(timezone.utc)

    with open(isolated_files["history_file"], "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=auto_tune.HISTORY_FIELDS)
        writer.writeheader()
        for strategy_name in auto_tune.STRATEGY_CLASSES:
            writer.writerow({
                "timestamp": today.isoformat(), "strategy": strategy_name, "applied": False,
                "old_params": "{}", "new_params": "{}",
                "old_score": 0, "new_score": 0, "old_trades": 0, "new_trades": 0,
            })

    # If this ran for real it would call _evaluate; make sure it never does.
    def _boom(*a, **k):
        raise AssertionError("_evaluate should not be called when already tuned today")
    monkeypatch.setattr(auto_tune, "_evaluate", _boom)

    before = isolated_files["params_file"].read_text()
    result = auto_tune.run_auto_tune(
        bars_by_symbol={}, skip_git=True, history_file_path=str(isolated_files["history_file"]),
    )

    assert result["halted"] is False
    assert all(r.get("skipped") == "already_ran_today" for r in result["results"])
    assert isolated_files["params_file"].read_text() == before


def test_runs_again_on_a_new_utc_day(isolated_files, monkeypatch):
    isolated_files["state_file"].write_text(json.dumps({"trading_halted": False}))
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)

    with open(isolated_files["history_file"], "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=auto_tune.HISTORY_FIELDS)
        writer.writeheader()
        writer.writerow({
            "timestamp": yesterday.isoformat(), "strategy": "mean_reversion", "applied": False,
            "old_params": "{}", "new_params": "{}",
            "old_score": 0, "new_score": 0, "old_trades": 0, "new_trades": 0,
        })

    monkeypatch.setattr(auto_tune, "_evaluate", lambda *a, **k: (0.0, 0, []))
    result = auto_tune.run_auto_tune(
        bars_by_symbol={}, skip_git=True, history_file_path=str(isolated_files["history_file"]),
    )

    mr_result = next(r for r in result["results"] if r["strategy"] == "mean_reversion")
    assert mr_result.get("skipped") is None  # ran again today, not skipped


# ===========================================================================
# 4. Margin-threshold adoption logic
# ===========================================================================

def test_sub_margin_improvement_is_not_adopted(isolated_files, monkeypatch):
    isolated_files["state_file"].write_text(json.dumps({"trading_halted": False}))

    # Candidate lookback=23 (one of the generated candidates around the
    # default 20) scores only +0.5 better than baseline -- below the
    # required margin (max(1.0, baseline*0.10)) -- so it must NOT be adopted.
    def fake_evaluate(strategy_name, params, bars_by_symbol, starting_equity, slippage_pct):
        if strategy_name == "mean_reversion" and params.get("lookback") == 23:
            return 10.5, 20, []
        return 10.0, 20, []

    monkeypatch.setattr(auto_tune, "_evaluate", fake_evaluate)
    before = isolated_files["params_file"].read_text()

    result = auto_tune.run_auto_tune(
        bars_by_symbol={}, skip_git=True, history_file_path=str(isolated_files["history_file"]),
    )

    mr_result = next(r for r in result["results"] if r["strategy"] == "mean_reversion")
    assert mr_result["adopted"] is False
    assert isolated_files["params_file"].read_text() == before  # file untouched

    with open(isolated_files["history_file"]) as f:
        rows = list(csv.DictReader(f))
    mr_row = next(r for r in rows if r["strategy"] == "mean_reversion")
    assert mr_row["applied"] == "False"


def test_margin_beating_improvement_is_adopted_and_recorded(isolated_files, monkeypatch):
    isolated_files["state_file"].write_text(json.dumps({"trading_halted": False}))

    def fake_evaluate(strategy_name, params, bars_by_symbol, starting_equity, slippage_pct):
        if strategy_name == "mean_reversion" and params.get("lookback") == 23:
            return 12.0, 20, []  # +2.0 over baseline 10.0, well above the margin
        return 10.0, 20, []

    monkeypatch.setattr(auto_tune, "_evaluate", fake_evaluate)

    result = auto_tune.run_auto_tune(
        bars_by_symbol={}, skip_git=True, history_file_path=str(isolated_files["history_file"]),
    )

    mr_result = next(r for r in result["results"] if r["strategy"] == "mean_reversion")
    assert mr_result["adopted"] is True

    with open(isolated_files["params_file"]) as f:
        written = json.load(f)
    assert written["mean_reversion"]["lookback"] == 23

    with open(isolated_files["history_file"]) as f:
        rows = list(csv.DictReader(f))
    mr_row = next(r for r in rows if r["strategy"] == "mean_reversion")
    assert mr_row["applied"] == "True"
    assert json.loads(mr_row["new_params"])["lookback"] == 23
    assert json.loads(mr_row["old_params"])["lookback"] == 20


def test_min_trades_floor_blocks_adoption_even_with_a_great_score(isolated_files, monkeypatch):
    """A candidate that scores far better than baseline but on too few
    trades (a classic overfit/degenerate edge case) must not be adopted."""
    isolated_files["state_file"].write_text(json.dumps({"trading_halted": False}))

    def fake_evaluate(strategy_name, params, bars_by_symbol, starting_equity, slippage_pct):
        if strategy_name == "momentum_breakout" and params.get("lookback") != config.MOMENTUM_BREAKOUT_PARAMS["lookback"]:
            return 1000.0, 1, []  # huge score, but only 1 trade -- below MIN_TRADES
        return 10.0, 20, []

    monkeypatch.setattr(auto_tune, "_evaluate", fake_evaluate)
    before = isolated_files["params_file"].read_text()

    result = auto_tune.run_auto_tune(
        bars_by_symbol={}, skip_git=True, history_file_path=str(isolated_files["history_file"]),
    )

    mb_result = next(r for r in result["results"] if r["strategy"] == "momentum_breakout")
    assert mb_result["adopted"] is False
    assert isolated_files["params_file"].read_text() == before


# ===========================================================================
# 5. Never touches risk_manager.py / correlation filter / hard stops /
#    circuit breaker -- only strategy_params.json & tuning_history.csv.
# ===========================================================================

def test_never_touches_risk_manager_or_drawdown_config(isolated_files, monkeypatch):
    isolated_files["state_file"].write_text(json.dumps({"trading_halted": False}))

    risk_manager_hash_before = _sha256(REPO_FILES["risk_manager"])
    config_hash_before = _sha256(REPO_FILES["config"])
    risk_params_before = json.loads(json.dumps(config.RISK_PARAMS))  # deep copy via round-trip
    max_drawdown_before = config.MAX_DRAWDOWN_PCT

    def fake_evaluate(strategy_name, params, bars_by_symbol, starting_equity, slippage_pct):
        # Reward literally any change, across every strategy, to maximize
        # the chance something gets adopted -- the point is that even a
        # guaranteed-adopted change never touches the restricted files.
        if params != auto_tune.params_store.load_strategy_params(strategy_name, auto_tune.CONFIG_DEFAULTS[strategy_name]):
            return 1000.0, 50, []
        return 0.0, 50, []

    monkeypatch.setattr(auto_tune, "_evaluate", fake_evaluate)

    result = auto_tune.run_auto_tune(
        bars_by_symbol={}, skip_git=True, history_file_path=str(isolated_files["history_file"]),
    )
    assert any(r.get("adopted") for r in result["results"])  # sanity: a change really was adopted

    assert _sha256(REPO_FILES["risk_manager"]) == risk_manager_hash_before
    assert _sha256(REPO_FILES["config"]) == config_hash_before
    assert config.RISK_PARAMS == risk_params_before
    assert config.MAX_DRAWDOWN_PCT == max_drawdown_before

    # And strategy_params.json only ever gained the tunable keys, never any
    # risk-related key.
    with open(isolated_files["params_file"]) as f:
        written = json.load(f)
    for strategy_name, allowed_keys in auto_tune.TUNABLE_KEYS.items():
        assert set(written.get(strategy_name, {}).keys()) <= set(allowed_keys)
