#!/usr/bin/env bash
#
# scripts/vps_tick.sh
#
# Runs one bot/live_tick.py cycle on your own always-on machine (Oracle
# Cloud VM, any VPS) instead of GitHub Actions, then pushes the updated
# trades.csv / daily_pnl.csv / bot_state.json / positions_snapshot.json
# back to the repo -- the Telegram morning/evening reports (run by scheduled
# Claude sessions) read those files straight from GitHub, so this push is
# what keeps them current regardless of where the trading loop itself runs.
#
# flock prevents two overlapping runs (e.g. a slow tick still running when
# cron fires the next one) from racing on bot_state.json or the git commit.
#
# Usage: called by cron every few minutes (see README "VPS setup"). Not
# meant to be run manually except for testing.
#
# Uses venv/bin/python3 explicitly (not a bare `python3`) because cron runs
# this script in a minimal environment with no shell profile/venv activation
# -- a bare `python3` would silently resolve to the system interpreter,
# which doesn't have this project's dependencies (e.g. python-dotenv)
# installed, causing a ModuleNotFoundError every tick.

set -euo pipefail
cd "$(dirname "$0")/.."

exec 200>/tmp/alpaca_bot_tick.lock
flock -n 200 || { echo "Another tick is still running; skipping this one."; exit 0; }

git pull --rebase origin main --quiet || echo "git pull failed, continuing with local state"

venv/bin/python3 -m bot.live_tick

git add -f trades.csv daily_pnl.csv bot_state.json positions_snapshot.json 2>/dev/null || true
if ! git diff --cached --quiet; then
  git commit -m "VPS tick $(date -u +%FT%TZ) [skip ci]" --quiet
  git push origin HEAD:main --quiet || echo "git push failed, see log -- will retry next tick"
else
  echo "No state changes to commit this tick."
fi
