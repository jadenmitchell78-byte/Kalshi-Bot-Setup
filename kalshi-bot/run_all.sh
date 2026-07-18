#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run_all.sh — Bot Suite Launcher
# Starts trading bots as background processes, then runs the Watchdog in
# the foreground so Replit can stream its console output.
#
# The Watchdog monitors all child processes and auto-restarts any that crash.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# Always run from the kalshi-bot directory, regardless of where this script
# is invoked from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# ── Give the system a moment to settle after relaunch ─────────────────────────
sleep 2

# ── Start trading bots in the background ──────────────────────────────────────
log "🏛️  Starting FOMC Minutes Sniper (poll mode)..."
nohup python3 -u fomc_minutes_sniper.py --poll > fomc_minutes_sniper.log 2>&1 &
sleep 1

log "🐦 Starting Social Trail Bot..."
nohup python3 -u social_trail_bot.py > social_trail_bot.log 2>&1 &
sleep 1

log "📊 Starting Social Sentiment Sniper..."
nohup python3 -u social_sentiment_sniper.py > social_sentiment_sniper.log 2>&1 &
sleep 1

log "✅ All trading bots launched."
log "ℹ️  Note: kalshi_auto_trader is a library module — social_trail_bot and fomc_minutes_sniper import it automatically."
log "🐕 Handing control to Watchdog (monitors + auto-restarts all processes)..."

# ── Run Watchdog in the foreground (this is what Replit sees) ─────────────────
exec python3 -u watchdog.py
