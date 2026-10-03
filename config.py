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


INSTRUMENTS: List[Instrument] = [
    Instrument(symbol="SPY", asset_class=EQUITY, strategy="mean_reversion", timeframe="15Min"),
    Instrument(symbol="QQQ", asset_class=EQUITY, strategy="mean_reversion", timeframe="15Min"),
    Instrument(symbol="BTC/USD", asset_class=CRYPTO, strategy="momentum_breakout", timeframe="1Hour"),
    Instrument(symbol="GLD", asset_class=EQUITY, strategy="trend_following", timeframe="4Hour"),
    Instrument(symbol="USO", asset_class=EQUITY, strategy="trend_following", timeframe="4Hour"),
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
    "timeframe": "15Min",
    # Widened from the original 1.5 / 1.8 after a 6-month backtest showed
    # those bands overtrading noise in a trending market (SPY: 398 trades,
    # 33.2% win rate, Sharpe -7.06; QQQ: 305 trades, 36.4% win rate, Sharpe
    # -0.43). Wider bands select for more extreme, higher-conviction
    # dislocations and should cut trade frequency substantially.
    # Widened again (2.2->2.5, 2.3->2.5) after the trend-filtered run still
    # showed SPY Sharpe -2.71 / MaxDD -29.22% and QQQ Sharpe -0.79 / MaxDD
    # -16.03% -- both still fail the Sharpe<0-or-MaxDD>15% bar. Fewer,
    # higher-conviction entries only.
    "entry_std_dev": {
        "SPY": 2.5,
        "QQQ": 2.5,
    },
    # Regime filter added after the widened-band backtest still showed SPY
    # Sharpe -4.89 / QQQ Sharpe -1.78 with ~30% win rates unchanged -- a
    # structural sign of fighting a persistent trend, not a band-width
    # problem (see bot/strategies/mean_reversion.py docstring). Lengthened
    # 100->150 bars for a stronger regime read after 100 bars still let too
    # many counter-trend entries through. Longs only above it, shorts only
    # below it. Set to None to disable and restore pure unfiltered mean
    # reversion.
    "trend_filter_period": 150,
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
    "timeframe": "1Hour",
    # Raised again from 2.0x -- the Sharpe +0.50 run was still carrying a
    # 23.36% max drawdown, over the user's 15% ceiling, so the volume bar
    # is tightened further to admit only the most convincing breakouts.
    "volume_multiple": 2.2,
    "atr_period": 14,
    # Tightened again (2.5->2.2->1.8) -- the 2.2x round only cut MaxDD from
    # 23.36% to 20.03%, still over the 15% ceiling. Locks in gains sooner
    # still, at further cost to how much room winning trades get to run.
    "trailing_stop_atr_multiple": 1.8,
}

# ---------------------------------------------------------------------------
# Strategy 3 -- Trend Following (GLD, USO)
# ---------------------------------------------------------------------------
TREND_FOLLOWING_PARAMS = {
    "fast_ema": 50,
    "slow_ema": 200,
    "timeframe": "4Hour",
    "atr_period": 14,
    "trailing_stop_atr_multiple": 3.0,
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
    # Correlation filter: block new BTC/USD longs when both of these
    # symbols are already long (risk-on exposure doubling).
    "correlation_block": {
        "guard_symbols": ["SPY", "QQQ"],
        "blocked_symbol": "BTC/USD",
        "blocked_side": "long",
    },
}

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
