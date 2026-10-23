import os
import time
import uuid
import json
import base64
import hashlib
import random
import sqlite3
import asyncio
import requests
from pathlib import Path
from collections import deque
from datetime import datetime, timezone
from dotenv import load_dotenv

try:
    import websockets
    HAS_WEBSOCKETS = True
except BaseException:
    websockets = None
    HAS_WEBSOCKETS = False

try:
    import aiohttp
    HAS_AIOHTTP = True
except BaseException:
    aiohttp = None
    HAS_AIOHTTP = False

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)
load_dotenv(BASE_DIR.parent / ".env")

# ==========================================
# CORE CREDENTIALS & ENDPOINTS
# ==========================================
KALSHI_KEY_ID = os.getenv(
    "KALSHI_KEY_ID",
    os.getenv("KALSHI_API_KEY", "df3fbc6c-0d8c-4601-9b00-f4f5beb90c34"),
)
PRIVATE_KEY_PATH = os.getenv(
    "KALSHI_KEY_PATH",
    os.getenv("KALSHI_PRIVATE_KEY_PATH", str(BASE_DIR / "kalshi_private.pem")),
)
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN", "8886914565:AAFbk2LRiPpPvnJDt3qnhSXw23Lj_h2TXCA"
).strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

BASE_URL = "https://api.elections.kalshi.com"
WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"

# ==========================================
# EXECUTION, OBI & SCALPER PARAMETERS
# ==========================================
LIVE_MODE = os.getenv("DEFAULT_MODE", "SIM").upper() == "LIVE"
SCAN_INTERVAL = 1.5              # Background REST sync & exit sweep interval
COOLDOWN_SECONDS = 45            # Per-ticker entry lock

# Risk & Sizing
KELLY_FRACTION = 0.25            # Quarter-Kelly sizing
MAX_BET_DOLLARS = 25.00          # Max allocation per trade
MAX_DAILY_DRAWDOWN = -75.00      # Circuit breaker hard stop ($)
MIN_LIQUIDITY_VOL = 350          # Minimum market volume
MIN_ORDERBOOK_DEPTH = 15         # Minimum contracts on best ask
OBI_TOXIC_THRESHOLD = -0.45      # Block snipes if ask wall dominates bid support

# Auto-Exit & Green-Out Scalper Rules
TAKE_PROFIT_CENTS = 8            # Instant hard take-profit (+8c per contract)
TRAIL_ACTIVATE_CENTS = 5         # Arm trailing stop once position is up +5c
TRAIL_DROP_CENTS = 2             # Fire exit if bid drops 2c off High-Water Mark
STOP_LOSS_CENTS = -9             # Hard stop-loss cut (-9c per contract)
CLOCK_PAUSE_STALE_SEC = 75       # If tick velocity halts >75s while green -> Green-Out

BANNED_PREFIXES = ("KXMVE", "COMBO", "PARLAY", "MULTIGAME")

SPORT_PROFILES = {
    "TENNIS": {
        "keywords": ("TENNIS", "ATP", "WTA", "CHALLENGER", "ITF"),
        "max_spread": 8,
        "min_ev": 0.035,
        "swing_trigger": 6
    },
    "NFL": {
        "keywords": ("NFL", "NCAAF", "FOOTBALL", "TD", "PASS", "RUSH", "REC"),
        "max_spread": 5,
        "min_ev": 0.04,
        "swing_trigger": 5
    },
    "MLB": {
        "keywords": ("MLB", "BASEBALL", "STRIKEOUT", "HITS", "HR", "RUNS"),
        "max_spread": 5,
        "min_ev": 0.04,
        "swing_trigger": 5
    },
    "WNBA": {
        "keywords": ("WNBA", "NBA", "BASKETBALL", "PTS", "REB", "AST"),
        "max_spread": 6,
        "min_ev": 0.04,
        "swing_trigger": 6
    },
    "ESPORTS": {
        "keywords": ("ESPORTS", "CS2", "VALORANT", "LOL", "DOTA", "COD", "NAVI", "FAZE", "VITALITY", "SPIRIT", "T1", "GENG", "KILLS", "MAP"),
        "max_spread": 7,
        "min_ev": 0.035,
        "swing_trigger": 6
    }
}

# ==========================================
# IN-MEMORY STATE & POSITION TRACKERS
# ==========================================
market_history = {}       # {ticker: deque(maxlen=12)}
cooldown_tracker = {}     # {ticker: last_entry_ts}
sharp_fair_cache = {}     # {ticker: sharp_implied_prob}
open_positions = {}       # {ticker: {"side": "yes", "entry": 48, "qty": 10, "hwm": 48, "last_tick_ts": ts}}
starting_bankroll = None
sim_realized_pnl = 0.0
total_trades_count = 0
total_cashouts_count = 0
circuit_breaker_tripped = False
pending_tg_Queue = deque(maxlen=30)
startup_notified = False

# ==========================================
# PURE-PYTHON RFC 8032 ED25519 SIGNER
# ==========================================
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_BY = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_B = (_BX, _BY, 1, (_BX * _BY) % _P)

def _ed_add(pt1, pt2):
    x1, y1, z1, t1 = pt1
    x2, y2, z2, t2 = pt2
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (t1 * 2 * _D * t2) % _P
    d = (z1 * 2 * z2) % _P
    e = (b - a) % _P
    f = (d - c) % _P
    g = (d + c) % _P
    h = (b + a) % _P
    return (e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P

def _ed_scalarmult(pt, e):
    res = (0, 1, 1, 0)
    addend = pt
    while e > 0:
        if e & 1:
            res = _ed_add(res, addend)
        addend = _ed_add(addend, addend)
        e >>= 1
    return res

def _ed_encode(pt):
    x, y, z, _ = pt
    zi = pow(z, _P - 2, _P)
    x = (x * zi) % _P
    y = (y * zi) % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")

def _extract_ed25519_seed(path: str) -> bytes:
    candidates = [
        Path(path),
        BASE_DIR / "kalshi_private.pem",
        BASE_DIR / "kalshi_private_key.pem",
        BASE_DIR.parent / "kalshi_private_key.pem",
        BASE_DIR.parent / "kalshi_private.pem",
    ]
    pem_text = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    if not pem_text:
        for p in candidates:
            if p.exists():
                pem_text = p.read_text(encoding="utf-8").strip()
                break
    if not pem_text:
        pem_text = "MC4CAQAwBQYDK2VwBCIEIBuv3LT8M7MKQyBnJK5UqCbD6ynZr1BnBI/eYoRMOjLM"
    b64_lines = [
        ln.strip() for ln in pem_text.splitlines() if ln.strip() and not ln.strip().startswith("-----")
    ]
    der_bytes = base64.b64decode("".join(b64_lines))
    return der_bytes[-32:]

_ED_SEED = _extract_ed25519_seed(PRIVATE_KEY_PATH)
_ED_H = hashlib.sha512(_ED_SEED).digest()
_ED_A = int.from_bytes(_ED_H[:32], "little")
_ED_A &= (1 << 254) - 8
_ED_A |= (1 << 254)
_ED_PREFIX = _ED_H[32:64]
_ED_PUB = _ed_encode(_ed_scalarmult(_B, _ED_A))

def sign_ed25519_msg(msg: bytes) -> bytes:
    r = int.from_bytes(hashlib.sha512(_ED_PREFIX + msg).digest(), "little") % _L
    r_bytes = _ed_encode(_ed_scalarmult(_B, r))
    k = int.from_bytes(hashlib.sha512(r_bytes + _ED_PUB + msg).digest(), "little") % _L
    s = (r + k * _ED_A) % _L
    return r_bytes + s.to_bytes(32, "little")

def get_auth_headers(method: str, path: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    clean_path = path.split("?")[0]
    msg = f"{timestamp}{method.upper()}{clean_path}".encode("utf-8")
    signature = sign_ed25519_msg(msg)
    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }

# ==========================================
# SQLITE TELEMETRY & TELEGRAM NOTIFIER
# ==========================================
def init_db():
    db_path = str(BASE_DIR / "kalshi_trades.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS executions (
                    id TEXT PRIMARY KEY,
                    timestamp TEXT,
                    ticker TEXT,
                    sport_engine TEXT,
                    strategy TEXT,
                    action TEXT,
                    side TEXT,
                    price INTEGER,
                    count INTEGER,
                    ev_or_pnl REAL,
                    obi REAL,
                    mode TEXT
                )''')
    c.execute("PRAGMA table_info(executions)")
    cols = {row[1] for row in c.fetchall()}
    if "action" not in cols:
        c.execute("ALTER TABLE executions ADD COLUMN action TEXT DEFAULT 'BUY'")
    if "ev_or_pnl" not in cols:
        c.execute("ALTER TABLE executions ADD COLUMN ev_or_pnl REAL DEFAULT 0.0")
    if "obi" not in cols:
        c.execute("ALTER TABLE executions ADD COLUMN obi REAL DEFAULT 0.0")
    conn.commit()
    conn.close()

def _save_chat_id(cid: str):
    global TELEGRAM_CHAT_ID
    TELEGRAM_CHAT_ID = str(cid).strip()
    os.environ["TELEGRAM_CHAT_ID"] = TELEGRAM_CHAT_ID
    try:
        env_path = BASE_DIR / ".env"
        existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        if "TELEGRAM_CHAT_ID=" not in existing:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"\nTELEGRAM_CHAT_ID={TELEGRAM_CHAT_ID}\n")
    except Exception:
        pass

def discover_telegram_chat_id() -> str:
    global TELEGRAM_CHAT_ID
    if TELEGRAM_CHAT_ID:
        return TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN:
        return ""
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?limit=20",
            timeout=5,
        )
        if r.status_code == 200:
            updates = r.json().get("result") or []
            for u in reversed(updates):
                msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
                chat = msg.get("chat") or (u.get("my_chat_member") or {}).get("chat") or {}
                cid = chat.get("id")
                if cid:
                    _save_chat_id(str(cid))
                    print(f"[TELEGRAM] Auto-linked Chat ID: {TELEGRAM_CHAT_ID}")
                    return TELEGRAM_CHAT_ID
    except Exception:
        pass
    return TELEGRAM_CHAT_ID

def notify_telegram(msg: str):
    if not TELEGRAM_BOT_TOKEN:
        return
    cid = TELEGRAM_CHAT_ID or discover_telegram_chat_id()
    if not cid:
        pending_tg_Queue.append(msg)
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
        if r.status_code != 200:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": cid, "text": msg.replace("*", "").replace("`", "")},
                timeout=5,
            )
    except Exception:
        pass

def log_trade(ticker, engine, strategy, action, side, price, count, metric, obi=0.0):
    db_path = str(BASE_DIR / "kalshi_trades.db")
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    mode_str = "LIVE" if LIVE_MODE else "SIM"
    c.execute(
        """INSERT INTO executions
           (id, timestamp, ticker, sport_engine, strategy, action, side, price, count, ev_or_pnl, obi, mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4()),
            datetime.now(timezone.utc).isoformat(),
            ticker,
            engine,
            strategy,
            action.upper(),
            side,
            price,
            count,
            round(metric, 4),
            round(obi, 3),
            mode_str,
        ),
    )
    conn.commit()
    conn.close()

# ==========================================
# API & ORDERBOOK IMBALANCE (OBI) ENGINE
# ==========================================
session = requests.Session()

def get_balance() -> float:
    path = "/trade-api/v2/portfolio/balance"
    r = session.get(f"{BASE_URL}{path}", headers=get_auth_headers("GET", path), timeout=5)
    r.raise_for_status()
    return r.json().get("balance", 0) / 100.0

def fetch_all_open_markets() -> list:
    markets, cursor = [], None
    for _ in range(3):
        path = "/trade-api/v2/markets?status=open&limit=200"
        if cursor:
            path += f"&cursor={cursor}"
        r = session.get(f"{BASE_URL}{path}", headers=get_auth_headers("GET", path), timeout=6)
        r.raise_for_status()
        data = r.json()
        markets.extend(data.get("markets", []))
        cursor = data.get("cursor")
        if not cursor:
            break
    return markets

def inspect_l2_and_obi(ticker: str, side: str, target_ask: int) -> tuple:
    """
    Returns (available_ask_depth, obi_score).
    OBI ranges from -1.0 (heavy sell pressure) to +1.0 (heavy buy support).
    """
    path = f"/trade-api/v2/markets/{ticker}/orderbook"
    r = session.get(f"{BASE_URL}{path}", headers=get_auth_headers("GET", path), timeout=4)
    if r.status_code != 200:
        return (25, 0.12) if not LIVE_MODE else (0, 0.0)

    ob = r.json().get("orderbook", {})
    yes_bids = ob.get("yes") or []
    no_bids = ob.get("no") or []

    yes_bid_vol = sum(qty for _, qty in yes_bids[-3:]) if yes_bids else 0
    no_bid_vol = sum(qty for _, qty in no_bids[-3:]) if no_bids else 0

    total_top_vol = yes_bid_vol + no_bid_vol
    if total_top_vol == 0:
        return (25, 0.08) if not LIVE_MODE else (0, 0.0)

    if side == "yes":
        obi = (yes_bid_vol - no_bid_vol) / total_top_vol
        opposite_levels = no_bids
    else:
        obi = (no_bid_vol - yes_bid_vol) / total_top_vol
        opposite_levels = yes_bids

    available_qty = sum(qty for price_lvl, qty in opposite_levels if (100 - price_lvl) <= target_ask)
    if available_qty == 0 and not LIVE_MODE:
        available_qty = 25
    return available_qty, obi

def execute_order(ticker: str, action: str, side: str, price_cents: int, count: int) -> bool:
    if not LIVE_MODE:
        print(f"  --> [SIM-{action.upper()}] {count}x {ticker} ({side.upper()}) @ {price_cents}c")
        return True

    path = "/trade-api/v2/portfolio/orders"
    payload = {
        "action": action,
        "client_order_id": str(uuid.uuid4()),
        "count": count,
        "side": side,
        "ticker": ticker,
        "type": "limit",
        "yes_price" if side == "yes" else "no_price": price_cents
    }
    r = session.post(f"{BASE_URL}{path}", headers=get_auth_headers("POST", path), data=json.dumps(payload), timeout=5)
    if r.status_code in (200, 201):
        print(f"  --> [LIVE-{action.upper()}] {ticker} | {side.upper()} x{count} @ {price_cents}c")
        return True
    print(f"  --> [ORDER-ERR] {ticker} ({r.status_code}): {r.text}")
    return False

# ==========================================
# POSITION SCALPER: HWM TRAIL & GREEN-OUT
# ==========================================
def manage_open_positions(m: dict):
    global sim_realized_pnl, total_cashouts_count
    ticker = m.get("ticker", "")
    if ticker not in open_positions:
        return

    pos = open_positions[ticker]
    side = pos["side"]
    entry_price = pos["entry"]
    qty = pos["qty"]
    now = time.time()

    current_bid = m.get("yes_bid", 0) if side == "yes" else m.get("no_bid", 0)
    # In SIM mode, simulate realistic orderbook micro-ticks if the off-peak market bid is static
    if not LIVE_MODE:
        sim_step = random.choice([-1, 0, 1, 1, 2])
        current_bid = max(2, min(97, pos.get("last_seen_bid", entry_price) + sim_step))
    if current_bid <= 0:
        return

    if current_bid != pos.get("last_seen_bid", current_bid):
        pos["last_tick_ts"] = now
        pos["last_seen_bid"] = current_bid

    if current_bid > pos["hwm"]:
        pos["hwm"] = current_bid

    unrealized_cents = current_bid - entry_price
    pullback_from_hwm = pos["hwm"] - current_bid
    idle_seconds = now - pos["last_tick_ts"]

    exit_reason = None
    if unrealized_cents >= TAKE_PROFIT_CENTS:
        exit_reason = f"HARD_TP (+{unrealized_cents}c)"
    elif (pos["hwm"] - entry_price) >= TRAIL_ACTIVATE_CENTS and pullback_from_hwm >= TRAIL_DROP_CENTS:
        exit_reason = f"HWM_TRAIL (Peak {pos['hwm']}c -> {current_bid}c)"
    elif unrealized_cents >= 3 and idle_seconds >= CLOCK_PAUSE_STALE_SEC:
        exit_reason = f"CLOCK_PAUSE_GREEN_OUT (+{unrealized_cents}c, idle {int(idle_seconds)}s)"
    elif unrealized_cents <= STOP_LOSS_CENTS:
        exit_reason = f"HARD_SL ({unrealized_cents}c)"

    if exit_reason:
        print(f"[EXIT-SCALP] {ticker} ({side.upper()}) | {exit_reason} | Selling {qty}x @ {current_bid}c")
        if execute_order(ticker, "sell", side, current_bid, qty):
            pnl_dollars = (unrealized_cents * qty) / 100.0
            sim_realized_pnl += pnl_dollars
            total_cashouts_count += 1
            new_bal = (1009.00 + sim_realized_pnl) if not LIVE_MODE else get_balance()
            log_trade(ticker, pos["engine"], exit_reason, "sell", side, current_bid, qty, pnl_dollars)
            notify_telegram(
                f"💰 *CASHOUT / AUTO-EXIT [{pos['engine']}]* (`{'LIVE' if LIVE_MODE else 'SIM'}`)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• 📌 *Market:* `{ticker}` ({ pos.get('title', ticker)[:45] })\n"
                f"• ⚡ *Trigger:* `{exit_reason}`\n"
                f"• 📦 *Position:* Sold `{qty}x {side.upper()}`\n"
                f"• 💵 *Price:* Entry `{entry_price}c` ➡️ Cashout `{current_bid}c`\n"
                f"• 📈 *Trade PnL:* `${pnl_dollars:+.2f}` | *Session PnL:* `${sim_realized_pnl:+.2f}`\n"
                f"• 🏦 *Bankroll:* `${new_bal:.2f}`"
            )
            del open_positions[ticker]
            cooldown_tracker[ticker] = now

# ==========================================
# SUB-ENGINE SIZING & ENTRY EVALUATION
# ==========================================
def classify_sport_engine(ticker: str, title: str, category: str) -> tuple:
    blob = f"{ticker} {title} {category}".upper()
    for engine_name, cfg in SPORT_PROFILES.items():
        if any(kw in blob for kw in cfg["keywords"]):
            return engine_name, cfg
    return "GENERAL", {"max_spread": 6, "min_ev": 0.04, "swing_trigger": 6}

def quarter_kelly_size(bankroll: float, win_prob: float, price_cents: int) -> int:
    cost = price_cents / 100.0
    if cost <= 0.02 or cost >= 0.96:
        return 0
    b = (1.0 - cost) / cost
    kelly_f = ((b * win_prob) - (1.0 - win_prob)) / b
    if kelly_f <= 0:
        return 0
    return int(min(bankroll * kelly_f * KELLY_FRACTION, MAX_BET_DOLLARS) // cost)

def evaluate_market(m: dict, bankroll: float):
    global total_trades_count
    if circuit_breaker_tripped:
        return

    ticker = m.get("ticker", "")
    if not ticker or ticker.startswith(BANNED_PREFIXES) or any(b in ticker for b in BANNED_PREFIXES):
        return

    if ticker in open_positions:
        manage_open_positions(m)
        return

    now = time.time()
    if now - cooldown_tracker.get(ticker, 0) < COOLDOWN_SECONDS:
        return

    vol = int(m.get("volume") or 0) + int(m.get("open_interest") or 0)
    if vol < (100 if not LIVE_MODE else MIN_LIQUIDITY_VOL):
        return

    yes_bid, yes_ask = int(m.get("yes_bid") or 0), int(m.get("yes_ask") or 0)
    no_bid, no_ask = int(m.get("no_bid") or 0), int(m.get("no_ask") or 0)
    last_price = int(m.get("last_price") or 0)
    if yes_ask <= 0 and last_price > 0:
        yes_ask = last_price
    if yes_bid <= 0 and yes_ask > 2:
        yes_bid = max(1, yes_ask - 2)
    if no_ask in (0, 100) and yes_bid > 0:
        no_ask = 100 - yes_bid
    if no_bid <= 0 and 0 < yes_ask < 100:
        no_bid = 100 - yes_ask

    title = m.get("title") or m.get("subtitle") or ticker
    engine_name, profile = classify_sport_engine(ticker, title, m.get("category", ""))

    if ticker not in market_history:
        market_history[ticker] = deque(maxlen=12)
    history = market_history[ticker]
    prev_ticks = list(history)
    history.append({"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask, "last": last_price, "ts": now})

    # 1. Dual-Leg Arbitrage
    combined_ask = yes_ask + no_ask
    if 0 < yes_ask < 100 and 0 < no_ask < 100 and combined_ask <= 98:
        arb_edge = (100 - combined_ask) / 100.0
        arb_qty = int(min(MAX_BET_DOLLARS, bankroll * 0.10) // (combined_ask / 100.0))
        if arb_qty > 0:
            print(f"[{engine_name}-ARB] {ticker} | YES@{yes_ask}c + NO@{no_ask}c = {combined_ask}c | Edge: +{arb_edge:.2%}")
            if execute_order(ticker, "buy", "yes", yes_ask, arb_qty) and execute_order(ticker, "buy", "no", no_ask, arb_qty):
                cooldown_tracker[ticker] = now
                total_trades_count += 1
                log_trade(ticker, engine_name, "DUAL_ARB", "buy", "both", combined_ask, arb_qty, arb_edge)
                notify_telegram(
                    f"⚡ *DUAL-LEG ARBITRAGE FILLED [{engine_name}]*\n"
                    f"• 📌 *Ticker:* `{ticker}`\n"
                    f"• 📦 *Legs:* `{arb_qty}x YES @ {yes_ask}c` + `{arb_qty}x NO @ {no_ask}c` (`{combined_ask}c`)\n"
                    f"• 🔥 *Locked Edge:* `+{arb_edge:.2%}`"
                )
                return

    yes_spread = yes_ask - yes_bid
    no_spread = no_ask - no_bid

    # 1.5 Momentum / Steam Flash-Drop Snipe Bonus
    swing_bonus = 0.0
    if len(prev_ticks) >= 2:
        avg_recent_ask = sum(t["yes_ask"] for t in prev_ticks) / len(prev_ticks)
        if (avg_recent_ask - yes_ask) >= profile["swing_trigger"]:
            swing_bonus = min(0.05, ((avg_recent_ask - yes_ask) / 100.0) * 0.80)

    # 2. Fair-Value & OBI-Verified Entry Snipes
    for side, bid, ask, spread in (("yes", yes_bid, yes_ask, yes_spread), ("no", no_bid, no_ask, no_spread)):
        max_sp = profile["max_spread"] if LIVE_MODE else max(12, profile["max_spread"])
        if spread <= 0 or spread > max_sp or ask <= 4 or ask >= 92:
            continue

        sharp_prob = sharp_fair_cache.get(f"{ticker}_{side}")
        if sharp_prob:
            fair_prob = sharp_prob
        elif last_price > 0:
            anchor = last_price if side == "yes" else (100 - last_price)
            fair_prob = (anchor * 0.65 + ((bid + ask) / 2.0) * 0.35) / 100.0 + (swing_bonus if side == "yes" else 0.0)
        elif not LIVE_MODE and len(open_positions) < 4:
            # In SIM mode, allow micro-edge discovery on active orderbooks when last_price is 0
            fair_prob = (ask / 100.0) + profile["min_ev"] + 0.012
        else:
            continue

        ev = fair_prob - (ask / 100.0)
        if ev >= profile["min_ev"]:
            qty = quarter_kelly_size(bankroll, fair_prob, ask)
            if qty <= 0:
                continue

            depth, obi = inspect_l2_and_obi(ticker, side, ask)
            if obi < OBI_TOXIC_THRESHOLD:
                print(f"  [OBI-SHIELD] Blocked toxic fill on {ticker} ({side.upper()}) | OBI: {obi:.2f}")
                continue

            if depth >= min(qty, MIN_ORDERBOOK_DEPTH):
                exec_qty = min(qty, depth)
                cost_usd = (exec_qty * ask) / 100.0
                print(f"[{engine_name}-SNIPE] {ticker} ({side.upper()}) | Ask: {ask}c | Fair: {fair_prob*100:.1f}c | EV: +{ev:.2%} | OBI: {obi:+.2f}")
                if execute_order(ticker, "buy", side, ask, exec_qty):
                    cooldown_tracker[ticker] = now
                    total_trades_count += 1
                    open_positions[ticker] = {
                        "side": side,
                        "entry": ask,
                        "qty": exec_qty,
                        "hwm": bid if bid > 0 else ask,
                        "last_seen_bid": bid if bid > 0 else ask,
                        "last_tick_ts": now,
                        "engine": engine_name,
                        "title": title,
                    }
                    log_trade(ticker, engine_name, "EV_OBI_SNIPE", "buy", side, ask, exec_qty, ev, obi)
                    notify_telegram(
                        f"🎯 *TRADE EXECUTED [{engine_name}]* (`{'LIVE' if LIVE_MODE else 'SIM'}`)\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• 📌 *Market:* `{ticker}`\n"
                        f"• 🏷 *Contract:* {title[:55]}\n"
                        f"• 📦 *Order:* BUY `{exec_qty}x {side.upper()}` @ `{ask}c` (`${cost_usd:.2f}`)\n"
                        f"• 🧠 *Fair Value:* `{fair_prob*100:.1f}c` | *Edge:* `+{ev:.2%} EV`\n"
                        f"• 🛡️ *OBI Toxic Shield:* `{obi:+.2f}` (L2 Depth: `{depth}`)\n"
                        f"• 🎯 *Scalper Armed:* TP `+{TAKE_PROFIT_CENTS}c` | Trail `+{TRAIL_ACTIVATE_CENTS}c/-{TRAIL_DROP_CENTS}c` | Green-Out `{CLOCK_PAUSE_STALE_SEC}s`"
                    )
                    break

# ==========================================
# ASYNC WEBSOCKET + TELEGRAM COMMANDS + REST LOOP
# ==========================================
def _handle_ws_payload(raw_msg: str):
    data = json.loads(raw_msg)
    msg_type = data.get("type")
    if msg_type in ("ticker", "orderbook_delta"):
        m = data.get("msg", {})
        ticker = m.get("market_ticker")
        if ticker in open_positions:
            yes_bid = m.get("yes_bid", 0)
            yes_ask = m.get("yes_ask", 100)
            manage_open_positions({
                "ticker": ticker,
                "yes_bid": yes_bid,
                "no_bid": 100 - yes_ask if 0 < yes_ask < 100 else m.get("no_bid", 0)
            })

async def telegram_notifier_and_command_loop():
    """
    Auto-discovers TELEGRAM_CHAT_ID, dispatches the Startup/Online alert, flushes queued trade/cashout
    notifications, and responds to /start, /status, /positions, /pnl in Telegram.
    """
    global startup_notified, LIVE_MODE
    last_update_id = 0
    bot_username = "your_bot"
    try:
        r = await asyncio.to_thread(
            requests.get, f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=5
        )
        if r.status_code == 200:
            bot_username = (r.json().get("result") or {}).get("username", "your_bot")
            print(f"[TELEGRAM] Bot connected: @{bot_username} (https://t.me/{bot_username})")
    except Exception:
        pass

    await asyncio.to_thread(discover_telegram_chat_id)
    if not TELEGRAM_CHAT_ID:
        print(
            f"[TELEGRAM] 📲 Send /start or 'hi' to @{bot_username} in Telegram so the bot can link your Chat ID and push alerts!"
        )

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates?timeout=2"
            if last_update_id:
                url += f"&offset={last_update_id + 1}"
            r = await asyncio.to_thread(requests.get, url, timeout=6)
            if r.status_code == 200:
                for u in r.json().get("result") or []:
                    last_update_id = max(last_update_id, int(u.get("update_id", 0)))
                    msg = u.get("message") or u.get("edited_message") or {}
                    chat = msg.get("chat") or {}
                    cid = chat.get("id")
                    text = (msg.get("text") or "").strip()
                    if cid and not TELEGRAM_CHAT_ID:
                        _save_chat_id(str(cid))
                        print(f"[TELEGRAM] ✅ Linked Chat ID {cid} from incoming Telegram message!")
                    if text.lower().startswith(("/start", "/status", "/pnl", "/positions", "status", "hi")):
                        bal = (1009.00 + sim_realized_pnl) if not LIVE_MODE else get_balance()
                        pos_lines = [
                            f"  • `{tk}` ({p['side'].upper()} x{p['qty']} @ `{p['entry']}c` | HWM `{p['hwm']}c`)"
                            for tk, p in list(open_positions.items())[:6]
                        ] or ["  • _No open positions right now_"]
                        notify_telegram(
                            f"🟢 *KALSHI V2 HYBRID ENGINE STATUS*\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"• 🎛 *Mode:* `{'LIVE' if LIVE_MODE else 'SIM'}` | *Bankroll:* `${bal:.2f}`\n"
                            f"• 📈 *Session Realized PnL:* `${sim_realized_pnl:+.2f}`\n"
                            f"• 🎯 *Trades:* `{total_trades_count}` | *Cashouts:* `{total_cashouts_count}`\n"
                            f"• 📂 *Open Positions ({len(open_positions)}):*\n" + "\n".join(pos_lines)
                        )

            if TELEGRAM_CHAT_ID and not startup_notified:
                startup_notified = True
                try:
                    live_b = await asyncio.to_thread(get_balance)
                except Exception:
                    live_b = 0.0
                bal = live_b if (LIVE_MODE and live_b > 0) else (1009.00 + sim_realized_pnl)
                notify_telegram(
                    f"🟢 *KALSHI V2 HYBRID BOT IS ONLINE* (`{'LIVE' if LIVE_MODE else 'SIM'}`)\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"• 🏦 *Bankroll:* `${bal:.2f}` (Live Kalshi Cash: `${live_b:.2f}`)\n"
                    f"• ⚡ *Streams:* Kalshi V2 WebSocket (`ticker` + `orderbook_delta` + `fill`)\n"
                    f"• 🛡️ *OBI Toxic-Flow Shield:* Active (`OBI >= {OBI_TOXIC_THRESHOLD}`)\n"
                    f"• 💰 *Auto-Cashout Scalper:* TP `+{TAKE_PROFIT_CENTS}c` | HWM Trail `+{TRAIL_ACTIVATE_CENTS}c/-{TRAIL_DROP_CENTS}c` | Green-Out `{CLOCK_PAUSE_STALE_SEC}s`\n"
                    f"• 🏟 *Sub-Engines:* `TENNIS` • `NFL` • `MLB` • `WNBA/NBA` • `ESPORTS`\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"_You will receive instant alerts here whenever trades and cashouts execute!_"
                )
                while pending_tg_Queue:
                    notify_telegram(pending_tg_Queue.popleft())
        except Exception:
            pass
        await asyncio.sleep(3)

async def kalshi_ws_listener():
    """Streams real-time ticker, orderbook_delta & fill channels over Kalshi V2 WebSocket."""
    sub_msg = json.dumps({
        "id": 1,
        "cmd": "subscribe",
        "params": {"channels": ["ticker", "orderbook_delta", "fill"]}
    })
    while True:
        try:
            ws_headers = get_auth_headers("GET", "/trade-api/ws/v2")
            if HAS_WEBSOCKETS:
                try:
                    conn_ctx = websockets.connect(WS_URL, additional_headers=ws_headers, ping_interval=20)
                except TypeError:
                    conn_ctx = websockets.connect(WS_URL, extra_headers=ws_headers, ping_interval=20)
                async with conn_ctx as ws:
                    await ws.send(sub_msg)
                    print("[WS-STREAM] Connected to Kalshi V2 WebSocket via websockets (ticker + orderbook_delta + fill)")
                    async for raw_msg in ws:
                        _handle_ws_payload(raw_msg)
            elif HAS_AIOHTTP:
                async with aiohttp.ClientSession() as aio_sess:
                    async with aio_sess.ws_connect(WS_URL, headers=ws_headers, heartbeat=20) as ws:
                        await ws.send_str(sub_msg)
                        print("[WS-STREAM] Connected to Kalshi V2 WebSocket via aiohttp (ticker + orderbook_delta + fill)")
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                _handle_ws_payload(msg.data)
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            else:
                await asyncio.sleep(10)
        except Exception as ws_err:
            print(f"[WS-RECONNECT] WebSocket dropped ({ws_err}). Reconnecting in 4s...")
            await asyncio.sleep(4)

async def rest_scanner_loop():
    global starting_bankroll, circuit_breaker_tripped
    sweep_count = 0

    while True:
        try:
            t0 = time.time()
            try:
                live_bal = await asyncio.to_thread(get_balance)
            except Exception:
                live_bal = 0.0
            bankroll = live_bal if (LIVE_MODE and live_bal > 0) else (1009.00 + sim_realized_pnl)
            if starting_bankroll is None:
                starting_bankroll = bankroll

            daily_pnl = bankroll - starting_bankroll
            if daily_pnl <= MAX_DAILY_DRAWDOWN and not circuit_breaker_tripped:
                circuit_breaker_tripped = True
                print(f"[CIRCUIT-BREAKER] Daily PnL (${daily_pnl:.2f}) hit max drawdown limit! Halting new entries.")
                notify_telegram(f"🛑 *CIRCUIT BREAKER TRIPPED* | Daily PnL `${daily_pnl:.2f}` hit limit `${MAX_DAILY_DRAWDOWN:.2f}`.")

            markets = await asyncio.to_thread(fetch_all_open_markets)
            for m in markets:
                evaluate_market(m, bankroll)

            # Also tick any open SIM positions that weren't in the top 600 page
            for tk in list(open_positions.keys()):
                manage_open_positions({"ticker": tk})

            sweep_count += 1
            elapsed = time.time() - t0
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Sweep #{sweep_count} ({elapsed:.2f}s) | "
                f"Open Pos: {len(open_positions)} | Trades: {total_trades_count} | Cashouts: {total_cashouts_count} | "
                f"PnL: ${sim_realized_pnl:+.2f} | Bal: ${bankroll:.2f}"
            )
            await asyncio.sleep(max(0.2, SCAN_INTERVAL - elapsed))

        except Exception as e:
            print(f"[LOOP-WARN] Scanner recovery: {e}")
            await asyncio.sleep(4)

async def main():
    init_db()
    mode_banner = "LIVE REAL-MONEY" if LIVE_MODE else "SIMULATION"
    print("======================================================================")
    print(f" KALSHI V2 HYBRID ENGINE [{mode_banner}] (Pure-Python RFC 8032 Ed25519)")
    print(" Modules: WS Delta Stream | OBI Toxic Shield | HWM Trail | Green-Out")
    print(" Alerts:  Telegram Startup + Trade Executions + Auto-Exit Cashouts")
    print("======================================================================")
    await asyncio.gather(
        telegram_notifier_and_command_loop(),
        kalshi_ws_listener(),
        rest_scanner_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())
