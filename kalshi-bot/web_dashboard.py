import os
import sys
import json
import time
import signal
import subprocess
import requests
from flask import Flask, jsonify, request, render_template_string

app = Flask(__name__)

# Paths
DEX_CONFIG = "dex_scanner_config.json"
DEX_STATE = "scanner_state.json"
DEX_HISTORY = "trade_history.json"
SOL_CONFIG = "solana_config.json"
SOL_STATE = "solana_state.json"
SOL_HISTORY = "solana_trade_history.json"
BLACKLIST_FILE = "blacklist.json"

# Process Helper Functions
def is_bot_running(script_name):
    try:
        out = subprocess.check_output(["pgrep", "-f", script_name]).decode()
        pids = [line.strip() for line in out.splitlines() if line.strip()]
        my_pid = os.getpid()
        pids = [p for p in pids if int(p) != my_pid]
        return len(pids) > 0
    except subprocess.CalledProcessError:
        return False
    except Exception as e:
        print(f"Error checking status for {script_name}: {e}")
        return False

def start_bot(script_name, log_name):
    if is_bot_running(script_name):
        return True, "Already running"
    try:
        # Start in background using nohup and redirect logs
        cmd = f"nohup python3 -u {script_name} > {log_name} 2>&1 &"
        subprocess.Popen(cmd, shell=True)
        return True, "Started successfully"
    except Exception as e:
        return False, str(e)

def stop_bot(script_name):
    try:
        out = subprocess.check_output(["pgrep", "-f", script_name]).decode()
        pids = [line.strip() for line in out.splitlines() if line.strip()]
        my_pid = os.getpid()
        pids = [p for p in pids if int(p) != my_pid]
        if not pids:
            return True, "Already stopped"
        for pid in pids:
            os.kill(int(pid), signal.SIGTERM)
        return True, "Stopped successfully"
    except subprocess.CalledProcessError:
        return True, "Already stopped"
    except Exception as e:
        try:
            subprocess.call(f"pkill -f {script_name}", shell=True)
            return True, "Stopped via pkill fallback"
        except Exception as ex:
            return False, f"Error stopping: {e} | Fallback err: {ex}"

# JSON Helpers
def read_json(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error reading {path}: {e}")
    return default if default is not None else {}

def write_json(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error writing {path}: {e}")
        return False

# Dashboard Main Page Template
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Meme Coin Sniper Control Center</title>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;800&family=JetBrains+Mono&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {
            --bg-color: #080911;
            --card-bg: rgba(17, 24, 39, 0.7);
            --border-color: rgba(255, 255, 255, 0.08);
            --primary: #6366f1;
            --primary-grad: linear-gradient(135deg, #6366f1 0%, #a855f7 100%);
            --danger-grad: linear-gradient(135deg, #ef4444 0%, #f43f5e 100%);
            --success-grad: linear-gradient(135deg, #10b981 0%, #059669 100%);
            --text: #f3f4f6;
            --text-dim: #9ca3af;
        }
        * {
            box-sizing: border-box;
        }
        body {
            font-family: 'Outfit', sans-serif;
            background: linear-gradient(135deg, #0b0d19 0%, var(--bg-color) 100%);
            color: var(--text);
            margin: 0;
            padding: 0;
            min-height: 100vh;
        }
        header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px 40px;
            background: rgba(11, 13, 25, 0.8);
            backdrop-filter: blur(12px);
            border-bottom: 1px solid var(--border-color);
            position: sticky;
            top: 0;
            z-index: 10;
        }
        h1 {
            font-size: 24px;
            font-weight: 800;
            background: var(--primary-grad);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            margin: 0;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .container {
            max-width: 1400px;
            margin: 40px auto;
            padding: 0 20px;
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 30px;
        }
        @media (max-width: 1024px) {
            .container {
                grid-template-columns: 1fr;
            }
        }
        .card {
            background: var(--card-bg);
            backdrop-filter: blur(12px);
            border: 1px solid var(--border-color);
            border-radius: 16px;
            padding: 24px;
            box-shadow: 0 8px 32px 0 rgba(0, 0, 0, 0.4);
            display: flex;
            flex-direction: column;
            gap: 20px;
        }
        .card-title {
            font-size: 18px;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 10px;
            margin: 0;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .grid-2 {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }
        .btn {
            font-family: inherit;
            font-size: 14px;
            font-weight: 600;
            padding: 12px 20px;
            border-radius: 8px;
            border: none;
            cursor: pointer;
            transition: all 0.2s ease;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            gap: 8px;
            color: white;
            text-shadow: 0 1px 2px rgba(0,0,0,0.2);
        }
        .btn:hover {
            transform: translateY(-2px);
        }
        .btn:active {
            transform: translateY(0);
        }
        .btn-primary {
            background: var(--primary-grad);
            box-shadow: 0 4px 15px rgba(168, 85, 247, 0.3);
        }
        .btn-primary:hover {
            box-shadow: 0 6px 20px rgba(168, 85, 247, 0.5);
        }
        .btn-danger {
            background: var(--danger-grad);
            box-shadow: 0 4px 15px rgba(239, 68, 68, 0.3);
        }
        .btn-danger:hover {
            box-shadow: 0 6px 20px rgba(239, 68, 68, 0.5);
        }
        .btn-success {
            background: var(--success-grad);
            box-shadow: 0 4px 15px rgba(16, 185, 129, 0.3);
        }
        .btn-success:hover {
            box-shadow: 0 6px 20px rgba(16, 185, 129, 0.5);
        }
        .btn-outline {
            background: transparent;
            border: 1px solid var(--border-color);
            color: var(--text);
        }
        .btn-outline:hover {
            background: rgba(255, 255, 255, 0.05);
        }
        .status-badge {
            padding: 4px 8px;
            border-radius: 6px;
            font-size: 12px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 5px;
        }
        .status-active {
            background: rgba(16, 185, 129, 0.15);
            color: #10b981;
            border: 1px solid rgba(16, 185, 129, 0.3);
        }
        .status-inactive {
            background: rgba(239, 68, 68, 0.15);
            color: #ef4444;
            border: 1px solid rgba(239, 68, 68, 0.3);
        }
        .status-paused {
            background: rgba(245, 158, 11, 0.15);
            color: #f59e0b;
            border: 1px solid rgba(245, 158, 11, 0.3);
        }
        .stat-box {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px;
            text-align: center;
        }
        .stat-value {
            font-size: 24px;
            font-weight: 800;
            margin-bottom: 4px;
        }
        .stat-label {
            font-size: 12px;
            color: var(--text-dim);
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        .form-group {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }
        label {
            font-size: 13px;
            font-weight: 600;
            color: var(--text-dim);
        }
        input, select {
            background: rgba(255, 255, 255, 0.05);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 12px;
            color: white;
            font-family: inherit;
            font-size: 14px;
            width: 100%;
            transition: border-color 0.2s;
        }
        input:focus, select:focus {
            outline: none;
            border-color: var(--primary);
        }
        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
        }
        th, td {
            text-align: left;
            padding: 12px;
            border-bottom: 1px solid var(--border-color);
        }
        th {
            color: var(--text-dim);
            font-weight: 600;
        }
        tr:hover td {
            background: rgba(255, 255, 255, 0.02);
        }
        .pnl-positive {
            color: #10b981;
            font-weight: 600;
        }
        .pnl-negative {
            color: #ef4444;
            font-weight: 600;
        }
        .log-box {
            font-family: 'JetBrains Mono', monospace;
            background: #05060b;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 16px;
            height: 250px;
            overflow-y: auto;
            font-size: 12px;
            line-height: 1.6;
            white-space: pre-wrap;
            color: #34d399;
        }
        .tab-btn {
            background: transparent;
            border: none;
            color: var(--text-dim);
            padding: 8px 16px;
            cursor: pointer;
            font-weight: 600;
            font-size: 14px;
            border-radius: 6px;
        }
        .tab-btn.active {
            background: rgba(255, 255, 255, 0.08);
            color: white;
        }
        .tabs {
            display: flex;
            gap: 10px;
            background: rgba(0, 0, 0, 0.2);
            padding: 5px;
            border-radius: 8px;
            width: fit-content;
        }
        /* Toast Notification Styles */
        .toast {
            animation: slideIn 0.35s cubic-bezier(0.175, 0.885, 0.32, 1.275) forwards;
            background: rgba(13, 17, 28, 0.85);
            backdrop-filter: blur(12px);
            -webkit-backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.08);
            border-radius: 10px;
            padding: 12px 20px;
            color: #f3f4f6;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
            display: flex;
            align-items: center;
            gap: 12px;
            font-size: 14px;
            min-width: 250px;
            max-width: 400px;
            z-index: 1000;
        }
        @keyframes slideIn {
            from {
                transform: translateX(120%);
                opacity: 0;
            }
            to {
                transform: translateX(0);
                opacity: 1;
            }
        }
        .toast.fade-out {
            animation: fadeOut 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275) forwards;
        }
        @keyframes fadeOut {
            from {
                transform: translateX(0);
                opacity: 1;
            }
            to {
                transform: translateX(120%);
                opacity: 0;
            }
        }
    </style>
</head>
<body>
    <div id="toast-container" style="position: fixed; bottom: 20px; right: 20px; z-index: 1000; display: flex; flex-direction: column; gap: 10px;"></div>
    <header>
        <h1>⚡ Meme Coin Control Center <span style="font-size: 12px; font-weight: 400; color: var(--text-dim); border: 1px solid var(--border-color); padding: 2px 8px; border-radius: 4px;">PRO Suite v3.0</span></h1>
        <div style="display: flex; gap: 15px; align-items: center;">
            <div id="connection-status" class="status-badge status-active">● Connected to gateway</div>
            <button class="btn btn-outline" style="padding: 8px 16px;" onclick="refreshData()">🔄 Refresh</button>
        </div>
    </header>

    <div class="container">
        <!-- System Health Diagnostics Card -->
        <div class="card" style="grid-column: span 2;">
            <h2 class="card-title">🛡️ System Health Diagnostics & Telemetry <span style="font-size: 12px; font-weight: 400; color: var(--text-dim);" id="health-last-updated">Last Updated: --</span></h2>
            <div class="grid-2" style="grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px;" id="health-grid">
                <div style="color: var(--text-dim); text-align: center; padding: 20px; grid-column: span 3;">Loading health diagnostics...</div>
            </div>
        </div>

        <!-- Bot Process Monitor Card -->
        <div class="card">
            <h2 class="card-title">🤖 Bot Process & Scanner Manager</h2>
            <div style="display: flex; flex-direction: column; gap: 15px;">
                <!-- TG Bot -->
                <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); padding: 12px; border-radius: 10px; border: 1px solid var(--border-color);">
                    <div>
                        <div style="font-weight: 600;">Telegram Gateway bot (@MemCoinAiBot)</div>
                        <div style="font-size: 12px; color: var(--text-dim);" id="tg-status-desc">Loading...</div>
                    </div>
                    <div style="display: flex; gap: 10px; align-items: center;">
                        <span id="tg-status-badge" class="status-badge">● Checking</span>
                        <button id="tg-btn" class="btn btn-outline" style="padding: 6px 12px; font-size: 12px;" onclick="toggleBot('tg_bot')">Start</button>
                    </div>
                </div>

                <!-- Solana Bot -->
                <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); padding: 12px; border-radius: 10px; border: 1px solid var(--border-color);">
                    <div>
                        <div style="font-weight: 600;">Solana Raydium Sniper Bot</div>
                        <div style="font-size: 12px; color: var(--text-dim);" id="sol-status-desc">Loading...</div>
                    </div>
                    <div style="display: flex; gap: 10px; align-items: center;">
                        <span id="sol-status-badge" class="status-badge">● Checking</span>
                        <button id="sol-btn" class="btn btn-outline" style="padding: 6px 12px; font-size: 12px;" onclick="toggleBot('solana')">Start</button>
                    </div>
                </div>

                <!-- Base Bot -->
                <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); padding: 12px; border-radius: 10px; border: 1px solid var(--border-color);">
                    <div>
                        <div style="font-weight: 600;">Base Uniswap Scanner Bot</div>
                        <div style="font-size: 12px; color: var(--text-dim);" id="base-status-desc">Loading...</div>
                    </div>
                    <div style="display: flex; gap: 10px; align-items: center;">
                        <span id="base-status-badge" class="status-badge">● Checking</span>
                        <button id="base-btn" class="btn btn-outline" style="padding: 6px 12px; font-size: 12px;" onclick="toggleBot('base')">Start</button>
                    </div>
                </div>

                <!-- OANDA Bot -->
                <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); padding: 12px; border-radius: 10px; border: 1px solid var(--border-color);">
                    <div>
                        <div style="font-weight: 600;">OANDA Forex Bot</div>
                        <div style="font-size: 12px; color: var(--text-dim);" id="oanda-status-desc">Loading...</div>
                    </div>
                    <div style="display: flex; gap: 10px; align-items: center;">
                        <span id="oanda-status-badge" class="status-badge">● Checking</span>
                        <button id="oanda-btn" class="btn btn-outline" style="padding: 6px 12px; font-size: 12px;" onclick="toggleBot('oanda')">Start</button>
                    </div>
                </div>

                <!-- Social Trail Bot -->
                <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); padding: 12px; border-radius: 10px; border: 1px solid var(--border-color);">
                    <div>
                        <div style="font-weight: 600;">Social Trail Bot (X/Instagram Scraper)</div>
                        <div style="font-size: 12px; color: var(--text-dim);" id="twitter-status-desc">Loading...</div>
                    </div>
                    <div style="display: flex; gap: 10px; align-items: center;">
                        <span id="twitter-status-badge" class="status-badge">● Checking</span>
                        <button id="twitter-btn" class="btn btn-outline" style="padding: 6px 12px; font-size: 12px;" onclick="toggleBot('social_trail')">Start</button>
                    </div>
                </div>

                <!-- Telegram Parlay Bot -->
                <div style="display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); padding: 12px; border-radius: 10px; border: 1px solid var(--border-color);">
                    <div>
                        <div style="font-weight: 600;">Telegram Parlay Bot (@ParlayAiBot)</div>
                        <div style="font-size: 12px; color: var(--text-dim);" id="tg_parlay-status-desc">Loading...</div>
                    </div>
                    <div style="display: flex; gap: 10px; align-items: center;">
                        <span id="tg_parlay-status-badge" class="status-badge">● Checking</span>
                        <button id="tg_parlay-btn" class="btn btn-outline" style="padding: 6px 12px; font-size: 12px;" onclick="toggleBot('tg_parlay')">Start</button>
                    </div>
                </div>
            </div>
        </div>

        <!-- Portfolio Scoreboard Card -->
        <div class="card">
            <h2 class="card-title">📊 Closed Trade Scoreboards</h2>
            <div class="grid-2">
                <div class="stat-box">
                    <div id="score-winrate" class="stat-value pnl-positive">--%</div>
                    <div class="stat-label">Win Rate</div>
                </div>
                <div class="stat-box">
                    <div id="score-profit" class="stat-value pnl-positive">-- SOL</div>
                    <div class="stat-label">Net P&L (SOL/USD)</div>
                </div>
            </div>
            <div class="grid-2" style="font-size: 14px;">
                <div style="text-align: center; background: rgba(255,255,255,0.01); padding: 8px; border-radius: 6px; border: 1px solid var(--border-color);">
                    Wins: <span id="score-wins" style="font-weight:600; color:#10b981;">0</span> | Losses: <span id="score-losses" style="font-weight:600; color:#ef4444;">0</span>
                </div>
                <div style="text-align: center; background: rgba(255,255,255,0.01); padding: 8px; border-radius: 6px; border: 1px solid var(--border-color);">
                    Total Trades: <span id="score-total" style="font-weight: 600;">0</span>
                </div>
            </div>
        </div>

        <!-- Active Position Card -->
        <div class="card">
            <h2 class="card-title">🚨 Active Trade & Positions</h2>
            
            <!-- Solana Position -->
            <div style="background: rgba(99, 102, 241, 0.05); border: 1px solid rgba(99, 102, 241, 0.2); border-radius: 12px; padding: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <span style="font-weight: 600; display: flex; align-items: center; gap: 6px;">
                        <span style="color:#a855f7;">SOL</span> Active Position
                    </span>
                    <span id="pos-sol-badge" class="status-badge status-inactive">No Position</span>
                </div>
                <div id="pos-sol-details" style="display: none; font-size: 14px; flex-direction: column; gap: 10px; margin-bottom: 15px;">
                    <div style="display: flex; justify-content: space-between;">
                        <span>Token: <strong id="pos-sol-symbol">--</strong></span>
                        <span>Entry: <span id="pos-sol-entry">-- SOL</span></span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Holdings: <span id="pos-sol-holdings">--</span></span>
                        <span>Current P&L: <span id="pos-sol-pnl" class="pnl-positive">+0.00%</span></span>
                    </div>
                    <div style="font-size: 11px; color: var(--text-dim); word-break: break-all;">
                        Mint: <span id="pos-sol-addr">--</span>
                    </div>
                </div>
                <div style="display: flex; gap: 10px;">
                    <button class="btn btn-outline" style="flex:1; padding: 8px 12px;" onclick="panicSell('solana', true)">Sell 50%</button>
                    <button class="btn btn-danger" style="flex:1.5; padding: 8px 12px;" onclick="panicSell('solana', false)">Panic Sell 100%</button>
                </div>
                <!-- Solana Live Chart -->
                <div id="sol-chart-container" style="display: none; margin-top: 15px; border-radius: 8px; overflow: hidden; border: 1px solid var(--border-color); height: 260px;">
                    <iframe id="sol-chart-iframe" src="" style="width: 100%; height: 100%; border: none;"></iframe>
                </div>
            </div>

            <!-- Base Position -->
            <div style="background: rgba(16, 185, 129, 0.05); border: 1px solid rgba(16, 185, 129, 0.2); border-radius: 12px; padding: 16px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <span style="font-weight: 600; display: flex; align-items: center; gap: 6px;">
                        <span style="color:#10b981;">BASE</span> Active Position
                    </span>
                    <span id="pos-base-badge" class="status-badge status-inactive">No Position</span>
                </div>
                <div id="pos-base-details" style="display: none; font-size: 14px; flex-direction: column; gap: 10px; margin-bottom: 15px;">
                    <div style="display: flex; justify-content: space-between;">
                        <span>Token: <strong id="pos-base-symbol">--</strong></span>
                        <span>Entry: <span id="pos-base-entry">$--</span></span>
                    </div>
                    <div style="display: flex; justify-content: space-between;">
                        <span>Holdings: <span id="pos-base-holdings">--</span></span>
                        <span>Current P&L: <span id="pos-base-pnl" class="pnl-positive">+0.00%</span></span>
                    </div>
                    <div style="font-size: 11px; color: var(--text-dim); word-break: break-all;">
                        Contract: <span id="pos-base-addr">--</span>
                    </div>
                </div>
                <div style="display: flex; gap: 10px;">
                    <button class="btn btn-outline" style="flex:1; padding: 8px 12px;" onclick="panicSell('base', true)">Sell 50%</button>
                    <button class="btn btn-danger" style="flex:1.5; padding: 8px 12px;" onclick="panicSell('base', false)">Panic Sell 100%</button>
                </div>
                <!-- Base Live Chart -->
                <div id="base-chart-container" style="display: none; margin-top: 15px; border-radius: 8px; overflow: hidden; border: 1px solid var(--border-color); height: 260px;">
                    <iframe id="base-chart-iframe" src="" style="width: 100%; height: 100%; border: none;"></iframe>
                </div>
            </div>

            <!-- OANDA Position -->
            <div style="background: rgba(34, 197, 94, 0.05); border: 1px solid rgba(34, 197, 94, 0.2); border-radius: 12px; padding: 16px; margin-top: 15px;">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <span style="font-weight: 600; display: flex; align-items: center; gap: 6px;">
                        <span style="color:#22c55e;">OANDA</span> Active Forex Positions
                    </span>
                    <span id="pos-oanda-badge" class="status-badge status-inactive">No Position</span>
                </div>
                <div id="pos-oanda-details" style="display: none; font-size: 14px; flex-direction: column; gap: 10px; margin-bottom: 15px;">
                    <div style="max-height: 150px; overflow-y: auto; width: 100%;">
                        <table style="width: 100%; text-align: left; font-size: 13px; border-collapse: collapse;">
                            <thead>
                                <tr style="border-bottom: 1px solid rgba(255,255,255,0.1); color: var(--text-dim);">
                                    <th style="padding: 4px 0;">Pair</th>
                                    <th style="padding: 4px 0;">Units</th>
                                    <th style="padding: 4px 0;">Entry</th>
                                    <th style="padding: 4px 0;">Current</th>
                                    <th style="padding: 4px 0;">PnL</th>
                                </tr>
                            </thead>
                            <tbody id="pos-oanda-list">
                                <!-- Active trades loaded here -->
                            </tbody>
                        </table>
                    </div>
                    <div style="display: flex; justify-content: space-between; font-size: 12px; color: var(--text-dim); border-top: 1px solid rgba(255,255,255,0.05); padding-top: 8px; width: 100%;">
                        <span>Balance: <strong id="oanda-acc-bal">$--</strong></span>
                        <span>NAV: <strong id="oanda-acc-nav">$--</strong></span>
                    </div>
                </div>
                <div style="display: flex; gap: 10px;">
                    <button class="btn btn-danger" style="flex:1; padding: 8px 12px;" onclick="panicSell('oanda', false)">Panic Close All OANDA Positions</button>
                </div>
            </div>
        </div>

        <!-- Configuration Settings Card -->
        <div class="card">
            <h2 class="card-title">⚙️ Scanner Configuration Panels</h2>
            
            <div class="tabs">
                <button id="tab-sol" class="tab-btn active" onclick="switchConfigTab('sol')">Solana Config</button>
                <button id="tab-base" class="tab-btn" onclick="switchConfigTab('base')">Base Config</button>
                <button id="tab-oanda" class="tab-btn" onclick="switchConfigTab('oanda')">OANDA Config</button>
                <button id="tab-social" class="tab-btn" onclick="switchConfigTab('social')">Social Trail Config</button>
            </div>

            <!-- Solana Config Form -->
            <form id="form-sol-config" style="display: flex; flex-direction: column; gap: 15px;" onsubmit="saveConfig(event, 'solana')">
                <div class="grid-2">
                    <div class="form-group">
                        <label>Trade Size (SOL)</label>
                        <input type="number" step="0.001" id="cfg-sol-amount" required>
                    </div>
                    <div class="form-group">
                        <label>Slippage %</label>
                        <input type="number" step="0.5" id="cfg-sol-slippage" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Stop Loss %</label>
                        <input type="number" step="0.5" id="cfg-sol-stop" required>
                    </div>
                    <div class="form-group">
                        <label>Take Profit %</label>
                        <input type="number" step="1" id="cfg-sol-profit" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Priority Fee (lamports / auto)</label>
                        <input type="text" id="cfg-sol-pfee" placeholder="100000 or auto" required>
                    </div>
                    <div class="form-group">
                        <label>Min Liquidity (SOL)</label>
                        <input type="number" step="0.1" id="cfg-sol-minliq" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Time-based Exit (Minutes)</label>
                        <input type="number" id="cfg-sol-timeexit" required>
                    </div>
                    <div class="form-group">
                        <label>Scanner State</label>
                        <select id="cfg-sol-active">
                            <option value="true">🟢 Active & Snipe</option>
                            <option value="false">⏸️ Paused</option>
                        </select>
                    </div>
                </div>
                <div class="form-group">
                    <label>Copy-Trading Target Wallets (comma-separated)</label>
                    <input type="text" id="cfg-sol-copywallets" placeholder="e.g. PubKey1, PubKey2">
                </div>
                <button type="submit" class="btn btn-primary">Save Solana Settings</button>
            </form>

            <!-- Base Config Form -->
            <form id="form-base-config" style="display: none; flex-direction: column; gap: 15px;" onsubmit="saveConfig(event, 'base')">
                <div class="grid-2">
                    <div class="form-group">
                        <label>Trade Size (ETH)</label>
                        <input type="number" step="0.0001" id="cfg-base-amount" required>
                    </div>
                    <div class="form-group">
                        <label>Gas Premium %</label>
                        <input type="number" step="0.5" id="cfg-base-gas" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Stop Loss %</label>
                        <input type="number" step="0.5" id="cfg-base-stop" required>
                    </div>
                    <div class="form-group">
                        <label>Take Profit %</label>
                        <input type="number" step="1" id="cfg-base-profit" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Min Liquidity (ETH)</label>
                        <input type="number" step="0.1" id="cfg-base-minliq" required>
                    </div>
                    <div class="form-group">
                        <label>Max Liquidity (ETH)</label>
                        <input type="number" step="0.1" id="cfg-base-maxliq" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Buy Block Delay</label>
                        <input type="number" id="cfg-base-delay" required>
                    </div>
                    <div class="form-group">
                        <label>Scanner State</label>
                        <select id="cfg-base-active">
                            <option value="true">🟢 Active & Snipe</option>
                            <option value="false">⏸️ Paused</option>
                        </select>
                    </div>
                </div>
                <div class="form-group">
                    <label>X (Twitter) Promo Webhook URL</label>
                    <input type="text" id="cfg-base-xwebhook" placeholder="Enter URL or 'none' / empty to disable">
                </div>
                <button type="submit" class="btn btn-primary">Save Base Settings</button>
            </form>

            <!-- OANDA Config Form -->
            <form id="form-oanda-config" style="display: none; flex-direction: column; gap: 15px;" onsubmit="saveConfig(event, 'oanda')">
                <div class="grid-2">
                    <div class="form-group">
                        <label>Account ID</label>
                        <input type="text" id="cfg-oanda-acct" required>
                    </div>
                    <div class="form-group">
                        <label>Environment</label>
                        <select id="cfg-oanda-env">
                            <option value="live">🟢 Live (fxtrade)</option>
                            <option value="practice">🧪 Practice (fxpractice)</option>
                        </select>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Trade Units size (100 = 0.001 lot)</label>
                        <input type="number" id="cfg-oanda-units" required>
                    </div>
                    <div class="form-group">
                        <label>Check Interval (seconds)</label>
                        <input type="number" id="cfg-oanda-interval" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Strategy</label>
                        <select id="cfg-oanda-strat">
                            <option value="ema_crossover">EMA Crossover + RSI</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Scanner State</label>
                        <select id="cfg-oanda-active">
                            <option value="true">🟢 Active & Trade</option>
                            <option value="false">⏸️ Paused</option>
                        </select>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>EMA Fast Period</label>
                        <input type="number" id="cfg-oanda-emafast" required>
                    </div>
                    <div class="form-group">
                        <label>EMA Slow Period</label>
                        <input type="number" id="cfg-oanda-emaslow" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>RSI Period</label>
                        <input type="number" id="cfg-oanda-rsiperiod" required>
                    </div>
                    <div class="form-group">
                        <label>Dry Run</label>
                        <select id="cfg-oanda-dryrun">
                            <option value="false">🔴 Live Trading</option>
                            <option value="true">🟡 Paper Trading</option>
                        </select>
                    </div>
                </div>
                <div class="form-group">
                    <label>Traded FX Pairs (comma-separated)</label>
                    <input type="text" id="cfg-oanda-instruments" placeholder="e.g. EUR_USD, USD_JPY, GBP_USD" required>
                </div>
                <button type="submit" class="btn btn-primary">Save OANDA Settings</button>
            </form>

            <!-- Social Trail Config Form -->
            <form id="form-social-config" style="display: none; flex-direction: column; gap: 15px;" onsubmit="saveConfig(event, 'social_trail')">
                <div class="grid-2">
                    <div class="form-group">
                        <label>Scraping Mode</label>
                        <select id="cfg-twitter-mode">
                            <option value="scraping_simulation">🧪 Simulation (Mock Posts)</option>
                            <option value="rss_feed">🐦 Rotating RSS Feed (Nitter/RSSHub)</option>
                            <option value="official_api">🔑 Official X API (v2)</option>
                            <option value="apify">☁️ Apify Actor API</option>
                            <option value="playwright">🎭 Playwright Headless Browser</option>
                            <option value="webhook_only">🔌 Webhook Only (Real-time Ingestion)</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Social Bot State</label>
                        <select id="cfg-twitter-active">
                            <option value="true">🟢 Active & Snipe</option>
                            <option value="false">⏸️ Paused</option>
                        </select>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Polling Interval (seconds)</label>
                        <input type="number" id="cfg-twitter-interval" required min="10">
                    </div>
                    <div class="form-group">
                        <label>Official X API Bearer Token</label>
                        <input type="password" id="cfg-twitter-bearer" placeholder="Enter X API v2 Bearer Token">
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Apify API Token</label>
                        <input type="password" id="cfg-twitter-apifytoken" placeholder="Enter Apify API Token">
                    </div>
                    <div class="form-group">
                        <label>Apify Actor IDs (X / Instagram)</label>
                        <div style="display: flex; gap: 10px;">
                            <input type="text" id="cfg-twitter-apifyactorx" placeholder="X Actor (e.g. apify/twitter-scraper)" style="flex: 1;">
                            <input type="text" id="cfg-twitter-apifyactorig" placeholder="IG Actor (e.g. apify/instagram-scraper)" style="flex: 1;">
                        </div>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group">
                        <label>Monitored X Accounts (comma-separated)</label>
                        <input type="text" id="cfg-twitter-accounts" placeholder="e.g. lockedlines, popularparlay" required>
                    </div>
                    <div class="form-group">
                        <label>Monitored Instagram Accounts (comma-separated)</label>
                        <input type="text" id="cfg-instagram-accounts" placeholder="e.g. lockedlines, dougspicks" required>
                    </div>
                </div>
                <div class="grid-2">
                    <div class="form-group" style="background: rgba(168, 85, 247, 0.05); padding: 12px; border-radius: 8px; border: 1px solid rgba(168, 85, 247, 0.2);">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-weight:600; color:#a855f7;">🟣 Solana Auto-Buy</span>
                            <select id="cfg-twitter-buysol" style="width:auto; padding: 2px 6px;">
                                <option value="true">Enable</option>
                                <option value="false">Disable</option>
                            </select>
                        </div>
                        <label>Solana Trade Amount (SOL)</label>
                        <input type="number" step="0.001" id="cfg-twitter-solamount" required>
                    </div>
                    <div class="form-group" style="background: rgba(59, 130, 246, 0.05); padding: 12px; border-radius: 8px; border: 1px solid rgba(59, 130, 246, 0.2);">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                            <span style="font-weight:600; color:#3b82f6;">🔷 Base Auto-Buy</span>
                            <select id="cfg-twitter-buybase" style="width:auto; padding: 2px 6px;">
                                <option value="true">Enable</option>
                                <option value="false">Disable</option>
                            </select>
                        </div>
                        <label>Base Trade Amount (ETH)</label>
                        <input type="number" step="0.0001" id="cfg-twitter-baseamount" required>
                    </div>
                </div>
                <div class="form-group" style="background: rgba(255, 255, 255, 0.03); padding: 12px; border-radius: 8px; border: 1px solid var(--border-color);">
                    <label style="font-weight: 600;">🔌 Real-Time Webhook Integration</label>
                    <div style="font-size: 13px; color: var(--text-dim); margin-top: 5px;">
                        Set your external scraper/listener (Apify task, Custom worker) webhooks to target:
                        <code style="background: #111; padding: 3px 6px; border-radius: 4px; color: #10b981; font-weight: bold; display: block; margin-top: 5px; word-break: break-all;">
                            http://YOUR_SERVER_IP:PORT/api/webhook/social
                        </code>
                    </div>
                </div>
                <button type="submit" class="btn btn-primary">Save Social Settings</button>
            </form>
        </div>

        <!-- Blacklist Creator Manager Card -->
        <div class="card">
            <h2 class="card-title">🚫 Serial Creator Blacklist Manager</h2>
            
            <form onsubmit="addToBlacklist(event)" style="display: flex; gap: 10px;">
                <input type="text" id="bl-address" placeholder="Deployer creator public key / contract address" required style="flex:1;">
                <input type="text" id="bl-reason" placeholder="Reason (e.g. Scammer)" style="flex:1;">
                <button type="submit" class="btn btn-success" style="white-space: nowrap;">➕ Blacklist</button>
            </form>

            <div style="max-height: 200px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 8px;">
                <table id="blacklist-table">
                    <thead>
                        <tr>
                            <th>Deployer Wallet</th>
                            <th>Reason</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="blacklist-body">
                        <tr>
                            <td colspan="3" style="text-align: center; color: var(--text-dim);">No blacklisted creators yet.</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Monitored Cappers & Leaderboard Manager -->
        <div class="card">
            <h2 class="card-title">🏆 Monitored Cappers & Leaderboard</h2>
            
            <form onsubmit="addCapper(event)" style="display: flex; gap: 10px; margin-bottom: 15px;">
                <input type="text" id="cap-handle" placeholder="Capper handle (e.g. popularparlay)" required style="flex:1;">
                <select id="cap-platform" style="width: auto; padding: 8px 12px; border-radius: 8px; background: #222; border: 1px solid var(--border-color); color: white;">
                    <option value="x">X (Twitter)</option>
                    <option value="instagram">Instagram</option>
                </select>
                <input type="number" step="0.1" id="cap-weight" placeholder="Weight" value="1.0" required style="width: 100px;">
                <button type="submit" class="btn btn-success" style="white-space: nowrap;">➕ Add</button>
            </form>

            <div style="max-height: 250px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 8px;">
                <table>
                    <thead>
                        <tr>
                            <th>Rank</th>
                            <th>Handle</th>
                            <th>Platform</th>
                            <th>Win Rate</th>
                            <th>ROI</th>
                            <th>Weight</th>
                            <th>Action</th>
                        </tr>
                    </thead>
                    <tbody id="cappers-body">
                        <tr>
                            <td colspan="7" style="text-align: center; color: var(--text-dim);">Loading cappers...</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- AI Parsed Bets & Manual Override Card -->
        <div class="card" style="grid-column: span 2;">
            <h2 class="card-title">🏈 AI Bets Parser & Manual Override Portal</h2>
            <div style="max-height: 350px; overflow-y: auto; border: 1px solid var(--border-color); border-radius: 8px;">
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Capper</th>
                            <th>Original Call Post</th>
                            <th>League</th>
                            <th>Matchup</th>
                            <th>Bet Type</th>
                            <th>Target</th>
                            <th>Line</th>
                            <th>Odds</th>
                            <th>Status</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="picks-body">
                        <tr>
                            <td colspan="11" style="text-align: center; color: var(--text-dim);">Loading parsed picks...</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- Cumulative PnL Curve Chart Card -->
        <div class="card" style="grid-column: span 2;">
            <h2 class="card-title">📈 Cumulative Profit Curve & Performance Chart</h2>
            <div style="height: 320px; width: 100%; position: relative;">
                <canvas id="pnl-chart"></canvas>
            </div>
        </div>

        <!-- Real-Time Logging Streaming Card -->
        <div class="card" style="grid-column: span 2;">
            <h2 class="card-title">
                <span>📟 Real-Time Log Streams</span>
                <div class="tabs" style="padding: 2px;">
                    <button id="log-tab-sol" class="tab-btn active" style="font-size:12px; padding: 4px 10px;" onclick="switchLogTab('sol')">Solana</button>
                    <button id="log-tab-base" class="tab-btn" style="font-size:12px; padding: 4px 10px;" onclick="switchLogTab('base')">Base</button>
                    <button id="log-tab-oanda" class="tab-btn" style="font-size:12px; padding: 4px 10px;" onclick="switchLogTab('oanda')">OANDA Forex</button>
                    <button id="log-tab-tg" class="tab-btn" style="font-size:12px; padding: 4px 10px;" onclick="switchLogTab('tg')">Telegram Gateway</button>
                    <button id="log-tab-social" class="tab-btn" style="font-size:12px; padding: 4px 10px;" onclick="switchLogTab('social')">Social Trail Bot</button>
                </div>
            </h2>
            <div id="log-display" class="log-box">Loading logs...</div>
        </div>
    </div>

    <script>
        let currentConfigTab = 'sol';
        let currentLogTab = 'sol';

        function switchConfigTab(tab) {
            currentConfigTab = tab;
            document.getElementById('tab-sol').classList.toggle('active', tab === 'sol');
            document.getElementById('tab-base').classList.toggle('active', tab === 'base');
            document.getElementById('tab-oanda').classList.toggle('active', tab === 'oanda');
            document.getElementById('tab-social').classList.toggle('active', tab === 'social');
            document.getElementById('form-sol-config').style.display = tab === 'sol' ? 'flex' : 'none';
            document.getElementById('form-base-config').style.display = tab === 'base' ? 'flex' : 'none';
            document.getElementById('form-oanda-config').style.display = tab === 'oanda' ? 'flex' : 'none';
            document.getElementById('form-social-config').style.display = tab === 'social' ? 'flex' : 'none';
        }

        function switchLogTab(tab) {
            currentLogTab = tab;
            document.getElementById('log-tab-sol').classList.toggle('active', tab === 'sol');
            document.getElementById('log-tab-base').classList.toggle('active', tab === 'base');
            document.getElementById('log-tab-oanda').classList.toggle('active', tab === 'oanda');
            document.getElementById('log-tab-tg').classList.toggle('active', tab === 'tg');
            document.getElementById('log-tab-social').classList.toggle('active', tab === 'social');
            fetchLogs();
        }

        async function fetchLogs() {
            try {
                let botParam = currentLogTab;
                if (botParam === 'social') botParam = 'social_trail';
                let r = await fetch(`/api/logs?bot=${botParam}`);
                let d = await r.json();
                document.getElementById('log-display').textContent = d.logs || 'No logs recorded.';
                // Auto scroll to bottom
                let box = document.getElementById('log-display');
                box.scrollTop = box.scrollHeight;
            } catch (e) {
                console.error('Error fetching logs', e);
            }
        }

        function formatUptime(secs) {
            if (!secs) return '--';
            if (secs < 60) return secs + 's';
            let mins = Math.floor(secs / 60);
            if (mins < 60) return mins + 'm ' + (secs % 60) + 's';
            let hrs = Math.floor(mins / 60);
            if (hrs < 24) return hrs + 'h ' + (mins % 60) + 'm';
            let days = Math.floor(hrs / 24);
            return days + 'd ' + (hrs % 24) + 'h';
        }

        async function refreshData() {
            try {
                let r = await fetch('/api/status');
                let d = await r.json();
                
                // Refresh sports betting components
                refreshCappers();
                refreshPicks();

                // Health Diagnostics Update
                let healthGrid = document.getElementById('health-grid');
                if (healthGrid && d.system_health && d.system_health.services) {
                    let healthKeys = ['watchdog', 'web_dashboard', 'tg_bot', 'dex_scanner_bot', 'solana_sniper_bot', 'oanda_bot', 'social_trail_bot', 'tg_parlay_bot'];
                    let gridHTML = '';
                    healthKeys.forEach(k => {
                        let s = d.system_health.services[k];
                        if (s) {
                            let memMB = (s.memory_bytes / 1024 / 1024).toFixed(1);
                            let uptimeStr = formatUptime(s.uptime_seconds);
                            let statusClass = s.running ? 'status-active' : 'status-inactive';
                            // Target 150MB as expected limit for bar coloring
                            let memPercent = Math.min(100, ((s.memory_bytes / 1024 / 1024) / 150) * 100).toFixed(0);
                            let barColor = s.running ? 'linear-gradient(90deg, #6366f1, #a855f7)' : 'rgba(255,255,255,0.05)';
                            gridHTML += `
                                <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border-color); border-radius: 12px; padding: 14px; display: flex; flex-direction: column; gap: 8px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">
                                    <div style="font-size: 14px; font-weight: 600; display: flex; justify-content: space-between; align-items: center;">
                                        <span>${s.name}</span>
                                        <span class="status-badge ${statusClass}" style="padding: 2px 6px; font-size: 10px;">${s.running ? 'ONLINE' : 'OFFLINE'}</span>
                                    </div>
                                    <div style="font-size: 12px; color: var(--text-dim); font-family: 'JetBrains Mono', monospace; display: flex; flex-direction: column; gap: 4px;">
                                        <span>PID: <code>${s.pid || '--'}</code></span>
                                        <span>Uptime: <code>${s.running ? uptimeStr : '--'}</code></span>
                                    </div>
                                    <div style="font-size: 11px; color: var(--text-dim); margin-top: 4px;">
                                        <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                                            <span>Memory Usage</span>
                                            <span style="font-family: 'JetBrains Mono'; font-weight: 600; color: ${s.running ? 'white' : 'var(--text-dim)'};">${s.running ? memMB + ' MB' : '--'}</span>
                                        </div>
                                        <div style="width:100%; height:5px; background:rgba(255,255,255,0.05); border-radius:3px; overflow:hidden;">
                                            <div style="width:${s.running ? memPercent : 0}%; height:100%; background:${barColor}; border-radius:3px; transition: width 0.5s ease;"></div>
                                        </div>
                                    </div>
                                </div>
                            `;
                        }
                    });
                    healthGrid.innerHTML = gridHTML;
                    document.getElementById('health-last-updated').textContent = 'Last Checked: ' + new Date(d.system_health.timestamp * 1000).toLocaleTimeString();
                }

                // Process Statuses
                updateBotStatus('tg', d.status.tg_bot);
                updateBotStatus('sol', d.status.solana);
                updateBotStatus('base', d.status.base);
                updateBotStatus('oanda', d.status.oanda);
                updateBotStatus('twitter', d.status.social_trail);
                updateBotStatus('tg_parlay', d.status.tg_parlay);

                // Configs
                if (d.configs.solana) {
                    document.getElementById('cfg-sol-amount').value = d.configs.solana.trade_amount_sol || 0.05;
                    document.getElementById('cfg-sol-slippage').value = d.configs.solana.slippage_percent || 10.0;
                    document.getElementById('cfg-sol-stop').value = d.configs.solana.hard_stop_loss_percent || 15.0;
                    document.getElementById('cfg-sol-profit').value = d.configs.solana.hard_take_profit_percent || 200;
                    document.getElementById('cfg-sol-pfee').value = d.configs.solana.priority_fee_lamports || 'auto';
                    document.getElementById('cfg-sol-minliq').value = d.configs.solana.min_liquidity_sol || 5.0;
                    document.getElementById('cfg-sol-timeexit').value = d.configs.solana.time_exit_minutes || 0;
                    document.getElementById('cfg-sol-active').value = String(d.configs.solana.scanner_active !== false);
                    document.getElementById('cfg-sol-copywallets').value = (d.configs.solana.copy_wallets || []).join(', ');
                }

                if (d.configs.base) {
                    document.getElementById('cfg-base-amount').value = d.configs.base.trade_amount_eth || 0.004;
                    document.getElementById('cfg-base-gas').value = d.configs.base.gas_premium_percent || 10.0;
                    document.getElementById('cfg-base-stop').value = d.configs.base.hard_stop_loss_percent || 10.0;
                    document.getElementById('cfg-base-profit').value = d.configs.base.hard_take_profit_percent || 60;
                    document.getElementById('cfg-base-minliq').value = d.configs.base.min_liquidity_eth || 0.5;
                    document.getElementById('cfg-base-maxliq').value = d.configs.base.max_liquidity_eth || 15.0;
                    document.getElementById('cfg-base-delay').value = d.configs.base.buy_block_delay || 3;
                    document.getElementById('cfg-base-active').value = String(d.configs.base.scanner_active !== false);
                    document.getElementById('cfg-base-xwebhook').value = d.configs.base.x_webhook_url || '';
                }

                if (d.configs.oanda) {
                    document.getElementById('cfg-oanda-acct').value = d.configs.oanda.account_id || '';
                    document.getElementById('cfg-oanda-env').value = d.configs.oanda.environment || 'live';
                    document.getElementById('cfg-oanda-units').value = d.configs.oanda.trade_units || 100;
                    document.getElementById('cfg-oanda-interval').value = d.configs.oanda.check_interval_seconds || 60;
                    document.getElementById('cfg-oanda-strat').value = d.configs.oanda.strategy || 'ema_crossover';
                    document.getElementById('cfg-oanda-active').value = String(d.configs.oanda.scanner_active !== false);
                    document.getElementById('cfg-oanda-emafast').value = d.configs.oanda.ema_fast || 9;
                    document.getElementById('cfg-oanda-emaslow').value = d.configs.oanda.ema_slow || 21;
                    document.getElementById('cfg-oanda-rsiperiod').value = d.configs.oanda.rsi_period || 14;
                    document.getElementById('cfg-oanda-dryrun').value = String(d.configs.oanda.dry_run === true);
                    document.getElementById('cfg-oanda-instruments').value = (d.configs.oanda.instruments || []).join(', ');
                }

                if (d.configs.social_trail) {
                    document.getElementById('cfg-twitter-mode').value = d.configs.social_trail.twitter_scraping_mode || 'scraping_simulation';
                    document.getElementById('cfg-twitter-active').value = String(d.configs.social_trail.scanner_active !== false);
                    document.getElementById('cfg-twitter-accounts').value = (d.configs.social_trail.monitored_accounts_x || []).join(', ');
                    document.getElementById('cfg-instagram-accounts').value = (d.configs.social_trail.monitored_accounts_instagram || []).join(', ');
                    document.getElementById('cfg-twitter-interval').value = d.configs.social_trail.polling_interval_seconds || 60;
                    document.getElementById('cfg-twitter-buysol').value = String(d.configs.social_trail.auto_buy_solana !== false);
                    document.getElementById('cfg-twitter-solamount').value = d.configs.social_trail.solana_trade_amount || 0.05;
                    document.getElementById('cfg-twitter-buybase').value = String(d.configs.social_trail.auto_buy_base !== false);
                    document.getElementById('cfg-twitter-baseamount').value = d.configs.social_trail.base_trade_amount || 0.002;
                    document.getElementById('cfg-twitter-bearer').value = d.configs.social_trail.bearer_token || '';
                    document.getElementById('cfg-twitter-apifytoken').value = d.configs.social_trail.apify_api_token || '';
                    document.getElementById('cfg-twitter-apifyactorx').value = d.configs.social_trail.apify_twitter_actor || 'apify/twitter-scraper';
                    document.getElementById('cfg-twitter-apifyactorig').value = d.configs.social_trail.apify_instagram_actor || 'apify/instagram-scraper';
                }

                // Stats
                let solTrades = d.trades.solana || [];
                let baseTrades = d.trades.base || [];
                let oandaTrades = d.trades.oanda || [];
                let allTrades = [...solTrades, ...baseTrades, ...oandaTrades];
                
                let total = allTrades.length;
                let wins = allTrades.filter(t => t.pnl_pct >= 0).length;
                let losses = total - wins;
                let winRate = total > 0 ? (wins / total * 100).toFixed(1) : '0.0';

                // Calculate Net SOL/USD gain
                let solGain = 0.0;
                let ethPrice = d.stats.eth_price || 3400.0;
                let solPrice = d.stats.sol_price || 150.0;
                
                solTrades.forEach(t => {
                    let entry = t.buy_price_sol || 0.06;
                    let exit = t.exit_price_sol || 0.0;
                    if (entry > 0) solGain += entry * ((exit / entry) - 1);
                });
                
                let baseUsdGain = 0.0;
                baseTrades.forEach(t => {
                    let entry = t.buy_price_usd || 0.0;
                    let exit = t.exit_price_usd || 0.0;
                    baseUsdGain += (exit - entry); // Assuming absolute usd tracking in Base trade history
                });

                let oandaUsdGain = 0.0;
                oandaTrades.forEach(t => {
                    oandaUsdGain += (t.pnl_usd || 0.0);
                });
                
                let netSolVal = solGain + ((baseUsdGain + oandaUsdGain) / solPrice);
                
                document.getElementById('score-winrate').textContent = `${winRate}%`;
                document.getElementById('score-profit').textContent = `${netSolVal >= 0 ? '+' : ''}${netSolVal.toFixed(4)} SOL (~$${(netSolVal * solPrice).toFixed(2)})`;
                document.getElementById('score-wins').textContent = wins;
                document.getElementById('score-losses').textContent = losses;
                document.getElementById('score-total').textContent = total;
                try {
                    updatePnLChart(allTrades);
                } catch(err) {
                    console.error("Chart update failed:", err);
                }

                // Active Positions
                updatePosition('sol', d.positions.solana);
                updatePosition('base', d.positions.base);
                updatePosition('oanda', d.positions.oanda);

                // Blacklist
                let blBody = document.getElementById('blacklist-body');
                blBody.innerHTML = '';
                let blKeys = Object.keys(d.blacklist || {});
                if (blKeys.length === 0) {
                    blBody.innerHTML = '<tr><td colspan="3" style="text-align: center; color: var(--text-dim);">No blacklisted creators yet.</td></tr>';
                } else {
                    blKeys.forEach(k => {
                        let row = document.createElement('tr');
                        row.innerHTML = `
                            <td style="font-family: monospace;">${k.substring(0, 10)}...${k.substring(k.length - 6)}</td>
                            <td>${d.blacklist[k].reason || 'No reason'}</td>
                            <td><button class="btn btn-outline" style="padding: 4px 8px; font-size:11px;" onclick="removeFromBlacklist('${k}')">Remove</button></td>
                        `;
                        blBody.appendChild(row);
                    });
                }

            } catch (e) {
                console.error('Error refreshing dashboard status', e);
            }
        }

        function updateBotStatus(prefix, info) {
            let badge = document.getElementById(`${prefix}-status-badge`);
            let desc = document.getElementById(`${prefix}-status-desc`);
            let btn = document.getElementById(`${prefix}-btn`);

            if (info && info.running) {
                badge.className = 'status-badge status-active';
                badge.textContent = '● Running';
                desc.textContent = 'Active process in background';
                btn.className = 'btn btn-danger';
                btn.textContent = 'Stop';
            } else {
                badge.className = 'status-badge status-inactive';
                badge.textContent = '● Stopped';
                desc.textContent = 'Inactive process';
                btn.className = 'btn btn-success';
                btn.textContent = 'Start';
            }
        }

        async function fetchOandaPrice(trade) {
            try {
                let r = await fetch(`/api/oanda/price?instrument=${trade.instrument}`);
                let d = await r.json();
                if (d.success && d.price) {
                    let curEl = document.getElementById(`oanda-cur-${trade.id}`);
                    let pnlEl = document.getElementById(`oanda-pnl-${trade.id}`);
                    if (curEl && pnlEl) {
                        let curPrice = d.price;
                        curEl.textContent = curPrice.toFixed(5);
                        
                        let entry = parseFloat(trade.price);
                        let units = parseInt(trade.units);
                        let isBuy = units > 0;
                        
                        let chg_pct = 0.0;
                        if (entry > 0) {
                            chg_pct = isBuy ? ((curPrice - entry) / entry * 100) : ((entry - curPrice) / entry * 100);
                        }
                        
                        let pnl_usd = (curPrice - entry) * units;
                        
                        pnlEl.textContent = `${pnl_usd >= 0 ? '+' : ''}${pnl_usd.toFixed(2)} USD (${chg_pct >= 0 ? '+' : ''}${chg_pct.toFixed(2)}%)`;
                        pnlEl.className = pnl_usd >= 0 ? 'pnl-positive' : 'pnl-negative';
                    }
                }
            } catch (e) {
                console.error("Failed to fetch OANDA price", e);
            }
        }

        function updatePosition(prefix, info) {
            if (prefix === 'oanda') {
                let badge = document.getElementById('pos-oanda-badge');
                let details = document.getElementById('pos-oanda-details');
                let tbody = document.getElementById('pos-oanda-list');
                
                if (info && info.in_position && info.active_trades && info.active_trades.length > 0) {
                    badge.className = 'status-badge status-active';
                    badge.textContent = `${info.active_trades.length} Position(s)`;
                    details.style.display = 'flex';
                    
                    document.getElementById('oanda-acc-bal').textContent = `$${(info.balance || 0.0).toFixed(2)}`;
                    document.getElementById('oanda-acc-nav').textContent = `$${(info.nav || 0.0).toFixed(2)}`;
                    
                    tbody.innerHTML = '';
                    info.active_trades.forEach(t => {
                        let entry = parseFloat(t.price);
                        let units = parseInt(t.units);
                        let row = document.createElement('tr');
                        row.style.borderBottom = '1px solid rgba(255,255,255,0.02)';
                        
                        let isBuy = units > 0;
                        row.innerHTML = `
                            <td style="padding: 6px 0; font-family: monospace; font-weight:600;">${t.instrument}</td>
                            <td style="padding: 6px 0; color: ${isBuy ? '#10b981' : '#ef4444'};">${units > 0 ? '+' : ''}${units}</td>
                            <td style="padding: 6px 0; font-family: monospace;">${entry.toFixed(5)}</td>
                            <td id="oanda-cur-${t.id}" style="padding: 6px 0; font-family: monospace;">--</td>
                            <td id="oanda-pnl-${t.id}" style="padding: 6px 0;" class="pnl-positive">--</td>
                        `;
                        tbody.appendChild(row);
                        
                        // Async fetch current price & PNL
                        fetchOandaPrice(t);
                    });
                } else {
                    badge.className = 'status-badge status-inactive';
                    badge.textContent = 'No Position';
                    details.style.display = 'none';
                    if (info) {
                        document.getElementById('oanda-acc-bal').textContent = `$${(info.balance || 0.0).toFixed(2)}`;
                        document.getElementById('oanda-acc-nav').textContent = `$${(info.nav || 0.0).toFixed(2)}`;
                    }
                }
                return;
            }

            let badge = document.getElementById(`pos-${prefix}-badge`);
            let details = document.getElementById(`pos-${prefix}-details`);
            let chartContainer = document.getElementById(`${prefix}-chart-container`);
            let chartIframe = document.getElementById(`${prefix}-chart-iframe`);

            if (info && info.in_position) {
                badge.className = 'status-badge status-active';
                badge.textContent = 'Active Position';
                details.style.display = 'flex';

                document.getElementById(`pos-${prefix}-symbol`).textContent = info.token_symbol || 'TOKEN';
                document.getElementById(`pos-${prefix}-addr`).textContent = info.token_address || '--';
                
                // Embed live chart dynamically
                if (info.token_address) {
                    let chartUrl = "";
                    if (prefix === 'sol') {
                        chartUrl = `https://dexscreener.com/solana/${info.token_address}?embed=1&theme=dark&trades=0&info=0`;
                    } else {
                        chartUrl = `https://dexscreener.com/base/${info.token_address}?embed=1&theme=dark&trades=0&info=0`;
                    }
                    if (chartIframe.src !== chartUrl) {
                        chartIframe.src = chartUrl;
                    }
                    chartContainer.style.display = 'block';
                } else {
                    chartContainer.style.display = 'none';
                    chartIframe.src = "";
                }
                
                if (prefix === 'sol') {
                    document.getElementById('pos-sol-entry').textContent = `${(info.buy_price_sol || 0).toFixed(6)} SOL`;
                    document.getElementById('pos-sol-holdings').textContent = info.amount_held ? info.amount_held.toFixed(2) : '--';
                    
                    // compute current pnl pct
                    let chg = 0.0;
                    if (info.buy_price_sol > 0) {
                        let cur = info.current_price_sol || info.buy_price_sol;
                        chg = ((cur - info.buy_price_sol) / info.buy_price_sol * 100);
                    }
                    let pnlEl = document.getElementById('pos-sol-pnl');
                    pnlEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`;
                    pnlEl.className = chg >= 0 ? 'pnl-positive' : 'pnl-negative';
                } else {
                    document.getElementById('pos-base-entry').textContent = `$${(info.buy_price_usd || 0).toFixed(6)}`;
                    document.getElementById('pos-base-holdings').textContent = info.amount_held ? info.amount_held.toFixed(2) : '--';
                    
                    let chg = 0.0;
                    if (info.buy_price_usd > 0) {
                        let cur = info.current_price_usd || info.buy_price_usd;
                        chg = ((cur - info.buy_price_usd) / info.buy_price_usd * 100);
                    }
                    let pnlEl = document.getElementById('pos-base-pnl');
                    pnlEl.textContent = `${chg >= 0 ? '+' : ''}${chg.toFixed(2)}%`;
                    pnlEl.className = chg >= 0 ? 'pnl-positive' : 'pnl-negative';
                }
            } else {
                badge.className = 'status-badge status-inactive';
                badge.textContent = 'No Position';
                details.style.display = 'none';
                if (chartContainer) chartContainer.style.display = 'none';
                if (chartIframe) chartIframe.src = "";
            }
        }

        function showToast(message, type = 'info') {
            const container = document.getElementById('toast-container');
            const toast = document.createElement('div');
            toast.className = `toast toast-${type}`;
            
            let icon = 'ℹ️';
            let color = '#6366f1';
            if (type === 'success') {
                icon = '✅';
                color = '#10b981';
            } else if (type === 'error') {
                icon = '🚨';
                color = '#ef4444';
            }
            
            toast.style.borderLeft = `4px solid ${color}`;
            toast.innerHTML = `<span style="font-size:16px;">${icon}</span> <span>${message}</span>`;
            container.appendChild(toast);
            
            // Fade out and remove
            setTimeout(() => {
                toast.classList.add('fade-out');
                setTimeout(() => {
                    toast.remove();
                }, 300);
            }, 3000);
        }

        async function toggleBot(botName) {
            try {
                let r = await fetch(`/api/toggle_bot?bot=${botName}`, { method: 'POST' });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                refreshData();
            } catch (e) {
                showToast('Toggle process failed', 'error');
            }
        }

        async function panicSell(botName, partial) {
            try {
                let r = await fetch(`/api/panic_sell?bot=${botName}&partial=${partial}`, { method: 'POST' });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                refreshData();
            } catch (e) {
                showToast('Panic sell execution request failed', 'error');
            }
        }

        async function saveConfig(event, botName) {
            event.preventDefault();
            let payload = {};
            if (botName === 'solana') {
                payload = {
                    trade_amount_sol: parseFloat(document.getElementById('cfg-sol-amount').value),
                    slippage_percent: parseFloat(document.getElementById('cfg-sol-slippage').value),
                    hard_stop_loss_percent: parseFloat(document.getElementById('cfg-sol-stop').value),
                    hard_take_profit_percent: parseFloat(document.getElementById('cfg-sol-profit').value),
                    priority_fee_lamports: document.getElementById('cfg-sol-pfee').value,
                    min_liquidity_sol: parseFloat(document.getElementById('cfg-sol-minliq').value),
                    time_exit_minutes: parseInt(document.getElementById('cfg-sol-timeexit').value),
                    scanner_active: document.getElementById('cfg-sol-active').value === 'true',
                    copy_wallets: document.getElementById('cfg-sol-copywallets').value.split(',').map(w => w.trim()).filter(w => w.length > 0)
                };
                // Parse integer fee if it is a number
                if (!isNaN(payload.priority_fee_lamports) && payload.priority_fee_lamports.trim() !== '') {
                    payload.priority_fee_lamports = parseInt(payload.priority_fee_lamports);
                }
            } else if (botName === 'base') {
                payload = {
                    trade_amount_eth: parseFloat(document.getElementById('cfg-base-amount').value),
                    gas_premium_percent: parseFloat(document.getElementById('cfg-base-gas').value),
                    hard_stop_loss_percent: parseFloat(document.getElementById('cfg-base-stop').value),
                    hard_take_profit_percent: parseFloat(document.getElementById('cfg-base-profit').value),
                    min_liquidity_eth: parseFloat(document.getElementById('cfg-base-minliq').value),
                    max_liquidity_eth: parseFloat(document.getElementById('cfg-base-maxliq').value),
                    buy_block_delay: parseInt(document.getElementById('cfg-base-delay').value),
                    scanner_active: document.getElementById('cfg-base-active').value === 'true',
                    x_webhook_url: document.getElementById('cfg-base-xwebhook').value.trim()
                };
            } else if (botName === 'oanda') {
                payload = {
                    account_id: document.getElementById('cfg-oanda-acct').value.trim(),
                    environment: document.getElementById('cfg-oanda-env').value,
                    trade_units: parseInt(document.getElementById('cfg-oanda-units').value),
                    check_interval_seconds: parseInt(document.getElementById('cfg-oanda-interval').value),
                    strategy: document.getElementById('cfg-oanda-strat').value,
                    scanner_active: document.getElementById('cfg-oanda-active').value === 'true',
                    ema_fast: parseInt(document.getElementById('cfg-oanda-emafast').value),
                    ema_slow: parseInt(document.getElementById('cfg-oanda-emaslow').value),
                    rsi_period: parseInt(document.getElementById('cfg-oanda-rsiperiod').value),
                    dry_run: document.getElementById('cfg-oanda-dryrun').value === 'true',
                    instruments: document.getElementById('cfg-oanda-instruments').value.split(',').map(i => i.trim().toUpperCase()).filter(i => i.length > 0)
                };
            } else if (botName === 'social_trail') {
                payload = {
                    twitter_scraping_mode: document.getElementById('cfg-twitter-mode').value,
                    scanner_active: document.getElementById('cfg-twitter-active').value === 'true',
                    monitored_accounts_x: document.getElementById('cfg-twitter-accounts').value.split(',').map(a => a.trim()).filter(a => a.length > 0),
                    monitored_accounts_instagram: document.getElementById('cfg-instagram-accounts').value.split(',').map(a => a.trim()).filter(a => a.length > 0),
                    polling_interval_seconds: parseInt(document.getElementById('cfg-twitter-interval').value),
                    auto_buy_solana: document.getElementById('cfg-twitter-buysol').value === 'true',
                    solana_trade_amount: parseFloat(document.getElementById('cfg-twitter-solamount').value),
                    auto_buy_base: document.getElementById('cfg-twitter-buybase').value === 'true',
                    base_trade_amount: parseFloat(document.getElementById('cfg-twitter-baseamount').value),
                    bearer_token: document.getElementById('cfg-twitter-bearer').value.trim(),
                    apify_api_token: document.getElementById('cfg-twitter-apifytoken').value.trim(),
                    apify_twitter_actor: document.getElementById('cfg-twitter-apifyactorx').value.trim(),
                    apify_instagram_actor: document.getElementById('cfg-twitter-apifyactorig').value.trim()
                };
            }

            try {
                let r = await fetch(`/api/update_config?bot=${botName}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                refreshData();
            } catch (e) {
                showToast('Save configuration failed', 'error');
            }
        }

        async function addToBlacklist(event) {
            event.preventDefault();
            let address = document.getElementById('bl-address').value.trim();
            let reason = document.getElementById('bl-reason').value.trim();
            if (!address) return;

            try {
                let r = await fetch('/api/blacklist', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ address, reason })
                });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                document.getElementById('bl-address').value = '';
                document.getElementById('bl-reason').value = '';
                refreshData();
            } catch (e) {
                showToast('Add to blacklist failed', 'error');
            }
        }

        let pnlChart = null;
        function updatePnLChart(allTrades) {
            let sortedTrades = [...allTrades].sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
            let labels = ['Start'];
            let data = [0];
            let cumulative = 0;
            sortedTrades.forEach((t, i) => {
                cumulative += (t.pnl_pct || 0.0);
                labels.push(`#${i+1} (${t.token_symbol || 'TOKEN'})`);
                data.push(parseFloat(cumulative.toFixed(2)));
            });
            const ctx = document.getElementById('pnl-chart').getContext('2d');
            if (pnlChart) {
                pnlChart.data.labels = labels;
                pnlChart.data.datasets[0].data = data;
                pnlChart.update();
            } else {
                pnlChart = new Chart(ctx, {
                    type: 'line',
                    data: {
                        labels: labels,
                        datasets: [{
                            label: 'Cumulative Return %',
                            data: data,
                            borderColor: '#6366f1',
                            backgroundColor: 'rgba(99, 102, 241, 0.1)',
                            borderWidth: 2,
                            fill: true,
                            tension: 0.3,
                            pointBackgroundColor: '#a855f7',
                            pointBorderColor: '#fff',
                            pointHoverRadius: 6
                        }]
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: false,
                        plugins: {
                            legend: { display: false }
                        },
                        scales: {
                            x: {
                                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                                ticks: { color: '#9ca3af', font: { family: 'Outfit', size: 11 } }
                            },
                            y: {
                                grid: { color: 'rgba(255, 255, 255, 0.05)' },
                                ticks: {
                                    color: '#9ca3af',
                                    font: { family: 'Outfit', size: 11 },
                                    callback: function(v) { return v + '%'; }
                                }
                            }
                        }
                    }
                });
            }
        }

        async function removeFromBlacklist(address) {
            try {
                let r = await fetch(`/api/blacklist?address=${address}`, { method: 'DELETE' });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                refreshData();
            } catch (e) {
                showToast('Remove from blacklist failed', 'error');
            }
        }

        let editingPickId = null;
        
        async function refreshCappers() {
            try {
                let r = await fetch('/api/cappers');
                let cappers = await r.json();
                let html = '';
                if (cappers.length === 0) {
                    html = '<tr><td colspan="7" style="text-align:center; color:var(--text-dim);">No cappers registered.</td></tr>';
                } else {
                    cappers.forEach((c, idx) => {
                        let platformIcon = c.platform === 'x' ? '🐦' : '📸';
                        let roiClass = c.roi >= 0 ? 'pnl-green' : 'pnl-red';
                        let roiSign = c.roi >= 0 ? '+' : '';
                        html += `
                            <tr>
                                <td><strong>${idx + 1}</strong></td>
                                <td><span style="font-weight:600; color:#818cf8;">@${c.handle}</span></td>
                                <td>${platformIcon} ${c.platform.toUpperCase()}</td>
                                <td><code>${(c.win_rate * 100).toFixed(1)}%</code></td>
                                <td><span class="${roiClass}" style="font-weight:600;">${roiSign}${c.roi}%</span></td>
                                <td><code>${c.weight}</code></td>
                                <td>
                                    <button class="btn btn-danger" style="padding: 2px 8px; font-size: 11px;" onclick="removeCapper('${c.handle}')">Remove</button>
                                </td>
                            </tr>
                        `;
                    });
                }
                document.getElementById('cappers-body').innerHTML = html;
            } catch (e) {
                console.error("Error refreshing cappers", e);
            }
        }

        async function addCapper(event) {
            event.preventDefault();
            let handle = document.getElementById('cap-handle').value.trim();
            let platform = document.getElementById('cap-platform').value;
            let weight = parseFloat(document.getElementById('cap-weight').value);
            try {
                let r = await fetch('/api/cappers', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ handle, platform, weight })
                });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                document.getElementById('cap-handle').value = '';
                refreshCappers();
            } catch (e) {
                showToast('Failed to add capper', 'error');
            }
        }

        async function removeCapper(handle) {
            if (!confirm(`Are you sure you want to remove capper @${handle}?`)) return;
            try {
                let r = await fetch(`/api/cappers?handle=${handle}`, { method: 'DELETE' });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                refreshCappers();
            } catch (e) {
                showToast('Failed to remove capper', 'error');
            }
        }

        async function refreshPicks() {
            try {
                let r = await fetch('/api/picks');
                let picks = await r.json();
                let html = '';
                if (picks.length === 0) {
                    html = '<tr><td colspan="11" style="text-align:center; color:var(--text-dim);">No AI parsed picks logged.</td></tr>';
                } else {
                    picks.forEach(p => {
                        let timeStr = new Date(p.timestamp * 1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
                        let outcomeClass = p.outcome === 'Win' ? 'pnl-green' : p.outcome === 'Loss' ? 'pnl-red' : 'status-inactive';
                        
                        if (editingPickId === p.pick_id) {
                            html += `
                                <tr>
                                    <td>${timeStr}</td>
                                    <td><span style="font-weight:600;">@${p.capper}</span></td>
                                    <td style="font-size:11px; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${p.original_text}">${p.original_text}</td>
                                    <td><input type="text" id="edit-league" value="${p.league || ''}" style="width:50px; padding:2px;"></td>
                                    <td><input type="text" id="edit-matchup" value="${p.matchup || ''}" style="width:120px; padding:2px;"></td>
                                    <td>
                                        <select id="edit-bettype" style="padding:2px; font-size:11px;">
                                            <option value="Moneyline" ${p.bet_type === 'Moneyline' ? 'selected' : ''}>Moneyline</option>
                                            <option value="Spread" ${p.bet_type === 'Spread' ? 'selected' : ''}>Spread</option>
                                            <option value="Player Prop Over" ${p.bet_type === 'Player Prop Over' ? 'selected' : ''}>Player Prop Over</option>
                                            <option value="Player Prop Under" ${p.bet_type === 'Player Prop Under' ? 'selected' : ''}>Player Prop Under</option>
                                            <option value="Total Over" ${p.bet_type === 'Total Over' ? 'selected' : ''}>Total Over</option>
                                            <option value="Total Under" ${p.bet_type === 'Total Under' ? 'selected' : ''}>Total Under</option>
                                        </select>
                                    </td>
                                    <td><input type="text" id="edit-target" value="${p.target_asset || ''}" style="width:90px; padding:2px;"></td>
                                    <td><input type="number" step="0.5" id="edit-line" value="${p.line_value !== null ? p.line_value : ''}" style="width:50px; padding:2px;"></td>
                                    <td><input type="number" id="edit-odds" value="${p.odds || ''}" style="width:50px; padding:2px;"></td>
                                    <td><span class="status-badge ${outcomeClass}">${p.outcome}</span></td>
                                    <td>
                                        <div style="display:flex; gap:4px;">
                                            <button class="btn btn-success" style="padding:2px 6px; font-size:11px;" onclick="savePickOverride(${p.pick_id})">Save</button>
                                            <button class="btn btn-outline" style="padding:2px 6px; font-size:11px;" onclick="cancelEditPickRow()">Cancel</button>
                                        </div>
                                    </td>
                                </tr>
                            `;
                        } else {
                            let lineValStr = p.line_value !== null ? p.line_value : '--';
                            let oddsValStr = p.odds > 0 ? '+' + p.odds : p.odds;
                            html += `
                                <tr>
                                    <td>${timeStr}</td>
                                    <td><span style="color:#818cf8; font-weight:600;">@${p.capper}</span></td>
                                    <td style="font-size:11px; max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${p.original_text}">${p.original_text}</td>
                                    <td><strong>${p.league}</strong></td>
                                    <td style="font-size:12px;">${p.matchup}</td>
                                    <td style="font-size:12px;"><code>${p.bet_type}</code></td>
                                    <td><span style="font-weight:600;">${p.target_asset}</span></td>
                                    <td><code>${lineValStr}</code></td>
                                    <td><code>${oddsValStr}</code></td>
                                    <td>
                                        <select class="status-badge ${outcomeClass}" style="border:none; padding:2px 4px; font-size:10px; font-weight:600; cursor:pointer;" onchange="updatePickOutcome(${p.pick_id}, this.value)">
                                            <option value="Pending" ${p.outcome === 'Pending' ? 'selected' : ''}>Pending</option>
                                            <option value="Win" ${p.outcome === 'Win' ? 'selected' : ''}>Win 🟢</option>
                                            <option value="Loss" ${p.outcome === 'Loss' ? 'selected' : ''}>Loss 🔴</option>
                                        </select>
                                    </td>
                                    <td>
                                        <button class="btn btn-outline" style="padding: 2px 8px; font-size: 11px;" onclick="editPickRow(${p.pick_id})">✏️ Override</button>
                                    </td>
                                </tr>
                            `;
                        }
                    });
                }
                document.getElementById('picks-body').innerHTML = html;
            } catch (e) {
                console.error("Error refreshing picks", e);
            }
        }

        function editPickRow(pickId) {
            editingPickId = pickId;
            refreshPicks();
        }

        function cancelEditPickRow() {
            editingPickId = null;
            refreshPicks();
        }

        async function savePickOverride(pickId) {
            let league = document.getElementById('edit-league').value.trim();
            let matchup = document.getElementById('edit-matchup').value.trim();
            let bet_type = document.getElementById('edit-bettype').value;
            let target_asset = document.getElementById('edit-target').value.trim();
            let line_raw = document.getElementById('edit-line').value;
            let line_value = line_raw !== '' ? parseFloat(line_raw) : null;
            let odds = parseInt(document.getElementById('edit-odds').value);
            
            try {
                let r = await fetch('/api/picks/override', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ pick_id: pickId, league, matchup, bet_type, target_asset, line_value, odds })
                });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                editingPickId = null;
                refreshPicks();
            } catch (e) {
                showToast('Override request failed', 'error');
            }
        }

        async function updatePickOutcome(pickId, outcome) {
            try {
                let r = await fetch('/api/picks/outcome', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ pick_id: pickId, outcome })
                });
                let res = await r.json();
                showToast(res.message, res.success ? 'success' : 'error');
                refreshPicks();
                refreshCappers();
            } catch (e) {
                showToast('Failed to update pick outcome', 'error');
            }
        }

        // Auto Refresh loops
        refreshData();
        refreshCappers();
        refreshPicks();
        fetchLogs();
        setInterval(refreshData, 5000);
        setInterval(fetchLogs, 3000);
    </script>
</body>
</html>
"""

# API Routes
@app.route("/")
def dashboard():
    try:
        with open("v2_dashboard.html", "r") as f:
            template = f.read()
        return render_template_string(template)
    except Exception as e:
        return f"Error loading V2 UI: {e}"

@app.route("/api/status")
def get_status():
    sol_cfg = read_json(SOL_CONFIG)
    base_cfg = read_json(DEX_CONFIG)
    oanda_cfg = read_json("oanda_config.json")
    sol_state = read_json(SOL_STATE)
    base_state = read_json(DEX_STATE)
    oanda_state = read_json("oanda_state.json")
    sol_history = read_json(SOL_HISTORY, [])
    base_history = read_json(DEX_HISTORY, [])
    oanda_history = read_json("oanda_trade_history.json", [])
    blacklist = read_json(BLACKLIST_FILE)

    # Fetch active position current price
    if sol_state.get("in_position"):
        # Let's call jupiter quote or estimate the price native
        addr = sol_state.get("token_address")
        amt = sol_state.get("amount_held", 0.0)
        sol_state["current_price_sol"] = get_solana_price_estimate(addr, amt)

    if base_state.get("in_position"):
        addr = base_state.get("token_address")
        base_state["current_price_usd"] = get_base_price_estimate(addr)

    # Query eth and sol price
    sol_price = 150.0
    eth_price = 3400.0
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) GhostQuant/2.0'}
    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/tokens/So11111111111111111111111111111111111111112", headers=headers, timeout=3)
        pairs = r.json().get("pairs", [])
        if pairs:
            sol_price = float(max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0)).get("priceUsd", 150.0))
    except:
        pass

    try:
        r = requests.get("https://api.dexscreener.com/latest/dex/tokens/0x4200000000000000000000000000000000000006", headers=headers, timeout=3)
        pairs = r.json().get("pairs", [])
        if pairs:
            eth_price = float(max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0)).get("priceUsd", 3400.0))
    except:
        pass

    system_health = read_json("system_health.json")
    
    # Fetch Kalshi data via lightweight fetcher (no heavy trader init, no WebSocket)
    kalshi_balance = 0.0
    kalshi_equity = 0.0
    kalshi_positions = []
    kalshi_orders = []
    kalshi_settlements = []
    try:
        import subprocess
        kalshi_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kalshi_data_fetch.py")
        out_kalshi = subprocess.check_output(
            ["python3", kalshi_script],
            timeout=15,
            cwd=os.path.dirname(os.path.abspath(__file__))
        ).decode(errors="replace")

        for line in out_kalshi.splitlines():
            line = line.strip()
            if line.startswith("Balance:"):
                try:
                    kalshi_balance = float(line.split("Balance:", 1)[1].strip())
                    system_health["kalshi_balance"] = kalshi_balance
                except Exception:
                    pass

            elif line.startswith("Equity:"):
                try:
                    kalshi_equity = float(line.split("Equity:", 1)[1].strip())
                    system_health["kalshi_equity"] = kalshi_equity
                except Exception:
                    pass

            elif line.startswith("HELD:"):
                try:
                    parts = line.split("|")
                    ticker = parts[0].replace("HELD:", "").strip()
                    pos = float(parts[1].split(":", 1)[1].strip())
                    cost = float(parts[2].split(":", 1)[1].strip())
                    kalshi_positions.append({
                        "ticker": ticker,
                        "position": pos,
                        "cost": cost
                    })
                except Exception:
                    pass

            elif line.startswith("Order:"):
                try:
                    parts = line.split("|")
                    ticker = parts[0].replace("Order:", "").strip()
                    status = parts[1].replace("Status:", "").strip() if len(parts) > 1 else ""
                    action_part = parts[2].strip() if len(parts) > 2 else ""
                    price_part = parts[3].replace("Price:", "").strip() if len(parts) > 3 else ""
                    count_part = parts[4].replace("Count:", "").strip() if len(parts) > 4 else ""
                    kalshi_orders.append({
                        "ticker": ticker,
                        "status": status,
                        "action": action_part,
                        "price": price_part,
                        "count": count_part
                    })
                except Exception:
                    pass

            elif line.startswith("Settlement:"):
                try:
                    parts = line.split("|")
                    ticker = parts[0].replace("Settlement:", "").strip()
                    result = parts[1].replace("Result:", "").strip() if len(parts) > 1 else ""
                    revenue = float(parts[2].replace("Revenue:", "").strip()) if len(parts) > 2 else 0.0
                    cost = float(parts[3].replace("Cost:", "").strip()) if len(parts) > 3 else 0.0
                    pnl = float(parts[4].replace("PnL:", "").strip()) if len(parts) > 4 else 0.0
                    settled_time = parts[5].replace("Time:", "").strip() if len(parts) > 5 else ""
                    kalshi_settlements.append({
                        "ticker": ticker,
                        "result": result,
                        "revenue": revenue,
                        "cost": cost,
                        "pnl": pnl,
                        "settled_time": settled_time
                    })
                except Exception:
                    pass
    except Exception as e:
        print(f"Error fetching Kalshi data: {e}")

    return jsonify({
        "status": {
            "tg_bot": {"running": is_bot_running("tg_bot.py")},
            "solana": {"running": is_bot_running("solana_sniper_bot.py")},
            "base": {"running": is_bot_running("dex_scanner_bot.py")},
            "oanda": {"running": is_bot_running("oanda_bot.py")},
            "social_trail": {"running": is_bot_running("social_trail_bot.py")},
            "tg_parlay": {"running": is_bot_running("tg_parlay_bot.py")}
        },
        "system_health": system_health,
        "configs": {
            "solana": sol_cfg,
            "base": base_cfg,
            "oanda": oanda_cfg,
            "social_trail": read_json("social_trail_config.json")
        },
        "positions": {
            "solana": sol_state,
            "base": base_state,
            "oanda": oanda_state,
            "kalshi": kalshi_positions
        },
        "trades": {
            "solana": sol_history,
            "base": base_history,
            "oanda": oanda_history,
            "kalshi": kalshi_orders
        },
        "kalshi": {
            "balance": kalshi_balance,
            "equity": kalshi_equity,
            "positions": kalshi_positions,
            "orders": kalshi_orders,
            "settlements": kalshi_settlements
        },
        "blacklist": blacklist,
        "stats": {
            "sol_price": sol_price,
            "eth_price": eth_price
        }
    })

def get_solana_price_estimate(token_mint, amount_held):
    if not token_mint or amount_held <= 0:
        return 0.0
    try:
        url = f"https://quote-api.jup.ag/v6/quote?inputMint={token_mint}&outputMint=So11111111111111111111111111111111111111112&amount={int(amount_held * 1e6)}&slippageBps=100"
        r = requests.get(url, timeout=3)
        if r.status_code == 200:
            out = int(r.json().get("outAmount", 0))
            return out / 1e9 / (amount_held * 1e6 / 1e6)
    except:
        pass
    return 0.0

def get_base_price_estimate(token_addr):
    if not token_addr:
        return 0.0
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
        r = requests.get(url, timeout=3)
        pairs = r.json().get("pairs", [])
        if pairs:
            return float(max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0)).get("priceUsd", 0.0))
    except:
        pass
    return 0.0

@app.route("/api/toggle_bot", methods=["POST"])
def toggle_bot_route():
    bot = request.args.get("bot")
    script = ""
    log = ""
    if bot == "tg_bot":
        script = "tg_bot.py"
        log = "tg_bot.log"
    elif bot == "solana":
        script = "solana_sniper_bot.py"
        log = "solana_sniper_bot.log"
    elif bot == "base":
        script = "dex_scanner_bot.py"
        log = "dex_scanner_bot.log"
    elif bot == "oanda":
        script = "oanda_bot.py"
        log = "oanda_log.log"
    elif bot == "social_trail":
        script = "social_trail_bot.py"
        log = "social_trail_bot.log"
    elif bot == "tg_parlay":
        script = "tg_parlay_bot.py"
        log = "tg_parlay_bot.log"
    else:
        return jsonify({"success": False, "message": "Invalid bot name"}), 400

    running = is_bot_running(script)
    if running:
        success, msg = stop_bot(script)
    else:
        success, msg = start_bot(script, log)

    return jsonify({"success": success, "message": f"{bot} action: {msg}"})

@app.route("/api/panic_sell", methods=["POST"])
def panic_sell_route():
    bot = request.args.get("bot")
    partial = request.args.get("partial", "false") == "true"
    
    if bot == "solana":
        s = read_json(SOL_STATE)
        if not s.get("in_position"):
            return jsonify({"success": False, "message": "No active Solana position."}), 400
        trig = {"token_address": s.get("token_address", "")}
        if partial:
            trig["partial"] = True
        write_json("sol_sell_trigger.json", trig)
        return jsonify({"success": True, "message": f"{'50%' if partial else '100%'} Solana panic sell trigger queued."})
    elif bot == "base":
        s = read_json(DEX_STATE)
        if not s.get("in_position"):
            return jsonify({"success": False, "message": "No active Base position."}), 400
        trig = {"token_address": s.get("token_address", "")}
        if partial:
            trig["partial"] = True
        write_json("sell_trigger.json", trig)
        return jsonify({"success": True, "message": f"{'50%' if partial else '100%'} Base panic sell trigger queued."})
    elif bot == "oanda":
        s = read_json("oanda_state.json")
        active = s.get("active_trades", [])
        if not active:
            return jsonify({"success": False, "message": "No active OANDA positions."}), 400
        write_json("oanda_sell_trigger.json", {"trade_id": "all"})
        return jsonify({"success": True, "message": "OANDA panic close trigger for all positions queued."})
    else:
        return jsonify({"success": False, "message": "Invalid bot name"}), 400

@app.route("/api/update_config", methods=["POST"])
def update_config_route():
    bot = request.args.get("bot")
    data = request.json
    if not data:
        return jsonify({"success": False, "message": "No data provided"}), 400

    if bot == "solana":
        cfg = read_json(SOL_CONFIG)
        cfg.update(data)
        write_json(SOL_CONFIG, cfg)
        return jsonify({"success": True, "message": "Solana configuration updated successfully."})
    elif bot == "base":
        cfg = read_json(DEX_CONFIG)
        cfg.update(data)
        write_json(DEX_CONFIG, cfg)
        return jsonify({"success": True, "message": "Base configuration updated successfully."})
    elif bot == "oanda":
        cfg = read_json("oanda_config.json")
        cfg.update(data)
        write_json("oanda_config.json", cfg)
        return jsonify({"success": True, "message": "OANDA configuration updated successfully."})
    elif bot == "social_trail":
        cfg = read_json("social_trail_config.json")
        cfg.update(data)
        write_json("social_trail_config.json", cfg)
        return jsonify({"success": True, "message": "Social (X/IG) configuration updated successfully."})
    else:
        return jsonify({"success": False, "message": "Invalid bot name"}), 400

@app.route("/api/blacklist", methods=["POST", "DELETE"])
def manage_blacklist():
    if request.method == "POST":
        data = request.json
        if not data or not data.get("address"):
            return jsonify({"success": False, "message": "Address is required"}), 400
        addr = data.get("address")
        reason = data.get("reason", "Manual blacklist")
        
        bl = read_json(BLACKLIST_FILE)
        bl[addr] = {
            "reason": reason,
            "timestamp": int(time.time())
        }
        write_json(BLACKLIST_FILE, bl)
        return jsonify({"success": True, "message": f"Added `{addr}` to blacklist."})
        
    elif request.method == "DELETE":
        addr = request.args.get("address")
        if not addr:
            return jsonify({"success": False, "message": "Address parameter required"}), 400
            
        bl = read_json(BLACKLIST_FILE)
        if addr in bl:
            del bl[addr]
            write_json(BLACKLIST_FILE, bl)
            return jsonify({"success": True, "message": f"Removed `{addr}` from blacklist."})
        return jsonify({"success": False, "message": f"Creator `{addr}` not found in blacklist."})

@app.route("/api/cappers", methods=["GET", "POST", "DELETE"])
def api_cappers():
    import betting_db
    if request.method == "GET":
        return jsonify(betting_db.get_cappers())
    elif request.method == "POST":
        data = request.json or {}
        handle = data.get("handle")
        platform = data.get("platform", "x")
        weight = float(data.get("weight", 1.0))
        if not handle:
            return jsonify({"success": False, "message": "Handle is required"}), 400
        success, msg = betting_db.add_capper(handle, platform, weight)
        return jsonify({"success": success, "message": msg})
    elif request.method == "DELETE":
        handle = request.args.get("handle")
        if not handle:
            return jsonify({"success": False, "message": "Handle parameter required"}), 400
        success, msg = betting_db.remove_capper(handle)
        return jsonify({"success": success, "message": msg})

@app.route("/api/picks", methods=["GET"])
def api_picks():
    import betting_db
    return jsonify(betting_db.get_recent_picks())

@app.route("/api/picks/override", methods=["POST"])
def api_picks_override():
    import betting_db
    import betting_aggregator
    data = request.json or {}
    pick_id = data.get("pick_id")
    league = data.get("league")
    matchup = data.get("matchup")
    bet_type = data.get("bet_type")
    target_asset = data.get("target_asset")
    line_value = data.get("line_value")
    odds = data.get("odds")
    
    if not pick_id:
        return jsonify({"success": False, "message": "Pick ID is required"}), 400
        
    success, msg = betting_db.override_parsed_pick(
        pick_id, league, matchup, bet_type, target_asset, line_value, odds
    )
    if success:
        betting_aggregator.build_consensus_parlay()
    return jsonify({"success": success, "message": msg})

@app.route("/api/picks/outcome", methods=["POST"])
def api_picks_outcome():
    import betting_db
    import betting_aggregator
    data = request.json or {}
    pick_id = data.get("pick_id")
    outcome = data.get("outcome")
    
    if not pick_id or not outcome:
        return jsonify({"success": False, "message": "Pick ID and outcome are required"}), 400
        
    try:
        betting_db.update_pick_outcome(pick_id, outcome)
        betting_aggregator.build_consensus_parlay()
        return jsonify({"success": True, "message": f"Pick outcome updated to {outcome}."})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500

@app.route("/api/logs")
def get_logs():
    bot = request.args.get("bot", "sol")
    log_file = ""
    if bot == "sol":
        log_file = "solana_sniper_bot.log"
    elif bot == "base":
        log_file = "dex_scanner_bot.log"
    elif bot == "tg":
        log_file = "tg_bot.log"
    elif bot == "oanda":
        log_file = "oanda_log.log"
    elif bot == "social_trail":
        log_file = "social_trail_bot.log"
    else:
        return jsonify({"success": False, "message": "Invalid log identifier"}), 400

    if os.path.exists(log_file):
        try:
            # Read last 50 lines of log
            with open(log_file, "r") as f:
                lines = f.readlines()
                last_lines = "".join(lines[-50:])
                return jsonify({"success": True, "logs": last_lines})
        except Exception as e:
            return jsonify({"success": False, "logs": f"Error reading log file: {e}"})
    return jsonify({"success": True, "logs": f"No logs recorded yet. Start the {bot} bot to generate logs."})

@app.route("/api/oanda/price")
def get_oanda_price():
    inst = request.args.get("instrument", "EUR_USD")
    cfg = read_json("oanda_config.json")
    api_key = cfg.get("api_key")
    acct_id = cfg.get("account_id")
    env = cfg.get("environment", "live")
    
    if not api_key or not acct_id:
        return jsonify({"success": False, "message": "Credentials missing"}), 400
        
    base_url = "https://api-fxtrade.oanda.com/v3" if env == "live" else "https://api-fxpractice.oanda.com/v3"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    url = f"{base_url}/instruments/{inst}/candles"
    params = {"count": 1, "granularity": "S5"}
    try:
        r = requests.get(url, headers=headers, params=params, timeout=5)
        if r.status_code == 200:
            candles = r.json().get("candles", [])
            if candles:
                price = float(candles[-1]["mid"]["c"])
                return jsonify({"success": True, "price": price})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": False, "message": "Failed to fetch price"}), 500

@app.route("/api/webhook/social", methods=["POST"])
def webhook_social_route():
    """
    Real-time webhook endpoint for external scrapers / Apify to submit posts directly.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        if not data:
            return jsonify({"success": False, "message": "Missing JSON payload"}), 400
        
        posts_to_queue = []
        
        # 1. Check if it's a direct list of posts
        if isinstance(data, list):
            posts_to_queue = data
        elif "platform" in data and "text" in data:
            posts_to_queue = [data]
        elif "event" in data and "data" in data:  # Apify style webhook trigger
            event_data = data.get("data", {})
            dataset_id = event_data.get("defaultDatasetId")
            
            # If we have a dataset ID, fetch items from it
            if dataset_id:
                cfg = read_json("social_trail_config.json")
                token = cfg.get("apify_api_token", "")
                if token:
                    dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={token}"
                    r = requests.get(dataset_url, timeout=15)
                    if r.status_code == 200:
                        items = r.json()
                        for item in items:
                            text = item.get("text") or item.get("full_text") or item.get("caption") or ""
                            tweet_id = item.get("id") or item.get("tweet_id") or item.get("shortCode")
                            platform = "x" if ("tweet_id" in item or "full_text" in item) else "instagram"
                            username = item.get("username") or item.get("user", {}).get("screen_name") or "unknown"
                            if tweet_id and text:
                                posts_to_queue.append({
                                    "platform": platform,
                                    "username": username,
                                    "post_id": f"{platform}_{tweet_id}",
                                    "text": text,
                                    "link": f"https://x.com/{username}/status/{tweet_id}" if platform == "x" else f"https://instagram.com/p/{tweet_id}"
                                })
        else:
            return jsonify({"success": False, "message": "Unsupported format"}), 400
            
        if not posts_to_queue:
            return jsonify({"success": True, "message": "Webhook processed, no posts extracted."})
            
        # Read current queue, append, and save
        queue_file = "social_trail_webhook_queue.json"
        current_queue = []
        if os.path.exists(queue_file):
            try:
                with open(queue_file, "r") as f:
                    current_queue = json.load(f)
            except Exception:
                pass
                
        # Append posts (avoid duplicates by checking post_id)
        existing_ids = {p.get("post_id") for p in current_queue}
        added_count = 0
        for p in posts_to_queue:
            p_id = p.get("post_id")
            if p_id and p_id not in existing_ids:
                if "platform" not in p:
                    p["platform"] = "x"
                if "timestamp" not in p:
                    p["timestamp"] = int(time.time())
                current_queue.append(p)
                added_count += 1
                
        if added_count > 0:
            with open(queue_file, "w") as f:
                json.dump(current_queue, f, indent=2)
                
            # Trigger instant background processing of the posts (bypassing the 20s loop bottleneck)
            try:
                import threading
                import social_trail_bot
                t = threading.Thread(target=social_trail_bot.process_posts, daemon=True)
                t.start()
            except Exception as e:
                print(f"⚠️ Failed to trigger instant post processing: {e}")
                
        return jsonify({"success": True, "message": f"Successfully queued {added_count} new posts."})
        
    except Exception as e:
        return jsonify({"success": False, "message": f"Webhook error: {str(e)}"}), 500

if __name__ == "__main__":
    print("==================================================")
    print("⚡ Meme Coin Sniper Bot Web Dashboard Gateway")
    print("👉 Running locally at: http://127.0.0.1:5001")
    print("==================================================")
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
