"""
tests/test_circuit_breaker.py

Covers the max-portfolio-drawdown circuit breaker in bot/main.py:

  1. It trips (closes all positions, halts trading) once equity drops more
     than config.MAX_DRAWDOWN_PCT below peak_equity.
  2. It stays halted on every subsequent cycle, even once equity recovers
     and even across a fresh process that reloads bot_state.json -- there
     is no automatic resume.
  3. It resumes trading ONLY after a human manually clears the
     "trading_halted" flag (simulated here as a manual bot_state.json edit).

None of these tests touch the network: bot/broker.py's AlpacaBroker is
replaced with a FakeBroker, and Portfolio's CSV paths are redirected to a
pytest tmp_path so the real repo's trades.csv/daily_pnl.csv are untouched.
"""

import json

import pandas as pd
import pytest

import config
from bot.main import TradingBot


class FakeBroker:
    """A network-free stand-in for bot.broker.AlpacaBroker."""

    def __init__(self):
        self.equity = 100_000.0
        self.market_open = True
        self.close_all_calls = 0
        self.submitted_orders = []

    def is_equity_market_open(self):
        return self.market_open

    def get_equity(self):
        return self.equity

    def get_bars(self, symbol, asset_class, timeframe, limit):
        # Empty frame -> _process_instrument() returns immediately, so no
        # strategy/signal logic runs; these tests only exercise the
        # circuit-breaker gate, not signal generation.
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    def close_all_positions(self, cancel_orders=True):
        self.close_all_calls += 1
        return []

    def submit_market_order(self, symbol, qty, side, asset_class):
        self.submitted_orders.append((symbol, qty, side, asset_class))
        return object()  # truthy "order" stand-in

    def get_last_trade_price(self, symbol, asset_class):
        return 100.0


@pytest.fixture
def bot(tmp_path, monkeypatch):
    """A TradingBot wired to a FakeBroker and tmp_path-scoped state/CSVs."""
    monkeypatch.setattr("bot.main.AlpacaBroker", FakeBroker)
    monkeypatch.setattr(config, "STATE_FILE_PATH", str(tmp_path / "bot_state.json"))
    monkeypatch.setattr(config, "TRADES_CSV_PATH", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(config, "DAILY_PNL_CSV_PATH", str(tmp_path / "daily_pnl.csv"))
    return TradingBot()


def test_trips_and_closes_all_positions_on_over_threshold_drawdown(bot):
    bot.peak_equity = 100_000.0
    bot.broker.equity = 89_000.0  # -11% from peak, over the 10% default threshold

    bot.portfolio.open_position(
        symbol="SPY", side="long", qty=10, entry_price=400.0, entry_atr=2.0,
        stop_price=395.0, strategy="mean_reversion", equity_at_entry=100_000.0,
    )

    bot.run_once()

    assert bot.trading_halted is True
    assert bot.halt_reason is not None and "circuit-breaker" in bot.halt_reason
    assert bot.halt_at is not None
    assert bot.broker.close_all_calls == 1
    assert "SPY" not in bot.portfolio.positions  # reconciled locally too

    with open(config.STATE_FILE_PATH) as f:
        state = json.load(f)
    assert state["trading_halted"] is True
    assert state["halt_reason"]
    assert state["halt_at"]


def test_within_threshold_drawdown_does_not_trip(bot):
    bot.peak_equity = 100_000.0
    bot.broker.equity = 95_000.0  # -5%, within the 10% threshold

    bot.run_once()

    assert bot.trading_halted is False
    assert bot.broker.close_all_calls == 0


def test_stays_halted_on_subsequent_cycles_even_after_equity_recovers(bot):
    bot.peak_equity = 100_000.0
    bot.broker.equity = 84_000.0  # trips it

    bot.run_once()
    assert bot.trading_halted is True
    calls_after_trip = bot.broker.close_all_calls

    # Equity fully recovers above the old peak -- must NOT auto-resume.
    bot.broker.equity = 150_000.0
    bot.run_once()
    bot.run_once()

    assert bot.trading_halted is True
    # No new close-all calls and no new entry orders once halted: run_once()
    # returns before _run_cycle() (and thus before any instrument/order
    # logic) ever runs again.
    assert bot.broker.close_all_calls == calls_after_trip
    assert bot.broker.submitted_orders == []


def test_resumes_only_after_manual_flag_clear_in_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr("bot.main.AlpacaBroker", FakeBroker)
    monkeypatch.setattr(config, "STATE_FILE_PATH", str(tmp_path / "bot_state.json"))
    monkeypatch.setattr(config, "TRADES_CSV_PATH", str(tmp_path / "trades.csv"))
    monkeypatch.setattr(config, "DAILY_PNL_CSV_PATH", str(tmp_path / "daily_pnl.csv"))

    # --- Process 1: trips the breaker and persists the halt. ---
    bot1 = TradingBot()
    bot1.peak_equity = 100_000.0
    bot1.broker.equity = 80_000.0
    bot1.run_once()
    assert bot1.trading_halted is True

    # --- Process 2: a fresh process loads the persisted halted state and
    # must stay halted without anyone touching it. ---
    bot2 = TradingBot()
    bot2.load_state(config.STATE_FILE_PATH)
    assert bot2.trading_halted is True
    bot2.broker.equity = 120_000.0
    bot2.run_once()
    assert bot2.trading_halted is True
    assert bot2.broker.close_all_calls == 0  # never re-evaluated; gated at the top

    # --- A human manually clears the flag in bot_state.json. ---
    with open(config.STATE_FILE_PATH) as f:
        state = json.load(f)
    state["trading_halted"] = False
    state["halt_reason"] = None
    state["halt_at"] = None
    with open(config.STATE_FILE_PATH, "w") as f:
        json.dump(state, f)

    # --- Process 3: loads the manually-cleared state and trades normally
    # again. ---
    bot3 = TradingBot()
    bot3.load_state(config.STATE_FILE_PATH)
    assert bot3.trading_halted is False

    bot3.broker.equity = 120_000.0
    bot3.run_once()  # should run a normal cycle, no exception, stays un-halted
    assert bot3.trading_halted is False
