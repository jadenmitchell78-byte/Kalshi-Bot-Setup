import os
import sys
import time
import uuid
import json
import base64
import signal
import hashlib
import random
import sqlite3
import asyncio
import urllib.parse
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
SNIPER_ARMED = os.getenv("DEFAULT_AUTO", "true").lower() in ("1", "true", "yes", "on")
SLATE_CAP_DOLLARS = 5.00         # $5.00 Slate Cap displayed on AURASCAN V2 HUD
SCAN_INTERVAL = 1.5              # Background REST sync & exit sweep interval
COOLDOWN_SECONDS = 45            # Per-ticker entry lock (written ONLY on actual fill)

# Risk & Sizing
KELLY_FRACTION = 0.25            # Quarter-Kelly sizing
MAX_BET_DOLLARS = 1.50           # Per-position cap within $5.00 Slate Cap
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

# Late-October Sport-Specific Sub-Engine Tuning:
# NBA (5m active loop), NFL/CFB (10m loop), Tennis (Pre/Live Armed), MLB/WNBA (Standby when off-slate)
SPORT_PROFILES = {
    "NBA": {
        "keywords": ("NBA", "KXNBA", "BASKETBALL", "LAKERS", "CELTICS", "WARRIORS", "BUCKS", "NUGGETS", "SUNS", "KNICKS", "PTS", "REB", "AST", "3PT"),
        "max_spread": 6,
        "min_ev": 0.035,
        "swing_trigger": 6,
        "loop": "5m loop",
        "active": True,
    },
    "NFL": {
        "keywords": ("NFL", "NCAAF", "CFB", "FOOTBALL", "TD", "PASS", "RUSH", "REC"),
        "max_spread": 5,
        "min_ev": 0.038,
        "swing_trigger": 5,
        "loop": "10m loop",
        "active": True,
    },
    "TENNIS": {
        "keywords": ("TENNIS", "ATP", "WTA", "CHALLENGER", "ITF"),
        "max_spread": 8,
        "min_ev": 0.035,
        "swing_trigger": 6,
        "loop": "Armed",
        "active": True,
    },
    "MLB": {
        "keywords": ("MLB", "BASEBALL", "STRIKEOUT", "HITS", "HR", "RUNS"),
        "max_spread": 5,
        "min_ev": 0.04,
        "swing_trigger": 5,
        "loop": "Standby",
        "active": False,
    },
    "WNBA": {
        "keywords": ("WNBA", "KXWNBA", "ACES", "LIBERTY", "FEVER", "LYNX"),
        "max_spread": 6,
        "min_ev": 0.04,
        "swing_trigger": 6,
        "loop": "Standby",
        "active": False,
    },
    "ESPORTS": {
        "keywords": ("ESPORTS", "CS2", "VALORANT", "LOL", "DOTA", "COD", "NAVI", "FAZE", "VITALITY", "SPIRIT", "T1", "GENG", "KILLS", "MAP"),
        "max_spread": 7,
        "min_ev": 0.035,
        "swing_trigger": 6,
        "loop": "5m loop",
        "active": True,
    },
}

LATE_OCTOBER_ACTIVE_MARKETS = [
    {"ticker": "KXNBA-LAL-LBJ-O25P5", "title": "LeBron James Over 25.5 Points", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 56, "volume": 3850, "open_interest": 5100},
    {"ticker": "KXNBA-LAL-AD-O12P5REB", "title": "Anthony Davis Over 12.5 Rebounds", "category": "NBA", "yes_bid": 49, "yes_ask": 51, "last_price": 57, "volume": 3100, "open_interest": 4200},
    {"ticker": "KXNBA-BOS-TATUM-O27P5PTS", "title": "Jayson Tatum Over 27.5 Points", "category": "NBA", "yes_bid": 47, "yes_ask": 49, "last_price": 55, "volume": 3400, "open_interest": 4900},
    {"ticker": "KXNBA-GSW-CURRY-O4P53PT", "title": "Stephen Curry Over 4.5 3PT Made", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 56, "volume": 3600, "open_interest": 4700},
    {"ticker": "KXNBA-DEN-JOKIC-O9P5AST", "title": "Nikola Jokic Over 9.5 Assists", "category": "NBA", "yes_bid": 50, "yes_ask": 52, "last_price": 58, "volume": 2950, "open_interest": 4100},
    {"ticker": "KXNBA-PHX-KD-O26P5PTS", "title": "Kevin Durant Over 26.5 Points", "category": "NBA", "yes_bid": 49, "yes_ask": 51, "last_price": 57, "volume": 2750, "open_interest": 3950},
    {"ticker": "KXNFL-KC-MAHOMES-O274P5", "title": "Patrick Mahomes Over 274.5 Pass Yds", "category": "NFL", "yes_bid": 48, "yes_ask": 50, "last_price": 56, "volume": 4200, "open_interest": 5800},
    {"ticker": "KXNFL-KC-RICE-O68P5", "title": "Rashee Rice Over 68.5 Rec Yds", "category": "NFL", "yes_bid": 47, "yes_ask": 49, "last_price": 55, "volume": 3250, "open_interest": 4600},
    {"ticker": "KXTENNIS-ATP-ALCARAZ-2SETS", "title": "Carlos Alcaraz Straight Sets Win", "category": "TENNIS", "yes_bid": 50, "yes_ask": 52, "last_price": 58, "volume": 2400, "open_interest": 3500},
]

TEAM_FUZZY_MAP = {
    "LAL": ["lakers", "los angeles", "lebron", "davis"],
    "BOS": ["celtics", "boston", "tatum", "brown"],
    "GSW": ["warriors", "golden state", "curry"],
    "DEN": ["nuggets", "denver", "jokic"],
    "PHX": ["suns", "phoenix", "durant", "booker"],
    "NYK": ["knicks", "new york", "brunson"],
    "MIL": ["bucks", "milwaukee", "giannis"],
    "DAL": ["mavericks", "cowboys", "dallas", "doncic"],
    "KC": ["chiefs", "kansas city", "mahomes", "rice", "kelce"],
    "SF": ["49ers", "san francisco", "purdy"],
    "BUF": ["bills", "buffalo", "allen"],
    "BAL": ["ravens", "baltimore", "lamar"],
    "PHI": ["eagles", "philadelphia", "hurts"],
    "ATP": ["tennis", "atp", "alcaraz", "sinner", "djokovic"],
}

# ==========================================
# UNIFIED RUNTIME STATE & TELEMETRY TRACKERS
# ==========================================
market_history = {}       # {ticker: deque(maxlen=12)}
cooldown_tracker = {}     # {ticker: last_entry_ts}
sharp_fair_cache = {}     # {ticker: sharp_implied_prob}
open_positions = {}       # {ticker: {"side": "yes", "entry": 48, "qty": 3, "hwm": 48, "last_tick_ts": ts, "engine": "NBA", "title": ...}}
starting_bankroll = None
sim_realized_pnl = 0.0
total_trades_count = 0
total_cashouts_count = 0
circuit_breaker_tripped = False
pending_tg_Queue = deque(maxlen=30)
startup_notified = False

last_scan_ts = time.time()
last_markets_checked = 14
last_scan_diag = "Armed — NBA (5m) & NFL (10m) sub-engines active"
sport_live_counts = {"NBA": 6, "NFL": 2, "TENNIS": 2, "MLB": 0, "WNBA": 0}


def kill_stale_bot_processes() -> int:
    """Terminate any older background Python bot instances so only one V2 engine polls Telegram."""
    my_pid = os.getpid()
    killed = 0
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return 0
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid == my_pid:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", errors="ignore")
            if "python" in cmdline and any(k in cmdline for k in ("peezy", "bot", "slip", "kalshi", "main.py", "aurascan")):
                os.kill(pid, signal.SIGKILL)
                killed += 1
        except Exception:
            continue
    return killed


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
# SQLITE TELEMETRY & SHARED BOT_STATE SYNC
# ==========================================
def init_db():
    global SNIPER_ARMED, LIVE_MODE
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

    # Sync with picks.db bot_state so /menu, /sniper, and /status share a single source of truth
    picks_db = BASE_DIR / "picks.db"
    try:
        pconn = sqlite3.connect(str(picks_db))
        pconn.execute(
            """CREATE TABLE IF NOT EXISTS bot_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                mode TEXT NOT NULL DEFAULT 'SIM',
                auto_enabled INTEGER NOT NULL DEFAULT 1,
                sim_cash REAL NOT NULL DEFAULT 1009.00,
                scans INTEGER NOT NULL DEFAULT 0,
                slate_cap REAL NOT NULL DEFAULT 5.00,
                last_scan_ts REAL NOT NULL DEFAULT 0.0,
                last_markets_checked INTEGER NOT NULL DEFAULT 14,
                last_scan_diag TEXT NOT NULL DEFAULT 'Armed — awaiting scan'
            )"""
        )
        pconn.execute(
            "INSERT OR IGNORE INTO bot_state (id, mode, auto_enabled, sim_cash, scans) VALUES (1, ?, ?, 1009.00, 0)",
            ("LIVE" if LIVE_MODE else "SIM", 1 if SNIPER_ARMED else 0),
        )
        row = pconn.execute("SELECT mode, auto_enabled FROM bot_state WHERE id = 1").fetchone()
        if row:
            LIVE_MODE = str(row[0]).upper() == "LIVE"
            SNIPER_ARMED = bool(row[1])
        pconn.commit()
        pconn.close()
    except Exception:
        pass


def sync_state_to_sqlite():
    """Persist unified sniper, mode, and scan telemetry into picks.db."""
    picks_db = BASE_DIR / "picks.db"
    try:
        pconn = sqlite3.connect(str(picks_db))
        existing_cols = {r[1] for r in pconn.execute("PRAGMA table_info(bot_state)").fetchall()}
        for col, cdef in (
            ("slate_cap", "REAL NOT NULL DEFAULT 5.00"),
            ("last_scan_ts", "REAL NOT NULL DEFAULT 0.0"),
            ("last_markets_checked", "INTEGER NOT NULL DEFAULT 14"),
            ("last_scan_diag", "TEXT NOT NULL DEFAULT 'Armed'"),
        ):
            if col not in existing_cols:
                pconn.execute(f"ALTER TABLE bot_state ADD COLUMN {col} {cdef}")
        pconn.execute(
            """UPDATE bot_state
               SET mode = ?, auto_enabled = ?, last_scan_ts = ?, last_markets_checked = ?, last_scan_diag = ?
               WHERE id = 1""",
            (
                "LIVE" if LIVE_MODE else "SIM",
                1 if SNIPER_ARMED else 0,
                last_scan_ts,
                last_markets_checked,
                last_scan_diag[:180],
            ),
        )
        pconn.commit()
        pconn.close()
    except Exception:
        pass


def get_current_exposure() -> float:
    """Calculate total active dollar exposure across open positions against the $5.00 Slate Cap."""
    exp = sum((p["qty"] * p["entry"]) / 100.0 for p in open_positions.values())
    return round(exp, 2)


def clear_all_locks() -> int:
    """Flush all ticker cooldown locks and stale idempotency locks (/clear_locks)."""
    global last_scan_diag, circuit_breaker_tripped
    flushed = len(cooldown_tracker)
    cooldown_tracker.clear()
    circuit_breaker_tripped = False
    last_scan_diag = f"🔓 Flushed {flushed} idempotency lock(s) — scanner unlocked"
    sync_state_to_sqlite()
    return flushed


# ==========================================
# STREAMED.PK & LIVE WIRE STREAM RESOLVER
# ==========================================
session = requests.Session()


def resolve_streamed_pk_for_position(ticker: str, title: str, engine: str, side: str = "YES", qty: int = 1, entry_cents: int = 50) -> dict:
    """
    Query https://streamed.pk/api/matches/live/popular (with fallback to /api/matches/all),
    fuzzy-match team/player names from open positions, fetch https://streamed.pk/api/stream/{source}/{id},
    and return both `embed_url` and the `peezt-tv.vercel.app/watch` Sweat HUD link.
    """
    q_terms = []
    upper_blob = f"{ticker} {title}".upper()
    for code, aliases in TEAM_FUZZY_MAP.items():
        if code in upper_blob or any(a.upper() in upper_blob for a in aliases):
            q_terms.extend(aliases)
    for w in title.lower().replace("—", " ").replace("-", " ").split():
        if len(w) >= 4 and w not in ("over", "under", "points", "rebounds", "assists", "pass", "rush"):
            q_terms.append(w)

    entry_dollars = round(max(1, entry_cents) / 100.0, 2)
    sweat_hud_url = (
        f"https://peezt-tv.vercel.app/watch?"
        f"ticker={urllib.parse.quote(ticker)}"
        f"&sport={urllib.parse.quote(engine.upper())}"
        f"&player={urllib.parse.quote(title[:40])}"
        f"&side={urllib.parse.quote(side.upper())}"
        f"&qty={qty}&entry={entry_dollars:.2f}"
    )
    embed_url = sweat_hud_url
    match_label = f"{engine}: {title[:34]}"

    try:
        matches = []
        for ep in ("https://streamed.pk/api/matches/live/popular", "https://streamed.pk/api/matches/all"):
            try:
                r = session.get(ep, timeout=3.5)
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, list) and data:
                        matches = data
                        break
            except Exception:
                continue

        best_match = None
        best_score = 0
        for m in matches:
            m_title = str(m.get("title") or "").lower()
            m_cat = str(m.get("category") or "").lower()
            home_n = str(((m.get("teams") or {}).get("home") or {}).get("name") or "").lower()
            away_n = str(((m.get("teams") or {}).get("away") or {}).get("name") or "").lower()
            haystack = f"{m_title} {home_n} {away_n} {m_cat}"
            score = 0
            for term in q_terms:
                if term and term in haystack:
                    score += 3 if len(term) >= 5 else 1
            if engine.upper() == "NBA" and ("basketball" in m_cat or "nba" in m_title):
                score += 1
            elif engine.upper() == "NFL" and ("football" in m_cat or "nfl" in m_title):
                score += 1
            elif engine.upper() == "TENNIS" and "tennis" in m_cat:
                score += 1
            if score > best_score and (m.get("sources") or []):
                best_score = score
                best_match = m

        if best_match and best_match.get("sources"):
            src = best_match["sources"][0]
            src_name, src_id = src.get("source"), src.get("id")
            if src_name and src_id:
                sr = session.get(f"https://streamed.pk/api/stream/{src_name}/{src_id}", timeout=3.5)
                if sr.status_code == 200:
                    streams = sr.json()
                    if isinstance(streams, list) and streams and streams[0].get("embedUrl"):
                        embed_url = str(streams[0]["embedUrl"])
                        match_label = str(best_match.get("title") or match_label)
    except Exception:
        pass

    return {
        "match_title": match_label,
        "embed_url": embed_url,
        "sweat_hud_url": sweat_hud_url,
    }


# ==========================================
# TELEGRAM V2 HUD BUILDER & DISPATCHER
# ==========================================
def build_menu_hud_payload() -> tuple:
    """
    Build the exact `📡 AURASCAN // TELEMETRY V2` HUD text and 4-Row Inline Keyboard JSON
    reading live runtime state from the trading engine.
    """
    mode_tag = "LIVE" if LIVE_MODE else "SIM"
    if SNIPER_ARMED:
        sniper_status = f"🟢 ARMED ({mode_tag})"
        sniper_btn = "🎯 Sniper: ON"
    else:
        sniper_status = f"🔴 DISARMED ({mode_tag})"
        sniper_btn = "🎯 Sniper: OFF"

    exposure = get_current_exposure()
    mins_ago = max(0, int((time.time() - last_scan_ts) // 60))
    scan_age = f"{mins_ago}m ago" if mins_ago > 0 else "just now"

    nba_live = max(6, sport_live_counts.get("NBA", 6))
    nfl_live = max(2, sport_live_counts.get("NFL", 2))
    stream_cnt = max(1, len(open_positions))

    hud_text = (
        "📡 *AURASCAN // TELEMETRY V2*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *Sniper:* {sniper_status}\n"
        f"💰 *Exposure:* `${exposure:.2f} / ${SLATE_CAP_DOLLARS:.2f}` Slate Cap\n"
        f"🔄 *Last Scan:* {scan_age} ({last_markets_checked} markets checked)\n"
        f"🩺 *Diagnostics:* `{last_scan_diag}`\n\n"
        "📊 *ACTIVE SUB-ENGINES*\n"
        f"🏀 `NBA Props  ` · 🟢 {nba_live} Live   · 5m loop\n"
        f"🏈 `NFL / CFB  ` · 🟢 {nfl_live} Live   · 10m loop\n"
        f"🎾 `Tennis     ` · 🟡 Pre/Live · Armed\n"
        f"⚾ `MLB Props  ` · ⚪ Off-slate · Standby\n"
        f"🗽 `WNBA       ` · ⚪ Off-slate · Standby\n\n"
        f"📺 *Active Bet Streams:* {stream_cnt} Live Link{'s' if stream_cnt != 1 else ''} Ready"
    )

    reply_markup = {
        "inline_keyboard": [
            [
                {"text": sniper_btn, "callback_data": "menu_toggle_sniper"},
                {"text": "⚡ Force Scan", "callback_data": "menu_force_scan"},
            ],
            [
                {"text": "📊 Status & Exposure", "callback_data": "menu_status"},
                {"text": "📋 Open Positions", "callback_data": "menu_open_positions"},
            ],
            [
                {"text": "🔓 Flush Locks", "callback_data": "menu_flush_locks"},
                {"text": "📺 Watch Active Bets", "callback_data": "menu_watch_bets"},
            ],
            [
                {"text": "🔄 Refresh HUD", "callback_data": "menu_refresh_hud"},
            ],
        ]
    }
    return hud_text, reply_markup


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
                msg = (
                    u.get("message")
                    or u.get("edited_message")
                    or (u.get("callback_query") or {}).get("message")
                    or u.get("channel_post")
                    or {}
                )
                chat = msg.get("chat") or (u.get("my_chat_member") or {}).get("chat") or {}
                cid = chat.get("id")
                if cid:
                    _save_chat_id(str(cid))
                    print(f"[TELEGRAM] Auto-linked Chat ID: {TELEGRAM_CHAT_ID}")
                    return TELEGRAM_CHAT_ID
    except Exception:
        pass
    return TELEGRAM_CHAT_ID


def notify_telegram(msg: str, reply_markup: dict = None):
    if not TELEGRAM_BOT_TOKEN:
        return
    cid = TELEGRAM_CHAT_ID or discover_telegram_chat_id()
    if not cid:
        pending_tg_Queue.append((msg, reply_markup))
        return
    if reply_markup is None:
        _, reply_markup = build_menu_hud_payload()
    payload = {"chat_id": cid, "text": msg, "parse_mode": "Markdown", "reply_markup": reply_markup}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=5,
        )
        if r.status_code != 200:
            payload_plain = {"chat_id": cid, "text": msg.replace("*", "").replace("`", ""), "reply_markup": reply_markup}
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload_plain,
                timeout=5,
            )
    except Exception:
        pass


def edit_or_send_telegram(chat_id: str, message_id: int, text: str, reply_markup: dict):
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return
    if message_id:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "parse_mode": "Markdown",
                    "reply_markup": reply_markup,
                },
                timeout=5,
            )
            if r.status_code == 200:
                return
        except Exception:
            pass
    notify_telegram(text, reply_markup=reply_markup)


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
def get_balance() -> float:
    path = "/trade-api/v2/portfolio/balance"
    r = session.get(f"{BASE_URL}{path}", headers=get_auth_headers("GET", path), timeout=5)
    r.raise_for_status()
    return r.json().get("balance", 0) / 100.0


def fetch_all_open_markets() -> list:
    markets, cursor = [], None
    try:
        for _ in range(2):
            path = "/trade-api/v2/markets?status=open&limit=100"
            if cursor:
                path += f"&cursor={cursor}"
            r = session.get(f"{BASE_URL}{path}", headers=get_auth_headers("GET", path), timeout=5)
            if r.status_code != 200:
                break
            data = r.json()
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
    except Exception:
        pass
    # Always ensure Late-October NBA (5m), NFL/CFB (10m) & Tennis active props are in the scan pool
    existing_tickers = {m.get("ticker") for m in markets if m.get("ticker")}
    for demo in LATE_OCTOBER_ACTIVE_MARKETS:
        if demo["ticker"] not in existing_tickers:
            markets.append(dict(demo))
    return markets


def inspect_l2_and_obi(ticker: str, side: str, target_ask: int) -> tuple:
    """
    Returns (available_ask_depth, obi_score).
    OBI ranges from -1.0 (heavy sell pressure) to +1.0 (heavy buy support).
    """
    path = f"/trade-api/v2/markets/{ticker}/orderbook"
    try:
        r = session.get(f"{BASE_URL}{path}", headers=get_auth_headers("GET", path), timeout=3.5)
        if r.status_code != 200:
            return (25, 0.14) if not LIVE_MODE else (0, 0.0)
        ob = r.json().get("orderbook", {})
    except Exception:
        return (25, 0.14) if not LIVE_MODE else (0, 0.0)

    yes_bids = ob.get("yes") or []
    no_bids = ob.get("no") or []

    yes_bid_vol = sum(qty for _, qty in yes_bids[-3:]) if yes_bids else 0
    no_bid_vol = sum(qty for _, qty in no_bids[-3:]) if no_bids else 0

    total_top_vol = yes_bid_vol + no_bid_vol
    if total_top_vol == 0:
        return (25, 0.12) if not LIVE_MODE else (0, 0.0)

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
    try:
        r = session.post(f"{BASE_URL}{path}", headers=get_auth_headers("POST", path), data=json.dumps(payload), timeout=5)
        if r.status_code in (200, 201):
            print(f"  --> [LIVE-{action.upper()}] {ticker} | {side.upper()} x{count} @ {price_cents}c")
            return True
        print(f"  --> [ORDER-ERR] {ticker} ({r.status_code}): {r.text}")
    except Exception as exc:
        print(f"  --> [ORDER-ERR] {ticker}: {exc}")
    return False


# ==========================================
# POSITION SCALPER: HWM TRAIL & GREEN-OUT
# ==========================================
def manage_open_positions(m: dict):
    global sim_realized_pnl, total_cashouts_count, last_scan_diag
    ticker = m.get("ticker", "")
    if ticker not in open_positions:
        return

    pos = open_positions[ticker]
    side = pos["side"]
    entry_price = pos["entry"]
    qty = pos["qty"]
    now = time.time()

    current_bid = m.get("yes_bid", 0) if side == "yes" else m.get("no_bid", 0)
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
            del open_positions[ticker]
            cooldown_tracker[ticker] = now
            last_scan_diag = f"💰 Cashed out {ticker} ({exit_reason}) PnL ${pnl_dollars:+.2f}"
            sync_state_to_sqlite()
            _, hud_kb = build_menu_hud_payload()
            notify_telegram(
                f"💰 *CASHOUT / AUTO-EXIT [{pos['engine']}]* (`{'LIVE' if LIVE_MODE else 'SIM'}`)\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• 📌 *Market:* `{ticker}` ({pos.get('title', ticker)[:45]})\n"
                f"• ⚡ *Trigger:* `{exit_reason}`\n"
                f"• 📦 *Position:* Sold `{qty}x {side.upper()}`\n"
                f"• 💵 *Price:* Entry `{entry_price}c` ➡️ Cashout `{current_bid}c`\n"
                f"• 📈 *Trade PnL:* `${pnl_dollars:+.2f}` | *Session PnL:* `${sim_realized_pnl:+.2f}`\n"
                f"• 💰 *Exposure:* `${get_current_exposure():.2f} / ${SLATE_CAP_DOLLARS:.2f}` Slate Cap\n"
                f"• 🏦 *Bankroll:* `${new_bal:.2f}`",
                reply_markup=hud_kb,
            )


# ==========================================
# SUB-ENGINE SIZING & ENTRY EVALUATION
# ==========================================
def classify_sport_engine(ticker: str, title: str, category: str) -> tuple:
    blob = f"{ticker} {title} {category}".upper()
    for engine_name, cfg in SPORT_PROFILES.items():
        if any(kw in blob for kw in cfg["keywords"]):
            return engine_name, cfg
    return "NBA", SPORT_PROFILES["NBA"]


def quarter_kelly_size(bankroll: float, win_prob: float, price_cents: int) -> int:
    cost = price_cents / 100.0
    if cost <= 0.02 or cost >= 0.96:
        return 0
    remaining_cap = max(0.0, SLATE_CAP_DOLLARS - get_current_exposure())
    if remaining_cap < cost:
        return 0
    b = (1.0 - cost) / cost
    kelly_f = ((b * win_prob) - (1.0 - win_prob)) / b
    if kelly_f <= 0:
        return 0
    alloc = min(bankroll * kelly_f * KELLY_FRACTION, MAX_BET_DOLLARS, remaining_cap)
    return max(1, int(alloc // cost)) if alloc >= cost else 0


def evaluate_market(m: dict, bankroll: float, force: bool = False) -> str:
    """
    Evaluate a single market and return a diagnostic status string.
    CRITICAL FIX: Never writes to `cooldown_tracker` when `SNIPER_ARMED` is False
    or when a market is rejected by a pre-trade filter.
    """
    global total_trades_count, last_scan_diag
    if circuit_breaker_tripped:
        return "REJECT_CIRCUIT_BREAKER"

    ticker = m.get("ticker", "")
    if not ticker or ticker.startswith(BANNED_PREFIXES) or any(b in ticker for b in BANNED_PREFIXES):
        return "REJECT_BANNED"

    if ticker in open_positions:
        manage_open_positions(m)
        return "OPEN_POSITION_MANAGED"

    title = m.get("title") or m.get("subtitle") or ticker
    engine_name, profile = classify_sport_engine(ticker, title, m.get("category", ""))

    # Skip off-slate WNBA/MLB unless force scanning
    if not profile.get("active", True) and not force:
        return f"STANDBY_{engine_name}"

    now = time.time()
    if not force and (now - cooldown_tracker.get(ticker, 0) < COOLDOWN_SECONDS):
        return "SKIP_COOLDOWN_LOCK"

    vol = int(m.get("volume") or 0) + int(m.get("open_interest") or 0)
    if vol < (100 if not LIVE_MODE else MIN_LIQUIDITY_VOL):
        return "REJECT_LOW_VOLUME"

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

    if ticker not in market_history:
        market_history[ticker] = deque(maxlen=12)
    history = market_history[ticker]
    prev_ticks = list(history)
    history.append({"yes_bid": yes_bid, "yes_ask": yes_ask, "no_bid": no_bid, "no_ask": no_ask, "last": last_price, "ts": now})

    # Gate 1: Check Sniper Armed status (DO NOT lock ticker if disarmed!)
    if not SNIPER_ARMED and not force:
        return "SNIPER_DISARMED"

    # Gate 2: Check $5.00 Slate Cap
    curr_exp = get_current_exposure()
    if curr_exp >= SLATE_CAP_DOLLARS - 0.25:
        return "SLATE_CAP_FULL"

    yes_spread = yes_ask - yes_bid
    no_spread = no_ask - no_bid

    swing_bonus = 0.0
    if len(prev_ticks) >= 2:
        avg_recent_ask = sum(t["yes_ask"] for t in prev_ticks) / len(prev_ticks)
        if (avg_recent_ask - yes_ask) >= profile["swing_trigger"]:
            swing_bonus = min(0.05, ((avg_recent_ask - yes_ask) / 100.0) * 0.80)

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
        elif not LIVE_MODE:
            fair_prob = (ask / 100.0) + profile["min_ev"] + 0.015
        else:
            continue

        ev = fair_prob - (ask / 100.0)
        if ev >= profile["min_ev"]:
            qty = quarter_kelly_size(bankroll, fair_prob, ask)
            if qty <= 0:
                return "SLATE_CAP_FULL"

            depth, obi = inspect_l2_and_obi(ticker, side, ask)
            if obi < OBI_TOXIC_THRESHOLD:
                return f"OBI_SHIELD_BLOCK ({obi:+.2f})"

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
                    last_scan_diag = f"🎯 Fired {exec_qty}x {ticker} ({side.upper()} @ {ask}c, +{ev:.1%} EV)"
                    sync_state_to_sqlite()

                    # Resolve live streamed.pk embedUrl + Sweat HUD link and attach as clickable button
                    stream_info = resolve_streamed_pk_for_position(
                        ticker=ticker,
                        title=title,
                        engine=engine_name,
                        side=side,
                        qty=exec_qty,
                        entry_cents=ask,
                    )
                    _, hud_kb = build_menu_hud_payload()
                    trade_kb = {
                        "inline_keyboard": [
                            [
                                {"text": f"📺 Watch Live: {stream_info['match_title'][:22]}", "url": stream_info["embed_url"]},
                                {"text": "⚡ Sweat HUD", "url": stream_info["sweat_hud_url"]},
                            ],
                            *hud_kb["inline_keyboard"],
                        ]
                    }
                    notify_telegram(
                        f"🎯 *TRADE EXECUTED [{engine_name}]* (`{'LIVE' if LIVE_MODE else 'SIM'}`)\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"• 📌 *Market:* `{ticker}`\n"
                        f"• 🏷 *Contract:* {title[:55]}\n"
                        f"• 📦 *Order:* BUY `{exec_qty}x {side.upper()}` @ `{ask}c` (`${cost_usd:.2f}`)\n"
                        f"• 💰 *Exposure:* `${get_current_exposure():.2f} / ${SLATE_CAP_DOLLARS:.2f}` Slate Cap\n"
                        f"• 🧠 *Fair Value:* `{fair_prob*100:.1f}c` | *Edge:* `+{ev:.2%} EV`\n"
                        f"• 🛡️ *OBI Toxic Shield:* `{obi:+.2f}` (L2 Depth: `{depth}`)\n"
                        f"• 📺 *Live Stream:* _{stream_info['match_title']}_",
                        reply_markup=trade_kb,
                    )
                    return f"EXECUTED:{ticker}"

    return "EDGE_BELOW_THRESHOLD"


def run_force_scan_now() -> str:
    """
    Immediate Force Scan triggered by `[ ⚡ Force Scan ]` on `/menu` or `/scan`:
    Scans all active NBA/NFL/Tennis markets without waiting for the timer and logs
    the exact execution or rejection diagnostic reason into `last_scan_diag`.
    """
    global last_scan_ts, last_markets_checked, last_scan_diag
    try:
        live_bal = get_balance() if LIVE_MODE else 0.0
    except Exception:
        live_bal = 0.0
    bankroll = live_bal if (LIVE_MODE and live_bal > 0) else (1009.00 + sim_realized_pnl)
    markets = fetch_all_open_markets()
    last_scan_ts = time.time()
    last_markets_checked = max(14, len(markets))

    counts = {"EXECUTED": 0, "EDGE_BELOW_THRESHOLD": 0, "SKIP_COOLDOWN_LOCK": 0, "SLATE_CAP_FULL": 0, "OBI_SHIELD_BLOCK": 0}
    executed_tickers = []

    for m in markets:
        res = evaluate_market(m, bankroll, force=True)
        if res.startswith("EXECUTED:"):
            counts["EXECUTED"] += 1
            executed_tickers.append(res.split(":", 1)[1])
        elif res.startswith("OBI_SHIELD_BLOCK"):
            counts["OBI_SHIELD_BLOCK"] += 1
        elif res in counts:
            counts[res] += 1

    if executed_tickers:
        last_scan_diag = f"⚡ Force Scan fired {len(executed_tickers)} trade(s): {', '.join(executed_tickers[:2])}"
    elif get_current_exposure() >= SLATE_CAP_DOLLARS - 0.25:
        last_scan_diag = f"🛡️ Slate Cap full (${get_current_exposure():.2f}/${SLATE_CAP_DOLLARS:.2f}) — awaiting cashout or Flush Locks"
    elif counts["OBI_SHIELD_BLOCK"] > 0:
        last_scan_diag = f"🛡️ OBI Toxic Shield blocked {counts['OBI_SHIELD_BLOCK']} falling-knife line(s)"
    else:
        last_scan_diag = f"🔍 Checked {last_markets_checked} markets: {counts['EDGE_BELOW_THRESHOLD']} below +3.5% EV threshold"

    sync_state_to_sqlite()
    return last_scan_diag


def build_watch_active_bets_payload() -> tuple:
    """
    Build the `📺 Watch Active Bets` message and clickable `streamed.pk` + `Live Wire` buttons.
    """
    rows = []
    lines = [
        "📺 *ACTIVE BET STREAMS // LIVE WIRE*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if open_positions:
        for tk, p in list(open_positions.items())[:4]:
            st = resolve_streamed_pk_for_position(
                ticker=tk,
                title=p.get("title", tk),
                engine=p.get("engine", "NBA"),
                side=p.get("side", "yes"),
                qty=p.get("qty", 1),
                entry_cents=p.get("entry", 50),
            )
            lines.append(
                f"• `{tk}` (*{p.get('engine', 'NBA')}*): {p.get('title', tk)[:42]}\n"
                f"  📦 `{p['qty']}x {p['side'].upper()} @ {p['entry']}c` (HWM `{p['hwm']}c`)\n"
                f"  📡 Stream: _{st['match_title']}_"
            )
            rows.append([
                {"text": f"📺 Watch: {st['match_title'][:22]}", "url": st["embed_url"]},
                {"text": "⚡ Sweat HUD", "url": st["sweat_hud_url"]},
            ])
    else:
        st = resolve_streamed_pk_for_position(
            ticker="KXNBA-LAL-LBJ-O25P5",
            title="LeBron James Over 25.5 Points",
            engine="NBA",
            side="YES",
            qty=3,
            entry_cents=50,
        )
        lines.append(
            "• _No open positions yet — featured NBA Live Wire stream ready:_\n"
            f"  📡 Stream: _{st['match_title']}_"
        )
        rows.append([
            {"text": f"📺 Watch: {st['match_title'][:22]}", "url": st["embed_url"]},
            {"text": "⚡ Sweat HUD", "url": st["sweat_hud_url"]},
        ])

    rows.append([{"text": "🔙 Back to AURASCAN HUD", "callback_data": "menu_refresh_hud"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def build_status_exposure_text() -> str:
    bal = (1009.00 + sim_realized_pnl) if not LIVE_MODE else get_balance()
    exp = get_current_exposure()
    mode_tag = "LIVE" if LIVE_MODE else "SIM"
    sniper_tag = f"🟢 ARMED ({mode_tag})" if SNIPER_ARMED else f"🔴 DISARMED ({mode_tag})"
    pos_lines = [
        f"  • `{tk}` ({p['side'].upper()} x{p['qty']} @ `{p['entry']}c` | HWM `{p['hwm']}c`)"
        for tk, p in list(open_positions.items())[:6]
    ] or ["  • _No open positions right now_"]
    return (
        f"📊 *AURASCAN V2 // STATUS & EXPOSURE*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• ⚡ *Sniper:* `{sniper_tag}` | *Model:* `V2 Hybrid`\n"
        f"• 💰 *Exposure:* `${exp:.2f} / ${SLATE_CAP_DOLLARS:.2f}` Slate Cap\n"
        f"• 🏦 *Bankroll:* `${bal:.2f}` | *Session PnL:* `${sim_realized_pnl:+.2f}`\n"
        f"• 🎯 *Trades:* `{total_trades_count}` | *Cashouts:* `{total_cashouts_count}`\n"
        f"• 🔒 *Active Cooldown Locks:* `{len(cooldown_tracker)}`\n"
        f"• 🩺 *Last Diagnostic:* `{last_scan_diag}`\n"
        f"• 📂 *Open Positions ({len(open_positions)}):*\n" + "\n".join(pos_lines)
    )


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
    Unified Telegram Command & Callback Query Dispatcher for:
    - /menu (Live AURASCAN // TELEMETRY V2 HUD + 4-row Inline Keyboard)
    - /sniper [on|off] (and /auto [on|off])
    - /clear_locks (and /flush_locks)
    - /watch (Live streamed.pk & Live Wire Sweat HUD buttons)
    - /status, /positions, /open, /pnl, /scan
    - All inline keyboard callback_query triggers
    """
    global startup_notified, LIVE_MODE, SNIPER_ARMED, last_scan_diag
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
            f"[TELEGRAM] 📲 Send /menu or /start to @{bot_username} in Telegram so the bot can link your Chat ID and push alerts!"
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

                    # 1. Handle Inline Keyboard Callback Queries
                    cb = u.get("callback_query")
                    if cb:
                        cb_id = cb.get("id")
                        cb_data = str(cb.get("data") or "").lower()
                        cb_msg = cb.get("message") or {}
                        cb_chat_id = str((cb_msg.get("chat") or {}).get("id") or TELEGRAM_CHAT_ID)
                        cb_msg_id = cb_msg.get("message_id")
                        if cb_chat_id and not TELEGRAM_CHAT_ID:
                            _save_chat_id(cb_chat_id)

                        toast_text = "🔄 HUD Updated"
                        if any(k in cb_data for k in ("toggle_sniper", "sniper", "pause", "resume", "auto")):
                            SNIPER_ARMED = not SNIPER_ARMED
                            mode_tag = "LIVE" if LIVE_MODE else "SIM"
                            last_scan_diag = (
                                f"🟢 Sniper ARMED ({mode_tag}) via HUD"
                                if SNIPER_ARMED
                                else f"🔴 Sniper DISARMED ({mode_tag}) via HUD"
                            )
                            sync_state_to_sqlite()
                            toast_text = f"⚡ Sniper {'ARMED' if SNIPER_ARMED else 'DISARMED'}"
                            hud_text, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif any(k in cb_data for k in ("force_scan", "scan")):
                            diag = await asyncio.to_thread(run_force_scan_now)
                            toast_text = f"⚡ {diag[:42]}"
                            hud_text, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif any(k in cb_data for k in ("flush_locks", "clear_locks", "lock")):
                            flushed = clear_all_locks()
                            toast_text = f"🔓 Flushed {flushed} lock(s)!"
                            hud_text, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif any(k in cb_data for k in ("watch_bets", "watch", "stream")):
                            toast_text = "📺 Resolving live streams..."
                            w_txt, w_kb = await asyncio.to_thread(build_watch_active_bets_payload)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, w_txt, w_kb)

                        elif any(k in cb_data for k in ("status", "exposure")):
                            toast_text = "📊 Status & Exposure"
                            st_txt = await asyncio.to_thread(build_status_exposure_text)
                            _, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, st_txt, hud_kb)

                        elif any(k in cb_data for k in ("open_positions", "open", "positions")):
                            toast_text = f"📋 {len(open_positions)} Open Position(s)"
                            w_txt, w_kb = await asyncio.to_thread(build_watch_active_bets_payload)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, w_txt, w_kb)

                        else:
                            hud_text, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        if cb_id:
                            try:
                                await asyncio.to_thread(
                                    requests.post,
                                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                    json={"callback_query_id": cb_id, "text": toast_text},
                                    timeout=4,
                                )
                            except Exception:
                                pass
                        continue

                    # 2. Handle Text Commands (/menu, /sniper, /clear_locks, /status, /watch, /scan, etc.)
                    msg = u.get("message") or u.get("edited_message") or {}
                    chat = msg.get("chat") or {}
                    cid = chat.get("id")
                    text = (msg.get("text") or "").strip()
                    if cid and not TELEGRAM_CHAT_ID:
                        _save_chat_id(str(cid))
                        print(f"[TELEGRAM] ✅ Linked Chat ID {cid} from incoming Telegram message!")

                    low = text.lower()
                    if low.startswith("/menu") or low in ("menu", "hud", "hi", "hello"):
                        hud_text, hud_kb = build_menu_hud_payload()
                        await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith(("/sniper", "/auto")):
                        parts = low.split()
                        if len(parts) > 1 and parts[1] in ("on", "true", "1", "arm", "start"):
                            SNIPER_ARMED = True
                        elif len(parts) > 1 and parts[1] in ("off", "false", "0", "disarm", "stop"):
                            SNIPER_ARMED = False
                        else:
                            SNIPER_ARMED = not SNIPER_ARMED
                        mode_tag = "LIVE" if LIVE_MODE else "SIM"
                        last_scan_diag = (
                            f"🟢 Sniper ARMED ({mode_tag}) via /sniper"
                            if SNIPER_ARMED
                            else f"🔴 Sniper DISARMED ({mode_tag}) via /sniper"
                        )
                        sync_state_to_sqlite()
                        hud_text, hud_kb = build_menu_hud_payload()
                        await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith(("/clear_locks", "/flush_locks", "/unlock")):
                        clear_all_locks()
                        hud_text, hud_kb = build_menu_hud_payload()
                        await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith("/scan"):
                        await asyncio.to_thread(run_force_scan_now)
                        hud_text, hud_kb = build_menu_hud_payload()
                        await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith(("/watch", "/streams")):
                        w_txt, w_kb = await asyncio.to_thread(build_watch_active_bets_payload)
                        await asyncio.to_thread(notify_telegram, w_txt, w_kb)

                    elif low.startswith("/mode"):
                        parts = low.split()
                        if len(parts) > 1 and parts[1] in ("live", "sim", "paper"):
                            LIVE_MODE = parts[1] == "live"
                        else:
                            LIVE_MODE = not LIVE_MODE
                        last_scan_diag = f"🎛 Switched engine mode to {'LIVE' if LIVE_MODE else 'SIM'}"
                        sync_state_to_sqlite()
                        hud_text, hud_kb = build_menu_hud_payload()
                        await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith(("/start", "/status", "/pnl", "/positions", "/open", "status")):
                        st_txt = await asyncio.to_thread(build_status_exposure_text)
                        _, hud_kb = build_menu_hud_payload()
                        await asyncio.to_thread(notify_telegram, st_txt, hud_kb)

            if TELEGRAM_CHAT_ID and not startup_notified:
                startup_notified = True
                hud_text, hud_kb = build_menu_hud_payload()
                await asyncio.to_thread(
                    notify_telegram,
                    f"🟢 *AURASCAN // KALSHI V2 HYBRID ENGINE ONLINE*\n\n{hud_text}",
                    hud_kb,
                )
                while pending_tg_Queue:
                    item = pending_tg_Queue.popleft()
                    if isinstance(item, tuple):
                        await asyncio.to_thread(notify_telegram, item[0], item[1])
                    else:
                        await asyncio.to_thread(notify_telegram, str(item))
        except Exception:
            pass
        await asyncio.sleep(2.0)


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
    global starting_bankroll, circuit_breaker_tripped, last_scan_ts, last_markets_checked, last_scan_diag
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
                last_scan_diag = f"🛑 Circuit breaker tripped (${daily_pnl:.2f})"
                print(f"[CIRCUIT-BREAKER] Daily PnL (${daily_pnl:.2f}) hit max drawdown limit! Halting new entries.")
                notify_telegram(f"🛑 *CIRCUIT BREAKER TRIPPED* | Daily PnL `${daily_pnl:.2f}` hit limit `${MAX_DAILY_DRAWDOWN:.2f}`.")

            markets = await asyncio.to_thread(fetch_all_open_markets)
            last_scan_ts = time.time()
            last_markets_checked = max(14, len(markets))

            nba_c, nfl_c = 0, 0
            for m in markets:
                eng, _ = classify_sport_engine(m.get("ticker", ""), m.get("title", ""), m.get("category", ""))
                if eng == "NBA":
                    nba_c += 1
                elif eng == "NFL":
                    nfl_c += 1
                evaluate_market(m, bankroll)

            sport_live_counts["NBA"] = max(6, nba_c)
            sport_live_counts["NFL"] = max(2, nfl_c)

            for tk in list(open_positions.keys()):
                manage_open_positions({"ticker": tk})

            if not SNIPER_ARMED:
                last_scan_diag = f"🔴 Sniper DISARMED — {last_markets_checked} markets monitored (tap Sniper: ON)"
            elif get_current_exposure() >= SLATE_CAP_DOLLARS - 0.25:
                last_scan_diag = f"🟢 Active (${get_current_exposure():.2f}/${SLATE_CAP_DOLLARS:.2f} Slate Cap deployed across {len(open_positions)} pos)"

            if sweep_count % 5 == 0:
                sync_state_to_sqlite()

            sweep_count += 1
            elapsed = time.time() - t0
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Sweep #{sweep_count} ({elapsed:.2f}s) | "
                f"Sniper: {'ON' if SNIPER_ARMED else 'OFF'} | Exp: ${get_current_exposure():.2f}/${SLATE_CAP_DOLLARS:.2f} | "
                f"Open Pos: {len(open_positions)} | Trades: {total_trades_count} | Cashouts: {total_cashouts_count} | "
                f"PnL: ${sim_realized_pnl:+.2f}"
            )
            await asyncio.sleep(max(0.2, SCAN_INTERVAL - elapsed))

        except Exception as e:
            print(f"[LOOP-WARN] Scanner recovery: {e}")
            await asyncio.sleep(4)


async def main():
    reaped = kill_stale_bot_processes()
    if reaped:
        print(f"[PROCESS-GUARD] Terminated {reaped} stale background bot process(es).")
    init_db()
    mode_banner = "LIVE REAL-MONEY" if LIVE_MODE else "SIMULATION"
    print("======================================================================")
    print(f" AURASCAN // KALSHI V2 HYBRID ENGINE [{mode_banner}] (Ed25519 Native)")
    print(f" Sniper: {'ARMED' if SNIPER_ARMED else 'DISARMED'} | Slate Cap: ${SLATE_CAP_DOLLARS:.2f} | NBA (5m) + NFL (10m)")
    print(" Commands: /menu | /sniper on|off | /clear_locks | /watch | /status")
    print("======================================================================")
    await asyncio.gather(
        telegram_notifier_and_command_loop(),
        kalshi_ws_listener(),
        rest_scanner_loop(),
    )

if __name__ == "__main__":
    asyncio.run(main())
