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

import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import config
from bot import params_store
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
        # Tracks strategy_params.json's mtime so a long-running `python -m
        # bot.main` process (unlike bot/live_tick.py, which gets this for
        # free every tick by constructing a brand-new TradingBot()) still
        # picks up bot/auto_tune.py's parameter updates without a restart.
        # This only ever re-instantiates the strategy objects above (their
        # own signal/indicator params) -- it never touches risk_manager,
        # the correlation filter, hard-stop logic, or the circuit breaker.
        self._strategy_params_mtime_seen = self._strategy_params_mtime()

        # Tracks the timestamp of the last bar we already acted on, per
        # symbol, so we don't re-process the same closed bar repeatedly
        # every poll cycle.
        self.last_seen_bar: Dict[str, Optional[datetime]] = {
            inst.symbol: None for inst in config.INSTRUMENTS
        }
        # High-water mark for total account equity, used to report portfolio
        # drawdown from peak. Persisted across restarts via state.json (see
        # save_state/load_state) since a fresh process otherwise has no
        # memory of equity highs reached in prior ticks.
        self.peak_equity: Optional[float] = None

        # Max-drawdown circuit breaker state. Once tripped (current equity
        # <= peak_equity * (1 - config.MAX_DRAWDOWN_PCT)), ALL open
        # positions are closed and `trading_halted` stays True forever --
        # there is no automatic resume logic anywhere in this class. A
        # human must manually clear it in bot_state.json (see README
        # "Circuit breaker" section) before the bot will trade again.
        # Persisted across restarts the same way peak_equity is.
        self.trading_halted: bool = False
        self.halt_reason: Optional[str] = None
        self.halt_at: Optional[str] = None

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
        self.load_state(config.STATE_FILE_PATH)

        while not self._shutdown:
            cycle_start = time.time()
            self.run_once()
            try:
                self.save_state(config.STATE_FILE_PATH)
            except Exception as e:
                logger.error("Failed to save state: %s", e)

            elapsed = time.time() - cycle_start
            sleep_for = max(1.0, config.POLL_INTERVAL_SECONDS - elapsed)
            if self._shutdown:
                break
            time.sleep(sleep_for)

        logger.info("Bot shut down cleanly.")

    def run_once(self):
        """
        Runs exactly one cycle and the end-of-day flush check, then returns
        -- no sleep, no loop. Used by bot/live_tick.py, where a GitHub
        Actions cron schedule re-invokes a fresh process every few minutes
        instead of one process looping forever (hosted runners cap a single
        job at a few hours, far short of the "run for weeks" requirement).
        """
        self._reload_strategies_if_params_changed()

        # --- Circuit breaker gate: checked first, before anything else. ---
        # If a prior cycle (this process or an earlier one, via bot_state.json)
        # tripped the max-drawdown circuit breaker, do not evaluate any new
        # signals or open any new trades. bot/live_tick.py still writes
        # positions_snapshot.json after this returns, so monitoring/reporting
        # keeps working -- only new-trade evaluation stops. No code anywhere
        # clears this flag automatically; see README "Circuit breaker".
        if self.trading_halted:
            logger.critical(
                "Trading is HALTED (circuit breaker tripped at %s: %s). Skipping this "
                "cycle entirely -- manual review and a bot_state.json edit are required "
                "to resume. See README.md 'Circuit breaker' section.",
                self.halt_at, self.halt_reason,
            )
            return

        try:
            self._run_cycle()
        except Exception as e:  # noqa: BLE001 -- never let a tick crash the workflow
            logger.exception("Unhandled error during trading cycle: %s", e)

        today = datetime.now(timezone.utc).date()
        if getattr(self, "_last_day_flushed", None) != today:
            try:
                self.portfolio.end_of_day_flush()
            except Exception as e:
                logger.error("Failed end-of-day P&L flush: %s", e)
            self._last_day_flushed = today

    # ------------------------------------------------------------------
    # State persistence (open positions, last-seen bars, peak equity) --
    # see Portfolio.to_state_dict/load_state_dict for why this exists.
    # ------------------------------------------------------------------
    def save_state(self, path: str):
        state = {
            "portfolio": self.portfolio.to_state_dict(),
            "last_seen_bar": {
                sym: (ts.isoformat() if ts is not None else None)
                for sym, ts in self.last_seen_bar.items()
            },
            "peak_equity": self.peak_equity,
            # Circuit breaker state -- see README "Circuit breaker". To
            # manually resume trading after a halt, a human edits this file
            # and sets "trading_halted": false (clearing halt_reason/halt_at
            # is optional but recommended for a clean audit trail).
            "trading_halted": self.trading_halted,
            "halt_reason": self.halt_reason,
            "halt_at": self.halt_at,
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp_path = path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, path)
        logger.info("Saved bot state to %s", path)

    def load_state(self, path: str):
        if not os.path.exists(path):
            logger.info("No existing state file at %s; starting fresh.", path)
            return
        with open(path, "r") as f:
            state = json.load(f)
        self.portfolio.load_state_dict(state.get("portfolio", {}))
        for sym, ts in state.get("last_seen_bar", {}).items():
            self.last_seen_bar[sym] = datetime.fromisoformat(ts) if ts else None
        self.peak_equity = state.get("peak_equity")
        self.trading_halted = bool(state.get("trading_halted", False))
        self.halt_reason = state.get("halt_reason")
        self.halt_at = state.get("halt_at")
        logger.info("Loaded bot state from %s (peak_equity=%s, trading_halted=%s)",
                    path, self.peak_equity, self.trading_halted)
        if self.trading_halted:
            logger.critical(
                "Loaded state with trading HALTED (tripped at %s: %s). Will not trade "
                "until bot_state.json is manually edited. See README 'Circuit breaker'.",
                self.halt_at, self.halt_reason,
            )

    def _run_cycle(self):
        equity_market_open = self._safe_is_equity_market_open()

        current_equity = self._safe_get_equity()
        if current_equity is not None:
            if self.peak_equity is None or current_equity > self.peak_equity:
                self.peak_equity = current_equity

            # --- Max-drawdown circuit breaker ---
            # Checked every cycle, independent of and in addition to the
            # existing 1%-per-trade/ATR sizing, correlation filter, and
            # hard-stop logic below (none of that is touched by this).
            if (not self.trading_halted and self.peak_equity and self.peak_equity > 0
                    and current_equity <= self.peak_equity * (1 - config.MAX_DRAWDOWN_PCT)):
                self._trip_circuit_breaker(current_equity)
                return  # halted: skip all instrument/new-entry processing this cycle

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

    # ------------------------------------------------------------------
    # Strategy param hot-reload (strategy_params.json, written by
    # bot/auto_tune.py) -- purely about each strategy's own signal/
    # indicator params; never touches risk_manager, the correlation
    # filter, hard-stop logic, or the circuit breaker below.
    # ------------------------------------------------------------------
    @staticmethod
    def _strategy_params_mtime() -> Optional[float]:
        try:
            return os.path.getmtime(params_store.PARAMS_FILE_PATH)
        except OSError:
            return None

    def _reload_strategies_if_params_changed(self):
        mtime = self._strategy_params_mtime()
        if mtime == self._strategy_params_mtime_seen:
            return
        if self._strategy_params_mtime_seen is not None:
            logger.info(
                "strategy_params.json changed on disk; reloading strategy parameters."
            )
        self.strategies = {name: cls() for name, cls in STRATEGY_CLASSES.items()}
        self._strategy_params_mtime_seen = mtime

    def _safe_is_equity_market_open(self) -> bool:
        try:
            return self.broker.is_equity_market_open()
        except Exception as e:
            logger.error("Could not determine market status, assuming CLOSED: %s", e)
            return False

    # ------------------------------------------------------------------
    # Max-drawdown circuit breaker
    # ------------------------------------------------------------------
    def _trip_circuit_breaker(self, current_equity: float):
        """
        Closes every open position immediately and permanently halts new
        trading (no automatic resume -- see README "Circuit breaker"). This
        is purely additive: the 1%-per-trade/ATR sizing, correlation
        filter, and per-position hard-stop logic are untouched and simply
        never get a chance to run again once `trading_halted` is True,
        because run_once()/`_run_cycle()` gate on it first.
        """
        drawdown_pct = (self.peak_equity - current_equity) / self.peak_equity * 100.0
        reason = (
            f"Equity {current_equity:.2f} is {drawdown_pct:.2f}% below peak equity "
            f"{self.peak_equity:.2f}, exceeding the {config.MAX_DRAWDOWN_PCT * 100:.1f}% "
            f"max-drawdown circuit-breaker threshold (config.MAX_DRAWDOWN_PCT)."
        )
        logger.critical(
            "CIRCUIT BREAKER TRIPPED: %s Closing ALL open positions and halting ALL "
            "new trading. This will NOT auto-resume -- a human must manually review and "
            "edit bot_state.json ('trading_halted': false) to continue trading. See "
            "README.md 'Circuit breaker' section.",
            reason,
        )

        self._close_all_positions(exit_reason="circuit_breaker_max_drawdown")

        self.trading_halted = True
        self.halt_reason = reason
        self.halt_at = datetime.now(timezone.utc).isoformat()

        # Persist immediately so the halt survives even if the process is
        # killed before the normal end-of-cycle save_state() call (run()'s
        # loop and live_tick.py both call save_state() after run_once(),
        # but a halt is important enough to not rely on that alone).
        try:
            self.save_state(config.STATE_FILE_PATH)
        except Exception as e:
            logger.error("Failed to persist circuit-breaker halt state: %s", e)

    def _close_all_positions(self, exit_reason: str):
        """
        Flattens everything at the broker first (authoritative, catches any
        position the local book doesn't know about), then reconciles
        bot/portfolio.py's local book + trades.csv/daily_pnl.csv so the
        local record matches reality and every closed trade is logged.
        """
        self.broker.close_all_positions(cancel_orders=True)

        for symbol in list(self.portfolio.positions.keys()):
            position = self.portfolio.positions.get(symbol)
            if position is None:
                continue
            inst = config.INSTRUMENT_BY_SYMBOL.get(symbol)
            asset_class = inst.asset_class if inst else config.EQUITY
            exit_price = self.broker.get_last_trade_price(symbol, asset_class)
            if exit_price is None:
                # Last resort so we still log a closed trade rather than
                # leaving local state stuck "open" after the broker has
                # already flattened it.
                exit_price = position.entry_price
                logger.warning(
                    "Circuit breaker: could not fetch last trade price for %s; "
                    "logging local close at entry price %.4f as an estimate.",
                    symbol, exit_price,
                )
            self.portfolio.close_position(symbol, exit_price=exit_price, exit_reason=exit_reason)

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
                equity_at_entry=equity,
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
