"""
bot/reporting/evening_report.py

Assembles the evening performance report facts, turns them into prose, and
sends to Telegram. Run by .github/workflows/evening_report.yml (9:00 PM IST
= 15:30 UTC).

Run with:  python -m bot.reporting.evening_report
"""

import logging
import sys

import config
from bot.broker import AlpacaBroker
from bot.reporting import data
from bot.reporting.narrate import narrate_evening
from bot.reporting.telegram import send_telegram_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                     stream=sys.stdout)
logger = logging.getLogger("bot.reporting.evening_report")

# Per-instrument total return %% from the last real 6-month backtest
# (results/backtest_console_output.txt as of the parameters finalized after
# the Sharpe<0-or-MaxDD>15% tuning pass), used only as a rough daily-pace
# baseline for the "on track vs backtest" comparison below. Re-run the
# backtest and update these if the strategy parameters change again.
BACKTEST_TOTAL_RETURN_PCT = {
    "SPY": -26.96, "QQQ": -9.98, "BTC/USD": 23.56, "GLD": 1.41, "USO": 7.23,
}
BACKTEST_TRADING_DAYS = {  # ~6 months: equities trade ~126 days, crypto is 24/7 (~180 days)
    "SPY": 126, "QQQ": 126, "BTC/USD": 180, "GLD": 126, "USO": 126,
}


def backtest_comparison_note(today_pnl_by_instrument: dict) -> str:
    notes = []
    for inst, pnl in today_pnl_by_instrument.items():
        if inst not in BACKTEST_TOTAL_RETURN_PCT:
            continue
        expected_daily_pct = BACKTEST_TOTAL_RETURN_PCT[inst] / BACKTEST_TRADING_DAYS[inst]
        direction = "profitable" if expected_daily_pct >= 0 else "losing"
        notes.append(f"{inst} backtested as net {direction} over 6mo "
                     f"({BACKTEST_TOTAL_RETURN_PCT[inst]:+.1f}% total)")
    if not notes:
        return "No instruments traded today to compare against backtest baseline."
    return "Backtest baseline (6-month, same parameters): " + "; ".join(notes) + \
        ". One day of live P&L is too small a sample to call 'on track' or " \
        "'diverging' with any confidence -- this is directional context only."


def build_facts() -> dict:
    broker = AlpacaBroker()
    equity = broker.get_equity()
    trades = data.load_trades()

    today_start, today_end = data.utc_day_bounds(days_ago=0)
    today_trades = data.trades_in_window(trades, today_start, today_end)

    today_pnl_by_instrument = data.pnl_by_instrument(today_trades)
    today_total_pnl = sum(today_pnl_by_instrument.values())

    best_trade = max(today_trades, key=lambda t: t["profit_loss"], default=None)
    worst_trade = min(today_trades, key=lambda t: t["profit_loss"], default=None)

    stop_trades_today = [t for t in today_trades if "hard stop hit" in (t.get("exit_reason") or "")]
    stop_violations_today = [v for v in data.stop_loss_audit(today_trades)]

    facts = {
        "trades_today_count": len(today_trades),
        "instruments_traded_today": sorted(set(t["instrument"] for t in today_trades)),
        "today_pnl_dollars": today_total_pnl,
        "today_pnl_by_instrument": today_pnl_by_instrument,
        "today_pnl_pct_of_equity": (today_total_pnl / equity * 100.0) if equity else 0.0,
        "best_trade": best_trade,
        "worst_trade": worst_trade,
        "current_equity": equity,
        "backtest_comparison_note": backtest_comparison_note(today_pnl_by_instrument),
        "stop_trades_today": stop_trades_today,
        "stop_loss_violations_today": stop_violations_today,
    }
    return facts


def main():
    facts = build_facts()
    text = narrate_evening(facts)
    logger.info("Evening report:\n%s", text)
    sent = send_telegram_message(text)
    if not sent:
        logger.error("Telegram send failed -- see TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID secrets.")
        sys.exit(1)


if __name__ == "__main__":
    main()
