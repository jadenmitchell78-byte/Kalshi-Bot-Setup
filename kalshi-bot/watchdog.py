"""
watchdog.py — Bot Watchdog & Self-Healing Service
Monitors bots, web dashboard, log sizes, and wallet balances.

Usage:
    nohup python3 -u watchdog.py > watchdog.log 2>&1 &
"""
import os
import sys
import time
import json
import subprocess
import signal
import requests

# ── Credentials & Web3 ────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

if os.path.exists("config.json"):
    try:
        with open("config.json", "r") as f:
            c = json.load(f)
            if not BOT_TOKEN and c.get("telegram_token"):
                BOT_TOKEN = c["telegram_token"]
            if not CHAT_ID and c.get("telegram_chat_id"):
                CHAT_ID = str(c["telegram_chat_id"])
    except Exception as e:
        print(f"Error reading config.json in watchdog: {e}")

API       = f"https://api.telegram.org/bot{BOT_TOKEN}"

DEX_CONFIG  = "dex_scanner_config.json"
SOL_CONFIG  = "solana_config.json"

# State trackers to avoid alert spamming
last_balance_alert_base = 0
last_balance_alert_sol = 0

# ── Helpers ───────────────────────────────────────────────────────────────────

def _rj(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return default if default is not None else {}

BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def _b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    res = []
    while n > 0:
        n, r = divmod(n, 58)
        res.append(BASE58_ALPHABET[r])
    pad = 0
    for c in b:
        if c == 0:
            pad += 1
        else:
            break
    return "1" * pad + "".join(reversed(res))

def _sol_wallet_address():
    try:
        kd = _rj("solana_key.json")
        if isinstance(kd, dict):
            return kd.get("pubkey") or kd.get("public_key") or kd.get("wallet_address", "")
        elif isinstance(kd, list):
            from Crypto.PublicKey import ECC
            secret_bytes = bytes(kd)
            seed = secret_bytes[:32]
            wallet_key = ECC.generate(curve="ed25519", randfunc=lambda n: seed[:n])
            return _b58encode(wallet_key.public_key().export_key(format="raw"))
    except Exception as e:
        print(f"[DERIVE ERR] Solana address: {e}")
    return ""


def send_telegram(msg: str):
    payload = {"chat_id": CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(f"{API}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        print(f"[WATCHDOG TG ERR] {e}")

def get_bot_pid(script_name):
    try:
        out = subprocess.check_output(["pgrep", "-f", script_name]).decode()
        pids = [line.strip() for line in out.splitlines() if line.strip()]
        my_pid = os.getpid()
        pids = [int(p) for p in pids if int(p) != my_pid]
        return pids[0] if pids else None
    except:
        return None

def is_bot_running(script_name):
    return get_bot_pid(script_name) is not None

def get_proc_stats(pid):
    if not pid:
        return {"memory_bytes": 0, "uptime_seconds": 0}
    try:
        rss = 0
        if os.path.exists(f"/proc/{pid}/status"):
            for line in open(f"/proc/{pid}/status"):
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1]) * 1024
                    break
        start_time = os.path.getmtime(f"/proc/{pid}")
        uptime = int(time.time() - start_time)
        return {"memory_bytes": rss, "uptime_seconds": uptime}
    except:
        return {"memory_bytes": 0, "uptime_seconds": 0}

def write_system_health():
    services = {
        "watchdog": ("watchdog.py", "Watchdog Monitor"),
        "web_dashboard": ("web_dashboard.py", "Web Dashboard"),
        "tg_bot": ("tg_bot.py", "Telegram Bot Gateway"),
        "dex_scanner_bot": ("dex_scanner_bot.py", "Base Scanner Bot"),
        "solana_sniper_bot": ("solana_sniper_bot.py", "Solana Sniper Bot"),
        "oanda_bot": ("oanda_bot.py", "OANDA Forex Bot"),
        "social_trail_bot": ("social_trail_bot.py", "Social Trail Bot"),
        "tg_parlay_bot": ("tg_parlay_bot.py", "Telegram Parlay Bot")
    }
    
    health_data = {
        "timestamp": int(time.time()),
        "services": {}
    }
    
    # Self-stats
    my_pid = os.getpid()
    self_stats = get_proc_stats(my_pid)
    health_data["services"]["watchdog"] = {
        "name": "Watchdog Monitor",
        "pid": my_pid,
        "running": True,
        "memory_bytes": self_stats["memory_bytes"],
        "uptime_seconds": self_stats["uptime_seconds"]
    }
    
    for key, (script, name) in services.items():
        if key == "watchdog":
            continue
        pid = get_bot_pid(script)
        stats = get_proc_stats(pid)
        health_data["services"][key] = {
            "name": name,
            "pid": pid or 0,
            "running": pid is not None,
            "memory_bytes": stats["memory_bytes"],
            "uptime_seconds": stats["uptime_seconds"]
        }
        
    try:
        with open("system_health.json", "w") as f:
            json.dump(health_data, f, indent=2)
    except Exception as e:
        print(f"Error writing system health: {e}")

def start_bot(script_name, log_name):
    try:
        cmd = f"nohup python3 -u {script_name} > {log_name} 2>&1 &"
        subprocess.Popen(cmd, shell=True)
        return True
    except Exception as e:
        print(f"Error starting {script_name}: {e}")
        return False

# ── Self-Healing Checks ──────────────────────────────────────────────────────

def perform_process_checks():
    # 1. Web Dashboard (Critical UI Gateway)
    if not is_bot_running("web_dashboard.py"):
        print("web_dashboard.py is offline. Restarting...")
        if start_bot("web_dashboard.py", "web_dashboard.log"):
            send_telegram("⚠️ *Watchdog Recovery Alert*\n`web_dashboard.py` was detected *OFFLINE*. Successfully restarted process.")

    # 2. Telegram Bot (Critical Notification Gateway)
    if not is_bot_running("tg_bot.py"):
        print("tg_bot.py is offline. Restarting...")
        if start_bot("tg_bot.py", "tg_bot.log"):
            send_telegram("⚠️ *Watchdog Recovery Alert*\n`tg_bot.py` (Telegram Gateway) was detected *OFFLINE*. Successfully restarted process.")

    # 3. Base Scanner Bot (Only if configured as active)
    base_cfg = _rj(DEX_CONFIG)
    if base_cfg.get("scanner_active", True):
        if not is_bot_running("dex_scanner_bot.py"):
            print("dex_scanner_bot.py is offline but marked active. Restarting...")
            if start_bot("dex_scanner_bot.py", "dex_scanner_bot.log"):
                send_telegram("⚠️ *Watchdog Recovery Alert*\n🔷 `dex_scanner_bot.py` (Base Bot) was active in config but *OFFLINE*. Successfully restarted process.")

    # 4. Solana Sniper Bot (Only if configured as active)
    sol_cfg = _rj(SOL_CONFIG)
    if sol_cfg.get("scanner_active", True):
        if not is_bot_running("solana_sniper_bot.py"):
            print("solana_sniper_bot.py is offline but marked active. Restarting...")
            if start_bot("solana_sniper_bot.py", "solana_sniper_bot.log"):
                send_telegram("⚠️ *Watchdog Recovery Alert*\n🟣 `solana_sniper_bot.py` (Solana Bot) was active in config but *OFFLINE*. Successfully restarted process.")

    # 5. OANDA Forex Bot (Only if configured as active)
    oanda_cfg = _rj("oanda_config.json")
    if oanda_cfg.get("scanner_active", True):
        if not is_bot_running("oanda_bot.py"):
            print("oanda_bot.py is offline but marked active. Restarting...")
            if start_bot("oanda_bot.py", "oanda_log.log"):
                send_telegram("⚠️ *Watchdog Recovery Alert*\n🟢 `oanda_bot.py` (OANDA Forex Bot) was active in config but *OFFLINE*. Successfully restarted process.")

    # 6. Social Trail Bot (Only if configured as active)
    tt_cfg = _rj("social_trail_config.json")
    if tt_cfg.get("scanner_active", True):
        if not is_bot_running("social_trail_bot.py"):
            print("social_trail_bot.py is offline but marked active. Restarting...")
            if start_bot("social_trail_bot.py", "social_trail_bot.log"):
                send_telegram("⚠️ *Watchdog Recovery Alert*\n🐦 `social_trail_bot.py` (Social Trail Bot) was active in config but *OFFLINE*. Successfully restarted process.")

    # 7. Telegram Parlay Bot Disabled
    # if tt_cfg.get("telegram_token"):
    #     if not is_bot_running("tg_parlay_bot.py"):
    #         print("tg_parlay_bot.py is offline. Restarting...")
    #         if start_bot("tg_parlay_bot.py", "tg_parlay_bot.log"):
    #             send_telegram("⚠️ *Watchdog Recovery Alert*\n🤖 `tg_parlay_bot.py` (Telegram Parlay Bot) was detected *OFFLINE*. Successfully restarted process.")

# ── Balance Checks ────────────────────────────────────────────────────────────

def perform_balance_checks():
    global last_balance_alert_base, last_balance_alert_sol
    now = time.time()
    
    # 1. Base Balance Check (Alert every 12 hours max)
    if now - last_balance_alert_base > 43200:
        try:
            pk = ""
            for ln in open(".env"):
                if "METAMASK_PRIVATE_KEY" in ln and "=" in ln:
                    pk = ln.split("=", 1)[1].strip()
            if pk:
                from web3 import Web3
                rpc = _rj(DEX_CONFIG).get("rpc_url", "https://mainnet.base.org")
                w3  = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 10}))
                acct = w3.eth.account.from_key(pk)
                bal  = float(w3.from_wei(w3.eth.get_balance(acct.address), "ether"))
                if bal < 0.003: # Alert threshold
                    send_telegram(
                        f"⚠️ *Base Low Gas Alert*\n"
                        f"Wallet: `{acct.address}`\n"
                        f"Current Balance: `{bal:.6f} ETH`\n"
                        f"Please top up to ensure trades execute successfully!"
                    )
                    last_balance_alert_base = now
        except Exception as e:
            print(f"Base balance check error: {e}")

    # 2. Solana Balance Check (Alert every 12 hours max)
    if now - last_balance_alert_sol > 43200:
        try:
            primary_rpc = _rj(SOL_CONFIG).get("rpc_url", "https://api.mainnet-beta.solana.com")
            rpc_pool = _rj(SOL_CONFIG).get("rpc_pool", [])
            endpoints = [primary_rpc]
            for u in rpc_pool:
                if u not in endpoints:
                    endpoints.append(u)
            wallet = _sol_wallet_address()
            if wallet:
                success = False
                sol = 0.0
                for rpc_url in endpoints:
                    try:
                        r = requests.post(
                            rpc_url,
                            json={"jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [wallet]},
                            timeout=6
                        )
                        if r.status_code == 200:
                            resp = r.json()
                            if "result" in resp and resp["result"] is not None:
                                lam = resp.get("result", {}).get("value", 0)
                                sol = lam / 1e9
                                success = True
                                break
                    except Exception as rpc_err:
                        print(f"⚠️ [WATCHDOG] Balance check fallback error on {rpc_url}: {rpc_err}")
                        continue
                
                if success:
                    if sol < 0.03: # Alert threshold
                        send_telegram(
                            f"⚠️ *Solana Low Gas Alert*\n"
                            f"Wallet: `{wallet}`\n"
                            f"Current Balance: `{sol:.6f} SOL`\n"
                            f"Please top up to ensure trades execute successfully!"
                        )
                        last_balance_alert_sol = now
                else:
                    print("⚠️ [WATCHDOG] Failed to retrieve SOL balance from all RPC endpoints during balance check.")
        except Exception as e:
            print(f"Solana balance check error: {e}")

# ── Log Rotator ───────────────────────────────────────────────────────────────

def rotate_logs():
    log_files = [
        "dex_scanner_bot.log",
        "solana_sniper_bot.log",
        "tg_bot.log",
        "tg_parlay_bot.log",
        "web_dashboard.log",
        "watchdog.log"
    ]
    MAX_SIZE = 10 * 1024 * 1024 # 10 MB
    for log_path in log_files:
        if os.path.exists(log_path):
            try:
                if os.path.getsize(log_path) > MAX_SIZE:
                    print(f"Rotating log: {log_path}")
                    with open(log_path, "r", errors="ignore") as f:
                        lines = f.readlines()
                    keep = lines[-1000:] # Keep only the last 1000 lines
                    with open(log_path, "w") as f:
                        f.writelines(keep)
            except Exception as e:
                print(f"Error rotating log {log_path}: {e}")

# ── Main Loop ─────────────────────────────────────────────────────────────────

def main():
    print("Watchdog process started.")
    send_telegram("🛡️ *Bot Watchdog Service Online*\nMonitoring processes, log file sizes, and wallet balances...")
    
    last_balance_check = 0
    last_log_rotation = 0
    
    while True:
        try:
            # 1. Process checks every 15 seconds
            perform_process_checks()
            write_system_health()
            
            # 2. Balance checks every 15 minutes
            now = time.time()
            if now - last_balance_check > 900:
                perform_balance_checks()
                last_balance_check = now
                
            # 3. Log rotation every 2 hours
            if now - last_log_rotation > 7200:
                rotate_logs()
                last_log_rotation = now
                
        except Exception as e:
            print(f"Error in watchdog loop: {e}")
            
        time.sleep(15)

if __name__ == "__main__":
    main()
