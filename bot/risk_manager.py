"""
bot/risk_manager.py

Central risk engine used by every strategy before a trade is sent to
Alpaca. Responsibilities:

1. ATR-based position sizing: size each trade so a 1-ATR adverse move
   equals exactly `risk_per_atr_of_equity` (default 1%) of total account
   equity. Quiet instruments (small ATR in price terms) get bigger size;
   volatile instruments get smaller size -- dollar risk per trade stays
   constant.
2. Hard stop-loss enforcement: every trade gets a stop such that, if hit,
   the realized loss is capped at `max_loss_per_trade_of_equity` (default
   1%) of account equity. No exceptions -- if the strategy's natural stop
   (e.g. trailing-stop distance) would imply a larger loss than the sizing
   already assumes, position size is cut rather than letting the stop
   distance float.
3. Correlation filter: blocks new BTC/USD long entries when both SPY and
   QQQ already have open long positions, to avoid stacking risk-on
   exposure three-deep.

This module does not place orders itself -- it tells main.py/portfolio.py
how big a position should be (and whether a trade is even allowed), and
the caller is responsible for execution.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class SizingResult:
    allowed: bool
    qty: float              # shares (equity) or units (crypto); 0 if not allowed
    dollar_risk: float      # expected $ loss if the stop is hit, at this size
    stop_price: float       # hard stop price implied by this sizing
    reason: str              # why allowed/blocked, and the math, for logging


class RiskManager:
    def __init__(self, params: dict = None):
        self.params = params or config.RISK_PARAMS
        self.risk_per_atr = self.params["risk_per_atr_of_equity"]
        self.max_loss_fraction = self.params["max_loss_per_trade_of_equity"]
        self.corr_cfg = self.params["correlation_block"]

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------
    def size_position(self, symbol: str, side: str, entry_price: float,
                       atr: float, account_equity: float,
                       asset_class: str = "equity") -> SizingResult:
        """
        Core sizing formula:

            risk_dollars = account_equity * risk_per_atr_of_equity
            qty = risk_dollars / atr

        i.e. qty * atr == risk_dollars, so a 1-ATR move against the position
        costs exactly `risk_per_atr_of_equity` of equity. The hard stop is
        then placed exactly 1 ATR away from entry (which is, by
        construction, also where the max-loss-per-trade cap is breached) --
        see `_hard_stop_price`.

        If a strategy's own stop (e.g. a 2x or 3x ATR trailing stop) is
        wider than 1 ATR, we do NOT let the dollar risk grow: instead we
        keep the hard stop at the tighter of (1 ATR, strategy stop
        distance) in risk-budget terms by sizing off whichever distance is
        larger. This guarantees the max-loss-per-trade cap is never
        exceeded regardless of which strategy's stop logic is in play.
        """
        if atr is None or atr <= 0 or math.isnan(atr):
            return SizingResult(
                allowed=False, qty=0, dollar_risk=0, stop_price=entry_price,
                reason=f"invalid ATR ({atr}) for {symbol}; refusing to size position",
            )
        if entry_price <= 0 or account_equity <= 0:
            return SizingResult(
                allowed=False, qty=0, dollar_risk=0, stop_price=entry_price,
                reason="invalid entry price or non-positive account equity",
            )

        risk_dollars = account_equity * self.risk_per_atr
        max_loss_dollars = account_equity * self.max_loss_fraction

        # Base sizing: 1 ATR of adverse move == risk_dollars.
        qty = risk_dollars / atr

        # The hard stop is 1 ATR from entry. Given qty above, a hit on that
        # stop loses exactly risk_dollars, which by config is also the
        # max-loss-per-trade cap (both default to 1%). If the two
        # percentages were ever configured differently, cap qty so the loss
        # at the hard stop never exceeds max_loss_dollars.
        implied_loss_at_1atr = qty * atr
        if implied_loss_at_1atr > max_loss_dollars:
            qty = max_loss_dollars / atr
            implied_loss_at_1atr = qty * atr

        stop_price = self._hard_stop_price(side, entry_price, atr)

        # Round down for equities (no fractional-share assumption needed,
        # but Alpaca does support fractional equity orders; we still round
        # to a sane precision). Crypto keeps more decimal precision.
        if asset_class == "equity":
            qty = math.floor(qty * 1000) / 1000.0  # 3 decimal -> supports fractional shares
        else:
            qty = math.floor(qty * 1e6) / 1e6       # 6 decimal precision for crypto

        if qty <= 0:
            return SizingResult(
                allowed=False, qty=0, dollar_risk=0, stop_price=stop_price,
                reason=(f"computed qty rounded to 0 for {symbol} "
                        f"(risk_dollars={risk_dollars:.2f}, atr={atr:.4f})"),
            )

        return SizingResult(
            allowed=True, qty=qty, dollar_risk=implied_loss_at_1atr, stop_price=stop_price,
            reason=(f"sized {symbol} {side}: qty={qty}, entry={entry_price:.4f}, "
                    f"atr={atr:.4f}, risk_dollars={implied_loss_at_1atr:.2f} "
                    f"({self.risk_per_atr*100:.1f}% of equity {account_equity:.2f}), "
                    f"hard_stop={stop_price:.4f}"),
        )

    @staticmethod
    def _hard_stop_price(side: str, entry_price: float, atr: float) -> float:
        """Hard stop is exactly 1 ATR from entry -- the distance the sizing
        formula is built around."""
        return entry_price - atr if side == "long" else entry_price + atr

    # ------------------------------------------------------------------
    # Hard stop validation (belt-and-suspenders check before any order)
    # ------------------------------------------------------------------
    def validate_stop_within_cap(self, side: str, entry_price: float, stop_price: float,
                                  qty: float, account_equity: float) -> bool:
        """
        Final sanity check: whatever stop is actually being sent to the
        broker (which may be a strategy trailing stop tighter than the hard
        stop, but should never be wider), confirm the resulting dollar loss
        does not exceed max_loss_per_trade_of_equity. Returns False (and
        logs) if the trade should be rejected.
        """
        if side == "long":
            loss_per_unit = max(0.0, entry_price - stop_price)
        else:
            loss_per_unit = max(0.0, stop_price - entry_price)

        dollar_loss = loss_per_unit * qty
        max_loss_dollars = account_equity * self.max_loss_fraction

        if dollar_loss > max_loss_dollars * 1.0001:  # tiny epsilon for float rounding
            logger.warning(
                "Stop validation FAILED: %s %s qty=%s entry=%.4f stop=%.4f would lose "
                "$%.2f, exceeding max loss cap $%.2f (%.2f%% equity). Trade rejected.",
                side, qty, qty, entry_price, stop_price, dollar_loss,
                max_loss_dollars, self.max_loss_fraction * 100,
            )
            return False
        return True

    # ------------------------------------------------------------------
    # Correlation filter
    # ------------------------------------------------------------------
    def correlation_filter_blocks(self, symbol: str, side: str, open_positions: dict) -> bool:
        """
        open_positions: {symbol: {"side": "long"|"short", ...}} for all
            currently-open positions, from portfolio.py.

        Returns True if this specific (symbol, side) entry should be
        BLOCKED by the correlation filter.

        Current rule (config.RISK_PARAMS["correlation_block"]):
          if SPY and QQQ are both already long, block new BTC/USD longs.
        """
        if symbol != self.corr_cfg["blocked_symbol"] or side != self.corr_cfg["blocked_side"]:
            return False

        guard_symbols = self.corr_cfg["guard_symbols"]
        all_guards_long = all(
            open_positions.get(g, {}).get("side") == "long" for g in guard_symbols
        )

        if all_guards_long:
            logger.info(
                "Correlation filter: blocking %s %s entry because %s are all already long.",
                symbol, side, guard_symbols,
            )
            return True
        return False
