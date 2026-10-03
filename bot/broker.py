"""
bot/broker.py

Thin wrapper around alpaca_trade_api.REST that centralizes:
  - connection setup
  - historical bar fetching for both equities and crypto (different
    endpoints/symbol conventions under the hood, unified interface here)
  - order submission (market orders with a note; the bot manages stops
    itself in software via the trailing-stop/hard-stop logic in
    risk_manager.py + the strategies, rather than relying solely on
    broker-side stop orders, so it can apply the ATR-based trailing-stop
    ratchet each bar)
  - market clock / crypto-is-always-open handling
  - retry/backoff around every network call, so a flaky connection or a
    momentary Alpaca outage doesn't crash the bot

alpaca-trade-api is synchronous/REST (the "v2" SDK). This module assumes
that package is installed (see requirements.txt).
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd

import config

logger = logging.getLogger(__name__)

try:
    import alpaca_trade_api as tradeapi
    from alpaca_trade_api.rest import TimeFrame, TimeFrameUnit, APIError
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "alpaca-trade-api is required. Install with: pip install alpaca-trade-api"
    ) from e


# Map our config-style timeframe strings to alpaca_trade_api TimeFrame objects.
def _timeframe_from_str(tf: str) -> TimeFrame:
    mapping = {
        "1Min": TimeFrame.Minute,
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "1Hour": TimeFrame.Hour,
        "4Hour": TimeFrame(4, TimeFrameUnit.Hour),
        "1Day": TimeFrame.Day,
    }
    if tf not in mapping:
        raise ValueError(f"Unsupported timeframe string: {tf}")
    return mapping[tf]


def _retry(fn, *args, max_retries=None, backoff_base=None, **kwargs):
    """
    Generic retry wrapper with exponential backoff for any Alpaca API call.
    Retries on APIError (rate limits, transient 5xx) and generic connection
    errors. Re-raises after exhausting retries so the caller's own
    error-handling (main.py's loop) can decide what to do (e.g. skip this
    cycle and try again next poll).
    """
    max_retries = max_retries or config.API_MAX_RETRIES
    backoff_base = backoff_base or config.API_BACKOFF_BASE_SECONDS

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            last_exc = e
            logger.warning("Alpaca APIError (attempt %d/%d): %s", attempt, max_retries, e)
        except (ConnectionError, TimeoutError, OSError) as e:
            last_exc = e
            logger.warning("Network error talking to Alpaca (attempt %d/%d): %s",
                            attempt, max_retries, e)
        except Exception as e:  # noqa: BLE001 - last line of defense, log and retry
            last_exc = e
            logger.warning("Unexpected error calling Alpaca (attempt %d/%d): %s",
                            attempt, max_retries, e)

        sleep_s = backoff_base * (2 ** (attempt - 1))
        logger.info("Retrying in %.1fs...", sleep_s)
        time.sleep(sleep_s)

    logger.error("Exhausted %d retries calling %s; giving up for this cycle.",
                 max_retries, getattr(fn, "__name__", fn))
    raise last_exc


class AlpacaBroker:
    def __init__(self):
        self.api = tradeapi.REST(
            key_id=config.APCA_API_KEY_ID,
            secret_key=config.APCA_API_SECRET_KEY,
            base_url=config.APCA_API_BASE_URL,
            api_version="v2",
        )
        self._verify_connection()

    def _verify_connection(self):
        try:
            account = _retry(self.api.get_account)
            logger.info("Connected to Alpaca. Account status=%s, equity=%s, buying_power=%s",
                        account.status, account.equity, account.buying_power)
        except Exception as e:
            logger.error("Could not connect to Alpaca on startup: %s", e)
            raise

    # ------------------------------------------------------------------
    # Account
    # ------------------------------------------------------------------
    def get_equity(self) -> float:
        account = _retry(self.api.get_account)
        return float(account.equity)

    # ------------------------------------------------------------------
    # Market status
    # ------------------------------------------------------------------
    def is_equity_market_open(self) -> bool:
        """Crypto trades 24/7 and never consults this; equities do."""
        try:
            clock = _retry(self.api.get_clock)
            return bool(clock.is_open)
        except Exception as e:
            logger.error("Failed to fetch market clock, assuming CLOSED for safety: %s", e)
            return False

    def next_market_open(self) -> Optional[datetime]:
        try:
            clock = _retry(self.api.get_clock)
            return clock.next_open
        except Exception as e:
            logger.error("Failed to fetch next market open: %s", e)
            return None

    # ------------------------------------------------------------------
    # Historical bars
    # ------------------------------------------------------------------
    def get_bars(self, symbol: str, asset_class: str, timeframe: str, limit: int) -> pd.DataFrame:
        """
        Returns an OHLCV DataFrame indexed by timestamp (UTC, ascending),
        with columns ['open', 'high', 'low', 'close', 'volume']. Works for
        both equities and crypto -- alpaca_trade_api's get_bars() handles
        crypto symbols like "BTC/USD" transparently in recent SDK versions,
        but we branch explicitly so the data source is obvious and future
        SDK differences are easy to patch in one place.
        """
        tf = _timeframe_from_str(timeframe)
        end = datetime.now(timezone.utc)
        # Pad the lookback window generously; weekends/holidays mean a
        # naive "limit * timeframe" window can come up short for equities.
        start = end - self._lookback_window(timeframe, limit)

        def _fetch():
            if asset_class == config.CRYPTO:
                bars = self.api.get_crypto_bars(
                    symbol, tf, start.isoformat(), end.isoformat(), limit=limit,
                )
            else:
                bars = self.api.get_bars(
                    symbol, tf, start.isoformat(), end.isoformat(), limit=limit,
                    adjustment="raw", feed=config.EQUITY_DATA_FEED,
                )
            return bars.df

        df = _retry(_fetch)

        if df is None or df.empty:
            logger.warning("No bar data returned for %s (%s)", symbol, timeframe)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        # Some SDK versions return a multi-symbol-indexed df when querying
        # crypto across exchanges; normalize to a plain OHLCV frame for one
        # symbol.
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level=0) if symbol in df.index.get_level_values(0) else df.droplevel(0)

        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        return df

    def get_historical_bars(self, symbol: str, asset_class: str, timeframe: str,
                             start: datetime, end: datetime, limit: int = 10000) -> pd.DataFrame:
        """
        Like get_bars(), but for an explicit [start, end) date range rather
        than "the last `limit` bars ending now" -- what bot/backtest.py uses
        to pull a fixed historical window (e.g. the last 6 months) per
        instrument.

        Note: Alpaca's REST API caps bars per request; for the timeframes
        and ~6-month windows this bot uses (15Min for equities, 1Hour/4Hour
        for BTC/GLD/USO) a single request with limit=10000 comfortably
        covers the range. For much longer backtest windows, add pagination
        here using the SDK's built-in page_token handling.
        """
        tf = _timeframe_from_str(timeframe)

        def _fetch():
            if asset_class == config.CRYPTO:
                bars = self.api.get_crypto_bars(
                    symbol, tf, start.isoformat(), end.isoformat(), limit=limit,
                )
            else:
                bars = self.api.get_bars(
                    symbol, tf, start.isoformat(), end.isoformat(), limit=limit,
                    adjustment="raw", feed=config.EQUITY_DATA_FEED,
                )
            return bars.df

        df = _retry(_fetch)

        if df is None or df.empty:
            logger.warning("No historical bar data returned for %s (%s) in range %s to %s",
                            symbol, timeframe, start, end)
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level=0) if symbol in df.index.get_level_values(0) else df.droplevel(0)

        df = df[["open", "high", "low", "close", "volume"]].sort_index()
        return df

    @staticmethod
    def _lookback_window(timeframe: str, limit: int) -> timedelta:
        per_bar = {
            "1Min": timedelta(minutes=1),
            "15Min": timedelta(minutes=15),
            "1Hour": timedelta(hours=1),
            "4Hour": timedelta(hours=4),
            "1Day": timedelta(days=1),
        }[timeframe]
        # 3x padding covers weekends/holidays for equities; harmless extra
        # for 24/7 crypto.
        return per_bar * limit * 3 + timedelta(days=5)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------
    def submit_market_order(self, symbol: str, qty: float, side: str, asset_class: str):
        """
        side: "buy" or "sell". The bot manages stop/trailing-stop logic
        itself (see risk_manager.py + strategy modules), so entries and
        exits are both plain market orders here -- this keeps order
        management logic in one place (Python) instead of split between
        local state and broker-side conditional orders.
        """
        time_in_force = "gtc" if asset_class == config.CRYPTO else "day"

        def _submit():
            return self.api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type="market",
                time_in_force=time_in_force,
            )

        try:
            order = _retry(_submit)
            logger.info("Submitted %s order: %s %s qty=%s (order id=%s)",
                        time_in_force, side, symbol, qty, getattr(order, "id", "?"))
            return order
        except Exception as e:
            logger.error("FAILED to submit %s %s qty=%s after retries: %s", side, symbol, qty, e)
            return None

    def get_positions(self) -> list:
        """
        Returns Alpaca's own list of currently open positions (the broker's
        source of truth), each with symbol, qty, side, avg_entry_price,
        current_price, unrealized_pl, unrealized_plpc, market_value. Used by
        the reporting scripts so "current positions" always reflects what
        Alpaca actually holds, not just this process's in-memory book.
        """
        try:
            positions = _retry(self.api.list_positions)
            return [
                {
                    "symbol": p.symbol,
                    "qty": float(p.qty),
                    "side": "long" if float(p.qty) >= 0 else "short",
                    "avg_entry_price": float(p.avg_entry_price),
                    "current_price": float(p.current_price),
                    "unrealized_pl": float(p.unrealized_pl),
                    "unrealized_plpc": float(p.unrealized_plpc),
                    "market_value": float(p.market_value),
                }
                for p in positions
            ]
        except Exception as e:
            logger.error("Failed to fetch open positions: %s", e)
            return []

    def get_last_trade_price(self, symbol: str, asset_class: str) -> Optional[float]:
        """Used as a fallback fill-price estimate if we want current price
        between bar closes (e.g. for logging); not required for the core
        signal logic, which works off bar closes."""
        try:
            if asset_class == config.CRYPTO:
                trade = _retry(self.api.get_latest_crypto_trade, symbol)
            else:
                trade = _retry(self.api.get_latest_trade, symbol)
            return float(trade.price)
        except Exception as e:
            logger.warning("Could not fetch last trade price for %s: %s", symbol, e)
            return None
