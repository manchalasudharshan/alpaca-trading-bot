"""
bot/indicators.py

Plain pandas/numpy implementations of the technical indicators the
strategies need. Kept dependency-free (no ta-lib) so the project installs
anywhere pandas does.

All functions take a DataFrame with at least ['high', 'low', 'close',
'volume'] columns, indexed by bar timestamp (oldest first), and return a
pandas Series aligned to that index unless noted otherwise.
"""

import numpy as np
import pandas as pd


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average."""
    return series.rolling(window=period, min_periods=period).mean()


def rolling_std(series: pd.Series, period: int) -> pd.Series:
    """Rolling standard deviation (population std, ddof=0 is fine for this use)."""
    return series.rolling(window=period, min_periods=period).std(ddof=0)


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average."""
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    """
    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    """
    prev_close = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - prev_close).abs()
    tr3 = (df["low"] - prev_close).abs()
    return pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """
    Average True Range, Wilder's smoothing (equivalent to an EMA with
    alpha = 1/period), which is the conventional ATR definition.
    """
    tr = true_range(df)
    return tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).max()


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).min()


def rolling_avg_volume(volume: pd.Series, period: int) -> pd.Series:
    return volume.rolling(window=period, min_periods=period).mean()


def ema_cross(fast: pd.Series, slow: pd.Series) -> pd.Series:
    """
    Returns a Series of {-1, 0, 1} where:
      1  = fast crossed above slow on this bar (bullish cross)
     -1  = fast crossed below slow on this bar (bearish cross)
      0  = no cross on this bar
    """
    diff = fast - slow
    prev_diff = diff.shift(1)
    cross_up = (prev_diff <= 0) & (diff > 0)
    cross_down = (prev_diff >= 0) & (diff < 0)
    result = pd.Series(0, index=fast.index)
    result[cross_up] = 1
    result[cross_down] = -1
    return result
