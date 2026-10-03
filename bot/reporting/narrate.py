"""
bot/reporting/narrate.py

Turns an already-computed facts dict into the <200-word report text. If
ANTHROPIC_API_KEY is set, asks Claude to write it up in the exact structure
requested (natural, readable prose); the model is given only the computed
facts as context and instructed not to invent numbers, so it's doing
writing, not arithmetic. If the key isn't set, falls back to a plain
deterministic template so the reports still work without it.
"""

import json
import logging
import os

logger = logging.getLogger(__name__)


def _try_anthropic_narrate(system_prompt: str, facts: dict) -> str:
    try:
        import anthropic
    except ImportError:
        return None

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=500,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": (
                    "Here are the computed facts for today's report, as JSON. "
                    "Use ONLY these numbers -- do not invent, estimate, or round "
                    "away any figure that's present. Write the report now, under "
                    "200 words, plain text (no markdown headers), suitable for a "
                    "Telegram message.\n\n" + json.dumps(facts, indent=2, default=str)
                ),
            }],
        )
        return message.content[0].text.strip()
    except Exception as e:
        logger.warning("Anthropic narration failed, falling back to template: %s", e)
        return None


MORNING_SYSTEM_PROMPT = (
    "You write a concise daily morning market briefing for a trader running "
    "an automated multi-strategy Alpaca paper-trading bot (SPY, QQQ, BTC/USD, "
    "GLD, USO). Cover, in order: (1) current open positions with entry price "
    "and unrealized P&L, (2) yesterday's total P&L and per-instrument P&L, "
    "(3) notable market conditions (note explicitly that the VIX figure is a "
    "realized-volatility proxy, not the real index, since Alpaca's feed "
    "doesn't carry VIX), (4) the bot's win rate over the last 7 days, (5) risk "
    "flags: any position approaching its stop, whether the correlation filter "
    "is currently blocking trades, and whether portfolio drawdown from peak "
    "exceeds 5%. Under 200 words. No markdown formatting."
)

EVENING_SYSTEM_PROMPT = (
    "You write a concise evening performance report for a trader running an "
    "automated multi-strategy Alpaca paper-trading bot (SPY, QQQ, BTC/USD, "
    "GLD, USO). Cover, in order: (1) total trades executed today and which "
    "instruments, (2) today's total P&L in dollars and as % of equity, (3) "
    "best and worst trade of the day with details, (4) current equity "
    "balance, (5) whether performance is on track with backtest expectations "
    "or diverging (the facts include the relevant backtest baseline numbers "
    "to compare against), (6) any trades that hit the stop loss today and "
    "whether the stop-loss audit found any that exceeded the 1%-of-equity cap. "
    "Under 200 words. No markdown formatting."
)


def narrate_morning(facts: dict) -> str:
    text = _try_anthropic_narrate(MORNING_SYSTEM_PROMPT, facts)
    if text:
        return text
    return _template_morning(facts)


def narrate_evening(facts: dict) -> str:
    text = _try_anthropic_narrate(EVENING_SYSTEM_PROMPT, facts)
    if text:
        return text
    return _template_evening(facts)


# ---------------------------------------------------------------------------
# Deterministic fallback templates (used if ANTHROPIC_API_KEY isn't set)
# ---------------------------------------------------------------------------
def _template_morning(f: dict) -> str:
    lines = ["MORNING BRIEFING"]
    if f["positions"]:
        lines.append("Open positions:")
        for p in f["positions"]:
            lines.append(f"  {p['symbol']} {p['side']} @ {p['avg_entry_price']:.2f} | "
                         f"unrealized P&L ${p['unrealized_pl']:.2f} ({p['unrealized_plpc']*100:.2f}%)")
    else:
        lines.append("Open positions: none")

    lines.append(f"Yesterday P&L: ${f['yesterday_total_pnl']:.2f}")
    for inst, pnl in f["yesterday_pnl_by_instrument"].items():
        lines.append(f"  {inst}: ${pnl:.2f}")

    mc = f.get("market_conditions", {})
    vix_note = "elevated" if mc.get("vix_proxy_elevated") else "normal"
    lines.append(f"Market: SPY realized-vol proxy {mc.get('spy_realized_vol_annualized_pct', '?')}% "
                 f"({vix_note}, proxy not real VIX); SPY trend {mc.get('spy_trending', '?')}, "
                 f"QQQ trend {mc.get('qqq_trending', '?')}; BTC volume "
                 f"{mc.get('btc_volume_ratio_vs_30h_avg', '?')}x 30h avg.")

    wr = f.get("win_rate_7d_pct")
    lines.append(f"7-day win rate: {wr if wr is not None else 'n/a'}%")

    flags = []
    for p in f.get("stop_proximity", []):
        if p["pct_distance_covered"] >= 80:
            flags.append(f"{p['symbol']} is {p['pct_distance_covered']}% of the way to its stop")
    if f.get("correlation_filter_blocking"):
        flags.append("correlation filter is currently blocking new BTC/USD longs")
    dd = f.get("drawdown_from_peak_pct")
    if dd is not None and dd <= -5:
        flags.append(f"portfolio is {abs(dd):.1f}% below peak equity")
    lines.append("Risk flags: " + ("; ".join(flags) if flags else "none"))

    return "\n".join(lines)


def _template_evening(f: dict) -> str:
    lines = ["EVENING REPORT"]
    lines.append(f"Trades today: {f['trades_today_count']} "
                 f"({', '.join(f['instruments_traded_today']) or 'none'})")
    lines.append(f"Today P&L: ${f['today_pnl_dollars']:.2f} ({f['today_pnl_pct_of_equity']:.2f}% of equity)")
    if f.get("best_trade"):
        b = f["best_trade"]
        lines.append(f"Best: {b['instrument']} {b['direction']} ${b['profit_loss']:.2f} ({b['exit_reason']})")
    if f.get("worst_trade"):
        w = f["worst_trade"]
        lines.append(f"Worst: {w['instrument']} {w['direction']} ${w['profit_loss']:.2f} ({w['exit_reason']})")
    lines.append(f"Current equity: ${f['current_equity']:.2f}")
    lines.append(f"Vs backtest: {f.get('backtest_comparison_note', 'n/a')}")

    stops = f.get("stop_trades_today", [])
    if stops:
        viol = f.get("stop_loss_violations_today", [])
        status = f"{len(viol)} exceeded the 1% cap" if viol else "all within the 1% cap"
        lines.append(f"Stop-loss exits today: {len(stops)} ({status}).")
    else:
        lines.append("Stop-loss exits today: none.")

    return "\n".join(lines)
