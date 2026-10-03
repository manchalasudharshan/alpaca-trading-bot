"""
bot/reporting/data.py

Shared data-gathering for the morning/evening Telegram reports. Everything
that becomes a *number* in a report is computed here in plain Python from
trades.csv / daily_pnl.csv / bot_state.json / the live Alpaca account --
never left to the LLM to infer or recall, so the report can't hallucinate a
P&L figure or a win rate. bot/reporting/narrate.py only turns these
already-correct numbers into prose.
"""

import csv
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
from bot.broker import AlpacaBroker
from bot.indicators import sma

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSV / state loading
# ---------------------------------------------------------------------------
def load_trades(path: str = None) -> list:
    path = path or config.TRADES_CSV_PATH
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["profit_loss"] = float(r["profit_loss"]) if r.get("profit_loss") else 0.0
        try:
            r["_ts"] = datetime.fromisoformat(r["timestamp"])
        except Exception:
            r["_ts"] = None
    return rows


def load_daily_pnl(path: str = None) -> list:
    path = path or config.DAILY_PNL_CSV_PATH
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["realized_pnl"] = float(r["realized_pnl"]) if r.get("realized_pnl") else 0.0
        r["trades_closed"] = int(r["trades_closed"]) if r.get("trades_closed") else 0
    return rows


def load_bot_state(path: str = None) -> dict:
    path = path or config.STATE_FILE_PATH
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Trade-level metrics
# ---------------------------------------------------------------------------
def trades_in_window(trades: list, start: datetime, end: datetime) -> list:
    return [t for t in trades if t["_ts"] is not None and start <= t["_ts"] < end]


def win_rate_pct(trades: list) -> Optional[float]:
    if not trades:
        return None
    wins = sum(1 for t in trades if t["profit_loss"] > 0)
    return round(100.0 * wins / len(trades), 1)


def pnl_by_instrument(trades: list) -> dict:
    out = {}
    for t in trades:
        out[t["instrument"]] = out.get(t["instrument"], 0.0) + t["profit_loss"]
    return {k: round(v, 2) for k, v in out.items()}


def stop_loss_audit(trades: list) -> list:
    """
    For every losing trade with a recorded loss_pct_of_equity_at_entry,
    flag any that breached the 1%-of-equity hard cap (with a small
    tolerance for the slippage model) -- the concrete answer to "are stop
    losses triggering at exactly 1% of equity, no exceptions."
    """
    cap_pct = -config.RISK_PARAMS["max_loss_per_trade_of_equity"] * 100.0
    tolerance_pct = 0.25  # slippage model can push a hair past the exact 1%
    violations = []
    for t in trades:
        raw = t.get("loss_pct_of_equity_at_entry")
        if not raw:
            continue
        try:
            pct = float(raw)
        except ValueError:
            continue
        if pct < cap_pct - tolerance_pct:
            violations.append({
                "instrument": t["instrument"], "timestamp": t["timestamp"],
                "loss_pct": pct, "exit_reason": t.get("exit_reason", ""),
            })
    return violations


# ---------------------------------------------------------------------------
# Live account state (via Alpaca -- the broker is the source of truth for
# what's actually open, not just this process's local book)
# ---------------------------------------------------------------------------
def gather_account_snapshot(broker: AlpacaBroker) -> dict:
    equity = broker.get_equity()
    positions = broker.get_positions()
    market_open = broker.is_equity_market_open()
    return {"equity": equity, "positions": positions, "equity_market_open": market_open}


def correlation_filter_active(positions: list) -> bool:
    guard_symbols = config.RISK_PARAMS["correlation_block"]["guard_symbols"]
    by_symbol = {p["symbol"]: p for p in positions}
    return all(by_symbol.get(g, {}).get("side") == "long" for g in guard_symbols)


def drawdown_from_peak_pct(current_equity: float, peak_equity: Optional[float]) -> Optional[float]:
    if not peak_equity or peak_equity <= 0:
        return None
    return round(100.0 * (current_equity - peak_equity) / peak_equity, 2)


def position_stop_proximity(positions: list, state: dict) -> list:
    """
    For each open position, how close is the current price to its stop,
    expressed as % of the 1-ATR stop distance already covered. >80% means
    "approaching the stop." Stop prices come from bot_state.json (Alpaca's
    own position object doesn't carry our software-managed stop).
    """
    state_positions = state.get("portfolio", {}).get("positions", {})
    out = []
    for p in positions:
        sym = p["symbol"]
        st = state_positions.get(sym)
        if not st:
            continue
        entry = st["entry_price"]
        stop = st["stop_price"]
        current = p["current_price"]
        total_distance = abs(entry - stop)
        if total_distance <= 0:
            continue
        if st["side"] == "long":
            covered = max(0.0, entry - current)
        else:
            covered = max(0.0, current - entry)
        pct_to_stop = round(100.0 * covered / total_distance, 1)
        out.append({
            "symbol": sym, "side": st["side"], "entry_price": entry,
            "stop_price": stop, "current_price": current,
            "pct_distance_covered": pct_to_stop,
        })
    return out


# ---------------------------------------------------------------------------
# Market-condition proxies
# ---------------------------------------------------------------------------
def market_conditions(broker: AlpacaBroker) -> dict:
    """
    Alpaca's data feed doesn't carry VIX, so "is VIX elevated" is
    approximated with SPY's own realized volatility (stdev of 15Min returns,
    annualized) against a fixed threshold -- labeled explicitly as a proxy,
    not the real index, in the report text. Trend/range is read off the
    same 100-period SMA used as the live mean-reversion trend filter, for
    consistency with what the bot itself is reacting to. Crypto volume is
    compared to its own 30-period average, same definition the momentum
    breakout strategy uses.
    """
    out = {}
    try:
        spy_bars = broker.get_bars("SPY", config.EQUITY, "15Min", limit=120)
        if not spy_bars.empty:
            returns = spy_bars["close"].pct_change().dropna()
            realized_vol_annualized = float(returns.std() * (252 * 26) ** 0.5 * 100)  # ~26 15-min bars/day
            out["spy_realized_vol_annualized_pct"] = round(realized_vol_annualized, 1)
            out["vix_proxy_elevated"] = realized_vol_annualized > 20.0

            trend_sma = sma(spy_bars["close"], min(100, len(spy_bars) - 1))
            last_close = float(spy_bars["close"].iloc[-1])
            last_sma = float(trend_sma.iloc[-1])
            out["spy_trending"] = "up" if last_close > last_sma else "down"
    except Exception as e:
        logger.warning("Could not compute SPY market-condition proxy: %s", e)

    try:
        qqq_bars = broker.get_bars("QQQ", config.EQUITY, "15Min", limit=120)
        if not qqq_bars.empty:
            trend_sma = sma(qqq_bars["close"], min(100, len(qqq_bars) - 1))
            last_close = float(qqq_bars["close"].iloc[-1])
            last_sma = float(trend_sma.iloc[-1])
            out["qqq_trending"] = "up" if last_close > last_sma else "down"
    except Exception as e:
        logger.warning("Could not compute QQQ market-condition proxy: %s", e)

    try:
        btc_bars = broker.get_bars("BTC/USD", config.CRYPTO, "1Hour", limit=40)
        if not btc_bars.empty and len(btc_bars) >= 31:
            avg_vol = float(btc_bars["volume"].iloc[-31:-1].mean())
            last_vol = float(btc_bars["volume"].iloc[-1])
            out["btc_volume_ratio_vs_30h_avg"] = round(last_vol / avg_vol, 2) if avg_vol > 0 else None
    except Exception as e:
        logger.warning("Could not compute BTC volume proxy: %s", e)

    return out


def utc_day_bounds(days_ago: int = 0):
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc) - timedelta(days=days_ago)
    end = start + timedelta(days=1)
    return start, end
