"""
bot/tuner_agent.py

An OPT-IN (config.TUNER_AGENT_ENABLED, default False), advisory-only LLM
layer on top of bot/auto_tune.py's existing deterministic 90-day
backtest/grid-search tuner.

What this module is allowed to do: read tuning_history.csv, the current
strategy_params.json, a summary of live trades.csv, and (best-effort,
optional) recent news/market context from OpenBB, hand all of that to an
LLM, and record whatever numeric parameter values it suggests trying next
into tuner_agent_suggestions.json -- plus any qualitative "this strategy
looks structurally broken" flags into the same file, for a human to read.

What this module can NEVER do, by construction:
  - Write strategy_params.json. Only bot/auto_tune.py's own adoption logic
    (MIN_TRADES + the improvement-margin check, exactly as before this
    module existed) can do that.
  - Choose a parameter value outside that parameter's hardcoded [lo, hi]
    bounds. bot/auto_tune.py's `_clip()` re-clips every suggestion this
    module produces before it's ever evaluated -- this module doesn't even
    need to know what those bounds are.
  - Touch bot/risk_manager.py, the correlation filter, the hard-stop logic,
    or the max-drawdown circuit breaker. This module never imports or
    writes to any of those, same guarantee bot/auto_tune.py already makes.
  - Run while the circuit breaker is tripped (same guard as auto_tune.py).

In short: this module can only ever WIDEN auto_tune.py's search within
already-approved bounds and leave a human-readable note. It never adopts
anything, and a malformed/missing/garbage LLM response just means zero
suggestions get added -- auto_tune.py behaves identically to before this
module existed (see tests/test_tuner_agent.py).

Run standalone with:   python3 -m bot.tuner_agent
"""

import csv
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Tuple

import config
from bot import params_store
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
TUNING_HISTORY_PATH = os.path.join(REPO_ROOT, "tuning_history.csv")
TRADES_CSV_PATH_DEFAULT = os.path.join(REPO_ROOT, "trades.csv")
LOG_FIELDS = [
    "timestamp", "llm_call_succeeded", "suggestions_kept", "flags_kept", "note",
]

KNOWN_STRATEGIES = set(STRATEGY_CLASSES.keys())


# ===========================================================================
# Public loader -- the only function bot/auto_tune.py calls
# ===========================================================================

def load_suggested_candidates(strategy_name: str, param_path: Tuple[str, ...],
                               suggestions_path: Optional[str] = None) -> List[float]:
    """
    Returns whatever numeric candidate values the agent last suggested for
    this exact (strategy, parameter path), or [] on ANY failure condition:
    the feature is disabled, the file doesn't exist, it's malformed JSON,
    the strategy/param isn't in it, or a value isn't numeric. This function
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
# Context gathering (all best-effort; missing data degrades gracefully)
# ===========================================================================

def _load_recent_tuning_history(history_path: str, lookback_days: int) -> List[dict]:
    if not os.path.exists(history_path):
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    rows = []
    try:
        with open(history_path, "r", newline="") as f:
            for row in csv.DictReader(f):
                ts = row.get("timestamp")
                if not ts:
                    continue
                try:
                    if datetime.fromisoformat(ts) >= cutoff:
                        rows.append(row)
                except ValueError:
                    continue
    except (OSError, csv.Error) as e:
        logger.warning("Could not read %s (%s); proceeding with no tuning history context.",
                        history_path, e)
        return []
    return rows


def _load_trades_summary(trades_csv_path: str) -> dict:
    """A tiny, cheap summary -- not full trade data -- since live trades are
    currently sparse and this is just context for the LLM, not an input to
    any adoption decision."""
    if not os.path.exists(trades_csv_path):
        return {"total_closed_trades": 0}
    try:
        with open(trades_csv_path, "r", newline="") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error) as e:
        logger.warning("Could not read %s (%s); proceeding with no trades context.",
                        trades_csv_path, e)
        return {"total_closed_trades": 0}

    wins = sum(1 for r in rows if _safe_float(r.get("profit_loss")) > 0)
    losses = sum(1 for r in rows if _safe_float(r.get("profit_loss")) < 0)
    return {"total_closed_trades": len(rows), "wins": wins, "losses": losses}


def _safe_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _fetch_market_context(symbols: List[str]) -> dict:
    """Best-effort OpenBB enrichment (recent news headline counts per
    underlying symbol). Returns {} if the `openbb` package isn't installed,
    isn't configured with a data provider, or any call fails -- this is
    pure optional context for the LLM's reasoning, never something the
    pipeline depends on to function."""
    try:
        from openbb import obb  # optional dependency, not in requirements.txt
    except ImportError:
        logger.info("openbb not installed; proceeding without market/news context.")
        return {}

    context = {}
    for symbol in symbols:
        try:
            news = obb.news.company(symbol=symbol, limit=5)
            df = news.to_dataframe() if hasattr(news, "to_dataframe") else None
            headlines = list(df["title"])[:5] if df is not None and "title" in df else []
            context[symbol] = {"recent_headlines": headlines}
        except Exception as e:  # noqa: BLE001 -- any provider/network failure is non-fatal
            logger.info("OpenBB news fetch failed for %s (%s); skipping.", symbol, e)
    return context


def _load_current_params(params_file_path: str) -> dict:
    try:
        with open(params_file_path, "r") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


# ===========================================================================
# LLM call (isolated behind a function so tests never need a real API key
# or network access -- they monkeypatch this)
# ===========================================================================

PROMPT_TEMPLATE = """\
You are advising an automated parameter tuner for an algorithmic trading \
bot. You do NOT make trading decisions and you do NOT have authority to \
change anything directly -- you may only suggest extra candidate values \
for an existing, bounded, deterministic backtest search to try next. Every \
suggestion you give will be clipped into that parameter's existing safety \
range and will only be adopted if it beats the current parameters on a \
90-day backtest by a real margin with at least 10 trades. Nothing you say \
is applied directly.

Recent tuning history (last {lookback_days} days, one row per tuning run):
{tuning_history_json}

Current live strategy parameters:
{current_params_json}

Live trading summary so far:
{trades_summary_json}

Market/news context (best-effort, may be empty):
{market_context_json}

The tunable strategies and their tunable parameter keys are:
  - mean_reversion: lookback, entry_std_dev.SPY, entry_std_dev.QQQ, trend_filter_period
  - momentum_breakout: lookback, volume_multiple, trailing_stop_atr_multiple
  - trend_following: fast_ema, slow_ema, trailing_stop_atr_multiple

Respond with ONLY a single JSON object (no markdown fences, no commentary \
outside the JSON), in exactly this shape:
{{
  "strategy_suggestions": {{
    "<strategy_name>": {{
      "<param_key>": [<number>, <number>, ...]
    }}
  }},
  "flags": [
    {{"strategy": "<strategy_name>", "concern": "<short human-readable note>"}}
  ],
  "note": "<one short sentence summarizing your reasoning>"
}}
Only use strategy names and parameter keys from the list above. Omit a \
strategy entirely if you have no suggestion for it. Use an empty list for \
"flags" if you have none.
"""


def _call_llm(prompt: str) -> Optional[str]:
    """Returns the raw text response, or None on any failure (missing API
    key, network error, SDK not installed, timeout, etc.) -- callers must
    treat None as "no suggestions this run," never as something to retry
    aggressively or crash on."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set; skipping tuner-agent LLM call.")
        return None

    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic package not installed; skipping tuner-agent LLM call.")
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=config.TUNER_AGENT_MODEL,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
    except Exception as e:  # noqa: BLE001 -- any API/network failure is non-fatal here
        logger.warning("Tuner-agent LLM call failed (%s); skipping this run.", e)
        return None


def _parse_llm_json(raw_text: Optional[str]) -> Optional[dict]:
    if not raw_text:
        return None
    text = raw_text.strip()
    # Tolerate a model wrapping its JSON in a markdown code fence despite
    # being asked not to.
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("Tuner-agent LLM response was not valid JSON (%s); discarding.", e)
        return None
    return parsed if isinstance(parsed, dict) else None


# ===========================================================================
# Sanitization: the untrusted-input boundary. Anything that doesn't pass
# this is silently dropped (with a log line), never raised.
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
# Orchestration
# ===========================================================================

def generate_suggestions(
    call_llm_fn: Optional[Callable[[str], Optional[str]]] = None,
    suggestions_path: Optional[str] = None,
    log_path: Optional[str] = None,
    history_path: Optional[str] = None,
    trades_csv_path: Optional[str] = None,
    params_file_path: Optional[str] = None,
    skip_git: bool = False,
) -> dict:
    """
    Runs one full tuner-agent pass. Separated from main() so tests can
    inject a fake `call_llm_fn` (no network/API key) and isolated file
    paths. Returns a summary dict describing what happened.
    """
    suggestions_path = suggestions_path or config.TUNER_AGENT_SUGGESTIONS_PATH
    log_path = log_path or config.TUNER_AGENT_LOG_PATH
    history_path = history_path or TUNING_HISTORY_PATH
    trades_csv_path = trades_csv_path or getattr(config, "TRADES_CSV_PATH", TRADES_CSV_PATH_DEFAULT)
    params_file_path = params_file_path or params_store.PARAMS_FILE_PATH
    call_llm_fn = call_llm_fn or _call_llm

    if not config.TUNER_AGENT_ENABLED:
        logger.info("TUNER_AGENT_ENABLED is false; tuner agent is a no-op.")
        return {"ran": False, "reason": "disabled"}

    if is_trading_halted():
        logger.warning(
            "Circuit breaker is tripped (bot_state.json trading_halted=true); "
            "tuner agent refusing to run. No-op."
        )
        return {"ran": False, "reason": "circuit_breaker_tripped"}

    tuning_history = _load_recent_tuning_history(history_path, config.TUNER_AGENT_HISTORY_LOOKBACK_DAYS)
    current_params = _load_current_params(params_file_path)
    trades_summary = _load_trades_summary(trades_csv_path)
    symbols = sorted({inst.symbol for inst in config.INSTRUMENTS})
    market_context = _fetch_market_context(symbols)

    prompt = PROMPT_TEMPLATE.format(
        lookback_days=config.TUNER_AGENT_HISTORY_LOOKBACK_DAYS,
        tuning_history_json=json.dumps(tuning_history, indent=2),
        current_params_json=json.dumps(current_params, indent=2),
        trades_summary_json=json.dumps(trades_summary, indent=2),
        market_context_json=json.dumps(market_context, indent=2),
    )

    raw_text = call_llm_fn(prompt)
    llm_call_succeeded = raw_text is not None
    parsed = _parse_llm_json(raw_text)
    sanitized = _sanitize_suggestions(parsed)

    suggestions_kept = sum(
        len(values)
        for params in sanitized["strategy_suggestions"].values()
        for values in params.values()
    )
    flags_kept = len(sanitized["flags"])

    _write_suggestions_file(suggestions_path, sanitized)
    _append_log_row(log_path, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "llm_call_succeeded": llm_call_succeeded,
        "suggestions_kept": suggestions_kept,
        "flags_kept": flags_kept,
        "note": sanitized["note"],
    })

    logger.info(
        "Tuner agent run complete: llm_call_succeeded=%s, suggestions_kept=%d, flags_kept=%d.",
        llm_call_succeeded, suggestions_kept, flags_kept,
    )
    if sanitized["flags"]:
        for flag in sanitized["flags"]:
            logger.warning("Tuner agent flag [%s]: %s", flag["strategy"], flag["concern"])

    if not skip_git:
        commit_and_push_files(
            [os.path.relpath(suggestions_path, REPO_ROOT), os.path.relpath(log_path, REPO_ROOT)],
            timestamped_commit_message("Tuner agent suggestions"),
        )

    return {
        "ran": True,
        "llm_call_succeeded": llm_call_succeeded,
        "suggestions_kept": suggestions_kept,
        "flags": sanitized["flags"],
    }


def main():
    logger.info("=== tuner_agent start ===")
    generate_suggestions()
    logger.info("=== tuner_agent end ===")


if __name__ == "__main__":
    main()
