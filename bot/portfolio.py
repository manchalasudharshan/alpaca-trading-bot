"""
bot/portfolio.py

Tracks the bot's view of open positions (per symbol: side, qty, entry
price, entry ATR, trailing/hard stop, entry timestamp) and handles all
CSV logging:

- trades.csv: one row per CLOSED trade (timestamp, instrument, direction,
  entry price, exit price, profit/loss, position size).
- daily_pnl.csv: one row per calendar day with that day's realized P&L.

This module is the single source of truth the strategies query via
`currently_long` / `currently_short` flags, and the only place that writes
to the CSV logs, so there's one consistent record no matter which strategy
or which part of main.py triggers a fill.
"""

import csv
import logging
import os
import threading
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Dict, Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class Position:
    symbol: str
    side: str                 # "long" or "short"
    qty: float
    entry_price: float
    entry_atr: float
    stop_price: float         # current active stop (hard stop initially, then ratcheted trailing stop)
    strategy: str
    entry_time: datetime


class Portfolio:
    """
    In-memory position book + CSV trade/PnL logger.

    Thread-safe-ish: a single lock guards mutation since main.py may poll
    multiple timeframes from a simple sequential loop, but we don't assume
    that and protect against concurrent access regardless.
    """

    def __init__(self, trades_csv_path: str = None, daily_pnl_csv_path: str = None):
        self.positions: Dict[str, Position] = {}
        self._lock = threading.Lock()
        self.trades_csv_path = trades_csv_path or config.TRADES_CSV_PATH
        self.daily_pnl_csv_path = daily_pnl_csv_path or config.DAILY_PNL_CSV_PATH
        self._ensure_csv_headers()
        # running tally of realized P&L for "today" (reset on date rollover)
        self._pnl_date = datetime.now(timezone.utc).date()
        self._daily_realized_pnl = 0.0

    # ------------------------------------------------------------------
    # CSV setup
    # ------------------------------------------------------------------
    def _ensure_csv_headers(self):
        if not os.path.exists(self.trades_csv_path):
            with open(self.trades_csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "instrument", "direction", "entry_price",
                    "exit_price", "profit_loss", "position_size", "strategy",
                    "exit_reason",
                ])
        if not os.path.exists(self.daily_pnl_csv_path):
            with open(self.daily_pnl_csv_path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["date", "realized_pnl", "trades_closed"])

    # ------------------------------------------------------------------
    # Position state queries (used by strategies to know current exposure)
    # ------------------------------------------------------------------
    def is_long(self, symbol: str) -> bool:
        pos = self.positions.get(symbol)
        return pos is not None and pos.side == "long"

    def is_short(self, symbol: str) -> bool:
        pos = self.positions.get(symbol)
        return pos is not None and pos.side == "short"

    def get_position(self, symbol: str) -> Optional[Position]:
        return self.positions.get(symbol)

    def open_positions_snapshot(self) -> dict:
        """Used by RiskManager.correlation_filter_blocks()."""
        with self._lock:
            return {sym: {"side": p.side, "qty": p.qty} for sym, p in self.positions.items()}

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------
    def open_position(self, symbol: str, side: str, qty: float, entry_price: float,
                       entry_atr: float, stop_price: float, strategy: str):
        with self._lock:
            self.positions[symbol] = Position(
                symbol=symbol, side=side, qty=qty, entry_price=entry_price,
                entry_atr=entry_atr, stop_price=stop_price, strategy=strategy,
                entry_time=datetime.now(timezone.utc),
            )
        logger.info("Opened %s %s qty=%s @ %.4f (stop=%.4f, strategy=%s)",
                    side, symbol, qty, entry_price, stop_price, strategy)

    def update_stop(self, symbol: str, new_stop_price: float):
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is not None:
                pos.stop_price = new_stop_price

    def close_position(self, symbol: str, exit_price: float, exit_reason: str) -> Optional[float]:
        """
        Closes the position (if any) and logs the trade to trades.csv and
        rolls it into daily_pnl.csv. Returns the realized P&L, or None if
        there was nothing open for this symbol.
        """
        with self._lock:
            pos = self.positions.pop(symbol, None)
            if pos is None:
                logger.warning("close_position called for %s but no open position found.", symbol)
                return None

            if pos.side == "long":
                pnl = (exit_price - pos.entry_price) * pos.qty
            else:
                pnl = (pos.entry_price - exit_price) * pos.qty

            self._log_trade(pos, exit_price, pnl, exit_reason)
            self._accumulate_daily_pnl(pnl)

        logger.info("Closed %s %s qty=%s entry=%.4f exit=%.4f pnl=%.2f reason=%s",
                     pos.side, symbol, pos.qty, pos.entry_price, exit_price, pnl, exit_reason)
        return pnl

    # ------------------------------------------------------------------
    # CSV writers
    # ------------------------------------------------------------------
    def _log_trade(self, pos: Position, exit_price: float, pnl: float, exit_reason: str):
        with open(self.trades_csv_path, "a", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([
                datetime.now(timezone.utc).isoformat(),
                pos.symbol,
                pos.side,
                f"{pos.entry_price:.6f}",
                f"{exit_price:.6f}",
                f"{pnl:.2f}",
                pos.qty,
                pos.strategy,
                exit_reason,
            ])

    def _accumulate_daily_pnl(self, pnl: float):
        today = datetime.now(timezone.utc).date()
        if today != self._pnl_date:
            # date rolled over since last trade; flush and reset
            self._flush_daily_pnl_row()
            self._pnl_date = today
            self._daily_realized_pnl = 0.0
            self._trades_closed_today = 0

        self._daily_realized_pnl += pnl
        self._trades_closed_today = getattr(self, "_trades_closed_today", 0) + 1
        self._flush_daily_pnl_row(upsert=True)

    def _flush_daily_pnl_row(self, upsert: bool = False):
        """
        Rewrites daily_pnl.csv with today's row upserted (keeps all prior
        days intact; replaces today's row if it already exists so the file
        always reflects running intraday P&L, not just end-of-day).
        """
        rows = []
        date_str = self._pnl_date.isoformat()
        found = False
        if os.path.exists(self.daily_pnl_csv_path):
            with open(self.daily_pnl_csv_path, "r", newline="") as f:
                reader = csv.reader(f)
                header = next(reader, ["date", "realized_pnl", "trades_closed"])
                for row in reader:
                    if row and row[0] == date_str:
                        if upsert:
                            rows.append([date_str, f"{self._daily_realized_pnl:.2f}",
                                         getattr(self, "_trades_closed_today", 0)])
                            found = True
                        else:
                            rows.append(row)
                    else:
                        rows.append(row)
        else:
            header = ["date", "realized_pnl", "trades_closed"]

        if upsert and not found:
            rows.append([date_str, f"{self._daily_realized_pnl:.2f}",
                         getattr(self, "_trades_closed_today", 0)])

        with open(self.daily_pnl_csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            writer.writerows(rows)

    def end_of_day_flush(self):
        """Call once near market close / once a day from main.py's scheduler
        to guarantee a row exists even on days with zero trades."""
        self._flush_daily_pnl_row(upsert=True)
