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

Per-symbol params
-----------------
fast_ema/slow_ema/trailing_stop_atr_multiple are per-symbol dicts (e.g.
{"GLD": 50, "USO": 50}), same convention as MeanReversionStrategy's
entry_std_dev -- see config.TREND_FOLLOWING_PARAMS for the 2026-10-05
rationale (GLD and USO had opposite-sign expectancy on the same shared
params, so they need to be independently tunable). atr_period stays a
single shared scalar (not split) since it's a measurement window, not an
entry/exit threshold. initial_trailing_stop()/update_trailing_stop() take
an optional `symbol` so the shared strategy instance (one per strategy,
not one per instrument -- see bot/backtest.py's STRATEGY_CLASSES) can
resolve the right multiple; omitting it falls back to a hardcoded default,
which only matters for callers that predate this change.
"""

import logging

import pandas as pd

from bot.indicators import ema, ema_cross, atr
from bot.params_store import load_strategy_params
from bot.strategies.base import Signal, SignalAction
import config

logger = logging.getLogger(__name__)


class TrendFollowingStrategy:
    name = "trend_following"

    def __init__(self, params: dict = None):
        # See MeanReversionStrategy.__init__ for the params precedence
        # (explicit dict > strategy_params.json > config.py default).
        self.params = params if params is not None else load_strategy_params(
            "trend_following", config.TREND_FOLLOWING_PARAMS
        )
        # These may be per-symbol dicts (e.g. {"GLD": 50, "USO": 50}) or,
        # defensively, a bare scalar (legacy data / a test passing a flat
        # dict) -- _resolve() below handles both.
        self.fast_ema = self.params["fast_ema"]
        self.slow_ema = self.params["slow_ema"]
        self.atr_period = self.params["atr_period"]
        self.trailing_stop_atr_multiple = self.params["trailing_stop_atr_multiple"]

    @staticmethod
    def _resolve(value, symbol: str, default: float):
        """Per-symbol dict -> value for `symbol` (falling back to `default`
        if this symbol has no entry); bare scalar -> itself, unchanged."""
        if isinstance(value, dict):
            return value.get(symbol, default)
        return value

    def required_bars(self) -> int:
        # 200-period EMA needs real warmup to be meaningful; pandas' ewm
        # with min_periods=slow_period handles the NaN-until-warm part, but
        # we want a bit of history past that so the cross detection itself
        # is on fully-warmed values. slow_ema may be per-symbol and this is
        # called with no symbol context (see bot/backtest.py), so use the
        # largest configured value -- conservative (more warmup than some
        # symbols strictly need), never insufficient.
        slow_values = self.slow_ema.values() if isinstance(self.slow_ema, dict) else [self.slow_ema]
        return max(slow_values) + 10

    def generate_signal(self, symbol: str, bars: pd.DataFrame,
                         currently_long: bool, currently_short: bool,
                         trailing_stop_price: float = None) -> Signal:
        if len(bars) < self.required_bars():
            return Signal(
                symbol=symbol, action=SignalAction.HOLD, price=float(bars["close"].iloc[-1]),
                atr=float("nan"), reason="insufficient history for EMA warmup (need 200+ bars)",
            )

        fast_period = self._resolve(self.fast_ema, symbol, 50)
        slow_period = self._resolve(self.slow_ema, symbol, 200)

        close = bars["close"]
        fast = ema(close, fast_period)
        slow = ema(close, slow_period)
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

        # Debug-level evaluation trace: purely additive logging, does not
        # affect any decision below.
        if last_cross == 1:
            trace_verdict = "LONG signal: golden cross"
        elif last_cross == -1:
            trace_verdict = "SHORT signal: death cross"
        else:
            trace_verdict = "no signal: no new cross"
        logger.debug(
            "%s trend_following: close=%.2f EMA%d=%.2f EMA%d=%.2f (spread=%.2f) -> %s",
            symbol, last_close, fast_period, fast_val, slow_period, slow_val,
            fast_val - slow_val, trace_verdict,
        )

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
                    reason=(f"{fast_period} EMA ({fast_val:.2f}) crossed below "
                            f"{slow_period} EMA ({slow_val:.2f}); exit long"),
                )
            if not currently_short:
                return Signal(
                    symbol=symbol, action=SignalAction.SHORT_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"{fast_period} EMA ({fast_val:.2f}) crossed below "
                            f"{slow_period} EMA ({slow_val:.2f}); enter short"),
                )

        # --- Golden cross: exit short if open, then open long if flat ---
        if last_cross == 1:
            if currently_short:
                return Signal(
                    symbol=symbol, action=SignalAction.EXIT_SHORT, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"{fast_period} EMA ({fast_val:.2f}) crossed above "
                            f"{slow_period} EMA ({slow_val:.2f}); exit short"),
                )
            if not currently_long:
                return Signal(
                    symbol=symbol, action=SignalAction.LONG_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"{fast_period} EMA ({fast_val:.2f}) crossed above "
                            f"{slow_period} EMA ({slow_val:.2f}); enter long"),
                )

        return Signal(
            symbol=symbol, action=SignalAction.HOLD, price=last_close,
            atr=last_atr, bar_timestamp=last_ts,
            reason="no new cross / stop not hit",
        )

    def initial_trailing_stop(self, side: str, entry_price: float, entry_atr: float,
                               symbol: str = None) -> float:
        mult = self._resolve(self.trailing_stop_atr_multiple, symbol, 3.0)
        offset = mult * entry_atr
        return entry_price - offset if side == "long" else entry_price + offset

    def update_trailing_stop(self, side: str, current_stop: float,
                              latest_close: float, latest_atr: float,
                              symbol: str = None) -> float:
        mult = self._resolve(self.trailing_stop_atr_multiple, symbol, 3.0)
        offset = mult * latest_atr
        if side == "long":
            candidate = latest_close - offset
            return max(current_stop, candidate)
        else:
            candidate = latest_close + offset
            return min(current_stop, candidate)
