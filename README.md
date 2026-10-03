# Alpaca Multi-Strategy Trading Bot

A continuously-running Python bot that trades 5 instruments (SPY, QQQ,
BTC/USD, GLD, USO) across 3 strategies via the Alpaca Markets API, with
ATR-based position sizing, hard 1%-of-equity stop losses, a correlation
filter between equity index exposure and BTC, and a portfolio-level
max-drawdown circuit breaker (see [Circuit breaker](#circuit-breaker)).

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

## Live paper trading + Telegram reports

Every "tick" (one `bot/live_tick.py` run) loads `bot_state.json` (open
positions, stop prices, the daily P&L accumulator, peak equity), runs one
trading cycle, writes `positions_snapshot.json` (Alpaca's own current
positions/equity), and saves state back. Without this persistence, a fresh
process each tick would have no memory of positions it already opened.
`trades.csv`, `daily_pnl.csv`, `bot_state.json`, and `positions_snapshot.json`
get committed back to the repo after every tick, so the live record is
durable and the Telegram reports (below) always see current state via
`git pull`, regardless of where the trading loop itself runs.

**Pick exactly one of these two ways to run it -- never both at once**,
since they'd both submit orders against the same paper account:

### Option A: your own VPS, 24/7 (recommended for an extended run)

`scripts/vps_tick.sh` runs one tick and pushes state back to GitHub; a
cron job fires it every few minutes. See "VPS setup" below for the full
walkthrough (Oracle Cloud, but any always-on Linux box works the same way).

### Option B: GitHub Actions cron (no server to manage)

`.github/workflows/live_trading.yml` runs the same `bot/live_tick.py` on a
schedule instead -- no VM required, but GitHub's scheduler can run a few
minutes late under load and a hosted runner has no local state between
runs (handled the same way, via `bot_state.json`). Runs are serialized
(`concurrency: group: live-trading, cancel-in-progress: false`) so two
ticks can never race. **This workflow is currently disabled**
(`gh workflow disable live_trading.yml`) because live trading moved to a
VPS -- re-enable it (`gh workflow enable live_trading.yml`) only if you
stop the VPS cron job first.

### VPS setup (Oracle Cloud Always Free, or any Ubuntu VPS)

1. Provision an Ubuntu 22.04+ VM (Oracle: an Ampere A1 Always Free shape is
   plenty). Only inbound SSH (22) is needed -- the bot makes outbound calls
   only.
2. `git clone` this repo, create a venv, `pip install -r requirements.txt`.
3. Copy `.env.example` to `.env` and fill in your real paper-trading keys.
4. Give the VM push access: a GitHub fine-grained PAT scoped to just this
   repo (Contents: read/write), set via
   `git remote set-url origin https://<user>:<token>@github.com/<owner>/<repo>.git`.
5. Test one tick by hand: `bash scripts/vps_tick.sh`.
6. Automate it with cron, e.g. every 5 minutes:
   ```
   */5 * * * * cd /home/ubuntu/alpaca-trading-bot && bash scripts/vps_tick.sh >> bot_cron.log 2>&1
   ```
   `scripts/vps_tick.sh` already uses `flock` internally so an overrunning
   tick never overlaps the next cron fire.
7. Ubuntu's `cron` service starts on boot by default, so a VM reboot
   resumes ticking on its own -- no systemd unit needed.

Every closed trade in `trades.csv` now also logs `equity_at_entry` and
`loss_pct_of_equity_at_entry` — the direct audit trail for "is the hard
stop actually capping losses at 1% of equity, no exceptions." A losing
trade's `loss_pct_of_equity_at_entry` should read ≈ -1.00 every time
regardless of instrument.

Each tick also writes **`positions_snapshot.json`**: Alpaca's own current
positions, account equity, and market-open status, straight from the
broker (not just this process's local book) — see `bot/broker.py`'s
`get_positions()` and `bot/live_tick.py`'s `write_positions_snapshot()`.
This is the bridge the Telegram reports below use to know "current
positions"/"current equity" without needing their own route to Alpaca.

### Telegram reports — written by scheduled Claude Cowork sessions

The morning/evening reports are **not** a canned script — they're written
by two scheduled Claude sessions (set up with this project's scheduling
tool), each of which actually reads `trades.csv`, `daily_pnl.csv`,
`positions_snapshot.json`, and `results/` out of this repo, computes the
requested numbers itself, and writes the briefing in prose, the same way
Claude would if asked to do it in a live conversation.

The one thing those scheduled sessions can't do directly is call the
Telegram API — the sandbox they run in only has network access to GitHub,
not to `api.telegram.org`. So delivery is split in two:

1. **Claude reads + writes** — pulls this repo, computes every number from
   the committed files (open positions and unrealized P&L from
   `positions_snapshot.json`, yesterday's/today's P&L from `trades.csv`,
   7-day win rate, stop-loss audit via `loss_pct_of_equity_at_entry`,
   correlation-filter and drawdown-from-peak flags), and composes the
   under-200-word report.
2. **A dumb pipe delivers it** — `.github/workflows/send_telegram.yml`
   (`workflow_dispatch`, takes a `message` input) does nothing but POST
   that finished text to Telegram via `scripts/send_telegram.py`. Claude
   triggers it with `gh api ... actions/workflows/send_telegram.yml/dispatches
   -f message="..."` and confirms the run succeeded.

**Market-condition caveat:** Alpaca's data feed doesn't carry VIX, so "is
VIX elevated" has to be approximated (e.g. from SPY's own realized
volatility) and should be reported as a proxy, not the real index.

### Setup

In addition to the `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY` repo secrets
from [Backtesting](#backtesting):

1. **Telegram bot**: message [@BotFather](https://t.me/BotFather) on
   Telegram, send `/newbot`, follow the prompts — it replies with a token.
2. Send any message to your new bot from whichever Telegram chat should
   receive reports.
3. Visit `https://api.telegram.org/bot<token>/getUpdates` in a browser and
   read the chat id out of `"chat":{"id": ...}` in the JSON.
4. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repo secrets:
   `https://github.com/<owner>/<repo>/settings/secrets/actions`.
5. The two scheduled tasks (morning/evening) are created directly in this
   assistant — no extra setup beyond the secrets above.

## Circuit breaker

In addition to the per-trade risk controls (ATR sizing, hard 1% stop,
correlation filter — see [How it works](#how-it-works)), the bot has a
**portfolio-level max-drawdown circuit breaker** that is independent of,
and never touched by, any of that per-trade logic.

**How it works:** every cycle, `bot/main.py` tracks `peak_equity`, the
highest total account equity ever observed (persisted in `bot_state.json`
across restarts). If current equity ever falls to **`config.MAX_DRAWDOWN_PCT`
below that peak (default 10%)**, the bot:

1. Logs a `CRITICAL` message naming the drawdown and the threshold.
2. Closes **every** open position immediately with market orders —
   first at the broker directly (`AlpacaBroker.close_all_positions()`,
   which flattens Alpaca's own books regardless of local state), then
   reconciles the local position book and logs each close to `trades.csv`.
3. Sets `"trading_halted": true` in `bot_state.json`, along with
   `halt_reason` (the drawdown math) and `halt_at` (UTC timestamp).
4. From that point on, **every subsequent cycle — this process or any
   later one that loads this state file — checks the flag first and skips
   all signal generation and trading entirely**, logging a `CRITICAL`
   reminder each time. `bot/live_tick.py` still writes
   `positions_snapshot.json` after a halted cycle, so monitoring/reporting
   keeps working even while trading is stopped.

**There is no automatic resume.** Nothing in this codebase ever sets
`trading_halted` back to `false` — that is intentional: a drawdown this
large should be reviewed by a human before any more capital is put at
risk. To resume trading:

1. Investigate why the drawdown happened (check `trades.csv`, `bot.log`,
   and the Alpaca dashboard for what was open and why).
2. Manually edit `bot_state.json` and set:
   ```json
   "trading_halted": false
   ```
   (Clearing `halt_reason`/`halt_at` to `null` as well is optional, but
   keeps the file's audit trail clean for the next incident.)
3. Restart the bot (`python -m bot.main`) or let the next `bot/live_tick.py`
   cron tick pick up the edited state — it will resume evaluating signals
   normally, with `peak_equity` and the threshold still in force for next
   time.

The threshold itself lives in `config.py` as `MAX_DRAWDOWN_PCT` (default
`0.10`, i.e. 10%), overridable via the `MAX_DRAWDOWN_PCT` environment
variable.

## How it works

The bot trades 5 instruments, each with a strategy chosen for how that
market actually behaves — the parameters below are read from `config.py`,
so if you retune them (see [Backtesting](#backtesting)) this section's
numbers will drift from the file; the file is always the source of truth.

### S&P 500 (SPY) — Mean Reversion, 15-minute candles
Indices tend to overextend in one direction over a few hours and then
snap back. The bot fades those small reversions: when price moves more
than **2.2 standard deviations** from the 20-period moving average on the
15-minute chart, it takes the opposite side, expecting a revert to the
mean, and exits when price crosses back through the mean. Entries are
additionally gated by a **100-period trend filter** (see below): longs
only fire when price is above the 100-period SMA, shorts only when below
it.

> Originally spec'd at 1.5σ with no trend filter. A 6-month backtest
> showed that overtrading noise — 398 trades, a 33% win rate, deeply
> negative Sharpe. Widening to 2.2σ alone didn't fix it (Sharpe -7.06 →
> -4.89, win rate stuck ~30%). Adding the 100-period trend filter improved
> it substantially (Sharpe -4.89 → **-2.71**, max drawdown -71.05% →
> **-29.22%**) but **still fails** the Sharpe≥0-and-MaxDD≤15% bar. See the
> "Known limitations" / recommendation note below — this one stays
> **paper-only**, not a tuning problem to keep chasing.

### Nasdaq (QQQ) — Mean Reversion, 15-minute candles
Same approach as SPY, same 100-period trend filter. Nasdaq tends to be
more volatile, so the entry threshold is wider: **2.3 standard
deviations** (originally 1.8σ).

> **Tuning history (4 full backtest rounds, see `results/` and commit
> history):** original 1.8σ/no filter → Sharpe -0.43. Widened to 2.3σ →
> Sharpe -1.78 (worse — proof band-width alone wasn't the lever). Added
> 100-period trend filter → **Sharpe -0.79, MaxDD -16.03%** (best result,
> close to the line but still fails on both Sharpe and drawdown). Tried
> widening entries further to 2.5σ with a 150-period filter → worse again
> (Sharpe -1.31, MaxDD -18.92%). Reverted to the 100-period/2.3σ
> combination above as the best of everything tried.
>
> **Root cause, not a parameter:** SPY and QQQ both drifted steadily in
> one direction for the entire 6-month window tested — a trending regime
> that structurally fights mean reversion. The trend filter helped a lot
> (cut SPY's drawdown by more than half) but a filter can only *reduce*
> counter-trend trades, it can't turn a trending window into a range-bound
> one. **Recommendation: do not run SPY/QQQ mean reversion live on these
> parameters.** Either re-backtest over a different (more range-bound)
> historical window before trusting it, or treat SPY/QQQ as paper-trade-only
> until a live range-bound period is observed.

### Bitcoin (BTC/USD) — Momentum Breakout, 1-hour candles
Crypto trends harder than indices, so instead of fading the move the bot
rides it. When price closes above the prior **30-period** high on the
1-hour chart with volume ≥ **2.2×** the 30-period average, it goes long;
a close below the prior 30-period low with the same volume confirmation
exits the long or opens a short. A **1.8×ATR** trailing stop ratchets in
the position's favor every bar.

> **Tuning history:** 20-period/1.5× volume/2.0×ATR stop → Sharpe -0.54,
> -14.7% return, 19.8% win rate (too many weak breakouts reversing
> immediately). Lengthened lookback to 30, volume filter to 2.0×, stop to
> 2.5×ATR → Sharpe +0.50, +9.1% return, but MaxDD -23.36% (over the 15%
> ceiling). Tightened volume to 2.2× and tried trailing-stop multiples of
> 2.2×, 1.8×, and 1.5× ATR to find the drawdown/Sharpe sweet spot — **1.8×
> was best** (Sharpe **+1.05**, MaxDD **-15.82%**, +23.6% return); 2.2× and
> 1.5× were both worse on both metrics.
>
> **Still just over the line:** -15.82% is ~0.8 points past the 15% MaxDD
> ceiling despite four tuning rounds, with a materially positive Sharpe.
> This is the closest of the three failing instruments to passing —
> reasonable next steps if you want to keep pushing it (not yet done):
> tightening `RISK_PARAMS["max_loss_per_trade_of_equity"]` specifically
> for BTC (smaller per-trade risk budget shrinks cumulative drawdown
> directly, at the cost of smaller position sizes), or re-testing the
> 30-period/2.2×-volume combination against a different 6-month window.
> **Recommendation: keep on paper trading until MaxDD is confirmed under
> 15% on at least one more independent window.**

### Gold (GLD) — Trend Following, 4-hour candles
Commodities move in cleaner waves, and intraday whipsaws just add noise,
so this uses a slower signal: a 50/200 EMA crossover on the 4-hour chart.
When the 50 EMA crosses above the 200, it goes long; when it crosses
below, it exits or goes short. A 3×ATR trailing stop ratchets every bar.

### Oil (USO) — Trend Following, 4-hour candles
Same approach and parameters as gold. Commodities tend to respond well to
longer-timeframe trend following — moves are more sustained and less
choppy than indices.

> GLD and USO parameters are unchanged from the original spec — both were
> profitable in the 6-month backtest, though on very few trades (2 and 1
> respectively), since 50/200 EMA crosses on 4h candles are rare. That low
> sample size means "profitable" here is a weak signal either way; a
> longer backtest window would be needed before reading much into it.

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

## Go-live readiness (as of the latest backtest)

Checked against the bar: **flag/fix any strategy with a negative Sharpe
ratio or a max drawdown over 15%.** After 4 real backtest rounds against
live Alpaca market data (see `results/` and the commit history for every
parameter tried):

| Instrument | Sharpe | MaxDD | Passes bar? |
|---|---|---|---|
| SPY | -2.71 | -29.22% | ❌ No — fails both. Regime mismatch, not a tunable parameter (see note above). |
| QQQ | -0.79 | -16.03% | ❌ No — close, but fails both. |
| BTC/USD | +1.05 | -15.82% | ❌ No — Sharpe is good, MaxDD just over the line. |
| GLD | +0.98 | -1.31% | ✅ Yes |
| USO | +3.37 | -3.42% | ✅ Yes (only 1 trade in 6 months — weak sample, read loosely) |

**Recommendation: do not go live on SPY, QQQ, or BTC/USD with these
parameters.** GLD and USO pass but on very few trades each, so "passing"
there is a weak signal rather than a strong one. If you want to keep
iterating instead of leaving these on paper: see the specific next-step
suggestions under each instrument above — this file documents exactly
what was tried and why it didn't fully close the gap, so you're not
starting from zero.

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
