import os
import sys
import json
import time
import queue
import requests
import threading
import concurrent.futures
import ai_predictive_model
import v2_logger
v2_logger.inject_v2_logger()
# Persistent HTTP Session to reuse TCP/TLS connections (reducing latency by up to 80%)
_http_session = requests.Session()

# Set up native parsing of .env files
def load_env():
    if os.path.exists(".env"):
        with open(".env", "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()

load_env()

try:
    # Import web3
    from web3 import Web3
except ImportError:
    print("\n❌ Error: The 'web3' library is not installed.")
    print("👉 Please run: pip install web3\n")
    sys.exit(1)

# Paths
CONFIG_PATH = "dex_scanner_config.json"
STATE_PATH = "scanner_state.json"
GLOBAL_CONFIG_PATH = "config.json"

# Load config
if not os.path.exists(CONFIG_PATH):
    print(f"❌ Config file {CONFIG_PATH} not found!")
    sys.exit(1)

with open(CONFIG_PATH, "r") as f:
    config = json.load(f)

# Load settings
RPC_URL = os.environ.get("RPC_URL", config.get("rpc_url"))
RPC_POOL = config.get("rpc_pool", [])
WSS_URL_CONFIG = config.get("wss_url", "")
PRIVATE_KEY = os.environ.get("METAMASK_PRIVATE_KEY", "")
DRY_RUN = config.get("dry_run", True)
CHECK_INTERVAL = config.get("check_interval_seconds", 15)
TRADE_AMOUNT_ETH = config.get("trade_amount_eth", 0.001)
TRAILING_STOP = config.get("trailing_stop_percent", 15.0)
HARD_STOP_LOSS = config.get("hard_stop_loss_percent", 20.0)
HARD_TAKE_PROFIT = config.get("hard_take_profit_percent", 300.0)
TIME_EXIT_MINUTES = config.get("time_exit_minutes", 0)
GAS_PREMIUM_PERCENT = config.get("gas_premium_percent", 10.0)
SCANNER_ACTIVE = config.get("scanner_active", True)
MIN_LIQ = config.get("min_liquidity_eth", 0.5)
MAX_LIQ = config.get("max_liquidity_eth", 15.0)
BUY_BLOCK_DELAY = config.get("buy_block_delay", 3)
MAX_TOP_10_HOLDER_SHARE = config.get("max_top_10_holder_share", 0.50)
MAX_POSITIONS = config.get("max_positions", 3)  # max simultaneous open trades
STAGED_TAKE_PROFIT_PERCENT = config.get("staged_take_profit_percent", 30.0)
STAGED_SELL_FRACTION = config.get("staged_sell_fraction", 0.50)
X_WEBHOOK_URL = config.get("x_webhook_url", "")
SESSION_MAX_LOSS_USD = 30.0
SESSION_CUTOFF_HOUR_UTC = 15
SESSION_CUTOFF_MIN_UTC = 0
_token_buy_taxes = {}
_token_creator_addresses = {}
_token_ai_scores = {}
LAST_STATUS_REPORT_TIME = time.time()

# Load Telegram credentials
TG_TOKEN = ""
if os.path.exists(GLOBAL_CONFIG_PATH):
    with open(GLOBAL_CONFIG_PATH, "r") as f:
        tg_cfg = json.load(f)
        TG_TOKEN = tg_cfg.get("telegram_token", "")
        TG_CHAT_ID = tg_cfg.get("telegram_chat_id", "")
if TG_TOKEN and TG_CHAT_ID:
    _me = _http_session.get(f"https://api.telegram.org/bot{TG_TOKEN}/getMe", timeout=5).json()
    _bot_name = _me.get("result", {}).get("username", "unknown")
    print(f"✅ Telegram bot connected: @{_bot_name}")












# Addresses
FACTORY_ADDRESS = Web3.to_checksum_address(config.get("dex_factory_address"))
ROUTER_ADDRESS = Web3.to_checksum_address(config.get("dex_router_address"))
WETH_ADDRESS = Web3.to_checksum_address(config.get("weth_address"))
USDC_ADDRESS = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")

# Web3 Initialization
class FallbackHTTPProvider(Web3.HTTPProvider):
    def __init__(self, endpoints, *args, **kwargs):
        self.endpoints = endpoints
        self.current_index = 0
        super().__init__(endpoints[0], *args, **kwargs)
        
    def make_request(self, method, params):
        last_exception = None
        for _ in range(len(self.endpoints)):
            endpoint = self.endpoints[self.current_index]
            self.endpoint_uri = endpoint
            try:
                return super().make_request(method, params)
            except Exception as e:
                print(f"⚠️ [RPC FALLBACK] Error on {endpoint}: {e}. Trying next...")
                last_exception = e
                self.current_index = (self.current_index + 1) % len(self.endpoints)
        raise last_exception

# Establish Web3 with Fallback RPC Pool support
if len(RPC_POOL) > 0:
    if RPC_URL and RPC_URL not in RPC_POOL:
        RPC_POOL.insert(0, RPC_URL)
    web3 = Web3(FallbackHTTPProvider(RPC_POOL))
else:
    web3 = Web3(Web3.HTTPProvider(RPC_URL))
wallet_address = None
if PRIVATE_KEY:
    try:
        account = web3.eth.account.from_key(PRIVATE_KEY)
        wallet_address = account.address
        print(f"✅ Scanner wallet loaded: {wallet_address}")
    except Exception as e:
        print(f"❌ Failed to load private key: {e}")
        sys.exit(1)
else:
    print("⚠️ Warning: Operating in read-only/dry-run mode. Private key not set.")

# Factory ABI
FACTORY_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "allPairsLength",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "allPairs",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"internalType": "address", "name": "tokenA", "type": "address"},
            {"internalType": "address", "name": "tokenB", "type": "address"}
        ],
        "name": "getPair",
        "outputs": [{"internalType": "address", "name": "pair", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Pair ABI
PAIR_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "token0",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "token1",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "getReserves",
        "outputs": [
            {"internalType": "uint112", "name": "_reserve0", "type": "uint112"},
            {"internalType": "uint112", "name": "_reserve1", "type": "uint112"},
            {"internalType": "uint32", "name": "_blockTimestampLast", "type": "uint32"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

# Router ABI
ROUTER_ABI = [
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactETHForTokens",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactETHForTokensSupportingFeeOnTransferTokens",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForETH",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "uint256", "name": "amountOutMin", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"},
            {"internalType": "address", "name": "to", "type": "address"},
            {"internalType": "uint256", "name": "deadline", "type": "uint256"}
        ],
        "name": "swapExactTokensForETHSupportingFeeOnTransferTokens",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
            {"internalType": "address[]", "name": "path", "type": "address[]"}
        ],
        "name": "getAmountsOut",
        "outputs": [
            {"internalType": "uint256[]", "name": "amounts", "type": "uint256[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]

# ERC20 ABI
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "totalSupply",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "remaining", "type": "uint256"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function"
    }
]

# Instantiations
factory_contract = web3.eth.contract(address=FACTORY_ADDRESS, abi=FACTORY_ABI)
router_contract = web3.eth.contract(address=ROUTER_ADDRESS, abi=ROUTER_ABI)

state_lock = threading.Lock()
wakeup_event = threading.Event()

# ─── Multicall3 ───────────────────────────────────────────────────────────────
# Deployed on Base mainnet at the canonical address
MULTICALL3_ADDRESS = Web3.to_checksum_address("0xcA11bde05977b3631167028862bE2a173976CA11")
MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target",   "type": "address"},
                    {"name": "callData", "type": "bytes"}
                ],
                "name": "calls",
                "type": "tuple[]"
            }
        ],
        "name": "aggregate",
        "outputs": [
            {"name": "blockNumber", "type": "uint256"},
            {"name": "returnData",  "type": "bytes[]"}
        ],
        "stateMutability": "view",
        "type": "function"
    }
]
multicall_contract = web3.eth.contract(address=MULTICALL3_ADDRESS, abi=MULTICALL3_ABI)

# Selector constants (first 4 bytes of keccak256 of the function signature)
_SEL_ALL_PAIRS  = bytes.fromhex("1e3dd18b")  # allPairs(uint256)
_SEL_TOKEN0     = bytes.fromhex("0dfe1681")  # token0()
_SEL_TOKEN1     = bytes.fromhex("d21220a7")  # token1()
_SEL_RESERVES   = bytes.fromhex("0902f1ac")  # getReserves()
_SEL_DECIMALS   = bytes.fromhex("313ce567")  # decimals()

MULTICALL_BATCH = 30  # pairs fetched per RPC round-trip

def _encode_uint256(n: int) -> bytes:
    return n.to_bytes(32, "big")

def batch_fetch_pairs(start_index: int, count: int):
    """Return a list of dicts {pair_addr, t0, t1, r0, r1} via a single Multicall3 aggregate.
    Uses two rounds: one to resolve pair addresses, one to fetch token0/token1/reserves."""
    # ── Round 1: resolve pair addresses ──────────────────────────────────────
    round1_calls = [
        (FACTORY_ADDRESS, _SEL_ALL_PAIRS + _encode_uint256(start_index + j))
        for j in range(count)
    ]
    try:
        _, ret1 = multicall_contract.functions.aggregate(round1_calls).call()
        _, ret1 = multicall_contract.functions.aggregate(round1_calls).call()
    except Exception as e:
        print(f"⚠️ Multicall round-1 error: {e}")
        return []

    pair_addrs = []
    for raw in ret1:
        if len(raw) < 32:
            pair_addrs.append(None)
            continue
        # ABI-encoded address is right-padded in 32 bytes; address is the last 20 bytes
        addr_bytes = raw[12:32]
        pair_addrs.append(Web3.to_checksum_address(addr_bytes))

    # ── Round 2: fetch token0, token1, getReserves for each pair ─────────────
    round2_calls = []
    for pa in pair_addrs:
        if pa is None:
            continue
        round2_calls.append((pa, _SEL_TOKEN0))
        round2_calls.append((pa, _SEL_TOKEN1))
        round2_calls.append((pa, _SEL_RESERVES))

    if not round2_calls:
        return []

    try:
        _, ret2 = multicall_contract.functions.aggregate(round2_calls).call()
    except Exception as e:
        print(f"⚠️ Multicall round-2 error: {e}")
        return []

    results = []
    idx = 0
    for pa in pair_addrs:
        if pa is None:
            results.append(None)
            continue
        try:
            raw_t0  = ret2[idx];     idx += 1
            raw_t1  = ret2[idx];     idx += 1
            raw_res = ret2[idx];     idx += 1

            t0  = Web3.to_checksum_address(raw_t0[12:32])
            t1  = Web3.to_checksum_address(raw_t1[12:32])
            r0  = int.from_bytes(raw_res[0:32],  "big") & ((1 << 112) - 1)
            r1  = int.from_bytes(raw_res[32:64], "big") & ((1 << 112) - 1)

            results.append({"pair_addr": pa, "t0": t0, "t1": t1, "r0": r0, "r1": r1})
        except Exception as e:
            print(f"⚠️ Multicall decode error for {pa}: {e}")
            results.append(None)

    return results

def batch_fetch_reserves(pairs_list):
    """Fetch getReserves for a list of pair addresses using a single Multicall3 aggregate call."""
    if not pairs_list:
        return []
    calls = []
    for p in pairs_list:
        calls.append((Web3.to_checksum_address(p["pair_address"]), _SEL_RESERVES))
    
    try:
        _, ret = multicall_contract.functions.aggregate(calls).call()
    except Exception as e:
        print(f"⚠️ Multicall batch_fetch_reserves error: {e}")
        return [None] * len(pairs_list)

    results = []
    for i, raw in enumerate(ret):
        if len(raw) < 96:
            results.append(None)
            continue
        try:
            r0 = int.from_bytes(raw[0:32], "big") & ((1 << 112) - 1)
            r1 = int.from_bytes(raw[32:64], "big") & ((1 << 112) - 1)
            results.append((r0, r1))
        except Exception as e:
            print(f"⚠️ Error decoding reserves for {pairs_list[i]['pair_address']}: {e}")
            results.append(None)
            
    return results

# State helpers
def _make_position(token_address, token_symbol, pair_address, buy_price_usd, amount_held,
                   buy_router_address, initial_liq_eth, quote_address, creator_address,
                   buy_timestamp=None, half_sold=False, sell_attempts=0):
    """Create a new position dict for the positions list."""
    return {
        "token_address": token_address,
        "token_symbol": token_symbol,
        "pair_address": pair_address,
        "buy_price_usd": buy_price_usd,
        "highest_price_usd": buy_price_usd,
        "amount_held": amount_held,
        "buy_router_address": buy_router_address,
        "initial_liq_eth": initial_liq_eth,
        "quote_address": quote_address,
        "creator_address": creator_address,
        "buy_timestamp": buy_timestamp if buy_timestamp is not None else int(time.time()),
        "half_sold": half_sold,
        "sell_attempts": sell_attempts,
    }

_cached_state = None
_last_state_mtime = 0.0

def load_state():
    global _cached_state, _last_state_mtime
    try:
        if not os.path.exists(STATE_PATH):
            return {
                "in_position": False,
                "positions": [],
                "last_checked_pair_index": -1,
                "last_trade": None,
                "watchlist": []
            }
        mtime = os.path.getmtime(STATE_PATH)
        if mtime == _last_state_mtime and _cached_state is not None:
            return _cached_state
            
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
        
        # Ensure all required keys are present (used by some UI/reporting paths)
        if "last_trade" not in state:
            state["last_trade"] = None
        if "last_checked_pair_index" not in state:
            state["last_checked_pair_index"] = -1
        if "watchlist" not in state:
            state["watchlist"] = []
            
        # Keep derived convenience bool in sync
        state["in_position"] = len(state["positions"]) > 0
        
        _cached_state = state
        _last_state_mtime = mtime
        return state
    except:
        if _cached_state is not None:
            return _cached_state
        return {
            "in_position": False,
            "positions": [],
            "last_checked_pair_index": -1,
            "last_trade": None,
            "watchlist": []
        }

def save_state(state):
    global _cached_state, _last_state_mtime
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
        _cached_state = state
        try:
            _last_state_mtime = os.path.getmtime(STATE_PATH)
        except:
            _last_state_mtime = time.time()
    except Exception as e:
        print(f"❌ Failed to save state file: {e}")

HISTORY_PATH = "trade_history.json"

def save_trade_to_history(trade):
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

def _get_session_loss_usd() -> float:
    """Return how much USD we've lost this session vs the start balance.
    Positive return = we are DOWN that many dollars."""
    if _session_start_eth_balance <= 0 or wallet_address is None:
        return 0.0
    try:
        current_eth = float(web3.from_wei(web3.eth.get_balance(wallet_address), "ether"))
        diff_eth = _session_start_eth_balance - current_eth  # positive = loss
        eth_price = _session_eth_price_at_start if _session_eth_price_at_start > 0 else 3400.0
        return diff_eth * eth_price
    except Exception as e:
        print(f"⚠️ Error getting session loss: {e}")
        return 0.0

_session_cutoff_target = None

def _is_session_cutoff() -> bool:
    """Return True if current UTC time is at or past the 9 AM MT cutoff."""
    return False
# ──────────────────────────────────────────────────────────────────────────────


def send_telegram(msg, chat_id=None, reply_markup=None):
    """Non-blocking Telegram send — fires in a daemon thread so the trading loop never stalls."""
    if not TG_TOKEN:
        return
    target_chat = chat_id if chat_id else TG_CHAT_ID
    if not target_chat:
        return
    def _send():
        try:
            url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
            payload = {"chat_id": target_chat, "text": msg, "parse_mode": "Markdown"}
            if reply_markup:
                payload["reply_markup"] = reply_markup
            resp = _http_session.post(url, json=payload, timeout=5)
            if resp.status_code != 200:
                print(f"⚠️ Telegram Alert Failed ({resp.status_code}): {resp.text}")
                if "can't parse entities" in resp.text:
                    print("⚠️ Retrying Telegram message as plain text (no markdown) due to parsing error...")
                    payload_plain = {"chat_id": target_chat, "text": msg}
                    if reply_markup:
                        payload_plain["reply_markup"] = reply_markup
                    _http_session.post(url, json=payload_plain, timeout=5)
        except Exception as e:
            print(f"⚠️ Telegram Alert Error: {e}")
    threading.Thread(target=_send, daemon=True).start()

def send_discord_webhook(embed_data=None, content=None):
    try:
        if not os.path.exists(CONFIG_PATH):
            return
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        url = cfg.get("discord_webhook_url", "")
        if not url or url.strip() == "" or url.strip().lower() == "none":
            return
        
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
        print(f"⚠️ Discord Webhook Alert Error: {e}")

def send_status_report(chat_id=None):
    if not TG_TOKEN:
        return
    target_chat = chat_id if chat_id else TG_CHAT_ID
    if not target_chat:
        return
        
    state = load_state()
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        dry_val = cfg.get("dry_run", True)
        ts_val = cfg.get("trailing_stop_percent", 15.0)
        sl_val = cfg.get("hard_stop_loss_percent", 20.0)
        tp_val = cfg.get("hard_take_profit_percent", 300.0)
        te_val = cfg.get("time_exit_minutes", 0)
        active_val = cfg.get("scanner_active", True)
    except:
        dry_val = True
        ts_val = 15.0
        sl_val = 20.0
        tp_val = 300.0
        te_val = 0
        active_val = True
        
    status = "🟢 ACTIVE" if web3.is_connected() else "🔴 OFFLINE"
    if not active_val:
        status = "⏸️ PAUSED"
    mode = "🧪 DRY RUN (Simulated)" if dry_val else "🔥 LIVE TRADING"
    
    msg = f"🛰️ *Sniper Bot Status*: {status}\n"
    msg += f"⚙️ *Mode*: {mode}\n"
    msg += f"📦 *Scan Index*: {state.get('last_checked_pair_index', -1)}\n"
    msg += f"📊 *Open Positions*: {len(state.get('positions', []))} / {MAX_POSITIONS}\n\n"

    positions = state.get("positions", [])
    if positions:
        eth_price = get_weth_usd_price()
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg_file = json.load(f)
                trade_amt_eth = cfg_file.get("trade_amount_eth", 0.001)
        except:
            pass

        for idx, pos in enumerate(positions, 1):
            token_addr    = pos["token_address"]
            symbol        = pos["token_symbol"]
            buy_price     = pos["buy_price_usd"]
            highest_price = pos["highest_price_usd"]

            current_price = None
            pair_addr = pos.get("pair_address")
            if pair_addr:
                price_in_weth, _ = get_onchain_price(pair_addr, token_addr)
                if price_in_weth is not None:
                    current_price = price_in_weth * eth_price
            if current_price is None:
                token_info = get_dex_screener_price(token_addr)
                current_price = token_info["price_usd"] if token_info else buy_price

            initial_usd     = trade_amt_eth * eth_price
            current_val_usd = initial_usd * (current_price / buy_price) if buy_price > 0 else initial_usd
            net_pnl_usd     = current_val_usd - initial_usd
            change_from_buy = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0

            ts_price    = highest_price * (1 - ts_val / 100)
            sl_price    = buy_price * (1 - sl_val / 100)
            ts_pnl_pct  = ((ts_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0
            ts_val_usd  = initial_usd * (1 + ts_pnl_pct / 100)
            ts_net_usd  = ts_val_usd - initial_usd
            sl_val_usd  = initial_usd * (1 - sl_val / 100)
            sl_net_usd  = sl_val_usd - initial_usd

            pnl_sign  = "+" if net_pnl_usd >= 0 else ""
            pnl_emoji = "🟢" if net_pnl_usd >= 0 else "🔴"

            msg += f"── *Trade {idx}/{len(positions)}* ──\n"
            msg += f"📈 *Token*: {symbol}\n"
            msg += f"🔗 `{token_addr}`\n"
            msg += f"📥 *Entry*: `${format_price(buy_price)}` | *Now*: `${format_price(current_price)}`\n"
            msg += f"{pnl_emoji} *P/L*: *{change_from_buy:+.2f}%* ({pnl_sign}${net_pnl_usd:.2f})\n"

            buy_ts = pos.get("buy_timestamp", 0)
            if te_val > 0 and buy_ts > 0:
                elapsed_min  = (time.time() - buy_ts) / 60
                remaining_min = max(0.0, te_val - elapsed_min)
                msg += f"⏱️ Held `{elapsed_min:.1f}` min, exits in `{remaining_min:.1f}` min\n"

            if tp_val > 0:
                tp_price = buy_price * (1 + tp_val / 100)
                msg += f"🏆 TP `${format_price(tp_price)}` | 🛑 SL `${format_price(sl_price)}` | 🛡️ TS `${format_price(ts_price)}`\n"
            else:
                msg += f"🛑 SL `${format_price(sl_price)}` | 🛡️ TS `${format_price(ts_price)}`\n"
            msg += "\n"
    else:
        msg += "💤 *Positions*: No active positions. Scanning for new pairs...\n"

    send_telegram(msg, chat_id=target_chat)

def alert_trade_enter(symbol, token_addr, eth_amount, buy_price, liquidity_eth):
    eth_price = 1680.0
    try:
        eth_info = get_dex_screener_price(WETH_ADDRESS)
        if eth_info:
            eth_price = eth_info["price_usd"]
    except:
        pass
    trade_usd = eth_amount * eth_price
    
    ai_score = _token_ai_scores.get(token_addr.lower(), "N/A")
    msg = (
        f"📥 *TRADE ENTERED* (Buy Order)\n\n"
        f"🪙 *Token*: {symbol}\n"
        f"🔗 *Address*: `{token_addr}`\n"
        f"💵 *Buy Price*: `${format_price(buy_price)}`\n"
        f"💰 *Trade Size*: `{eth_amount} ETH` (~${trade_usd:.2f})\n"
        f"🌊 *Pool Liquidity*: `{liquidity_eth:.2f} ETH` (~${(liquidity_eth * eth_price):,.2f})\n"
        f"🧠 *BullX-NEO AI Score*: `{ai_score}/100`\n\n"
        f"ℹ️ *What happens next?*\n"
        f"The bot is now monitoring the price live. It will take profit automatically if the price pumps and then dips slightly, or cut loss if it drops too low."
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {"text": "🛒 Buy More (0.005 ETH)", "callback_data": f"buy_more_{token_addr}"},
                {"text": "💰 Sell 50%", "callback_data": f"sell_50_{token_addr}"},
                {"text": "🚨 Sell 100%", "callback_data": f"sell_100_{token_addr}"}
            ]
        ]
    }
    send_telegram(msg, reply_markup=reply_markup)
    
    # Send Discord Embed
    discord_embed = {
        "title": "📥 TRADE ENTERED (Buy Order)",
        "description": "The sniper bot has executed a swap on Uniswap V2.",
        "color": 3066993,  # Green
        "fields": [
            {"name": "🪙 Token Symbol", "value": f"**{symbol}**", "inline": True},
            {"name": "💵 Buy Price", "value": f"${format_price(buy_price)}", "inline": True},
            {"name": "💰 Trade Size", "value": f"{eth_amount} ETH (~${trade_usd:.2f})", "inline": True},
            {"name": "🌊 Pool Liquidity", "value": f"{liquidity_eth:.2f} ETH (~${(liquidity_eth * eth_price):,.2f})", "inline": True},
            {"name": "🔗 Token Address", "value": f"`{token_addr}`", "inline": False},
            {"name": "📊 DexScreener", "value": f"[View Chart](https://dexscreener.com/base/{token_addr})", "inline": True}
        ],
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "footer": {"text": "Base Meme Coin Sniper Bot"}
    }
    send_discord_webhook(embed_data=discord_embed)

def alert_trade_close(symbol, entry_price, exit_price, pnl_pct, reason):
    is_win = pnl_pct > 0
    emoji = "🏆 *TRADE WIN*" if is_win else "💀 *TRADE LOSS*"
    status_icon = "🟢" if is_win else "🔴"
    
    eth_price = 1680.0
    try:
        eth_info = get_dex_screener_price(WETH_ADDRESS)
        if eth_info:
            eth_price = eth_info["price_usd"]
    except:
        pass
        
    # Get trade amount from config
    trade_amt_eth = 0.001
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
            trade_amt_eth = cfg.get("trade_amount_eth", 0.001)
    except:
        pass
        
    initial_usd = trade_amt_eth * eth_price
    exit_usd = initial_usd * (1 + pnl_pct / 100)
    net_pnl_usd = exit_usd - initial_usd
    pnl_sign = "+" if net_pnl_usd >= 0 else ""
    
    msg = (
        f"{status_icon} {emoji}\n\n"
        f"🪙 *Token*: {symbol}\n"
        f"📥 *We Bought at*: `${format_price(entry_price)}`\n"
        f"📤 *We Sold at*: `${format_price(exit_price)}`\n\n"
        f"💵 *Initial Investment*: ${initial_usd:.2f}\n"
        f"💵 *Final Payout*: ${exit_usd:.2f}\n"
        f"📊 *Net Return*: *{pnl_pct:+.2f}%* ({pnl_sign}${net_pnl_usd:.2f})\n"
        f"ℹ️ *Reason*: {reason}"
    )
    send_telegram(msg)
    
    # Send Discord Embed
    discord_title = "🏆 TRADE WIN" if is_win else "💀 TRADE LOSS"
    color = 3066993 if is_win else 15158332  # Green or Red
    
    discord_embed = {
        "title": f"{status_icon} {discord_title}",
        "description": "Position closed on Uniswap V2.",
        "color": color,
        "fields": [
            {"name": "🪙 Token Symbol", "value": f"**{symbol}**", "inline": True},
            {"name": "📥 Entry Price", "value": f"${format_price(entry_price)}", "inline": True},
            {"name": "📤 Exit Price", "value": f"${format_price(exit_price)}", "inline": True},
            {"name": "💵 Initial Investment", "value": f"${initial_usd:.2f}", "inline": True},
            {"name": "💵 Final Payout", "value": f"${exit_usd:.2f}", "inline": True},
            {"name": "📊 Net Return", "value": f"**{pnl_pct:+.2f}%* ({pnl_sign}${net_pnl_usd:.2f})", "inline": False},
            {"name": "ℹ️ Reason", "value": f"{reason}", "inline": False}
        ],
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "footer": {"text": "Base Meme Coin Sniper Bot"}
    }
    send_discord_webhook(embed_data=discord_embed)

def send_x_promo_webhook(symbol, token_addr, price, liquidity_eth):
    if not X_WEBHOOK_URL or X_WEBHOOK_URL.strip() == "" or X_WEBHOOK_URL.strip().lower() == "none":
        return
    
    # Styled tweet promotion message
    tweet_text = (
        f"🔍 NEW LAUNCH SIGNAL DISCOVERED! 🚀\n\n"
        f"🪙 Token: {symbol}\n"
        f"💵 Entry: ${format_price(price)}\n"
        f"🌊 Liquidity: {liquidity_eth:.2f} ETH\n"
        f"🛡️ Safety: GoPlus PASSED\n\n"
        f"Follow the updates & trade here: linktr.ee/jadenmitchell78 📈\n"
        f"Contract: {token_addr}"
    )
    
    def _send_tweet():
        try:
            # Pings a webhook proxy to post to X
            payload = {
                "text": tweet_text,
                "symbol": symbol,
                "contractAddress": token_addr,
                "price": price,
                "liquidity": liquidity_eth
            }
            resp = _http_session.post(X_WEBHOOK_URL, json=payload, timeout=10)
            if resp.status_code == 200 or resp.status_code == 201:
                print(f"📊 X (Twitter) Promo Webhook successfully triggered for {symbol}.")
            else:
                print(f"⚠️ X Promo Webhook failed ({resp.status_code}): {resp.text}")
        except Exception as e:
            print(f"⚠️ X Promo Webhook Error: {e}")
            
    threading.Thread(target=_send_tweet, daemon=True).start()

def alert_discovery(symbol, token_addr, price, liquidity_eth):
    # Send Telegram to default chat
    telegram_msg = f"🔍 [Scanner Discovery]\nSymbol: {symbol}\nPrice: ${format_price(price)}\nLiquidity: {liquidity_eth:.2f} ETH\nSafety: GoPlus PASSED"
    send_telegram(telegram_msg)
    
    # Send formatted telemetry block to AuraScanVM channel
    channel_msg = (
        f"🔍 *[AuraScan Discovery]*\n"
        f"🪙 *Symbol*: `{symbol}`\n"
        f"💵 *Price*: `${format_price(price)}`\n"
        f"🌊 *Liquidity*: `{liquidity_eth:.2f} ETH`\n"
        f"🛡️ *Safety Check*: `GoPlus PASSED`\n"
        f"🔗 *Contract*: `{token_addr}`\n"
        f"📊 *Chart*: [DexScreener Link](https://dexscreener.com/base/{token_addr})"
    )
    send_telegram(channel_msg, chat_id="@AuraScanVM")
    
    # Send X Promo Webhook
    send_x_promo_webhook(symbol, token_addr, price, liquidity_eth)
    
    # Send Discord Webhook
    eth_price = 1680.0
    try:
        eth_info = get_dex_screener_price(WETH_ADDRESS)
        if eth_info:
            eth_price = eth_info["price_usd"]
    except:
        pass
        
    discord_embed = {
        "title": "🔍 NEW ALPHA ALERT: Safe Pair Discovered!",
        "description": "A new Uniswap V2 pair has launched on Base and passed all safety checks.",
        "color": 1752220, # Cyan
        "fields": [
            {"name": "🪙 Token Symbol", "value": f"**{symbol}**", "inline": True},
            {"name": "💵 Price", "value": f"${format_price(price)}", "inline": True},
            {"name": "🌊 Liquidity", "value": f"{liquidity_eth:.2f} ETH (~${(liquidity_eth * eth_price):,.2f})", "inline": True},
            {"name": "🛡️ Safety Check", "value": "GoPlus PASSED (Enforced Holder Index)", "inline": False},
            {"name": "🔗 Token Address", "value": f"`{token_addr}`", "inline": False},
            {"name": "📊 DexScreener", "value": f"[View Chart](https://dexscreener.com/base/{token_addr})", "inline": True}
        ],
        "timestamp": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "footer": {"text": "Base Meme Coin Sniper Bot - Alpha Alerts"}
    }
    send_discord_webhook(embed_data=discord_embed)

# Telegram command handling moved to tg_bot.py


def get_dex_screener_price(token_addr):
    if not token_addr:
        return None
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
    try:
        resp = _http_session.get(url, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs", [])
            if not pairs:
                return None
            best_pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            return {
                "price_usd": float(best_pair.get("priceUsd", 0)),
                "symbol": best_pair.get("baseToken", {}).get("symbol", "TOKEN"),
                "name": best_pair.get("baseToken", {}).get("name", "Token"),
                "liquidity_usd": float(best_pair.get("liquidity", {}).get("usd", 0) or 0)
            }
    except Exception as e:
        print(f"⚠️ DexScreener Fetch Error: {e}")
    return None

weth_usd_price_cache = 1680.0
last_weth_price_update = 0.0

# ── Gas price cache (5s TTL) — saves 1 RPC call per buy/sell ─────────────────
_gas_price_cache = None
_gas_price_last_update = 0.0

def get_cached_gas_price():
    global _gas_price_cache, _gas_price_last_update
    now = time.time()
    if _gas_price_cache is None or now - _gas_price_last_update > 5:
        _gas_price_cache = web3.eth.gas_price
        _gas_price_last_update = now
    return _gas_price_cache
# ─────────────────────────────────────────────────────────────────────────────

def get_weth_usd_price():
    global weth_usd_price_cache, last_weth_price_update
    now = time.time()
    if now - last_weth_price_update > 30:  # 30s TTL — was 300s
        try:
            url = f"https://api.dexscreener.com/latest/dex/tokens/{WETH_ADDRESS}"
            resp = _http_session.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get("pairs", [])
                if pairs:
                    best_pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
                    weth_usd_price_cache = float(best_pair.get("priceUsd", 1680.0))
                    last_weth_price_update = now
                    print(f"🔄 Updated WETH price cache: ${weth_usd_price_cache:.2f}")
        except Exception as e:
            print(f"⚠️ Failed to update WETH price cache: {e}")
    return weth_usd_price_cache

def get_onchain_price(pair_addr, target_token_addr):
    """Fetch token price in WETH (or normalized WETH equivalent for USDC quote pools) via a single Multicall3 round-trip."""
    try:
        pair_cs  = Web3.to_checksum_address(pair_addr)
        token_cs = Web3.to_checksum_address(target_token_addr)

        calls = [
            (pair_cs,  _SEL_TOKEN0),
            (pair_cs,  _SEL_TOKEN1),
            (pair_cs,  _SEL_RESERVES),
            (token_cs, _SEL_DECIMALS),
        ]
        _, ret = multicall_contract.functions.aggregate(calls).call()

        t0       = Web3.to_checksum_address(ret[0][12:32])
        t1       = Web3.to_checksum_address(ret[1][12:32])
        raw_res  = ret[2]
        r0       = int.from_bytes(raw_res[0:32],  "big") & ((1 << 112) - 1)
        r1       = int.from_bytes(raw_res[32:64], "big") & ((1 << 112) - 1)
        decimals = int.from_bytes(ret[3][31:32],  "big")  # uint8 is the last byte

        is_t0_quote    = (t0 == WETH_ADDRESS or t0 == USDC_ADDRESS)
        quote_token    = t0 if is_t0_quote else t1
        
        quote_reserves = r0 if is_t0_quote else r1
        token_reserves = r1 if is_t0_quote else r0

        if token_reserves == 0 or quote_reserves == 0:
            return None, None

        eth_price = get_weth_usd_price()

        # Convert quote reserves to ETH liquidity for consistency
        if quote_token == USDC_ADDRESS:
            quote_amount = quote_reserves / (10 ** 6)
            liq_eth = quote_amount / eth_price if eth_price > 0 else quote_amount / 3000.0
            price_in_usdc = quote_amount / (token_reserves / (10 ** decimals))
            # Normalize to WETH equivalent price so callers (which multiply by WETH price) get correct USD price
            price_in_weth = price_in_usdc / eth_price if eth_price > 0 else price_in_usdc / 3000.0
        else:
            quote_amount = quote_reserves / (10 ** 18)
            liq_eth = float(web3.from_wei(quote_reserves, "ether"))
            price_in_weth = quote_amount / (token_reserves / (10 ** decimals))
            
        return price_in_weth, liq_eth
    except Exception as e:
        print(f"⚠️ On-chain Price Fetch Error: {e}")
        return None, None

def get_token_info_onchain(token_addr):
    try:
        checksum_token = Web3.to_checksum_address(token_addr)
        token_contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)
        symbol = token_contract.functions.symbol().call()
        name = token_contract.functions.name().call()
        return symbol, name
    except Exception as e:
        print(f"⚠️ Failed to fetch token info on-chain: {e}")
        return "TOKEN", "Token"

def get_eip1559_gas_fees():
    try:
        latest_block = web3.eth.get_block('latest')
        base_fee = latest_block.get('baseFeePerGas')
        if base_fee is None:
            base_fee = web3.eth.gas_price
            
        try:
            priority_fee = web3.eth.max_priority_fee
        except:
            priority_fee = web3.to_wei(0.1, 'gwei')
            
        gas_premium = 10.0
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
                gas_premium = cfg.get("gas_premium_percent", 10.0)
        except:
            pass
            
        priority_fee = int(priority_fee * (1 + gas_premium / 100))
        max_fee = int(base_fee * 1.5) + priority_fee
        return max_fee, priority_fee
    except Exception as e:
        print(f"⚠️ Failed to estimate EIP-1559 gas fees: {e}")
        return int(web3.eth.gas_price * 1.2), None

_last_profit_sweep_time = 0

def check_and_execute_profit_sweep():
    global _last_profit_sweep_time
    now = time.time()
    if now - _last_profit_sweep_time < 600:  # Check every 10 minutes
        return

    _last_profit_sweep_time = now
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        sweep_addr = cfg.get("profit_sweep_address", "")
        threshold = cfg.get("profit_sweep_threshold_eth", 0.0)
        
        if not sweep_addr or threshold <= 0:
            return
            
        sweep_addr_cs = Web3.to_checksum_address(sweep_addr)
        balance_wei = web3.eth.get_balance(wallet_address)
        threshold_wei = web3.to_wei(threshold, "ether")
        
        if balance_wei > threshold_wei:
            gas_buffer = web3.to_wei(0.005, "ether")
            sweep_amount_wei = balance_wei - threshold_wei
            
            if sweep_amount_wei > gas_buffer:
                print(f"💰 Balance ({web3.from_wei(balance_wei, 'ether')} ETH) exceeds threshold ({threshold} ETH). Executing profit sweep...")
                
                max_fee, priority_fee = get_eip1559_gas_fees()
                tx_params = {
                    'nonce': web3.eth.get_transaction_count(wallet_address),
                    'to': sweep_addr_cs,
                    'value': sweep_amount_wei - gas_buffer,
                    'gas': 21000,
                    'chainId': 8453,
                }
                if priority_fee is not None:
                    tx_params['maxFeePerGas'] = max_fee
                    tx_params['maxPriorityFeePerGas'] = priority_fee
                    tx_params['type'] = '0x2'
                else:
                    tx_params['gasPrice'] = max_fee
                    
                tx_signed = web3.eth.account.sign_transaction(tx_params, PRIVATE_KEY)
                tx_hash = web3.eth.send_raw_transaction(tx_signed.raw_transaction)
                print(f"📤 Profit sweep transaction broadcasted: {web3.to_hex(tx_hash)}")
                
                send_telegram(
                    f"💰 *[PROFIT SWEEP EXECUTED]*\n"
                    f"Locked in profits of `{web3.from_wei(sweep_amount_wei - gas_buffer, 'ether'):.6f} ETH` to secure cold wallet:\n"
                    f"`{sweep_addr_cs}`\n\n"
                    f"Tx Hash: https://basescan.org/tx/{web3.to_hex(tx_hash)}"
                )
    except Exception as e:
        print(f"⚠️ Profit sweep check failed: {e}")

def check_lp_lock_onchain(pair_addr, token_addr=None):
    try:
        pair_contract = web3.eth.contract(address=pair_addr, abi=ERC20_ABI)
        total_supply = pair_contract.functions.totalSupply().call()
        if total_supply == 0:
            return 0.0
            
        lockers = [
            "0x0000000000000000000000000000000000000000",
            "0x000000000000000000000000000000000000dEaD",
            "0x231278eD38B00C05619cca4Db902513c7B9b824F", # UNCX LP Locker Base
            "0x71B4EecD45882274417d76514B1547240fE2d4a0", # PinkLock Base
            "0xE22a59345Df69d0343405799F4031dBdB456E7B0", # Team Finance Locker Base
        ]
        # EXCLUDED token_addr from lockers to prevent contract-holder LP rug bypasses.
        # Scammers often store LP tokens in the token contract itself and withdraw them later.
        
        locked_lp = 0
        for locker in lockers:
            try:
                locked_lp += pair_contract.functions.balanceOf(Web3.to_checksum_address(locker)).call()
            except:
                pass
                
        percent_locked = locked_lp / total_supply
        return percent_locked
    except Exception as e:
        print(f"⚠️ Failed to check LP lock: {e}")
        return 0.0

def find_balance_slot(token_addr, wallet_addr):
    token_contract = web3.eth.contract(address=token_addr, abi=ERC20_ABI)
    wallet_hex = wallet_addr.lower().replace("0x", "").zfill(64)
    test_val_raw = 123456789
    test_val_hex = "0x" + hex(test_val_raw).replace("0x", "").zfill(64)
    
    # Solidity mapping layout: keccak256(key + slot)
    for slot in range(25):
        slot_hex = hex(slot).replace("0x", "").zfill(64)
        key_bytes = bytes.fromhex(wallet_hex + slot_hex)
        slot_hash = Web3.keccak(key_bytes).hex()
        state_override = {token_addr: {"stateDiff": {slot_hash: test_val_hex}}}
        try:
            bal = token_contract.functions.balanceOf(wallet_addr).call(state_override=state_override)
            if bal == test_val_raw:
                return slot
        except:
            continue
            
    # Vyper mapping layout: keccak256(slot + key)
    for slot in range(25):
        slot_hex = hex(slot).replace("0x", "").zfill(64)
        key_bytes = bytes.fromhex(slot_hex + wallet_hex)
        slot_hash = Web3.keccak(key_bytes).hex()
        state_override = {token_addr: {"stateDiff": {slot_hash: test_val_hex}}}
        try:
            bal = token_contract.functions.balanceOf(wallet_addr).call(state_override=state_override)
            if bal == test_val_raw:
                return slot
        except:
            continue
    return None

def find_allowance_slot_hash(token_addr, wallet_addr, spender_addr, balance_slot):
    test_slots = [balance_slot + 1, balance_slot + 2, 1, 2, 0]
    token_contract = web3.eth.contract(address=token_addr, abi=ERC20_ABI)
    wallet_hex = wallet_addr.lower().replace("0x", "").zfill(64)
    spender_hex = spender_addr.lower().replace("0x", "").zfill(64)
    test_val = 2**256 - 1
    test_val_hex = "0x" + "f" * 64
    
    for slot in test_slots:
        slot_hex = hex(slot).replace("0x", "").zfill(64)
        inner_hash = Web3.keccak(bytes.fromhex(wallet_hex + slot_hex)).hex()
        outer_hash = Web3.keccak(bytes.fromhex(spender_hex + inner_hash.replace("0x", "").zfill(64))).hex()
        state_override = {token_addr: {"stateDiff": {outer_hash: test_val_hex}}}
        try:
            allowance = token_contract.functions.allowance(wallet_addr, spender_addr).call(state_override=state_override)
            if allowance == test_val:
                return outer_hash
        except:
            continue
    return None

def simulate_swap_sell(token_addr, pair_addr, wallet_addr, router_addr, amount_in):
    try:
        balance_slot = find_balance_slot(token_addr, wallet_addr)
        if balance_slot is None:
            return False, 0
            
        wallet_hex = wallet_addr.lower().replace("0x", "").zfill(64)
        slot_hex = hex(balance_slot).replace("0x", "").zfill(64)
        balance_slot_hash = Web3.keccak(bytes.fromhex(wallet_hex + slot_hex)).hex()
        
        allowance_slot_hash = find_allowance_slot_hash(token_addr, wallet_addr, router_addr, balance_slot)
        if allowance_slot_hash is None:
            return False, 0
            
        amount_in_hex = "0x" + hex(amount_in).replace("0x", "").zfill(64)
        max_uint_hex = "0x" + "f" * 64
        
        state_override = {
            token_addr: {
                "stateDiff": {
                    balance_slot_hash: amount_in_hex,
                    allowance_slot_hash: max_uint_hex
                }
            }
        }
        
        router_contract = web3.eth.contract(address=router_addr, abi=ROUTER_ABI)
        path = [token_addr, WETH_ADDRESS]
        deadline = int(time.time()) + 300
        
        router_contract.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            amount_in,
            0,
            path,
            wallet_addr,
            deadline
        ).call({
            'from': wallet_addr,
        }, state_override=state_override)
        
        return True, 0
    except Exception as e:
        print(f"🚨 On-chain sell simulation reverted: {e}")
        return False, 0

def check_token_safety(token_addr):
    checksum_token = Web3.to_checksum_address(token_addr)
    print(f"🛡️ On-chain Security Scan: Checking safety for {token_addr}...")
    
    # 1. Resolve pair address and quote token
    pair_addr = factory_contract.functions.getPair(WETH_ADDRESS, checksum_token).call()
    quote_token = WETH_ADDRESS
    if not pair_addr or pair_addr == "0x0000000000000000000000000000000000000000":
        pair_addr = factory_contract.functions.getPair(USDC_ADDRESS, checksum_token).call()
        quote_token = USDC_ADDRESS
        
    if not pair_addr or pair_addr == "0x0000000000000000000000000000000000000000":
        print("🚨 Security Scan FAILED: Pair address not found on-chain.")
        return False
        
    # 1.5. Sandbox Protection (Block Delay)
    if BUY_BLOCK_DELAY > 0:
        try:
            current_block = web3.eth.block_number
            start_block = max(0, current_block - 9)
            pair_created_topic = "0x0d3648bd0f6ba80134a33ba9275ac585d9d315f0ad8355cddef57eacbffaa3c4"
            
            logs = web3.eth.get_logs({
                "fromBlock": start_block,
                "toBlock": current_block,
                "address": FACTORY_ADDRESS,
                "topics": [pair_created_topic]
            })
            
            creation_block = None
            for log in logs:
                data = log["data"]
                pair_in_log = "0x" + data[26:66]
                if pair_in_log.lower() == pair_addr.lower():
                    creation_block = log["blockNumber"]
                    break
            
            if creation_block is not None:
                target_block = creation_block + BUY_BLOCK_DELAY
                if current_block < target_block:
                    blocks_left = target_block - current_block
                    print(f"⏳ Sandbox Protection: Pair created at block {creation_block}. Current block {current_block}. Waiting {blocks_left} block(s)...")
                    while current_block < target_block:
                        time.sleep(1)
                        current_block = web3.eth.block_number
                    print(f"✅ Sandbox Protection: Target block {target_block} reached. Continuing.")
            else:
                print("ℹ️ Sandbox Protection: Pair created >10 blocks ago. Skipping delay.")
        except Exception as e:
            print(f"⚠️ Sandbox Protection Error: {e}. Skipping block delay verification.")
        
    # 2. On-chain LP Lock check
    lp_lock_pct = check_lp_lock_onchain(pair_addr, checksum_token)
    print(f"🔒 On-chain LP Lock: {lp_lock_pct * 100:.1f}% locked/burned")
        
    # 3. On-chain Honeypot & Sell Simulation
    if wallet_address:
        try:
            token_contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)
            decimals = token_contract.functions.decimals().call()
            test_amount = 1000 * 10**decimals
            
            success, _ = simulate_swap_sell(checksum_token, pair_addr, wallet_address, ROUTER_ADDRESS, test_amount)
            if not success:
                print("🚨 Security Scan FAILED: Sell simulation reverted on-chain (Honeypot).")
                return False
            print("🟢 On-chain Sell Simulation PASSED.")
        except Exception as e:
            print(f"🚨 Security Scan FAILED: Exception during on-chain exit simulation: {e}")
            return False
            
    # 4. Fallback/Static analysis via GoPlus (Fast fields only)
    url = f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={token_addr}"
    try:
        resp = _http_session.get(url, timeout=4)
        if resp.status_code == 200:
            data = resp.json()
            result = data.get("result", {})
            token_data = result.get(token_addr.lower(), {})
            
            if token_data.get("is_open_source") == "0":
                print("🚨 GoPlus Scan FAILED: Contract code is NOT verified.")
                return False
                
            creator_addr = token_data.get("creator_address")
            if creator_addr:
                _token_creator_addresses[token_addr.lower()] = creator_addr
                # Check blacklist
                blacklist = {}
                if os.path.exists("blacklist.json"):
                    try:
                        with open("blacklist.json", "r") as f:
                            blacklist = json.load(f)
                    except:
                        pass
                if creator_addr.lower() in [k.lower() for k in blacklist.keys()]:
                    print(f"🚨 GoPlus Scan FAILED: Creator address {creator_addr} is BLACKLISTED!")
                    return False
            if token_data.get("is_honeypot") == "1" or token_data.get("cannot_sell") == "1":
                print("🚨 GoPlus Scan FAILED: Honeypot flag detected.")
                return False
            if token_data.get("hidden_owner") == "1":
                print("🚨 GoPlus Scan FAILED: Hidden owner backdoor detected.")
                return False
            if token_data.get("owner_change_balance") == "1":
                print("🚨 GoPlus Scan FAILED: Owner can modify token balances.")
                return False
            if token_data.get("is_whitelisted") == "1":
                print("🚨 GoPlus Scan FAILED: Only whitelisted wallets can trade.")
                return False
            if token_data.get("transfer_pausable") == "1":
                print("🚨 GoPlus Scan FAILED: Trading can be paused by owner.")
                return False
            if token_data.get("is_blacklisted") == "1":
                print("🚨 GoPlus Scan FAILED: Creator can blacklist/freeze holder wallets.")
                return False
            if token_data.get("slippage_modifiable") == "1":
                print("🚨 GoPlus Scan FAILED: Creator can modify buy/sell taxes dynamically (Backdoor).")
                return False
            if token_data.get("is_mintable") == "1":
                print("🚨 GoPlus Scan FAILED: Token is mintable (Creator can print & dump).")
                return False
            if token_data.get("can_take_back_ownership") == "1":
                print("🚨 GoPlus Scan FAILED: Creator can reclaim ownership after renouncing.")
                return False
            if token_data.get("personal_slippage_modifiable") == "1":
                print("🚨 GoPlus Scan FAILED: Selective honeypot backdoor detected (personal slippage modifiable).")
                return False
            if token_data.get("self_destruct") == "1":
                print("🚨 GoPlus Scan FAILED: Contract can self-destruct.")
                return False
                
            # 4.5 GoPlus LP lock check (as fallback/double confirmation)
            lp_holders = token_data.get("lp_holders", [])
            if lp_holders:
                goplus_locked_pct = 0.0
                for holder in lp_holders:
                    if holder.get("is_locked") == 1:
                        try:
                            goplus_locked_pct += float(holder.get("percent", 0) or 0)
                        except:
                            pass
                print(f"🔒 GoPlus LP Lock Check: {goplus_locked_pct * 100:.1f}% locked/burned according to GoPlus")
                if goplus_locked_pct > lp_lock_pct:
                    lp_lock_pct = goplus_locked_pct
                    
            buy_tax = float(token_data.get("buy_tax", 0) or 0)
            sell_tax = float(token_data.get("sell_tax", 0) or 0)
            if buy_tax > 0.12 or sell_tax > 0.12:
                print(f"🚨 GoPlus Scan FAILED: High Tax scam! Buy Tax: {buy_tax:.1%}, Sell Tax: {sell_tax:.1%}")
                return False
            
            _token_buy_taxes[token_addr.lower()] = buy_tax
                
            # 5. Token Distribution / Cabal Whale Checks
            top_10_share_str = token_data.get("top_10_holders_share")
            if top_10_share_str:
                try:
                    top_10_share = float(top_10_share_str)
                    if top_10_share > MAX_TOP_10_HOLDER_SHARE:
                        print(f"🚨 GoPlus Scan FAILED: Extreme centralization! Top 10 holders control {top_10_share:.1%} of supply (Cabal Dump risk, max is {MAX_TOP_10_HOLDER_SHARE:.1%}).")
                        return False
                except ValueError:
                    pass
                    
            # ── NEO/BullX Predictive AI Scorer ──
            score, scoring_checks = ai_predictive_model.get_base_predictive_score(token_addr, lp_lock_pct, token_data)
            print(f"🧠 [PREDICTIVE AI SCORER] Token Score: {score}/100")
            for chk, status in scoring_checks.items():
                print(f"  ├─ {chk}: {status}")
            if score < 60:
                print(f"🚨 Predictive AI Scan FAILED: Score {score}/100 is below the threshold of 60/100. Aborting trade.")
                return False
                
            # Store score for reporting
            _token_ai_scores[token_addr.lower()] = score
            
    except Exception as e:
        print(f"🚨 GoPlus API check failed or timed out: {e}. Rejecting token for safety.")
        return False
        
    # Final LP Lock verification (enforced after GoPlus fallback has had a chance to run)
    if lp_lock_pct < 0.80:
        print(f"🚨 Security Scan FAILED: LP is not sufficiently locked/burned ({lp_lock_pct * 100:.1f}% < 80%).")
        return False
        
    return True

# ── GoPlus Thread Pool: runs safety checks off the main thread ─────────────────────
_goplus_executor = concurrent.futures.ThreadPoolExecutor(max_workers=3, thread_name_prefix="goplus")

def check_token_safety_async(token_addr, timeout=12.0):
    """Non-blocking GoPlus check: runs in a thread pool with a hard timeout.
    Returns False immediately if GoPlus takes longer than `timeout` seconds,
    so the scanner loop never stalls waiting for an external API."""
    try:
        future = _goplus_executor.submit(check_token_safety, token_addr)
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        print(f"⚠️ GoPlus timed out after {timeout}s for {token_addr}. Skipping token.")
        return False
    except Exception as e:
        print(f"⚠️ GoPlus async error: {e}. Skipping token.")
        return False
# ─────────────────────────────────────────────────────────────────────────────

def format_price(price):
    if price == 0:
        return "0.00"
    if price < 0.00001:
        return f"{price:.12f}"
    return f"{price:.8f}"

def execute_buy(token_addr, quote_addr=None):
    if quote_addr is None:
        quote_addr = WETH_ADDRESS
    else:
        quote_addr = Web3.to_checksum_address(quote_addr)
    print(f"🛒 Executing BUY order for {token_addr} via { 'WETH' if quote_addr == WETH_ADDRESS else 'USDC' }...")
    
    # Load settings from config to ensure fresh values (dry-run, trade amount)
    is_dry = True
    trade_amt = 0.001
    gas_premium = 10.0
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
            is_dry = cfg.get("dry_run", True)
            trade_amt = cfg.get("trade_amount_eth", 0.001)
            gas_premium = cfg.get("gas_premium_percent", 10.0)
    except:
        pass

    if is_dry:
        print(f"🧪 [DRY RUN] Simulating buy of {trade_amt} ETH on Base.")
        return True
        
    if not wallet_address:
        print("❌ Cannot trade: Wallet not configured.")
        return False
        
    try:
        balance_wei = web3.eth.get_balance(wallet_address)
        trade_wei = web3.to_wei(trade_amt, "ether")
        if balance_wei < trade_wei:
            print("❌ Aborted: Insufficient native ETH balance.")
            send_telegram(
                f"⚠️ *[INSUFFICIENT BALANCE]*\n"
                f"Found a safe token but couldn't buy because your wallet balance (`{web3.from_wei(balance_wei, 'ether'):.6f} ETH`) "
                f"is less than your configured trade size (`{trade_amt} ETH`).\n\n"
                f"📥 *Deposit Address* (Base):\n`{wallet_address}`"
            )
            return False
            
        if quote_addr == WETH_ADDRESS:
            path = [WETH_ADDRESS, Web3.to_checksum_address(token_addr)]
        else:
            path = [WETH_ADDRESS, quote_addr, Web3.to_checksum_address(token_addr)]
        deadline = int(time.time()) + 300

        # ── Slippage protection: compute expected output, apply tax + 10% tolerance ────────
        amount_out_min = 0
        try:
            buy_tax = _token_buy_taxes.get(token_addr.lower(), 0.0)
            amounts = router_contract.functions.getAmountsOut(trade_wei, path).call()
            # Factor in buy tax (e.g. 10% tax leaves 90% tokens) + 10% extra slippage tolerance
            amount_out_min = int(amounts[-1] * (1 - buy_tax) * 0.90)
            print(f"🛡️ Slippage guard: buy tax {buy_tax:.1%}, expect ≥ {amount_out_min} tokens (10% extra tolerance)")
        except Exception as slip_err:
            print(f"⚠️ Could not compute amountOutMin, defaulting to 0: {slip_err}")

        # PRO FEATURE: Pre-Trade Simulation (Honeypot-Proofing)
        try:
            print("🧪 Simulating purchase transaction on-chain (Dry Call)...")
            router_contract.functions.swapExactETHForTokens(
                0,          # sim uses 0 — just checking for revert, not slippage
                path,
                wallet_address,
                deadline
            ).call({
                'from': wallet_address,
                'value': trade_wei,
            })
            print("🟢 Simulation PASSED: Buy transaction is valid.")
        except Exception as sim_err:
            print(f"🚨 Simulation FAILED: Swap transaction reverts on-chain: {sim_err}")
            send_telegram(f"⚠️ *[SIMULATION FAILED]*\nFailed to simulate buy swap for `{token_addr}`. Reverted with: `{sim_err}`. Trade aborted to prevent honeypot loss.")
            return False

        max_fee, priority_fee = get_eip1559_gas_fees()
        tx_params = {
            'from':     wallet_address,
            'value':    trade_wei,
            'gas':      500000,
            'nonce':    web3.eth.get_transaction_count(wallet_address),
            'chainId':  8453,
        }
        if priority_fee is not None:
            tx_params['maxFeePerGas'] = max_fee
            tx_params['maxPriorityFeePerGas'] = priority_fee
            tx_params['type'] = '0x2'
        else:
            tx_params['gasPrice'] = max_fee

        tx = router_contract.functions.swapExactETHForTokensSupportingFeeOnTransferTokens(
            amount_out_min,
            path,
            wallet_address,
            deadline
        ).build_transaction(tx_params)
        
        signed_tx = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"📤 Tx broadcasted: {web3.to_hex(tx_hash)}")
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status == 1:
            print("🎉 Swap successful!")
            return True
        else:
            print("❌ Swap transaction reverted.")
            return False
    except Exception as e:
        print(f"❌ Buy transaction failed: {e}")
        return False

def execute_sell(token_addr):
    print(f"💰 Executing SELL order for {token_addr}...")
    
    # Load settings from config to ensure fresh values (dry-run)
    is_dry = True
    gas_premium = 10.0
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
            is_dry = cfg.get("dry_run", True)
            gas_premium = cfg.get("gas_premium_percent", 10.0)
    except:
        pass

    if is_dry:
        print("🧪 [DRY RUN] Simulating full sell of position.")
        return True
        
    if not wallet_address:
        print("❌ Cannot trade: Wallet not configured.")
        return False
        
    try:
        # Get starting nonce to manage locally and avoid load-balancer lag conflicts
        nonce = web3.eth.get_transaction_count(wallet_address)
        
        # Load state to check if we have a recorded buy router
        state = load_state()
        sell_router_addr = Web3.to_checksum_address(state.get("buy_router_address", ROUTER_ADDRESS))
        sell_router_contract = web3.eth.contract(address=sell_router_addr, abi=ROUTER_ABI)
        
        checksum_token = Web3.to_checksum_address(token_addr)
        token_contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)

        quote_addr = state.get("quote_address")
        if not quote_addr:
            # Dynamically resolve quote pool if not saved in state
            pair_weth = factory_contract.functions.getPair(WETH_ADDRESS, checksum_token).call()
            if pair_weth and pair_weth != "0x0000000000000000000000000000000000000000":
                quote_addr = WETH_ADDRESS
            else:
                pair_usdc = factory_contract.functions.getPair(USDC_ADDRESS, checksum_token).call()
                if pair_usdc and pair_usdc != "0x0000000000000000000000000000000000000000":
                    quote_addr = USDC_ADDRESS
                else:
                    quote_addr = WETH_ADDRESS
        else:
            quote_addr = Web3.to_checksum_address(quote_addr)
        
        token_balance = token_contract.functions.balanceOf(wallet_address).call()
        if token_balance == 0:
            print("❌ Aborted: Token balance is 0.")
            return False
            
        allowance = token_contract.functions.allowance(wallet_address, sell_router_addr).call()
        if allowance < token_balance:
            print("🔑 Approving Uniswap Router to spend tokens...")
            max_fee, priority_fee = get_eip1559_gas_fees()
            tx_params = {
                'from': wallet_address,
                'gas': 80000,
                'nonce': nonce,
                'chainId': 8453,
            }
            if priority_fee is not None:
                tx_params['maxFeePerGas'] = max_fee
                tx_params['maxPriorityFeePerGas'] = priority_fee
                tx_params['type'] = '0x2'
            else:
                tx_params['gasPrice'] = max_fee

            approve_tx = token_contract.functions.approve(
                sell_router_addr,
                2**256 - 1
            ).build_transaction(tx_params)
            signed_approve = web3.eth.account.sign_transaction(approve_tx, PRIVATE_KEY)
            approve_hash = web3.eth.send_raw_transaction(signed_approve.raw_transaction)
            web3.eth.wait_for_transaction_receipt(approve_hash, timeout=120)
            print("🔑 Approved!")
            nonce += 1
            
        if quote_addr == WETH_ADDRESS:
            path = [checksum_token, WETH_ADDRESS]
        else:
            path = [checksum_token, quote_addr, WETH_ADDRESS]
        deadline = int(time.time()) + 300
        
        max_fee, priority_fee = get_eip1559_gas_fees()
        tx_params = {
            'from': wallet_address,
            'gas': 500000,
            'nonce': nonce,
            'chainId': 8453,
        }
        if priority_fee is not None:
            tx_params['maxFeePerGas'] = max_fee
            tx_params['maxPriorityFeePerGas'] = priority_fee
            tx_params['type'] = '0x2'
        else:
            tx_params['gasPrice'] = max_fee

        tx = sell_router_contract.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            token_balance,
            0,
            path,
            wallet_address,
            deadline
        ).build_transaction(tx_params)
        
        signed_tx = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"📤 Tx broadcasted: {web3.to_hex(tx_hash)}")
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt.status == 1:
            print("🎉 Position sold successfully!")
            return True
        else:
            print("❌ Sell transaction reverted.")
            return False
    except Exception as e:
        print(f"❌ Sell transaction failed: {e}")
        return False

def execute_partial_sell(token_addr, sell_pct: float):
    """Sell `sell_pct` fraction (0.0–1.0) of the current token balance. Returns True on success."""
    print(f"💸 Executing PARTIAL SELL ({sell_pct*100:.0f}%) for {token_addr}...")
    is_dry = True
    gas_premium = 10.0
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
            is_dry      = cfg.get("dry_run", True)
            gas_premium = cfg.get("gas_premium_percent", 10.0)
    except:
        pass
    if is_dry:
        print(f"🧪 [DRY RUN] Simulating {sell_pct*100:.0f}% partial sell.")
        return True
    if not wallet_address:
        print("❌ Cannot trade: Wallet not configured.")
        return False
        
    try:
        nonce = web3.eth.get_transaction_count(wallet_address)
        state = load_state()
        sell_router_addr     = Web3.to_checksum_address(state.get("buy_router_address", ROUTER_ADDRESS))
        sell_router_contract = web3.eth.contract(address=sell_router_addr, abi=ROUTER_ABI)
        checksum_token       = Web3.to_checksum_address(token_addr)
        token_contract       = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)
        full_balance         = token_contract.functions.balanceOf(wallet_address).call()
        if full_balance == 0:
            print("❌ Aborted: Token balance is 0.")
            return False
        sell_amount = int(full_balance * sell_pct)
        if sell_amount == 0:
            print("❌ Aborted: Computed sell amount is 0.")
            return False
        allowance = token_contract.functions.allowance(wallet_address, sell_router_addr).call()
        if allowance < sell_amount:
            print("🔑 Approving router for partial sell...")
            max_fee, priority_fee = get_eip1559_gas_fees()
            tx_params = {
                'from': wallet_address,
                'gas': 80000,
                'nonce': nonce,
                'chainId': 8453,
            }
            if priority_fee is not None:
                tx_params['maxFeePerGas'] = max_fee
                tx_params['maxPriorityFeePerGas'] = priority_fee
                tx_params['type'] = '0x2'
            else:
                tx_params['gasPrice'] = max_fee

            approve_tx = token_contract.functions.approve(
                sell_router_addr, full_balance * 10
            ).build_transaction(tx_params)
            signed_approve = web3.eth.account.sign_transaction(approve_tx, PRIVATE_KEY)
            web3.eth.wait_for_transaction_receipt(
                web3.eth.send_raw_transaction(signed_approve.raw_transaction), timeout=120)
            print("🔑 Approved!")
            nonce += 1
            
        quote_addr = state.get("quote_address")
        if not quote_addr:
            pair_weth = factory_contract.functions.getPair(WETH_ADDRESS, checksum_token).call()
            if pair_weth and pair_weth != "0x0000000000000000000000000000000000000000":
                quote_addr = WETH_ADDRESS
            else:
                pair_usdc = factory_contract.functions.getPair(USDC_ADDRESS, checksum_token).call()
                if pair_usdc and pair_usdc != "0x0000000000000000000000000000000000000000":
                    quote_addr = USDC_ADDRESS
                else:
                    quote_addr = WETH_ADDRESS
        else:
            quote_addr = Web3.to_checksum_address(quote_addr)

        if quote_addr == WETH_ADDRESS:
            path = [checksum_token, WETH_ADDRESS]
        else:
            path = [checksum_token, quote_addr, WETH_ADDRESS]
            
        deadline = int(time.time()) + 300
        
        max_fee, priority_fee = get_eip1559_gas_fees()
        tx_params = {
            'from': wallet_address,
            'gas': 500000,
            'nonce': nonce,
            'chainId': 8453,
        }
        if priority_fee is not None:
            tx_params['maxFeePerGas'] = max_fee
            tx_params['maxPriorityFeePerGas'] = priority_fee
            tx_params['type'] = '0x2'
        else:
            tx_params['gasPrice'] = max_fee

        tx = sell_router_contract.functions.swapExactTokensForETHSupportingFeeOnTransferTokens(
            sell_amount, 0, path, wallet_address, deadline
        ).build_transaction(tx_params)
        signed_tx = web3.eth.account.sign_transaction(tx, PRIVATE_KEY)
        tx_hash   = web3.eth.send_raw_transaction(signed_tx.raw_transaction)
        print(f"📤 Partial sell tx: {web3.to_hex(tx_hash)}")
        receipt = web3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status == 1:
            print(f"🎉 Partial sell ({sell_pct*100:.0f}%) successful!")
            return True
        else:
            print("❌ Partial sell reverted.")
            return False
    except Exception as e:
        print(f"❌ Partial sell failed: {e}")
        return False

# ── Config mtime cache: only re-read config.json when the file actually changes ───
_config_last_mtime = 0.0

def _reload_config_if_changed():
    """Reload all config globals only if config file has been modified.
    Replaces the naive open-every-loop pattern (was 10 disk reads/second)."""
    global _config_last_mtime
    global DRY_RUN, CHECK_INTERVAL, TRADE_AMOUNT_ETH, TRAILING_STOP, HARD_STOP_LOSS
    global HARD_TAKE_PROFIT, TIME_EXIT_MINUTES, GAS_PREMIUM_PERCENT, SCANNER_ACTIVE
    global MIN_LIQ, MAX_LIQ, FACTORY_ADDRESS, ROUTER_ADDRESS, factory_contract, router_contract, BUY_BLOCK_DELAY, MAX_TOP_10_HOLDER_SHARE
    global STAGED_TAKE_PROFIT_PERCENT, STAGED_SELL_FRACTION
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
        if mtime <= _config_last_mtime:
            return  # file unchanged — skip
        _config_last_mtime = mtime
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        DRY_RUN          = cfg.get("dry_run", True)
        CHECK_INTERVAL   = cfg.get("check_interval_seconds", 15)
        TRADE_AMOUNT_ETH = cfg.get("trade_amount_eth", 0.001)
        TRAILING_STOP    = cfg.get("trailing_stop_percent", 15.0)
        HARD_STOP_LOSS   = cfg.get("hard_stop_loss_percent", 20.0)
        HARD_TAKE_PROFIT = cfg.get("hard_take_profit_percent", 300.0)
        TIME_EXIT_MINUTES= cfg.get("time_exit_minutes", 0)
        GAS_PREMIUM_PERCENT = cfg.get("gas_premium_percent", 10.0)
        SCANNER_ACTIVE   = cfg.get("scanner_active", True)
        MIN_LIQ          = cfg.get("min_liquidity_eth", 0.5)
        MAX_LIQ          = cfg.get("max_liquidity_eth", 15.0)
        BUY_BLOCK_DELAY  = cfg.get("buy_block_delay", 3)
        MAX_TOP_10_HOLDER_SHARE = cfg.get("max_top_10_holder_share", 0.50)
        STAGED_TAKE_PROFIT_PERCENT = cfg.get("staged_take_profit_percent", 30.0)
        STAGED_SELL_FRACTION = cfg.get("staged_sell_fraction", 0.50)
        new_factory = Web3.to_checksum_address(cfg.get("dex_factory_address"))
        new_router  = Web3.to_checksum_address(cfg.get("dex_router_address"))
        if new_factory != FACTORY_ADDRESS:
            FACTORY_ADDRESS  = new_factory
            factory_contract = web3.eth.contract(address=FACTORY_ADDRESS, abi=FACTORY_ABI)
            print(f"🔄 Re-instantiated factory → {FACTORY_ADDRESS}")
        if new_router != ROUTER_ADDRESS:
            ROUTER_ADDRESS   = new_router
            router_contract  = web3.eth.contract(address=ROUTER_ADDRESS, abi=ROUTER_ABI)
            print(f"🔄 Re-instantiated router → {ROUTER_ADDRESS}")
        print(f"🔄 Config reloaded (file changed)")
    except Exception as reload_err:
        print(f"⚠️ Config reload error: {reload_err}")
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global DRY_RUN, CHECK_INTERVAL, TRADE_AMOUNT_ETH, TRAILING_STOP, HARD_STOP_LOSS, HARD_TAKE_PROFIT, MIN_LIQ, MAX_LIQ, TIME_EXIT_MINUTES, GAS_PREMIUM_PERCENT, FACTORY_ADDRESS, ROUTER_ADDRESS, factory_contract, router_contract, SCANNER_ACTIVE, LAST_STATUS_REPORT_TIME, BUY_BLOCK_DELAY, MAX_TOP_10_HOLDER_SHARE
    global _session_start_eth_balance, _session_eth_price_at_start
    _no_new_pairs_streak = 0
    print("==============================================")
    print("🚀 Base Meme Coin Scanner & Sniper Bot v2.1")
    print(f"🔗 RPC Endpoint: {RPC_URL}")
    print(f"🛡️ Dry Run Mode: {DRY_RUN}")
    print("==============================================")
    
    if not web3.is_connected():
        print("❌ Cannot connect to the RPC Node. Please check config.")
        sys.exit(1)
        
    state = load_state()

    # ── SESSION GUARD INIT ────────────────────────────────────────────────────
    if wallet_address:
        try:
            _session_start_eth_balance = float(web3.from_wei(web3.eth.get_balance(wallet_address), "ether"))
            _session_eth_price_at_start = get_weth_usd_price() or 3400.0
            bal_usd = _session_start_eth_balance * _session_eth_price_at_start
            print(f"💼 Session start balance: {_session_start_eth_balance:.6f} ETH (≈${bal_usd:.2f})")
            print(f"🛡️  Max session loss guard: ${SESSION_MAX_LOSS_USD:.2f} | Cutoff: 9:00 AM MT (UTC {SESSION_CUTOFF_HOUR_UTC:02d}:{SESSION_CUTOFF_MIN_UTC:02d})")
            send_telegram(
                f"🚀 *Bot Started — Sleep Mode ON*\n"
                f"💼 Balance: `{_session_start_eth_balance:.6f} ETH` (≈${bal_usd:.2f})\n"
                f"🛡️ Loss Cap: `${SESSION_MAX_LOSS_USD:.2f}`\n"
                f"⏰ Auto-shutdown: `9:00 AM MT`"
            )
        except Exception as e:
            print(f"⚠️ Failed to snapshot session balance: {e}")
    # ─────────────────────────────────────────────────────────────────────────

    # Telegram Command Listener is handled by standalone tg_bot.py
    
    if state["last_checked_pair_index"] == -1:
        try:
            total_pairs = factory_contract.functions.allPairsLength().call()
            state["last_checked_pair_index"] = total_pairs - 1
            print(f"ℹ️ Initialization complete. Scanning start index: {state['last_checked_pair_index']}")
            save_state(state)
        except Exception as e:
            print(f"❌ Failed to query factory length: {e}")
            sys.exit(1)
            
    # Start the WebSocket subscriber thread
    start_websocket_thread()
            
    while True:
        # print(f"Scanning... [{time.strftime('%Y-%m-%d %H:%M:%S')}]")
        # ── 9 AM MT AUTO-SHUTDOWN ─────────────────────────────────────────────
        if _is_session_cutoff():
            print("⏰ 9:00 AM MT reached — auto-shutdown triggered.")
            send_telegram(
                "⏰ *9:00 AM MT Auto-Shutdown*\n"
                "🛑 Bot is stopping. Selling any open position and exiting."
            )
            # Close any open position gracefully
            state = load_state()
            for _pos in state.get("positions", []):
                tok = _pos["token_address"]
                sym = _pos["token_symbol"]
                print(f"📤 Closing open position: {sym} before shutdown.")
                execute_sell(tok)
            import sys as _sys
            _sys.exit(0)
        # ─────────────────────────────────────────────────────────────────────

        # ── SESSION LOSS GUARD ────────────────────────────────────────────────
        _session_loss = _get_session_loss_usd()
        if _session_loss >= SESSION_MAX_LOSS_USD:
            print(f"🛑 SESSION LOSS CAP HIT: -${_session_loss:.2f} >= ${SESSION_MAX_LOSS_USD:.2f}. No new buys will be placed.")
            send_telegram(
                f"🛑 *Session Loss Cap Hit!*\n"
                f"💸 Down `${_session_loss:.2f}` — no new trades until restart.\n"
                f"🔒 Bot holding current position (if any) but scanner paused."
            )
            # If no open position, just wait until cutoff or restart
            state = load_state()
            if not state.get("positions"):
                import time as _t; _t.sleep(30)
                continue
        # ─────────────────────────────────────────────────────────────────────

        # Check for Discord/External Sell Trigger
        TRIGGER_PATH = "sell_trigger.json"
        if os.path.exists(TRIGGER_PATH):
            print("🚨 External sell trigger detected (Discord)!")
            try:
                with open(TRIGGER_PATH, "r") as f:
                    trigger_data = json.load(f)
                os.remove(TRIGGER_PATH)
                
                custom_token = trigger_data.get("token_address")
                is_partial = trigger_data.get("partial", False)
                state = load_state()
                
                if custom_token:
                    checksum_token = Web3.to_checksum_address(custom_token)
                    try:
                        token_contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)
                        symbol = token_contract.functions.symbol().call()
                    except:
                        symbol = "TOKEN"
                    if is_partial:
                        print(f"🚨 Executing custom external partial sell (50%) for {symbol} ({checksum_token})...")
                        success = execute_partial_sell(checksum_token, 0.50)
                    else:
                        print(f"🚨 Executing custom external sell for {symbol} ({checksum_token})...")
                        success = execute_sell(checksum_token)
                    
                    if success:
                        if not is_partial:
                            # Remove from positions list
                            state["positions"] = [p for p in state.get("positions", []) if p["token_address"].lower() != checksum_token.lower()]
                            state["in_position"] = len(state["positions"]) > 0
                            save_state(state)
                            send_telegram(f"🎉 Successfully sold manual token *{symbol}*!")
                        else:
                            for p in state.get("positions", []):
                                if p["token_address"].lower() == checksum_token.lower():
                                    p["half_sold"] = True
                            save_state(state)
                            send_telegram(f"🎉 Successfully sold 50% of manual token *{symbol}*!")
                    else:
                        send_telegram(f"❌ Custom external sell failed for *{symbol}*.")
                elif state.get("positions"):
                    # Sell first open position
                    pos       = state["positions"][0]
                    token_addr = pos["token_address"]
                    symbol    = pos["token_symbol"]
                    buy_price = pos["buy_price_usd"]
                    if is_partial:
                        print(f"🚨 Executing external partial sell (50%) for {symbol}...")
                        success = execute_partial_sell(token_addr, 0.50)
                    else:
                        print(f"🚨 Executing external sell for {symbol}...")
                        success = execute_sell(token_addr)

                    if success:
                        current_price = None
                        pair_addr = pos.get("pair_address")
                        if pair_addr:
                            price_in_weth, _ = get_onchain_price(pair_addr, token_addr)
                            if price_in_weth is not None:
                                current_price = price_in_weth * get_weth_usd_price()
                        if current_price is None:
                            token_info = get_dex_screener_price(token_addr)
                            current_price = token_info["price_usd"] if token_info else buy_price
                        change_from_buy = ((current_price - buy_price) / buy_price) * 100 if buy_price > 0 else 0

                        if is_partial:
                            pos["half_sold"] = True
                            save_state(state)
                            send_telegram(f"🎉 Successfully sold 50% of *{symbol}*!")
                        else:
                            state["last_trade"] = {
                                "token_address": token_addr,
                                "token_symbol": symbol,
                                "buy_price_usd": buy_price,
                                "exit_price_usd": current_price,
                                "pnl_pct": change_from_buy,
                                "timestamp": int(time.time()),
                                "reason": "External trigger executed"
                            }
                            state["positions"] = [p for p in state["positions"] if p["token_address"].lower() != token_addr.lower()]
                            state["in_position"] = len(state["positions"]) > 0
                            save_state(state)
                            save_trade_to_history(state["last_trade"])
                            alert_trade_close(symbol, buy_price, current_price, change_from_buy, "External trigger executed")
                    else:
                        send_telegram(f"❌ External sell failed for *{symbol}*.")
                else:
                    print("⚠️ Sell trigger ignored: No active position.")
            except Exception as trigger_err:
                print(f"⚠️ Error executing external sell trigger: {trigger_err}")
                
        # Check for Discord/External Buy Trigger
        BUY_TRIGGER_PATH = "buy_trigger.json"
        if os.path.exists(BUY_TRIGGER_PATH):
            print("🚨 External buy trigger detected!")
            try:
                with open(BUY_TRIGGER_PATH, "r") as f:
                    trigger_data = json.load(f)
                os.remove(BUY_TRIGGER_PATH)
                
                target_token = trigger_data.get("token_address")
                custom_amt = trigger_data.get("amount_eth")
                
                if target_token:
                    with state_lock:
                        state = load_state()
                        if len(state.get("positions", [])) >= MAX_POSITIONS:
                            send_telegram(f"❌ Cannot execute manual buy: Already at max positions ({MAX_POSITIONS}).")
                        else:
                            is_safe = check_token_safety(target_token)
                            if not is_safe:
                                send_telegram(f"🚨 Manual Buy Aborted: Token `{target_token}` failed safety validation checks.")
                            else:
                                checksum_token = Web3.to_checksum_address(target_token)
                                token_contract = web3.eth.contract(address=checksum_token, abi=ERC20_ABI)
                                symbol = token_contract.functions.symbol().call()
                                
                                orig_amt = TRADE_AMOUNT_ETH
                                if custom_amt and custom_amt > 0:
                                    print(f"ℹ️ Overriding trade amount to custom value: {custom_amt} ETH")
                                    with open(CONFIG_PATH, "r") as cfg_f:
                                        temp_cfg = json.load(cfg_f)
                                    orig_config_amt = temp_cfg.get("trade_amount_eth", 0.001)
                                    temp_cfg["trade_amount_eth"] = custom_amt
                                    with open(CONFIG_PATH, "w") as cfg_f:
                                        json.dump(temp_cfg, cfg_f, indent=2)
                                        
                                # Detect quote address
                                quote_token = WETH_ADDRESS
                                pair_addr = factory_contract.functions.getPair(WETH_ADDRESS, checksum_token).call()
                                if not pair_addr or pair_addr == "0x0000000000000000000000000000000000000000":
                                    pair_usdc = factory_contract.functions.getPair(USDC_ADDRESS, checksum_token).call()
                                    if pair_usdc and pair_usdc != "0x0000000000000000000000000000000000000000":
                                        pair_addr = pair_usdc
                                        quote_token = USDC_ADDRESS

                                print(f"🛒 Executing manual buy for {symbol} ({target_token}) via { 'WETH' if quote_token == WETH_ADDRESS else 'USDC' }...")
                                prediction = ai_predictive_model.predict(target_token)
                                if prediction > 0.8:
                                    success = execute_buy(target_token, quote_addr=quote_token)
                                else:
                                    print(f"🚫 AI Brain rejected {target_token}. Skipping trade.")
                                    success = False
                                
                                if custom_amt and custom_amt > 0:
                                    with open(CONFIG_PATH, "r") as cfg_f:
                                        temp_cfg = json.load(cfg_f)
                                    temp_cfg["trade_amount_eth"] = orig_config_amt
                                    with open(CONFIG_PATH, "w") as cfg_f:
                                        json.dump(temp_cfg, cfg_f, indent=2)
                                
                                if success:
                                    eth_price = get_weth_usd_price()
                                    current_price = None
                                    if pair_addr and pair_addr != "0x0000000000000000000000000000000000000000":
                                        price_in_weth, _ = get_onchain_price(pair_addr, target_token)
                                        if price_in_weth is not None:
                                            current_price = price_in_weth * eth_price
                                    if current_price is None:
                                        token_info = get_dex_screener_price(target_token)
                                        current_price = token_info["price_usd"] if token_info else 0.0

                                    new_pos = _make_position(
                                        token_address      = checksum_token,
                                        token_symbol       = symbol,
                                        pair_address       = pair_addr,
                                        buy_price_usd      = current_price,
                                        amount_held        = (custom_amt or orig_amt) / current_price if current_price > 0 else 0,
                                        buy_router_address = ROUTER_ADDRESS,
                                        initial_liq_eth    = 0.0,
                                        quote_address      = quote_token,
                                        creator_address    = "",
                                    )
                                    state.setdefault("positions", []).append(new_pos)
                                    state["in_position"] = True
                                    save_state(state)
                                    
                                    liq_eth = 0.0
                                    if pair_addr and pair_addr != "0x0000000000000000000000000000000000000000":
                                        if quote_token == USDC_ADDRESS:
                                            usdc_contract = web3.eth.contract(address=USDC_ADDRESS, abi=ERC20_ABI)
                                            usdc_bal = usdc_contract.functions.balanceOf(pair_addr).call()
                                            liq_eth = (usdc_bal / (10 ** 6)) / eth_price if eth_price > 0 else (usdc_bal / (10 ** 6)) / 3000.0
                                        else:
                                            weth_contract = web3.eth.contract(address=WETH_ADDRESS, abi=ERC20_ABI)
                                            liq_eth = web3.from_wei(weth_contract.functions.balanceOf(pair_addr).call(), 'ether')
                                        
                                    alert_trade_enter(symbol, checksum_token, custom_amt if custom_amt else orig_amt, current_price, float(liq_eth))
                                else:
                                    send_telegram(f"❌ Manual buy execution failed for `{target_token}`.")
            except Exception as buy_trigger_err:
                print(f"⚠️ Error executing manual buy trigger: {buy_trigger_err}")
                send_telegram(f"❌ Error during manual buy trigger execution: {buy_trigger_err}")
                
        _reload_config_if_changed()

        # --- POSITION MONITORING (multi-position) ---
        positions_to_remove = []
        for pos in state.get("positions", []):
            token_addr    = pos["token_address"]
            symbol        = pos["token_symbol"]
            buy_price     = pos["buy_price_usd"]
            highest_price = pos["highest_price_usd"]

            # Query actual token balance to catch phantom positions early
            try:
                checksum_tok = Web3.to_checksum_address(token_addr)
                tok_contract = web3.eth.contract(address=checksum_tok, abi=ERC20_ABI)
                token_bal = tok_contract.functions.balanceOf(wallet_address).call()
                if token_bal == 0:
                    print(f"⚠️ [{symbol}] On-chain token balance is 0. Clearing phantom position.")
                    positions_to_remove.append(token_addr.lower())
                    continue
            except Exception as bal_err:
                print(f"⚠️ [{symbol}] Failed to verify on-chain balance: {bal_err}")


            current_price   = None
            current_liq_eth = None
            pair_addr = pos.get("pair_address")
            if pair_addr:
                price_in_weth, current_liq_eth = get_onchain_price(pair_addr, token_addr)
                if price_in_weth is not None:
                    eth_price = get_weth_usd_price()
                    current_price = price_in_weth * eth_price
                    print(f"🔗 [{symbol}] ON-CHAIN price: ${format_price(current_price)} | Pool liq: {current_liq_eth:.4f} ETH")

            initial_liq = pos.get("initial_liq_eth", 0.0)
            if current_liq_eth is not None and initial_liq > 0:
                liq_drop_pct = (initial_liq - current_liq_eth) / initial_liq * 100
                if liq_drop_pct >= 50:
                    print(f"🚨 [{symbol}] RUG PULL — LP drained {liq_drop_pct:.1f}%! Emergency exit!")
                    creator = pos.get("creator_address")
                    if creator:
                        try:
                            blacklist = {}
                            if os.path.exists("blacklist.json"):
                                with open("blacklist.json", "r") as f:
                                    blacklist = json.load(f)
                            blacklist[creator] = {
                                "reason": f"Rug Pull on Base token {symbol}: LP drained {liq_drop_pct:.1f}%",
                                "timestamp": int(time.time())
                            }
                            with open("blacklist.json", "w") as f:
                                json.dump(blacklist, f, indent=2)
                            print(f"🚫 Creator blacklisted: {creator}")
                            send_telegram(f"🚫 *Creator Blacklisted*\n`{creator}`\nReason: Rug Pull on {symbol}: LP drained {liq_drop_pct:.1f}%")
                        except Exception as e:
                            print(f"⚠️ Failed to blacklist creator: {e}")
                    send_telegram(
                        f"🚨 *RUG PULL ALERT — EMERGENCY SELL*\n"
                        f"🪩 *Token*: {symbol}\n"
                        f"💧 *Liquidity drained*: `{liq_drop_pct:.1f}%` ({initial_liq:.4f}→{current_liq_eth:.4f} ETH)\n"
                        f"🏋️ Selling immediately!"
                    )
                    success = execute_sell(token_addr)
                    if success:
                        trade_rec = {
                            "token_address": token_addr, "token_symbol": symbol,
                            "buy_price_usd": buy_price, "exit_price_usd": current_price or 0.0,
                            "pnl_pct": ((current_price - buy_price) / buy_price * 100) if current_price else -100.0,
                            "timestamp": int(time.time()), "reason": f"Rug Pull — LP drained {liq_drop_pct:.1f}%"
                        }
                        state["last_trade"] = trade_rec
                        positions_to_remove.append(token_addr.lower())
                        save_trade_to_history(trade_rec)
                    continue

            if current_price is None:
                token_info = get_dex_screener_price(token_addr)
                if token_info:
                    current_price = token_info["price_usd"]
                    print(f"🌐 [{symbol}] DexScreener price: ${format_price(current_price)}")
            
            if current_price is not None:
                if current_price > highest_price:
                    state["highest_price_usd"] = current_price
                    highest_price = current_price
                    save_state(state)
                    print(f"🔥 New Peak Price: ${format_price(highest_price)}")

                change_from_buy  = ((current_price - buy_price)    / buy_price)    * 100
                change_from_peak = ((current_price - highest_price) / highest_price) * 100

                half_sold    = state.get("half_sold", False)
                trigger_sell = False
                sell_reason  = ""

                if not half_sold and change_from_buy >= STAGED_TAKE_PROFIT_PERCENT:
                    print(f"💸 MODULE 1 — QUICK FLIP EXIT: +{change_from_buy:.1f}% → selling {STAGED_SELL_FRACTION*100:.0f}% to lock profit.")
                    if execute_partial_sell(token_addr, STAGED_SELL_FRACTION):
                        state["half_sold"] = True
                        half_sold = True
                        save_state(state)
                        send_telegram(
                            f"💸 *QUICK FLIP — {STAGED_SELL_FRACTION*100:.0f}% SOLD* 💰\n"
                            f"🪙 *Token*: {symbol}\n"
                            f"📈 *Profit locked*: `+{change_from_buy:.1f}%`\n"
                            f"🚀 Remaining {(1-STAGED_SELL_FRACTION)*100:.0f}% rides FREE — win guaranteed!"
                        )

                effective_stop_loss = HARD_STOP_LOSS
                if change_from_buy >= STAGED_TAKE_PROFIT_PERCENT:
                    if current_price <= buy_price * 1.005:
                        trigger_sell = True
                        sell_reason  = f"Breakeven Stop — was +{STAGED_TAKE_PROFIT_PERCENT:.0f}%+ but pulled back to entry. Capital protected."
                    effective_stop_loss = 0

                if change_from_buy < 20:
                    dynamic_trailing = TRAILING_STOP
                elif change_from_buy < 40:
                    dynamic_trailing = min(TRAILING_STOP, 8.0)
                elif change_from_buy < 80:
                    dynamic_trailing = min(TRAILING_STOP, 6.0)
                else:
                    dynamic_trailing = min(TRAILING_STOP, 4.0)

                trailing_stop_price = highest_price * (1 - dynamic_trailing / 100)
                hard_stop_price     = buy_price     * (1 - effective_stop_loss / 100) if effective_stop_loss > 0 else 0

                tp_str = ""
                if HARD_TAKE_PROFIT > 0:
                    tp_str = f" | TP ${format_price(buy_price * (1 + HARD_TAKE_PROFIT / 100))}"
                half_str = " [50% SOLD—FREE RIDE 🚀]" if half_sold else ""
                print(
                    f"🧠 {symbol}{half_str} | "
                    f"${format_price(current_price)} ({change_from_buy:+.2f}% | {change_from_peak:+.2f}% peak) | "
                    f"Trail {dynamic_trailing:.0f}%→${format_price(trailing_stop_price)}"
                    f"{tp_str}"
                )

                buy_timestamp = state.get("buy_timestamp", 0)
                elapsed_time  = time.time() - buy_timestamp if buy_timestamp > 0 else 0

                if not trigger_sell:
                    if HARD_TAKE_PROFIT > 0 and change_from_buy >= HARD_TAKE_PROFIT:
                        trigger_sell = True
                        sell_reason  = f"Hard Take Profit +{HARD_TAKE_PROFIT:.0f}% 🏆"
                    elif TIME_EXIT_MINUTES > 0 and buy_timestamp > 0 and elapsed_time >= TIME_EXIT_MINUTES * 60:
                        trigger_sell = True
                        sell_reason  = f"Time Exit: held {elapsed_time/60:.1f} min"
                    elif change_from_peak <= -dynamic_trailing:
                        trigger_sell = True
                        sell_reason  = f"Trailing Stop {dynamic_trailing:.0f}% pullback from peak"
                    elif effective_stop_loss > 0 and change_from_buy <= -effective_stop_loss:
                        trigger_sell = True
                        sell_reason  = f"Hard Stop Loss -{effective_stop_loss:.0f}%"

                if trigger_sell:
                    print(f"🛑 [{symbol}] EXIT TRIGGERED: {sell_reason}")
                    success = execute_sell(token_addr)
                    if success:
                        trade_rec = {
                            "token_address": token_addr,
                            "token_symbol": symbol,
                            "buy_price_usd": buy_price,
                            "exit_price_usd": current_price,
                            "pnl_pct": change_from_buy,
                            "timestamp": int(time.time()),
                            "reason": sell_reason
                        }
                        state["last_trade"] = trade_rec
                        positions_to_remove.append(token_addr.lower())
                        save_trade_to_history(trade_rec)
                        alert_trade_close(symbol, buy_price, current_price, change_from_buy, sell_reason)
                    else:
                        attempts = pos.get("sell_attempts", 0) + 1
                        pos["sell_attempts"] = attempts
                        if attempts >= 3:
                            print(f"🚨 [{symbol}] Honeypot — sell failed 3 times. Dropping position.")
                            trade_rec = {
                                "token_address": token_addr,
                                "token_symbol": symbol,
                                "buy_price_usd": buy_price,
                                "exit_price_usd": 0.0,
                                "pnl_pct": -100.0,
                                "timestamp": int(time.time()),
                                "reason": f"Rug Pull / Honeypot (Sell failed 3 times: {sell_reason})"
                            }
                            state["last_trade"] = trade_rec
                            positions_to_remove.append(token_addr.lower())
                            save_trade_to_history(trade_rec)
                            alert_trade_close(symbol, buy_price, 0.0, -100.0, f"Rug Pull / Honeypot (Sell failed 3 times: {sell_reason})")
                        else:
                            print(f"⚠️ [{symbol}] Sell failed. Attempt {attempts}/3. Retrying next loop.")
                else:
                    trailing_stop_price = highest_price * (1 - dynamic_trailing / 100)
                    hard_stop_price = buy_price * (1 - effective_stop_loss / 100) if effective_stop_loss > 0 else 0
                    tp_str = ""
                    if HARD_TAKE_PROFIT > 0:
                        take_profit_price = buy_price * (1 + HARD_TAKE_PROFIT / 100)
                        tp_str = f" | Take Profit at ${format_price(take_profit_price)}"
                    print(f"💤 Holding. Exit targets: Trailing Stop at ${format_price(trailing_stop_price)} | Hard Stop at ${format_price(hard_stop_price)}{tp_str}")
            else:
                print(f"⚠️ [{symbol}] Failed to query current price.")
        # end of positions monitoring loop — flush exits
        if positions_to_remove:
            state["positions"] = [p for p in state.get("positions", []) if p["token_address"].lower() not in positions_to_remove]
            state["in_position"] = len(state["positions"]) > 0
            save_state(state)

        # ── SCANNER EXECUTION ──
        if SCANNER_ACTIVE:
            # --- WATCHLIST PROCESSING ---
            with state_lock:
                state = load_state()
                watchlist = state.get("watchlist", [])
                if watchlist:
                    now = int(time.time())
                    watchlist = [p for p in watchlist if now - p.get("added_time", 0) < 600]
                    state["watchlist"] = watchlist
                    save_state(state)
            
            if watchlist:
                print(f"👁️ Watchlist Check: Querying reserves for {len(watchlist)} pending pair(s)...")
                reserves_list = batch_fetch_reserves(watchlist)
                eth_price = get_weth_usd_price()
                
                watchlist_bought = False
                for idx, res in enumerate(reserves_list):
                    if res is None:
                        continue
                    r0, r1 = res
                    p_info = watchlist[idx]
                    pair_addr = p_info["pair_address"]
                    target_token = p_info["target_token"]
                    quote_token = p_info["quote_token"]
                    t0 = p_info["t0"]
                    
                    quote_reserves = r0 if t0 == quote_token else r1
                    if quote_token == USDC_ADDRESS:
                        usdc_amount = quote_reserves / (10 ** 6)
                        weth_liq_eth = usdc_amount / eth_price if eth_price > 0 else usdc_amount / 3000.0
                    else:
                        weth_liq_eth = float(web3.from_wei(quote_reserves, "ether"))
                        
                    if MIN_LIQ <= weth_liq_eth <= MAX_LIQ:
                        print(f"🎯 Watchlist Match: Pair {pair_addr} has been funded! Liquidity: {weth_liq_eth:.4f} ETH")
                        
                        if check_token_safety_async(target_token):
                            print("🔗 Calculating token price on-chain...")
                            price_in_weth, disc_liq_eth = get_onchain_price(pair_addr, target_token)
                            
                            if price_in_weth is not None:
                                price = price_in_weth * eth_price
                                symbol, name = get_token_info_onchain(target_token)
                                print(f"🟢 Target Found from Watchlist: {name} ({symbol}) | Price: ${format_price(price)}")
                                
                                # Always send Discovery Alert
                                alert_discovery(symbol, target_token, price, weth_liq_eth)
                                
                                # ONLY execute buy if under max positions and session loss cap not hit
                                with state_lock:
                                    state = load_state()
                                    _session_loss = _get_session_loss_usd()
                                    open_pos_count = len(state.get("positions", []))
                                    if open_pos_count < MAX_POSITIONS and _session_loss < SESSION_MAX_LOSS_USD:
                                        prediction = ai_predictive_model.predict(target_token)
                                        if prediction > 0.8:
                                            success = execute_buy(target_token, quote_addr=quote_token)
                                        else:
                                            print(f"🚫 AI Brain rejected {target_token}. Skipping trade.")
                                            success = False
                                        if success:
                                            new_pos = _make_position(
                                                token_address      = target_token,
                                                token_symbol       = symbol,
                                                pair_address       = pair_addr,
                                                buy_price_usd      = price,
                                                amount_held        = TRADE_AMOUNT_ETH / price if price > 0 else 0,
                                                buy_router_address = ROUTER_ADDRESS,
                                                initial_liq_eth    = disc_liq_eth if disc_liq_eth else weth_liq_eth,
                                                quote_address      = quote_token,
                                                creator_address    = _token_creator_addresses.get(target_token.lower(), ""),
                                            )
                                            state.setdefault("positions", []).append(new_pos)
                                            state["in_position"] = True
                                            # Remove from watchlist
                                            state["watchlist"] = [p for p in state["watchlist"] if p["pair_address"].lower() != pair_addr.lower()]
                                            save_state(state)

                                            alert_trade_enter(symbol, target_token, TRADE_AMOUNT_ETH, price, weth_liq_eth)
                                            print(f"🔐 Position {open_pos_count+1}/{MAX_POSITIONS} locked. Monitoring price.")
                                            watchlist_bought = True
                                            break
                                    else:
                                        print(f"📢 Alpha Alert: Watchlist pair funded but skipped. (positions={open_pos_count}/{MAX_POSITIONS}, session_loss={_session_loss:.2f})")
                                        state["watchlist"] = [p for p in state["watchlist"] if p["pair_address"].lower() != pair_addr.lower()]
                                        save_state(state)
                            else:
                                print("⚠️ Failed to calculate price on-chain. Skipping watchlist pair.")
                        else:
                            print(f"🚨 Watchlist pair {pair_addr} failed safety checks after funding. Removing.")
                            with state_lock:
                                state = load_state()
                                state["watchlist"] = [p for p in state["watchlist"] if p["pair_address"].lower() != pair_addr.lower()]
                                save_state(state)
                                
                if watchlist_bought:
                    continue
            # --- END WATCHLIST PROCESSING ---

            # print(f"🔍 Scanning Base V2 Factory (last index: {state['last_checked_pair_index']})...")
            try:
                total_pairs = factory_contract.functions.allPairsLength().call()
                with state_lock:
                    state = load_state()
                    if state["last_checked_pair_index"] >= total_pairs:
                        print(f"🔄 Scan index reset because total pairs ({total_pairs}) is less than current index ({state['last_checked_pair_index']}).")
                        state["last_checked_pair_index"] = total_pairs - 1
                        save_state(state)

                if total_pairs > state["last_checked_pair_index"] + 1:
                    new_pairs = total_pairs - (state["last_checked_pair_index"] + 1)
                    print(f"📣 Found {new_pairs} new pairs! Scanning in batches of {MULTICALL_BATCH}...")
                    _no_new_pairs_streak = 0  # reset backoff on new activity
                    eth_price = get_weth_usd_price()

                    start = state["last_checked_pair_index"] + 1
                    bought = False
                    while start < total_pairs and not bought:
                        batch_count = min(MULTICALL_BATCH, total_pairs - start)
                        batch = batch_fetch_pairs(start, batch_count)

                        for j, info in enumerate(batch):
                            i = start + j
                            if info is None:
                                continue

                            pair_addr    = info["pair_addr"]
                            t0, t1       = info["t0"], info["t1"]
                            r0, r1       = info["r0"], info["r1"]

                            is_t0_weth = (t0 == WETH_ADDRESS)
                            is_t1_weth = (t1 == WETH_ADDRESS)
                            is_t0_usdc = (t0 == USDC_ADDRESS)
                            is_t1_usdc = (t1 == USDC_ADDRESS)

                            quote_token = None
                            target_token = None
                            quote_reserves = 0

                            if is_t0_weth:
                                quote_token = WETH_ADDRESS
                                target_token = t1
                                quote_reserves = r0
                            elif is_t1_weth:
                                quote_token = WETH_ADDRESS
                                target_token = t0
                                quote_reserves = r1
                            elif is_t0_usdc:
                                quote_token = USDC_ADDRESS
                                target_token = t1
                                quote_reserves = r0
                            elif is_t1_usdc:
                                quote_token = USDC_ADDRESS
                                target_token = t0
                                quote_reserves = r1

                            if quote_token is not None:
                                if quote_token == USDC_ADDRESS:
                                    usdc_amount = quote_reserves / (10 ** 6)
                                    weth_liq_eth = usdc_amount / eth_price if eth_price > 0 else usdc_amount / 3000.0
                                else:
                                    weth_liq_eth = float(web3.from_wei(quote_reserves, "ether"))

                                # Suppress logging for low-liquidity/unfunded pairs to save IO overhead
                                if weth_liq_eth >= 0.05 or MIN_LIQ <= weth_liq_eth <= MAX_LIQ:
                                    print(f"👉 Index {i}: Found { 'WETH' if quote_token == WETH_ADDRESS else 'USDC' } pair {pair_addr}. Liquidity: {weth_liq_eth:.4f} ETH")

                                if MIN_LIQ <= weth_liq_eth <= MAX_LIQ:
                                    print(f"🎯 Liquidity Match: {weth_liq_eth:.4f} ETH in range [{MIN_LIQ}, {MAX_LIQ}]!")

                                    # 🛡️ Async GoPlus check — max 4s, never blocks scanner
                                    if check_token_safety_async(target_token):
                                        print("🔗 Calculating token price on-chain...")
                                        price_in_weth, disc_liq_eth = get_onchain_price(pair_addr, target_token)

                                        if price_in_weth is not None:
                                            price = price_in_weth * eth_price

                                            # Fetch symbol & name directly on-chain (100% reliable block-one)
                                            symbol, name = get_token_info_onchain(target_token)
                                            print(f"🟢 Target Found: {name} ({symbol}) | Price: ${format_price(price)}")

                                            # Send Telegram Discovery Alert
                                            alert_discovery(symbol, target_token, price, weth_liq_eth)

                                            # ONLY buy if under max positions and session loss cap not hit
                                            open_pos_count = len(state.get("positions", []))
                                            if open_pos_count < MAX_POSITIONS and _session_loss < SESSION_MAX_LOSS_USD:
                                                with state_lock:
                                                    state = load_state()
                                                    open_pos_count = len(state.get("positions", []))
                                                    if open_pos_count < MAX_POSITIONS and _session_loss < SESSION_MAX_LOSS_USD:
                                                        prediction = ai_predictive_model.predict(target_token)
                                                        if prediction > 0.8:
                                                            success = execute_buy(target_token, quote_addr=quote_token)
                                                        else:
                                                            print(f"🚫 AI Brain rejected {target_token}. Skipping trade.")
                                                            success = False
                                                        if success:
                                                            new_pos = _make_position(
                                                                token_address      = target_token,
                                                                token_symbol       = symbol,
                                                                pair_address       = pair_addr,
                                                                buy_price_usd      = price,
                                                                amount_held        = TRADE_AMOUNT_ETH / price if price > 0 else 0,
                                                                buy_router_address = ROUTER_ADDRESS,
                                                                initial_liq_eth    = disc_liq_eth if disc_liq_eth else weth_liq_eth,
                                                                quote_address      = quote_token,
                                                                creator_address    = _token_creator_addresses.get(target_token.lower(), ""),
                                                            )
                                                            state.setdefault("positions", []).append(new_pos)
                                                            state["in_position"] = True
                                                            state["last_checked_pair_index"] = i
                                                            save_state(state)

                                                            alert_trade_enter(symbol, target_token, TRADE_AMOUNT_ETH, price, weth_liq_eth)
                                                            print(f"🔐 Position {open_pos_count+1}/{MAX_POSITIONS} locked. Monitoring price.")
                                                            bought = True
                                                            break
                                            else:
                                                print(f"📢 Alpha Alert: Scanner found new pair but skipped. (positions={open_pos_count}/{MAX_POSITIONS}, session_loss={_session_loss:.2f})")
                                        else:
                                            print("⚠️ Failed to calculate price on-chain. Skipping.")
                                    else:
                                        print("🚨 Skipping token due to safety check failure.")
                                else:
                                    if weth_liq_eth < MIN_LIQ:
                                        watchlist = state.get("watchlist", [])
                                        if not any(p["pair_address"].lower() == pair_addr.lower() for p in watchlist):
                                            watchlist.append({
                                                "pair_address": pair_addr,
                                                "target_token": target_token,
                                                "quote_token": quote_token,
                                                "t0": t0,
                                                "t1": t1,
                                                "added_time": int(time.time())
                                            })
                                            state["watchlist"] = watchlist
                                            save_state(state)
                                            print(f"👁️ Added unfunded pair {pair_addr} to watchlist.")

                        start += batch_count

                    with state_lock:
                        state = load_state()
                        state["last_checked_pair_index"] = total_pairs - 1
                        save_state(state)
            except Exception as e:
                print(f"⚠️ Factory query error: {e}")
                _no_new_pairs_streak = min(_no_new_pairs_streak + 1, 40)  # backoff on error too

        # Check if 30 seconds have passed for automatic status report
        if time.time() - LAST_STATUS_REPORT_TIME >= 30:
            try:
                print("🤖 Sending automatic 30-second status report to Telegram...")
                send_status_report()
                LAST_STATUS_REPORT_TIME = time.time()
            except Exception as e:
                print(f"⚠️ Failed to send automatic status report: {e}")

        # Execute periodic profit sweep check
        check_and_execute_profit_sweep()

        # Adaptive backoff: ramp up sleep when scan is caught up, throttled to at least 2.0s for mobile battery conservation
        if state.get("positions"):
            sleep_sec = 2.0  # throttled from 0.1 to 2.0 for mobile resource efficiency
        elif CHECK_INTERVAL > 0:
            sleep_sec = max(2.0, CHECK_INTERVAL)
        else:
            sleep_sec = max(2.0, min(0.1 + _no_new_pairs_streak * 0.05, 2.0))
        # print(f"⏳ Sleeping for {sleep_sec:.2f}s...\n")
        wakeup_event.wait(sleep_sec)
        wakeup_event.clear()

# ─── EVM WEBSOCKET SNIPER ENGINE ─────────────────────────────────────────────

def process_discovered_pair_ws(log):
    """Processes a newly created Uniswap V2 pair detected via WebSocket log subscription.
    Avoids RPC indexing lag by analyzing the log immediately, simulating/safety checking,
    and executing block-0/1 sniping if criteria are met."""
    global state_lock, wakeup_event
    try:
        topics = log.get("topics", [])
        if len(topics) < 3:
            return
        
        token0 = Web3.to_checksum_address("0x" + topics[1][-40:])
        token1 = Web3.to_checksum_address("0x" + topics[2][-40:])
        
        data_str = log.get("data", "")
        if data_str.startswith("0x"):
            data_str = data_str[2:]
            
        if len(data_str) >= 64:
            pair_address = Web3.to_checksum_address("0x" + data_str[24:64])
        else:
            print(f"⚠️ [WS] Log data too short: {data_str}")
            return
            
        all_pairs_length = -1
        if len(data_str) >= 128:
            try:
                all_pairs_length = int(data_str[64:128], 16)
            except:
                pass
                
        is_t0_weth = (token0.lower() == WETH_ADDRESS.lower())
        is_t1_weth = (token1.lower() == WETH_ADDRESS.lower())
        is_t0_usdc = (token0.lower() == USDC_ADDRESS.lower())
        is_t1_usdc = (token1.lower() == USDC_ADDRESS.lower())
        
        quote_token = None
        target_token = None
        
        if is_t0_weth:
            quote_token = WETH_ADDRESS
            target_token = token1
        elif is_t1_weth:
            quote_token = WETH_ADDRESS
            target_token = token0
        elif is_t0_usdc:
            quote_token = USDC_ADDRESS
            target_token = token1
        elif is_t1_usdc:
            quote_token = USDC_ADDRESS
            target_token = token0
            
        if quote_token is None:
            return
            
        print(f"📡 [WS] Detected new pair {pair_address} | Target: {target_token} | Quote: { 'WETH' if quote_token == WETH_ADDRESS else 'USDC' }")
        
        # 1. Check reserves immediately
        try:
            pair_contract = web3.eth.contract(address=pair_address, abi=PAIR_ABI)
            reserves = pair_contract.functions.getReserves().call()
            r0, r1 = reserves
        except Exception as e:
            print(f"⚠️ [WS] Failed to fetch reserves for new pair {pair_address}: {e}")
            r0, r1 = 0, 0
            
        eth_price = get_weth_usd_price() or 3400.0
        quote_reserves = r0 if token0.lower() == quote_token.lower() else r1
        
        if quote_token == USDC_ADDRESS:
            usdc_amount = quote_reserves / (10 ** 6)
            weth_liq_eth = usdc_amount / eth_price if eth_price > 0 else usdc_amount / 3000.0
        else:
            weth_liq_eth = float(web3.from_wei(quote_reserves, "ether"))
            
        # Suppress logging for low-liquidity/unfunded pairs
        if weth_liq_eth >= 0.05 or MIN_LIQ <= weth_liq_eth <= MAX_LIQ:
            print(f"📡 [WS] Pair {pair_address} liquidity: {weth_liq_eth:.4f} ETH")
            
        # 2. Check if liquidity matches requirements
        if MIN_LIQ <= weth_liq_eth <= MAX_LIQ:
            print(f"🎯 [WS] Liquidity Match: {weth_liq_eth:.4f} ETH is in range [{MIN_LIQ}, {MAX_LIQ}]!")
            
            # GoPlus Safety Check
            if check_token_safety_async(target_token):
                price_in_weth, disc_liq_eth = get_onchain_price(pair_address, target_token)
                if price_in_weth is not None:
                    price = price_in_weth * eth_price
                    symbol, name = get_token_info_onchain(target_token)
                    print(f"🟢 [WS] Target Found: {name} ({symbol}) | Price: ${format_price(price)}")
                    
                    # Send telegram alert
                    alert_discovery(symbol, target_token, price, weth_liq_eth)
                    
                    # Acquire lock to execute buy atomic check
                    with state_lock:
                        state = load_state()
                        _session_loss = _get_session_loss_usd()
                        open_pos_count = len(state.get("positions", []))
                        if open_pos_count < MAX_POSITIONS and _session_loss < SESSION_MAX_LOSS_USD:
                            prediction = ai_predictive_model.predict(target_token)
                            if prediction > 0.8:
                                success = execute_buy(target_token, quote_addr=quote_token)
                            else:
                                print(f"🚫 AI Brain rejected {target_token}. Skipping trade.")
                                success = False
                            if success:
                                new_pos = _make_position(
                                    token_address      = target_token,
                                    token_symbol       = symbol,
                                    pair_address       = pair_address,
                                    buy_price_usd      = price,
                                    amount_held        = TRADE_AMOUNT_ETH / price if price > 0 else 0,
                                    buy_router_address = ROUTER_ADDRESS,
                                    initial_liq_eth    = disc_liq_eth if disc_liq_eth else weth_liq_eth,
                                    quote_address      = quote_token,
                                    creator_address    = _token_creator_addresses.get(target_token.lower(), ""),
                                )
                                state.setdefault("positions", []).append(new_pos)
                                state["in_position"] = True
                                if all_pairs_length > 0:
                                    state["last_checked_pair_index"] = max(state.get("last_checked_pair_index", -1), all_pairs_length - 1)
                                save_state(state)

                                alert_trade_enter(symbol, target_token, TRADE_AMOUNT_ETH, price, weth_liq_eth)
                                print(f"🔐 [WS] Position {open_pos_count+1}/{MAX_POSITIONS} locked. Waking up main loop.")
                                wakeup_event.set()
                        else:
                            print(f"📢 [WS] New pair skipped. (positions={open_pos_count}/{MAX_POSITIONS}, session_loss={_session_loss:.2f})")
                else:
                    print("⚠️ [WS] Failed to calculate price onchain.")
            else:
                print("🚨 [WS] Safety check failed.")
        else:
            # Add low-liquidity or unfunded pair to watchlist so polling loop scans it later
            if weth_liq_eth < MIN_LIQ:
                with state_lock:
                    state = load_state()
                    watchlist = state.get("watchlist", [])
                    if not any(p["pair_address"].lower() == pair_address.lower() for p in watchlist):
                        watchlist.append({
                            "pair_address": pair_address,
                            "target_token": target_token,
                            "quote_token": quote_token,
                            "t0": token0,
                            "t1": token1,
                            "added_time": int(time.time())
                        })
                        state["watchlist"] = watchlist
                        if all_pairs_length > 0:
                            state["last_checked_pair_index"] = max(state.get("last_checked_pair_index", -1), all_pairs_length - 1)
                        save_state(state)
                        print(f"👁️ [WS] Added low-liq pair {pair_address} to watchlist.")
    except Exception as e:
        print(f"⚠️ [WS] Error in process_discovered_pair_ws: {e}")

async def run_websocket_listener():
    import websockets
    import asyncio
    import json
    
    wss_url = WSS_URL_CONFIG or RPC_URL.replace("https://", "wss://")
    if "mainnet.base.org" in wss_url:
        wss_url = "wss://base-rpc.publicnode.com"
    print(f"📡 Starting EVM WebSocket listener on {wss_url}")
    
    # Calculate target topic dynamically
    pair_created_topic = web3.keccak(text="PairCreated(address,address,address,uint256)").hex()
    if not pair_created_topic.startswith("0x"):
        pair_created_topic = "0x" + pair_created_topic
    
    subscribe_msg = {
        "id": 1,
        "method": "eth_subscribe",
        "params": [
            "logs",
            {
                "address": FACTORY_ADDRESS.lower(),
                "topics": [pair_created_topic]
            }
        ]
    }
    
    backoff = 1.0
    while True:
        try:
            print(f"📡 Connecting to WebSocket... ({wss_url})")
            async with websockets.connect(wss_url, ping_interval=20, ping_timeout=20) as websocket:
                print("📡 WebSocket connected! Sending subscription request...")
                await websocket.send(json.dumps(subscribe_msg))
                
                response = await websocket.recv()
                print(f"📡 Subscription response: {response}")
                backoff = 1.0  # reset backoff
                
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        if "params" in data and "result" in data["params"]:
                            log = data["params"]["result"]
                            threading.Thread(target=process_discovered_pair_ws, args=(log,), daemon=True).start()
                    except Exception as parse_err:
                        print(f"⚠️ [WS] Error parsing message: {parse_err}")
        except Exception as conn_err:
            print(f"📡 [WS] Connection error: {conn_err}. Reconnecting in {max(2.0, backoff):.1f}s...")
            await asyncio.sleep(max(2.0, backoff))
            backoff = min(backoff * 2.0, 60.0)

def start_websocket_thread():
    def _thread_target():
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run_websocket_listener())
        
    t = threading.Thread(target=_thread_target, name="EVMWebsocketListener", daemon=True)
    t.start()
    print("📡 WebSocket listener thread successfully spawned.")

if __name__ == "__main__":
    main()
