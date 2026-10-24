#!/data/data/com.termux/files/usr/bin/bash
set -e
echo "🚀 Terminating any stale bot instances..."
pkill -9 -f "python.*bot.py" 2>/dev/null || true
pkill -9 -f "python.*peezy.py" 2>/dev/null || true

cd /data/data/com.termux/files/home/peezy_bot

echo "📦 Syncing code to GitHub..."
python deploy_push.py || true

echo "⚡ Starting AURASCAN // KALSHI V2 ENGINE..."
exec python -u bot.py
