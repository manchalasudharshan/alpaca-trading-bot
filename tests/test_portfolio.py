"""
tests/test_portfolio.py

Covers the 2026-10-06 incident: daily_pnl.csv silently froze on the last
date a trade actually closed, because the date-rollover logic only lived
inside _accumulate_daily_pnl() (reached only on a closed trade), while
each GitHub Actions tick is a fresh process that reloads _pnl_date from
bot_state.json. On a quiet day with zero closes, _pnl_date never advanced,
so end_of_day_flush() kept re-upserting the stale date's row forever
instead of writing a fresh (0.00, 0 trades) row for the new day.

Fix: _maybe_roll_date() is now called from BOTH _accumulate_daily_pnl()
and end_of_day_flush(), so the date advances every tick regardless of
whether a trade closed.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from bot.portfolio import Portfolio


@pytest.fixture
def portfolio(tmp_path):
    trades_csv = tmp_path / "trades.csv"
    daily_pnl_csv = tmp_path / "daily_pnl.csv"
    return Portfolio(trades_csv_path=str(trades_csv), daily_pnl_csv_path=str(daily_pnl_csv))


def _read_rows(csv_path):
    with open(csv_path, newline="") as f:
        lines = f.read().splitlines()
    return [line.split(",") for line in lines[1:] if line.strip()]


class TestEndOfDayFlushRollsDateForward:
    def test_quiet_day_after_a_trade_gets_its_own_zero_row(self, portfolio):
        """
        Reproduces the exact incident: a trade closes on day 1 (so
        _pnl_date gets set and persisted to day 1), then a quiet day 2
        passes with zero closed trades. end_of_day_flush() alone -- with
        no trade closing -- must still create day 2's (0.00, 0) row
        instead of re-upserting day 1's row forever.
        """
        day1 = datetime(2026, 10, 3, tzinfo=timezone.utc)
        day2 = day1 + timedelta(days=1)
        day3 = day1 + timedelta(days=2)

        with patch("bot.portfolio.datetime") as mock_dt:
            mock_dt.now.return_value = day1
            # simulate load_state_dict() restoring _pnl_date from a prior save
            portfolio._pnl_date = day1.date()
            portfolio._accumulate_daily_pnl(10.0)  # one closed trade on day1

        # Day 2: zero trades close, only end_of_day_flush() runs (as
        # bot/main.py's run_once() does on every tick).
        with patch("bot.portfolio.datetime") as mock_dt:
            mock_dt.now.return_value = day2
            portfolio.end_of_day_flush()

        # Day 3: also zero trades, same as day 2.
        with patch("bot.portfolio.datetime") as mock_dt:
            mock_dt.now.return_value = day3
            portfolio.end_of_day_flush()

        rows = _read_rows(portfolio.daily_pnl_csv_path)
        dates = [r[0] for r in rows]

        assert "2026-10-03" in dates
        assert "2026-10-04" in dates, (
            "day 2 (zero trades) must get its own row -- this is the bug: "
            "it used to be silently skipped, leaving day 1's row stuck."
        )
        assert "2026-10-05" in dates
        # day 3's row should show zero realized pnl / zero trades closed
        day3_row = rows[dates.index("2026-10-05")]
        assert day3_row[1] == "0.00"
        assert day3_row[2] == "0"

    def test_end_of_day_flush_with_no_prior_trade_ever(self, portfolio):
        """A brand new portfolio with no trades at all still gets a row
        for today when end_of_day_flush() runs."""
        today = datetime(2026, 10, 6, tzinfo=timezone.utc)
        with patch("bot.portfolio.datetime") as mock_dt:
            mock_dt.now.return_value = today
            portfolio._pnl_date = today.date()
            portfolio.end_of_day_flush()

        rows = _read_rows(portfolio.daily_pnl_csv_path)
        assert len(rows) == 1
        assert rows[0][0] == "2026-10-06"
        assert rows[0][1] == "0.00"
        assert rows[0][2] == "0"

    def test_multiple_ticks_same_day_do_not_duplicate_rows(self, portfolio):
        """Several end_of_day_flush() calls within the same UTC day
        (e.g. every 5-minute tick) must upsert, not append duplicate rows."""
        today = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
        later_today = today + timedelta(minutes=5)

        with patch("bot.portfolio.datetime") as mock_dt:
            mock_dt.now.return_value = today
            portfolio._pnl_date = today.date()
            portfolio.end_of_day_flush()

        with patch("bot.portfolio.datetime") as mock_dt:
            mock_dt.now.return_value = later_today
            portfolio.end_of_day_flush()

        rows = _read_rows(portfolio.daily_pnl_csv_path)
        assert len(rows) == 1
        assert rows[0][0] == "2026-10-06"
