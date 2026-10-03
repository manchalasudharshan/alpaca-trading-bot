"""
bot/strategies/momentum_breakout.py

Strategy 2 -- Momentum Breakout (BTC/USD), 1-hour candles.

Logic
-----
- Track the 20-period rolling high and low (computed on bars EXCLUDING the
  current/last bar, i.e. "has the latest close broken the prior channel").
- Long entry: last close > prior 20-period high AND last volume >= 1.5x the
  20-period average volume.
- Short entry / exit-long: last close < prior 20-period low AND same volume
  confirmation. Since this is a long/flip style breakout system, a short
  signal both closes an existing long and (if flat) opens a new short.
- Trailing stop: 2x ATR(14), recalculated on every bar and only ever
  tightened in the position's favor (standard trailing-stop ratchet). The
  strategy module computes where the stop *would* be; portfolio.py is
  responsible for persisting the ratcheted stop level and triggering the
  actual exit if price breaches it intrabar (checked against each new bar's
  low/high here since we're working off closed candles, not tick data).
"""

import logging

import pandas as pd

from bot.indicators import rolling_high, rolling_low, rolling_avg_volume, atr
from bot.params_store import load_strategy_params
from bot.strategies.base import Signal, SignalAction
import config

logger = logging.getLogger(__name__)


class MomentumBreakoutStrategy:
    name = "momentum_breakout"

    def __init__(self, params: dict = None):
        # See MeanReversionStrategy.__init__ for the params precedence
        # (explicit dict > strategy_params.json > config.py default).
        self.params = params if params is not None else load_strategy_params(
            "momentum_breakout", config.MOMENTUM_BREAKOUT_PARAMS
        )
        self.lookback = self.params["lookback"]
        self.volume_multiple = self.params["volume_multiple"]
        self.atr_period = self.params["atr_period"]
        self.trailing_stop_atr_mult = self.params["trailing_stop_atr_multiple"]

    def required_bars(self) -> int:
        return max(self.lookback, self.atr_period) + 5

    def generate_signal(self, symbol: str, bars: pd.DataFrame,
                         currently_long: bool, currently_short: bool,
                         trailing_stop_price: float = None) -> Signal:
        """
        trailing_stop_price: the currently-active trailing stop for this
            symbol's open position (None if flat). Used to check whether the
            latest closed bar breached the stop.
        """
        if len(bars) < self.required_bars() + 1:
            return Signal(
                symbol=symbol, action=SignalAction.HOLD, price=float(bars["close"].iloc[-1]),
                atr=float("nan"), reason="insufficient history for breakout calc",
            )

        close = bars["close"]
        high = bars["high"]
        low = bars["low"]
        volume = bars["volume"]

        # Prior channel = high/low/avg-volume computed on all bars EXCLUDING
        # the most recent one, so the breakout is measured against the
        # established range, not a range that already includes this bar.
        prior_high = rolling_high(high, self.lookback).shift(1)
        prior_low = rolling_low(low, self.lookback).shift(1)
        prior_avg_vol = rolling_avg_volume(volume, self.lookback).shift(1)
        atr_series = atr(bars, self.atr_period)

        last_close = float(close.iloc[-1])
        last_low = float(low.iloc[-1])
        last_high = float(high.iloc[-1])
        last_volume = float(volume.iloc[-1])
        last_atr = float(atr_series.iloc[-1])
        last_ts = bars.index[-1]

        p_high = float(prior_high.iloc[-1])
        p_low = float(prior_low.iloc[-1])
        p_avg_vol = float(prior_avg_vol.iloc[-1])

        volume_confirmed = last_volume >= self.volume_multiple * p_avg_vol
        breaks_up = last_close > p_high and volume_confirmed
        breaks_down = last_close < p_low and volume_confirmed

        # Debug-level evaluation trace: purely additive logging, does not
        # affect any decision below.
        vol_ratio = last_volume / p_avg_vol if p_avg_vol else float("nan")
        if breaks_up:
            trace_verdict = "LONG signal: breakout confirmed"
        elif breaks_down:
            trace_verdict = "SHORT/exit signal: breakdown confirmed"
        elif not volume_confirmed:
            trace_verdict = "no signal: volume not confirmed"
        else:
            trace_verdict = "no signal: price within prior range"
        logger.debug(
            "%s momentum_breakout: close=%.2f prior_%d-bar_range=[%.2f, %.2f], "
            "current volume %.2fx %d-bar avg (%.2f), need >=%.2fx -> %s",
            symbol, last_close, self.lookback, p_low, p_high, vol_ratio, self.lookback,
            p_avg_vol, self.volume_multiple, trace_verdict,
        )

        # --- Trailing stop check takes priority over fresh entries ---
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

        # --- Breakdown: exit long if open, else consider fresh short ---
        if breaks_down:
            if currently_long:
                return Signal(
                    symbol=symbol, action=SignalAction.EXIT_LONG, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"close {last_close:.2f} broke below {self.lookback}-period low "
                            f"{p_low:.2f} w/ volume {last_volume:.2f} >= "
                            f"{self.volume_multiple}x avg {p_avg_vol:.2f}"),
                )
            if not currently_short:
                return Signal(
                    symbol=symbol, action=SignalAction.SHORT_ENTRY, price=last_close,
                    atr=last_atr, bar_timestamp=last_ts,
                    reason=(f"close {last_close:.2f} broke below {self.lookback}-period low "
                            f"{p_low:.2f} w/ volume confirmation"),
                )

        # --- Breakout: fresh long if flat ---
        if breaks_up and not currently_long and not currently_short:
            return Signal(
                symbol=symbol, action=SignalAction.LONG_ENTRY, price=last_close,
                atr=last_atr, bar_timestamp=last_ts,
                reason=(f"close {last_close:.2f} broke above {self.lookback}-period high "
                        f"{p_high:.2f} w/ volume {last_volume:.2f} >= "
                        f"{self.volume_multiple}x avg {p_avg_vol:.2f}"),
            )

        return Signal(
            symbol=symbol, action=SignalAction.HOLD, price=last_close,
            atr=last_atr, bar_timestamp=last_ts,
            reason="no breakout / stop not hit",
        )

    def initial_trailing_stop(self, side: str, entry_price: float, entry_atr: float) -> float:
        """Stop level immediately after entry, before any ratcheting."""
        offset = self.trailing_stop_atr_mult * entry_atr
        return entry_price - offset if side == "long" else entry_price + offset

    def update_trailing_stop(self, side: str, current_stop: float,
                              latest_close: float, latest_atr: float) -> float:
        """
        Ratchet the trailing stop in the position's favor only. Never moves
        against the position.
        """
        offset = self.trailing_stop_atr_mult * latest_atr
        if side == "long":
            candidate = latest_close - offset
            return max(current_stop, candidate)
        else:  # short
            candidate = latest_close + offset
            return min(current_stop, candidate)
