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
├── strategy_params.json             # auto-tunable signal params only (see below)
├── tuning_history.csv               # auto-tuner audit trail (git-committed)
├── .env                             # your real secrets (gitignored)
├── .env.example                     # template for .env
├── requirements.txt
└── bot/
    ├── main.py                      # entry point / continuous loop
    ├── backtest.py                  # 6-month historical backtest + report
    ├── auto_tune.py                 # automated signal-param re-tuning (see "Automated parameter tuning")
    ├── params_store.py              # strategy_params.json loader (defensive fallback to config.py)
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

`bot/backtest.py` pulls 6 months of historical bars per instrument (5Min
for all 5 instruments -- see the "5-minute timeframe" note below) from Alpaca and replays
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

## Automated parameter tuning

`bot/auto_tune.py` lets the bot re-tune each strategy's own signal/indicator
parameters automatically, with no human approval gate -- so it can keep
adapting to changing market conditions on its own. It is deliberately
**scoped down to just the knobs each strategy already exposes**
(`strategy_params.json`, seeded from the values that used to be hardcoded in
`config.py`):

| Strategy | Tunable params |
|---|---|
| `mean_reversion` (SPY, QQQ) | `lookback` (SMA/stddev period), `entry_std_dev` (per symbol), `trend_filter_period` |
| `momentum_breakout` (BTC/USD) | `lookback`, `volume_multiple`, `trailing_stop_atr_multiple` |
| `trend_following` (GLD, USO) | `fast_ema`, `slow_ema`, `trailing_stop_atr_multiple` |

**It never touches the user's non-negotiable risk rules.** The auto-tuner
has no code path that can modify `bot/risk_manager.py` (the 1%-of-equity
ATR position sizing, the correlation filter, or the hard-stop-never-widens
logic), or `config.RISK_PARAMS`/`config.MAX_DRAWDOWN_PCT`/the circuit
breaker in `bot/main.py`. Those stay exactly as a human configured them,
regardless of what any backtest finds. `tests/test_auto_tune.py` asserts
this directly (byte-for-byte hash of `bot/risk_manager.py` and `config.py`,
plus an equality check on `config.RISK_PARAMS`/`MAX_DRAWDOWN_PCT`, before
and after a tuning run that is engineered to adopt a change).

**How each run works** (`python3 -m bot.auto_tune`):

1. Refuses to run at all if `bot_state.json`'s `trading_halted` flag is set
   (the circuit breaker has tripped) -- re-tuning while trading is halted
   would be tuning against whatever caused the halt, so it no-ops and logs
   why.
2. Refuses to re-tune a strategy it already tuned earlier the same UTC
   calendar day (guarded by `tuning_history.csv`'s last timestamp for that
   strategy) -- safe to put on an hourly cron if you want, it will still
   only actually act once/day per strategy.
3. Pulls a trailing ~90 days of historical bars (reusing
   `bot.backtest.fetch_all_bars`/`AlpacaBroker` -- no separate data-fetching
   logic) and backtests candidates through `bot.backtest.simulate_instrument`/
   `compute_metrics` -- the exact same strategy, risk-sizing, and hard-stop
   code the live bot and `bot/backtest.py` already use.
4. Searches a small, bounded, **per-parameter** grid of candidate values
   (not a full cross-product grid): starting from the strategy's current
   live params, each tunable parameter is nudged through a handful of
   values around its current setting, one parameter at a time, each
   candidate clipped to a hardcoded `[min, max]` range (see
   `bot/auto_tune.py`'s `TUNE_SPECS` for the exact bounds and the reasoning
   behind each one -- e.g. an ATR multiple can never go near zero, a
   lookback period can never collapse to a handful of bars or balloon past
   what the tuning window can even warm up on).
5. Scores each backtest with a simple combination of metrics
   `bot/backtest.py` already computes -- Sharpe ratio, total return, and a
   max-drawdown penalty (`bot.auto_tune.score_metrics`) -- no new risk
   framework invented.
6. Only adopts a candidate if it produced **at least 10 trades** in the
   tuning window (rejects degenerate/overfit candidates that "win" mostly
   by barely trading) **and** beats the current live params' score by a
   minimum margin (the larger of 1.0 score points or 10% of the baseline
   score), so it doesn't thrash parameters chasing backtest noise.
7. Writes any adopted change to `strategy_params.json` and always appends
   one audit row per strategy to `tuning_history.csv` -- timestamp, old/new
   params, old/new score and trade count, and whether it was applied --
   whether or not anything changed, so the full tuning history (including
   "considered but rejected") is reviewable.
8. If either file changed, commits and pushes them to `main`, using the
   same git identity and pull-rebase-then-retry-once pattern as
   `scripts/vps_tick.sh`.

**Cron setup (VPS):** add one line to the same crontab you set up in
[VPS setup](#vps-setup-oracle-cloud-always-free-or-any-ubuntu-vps), running
once a day during low-activity hours (adjust the path/venv to match your
actual setup, same as the tick line above):

```
0 3 * * * cd /home/ubuntu/alpaca-trading-bot && venv/bin/python3 -m bot.auto_tune >> tuning.log 2>&1
```

Notes:
- This is a separate cron line from the `*/5 * * * * ... scripts/vps_tick.sh`
  line -- the tuner is its own process, run far less often, and does not
  need `flock` coordination with the trading tick (it only reads
  `bot_state.json` and writes `strategy_params.json`/`tuning_history.csv`,
  neither of which the tick depends on mid-write).
- **Circuit breaker interaction:** if the circuit breaker is tripped when
  this cron fires, the run logs a warning and exits immediately without
  touching anything -- it will start tuning again automatically once a
  human clears `trading_halted` in `bot_state.json` (see
  [Circuit breaker](#circuit-breaker)) and the next day's cron fire comes
  around.
- **Picking up new params live:** `bot/live_tick.py` (what the VPS cron
  actually runs every few minutes) constructs a brand-new `TradingBot()`
  every tick, which re-reads `strategy_params.json` from scratch on every
  single tick -- so a tuned parameter change is live within one tick of
  being committed, no restart needed. A long-running `python -m bot.main`
  process (if you run it that way instead) also picks up changes without a
  restart: it checks `strategy_params.json`'s mtime at the top of every
  cycle and re-instantiates just the strategy objects when it changes
  (`TradingBot._reload_strategies_if_params_changed`) -- this never
  touches `risk_manager`, the correlation filter, or the circuit breaker,
  all of which live outside the strategy objects.
- **Review history:** `tuning_history.csv` (committed to the repo, same as
  `trades.csv`) has one row per strategy per run -- `old_params`/`new_params`
  are JSON blobs of just that strategy's tunable keys, so you can diff
  exactly what changed and when, and `old_score`/`new_score`/`old_trades`/
  `new_trades` show why (or why not) a change was adopted.

## Tuner agent (LLM advisory layer)

`bot/tuner_agent.py` is an **opt-in** (`TUNER_AGENT_ENABLED=true`, default
`false`) layer on top of the deterministic auto-tuner above. It exists to
bring in context the mechanical 90-day backtest search structurally cannot
see -- news, and a longer memory of past tuning runs -- without weakening
any of the safety guarantees the deterministic tuner already has.

**The reasoning is done by a scheduled Claude session, not an API call.**
Same pattern as the [Telegram reports](#telegram-reports--written-by-scheduled-claude-cowork-sessions)
above -- there is no `ANTHROPIC_API_KEY` anywhere in this feature. Once a
day, a scheduled Claude session (set up with this project's scheduling
tool, same as the report sessions):

1. Pulls this repo (`add_repo`/clone, same as any other scheduled session
   working on this project).
2. Reads `tuning_history.csv` (last 30 days), the current
   `strategy_params.json`, and a summary of live `trades.csv` directly out
   of the repo -- and may do its own web research on the traded symbols
   (SPY, QQQ, BTC/USD, GLD, USO) if that seems useful.
3. Reasons about which extra parameter values might be worth trying, and/or
   flags a strategy that looks structurally broken.
4. Writes a small raw JSON object to a file (see `bot/tuner_agent.py`'s
   module docstring for the exact schema: `strategy_suggestions`, `flags`,
   `note`).
5. Sets dummy Alpaca credentials for the step (`config.py` requires
   `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY` to import at all, even though
   `--apply` never calls Alpaca's API -- same pattern `tests/conftest.py`
   already uses: `export APCA_API_KEY_ID=test-key-id APCA_API_SECRET_KEY=test-secret-key`)
   and runs `python3 -m bot.tuner_agent --apply <path-to-that-json>`.
6. Reports back what it suggested and why.

`bot/tuner_agent.py` itself never calls an LLM and never makes a network
request -- it is purely the safety boundary that session's JSON output has
to pass through (`apply_suggestions()`) before it can ever influence
anything.

**What it is never allowed to do, by construction, not by convention:**
- **Never writes `strategy_params.json`.** Only `bot/auto_tune.py`'s
  existing adoption logic (unchanged: `MIN_TRADES` + the improvement-margin
  check) can do that. The agent can only add extra values to the list of
  candidates `bot/auto_tune.py` was already going to backtest.
- **Never picks a value outside a parameter's existing hardcoded bounds.**
  `bot/auto_tune.py`'s `_clip()` re-clips every agent-suggested value into
  the exact same `[lo, hi]` range (from `TUNE_SPECS`, see "Automated
  parameter tuning" above) that every mechanically-generated candidate is
  already clipped to. This is the single choke point every candidate
  passes through regardless of where it came from --
  `tests/test_tuner_agent.py`'s `TestAgentSuggestionsAlwaysClipped` proves
  a wildly out-of-range suggestion (e.g. `999999.0`) still gets clipped to
  the boundary before it's ever backtested.
- **Never touches `bot/risk_manager.py`, the correlation filter, the
  hard-stop logic, or the max-drawdown circuit breaker.** Same guarantee
  `bot/auto_tune.py` already makes; this module doesn't import or write to
  any of those files.
- **Never runs while the circuit breaker is tripped** -- same guard as
  `bot/auto_tune.py` (`bot/state_utils.py::is_trading_halted()`, shared by
  both).
- **A malformed, missing, or garbage input is a safe no-op.** Whatever raw
  JSON the scheduled session hands to `--apply` passes through
  `_sanitize_suggestions()`: unknown strategy names, non-numeric values, and
  malformed flags are silently dropped (logged, never raised). With the
  feature disabled (the default), or if `--apply` is never run, or trading
  is halted, `bot/auto_tune.py` behaves byte-for-byte identically to a world
  where this module doesn't exist --
  `tests/test_tuner_agent.py`'s `TestDisabledIsIdenticalToBeforeFeatureExisted`
  proves this directly.

**Setup (opt-in):** no extra package, no API key. Add to `.env` on the VPS:
```
TUNER_AGENT_ENABLED=true
```
Set up one more scheduled Claude session (same mechanism as the Telegram
report sessions above), timed to run after `bot/auto_tune.py` usually
finishes (it can take ~2 hours for a 90-day/3-strategy pass) -- e.g. daily
around 6 AM UTC -- with a standalone prompt instructing it to do the 6 steps
above against this repo (`<owner>/<repo>`).

Its suggestions land in `tuner_agent_suggestions.json` and get picked up by
the *next* day's `bot/auto_tune.py` run -- so there's always at least one
full day's lag between a suggestion appearing and it ever being backtested,
let alone adopted. `tuner_agent_log.csv` (git-tracked, same pattern as
`tuning_history.csv`) has one audit row per `--apply` run: how many
suggestions/flags were kept after sanitization, and the session's own
one-line summary of its reasoning.

## How it works

The bot trades 5 instruments, each with a strategy chosen for how that
market actually behaves — the parameters below are read from `config.py`,
so if you retune them (see [Backtesting](#backtesting)) this section's
numbers will drift from the file; the file is always the source of truth.

### 5-minute timeframe (SPY/QQQ/GLD/USO) and 1-hour (BTC/USD)

SPY, QQQ, GLD, and USO run on **5-minute candles** (`timeframe="5Min"` in
`config.INSTRUMENTS`), previously 15Min (SPY/QQQ) and 4Hour (GLD/USO). The
live bot's cron tick fires every 5 minutes, but `bot/main.py` only
evaluates a symbol's strategy when a *new* bar has closed for that
symbol's timeframe -- with the old timeframes, most ticks had no chance of
producing a signal for most symbols (e.g. 11 of every 12 ticks did nothing
for an hourly/4-hourly instrument). Moving these 4 to 5Min aligns strategy
evaluation with the tick cadence, so each tick has a genuine chance to see
a freshly-closed bar and run real strategy logic. This changes *how often*
a signal can fire, not *what* counts as a signal -- the SMA/EMA math is
unchanged.

BTC/USD runs on **1-hour candles** — its original design timeframe,
reverted back on 2026-10-05 after a brief stint on 5Min; see the "Bitcoin"
section below for why. The 5-minute cron tick still fires for BTC/USD just
like every other instrument: `bot/main.py`'s last_seen_bar gate just means
most of those ticks see "no new 1-hour bar yet" and skip it cheaply, while
a genuinely new hourly close is still always picked up and evaluated
within 5 minutes of happening (the next tick) -- there's no gap in
coverage, just some skipped/no-op ticks for this one instrument, same as
every instrument had under the old per-strategy timeframes.

**Real bug this exposed (found and fixed 2026-10-03):** after this switch,
BTC/USD (24/7 crypto) never once saw a new closed bar across 8+ days of
5-minute ticks, even though `bot/broker.py`'s `get_bars()` was fetching
data successfully every time. Root cause: `get_bars()` padded its lookback
window 3x (to cover equities' weekend/holiday gaps) but then passed the
caller's `limit` straight through as the Alpaca API's own `limit` —
Alpaca returns bars ascending from the window's start and caps the total
at `limit`, so for a 24/7 symbol whose padded window contains ~3x more
real bars than `limit`, every single request got silently truncated to the
*oldest* `limit` bars in the window, stuck roughly 8 days behind "now" —
and because the window shifts forward by exactly as much as the truncation
point every tick, it never caught up on its own. `get_bars()` now requests
the full window uncapped (same approach `get_historical_bars()` already
used) and slices to the most recent `limit` bars itself, so it always
returns data ending at "now." Equities were far less affected (market
hours naturally keep the real bar count in a window close to `limit`),
which is why this went unnoticed until BTC/USD's cadence was investigated
directly. See `tests/test_broker.py` for a regression test that reproduces
the exact failure mode.

**This is not free, though, for the 4 instruments that did move to 5Min:**
every SPY/QQQ/GLD/USO threshold below (entry std-dev bands, EMA periods)
was tuned against 15Min/4Hour data. The same numeric values can behave
very differently on 5Min bars (faster-moving indicators, more noise, more
frequent entries), so **treat every SPY/QQQ/GLD/USO parameter in this
section as stale until `bot/auto_tune.py` has re-calibrated it against
live 5Min data** (it runs at most once/day per strategy -- see "Automated
parameter tuning" below) or you've re-run `bot/backtest.py` against a
fresh 5Min historical window. Until then, the historical backtest numbers
quoted in those sections reflect the *old* timeframes and should be read
as "how this strategy's logic performed in the past," not as a live
expectation at 5Min. **BTC/USD is the exception**: its parameters below
were originally tuned on 1Hour data, moved to 5Min, then moved back to
1Hour on 2026-10-05 (see its section below) -- so its numbers are not
stale in the same way, though they still predate live BTC/USD trading
itself and should be read as backtest history, not a live guarantee.

### S&P 500 (SPY) — Mean Reversion, 5-minute candles
Indices tend to overextend in one direction over a few hours and then
snap back. The bot fades those small reversions: when price moves more
than **2.2 standard deviations** from the 20-period moving average on the
5-minute chart, it takes the opposite side, expecting a revert to the
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

### Nasdaq (QQQ) — Mean Reversion, 5-minute candles
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
> 1.5× were both worse on both metrics. All of the above is on 1Hour data,
> this strategy's original and current live timeframe.
>
> **2026-10-05 timeframe incident:** this strategy was swept into the
> "move everything to 5Min" change along with the other 4 instruments
> (same `volume_multiple`/`lookback`/`trailing_stop_atr_multiple` values,
> just a different candle size). On 5Min candles the live bot went days
> without a single trade, `tuning_history.csv` showed a 90-day backtest
> score stuck at -573 across 261 trades with the auto-tuner unable to find
> any improving parameter, and a direct same-params/same-code 1Hour-vs-5Min
> 6-month comparison confirmed why: 1Hour scores Sharpe **+1.03**, profit
> factor **1.40**, MaxDD **-8.6%**, total return **+11.6%** (66 trades);
> 5Min scores Sharpe **-7.57**, profit factor **0.35**, MaxDD **-53.5%**,
> total return **-53.3%** (568 trades) — same rules, 8.6x more trades,
> mostly on volume noise rather than real moves (BTC/USD's 5Min volume per
> bar is tiny and swings 0.15x-3x of its own average within minutes).
> Reverted BTC/USD to `timeframe="1Hour"` in `config.INSTRUMENTS`; the
> other 4 instruments were unaffected and stayed on 5Min.
>
> **Current read:** on its native 1Hour timeframe this is the
> best-performing of the three non-trivial strategies in this doc — a
> genuinely positive Sharpe and a MaxDD *under* the 15% ceiling (-8.6%,
> vs. the -15.82% quoted above from an earlier tuning round on a different
> window). Still only backtest evidence, not live performance.
> **Recommendation: let it accumulate real 1Hour trades on paper before
> treating the backtest numbers as a live guarantee**, and watch
> `tuning_history.csv` over the next several auto-tune runs to confirm the
> score stays positive now that the timeframe matches the design.

### Gold (GLD) — Trend Following, 5-minute candles
Commodities move in cleaner waves, and intraday whipsaws just add noise,
so this uses a slower signal relative to its own bars: a 50/200 EMA
crossover. When the 50 EMA crosses above the 200, it goes long; when it
crosses below, it exits or goes short. A 3×ATR trailing stop ratchets
every bar.

### Oil (USO) — Trend Following, 5-minute candles
Same approach and parameters as gold. Commodities tend to respond well to
trend following — moves are more sustained and less choppy than indices.

> GLD and USO parameters are unchanged from the original spec — both were
> profitable in the 6-month backtest *on the old 4-hour candles*, though on
> very few trades (2 and 1 respectively), since 50/200 EMA crosses on 4h
> candles are rare. That low sample size means "profitable" here was a weak
> signal either way even before the timeframe change. On 5-minute candles
> the 50/200 EMA crossover will fire far more often (200 bars is ~16.7
> hours of 5Min data vs. ~33 days of 4Hour data) -- this is exactly the kind
> of parameter that needs fresh backtesting/auto-tuning at the new
> granularity before being trusted, per the "5-minute timeframe" note
> above.

### Risk management (`bot/risk_manager.py`)
- **Sizing**: `qty = (account_equity × 1%) / ATR`, so a 1-ATR adverse move
  always costs ~1% of equity regardless of instrument — quiet instruments
  get larger size, volatile ones get smaller size.
- **Notional cap**: the ATR formula above only controls dollar risk at a
  1-ATR move. If an instrument's ATR is small relative to its price (e.g.
  BTC/USD during a quiet stretch, ATR ~0.3% of price), that formula alone
  can size a position many times larger than total account equity — the
  dollar risk at 1 ATR is still "correct" on paper, but a gap or flash move
  well beyond 1 ATR could lose far more than intended, and the order may
  not even be fillable against real buying power. This happened for real:
  on 2026-10-03 the bot attempted a **$328,951.11 notional BTC/USD order
  against a $100,000 account** (over 3× equity); only Alpaca's own
  $200,000 max-notional-per-order limit rejected it before anything went
  wrong. `size_position()` now caps `qty` so total notional never exceeds
  `max_position_notional_pct_of_equity` (config.py, default `1.0` = never
  more than 100% of equity, i.e. no leverage), even if that means realized
  risk at the hard stop comes in under the 1%-ATR target. When this cap
  binds, `bot.log` gets a `WARNING` line and the trade's logged reason
  includes `NOTIONAL-CAPPED`.
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
- `bot.log`: every tick's run, at **INFO** level by default — tick
  start/end, one line per instrument's signal (action + reason), order
  submissions, and errors.
- **`LOG_LEVEL`**: set this env var to `DEBUG` to additionally see *why*
  each strategy did or didn't signal on every tick — current price, the
  relevant indicator values (SMA/std-dev, volume vs. its rolling average,
  fast/slow EMA spread, as applicable to that strategy), the threshold(s)
  being compared against, and a one-line verdict, e.g.:
  ```
  BTC/USD momentum_breakout: close=67120.50 prior_20-bar_range=[65800.00, 67000.00], current volume 1.4x 20-bar avg (812.3), need >=2.2x -> no signal: volume not confirmed
  SPY mean_reversion: price $512.30 is 1.10 std-dev from 20-SMA ($508.10, std=3.80), threshold=1.80 std-dev, bands=[501.26, 514.94], trend_filter=long_ok=True short_ok=False -> no signal
  ```
  Unset (or an invalid value) falls back to `INFO`, so nothing changes
  unless you opt in. Add `LOG_LEVEL=DEBUG` to your VPS's `.env` for this
  permanently, or set it for just one manual run without touching `.env`:
  ```
  LOG_LEVEL=DEBUG venv/bin/python3 -m bot.live_tick
  ```
  All logging setup goes through `bot/log_setup.py`'s single
  `setup_logging()` function now, called by both `bot/main.py` and
  `bot/live_tick.py` with `force=True` — this fixes a real bug where
  `bot/main.py`'s own hardcoded `logging.basicConfig()` ran first (via
  `live_tick.py`'s `from bot.main import TradingBot`) and silently made
  `LOG_LEVEL` have no effect at all, regardless of what it was set to.

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
| BTC/USD | +1.03 | -8.6% | ✅ Yes — on its native 1Hour timeframe (see 2026-10-05 note in the Bitcoin section above); earlier rows on this instrument (+1.05/-15.82%, from an older tuning round/window) and the brief 5Min mismatch (-7.57/-53.5%) are both superseded by this result. |
| GLD | +0.98 | -1.31% | ✅ Yes |
| USO | +3.37 | -3.42% | ✅ Yes (only 1 trade in 6 months — weak sample, read loosely) |

**Recommendation: do not go live on SPY or QQQ with these parameters.**
BTC/USD now passes the bar on its corrected (1Hour) timeframe, but is
still backtest evidence only — give it real paper-trading time before
treating that as a live guarantee. GLD and USO pass but on very few
trades each, so "passing" there is a weak signal rather than a strong
one. If you want to keep iterating on SPY/QQQ instead of leaving them on
paper: see the specific next-step suggestions under each instrument
above — this file documents exactly what was tried and why it didn't
fully close the gap, so you're not starting from zero.

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
