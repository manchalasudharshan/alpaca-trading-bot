"""
tests/test_tuner_agent.py

Covers bot/tuner_agent.py (the opt-in, advisory-only LLM layer on top of
bot/auto_tune.py's deterministic tuner) and its one integration point with
bot/auto_tune.py:

  1. load_suggested_candidates() returns [] (never raises) for every
     failure mode: disabled, missing file, malformed JSON, wrong shape,
     non-numeric values -- and returns the right numbers when everything
     is well-formed.
  2. _sanitize_suggestions() drops anything untrusted: unknown strategy
     names, non-numeric values, malformed flags, non-dict/non-list shapes
     -- it never raises on garbage input.
  3. _parse_llm_json() tolerates a markdown-fenced response and returns
     None (not an exception) on invalid JSON.
  4. generate_suggestions() end-to-end: no-ops when disabled or when the
     circuit breaker is tripped; writes an empty-but-valid suggestions file
     when the LLM call fails; writes sanitized suggestions when it
     succeeds. Never touches git (skip_git=True) or the network (the LLM
     call is injected).
  5. THE critical safety integration test: bot.auto_tune._tune_strategy()
     merges in agent-suggested candidates, but ALWAYS re-clips them to the
     same hardcoded bounds every other candidate uses -- an agent
     suggestion wildly outside a parameter's safe range can never reach
     the backtest evaluator unclipped.
  6. Regression guarantee: with the feature disabled (the default), a
     tuning run behaves identically to a world where bot/tuner_agent.py
     never existed.
"""

import json

import pytest

import config
from bot import auto_tune, tuner_agent


# ===========================================================================
# 1. load_suggested_candidates()
# ===========================================================================

class TestLoadSuggestedCandidates:
    def test_returns_empty_when_disabled(self, tmp_path, monkeypatch):
        suggestions_file = tmp_path / "suggestions.json"
        suggestions_file.write_text(json.dumps({
            "strategy_suggestions": {"momentum_breakout": {"volume_multiple": [1.5]}},
        }))
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", False)

        result = tuner_agent.load_suggested_candidates(
            "momentum_breakout", ("volume_multiple",), suggestions_path=str(suggestions_file),
        )
        assert result == []

    def test_returns_empty_when_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        missing = tmp_path / "does_not_exist.json"

        result = tuner_agent.load_suggested_candidates(
            "momentum_breakout", ("volume_multiple",), suggestions_path=str(missing),
        )
        assert result == []

    def test_returns_empty_on_malformed_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        bad_file = tmp_path / "bad.json"
        bad_file.write_text("{not valid json!!")

        result = tuner_agent.load_suggested_candidates(
            "momentum_breakout", ("volume_multiple",), suggestions_path=str(bad_file),
        )
        assert result == []

    @pytest.mark.parametrize("payload", [
        [],  # top-level must be a dict
        {"strategy_suggestions": "not-a-dict"},
        {"strategy_suggestions": {"momentum_breakout": "not-a-dict"}},
        {"strategy_suggestions": {"momentum_breakout": {"volume_multiple": "not-a-list"}}},
    ])
    def test_returns_empty_on_wrong_shape(self, tmp_path, monkeypatch, payload):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        f = tmp_path / "suggestions.json"
        f.write_text(json.dumps(payload))

        result = tuner_agent.load_suggested_candidates(
            "momentum_breakout", ("volume_multiple",), suggestions_path=str(f),
        )
        assert result == []

    def test_ignores_non_numeric_and_bool_values(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        f = tmp_path / "suggestions.json"
        f.write_text(json.dumps({
            "strategy_suggestions": {
                "momentum_breakout": {"volume_multiple": [1.8, "two point two", True, None, 2.4]},
            },
        }))

        result = tuner_agent.load_suggested_candidates(
            "momentum_breakout", ("volume_multiple",), suggestions_path=str(f),
        )
        assert result == [1.8, 2.4]

    def test_returns_correct_values_for_nested_param_path(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        f = tmp_path / "suggestions.json"
        f.write_text(json.dumps({
            "strategy_suggestions": {
                "mean_reversion": {"entry_std_dev.SPY": [2.0, 2.5]},
            },
        }))

        result = tuner_agent.load_suggested_candidates(
            "mean_reversion", ("entry_std_dev", "SPY"), suggestions_path=str(f),
        )
        assert result == [2.0, 2.5]

    def test_unrelated_strategy_or_param_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        f = tmp_path / "suggestions.json"
        f.write_text(json.dumps({
            "strategy_suggestions": {"momentum_breakout": {"volume_multiple": [1.8]}},
        }))

        assert tuner_agent.load_suggested_candidates(
            "trend_following", ("fast_ema",), suggestions_path=str(f),
        ) == []
        assert tuner_agent.load_suggested_candidates(
            "momentum_breakout", ("lookback",), suggestions_path=str(f),
        ) == []


# ===========================================================================
# 2. _sanitize_suggestions()
# ===========================================================================

class TestSanitizeSuggestions:
    def test_none_input_produces_empty_safe_structure(self):
        result = tuner_agent._sanitize_suggestions(None)
        assert result == {"strategy_suggestions": {}, "flags": [], "note": ""}

    def test_unknown_strategy_name_dropped(self):
        raw = {"strategy_suggestions": {"totally_made_up_strategy": {"x": [1, 2]}}}
        result = tuner_agent._sanitize_suggestions(raw)
        assert result["strategy_suggestions"] == {}

    def test_known_strategy_with_mixed_valid_invalid_params_kept_partially(self):
        raw = {
            "strategy_suggestions": {
                "momentum_breakout": {
                    "volume_multiple": [1.5, 1.8],
                    "not_a_list": "oops",
                    "has_garbage": ["a", "b"],
                },
            },
        }
        result = tuner_agent._sanitize_suggestions(raw)
        assert result["strategy_suggestions"] == {"momentum_breakout": {"volume_multiple": [1.5, 1.8]}}

    def test_malformed_flags_dropped_well_formed_kept(self):
        raw = {
            "flags": [
                {"strategy": "momentum_breakout", "concern": "deeply unprofitable"},
                {"strategy": "not_a_real_strategy", "concern": "should be dropped"},
                {"strategy": "trend_following"},  # missing "concern"
                "just a string",  # not even a dict
            ],
        }
        result = tuner_agent._sanitize_suggestions(raw)
        assert result["flags"] == [{"strategy": "momentum_breakout", "concern": "deeply unprofitable"}]

    def test_garbage_top_level_types_never_raise(self):
        for garbage in ["a string", 42, [1, 2, 3], True]:
            result = tuner_agent._sanitize_suggestions(garbage)
            assert result == {"strategy_suggestions": {}, "flags": [], "note": ""}


# ===========================================================================
# 3. _parse_llm_json()
# ===========================================================================

class TestParseLlmJson:
    def test_plain_json(self):
        assert tuner_agent._parse_llm_json('{"a": 1}') == {"a": 1}

    def test_markdown_fenced_json_is_tolerated(self):
        text = '```json\n{"a": 1}\n```'
        assert tuner_agent._parse_llm_json(text) == {"a": 1}

    def test_invalid_json_returns_none_not_exception(self):
        assert tuner_agent._parse_llm_json("not json at all {{{") is None

    def test_none_input_returns_none(self):
        assert tuner_agent._parse_llm_json(None) is None

    def test_non_dict_json_returns_none(self):
        assert tuner_agent._parse_llm_json("[1, 2, 3]") is None


# ===========================================================================
# 4. generate_suggestions() end-to-end
# ===========================================================================

@pytest.fixture
def agent_paths(tmp_path):
    return {
        "suggestions_path": str(tmp_path / "tuner_agent_suggestions.json"),
        "log_path": str(tmp_path / "tuner_agent_log.csv"),
        "history_path": str(tmp_path / "tuning_history.csv"),
        "trades_csv_path": str(tmp_path / "trades.csv"),
        "params_file_path": str(tmp_path / "strategy_params.json"),
    }


class TestGenerateSuggestionsEndToEnd:
    def test_noop_when_disabled(self, agent_paths, monkeypatch):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", False)
        result = tuner_agent.generate_suggestions(skip_git=True, **agent_paths)
        assert result == {"ran": False, "reason": "disabled"}
        assert not __import__("os").path.exists(agent_paths["suggestions_path"])

    def test_noop_when_circuit_breaker_tripped(self, agent_paths, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        state_file = tmp_path / "bot_state.json"
        state_file.write_text(json.dumps({"trading_halted": True}))
        monkeypatch.setattr(config, "STATE_FILE_PATH", str(state_file))

        result = tuner_agent.generate_suggestions(skip_git=True, **agent_paths)
        assert result == {"ran": False, "reason": "circuit_breaker_tripped"}

    def test_llm_failure_writes_empty_valid_suggestions_file(self, agent_paths, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        monkeypatch.setattr(config, "STATE_FILE_PATH", str(tmp_path / "bot_state.json"))

        result = tuner_agent.generate_suggestions(
            call_llm_fn=lambda prompt: None, skip_git=True, **agent_paths,
        )
        assert result["ran"] is True
        assert result["llm_call_succeeded"] is False

        with open(agent_paths["suggestions_path"]) as f:
            data = json.load(f)
        assert data["strategy_suggestions"] == {}
        assert "generated_at" in data

    def test_llm_success_writes_sanitized_suggestions(self, agent_paths, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        monkeypatch.setattr(config, "STATE_FILE_PATH", str(tmp_path / "bot_state.json"))

        fake_response = json.dumps({
            "strategy_suggestions": {"momentum_breakout": {"volume_multiple": [1.5, 1.9]}},
            "flags": [{"strategy": "momentum_breakout", "concern": "thin volume feed"}],
            "note": "volume data looks unusually thin",
        })

        result = tuner_agent.generate_suggestions(
            call_llm_fn=lambda prompt: fake_response, skip_git=True, **agent_paths,
        )
        assert result["llm_call_succeeded"] is True
        assert result["suggestions_kept"] == 2
        assert result["flags"] == [{"strategy": "momentum_breakout", "concern": "thin volume feed"}]

        with open(agent_paths["suggestions_path"]) as f:
            data = json.load(f)
        assert data["strategy_suggestions"]["momentum_breakout"]["volume_multiple"] == [1.5, 1.9]

    def test_prompt_never_crashes_on_missing_context_files(self, agent_paths, monkeypatch, tmp_path):
        """None of history/trades/params files exist -- must degrade to empty
        context, not crash building the prompt."""
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        monkeypatch.setattr(config, "STATE_FILE_PATH", str(tmp_path / "bot_state.json"))

        captured = {}

        def fake_llm(prompt):
            captured["prompt"] = prompt
            return None

        tuner_agent.generate_suggestions(call_llm_fn=fake_llm, skip_git=True, **agent_paths)
        assert "prompt" in captured
        assert "strategy_suggestions" in captured["prompt"]  # the instructed output shape


# ===========================================================================
# 5. THE critical safety test: auto_tune always clips agent suggestions
# ===========================================================================

class TestAgentSuggestionsAlwaysClipped:
    def test_wildly_out_of_bounds_suggestion_is_clipped_before_evaluation(
        self, tmp_path, monkeypatch,
    ):
        """An agent suggestion far outside volume_multiple's [1.2, 3.5]
        bound must never reach _evaluate() unclipped -- bot.auto_tune._clip()
        must catch it regardless of source."""
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        suggestions_file = tmp_path / "suggestions.json"
        suggestions_file.write_text(json.dumps({
            "strategy_suggestions": {
                "momentum_breakout": {"volume_multiple": [999999.0, -500.0]},
            },
        }))
        monkeypatch.setattr(config, "TUNER_AGENT_SUGGESTIONS_PATH", str(suggestions_file))

        seen_values = []

        def fake_evaluate(strategy_name, params, bars_by_symbol, starting_equity, slippage_pct):
            seen_values.append(params["volume_multiple"])
            # Reward the largest value seen, to try to pull sizing toward the
            # (would-be) out-of-bounds suggestion if clipping ever failed.
            return params["volume_multiple"], 50, []

        monkeypatch.setattr(auto_tune, "_evaluate", fake_evaluate)

        baseline = dict(config.MOMENTUM_BREAKOUT_PARAMS)
        best_params, _, _, _ = auto_tune._tune_strategy(
            "momentum_breakout", baseline, bars_by_symbol={}, starting_equity=100_000.0,
            slippage_pct=0.0005,
        )

        lo, hi = dict((s.path, s.bounds) for s in auto_tune.TUNE_SPECS["momentum_breakout"])[("volume_multiple",)]
        assert lo <= best_params["volume_multiple"] <= hi
        # Every single value _evaluate() was ever called with for this
        # parameter must also have been within bounds -- proves the clip
        # happens BEFORE evaluation, not just on the final pick.
        assert all(lo <= v <= hi for v in seen_values)
        # And the huge/negative raw suggestions must have actually been
        # exercised (clipped to the boundary), proving the agent's values
        # were used at all, just safely.
        assert hi in seen_values or lo in seen_values

    def test_agent_candidate_still_subject_to_min_trades_gate(self, tmp_path, monkeypatch):
        """An agent-suggested value that produces too few trades must still
        be rejected, exactly like any mechanically-generated candidate."""
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", True)
        suggestions_file = tmp_path / "suggestions.json"
        suggestions_file.write_text(json.dumps({
            "strategy_suggestions": {"momentum_breakout": {"volume_multiple": [3.5]}},
        }))
        monkeypatch.setattr(config, "TUNER_AGENT_SUGGESTIONS_PATH", str(suggestions_file))

        def fake_evaluate(strategy_name, params, bars_by_symbol, starting_equity, slippage_pct):
            if params["volume_multiple"] == 3.5:
                return 999999.0, 1, []  # amazing score, but far below MIN_TRADES
            return 0.0, 50, []

        monkeypatch.setattr(auto_tune, "_evaluate", fake_evaluate)

        baseline = dict(config.MOMENTUM_BREAKOUT_PARAMS)
        best_params, best_score, best_trades, _ = auto_tune._tune_strategy(
            "momentum_breakout", baseline, bars_by_symbol={}, starting_equity=100_000.0,
            slippage_pct=0.0005,
        )
        assert best_params["volume_multiple"] != 3.5


# ===========================================================================
# 6. Regression: disabled-by-default behaves exactly like before this
#    feature existed
# ===========================================================================

class TestDisabledIsIdenticalToBeforeFeatureExisted:
    def test_no_suggestions_file_and_disabled_produce_identical_tuning(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "TUNER_AGENT_ENABLED", False)
        # Even if a suggestions file somehow exists, disabled must ignore it.
        suggestions_file = tmp_path / "suggestions.json"
        suggestions_file.write_text(json.dumps({
            "strategy_suggestions": {"momentum_breakout": {"volume_multiple": [3.3]}},
        }))
        monkeypatch.setattr(config, "TUNER_AGENT_SUGGESTIONS_PATH", str(suggestions_file))

        calls = []

        def fake_evaluate(strategy_name, params, bars_by_symbol, starting_equity, slippage_pct):
            calls.append(params["volume_multiple"])
            return 0.0, 50, []

        monkeypatch.setattr(auto_tune, "_evaluate", fake_evaluate)
        baseline = dict(config.MOMENTUM_BREAKOUT_PARAMS)
        auto_tune._tune_strategy(
            "momentum_breakout", baseline, bars_by_symbol={}, starting_equity=100_000.0,
            slippage_pct=0.0005,
        )
        assert 3.3 not in calls
