"""
tests/test_broker.py

Covers bot/broker.py's AlpacaBroker.get_bars(), specifically a real bug
found on 2026-10-03: BTC/USD (5Min, 24/7 crypto) never saw a new closed
bar after the 5-minute-timeframe switch, so bot/main.py's "no new bar
since last seen" gate fired silently forever.

Root cause: get_bars() passed the caller's `limit` straight through as the
Alpaca API call's own `limit` -- but that parameter caps the *total* bars
Alpaca paginates back for the [start, end) window, returned ascending from
`start`, not "the most recent N bars." _lookback_window() pads the window
3x (+5 days) so equities' weekend/holiday gaps don't starve it of `limit`
real bars. For 24/7 crypto, that 3x padding means the window contains far
more real bars than `limit`, so the API's own pagination cap silently
truncated the response to the OLDEST `limit` bars in the window --
permanently stale data, no matter how many times you poll, because the
window shifts forward each tick by exactly as much as the truncation point
does.

The fix: request the full window (uncapped by the caller's `limit`, via
the same _estimate_bar_count() approach get_historical_bars() already
used), then slice to the most recent `limit` bars client-side.

These tests mock bot.broker.AlpacaBroker.api directly (bypassing __init__,
which calls the real Alpaca API) so nothing here touches the network.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pandas as pd
import pytest

import config
from bot.broker import AlpacaBroker


def make_broker_with_fake_api(fake_df: pd.DataFrame) -> AlpacaBroker:
    """An AlpacaBroker whose .api.get_crypto_bars()/.get_bars() return a
    pre-built DataFrame, without ever touching __init__'s network call."""
    broker = AlpacaBroker.__new__(AlpacaBroker)
    fake_bars_response = MagicMock()
    fake_bars_response.df = fake_df
    fake_api = MagicMock()
    fake_api.get_crypto_bars.return_value = fake_bars_response
    fake_api.get_bars.return_value = fake_bars_response
    broker.api = fake_api
    return broker


def make_ascending_bars(n: int, start: datetime, step: timedelta) -> pd.DataFrame:
    index = [start + i * step for i in range(n)]
    return pd.DataFrame(
        {
            "open": [1.0] * n,
            "high": [1.0] * n,
            "low": [1.0] * n,
            "close": [1.0] * n,
            "volume": [1.0] * n,
        },
        index=pd.DatetimeIndex(index, name="timestamp"),
    )


class TestGetBarsReturnsRecentData:
    def test_crypto_5min_returns_bars_ending_near_now_not_stale(self):
        """Reproduces the real incident: a 24/7 crypto window padded 3x
        contains far more than `limit` real bars. Before the fix, Alpaca's
        own pagination cap (simulated here by the fake API only returning
        the oldest `api_limit`-sized slice it was asked for) would hand
        back data stuck ~8 days behind `now`. After the fix, get_bars()
        must request the FULL window and then take the most recent `limit`
        bars itself, so the returned data always ends at (approximately)
        now.
        """
        limit = 500
        timeframe = "5Min"
        now = datetime.now(timezone.utc)

        # Simulate what the real Alpaca API would return for the full,
        # padded [start, end) window: every 5Min bar across the whole span
        # exists, because BTC/USD trades 24/7. This is exactly the
        # over-abundance of real bars that triggered the bug.
        window = AlpacaBroker._lookback_window(timeframe, limit)
        start = now - window
        full_bar_count = int(window / timedelta(minutes=5))
        full_df = make_ascending_bars(full_bar_count, start, timedelta(minutes=5))

        broker = make_broker_with_fake_api(full_df)

        # The fake API below stands in for Alpaca: it always returns
        # whatever full_df it was given, regardless of what `limit` keyword
        # it's called with -- exactly like the real API does within a
        # window that contains more bars than any single `limit` captures
        # in one page (alpaca_trade_api already paginates internally up to
        # `limit`, so the net effect is identical: the caller gets back up
        # to `limit` bars, ascending from `start`).
        def fake_get_crypto_bars(symbol, tf, start_iso, end_iso, limit):
            resp = MagicMock()
            resp.df = full_df.iloc[:limit]
            return resp

        broker.api.get_crypto_bars.side_effect = fake_get_crypto_bars

        result = broker.get_bars(
            symbol="BTC/USD", asset_class=config.CRYPTO, timeframe=timeframe, limit=limit,
        )

        assert not result.empty
        # The whole point of the fix: the latest bar in the result must be
        # close to "now", not stranded days behind it.
        latest_bar_age = now - result.index[-1].to_pydatetime()
        assert latest_bar_age < timedelta(minutes=30), (
            f"latest bar is {latest_bar_age} old; get_bars() returned stale "
            f"data instead of the most recent {limit} bars"
        )
        # And it should respect the caller's requested bar count.
        assert len(result) == limit

    def test_requests_the_full_window_from_the_api_not_just_limit(self):
        """The API call itself must ask for (at least) as many bars as the
        padded window could actually contain -- not the caller's raw
        `limit` -- or the truncation bug reappears."""
        limit = 500
        timeframe = "5Min"

        full_df = make_ascending_bars(10, datetime.now(timezone.utc), timedelta(minutes=5))
        broker = make_broker_with_fake_api(full_df)

        broker.get_bars(
            symbol="BTC/USD", asset_class=config.CRYPTO, timeframe=timeframe, limit=limit,
        )

        _, kwargs = broker.api.get_crypto_bars.call_args
        called_limit = kwargs.get("limit") if "limit" in kwargs else broker.api.get_crypto_bars.call_args[0][-1]
        assert called_limit > limit, (
            "get_bars() must request more than the caller's `limit` from the "
            "API for a 24/7 symbol's padded window, or the old truncation "
            "bug is still present"
        )

    def test_equity_short_history_still_returns_everything_available(self):
        """For equities (fewer real bars in the window thanks to market
        hours), the fix must not regress the normal case: asking for more
        than exists should just return everything available, trimmed to
        `limit` if there happens to be more."""
        limit = 500
        timeframe = "5Min"
        # Fewer bars than `limit` -- the pre-fix behavior for equities.
        small_df = make_ascending_bars(50, datetime.now(timezone.utc) - timedelta(hours=4),
                                        timedelta(minutes=5))
        broker = make_broker_with_fake_api(small_df)

        result = broker.get_bars(
            symbol="SPY", asset_class=config.EQUITY, timeframe=timeframe, limit=limit,
        )
        assert len(result) == 50

    def test_empty_response_handled(self):
        empty_df = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        broker = make_broker_with_fake_api(empty_df)
        result = broker.get_bars(
            symbol="BTC/USD", asset_class=config.CRYPTO, timeframe="5Min", limit=500,
        )
        assert result.empty
