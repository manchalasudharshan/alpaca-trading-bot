"""
bot/main.py

Entry point. Wires together the broker, the three strategy modules, the
risk manager, and the portfolio/logger, then runs a continuous loop that:

  1. Wakes up every config.POLL_INTERVAL_SECONDS.
  2. For each instrument, checks whether a new bar has closed on its
     strategy's timeframe (15Min / 1Hour / 4Hour). If so, pulls fresh bars
     and asks that instrument's strategy for a signal.
  3. Runs every signal through the correlation filter and ATR-based
     position sizing in risk_manager.py.
  4. Executes allowed trades via the broker, updates portfolio.py (which
     handles trades.csv / daily_pnl.csv logging), and ratchets trailing
     stops on open positions every cycle.
  5. Respects equity market hours (SPY, QQQ, GLD, USO) vs. crypto's 24/7
     schedule (BTC/USD), and handles API/network errors without crashing.

Run with:  python -m bot.main
"""

import logging
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import config
from bot.broker import AlpacaBroker
from bot.portfolio import Portfolio
from bot.risk_manager import RiskManager
from bot.strategies.base import Signal, SignalAction
from bot.strategies.mean_reversion import MeanReversionStrategy
from bot.strategies.momentum_breakout import MomentumBreakoutStrategy
from bot.strategies.trend_following import TrendFollowingStrategy

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.BOT_LOG_PATH),
    ],
)
logger = logging.getLogger("bot.main")

# Timeframe -> approximate bar duration in seconds, used to decide when a
# new bar has likely closed and it's worth re-fetching data for that
# instrument.
TIMEFRAME_SECONDS = {
    "15Min": 15 * 60,
    "1Hour": 60 * 60,
    "4Hour": 4 * 60 * 60,
}

STRATEGY_CLASSES = {
    "mean_reversion": MeanReversionStrategy,
    "momentum_breakout": MomentumBreakoutStrategy,
    "trend_following": TrendFollowingStrategy,
}


class TradingBot:
    def __init__(self):
        logger.info("Initializing trading bot...")
        self.broker = AlpacaBroker()
        self.portfolio = Portfolio()
        self.risk_manager = RiskManager()
        self.strategies = {name: cls() for name, cls in STRATEGY_CLASSES.items()}

        # Tracks the timestamp of the last bar we already acted on, per
        # symbol, so we don't re-process the same closed bar repeatedly
        # every poll cycle.
        self.last_seen_bar: Dict[str, Optional[datetime]] = {
            inst.symbol: None for inst in config.INSTRUMENTS
        }

        self._shutdown = False
        signal.signal(signal.SIGINT, self._handle_shutdown)
        signal.signal(signal.SIGTERM, self._handle_shutdown)

        logger.info("Bot initialized. Trading instruments: %s",
                    [i.symbol for i in config.INSTRUMENTS])

    def _handle_shutdown(self, signum, frame):
        logger.info("Shutdown signal received (%s). Finishing current cycle then exiting.", signum)
        self._shutdown = True

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run(self):
        logger.info("Starting main loop. Poll interval=%ds", config.POLL_INTERVAL_SECONDS)
        last_day_flushed = datetime.now(timezone.utc).date()

        while not self._shutdown:
            cycle_start = time.time()
            try:
                self._run_cycle()
            except Exception as e:  # noqa: BLE001 -- never let the loop die
                logger.exception("Unhandled error during trading cycle: %s", e)

            # End-of-day P&L flush, once per UTC calendar day.
            today = datetime.now(timezone.utc).date()
            if today != last_day_flushed:
                try:
                    self.portfolio.end_of_day_flush()
                except Exception as e:
                    logger.error("Failed end-of-day P&L flush: %s", e)
                last_day_flushed = today

            elapsed = time.time() - cycle_start
            sleep_for = max(1.0, config.POLL_INTERVAL_SECONDS - elapsed)
            if self._shutdown:
                break
            time.sleep(sleep_for)

        logger.info("Bot shut down cleanly.")

    def _run_cycle(self):
        equity_market_open = self._safe_is_equity_market_open()

        for inst in config.INSTRUMENTS:
            is_crypto = inst.asset_class == config.CRYPTO
            if not is_crypto and not equity_market_open:
                # Equities: skip signal generation outside market hours.
                # Crypto trades 24/7 and is never skipped here.
                continue

            try:
                self._process_instrument(inst)
            except Exception as e:  # noqa: BLE001 -- isolate per-instrument failures
                logger.exception("Error processing %s: %s", inst.symbol, e)

    def _safe_is_equity_market_open(self) -> bool:
        try:
            return self.broker.is_equity_market_open()
        except Exception as e:
            logger.error("Could not determine market status, assuming CLOSED: %s", e)
            return False

    # ------------------------------------------------------------------
    # Per-instrument processing
    # ------------------------------------------------------------------
    def _process_instrument(self, inst: "config.Instrument"):
        strategy = self.strategies[inst.strategy]
        required = strategy.required_bars() + 2

        bars = self.broker.get_bars(
            symbol=inst.symbol, asset_class=inst.asset_class,
            timeframe=inst.timeframe, limit=max(required, config.BARS_LIMIT),
        )
        if bars.empty or len(bars) < 2:
            logger.debug("Not enough bars yet for %s (%s)", inst.symbol, inst.timeframe)
            return

        latest_bar_ts = bars.index[-1]
        if self.last_seen_bar[inst.symbol] is not None and latest_bar_ts <= self.last_seen_bar[inst.symbol]:
            # No new closed bar since we last acted on this symbol --
            # nothing to do until the next one forms.
            return

        position = self.portfolio.get_position(inst.symbol)

        # --- Universal hard-stop check, independent of strategy logic ---
        # Mean reversion's own exit rule only fires when price reverts to
        # the mean, which may never happen. The risk manager's 1-ATR hard
        # stop set at entry (position.stop_price) must be enforced as a
        # hard ceiling on loss regardless of what the strategy's signal
        # says, so it's checked first, every new bar, before the strategy
        # is even asked for a signal. (Momentum/trend already enforce an
        # equal-or-tighter stop inside their own generate_signal, so this
        # is a no-op double-check for them, not a behavior change.)
        if position is not None:
            last_low = float(bars["low"].iloc[-1])
            last_high = float(bars["high"].iloc[-1])
            hard_stop_hit = ((position.side == "long" and last_low <= position.stop_price) or
                              (position.side == "short" and last_high >= position.stop_price))
            if hard_stop_hit:
                logger.warning(
                    "Hard stop breached for %s: bar %s-%s crossed stop %.4f. Forcing exit.",
                    inst.symbol, last_low, last_high, position.stop_price,
                )
                self.last_seen_bar[inst.symbol] = latest_bar_ts
                stop_signal = Signal(
                    symbol=inst.symbol, action=SignalAction.EXIT_LONG,  # action unused by _exit_position
                    price=position.stop_price, atr=float("nan"),
                    reason=f"hard stop hit: bar breached {position.stop_price:.4f}",
                )
                self._exit_position(inst, stop_signal)
                return

        currently_long = position is not None and position.side == "long"
        currently_short = position is not None and position.side == "short"
        trailing_stop = position.stop_price if position is not None else None

        # Strategy-specific call signature: mean reversion doesn't use a
        # trailing stop (exits at the mean instead), the other two do.
        if inst.strategy == "mean_reversion":
            sig = strategy.generate_signal(inst.symbol, bars, currently_long, currently_short)
        else:
            sig = strategy.generate_signal(inst.symbol, bars, currently_long, currently_short,
                                            trailing_stop_price=trailing_stop)

        self.last_seen_bar[inst.symbol] = latest_bar_ts

        # Ratchet the trailing stop on open positions every new bar, even
        # when the signal itself is HOLD (so the stop keeps tightening in
        # the position's favor between entry/exit events).
        if position is not None and inst.strategy in ("momentum_breakout", "trend_following"):
            new_stop = strategy.update_trailing_stop(
                position.side, position.stop_price, float(bars["close"].iloc[-1]), sig.atr,
            )
            if new_stop != position.stop_price:
                self.portfolio.update_stop(inst.symbol, new_stop)
                logger.debug("Ratcheted trailing stop for %s: %.4f -> %.4f",
                             inst.symbol, position.stop_price, new_stop)

        if sig.action == SignalAction.HOLD:
            return

        logger.info("Signal for %s [%s]: %s -- %s", inst.symbol, inst.strategy,
                    sig.action.value, sig.reason)

        self._handle_signal(inst, sig)

    # ------------------------------------------------------------------
    # Signal -> order execution
    # ------------------------------------------------------------------
    def _handle_signal(self, inst: "config.Instrument", sig):
        strategy = self.strategies[inst.strategy]

        if sig.action in (SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT):
            self._exit_position(inst, sig)
            return

        if sig.action in (SignalAction.LONG_ENTRY, SignalAction.SHORT_ENTRY):
            side = "long" if sig.action == SignalAction.LONG_ENTRY else "short"

            # --- Correlation filter ---
            open_positions = self.portfolio.open_positions_snapshot()
            if self.risk_manager.correlation_filter_blocks(inst.symbol, side, open_positions):
                logger.info("Blocked %s %s entry by correlation filter.", inst.symbol, side)
                return

            # --- ATR-based position sizing + hard stop ---
            equity = self._safe_get_equity()
            if equity is None:
                logger.error("Could not fetch account equity; skipping entry for %s.", inst.symbol)
                return

            sizing = self.risk_manager.size_position(
                symbol=inst.symbol, side=side, entry_price=sig.price, atr=sig.atr,
                account_equity=equity, asset_class=inst.asset_class,
            )
            if not sizing.allowed:
                logger.warning("Sizing rejected trade for %s: %s", inst.symbol, sizing.reason)
                return

            logger.info(sizing.reason)

            # Initial stop: use the strategy's own trailing-stop formula
            # where it has one (breakout/trend), but never let it be wider
            # than the risk-manager's hard stop -- the tighter of the two
            # wins, enforcing the "max 1% loss, no exceptions" rule.
            if inst.strategy in ("momentum_breakout", "trend_following"):
                strat_stop = strategy.initial_trailing_stop(side, sig.price, sig.atr)
                stop_price = self._tighter_stop(side, sizing.stop_price, strat_stop)
            else:
                stop_price = sizing.stop_price

            if not self.risk_manager.validate_stop_within_cap(
                side, sig.price, stop_price, sizing.qty, equity
            ):
                logger.warning("Stop validation failed for %s; skipping entry.", inst.symbol)
                return

            order_side = "buy" if side == "long" else "sell"
            order = self.broker.submit_market_order(
                symbol=inst.symbol, qty=sizing.qty, side=order_side, asset_class=inst.asset_class,
            )
            if order is None:
                logger.error("Order submission failed for %s; not recording a position.", inst.symbol)
                return

            self.portfolio.open_position(
                symbol=inst.symbol, side=side, qty=sizing.qty, entry_price=sig.price,
                entry_atr=sig.atr, stop_price=stop_price, strategy=inst.strategy,
            )

    def _exit_position(self, inst, sig):
        position = self.portfolio.get_position(inst.symbol)
        if position is None:
            logger.debug("Exit signal for %s but no open position tracked; ignoring.", inst.symbol)
            return

        order_side = "sell" if position.side == "long" else "buy"
        order = self.broker.submit_market_order(
            symbol=inst.symbol, qty=position.qty, side=order_side, asset_class=inst.asset_class,
        )
        if order is None:
            logger.error("Exit order FAILED for %s -- position remains open in local state. "
                         "Will retry on next signal.", inst.symbol)
            return

        self.portfolio.close_position(inst.symbol, exit_price=sig.price, exit_reason=sig.reason)

    @staticmethod
    def _tighter_stop(side: str, stop_a: float, stop_b: float) -> float:
        if side == "long":
            return max(stop_a, stop_b)  # higher stop = tighter for a long
        else:
            return min(stop_a, stop_b)  # lower stop = tighter for a short

    def _safe_get_equity(self) -> Optional[float]:
        try:
            return self.broker.get_equity()
        except Exception as e:
            logger.error("Failed to fetch account equity: %s", e)
            return None


def main():
    bot = TradingBot()
    bot.run()


if __name__ == "__main__":
    main()
