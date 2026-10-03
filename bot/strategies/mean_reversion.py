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
- threshold is per-symbol (config.MEAN_REVERSION_PARAMS["entry_std_dev"]).

Trend filter
------------
A 6-month backtest showed pure mean reversion losing badly on SPY/QQQ
specifically because both drifted in one direction for the whole window --
every dip-buy (long) was fighting a persistent downtrend. Widening the
entry bands alone didn't fix it (win rate stayed ~30%), because the
problem isn't trade selectivity, it's taking trades *against* the
prevailing direction in a trending regime.

The fix: a longer-period SMA (`trend_filter_period`, e.g. 100 bars) gives
a regime read independent of the fast 20-period mean. Longs are only
taken when price is above that longer SMA (don't fade dips in a
downtrend), shorts only when price is below it (don't fade rallies in an
uptrend). This keeps the strategy to reversions *within* the prevailing
trend -- pullbacks, not full counter-trend bets -- which is the standard
way mean reversion is paired with trend context. Set
`trend_filter_period` to `None` (or omit it) to disable the filter and
get the original unfiltered behavior.

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
        self.trend_filter_period = self.params.get("trend_filter_period")

    def required_bars(self) -> int:
        # Need lookback bars for SMA/std, the trend filter's longer SMA (if
        # enabled), and a little headroom for ATR warmup.
        periods = [self.lookback, self.atr_period]
        if self.trend_filter_period:
            periods.append(self.trend_filter_period)
        return max(periods) + 5

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

        # Trend filter: only allow entries in the direction consistent with
        # the longer-period trend (see module docstring). None/absent
        # disables this and restores the original unfiltered behavior.
        trend_allows_long = True
        trend_allows_short = True
        if self.trend_filter_period:
            trend_sma = sma(close, self.trend_filter_period)
            last_trend_sma = float(trend_sma.iloc[-1])
            if pd.notna(last_trend_sma):
                trend_allows_long = last_close > last_trend_sma
                trend_allows_short = last_close < last_trend_sma

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
            if last_close <= lower_band and trend_allows_long:
                return Signal(
                    symbol=symbol, action=SignalAction.LONG_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"price {last_close:.2f} <= lower band {lower_band:.2f} "
                            f"(mean {last_mean:.2f} - {threshold}*std {last_std:.2f}); "
                            f"trend filter OK"),
                )
            if last_close >= upper_band and trend_allows_short:
                return Signal(
                    symbol=symbol, action=SignalAction.SHORT_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"price {last_close:.2f} >= upper band {upper_band:.2f} "
                            f"(mean {last_mean:.2f} + {threshold}*std {last_std:.2f}); "
                            f"trend filter OK"),
                )
            if last_close <= lower_band and not trend_allows_long:
                return Signal(
                    symbol=symbol, action=SignalAction.HOLD, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"price {last_close:.2f} <= lower band {lower_band:.2f} but "
                            f"trend filter blocked the long (price below "
                            f"{self.trend_filter_period}-period trend SMA)"),
                )
            if last_close >= upper_band and not trend_allows_short:
                return Signal(
                    symbol=symbol, action=SignalAction.HOLD, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"price {last_close:.2f} >= upper band {upper_band:.2f} but "
                            f"trend filter blocked the short (price above "
                            f"{self.trend_filter_period}-period trend SMA)"),
                )

        return Signal(
            symbol=symbol, action=SignalAction.HOLD, price=last_close,
            atr=last_atr, bar_timestamp=last_ts,
            reason="within bands / no state change",
        )
