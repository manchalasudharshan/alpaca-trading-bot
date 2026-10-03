"""
bot/strategies/base.py

Shared types used across all three strategy modules, so main.py can treat
every strategy's output uniformly.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class SignalAction(Enum):
    LONG_ENTRY = "long_entry"
    SHORT_ENTRY = "short_entry"
    EXIT_LONG = "exit_long"
    EXIT_SHORT = "exit_short"
    HOLD = "hold"


@dataclass
class Signal:
    """A single strategy's verdict for one symbol on its latest closed bar."""
    symbol: str
    action: SignalAction
    price: float              # reference price (close of the signal bar)
    atr: float                # current ATR for this symbol (for sizing/stops)
    reason: str                # human-readable explanation, goes into logs
    bar_timestamp: object = None  # pandas.Timestamp of the bar that triggered this
