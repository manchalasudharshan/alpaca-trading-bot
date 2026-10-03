# Alpaca Multi-Strategy Trading Bot

A continuously-running Python bot that trades 5 instruments (SPY, QQQ,
BTC/USD, GLD, USO) across 3 strategies via the Alpaca Markets API, with
ATR-based position sizing, hard 1%-of-equity stop losses, and a
correlation filter between equity index exposure and BTC.

**This is not financial advice, and this code is not a guarantee of
profitability.** It is a working implementation of the strategy
specification given, meant to be paper-traded, inspected, and tuned before
any consideration of live capital. Algorithmic trading carries real risk
of loss. Test thoroughly on the Alpaca **paper** endpoint first.

## Project structure

```
.
├── config.py                       # instruments, timeframes, strategy & risk params
├── .env                             # your real secrets (gitignored)
├── .env.example                     # template for .env
├── requirements.txt
└── bot/
    ├── main.py                      # entry point / continuous loop
    ├── backtest.py                  # 6-month historical backtest + report
    ├── broker.py                    # Alpaca REST wrapper (bars, orders, clock)
    ├── portfolio.py                 # position tracking + trades.csv / daily_pnl.csv
    ├── risk_manager.py              # ATR sizing, hard stop cap, correlation filter
    ├── indicators.py                # SMA, std-dev, EMA, ATR, rolling high/low
    └── strategies/
        ├── base.py                  # shared Signal / SignalAction types
        ├── mean_reversion.py        # Strategy 1: SPY, QQQ (15m)
        ├── momentum_breakout.py     # Strategy 2: BTC/USD (1h)
        └── trend_following.py       # Strategy 3: GLD, USO (4h)
```

## Setup

1. **Python 3.9+** recommended.

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill in your Alpaca API keys:
   ```bash
   cp .env.example .env
   ```
   Get paper-trading keys from
   https://app.alpaca.markets/paper/dashboard/overview — the bot defaults
   to the paper endpoint (`APCA_API_BASE_URL=https://paper-api.alpaca.markets`)
   so you cannot accidentally go live without explicitly changing that URL.

4. Run it:
   ```bash
   python -m bot.main
   ```

   The bot will log to stdout and to `bot.log`, and will create
   `trades.csv` / `daily_pnl.csv` on first run.

5. **Before running it live (or even on paper with real signals), backtest
   it:**
   ```bash
   python -m bot.backtest
   ```
   See [Backtesting](#backtesting) below.

## Backtesting

`bot/backtest.py` pulls 6 months of historical bars per instrument (15Min
for SPY/QQQ, 1Hour for BTC/USD, 4Hour for GLD/USO) from Alpaca and replays
them through the *exact same* strategy, sizing, and trailing-stop code the
live bot uses — there's no separate "backtest version" of the trading
rules to drift out of sync.

```bash
python -m bot.backtest
```

It simulates realistic conditions:
- **Slippage**: 0.05% against the trader on every fill (entries pay up,
  exits give back).
- **Commission**: $0 (Alpaca is commission-free).
- **No lookahead**: each bar's signal is computed only from bars up to and
  including that bar, same as live.

Two passes are run:

1. **Per-instrument, strategy-isolated** — each instrument trades alone
   against its own $100,000 starting account, so its numbers reflect that
   strategy's edge on that instrument in isolation.
2. **Combined portfolio** — all 5 instruments trade together against one
   shared account, processed in true chronological order across
   timeframes, with the correlation filter actually live (BTC/USD longs
   get blocked in real time whenever SPY and QQQ are both already long).

For each of the above you get: total trades, win rate, average win,
average loss, profit factor, max drawdown, Sharpe ratio, and total return,
printed as a summary table in the console. **Any instrument/strategy with
a negative Sharpe ratio is flagged explicitly** at the end of the table so
you know what to go tune (entry thresholds, trailing-stop multiples, or
timeframe).

Outputs:
- `backtest_results.png` — equity curve chart, one panel per instrument
  plus the combined portfolio.
- `backtest_trades.csv` — every simulated trade (separate from the live
  bot's `trades.csv`).

Starting capital, slippage %, and the trading-day convention used to
annualize Sharpe are all in `config.BACKTEST_PARAMS`.

## How it works

### Strategy 1 — Mean Reversion (SPY, QQQ)
15-minute candles. 20-period SMA ± (1.5σ for SPY, 1.8σ for QQQ) entry
bands; exits when price reverts back through the mean.

### Strategy 2 — Momentum Breakout (BTC/USD)
1-hour candles. Enters long/short on a close beyond the prior 20-period
high/low with volume ≥ 1.5× the 20-period average volume. Uses a 2×ATR
trailing stop that ratchets every bar.

### Strategy 3 — Trend Following (GLD, USO)
4-hour candles. 50/200 EMA golden/death cross entries, 3×ATR trailing
stop.

### Risk management (`bot/risk_manager.py`)
- **Sizing**: `qty = (account_equity × 1%) / ATR`, so a 1-ATR adverse move
  always costs ~1% of equity regardless of instrument — quiet instruments
  get larger size, volatile ones get smaller size.
- **Hard stop**: every position's stop is capped so realized loss can
  never exceed 1% of equity, even if a strategy's own trailing-stop
  distance (2× or 3× ATR) would imply more — the tighter of the two always
  wins.
- **Correlation filter**: if SPY and QQQ are both already long, new
  BTC/USD long entries are blocked.

### Logging
- `trades.csv`: one row per **closed** trade — timestamp, instrument,
  direction, entry price, exit price, P&L, position size, strategy, exit
  reason.
- `daily_pnl.csv`: one row per UTC calendar day with that day's realized
  P&L and trade count, updated intraday as trades close.

### Market hours
- Equities (SPY, QQQ, GLD, USO): the bot checks Alpaca's market clock
  every cycle and only evaluates signals while the market is open.
- Crypto (BTC/USD): evaluated every cycle, 24/7, independent of the
  equity clock.

### Error handling
`bot/broker.py` wraps every Alpaca API call in retry logic with
exponential backoff (`API_MAX_RETRIES` / `API_BACKOFF_BASE_SECONDS` in
`.env`). The main loop catches and logs exceptions per-instrument and
per-cycle so one bad API response or one strategy error never kills the
whole process.

## Customizing

All thresholds (SMA/EMA periods, std-dev multiples, ATR multiples, volume
multiple, risk-per-trade %, poll interval) live in `config.py` — nothing
is hardcoded in the strategy files beyond the logic itself.

## Known limitations / things to review before going live

- Orders are plain market orders; stops are managed in software (checked
  each closed bar), not as broker-side stop orders. This means a large
  intrabar gap could blow through your intended stop before the bot's
  next check — broker-side bracket/stop orders would tighten this at the
  cost of more complex order-state reconciliation.
- Position sizing and the hard-stop cap assume a single account with no
  other manual trading happening concurrently; if you trade the same
  account by hand, the bot's local position book can drift from reality.
- The **live** bot (`bot/main.py`) does not model slippage/commission when
  sizing — expect real fills to differ slightly from the signal-bar close
  price used for sizing math. The **backtest** (`bot/backtest.py`) does
  apply a 0.05% slippage model, but it's still a simplification (fixed %,
  no volatility- or liquidity-dependent slippage, no partial fills).
- Crypto bar/volume semantics can differ by exchange on Alpaca's crypto
  data feed; verify `BTC/USD` volume figures match your expectations
  before relying on the volume-confirmation filter.
- Backtest results are naturally sensitive to the specific 6-month window
  tested (regime-dependent) and to Alpaca's historical data quality/gaps;
  treat them as a sanity check and parameter-tuning aid, not as a
  forward-looking performance guarantee. Past performance, simulated or
  real, does not predict future results.
- The backtest's Sharpe ratio annualizes daily returns using a 252-day
  convention for every instrument, including 24/7 BTC/USD — a standard
  simplification, but worth knowing if you're comparing Sharpe figures
  across instruments precisely.
