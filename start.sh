#!/data/data/com.termux/files/usr/bin/bash
# ~/peezy_bot/start.sh - Self-healing launcher with tmux & log rotation
set -e

BOT_DIR="/data/data/com.termux/files/home/peezy_bot"
LOG_FILE="$BOT_DIR/bot.log"
OLD_LOG="$BOT_DIR/bot.log.old"
SESSION_NAME="peezy"

cd "$BOT_DIR"

echo "🔄 [LOG-ROTATE] Checking bot log size..."
if [ -f "$LOG_FILE" ]; then
    LOG_SIZE=$(wc -c < "$LOG_FILE" 2>/dev/null || echo 0)
    # If log is larger than 5MB (5242880 bytes), rotate it
    if [ "$LOG_SIZE" -gt 5242880 ]; then
        echo "  📦 Rotating $LOG_FILE ($LOG_SIZE bytes) -> $OLD_LOG"
        mv -f "$LOG_FILE" "$OLD_LOG"
    fi
fi

echo "🚀 Terminating any stale bot instances..."
pkill -9 -f "python.*bot.py" 2>/dev/null || true
pkill -9 -f "python.*peezy.py" 2>/dev/null || true

echo "📦 Syncing code via Multi-Repo Hub..."
python /data/data/com.termux/files/home/multi_repo_hub.py || true

# If --fg or --foreground is passed, run in foreground directly
if [ "$1" = "--fg" ] || [ "$1" = "--foreground" ]; then
    echo "⚡ Starting AURASCAN // KALSHI V2 ENGINE (Foreground)..."
    exec python -u bot.py 2>&1 | tee -a "$LOG_FILE"
fi

# Check if tmux is installed
if command -v tmux >/dev/null 2>&1; then
    # Kill any existing tmux session with the same name
    tmux kill-session -t "$SESSION_NAME" 2>/dev/null || true

    echo "⚡ Launching bot in detached tmux session '$SESSION_NAME'..."
    tmux new-session -d -s "$SESSION_NAME" "cd $BOT_DIR && python -u bot.py 2>&1 | tee -a $LOG_FILE"
    echo "✅ Bot is now running inside tmux session '$SESSION_NAME'."
    echo "   • View output anytime:  tmux attach -t $SESSION_NAME"
    echo "   • Detach from tmux:     Press Ctrl+B, then D"
    echo "   • Follow log directly:  tail -f $LOG_FILE"
else
    echo "⚠️ tmux not found (run 'pkg install tmux -y'). Running in background with nohup..."
    nohup python -u bot.py >> "$LOG_FILE" 2>&1 &
    echo "✅ Bot launched in background (PID: $!). Follow log: tail -f $LOG_FILE"
fi
