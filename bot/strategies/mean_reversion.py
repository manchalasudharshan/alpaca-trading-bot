"""
bot/strategies/mean_reversion.py

Strategy 1 -- Mean Reversion (SPY, QQQ), 15-minute candles.

Logic
-----
- 20-period SMA and rolling std-dev of closing price.
- Entry (long):  close <= SMA - (threshold * std)   -> expect reversion up
- Entry (short): close >= SMA + (threshold * std)   -> expect reversion down
- Exit: close crosses back through the SMA (i.e. price has reverted to the
  mean), independent of entry side.
- threshold is per-symbol: 1.5 for SPY, 1.8 for QQQ (config.MEAN_REVERSION_PARAMS).

This module is stateless with respect to *position* -- it only looks at
price vs. bands and emits entry signals, plus an exit signal whenever price
is back at the mean. bot/portfolio.py is the source of truth for whether a
position is actually open, so an EXIT signal is a no-op if nothing is open
on that side.
"""

import logging

import pandas as pd

from bot.indicators import sma, rolling_std, atr
from bot.strategies.base import Signal, SignalAction
import config

logger = logging.getLogger(__name__)


class MeanReversionStrategy:
    name = "mean_reversion"

    def __init__(self, params: dict = None):
        self.params = params or config.MEAN_REVERSION_PARAMS
        self.lookback = self.params["lookback"]
        self.entry_std_dev = self.params["entry_std_dev"]
        self.atr_period = config.RISK_PARAMS["atr_period"]

    def required_bars(self) -> int:
        # Need lookback bars for SMA/std plus a little headroom for ATR warmup.
        return max(self.lookback, self.atr_period) + 5

    def generate_signal(self, symbol: str, bars: pd.DataFrame,
                         currently_long: bool, currently_short: bool) -> Signal:
        """
        bars: OHLCV DataFrame, oldest-first, with the LAST row being the most
              recently closed bar (we never trade on an in-progress bar).
        currently_long / currently_short: current position state for this
              symbol, from portfolio.py, used only to decide whether an
              exit-at-mean signal is meaningful.
        """
        if len(bars) < self.required_bars():
            return Signal(
                symbol=symbol, action=SignalAction.HOLD, price=float(bars["close"].iloc[-1]),
                atr=float("nan"), reason="insufficient history for mean reversion calc",
            )

        close = bars["close"]
        mean = sma(close, self.lookback)
        std = rolling_std(close, self.lookback)
        atr_series = atr(bars, self.atr_period)

        last_close = float(close.iloc[-1])
        last_mean = float(mean.iloc[-1])
        last_std = float(std.iloc[-1])
        last_atr = float(atr_series.iloc[-1])
        last_ts = bars.index[-1]

        threshold = self.entry_std_dev.get(symbol, 1.5)
        lower_band = last_mean - threshold * last_std
        upper_band = last_mean + threshold * last_std

        # --- Exit logic takes priority: if we're in a position and price has
        # reverted to (or past) the mean, flatten first. ---
        if currently_long and last_close >= last_mean:
            return Signal(
                symbol=symbol, action=SignalAction.EXIT_LONG, price=last_close,
                atr=last_atr, bar_timestamp=last_ts,
                reason=f"price {last_close:.2f} reverted to/above mean {last_mean:.2f}; exit long",
            )
        if currently_short and last_close <= last_mean:
            return Signal(
                symbol=symbol, action=SignalAction.EXIT_SHORT, price=last_close,
                atr=last_atr, bar_timestamp=last_ts,
                reason=f"price {last_close:.2f} reverted to/below mean {last_mean:.2f}; exit short",
            )

        # --- Entry logic (only if flat on the relevant side) ---
        if not currently_long and not currently_short:
            if last_close <= lower_band:
                return Signal(
                    symbol=symbol, action=SignalAction.LONG_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"price {last_close:.2f} <= lower band {lower_band:.2f} "
                            f"(mean {last_mean:.2f} - {threshold}*std {last_std:.2f})"),
                )
            if last_close >= upper_band:
                return Signal(
                    symbol=symbol, action=SignalAction.SHORT_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"price {last_close:.2f} >= upper band {upper_band:.2f} "
                            f"(mean {last_mean:.2f} + {threshold}*std {last_std:.2f})"),
                )

        return Signal(
            symbol=symbol, action=SignalAction.HOLD, price=last_close,
            atr=last_atr, bar_timestamp=last_ts,
            reason="within bands / no state change",
        )
