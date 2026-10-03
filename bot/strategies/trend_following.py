"""
bot/strategies/trend_following.py

Strategy 3 -- Trend Following (GLD, USO), 4-hour candles.

Logic
-----
- 50-period and 200-period EMA of closing price.
- Long entry: 50 EMA crosses above 200 EMA (golden cross) on the latest
  closed bar.
- Short entry / exit-long: 50 EMA crosses below 200 EMA (death cross) ->
  close any open long, and open a short (this is a flip-style trend
  follower: always in the market in the direction of the trend, unless the
  trailing stop takes it flat first).
- Trailing stop: 3x ATR(14), ratcheted in the position's favor each bar,
  same mechanics as the breakout strategy but with a wider multiple
  reflecting the longer 4h holding period.
"""

import logging

import pandas as pd

from bot.indicators import ema, ema_cross, atr
from bot.strategies.base import Signal, SignalAction
import config

logger = logging.getLogger(__name__)


class TrendFollowingStrategy:
    name = "trend_following"

    def __init__(self, params: dict = None):
        self.params = params or config.TREND_FOLLOWING_PARAMS
        self.fast_period = self.params["fast_ema"]
        self.slow_period = self.params["slow_ema"]
        self.atr_period = self.params["atr_period"]
        self.trailing_stop_atr_mult = self.params["trailing_stop_atr_multiple"]

    def required_bars(self) -> int:
        # 200-period EMA needs real warmup to be meaningful; pandas' ewm
        # with min_periods=slow_period handles the NaN-until-warm part, but
        # we want a bit of history past that so the cross detection itself
        # is on fully-warmed values.
        return self.slow_period + 10

    def generate_signal(self, symbol: str, bars: pd.DataFrame,
                         currently_long: bool, currently_short: bool,
                         trailing_stop_price: float = None) -> Signal:
        if len(bars) < self.required_bars():
            return Signal(
                symbol=symbol, action=SignalAction.HOLD, price=float(bars["close"].iloc[-1]),
                atr=float("nan"), reason="insufficient history for EMA warmup (need 200+ bars)",
            )

        close = bars["close"]
        fast = ema(close, self.fast_period)
        slow = ema(close, self.slow_period)
        crosses = ema_cross(fast, slow)
        atr_series = atr(bars, self.atr_period)

        last_close = float(close.iloc[-1])
        last_low = float(bars["low"].iloc[-1])
        last_high = float(bars["high"].iloc[-1])
        last_cross = int(crosses.iloc[-1])
        last_atr = float(atr_series.iloc[-1])
        last_ts = bars.index[-1]
        fast_val = float(fast.iloc[-1])
        slow_val = float(slow.iloc[-1])

        # --- Trailing stop check takes priority ---
        if currently_long and trailing_stop_price is not None and last_low <= trailing_stop_price:
            return Signal(
                symbol=symbol, action=SignalAction.EXIT_LONG, price=last_close,
                atr=last_atr, bar_timestamp=last_ts,
                reason=(f"trailing stop hit: bar low {last_low:.2f} <= stop "
                        f"{trailing_stop_price:.2f}"),
            )
        if currently_short and trailing_stop_price is not None and last_high >= trailing_stop_price:
            return Signal(
                symbol=symbol, action=SignalAction.EXIT_SHORT, price=last_close,
                atr=last_atr, bar_timestamp=last_ts,
                reason=(f"trailing stop hit: bar high {last_high:.2f} >= stop "
                        f"{trailing_stop_price:.2f}"),
            )

        # --- Death cross: exit long if open, then open short if flat ---
        if last_cross == -1:
            if currently_long:
                return Signal(
                    symbol=symbol, action=SignalAction.EXIT_LONG, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"{self.fast_period} EMA ({fast_val:.2f}) crossed below "
                            f"{self.slow_period} EMA ({slow_val:.2f}); exit long"),
                )
            if not currently_short:
                return Signal(
                    symbol=symbol, action=SignalAction.SHORT_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"{self.fast_period} EMA ({fast_val:.2f}) crossed below "
                            f"{self.slow_period} EMA ({slow_val:.2f}); enter short"),
                )

        # --- Golden cross: exit short if open, then open long if flat ---
        if last_cross == 1:
            if currently_short:
                return Signal(
                    symbol=symbol, action=SignalAction.EXIT_SHORT, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"{self.fast_period} EMA ({fast_val:.2f}) crossed above "
                            f"{self.slow_period} EMA ({slow_val:.2f}); exit short"),
                )
            if not currently_long:
                return Signal(
                    symbol=symbol, action=SignalAction.LONG_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"{self.fast_period} EMA ({fast_val:.2f}) crossed above "
                            f"{self.slow_period} EMA ({slow_val:.2f}); enter long"),
                )

        return Signal(
            symbol=symbol, action=SignalAction.HOLD, price=last_close,
            atr=last_atr, bar_timestamp=last_ts,
            reason="no new cross / stop not hit",
        )

    def initial_trailing_stop(self, side: str, entry_price: float, entry_atr: float) -> float:
        offset = self.trailing_stop_atr_mult * entry_atr
        return entry_price - offset if side == "long" else entry_price + offset

    def update_trailing_stop(self, side: str, current_stop: float,
                              latest_close: float, latest_atr: float) -> float:
        offset = self.trailing_stop_atr_mult * latest_atr
        if side == "long":
            candidate = latest_close - offset
            return max(current_stop, candidate)
        else:
            candidate = latest_close + offset
            return min(current_stop, candidate)
