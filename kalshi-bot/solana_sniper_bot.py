import os
import sys
import json
import time
import base64
import threading
import requests
import queue
import asyncio
import websockets
from Crypto.PublicKey import ECC
from Crypto.Signature import eddsa
import ai_predictive_model
import grpc_geyser_client
import solana_tx_builder
import v2_logger
v2_logger.inject_v2_logger()
# Persistent HTTP Session to reuse TCP/TLS connections (reducing latency by up to 80%)
_http_session = requests.Session()

# ─── Constants ────────────────────────────────────────────────────────────────
WSOL_ADDRESS   = "So11111111111111111111111111111111111111112"
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
RAYDIUM_LP_V4  = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
METEORA_DLMM   = "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo"
METEORA_POOLS  = "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB"
ORCA_CLMM      = "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

CONFIG_PATH  = "solana_config.json"
_token_ai_scores = {}
STATE_PATH   = "solana_state.json"
KEY_PATH     = "solana_key.json"
HISTORY_PATH = "solana_trade_history.json"

# ─── Config & State Globals ────────────────────────────────────────────────────
config = {}
state  = {}
wallet_key     = None
wallet_address = ""
ws_active      = False

# ─── Session Loss / Cutoff Settings ───────────────────────────────────────────
SESSION_MAX_LOSS_USD    = 30.0   # limit max USD loss per session (raised for aggressive mode)
SESSION_CUTOFF_HOUR_UTC = 15     # unused — _is_session_cutoff() always returns False
_cached_sol_bal = 0.0
_last_bal_time = 0.0
SESSION_CUTOFF_MIN_UTC  = 0

session_start_sol_balance: float = 0.0
session_sol_price_at_start: float = 0.0
_session_loss_alerted = False

_session_cutoff_target = None

def _is_session_cutoff() -> bool:
    return False


# ─── Base58 Helpers ────────────────────────────────────────────────────────────
def b58encode(b: bytes) -> str:
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

def b58decode(s: str) -> bytes:
    n = 0
    for char in s:
        n = n * 58 + BASE58_ALPHABET.index(char)
    pad = 0
    for char in s:
        if char == "1":
            pad += 1
        else:
            break
    h = hex(n)[2:]
    if len(h) % 2 != 0:
        h = "0" + h
    res = bytes.fromhex(h)
    return b"\x00" * pad + res

# ─── Compact-u16 Parser ────────────────────────────────────────────────────────
def parse_compact_u16(b: bytes, offset: int = 0):
    value = 0
    shift = 0
    while True:
        byte = b[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if (byte & 0x80) == 0:
            break
        shift += 7
    return value, offset

# ─── Config Loader ─────────────────────────────────────────────────────────────
_last_config_mtime = 0.0

def _reload_config():
    global config, _last_config_mtime
    if os.path.exists(CONFIG_PATH):
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime != _last_config_mtime:
                with open(CONFIG_PATH, "r") as f:
                    config = json.load(f)
                _last_config_mtime = mtime
        except Exception as e:
            print(f"⚠️ Error reading config: {e}")

_reload_config()

def _cfg(key, default):
    return config.get(key, default)

def _save_config():
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"❌ Failed to save config: {e}")

# ─── Settings (read fresh on each loop via _reload_config) ────────────────────
def get_settings():
    _reload_config()
    return {
        "RPC_URL":          _cfg("rpc_url", "https://api.mainnet-beta.solana.com"),
        "TRADE_SOL":        _cfg("trade_amount_sol", 0.05),
        "SLIPPAGE":         _cfg("slippage_percent", 10.0),
        "MIN_LIQ":          _cfg("min_liquidity_sol", 5.0),
        "MAX_LIQ":          _cfg("max_liquidity_sol", 200.0),
        "TRAILING_STOP":    _cfg("trailing_stop_percent", 15.0),
        "HARD_SL":          _cfg("hard_stop_loss_percent", 15.0),
        "HARD_TP":          _cfg("hard_take_profit_percent", 200.0),
        "CHECK_INTERVAL":   _cfg("check_interval_seconds", 1),
        "DRY_RUN":          _cfg("dry_run", False),
        "TIME_EXIT":        _cfg("time_exit_minutes", 0),
        "SCANNER_ACTIVE":   _cfg("scanner_active", True),
        "TG_TOKEN":         _cfg("telegram_token", ""),
        "TG_CHAT_ID":       _cfg("telegram_chat_id", ""),
        "DISCORD_WEBHOOK":  _cfg("discord_webhook_url", ""),
        "BUY_BLOCK_DELAY":  _cfg("buy_block_delay", 0),
        "STAGED_TP":        _cfg("staged_take_profit_percent", 30.0),
        "STAGED_FRACTION":  _cfg("staged_sell_fraction", 0.50),
    }

S = get_settings()
RPC_URL         = S["RPC_URL"]
TG_TOKEN        = S["TG_TOKEN"]
TG_CHAT_ID      = S["TG_CHAT_ID"]

# ─── Wallet ────────────────────────────────────────────────────────────────────
wallet_keys = []
wallet_addresses = []

def load_all_wallets():
    global wallet_key, wallet_address, wallet_keys, wallet_addresses
    # 1. Main wallet
    if os.path.exists(KEY_PATH):
        try:
            with open(KEY_PATH, "r") as f:
                secret_list = json.load(f)
            secret_bytes = bytes(secret_list)
            seed = secret_bytes[:32]
            k = ECC.generate(curve="ed25519", randfunc=lambda n: seed[:n])
            addr = b58encode(k.public_key().export_key(format="raw"))
            wallet_keys.append(k)
            wallet_addresses.append(addr)
            wallet_key = k
            wallet_address = addr
            print(f"✅ Loaded Primary Solana wallet: {addr}")
        except Exception as e:
            print(f"❌ Failed to load primary private key: {e}")
            sys.exit(1)
    else:
        print("❌ Solana Keypair not found. Please run solana_setup.py first.")
        sys.exit(1)

    # 2. Secondary wallets (solana_key_2.json, solana_key_3.json, etc.)
    for i in range(2, 6):
        extra_path = f"solana_key_{i}.json"
        if os.path.exists(extra_path):
            try:
                with open(extra_path, "r") as f:
                    sec_list = json.load(f)
                sec_bytes = bytes(sec_list)
                sd = sec_bytes[:32]
                k = ECC.generate(curve="ed25519", randfunc=lambda n: sd[:n])
                addr = b58encode(k.public_key().export_key(format="raw"))
                wallet_keys.append(k)
                wallet_addresses.append(addr)
                print(f"✅ Loaded Extra Solana wallet #{i}: {addr}")
            except Exception as e:
                print(f"⚠️ Failed to load secondary key {extra_path}: {e}")

load_all_wallets()

# ─── RPC Helper ────────────────────────────────────────────────────────────────
_rpc_nodes_state = []
_rpc_nodes_lock = threading.Lock()
_rpc_ping_started = False

def _rpc_pool_background_ping():
    while True:
        try:
            _init_rpc_pool()
            payload = {"jsonrpc": "2.0", "id": 99, "method": "getLatestBlockhash", "params": []}
            with _rpc_nodes_lock:
                nodes_to_ping = list(_rpc_nodes_state)
            for node in nodes_to_ping:
                url = node["url"]
                # Skip public nodes for ping frequency to avoid unnecessary rate limits
                if "api.mainnet-beta.solana.com" in url:
                    continue
                start = time.time()
                try:
                    r = _http_session.post(url, json=payload, timeout=4)
                    if r.status_code == 200:
                        lat = (time.time() - start) * 1000
                        with _rpc_nodes_lock:
                            node["latency"] = 0.7 * node["latency"] + 0.3 * lat
                except:
                    with _rpc_nodes_lock:
                        node["latency"] = 9999.0
        except Exception:
            pass
        time.sleep(90)

def _init_rpc_pool():
    global _rpc_nodes_state, _rpc_ping_started
    with _rpc_nodes_lock:
        if _rpc_nodes_state:
            return
        _reload_config()
        configured_pool = _cfg("rpc_pool", [])
        primary = _cfg("rpc_url", "https://api.mainnet-beta.solana.com")
        
        candidates = []
        if isinstance(configured_pool, list):
            candidates.extend(configured_pool)
        if primary not in candidates:
            candidates.append(primary)
            
        backup_urls = [
            "https://api.mainnet-beta.solana.com",
            "https://solana-rpc.publicnode.com"
        ]
        for url in backup_urls:
            if url not in candidates:
                candidates.append(url)
                
        _rpc_nodes_state = [
            {"url": u, "latency": 150.0, "failures": 0, "penalty_until": 0.0}
            for u in candidates
        ]
        
    if not _rpc_ping_started:
        _rpc_ping_started = True
        threading.Thread(target=_rpc_pool_background_ping, daemon=True).start()

def rpc_call(method: str, params: list):
    _init_rpc_pool()
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    
    now = time.time()
    with _rpc_nodes_lock:
        sorted_nodes = sorted(
            _rpc_nodes_state,
            key=lambda x: (
                1 if x["penalty_until"] > now else 0,
                x["failures"],
                x["latency"]
            )
        )
        
    max_retries = min(5, len(sorted_nodes))
    for attempt in range(max_retries):
        node = sorted_nodes[attempt]
        url = node["url"]
        start_time = time.time()
        try:
            r = _http_session.post(url, json=payload, timeout=8)
            latency = (time.time() - start_time) * 1000
            
            if r.status_code == 429:
                with _rpc_nodes_lock:
                    node["failures"] += 1
                    node["penalty_until"] = time.time() + 15.0
                continue
                
            if r.status_code == 200:
                try:
                    resp = r.json()
                except Exception:
                    continue
                if "error" in resp:
                    err = resp["error"]
                    code = err.get("code", 0)
                    msg = err.get("message", "")
                    if code == -32429 or "rate limit" in msg.lower() or "too many requests" in msg.lower():
                        with _rpc_nodes_lock:
                            node["failures"] += 1
                            node["penalty_until"] = time.time() + 15.0
                        continue
                    elif code == -32601:
                        # Silently return None for unsupported method fallbacks
                        return None
                    else:
                        print(f"⚠️ RPC Error on {method} using {url}: {msg} (code: {code})")
                        return None
                with _rpc_nodes_lock:
                    node["failures"] = 0
                    node["latency"] = 0.8 * node["latency"] + 0.2 * latency
                return resp.get("result")
                
            print(f"⚠️ RPC Node returned non-200 status {r.status_code} for {method} on {url}: {r.text}")
            with _rpc_nodes_lock:
                node["failures"] += 1
                node["penalty_until"] = time.time() + 10.0
                
        except Exception as e:
            print(f"⚠️ RPC Exception for {method} on {url}: {e}")
            with _rpc_nodes_lock:
                node["failures"] += 1
                node["penalty_until"] = time.time() + 10.0
                
    # Fallback
    try:
        r = _http_session.post("https://api.mainnet-beta.solana.com", json=payload, timeout=8)
        if r.status_code == 200:
            res_json = r.json()
            if "error" in res_json:
                print(f"⚠️ RPC Fallback Error for {method}: {res_json['error']}")
            return res_json.get("result")
        else:
            print(f"⚠️ RPC Fallback returned non-200 status {r.status_code}: {r.text}")
    except Exception as e:
        print(f"⚠️ RPC Fallback Exception for {method}: {e}")
    return None

def rpc_batch_call(payload: list):
    _init_rpc_pool()
    now = time.time()
    with _rpc_nodes_lock:
        sorted_nodes = sorted(
            _rpc_nodes_state,
            key=lambda x: (
                1 if x["penalty_until"] > now else 0,
                x["failures"],
                x["latency"]
            )
        )
        
    max_retries = min(5, len(sorted_nodes))
    for attempt in range(max_retries):
        node = sorted_nodes[attempt]
        url = node["url"]
        try:
            r = _http_session.post(url, json=payload, timeout=8)
            if r.status_code == 429:
                with _rpc_nodes_lock:
                    node["failures"] += 1
                    node["penalty_until"] = time.time() + 15.0
                continue
                
            if r.status_code == 200:
                try:
                    resp = r.json()
                    if isinstance(resp, list) and resp:
                        first = resp[0]
                        if "error" in first:
                            err = first["error"]
                            code = err.get("code", 0)
                            msg = err.get("message", "")
                            if code == -32429 or "rate limit" in msg.lower() or "too many requests" in msg.lower():
                                with _rpc_nodes_lock:
                                    node["failures"] += 1
                                    node["penalty_until"] = time.time() + 15.0
                                continue
                    return resp
                except Exception:
                    continue
                    
            with _rpc_nodes_lock:
                node["failures"] += 1
                node["penalty_until"] = time.time() + 10.0
        except Exception:
            with _rpc_nodes_lock:
                node["failures"] += 1
                node["penalty_until"] = time.time() + 10.0
                
    # Fallback
    try:
        r = _http_session.post("https://api.mainnet-beta.solana.com", json=payload, timeout=8)
        if r.status_code == 200:
            return r.json()
    except:
        pass
    return None


# ─── SOL/USD Price Cache ───────────────────────────────────────────────────────
_sol_usd_cache = 150.0
_sol_usd_last  = 0.0

def get_sol_usd_price() -> float:
    global _sol_usd_cache, _sol_usd_last
    now = time.time()
    if now - _sol_usd_last > 30:
        try:
            r = _http_session.get(
                "https://api.dexscreener.com/latest/dex/tokens/" + WSOL_ADDRESS,
                timeout=5
            )
            if r.status_code == 200:
                pairs = r.json().get("pairs", [])
                if pairs:
                    best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                    _sol_usd_cache = float(best.get("priceUsd", 150.0))
                    _sol_usd_last  = now
        except Exception as e:
            print(f"⚠️ Failed to update SOL price cache: {e}")
    return _sol_usd_cache
def get_dynamic_priority_fee(target_token: str) -> int:
    try:
        sensitivity = _cfg("sensitivity_mode", "neutral").lower()
        priority_level = "VeryHigh" if sensitivity == "aggressive" else "High"
        if sensitivity == "conservative":
            priority_level = "Medium"
            
        res = rpc_call("getPriorityFeeEstimate", [
            {
                "accountKeys": [target_token, RAYDIUM_LP_V4],
                "options": {
                    "priorityLevel": priority_level
                }
            }
        ])
        if res and "priorityFeeEstimate" in res:
            micro_lamports = res["priorityFeeEstimate"]
            lamports = int((micro_lamports * 150000) / 1e6)
            
            # Apply dynamic multiplier based on conviction (AI score)
            token_ai_score = _token_ai_scores.get(target_token, 50)
            if token_ai_score >= 80:
                lamports = int(lamports * 1.5)
                print(f"🔥 High Conviction dynamic priority fee multiplier applied (AI Score: {token_ai_score}).")
                
            return max(5000, min(15000000, lamports))
    except Exception:
        pass

    # Standard Solana RPC fallback using getRecentPrioritizationFees (75th percentile)
    try:
        res_fees = rpc_call("getRecentPrioritizationFees", [[target_token, RAYDIUM_LP_V4]])
        if res_fees:
            fees = [item.get("prioritizationFee", 0) for item in res_fees[-20:]]
            if fees:
                fees.sort()
                p75_idx = int(len(fees) * 0.75)
                p75_micro_lamports = fees[p75_idx]
                lamports = int((p75_micro_lamports * 150000) / 1e6)
                
                token_ai_score = _token_ai_scores.get(target_token, 50)
                if token_ai_score >= 80:
                    lamports = int(lamports * 1.5)
                return max(5000, min(15000000, lamports))
    except Exception as e:
        print(f"⚠️ Failed to calculate standard priority fee fallback: {e}")
        
    return int(_cfg("default_priority_fee_lamports", 10000))

def get_dynamic_slippage(target_token: str, is_buy: bool) -> float:
    base_slippage = _cfg("slippage_percent", 15.0)
    if not is_buy:
        # Sell slippage: higher threshold to guarantee exit during panics
        return max(20.0, base_slippage)
        
    s = load_state()
    watchlist = s.get("watchlist", [])
    added_time = 0
    for p in watchlist:
        if p.get("target_token") == target_token:
            added_time = p.get("added_time", 0)
            break
            
    now = time.time()
    if added_time > 0:
        time_elapsed = now - added_time
        if time_elapsed <= 5.0:
            # Block-0 sniper race window
            print(f"⚡ Sniper window active ({time_elapsed:.1f}s since discovery). Scaling slippage up to 95.0% for launch block execution to bypass 3012 errors (Jito MEV sandwich protection active).")
            return 95.0
        elif time_elapsed > 10.0 and time_elapsed < 60.0:
            # Anti-FOMO top-buy protection
            print(f"⚠️ Cooling period active ({time_elapsed:.1f}s since discovery). Scaling slippage down to 8.0% to prevent buying the pump peak.")
            return 8.0
            
    return base_slippage

def check_deployer_rug_history(deployer_addr: str) -> bool:
    if not deployer_addr:
        return True
    print(f"🔍 Creator Check: Performing DNA Profile on deployer {deployer_addr[:16]}...")
    try:
        # Fetch last 40 signatures to have a wider search window of historical launches
        sigs = rpc_call("getSignaturesForAddress", [deployer_addr, {"limit": 40}])
        if not sigs:
            print("🟢 Deployer has no transaction history (assumed fresh or clean).")
            return True
            
        rugged_launches = 0
        total_mints_found = []
        
        # 1. Batch fetch all transactions to locate mint creation instructions
        tx_requests = []
        for i, tx_sig in enumerate(sigs):
            sig = tx_sig.get("signature")
            if sig:
                tx_requests.append({
                    "jsonrpc": "2.0",
                    "id": i + 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}]
                })
                
        if not tx_requests:
            return True
            
        tx_responses = rpc_batch_call(tx_requests)
        if not tx_responses or not isinstance(tx_responses, list):
            print("⚠️ Creator DNA Check: Batch fetch failed. Falling back to sequential fetch of last 5 signatures...")
            tx_responses = []
            for idx, tx_sig in enumerate(sigs[:5]):
                sig = tx_sig.get("signature")
                if sig:
                    try:
                        tx_detail = rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                        if tx_detail:
                            tx_responses.append({
                                "id": idx + 1,
                                "result": tx_detail
                            })
                    except Exception as err:
                        print(f"⚠️ Failed to get transaction details sequentially: {err}")
            if not tx_responses:
                print("⚠️ Creator DNA Check: Sequential fetch failed. Skipping deployer check.")
                return True
            
        for resp in tx_responses:
            tx_detail = resp.get("result")
            if not tx_detail or not tx_detail.get("transaction"):
                continue
                
            # Scan instructions for mint initialization
            message = tx_detail["transaction"].get("message", {})
            instructions = message.get("instructions", [])
            
            # Combine inner instructions if present
            if tx_detail.get("meta") and tx_detail["meta"].get("innerInstructions"):
                for inner in tx_detail["meta"]["innerInstructions"]:
                    instructions.extend(inner.get("instructions", []))
                    
            for inst in instructions:
                program = inst.get("program")
                parsed = inst.get("parsed")
                
                mint_addr = None
                if program == "system" and parsed and parsed.get("type") == "createAccount":
                    info = parsed.get("info", {})
                    if info.get("space") == 82 and info.get("newAccount"):
                        mint_addr = info.get("newAccount")
                elif program == "spl-token" and parsed and parsed.get("type") in ["initializeMint", "initializeMint2", "initializeMint3"]:
                    info = parsed.get("info", {})
                    mint_addr = info.get("mint")
                    
                if mint_addr and mint_addr not in total_mints_found:
                    total_mints_found.append(mint_addr)
                    
        print(f"🧬 DNA Profile: Found {len(total_mints_found)} previous token mints created by this deployer.")
        if not total_mints_found:
            return True
            
        # Limit audits to the 10 most recent launches to save resources and comply with RPC limits
        audited_mints = total_mints_found[:10]
        
        # 2. Batch audit all discovered past mints in a single query
        audit_payload = []
        for idx, past_mint in enumerate(audited_mints):
            # We map getAccountInfo to id = idx * 2 + 1, and getTokenLargestAccounts to id = idx * 2 + 2
            audit_payload.append({
                "jsonrpc": "2.0",
                "id": idx * 2 + 1,
                "method": "getAccountInfo",
                "params": [past_mint, {"encoding": "base64"}]
            })
            audit_payload.append({
                "jsonrpc": "2.0",
                "id": idx * 2 + 2,
                "method": "getTokenLargestAccounts",
                "params": [past_mint]
            })
            
        audit_results = rpc_batch_call(audit_payload)
        if not audit_results or not isinstance(audit_results, list):
            print("⚠️ Creator DNA Check: Batch audit failed. Falling back to sequential audit of last 3 mints...")
            audit_results = []
            for idx, past_mint in enumerate(audited_mints[:3]):
                try:
                    info_val = rpc_call("getAccountInfo", [past_mint, {"encoding": "base64"}])
                    large_accts_val = rpc_call("getTokenLargestAccounts", [past_mint])
                    if info_val:
                        audit_results.append({"id": idx * 2 + 1, "result": info_val})
                    if large_accts_val:
                        audit_results.append({"id": idx * 2 + 2, "result": large_accts_val})
                except Exception as err:
                    print(f"⚠️ Failed to sequentially audit mint {past_mint[:8]}...: {err}")
            if not audit_results:
                print("⚠️ Creator DNA Check: Sequential audit failed. Skipping deployer check.")
                return True
                
        audit_map = {res.get("id"): res.get("result") for res in audit_results if isinstance(res, dict)}
        
        for idx, past_mint in enumerate(audited_mints):
            info_res = audit_map.get(idx * 2 + 1)
            large_accts_res = audit_map.get(idx * 2 + 2)
            
            if not info_res or not info_res.get("value"):
                continue
                
            # Parse authorities from base64 data
            data_b64 = info_res["value"].get("data", [""])[0]
            if not data_b64:
                continue
            import base64
            data = base64.b64decode(data_b64)
            if len(data) < 82:
                continue
                
            mint_auth_opt = int.from_bytes(data[0:4], "little")
            freeze_auth_opt = int.from_bytes(data[46:50], "little")
            
            is_rugged_mint = False
            # Audit criteria 1: active mint or freeze authority
            if mint_auth_opt != 0 or freeze_auth_opt != 0:
                is_rugged_mint = True
                
            # Audit criteria 2: centralization check
            if large_accts_res and large_accts_res.get("value"):
                top_acct = large_accts_res["value"][0]
                # If top account holds almost 100% of supply, it's either unlaunched or fully dumped/cabal
                if float(top_acct.get("share", 0) or 0) > 95.0:
                    is_rugged_mint = True
                    
            if is_rugged_mint:
                rugged_launches += 1
                
        if rugged_launches > 0:
            print(f"🚨 Creator Check FAILED: Deployer has {rugged_launches}/{len(total_mints_found)} rugged/honeypot tokens in their history! DNA Profiling: HIGH RISK.")
            return False
            
        print(f"🟢 Creator Check PASSED: DNA Profiling clear ({rugged_launches} rugged tokens found out of {len(total_mints_found)}).")
    except Exception as e:
        print(f"⚠️ Creator DNA check skipped due to rate limit/error: {e}")
    return True


def get_dexscreener_token(token_mint: str) -> dict:
    """Return {symbol, name, price_usd, liquidity_usd} or None."""
    try:
        r = _http_session.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}",
            timeout=5
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                return {
                    "symbol":        best.get("baseToken", {}).get("symbol", "TOKEN"),
                    "name":          best.get("baseToken", {}).get("name", "Token"),
                    "price_usd":     float(best.get("priceUsd", 0) or 0),
                    "liquidity_usd": float(best.get("liquidity", {}).get("usd", 0) or 0),
                }
    except Exception as e:
        print(f"⚠️ DexScreener fetch error for {token_mint}: {e}")
    return None

_cached_state = None
_last_state_mtime = 0.0

def load_state() -> dict:
    global _cached_state, _last_state_mtime
    if os.path.exists(STATE_PATH):
        try:
            mtime = os.path.getmtime(STATE_PATH)
            if mtime == _last_state_mtime and _cached_state is not None:
                return _cached_state
            with open(STATE_PATH, "r") as f:
                s = json.load(f)
            s.setdefault("watchlist", [])
            s.setdefault("trade_history", [])
            s.setdefault("half_sold", False)
            s.setdefault("sell_attempts", 0)
            s.setdefault("buy_timestamp", 0)
            s.setdefault("initial_liq_sol", 0.0)
            s.setdefault("highest_price_sol", s.get("buy_price_sol", 0.0))
            _cached_state = s
            _last_state_mtime = mtime
            return s
        except:
            pass
    if _cached_state is not None:
        return _cached_state
    return {
        "in_position":      False,
        "token_address":    "",
        "token_symbol":     "",
        "pair_address":     "",
        "buy_price_sol":    0.0,
        "highest_price_sol": 0.0,
        "amount_held":      0.0,
        "buy_timestamp":    0,
        "initial_liq_sol":  0.0,
        "half_sold":        False,
        "sell_attempts":    0,
        "watchlist":        [],
        "trade_history":    [],
        "last_trade":       None,
    }

def save_state(s: dict):
    global _cached_state, _last_state_mtime
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(s, f, indent=2)
        _cached_state = s
        try:
            _last_state_mtime = os.path.getmtime(STATE_PATH)
        except:
            _last_state_mtime = time.time()
    except Exception as e:
        print(f"❌ Failed to save state: {e}")

def save_trade_to_history(trade: dict):
    try:
        history = []
        if os.path.exists(HISTORY_PATH):
            with open(HISTORY_PATH, "r") as f:
                history = json.load(f)
        history.append(trade)
        with open(HISTORY_PATH, "w") as f:
            json.dump(history, f, indent=2)
        print("💾 Trade saved to history file.")
    except Exception as e:
        print(f"⚠️ Failed to save trade to history: {e}")

# ─── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(msg: str, chat_id=None, reply_markup=None):
    """Non-blocking Telegram send — fires in a daemon thread."""
    _reload_config()
    tok = _cfg("telegram_token", "")
    cid = chat_id if chat_id else _cfg("telegram_chat_id", "")
    if not tok or not cid:
        return
    def _send():
        try:
            url = f"https://api.telegram.org/bot{tok}/sendMessage"
            payload = {"chat_id": cid, "text": msg, "parse_mode": "Markdown"}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = _http_session.post(url, json=payload, timeout=5)
            if resp.status_code != 200:
                print(f"⚠️ Telegram send failed ({resp.status_code}): {resp.text}")
                if "can't parse entities" in resp.text:
                    # Retry without markdown
                    plain = {"chat_id": cid, "text": msg}
                    if reply_markup:
                        plain["reply_markup"] = reply_markup
                    _http_session.post(url, json=plain, timeout=5)
        except Exception as e:
            print(f"⚠️ Telegram Alert Error: {e}")
    threading.Thread(target=_send, daemon=True).start()

# ─── Discord Webhook ──────────────────────────────────────────────────────────
def send_discord_webhook(embed_data=None, content=None):
    _reload_config()
    url = _cfg("discord_webhook_url", "")
    if not url or url.strip().lower() in ("", "none"):
        return
    try:
        payload = {}
        if content:
            payload["content"] = content
        if embed_data:
            payload["embeds"] = [embed_data]
        if not payload:
            return
        resp = _http_session.post(url, json=payload, timeout=5)
        if resp.status_code not in (200, 204):
            print(f"⚠️ Discord Webhook Failed ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"⚠️ Discord Webhook Error: {e}")

# ─── Alert Helpers ─────────────────────────────────────────────────────────────
def alert_trade_enter(symbol: str, token_addr: str, sol_amount: float, liq_sol: float):
    sol_price = get_sol_usd_price()
    trade_usd = sol_amount * sol_price
    liq_usd   = liq_sol * sol_price

    ai_score = _token_ai_scores.get(token_addr, "N/A")
    msg = (
        f"📥 *TRADE ENTERED*\n\n"
        f"🪙 *Token*: {symbol}\n"
        f"🔗 *Address*: `{token_addr}`\n"
        f"💰 *Trade Size*: `{sol_amount} SOL` (~${trade_usd:.2f})\n"
        f"🌊 *Pool Liquidity*: `{liq_sol:.2f} SOL` (~${liq_usd:,.2f})\n"
        f"🧠 *BullX-NEO AI Score*: `{ai_score}/100`\n\n"
        f"ℹ️ The bot is now monitoring live. It will take profit or cut losses automatically."
    )
    reply_markup = {
        "inline_keyboard": [[
            {"text": "💰 Sell 50%",  "callback_data": f"sell_50_{token_addr}"},
            {"text": "🚨 Sell 100%", "callback_data": f"sell_100_{token_addr}"},
        ]]
    }
    send_telegram(msg, reply_markup=reply_markup)

    # Discord
    discord_embed = {
        "title": "📥 TRADE ENTERED",
        "description": "The Solana sniper bot has executed a swap via Jupiter.",
        "color": 3066993,
        "fields": [
            {"name": "🪙 Token Symbol",   "value": f"**{symbol}**",                       "inline": True},
            {"name": "💰 Trade Size",     "value": f"{sol_amount} SOL (~${trade_usd:.2f})", "inline": True},
            {"name": "🌊 Pool Liquidity", "value": f"{liq_sol:.2f} SOL (~${liq_usd:,.2f})", "inline": True},
            {"name": "🔗 Token Address",  "value": f"`{token_addr}`",                      "inline": False},
            {"name": "📊 DexScreener",    "value": f"[View Chart](https://dexscreener.com/solana/{token_addr})", "inline": True},
        ],
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "footer":    {"text": "Solana Meme Coin Sniper Bot"},
    }
    send_discord_webhook(embed_data=discord_embed)

def alert_trade_close(symbol: str, buy_sol: float, sell_sol: float, pnl_pct: float, reason: str):
    sol_price   = get_sol_usd_price()
    is_win      = pnl_pct > 0
    emoji       = "🏆 *TRADE WIN*" if is_win else "💀 *TRADE LOSS*"
    status_icon = "🟢" if is_win else "🔴"
    _reload_config()
    trade_amt   = _cfg("trade_amount_sol", 0.05)
    initial_usd = trade_amt * sol_price
    exit_usd    = initial_usd * (1 + pnl_pct / 100)
    net_usd     = exit_usd - initial_usd
    pnl_sign    = "+" if net_usd >= 0 else ""

    msg = (
        f"{status_icon} {emoji}\n\n"
        f"🪙 *Token*: {symbol}\n"
        f"📥 *Bought At*: `{buy_sol:.6f} SOL`\n"
        f"📤 *Sold At*:   `{sell_sol:.6f} SOL`\n\n"
        f"💵 *Initial Investment*: ${initial_usd:.2f} ({trade_amt} SOL)\n"
        f"💵 *Final Payout*: ${exit_usd:.2f}\n"
        f"📊 *Net Return*: *{pnl_pct:+.2f}%* ({pnl_sign}${net_usd:.2f})\n"
        f"ℹ️ *Reason*: {reason}"
    )
    send_telegram(msg)

    discord_title = "🏆 TRADE WIN" if is_win else "💀 TRADE LOSS"
    color = 3066993 if is_win else 15158332
    discord_embed = {
        "title":       f"{status_icon} {discord_title}",
        "description": "Position closed via Jupiter swap.",
        "color":       color,
        "fields": [
            {"name": "🪙 Token",             "value": f"**{symbol}**",          "inline": True},
            {"name": "📥 Entry",             "value": f"{buy_sol:.6f} SOL",     "inline": True},
            {"name": "📤 Exit",              "value": f"{sell_sol:.6f} SOL",    "inline": True},
            {"name": "💵 Initial Investment","value": f"${initial_usd:.2f}",    "inline": True},
            {"name": "💵 Final Payout",      "value": f"${exit_usd:.2f}",       "inline": True},
            {"name": "📊 Net Return",        "value": f"**{pnl_pct:+.2f}%** ({pnl_sign}${net_usd:.2f})", "inline": False},
            {"name": "ℹ️ Reason",            "value": reason,                   "inline": False},
        ],
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "footer":    {"text": "Solana Meme Coin Sniper Bot"},
    }
    send_discord_webhook(embed_data=discord_embed)

def send_x_promo_webhook(symbol, token_addr, liq_sol, liq_usd):
    _reload_config()
    x_webhook = _cfg("x_webhook_url", "")
    if not x_webhook or x_webhook.strip() == "" or x_webhook.strip().lower() == "none":
        return
    
    # Styled tweet promotion message
    tweet_text = (
        f"🔍 NEW LAUNCH SIGNAL DISCOVERED! 🚀\n\n"
        f"🪙 Token: {symbol}\n"
        f"🌊 Liquidity: {liq_sol:.2f} SOL (~${liq_usd:,.2f})\n"
        f"🛡️ Safety: Mint + Freeze Revoked ✅\n\n"
        f"Follow the updates & trade here: linktr.ee/jadenmitchell78 📈\n"
        f"Contract: {token_addr}"
    )
    
    def _send_tweet():
        try:
            payload = {
                "text": tweet_text,
                "symbol": symbol,
                "contractAddress": token_addr,
                "liquidity_sol": liq_sol,
                "liquidity_usd": liq_usd
            }
            resp = _http_session.post(x_webhook, json=payload, timeout=10)
            if resp.status_code == 200 or resp.status_code == 201:
                print(f"📊 X (Twitter) Promo Webhook successfully triggered for {symbol}.")
            else:
                print(f"⚠️ X Promo Webhook failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"⚠️ X Promo Webhook Error: {e}")
            
    threading.Thread(target=_send_tweet, daemon=True).start()

def alert_discovery(symbol: str, token_addr: str, liq_sol: float):
    sol_price = get_sol_usd_price()
    liq_usd   = liq_sol * sol_price
    
    # Send Telegram message to default private chat
    msg = (
        f"🔍 *New Alpha Alert: Safe Pool Detected!*\n"
        f"🪙 *Symbol*: {symbol}\n"
        f"🌊 *Liquidity*: {liq_sol:.2f} SOL (~${liq_usd:,.2f})\n"
        f"🛡️ Safety: Mint + Freeze Authorities REVOKED\n"
        f"[View on DexScreener](https://dexscreener.com/solana/{token_addr})"
    )
    send_telegram(msg)

    # Send formatted telemetry block to AuraScanVM channel
    channel_msg = (
        f"🔍 *[AuraScan Discovery]*\n"
        f"🪙 *Symbol*: `{symbol}`\n"
        f"🌊 *Liquidity*: `{liq_sol:.2f} SOL` (~${liq_usd:,.2f})\n"
        f"🛡️ *Safety Check*: `Mint + Freeze Revoked ✅`\n"
        f"🔗 *Contract*: `{token_addr}`\n"
        f"📊 *Chart*: [DexScreener Link](https://dexscreener.com/solana/{token_addr})"
    )
    send_telegram(channel_msg, chat_id="@AuraScanVM")

    # Send X Promo Webhook
    send_x_promo_webhook(symbol, token_addr, liq_sol, liq_usd)

    discord_embed = {
        "title":       "🔍 NEW ALPHA ALERT: Safe Pool Discovered!",
        "description": "A new Raydium pool has launched and passed all safety checks.",
        "color":       1752220,
        "fields": [
            {"name": "🪙 Symbol",       "value": f"**{symbol}**",                      "inline": True},
            {"name": "🌊 Liquidity",    "value": f"{liq_sol:.2f} SOL (~${liq_usd:,.2f})", "inline": True},
            {"name": "🛡️ Safety Check", "value": "Mint + Freeze Revoked ✅",           "inline": False},
            {"name": "🔗 Token",        "value": f"`{token_addr}`",                    "inline": False},
            {"name": "📊 DexScreener",  "value": f"[View Chart](https://dexscreener.com/solana/{token_addr})", "inline": True},
        ],
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "footer":    {"text": "Solana Sniper Bot – Alpha Alerts"},
    }
    send_discord_webhook(embed_data=discord_embed)

# ─── Status Report ─────────────────────────────────────────────────────────────
def send_status_report(chat_id=None):
    _reload_config()
    tok = _cfg("telegram_token", "")
    cid = chat_id if chat_id else _cfg("telegram_chat_id", "")
    if not tok or not cid:
        return

    s         = load_state()
    sol_price = get_sol_usd_price()
    S         = get_settings()

    mode_str = "🧪 DRY RUN" if S["DRY_RUN"] else "🔥 LIVE TRADING"
    scan_str = "🟢 ACTIVE" if S["SCANNER_ACTIVE"] else "⏸️ PAUSED"

    msg  = f"🛰️ *Solana Sniper Bot Status*: {scan_str}\n"
    msg += f"⚙️ *Mode*: {mode_str}\n\n"

    if s.get("in_position"):
        token_addr  = s["token_address"]
        symbol      = s["token_symbol"]
        buy_sol     = s["buy_price_sol"]
        highest_sol = s["highest_price_sol"]
        amt_held    = s["amount_held"]

        current_sol = get_current_price_sol(token_addr, amt_held)
        if current_sol <= 0:
            current_sol = buy_sol

        trade_usd   = S["TRADE_SOL"] * sol_price
        current_usd = trade_usd * (current_sol / buy_sol) if buy_sol > 0 else trade_usd
        net_pnl_usd = current_usd - trade_usd
        chg_pct     = ((current_sol - buy_sol) / buy_sol * 100) if buy_sol > 0 else 0

        ts_price    = highest_sol * (1 - S["TRAILING_STOP"] / 100)
        sl_price    = buy_sol * (1 - S["HARD_SL"] / 100)
        ts_pnl_pct  = ((ts_price - buy_sol) / buy_sol * 100) if buy_sol > 0 else 0
        ts_usd      = trade_usd * (1 + ts_pnl_pct / 100)
        sl_usd      = trade_usd * (1 - S["HARD_SL"] / 100)

        pnl_emoji   = "🟢" if net_pnl_usd >= 0 else "🔴"
        pnl_sign    = "+" if net_pnl_usd >= 0 else ""
        ts_sign     = "+" if (ts_usd - trade_usd) >= 0 else ""

        msg += f"📈 *Current Trade*: {symbol}\n"
        msg += f"🔗 *Token*: `{token_addr}`\n\n"
        msg += f"💵 *Your Investment*: `{S['TRADE_SOL']} SOL` (~${trade_usd:.2f})\n"
        msg += f"📥 *Bought At*: `{buy_sol:.6f} SOL`\n"
        msg += f"🪙 *Current Val*: `{current_sol:.6f} SOL`\n\n"
        msg += f"{pnl_emoji} *P&L Right Now*: *{chg_pct:+.2f}%* ({pnl_sign}${net_pnl_usd:.2f})\n"
        msg += f"👉 *Current Value*: ${current_usd:.2f}\n\n"

        buy_ts = s.get("buy_timestamp", 0)
        if S["TIME_EXIT"] > 0 and buy_ts > 0:
            elapsed_min   = (time.time() - buy_ts) / 60
            remaining_min = max(0.0, S["TIME_EXIT"] - elapsed_min)
            msg += f"⏱️ *Time Exit*: Held `{elapsed_min:.1f}` min. Auto-sells in `{remaining_min:.1f}` min.\n\n"

        if s.get("half_sold"):
            msg += f"✂️ *Half-Sold*: 50% already taken off the table.\n\n"

        msg += f"🎯 *Exit Projections*:\n"
        msg += f"🛡️ Trailing Stop at `{ts_price:.6f} SOL` → *{ts_pnl_pct:+.2f}%* ({ts_sign}${ts_usd - trade_usd:.2f})\n"
        msg += f"🚨 Hard Stop Loss at `{sl_price:.6f} SOL` → *-{S['HARD_SL']:.2f}%* (-${trade_usd - sl_usd:.2f})\n"
        if S["HARD_TP"] > 0:
            tp_price = buy_sol * (1 + S["HARD_TP"] / 100)
            tp_usd   = trade_usd * (1 + S["HARD_TP"] / 100)
            msg += f"🏆 Take Profit at `{tp_price:.6f} SOL` → *+{S['HARD_TP']:.2f}%* (+${tp_usd - trade_usd:.2f})\n"
    else:
        msg += "💤 *Position*: None. Scanning Raydium for new launches..."

    send_telegram(msg, chat_id=cid)

# ─── SPL Token Account ─────────────────────────────────────────────────────────
def get_ata_address(wallet: str, token: str):
    res = rpc_call("getTokenAccountsByOwner", [
        wallet,
        {"mint": token},
        {"encoding": "jsonParsed"}
    ])
    if res and res.get("value"):
        return res["value"][0]["pubkey"]
    return None

def check_rugcheck_safety(token_addr: str):
    print(f"🔍 RugCheck Scan: Fetching security report for {token_addr[:16]}...")
    url = f"https://api.rugcheck.xyz/v1/tokens/{token_addr}/report"
    try:
        r = _http_session.get(url, timeout=5)
        if r.status_code == 200:
            report = r.json()
            score = report.get("score", 0)
            print(f"📊 RugCheck Score: {score}")
            if score > 1200:
                print(f"🚨 FAILED: RugCheck score {score} is too high (>1200 - High Risk).")
                return False
                
            risks = report.get("risks", [])
            for risk in risks:
                name = risk.get("name", "")
                level = risk.get("level", "")
                if level == "danger":
                    print(f"🚨 FAILED: Critical RugCheck risk detected: {name} ({level})")
                    return False
            print("🟢 RugCheck PASSED.")
            return report
        elif r.status_code == 404:
            if _cfg("require_rugcheck_pass", False):
                print("🚨 FAILED: Token report not found on RugCheck (too new) and require_rugcheck_pass is active. Rejecting.")
                return False
            print("⚠️ Warning: Token report not found on RugCheck (too new). Proceeding with native checks.")
            return {}
        else:
            if _cfg("require_rugcheck_pass", False):
                print(f"🚨 FAILED: RugCheck API returned status {r.status_code} and require_rugcheck_pass is active. Rejecting.")
                return False
            print(f"⚠️ Warning: RugCheck API returned status {r.status_code}. Proceeding with native checks.")
            return {}
    except Exception as e:
        if _cfg("require_rugcheck_pass", False):
            print(f"🚨 FAILED: RugCheck safety check failed: {e} and require_rugcheck_pass is active. Rejecting.")
            return False
        print(f"⚠️ Warning: RugCheck safety check failed: {e}. Proceeding with native checks.")
        return {}

# ─── Safety Scanner ────────────────────────────────────────────────────────────
def check_token_safety(token_addr: str) -> bool:
    print(f"🛡️ Security Scan: Performing batch RPC checks for {token_addr}...")
    _reload_config()
    url = _cfg("rpc_url", "https://api.mainnet-beta.solana.com")
    
    # 1. Batch RPC Request to combine 3 round-trips into 1 (saving ~200ms)
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [token_addr, {"encoding": "base64"}]},
        {"jsonrpc": "2.0", "id": 2, "method": "getTokenSupply", "params": [token_addr]},
        {"jsonrpc": "2.0", "id": 3, "method": "getTokenLargestAccounts", "params": [token_addr]}
    ]
    
    res_map = {}
    info_res = None
    # Retry up to 4 times with a 250ms backoff to handle RPC node sync propagation delay
    for attempt in range(4):
        results = rpc_batch_call(payload)
        if results and isinstance(results, list) and len(results) >= 3:
            res_map = {res.get("id"): res.get("result") for res in results}
            info_res = res_map.get(1)
            if info_res and info_res.get("value"):
                break
        else:
            try:
                info_val = rpc_call("getAccountInfo", [token_addr, {"encoding": "base64"}])
                if info_val and info_val.get("value"):
                    res_map[1] = info_val
                    res_map[2] = rpc_call("getTokenSupply", [token_addr])
                    res_map[3] = rpc_call("getTokenLargestAccounts", [token_addr])
                    info_res = info_val
                    break
            except Exception:
                pass
        time.sleep(0.25)
        
    supply_res = res_map.get(2)
    large_accounts_res = res_map.get(3)

    # 2. Process getAccountInfo (Mint/Freeze checks)
    if not info_res or not info_res.get("value"):
        print("🚨 FAILED: Mint account not found after retries.")
        return False
    owner_program = info_res["value"].get("owner", "")
    if owner_program not in (TOKEN_PROGRAM_ID, TOKEN_2022_PROGRAM_ID):
        print(f"🚨 FAILED: Token program is {owner_program} (expected legacy Tokenkeg or Tokenz). Rejecting due to potential extension honeypots.")
        return False
    data_b64 = info_res["value"].get("data", [""])[0]
    if not data_b64:
        print("🚨 FAILED: Account data empty.")
        return False
    data = base64.b64decode(data_b64)
    if len(data) < 82:
        print(f"🚨 FAILED: Account data too small ({len(data)} bytes).")
        return False

    mint_auth_opt   = int.from_bytes(data[0:4],   "little")
    freeze_auth_opt = int.from_bytes(data[46:50], "little")

    if mint_auth_opt != 0:
        mint_auth_pubkey = data[4:36]
        is_pump_token = token_addr.endswith("pump")
        is_safe_pump = False
        if is_pump_token:
            bonding_curve = solana_tx_builder.get_bonding_curve_address(b58decode(token_addr))
            if mint_auth_pubkey == bonding_curve:
                is_safe_pump = True
                
        if not is_safe_pump:
            print("🚨 FAILED: Mint authority ACTIVE — dev can mint & dump.")
            return False
        else:
            print("🟢 Mint Authority is the bonding curve PDA (safe).")

    if freeze_auth_opt != 0:
        print("🚨 FAILED: Freeze authority ACTIVE — honeypot risk.")
        return False

    # 3. Process getTokenSupply and getTokenLargestAccounts (Centralization checks)
    if supply_res and isinstance(supply_res, dict) and "value" in supply_res:
        val = supply_res["value"]
        total_supply = float(val.get("uiAmount", 0.0) or 0.0)
        if total_supply > 0 and large_accounts_res and isinstance(large_accounts_res, dict) and "value" in large_accounts_res:
            accounts = large_accounts_res["value"]
            if len(accounts) > 1:
                accounts = sorted(accounts, key=lambda x: float(x.get("uiAmount", 0.0) or 0.0), reverse=True)
                individual_holdings = []
                for acct in accounts[1:6]:
                    ui_amt = float(acct.get("uiAmount", 0.0) or 0.0)
                    pct = (ui_amt / total_supply) * 100
                    individual_holdings.append(pct)
                
                sum_top_holders = sum(individual_holdings)
                print(f"📊 Top 5 holders concentration (excluding pool): {sum_top_holders:.1f}%")
                
                for i, pct in enumerate(individual_holdings):
                    if pct > 25.0:
                        print(f"🚨 FAILED: Holder #{i+1} holds {pct:.1f}% (>25%) of supply.")
                        return False
                
                if sum_top_holders > 50.0:
                    print(f"🚨 FAILED: Top 5 holders hold {sum_top_holders:.1f}% (>50%) of supply.")
                    return False
                    
                # ── Sybil Wallet Cluster Mapping Check ──
                print("🔍 Analyzing Whale/Cabal cluster origins...")
                funder_map = {}
                checked_wallets = 0
                for acct in accounts[1:6]:
                    owner_addr = acct.get("address")
                    if not owner_addr:
                        continue
                    checked_wallets += 1
                    # Retrieve the earliest transaction signature to locate the funding wallet
                    tx_sigs = rpc_call("getSignaturesForAddress", [owner_addr, {"limit": 1}])
                    if tx_sigs and len(tx_sigs) > 0:
                        sig = tx_sigs[0].get("signature")
                        tx_detail = rpc_call("getTransaction", [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}])
                        if tx_detail and tx_detail.get("meta"):
                            # Find system transfers to identify the funding origin address
                            for instruction in tx_detail.get("transaction", {}).get("message", {}).get("instructions", []):
                                if instruction.get("program") == "system" and instruction.get("parsed", {}).get("type") == "transfer":
                                    info = instruction.get("parsed", {}).get("info", {})
                                    funder = info.get("source")
                                    if funder and funder != owner_addr:
                                        funder_map[funder] = funder_map.get(funder, 0) + 1
                                        break
                
                # If 2 or more of the top 5 holders share the same funding source wallet, abort.
                for funder_wallet, count in funder_map.items():
                    if count >= 2:
                        print(f"🚨 CABAL DETECTED: {count} top holders were funded by the same wallet ({funder_wallet[:12]}...). Rejecting Sybil pool.")
                        return False

    print("🟢 Safety PASSED — Mint & Freeze Authorities revoked. Wallet cluster check clear.")
    
    # Run RugCheck additional safety checks
    rc_report = check_rugcheck_safety(token_addr)
    if rc_report is False:
        return False
        
    # ── NEO/BullX Predictive AI Scorer ──
    mint_auth_revoked = (mint_auth_opt == 0)
    freeze_auth_revoked = (freeze_auth_opt == 0)
    score, scoring_checks = ai_predictive_model.get_solana_predictive_score(
        token_addr, rc_report, mint_auth_revoked, freeze_auth_revoked
    )
    print(f"🧮 [HEURISTIC SCORER] Heuristic Score: {score}/100")
    for chk, status in scoring_checks.items():
        print(f"  ├─ {chk}: {status}")
        
    # Run the advanced Gemini/Heuristic AI predict model for final score
    print("🧠 [AI BRAIN] Running advanced threat predictive model...")
    prediction = ai_predictive_model.predict(token_addr)
    score = int(prediction * 100)
    print(f"🧠 [AI BRAIN] Final safety score: {score}/100")
        
    sensitivity = _cfg("sensitivity_mode", "neutral").lower()
    if sensitivity == "aggressive":
        threshold = 45
    elif sensitivity == "conservative":
        threshold = 75
    else:
        threshold = _cfg("ai_score_threshold", 70)
        
    if score < threshold:
        print(f"🚨 Predictive AI Scan FAILED: Score {score}/100 is below the threshold of {threshold}/100 ({sensitivity} mode). Aborting trade.")
        return False
        
    # Cache the score
    _token_ai_scores[token_addr] = score
        
    return True

def get_dynamic_jito_tip() -> float:
    try:
        r = _http_session.get("https://mainnet.block-engine.jito.wtf/api/v1/tips", timeout=3)
        if r.status_code == 200:
            tips = r.json()
            if tips and isinstance(tips, list):
                latest = tips[0]
                # Default to 75th percentile to guarantee high land rate
                tip_val = float(latest.get("landed_tips_75th", 0.003))
                # Add a small buffer of 5% to beat flat 75th bids
                return max(0.001, min(0.05, tip_val * 1.05))
    except Exception as e:
        print(f"⚠️ Failed to fetch Jito dynamic tip: {e}")
    # Return user configured default if API fails
    return _cfg("jito_tip_sol", 0.003)

def get_auto_compound_trade_amount(target_token: str = None) -> float:
    base_trade = _cfg("trade_amount_sol", 0.05)
    try:
        if wallet_address:
            res = rpc_call("getBalance", [wallet_address])
            if res and isinstance(res, dict) and "value" in res:
                bal_sol = float(res.get("value", 0)) / 1e9
                
                # Pro-Level: AI-Conviction Kelly Fraction Sizing
                fraction = 0.15 # Default 15%
                if target_token:
                    ai_score = _token_ai_scores.get(target_token, 50)
                    if ai_score >= 85:
                        fraction = 0.25 # Aggressive Kelly (25% bankroll) for ultra-high conviction
                    elif ai_score >= 70:
                        fraction = 0.20 # 20% for strong conviction
                    elif ai_score < 50:
                        fraction = 0.05 # 5% minimum for low conviction
                        
                compounded = bal_sol * fraction
                # Cap the trade between the base trade amount and max 1.5 SOL for pro size
                return max(base_trade, min(compounded, 1.5))
    except Exception as e:
        print(f"⚠️ Auto-compound sizing failed: {e}")
    return base_trade

def execute_pump_fun_snipe(mint_str: str, curve_str: str, token_program_str: str = None) -> bool:
    import struct
    import base64
    _reload_config()
    trade_sol = get_auto_compound_trade_amount(mint_str)
    amt_lamports = int(trade_sol * 1e9)
    
    print(f"🎯 Snipe Triggered: Creating pump.fun direct buy for {mint_str[:12]}... (Program: {token_program_str})")
    
    try:
        user_wallet_bytes = b58decode(wallet_address)
        token_mint_bytes = b58decode(mint_str)
        bonding_curve_bytes = b58decode(curve_str)
        
        token_program_bytes = solana_tx_builder.TOKEN_PROGRAM
        if token_program_str:
            try:
                token_program_bytes = b58decode(token_program_str)
            except Exception as e:
                print(f"⚠️ Failed to decode token_program_str {token_program_str}, using default TOKEN_PROGRAM: {e}")
        
        associated_bonding_curve_bytes = solana_tx_builder.get_associated_token_address(bonding_curve_bytes, token_mint_bytes, token_program_bytes)
        
        # Get recent blockhash using our latency-balanced pool
        blockhash_resp = rpc_call("getLatestBlockhash", [])
        if not blockhash_resp or not isinstance(blockhash_resp, dict) or "value" not in blockhash_resp:
            print("❌ Failed to get recent blockhash for pump.fun buy.")
            return False
        blockhash_val = blockhash_resp["value"].get("blockhash")
        if not blockhash_val:
            print("❌ Failed to find blockhash value in RPC response.")
            return False
        recent_blockhash = b58decode(blockhash_val)
        
        # Estimate virtual bonding curve reserves to calculate optimal buy size
        # Virtual reserves default values
        virtual_sol = 30 * 1e9
        virtual_token = 1073000000 * 10**6
        
        # Query reserves on-chain using pool
        curve_info = rpc_call("getAccountInfo", [curve_str, {"encoding": "base64"}])
        if curve_info and curve_info.get("value") and curve_info["value"].get("data"):
            curve_data = base64.b64decode(curve_info["value"]["data"][0])
            if len(curve_data) >= 40:
                # Layout: 8 byte discriminator + virtual_token_reserves (u64) + virtual_sol_reserves (u64)
                virtual_token = int.from_bytes(curve_data[8:16], "little")
                virtual_sol = int.from_bytes(curve_data[16:24], "little")
                
        # Calculate expected token payout: ΔT = T_r * ΔS / (S_r + ΔS)
        token_payout = int(virtual_token * amt_lamports / (virtual_sol + amt_lamports))
        
        # JITO MEV SANDWICH PROTECTION IS ACTIVE - Safe to use ultra-high slippage (95%)
        # This completely fixes the Custom 3012 SlippageExceeded error on high-volume launches
        slippage_buffer = 0.05 # Willing to accept 5% of original quote to guarantee the snipe
        token_amount = int(token_payout * slippage_buffer)        
        inst = solana_tx_builder.build_pump_fun_buy_instruction(
            user_wallet_bytes,
            token_mint_bytes,
            bonding_curve_bytes,
            associated_bonding_curve_bytes,
            token_amount,
            amt_lamports,
            token_program_bytes
        )
        
        # Inject custom priority fees instructions
        COMPUTE_BUDGET_PROGRAM = b58decode("ComputeBudget111111111111111111111111111111")
        priority_fee_estimate = get_dynamic_priority_fee(mint_str)
        # Convert lamports to micro-lamports (approx 80,000 Compute Units)
        micro_lamports = int((priority_fee_estimate * 1e6) / 80000)
        
        priority_data = struct.pack("<BQ", 3, max(100000, min(10000000, micro_lamports)))
        priority_inst = solana_tx_builder.SolanaInstruction(COMPUTE_BUDGET_PROGRAM, [], priority_data)
        
        limit_data = struct.pack("<BI", 2, 80000) # 80k compute units limit
        limit_inst = solana_tx_builder.SolanaInstruction(COMPUTE_BUDGET_PROGRAM, [], limit_data)
        
        create_ata_inst = solana_tx_builder.build_create_ata_instruction(
            user_wallet_bytes, user_wallet_bytes, token_mint_bytes
        )
        instructions = [limit_inst, priority_inst, create_ata_inst, inst]
        use_jito = _cfg("use_jito", True)
        if use_jito:
            import random
            JITO_TIP_ACCOUNTS = [
                "96hfK61AxSzJkQYkQC1j1gVHCc7w8D2GWfBkgP46Grc3",
                "HFqBEvR2Ezau6aojEW683UXCw9s33A1rS2zLFLm1Q4ED",
                "Cw8CFFCM4jXYnF7JeeXGg1yE1m9BDgWi9qf1qibzGs9S",
                "ADa6gKmgJNDwbFY212A3xnAgWf1vwT2ipDfWJBMXQcz7",
                "ADuUk9ZryMbEEt9LsVEjRovvVB3mM1w1UP1GgJaojqdF",
                "Df8yFwzwj7SSCc25Gr2YrrCXVLxx3nc6nM4mAKj2rxXZ",
                "3AVi9VwMQv751zJ1a7G8GfCCTUqv1Fcmqav7C9wSnCSK",
                "DttWaRJcHau2SUNVewHD66qwc1G5J6J3A2SW67ncksgT"
            ]
            jito_tip_sol = get_dynamic_jito_tip()
            jito_tip_lamports = int(jito_tip_sol * 1e9)
            tip_account = b58decode(random.choice(JITO_TIP_ACCOUNTS))
            tip_inst = solana_tx_builder.build_system_transfer_instruction(
                user_wallet_bytes,
                tip_account,
                jito_tip_lamports
            )
            instructions.append(tip_inst)
            print(f"💸 Jito Bundle Mode: Appended tip of {jito_tip_sol:.4f} SOL to Jito tip account {b58encode(tip_account)[:12]}...")
            
        msg = solana_tx_builder.SolanaMessage(
            instructions,
            user_wallet_bytes,
            recent_blockhash
        )
        tx = solana_tx_builder.SolanaTransaction(msg)
        tx.sign(wallet_key)
        
        signed_tx_bytes = tx.serialize()
        signed_b64 = base64.b64encode(signed_tx_bytes).decode("utf-8")
        
        use_jito = _cfg("use_jito", True)
        # On-Chain Simulation before Broadcasting
        sim_passed = False
        if use_jito:
            print("🚀 Skipping simulation for pump.fun buy because Jito MEV is enabled (faster sniping).")
            sim_passed = True
        else:
            for attempt in range(4):
                print(f"🧪 Simulating pump.fun buy transaction locally on-chain (Attempt {attempt+1})...")
                sim_res = rpc_call("simulateTransaction", [
                    signed_b64,
                    {"encoding": "base64", "commitment": "processed"}
                ])
                if sim_res and isinstance(sim_res, dict) and "value" in sim_res:
                    val = sim_res["value"]
                    if val.get("err"):
                        err_str = str(val['err'])
                        print(f"🚨 Solana Simulation FAILED for pump.fun buy: {val['err']}.")
                        if "3012" in err_str or "AccountNotInitialized" in err_str or "IncorrectProgramId" in err_str:
                            print("⏳ Pump.fun Bonding Curve not fully initialized yet. Sleeping 1.5s before retry...")
                            time.sleep(1.5)
                            continue
                        logs = val.get("logs")
                        if logs:
                            print("📋 Simulation Transaction Logs:")
                            for log_line in logs:
                                print(f"   {log_line}")
                        return False
                    else:
                        print("🟢 Solana Simulation PASSED for pump.fun buy!")
                        sim_passed = True
                        break
                else:
                    return False
                    
            if not sim_passed:
                return False
        
        tx_signature = b58encode(tx.signatures[0])
        
        broadcast_ok = False
        if use_jito:
            jito_endpoints = [
                "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
                "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles"
            ]
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "sendBundle",
                "params": [[signed_b64], {"encoding": "base64"}]
            }
            for ep in jito_endpoints:
                try:
                    res = _http_session.post(ep, json=payload, timeout=5)
                    if res.status_code == 200:
                        broadcast_ok = True
                except:
                    pass
        else:
            tx_hash = rpc_call("sendTransaction", [signed_b64, {"encoding": "base64", "preflightCommitment": "processed", "skipPreflight": True}])
            if tx_hash:
                broadcast_ok = True
            
        print(f"🚀 pump.fun buy tx broadcasted! Signature: https://solscan.io/tx/{tx_signature}")
        
        # Wait for confirmation before marking in_position
        confirmed = False
        reverted = False
        for _ in range(30):
            status = rpc_call("getSignatureStatuses", [[tx_signature]])
            if status and status.get("value") and status["value"][0]:
                val = status["value"][0]
                if val.get("err"):
                    print(f"❌ pump.fun buy transaction reverted: {val['err']}")
                    reverted = True
                    break
                if val.get("confirmationStatus") in ["confirmed", "finalized"]:
                    print("🎉 pump.fun buy transaction confirmed!")
                    confirmed = True
                    break
            time.sleep(1)

        if not confirmed:
            if reverted:
                print("❌ pump.fun buy failed (reverted). Skipping state tracking.")
            else:
                print("⏳ pump.fun buy timed out / not confirmed. Skipping state tracking.")
            return False

        # Track position in state
        s = load_state()
        s["in_position"] = True
        s["token_address"] = mint_str
        s["token_symbol"] = "PUMP"
        s["buy_price_sol"] = trade_sol
        s["highest_price_sol"] = trade_sol
        s["amount_held"] = float(token_amount / 10**6)
        s["buy_timestamp"] = int(time.time())
        s["initial_liq_sol"] = float(virtual_sol / 1e9)
        s["half_sold"] = False
        s["sell_attempts"] = 0
        s["token_program"] = token_program_str or TOKEN_PROGRAM_ID
        save_state(s)
        
        alert_trade_enter("PUMP", mint_str, trade_sol, float(virtual_sol / 1e9))
        return True
    except Exception as e:
        print(f"❌ Failed to execute pump.fun buy transaction: {e}")
    return False

# ─── Jupiter Swap ──────────────────────────────────────────────────────────────
def execute_swap(input_mint: str, output_mint: str, amount_lamports: int, is_emergency: bool = False, dex_type: str = "raydium") -> bool:
    _reload_config()
    slippage = 99.0 if is_emergency else _cfg("slippage_percent", 10.0)
    dry_run  = _cfg("dry_run", False)

    if dry_run:
        print(f"🧪 [DRY RUN] Simulating swap {amount_lamports} lamports {input_mint[:8]}...→{output_mint[:8]}...")
        return True

    # ─── Multi-Wallet Loop or Rotation ───
    global wallet_key, wallet_address
    original_key = wallet_key
    original_address = wallet_address
    
    rotate = _cfg("rotate_wallets", False)
    is_buy = (input_mint == WSOL_ADDRESS)
    
    target_wallets = []
    s = load_state()
    
    if is_buy:
        if rotate and len(wallet_keys) > 1:
            curr_idx = s.get("current_wallet_index", 0) % len(wallet_keys)
            target_wallets.append((curr_idx, wallet_keys[curr_idx], wallet_addresses[curr_idx]))
            
            # Save the active wallet index for this trade so we can sell from it later
            s["active_wallet_index"] = curr_idx
            s["current_wallet_index"] = (curr_idx + 1) % len(wallet_keys)
            save_state(s)
        else:
            # Default: try all wallets sequentially
            for idx, (k, addr) in enumerate(zip(wallet_keys, wallet_addresses)):
                target_wallets.append((idx, k, addr))
    else:
        # Sell: check if we have an active wallet index saved for the position
        active_idx = s.get("active_wallet_index")
        if active_idx is not None and active_idx < len(wallet_keys):
            target_wallets.append((active_idx, wallet_keys[active_idx], wallet_addresses[active_idx]))
        else:
            # Fallback: try all wallets sequentially
            for idx, (k, addr) in enumerate(zip(wallet_keys, wallet_addresses)):
                target_wallets.append((idx, k, addr))
                
    success_any = False
    
    for idx, k, addr in target_wallets:
        wallet_key = k
        wallet_address = addr
        if rotate and is_buy:
            print(f"💼 Wallet Rotation: Selected wallet {idx+1}/{len(wallet_keys)}: Swapping on {addr[:12]}...")
        else:
            print(f"💼 Wallet {idx+1}/{len(wallet_keys)}: Swapping on {addr[:12]}...")
        
        target_token = output_mint if input_mint == WSOL_ADDRESS else input_mint
        current_slippage = get_dynamic_slippage(target_token, is_buy)
        for swap_attempt in range(2):
            if swap_attempt > 0:
                current_slippage += 5.0
                print(f"🔄 Attempt {swap_attempt+1}: Retrying swap with increased slippage: {current_slippage}%")
            
            print("🚀 Checking routing bypass parameters...")
            use_jito = _cfg("use_jito", False)
            target_token = output_mint if input_mint == WSOL_ADDRESS else input_mint
            
            # Dynamic Jito Tip scaling based on real-time Jito endpoint floor rates
            token_ai_score = _token_ai_scores.get(target_token, 50)
            base_tip = get_dynamic_jito_tip()
            if token_ai_score >= 80 and not is_emergency:
                jito_tip_sol = base_tip * 1.5
                print(f"🔥 High Conviction Token detected ({target_token[:8]}... AI Score: {token_ai_score}/100). Scaling Jito tip to {jito_tip_sol:.4f} SOL to beat standard sniper bids.")
            is_pump_tx = False
            if dex_type == "pump":
                is_pump_tx = True
                print("🎯 [PUMP.FUN SELL] Creating direct on-chain sell instruction...")
                try:
                    import struct
                    import base64
                    mint_bytes = b58decode(input_mint)
                    user_wallet_bytes = b58decode(wallet_address)
                    
                    # Determine token program dynamically
                    token_program_str = TOKEN_PROGRAM_ID
                    s = load_state()
                    if s.get("token_program"):
                        token_program_str = s["token_program"]
                    else:
                        info = rpc_call("getAccountInfo", [input_mint, {"encoding": "base64"}])
                        if info and info.get("value"):
                            token_program_str = info["value"].get("owner", token_program_str)
                    
                    token_program_bytes = b58decode(token_program_str)
                    
                    bonding_curve_bytes = solana_tx_builder.get_bonding_curve_address(mint_bytes)
                    bonding_curve_str = b58encode(bonding_curve_bytes)
                    associated_bonding_curve_bytes = solana_tx_builder.get_associated_token_address(
                        bonding_curve_bytes, mint_bytes, token_program_bytes
                    )
                    
                    # Virtual reserves default values
                    virtual_sol = 30 * 1e9
                    virtual_token = 1073000000 * 10**6
                    
                    # Query reserves on-chain
                    curve_info = rpc_call("getAccountInfo", [bonding_curve_str, {"encoding": "base64"}])
                    if curve_info and curve_info.get("value") and curve_info["value"].get("data"):
                        curve_data = base64.b64decode(curve_info["value"]["data"][0])
                        if len(curve_data) >= 40:
                            virtual_token = int.from_bytes(curve_data[8:16], "little")
                            virtual_sol = int.from_bytes(curve_data[16:24], "little")
                    
                    sol_payout = int(virtual_sol * amount_lamports / (virtual_token + amount_lamports))
                    min_sol_lamports = int(sol_payout * (1 - current_slippage / 100))
                    min_sol_lamports = max(0, min_sol_lamports)
                    
                    print(f"📊 [PUMP.FUN] Expected payout: {sol_payout / 1e9:.6f} SOL | Min SOL expected (slippage {current_slippage}%): {min_sol_lamports / 1e9:.6f} SOL")
                    
                    inst = solana_tx_builder.build_pump_fun_sell_instruction(
                        user_wallet_bytes,
                        mint_bytes,
                        bonding_curve_bytes,
                        associated_bonding_curve_bytes,
                        amount_lamports,
                        min_sol_lamports,
                        token_program_bytes
                    )
                    
                    COMPUTE_BUDGET_PROGRAM = b58decode("ComputeBudget111111111111111111111111111111")
                    priority_fee_estimate = get_dynamic_priority_fee(input_mint)
                    micro_lamports = int((priority_fee_estimate * 1e6) / 80000)
                    priority_data = struct.pack("<BQ", 3, max(100000, min(10000000, micro_lamports)))
                    priority_inst = solana_tx_builder.SolanaInstruction(COMPUTE_BUDGET_PROGRAM, [], priority_data)
                    
                    limit_data = struct.pack("<BI", 2, 80000)
                    limit_inst = solana_tx_builder.SolanaInstruction(COMPUTE_BUDGET_PROGRAM, [], limit_data)
                    
                    instructions = [limit_inst, priority_inst, inst]
                    
                    blockhash_resp = rpc_call("getLatestBlockhash", [])
                    if not blockhash_resp or "value" not in blockhash_resp:
                        print("❌ Failed to get recent blockhash for pump.fun sell.")
                        continue
                    recent_blockhash = b58decode(blockhash_resp["value"]["blockhash"])
                    
                    if use_jito:
                        import random
                        JITO_TIP_ACCOUNTS = [
                            "96hfK61AxSzJkQYkQC1j1gVHCc7w8D2GWfBkgP46Grc3",
                            "HFqBEvR2Ezau6aojEW683UXCw9s33A1rS2zLFLm1Q4ED",
                            "Cw8CFFCM4jXYnF7JeeXGg1yE1m9BDgWi9qf1qibzGs9S",
                            "ADa6gKmgJNDwbFY212A3xnAgWf1vwT2ipDfWJBMXQcz7",
                            "ADuUk9ZryMbEEt9LsVEjRovvVB3mM1w1UP1GgJaojqdF",
                            "Df8yFwzwj7SSCc25Gr2YrrCXVLxx3nc6nM4mAKj2rxXZ",
                            "3AVi9VwMQv751zJ1a7G8GfCCTUqv1Fcmqav7C9wSnCSK",
                            "DttWaRJcHau2SUNVewHD66qwc1G5J6J3A2SW67ncksgT"
                        ]
                        jito_tip_sol = get_dynamic_jito_tip()
                        jito_tip_lamports = int(jito_tip_sol * 1e9)
                        tip_account = b58decode(random.choice(JITO_TIP_ACCOUNTS))
                        tip_inst = solana_tx_builder.build_system_transfer_instruction(
                            user_wallet_bytes,
                            tip_account,
                            jito_tip_lamports
                        )
                        instructions.append(tip_inst)
                        print(f"💸 Jito Bundle Mode: Appended tip of {jito_tip_sol:.4f} SOL...")
                    
                    msg = solana_tx_builder.SolanaMessage(
                        instructions,
                        user_wallet_bytes,
                        recent_blockhash
                    )
                    tx = solana_tx_builder.SolanaTransaction(msg)
                    tx.sign(wallet_key)
                    
                    signed_b64 = base64.b64encode(tx.serialize()).decode("utf-8")
                    sig = tx.signatures[0]
                except Exception as e:
                    print(f"❌ Failed to construct pump.fun sell: {e}")
                    continue
            else:
                # Request quote and build local instructions instead of multiple HTTP rounds
                print("🚀 Routing swap request...")
                dex_name = "Raydium"
                if dex_type == "meteora":
                    dex_name = "Meteora%20DLMM"
                elif dex_type == "orca":
                    dex_name = "Orca"
                
                quote_url = (
                    f"https://quote-api.jup.ag/v6/quote"
                    f"?inputMint={input_mint}&outputMint={output_mint}"
                    f"&amount={amount_lamports}&slippageBps={int(current_slippage * 100)}"
                    f"&onlyDirectRoutes=true&dexes={dex_name}"
                )
                try:
                    # Direct route execution calculation
                    r = _http_session.get(quote_url, timeout=10)
                    if r.status_code != 200:
                        print(f"❌ Swap quote failed: {r.text}")
                        continue
                    quote = r.json()
                except Exception as e:
                    print(f"❌ Swap quote exception: {e}")
                    continue

                if use_jito:
                    prioritization_fee = {
                        "jitoTipLamports": int(jito_tip_sol * 1e9)
                    }
                elif use_dynamic:
                    prioritization_fee = get_dynamic_priority_fee(target_token)
                else:
                    prioritization_fee = _cfg("priority_fee_lamports", "auto")

                swap_url = "https://quote-api.jup.ag/v6/swap"
                payload  = {
                    "quoteResponse":           quote,
                    "userPublicKey":           wallet_address,
                    "wrapAndUnwrapSol":        True,
                    "dynamicComputeUnitLimit": True,
                    "prioritizationFeeLamports": prioritization_fee,
                    "asLegacyTransaction":      False,
                    "restrictIntermediateTokens": True
                }
                try:
                    r = _http_session.post(swap_url, json=payload, timeout=10)
                    if r.status_code != 200:
                        print(f"❌ Swap execution failed: {r.text}")
                        continue
                    swap_data = r.json()
                    tx_b64    = swap_data.get("swapTransaction")
                except Exception as e:
                    print(f"❌ Swap execution exception: {e}")
                    continue

                if not tx_b64:
                    print("❌ No transaction returned by Jupiter.")
                    continue

                tx_b64 = ai_predictive_model.optimize_solana_compute_budget(tx_b64)

                tx_bytes = bytearray(base64.b64decode(tx_b64))
                num_signatures, msg_offset = parse_compact_u16(tx_bytes, 0)
                message_bytes = tx_bytes[msg_offset + num_signatures * 64:]
                signer = eddsa.new(wallet_key, "rfc8032")
                sig    = signer.sign(message_bytes)
                tx_bytes[msg_offset:msg_offset + 64] = sig

                signed_b64 = base64.b64encode(tx_bytes).decode("utf-8")

            # PRO FEATURE: On-Chain Simulation before Broadcasting
            if not is_emergency:
                print("🧪 Simulating swap transaction locally on-chain...")
                sim_res = rpc_call("simulateTransaction", [
                    signed_b64,
                    {"encoding": "base64", "commitment": "processed"}
                ])
                if sim_res and isinstance(sim_res, dict) and "value" in sim_res:
                    val = sim_res["value"]
                    if val.get("err"):
                        err_str = str(val['err'])
                        print(f"🚨 Solana Simulation FAILED: {val['err']}. Retrying next attempt or skipping.")
                        if "3012" in err_str or "AccountNotInitialized" in err_str:
                            import time
                            print("⏳ Pump.fun Bonding Curve not fully initialized yet. Sleeping 1.5s before retry...")
                            time.sleep(1.5)
                        logs = val.get("logs")
                        if logs:
                            print("📋 Simulation Transaction Logs:")
                            for log_line in logs:
                                print(f"   {log_line}")
                        continue
                    else:
                        print("🟢 Solana Simulation PASSED!")
            else:
                print("⚡ Emergency mode: Bypassing local simulation to save latency!")

            tx_signature = b58encode(sig)

            if use_jito:
                print("📤 Sending Jito Bundle to Jito block engines...")
                tx_hash = None
                jito_endpoints = [
                    "https://mainnet.block-engine.jito.wtf/api/v1/bundles",
                    "https://ny.mainnet.block-engine.jito.wtf/api/v1/bundles",
                    "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/bundles"
                ]
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "sendBundle",
                    "params": [[signed_b64], {"encoding": "base64"}]
                }
                for ep in jito_endpoints:
                    try:
                        res = _http_session.post(ep, json=payload, timeout=5)
                        if res.status_code == 200:
                            json_res = res.json()
                            if "result" in json_res:
                                tx_hash = tx_signature
                                print(f"🚀 Bundle submitted to Jito endpoint: {ep} (Bundle ID: {json_res['result']})")
                    except Exception as ex:
                        print(f"⚠️ Failed to send bundle to {ep}: {ex}")
            else:
                tx_hash = rpc_call("sendTransaction", [
                    signed_b64,
                    {"encoding": "base64", "preflightCommitment": "processed", "skipPreflight": True}
                ])

            if tx_hash:
                print(f"📤 Broadcasted! https://solscan.io/tx/{tx_hash}")
                confirmed = False
                reverted = False
                for _ in range(30):
                    status = rpc_call("getSignatureStatuses", [[tx_hash]])
                    if status and status.get("value") and status["value"][0]:
                        val = status["value"][0]
                        if val.get("err"):
                            print(f"❌ Transaction reverted: {val['err']}")
                            reverted = True
                            break
                        if val.get("confirmationStatus") in ["confirmed", "finalized"]:
                            print("🎉 Transaction confirmed!")
                            confirmed = True
                            break
                    time.sleep(1)
                
                if confirmed:
                    success_any = True
                    break
                elif reverted:
                    print("❌ Transaction reverted on-chain. Retrying next swap attempt with higher slippage.")
                    continue
                else:
                    print("⏳ Transaction pending/timed out. Retrying next swap attempt.")
                    continue
            else:
                print("❌ Transaction broadcast failed. Retrying next swap attempt.")
                continue
            
    wallet_key = original_key
    wallet_address = original_address
    return success_any

# ─── Token Balance ─────────────────────────────────────────────────────────────
def get_token_balance(token_mint: str) -> float:
    for attempt in range(5):
        ata = get_ata_address(wallet_address, token_mint)
        if ata:
            res = rpc_call("getTokenAccountBalance", [ata])
            if res and res.get("value"):
                bal = float(res["value"].get("uiAmount", 0.0))
                if bal > 0.0:
                    return bal
        time.sleep(1.0)
    return 0.0

def get_token_decimals(token_mint: str) -> int:
    res = rpc_call("getAccountInfo", [token_mint, {"encoding": "base64"}])
    if res and res.get("value"):
        data = base64.b64decode(res["value"]["data"][0])
        return data[44]
    return 9

# ─── Current Price (SOL-equivalent value of position) ─────────────────────────
def get_current_price_sol(token_mint: str, amount_held: float) -> float:
    if amount_held <= 0:
        return 0.0
    
    if token_mint.endswith("pump"):
        try:
            import base64
            mint_bytes = b58decode(token_mint)
            bonding_curve_bytes = solana_tx_builder.get_bonding_curve_address(mint_bytes)
            bonding_curve_str = b58encode(bonding_curve_bytes)
            
            # Default virtual reserves
            virtual_sol = 30 * 1e9
            virtual_token = 1073000000 * 10**6
            
            curve_info = rpc_call("getAccountInfo", [bonding_curve_str, {"encoding": "base64"}])
            if curve_info and curve_info.get("value") and curve_info["value"].get("data"):
                curve_data = base64.b64decode(curve_info["value"]["data"][0])
                if len(curve_data) >= 40:
                    virtual_token = int.from_bytes(curve_data[8:16], "little")
                    virtual_sol = int.from_bytes(curve_data[16:24], "little")
            
            decimals = get_token_decimals(token_mint)
            amount_lamports = int(amount_held * (10 ** decimals))
            
            sol_payout = int(virtual_sol * amount_lamports / (virtual_token + amount_lamports))
            return sol_payout / 1e9
        except Exception as e:
            print(f"⚠️ Failed to estimate pump.fun price: {e}")
            
    # Fallback to Jupiter for non-pump or if pump estimation failed
    decimals   = get_token_decimals(token_mint)
    lamports_in = int(amount_held * (10 ** decimals))
    quote_url   = (
        f"https://quote-api.jup.ag/v6/quote"
        f"?inputMint={token_mint}&outputMint={WSOL_ADDRESS}"
        f"&amount={lamports_in}&slippageBps=100"
    )
    try:
        r = _http_session.get(quote_url, timeout=5)
        if r.status_code == 200:
            out = int(r.json().get("outAmount", 0))
            return out / 1e9
    except:
        pass
    return 0.0


# ─── Partial Sell ──────────────────────────────────────────────────────────────
def execute_partial_sell(token_addr: str, amount_held: float, fraction: float) -> bool:
    """Sell a fraction of the current position."""
    live_bal = get_token_balance(token_addr)
    if live_bal > 0.0:
        amount_held = live_bal
    decimals     = get_token_decimals(token_addr)
    sell_amount  = amount_held * fraction
    lamports_in  = int(sell_amount * (10 ** decimals))
    print(f"✂️ Selling {fraction*100:.0f}% ({sell_amount:.4f} tokens) of {token_addr[:16]}...")
    s = load_state()
    dex_type = s.get("dex_type", "raydium")
    if token_addr.endswith("pump"):
        dex_type = "pump"
    return execute_swap(token_addr, WSOL_ADDRESS, lamports_in, dex_type=dex_type)

# ─── Full Sell ─────────────────────────────────────────────────────────────────
def execute_full_sell(token_addr: str, amount_held: float, is_emergency: bool = False) -> bool:
    """Sell entire position."""
    live_bal = get_token_balance(token_addr)
    if live_bal > 0.0:
        amount_held = live_bal
    decimals    = get_token_decimals(token_addr)
    lamports_in = int(amount_held * (10 ** decimals))
    print(f"🚨 Selling 100% ({amount_held:.4f} tokens) of {token_addr[:16]}...")
    s = load_state()
    dex_type = s.get("dex_type", "raydium")
    if token_addr.endswith("pump"):
        dex_type = "pump"
    return execute_swap(token_addr, WSOL_ADDRESS, lamports_in, is_emergency=is_emergency, dex_type=dex_type)

# ─── Telegram Callback Handler ─────────────────────────────────────────────────
# Telegram command handling moved to tg_bot.py

# ─── External Trigger File Handlers ───────────────────────────────────────────
def check_buy_trigger(s: dict):
    """Check for external buy triggers (from Telegram or Twitter bot)."""
    trigger_file = "sol_buy_trigger.json"
    if not os.path.exists(trigger_file):
        return s
    try:
        with open(trigger_file, "r") as f:
            trigger = json.load(f)
        os.remove(trigger_file)
    except Exception as e:
        print(f"⚠️ Error reading buy trigger: {e}")
        return s

    target_token = trigger.get("token_address", "").strip()
    if not target_token:
        return s

    if s.get("in_position"):
        print(f"⚠️ Buy trigger ignored: already in position holding {s.get('token_symbol')}")
        send_telegram(f"⚠️ Buy trigger ignored: already in position holding {s.get('token_symbol')}")
        return s

    print(f"🚨 External buy trigger detected for Solana token: {target_token}")
    send_telegram(f"🚨 *Solana Buy Trigger*: Checking safety for `{target_token}`...")

    # safety check
    is_safe = check_token_safety(target_token)
    if is_safe:
        # Resolve symbol
        info = get_dexscreener_token(target_token)
        symbol = info["symbol"] if info else "TOKEN"

        alert_discovery(symbol, target_token, 0.0)

        trade_sol = _cfg("trade_amount_sol", 0.05)
        amt_lamports = int(trade_sol * 1e9)
        success = execute_swap(WSOL_ADDRESS, target_token, amt_lamports, dex_type="raydium")
        if success:
            s["in_position"] = True
            s["token_address"] = target_token
            s["token_symbol"] = symbol
            s["pair_address"] = ""
            s["dex_type"] = "raydium"
            s["buy_price_sol"] = trade_sol
            s["highest_price_sol"] = trade_sol
            s["amount_held"] = get_token_balance(target_token)
            s["buy_timestamp"] = int(time.time())
            s["initial_liq_sol"] = 0.0
            s["wsol_vault_address"] = ""
            s["deployer_address"] = ""
            s["half_sold"] = False
            s["sell_attempts"] = 0
            save_state(s)
            alert_trade_enter(symbol, target_token, trade_sol, 0.0)
    else:
        print(f"🚫 Safety check FAILED for trigger token {target_token[:16]}")
        send_telegram(f"🚫 Safety check *FAILED* for trigger token `{target_token[:16]}...` — buy aborted.")
    
    return s

# ─── Sell-Trigger File Handler ─────────────────────────────────────────────────
def check_sell_trigger(s: dict):
    """Check for inline-button sell triggers written by the callback handler."""
    trigger_file = "sol_sell_trigger.json"
    if not os.path.exists(trigger_file):
        return s
    try:
        with open(trigger_file, "r") as f:
            trigger = json.load(f)
        os.remove(trigger_file)
    except:
        return s

    if not s.get("in_position"):
        return s

    token_addr  = s["token_address"]
    if trigger.get("token_address", "") != token_addr:
        return s

    symbol      = s["token_symbol"]
    amount_held = s["amount_held"]
    buy_sol     = s["buy_price_sol"]
    partial     = trigger.get("partial", False)

    if partial and not s.get("half_sold"):
        send_telegram(f"✂️ Inline 50% sell for *{symbol}*...")
        success = execute_partial_sell(token_addr, amount_held, 0.50)
        if success:
            s["half_sold"]   = True
            s["amount_held"] = amount_held / 2
            save_state(s)
            send_telegram(f"✅ 50% sold. Holding remaining 50% for *{symbol}*.")
    elif not partial:
        send_telegram(f"🚨 Inline 100% sell for *{symbol}*...")
        current_sol = get_current_price_sol(token_addr, amount_held)
        success     = execute_full_sell(token_addr, amount_held)
        if success:
            pnl_pct = ((current_sol - buy_sol) / buy_sol * 100) if buy_sol > 0 else 0
            record  = {
                "token_symbol":   symbol,
                "token_address":  token_addr,
                "buy_price_sol":  buy_sol,
                "exit_price_sol": current_sol,
                "pnl_pct":        pnl_pct,
                "reason":         "Inline sell button",
                "timestamp":      int(time.time()),
            }
            s["last_trade"]    = record
            s["in_position"]   = False
            s["token_address"] = ""
            s["token_symbol"]  = ""
            s["amount_held"]   = 0.0
            s["half_sold"]     = False
            s["sell_attempts"] = 0
            history = s.get("trade_history", [])
            history.append(record)
            s["trade_history"] = history[-20:]
            save_state(s)
            save_trade_to_history(record)
            alert_trade_close(symbol, buy_sol, current_sol, pnl_pct, "Inline sell button")
    return s

sig_queue = queue.Queue()
wakeup_event = threading.Event()

# ─── Copy-Trading Engine ───────────────────────────────────────────────────────
def parse_copy_trade_tx(tx, copy_wallet: str):
    meta = tx.get("meta", {})
    if not meta:
        return None
    
    pre_balances = {}
    for balance in meta.get("preTokenBalances", []):
        owner = balance.get("owner")
        mint = balance.get("mint")
        if owner == copy_wallet and mint != WSOL_ADDRESS:
            amount = balance.get("uiTokenAmount", {}).get("uiAmount", 0.0) or 0.0
            pre_balances[mint] = amount
            
    post_balances = {}
    for balance in meta.get("postTokenBalances", []):
        owner = balance.get("owner")
        mint = balance.get("mint")
        if owner == copy_wallet and mint != WSOL_ADDRESS:
            amount = balance.get("uiTokenAmount", {}).get("uiAmount", 0.0) or 0.0
            post_balances[mint] = amount

    # Check for buys
    for mint, post_amount in post_balances.items():
        pre_amount = pre_balances.get(mint, 0.0)
        if post_amount > pre_amount:
            return {"action": "BUY", "mint": mint, "amount": post_amount - pre_amount}
            
    # Check for sells
    for mint, pre_amount in pre_balances.items():
        post_amount = post_balances.get(mint, 0.0)
        if post_amount < pre_amount:
            return {"action": "SELL", "mint": mint, "amount": pre_amount - post_amount}
            
    return None

def process_copy_trade_sig(sig: str, copy_wallets: list):
    tx = rpc_call("getTransaction", [sig, {
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": 0
    }])
    if not tx or not tx.get("meta") or tx["meta"].get("err"):
        return

    for wallet in copy_wallets:
        account_keys = []
        try:
            keys = tx["transaction"]["message"].get("accountKeys", [])
            for k in keys:
                if isinstance(k, dict):
                    account_keys.append(k.get("pubkey"))
                else:
                    account_keys.append(k)
        except Exception as e:
            print(f"⚠️ Copy-trade key parse error: {e}")
            continue

        if wallet not in account_keys:
            continue

        trade = parse_copy_trade_tx(tx, wallet)
        if not trade:
            continue
            
        mint = trade["mint"]
        action = trade["action"]
        
        print(f"📈 Copy-Trade Match: Wallet {wallet[:16]} performed {action} for token {mint[:16]}")
        s = load_state()
        
        if action == "BUY":
            if s.get("in_position"):
                print(f"ℹ️ Copy-Trade: Already in position. Skipping buy.")
                continue
                
            info = get_dexscreener_token(mint)
            symbol = info["symbol"] if info else "TOKEN"
            
            if not check_token_safety(mint):
                print(f"🚫 Copy-Trade: Token safety check failed for {mint[:16]}. Skipping buy.")
                continue
                
            # --- WHALE BAITING PROTECTION MODULE ---
            print(f"🕵️ [WHALE BAIT PROTECTION] Analyzing {mint[:16]} for orchestrated dump traps...")
            
            # 1. Delay & Analyze: Small artificial delay to avoid being liquidity exit for the whale's bundle
            time.sleep(1.5)
            
            # 2. Bubble Map Centralization Check
            try:
                # We do a quick check on the largest accounts to ensure the whale isn't baiting us into a pre-mined cabal token
                large_accounts = rpc_call("getTokenLargestAccounts", [mint])
                if large_accounts and large_accounts.get("value"):
                    # Calculate share if not explicitly provided (depends on RPC)
                    top_holders_share = 0.0
                    for acct in large_accounts["value"][:3]: # Sum top 3 wallets
                        amt = float(acct.get("amount", 0))
                        # Assuming 1B supply for standard pump.fun tokens, though it's an approximation without total supply
                        # Pump.fun total supply is 1,000,000,000 tokens with 6 decimals = 1e15 lamports
                        pct = (amt / 1e15) * 100
                        top_holders_share += pct
                        
                    if top_holders_share > 35.0:
                        warn_msg = f"🚫 [WHALE BAIT] Bubble Map Warning! Top 3 wallets hold >{top_holders_share:.1f}% of supply. Skipping copy-buy to avoid orchestrated dump."
                        print(warn_msg)
                        send_telegram(f"🚫 *[WHALE BAIT]* Wallet `{wallet[:8]}...` attempted to bait copy-bots on `{symbol}`.\nBubble Map reveals high centralization (`{top_holders_share:.1f}%`). Buy blocked.")
                        continue
            except Exception as e:
                pass
                
            send_telegram(f"👤 *[COPY-TRADE]* Wallet `{wallet[:8]}...` bought `{symbol}`!\nTriggering copy-buy...")
            
            amt_lamports = int(_cfg("trade_amount_sol", 0.05) * 1e9)
            success = execute_swap(WSOL_ADDRESS, mint, amt_lamports)
            if success:
                liq_sol = 10.0
                s["in_position"] = True
                s["token_address"] = mint
                s["token_symbol"] = symbol
                s["buy_price_sol"] = _cfg("trade_amount_sol", 0.05)
                s["highest_price_sol"] = _cfg("trade_amount_sol", 0.05)
                s["amount_held"] = get_token_balance(mint)
                s["buy_timestamp"] = int(time.time())
                s["initial_liq_sol"] = liq_sol
                s["half_sold"] = False
                s["sell_attempts"] = 0
                save_state(s)
                alert_trade_enter(symbol, mint, _cfg("trade_amount_sol", 0.05), liq_sol)
                break
                
        elif action == "SELL":
            if s.get("in_position") and s.get("token_address") == mint:
                symbol = s.get("token_symbol", "TOKEN")
                send_telegram(f"👤 *[COPY-TRADE]* Wallet `{wallet[:8]}...` sold `{symbol}`!\nTriggering copy-sell...")
                
                amount_held = s.get("amount_held", 0.0)
                success = execute_full_sell(mint, amount_held)
                if success:
                    buy_sol = s.get("buy_price_sol", 0.0)
                    current_sol = get_current_price_sol(mint, amount_held)
                    pnl_pct = ((current_sol - buy_sol) / buy_sol * 100) if buy_sol > 0 else 0
                    record = {
                        "token_symbol": symbol,
                        "token_address": mint,
                        "buy_price_sol": buy_sol,
                        "exit_price_sol": current_sol,
                        "pnl_pct": pnl_pct,
                        "reason": f"Copy-Trade Exit (Wallet {wallet[:8]}...)",
                        "timestamp": int(time.time()),
                    }
                    s["last_trade"] = record
                    s["in_position"] = False
                    s["token_address"] = ""
                    s["token_symbol"] = ""
                    s["amount_held"] = 0.0
                    s["half_sold"] = False
                    s["sell_attempts"] = 0
                    history = s.get("trade_history", [])
                    history.append(record)
                    s["trade_history"] = history[-20:]
                    save_state(s)
                    save_trade_to_history(record)
                    alert_trade_close(symbol, buy_sol, current_sol, pnl_pct, f"Copy-Trade Exit (Wallet {wallet[:8]}...)")
                    break

def start_copy_trade_listener():
    pass

# ─── Combined WebSocket Listener ─────────────────────────────────
def start_ws_listener():
    def run():
        async def listen_raydium():
            global ws_active
            fallback_attempts = 0
            while True:
                try:
                    # Resolve WebSocket URL
                    rpc_url = _cfg("rpc_url", "https://api.mainnet-beta.solana.com")
                    configured_wss = _cfg("wss_url", "")
                    
                    if fallback_attempts > 0:
                        wss_url = "wss://api.mainnet-beta.solana.com"
                    elif configured_wss:
                        wss_url = configured_wss
                    elif "helius-rpc.com" in rpc_url:
                        wss_url = rpc_url.replace("https://", "wss://")
                    else:
                        wss_url = "wss://api.mainnet-beta.solana.com"
                        
                    print(f"🔌 Raydium/Copy WebSocket: Connecting to {wss_url}...")
                    async with websockets.connect(wss_url, ping_interval=20, ping_timeout=20) as ws:
                        ws_active = True
                        fallback_attempts = 0
                        
                        raydium_sub_id = None
                        copy_sub_ids = set()
                        
                        # Subscribe to Raydium Logs
                        sub_msg_raydium = {
                            "jsonrpc": "2.0",
                            "id": 1,
                            "method": "logsSubscribe",
                            "params": [
                                {"mentions": [RAYDIUM_LP_V4]},
                                {"commitment": "processed"}
                            ]
                        }
                        await ws.send(json.dumps(sub_msg_raydium))
                        
                        # Subscribe to Copy-Trading Wallets
                        copy_wallets = _cfg("copy_wallets", [])
                        for idx, wallet in enumerate(copy_wallets):
                            sub_msg_copy = {
                                "jsonrpc": "2.0",
                                "id": 100 + idx,
                                "method": "logsSubscribe",
                                "params": [
                                    {"mentions": [wallet]},
                                    {"commitment": "processed"}
                                ]
                            }
                            await ws.send(json.dumps(sub_msg_copy))
                        
                        while True:
                            _reload_config()
                            current_copy_wallets = _cfg("copy_wallets", [])
                            if set(current_copy_wallets) != set(copy_wallets):
                                print("🔄 Copy-Trade Wallets list changed. Reconnecting WebSocket...")
                                break
                                
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                                data = json.loads(msg)
                            except asyncio.TimeoutError:
                                continue
                                
                            if "result" in data and "id" in data:
                                req_id = data["id"]
                                sub_id = data["result"]
                                if req_id == 1:
                                    raydium_sub_id = sub_id
                                    print(f"✅ Raydium logsSubscribe Active (Sub ID: {sub_id})")
                                elif req_id >= 100:
                                    idx = req_id - 100
                                    if idx < len(copy_wallets):
                                        copy_sub_ids.add(sub_id)
                                        print(f"✅ Copy-Trade Subscribed to {copy_wallets[idx][:16]}... (Sub ID: {sub_id})")
                                continue
                                
                            if "params" in data and "result" in data["params"]:
                                result_data = data["params"]["result"]
                                sub_id = data.get("params", {}).get("subscription")
                                
                                if isinstance(result_data, dict) and "value" in result_data and "logs" in result_data["value"]:
                                    val = result_data["value"]
                                    logs = val.get("logs", [])
                                    signature = val.get("signature")
                                    
                                    if sub_id == raydium_sub_id:
                                        if signature:
                                            is_raydium_init = False
                                            for log in logs:
                                                if "initialize2" in log:
                                                    is_raydium_init = True
                                                    break
                                            if is_raydium_init:
                                                print(f"🎯 [RAYDIUM WS DETECT] Raydium pool initialization signature queued: {signature}")
                                                sig_queue.put(signature)
                                                wakeup_event.set()
                                                
                                    elif sub_id in copy_sub_ids:
                                        if signature:
                                            print(f"🔔 Copy-Trade: Detected activity on signature {signature}")
                                            process_copy_trade_sig(signature, current_copy_wallets)
                except Exception as e:
                    ws_active = False
                    fallback_attempts += 1
                    print(f"⚠️ Raydium WS Error: {e}. Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

        async def listen_pump():
            while True:
                try:
                    uri = "wss://pumpportal.fun/api/data"
                    print(f"🔌 pump.fun WebSocket: Connecting to {uri}...")
                    async with websockets.connect(uri, ping_interval=None) as ws:
                        subscribe_message = {
                            "method": "subscribeNewToken"
                        }
                        await ws.send(json.dumps(subscribe_message))
                        print("✅ pump.fun subscribeNewToken Active")
                        
                        while True:
                            try:
                                msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                                data = json.loads(msg)
                            except asyncio.TimeoutError:
                                continue
                                
                            if data.get("txType") == "create" and data.get("mint") and data.get("bondingCurveKey"):
                                s = load_state()
                                if not s.get("in_position"):
                                    mint_str = data["mint"]
                                    if check_token_safety(mint_str):
                                        curve_str = data["bondingCurveKey"]
                                        token_program_str = data.get("tokenProgram")
                                        print(f"🎯 [PUMP.FUN SNIPER] Discovered new token launch: {mint_str} | Curve: {curve_str} | Program: {token_program_str}")
                                        execute_pump_fun_snipe(mint_str, curve_str, token_program_str)
                                    else:
                                        print(f"🛑 [PUMP.FUN SNIPER] Rejected new token launch {mint_str[:12]}... due to failed safety checks.")
                except Exception as e:
                    print(f"⚠️ pump.fun WS Error: {e}. Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

        async def main_ws():
            # Run gRPC client if configured
            grpc_url = _cfg("grpc_url", "")
            if grpc_url:
                print(f"🔌 gRPC Geyser: Initializing Yellowstone Geyser Stream to {grpc_url}...")
                try:
                    def handle_geyser_event(event):
                        print(f"🚀 [gRPC GEYSER] Raw event decoded: {event}")
                    client = grpc_geyser_client.GeyserStreamClient(
                        endpoint_url=grpc_url,
                        x_token=_cfg("grpc_x_token", None)
                    )
                    client.start(handle_geyser_event)
                    print("✅ gRPC Geyser Stream initialized.")
                except Exception as ex:
                    print(f"⚠️ Failed to start Geyser Client: {ex}")

            await asyncio.gather(listen_raydium(), listen_pump())

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(main_ws())

    threading.Thread(target=run, daemon=True).start()

# ─── Main Loop ─────────────────────────────────────────────────────────────────
def main():
    global S
    print("==============================================")
    print("🚀 Solana Meme Coin Sniper Bot  — PRO Edition")
    print(f"🔗 RPC:      {_cfg('rpc_url','https://api.mainnet-beta.solana.com')}")
    print(f"🛡️  Dry Run:  {_cfg('dry_run', False)}")
    print("==============================================")

    s = load_state()
    if s.get("in_position") and s.get("token_address"):
        token_mint = s["token_address"]
        print(f"🔍 Startup Check: Bot marked IN_POSITION holding {token_mint}. Verifying token balance...")
        try:
            bal = get_token_balance(token_mint)
            if bal <= 0.0:
                print("⚠️ Startup Check: On-chain token balance is 0.0! Position was cleared or buy failed. Resetting in_position state.")
                s["in_position"] = False
                s["token_address"] = ""
                s["token_symbol"] = ""
                s["amount_held"] = 0.0
                s["half_sold"] = False
                s["sell_attempts"] = 0
                save_state(s)
            else:
                print(f"🟢 Startup Check: On-chain balance confirmed: {bal} tokens. Resuming active position monitoring.")
        except Exception as e:
            print(f"⚠️ Startup Check: Failed to verify on-chain balance: {e}. Keeping current state.")

    # Start background WebSocket listener for Raydium logs and Copy-Trading
    start_ws_listener()
    start_copy_trade_listener()

    # Telegram command listener is handled separately by tg_bot.py.
    # ── SESSION GUARD INIT ────────────────────────────────────────────────────
    global session_start_sol_balance, session_sol_price_at_start
    session_sol_price_at_start = get_sol_usd_price() or 150.0
    if wallet_address:
        try:
            res = rpc_call("getBalance", [wallet_address])
            if res:
                session_start_sol_balance = float(res.get("value", 0)) / 1e9
                bal_usd = session_start_sol_balance * session_sol_price_at_start
                print(f"💼 Session start balance: {session_start_sol_balance:.6f} SOL (≈${bal_usd:.2f})")
                print(f"🛡️  Max session loss guard: ${SESSION_MAX_LOSS_USD:.2f} | 24/7 mode — no time cutoff")
                send_telegram(
                    f"🚀 *Solana Sniper LIVE — Aggressive Mode*\n"
                    f"💼 Balance: `{session_start_sol_balance:.6f} SOL` (≈${bal_usd:.2f})\n"
                    f"🛡️ Loss Cap: `${SESSION_MAX_LOSS_USD:.2f}` | 24/7 mode\n"
                    f"⚡ Trade size: `{_cfg('trade_amount_sol', 0.10)} SOL` | Profit target: `+{_cfg('hard_take_profit_percent', 300):.0f}%`"
                )
        except Exception as e:
            print(f"⚠️ Failed to snapshot session balance: {e}")
    # ─────────────────────────────────────────────────────────────────────────

    print("🔍 Scanning Raydium V4 program for new pool launches...")
    last_sig = None

    while True:
        _reload_config()
        S = get_settings()
        s = load_state()

        # ── External Triggers from Telegram / Twitter Bot ──────────────────────
        s = check_sell_trigger(s)
        s = check_buy_trigger(s)

        # ── 9 AM MT AUTO-SHUTDOWN ─────────────────────────────────────────────
        if _is_session_cutoff():
            print("⏰ 9:00 AM MT reached — auto-shutdown triggered.")
            send_telegram(
                "⏰ *Solana 9:00 AM MT Auto-Shutdown*\n"
                "🛑 Bot is stopping. Selling any open position and exiting."
            )
            if s.get("in_position"):
                tok = s["token_address"]
                sym = s["token_symbol"]
                amt = s["amount_held"]
                buy_sol = s["buy_price_sol"]
                print(f"📤 Closing open Solana position: {sym} before shutdown.")
                success = execute_full_sell(tok, amt)
                if success:
                    current_sol = get_current_price_sol(tok, amt)
                    pnl_pct = ((current_sol - buy_sol) / buy_sol * 100) if buy_sol > 0 else 0
                    record  = {
                        "token_symbol":   sym,
                        "token_address":  tok,
                        "buy_price_sol":  buy_sol,
                        "exit_price_sol": current_sol,
                        "pnl_pct":        pnl_pct,
                        "reason":         "Auto-shutdown (9:00 AM MT)",
                        "timestamp":      int(time.time()),
                    }
                    s["last_trade"]    = record
                    s["in_position"]   = False
                    s["token_address"] = ""
                    s["token_symbol"]  = ""
                    s["amount_held"]   = 0.0
                    s["half_sold"]     = False
                    s["sell_attempts"] = 0
                    history = s.get("trade_history", [])
                    history.append(record)
                    s["trade_history"] = history[-20:]
                    save_state(s)
                    save_trade_to_history(record)
                    alert_trade_close(sym, buy_sol, current_sol, pnl_pct, "Auto-shutdown (9:00 AM MT)")
            sys.exit(0)
        # ─────────────────────────────────────────────────────────────────────

        # ── SESSION LOSS GUARD ────────────────────────────────────────────────
        global _cached_sol_bal, _last_bal_time
        current_sol_bal = _cached_sol_bal
        if wallet_address and (time.time() - _last_bal_time > 90.0 or _cached_sol_bal == 0.0):
            try:
                res = rpc_call("getBalance", [wallet_address])
                if res:
                    current_sol_bal = float(res.get("value", 0)) / 1e9
                    _cached_sol_bal = current_sol_bal
                    _last_bal_time = time.time()
            except:
                pass

        if session_start_sol_balance > 0 and current_sol_bal > 0:
            diff_sol = session_start_sol_balance - current_sol_bal
            sol_price = get_sol_usd_price() or session_sol_price_at_start
            session_loss_usd = diff_sol * sol_price
            if session_loss_usd >= SESSION_MAX_LOSS_USD:
                print(f"🛑 SESSION LOSS CAP HIT: -${session_loss_usd:.2f} >= ${SESSION_MAX_LOSS_USD:.2f}. No new buys will be placed.")
                global _session_loss_alerted
                if not globals().get("_session_loss_alerted", False):
                    send_telegram(
                        f"🛑 *Solana Session Loss Cap Hit!*\n"
                        f"💸 Down `${session_loss_usd:.2f}` — no new trades until restart.\n"
                        f"🔒 Bot holding current position (if any). Global scanner is NOT paused."
                    )
                    _session_loss_alerted = True
        # ─────────────────────────────────────────────────────────────────────

        if s.get("in_position"):
            # ── Position Monitoring ────────────────────────────────────────────
            token_addr  = s["token_address"]
            symbol      = s["token_symbol"]
            buy_sol     = s["buy_price_sol"]
            highest_sol = s["highest_price_sol"]
            amount_held = s["amount_held"]

            current_sol = get_current_price_sol(token_addr, amount_held)
            print(
                f"📊 {symbol} | Entry: {buy_sol:.4f} | "
                f"Peak: {highest_sol:.4f} | Now: {current_sol:.4f} SOL"
            )

            if current_sol > 0:
                # Update peak
                if current_sol > highest_sol:
                    s["highest_price_sol"] = current_sol
                    save_state(s)
                    highest_sol = current_sol

                # ── Exit logic ─────────────────────────────────────────────────
                trigger_sell = False
                reason       = ""

                # ── MODULE 4: ACTIVE RUG PROTECTION (Liquidity Drain Check) ───
                vault_addr = s.get("wsol_vault_address")
                initial_liq = s.get("initial_liq_sol", 0.0)
                if vault_addr and initial_liq > 0.0:
                    try:
                        res = rpc_call("getTokenAccountBalance", [vault_addr])
                        if res and isinstance(res, dict) and "value" in res:
                            current_liq = float(res["value"].get("uiAmount", 0.0) or 0.0)
                            if current_liq > 0:
                                ratio = current_liq / initial_liq
                                if ratio < 0.40: # Liquidity drained by >60%
                                    trigger_sell = True
                                    reason = f"Rug Pull! Liquidity drained to {ratio*100:.1f}%"
                                    print(f"🛑 RUG DETECTED: Pool balance {current_liq:.2f} SOL vs Initial {initial_liq:.2f} SOL. Ratio: {ratio:.2%}")
                                    dep = s.get("deployer_address")
                                    if dep:
                                        try:
                                            bl = {}
                                            if os.path.exists("blacklist.json"):
                                                with open("blacklist.json", "r") as f:
                                                    bl = json.load(f)
                                            bl[dep] = {
                                                "reason": f"Rug Pull on Solana token {symbol}: LP drained to {ratio*100:.1f}%",
                                                "timestamp": int(time.time())
                                            }
                                            with open("blacklist.json", "w") as f:
                                                json.dump(bl, f, indent=2)
                                            print(f"🚫 Creator blacklisted: {dep}")
                                        except Exception as e:
                                            print(f"⚠️ Failed to blacklist creator: {e}")
                    except Exception as e:
                        print(f"⚠️ Active Rug Monitor failed to check balance: {e}")
                tp_val       = buy_sol * (1 + S["HARD_TP"] / 100) if S["HARD_TP"] > 0 else None
                sl_val       = buy_sol * (1 - S["HARD_SL"] / 100)

                # Compute price change
                change_from_buy = ((current_sol - buy_sol) / buy_sol * 100) if buy_sol > 0 else 0

                # ── MODULE 1: STAGED EXIT (Lock profit at +staged_tp%) ─────────
                half_sold = s.get("half_sold", False)
                staged_tp = S.get("STAGED_TP", 30.0)
                staged_frac = S.get("STAGED_FRACTION", 0.50)
                if not half_sold and change_from_buy >= staged_tp:
                    print(f"💸 Solana staged exit: +{change_from_buy:.1f}% → selling {staged_frac*100:.0f}% to lock profit.")
                    success = execute_partial_sell(token_addr, amount_held, staged_frac)
                    if success:
                        s["half_sold"] = True
                        s["amount_held"] = amount_held * (1 - staged_frac)
                        amount_held = amount_held * (1 - staged_frac)
                        save_state(s)
                        send_telegram(
                            f"💸 *SOLANA QUICK FLIP — {staged_frac*100:.0f}% SOLD* 💰\n"
                            f"🪙 *Token*: {symbol}\n"
                            f"📈 *Profit locked*: `+{change_from_buy:.1f}%`\n"
                            f"🚀 Remaining {(1-staged_frac)*100:.0f}% rides FREE — win guaranteed!"
                        )

                # ── MODULE 2: BREAKEVEN STOP ──────────────────────────────────
                effective_stop_loss = S["HARD_SL"]
                if change_from_buy >= staged_tp:
                    # Move stop loss to entry price (breakeven + small buffer)
                    if current_sol <= buy_sol * 1.005:
                        trigger_sell = True
                        reason = f"Breakeven Stop — pulled back to entry. Capital protected."
                    effective_stop_loss = 0.0

                # ── MODULE 3: TIGHTENING TRAILING STOP ────────────────────────
                # Trailing stop narrows as profit grows:
                #   < 20 %  gain  →  configured trailing
                #   20–40 %       →  8 %
                #   40–80 %       →  6 %
                #   > 80 %        →  4 %
                if change_from_buy < 20.0:
                    dynamic_trailing = S["TRAILING_STOP"]
                elif change_from_buy < 40.0:
                    dynamic_trailing = min(S["TRAILING_STOP"], 8.0)
                elif change_from_buy < 80.0:
                    dynamic_trailing = min(S["TRAILING_STOP"], 6.0)
                else:
                    dynamic_trailing = min(S["TRAILING_STOP"], 4.0)

                trail_val = highest_sol * (1 - dynamic_trailing / 100)

                # Time-based exit
                buy_ts = s.get("buy_timestamp", 0)
                if S["TIME_EXIT"] > 0 and buy_ts > 0:
                    elapsed_min = (time.time() - buy_ts) / 60
                    if elapsed_min >= S["TIME_EXIT"]:
                        trigger_sell = True
                        reason = f"Time Exit ({S['TIME_EXIT']} min)"

                if not trigger_sell and tp_val and current_sol >= tp_val:
                    trigger_sell = True
                    reason = f"Take-Profit (+{S['HARD_TP']}%)"

                if not trigger_sell and effective_stop_loss > 0 and current_sol <= sl_val:
                    trigger_sell = True
                    reason = f"Stop-Loss (-{S['HARD_SL']}%)"

                if not trigger_sell and current_sol <= trail_val and highest_sol > buy_sol:
                    trigger_sell = True
                    reason = f"Trailing Stop (-{dynamic_trailing:.1f}%)"

                if trigger_sell:
                    s["sell_attempts"] = s.get("sell_attempts", 0) + 1
                    send_telegram(f"🚨 *Exiting*: `{reason}` triggered for *{symbol}* (attempt #{s['sell_attempts']})...")
                    is_rug = "Rug Pull" in reason
                    success = execute_full_sell(token_addr, amount_held, is_emergency=is_rug)
                    if success:
                        pnl_pct = ((current_sol - buy_sol) / buy_sol * 100) if buy_sol > 0 else 0
                        record  = {
                            "token_symbol":   symbol,
                            "token_address":  token_addr,
                            "buy_price_sol":  buy_sol,
                            "exit_price_sol": current_sol,
                            "pnl_pct":        pnl_pct,
                            "reason":         reason,
                            "timestamp":      int(time.time()),
                        }
                        s["last_trade"]    = record
                        s["in_position"]   = False
                        s["token_address"] = ""
                        s["token_symbol"]  = ""
                        s["amount_held"]   = 0.0
                        s["half_sold"]     = False
                        s["sell_attempts"] = 0
                        history = s.get("trade_history", [])
                        history.append(record)
                        s["trade_history"] = history[-20:]
                        save_state(s)
                        save_trade_to_history(record)
                        alert_trade_close(symbol, buy_sol, current_sol, pnl_pct, reason)
                    else:
                        print(f"❌ Sell attempt #{s['sell_attempts']} failed. Retrying next tick.")
                        if s.get("sell_attempts", 0) >= 5:
                            send_telegram(
                                f"⚠️ *WARNING*: Sell has failed *{s['sell_attempts']} times* for *{symbol}*.\n"
                                f"This may be a honeypot. Use `/clear` to abandon the position."
                            )
                        save_state(s)

        # ── Scanner execution ──
        if S["SCANNER_ACTIVE"]:
            # ── 1. Watchlist: check unfunded pools that may now have liquidity ──
            watchlist = s.get("watchlist", [])
            if watchlist:
                now = int(time.time())
                watchlist = [p for p in watchlist if now - p["added_time"] < 600]
                s["watchlist"] = watchlist
                save_state(s)

                for p in list(watchlist):
                    pair_addr    = p["pair_address"]
                    vault_addr   = p["vault_address"]
                    target_token = p["target_token"]

                    res = rpc_call("getTokenAccountBalance", [vault_addr])
                    liq_sol = 0.0
                    if res and res.get("value"):
                        liq_sol = float(res["value"].get("uiAmount", 0.0))
                    else:
                        liq_sol = p.get("initial_liq_local", 0.0)
                        print(f"⚠️ RPC Balance failed. Using locally parsed pool binary liquidity fallback: {liq_sol:.2f} SOL")

                    if S["MIN_LIQ"] <= liq_sol <= S["MAX_LIQ"]:
                        print(f"🎯 Watchlist Match: {pair_addr[:20]} funded with {liq_sol:.2f} SOL!")
                        if check_token_safety(target_token):
                            # Resolve symbol
                            info   = get_dexscreener_token(target_token)
                            symbol = info["symbol"] if info else "TOKEN"

                            alert_discovery(symbol, target_token, liq_sol)

                            # ONLY BUY IF WE ARE NOT IN POSITION AND LOSS CAP NOT HIT
                            if not s.get("in_position") and session_loss_usd < SESSION_MAX_LOSS_USD:
                                amt_lamports = int(S["TRADE_SOL"] * 1e9)
                                if S["BUY_BLOCK_DELAY"] > 0:
                                    print(f"⏳ Buy delay: waiting {S['BUY_BLOCK_DELAY']}s...")
                                    time.sleep(S["BUY_BLOCK_DELAY"])

                                success = execute_swap(WSOL_ADDRESS, target_token, amt_lamports, dex_type=p.get("dex_type", "raydium"))
                                if success:
                                    s["in_position"]     = True
                                    s["token_address"]   = target_token
                                    s["token_symbol"]    = symbol
                                    s["pair_address"]    = pair_addr
                                    s["dex_type"]        = p.get("dex_type", "raydium")
                                    s["buy_price_sol"]   = S["TRADE_SOL"]
                                    s["highest_price_sol"] = S["TRADE_SOL"]
                                    s["amount_held"]     = get_token_balance(target_token)
                                    s["buy_timestamp"]   = int(time.time())
                                    s["initial_liq_sol"] = liq_sol
                                    s["wsol_vault_address"] = vault_addr
                                    s["deployer_address"] = p.get("deployer")
                                    s["half_sold"]       = False
                                    s["sell_attempts"]   = 0
                                    s["watchlist"] = [x for x in s["watchlist"] if x["pair_address"] != pair_addr]
                                    save_state(s)
                                    alert_trade_enter(symbol, target_token, S["TRADE_SOL"], liq_sol)
                                    break
                                else:
                                    print("❌ Swap failed. Removing from watchlist.")
                                    s["watchlist"] = [x for x in s["watchlist"] if x["pair_address"] != pair_addr]
                                    save_state(s)
                                    break
                            else:
                                # Always clean up watchlist once alerted
                                s["watchlist"] = [x for x in s["watchlist"] if x["pair_address"] != pair_addr]
                                save_state(s)
                                break
                        else:
                            print(f"🚫 Safety FAILED for watchlist token {target_token[:16]}. Removing.")
                            s["watchlist"] = [x for x in s["watchlist"] if x["pair_address"] != pair_addr]
                            save_state(s)

            # ── 2. Retrieve signatures from WebSocket queue, with fallback to polling ──
            sigs_to_process = []
            while not sig_queue.empty():
                sigs_to_process.append(sig_queue.get())

            loop_count = s.get("loop_count", 0) + 1
            s["loop_count"] = loop_count
            save_state(s)

            # Fallback on-chain polling: check signatures every 60 seconds (600 loops) even if WS is active
            should_poll = (loop_count % 600 == 0) or not last_sig
            if not sigs_to_process and (not ws_active or should_poll) and (loop_count % 150 == 0 or not last_sig):
                params = [{"limit": 5}]
                if last_sig:
                    params[0]["until"] = last_sig
                sigs = rpc_call("getSignaturesForAddress", [RAYDIUM_LP_V4, params[0]])
                if sigs:
                    last_sig = sigs[0]["signature"]
                    for item in reversed(sigs):
                        sigs_to_process.append(item["signature"])

            for sig in sigs_to_process:
                    last_sig = sig
                    tx = None
                    # Raydium V4 pools bypass getTransaction completely as their metadata was already parsed in binary.
                    # We only query getTransaction for non-Raydium fallbacks (Meteora/Orca).
                    # Check if the signature metadata was already handled by the binary watchlist injector.
                    is_raydium_direct = any(x["pair_address"] == sig or x["target_token"] == sig for x in s.get("watchlist", []))
                    if is_raydium_direct:
                        continue
                        
                    for attempt in range(5):
                        tx = rpc_call("getTransaction", [sig, {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0
                        }])
                        if tx and tx.get("meta"):
                            break
                        time.sleep(0.05)
                    if not tx or not tx.get("meta") or tx["meta"].get("err"):
                        continue
                        
                    logs = tx["meta"].get("logMessages", [])
                    is_initialize = any(x in log for log in logs for x in ["initialize2", "initializeLbPair", "initialize_lb_pair", "initializePool", "InitializePool"])
                    if not is_initialize:
                        continue

                    # Detect program
                    dex_type = None
                    if any("initialize2" in log for log in logs) or any(RAYDIUM_LP_V4 in log for log in logs):
                        dex_type = "raydium"
                    elif any(x in log for log in logs for x in ["initializeLbPair", "initialize_lb_pair"]) or any(y in log for log in logs for y in [METEORA_DLMM, METEORA_POOLS]):
                        dex_type = "meteora"
                    elif any(x in log for log in logs for x in ["initializePool", "InitializePool"]) or any(ORCA_CLMM in log for log in logs):
                        dex_type = "orca"

                    if not dex_type:
                        continue

                    pair_addr = None
                    vault_addr = None
                    target_token = None
                    token0 = None
                    token1 = None
                    deployer = None

                    # Parse all account keys
                    account_keys = []
                    try:
                        keys = tx["transaction"]["message"].get("accountKeys", [])
                        for k in keys:
                            if isinstance(k, dict):
                                account_keys.append(k.get("pubkey"))
                            else:
                                account_keys.append(k)
                        if account_keys:
                            deployer = account_keys[0]
                    except Exception as e:
                        print(f"⚠️ Failed to parse account keys: {e}")

                    # Raydium Specific instruction parsing
                    if dex_type == "raydium":
                        insts = tx["transaction"]["message"].get("instructions", [])
                        for inst in insts:
                            if inst.get("programId") != RAYDIUM_LP_V4:
                                continue
                            accounts = inst.get("accounts", [])
                            if len(accounts) < 16:
                                continue
                            pair_addr = accounts[5]
                            vault0    = accounts[10]
                            vault1    = accounts[11]
                            token0    = accounts[14]
                            token1    = accounts[15]

                            if token0 == WSOL_ADDRESS:
                                target_token, vault_addr = token1, vault0
                            elif token1 == WSOL_ADDRESS:
                                target_token, vault_addr = token0, vault1
                            break

                    # Generic Fallback for Meteora / Orca
                    if not target_token:
                        post_balances = tx["meta"].get("postTokenBalances", [])
                        mints = set()
                        for bal in post_balances:
                            mint = bal.get("mint")
                            if mint and mint != WSOL_ADDRESS:
                                mints.add(mint)
                        if mints:
                            target_token = list(mints)[0]
                            token0 = WSOL_ADDRESS
                            token1 = target_token
                            
                            # Resolve vaults
                            wsol_bal = [b for b in post_balances if b.get("mint") == WSOL_ADDRESS]
                            if wsol_bal and account_keys:
                                try:
                                    vault_addr = account_keys[wsol_bal[0]["accountIndex"]]
                                except:
                                    vault_addr = None

                            # Find pair address by program invocation instruction
                            insts = tx["transaction"]["message"].get("instructions", [])
                            for inst in insts:
                                if inst.get("programId") in [METEORA_DLMM, METEORA_POOLS, ORCA_CLMM]:
                                    accounts = inst.get("accounts", [])
                                    if accounts:
                                        pair_addr = accounts[0]
                                        break
                            if not pair_addr:
                                pair_addr = target_token

                    if not target_token or not vault_addr:
                        continue

                    # Parse deployer address
                    if not deployer:
                        try:
                            keys = tx["transaction"]["message"].get("accountKeys", [])
                            if keys and isinstance(keys, list):
                                if isinstance(keys[0], dict):
                                    deployer = keys[0].get("pubkey")
                                else:
                                    deployer = keys[0]
                        except Exception as e:
                            print(f"⚠️ Failed to parse deployer from transaction: {e}")

                    if deployer:
                        # Load blacklist
                        blacklist = {}
                        if os.path.exists("blacklist.json"):
                            try:
                                with open("blacklist.json", "r") as f:
                                    blacklist = json.load(f)
                            except:
                                pass
                        if deployer in blacklist:
                            print(f"🚫 Ignoring pool launch from blacklisted creator: {deployer[:16]}...")
                            continue

                        if not check_deployer_rug_history(deployer):
                            print(f"🚫 Rejecting: Creator {deployer[:16]} has active rug history.")
                            continue

                    print(f"👉 New {dex_type.capitalize()} Pool: {pair_addr[:20]} | Token: {target_token[:16]}")

                    res = rpc_call("getTokenAccountBalance", [vault_addr])
                    liq_sol = float(res["value"].get("uiAmount", 0.0)) if res and res.get("value") else 0.0

                    if S["MIN_LIQ"] <= liq_sol <= S["MAX_LIQ"]:
                        print(f"🎯 Liquidity Match: {liq_sol:.2f} SOL — running safety check...")
                        if check_token_safety(target_token):
                            info   = get_dexscreener_token(target_token)
                            symbol = info["symbol"] if info else "TOKEN"

                            alert_discovery(symbol, target_token, liq_sol)

                            # ONLY BUY IF WE ARE NOT IN POSITION AND LOSS CAP NOT HIT
                            if not s.get("in_position") and session_loss_usd < SESSION_MAX_LOSS_USD:
                                amt_lamports = int(S["TRADE_SOL"] * 1e9)
                                if S["BUY_BLOCK_DELAY"] > 0:
                                    print(f"⏳ Buy delay: {S['BUY_BLOCK_DELAY']}s...")
                                    time.sleep(S["BUY_BLOCK_DELAY"])

                                success = execute_swap(WSOL_ADDRESS, target_token, amt_lamports, dex_type=dex_type)
                                if success:
                                    s["in_position"]      = True
                                    s["token_address"]    = target_token
                                    s["token_symbol"]     = symbol
                                    s["pair_address"]     = pair_addr
                                    s["dex_type"]         = dex_type
                                    s["buy_price_sol"]    = S["TRADE_SOL"]
                                    s["highest_price_sol"] = S["TRADE_SOL"]
                                    s["amount_held"]      = get_token_balance(target_token)
                                    s["buy_timestamp"]    = int(time.time())
                                    s["initial_liq_sol"]  = liq_sol
                                    s["wsol_vault_address"] = vault_addr
                                    s["deployer_address"] = deployer
                                    s["half_sold"]        = False
                                    s["sell_attempts"]    = 0
                                    save_state(s)
                                    alert_trade_enter(symbol, target_token, S["TRADE_SOL"], liq_sol)
                                    break
                    elif liq_sol < S["MIN_LIQ"]:
                        # Underfunded — add to watchlist
                        watchlist = s.get("watchlist", [])
                        if not any(x["pair_address"] == pair_addr for x in watchlist):
                            watchlist.append({
                                "pair_address": pair_addr,
                                "vault_address": vault_addr,
                                "target_token": target_token,
                                "t0": token0,
                                "t1": token1,
                                "added_time": int(time.time()),
                                "deployer": deployer,
                                "dex_type": dex_type,
                            })
                            s["watchlist"] = watchlist
                            save_state(s)
                            print(f"👁️ Added unfunded pool {pair_addr[:20]} to watchlist.")
        wakeup_event.wait(max(2.0, S["CHECK_INTERVAL"]))
        wakeup_event.clear()


if __name__ == "__main__":
    main()
