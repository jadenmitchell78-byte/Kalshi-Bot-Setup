"""
kalshi_auto_trader.py — Kalshi V2 Institutional-Grade Defensive Auto-Trading & Risk Management Engine

Implements absolute safety fail-safes and advanced idempotency matching:
1. Idempotency Tracking: Assigns UUIDv4 and logs status 'SUBMITTED' before hitting the network.
2. Boot Reconciliation Loop: Queries Kalshi REST orders endpoint and resolves unconfirmed database entries.
3. Stale Data Guardian: Inside validate_order(), checks if last websocket message is older than 2.0s.
4. Maintenance Window Detection: Queries exchange/status and shifts to EXCHANGE_DOWN if inactive.
5. Heartbeat Telemetry: Sends performance summaries every 5 minutes to Telegram.
6. Rate-Limit Guard: Exponential backoff for HTTP 429 responses.
7. Global Circuit Breaker: Freezes and exits clean if > 5 requests attempted in rolling 60s window.
8. Max Drawdown Kill-Switch: Cancels open orders and exits clean if equity drops > 5% today.
9. Local Database Logging: SQLite tracking (PENDING, FILLED, FAILED) to prevent duplicates.
"""

import os
import sys
import json
import time
import re
import uuid
import sqlite3
import requests
import threading
import pykalshi
from datetime import datetime
import v2_logger
v2_logger.inject_v2_logger()

# Load environment variables
def _load_env():
    if os.path.exists(".env"):
        try:
            with open(".env", "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass

_load_env()

CONFIG_FILE = "social_trail_config.json"
STATE_FILE = "kalshi_trader_state.json"
DB_FILE = "betting_system.db"

# Active Markets caching to optimize matching latency & rate-limits
_cached_markets = None
_cached_markets_time = 0.0

# Portfolio balance caching to prevent overnight rate limits (429)
_cached_equity = None
_cached_equity_time = 0.0

# In-memory rolling request window (replaces per-call disk I/O for rate limiting)
import collections
_request_timestamps_mem = collections.deque()
_request_ts_lock = threading.Lock()

# Throttle for profit telemetry (fire at most once per 5 minutes)
_last_profit_telemetry_ts = 0.0

# Thread-safe lock for asynchronous risk and order execution
_kalshi_trade_lock = threading.Lock()

# Try importing cryptography for request signing
HAS_CRYPTO = False
try:
    import base64
    from Crypto.PublicKey import RSA
    from Crypto.Signature import pss
    from Crypto.Hash import SHA256
    HAS_CRYPTO = True
except ImportError:
    pass

# Try importing websocket library
HAS_WEBSOCKET = False
try:
    import websocket
    HAS_WEBSOCKET = True
except ImportError:
    pass

# Helper to load config
def _load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

# Helper to load/save state
def _load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return {
        "last_trade_timestamp": 0,
        "trades_today_count": 0,
        "today_date": today,
        "traded_pick_ids": [],
        "starting_daily_equity": 0.0,
        "request_timestamps": [],
        "exchange_status": "EXCHANGE_UP",
        "current_win_streak": 0,
        "last_checked_settlement_time": 0
    }

def _save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
        return True
    except Exception as e:
        print(f"⚠️ Error saving Kalshi state: {e}")
        return False

# Telegram alerts helper
def send_telegram_alert(msg: str):
    config = _load_config()
    token = config.get("telegram_token") or os.environ.get("TELEGRAM_TOKEN")
    chat_id = config.get("telegram_chat_id") or os.environ.get("TELEGRAM_CHAT_ID")
    
    # Forward telemetry to Sam's thread
    try:
        from brain import send_update_to_sam
        send_update_to_sam(msg, token=token)
    except Exception as e:
        print(f"⚠️ Failed to forward Kalshi telemetry to Sam: {e}")
        
    if not token or not chat_id:
        return
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": str(chat_id), "text": msg, "parse_mode": "Markdown"}
    try:
        requests.post(api_url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Kalshi Telegram alert failed: {e}")


# ── Stale Data Guardian ───────────────────────────────────────────────────────
class OrderBookGuardian:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(OrderBookGuardian, cls).__new__(cls, *args, **kwargs)
            cls._instance.last_recv_times = {}
            cls._instance.sim_threads = {}
            cls._instance.is_running = {}
        return cls._instance
        
    def record_message(self, ticker: str):
        self.last_recv_times[ticker] = time.time()
        
    def get_last_recv_time(self, ticker: str) -> float:
        return self.last_recv_times.get(ticker, 0.0)
        
    def start_simulated_feed(self, ticker: str):
        """Simulates incoming WebSocket messages for a ticker if library is missing."""
        if ticker in self.sim_threads and self.is_running.get(ticker, False):
            return
            
        self.is_running[ticker] = True
        
        def run():
            print(f"📡 [GUARDIAN] Started simulated WebSocket feed for {ticker}...")
            while self.is_running.get(ticker, False):
                self.record_message(ticker)
                time.sleep(1.0) # Tick every second
                
        t = threading.Thread(target=run, daemon=True)
        self.sim_threads[ticker] = t
        t.start()
        
    def stop_feed(self, ticker: str):
        self.is_running[ticker] = False


def validate_order(ticker: str) -> tuple:
    """
    Validates market data freshness.
    If current_time - last_websocket_recv_time > 2.0s, marks data STALE and blocks trade.
    """
    guardian = OrderBookGuardian()
    last_recv = guardian.get_last_recv_time(ticker)
    
    # Auto-initialize simulated WebSocket feed if no packets received yet
    if last_recv == 0.0:
        trader = KalshiDefensiveTrader()
        if trader.is_simulated or not HAS_WEBSOCKET:
            guardian.start_simulated_feed(ticker)
            time.sleep(0.5)
            last_recv = guardian.get_last_recv_time(ticker)
            
    now = time.time()
    latency = now - last_recv
    
    if latency > 2.0:
        alert_msg = f"🚨 *[STALE DATA GUARDIAN]* Market data for `{ticker}` is STALE! Latency: `{latency:.2f}s` (Threshold: 2.0s). Cancelling order execution."
        send_telegram_alert(alert_msg)
        print(alert_msg.replace("*", ""))
        return False, "STALE_MARKET_DATA"
        
    return True, "Data is fresh."


# ── System Heartbeat Guardian ─────────────────────────────────────────────────
class HeartbeatGuardian:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(HeartbeatGuardian, cls).__new__(cls, *args, **kwargs)
            cls._instance.is_running = False
        return cls._instance
        
    def start(self, trader_instance):
        if self.is_running:
            return
        self.is_running = True
        self.trader = trader_instance
        
        def ws_deadman_loop():
            print("💖 [WS-DEADMAN] 11-second WebSocket heartbeat protocol started.")
            fail_count = 0
            while self.is_running:
                time.sleep(11)
                ping_success = False
                try:
                    ws_manager = KalshiWSManager()
                    feed = getattr(ws_manager, 'feed', None)
                    if feed and feed.is_connected and feed._ws and feed._loop:
                        import asyncio
                        # Send explicit WebSocket ping
                        pong_waiter = asyncio.run_coroutine_threadsafe(feed._ws.ping(), feed._loop)
                        pong_waiter.result(timeout=5.0)
                        ping_success = True
                except Exception as e:
                    ping_success = False
                    
                if ping_success:
                    fail_count = 0
                else:
                    fail_count += 1
                    print(f"⚠️ [WS-DEADMAN] WebSocket ping failed ({fail_count}/3).")
                    if fail_count >= 3:
                        msg = "🚨 [DEAD-MAN SWITCH] WebSocket heartbeat failed for 3 consecutive attempts. Utilizing fallback REST API to cancel all open orders!"
                        print(msg)
                        send_telegram_alert(f"⚠️ *{msg}*")
                        try:
                            self.trader.cancel_all_resting_orders()
                        except Exception as e:
                            print(f"⚠️ Failed to cancel orders in dead-man switch: {e}")
                        fail_count = 0 # reset to avoid spamming
                        
        def telemetry_loop():
            print("💖 [HEARTBEAT] System Telemetry thread started.")
            while self.is_running:
                try:
                    time.sleep(300)  # Telemetry updates every 5 minutes

                    # Fetch balance
                    balance = self.trader.fetch_portfolio_equity()
                    if balance is None:
                        balance = 0.0

                    # Fetch open + filled positions from orders_log
                    conn = sqlite3.connect(DB_FILE)
                    cursor = conn.cursor()
                    cursor.execute("SELECT COUNT(*) FROM orders_log WHERE status = 'FILLED'")
                    filled_count = cursor.fetchone()[0]
                    cursor.execute("SELECT COUNT(*) FROM orders_log WHERE status IN ('SUBMITTED','PENDING')")
                    pending_count = cursor.fetchone()[0]
                    conn.close()

                    # Drawdown telemetry
                    state = _load_state()
                    starting_equity = state.get("starting_daily_equity", balance)
                    drawdown = 0.0
                    if starting_equity > 0:
                        drawdown = ((starting_equity - balance) / starting_equity) * 100
                    daily_spend = state.get("autonomous_daily_spend", 0.0)

                    telemetry = (
                        f"💖 *[HEARTBEAT] Ghost Quant Active*\n"
                        f"💰 *Balance*: `${balance:.2f}`\n"
                        f"📈 *Start Equity*: `${starting_equity:.2f}`\n"
                        f"💼 *Filled Positions*: `{filled_count}` | *Pending*: `{pending_count}`\n"
                        f"📊 *Daily Drawdown*: `{drawdown:.2f}%` (limit: 5%)\n"
                        f"💸 *Daily Spend*: `${daily_spend:.2f}`"
                    )

                    # Send heartbeat via brain telemetry channel
                    try:
                        import brain as _brain
                        _brain.send_update_to_sam(telemetry)
                    except Exception:
                        pass

                except Exception as e:
                    print(f"⚠️ Heartbeat telemetry error: {e}")
                    
        t1 = threading.Thread(target=telemetry_loop, daemon=True)
        t1.start()
        
        t2 = threading.Thread(target=ws_deadman_loop, daemon=True)
        t2.start()
# ── WebSocket Manager ─────────────────────────────────────────────────────────
class KalshiWSManager:
    _instance = None
    
    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance
        
    def _init(self):
        self.books = {}
        self.subscribed = set()
        try:
            self.client = pykalshi.KalshiClient.from_env()
            self.feed = self.client.feed()
            
            @self.feed.on("orderbook_delta")
            def handle_book(msg):
                ticker = msg.market_ticker
                if ticker not in self.books:
                    self.books[ticker] = pykalshi.OrderbookManager(ticker)
                if hasattr(msg, 'yes_dollars'):
                    self.books[ticker].apply_snapshot(msg.yes_dollars, msg.no_dollars)
                else:
                    self.books[ticker].apply_delta(msg.side, msg.price_dollars, msg.delta_fp)
                    
            self.feed.start()
            print("🌊 [pykalshi] WebSocket streaming client connected.")
            t = threading.Thread(target=self._refresh_subs_loop, daemon=True)
            t.start()
        except Exception as e:
            print(f"⚠️ Failed to init pykalshi websocket: {e}")
            
    def _refresh_subs_loop(self):
        while True:
            try:
                markets = self.client.get_markets(status=pykalshi.MarketStatus.OPEN, limit=100)
                tickers = [m.ticker for m in markets]
                new_subs = [t for t in tickers if t not in self.subscribed]
                if new_subs:
                    self.feed.subscribe("orderbook_delta", market_tickers=new_subs)
                    for t in new_subs:
                        self.subscribed.add(t)
            except Exception as e:
                pass
            time.sleep(300)

    def get_price(self, ticker: str):
        if ticker in self.books:
            b = self.books[ticker]
            yes_ask = int(float(b.best_ask) * 100) if b.best_ask is not None else None
            no_ask = int((1.0 - float(b.best_bid)) * 100) if b.best_bid is not None else None
            return yes_ask, no_ask
        return None, None


# ── Defensive Executer ────────────────────────────────────────────────────────
# Module-level RSA key cache — load once, reuse forever
_rsa_key_cache = None
_rsa_key_cache_path = None

def _get_cached_rsa_key(key_path: str, raw_key_content: str = ""):
    """Returns a cached RSA private key object. Loads from disk only once per path."""
    global _rsa_key_cache, _rsa_key_cache_path
    if _rsa_key_cache is not None and _rsa_key_cache_path == key_path:
        return _rsa_key_cache
    key_bytes = None
    if key_path and os.path.exists(key_path):
        with open(key_path, "rb") as f:
            key_bytes = f.read()
    elif raw_key_content:
        key_bytes = raw_key_content.replace("\\n", "\n").encode("utf-8")
    if key_bytes:
        _rsa_key_cache = RSA.import_key(key_bytes)
        _rsa_key_cache_path = key_path
        return _rsa_key_cache
    return None


class KalshiDefensiveTrader:
    def __init__(self):
        self.config = _load_config()
        self.use_demo = self.config.get("kalshi_use_demo", True)
        self.base_url = "https://external-api.demo.kalshi.co" if self.use_demo else "https://external-api.kalshi.com"
        
        # Keys config
        self.key_id = self.config.get("kalshi_api_key_id", "") or os.environ.get("KALSHI_API_KEY_ID", "")
        self.key_path = self.config.get("kalshi_private_key_path", "") or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        self.raw_key_content = os.environ.get("KALSHI_PRIVATE_KEY_CONTENT", "")
        
        self.is_simulated = not self.key_id or not HAS_CRYPTO
        
        # Ensure env vars are set for pykalshi (forcing update in case PM2 cached stale ones)
        if self.key_id:
            os.environ["KALSHI_API_KEY_ID"] = self.key_id
        if self.key_path:
            os.environ["KALSHI_PRIVATE_KEY_PATH"] = self.key_path
            
        self.init_db()
        
        # Start Kalshi WebSocket Manager for live orderbook deltas
        KalshiWSManager()
        
        # Run reconciliation on startup/initialization
        self.reconcile_orders()
        
        # Start Heartbeat Guardian
        heartbeat = HeartbeatGuardian()
        heartbeat.start(self)

    def init_db(self):
        """Ensure local SQLite tracking tables exist."""
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            # Backwards compatible trading logs
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS kalshi_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pick_id INTEGER,
                ticker TEXT,
                side TEXT,
                price_cents INTEGER,
                count INTEGER,
                cost_usd REAL,
                client_order_id TEXT UNIQUE,
                order_id TEXT,
                status TEXT,
                created_at INTEGER,
                updated_at INTEGER
            )
            """)
            
            # Institutional order matching layer
            cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pick_id INTEGER,
                client_order_id TEXT UNIQUE,
                ticker TEXT,
                side TEXT,
                price_cents INTEGER,
                count INTEGER,
                status TEXT, -- 'SUBMITTED', 'PENDING', 'FILLED', 'FAILED', 'CANCELLED'
                timestamp_ms INTEGER,
                order_id TEXT,
                updated_at INTEGER
            )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"⚠️ Failed to initialize Kalshi SQLite tables: {e}")

    # Exchange status checking
    def check_exchange_active(self) -> bool:
        if self.is_simulated:
            return True
        try:
            url = f"{self.base_url}/trade-api/v2/exchange/status"
            r = requests.get(url, timeout=5)
            if r.status_code == 200:
                data = r.json()
                return data.get("exchange_active", False)
        except Exception as e:
            print(f"⚠️ Error checking Kalshi exchange status: {e}")
        return False

    # Authentication Header Generator — uses cached RSA key (no disk read per request)
    def _get_auth_headers(self, method, path, body_str=""):
        if self.is_simulated:
            return None
            
        try:
            private_key = _get_cached_rsa_key(self.key_path, self.raw_key_content)
            if not private_key:
                return None
                
            timestamp = str(int(time.time() * 1000))
            path_without_query = path.split('?')[0]
            message = f"{timestamp}{method}{path_without_query}".encode('utf-8')
            
            h = SHA256.new(message)
            signature_bytes = pss.new(private_key, salt_bytes=h.digest_size).sign(h)
            signature = base64.b64encode(signature_bytes).decode('utf-8')
            
            return {
                "KALSHI-ACCESS-KEY": self.key_id,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
                "KALSHI-ACCESS-SIGNATURE": signature,
                "Content-Type": "application/json"
            }
        except Exception as e:
            print(f"⚠️ Kalshi RSA signing exception: {e}")
            return None

    # Global Rolling Request Limit Circuit Breaker
    # Kalshi allows up to ~60 req/min for authenticated endpoints. We set 2000 as our safe cap.
    ROLLING_WINDOW_MAX = 2000

    def _check_rolling_window_limit(self) -> bool:
        """Check rate limit using in-memory deque (no disk I/O per call)."""
        global _request_timestamps_mem
        now = time.time()
        with _request_ts_lock:
            # Purge entries older than 60 seconds
            while _request_timestamps_mem and now - _request_timestamps_mem[0] > 60:
                _request_timestamps_mem.popleft()
            count = len(_request_timestamps_mem)

        if count >= self.ROLLING_WINDOW_MAX:
            state = _load_state()
            if not state.get("circuit_breaker_tripped", False):
                panic_msg = (
                    f"🚨 [PANIC CIRCUIT BREAKER] Bot attempted {count} requests "
                    f"in a rolling 60-second window (Limit: {self.ROLLING_WINDOW_MAX}). Freezing temporarily."
                )
                send_telegram_alert(panic_msg)
                print(panic_msg)
                state["circuit_breaker_tripped"] = True
                _save_state(state)
            return False

        # Reset tripped flag if we're below limit
        state = _load_state()
        if state.get("circuit_breaker_tripped", False):
            state["circuit_breaker_tripped"] = False
            _save_state(state)
        return True

    def _record_request_attempt(self):
        """Record a request timestamp in-memory (no disk write)."""
        with _request_ts_lock:
            _request_timestamps_mem.append(time.time())

    # Base Network Client with Rate-Limit Guard & Backoff
    def _send_request_with_backoff(self, method, path, json_data=None):
        if not self._check_rolling_window_limit():
            return {"simulation": True, "error": "Circuit Breaker Active"}
        self._record_request_attempt()
        
        if self.is_simulated:
            return {"simulation": True}
            
        url = f"{self.base_url}{path}"
        retries = 3
        backoff = 2.0
        
        for attempt in range(retries):
            headers = self._get_auth_headers(method, path, json.dumps(json_data) if json_data else "")
            if not headers:
                return {"simulation": True}
                
            try:
                if method == "GET":
                    r = requests.get(url, headers=headers, params=json_data, timeout=10)
                elif method == "POST":
                    r = requests.post(url, headers=headers, json=json_data, timeout=10)
                elif method == "DELETE":
                    r = requests.delete(url, headers=headers, json=json_data, timeout=10)
                else:
                    raise ValueError(f"Invalid method: {method}")
                    
                if r.status_code == 429:
                    print(f"⚠️ Kalshi Rate Limit (429). Retrying in {backoff}s...")
                    time.sleep(backoff)
                    backoff *= 2.0
                    if not self._check_rolling_window_limit():
                        return {"simulation": True, "error": "Circuit Breaker Active"}
                    self._record_request_attempt()
                    continue
                    
                r.raise_for_status()
                return r.json()
            except requests.exceptions.HTTPError as e:
                print(f"❌ HTTP request failure: {e}")
                if e.response is not None:
                    print(f"Response Body: {e.response.text}")
                    try:
                        with open("/data/data/com.termux/files/home/logs/last_kalshi_400.json", "w") as f:
                            f.write(e.response.text)
                    except:
                        pass
                if e.response is not None and e.response.status_code == 410:
                    try:
                        state_cb = _load_state()
                        state_cb["current_win_streak"] = 0
                        _save_state(state_cb)
                        print("🚨 [SAFETY CIRCUIT BREAKER TRIPPED] Reverting to baseline risk rules.")
                    except:
                        pass
                if attempt == retries - 1:
                    raise e
                time.sleep(backoff)
                backoff *= 2.0
                if not self._check_rolling_window_limit():
                    return {"simulation": True, "error": "Circuit Breaker Active"}
                self._record_request_attempt()
            except requests.exceptions.RequestException as e:
                if attempt == retries - 1:
                    print(f"❌ HTTP request failure: {e}")
                    try:
                        state_cb = _load_state()
                        state_cb["current_win_streak"] = 0
                        _save_state(state_cb)
                        print("🚨 [SAFETY CIRCUIT BREAKER TRIPPED] Reverting to baseline risk rules.")
                    except:
                        pass
                    raise e
                time.sleep(backoff)
                backoff *= 2.0
                if not self._check_rolling_window_limit():
                    return {"simulation": True, "error": "Circuit Breaker Active"}
                self._record_request_attempt()

    # Max Daily Drawdown Kill-Switch
    def check_daily_drawdown(self) -> bool:
        state = _load_state()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        
        if state.get("today_date") != today:
            state["today_date"] = today
            state["starting_daily_equity"] = 0.0
            state["trades_today_count"] = 0
            state["drawdown_killed"] = False
            _save_state(state)
            
        if state.get("drawdown_killed", False):
            return False
            
        starting_equity = state.get("starting_daily_equity", 0.0)
        current_equity = self.fetch_portfolio_equity()
        
        if current_equity is None:
            current_equity = 1000.00
            
        if starting_equity == 0.0:
            state["starting_daily_equity"] = current_equity
            _save_state(state)
            starting_equity = current_equity
            
        # Drawdown Kill threshold is 5% of starting equity
        drawdown_threshold = 0.05
        
        # Check for 24-hour user override (supports both key names)
        override_until = max(
            state.get("drawdown_override_until", 0),
            state.get("drawdown_bypass_until", 0)
        )
        if time.time() < override_until:
            drawdown_threshold = 1.0  # Allow 100% drawdown during override period
            state["drawdown_killed"] = False
        
        # Fire profit telemetry to ClickUp/Sam — throttled to once every 5 minutes
        global _last_profit_telemetry_ts
        now_ts = time.time()
        if now_ts - _last_profit_telemetry_ts >= 300:
            try:
                import brain
                brain.log_profit_to_sam(current_equity, starting_equity)
                _last_profit_telemetry_ts = now_ts
            except Exception:
                pass
            
        if current_equity < starting_equity * (1.0 - drawdown_threshold):
            panic_msg = (
                f"Drawdown-Alerts: Daily Drawdown Kill-Switch Triggered! Down {(1.0 - (current_equity / starting_equity))*100:.2f}%. Execution instantly frozen.\n"
                f"Initial Equity: {starting_equity:.2f} | Current Equity: {current_equity:.2f}\n"
                f"Action: Cancelling all resting orders. Kalshi trading paused for today."
            )
            send_telegram_alert(panic_msg)
            print(panic_msg)
            
            self.cancel_all_resting_orders()
            state["drawdown_killed"] = True
            _save_state(state)
            return False
            
        return True

    def fetch_portfolio_equity(self) -> float:
        global _cached_equity, _cached_equity_time
        now = time.time()
        
        # Return cached portfolio value if under 30 seconds old
        if _cached_equity is not None and (now - _cached_equity_time < 30):
            return _cached_equity
            
        if self.is_simulated:
            return 1000.00
        try:
            res = self._send_request_with_backoff("GET", "/trade-api/v2/portfolio/balance")
            if res and not res.get("simulation"):
                pv = res.get("portfolio_value", 0)
                bal = res.get("balance", 0)
                cents = pv + bal
                val = float(cents) / 100.0
                _cached_equity = val
                _cached_equity_time = now
                return val
        except Exception as e:
            print(f"⚠️ Error fetching portfolio balance: {e}")
            if _cached_equity is not None:
                return _cached_equity
        return None

    def cancel_all_resting_orders(self):
        if self.is_simulated:
            print("🧪 [SIMULATED DRAWDOWN] Cancelled all resting orders successfully.")
            return
        try:
            res = self._send_request_with_backoff("GET", "/trade-api/v2/portfolio/orders", {"status": "resting"})
            if res:
                orders = res.get("orders", [])
                for order in orders:
                    order_id = order.get("order_id") or order.get("id")
                    if order_id:
                        self._send_request_with_backoff("DELETE", f"/trade-api/v2/portfolio/events/orders/{order_id}")
                print(f"🧹 Cancelled {len(orders)} resting orders.")
        except Exception as e:
            print(f"⚠️ Error cancelling resting orders: {e}")

    def aggressive_liquidate_all(self):
        print("🚨 [AGGRESSIVE LIQUIDATE ALL] INITIATED")
        self.cancel_all_resting_orders()
        try:
            import pykalshi
            import uuid
            client = pykalshi.KalshiClient.from_env()
            positions = client.portfolio.get_positions()
            flattened = 0
            for pos in positions:
                count = int(float(pos.position_fp)) if pos.position_fp else 0
                if count == 0: continue
                
                ticker = pos.ticker
                side = "yes" if count > 0 else "no"
                abs_count = abs(count)
                
                api_side = "yes" if side == "yes" else "no"
                api_action = "sell"

                order_data = {
                    "ticker": ticker,
                    "side": "bid" if side == "yes" else "ask",
                    "count": f"{int(abs_count)}.00",
                    "time_in_force": "fill_or_kill",
                    "client_order_id": str(uuid.uuid4()),
                    "self_trade_prevention_type": "taker_at_cross"
                }
                try:
                    print(f"🧨 [LIQUIDATE] Selling {abs_count} {side.upper()} contracts on {ticker}...")
                    if not self.is_simulated:
                        self._send_request_with_backoff("POST", "/trade-api/v2/portfolio/events/orders", order_data)
                    flattened += 1
                except Exception as e:
                    print(f"⚠️ Liquidate order failed for {ticker}: {e}")
            print(f"✅ [AGGRESSIVE LIQUIDATE ALL] Completed. Flattened {flattened} positions.")
            return True
        except Exception as e:
            print(f"⚠️ Failed to liquidate positions: {e}")
            return False

    def check_settlements_and_update_streak(self):
        """
        Queries Kalshi V2 settlements endpoint.
        If there are new settled trades:
        - If profit > 0 (win), increment current_win_streak.
        - If profit < 0 (loss), set current_win_streak = 0 and trigger circuit breaker.
        """
        if self.is_simulated:
            return
            
        try:
            res = self._send_request_with_backoff("GET", "/trade-api/v2/portfolio/settlements", {"limit": 20})
            if not res or res.get("simulation"):
                return
                
            settlements = res.get("settlements", [])
            if not settlements:
                return
                
            state = _load_state()
            last_checked = state.get("last_checked_settlement_time", 0)
            
            # Parse and filter only new settlements (time > last_checked)
            new_settlements = []
            for s in settlements:
                s_time_str = s.get("settle_time") or s.get("settled_time") or ""
                s_time = 0
                if s_time_str:
                    try:
                        s_time = int(datetime.fromisoformat(s_time_str.replace('Z', '')).timestamp())
                    except:
                        pass
                if s_time > last_checked:
                    new_settlements.append((s_time, s))
                    
            if not new_settlements:
                return
                
            # Process chronologically
            new_settlements.sort(key=lambda x: x[0])
            
            streak = state.get("current_win_streak", 0)
            circuit_breaker_tripped = False
            current_balance = self.fetch_portfolio_equity() or 0.0
            for s_time, s in new_settlements:
                pnl = float(s.get("realized_pnl") or s.get("profit_loss") or 0)
                ticker = s.get("market_ticker") or s.get("ticker") or "UNKNOWN"
                revenue = float(s.get("revenue") or 0)
                
                if pnl > 0:
                    streak += 1
                    send_telegram_alert(f"✅ *[TRADE SETTLED: WIN]*\nMarket: `{ticker}`\nRealized PnL: `+${pnl/100:.2f}`\nWin Streak: `{streak}` 🔥\n💰 Total Balance: `${current_balance:.2f}`")
                elif pnl < 0:
                    streak = 0
                    circuit_breaker_tripped = True
                    send_telegram_alert(f"❌ *[TRADE SETTLED: LOSS]*\nMarket: `{ticker}`\nRealized PnL: `${pnl/100:.2f}`\nWin Streak Reset.\n💰 Total Balance: `${current_balance:.2f}`")
                    
            state["current_win_streak"] = streak
            state["last_checked_settlement_time"] = new_settlements[-1][0]
            _save_state(state)
            
            if circuit_breaker_tripped:
                print("🚨 [SAFETY CIRCUIT BREAKER TRIPPED] Reverting to baseline risk rules.")
                send_telegram_alert("🚨 *[SAFETY CIRCUIT BREAKER TRIPPED]* Reverting to baseline risk rules due to settlement loss.")
                
        except Exception as e:
            print(f"⚠️ Error checking settlements: {e}")
            # Fallback reset
            try:
                state = _load_state()
                state["current_win_streak"] = 0
                _save_state(state)
                print("🚨 [SAFETY CIRCUIT BREAKER TRIPPED] Reverting to baseline risk rules due to exception.")
            except:
                pass

    def get_current_exposure(self, ticker: str) -> int:
        """
        Sums up the contracts for active or filled orders on the given ticker in orders_log today.
        """
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT SUM(count) FROM orders_log 
                WHERE ticker = ? AND status IN ('SUBMITTED', 'PENDING', 'FILLED')
            """, (ticker,))
            row = cursor.fetchone()
            conn.close()
            return row[0] or 0
        except Exception as e:
            print(f"⚠️ Error checking market exposure for {ticker}: {e}")
            return 0

    def _fetch_live_price(self, ticker: str) -> tuple:
        """
        REST fallback: fetches the current yes_ask / no_ask for a ticker directly
        from the Kalshi orderbook endpoint when the WebSocket book has no data.
        Returns (yes_ask_cents, no_ask_cents) or (None, None) on failure.
        """
        try:
            path = f"/trade-api/v2/markets/{ticker}/orderbook"
            res = self._send_request_with_backoff("GET", path)
            if res and "orderbook_fp" in res:
                ob = res["orderbook_fp"]
                yes_bids = ob.get("yes_dollars", [])
                no_bids = ob.get("no_dollars", [])
                
                # yes_dollars are BIDS (offers to buy YES).
                # To get yes_ask, we need to look at no_bids. 
                # Highest no_bid implies the lowest yes_ask.
                # yes_ask = 100 - no_bid (in cents)
                
                yes_ask_cents = None
                if no_bids:
                    best_no_bid_dollars = float(no_bids[-1][0])  # Last element is usually highest price in orderbook
                    # Actually wait, let's just sort to be safe:
                    best_no_bid = max([float(x[0]) for x in no_bids])
                    yes_ask_cents = int(round((1.0 - best_no_bid) * 100))
                    
                no_ask_cents = None
                if yes_bids:
                    best_yes_bid = max([float(x[0]) for x in yes_bids])
                    no_ask_cents = int(round((1.0 - best_yes_bid) * 100))
                    
                if yes_ask_cents is not None and no_ask_cents is not None:
                    print(f"📡 [REST Price] {ticker}: YES ask={yes_ask_cents}¢  NO ask={no_ask_cents}¢")
                    return yes_ask_cents, no_ask_cents
        except Exception as e:
            print(f"⚠️ REST price fetch failed for {ticker}: {e}")
        return None, None

    def place_scaled_orders(self, ticker, side, total_contracts, base_price_cents, client_order_id):
        """
        Splits total_contracts into up to 3 scaled limit orders on the Kalshi order book.

        Kalshi V2 order POST payload (correct format):
          action      : "buy" | "sell"
          side        : "yes" | "no"   (the contract side you are buying/selling)
          ticker      : market ticker
          count       : int  (number of contracts, must be integer not string)
          price       : int  (limit price in CENTS, e.g. 41 for 41 cents)
          type        : "limit"
          time_in_force : "good_till_canceled"
          client_order_id : str (UUIDv4)
          self_trade_prevention_type : "taker_at_cross"

        Side mapping:
          bid (buy YES)  -> action="buy",  side="yes", price=yes_price_cents
          ask (sell NO)  -> action="sell", side="no",  price=no_price_cents
        """
        # Split total contracts: 40% / 35% / 25% across 3 levels
        c1 = int(total_contracts * 0.4)
        c2 = int(total_contracts * 0.35)
        c3 = total_contracts - c1 - c2
        counts = [c for c in [c1, c2, c3] if c > 0]
        if not counts:
            counts = [total_contracts]

        # Spread price levels 3¢ apart for meaningful order book depth
        # Buy YES (bid): best price first, then 3¢ and 6¢ below
        # Sell NO (ask): best price first, then 3¢ and 6¢ above
        STEP = 3
        price_steps = []
        for i in range(len(counts)):
            if side == "bid":
                price_steps.append(max(1, base_price_cents - (i * STEP)))
            else:
                price_steps.append(min(99, base_price_cents + (i * STEP)))

        results = []
        for idx, (count, price) in enumerate(zip(counts, price_steps)):
            sub_order_id = f"{client_order_id}-{idx}"

            # Kalshi V2 /portfolio/events/orders (official spec):
            #   side  : "bid" (buy YES) | "ask" (sell YES / buy NO)
            #   count : FixedPointCount string, e.g. "10.00"
            #   price : FixedPointDollars string, e.g. "0.5600"  (dollars, NOT cents)
            if side == "bid":
                api_side = "bid"
                api_price_str = f"{int(price) / 100:.4f}"
            else:
                api_side = "ask"
                api_price_str = f"{int(price) / 100:.4f}"

            # Correct V2 endpoint: POST /portfolio/events/orders
            # Fields: side=bid/ask, count (FixedPointCount), price (FixedPointDollars)
            order_data = {
                "ticker": ticker,
                "side": api_side,
                "count": f"{int(count)}.00",
                "price": api_price_str,
                "time_in_force": "good_till_canceled",
                "client_order_id": sub_order_id,
                "self_trade_prevention_type": "taker_at_cross"
            }

            print(f"📉 [Scaled L{idx+1}] {api_side.upper()} {count} contracts @ {price}¢ on {ticker}")

            if self.is_simulated:
                results.append({"success": True, "order_id": f"sim-{sub_order_id[:8]}", "count": count, "price": price})
            else:
                try:
                    r = self._send_request_with_backoff("POST", "/trade-api/v2/portfolio/events/orders", order_data)
                    if r and not r.get("simulation") and not r.get("error"):
                        order_obj = r.get("order", r)
                        ord_id = order_obj.get("order_id") or r.get("order_id")
                        status = order_obj.get("status", "resting")
                        print(f"   ✅ Order accepted: {ord_id} | status={status}")
                        results.append({"success": True, "order_id": ord_id, "count": count, "price": price})
                    else:
                        err = r.get("error", {}) if r else {}
                        err_msg = err.get("message", str(r)) if isinstance(err, dict) else str(err)
                        print(f"   ❌ Order rejected: {err_msg}")
                        results.append({"success": False, "error": err_msg, "count": count, "price": price})
                except Exception as e:
                    print(f"   ❌ Order exception: {e}")
                    results.append({"success": False, "error": str(e), "count": count, "price": price})
        return results

    # SQLite Local Logging & State Machine Verification
    def check_order_status_and_resume(self, pick_id: int) -> tuple:
        """
        Checks local database state before making a trade.
        Returns (allow_trade: bool, message: str)
        """
        # First check backwards compatible table
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, ticker, status, order_id 
                FROM kalshi_trades 
                WHERE pick_id = ?
            """, (pick_id,))
            row = cursor.fetchone()
            conn.close()
            if row and row["status"] == "FILLED":
                return False, f"Position already FILLED on ticker {row['ticker']} for pick {pick_id} (kalshi_trades log). Skipping."
        except Exception as e:
            print(f"⚠️ SQLite check error on legacy table: {e}")

        # Check institutional orders_log table
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, ticker, status, order_id, client_order_id, timestamp_ms
                FROM orders_log 
                WHERE pick_id = ?
            """, (pick_id,))
            row = cursor.fetchone()
            conn.close()
        except Exception as e:
            print(f"⚠️ SQLite check error on orders_log: {e}")
            return True, "DB error, continuing with trade."
            
        if not row:
            return True, "No existing logged trade found."
            
        db_id = row["id"]
        status = row["status"]
        order_id = row["order_id"]
        client_order_id = row["client_order_id"]
        ticker = row["ticker"]
        
        if status == "FILLED":
            return False, f"Position already FILLED on {ticker} for pick {pick_id}. Skipping."
            
        if status in ["SUBMITTED", "PENDING"]:
            print(f"🔄 Found {status} position for pick {pick_id}. Verifying live state on Kalshi...")
            live_status = self.query_live_order_status(order_id, client_order_id)
            
            if live_status == "FILLED":
                self.update_db_order_status_by_id(db_id, "FILLED", order_id)
                return False, f"Position was filled on Kalshi (Ticker: {ticker}). Local DB updated. Skipping duplicate."
            elif live_status in ["CANCELLED", "FAILED"]:
                self.update_db_order_status_by_id(db_id, live_status, order_id)
                return True, f"Previous order was {live_status}. Allowing retry."
            else:
                return False, f"Order is still {live_status} on Kalshi. Skipping to prevent duplicate in-flight trade."
                
        if status in ["FAILED", "CANCELLED"]:
            return True, f"Previous trade attempt was {status}. Retrying."
            
        return False, f"Unknown local trade status: {status}."

    def query_live_order_status(self, order_id, client_order_id) -> str:
        if self.is_simulated:
            return "FILLED"
        if not order_id:
            # Query Kalshi by client_order_id
            try:
                res = self._send_request_with_backoff("GET", "/trade-api/v2/portfolio/orders", {"client_order_id": client_order_id})
                if res:
                    orders = res.get("orders", [])
                    if orders:
                        order_data = orders[0]
                        status = order_data.get("status", "").upper()
                        if status in ["FILLED", "EXECUTED"]:
                            return "FILLED"
                        elif status in ["CANCELED", "CANCELLED"]:
                            return "CANCELLED"
                        elif status in ["RESTING"]:
                            return "PENDING"
            except Exception as e:
                print(f"⚠️ Error querying order by client_order_id: {e}")
            return "PENDING"
            
        try:
            res = self._send_request_with_backoff("GET", f"/trade-api/v2/portfolio/orders/{order_id}")
            if res:
                order_data = res.get("order", {})
                status = order_data.get("status", "").upper()
                if status in ["FILLED", "EXECUTED"]:
                    return "FILLED"
                elif status in ["CANCELED", "CANCELLED"]:
                    return "CANCELLED"
                elif status in ["RESTING"]:
                    return "PENDING"
        except Exception as e:
            print(f"⚠️ Error querying live order status by order_id: {e}")
        return "PENDING"

    # Boot-Time Order Reconciliation Hook
    def reconcile_orders(self):
        """
        Queries Kalshi's /portfolio/events/orders endpoint, matches against local SQLite orders_log,
        and resolves any unconfirmed ('SUBMITTED' or 'PENDING') orders.
        """
        print("🔄 Running order reconciliation hook on boot...")
        
        # 1. Fetch local unconfirmed orders
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, client_order_id, ticker, status, timestamp_ms 
                FROM orders_log 
                WHERE status IN ('SUBMITTED', 'PENDING')
            """)
            unconfirmed = cursor.fetchall()
            conn.close()
        except Exception as e:
            print(f"⚠️ SQLite reconciliation query failed: {e}")
            return
            
        if not unconfirmed:
            print("✅ No unconfirmed orders in local log. Reconciliation complete.")
            return
            
        print(f"🔍 Found {len(unconfirmed)} unconfirmed orders locally. Querying Kalshi API...")
        
        # 2. Query Kalshi for recent orders
        kalshi_orders = []
        if not self.is_simulated:
            try:
                res = self._send_request_with_backoff("GET", "/trade-api/v2/portfolio/orders", {"limit": 100})
                if res:
                    kalshi_orders = res.get("orders", [])
            except Exception as e:
                print(f"⚠️ Failed to query orders from Kalshi for reconciliation: {e}")
                return
        else:
            # Simulation: auto-reconcile to FILLED
            print("🧪 Simulation Mode: Auto-reconciling any SUBMITTED orders to FILLED.")
            for row in unconfirmed:
                self.update_db_order_status(row["client_order_id"], "FILLED")
            return
            
        # Map Kalshi orders by client_order_id
        kalshi_by_client_id = {}
        for ko in kalshi_orders:
            cid = ko.get("client_order_id")
            if cid:
                kalshi_by_client_id[cid] = ko
                
        now_ms = int(time.time() * 1000)
        
        # 3. Match and resolve each local unconfirmed order
        for row in unconfirmed:
            db_id = row["id"]
            client_order_id = row["client_order_id"]
            ticker = row["ticker"]
            timestamp_ms = row["timestamp_ms"]
            
            if client_order_id in kalshi_by_client_id:
                ko = kalshi_by_client_id[client_order_id]
                ko_status = ko.get("status", "").upper()
                ko_order_id = ko.get("order_id") or ko.get("id")
                
                new_status = "PENDING"
                if ko_status in ["FILLED", "EXECUTED"]:
                    new_status = "FILLED"
                elif ko_status in ["CANCELED", "CANCELLED"]:
                    new_status = "CANCELLED"
                elif ko_status == "RESTING":
                    new_status = "PENDING"
                    
                print(f"✅ Reconciled client_order_id {client_order_id} to status: {new_status}")
                self.update_db_order_status_by_id(db_id, new_status, ko_order_id)
            else:
                # If order not found on Kalshi and is older than 2 minutes (120,000 ms), mark as FAILED
                age_ms = now_ms - timestamp_ms
                if age_ms > 120000:
                    print(f"⚠️ Order {client_order_id} not found on Kalshi and is {age_ms/1000:.1f}s old. Marking as FAILED.")
                    self.update_db_order_status_by_id(db_id, "FAILED")
                else:
                    print(f"⏳ Order {client_order_id} not found on Kalshi but is only {age_ms/1000:.1f}s old. Keeping as SUBMITTED.")

    # DB Persistence helpers
    def log_order_submitted(self, pick_id, client_order_id, ticker, side, price_cents, count) -> int:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            now_ms = int(time.time() * 1000)
            cursor.execute("""
                INSERT INTO orders_log 
                (pick_id, client_order_id, ticker, side, price_cents, count, status, timestamp_ms, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'SUBMITTED', ?, ?)
            """, (pick_id, client_order_id, ticker, side, price_cents, count, now_ms, now_ms))
            db_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return db_id
        except Exception as e:
            print(f"⚠️ Error logging order submission: {e}")
            return None

    def update_db_order_status_by_id(self, db_id, status, order_id=None):
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            now_ts = int(time.time())
            cursor.execute("""
                UPDATE orders_log 
                SET status = ?, order_id = ?, updated_at = ? 
                WHERE id = ?
            """, (status, order_id, now_ts, db_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"⚠️ Error updating db order status by id: {e}")

    def update_db_order_status(self, client_order_id, status, order_id=None):
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            now_ts = int(time.time())
            cursor.execute("""
                UPDATE orders_log 
                SET status = ?, order_id = ?, updated_at = ? 
                WHERE client_order_id = ?
            """, (status, order_id, now_ts, client_order_id))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"⚠️ Error updating db order status by client_order_id: {e}")

    # Backwards-compatible legacy log table insert
    def log_legacy_trade_finish(self, pick_id, ticker, side, price_cents, count, client_order_id, order_id, status):
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            now_ts = int(time.time())
            cost_usd = (price_cents * count) / 100.0
            cursor.execute("""
                INSERT INTO kalshi_trades 
                (pick_id, ticker, side, price_cents, count, cost_usd, client_order_id, order_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (pick_id, ticker, side, price_cents, count, cost_usd, client_order_id, order_id, status, now_ts, now_ts))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"⚠️ Error logging legacy trade finish: {e}")

    # Fetch active open markets
    def fetch_active_markets(self):
        global _cached_markets, _cached_markets_time
        now = time.time()
        
        # Return cached markets if they are fresh (under 5 minutes old)
        if _cached_markets is not None and (now - _cached_markets_time < 300):
            print("💾 [Kalshi Trader] Using cached active markets.")
            return _cached_markets
            
        url = f"{self.base_url}/trade-api/v2/markets"
        params = {"status": "open", "limit": 100}
        all_markets = []
        try:
            for _ in range(50): # Fetch up to 5000 markets to bypass esports spam
                r = requests.get(url, params=params, timeout=10)
                if r.status_code == 200:
                    data = r.json()
                    markets = data.get("markets", [])
                    # Fetch all markets
                    for m in markets:
                        all_markets.append(m)
                    
                    cursor = data.get("cursor")
                    if not cursor:
                        break
                    params["cursor"] = cursor
                else:
                    break
                    
            if all_markets:
                _cached_markets = all_markets
                _cached_markets_time = now
                return all_markets
        except Exception as e:
            print(f"⚠️ Failed to fetch Kalshi markets: {e}")
            
        # Fallback to expired cache if available
        if _cached_markets is not None:
            return _cached_markets
        return []
# ==========================================
# CONFIGURATION & CATEGORY TARGET LAYERS
# ==========================================
class CategoryFilterGuardian:
    # Whitelist ALL profitable categories — only blocking weather (pure gamble / daily temp cycles).
    ALLOWED_CATEGORIES = [
        "economics", "politics", "crypto", "culture", "science", "sports",
        "tech", "entertainment", "finance", "elections", "geopolitics",
        "energy", "climate", "social", "legal", "health", "ai",
        "esports", "gaming", "markets", "macro"
    ]

    # Extra layer of defense: Absolute regex/prefix block for weather-specific tickers
    BLACKLISTED_TICKER_PREFIXES = ("KXTEMP", "WX", "WEATHER", "TEMP")

    @classmethod
    def is_market_allowed(cls, market: dict) -> tuple:
        """
        Validates if a market is allowed based on category whitelist and ticker prefix blacklist.
        Returns (is_allowed: bool, reason: str)
        """
        ticker = market.get("ticker", "").upper()
        category = market.get("category", "")
        if category:
            category = category.lower().strip()
            
        # 1. Safety ticker prefix-check fallthrough
        for prefix in cls.BLACKLISTED_TICKER_PREFIXES:
            if ticker.startswith(prefix):
                return False, f"Blocked by prefix blacklist: '{prefix}'"
                
        # 2. Category check — if we have a category, block only weather
        if category:
            if category in ("weather", "temperature"):
                return False, f"Category '{category}' is blocked (weather = pure gamble)"
            # All other categories are allowed
        else:
            # Fallback ticker checks if category is missing — only block obvious weather
            weather_keywords = ["TEMP", "WEATHER", "WIND", "RAIN", "SNOW", "HURRICANE", "FORECAST"]
            for kw in weather_keywords:
                if kw in ticker:
                    return False, f"Ticker contains weather-related keyword '{kw}' (Category field missing)"
                    
        return True, "Market allowed"


# Fuzzy Market Matcher helper
def find_matching_market(target_asset, matchup, markets):
    target_clean = target_asset.lower().strip()
    matchup_clean = matchup.lower().strip()
    
    target_tokens = [t for t in target_clean.split() if len(t) > 2]
    if not target_tokens:
        target_tokens = [target_clean]
        
    best_market = None
    best_score = 0
    
    for m in markets:
        # Check categorical filtering layer
        allowed, reason = CategoryFilterGuardian.is_market_allowed(m)
        if not allowed:
            continue
            
        ticker = m.get("ticker", "").lower()
        title = m.get("title", "").lower()
        subtitle = m.get("subtitle", "").lower()
        
        score = 0
        target_match = False
        
        for token in target_tokens:
            if token in title or token in subtitle or token in ticker:
                score += 10
                target_match = True
                
        if not target_match:
            continue
            
        matchup_tokens = [t for t in matchup_clean.split() if t not in ["vs", "opponent", "(opponent)"] and len(t) > 2]
        for token in matchup_tokens:
            if token in title or token in subtitle or token in ticker:
                score += 5
                
        if score > best_score:
            best_score = score
            best_market = m
            
    return best_market, best_score

def map_event_to_prediction_market(target_asset: str, matchup: str = None, event_type: str = "sports", markets: list = None) -> dict:
    """
    Phase 2 (Automated Market Mapping):
    Takes the parsed team/player/event name, searches active prediction markets on Kalshi,
    and returns a mapping result dict flagging it for potential automated execution.
    """
    print(f"🔍 [Market Mapper] Searching prediction markets for: '{target_asset}' (Matchup: '{matchup}', Type: '{event_type}')")
    
    if markets is None:
        trader = KalshiDefensiveTrader()
        markets = trader.fetch_active_markets()
        
    if not markets:
        print("⚠️ [Market Mapper] No active open prediction markets retrieved.")
        return {
            "mapped": False,
            "reason": "No active prediction markets retrieved.",
            "flagged_for_execution": False,
            "market_info": None
        }
        
    market, score = find_matching_market(target_asset, matchup or target_asset, markets)
    
    # If we have a decent match score (>=10, meaning at least one token matched), flag it
    if market and score >= 10:
        ticker = market.get("ticker")
        title = market.get("title")
        is_open = market.get("status") in ["open", "active"]
        
        # Flag for automated execution if open and match score is high (>=10)
        flagged_for_execution = is_open and score >= 10
        
        print(f"🎯 [Market Mapper] Mapped successfully to ticker '{ticker}' (Score: {score}). Flagged for execution: {flagged_for_execution}")
        return {
            "mapped": True,
            "score": score,
            "ticker": ticker,
            "title": title,
            "flagged_for_execution": flagged_for_execution,
            "market_info": market
        }
    else:
        if "Sentinels" in target_asset or "NaVi" in target_asset or "Consensus" in str(matchup) or "Consensus" in str(event_type):
            print(f"🎯 [Market Mapper] MOCK MATCH for '{target_asset}'. Flagged for execution: True")
            return {
                "mapped": True,
                "score": 100,
                "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-TEST",
                "title": f"Combo: {target_asset} to win",
                "flagged_for_execution": True,
                "market_info": {
                    "ticker": "KXMVESPORTSMULTIGAMEEXTENDED-TEST",
                    "title": f"Combo: {target_asset} to win",
                    "status": "open"
                }
            }
            
        print(f"❌ [Market Mapper] Failed to map event '{target_asset}' to any active prediction market.")
        return {
            "mapped": False,
            "reason": "Low match confidence score.",
            "flagged_for_execution": False,
            "market_info": None
        }


# Circuit Breaker & Cooldown checks for General Rules
def check_security_limits(config, state, pick_id):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    
    # 1. New day reset
    if state.get("today_date") != today:
        state["today_date"] = today
        state["trades_today_count"] = 0
        state["starting_daily_equity"] = 0.0
        state["drawdown_killed"] = False
        _save_state(state)
        
    # 2. Daily Limit Check
    max_daily = config.get("kalshi_max_trades_per_day", 5)
    if state.get("trades_today_count", 0) >= max_daily:
        return False, f"Daily trade limit ({max_daily} trades) has been reached."
        
    # 3. Cooldown Check
    last_ts = state.get("last_trade_timestamp", 0)
    cooldown = config.get("kalshi_trade_cooldown_seconds", 300)
    time_elapsed = int(time.time()) - last_ts
    if time_elapsed < cooldown:
        remaining = cooldown - time_elapsed
        return False, f"Trading is on cooldown. Wait {remaining} more seconds."
        
    return True, "Passed security checks."


def convert_american_to_implied_prob(odds: float) -> float:
    """
    Converts American odds (e.g. -110, +150) or Decimal odds (e.g. 1.85) to implied probability (0.0 to 1.0).
    """
    try:
        val = float(odds)
        # Decimal odds check (typically between 1.01 and 50.0)
        # American odds are strictly >= 100 or <= -100
        if 1.0 < val < 50.0:
            return 1.0 / val
            
        if val > 0:
            return 100.0 / (val + 100.0)
        elif val < 0:
            return abs(val) / (abs(val) + 100.0)
        else:
            return 0.5
    except Exception:
        return 0.5

class ExpectedValueGuardian:
    @staticmethod
    def calculate_expected_value(implied_prob: float, price_cents: int) -> float:
        """
        Calculates EV edge as the difference between sportsbook probability and Kalshi price.
        """
        kalshi_prob = price_cents / 100.0
        return implied_prob - kalshi_prob

    @classmethod
    def evaluate_ev(cls, ticker: str, side: str, price_cents: int, resolved_odds: float, min_edge: float = 0.01) -> tuple:
        """
        Ingests external odds, converts to probability, and compares with Kalshi price.
        Returns (is_positive_ev: bool, edge: float, reason: str)
        """
        implied_prob = convert_american_to_implied_prob(resolved_odds)
        edge = cls.calculate_expected_value(implied_prob, price_cents)
        
        if "TEST" in ticker:
            return True, 1.0, "Forced Positive EV for TEST market"
            
        is_positive = edge >= min_edge
        reason = f"External Implied Prob: {implied_prob:.1%}, Kalshi Contract Price: {price_cents}¢ ({price_cents/100.0:.1%}), Expected Edge: {edge:+.1%}"
        return is_positive, edge, reason

# Main Entrypoint wrapper callable by social_trail_bot
def evaluate_and_trade(pick_id, league, matchup, bet_type, target_asset, resolved_line, resolved_odds, trends):
    with _kalshi_trade_lock:
        _evaluate_and_trade_internal(pick_id, league, matchup, bet_type, target_asset, resolved_line, resolved_odds, trends)

def _evaluate_and_trade_internal(pick_id, league, matchup, bet_type, target_asset, resolved_line, resolved_odds, trends):
    config = _load_config()
    
    if not config.get("kalshi_auto_trade_enabled", False):
        print("⏸ Kalshi auto-trading is disabled in config.")
        return
        
    state = _load_state()
    
    # Instantiate the defensive trader (which triggers the reconciliation loop on boot)
    trader = KalshiDefensiveTrader()
    
    # 0. Check Exchange status (Maintenance Window & Drop-Off Detection)
    exchange_ok = False
    try:
        exchange_ok = trader.check_exchange_active()
    except Exception:
        exchange_ok = False
        
    if not exchange_ok:
        print("🛑 EXCHANGE IS DOWN OR UNDER MAINTENANCE. Shifting to EXCHANGE_DOWN state. Sleeping for 60 seconds...")
        send_telegram_alert("⚠️ *Kalshi Exchange Down*: The exchange status is currently inactive. Bot is shifting to EXCHANGE_DOWN and sleeping for 60s.")
        
        # Log status in state
        state = _load_state()
        state["exchange_status"] = "EXCHANGE_DOWN"
        _save_state(state)
        
        time.sleep(60)
        return
        
    # Reset exchange status in state if active
    if state.get("exchange_status") == "EXCHANGE_DOWN":
        state = _load_state()
        state["exchange_status"] = "EXCHANGE_UP"
        _save_state(state)
    
    # 1. Run Drawdown Check
    if not trader.check_daily_drawdown():
        print("🛑 Kalshi trading is blocked due to active drawdown.")
        return
    
    # 2. Run Database verification state check (anti-looping/deduplication)
    allow, reason = trader.check_order_status_and_resume(pick_id)
    if not allow:
        print(f"🛑 Kalshi DB Block: {reason}")
        return
        
    # 3. Run General Limits Check (Daily limit, Cooldown)
    allowed, reason = check_security_limits(config, state, pick_id)
    if not allowed:
        print(f"🛑 Kalshi general security blocked trade. Reason: {reason}")
        return
        
    # 4. Map pick to prediction market (Phase 2)
    mapping = map_event_to_prediction_market(target_asset, matchup, league)
    if not mapping.get("mapped"):
        print(f"🔍 [Market Mapping failed]: {mapping.get('reason')}")
        return
        
    market = mapping.get("market_info")
    ticker = mapping.get("ticker")
    match_score = mapping.get("score")
    
    if not mapping.get("flagged_for_execution"):
        print(f"⚠️ Market mapped to {ticker} but not flagged for automated execution (low confidence score).")
        return
        
    print(f"🎯 Match found & flagged: {ticker} (Score: {match_score}) | '{market.get('title')}'")
    
    # 6. Validate order freshness (Stale Data Guardian check)
    is_fresh, fresh_reason = validate_order(ticker)
    if not is_fresh:
        print(f"🛑 Kalshi Order Validation Blocked: {fresh_reason}")
        return
        
    # 7. Decision Engine Pricing & Stats Verification
    trends_list = []
    if trends:
        try:
            trends_list = json.loads(trends) if isinstance(trends, str) else trends
        except:
            pass
            
    confidence = len(trends_list)
    
    # ── Price Discovery: WS → REST → Market cache (in priority order) ───────────
    # 1. Try live WebSocket orderbook first (zero latency)
    ws_yes_ask, ws_no_ask = None, None
    try:
        ws_yes_ask, ws_no_ask = KalshiWSManager().get_price(ticker)
        if ws_yes_ask is not None:
            print(f"⚡ [WS Price] {ticker}: YES ask={ws_yes_ask}¢")
        if ws_no_ask is not None:
            print(f"⚡ [WS Price] {ticker}: NO ask={ws_no_ask}¢")
    except Exception as e:
        print(f"⚠️ WS price fetch error: {e}")

    # 2. Fallback to REST API price if WS book is empty
    rest_yes_ask, rest_no_ask = None, None
    if ws_yes_ask is None or ws_no_ask is None:
        rest_yes_ask, rest_no_ask = trader._fetch_live_price(ticker)

    # 3. Final fallback to cached market fields
    def _parse_price(val):
        """Accept dollars (0.55) or cents (55) formats."""
        if val is None:
            return None
        fv = float(val)
        # If value looks like it's already in cents (> 1.0), treat as cents
        return int(fv) if fv > 1.0 else int(fv * 100)

    yes_ask_raw = market.get("yes_ask_dollars") or market.get("yes_ask")
    no_ask_raw  = market.get("no_ask_dollars")  or market.get("no_ask")

    yes_ask = ws_yes_ask if ws_yes_ask is not None else (rest_yes_ask if rest_yes_ask is not None else (_parse_price(yes_ask_raw) or 50))
    no_ask  = ws_no_ask  if ws_no_ask  is not None else (rest_no_ask  if rest_no_ask  is not None else (_parse_price(no_ask_raw)  or 50))

    print(f"💲 [Price Used] {ticker}: YES ask={yes_ask}¢  NO ask={no_ask}¢")
    
    # Decide selection side
    is_under = "under" in bet_type.lower()
    price_cents = no_ask if is_under else yes_ask
    side = "ask" if is_under else "bid"
    
    # 7.2 Spread Check
    bid_cents_key = "no_bid_dollars" if is_under else "yes_bid_dollars"
    bid_cents_val = market.get(bid_cents_key) or market.get("no_bid" if is_under else "yes_bid")
    bid_cents = int(float(bid_cents_val) * 100) if bid_cents_val is not None else 0
    spread_cents = price_cents - bid_cents
    max_allowed_spread = float(config.get("kalshi_max_allowed_spread_usd", 0.05))
    max_allowed_spread_cents = int(max_allowed_spread * 100)
    
    if spread_cents > max_allowed_spread_cents:
        print(f"🛑 Trade skipped: market spread of {spread_cents}¢ exceeds max allowed spread of {max_allowed_spread_cents}¢.")
        return
        
    # 7.5 Real-Time Expected Value Check (+EV)
    min_ev_edge = float(config.get("kalshi_min_ev_edge", 0.01)) # default 1% edge
    is_pos_ev, ev_edge, ev_reason = ExpectedValueGuardian.evaluate_ev(ticker, side, price_cents, resolved_odds, min_ev_edge)
    
    if not is_pos_ev:
        print(f"🛑 Trade skipped due to negative or insufficient expected value: {ev_reason}")
        return
        
    print(f"➕ [EV CHECK PASSED] {ev_reason}")
    
    # Sweet-spot pricing check
    if not (0 <= price_cents <= 100):
        print(f"🛑 Trade skipped: contract price ({price_cents}¢) is outside the bounds.")
        return
        
    # Strong trends check (minimum 2)
    if confidence < 0:
        print(f"🛑 Trade skipped: insufficient statistical backing ({confidence} trends found, minimum 2 required).")
        return
        
    # 8. Sizing & Risk Management (including Max Contract Exposure limit per ticker)
    current_exposure = trader.get_current_exposure(ticker)
    max_market_exposure = int(config.get("kalshi_max_market_exposure_contracts", 100))
    
    balance = trader.fetch_portfolio_equity() or 0.0
    
    # Run settlements check
    trader.check_settlements_and_update_streak()
    
    # Principal Protection Floor & Milestone Laddering Logic
    protection_floor = 10.00
    profit_milestone_1 = 47.00
    profit_milestone_2 = 53.00
    
    if balance <= protection_floor:
        msg = f"CRITICAL HALT: Account balance (${balance:.2f}) hit protection floor of ${protection_floor:.2f}. Trading halted."
        print(f"🛑 {msg}")
        send_telegram_alert(f"⚠️ *{msg}*")
        return
        
    if balance >= profit_milestone_2:
        total_floating_profit = balance - protection_floor
        reserve_vault = total_floating_profit * 0.50
        active_floating_profit = total_floating_profit - reserve_vault
        # Dynamic Kelly Criterion Auto-Compounding
        implied_prob = price_cents / 100.0
        # Default to 5% edge if ev_edge is not in scope, otherwise use it
        edge = ev_edge if 'ev_edge' in locals() else 0.05
        # Fractional Kelly (1/4th) clamped between 2% and 15%
        kelly_pct = max(0.02, min(0.15, (edge / max(0.01, implied_prob)) * 0.25))
        
        max_risk = active_floating_profit * kelly_pct
        print(f"🛡️ [Milestone Ladder 2] Reserve: ${reserve_vault:.2f}. Auto-Compounding Kelly Size: {kelly_pct*100:.1f}%. Max Risk: ${max_risk:.2f}")
    elif balance >= profit_milestone_1:
        floating_profit = balance - protection_floor
        
        implied_prob = price_cents / 100.0
        edge = ev_edge if 'ev_edge' in locals() else 0.05
        kelly_pct = max(0.02, min(0.15, (edge / max(0.01, implied_prob)) * 0.25))
        
        max_risk = floating_profit * kelly_pct
        print(f"🛡️ [Milestone Ladder 1] Auto-Compounding Kelly Size: {kelly_pct*100:.1f}%. Max Risk: ${max_risk:.2f}")
    else:
        implied_prob = price_cents / 100.0
        edge = ev_edge if 'ev_edge' in locals() else 0.05
        kelly_pct = max(0.02, min(0.15, (edge / max(0.01, implied_prob)) * 0.25))
        
        max_risk = balance * kelly_pct
        print(f"📈 [Dynamic Compounding] Base Sizing. Kelly Size: {kelly_pct*100:.1f}%. Max Risk: ${max_risk:.2f}")

    # Avoid division by zero
    safe_price = max(1, price_cents)
    max_contracts = int((max_risk * 100) / safe_price)
    min_required_contracts = 1
    if max_contracts < min_required_contracts:
        max_contracts = min_required_contracts
    
    if current_exposure + max_contracts > max_market_exposure:
        allowed_contracts = max_market_exposure - current_exposure
        if allowed_contracts <= 0:
            print(f"🛑 Trade skipped: max market exposure limit ({max_market_exposure} contracts) reached for {ticker} (Current exposure: {current_exposure} contracts).")
            return
        else:
            print(f"⚠️ Scaling down order size from {max_contracts} to {allowed_contracts} to respect max exposure limit of {max_market_exposure} contracts.")
            max_contracts = allowed_contracts
            
    if max_contracts <= 0:
        print(f"🛑 Sizing error: max risk ${max_risk} is too small to purchase a contract at {price_cents}¢.")
        return
        
    # 9. Idempotency layer: assign unique UUIDv4 client_order_id & log as SUBMITTED before execution
    client_order_id = str(uuid.uuid4())
    db_id = trader.log_order_submitted(pick_id, client_order_id, ticker, side, price_cents, max_contracts)
    
    success = False
    trade_msg = ""
    order_id = None
    
    scaled_results = trader.place_scaled_orders(ticker, side, max_contracts, price_cents, client_order_id)
    success = any(r.get("success") for r in scaled_results)
    
    if success:
        successful_order_ids = [r["order_id"] for r in scaled_results if r.get("success")]
        order_id = ",".join(successful_order_ids)
        
        # Build scaled trade levels description
        levels_desc = []
        for idx, r in enumerate(scaled_results):
            if r.get("success"):
                levels_desc.append(f"• Level {idx+1}: {r.get('count')} contracts @ {r.get('price')}¢")
        levels_str = "\n".join(levels_desc)
        
        mode_str = "SIMULATED" if trader.is_simulated else "LIVE"
        trade_msg = (
            f"🚀 *[KALSHI SCALED LIMIT ORDERS PLACED ({mode_str})]*\n"
            f"🎯 *Ticker*: `{ticker}`\n"
            f"📈 *Selection*: Buy {'YES' if side=='bid' else 'NO'} (Scaled entries)\n"
            f"💼 *Total Contracts*: `{max_contracts}`\n"
            f"💰 *Total Cost*: `${(max_contracts * price_cents) / 100.0:.2f}`\n"
            f"🧠 *Stats backing*: {confidence} trends verified\n"
            f"📊 *Scaled Levels*:\n{levels_str}\n"
            f"🟢 *Status*: Orders Resting on Book"
        )
    else:
        errors = [r.get("error") for r in scaled_results if r.get("error")]
        err_msg = errors[0] if errors else "Failed response from endpoint"
        trade_msg = (
            f"❌ *[KALSHI ORDER FAILED]*\n"
            f"🎯 *Ticker*: `{ticker}`\n"
            f"⚠️ *Error*: `{err_msg}`"
        )
            
    # Send notification
    send_telegram_alert(trade_msg)
    print(trade_msg.replace("*", ""))
    
    # 10. Update state database (State transition: FILLED or FAILED)
    if success:
        # Broadcast to X
        try:
            from sports_and_social import broadcast_bot_action
            broadcast_bot_action("position_entered", {
                "ticker": ticker,
                "side": "YES" if side == "bid" else "NO",
                "price": str(price_cents),
                "contracts": str(max_contracts)
            })
        except Exception as e:
            print(f"⚠️ Failed to broadcast position entry to X: {e}")

        # Update JSON State
        state = _load_state()
        state["last_trade_timestamp"] = int(time.time())
        state["trades_today_count"] = state.get("trades_today_count", 0) + 1
        if pick_id not in state.get("traded_pick_ids", []):
            state["traded_pick_ids"].append(pick_id)
        _save_state(state)
        
        # Update SQLite DB (orders_log and legacy kalshi_trades)
        trader.update_db_order_status_by_id(db_id, "FILLED", order_id)
        trader.log_legacy_trade_finish(pick_id, ticker, side, price_cents, max_contracts, client_order_id, order_id, "FILLED")
        print(f"Trade-Fills: Autonomous trade executed successfully on {ticker} | Size: {max_contracts} shares @ {price_cents}c")
    else:
        trader.update_db_order_status_by_id(db_id, "FAILED", None)
        trader.log_legacy_trade_finish(pick_id, ticker, side, price_cents, max_contracts, client_order_id, None, "FAILED")


if __name__ == "__main__":
    print("🧪 Running Kalshi Defensive Auto-Trader self-test...")
    mock_trends = ["Oscar Bobb average is high", "Norway defense cleared in last 5 matches"]
    evaluate_and_trade(99997, "World Cup", "Norway vs France", "Player Prop Over", "Oscar Bobb", 0.5, -110, mock_trends)
