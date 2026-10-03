"""
bot/backtest.py

Backtests all 3 strategies against historical Alpaca data for all 5
instruments, simulating realistic trading conditions:

  - 6 months of history per instrument, pulled at each strategy's own
    timeframe (15Min for SPY/QQQ, 1Hour for BTC/USD, 4Hour for GLD/USO).
  - The exact same strategy, risk-sizing, and trailing-stop logic used by
    the live bot (bot/strategies/*, bot/risk_manager.py) -- no separate
    "backtest version" of the trading rules, so results reflect what the
    live bot would actually do.
  - Slippage: 0.05% applied against the trader on every fill (entries pay
    up, exits give back) -- see `_apply_slippage`.
  - Commission: $0 (Alpaca is commission-free for equities and crypto).

Two kinds of results are produced:

  1. Per-instrument, strategy-isolated backtests -- each instrument trades
     alone against its own starting capital, so its stats reflect that
     strategy's edge on that instrument without cross-instrument effects.
     Reported: total trades, win rate, avg win, avg loss, profit factor,
     max drawdown, Sharpe ratio, total return.

  2. A combined portfolio backtest -- all 5 instruments trade together
     against one shared account, processed in true chronological order
     across timeframes, with the correlation filter actually live (BTC/USD
     longs get blocked whenever SPY and QQQ are both already long). This is
     what the live bot's actual behavior would look like.

Outputs:
  - Console summary table for both the per-instrument and combined results,
    with a clear WARNING flag on any strategy whose Sharpe ratio is
    negative.
  - backtest_results.png: equity curve chart (per-instrument + combined).
  - backtest_trades.csv: every simulated trade, for inspection.

Run with:  python -m bot.backtest
"""

import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import config
from bot.broker import AlpacaBroker
from bot.risk_manager import RiskManager
from bot.strategies.base import SignalAction
from bot.strategies.mean_reversion import MeanReversionStrategy
from bot.strategies.momentum_breakout import MomentumBreakoutStrategy
from bot.strategies.trend_following import TrendFollowingStrategy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("bot.backtest")

STRATEGY_CLASSES = {
    "mean_reversion": MeanReversionStrategy,
    "momentum_breakout": MomentumBreakoutStrategy,
    "trend_following": TrendFollowingStrategy,
}


# ===========================================================================
# Fills: slippage + commission
# ===========================================================================

def _apply_slippage(price: float, side: str, is_exit: bool, slippage_pct: float) -> float:
    """
    Conservative, symmetric slippage model applied against the trader:
      - Long entry / short exit  -> a BUY -> fills `slippage_pct` higher.
      - Short entry / long exit  -> a SELL -> fills `slippage_pct` lower.
    """
    is_buy = (side == "long" and not is_exit) or (side == "short" and is_exit)
    return price * (1 + slippage_pct) if is_buy else price * (1 - slippage_pct)


# ===========================================================================
# Trade record
# ===========================================================================

@dataclass
class BacktestTrade:
    symbol: str
    strategy: str
    side: str
    qty: float
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp
    exit_price: float
    pnl: float
    exit_reason: str


@dataclass
class OpenPosition:
    side: str
    qty: float
    entry_price: float
    entry_atr: float
    stop_price: float
    entry_time: pd.Timestamp


# ===========================================================================
# Data fetching
# ===========================================================================

def fetch_all_bars(broker: AlpacaBroker, months: int = None) -> Dict[str, pd.DataFrame]:
    """Pulls `months` of history for every instrument at its own strategy
    timeframe. Returns {symbol: OHLCV DataFrame}."""
    months = months or config.BACKTEST_PARAMS["lookback_months"]
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=months * 30)

    bars_by_symbol = {}
    for inst in config.INSTRUMENTS:
        logger.info("Fetching %s months of %s bars for %s...", months, inst.timeframe, inst.symbol)
        df = broker.get_historical_bars(
            symbol=inst.symbol, asset_class=inst.asset_class,
            timeframe=inst.timeframe, start=start, end=end,
        )
        logger.info("  -> %d bars (%s to %s)", len(df),
                    df.index[0] if len(df) else "n/a", df.index[-1] if len(df) else "n/a")
        bars_by_symbol[inst.symbol] = df
    return bars_by_symbol


# ===========================================================================
# Single-instrument (strategy-isolated) simulation
# ===========================================================================

def simulate_instrument(inst: "config.Instrument", bars: pd.DataFrame,
                         starting_equity: float, slippage_pct: float) -> Tuple[List[BacktestTrade], pd.Series]:
    """
    Runs one instrument's strategy alone against its own simulated account,
    bar by bar, using only information available up to and including each
    bar (no lookahead -- the strategy only ever sees bars.iloc[:i+1]).

    Returns (trades, equity_curve) where equity_curve is a pd.Series of
    mark-to-market equity indexed by bar timestamp.
    """
    strategy = STRATEGY_CLASSES[inst.strategy]()
    risk_manager = RiskManager()
    required = strategy.required_bars() + 2

    if len(bars) <= required:
        logger.warning("Not enough history for %s to run a backtest (%d bars, need > %d).",
                        inst.symbol, len(bars), required)
        return [], pd.Series(dtype=float)

    equity = starting_equity
    position: Optional[OpenPosition] = None
    trades: List[BacktestTrade] = []
    curve_index, curve_values = [], []

    for i in range(required, len(bars)):
        window = bars.iloc[: i + 1]
        ts = window.index[-1]
        last_close = float(window["close"].iloc[-1])

        currently_long = position is not None and position.side == "long"
        currently_short = position is not None and position.side == "short"
        trailing_stop = position.stop_price if position is not None else None

        if inst.strategy == "mean_reversion":
            sig = strategy.generate_signal(inst.symbol, window, currently_long, currently_short)
        else:
            sig = strategy.generate_signal(inst.symbol, window, currently_long, currently_short,
                                            trailing_stop_price=trailing_stop)

        if position is not None and inst.strategy in ("momentum_breakout", "trend_following"):
            position.stop_price = strategy.update_trailing_stop(
                position.side, position.stop_price, last_close, sig.atr,
            )

        if sig.action in (SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT) and position is not None:
            exit_price = _apply_slippage(sig.price, position.side, True, slippage_pct)
            pnl = ((exit_price - position.entry_price) if position.side == "long"
                   else (position.entry_price - exit_price)) * position.qty
            equity += pnl
            trades.append(BacktestTrade(
                symbol=inst.symbol, strategy=inst.strategy, side=position.side, qty=position.qty,
                entry_time=position.entry_time, entry_price=position.entry_price,
                exit_time=ts, exit_price=exit_price, pnl=pnl, exit_reason=sig.reason,
            ))
            position = None

        elif sig.action in (SignalAction.LONG_ENTRY, SignalAction.SHORT_ENTRY):
            side = "long" if sig.action == SignalAction.LONG_ENTRY else "short"
            sizing = risk_manager.size_position(
                symbol=inst.symbol, side=side, entry_price=sig.price, atr=sig.atr,
                account_equity=equity, asset_class=inst.asset_class,
            )
            if sizing.allowed:
                entry_price = _apply_slippage(sig.price, side, False, slippage_pct)
                if inst.strategy in ("momentum_breakout", "trend_following"):
                    strat_stop = strategy.initial_trailing_stop(side, sig.price, sig.atr)
                    stop_price = (max(sizing.stop_price, strat_stop) if side == "long"
                                  else min(sizing.stop_price, strat_stop))
                else:
                    stop_price = sizing.stop_price
                position = OpenPosition(
                    side=side, qty=sizing.qty, entry_price=entry_price, entry_atr=sig.atr,
                    stop_price=stop_price, entry_time=ts,
                )

        # mark-to-market equity point for the curve
        unrealized = 0.0
        if position is not None:
            unrealized = ((last_close - position.entry_price) if position.side == "long"
                          else (position.entry_price - last_close)) * position.qty
        curve_index.append(ts)
        curve_values.append(equity + unrealized)

    # Close any still-open position at the final bar's close so stats reflect
    # a fully realized backtest.
    if position is not None:
        last_close = float(bars["close"].iloc[-1])
        exit_price = _apply_slippage(last_close, position.side, True, slippage_pct)
        pnl = ((exit_price - position.entry_price) if position.side == "long"
               else (position.entry_price - exit_price)) * position.qty
        equity += pnl
        trades.append(BacktestTrade(
            symbol=inst.symbol, strategy=inst.strategy, side=position.side, qty=position.qty,
            entry_time=position.entry_time, entry_price=position.entry_price,
            exit_time=bars.index[-1], exit_price=exit_price, pnl=pnl,
            exit_reason="end_of_backtest_window",
        ))
        curve_values[-1] = equity

    equity_curve = pd.Series(curve_values, index=pd.DatetimeIndex(curve_index), name=inst.symbol)
    return trades, equity_curve


# ===========================================================================
# Combined portfolio simulation (correlation filter active)
# ===========================================================================

def simulate_combined_portfolio(bars_by_symbol: Dict[str, pd.DataFrame],
                                 starting_equity: float, slippage_pct: float
                                 ) -> Tuple[List[BacktestTrade], pd.Series]:
    """
    Drives all 5 instruments off one shared account and one shared position
    book, processing bar-close events across every instrument in true
    chronological order (not timeframe by timeframe), so the correlation
    filter sees real-time cross-instrument state exactly as the live bot
    would: SPY/QQQ positions open before a BTC/USD breakout bar closes can
    actually block that BTC entry, and vice versa.
    """
    inst_by_symbol = {i.symbol: i for i in config.INSTRUMENTS}
    strategies = {name: cls() for name, cls in STRATEGY_CLASSES.items()}
    risk_manager = RiskManager()

    # Build the global, time-sorted event list: (timestamp, symbol, bar_index)
    events = []
    for inst in config.INSTRUMENTS:
        bars = bars_by_symbol[inst.symbol]
        strat = strategies[inst.strategy]
        required = strat.required_bars() + 2
        if len(bars) <= required:
            logger.warning("Skipping %s in combined backtest: insufficient history.", inst.symbol)
            continue
        for i in range(required, len(bars)):
            events.append((bars.index[i], inst.symbol, i))
    events.sort(key=lambda e: e[0])

    cash_equity = starting_equity           # realized P&L accumulator
    positions: Dict[str, OpenPosition] = {}  # symbol -> OpenPosition
    last_price: Dict[str, float] = {}
    trades: List[BacktestTrade] = []
    curve_index, curve_values = [], []

    def total_equity() -> float:
        unrealized = 0.0
        for sym, pos in positions.items():
            px = last_price.get(sym, pos.entry_price)
            unrealized += ((px - pos.entry_price) if pos.side == "long"
                           else (pos.entry_price - px)) * pos.qty
        return cash_equity + unrealized

    for ts, symbol, i in events:
        inst = inst_by_symbol[symbol]
        strategy = strategies[inst.strategy]
        bars = bars_by_symbol[symbol]
        window = bars.iloc[: i + 1]
        last_close = float(window["close"].iloc[-1])
        last_price[symbol] = last_close

        position = positions.get(symbol)
        currently_long = position is not None and position.side == "long"
        currently_short = position is not None and position.side == "short"
        trailing_stop = position.stop_price if position is not None else None

        if inst.strategy == "mean_reversion":
            sig = strategy.generate_signal(symbol, window, currently_long, currently_short)
        else:
            sig = strategy.generate_signal(symbol, window, currently_long, currently_short,
                                            trailing_stop_price=trailing_stop)

        if position is not None and inst.strategy in ("momentum_breakout", "trend_following"):
            position.stop_price = strategy.update_trailing_stop(
                position.side, position.stop_price, last_close, sig.atr,
            )

        if sig.action in (SignalAction.EXIT_LONG, SignalAction.EXIT_SHORT) and position is not None:
            exit_price = _apply_slippage(sig.price, position.side, True, slippage_pct)
            pnl = ((exit_price - position.entry_price) if position.side == "long"
                   else (position.entry_price - exit_price)) * position.qty
            cash_equity += pnl
            trades.append(BacktestTrade(
                symbol=symbol, strategy=inst.strategy, side=position.side, qty=position.qty,
                entry_time=position.entry_time, entry_price=position.entry_price,
                exit_time=ts, exit_price=exit_price, pnl=pnl, exit_reason=sig.reason,
            ))
            del positions[symbol]

        elif sig.action in (SignalAction.LONG_ENTRY, SignalAction.SHORT_ENTRY):
            side = "long" if sig.action == SignalAction.LONG_ENTRY else "short"
            open_snapshot = {s: {"side": p.side} for s, p in positions.items()}
            if risk_manager.correlation_filter_blocks(symbol, side, open_snapshot):
                logger.debug("Combined backtest: correlation filter blocked %s %s entry @ %s",
                             symbol, side, ts)
            else:
                current_equity = total_equity()
                sizing = risk_manager.size_position(
                    symbol=symbol, side=side, entry_price=sig.price, atr=sig.atr,
                    account_equity=current_equity, asset_class=inst.asset_class,
                )
                if sizing.allowed:
                    entry_price = _apply_slippage(sig.price, side, False, slippage_pct)
                    if inst.strategy in ("momentum_breakout", "trend_following"):
                        strat_stop = strategy.initial_trailing_stop(side, sig.price, sig.atr)
                        stop_price = (max(sizing.stop_price, strat_stop) if side == "long"
                                      else min(sizing.stop_price, strat_stop))
                    else:
                        stop_price = sizing.stop_price
                    positions[symbol] = OpenPosition(
                        side=side, qty=sizing.qty, entry_price=entry_price, entry_atr=sig.atr,
                        stop_price=stop_price, entry_time=ts,
                    )

        curve_index.append(ts)
        curve_values.append(total_equity())

    # Flatten any still-open positions at the last price seen for that symbol.
    for symbol, pos in list(positions.items()):
        exit_price = _apply_slippage(last_price[symbol], pos.side, True, slippage_pct)
        pnl = ((exit_price - pos.entry_price) if pos.side == "long"
               else (pos.entry_price - exit_price)) * pos.qty
        cash_equity += pnl
        trades.append(BacktestTrade(
            symbol=symbol, strategy=inst_by_symbol[symbol].strategy, side=pos.side, qty=pos.qty,
            entry_time=pos.entry_time, entry_price=pos.entry_price,
            exit_time=curve_index[-1] if curve_index else pos.entry_time,
            exit_price=exit_price, pnl=pnl, exit_reason="end_of_backtest_window",
        ))
    positions.clear()
    if curve_index:
        curve_values[-1] = cash_equity

    equity_curve = pd.Series(curve_values, index=pd.DatetimeIndex(curve_index), name="combined_portfolio")
    return trades, equity_curve


# ===========================================================================
# Metrics
# ===========================================================================

@dataclass
class BacktestMetrics:
    label: str
    total_trades: int
    win_rate_pct: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_return_pct: float
    final_equity: float


def compute_metrics(label: str, trades: List[BacktestTrade], equity_curve: pd.Series,
                     starting_equity: float, trading_days_per_year: int = 252) -> BacktestMetrics:
    n = len(trades)
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl <= 0]

    win_rate_pct = (len(wins) / n * 100.0) if n > 0 else 0.0
    avg_win = float(np.mean(wins)) if wins else 0.0
    avg_loss = float(np.mean(losses)) if losses else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0 else 0.0

    if equity_curve.empty:
        final_equity = starting_equity
        max_dd_pct = 0.0
        sharpe = 0.0
    else:
        final_equity = float(equity_curve.iloc[-1])
        eq = equity_curve[~equity_curve.index.duplicated(keep="last")].sort_index()
        running_max = eq.cummax()
        drawdown = (eq - running_max) / running_max
        max_dd_pct = float(drawdown.min() * 100.0)

        # Resample to daily for a Sharpe ratio on a conventional, comparable
        # basis regardless of the instrument's native bar timeframe.
        daily = eq.resample("1D").last().ffill().dropna()
        daily_returns = daily.pct_change().dropna()
        if len(daily_returns) > 1 and daily_returns.std() > 0:
            sharpe = float(daily_returns.mean() / daily_returns.std() * np.sqrt(trading_days_per_year))
        else:
            sharpe = 0.0

    total_return_pct = (final_equity - starting_equity) / starting_equity * 100.0

    return BacktestMetrics(
        label=label, total_trades=n, win_rate_pct=win_rate_pct, avg_win=avg_win,
        avg_loss=avg_loss, profit_factor=profit_factor, max_drawdown_pct=max_dd_pct,
        sharpe_ratio=sharpe, total_return_pct=total_return_pct, final_equity=final_equity,
    )


# ===========================================================================
# Reporting: console table + chart + trades CSV
# ===========================================================================

def print_summary_table(metrics_list: List[BacktestMetrics]):
    headers = ["Instrument", "Trades", "Win%", "AvgWin", "AvgLoss", "ProfitFactor",
               "MaxDD%", "Sharpe", "TotalRet%", "FinalEquity"]
    col_widths = [14, 7, 7, 10, 10, 13, 8, 8, 10, 13]

    def fmt_row(cells):
        return "  ".join(str(c).rjust(w) for c, w in zip(cells, col_widths))

    print("\n" + "=" * 100)
    print("BACKTEST SUMMARY")
    print("=" * 100)
    print(fmt_row(headers))
    print("-" * 100)
    for m in metrics_list:
        pf_str = "inf" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
        print(fmt_row([
            m.label, m.total_trades, f"{m.win_rate_pct:.1f}", f"{m.avg_win:.2f}",
            f"{m.avg_loss:.2f}", pf_str, f"{m.max_drawdown_pct:.2f}", f"{m.sharpe_ratio:.2f}",
            f"{m.total_return_pct:.2f}", f"{m.final_equity:,.2f}",
        ]))
    print("=" * 100)

    negative_sharpe = [m for m in metrics_list if m.sharpe_ratio < 0 and m.label != "COMBINED PORTFOLIO"]
    if negative_sharpe:
        print("\n⚠️  NEGATIVE SHARPE RATIO -- parameters likely need adjustment:")
        for m in negative_sharpe:
            print(f"   - {m.label}: Sharpe = {m.sharpe_ratio:.2f}, "
                  f"total return = {m.total_return_pct:.2f}%, "
                  f"win rate = {m.win_rate_pct:.1f}%, "
                  f"profit factor = {('inf' if m.profit_factor == float('inf') else f'{m.profit_factor:.2f}')}")
        print("   Consider revisiting entry thresholds, trailing-stop multiples, or timeframe for these.")
    else:
        print("\nNo strategy/instrument showed a negative Sharpe ratio over this window.")
    print()


def plot_equity_curves(instrument_curves: Dict[str, pd.Series], combined_curve: pd.Series,
                        starting_equity: float, out_path: str):
    import matplotlib
    matplotlib.use("Agg")  # headless/non-interactive backend, safe for servers
    import matplotlib.pyplot as plt

    symbols = list(instrument_curves.keys())
    n_panels = len(symbols) + 1  # + combined
    ncols = 2
    nrows = (n_panels + ncols - 1) // ncols

    fig, axes = plt.subplots(nrows, ncols, figsize=(13, 3.4 * nrows))
    axes = np.array(axes).reshape(-1)

    for ax, symbol in zip(axes, symbols):
        curve = instrument_curves[symbol]
        if curve.empty:
            ax.set_title(f"{symbol} (no trades)")
            continue
        ax.plot(curve.index, curve.values, linewidth=1.3, color="#2563eb")
        ax.axhline(starting_equity, color="gray", linestyle="--", linewidth=0.8)
        ax.set_title(symbol)
        ax.set_ylabel("Equity ($)")
        ax.tick_params(axis="x", rotation=30)
        ax.grid(alpha=0.25)

    combined_ax = axes[len(symbols)]
    if not combined_curve.empty:
        combined_ax.plot(combined_curve.index, combined_curve.values, linewidth=1.6, color="#16a34a")
        combined_ax.axhline(starting_equity, color="gray", linestyle="--", linewidth=0.8)
    combined_ax.set_title("COMBINED PORTFOLIO (correlation filter active)")
    combined_ax.set_ylabel("Equity ($)")
    combined_ax.tick_params(axis="x", rotation=30)
    combined_ax.grid(alpha=0.25)

    for ax in axes[n_panels:]:
        ax.axis("off")

    fig.suptitle("Backtest Equity Curves (6 months, 0.05% slippage, $0 commission)", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    logger.info("Saved equity curve chart to %s", out_path)


def write_trades_csv(all_trades: List[BacktestTrade], out_path: str):
    import csv
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["symbol", "strategy", "side", "qty", "entry_time", "entry_price",
                          "exit_time", "exit_price", "pnl", "exit_reason"])
        for t in all_trades:
            writer.writerow([t.symbol, t.strategy, t.side, t.qty, t.entry_time, f"{t.entry_price:.6f}",
                              t.exit_time, f"{t.exit_price:.6f}", f"{t.pnl:.2f}", t.exit_reason])
    logger.info("Wrote %d simulated trades to %s", len(all_trades), out_path)


# ===========================================================================
# Orchestration
# ===========================================================================

def run_backtest():
    params = config.BACKTEST_PARAMS
    starting_equity = params["starting_equity"]
    slippage_pct = params["slippage_pct"]

    logger.info("Connecting to Alpaca...")
    broker = AlpacaBroker()

    logger.info("Fetching %s months of historical data for all instruments...",
                params["lookback_months"])
    bars_by_symbol = fetch_all_bars(broker, months=params["lookback_months"])

    # --- Pass 1: per-instrument, strategy-isolated backtests ---
    instrument_curves: Dict[str, pd.Series] = {}
    metrics_list: List[BacktestMetrics] = []
    all_trades: List[BacktestTrade] = []

    for inst in config.INSTRUMENTS:
        bars = bars_by_symbol[inst.symbol]
        logger.info("Backtesting %s (%s strategy, %s candles)...", inst.symbol, inst.strategy, inst.timeframe)
        trades, curve = simulate_instrument(inst, bars, starting_equity, slippage_pct)
        instrument_curves[inst.symbol] = curve
        all_trades.extend(trades)
        metrics_list.append(compute_metrics(
            label=inst.symbol, trades=trades, equity_curve=curve,
            starting_equity=starting_equity,
            trading_days_per_year=params["trading_days_per_year"],
        ))

    # --- Pass 2: combined portfolio with the correlation filter live ---
    logger.info("Running combined portfolio backtest (correlation filter active)...")
    combined_trades, combined_curve = simulate_combined_portfolio(
        bars_by_symbol, starting_equity, slippage_pct,
    )
    all_trades.extend(combined_trades)
    combined_metrics = compute_metrics(
        label="COMBINED PORTFOLIO", trades=combined_trades, equity_curve=combined_curve,
        starting_equity=starting_equity, trading_days_per_year=params["trading_days_per_year"],
    )
    metrics_list.append(combined_metrics)

    # --- Reporting ---
    print_summary_table(metrics_list)
    plot_equity_curves(instrument_curves, combined_curve, starting_equity, params["results_png_path"])
    write_trades_csv(all_trades, params["trades_csv_path"])

    return metrics_list


if __name__ == "__main__":
    run_backtest()
