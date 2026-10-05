"""
config.py
Central configuration for the Alpaca multi-strategy trading bot.

Loads secrets from .env and defines all static parameters: instruments,
timeframes, strategy thresholds, and risk limits. Nothing in this file
should need to change at runtime -- it's the single source of truth that
every other module imports from.
"""

import os
from dataclasses import dataclass, field
from typing import Dict, List

from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Alpaca API credentials / endpoint
# ---------------------------------------------------------------------------
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID", "")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY", "")

# Paper vs live trading. Defaults to Alpaca's paper endpoint so nobody
# accidentally goes live by running this with a fresh .env.
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

# Alpaca's market data base URL (v2 data API)
APCA_DATA_URL = os.getenv("APCA_DATA_URL", "https://data.alpaca.markets")

# Equity market data feed. Free Alpaca accounts (including paper accounts
# on the free tier) only have access to the IEX feed; the SIP (consolidated
# tape) feed requires a paid market-data subscription and raises
# "subscription does not permit querying recent SIP data" otherwise. "iex"
# is the safe default; override to "sip" in .env only if the account
# actually has a SIP subscription.
EQUITY_DATA_FEED = os.getenv("APCA_EQUITY_DATA_FEED", "iex")

if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
    raise EnvironmentError(
        "Missing Alpaca API credentials. Set APCA_API_KEY_ID and "
        "APCA_API_SECRET_KEY in your .env file (see .env.example)."
    )

# ---------------------------------------------------------------------------
# Instrument universe
# ---------------------------------------------------------------------------
# Alpaca uses different symbol conventions for crypto (e.g. "BTC/USD") vs
# equities ("SPY"). asset_class drives which REST calls / data feed each
# symbol uses throughout the bot.

EQUITY = "equity"
CRYPTO = "crypto"


@dataclass
class Instrument:
    symbol: str                 # Alpaca symbol, e.g. "SPY" or "BTC/USD"
    asset_class: str            # "equity" or "crypto"
    strategy: str                # "mean_reversion" | "momentum_breakout" | "trend_following"
    timeframe: str               # Alpaca TimeFrame string, e.g. "15Min", "1Hour", "4Hour"


# NOTE on timeframe="5Min" below (SPY/QQQ/GLD/USO -- NOT BTC/USD, see next
# note): the live bot's cron tick fires every 5 minutes (see
# scripts/vps_tick.sh / .github/workflows/live_trading.yml), but
# bot/main.py._process_instrument only evaluates a symbol's strategy when a
# NEW bar has closed for that symbol's timeframe (the last_seen_bar check).
# With the old 15Min/1Hour/4Hour timeframes, most ticks did nothing for
# most symbols -- a tick could fire 11 times between two 1Hour closes with
# zero chance of a signal. Setting these 4 instruments to "5Min" aligns the
# strategy evaluation cadence with the tick cadence, so (almost) every tick
# has a genuine chance to see a freshly-closed bar and run real strategy
# logic -- this does NOT change what counts as a signal (same SMA/EMA
# rules), only how often a fresh bar is available to evaluate them against.
# See bot/broker.py's _lookback_window / _estimate_bar_count for how the
# bar-fetch window was re-checked against this change.
#
# NOTE on BTC/USD's timeframe="1Hour" (reverted from "5Min" on 2026-10-05):
# momentum_breakout was originally designed and tuned on 1Hour candles (see
# MOMENTUM_BREAKOUT_PARAMS' history below), then swept into the "move
# everything to 5Min" change above along with the other 4 instruments.
# That was a mistake specific to this one strategy: a live run turned up a
# persistently-negative 90-day backtest score the auto-tuner could never
# improve, and a direct same-params/same-code 1Hour-vs-5Min comparison
# (6-month backtest) showed why --
#   1Hour: 66 trades, win rate 27.3%, profit factor 1.40, Sharpe +1.03,
#          max drawdown -8.6%, total return +11.6%
#   5Min:  568 trades, win rate 14.6%, profit factor 0.35, Sharpe -7.57,
#          max drawdown -53.5%, total return -53.3%
# Same strategy, same risk sizing, same slippage model -- squeezed onto
# 5-minute crypt candles, the breakout+volume-spike signal fires 8.6x more
# often on noise rather than real moves (BTC/USD's 5Min volume per bar is
# tiny and swings 0.15x-3x of its own rolling average within minutes). The
# fix is the candle size, not the strategy or its parameters, so BTC/USD
# alone moved back to "1Hour" while SPY/QQQ/GLD/USO stay on "5Min".
#
# This does NOT reintroduce the "most ticks wasted" problem described
# above in a way that matters: the live tick still fires every 5 minutes,
# and bot/main.py's last_seen_bar gate just means most of those ticks see
# "no new bar yet" for BTC/USD and skip it cheaply -- a new 1Hour close is
# still always picked up and evaluated within 5 minutes of it happening
# (the next tick), which is what "within the ticking cycle" means here.
# The other 4 instruments are unaffected and keep evaluating on every tick
# as before.
INSTRUMENTS: List[Instrument] = [
    Instrument(symbol="SPY", asset_class=EQUITY, strategy="mean_reversion", timeframe="5Min"),
    Instrument(symbol="QQQ", asset_class=EQUITY, strategy="mean_reversion", timeframe="5Min"),
    Instrument(symbol="BTC/USD", asset_class=CRYPTO, strategy="momentum_breakout", timeframe="1Hour"),
    Instrument(symbol="GLD", asset_class=EQUITY, strategy="trend_following", timeframe="5Min"),
    Instrument(symbol="USO", asset_class=EQUITY, strategy="trend_following", timeframe="5Min"),
]

SYMBOLS_BY_STRATEGY: Dict[str, List[str]] = {
    "mean_reversion": [i.symbol for i in INSTRUMENTS if i.strategy == "mean_reversion"],
    "momentum_breakout": [i.symbol for i in INSTRUMENTS if i.strategy == "momentum_breakout"],
    "trend_following": [i.symbol for i in INSTRUMENTS if i.strategy == "trend_following"],
}

INSTRUMENT_BY_SYMBOL: Dict[str, Instrument] = {i.symbol: i for i in INSTRUMENTS}

# ---------------------------------------------------------------------------
# Strategy 1 -- Mean Reversion (SPY, QQQ)
# ---------------------------------------------------------------------------
MEAN_REVERSION_PARAMS = {
    "lookback": 20,                 # SMA / stddev period
    "timeframe": "5Min",
    # Widened from the original 1.5 / 1.8 after a 6-month backtest showed
    # those bands overtrading noise in a trending market (SPY: 398 trades,
    # 33.2% win rate, Sharpe -7.06; QQQ: 305 trades, 36.4% win rate, Sharpe
    # -0.43). Wider bands select for more extreme, higher-conviction
    # dislocations and should cut trade frequency substantially.
    # Reverted to 2.2/2.3 -- widening further to 2.5 (with a 150-bar trend
    # filter) made SPY *worse* (Sharpe -2.71->-3.02, MaxDD -29.22%->-30.66%,
    # win rate 34.1%->27.6%) and did not fix QQQ either. That's the second
    # straight piece of evidence that SPY/QQQ's problem is NOT band width:
    # tightening the trade selection in either direction (more trades at
    # 2.2/2.3, fewer at 2.5) still fails the Sharpe/MaxDD bar on this
    # 6-month window. See the README "known limitations" note -- these two
    # should stay out of live trading until re-validated on a different
    # (less persistently trending) window, not tuned further on this one.
    "entry_std_dev": {
        "SPY": 2.2,
        "QQQ": 2.3,
    },
    # Regime filter: a longer SMA gates mean-reversion entries to only fire
    # *with* the prevailing direction (longs above it, shorts below it).
    # Reverted 150->100: swapping only this value (bands held at 2.2/2.3)
    # made SPY *and* QQQ both worse (SPY MaxDD -29.22%->-34.56%, QQQ MaxDD
    # -16.03%->-18.72%), so 100 bars is the better of the two tried. Set to
    # None to disable and restore pure unfiltered mean reversion.
    "trend_filter_period": 100,
    # Exit when price crosses back through the mean -- no separate param
    # needed, handled in strategy logic.
}

# ---------------------------------------------------------------------------
# Strategy 2 -- Momentum Breakout (BTC/USD)
# ---------------------------------------------------------------------------
MOMENTUM_BREAKOUT_PARAMS = {
    # Widened from 20 after a 6-month backtest showed a 19.8% win rate
    # despite a healthy win/loss ratio (avg win $3,025 vs avg loss $920) --
    # a sign of too many false/weak breakouts, not a bad edge. A longer
    # channel selects for more significant breakouts.
    "lookback": 30,
    # Descriptive only -- the actual live/backtest fetch timeframe comes
    # from config.INSTRUMENTS' BTC/USD entry, not this field (nothing reads
    # params["timeframe"]). Kept in sync with it for clarity. Reverted to
    # "1Hour" on 2026-10-05 -- see the long note above INSTRUMENTS for why.
    "timeframe": "1Hour",
    # Raised again from 2.0x -- the Sharpe +0.50 run was still carrying a
    # 23.36% max drawdown, over the user's 15% ceiling, so the volume bar
    # is tightened further to admit only the most convincing breakouts.
    "volume_multiple": 2.2,
    "atr_period": 14,
    # Reverted 1.5->1.8: tightening further to 1.5x made things worse
    # (Sharpe +1.05->+0.88, MaxDD -15.82%->-16.39%) -- 1.8x is the best of
    # the three multiples tried (2.2x: Sharpe +0.54/MaxDD -20.03%; 1.8x:
    # Sharpe +1.05/MaxDD -15.82%; 1.5x: Sharpe +0.88/MaxDD -16.39%). Still
    # ~0.8 points over the 15% ceiling -- see README for the honest
    # conclusion on this one.
    "trailing_stop_atr_multiple": 1.8,
}

# ---------------------------------------------------------------------------
# Strategy 3 -- Trend Following (GLD, USO)
# ---------------------------------------------------------------------------
# 2026-10-05 per-symbol split: a same-params/same-code timeframe comparison
# (4Hour original design vs. 5Min current live) showed GLD losing money at
# BOTH timeframes (PF 0.00/-2.15% at 4Hour, PF 0.51/-4.58% at 5Min) while USO
# on the exact same shared params was profitable at 5Min (PF 1.79/+8.21%,
# Sharpe +1.30) and never even traded at 4Hour (0 trades in 6 months -- a
# 50/200 EMA crossover is too rare on 259 bars to evaluate). That rules out
# "wrong timeframe" as GLD's problem (unlike BTC/momentum_breakout's 2026-
# 10-05 incident, see Instrument comment below) -- it's a params/strategy-fit
# problem specific to gold. fast_ema/slow_ema/trailing_stop_atr_multiple were
# previously single shared scalars, so bot/auto_tune.py's coordinate search
# could only ever find one compromise value across both symbols -- any fix
# for GLD's negative expectancy would also perturb USO's already-profitable
# params. Converted to per-symbol dicts (same pattern as mean_reversion's
# entry_std_dev) so GLD and USO can be tuned independently; both start from
# the prior shared values, so this change by itself doesn't alter live
# behavior until the tuner (or a manual retune) actually diverges them.
TREND_FOLLOWING_PARAMS = {
    "fast_ema": {"GLD": 50, "USO": 50},
    "slow_ema": {"GLD": 200, "USO": 200},
    "timeframe": "5Min",
    "atr_period": 14,
    "trailing_stop_atr_multiple": {"GLD": 3.0, "USO": 3.0},
}

# ---------------------------------------------------------------------------
# Risk management (applies to every strategy)
# ---------------------------------------------------------------------------
RISK_PARAMS = {
    "atr_period": 14,
    # Position sizing: a 1-ATR adverse move should equal this fraction of
    # total account equity.
    "risk_per_atr_of_equity": 0.01,
    # Hard stop loss, expressed as a fraction of total account equity, that
    # is never exceeded regardless of how sizing works out.
    "max_loss_per_trade_of_equity": 0.01,
    # Hard cap on a single position's total NOTIONAL exposure, as a fraction
    # of account equity. 1.0 == never put on a position worth more than the
    # whole account (no leverage), which is the right default for a cash
    # paper-trading account.
    #
    # Why this exists: the ATR-based sizing above only controls dollar risk
    # at a 1-ATR move (qty = risk_dollars / atr). If an instrument's ATR is
    # small relative to its price (e.g. BTC/USD during a quiet stretch,
    # ATR ~0.3% of price), that formula alone can size a position many times
    # larger than total equity -- the dollar risk at 1 ATR is still correct,
    # but a gap or flash move well beyond 1 ATR could lose far more than
    # intended, and the order may not even be fillable against actual buying
    # power. This was a real bug: on 2026-10-03 the bot attempted a
    # $328,951.11 notional BTC/USD order against a $100,000 account (over
    # 3x equity); Alpaca's own $200,000 max-notional-per-order cap rejected
    # it, but the bot's own sizing should never have produced it.
    # size_position() now caps qty so entry_price * qty never exceeds
    # account_equity * max_position_notional_pct_of_equity, even if that
    # means realized risk at the hard stop comes in under the 1% ATR target.
    "max_position_notional_pct_of_equity": 1.0,
    # Correlation filter: block new BTC/USD longs when both of these
    # symbols are already long (risk-on exposure doubling).
    "correlation_block": {
        "guard_symbols": ["SPY", "QQQ"],
        "blocked_symbol": "BTC/USD",
        "blocked_side": "long",
    },
}

# ---------------------------------------------------------------------------
# Portfolio-level circuit breaker (applies across every strategy/instrument)
# ---------------------------------------------------------------------------
# If current account equity ever falls this fraction below the highest
# equity ever observed (bot/main.py's self.peak_equity), the bot closes
# every open position immediately and halts ALL new trading. There is no
# automatic resume: a human must manually clear the "trading_halted" flag
# in bot_state.json after reviewing what happened. See README.md's
# "Circuit breaker" section for the manual reset procedure.
MAX_DRAWDOWN_PCT = float(os.getenv("MAX_DRAWDOWN_PCT", "0.10"))

# ---------------------------------------------------------------------------
# Scheduling / polling
# ---------------------------------------------------------------------------
# How often (seconds) the main loop wakes up to check whether any strategy's
# timeframe has produced a new closed bar. This is intentionally much finer
# than the coarsest timeframe (4h) so new bars are picked up promptly.
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))

# Market-hours check interval for equities (seconds) -- separate from the
# strategy poll loop so we don't hammer the clock endpoint.
MARKET_CLOCK_CHECK_SECONDS = int(os.getenv("MARKET_CLOCK_CHECK_SECONDS", "30"))

# ---------------------------------------------------------------------------
# Logging / file paths
# ---------------------------------------------------------------------------
LOG_DIR = os.getenv("LOG_DIR", ".")
TRADES_CSV_PATH = os.path.join(LOG_DIR, "trades.csv")
DAILY_PNL_CSV_PATH = os.path.join(LOG_DIR, "daily_pnl.csv")
BOT_LOG_PATH = os.path.join(LOG_DIR, "bot.log")
# Open positions, last-seen-bar timestamps, and peak equity, persisted
# across process restarts -- required for bot/live_tick.py, where each
# GitHub Actions cron run is a fresh process (see bot/main.py TradingBot
# .save_state/.load_state). Also used by the continuous bot/main.py loop
# so a VPS restart doesn't lose track of open risk either.
STATE_FILE_PATH = os.path.join(LOG_DIR, "bot_state.json")

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
# Max number of historical bars to pull per data request.
BARS_LIMIT = 500

# Retry/backoff settings for API calls
API_MAX_RETRIES = int(os.getenv("API_MAX_RETRIES", "5"))
API_BACKOFF_BASE_SECONDS = float(os.getenv("API_BACKOFF_BASE_SECONDS", "2.0"))

# ---------------------------------------------------------------------------
# Backtesting (bot/backtest.py)
# ---------------------------------------------------------------------------
BACKTEST_PARAMS = {
    "lookback_months": 6,
    "starting_equity": float(os.getenv("BACKTEST_STARTING_EQUITY", "100000")),
    # Round-trip slippage applied on both entry and exit fills: 0.05% means
    # a long buys 0.05% above the signal price and sells 0.05% below it (and
    # the mirror image for shorts) -- a conservative, symmetric model.
    "slippage_pct": 0.0005,
    # Alpaca is commission-free for equities and crypto.
    "commission_per_trade": 0.0,
    # Trading-day convention used to annualize the Sharpe ratio from daily
    # returns. 252 is the standard US-equity convention; since the combined
    # portfolio includes 24/7 BTC/USD alongside equities, this is a
    # simplifying assumption documented in the backtest report.
    "trading_days_per_year": 252,
    "results_png_path": os.path.join(LOG_DIR, "backtest_results.png"),
    "trades_csv_path": os.path.join(LOG_DIR, "backtest_trades.csv"),
}

# ---------------------------------------------------------------------------
# Tuner agent (bot/tuner_agent.py) -- an OPT-IN, advisory-only layer on top
# of bot/auto_tune.py's existing deterministic backtest/grid-search tuner.
# The actual reasoning is done by a SCHEDULED CLAUDE SESSION (see README
# "Tuner agent (LLM advisory layer)"), not by an API call from this
# process -- bot/tuner_agent.py is only the safety boundary that session's
# output passes through: it never writes strategy_params.json itself and
# never bypasses any existing adoption gate (MIN_TRADES, the improvement
# margin, or a parameter's hardcoded [lo, hi] bounds in
# bot/auto_tune.py's TUNE_SPECS) -- it can only ever SUGGEST extra
# candidate values for auto_tune.py's existing per-parameter search to try,
# which still have to beat the current params by the same margin, on the
# same backtest, as any other candidate.
# ---------------------------------------------------------------------------
TUNER_AGENT_ENABLED = os.getenv("TUNER_AGENT_ENABLED", "false").strip().lower() in ("1", "true", "yes")
TUNER_AGENT_SUGGESTIONS_PATH = os.path.join(LOG_DIR, "tuner_agent_suggestions.json")
TUNER_AGENT_LOG_PATH = os.path.join(LOG_DIR, "tuner_agent_log.csv")
