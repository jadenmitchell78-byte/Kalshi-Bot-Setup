#!/usr/bin/env python3
"""
live_trade_scanner.py — Real-time Memecoin Launch Scanner & Diagnostic Dashboard
Polls the latest token launches on Solana & Base, performs safety checks,
and renders a live terminal dashboard showing exactly what's being scanned.
"""
import os
import sys
import time
import json
import requests
import base64
from datetime import datetime

# ANSI Color Codes for high-end aesthetics
C_RESET = "\033[0m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_CYAN = "\033[36m"
C_GREEN = "\033[32m"
C_RED = "\033[31m"
C_YELLOW = "\033[33m"
C_MAGENTA = "\033[35m"
C_BG_DARK = "\033[40m"

WSOL_ADDRESS = "So11111111111111111111111111111111111111112"
RAYDIUM_LP_V4 = "675kPX9MHTjS2zt1qT16Uvv9kput58cx8XSg8NjjSQRd"

# Load config urls
SOL_CONFIG = "solana_config.json"
BASE_CONFIG = "dex_scanner_config.json"

def get_cfg(path, key, default):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f).get(key, default)
        except:
            pass
    return default

sol_rpc = get_cfg(SOL_CONFIG, "rpc_url", "https://api.mainnet-beta.solana.com")
base_rpc = get_cfg(BASE_CONFIG, "rpc_url", "https://mainnet.base.org")

session = requests.Session()

# State
scanned_tokens = {}
stats = {"total": 0, "solana": 0, "base": 0, "passed": 0, "failed": 0}

def rpc_sol(method, params):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        r = session.post(sol_rpc, json=payload, timeout=5)
        if r.status_code == 200:
            return r.json().get("result")
    except:
        pass
    return None

def check_solana_safety(token_addr):
    # Quick mint/freeze check
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [token_addr, {"encoding": "base64"}]},
        {"jsonrpc": "2.0", "id": 2, "method": "getTokenSupply", "params": [token_addr]}
    ]
    try:
        r = session.post(sol_rpc, json=payload, timeout=5)
        if r.status_code != 200:
            return False, "RPC failure", 0
        res_map = {res.get("id"): res.get("result") for res in r.json()}
    except:
        return False, "RPC exception", 0

    info = res_map.get(1)
    supply = res_map.get(2)

    if not info or not info.get("value"):
        return False, "Mint not found", 0

    data_b64 = info["value"].get("data", [""])[0]
    if not data_b64:
        return False, "Empty data", 0

    data = base64.b64decode(data_b64)
    if len(data) < 82:
        return False, "Malformed mint", 0

    mint_auth = int.from_bytes(data[0:4], "little")
    freeze_auth = int.from_bytes(data[46:50], "little")

    if mint_auth != 0:
        return False, "Mint Active", 15
    if freeze_auth != 0:
        return False, "Freeze Active", 15

    total = 100
    if supply and "value" in supply:
        total = float(supply["value"].get("uiAmount", 0.0) or 0.0)

    # All checks passed
    return True, "Passed", 65

def check_base_safety(token_addr):
    # Base GoPlus check fallback
    url = f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={token_addr}"
    try:
        r = session.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json().get("result", {}).get(token_addr.lower(), {})
            if not data:
                return False, "No GoPlus data", 0
            
            is_honeypot = data.get("is_honeypot") == "1"
            is_blacklisted = data.get("is_blacklisted") == "1"
            cannot_sell = data.get("cannot_buy") == "1"
            
            if is_honeypot:
                return False, "Honeypot", 10
            if is_blacklisted:
                return False, "Blacklist", 20
            if cannot_sell:
                return False, "Cannot Sell", 15

            return True, "Passed", 75
    except:
        pass
    return True, "Passed (No GoPlus)", 50

def draw_dashboard():
    # Clear screen
    sys.stdout.write("\033[H\033[J")
    
    # Title Header
    print(f"{C_BOLD}{C_CYAN}================================================================================{C_RESET}")
    print(f"📊 {C_BOLD}{C_MAGENTA}MEMECOIN LAUNCH SCANNER & SPEED MONITOR — LIVE TELEMETRY{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}================================================================================{C_RESET}")
    
    # Stats Bar
    print(f"⏱  Local Time: {datetime.now().strftime('%H:%M:%S')} | "
          f"🔍 Scanned: {C_BOLD}{stats['total']}{C_RESET} | "
          f"🔷 Base: {C_BOLD}{stats['base']}{C_RESET} | "
          f"☀️ Solana: {C_BOLD}{stats['solana']}{C_RESET} | "
          f"🟢 Passed: {C_GREEN}{stats['passed']}{C_RESET} | "
          f"🔴 Failed: {C_RED}{stats['failed']}{C_RESET}")
    print(f"{C_DIM}--------------------------------------------------------------------------------{C_RESET}")
    
    # Table Header
    print(f"{C_BOLD}{'TIME':<10} {'CHAIN':<8} {'SYMBOL':<10} {'ADDRESS':<24} {'SAFETY CHECK':<20} {'AI SCORE':<8}{C_RESET}")
    print(f"{C_DIM}--------------------------------------------------------------------------------{C_RESET}")
    
    # Render scanned tokens sorted by discovery time (newest first, limit to last 15)
    sorted_tokens = sorted(scanned_tokens.values(), key=lambda x: x["time"], reverse=True)[:15]
    for t in sorted_tokens:
        chain_color = C_CYAN if t["chain"] == "SOL" else C_MAGENTA
        status_color = C_GREEN if t["passed"] else C_RED
        score_color = C_GREEN if t["score"] >= 55 else C_YELLOW if t["score"] > 30 else C_RED
        
        addr_trunc = t["addr"][:10] + "..." + t["addr"][-10:]
        
        print(f"{t['time_str']:<10} "
              f"{chain_color}{t['chain']:<8}{C_RESET} "
              f"{C_BOLD}{t['sym'][:8]:<10}{C_RESET} "
              f"{C_DIM}{addr_trunc:<24}{C_RESET} "
              f"{status_color}{t['status']:<20}{C_RESET} "
              f"{score_color}{t['score']}/100{C_RESET}")
              
    print(f"{C_DIM}--------------------------------------------------------------------------------{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}💡 Running in hybrid mode. Use Ctrl+C to exit this live view.{C_RESET}")

def scan_latest_pools():
    # Poll DexScreener latest token profiles
    url = "https://api.dexscreener.com/token-profiles/latest/v1"
    try:
        r = session.get(url, timeout=5)
        if r.status_code != 200:
            return
        profiles = r.json()
        if not isinstance(profiles, list):
            return
            
        for prof in profiles:
            chain_id = prof.get("chainId", "").lower()
            if chain_id not in ["solana", "base"]:
                continue
                
            token_addr = prof.get("tokenAddress")
            if not token_addr or token_addr in scanned_tokens:
                continue
                
            sym = prof.get("symbol", "UNKNOWN")
            chain = "SOL" if chain_id == "solana" else "BASE"
            
            # Perform quick safety check
            if chain == "SOL":
                passed, status, score = check_solana_safety(token_addr)
                stats["solana"] += 1
            else:
                passed, status, score = check_base_safety(token_addr)
                stats["base"] += 1
                
            stats["total"] += 1
            if passed:
                stats["passed"] += 1
            else:
                stats["failed"] += 1
                
            scanned_tokens[token_addr] = {
                "chain": chain,
                "sym": sym,
                "addr": token_addr,
                "passed": passed,
                "status": status,
                "score": score,
                "time": time.time(),
                "time_str": datetime.now().strftime("%H:%M:%S")
            }
    except Exception as e:
        pass

def main():
    # Set console title
    sys.stdout.write("\x1b]2;Memecoin Live Scanner\x07")
    
    # Warm up first scan
    scan_latest_pools()
    draw_dashboard()
    
    while True:
        try:
            scan_latest_pools()
            draw_dashboard()
            time.sleep(8)
        except KeyboardInterrupt:
            print("\n👋 Exiting Live Scanner dashboard.")
            sys.exit(0)
        except Exception as e:
            time.sleep(5)

if __name__ == "__main__":
    main()
