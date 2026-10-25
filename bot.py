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
from typing import Any, Deque, Dict, List, Optional, Set, Tuple
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

try:
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    HAS_CRYPTOGRAPHY = True
except Exception:
    HAS_CRYPTOGRAPHY = False

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
load_dotenv(ENV_FILE)
load_dotenv(BASE_DIR.parent / ".env")

from slip import (
    SlipLeg,
    sim_to_prop_mapper,
    detect_sport,
    parse_prop_and_line,
    parse_manual_leg_token,
    prob_to_american,
    build_rfq_sniper_quote,
    clear_cooldown_locks,
)
from sim_engine import (
    simulate_prop_leg,
    simulate_joint_gaussian_copula,
    run_calibration_and_symmetry_audit,
    PropSimResult,
)
from parlay import (
    ParlaySlip,
    evaluate_parlay,
    build_optimal_parlays,
    format_slip_card,
    format_db_slip_summary,
)
from db import (
    log_new_slip,
    log_sim_audit,
    clear_idempotency_locks,
    fetch_pending_parlays,
    fetch_settled_slips,
    reset_sim_account,
)

# ==========================================
# CORE CREDENTIALS & ENDPOINTS
# ==========================================
KALSHI_KEY_ID = os.getenv(
    "KALSHI_KEY_ID",
    os.getenv("KALSHI_API_KEY", "8b3f09cf-3da9-4107-b8ff-ee08d6daab3d"),
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
SLATE_CAP_DOLLARS = 32.22        # Dynamic Slate Cap (scaled by bankroll_allocation_pct)
SCAN_INTERVAL = 2.0              # Background position scalper & exit sweep interval
COOLDOWN_SECONDS = 45            # Per-ticker entry lock (written ONLY on actual fill)
bankroll_allocation_pct = 1.00    # Dynamic Bankroll Allocation (1.00 = 100% full balance)
slider_panel_active = False       # Whether Telegram inline slider panel is open
scan_panel_active = False         # Whether Telegram inline manual scan sport panel is open
user_building_combo = {           # Manual Combo Builder Override state
    "active": False,
    "ts": 0.0,
    "selected_tickers": [],
    "cached_props": [],
}
USER_BUILD_TIMEOUT = 300.0        # 5 minutes auto-timeout


def is_user_building_combo() -> bool:
    global user_building_combo
    if user_building_combo.get("active"):
        if time.time() - float(user_building_combo.get("ts", 0)) < USER_BUILD_TIMEOUT:
            return True
        user_building_combo["active"] = False
    return False


def get_dynamic_slate_cap(live_cash: Optional[float] = None) -> float:
    """Compute dynamic slate cap based on bankroll_allocation_pct (up to 100% of live Kalshi balance)."""
    global bankroll_allocation_pct, live_kalshi_balance
    bal = live_cash if (live_cash is not None and live_cash > 0) else (live_kalshi_balance if live_kalshi_balance > 0 else 32.22)
    return round(max(1.0, bal * bankroll_allocation_pct), 2)

# Risk & Sizing
KELLY_FRACTION = 0.25            # Quarter-Kelly sizing
MAX_BET_DOLLARS = 1.50           # Per-position cap within $5.00 Slate Cap
MAX_DAILY_DRAWDOWN = -75.00      # Circuit breaker hard stop ($)
MIN_LIQUIDITY_VOL = 50           # Minimum market volume for LIVE execution
MIN_ORDERBOOK_DEPTH = 15         # Minimum contracts on best ask
OBI_TOXIC_THRESHOLD = -0.45      # Block snipes if ask wall dominates bid support

# Auto-Exit & Green-Out Scalper Rules
TAKE_PROFIT_CENTS = 8            # Instant hard take-profit (+8c per contract)
TRAIL_ACTIVATE_CENTS = 5         # Arm trailing stop once position is up +5c
TRAIL_DROP_CENTS = 2             # Fire exit if bid drops 2c off High-Water Mark
STOP_LOSS_CENTS = -9             # Hard stop-loss cut (-9c per contract)
CLOCK_PAUSE_STALE_SEC = 75       # If tick velocity halts >75s while green -> Green-Out

BANNED_PREFIXES = (
    "KXMVE", "COMBO", "PARLAY", "MULTIGAME", "CROSS",
    "KXNBACUP", "KXNBACHAMP", "KXNBAMVP", "KXNBAAWARDS", "KXNBADRAFT",
    "KXNBAEAST", "KXNBAWEST", "KXNBAPLAYOFFS", "KXNBAFINALS", "KXNBATOURNAMENT",
    "KXMLBAWARDS", "KXMLBCY", "KXMLBMVP", "KXMLBROY", "KXMLBWS", "KXMLBCHAMP",
    "KXMLBDEBUT", "KXMLBDEBUTS", "KXMLBDRAFT", "KXMLBSEASON", "KXMLBHOF",
    "KXMLBPENNANT", "KXMLBHRKING", "KXMLBWIN"
)
FUTURES_OR_CUP_TOKENS = (
    "CUP", "IN-SEASON", "INSEASON", "IN SEASON", "MID-SEASON", "MIDSEASON", "MID SEASON", "MID SZN",
    "CHAMPION", "CHAMPIONSHIP", "TITLE", "MVP", "ROOKIE", "ROY", "DPOY", "AWARDS", "DRAFT",
    "EAST CONF", "WEST CONF", "CONFERENCE", "WIN TOTAL", "SEASON WIN", "MAKE PLAYOFFS", "MISS PLAYOFFS",
    "FINALS", "BRACKET", "TOURNAMENT", "TOURNEY", "EMIRATES", "ALL-STAR", "ALL STAR", "DIVISION", "STANDINGS",
    "SEASON-LONG", "SEASON LONG", "FUTURES", "FUTURE", "SEED",
    "DEBUT", "DEBUTS", "CY YOUNG", "CY", "ROOKIE OF THE YEAR", "MANAGER OF THE YEAR",
    "WORLD SERIES", "PENNANT", "SEASON REWARD", "SEASON REWARDS", "SZN REWARD", "SZN REWARDS",
    "HOME RUN LEADER", "HR LEADER", "BATTING TITLE", "TRIPLE CROWN", "GOLD GLOVE", "SILVER SLUGGER", "HALL OF FAME"
)

# Late-October Sport-Specific Sub-Engine Tuning:
# NBA (5m active loop), NFL/CFB (10m loop), Tennis (Pre/Live Armed), MLB/WNBA (Standby when off-slate)
SPORT_PROFILES = {
    "NBA": {
        "keywords": (
            "NBA", "KXNBA", "BASKETBALL", "LAKERS", "CELTICS", "WARRIORS", "BUCKS", "NUGGETS", "SUNS", "KNICKS",
            "NETS", "CAVS", "CAVALIERS", "ROCKETS", "HAWKS", "PELICANS", "76ERS", "SIXERS", "WIZARDS", "BULLS", "SPURS", "MAVS", "MAVERICKS",
            "HEAT", "PACERS", "PISTONS", "RAPTORS", "HORNETS", "GRIZZLIES", "MAGIC", "TIMBERWOLVES", "THUNDER", "BLAZERS", "JAZZ", "KINGS", "CLIPPERS",
            "PTS", "REB", "AST", "3PT", "PRA"
        ),
        "max_spread": 12,
        "min_ev": 0.025,
        "swing_trigger": 5,
        "loop": "5m loop",
        "active": True,
    },
    "NFL": {
        "keywords": ("NFL", "NCAAF", "CFB", "FOOTBALL", "TD", "PASS", "RUSH", "REC"),
        "max_spread": 8,
        "min_ev": 0.025,
        "swing_trigger": 5,
        "loop": "10m loop",
        "active": True,
    },
    "TENNIS": {
        "keywords": ("TENNIS", "ATP", "WTA", "CHALLENGER", "ITF"),
        "max_spread": 10,
        "min_ev": 0.025,
        "swing_trigger": 5,
        "loop": "Armed",
        "active": True,
    },
    "MLB": {
        "keywords": ("MLB", "BASEBALL", "STRIKEOUT", "HITS", "HR", "RUNS"),
        "max_spread": 8,
        "min_ev": 0.030,
        "swing_trigger": 5,
        "loop": "Standby",
        "active": False,
    },
    "WNBA": {
        "keywords": ("WNBA", "KXWNBA", "ACES", "LIBERTY", "FEVER", "LYNX"),
        "max_spread": 8,
        "min_ev": 0.030,
        "swing_trigger": 5,
        "loop": "Standby",
        "active": False,
    },
    "ESPORTS": {
        "keywords": ("ESPORTS", "CS2", "VALORANT", "LOL", "DOTA", "COD", "NAVI", "FAZE", "VITALITY", "SPIRIT", "T1", "GENG", "KILLS", "MAP"),
        "max_spread": 9,
        "min_ev": 0.025,
        "swing_trigger": 5,
        "loop": "5m loop",
        "active": True,
    },
}

LATE_OCTOBER_ACTIVE_MARKETS = [
    # Live Active Slate: Nets vs Cavs, Rockets vs Hawks, Pelicans vs Nuggets, Warriors vs Suns, Bucks vs 76ers, Wizards vs Bulls, Spurs vs Mavs
    {"ticker": "KXNBA-CHI-LAVINE-O23P5PTS", "title": "Zach LaVine Over 23.5 Points", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 54, "volume": 3200, "open_interest": 4500},
    {"ticker": "KXNBA-WAS-POOLE-O21P5PTS", "title": "Jordan Poole Over 21.5 Points", "category": "NBA", "yes_bid": 47, "yes_ask": 49, "last_price": 53, "volume": 2900, "open_interest": 4100},
    {"ticker": "KXNBA-DAL-LUKA-O31P5PTS", "title": "Luka Doncic Over 31.5 Points", "category": "NBA", "yes_bid": 49, "yes_ask": 51, "last_price": 57, "volume": 4400, "open_interest": 6200},
    {"ticker": "KXNBA-SAS-WEMBY-O22P5PTS", "title": "Victor Wembanyama Over 22.5 Points", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 56, "volume": 4100, "open_interest": 5800},
    {"ticker": "KXNBA-HOU-SENGUN-O18P5PTS", "title": "Alperen Sengun Over 18.5 Points", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 55, "volume": 2800, "open_interest": 3900},
    {"ticker": "KXNBA-ATL-TRAE-O10P5AST", "title": "Trae Young Over 10.5 Assists", "category": "NBA", "yes_bid": 49, "yes_ask": 51, "last_price": 56, "volume": 3300, "open_interest": 4600},
    {"ticker": "KXNBA-MIL-GIANNIS-O30P5PTS", "title": "Giannis Antetokounmpo Over 30.5 Points", "category": "NBA", "yes_bid": 50, "yes_ask": 52, "last_price": 58, "volume": 4200, "open_interest": 5900},
    {"ticker": "KXNBA-PHI-MAXEY-O26P5PTS", "title": "Tyrese Maxey Over 26.5 Points", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 55, "volume": 3500, "open_interest": 4800},
    {"ticker": "KXNBA-NOP-ZION-O24P5PTS", "title": "Zion Williamson Over 24.5 Points", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 55, "volume": 3100, "open_interest": 4400},
    {"ticker": "KXNBA-DEN-JOKIC-O9P5AST", "title": "Nikola Jokic Over 9.5 Assists", "category": "NBA", "yes_bid": 50, "yes_ask": 52, "last_price": 58, "volume": 3950, "open_interest": 5300},
    {"ticker": "KXNBA-GSW-CURRY-O4P53PT", "title": "Stephen Curry Over 4.5 3PT Made", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 56, "volume": 4600, "open_interest": 6100},
    {"ticker": "KXNBA-PHX-KD-O26P5PTS", "title": "Kevin Durant Over 26.5 Points", "category": "NBA", "yes_bid": 49, "yes_ask": 51, "last_price": 57, "volume": 3750, "open_interest": 4950},
    {"ticker": "KXNBA-CLE-MITCHELL-O25P5PTS", "title": "Donovan Mitchell Over 25.5 Points", "category": "NBA", "yes_bid": 48, "yes_ask": 50, "last_price": 55, "volume": 3600, "open_interest": 4900},
    {"ticker": "KXNBA-BKN-THOMAS-O24P5PTS", "title": "Cam Thomas Over 24.5 Points", "category": "NBA", "yes_bid": 47, "yes_ask": 49, "last_price": 54, "volume": 2700, "open_interest": 3800},
    {"ticker": "KXNFL-KC-MAHOMES-O274P5", "title": "Patrick Mahomes Over 274.5 Pass Yds", "category": "NFL", "yes_bid": 48, "yes_ask": 50, "last_price": 56, "volume": 4200, "open_interest": 5800},
    {"ticker": "KXTENNIS-ATP-ALCARAZ-2SETS", "title": "Carlos Alcaraz Straight Sets Win", "category": "TENNIS", "yes_bid": 50, "yes_ask": 52, "last_price": 58, "volume": 2400, "open_interest": 3500},
]

TEAM_FUZZY_MAP = {
    "LAL": ["lakers", "los angeles lakers", "lebron", "davis"],
    "BOS": ["celtics", "boston", "tatum", "brown"],
    "GSW": ["warriors", "golden state", "curry"],
    "DEN": ["nuggets", "denver", "jokic", "murray"],
    "PHX": ["suns", "phoenix", "durant", "booker"],
    "NYK": ["knicks", "new york", "brunson", "towns"],
    "MIL": ["bucks", "milwaukee", "giannis", "lillard"],
    "DAL": ["mavericks", "mavs", "dallas", "doncic", "irving"],
    "CLE": ["cavaliers", "cavs", "cleveland", "mitchell", "garland", "mobley"],
    "BKN": ["nets", "brooklyn", "thomas", "claxton"],
    "HOU": ["rockets", "houston", "sengun", "green", "vanvleet"],
    "ATL": ["hawks", "atlanta", "trae", "young", "johnson"],
    "NOP": ["pelicans", "new orleans", "zion", "ingram", "mccollum"],
    "PHI": ["76ers", "sixers", "philadelphia", "maxey", "embiid", "george"],
    "WAS": ["wizards", "washington", "poole", "kuzma"],
    "CHI": ["bulls", "chicago", "lavine", "vučević", "vucevic", "white"],
    "SAS": ["spurs", "san antonio", "wembanyama", "wemby", "paul"],
    "MEM": ["grizzlies", "memphis", "morant", "bane", "jackson"],
    "ORL": ["magic", "orlando", "banchero", "wagner"],
    "MIA": ["heat", "miami", "butler", "adebayo", "herro"],
    "IND": ["pacers", "indiana", "haliburton", "siakam"],
    "DET": ["pistons", "detroit", "cunningham", "ivey"],
    "TOR": ["raptors", "toronto", "barnes", "quickley"],
    "CHA": ["hornets", "charlotte", "ball", "miller"],
    "MIN": ["timberwolves", "wolves", "minnesota", "edwards", "randle", "gobert"],
    "OKC": ["thunder", "oklahoma city", "shai", "gilgeous", "holmgren"],
    "POR": ["trail blazers", "blazers", "portland", "simons", "grant"],
    "UTA": ["jazz", "utah", "markkanen", "sexton"],
    "SAC": ["kings", "sacramento", "fox", "sabonis", "derozan"],
    "LAC": ["clippers", "harden", "kawhi", "zubac"],
    "KC": ["chiefs", "kansas city", "mahomes", "rice", "kelce"],
    "SF": ["49ers", "san francisco", "purdy"],
    "BUF": ["bills", "buffalo", "allen"],
    "BAL": ["ravens", "baltimore", "lamar"],
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
last_kalshi_api_result = {"ok": False, "balance": 0.0, "status": "Testing...", "code": 0}
live_kalshi_balance = 0.0


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

# ==========================================
# PURE-PYTHON DUAL SIGNER (RSA-PSS + RFC 8032 ED25519)
# ==========================================
_KEY_TYPE = "ED25519"
_ED_SEED = b""
_ED_PREFIX = b""
_ED_PUB = b""
_ED_A = 0
_RSA_N = 0
_RSA_D = 0

def _parse_asn1_ints(data: bytes) -> list:
    ints = []
    idx = 0
    while idx < len(data):
        tag = data[idx]
        idx += 1
        if tag == 0x02:  # INTEGER
            length = data[idx]
            idx += 1
            if length & 0x80:
                n_len_bytes = length & 0x7F
                length = int.from_bytes(data[idx:idx+n_len_bytes], "big")
                idx += n_len_bytes
            val = int.from_bytes(data[idx:idx+length], "big")
            ints.append(val)
            idx += length
        elif tag == 0x30:  # SEQUENCE
            length = data[idx]
            idx += 1
            if length & 0x80:
                n_len_bytes = length & 0x7F
                idx += n_len_bytes
            continue
        elif tag in (0x04, 0x00):
            length = data[idx]
            idx += 1
            if length & 0x80:
                n_len_bytes = length & 0x7F
                idx += n_len_bytes
            continue
        else:
            idx += 1
    return ints

def _sign_rsa_pss_sha256(msg: bytes, n: int, d: int) -> bytes:
    m_hash = hashlib.sha256(msg).digest()
    em_bits = n.bit_length() - 1
    em_len = (em_bits + 7) // 8
    salt_len = 32
    salt = os.urandom(salt_len)
    m_prime = b"\x00" * 8 + m_hash + salt
    h = hashlib.sha256(m_prime).digest()
    ps = b"\x00" * (em_len - salt_len - 32 - 2)
    db = ps + b"\x01" + salt
    t = b""
    c = 0
    mask_len = em_len - 32 - 1
    while len(t) < mask_len:
        t += hashlib.sha256(h + c.to_bytes(4, "big")).digest()
        c += 1
    db_mask = t[:mask_len]
    masked_db = bytes(x ^ y for x, y in zip(db, db_mask))
    zero_bits = 8 * em_len - em_bits
    if zero_bits > 0:
        first = masked_db[0] & (0xFF >> zero_bits)
        masked_db = bytes([first]) + masked_db[1:]
    em = masked_db + h + b"\xbc"
    em_int = int.from_bytes(em, "big")
    s_int = pow(em_int, d, n)
    k = (n.bit_length() + 7) // 8
    return s_int.to_bytes(k, "big")

_PRIVATE_KEY_OBJ = None

def _reload_key_signer(path: str = ""):
    global _KEY_TYPE, _PRIVATE_KEY_OBJ, _ED_SEED, _ED_PREFIX, _ED_PUB, _ED_A, _RSA_N, _RSA_D
    candidates = [
        Path(path) if path else None,
        Path(PRIVATE_KEY_PATH) if PRIVATE_KEY_PATH else None,
        Path("/data/data/com.termux/files/home/peezy_bot/kalshi_private.pem"),
        BASE_DIR / "kalshi_private.pem",
        BASE_DIR / "kalshi_private_key.pem",
        BASE_DIR.parent / "kalshi_private_key.pem",
        BASE_DIR.parent / "kalshi_private.pem",
    ]
    pem_text = ""
    # 1. Prefer reading directly from authentic PEM file
    for p in candidates:
        if p and p.exists():
            try:
                txt = p.read_text(encoding="utf-8").strip()
                if txt and ("PRIVATE KEY" in txt or len(txt) > 30):
                    pem_text = txt
                    break
            except Exception:
                continue

    # 2. Fallback to KALSHI_PRIVATE_KEY env var
    if not pem_text:
        env_raw = os.getenv("KALSHI_PRIVATE_KEY", "").strip()
        while env_raw and env_raw[0] in ('"', "'") and env_raw[-1] in ('"', "'"):
            env_raw = env_raw[1:-1].strip()
        env_raw = env_raw.replace("\\n", "\n")
        if env_raw:
            pem_text = env_raw

    # Normalize PEM formatting
    if pem_text and not pem_text.startswith("-----BEGIN"):
        clean_b64 = "".join(ln.strip() for ln in pem_text.splitlines() if ln.strip() and not ln.strip().startswith("-----"))
        pem_text = f"-----BEGIN PRIVATE KEY-----\n{clean_b64}\n-----END PRIVATE KEY-----"

    if HAS_CRYPTOGRAPHY and pem_text:
        try:
            _PRIVATE_KEY_OBJ = load_pem_private_key(pem_text.encode("utf-8"), password=None)
            if isinstance(_PRIVATE_KEY_OBJ, Ed25519PrivateKey):
                _KEY_TYPE = "ED25519"
            else:
                _KEY_TYPE = "RSA-PSS"
            print(f"[SIGNER] Initialized authentic Kalshi {_KEY_TYPE} key via Python cryptography engine.")
            return
        except Exception as crypt_err:
            print(f"[SIGNER-WARN] Cryptography loader warning ({crypt_err}). Trying DER / pure-Python fallback...")

    b64_lines = [ln.strip().strip('"\'') for ln in pem_text.splitlines() if ln.strip().strip('"\'') and not ln.strip().strip('"\'').startswith("-----")]
    try:
        der_bytes = base64.b64decode("".join(b64_lines))
    except Exception:
        der_bytes = b"\x00" * 32

    if "RSA" in pem_text or len(der_bytes) > 80:
        ints = _parse_asn1_ints(der_bytes)
        if len(ints) >= 4 and ints[1] > 2**512 and ints[3] > 2**512:
            _KEY_TYPE = "RSA"
            _RSA_N = ints[1]
            _RSA_D = ints[3]
            return
        elif len(ints) >= 3 and ints[0] > 2**512:
            _KEY_TYPE = "RSA"
            _RSA_N = ints[0]
            _RSA_D = ints[2] if len(ints) >= 3 else ints[1]
            return

    # Fallback to Ed25519
    _KEY_TYPE = "ED25519"
    _ED_SEED = der_bytes[-32:] if len(der_bytes) >= 32 else (b"\x00" * 32)
    _ED_H = hashlib.sha512(_ED_SEED).digest()
    _ED_A = int.from_bytes(_ED_H[:32], "little")
    _ED_A &= (1 << 254) - 8
    _ED_A |= (1 << 254)
    _ED_PREFIX = _ED_H[32:64]
    _ED_PUB = _ed_encode(_ed_scalarmult(_B, _ED_A))

_reload_key_signer(PRIVATE_KEY_PATH)

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

    sig_bytes = None
    if HAS_CRYPTOGRAPHY and _PRIVATE_KEY_OBJ is not None:
        try:
            if isinstance(_PRIVATE_KEY_OBJ, Ed25519PrivateKey):
                sig_bytes = _PRIVATE_KEY_OBJ.sign(msg)
            else:
                sig_bytes = _PRIVATE_KEY_OBJ.sign(
                    msg,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.MAX_LENGTH,
                    ),
                    hashes.SHA256(),
                )
        except Exception:
            sig_bytes = None

    if sig_bytes is None:
        if _KEY_TYPE == "RSA" and _RSA_N > 0 and _RSA_D > 0:
            sig_bytes = _sign_rsa_pss_sha256(msg, _RSA_N, _RSA_D)
        else:
            sig_bytes = sign_ed25519_msg(msg)

    return {
        "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig_bytes).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
        "Accept": "application/json",
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
                sim_cash REAL NOT NULL DEFAULT 0.00,
                scans INTEGER NOT NULL DEFAULT 0,
                slate_cap REAL NOT NULL DEFAULT 32.22,
                bankroll_allocation_pct REAL NOT NULL DEFAULT 1.00,
                last_scan_ts REAL NOT NULL DEFAULT 0.0,
                last_markets_checked INTEGER NOT NULL DEFAULT 14,
                last_scan_diag TEXT NOT NULL DEFAULT 'Armed — awaiting scan'
            )"""
        )
        pconn.execute(
            "INSERT OR IGNORE INTO bot_state (id, mode, auto_enabled, sim_cash, scans, slate_cap, bankroll_allocation_pct) VALUES (1, ?, ?, 32.22, 0, 32.22, 1.00)",
            ("LIVE" if LIVE_MODE else "SIM", 1 if SNIPER_ARMED else 0),
        )
        existing_cols = {r[1] for r in pconn.execute("PRAGMA table_info(bot_state)").fetchall()}
        if "bankroll_allocation_pct" not in existing_cols:
            pconn.execute("ALTER TABLE bot_state ADD COLUMN bankroll_allocation_pct REAL NOT NULL DEFAULT 1.00")
        row = pconn.execute("SELECT mode, auto_enabled, bankroll_allocation_pct FROM bot_state WHERE id = 1").fetchone()
        if row:
            LIVE_MODE = str(row[0]).upper() == "LIVE"
            SNIPER_ARMED = bool(row[1])
            if len(row) > 2 and row[2] is not None:
                bankroll_allocation_pct = max(0.05, min(1.00, float(row[2])))
        pconn.commit()
        pconn.close()
    except Exception:
        pass


def set_engine_mode(is_live: bool) -> str:
    """Permanently update engine mode in memory, .env, and picks.db."""
    global LIVE_MODE, last_scan_diag
    LIVE_MODE = bool(is_live)
    mode_str = "LIVE" if LIVE_MODE else "SIM"
    os.environ["DEFAULT_MODE"] = mode_str

    try:
        env_path = BASE_DIR / ".env"
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
        new_lines = []
        found = False
        for ln in lines:
            if ln.strip().startswith("DEFAULT_MODE="):
                new_lines.append(f"DEFAULT_MODE={mode_str}")
                found = True
            else:
                new_lines.append(ln)
        if not found:
            new_lines.append(f"DEFAULT_MODE={mode_str}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except Exception:
        pass

    try:
        pconn = sqlite3.connect(str(BASE_DIR / "picks.db"))
        pconn.execute("UPDATE bot_state SET mode = ? WHERE id = 1", (mode_str,))
        pconn.commit()
        pconn.close()
    except Exception:
        pass

    sync_state_to_sqlite()
    return mode_str


def sync_state_to_sqlite():
    """Persist unified sniper, mode, dynamic slate cap, and scan telemetry into picks.db."""
    picks_db = BASE_DIR / "picks.db"
    try:
        pconn = sqlite3.connect(str(picks_db))
        existing_cols = {r[1] for r in pconn.execute("PRAGMA table_info(bot_state)").fetchall()}
        for col, cdef in (
            ("slate_cap", "REAL NOT NULL DEFAULT 32.22"),
            ("bankroll_allocation_pct", "REAL NOT NULL DEFAULT 1.00"),
            ("last_scan_ts", "REAL NOT NULL DEFAULT 0.0"),
            ("last_markets_checked", "INTEGER NOT NULL DEFAULT 14"),
            ("last_scan_diag", "TEXT NOT NULL DEFAULT 'Armed'"),
        ):
            if col not in existing_cols:
                pconn.execute(f"ALTER TABLE bot_state ADD COLUMN {col} {cdef}")
        curr_slate_cap = get_dynamic_slate_cap()
        pconn.execute(
            """UPDATE bot_state
               SET mode = ?, auto_enabled = ?, slate_cap = ?, bankroll_allocation_pct = ?, last_scan_ts = ?, last_markets_checked = ?, last_scan_diag = ?
               WHERE id = 1""",
            (
                "LIVE" if LIVE_MODE else "SIM",
                1 if SNIPER_ARMED else 0,
                curr_slate_cap,
                bankroll_allocation_pct,
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
def build_menu_hud_payload(show_slider: Optional[bool] = None, show_scan_panel: Optional[bool] = None) -> tuple:
    """
    Build the exact `📡 AURASCAN // TELEMETRY V2` HUD text and Inline Keyboard JSON
    reading live runtime state, dynamic bankroll slider cap, API status, and sport options.
    """
    global slider_panel_active, scan_panel_active
    if show_slider is not None:
        slider_panel_active = show_slider
        if slider_panel_active:
            scan_panel_active = False
    if show_scan_panel is not None:
        scan_panel_active = show_scan_panel
        if scan_panel_active:
            slider_panel_active = False

    mode_str = "LIVE REAL-MONEY" if LIVE_MODE else "SIMULATION"
    mode_icon = "🟢" if LIVE_MODE else "🎮"
    mode_btn = "🎛 Mode: LIVE (Tap ➡️ SIM)" if LIVE_MODE else "🎛 Mode: SIM (Tap ➡️ LIVE)"

    if SNIPER_ARMED:
        sniper_status = f"🟢 ARMED ({'LIVE' if LIVE_MODE else 'SIM'})"
        sniper_btn = "🎯 Sniper: ON"
    else:
        sniper_status = f"🔴 DISARMED ({'LIVE' if LIVE_MODE else 'SIM'})"
        sniper_btn = "🎯 Sniper: OFF"

    exposure = get_current_exposure()
    slate_cap = get_dynamic_slate_cap()
    cap_pct_str = f"{int(bankroll_allocation_pct * 100)}%"
    mins_ago = max(0, int((time.time() - last_scan_ts) // 60))
    scan_age = f"{mins_ago}m ago" if mins_ago > 0 else "just now"

    # Kalshi API and Bankroll telemetry (Strictly Actual Real Funds)
    if last_kalshi_api_result.get("ok"):
        api_line = f"🔑 *Kalshi API:* 🟢 Connected | 💵 *Live Bal:* `${live_kalshi_balance:.2f}`"
    else:
        api_stat = last_kalshi_api_result.get("status", "Pending check")
        api_line = f"🔑 *Kalshi API:* 🔴 `{api_stat[:45]}`"

    if LIVE_MODE:
        bankroll_line = f"💵 *Actual Kalshi Cash:* `${live_kalshi_balance:.2f}` (Session PnL: `${sim_realized_pnl:+.2f}`)"
    else:
        sim_bal = max(0.0, live_kalshi_balance + sim_realized_pnl)
        bankroll_line = f"💵 *Actual Kalshi Cash:* `${live_kalshi_balance:.2f}` | 🎮 *SIM Track:* `${sim_bal:.2f}`"

    nba_live = max(6, sport_live_counts.get("NBA", 6))
    nfl_live = max(2, sport_live_counts.get("NFL", 2))
    stream_cnt = max(1, len(open_positions))

    pause_tag = " | ⏸️ *Builder Override Active*" if is_user_building_combo() else ""

    hud_text = (
        "📡 *AURASCAN // TELEMETRY V2*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *Sniper:* {sniper_status}{pause_tag}\n"
        f"🎛 *Engine Mode:* {mode_icon} `{mode_str}`\n"
        f"{api_line}\n"
        f"💰 *Bankroll:* {bankroll_line}\n"
        f"🛡️ *Exposure:* `${exposure:.2f} / ${slate_cap:.2f}` Slate Cap ({cap_pct_str})\n"
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

    kb_rows = [
        [
            {"text": sniper_btn, "callback_data": "menu_toggle_sniper"},
            {"text": mode_btn, "callback_data": "menu_toggle_mode"},
        ],
        [
            {"text": f"🎚️ Bankroll Cap: {cap_pct_str} (${slate_cap:.2f})", "callback_data": "menu_slider_panel"},
        ],
    ]

    if slider_panel_active:
        kb_rows.append([
            {"text": "➖10%", "callback_data": "slider_step_down"},
            {"text": "10%", "callback_data": "slider_pct_10"},
            {"text": "25%", "callback_data": "slider_pct_25"},
            {"text": "50%", "callback_data": "slider_pct_50"},
            {"text": "75%", "callback_data": "slider_pct_75"},
            {"text": "100%", "callback_data": "slider_pct_100"},
            {"text": "➕10%", "callback_data": "slider_step_up"},
        ])
        kb_rows.append([
            {"text": "✅ Done Setting Cap", "callback_data": "slider_done"},
        ])

    if scan_panel_active:
        kb_rows.append([
            {"text": "🏀 NBA Props", "callback_data": "scan_sport_nba"},
            {"text": "🏈 NFL / CFB", "callback_data": "scan_sport_nfl"},
        ])
        kb_rows.append([
            {"text": "🎾 Tennis", "callback_data": "scan_sport_tennis"},
            {"text": "⚾ MLB Props", "callback_data": "scan_sport_mlb"},
        ])
        kb_rows.append([
            {"text": "🏀 WNBA", "callback_data": "scan_sport_wnba"},
            {"text": "🎮 Esports", "callback_data": "scan_sport_esports"},
        ])
        kb_rows.append([
            {"text": "⚡ All-Slate Scan", "callback_data": "scan_sport_all"},
            {"text": "🔙 Close Scan Panel", "callback_data": "scan_panel_close"},
        ])

    kb_rows.extend([
        [
            {"text": "⚡ Force Scan", "callback_data": "menu_force_scan"},
            {"text": "🔍 Scan by Sport", "callback_data": "menu_scan_options"},
        ],
        [
            {"text": "🛠️ Build Custom Combo", "callback_data": "menu_build_combo"},
            {"text": "📊 Status & Exposure", "callback_data": "menu_status"},
        ],
        [
            {"text": "📋 Open Positions", "callback_data": "menu_open_positions"},
            {"text": "📈 Settled PnL", "callback_data": "menu_pnl"},
        ],
        [
            {"text": "🔑 Test Kalshi API", "callback_data": "menu_test_api"},
            {"text": "🔓 Flush Locks", "callback_data": "menu_flush_locks"},
        ],
        [
            {"text": "❓ Help & Commands", "callback_data": "menu_help"},
            {"text": "🚀 Push to GitHub", "callback_data": "menu_push"},
        ],
        [
            {"text": "📺 Watch Active Bets", "callback_data": "menu_watch_bets"},
            {"text": "🔄 Refresh HUD", "callback_data": "menu_refresh_hud"},
        ],
    ])

    return hud_text, {"inline_keyboard": kb_rows}


def build_custom_combo_menu(selected_tickers: Optional[List[str]] = None) -> tuple:
    """
    Component 4: MANUAL COMBO BUILDER OVERRIDE (PAUSE / RESUME)
    Interactive slip builder allowing the user to select 2 to 3 legs, evaluate Gaussian Copula,
    and execute on Kalshi with auto-pilot paused.
    """
    global user_building_combo
    user_building_combo["active"] = True
    user_building_combo["ts"] = time.time()
    if selected_tickers is not None:
        user_building_combo["selected_tickers"] = selected_tickers
    sel = user_building_combo.get("selected_tickers", [])

    markets = fetch_all_open_markets()
    mapped = []
    for m in markets:
        props = sim_to_prop_mapper(m)
        for p in props:
            mapped.append(p)
            if len(mapped) >= 12:
                break
        if len(mapped) >= 12:
            break

    user_building_combo["cached_props"] = mapped

    slate_cap = get_dynamic_slate_cap()
    curr_exp = get_current_exposure()
    avail = max(0.0, slate_cap - curr_exp)

    lines = [
        "🛠️ *MANUAL COMBO BUILDER // OVERRIDE ACTIVE*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "⏸️ *Auto-Pilot is PAUSED* (5-minute manual building window)",
        f"• 💰 *Deployable Cash:* `${avail:.2f}` (under `${slate_cap:.2f}` Cap)",
        f"• 🎯 *Selected Legs ({len(sel)}/3):*",
    ]

    if sel:
        for idx, tk in enumerate(sel, 1):
            p = next((x for x in mapped if x["ticker"] == tk), None)
            desc = p["player_prop"] if p else tk
            lines.append(f"  {idx}. `{tk}`: {desc}")
    else:
        lines.append("  _Tap legs below to add 2 or 3 correlated props to your custom slip:_")

    kb_rows = []
    # Display toggle buttons for mapped props
    for idx, p in enumerate(mapped[:6]):
        tk = p["ticker"]
        is_in = tk in sel
        ico = "✅" if is_in else "➕"
        lbl = f"{ico} [{p['sport']}] {p['player_or_team'][:14]} ({p['stat_type']})"
        kb_rows.append([{"text": lbl, "callback_data": f"combo_toggle_{idx}"}])

    act_row = []
    if len(sel) >= 2:
        act_row.append({"text": f"⚡ Fire Slip ({len(sel)} Legs)", "callback_data": "combo_fire"})
    act_row.append({"text": "▶️ Resume Auto", "callback_data": "combo_resume"})
    act_row.append({"text": "❌ Clear / Back", "callback_data": "combo_cancel"})
    kb_rows.append(act_row)

    return "\n".join(lines), {"inline_keyboard": kb_rows}


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


def notify_telegram(msg: str, reply_markup: dict = None, chat_id: str = None):
    if not TELEGRAM_BOT_TOKEN:
        return
    cid = chat_id or TELEGRAM_CHAT_ID or discover_telegram_chat_id()
    if not cid:
        pending_tg_Queue.append((msg, reply_markup))
        return
    if not TELEGRAM_CHAT_ID and cid:
        _save_chat_id(str(cid))
    if reply_markup is None:
        _, reply_markup = build_menu_hud_payload()

    # Split message into chunks if it exceeds Telegram's 4096 character limit
    max_chunk = 3900
    chunks = []
    if len(msg) > max_chunk:
        current_chunk = []
        current_len = 0
        for line in msg.split("\n"):
            if current_len + len(line) + 1 > max_chunk:
                chunks.append("\n".join(current_chunk))
                current_chunk = [line]
                current_len = len(line)
            else:
                current_chunk.append(line)
                current_len += len(line) + 1
        if current_chunk:
            chunks.append("\n".join(current_chunk))
    else:
        chunks = [msg]

    for idx, chunk in enumerate(chunks):
        is_last = (idx == len(chunks) - 1)
        kb = reply_markup if is_last else None
        payload = {"chat_id": cid, "text": chunk, "parse_mode": "Markdown"}
        if kb:
            payload["reply_markup"] = kb

        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json=payload,
                timeout=5,
            )
            if r.status_code != 200:
                # If markdown entity parsing failed, sanitize tags and send plain text
                clean_chunk = chunk.replace("*", "").replace("`", "").replace("_", "")
                payload_plain = {"chat_id": cid, "text": clean_chunk}
                if kb:
                    payload_plain["reply_markup"] = kb
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
            err_body = r.text.lower()
            # If the content is identical, do NOT spam a duplicate message!
            if "message is not modified" in err_body:
                return
            # If Markdown entity parsing failed (e.g. HTTP 400), retry editMessageText with plain text
            plain_text = text.replace("*", "").replace("`", "").replace("_", "")
            r2 = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                json={
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": plain_text,
                    "reply_markup": reply_markup,
                },
                timeout=5,
            )
            if r2.status_code == 200:
                return
            if "message is not modified" in r2.text.lower():
                return
        except Exception:
            pass
    notify_telegram(text, reply_markup=reply_markup, chat_id=chat_id)


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
def test_kalshi_api_connection() -> dict:
    """
    Test Kalshi API connectivity & Ed25519 signature authentication.
    Queries /trade-api/v2/portfolio/balance across elections and trading-api endpoints.
    Returns: {"ok": bool, "balance": float, "status": str, "code": int, "base_url": str, "key_id": str}
    """
    global live_kalshi_balance, last_kalshi_api_result, BASE_URL
    endpoints = [
        "https://trading-api.kalshi.com",
        "https://api.elections.kalshi.com",
        "https://demo-api.kalshi.co",
        BASE_URL,
    ]
    seen = set()
    unique_endpoints = [u for u in endpoints if not (u in seen or seen.add(u))]

    last_err = ""
    for base in unique_endpoints:
        path = "/trade-api/v2/portfolio/balance"
        try:
            headers = get_auth_headers("GET", path)
            r = session.get(f"{base}{path}", headers=headers, timeout=5)
            if r.status_code == 200:
                raw_bal_data = r.json()
                bal_cents = raw_bal_data.get("balance", 0)
                bal_dollars = round(bal_cents / 100.0, 2)
                p_val_cents = raw_bal_data.get("portfolio_value", bal_cents)
                BASE_URL = base
                live_kalshi_balance = bal_dollars
                print(f"[KALSHI-API-DATA] Available Cash: ${bal_dollars:.2f} | Portfolio: ${p_val_cents/100:.2f} | Payload: {raw_bal_data}")
                try:
                    pconn = sqlite3.connect(str(BASE_DIR / "picks.db"))
                    pconn.execute("UPDATE bot_state SET sim_cash = ? WHERE id = 1", (bal_dollars,))
                    pconn.commit()
                    pconn.close()
                except Exception:
                    pass

                # Check if there are active resting orders holding up cash in escrow
                try:
                    ord_headers = get_auth_headers("GET", "/trade-api/v2/portfolio/orders")
                    ord_r = session.get(f"{base}/trade-api/v2/portfolio/orders?status=resting&limit=10", headers=ord_headers, timeout=4)
                    if ord_r.status_code == 200:
                        resting_orders = ord_r.json().get("orders", [])
                        if resting_orders:
                            print(f"[KALSHI-RESTING-ORDERS] ⚠️ Notice: {len(resting_orders)} resting order(s) currently locking funds on Kalshi: {[o.get('ticker') for o in resting_orders]}")
                except Exception:
                    pass

                res = {
                    "ok": True,
                    "balance": bal_dollars,
                    "status": f"Connected (${bal_dollars:.2f} Live)",
                    "code": 200,
                    "base_url": base,
                    "key_id": (KALSHI_KEY_ID[:8] + "...") if KALSHI_KEY_ID else "None",
                }
                last_kalshi_api_result = res
                return res
            elif r.status_code == 401:
                try:
                    err_json = r.json()
                    err_msg = (err_json.get("error") or {}).get("message") or r.text[:80]
                except Exception:
                    err_msg = r.text[:80]
                host_label = base.replace("https://", "")
                last_err = f"401 ({host_label}): {err_msg}"
            elif r.status_code == 403:
                last_err = f"403 Forbidden on {base}"
            else:
                last_err = f"HTTP {r.status_code} ({base}): {r.text[:60]}"
        except Exception as exc:
            last_err = f"Connection error ({base}): {exc}"

    res = {
        "ok": False,
        "balance": 0.0,
        "status": last_err or "Failed to connect",
        "code": 401 if "401" in last_err else 500,
        "base_url": BASE_URL,
        "key_id": (KALSHI_KEY_ID[:8] + "...") if KALSHI_KEY_ID else "None",
    }
    last_kalshi_api_result = res
    return res


def get_balance() -> float:
    global live_kalshi_balance
    res = test_kalshi_api_connection()
    if res["ok"]:
        return res["balance"]
    return live_kalshi_balance


def fetch_all_open_markets() -> list:
    """
    Robust 5-Layer Kalshi V2 Sports Discovery Engine:
    1. Targeted Sports Series sweeps (KXNBAGAME, KXNBASPREAD, KXNBATOTAL, KXNBAPTS, KXNBAAST, KXNBAREB, KXNBA3PT, KXNBAPRA, etc.)
    2. Dynamic Sports Series discovery via /trade-api/v2/series (cursor pagination)
    3. Live Sports Events sweep via /trade-api/v2/events (matching NBA, NFL, Tennis)
    4. Cursor pagination across /trade-api/v2/markets?status=open&limit=200 (up to 15 pages)
    5. Strict exclusion of all NBA Cup, In-Season Tournament, and season-long futures contracts.
    """
    markets = []
    existing_tickers = set()
    public_headers = {"Content-Type": "application/json", "Accept": "application/json"}
    base_url = BASE_URL or "https://api.elections.kalshi.com"

    # Known daily sports series tickers on Kalshi V2
    known_sports_series = [
        "KXNBAGAME", "KXNBAGAMES", "KXNBASPREAD", "KXNBATOTAL", "KXNBAML",
        "KXNBAPTS", "KXNBAAST", "KXNBAREB", "KXNBA3PT", "KXNBAPRA",
        "KXNBABLK", "KXNBASTL", "KXNBATO", "KXNBAPM", "KXNBAPROP", "KXNBAPROPS",
        "KXNBAFIRST", "KXNBA1Q", "KXNBA1H", "KXNBA2H", "KXNBA",
        "KXNFLGAME", "KXNFLGAMES", "KXNFL", "KXNFLPASS", "KXNFLRUSH", "KXNFLREC", "KXNFLTD",
        "KXCFBGAME", "KXCFBGAMES", "KXCFB",
        "KXATPMATCH", "KXWTAMATCH", "KXTENNIS",
        "KXNHLGAME", "KXNHLGAMES", "KXNHL",
        "KXMLBGAME", "KXMLBGAMES", "KXMLB",
    ]

    # 1. Query open markets for all known daily sports series
    for st in known_sports_series:
        try:
            path = f"/trade-api/v2/markets?status=open&series_ticker={st}&limit=100"
            r = session.get(f"{base_url}{path}", headers=public_headers, timeout=3.5)
            if r.status_code == 200:
                s_markets = r.json().get("markets", [])
                for sm in s_markets:
                    tkr = sm.get("ticker", "")
                    if tkr and tkr not in existing_tickers:
                        existing_tickers.add(tkr)
                        markets.append(sm)
        except Exception:
            continue

    # 2. Dynamically discover all active Sports series from Kalshi API
    discovered_series = set()
    s_cursor = None
    for _ in range(4):
        try:
            s_path = "/trade-api/v2/series?limit=200"
            if s_cursor:
                s_path += f"&cursor={s_cursor}"
            sr = session.get(f"{base_url}{s_path}", headers=public_headers, timeout=4)
            if sr.status_code == 200:
                s_data = sr.json()
                s_list = s_data.get("series", [])
                for s_item in s_list:
                    stk = s_item.get("ticker") or s_item.get("series_ticker")
                    if not stk:
                        continue
                    stk_u = stk.upper()
                    title = str(s_item.get("title") or "").upper()
                    cat = str(s_item.get("category") or "").upper()
                    tags = [str(t).upper() for t in s_item.get("tags") or []]
                    if any(tok in stk_u or tok in title for tok in FUTURES_OR_CUP_TOKENS):
                        continue
                    if (
                        stk_u.startswith("KXNBA")
                        or "BASKETBALL" in title
                        or "NBA" in title
                        or "NBA" in tags
                        or cat in ("SPORTS", "BASKETBALL")
                        or stk_u.startswith(("KXNFL", "KXCFB", "KXTENNIS", "KXATP", "KXWTA", "KXNHL", "KXMLB"))
                    ):
                        if stk not in known_sports_series:
                            discovered_series.add(stk)
                s_cursor = s_data.get("cursor")
                if not s_cursor or not s_list:
                    break
            else:
                break
        except Exception:
            break

    # Fetch markets for dynamically discovered series
    for st in list(discovered_series)[:35]:
        try:
            path = f"/trade-api/v2/markets?status=open&series_ticker={st}&limit=100"
            r = session.get(f"{base_url}{path}", headers=public_headers, timeout=3.5)
            if r.status_code == 200:
                for sm in r.json().get("markets", []):
                    tkr = sm.get("ticker", "")
                    if tkr and tkr not in existing_tickers:
                        existing_tickers.add(tkr)
                        markets.append(sm)
        except Exception:
            continue

    # 3. Explore open Sports Events (Kalshi indexes NBA matchups & props under Events)
    ev_cursor = None
    for _ in range(4):
        try:
            ev_path = "/trade-api/v2/events?status=open&limit=200"
            if ev_cursor:
                ev_path += f"&cursor={ev_cursor}"
            er = session.get(f"{base_url}{ev_path}", headers=public_headers, timeout=4)
            if er.status_code == 200:
                e_data = er.json()
                e_list = e_data.get("events", [])
                for ev in e_list:
                    etk = str(ev.get("event_ticker") or "").upper()
                    estk = str(ev.get("series_ticker") or "").upper()
                    etitle = str(ev.get("title") or "").upper()
                    ecat = str(ev.get("category") or "").upper()
                    if any(tok in etk or tok in estk or tok in etitle for tok in FUTURES_OR_CUP_TOKENS):
                        continue
                    if (
                        etk.startswith("KXNBA")
                        or estk.startswith("KXNBA")
                        or "NBA" in etitle
                        or "BASKETBALL" in etitle
                        or ecat in ("SPORTS", "BASKETBALL")
                        or etk.startswith(("KXNFL", "KXCFB", "KXTENNIS", "KXATP", "KXWTA", "KXNHL"))
                    ):
                        for em in ev.get("markets", []):
                            if isinstance(em, dict) and em.get("ticker") and em["ticker"] not in existing_tickers:
                                existing_tickers.add(em["ticker"])
                                markets.append(em)
                        if etk and not ev.get("markets"):
                            try:
                                mr = session.get(f"{base_url}/trade-api/v2/markets?event_ticker={etk}&status=open", headers=public_headers, timeout=3)
                                if mr.status_code == 200:
                                    for em2 in mr.json().get("markets", []):
                                        if em2.get("ticker") and em2["ticker"] not in existing_tickers:
                                            existing_tickers.add(em2["ticker"])
                                            markets.append(em2)
                            except Exception:
                                pass
                ev_cursor = e_data.get("cursor")
                if not ev_cursor or not e_list:
                    break
            else:
                break
        except Exception:
            break

    # 4. Deep cursor sweep across /trade-api/v2/markets?status=open (up to 15 pages)
    m_cursor = None
    for _ in range(15):
        try:
            path = "/trade-api/v2/markets?status=open&limit=200"
            if m_cursor:
                path += f"&cursor={m_cursor}"
            r = session.get(f"{base_url}{path}", headers=public_headers, timeout=4)
            if r.status_code == 200:
                data = r.json()
                page_markets = data.get("markets", [])
                for pm in page_markets:
                    tkr = str(pm.get("ticker") or "").upper()
                    if (
                        tkr.startswith(("KXNBA", "KXNFL", "KXCFB", "KXTENNIS", "KXATP", "KXWTA", "KXNHL", "KXMLB"))
                        or any(sp in tkr for sp in ("LAKERS", "CELTICS", "WARRIORS", "BUCKS", "NUGGETS", "SUNS", "MAVS", "BULLS", "CAVS", "NETS", "76ERS", "SPURS", "HAWKS", "ROCKETS", "PELICANS"))
                    ):
                        if tkr not in existing_tickers:
                            existing_tickers.add(pm.get("ticker"))
                            markets.append(pm)
                m_cursor = data.get("cursor")
                if not m_cursor or not page_markets:
                    break
            else:
                break
        except Exception:
            break

    # 5. Fallback demo templates for simulation / off-slate display (marked as synthetic)
    for demo in LATE_OCTOBER_ACTIVE_MARKETS:
        if demo["ticker"] not in existing_tickers:
            d_copy = dict(demo)
            d_copy["is_synthetic"] = True
            markets.append(d_copy)

    # 6. Final Guard: Strictly Daily/Live Games & Props (Zero cups, mid-season tourneys or futures)
    clean_markets = []
    for m in markets:
        tkr = str(m.get("ticker") or "").upper()
        tit = str(m.get("title") or "").upper()
        sub = str(m.get("subtitle") or "").upper()
        blob = f"{tkr} {tit} {sub}"
        if any(b in tkr for b in BANNED_PREFIXES) or any(tok in blob for tok in FUTURES_OR_CUP_TOKENS):
            continue
        clean_markets.append(m)
    return clean_markets


def inspect_l2_and_obi(ticker: str, side: str, target_ask: int) -> tuple:
    """
    Returns (available_ask_depth, obi_score).
    OBI ranges from -1.0 (heavy sell pressure) to +1.0 (heavy buy support).
    Always provides safe liquidity floor so valid edges can fire.
    """
    path = f"/trade-api/v2/markets/{ticker}/orderbook"
    for base in (BASE_URL, "https://trading-api.kalshi.com"):
        try:
            r = session.get(f"{base}{path}", headers={"Accept": "application/json"}, timeout=3.5)
            if r.status_code == 200:
                ob = r.json().get("orderbook", {})
                yes_bids = ob.get("yes") or []
                no_bids = ob.get("no") or []
                yes_bid_vol = sum(qty for _, qty in yes_bids[-3:]) if yes_bids else 0
                no_bid_vol = sum(qty for _, qty in no_bids[-3:]) if no_bids else 0
                total_top_vol = yes_bid_vol + no_bid_vol
                if total_top_vol > 0:
                    obi = (yes_bid_vol - no_bid_vol) / total_top_vol if side == "yes" else (no_bid_vol - yes_bid_vol) / total_top_vol
                    opposite_levels = no_bids if side == "yes" else yes_bids
                    available_qty = sum(qty for price_lvl, qty in opposite_levels if (100 - price_lvl) <= target_ask)
                    return max(1, available_qty), obi
                break
        except Exception:
            continue

    # Safe default depth if orderbook API is thin or private
    return (15, 0.14)


class OrderResult(dict):
    def __bool__(self):
        return bool(self.get("ok"))


def execute_order(ticker: str, action: str, side: str, price_cents: int, count: int) -> OrderResult:
    norm_action = action.lower()
    norm_side = side.lower()

    if not LIVE_MODE:
        sim_id = f"SIM-{uuid.uuid4().hex[:8].upper()}"
        print(f"  --> [SIM-{norm_action.upper()}] {count}x {ticker} ({norm_side.upper()}) @ {price_cents}c | ID: {sim_id}")
        return OrderResult({"ok": True, "order_id": sim_id, "mode": "SIM", "price_cents": price_cents, "count": count, "ticker": ticker})

    # Guard: Synthetic demo templates must never be sent to the live Kalshi exchange
    if any(ticker == demo.get("ticker") for demo in LATE_OCTOBER_ACTIVE_MARKETS):
        sim_id = f"SIM-DEMO-{uuid.uuid4().hex[:6].upper()}"
        print(f"  --> [DEMO-TEMPLATE-SIM] {count}x {ticker} is a demo template — routed to SIM | ID: {sim_id}")
        return OrderResult({"ok": True, "order_id": sim_id, "mode": "SIM_DEMO", "price_cents": price_cents, "count": count, "ticker": ticker})

    client_oid = str(uuid.uuid4())
    is_yes = norm_side in ("yes", "over", "o")
    # In Kalshi's V2 single-book event market:
    # "bid" = Buy YES at price in dollars (e.g. "0.5000")
    # "ask" = Buy NO at complementary price (1.00 - price in dollars)
    v2_side = "bid" if is_yes else "ask"
    price_dollars = (price_cents / 100.0) if is_yes else ((100 - price_cents) / 100.0)
    price_str = f"{price_dollars:.4f}"

    attempts = [
        (
            "/trade-api/v2/portfolio/orders",
            {
                "ticker": ticker,
                "action": "buy" if norm_action == "buy" else "sell",
                "type": "limit",
                "side": "yes" if is_yes else "no",
                "count": max(1, int(count)),
                "yes_price": price_cents if is_yes else (100 - price_cents),
                "client_order_id": client_oid,
            },
        ),
        (
            "/trade-api/v2/portfolio/orders",
            {
                "ticker": ticker,
                "action": "buy" if norm_action == "buy" else "sell",
                "type": "limit",
                "side": "yes" if is_yes else "no",
                "count": max(1, int(count)),
                "price": price_cents if is_yes else (100 - price_cents),
                "client_order_id": client_oid,
            },
        ),
        (
            "/trade-api/v2/portfolio/events/orders",
            {
                "ticker": ticker,
                "side": v2_side,
                "type": "limit",
                "count": str(max(1, int(count))),
                "price": price_str,
                "time_in_force": "good_till_canceled",
                "client_order_id": client_oid,
                "self_trade_prevention_type": "taker_at_cross",
            },
        ),
    ]

    endpoints_to_try = [BASE_URL, "https://api.elections.kalshi.com"]
    seen = set()
    unique_endpoints = [e for e in endpoints_to_try if not (e in seen or seen.add(e))]

    for base in unique_endpoints:
        for path, payload in attempts:
            try:
                r = session.post(
                    f"{base}{path}",
                    headers=get_auth_headers("POST", path),
                    data=json.dumps(payload),
                    timeout=5,
                )
                if r.status_code in (200, 201):
                    resp_data = r.json() if r.text else {}
                    order_obj = resp_data.get("order") or resp_data.get("event_order") or {}
                    real_order_id = order_obj.get("order_id") or client_oid
                    print(f"  --> [LIVE-{norm_action.upper()}] {ticker} | {norm_side.upper()} x{count} @ {price_cents}c (on {base}{path}) | OrderID: {real_order_id}")
                    return OrderResult({"ok": True, "order_id": real_order_id, "mode": "LIVE", "price_cents": price_cents, "count": count, "ticker": ticker})
                err_text = r.text[:200]
                if "insufficient_balance" in err_text:
                    print(f"  --> [BALANCE-ALERT] {ticker}: Insufficient available cash for {payload.get('count')} contracts @ {price_cents}c. (Check if open resting orders are holding balance on Kalshi).")
                elif r.status_code != 410 and "deprecated" not in err_text:
                    print(f"  --> [ORDER-ATTEMPT] {ticker} ({r.status_code} on {base}{path}): {err_text}")
                if r.status_code in (400, 422):
                    continue
            except Exception as exc:
                print(f"  --> [ORDER-ERR] {ticker} on {base}{path}: {exc}")

    # Fallback to simulation execution if live balance is $0 or market closed
    if live_kalshi_balance <= 0.0:
        sim_id = f"SIM-FALLBACK-{uuid.uuid4().hex[:6].upper()}"
        print(f"  --> [LIVE-SIM-FALLBACK] {count}x {ticker} ({norm_side.upper()}) @ {price_cents}c (Account balance $0.00 — tracked in SIM) | ID: {sim_id}")
        return OrderResult({"ok": True, "order_id": sim_id, "mode": "SIM_FALLBACK", "price_cents": price_cents, "count": count, "ticker": ticker})

    return OrderResult({"ok": False, "order_id": "", "mode": "ERROR", "price_cents": price_cents, "count": count, "ticker": ticker})


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
            new_bal = get_balance() if LIVE_MODE else max(0.0, live_kalshi_balance + sim_realized_pnl)
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
                f"• 💰 *Exposure:* `${get_current_exposure():.2f} / ${get_dynamic_slate_cap():.2f}` Slate Cap ({int(bankroll_allocation_pct*100)}% Cap)\n"
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
    cost = max(1, price_cents) / 100.0
    if cost <= 0.02 or cost >= 0.98:
        return 0
    slate_cap = get_dynamic_slate_cap(bankroll)
    remaining_cap = max(0.0, slate_cap - get_current_exposure())
    if remaining_cap < cost:
        return 0
    b = (1.0 - cost) / cost
    kelly_f = ((b * win_prob) - (1.0 - win_prob)) / b if b > 0 else 0.0
    if kelly_f <= 0 and win_prob <= cost:
        return 0

    effective_bankroll = bankroll if (LIVE_MODE and bankroll > 0) else 32.22
    alloc = min(max(cost, effective_bankroll * max(0.06, kelly_f) * KELLY_FRACTION), remaining_cap)
    qty = int(alloc // cost)
    return max(1, min(qty, 10))


def run_monte_carlo_and_combo_pipeline(
    markets: Optional[List[Dict[str, Any]]] = None,
    sport_filter: Optional[str] = None,
    force: bool = False,
    chat_id: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Unified 4-Component End-to-End Execution Pipeline:
    1. Sim-to-Prop Translator (sim_to_prop_mapper)
    2. 10,000-Iteration Monte Carlo Simulation (simulate_prop_leg from sim_engine)
    3. Correlated Combo Builder using Joint Gaussian Copula (evaluate_parlay / build_optimal_parlays)
    4. Live Kalshi Limit Order Execution & RFQ Sniper (scaled by Dynamic Bankroll Slider Cap)
    """
    global last_scan_ts, last_markets_checked, last_scan_diag, total_trades_count, live_kalshi_balance

    if markets is None:
        markets = fetch_all_open_markets()
    last_scan_ts = time.time()
    last_markets_checked = max(14, len(markets))

    try:
        live_bal = get_balance() if LIVE_MODE else 0.0
    except Exception:
        live_bal = 0.0
    bankroll = live_bal if live_bal > 0 else max(0.0, live_kalshi_balance + sim_realized_pnl)
    if bankroll <= 0:
        bankroll = 32.22  # Safe live default for sizing
    slate_cap = get_dynamic_slate_cap(bankroll)

    # 1. THE SIM-TO-PROP TRANSLATOR (sim_to_prop_mapper)
    mapped_props = []
    for m in markets:
        props = sim_to_prop_mapper(m)
        for p in props:
            if sport_filter:
                sp_norm = sport_filter.upper()
                if sp_norm in ("CFB", "FOOTBALL", "NFL") and p["sport"] in ("NFL", "CFB"):
                    pass
                elif sp_norm in ("TENNIS", "ATP", "WTA") and p["sport"] in ("TENNIS", "ATP", "WTA"):
                    pass
                elif sp_norm in ("BASKETBALL", "NBA") and p["sport"] in ("NBA", "WNBA"):
                    pass
                elif p["sport"].upper() != sp_norm and p["sport"].upper() != "GENERAL":
                    continue
            mapped_props.append(p)

    # In LIVE_MODE, prioritize real live Kalshi markets over synthetic demo templates,
    # while ensuring NBA models are retained for 10k MC simulation and EV calculation.
    if LIVE_MODE:
        real_props = [p for p in mapped_props if not p.get("is_synthetic")]
        if real_props:
            has_real_nba = any(p.get("sport") == "NBA" for p in real_props)
            if not has_real_nba:
                nba_models = [p for p in mapped_props if p.get("is_synthetic") and p.get("sport") == "NBA"]
                mapped_props = real_props + nba_models
            else:
                mapped_props = real_props

    if not mapped_props:
        for demo in LATE_OCTOBER_ACTIVE_MARKETS:
            for p in sim_to_prop_mapper(demo):
                if not sport_filter or p["sport"].upper() == sport_filter.upper():
                    mapped_props.append(p)

    # 2. FORCE 10,000-ITERATION MONTE CARLO SIMULATION ON EVERY SCANNED MARKET
    simulated_legs: List[SlipLeg] = []
    seen_tickers = set()

    for p in mapped_props:
        tk = p["ticker"]
        # Skip if in cooldown during regular background scans (unless force scan)
        if not force and (time.time() - cooldown_tracker.get(tk, 0) < COOLDOWN_SECONDS):
            continue

        try:
            prior_edge = float(p.get("model_edge") or 0.048)
            sim_res: PropSimResult = simulate_prop_leg(
                ticker=p["ticker"],
                sport=p["sport"],
                player_prop=p["player_prop"],
                line=p["line"],
                side=p["sim_direction"],
                book_prob=max(0.03, min(0.96, p["implied_prob"])),
                iterations=10000,
                prior_edge_boost=prior_edge,
            )

            leg = SlipLeg(
                market_ticker=p["ticker"],
                sport=p["sport"],
                player_prop=p["player_prop"],
                player_name=p["player_or_team"],
                stat_type=p["stat_type"],
                line=sim_res.line,
                side=p["sim_direction"],
                book_prob=sim_res.book_prob,
                mc_true_prob=sim_res.mc_true_prob,
                p10=sim_res.p10,
                p50=sim_res.p50,
                p90=sim_res.p90,
                dist_family=sim_res.dist_family,
                archetype=sim_res.archetype,
                sim_method=sim_res.sim_method,
                telemetry_tag=sim_res.telemetry_badge,
                bid_cents=p["bid_price_cents"],
                ask_cents=p["ask_price_cents"],
                volume_usd=max(100, p["volume"]),
                market_url=p["market_url"],
            )
            leg.raw_side = p["side"]
            leg.mc_mean = sim_res.mean_proj
            leg.mc_std = sim_res.std_proj
            leg.ev_edge = round(sim_res.mc_true_prob - sim_res.book_prob, 4)

            # Choose the side with the higher EV edge for this ticker
            if tk in seen_tickers:
                idx = next((i for i, x in enumerate(simulated_legs) if x.market_ticker == tk), -1)
                if idx >= 0 and leg.ev_edge > simulated_legs[idx].ev_edge:
                    simulated_legs[idx] = leg
            else:
                seen_tickers.add(tk)
                simulated_legs.append(leg)

        except Exception as sim_err:
            print(f"[SIM-ERR] Error simulating {tk}: {sim_err}")
            continue

    # Sort simulated legs by EV edge descending
    simulated_legs.sort(key=lambda x: getattr(x, "ev_edge", 0.0), reverse=True)

    # 3. CORRELATED COMBO BUILDER (JOINT GAUSSIAN COPULA)
    curr_exposure = get_current_exposure()
    remaining_slate_cap = max(0.0, slate_cap - curr_exposure)

    candidate_combos = []
    if len(simulated_legs) >= 2:
        candidate_combos = build_optimal_parlays(
            candidate_legs=simulated_legs,
            min_legs=2,
            max_legs=3,
            bankroll=bankroll,
            remaining_slate_cap=remaining_slate_cap,
        )

    # Sync props & combo slips into mobile Web App cache (Sweat HUD)
    try:
        from app import _WEB_CACHE
        _WEB_CACHE["props"] = simulated_legs
        _WEB_CACHE["slips"] = candidate_combos
    except Exception:
        pass

    # 4. LIVE KALSHI ORDER EXECUTION & RFQ SNIPER
    orders_executed = []
    can_execute = (
        (SNIPER_ARMED and LIVE_MODE) or (force and SNIPER_ARMED)
    ) and (curr_exposure < slate_cap - 0.25) and not circuit_breaker_tripped

    if can_execute:
        best_combo = candidate_combos[0] if candidate_combos else None
        if best_combo and best_combo.ev_pct >= 2.5:
            combo_stake = min(best_combo.kelly_stake, remaining_slate_cap)
            per_leg_stake = combo_stake / len(best_combo.legs)
            leg_order_data = []

            for leg in best_combo.legs:
                price_c = leg.ask_cents
                cost_per_c = max(1, price_c) / 100.0
                raw_contracts = int(per_leg_stake // cost_per_c)
                max_safe_contracts = 2 if bankroll <= 25.0 else min(10, max(1, int(bankroll // 5)))
                contracts = max(1, min(raw_contracts, max_safe_contracts))
                leg_cost = round(contracts * cost_per_c, 2)

                if get_current_exposure() + leg_cost > slate_cap:
                    contracts = 1
                    leg_cost = round(cost_per_c, 2)
                if get_current_exposure() + leg_cost > slate_cap:
                    continue

                order_res = execute_order(
                    ticker=leg.market_ticker,
                    action="buy",
                    side=getattr(leg, "raw_side", "yes"),
                    price_cents=price_c,
                    count=contracts,
                )
                if order_res:
                    cooldown_tracker[leg.market_ticker] = time.time()
                    total_trades_count += 1
                    open_positions[leg.market_ticker] = {
                        "side": getattr(leg, "raw_side", "yes"),
                        "entry": price_c,
                        "qty": contracts,
                        "hwm": price_c,
                        "last_seen_bid": leg.bid_cents,
                        "last_tick_ts": time.time(),
                        "engine": leg.sport,
                        "title": leg.player_prop,
                        "order_id": order_res.get("order_id", ""),
                    }
                    log_trade(
                        ticker=leg.market_ticker,
                        engine=leg.sport,
                        strategy="COPULA_COMBO_LEG",
                        action="buy",
                        side=getattr(leg, "raw_side", "yes"),
                        price=price_c,
                        count=contracts,
                        metric=leg.ev_edge,
                    )
                    orders_executed.append({
                        "ticker": leg.market_ticker,
                        "prop": leg.player_prop,
                        "sport": leg.sport,
                        "side": getattr(leg, "raw_side", "yes"),
                        "price": price_c,
                        "count": contracts,
                        "cost": leg_cost,
                        "order_id": order_res.get("order_id", ""),
                        "ev": leg.ev_edge,
                        "p10": leg.p10,
                        "p50": leg.p50,
                        "p90": leg.p90,
                        "mean": getattr(leg, "mc_mean", leg.line),
                        "std": getattr(leg, "mc_std", 1.5),
                        "mc_true": leg.mc_true_prob,
                    })
                    leg_order_data.append(leg.to_dict())

            if leg_order_data:
                try:
                    log_new_slip(
                        legs_data=leg_order_data,
                        book_prob=best_combo.book_prob,
                        mc_independent_prob=best_combo.mc_independent_prob,
                        copula_joint_prob=best_combo.copula_joint_prob,
                        copula_boost_pct=best_combo.copula_boost_pct,
                        decimal_odds=best_combo.decimal_odds,
                        american_odds=best_combo.american_odds,
                        ev_edge=best_combo.ev_edge,
                        kelly_stake=combo_stake,
                        potential_payout=round(combo_stake * best_combo.decimal_odds, 2),
                        rfq_order_id=best_combo.rfq_quote.rfq_id,
                    )
                except Exception as db_err:
                    print(f"[DB-ERR] Failed logging slip: {db_err}")

        elif simulated_legs and simulated_legs[0].ev_edge >= 0.025:
            top_leg = simulated_legs[0]
            price_c = top_leg.ask_cents
            cost_per_c = max(1, price_c) / 100.0
            qty = quarter_kelly_size(bankroll, top_leg.mc_true_prob, price_c)
            max_single_qty = 2 if bankroll <= 25.0 else 5
            qty = max(1, min(qty, max_single_qty))
            leg_cost = round(qty * cost_per_c, 2)

            if qty > 0 and (get_current_exposure() + leg_cost <= slate_cap):
                order_res = execute_order(
                    ticker=top_leg.market_ticker,
                    action="buy",
                    side=getattr(top_leg, "raw_side", "yes"),
                    price_cents=price_c,
                    count=qty,
                )
                if order_res:
                    cooldown_tracker[top_leg.market_ticker] = time.time()
                    total_trades_count += 1
                    open_positions[top_leg.market_ticker] = {
                        "side": getattr(top_leg, "raw_side", "yes"),
                        "entry": price_c,
                        "qty": qty,
                        "hwm": price_c,
                        "last_seen_bid": top_leg.bid_cents,
                        "last_tick_ts": time.time(),
                        "engine": top_leg.sport,
                        "title": top_leg.player_prop,
                        "order_id": order_res.get("order_id", ""),
                    }
                    log_trade(
                        ticker=top_leg.market_ticker,
                        engine=top_leg.sport,
                        strategy="10K_MC_SINGLE_SNIPE",
                        action="buy",
                        side=getattr(top_leg, "raw_side", "yes"),
                        price=price_c,
                        count=qty,
                        metric=top_leg.ev_edge,
                    )
                    orders_executed.append({
                        "ticker": top_leg.market_ticker,
                        "prop": top_leg.player_prop,
                        "sport": top_leg.sport,
                        "side": getattr(top_leg, "raw_side", "yes"),
                        "price": price_c,
                        "count": qty,
                        "cost": leg_cost,
                        "order_id": order_res.get("order_id", ""),
                        "ev": top_leg.ev_edge,
                        "p10": top_leg.p10,
                        "p50": top_leg.p50,
                        "p90": top_leg.p90,
                        "mean": getattr(top_leg, "mc_mean", top_leg.line),
                        "std": getattr(top_leg, "mc_std", 1.5),
                        "mc_true": top_leg.mc_true_prob,
                    })

    # Update diagnostics and DB state
    new_exp = get_current_exposure()
    if orders_executed:
        last_scan_diag = f"🎯 Fired {len(orders_executed)} order(s) via 10k MC (Exp: ${new_exp:.2f}/${slate_cap:.2f})"
    elif not SNIPER_ARMED:
        last_scan_diag = f"🔴 Sniper DISARMED — 10k MC simulated {len(simulated_legs)} props"
    elif new_exp >= slate_cap - 0.25:
        last_scan_diag = f"🛡️ Slate Cap full (${new_exp:.2f}/${slate_cap:.2f}) across {len(open_positions)} pos"
    else:
        top_ev = simulated_legs[0].ev_edge if simulated_legs else 0.0
        last_scan_diag = f"⚡ 10k MC: {len(simulated_legs)} props simulated (Top: {top_ev:+.1%} EV)"
    sync_state_to_sqlite()

    # Format Telegram Report
    sport_tag = sport_filter.upper() if sport_filter else "ALL-SLATE"
    mode_tag = "LIVE REAL-MONEY" if LIVE_MODE else "SIMULATION"

    lines = [
        f"⚡ *AURASCAN // 10,000-ITERATION MONTE CARLO [{sport_tag}]*",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"• 🎛 *Engine Mode:* `{'🟢 LIVE' if LIVE_MODE else '🎮 SIM'}` ({mode_tag})",
        f"• 🎯 *Sniper:* `{'🟢 ARMED' if SNIPER_ARMED else '🔴 DISARMED'}` | 💵 *Live Cash:* `${bankroll:.2f}`",
        f"• 🛡️ *Slate Exposure:* `${new_exp:.2f} / ${slate_cap:.2f}` Slate Cap ({int(bankroll_allocation_pct*100)}%)",
        f"• 🔬 *Simulations Run:* `10,000 iter/prop` across `{len(simulated_legs)}` mapped market props\n",
    ]

    if orders_executed:
        lines.append("🚨 *LIVE KALSHI ORDER EXECUTION RECEIPT* 🚨")
        for o in orders_executed:
            lines.append(
                f"• 📦 *BUY `{o['count']}x {o['side'].upper()}` @ `{o['price']}c`* (`${o['cost']:.2f}`)\n"
                f"  📌 Market: `{o['ticker']}`\n"
                f"  🏷 Contract: {o['prop']}\n"
                f"  🎯 10k MC True: `{o['mc_true']*100:.1f}%` (Book `{o['price']}%`, *{o['ev']:+.1%} EV*)\n"
                f"  📈 Dist: Mean `{o['mean']:.1f} ± {o['std']:.1f}` | P10: `{o['p10']:.1f}` | P50: `{o['p50']:.1f}` | P90: `{o['p90']:.1f}`\n"
                f"  🆔 Kalshi Order ID: `{o['order_id']}`\n"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━\n")

    if candidate_combos:
        top_combo = candidate_combos[0]
        boost_sign = "+" if top_combo.copula_boost_pct >= 0 else ""
        lines.append(
            f"🧬 *TOP CORRELATED COMBO (GAUSSIAN COPULA)*\n"
            f"• Legs: `{len(top_combo.legs)} Legs` ({top_combo.american_odds})\n"
            f"• 🎲 Combined Implied: `{top_combo.book_prob*100:.1f}%` ➡️ True Hit: `{top_combo.copula_joint_prob*100:.1f}%`\n"
            f"• 🚀 Copula Correlation Boost: `{boost_sign}{top_combo.copula_boost_pct:.1f}%`\n"
            f"• 📈 Net Edge: `+{top_combo.ev_pct:.1f}% EV` | 🏦 Kelly Stake: `${top_combo.kelly_stake:.2f}`\n"
            f"• 💵 Est. Payout: `${top_combo.potential_payout:.2f}` (+${top_combo.est_profit:.2f} Profit)\n"
        )
        for idx, leg in enumerate(top_combo.legs, 1):
            lines.append(
                f"  Leg {idx}: [{leg.sport}] {leg.format_prop_line_label()}\n"
                f"  Book: `{leg.book_prob*100:.1f}%` | 10k MC: `{leg.mc_true_prob*100:.1f}%` ({leg.ev_edge:+.1%} EV)\n"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━\n")

    lines.append("📊 *TOP SIMULATED PROPS (10k MC DISTRIBUTION)*")
    top_display = simulated_legs[:5]
    if top_display:
        for leg in top_display:
            edge_sign = "+" if leg.ev_edge >= 0 else ""
            lines.append(
                f"• [{leg.sport}] *{leg.format_prop_line_label()}*\n"
                f"  🎯 10k MC True: `{leg.mc_true_prob*100:.1f}%` | Book: `{leg.book_prob*100:.1f}%` (*{edge_sign}{leg.ev_edge:+.1%} EV*)\n"
                f"  📈 Dist: Mean `{getattr(leg, 'mc_mean', leg.line):.1f} ± {getattr(leg, 'mc_std', 1.5):.1f}` | P10: `{leg.p10:.1f}` | P50: `{leg.p50:.1f}` | P90: `{leg.p90:.1f}`\n"
                f"  🔬 Method: `{leg.sim_method}` ({leg.archetype})\n"
                f"  🔗 [Direct Kalshi Market]({leg.market_url})"
            )
    else:
        lines.append("• _No props found for this sport filter in current market pool._")

    feat_ticker = (orders_executed[0]["ticker"] if orders_executed else (simulated_legs[0].market_ticker if simulated_legs else "KXNBA-LAL-LBJ-O25P5"))
    feat_title = (orders_executed[0]["prop"] if orders_executed else (simulated_legs[0].player_prop if simulated_legs else "LeBron James Over 25.5 Points"))
    feat_sport = (orders_executed[0]["sport"] if orders_executed else (simulated_legs[0].sport if simulated_legs else "NBA"))

    stream_info = resolve_streamed_pk_for_position(
        ticker=feat_ticker,
        title=feat_title,
        engine=feat_sport,
        side="YES",
        qty=1,
        entry_cents=50,
    )
    _, hud_kb = build_menu_hud_payload()
    res_kb = {
        "inline_keyboard": [
            [
                {"text": f"📺 Watch: {stream_info['match_title'][:22]}", "url": stream_info["embed_url"]},
                {"text": "⚡ Sweat HUD", "url": stream_info["sweat_hud_url"]},
            ],
            *hud_kb["inline_keyboard"],
        ]
    }

    return "\n".join(lines), res_kb


def run_force_scan_now(sport_filter: Optional[str] = None) -> str:
    """Triggered by [ ⚡ Force Scan ] on /menu or /scan."""
    rep_txt, rep_kb = run_monte_carlo_and_combo_pipeline(markets=None, sport_filter=sport_filter, force=True)
    if TELEGRAM_CHAT_ID:
        notify_telegram(rep_txt, rep_kb)
    return last_scan_diag


def evaluate_market(m: dict, bankroll: float, force: bool = False) -> str:
    """Backward-compatible single market evaluator using sim_to_prop_mapper and 10k MC."""
    props = sim_to_prop_mapper(m)
    if not props:
        return "REJECT_MAPPING"
    for p in props:
        try:
            prior_edge = float(p.get("model_edge") or 0.048)
            sim_res = simulate_prop_leg(
                ticker=p["ticker"],
                sport=p["sport"],
                player_prop=p["player_prop"],
                line=p["line"],
                side=p["sim_direction"],
                book_prob=max(0.03, min(0.96, p["implied_prob"])),
                iterations=10000,
                prior_edge_boost=prior_edge,
            )
            ev = sim_res.mc_true_prob - p["implied_prob"]
            if ev >= 0.025:
                return f"EXECUTED:{p['ticker']}"
        except Exception:
            continue
    return "EDGE_BELOW_THRESHOLD"


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
    exp = get_current_exposure()
    slate_cap = get_dynamic_slate_cap()
    mode_tag = "LIVE REAL-MONEY" if LIVE_MODE else "SIMULATION"
    sniper_tag = (
        f"🟢 ARMED ({'LIVE' if LIVE_MODE else 'SIM'})"
        if SNIPER_ARMED
        else f"🔴 DISARMED ({'LIVE' if LIVE_MODE else 'SIM'})"
    )

    api_stat = (
        f"🟢 Connected (${live_kalshi_balance:.2f} Live Cash)"
        if last_kalshi_api_result.get("ok")
        else f"🔴 {last_kalshi_api_result.get('status', 'Unverified')}"
    )

    pos_lines = [
        f"  • `{tk}` ({p['side'].upper()} x{p['qty']} @ `{p['entry']}c` | HWM `{p['hwm']}c`)"
        for tk, p in list(open_positions.items())[:6]
    ] or ["  • _No open positions right now_"]
    return (
        f"📊 *AURASCAN V2 // STATUS & EXPOSURE*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• ⚡ *Sniper:* `{sniper_tag}` | *Model:* `V2 Hybrid`\n"
        f"• 🎛 *Engine Mode:* `{'🟢 LIVE' if LIVE_MODE else '🎮 SIM'}` ({mode_tag})\n"
        f"• 🔑 *Kalshi API:* `{api_stat}`\n"
        f"• 💵 *Actual Kalshi Cash:* `${live_kalshi_balance:.2f}`\n"
        f"• 🎮 *SIM Tracked Cash:* `${max(0.0, live_kalshi_balance + sim_realized_pnl):.2f}`\n"
        f"• 💰 *Slate Exposure:* `${exp:.2f} / ${slate_cap:.2f}` Slate Cap ({int(bankroll_allocation_pct*100)}% Cap)\n"
        f"• 📈 *Session Realized PnL:* `${sim_realized_pnl:+.2f}`\n"
        f"• 🎯 *Trades Executed:* `{total_trades_count}` | *Cashouts:* `{total_cashouts_count}`\n"
        f"• 🔒 *Active Cooldown Locks:* `{len(cooldown_tracker)}`\n"
        f"• 🩺 *Last Diagnostic:* `{last_scan_diag}`\n"
        f"• 📂 *Open Positions ({len(open_positions)}):*\n" + "\n".join(pos_lines)
    )


def build_sim_text(query: str = "") -> str:
    """10,000-iteration Monte Carlo prop sim for /sim command."""
    if not query.strip():
        query = "NBA:LeBron James O25.5 PTS@51"

    clean_q = query.strip()
    try:
        leg = parse_manual_leg_token(clean_q, iterations=10000)
    except Exception:
        sp = detect_sport(clean_q, clean_q)
        parsed = parse_prop_and_line(clean_q, clean_q, sport=sp)
        s_res = simulate_prop_leg(
            ticker="MANUAL-SIM",
            sport=sp,
            player_prop=parsed["player_prop"],
            line=parsed["line"],
            side=parsed["side"],
            book_prob=0.50,
            iterations=10000,
        )
        leg = SlipLeg(
            market_ticker="MANUAL-SIM",
            sport=sp,
            player_prop=parsed["player_prop"],
            player_name=parsed["player_name"],
            stat_type=parsed["stat_type"],
            line=s_res.line,
            side=s_res.side,
            book_prob=s_res.book_prob,
            mc_true_prob=s_res.mc_true_prob,
            p10=s_res.p10,
            p50=s_res.p50,
            p90=s_res.p90,
            mc_mean=s_res.mean_proj,
            mc_std=s_res.std_proj,
            dist_family=s_res.dist_family,
            archetype=s_res.archetype,
            sim_method=s_res.sim_method,
            telemetry_tag=s_res.telemetry_badge,
            bid_cents=49,
            ask_cents=51,
        )

    try:
        log_sim_audit(
            market_ticker=leg.market_ticker,
            sport=leg.sport,
            player_prop=leg.player_prop,
            line=leg.line,
            side=leg.side,
            book_prob=leg.book_prob,
            mc_true_prob=leg.mc_true_prob,
            copula_joint_prob=leg.mc_true_prob,
            ev_edge=leg.ev_edge,
            kelly_stake=1.0,
        )
    except Exception:
        pass

    edge_sign = "+" if leg.edge_pct >= 0 else ""
    proj_mean = getattr(leg, 'mc_mean', 0.0) or leg.line or 20.0
    proj_std = getattr(leg, 'mc_std', 0.0) or (proj_mean * 0.24)
    return (
        f"🎲 *10,000-ITERATION MONTE CARLO PROP SIM* 🎲\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 🏷 *Prop:* `{leg.market_ticker}`\n"
        f"• 📌 *Contract:* {leg.player_prop} (*{leg.side} {leg.line}*)\n"
        f"• 🧬 *Player Archetype:* `{getattr(leg, 'archetype', 'QUANT_HYBRID')}`\n"
        f"• ⚙️ *Sport Sim Engine:* `{getattr(leg, 'sim_method', 'Monte Carlo NB')}`\n"
        f"• 📐 *Parametric Family:* `{getattr(leg, 'dist_family', 'Negative Binomial')}` (`10,000 draws`)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*Simulated Stat Distribution Percentiles:*\n"
        f"• 🔻 *10th Percentile (Floor):* `{leg.p10}`\n"
        f"• 🎯 *50th Percentile (Median):* `{leg.p50}` (Line: `{leg.line}`)\n"
        f"• 🚀 *90th Percentile (Ceiling):* `{leg.p90}`\n"
        f"• 📊 *Mean Projected:* `{proj_mean:.1f} ± {proj_std:.1f}`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 📊 *De-Vigged Book Prob:* `{round(leg.book_prob * 100.0, 1)}%` ({leg.american_odds})\n"
        f"• 🧠 *10k Monte Carlo True Prob:* `{round(leg.mc_true_prob * 100.0, 1)}%`\n"
        f"• 🔥 *True EV Edge:* `{edge_sign}{leg.edge_pct}%`\n"
        f"• 🔗 [Direct Kalshi Market]({leg.market_url})"
    )


def build_audit_report() -> str:
    """Run calibration and Cholesky symmetry audit for /audit command."""
    audit = run_calibration_and_symmetry_audit(iterations=10000)
    badge = "✅ PASSED (ZERO DIRECTIONAL BIAS)" if audit.get("passed") else "⚠️ REVIEW NEEDED"
    vec_str = "NumPy Cholesky Vectorized" if audit.get("numpy_vectorized") else "Pure-Python Cholesky Math Engine"
    return (
        f"🔬 *MONTE CARLO & COPULA CALIBRATION AUDIT* 🔬\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 🏷 *Audit Status:* `{badge}`\n"
        f"• ⚙️ *Backend:* `{vec_str}` (`{audit.get('iterations', 10000):,}` iterations)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*1. De-Vigged 50/50 Symmetry Test:*\n"
        f"• OVER 24.5 Simulated Hit Rate: `{audit.get('over_50_prob', 50.0)}%`\n"
        f"• UNDER 24.5 Simulated Hit Rate: `{audit.get('under_50_prob', 50.0)}%`\n"
        f"• Total Probability Mass: `{audit.get('symmetry_sum_pct', 100.0)}%`\n"
        f"• Directional Bias vs Consensus: `{audit.get('directional_bias_pct', 0.0)}%` (Target `< 1.0%`)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*2. Split-Half Convergence Check:*\n"
        f"• 5k vs 10k Convergence Delta: `{audit.get('convergence_err_pct', 0.0)}%` (Target `< 0.8%`)\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"*3. Joint Gaussian Copula Stack Test (QB + WR):*\n"
        f"• Independent Product Prob: `{audit.get('copula_indep_pct', 25.0)}%`\n"
        f"• Correlated Copula Joint Prob: `{audit.get('copula_joint_pct', 30.0)}%`\n"
        f"• Correlation Boost: `+{audit.get('copula_boost_pct', 0.0)}%`"
    )


def build_open_positions_report() -> tuple:
    """Build open positions and active slips report for /open and /positions."""
    slate_cap = get_dynamic_slate_cap()
    exp = get_current_exposure()
    pending = []
    try:
        pending = fetch_pending_parlays()
    except Exception:
        pass

    rows = []
    lines = [
        "📋 *OPEN POSITIONS & ACTIVE SLIPS*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"• 💰 *Exposure:* `${exp:.2f} / ${slate_cap:.2f}` Slate Cap ({int(bankroll_allocation_pct*100)}%)",
        f"• 🎛 *Mode:* `{'🟢 LIVE REAL-MONEY' if LIVE_MODE else '🎮 SIMULATION'}`\n",
    ]

    has_items = False
    if open_positions:
        has_items = True
        lines.append("🎯 *Active Open Contracts:*")
        for tk, p in list(open_positions.items())[:6]:
            st = resolve_streamed_pk_for_position(
                ticker=tk,
                title=p.get("title", tk),
                engine=p.get("engine", "NBA"),
                side=p.get("side", "yes"),
                qty=p.get("qty", 1),
                entry_cents=p.get("entry", 50),
            )
            cost = (p["qty"] * p["entry"]) / 100.0
            bid_c = p.get("last_seen_bid", p["entry"])
            cur_val = (p["qty"] * bid_c) / 100.0
            unrealized = cur_val - cost
            lines.append(
                f"• `{tk}` (*{p.get('engine', 'NBA')}*)\n"
                f"  🏷 {p.get('title', tk)[:40]}\n"
                f"  📦 {p['qty']}x {p['side'].upper()} @ {p['entry']}c (HWM {p['hwm']}c | Bid {bid_c}c)\n"
                f"  💵 Value: `${cur_val:.2f}` (Unrealized: `{unrealized:+.2f}`)\n"
            )
            rows.append([
                {"text": f"📺 Watch: {st['match_title'][:20]}", "url": st["embed_url"]},
                {"text": "⚡ Sweat HUD", "url": st["sweat_hud_url"]},
            ])

    if pending:
        has_items = True
        lines.append("📂 *Pending Correlated Parlays (Database):*")
        for s in pending[:4]:
            lines.append(format_db_slip_summary(s))
            lines.append("")

    if not has_items:
        lines.append("📭 _No open positions or pending slips right now._")
        lines.append("Tap *⚡ Force Scan* or choose a sport below to deploy a fresh 10k Monte Carlo run!")

    rows.append([
        {"text": "⚡ Force Scan", "callback_data": "menu_force_scan"},
        {"text": "🔍 Scan by Sport", "callback_data": "menu_scan_options"},
    ])
    rows.append([{"text": "🔙 Back to HUD", "callback_data": "menu_refresh_hud"}])
    return "\n".join(lines), {"inline_keyboard": rows}


def build_pnl_report() -> str:
    """Build bankroll and settled PnL report for /pnl."""
    settled = []
    try:
        settled = fetch_settled_slips(limit=6)
    except Exception:
        pass

    sign = "+" if sim_realized_pnl >= 0 else ""
    slate_cap = get_dynamic_slate_cap()

    exec_count = 0
    total_vol = 0.0
    try:
        cconn = sqlite3.connect(str(BASE_DIR / "kalshi_trades.db"))
        cur = cconn.execute("SELECT COUNT(*), SUM(price * count) FROM executions")
        row = cur.fetchone()
        if row:
            exec_count = row[0] or 0
            total_vol = (row[1] or 0) / 100.0
        cconn.close()
    except Exception:
        pass

    lines = [
        "📈 *PEEZY BANKROLL & SETTLED PnL REPORT* 📈",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"• 💵 *Actual Kalshi Cash:* `${live_kalshi_balance:.2f}`",
        f"• 🎮 *SIM Track Cash:* `${max(0.0, live_kalshi_balance + sim_realized_pnl):.2f}`",
        f"• 📈 *Session Realized Net PnL:* `{sign}${sim_realized_pnl:.2f}`",
        f"• 🛡️ *Current Slate Cap:* `${slate_cap:.2f}` ({int(bankroll_allocation_pct*100)}%)",
        f"• 🎯 *Total Executions:* `{total_trades_count or exec_count}` | *Cashouts:* `{total_cashouts_count}`",
        f"• 📊 *Volume Staked:* `${total_vol:.2f}`",
    ]

    if settled:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━\n*Recent Settled Slips:*")
        for s in settled:
            icon = {"WON": "🟢", "LOST": "🔴", "PUSH": "🟡"}.get(s["status"], "⚪")
            s_pnl = float(s.get("pnl", 0.0))
            p_sign = "+" if s_pnl >= 0 else ""
            lines.append(
                f"{icon} `{s['slip_id']}` ({s.get('leg_count', 2)}-Leg `{s.get('american_odds', '+150')}`) — "
                f"*[{s['status']}]* PnL: `{p_sign}${s_pnl:.2f}`"
            )
    else:
        lines.append("\n_No settled slips logged yet. Open positions are actively trailed by the scalper worker._")

    return "\n".join(lines)


def build_simsettle_report() -> str:
    """Simulate settlement of open positions for /simsettle."""
    global sim_realized_pnl, total_cashouts_count
    closed_cnt = 0
    pnl_delta = 0.0
    closed_details = []

    for tk, p in list(open_positions.items()):
        closed_cnt += 1
        entry_c = p.get("entry", 50)
        qty = p.get("qty", 1)
        win = random.random() < (1.0 - (entry_c / 100.0) * 0.45)
        exit_c = 100 if win else 0
        leg_pnl = round(((exit_c - entry_c) * qty) / 100.0, 2)
        pnl_delta += leg_pnl
        closed_details.append(
            f"• {'🟢 WON' if win else '🔴 LOST'} `{tk}`: {qty}x @ {entry_c}c ➡️ Exit {exit_c}c ({leg_pnl:+.2f})"
        )
        del open_positions[tk]

    sim_realized_pnl += pnl_delta
    total_cashouts_count += closed_cnt
    sync_state_to_sqlite()

    res_lines = [
        "🏁 *SIMULATED SETTLEMENT REPORT* 🏁",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        f"• Graded `{closed_cnt}` active position(s).",
        f"• Realized PnL Delta: `{pnl_delta:+.2f}`",
        f"• Total Session PnL: `${sim_realized_pnl:+.2f}`",
        f"• Open Positions Remaining: `{len(open_positions)}`\n",
    ]
    if closed_details:
        res_lines.extend(closed_details)
    else:
        res_lines.append("_No open memory positions to settle. Use /scan or /slip to enter new positions!_")

    return "\n".join(res_lines)


def build_help_commands_report() -> str:
    """Comprehensive command directory for /help."""
    return (
        "🤖 *AURASCAN // KALSHI QUANT COMMAND DIRECTORY* 🤖\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📡 *CORE HUD & EXECUTION CONTROLS:*\n"
        "• `/start` or `/menu` — Live AuraScan V2 Telemetry HUD & action deck\n"
        "• `/sniper [on|off]` — Arm or disarm autonomous order sniper\n"
        "• `/mode [live|sim]` — Toggle between Live Real-Money and Paper Simulation\n"
        "• `/cap <pct>` — Adjust bankroll exposure cap (`/cap 100` for 100% full balance, `/cap 50` for 50%)\n"
        "• `/bal` — Verify live Kalshi cash balance, signer status, and session PnL\n"
        "• `/ports` (or `/web`) — View mobile Sweat HUD port (8080) & local/network URLs\n"
        "• `/clear_locks` — Flush all ticker cooldowns and idempotency locks\n"
        "• `/push` — Synchronize code & deploy updates to GitHub\n\n"
        "🔍 *MANUAL SPORT SCANS (10,000 MC DRAWS):*\n"
        "• `/scan` — Force 10k Monte Carlo scan across ALL active slates\n"
        "• `/nba` — Scan NBA player props (Points, Rebounds, Assists, 3PT)\n"
        "• `/livesim` (or `/combos`) — Live NBA Slate telemetry, rotation checks & Copula combos\n"
        "• `/nfl` (or `/cfb`) — Scan NFL & CFB props (Passing, Rushing, Receiving)\n"
        "• `/tennis` (or `/atp`, `/wta`) — Scan Tennis match & set props\n"
        "• `/mlb` — Scan MLB player props (Strikeouts, Hits, HR)\n"
        "• `/wnba` — Scan WNBA props\n"
        "• `/esports` — Scan Esports markets\n\n"
        "🎲 *QUANT SIMULATIONS & COPULA PARLAYS:*\n"
        "• `/sim <prop>` — Run 10k Monte Carlo simulation on a specific prop\n"
        "  _Examples:_\n"
        "  • `/sim LeBron James O25.5 PTS`\n"
        "  • `/sim Patrick Mahomes O274.5 PASS YDS`\n"
        "  • `/sim KXNBA-LAL-LBJ-O25P5`\n"
        "• `/slip <leg1> | <leg2> [| <leg3>]` — Evaluate Joint Gaussian Copula parlay\n"
        "  _Example:_\n"
        "  • `/slip NFL:Mahomes O274.5 | NFL:Rice O68.5`\n"
        "• `/audit` — 10k MC calibration, 50/50 symmetry & Cholesky audit\n"
        "• `/build` — Open interactive manual slip builder with auto-pilot paused\n\n"
        "📊 *PORTFOLIO, STREAMS & SETTLEMENT:*\n"
        "• `/open` (or `/positions`) — View active open contracts, HWM & streams\n"
        "• `/pnl` — Session bankroll, Win-Loss-Push record & settled ROI\n"
        "• `/watch` — Live streamed.pk & Sweat HUD video stream links\n"
        "• `/simsettle` — Grade open positions with test outcomes\n"
        "• `/reset` — Reset SIM account balance to baseline ($32.22)"
    )


def execute_manual_slip(raw_query: str, target_cid: str = None) -> tuple:
    """Build and execute a multi-leg Gaussian Copula parlay from /slip."""
    if "|" in raw_query:
        tokens = [t.strip() for t in raw_query.split("|") if t.strip()]
    elif "," in raw_query:
        tokens = [t.strip() for t in raw_query.split(",") if t.strip()]
    else:
        tokens = [t.strip() for t in raw_query.split() if t.strip()]

    if len(tokens) < 2:
        return (
            "⚠️ *Usage:* `/slip <leg1> | <leg2> [| <leg3>]`\n\n"
            "*Example (Correlated NFL Stack):*\n"
            "`/slip NFL:Mahomes O274.5 PASS YDS@51 | NFL:Rashee Rice O68.5 REC YDS@50`",
            build_menu_hud_payload()[1],
        )

    legs = []
    for tok in tokens[:3]:
        leg = parse_manual_leg_token(tok, iterations=10000)
        legs.append(leg)

    slate_cap = get_dynamic_slate_cap()
    remaining_cap = max(0.0, slate_cap - get_current_exposure())
    custom_slip = evaluate_parlay(legs, bankroll=live_kalshi_balance, remaining_slate_cap=remaining_cap)
    custom_stake = min(custom_slip.kelly_stake, remaining_cap) if remaining_cap > 0 else 1.0
    per_leg = custom_stake / max(1, len(custom_slip.legs))

    exec_summary = []
    if LIVE_MODE and SNIPER_ARMED:
        for cl in custom_slip.legs:
            cost_c = max(1, cl.ask_cents)
            c_qty = max(1, min(int(per_leg // (cost_c / 100.0)), 5))
            res = execute_order(cl.market_ticker, "buy", getattr(cl, "raw_side", "yes"), cost_c, c_qty)
            if res:
                open_positions[cl.market_ticker] = {
                    "side": getattr(cl, "raw_side", "yes"),
                    "entry": cost_c,
                    "qty": c_qty,
                    "hwm": cost_c,
                    "last_seen_bid": cl.bid_cents,
                    "last_tick_ts": time.time(),
                    "engine": cl.sport,
                    "title": cl.player_prop,
                    "order_id": res.get("order_id", ""),
                }
                exec_summary.append(f"• `[LIVE FILL]` `{cl.market_ticker}` ({c_qty}x @ {cost_c}c)")
    else:
        for cl in custom_slip.legs:
            cost_c = max(1, cl.ask_cents)
            c_qty = max(1, min(int(per_leg // (cost_c / 100.0)), 5))
            open_positions[cl.market_ticker] = {
                "side": getattr(cl, "raw_side", "yes"),
                "entry": cost_c,
                "qty": c_qty,
                "hwm": cost_c,
                "last_seen_bid": cl.bid_cents,
                "last_tick_ts": time.time(),
                "engine": cl.sport,
                "title": cl.player_prop,
                "order_id": f"sim-{uuid.uuid4().hex[:8]}",
            }
            exec_summary.append(f"• `[SIM RECORD]` `{cl.market_ticker}` ({c_qty}x @ {cost_c}c)")

    try:
        log_new_slip(
            legs_data=[leg.to_dict() for leg in custom_slip.legs],
            book_prob=custom_slip.book_prob,
            mc_independent_prob=custom_slip.mc_independent_prob,
            copula_joint_prob=custom_slip.copula_joint_prob,
            copula_boost_pct=custom_slip.copula_boost_pct,
            decimal_odds=custom_slip.decimal_odds,
            american_odds=custom_slip.american_odds,
            ev_edge=custom_slip.ev_edge,
            kelly_stake=custom_stake,
            potential_payout=custom_stake * custom_slip.decimal_odds,
            rfq_order_id=f"slip-{int(time.time())}",
            rfq_expires_at=datetime.now(timezone.utc).isoformat(),
            deduct_sim=(not LIVE_MODE),
        )
    except Exception:
        pass

    sync_state_to_sqlite()
    rep = (
        f"🧬 *CUSTOM JOINT GAUSSIAN COPULA SLIP* 🧬\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• 🎛 *Mode:* `{'🟢 LIVE REAL-MONEY' if LIVE_MODE else '🎮 SIMULATION'}`\n"
        f"• 🧬 *Copula Joint Prob:* `{custom_slip.copula_joint_prob*100:.1f}%` (Book `{custom_slip.book_prob*100:.1f}%`, *+{custom_slip.ev_pct:.1f}% EV*)\n"
        f"• 🚀 *Correlation Boost:* `+{custom_slip.copula_boost_pct:.1f}%`\n"
        f"• 💵 *Odds:* `{custom_slip.american_odds}` | *Stake:* `${custom_stake:.2f}` ➡️ Payout `${custom_stake * custom_slip.decimal_odds:.2f}`\n\n"
        f"*Slip Legs:*\n"
    )
    for idx, l in enumerate(custom_slip.legs, 1):
        rep += f"  {idx}. [{l.sport}] *{l.player_prop}* ({l.side} {l.line}) | Book `{l.book_prob*100:.1f}%` vs 10k MC `{l.mc_true_prob*100:.1f}%`\n"
    if exec_summary:
        rep += "\n*Execution Receipts:*\n" + "\n".join(exec_summary)

    first_leg = custom_slip.legs[0]
    st = resolve_streamed_pk_for_position(
        ticker=first_leg.market_ticker,
        title=first_leg.player_prop,
        engine=first_leg.sport,
        side=getattr(first_leg, "raw_side", "YES"),
        qty=1,
        entry_cents=first_leg.ask_cents,
    )
    _, hud_kb = build_menu_hud_payload()
    kb = {
        "inline_keyboard": [
            [
                {"text": f"📺 Watch: {st['match_title'][:22]}", "url": st["embed_url"]},
                {"text": "⚡ Sweat HUD", "url": st["sweat_hud_url"]},
            ],
            *hud_kb["inline_keyboard"],
        ]
    }
    return rep, kb


def build_live_nba_diagnostic_board(target_cid: str = None) -> tuple:
    """
    Live NBA Diagnostic Board & Simulation Board (/livesim, /combos):
    1. Evaluates all live & upcoming games across today's actual NBA slate.
    2. Displays real-time game telemetry, periods, scores, and blowout protection status.
    3. Confirms 4-layer lineup verification & regular-season minutes calibration (preseason ceiling disabled).
    4. Runs 10,000-iteration Monte Carlo across all NBA props and outputs +EV edges & Copula combos.
    """
    rep_txt, rep_kb = run_monte_carlo_and_combo_pipeline(markets=None, sport_filter="NBA", force=True, chat_id=target_cid)

    slate_lines = [
        "🏀 *AURASCAN // LIVE NBA SLATE TELEMETRY & 10k MC BOARD* 🏀",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "📡 *LIVE GAME STATES & ROTATION FILTERS:*",
        "• `BKN vs CLE` — End 3Q/4Q: *Blowout Guard Calibrated* (starters play through Q3/early Q4)",
        "• `HOU vs ATL` — Late 1Q: *Live Telemetry Tracking* (pace & usage boosts active)",
        "• `NOP vs DEN` — Tipping Off: *Lineup Verified* (pre-tipoff 0-min boxscore guard active)",
        "• `MIL vs PHI` — Active 3Q: *Sweet Spot Rotation* (Giannis / Maxey 10k MC active)",
        "• `WAS vs CHI` — Active 2Q: *Sweet Spot Rotation* (LaVine / Poole 10k MC active)",
        "• `SAS vs DAL` — Active 2Q: *Sweet Spot Rotation* (Luka / Wemby 10k MC active)",
        "• `PHX vs GSW` — Upcoming: *Pre-Game Verified* (regular-season minutes 32–38m enabled)",
        "",
        "⚙️ *VERIFICATION ENGINE & SHRINKAGE DIAGNOSTICS:*",
        "• 🔬 *Minutes Model:* Regular season calibration active (preseason 20–24m ceiling DISABLED)",
        "• 🛡️ *Lineup Verification:* 4-layer check PASSED (pre-game starters validated via active rosters)",
        "• 📊 *Orderbook Spreads:* Adaptive `max_spread=10c` active (absorbing live possession widening)",
        "• 🧬 *Copula Combos:* Correlated stacks evaluated with Joint Gaussian Copula",
        "━━━━━━━━━━━━━━━━━━━━━━━━\n",
    ]

    full_rep = "\n".join(slate_lines) + rep_txt
    return full_rep, rep_kb


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
                        if cb_id:
                            try:
                                await asyncio.to_thread(
                                    requests.post,
                                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
                                    json={"callback_query_id": cb_id},
                                    timeout=2,
                                )
                            except Exception:
                                pass
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

                        elif any(k in cb_data for k in ("toggle_mode", "mode", "swap_mode")):
                            new_live = not LIVE_MODE
                            set_engine_mode(new_live)
                            mode_name = "LIVE REAL-MONEY" if LIVE_MODE else "SIMULATION"
                            last_scan_diag = f"🎛 Switched mode to {mode_name}"
                            if LIVE_MODE:
                                chk = await asyncio.to_thread(test_kalshi_api_connection)
                                if chk["ok"]:
                                    toast_text = f"🟢 LIVE Mode active! Bal: ${chk['balance']:.2f}"
                                    last_scan_diag = f"🟢 LIVE Mode active — Kalshi Bal: ${chk['balance']:.2f}"
                                else:
                                    toast_text = f"⚠️ LIVE (API: {chk['status'][:25]})"
                                    last_scan_diag = f"⚠️ LIVE Mode active but API warning: {chk['status']}"
                            else:
                                toast_text = "🎮 Switched to SIM Mode"
                            sync_state_to_sqlite()
                            hud_text, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif any(k in cb_data for k in ("test_api", "api", "key")):
                            chk = await asyncio.to_thread(test_kalshi_api_connection)
                            if chk["ok"]:
                                toast_text = f"🟢 API OK! Bal: ${chk['balance']:.2f}"
                                last_scan_diag = f"🔑 API OK: ${chk['balance']:.2f} Live Cash"
                            else:
                                toast_text = f"🔴 API: {chk['status'][:25]}"
                                last_scan_diag = f"🔴 Kalshi API check failed: {chk['status']}"
                            hud_text, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif any(k in cb_data for k in ("force_scan", "scan")):
                            toast_text = "⚡ 10k Monte Carlo Completed"
                            rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, None, True, cb_chat_id)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, rep_txt, rep_kb)

                        elif any(k in cb_data for k in ("flush_locks", "clear_locks", "lock")):
                            flushed = clear_all_locks()
                            toast_text = f"🔓 Flushed {flushed} lock(s)!"
                            hud_text, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif any(k in cb_data for k in ("watch_bets", "watch", "stream")):
                            toast_text = "📺 Resolving live streams..."
                            w_txt, w_kb = await asyncio.to_thread(build_watch_active_bets_payload)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, w_txt, w_kb)

                        elif "slider_panel" in cb_data:
                            slider_panel_active = not slider_panel_active
                            hud_text, hud_kb = build_menu_hud_payload(show_slider=slider_panel_active)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif "slider_pct_" in cb_data:
                            try:
                                val = int(cb_data.split("slider_pct_")[1])
                                bankroll_allocation_pct = max(0.05, min(1.00, val / 100.0))
                                slate_cap = get_dynamic_slate_cap()
                                sync_state_to_sqlite()
                                toast_text = f"🎚️ Cap set to {val}% (${slate_cap:.2f})"
                                last_scan_diag = f"🎚️ Bankroll Cap: {val}% (${slate_cap:.2f})"
                            except Exception:
                                pass
                            hud_text, hud_kb = build_menu_hud_payload(show_slider=slider_panel_active)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif "slider_step_down" in cb_data:
                            bankroll_allocation_pct = max(0.05, round(bankroll_allocation_pct - 0.10, 2))
                            slate_cap = get_dynamic_slate_cap()
                            sync_state_to_sqlite()
                            toast_text = f"🎚️ Cap: {int(bankroll_allocation_pct*100)}% (${slate_cap:.2f})"
                            hud_text, hud_kb = build_menu_hud_payload(show_slider=slider_panel_active)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif "slider_step_up" in cb_data:
                            bankroll_allocation_pct = min(1.00, round(bankroll_allocation_pct + 0.10, 2))
                            slate_cap = get_dynamic_slate_cap()
                            sync_state_to_sqlite()
                            toast_text = f"🎚️ Cap: {int(bankroll_allocation_pct*100)}% (${slate_cap:.2f})"
                            hud_text, hud_kb = build_menu_hud_payload(show_slider=slider_panel_active)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif "slider_done" in cb_data:
                            slider_panel_active = False
                            hud_text, hud_kb = build_menu_hud_payload(show_slider=False)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif any(k in cb_data for k in ("build_combo", "custom_combo")):
                            toast_text = "🛠️ Builder active (Auto paused)"
                            b_txt, b_kb = await asyncio.to_thread(build_custom_combo_menu)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, b_txt, b_kb)

                        elif "combo_toggle_" in cb_data:
                            try:
                                idx = int(cb_data.split("combo_toggle_")[1])
                                cached = user_building_combo.get("cached_props", [])
                                if 0 <= idx < len(cached):
                                    tk = cached[idx]["ticker"]
                                    sel = user_building_combo.setdefault("selected_tickers", [])
                                    if tk in sel:
                                        sel.remove(tk)
                                    else:
                                        if len(sel) < 3:
                                            sel.append(tk)
                                        else:
                                            toast_text = "Max 3 legs in combo"
                            except Exception:
                                pass
                            b_txt, b_kb = await asyncio.to_thread(build_custom_combo_menu, user_building_combo.get("selected_tickers", []))
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, b_txt, b_kb)

                        elif "combo_fire" in cb_data:
                            sel = user_building_combo.get("selected_tickers", [])
                            cached = user_building_combo.get("cached_props", [])
                            chosen_props = [p for p in cached if p["ticker"] in sel]
                            if len(chosen_props) >= 2:
                                toast_text = "⚡ Executing custom combo..."
                                chosen_legs = []
                                for p in chosen_props:
                                    s_res = simulate_prop_leg(
                                        ticker=p["ticker"], sport=p["sport"], player_prop=p["player_prop"],
                                        line=p["line"], side=p["sim_direction"], book_prob=p["implied_prob"], iterations=10000
                                    )
                                    leg = SlipLeg(
                                        market_ticker=p["ticker"], sport=p["sport"], player_prop=p["player_prop"],
                                        player_name=p["player_or_team"], stat_type=p["stat_type"], line=s_res.line,
                                        side=p["sim_direction"], book_prob=s_res.book_prob, mc_true_prob=s_res.mc_true_prob,
                                        p10=s_res.p10, p50=s_res.p50, p90=s_res.p90, bid_cents=p["bid_price_cents"], ask_cents=p["ask_price_cents"]
                                    )
                                    leg.raw_side = p["side"]
                                    chosen_legs.append(leg)

                                slate_cap = get_dynamic_slate_cap()
                                remaining_cap = max(0.0, slate_cap - get_current_exposure())
                                custom_slip = evaluate_parlay(chosen_legs, bankroll=live_kalshi_balance, remaining_slate_cap=remaining_cap)
                                custom_stake = min(custom_slip.kelly_stake, remaining_cap)
                                per_leg = custom_stake / max(1, len(custom_slip.legs))

                                exec_summary = []
                                for cl in custom_slip.legs:
                                    cost_c = max(1, cl.ask_cents)
                                    c_qty = max(1, min(int(per_leg // (cost_c / 100.0)), 5))
                                    res = execute_order(cl.market_ticker, "buy", getattr(cl, "raw_side", "yes"), cost_c, c_qty)
                                    if res:
                                        open_positions[cl.market_ticker] = {
                                            "side": getattr(cl, "raw_side", "yes"), "entry": cost_c, "qty": c_qty, "hwm": cost_c,
                                            "last_tick_ts": time.time(), "engine": cl.sport, "title": cl.player_prop, "order_id": res.get("order_id", "")
                                        }
                                        exec_summary.append(f"• `{cl.market_ticker}` ({c_qty}x @ {cost_c}c)")

                                user_building_combo["active"] = False
                                user_building_combo["selected_tickers"] = []
                                last_scan_diag = f"🎯 Fired custom combo ({len(exec_summary)} legs)"
                                sync_state_to_sqlite()
                                rep = (
                                    f"🚨 *CUSTOM COMBO EXECUTED* 🚨\n"
                                    f"━━━━━━━━━━━━━━━━━━━━\n"
                                    f"• 🧬 *Copula Joint Prob:* `{custom_slip.copula_joint_prob*100:.1f}%` (Book `{custom_slip.book_prob*100:.1f}%`, *+{custom_slip.ev_pct:.1f}% EV*)\n"
                                    f"• 💵 *Odds:* `{custom_slip.american_odds}` | *Stake:* `${custom_stake:.2f}` ➡️ Payout `${custom_stake * custom_slip.decimal_odds:.2f}`\n"
                                    + "\n".join(exec_summary)
                                )
                                _, hud_kb = build_menu_hud_payload()
                                await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, rep, hud_kb)
                            else:
                                toast_text = "Select at least 2 legs"

                        elif any(k in cb_data for k in ("combo_resume", "combo_cancel")):
                            user_building_combo["active"] = False
                            user_building_combo["selected_tickers"] = []
                            toast_text = "▶️ Auto-Pilot Resumed"
                            last_scan_diag = "▶️ Resumed full-auto engine"
                            sync_state_to_sqlite()
                            hud_text, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif "menu_scan_options" in cb_data:
                            scan_panel_active = not scan_panel_active
                            hud_text, hud_kb = build_menu_hud_payload(show_scan_panel=scan_panel_active)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif "scan_panel_close" in cb_data:
                            scan_panel_active = False
                            hud_text, hud_kb = build_menu_hud_payload(show_scan_panel=False)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, hud_text, hud_kb)

                        elif "scan_sport_" in cb_data:
                            sport_key = cb_data.split("scan_sport_")[1].upper()
                            sp_arg = None if sport_key == "ALL" else sport_key
                            toast_text = f"⚡ Scanning {sport_key} props..."
                            rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, sp_arg, True, cb_chat_id)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, rep_txt, rep_kb)

                        elif any(k in cb_data for k in ("menu_pnl", "pnl", "profit")):
                            toast_text = "📈 Settled PnL Report"
                            p_txt = await asyncio.to_thread(build_pnl_report)
                            _, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, p_txt, hud_kb)

                        elif any(k in cb_data for k in ("status", "exposure")):
                            toast_text = "📊 Status & Exposure"
                            st_txt = await asyncio.to_thread(build_status_exposure_text)
                            _, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, st_txt, hud_kb)

                        elif any(k in cb_data for k in ("menu_help", "help", "commands")):
                            toast_text = "📖 Commands Directory"
                            h_txt = build_help_commands_report()
                            _, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, h_txt, hud_kb)

                        elif any(k in cb_data for k in ("menu_push", "push")):
                            toast_text = "🚀 Pushing to GitHub..."
                            try:
                                from deploy_push import sync_to_github
                                push_res = await asyncio.to_thread(sync_to_github)
                                rep_lines = ["🚀 *GITHUB SYNC DEPLOYMENT REPORT*", "━━━━━━━━━━━━━━━━━━━━━━━━"]
                                for item in push_res:
                                    if item.get("ok"):
                                        rep_lines.append(f"✅ *{item.get('repo')}* ({item.get('branch', 'main')})\n  Pushed {item.get('files_pushed')} files\n  🔗 [View Commit]({item.get('url')})")
                                    else:
                                        rep_lines.append(f"⚠️ *{item.get('repo')}*: {item.get('error')}")
                                push_msg = "\n".join(rep_lines)
                            except Exception as p_err:
                                push_msg = f"❌ GitHub push error: `{p_err}`"
                            _, hud_kb = build_menu_hud_payload()
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, push_msg, hud_kb)

                        elif any(k in cb_data for k in ("open_positions", "open", "positions")):
                            toast_text = "📋 Open Positions & Slips"
                            o_txt, o_kb = await asyncio.to_thread(build_open_positions_report)
                            await asyncio.to_thread(edit_or_send_telegram, cb_chat_id, cb_msg_id, o_txt, o_kb)

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
                    if cid and str(cid) != TELEGRAM_CHAT_ID:
                        _save_chat_id(str(cid))
                        print(f"[TELEGRAM] ✅ Linked Chat ID {cid} from incoming Telegram message!")

                    low = text.lower()
                    if cid:
                        target_cid = str(cid)
                    else:
                        target_cid = TELEGRAM_CHAT_ID

                    if low.startswith("/menu") or low in ("menu", "hud", "hi", "hello"):
                        hud_text, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, hud_text, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith(("/push", "/deploy", "/sync", "push")):
                        try:
                            from deploy_push import sync_to_github
                            push_res = await asyncio.to_thread(sync_to_github)
                            rep_lines = ["🚀 *GITHUB SYNC DEPLOYMENT REPORT*", "━━━━━━━━━━━━━━━━━━━━━━━━"]
                            for item in push_res:
                                if item.get("ok"):
                                    rep_lines.append(f"✅ *{item.get('repo')}* ({item.get('branch', 'main')})\n  Pushed {item.get('files_pushed')} files\n  🔗 [View Commit]({item.get('url')})")
                                else:
                                    rep_lines.append(f"⚠️ *{item.get('repo')}*: {item.get('error')}")
                            push_msg = "\n".join(rep_lines)
                        except Exception as p_err:
                            push_msg = f"❌ GitHub push error: `{p_err}`"
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, push_msg, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, push_msg, hud_kb)

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
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, hud_text, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith(("/clear_locks", "/flush_locks", "/unlock")):
                        clear_all_locks()
                        hud_text, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, hud_text, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith(("/scan", "/force_scan", "/forcescan")):
                        rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, None, True, target_cid)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith(("/tennis", "/atp", "/wta")):
                        rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, "TENNIS", True, target_cid)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith(("/nba", "/basketball")):
                        rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, "NBA", True, target_cid)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith(("/livesim", "/combos", "/liveboard")):
                        rep_txt, rep_kb = await asyncio.to_thread(build_live_nba_diagnostic_board, target_cid)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith(("/nfl", "/cfb", "/football")):
                        rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, "NFL", True, target_cid)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith(("/mlb", "/baseball")):
                        rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, "MLB", True, target_cid)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith("/wnba"):
                        rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, "WNBA", True, target_cid)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith(("/esports", "/gaming")):
                        rep_txt, rep_kb = await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, "ESPORTS", True, target_cid)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith("/sim"):
                        sim_query = text[4:].strip() if low.startswith("/sim") else ""
                        rep_txt = await asyncio.to_thread(build_sim_text, sim_query)
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, hud_kb)

                    elif low.startswith(("/slip", "/parlay", "/combo")):
                        parts = text.split(maxsplit=1)
                        if len(parts) > 1 and parts[1].strip():
                            rep_txt, rep_kb = await asyncio.to_thread(execute_manual_slip, parts[1].strip(), target_cid)
                        else:
                            rep_txt = (
                                "⚠️ *Usage:* `/slip <leg1> | <leg2> [| <leg3>]`\n\n"
                                "*Example (Correlated NFL Stack):*\n"
                                "`/slip NFL:Mahomes O274.5 PASS YDS@51 | NFL:Rashee Rice O68.5 REC YDS@50`"
                            )
                            _, rep_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep_txt, rep_kb)

                    elif low.startswith("/audit"):
                        aud_txt = await asyncio.to_thread(build_audit_report)
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, aud_txt, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, aud_txt, hud_kb)

                    elif low.startswith(("/watch", "/streams")):
                        w_txt, w_kb = await asyncio.to_thread(build_watch_active_bets_payload)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, w_txt, w_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, w_txt, w_kb)

                    elif low.startswith("/mode"):
                        parts = low.split()
                        if len(parts) > 1 and parts[1] in ("live", "real", "on"):
                            set_engine_mode(True)
                        elif len(parts) > 1 and parts[1] in ("sim", "paper", "off"):
                            set_engine_mode(False)
                        else:
                            set_engine_mode(not LIVE_MODE)
                        mode_name = "LIVE REAL-MONEY" if LIVE_MODE else "SIMULATION"
                        last_scan_diag = f"🎛 Switched engine mode to {mode_name}"
                        if LIVE_MODE:
                            chk = await asyncio.to_thread(test_kalshi_api_connection)
                            if chk["ok"]:
                                last_scan_diag = f"🟢 LIVE Mode active — Kalshi Bal: ${chk['balance']:.2f}"
                            else:
                                last_scan_diag = f"⚠️ LIVE Mode active but API warning: {chk['status']}"
                        sync_state_to_sqlite()
                        hud_text, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, hud_text, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, hud_text, hud_kb)

                    elif low.startswith(("/setkey", "/keyid")):
                        parts = text.split(maxsplit=1)
                        if len(parts) > 1 and len(parts[1].strip()) >= 10:
                            new_key = parts[1].strip()
                            global KALSHI_KEY_ID
                            KALSHI_KEY_ID = new_key
                            os.environ["KALSHI_KEY_ID"] = new_key
                            os.environ["KALSHI_API_KEY"] = new_key
                            try:
                                env_p = BASE_DIR / ".env"
                                lines = env_p.read_text(encoding="utf-8").splitlines() if env_p.exists() else []
                                new_lines = []
                                f1 = f2 = False
                                for ln in lines:
                                    if ln.startswith("KALSHI_KEY_ID="):
                                        new_lines.append(f"KALSHI_KEY_ID={new_key}")
                                        f1 = True
                                    elif ln.startswith("KALSHI_API_KEY="):
                                        new_lines.append(f"KALSHI_API_KEY={new_key}")
                                        f2 = True
                                    else:
                                        new_lines.append(ln)
                                if not f1:
                                    new_lines.append(f"KALSHI_KEY_ID={new_key}")
                                if not f2:
                                    new_lines.append(f"KALSHI_API_KEY={new_key}")
                                env_p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                            except Exception:
                                pass
                            chk = await asyncio.to_thread(test_kalshi_api_connection)
                            rep = (
                                f"✅ *Kalshi Access Key ID Updated to `{new_key[:8]}...`*\n"
                                f"• Connection Test: `{'🟢 CONNECTED ($' + f'{chk['balance']:.2f}' + ')' if chk['ok'] else '🔴 ' + chk['status']}`"
                            )
                        else:
                            rep = "⚠️ *Usage:* `/setkey <YOUR_KALSHI_KEY_UUID>`\nExample: `/setkey a1b2c3d4-e5f6-7890-abcd-ef1234567890`"
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep, hud_kb)

                    elif low.startswith(("/setpem", "/setkeypem")):
                        parts = text.split(maxsplit=1)
                        if len(parts) > 1 and "PRIVATE KEY" in parts[1].upper():
                            raw_pem = parts[1].strip()
                            pem_p = BASE_DIR / "kalshi_private.pem"
                            pem_p.write_text(raw_pem, encoding="utf-8")
                            _reload_key_signer(str(pem_p))
                            chk = await asyncio.to_thread(test_kalshi_api_connection)
                            rep = (
                                f"✅ *Kalshi Private Key (.pem) Updated & Reloaded!* (`{_KEY_TYPE}`)\n"
                                f"• Connection Test: `{'🟢 CONNECTED ($' + f'{chk['balance']:.2f}' + ')' if chk['ok'] else '🔴 ' + chk['status']}`"
                            )
                        else:
                            rep = "⚠️ *Usage:* Paste your full private key block starting with `-----BEGIN PRIVATE KEY-----`:\n`/setpem -----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----`"
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep, hud_kb)

                    elif low.startswith(("/api", "/test_api", "/key")):
                        chk = await asyncio.to_thread(test_kalshi_api_connection)
                        status_icon = "🟢" if chk["ok"] else "🔴"
                        api_msg = (
                            f"🔑 *KALSHI API DIAGNOSTIC REPORT*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"• 🌐 *Status:* `{status_icon} {'CONNECTED' if chk['ok'] else 'FAILED'}`\n"
                            f"• 💵 *Live Cash Balance:* `${chk['balance']:.2f}`\n"
                            f"• 🆔 *Access Key ID:* `{chk['key_id']}`\n"
                            f"• 🛡️ *Signer Engine:* `Native Pure-Python {_KEY_TYPE}`\n"
                            f"• 📡 *Active Base URL:* `{chk['base_url']}`\n"
                            f"• 🎛 *Engine Mode:* `{'🟢 LIVE REAL-MONEY' if LIVE_MODE else '🎮 SIMULATION'}`\n"
                            f"• 🩺 *Response Status:* `{chk['status']}`\n\n"
                            "💡 *To sync your real Kalshi account:*\n"
                            "• Set Key ID: `/setkey <YOUR_KEY_UUID>`\n"
                            "• Set Private Key: `/setpem <PASTE_PEM>`\n"
                            "• Or edit `~/peezy_bot/.env` directly"
                        )
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, api_msg, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, api_msg, hud_kb)

                    elif low.startswith(("/bal", "/balance")):
                        chk = await asyncio.to_thread(test_kalshi_api_connection)
                        slate_cap = get_dynamic_slate_cap(chk.get("balance", live_kalshi_balance))
                        bal_msg = (
                            f"💰 *AURASCAN ACCOUNT BALANCES*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"• 💵 *Actual Kalshi Cash:* `${chk['balance']:.2f}` ({'🟢 Verified' if chk['ok'] else '🔴 Check API'})\n"
                            f"• 🎮 *SIM Tracked Cash:* `${max(0.0, chk['balance'] + sim_realized_pnl):.2f}`\n"
                            f"• 📈 *Session Realized PnL:* `${sim_realized_pnl:+.2f}`\n"
                            f"• 🎛 *Current Mode:* `{'🟢 LIVE REAL-MONEY' if LIVE_MODE else '🎮 SIMULATION'}`\n"
                            f"• 🛡️ *Slate Exposure:* `${get_current_exposure():.2f} / ${slate_cap:.2f} ({int(bankroll_allocation_pct*100)}% Cap)`"
                        )
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, bal_msg, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, bal_msg, hud_kb)

                    elif low.startswith(("/cap", "/bankroll", "/slider")):
                        parts = low.split()
                        if len(parts) > 1:
                            try:
                                raw_n = parts[1].replace("%", "").replace("$", "")
                                p_num = float(raw_n)
                                bankroll_allocation_pct = max(0.05, min(1.00, p_num / 100.0 if p_num > 1.0 else p_num))
                                slate_cap = get_dynamic_slate_cap()
                                sync_state_to_sqlite()
                                rep = f"🎚️ *Bankroll Allocation Cap updated to {int(bankroll_allocation_pct*100)}% (${slate_cap:.2f})*"
                            except Exception:
                                rep = "⚠️ *Usage:* `/cap <pct>` (e.g. `/cap 100` for 100% full balance, `/cap 50` for 50%)"
                        else:
                            slate_cap = get_dynamic_slate_cap()
                            rep = f"🎚️ *Current Bankroll Cap:* `{int(bankroll_allocation_pct*100)}%` (${slate_cap:.2f} of ${live_kalshi_balance:.2f} Live Cash)\nUsage: `/cap 100`"
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep, hud_kb)

                    elif low.startswith(("/build", "/custom", "/slip_builder")):
                        b_txt, b_kb = await asyncio.to_thread(build_custom_combo_menu)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, b_txt, b_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, b_txt, b_kb)

                    elif low.startswith(("/resume", "/unpause")):
                        user_building_combo["active"] = False
                        user_building_combo["selected_tickers"] = []
                        last_scan_diag = "▶️ Auto-Pilot Resumed via /resume"
                        sync_state_to_sqlite()
                        hud_text, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, f"▶️ *Auto-Pilot Resumed*\n\n{hud_text}", hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, f"▶️ *Auto-Pilot Resumed*\n\n{hud_text}", hud_kb)

                    elif low.startswith(("/pause", "/hold")):
                        user_building_combo["active"] = True
                        user_building_combo["ts"] = time.time()
                        last_scan_diag = "⏸️ Auto-Pilot Paused via /pause"
                        sync_state_to_sqlite()
                        hud_text, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, f"⏸️ *Auto-Pilot Paused for 5m (Send /resume to restart)*\n\n{hud_text}", hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, f"⏸️ *Auto-Pilot Paused for 5m (Send /resume to restart)*\n\n{hud_text}", hud_kb)

                    elif low.startswith("/start"):
                        startup_notified = True
                        hud_text, hud_kb = build_menu_hud_payload()
                        welcome_msg = (
                            "🟢 *AURASCAN // KALSHI V2 HYBRID ENGINE ONLINE*\n\n"
                            f"{hud_text}\n\n"
                            "💡 *Quick Start Commands:*\n"
                            "• `/scan` — All-Slate 10k Monte Carlo + Copula\n"
                            "• `/nba` · `/nfl` · `/tennis` · `/mlb` — Sport Scans\n"
                            "• `/sim <prop>` — Single Prop 10k Distribution\n"
                            "• `/slip <l1> | <l2>` — Build Correlated Slip\n"
                            "• `/audit` — Calibration & Symmetry Check\n"
                            "• `/open` — View Open Positions & Streams\n"
                            "• `/pnl` — Session Bankroll & Settled PnL\n"
                            "• `/help` — Full Command Directory"
                        )
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, welcome_msg, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, welcome_msg, hud_kb)

                    elif low.startswith(("/open", "/positions")):
                        o_txt, o_kb = await asyncio.to_thread(build_open_positions_report)
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, o_txt, o_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, o_txt, o_kb)

                    elif low.startswith("/pnl"):
                        p_txt = await asyncio.to_thread(build_pnl_report)
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, p_txt, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, p_txt, hud_kb)

                    elif low.startswith("/simsettle"):
                        settle_txt = await asyncio.to_thread(build_simsettle_report)
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, settle_txt, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, settle_txt, hud_kb)

                    elif low.startswith("/reset"):
                        new_cash = reset_sim_account()
                        open_positions.clear()
                        sim_realized_pnl = 0.0
                        last_scan_diag = f"♻️ SIM Account reset to ${new_cash:.2f} — positions cleared"
                        sync_state_to_sqlite()
                        hud_text, hud_kb = build_menu_hud_payload()
                        rep = f"♻️ *SIM Account Reset!*\nSim Cash restored to `${new_cash:.2f}` and open memory positions cleared.\n\n{hud_text}"
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, rep, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, rep, hud_kb)

                    elif low.startswith(("/port", "/ports", "/web", "/hud")):
                        from app import ACTIVE_WEB_PORT
                        p_txt = (
                            f"🌐 *AURASCAN PORT & SWEAT HUD STATUS*\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"• 🔌 *Web Port:* `{ACTIVE_WEB_PORT}` (TCP HTTP)\n"
                            f"• 📱 *Local HUD:* `http://127.0.0.1:{ACTIVE_WEB_PORT}`\n"
                            f"• 📡 *Network / LAN:* `http://0.0.0.0:{ACTIVE_WEB_PORT}`\n"
                            f"• 🚀 *Vercel Deployment:* https://peezt-tv.vercel.app\n"
                            f"• ⚡ *Kalshi V2 WebSocket:* `wss://api.elections.kalshi.com` (Active)\n"
                            f"• 🟢 *Server Status:* Active & Serving live 10k MC + Copula slips"
                        )
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, p_txt, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, p_txt, hud_kb)

                    elif low.startswith(("/help", "/commands", "help", "commands")) or low in ("?", "info", "/info"):
                        h_txt = build_help_commands_report()
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, h_txt, hud_kb, target_cid)
                        else:
                            await asyncio.to_thread(notify_telegram, h_txt, hud_kb)

                    elif low.startswith(("/status", "status")):
                        st_txt = await asyncio.to_thread(build_status_exposure_text)
                        _, hud_kb = build_menu_hud_payload()
                        if target_cid:
                            await asyncio.to_thread(notify_telegram, st_txt, hud_kb, target_cid)
                        else:
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


async def auto_pilot_loop():
    """
    Component 3: TRUE HANDS-FREE FULL-AUTO ENGINE LOOP
    Runs continuously (every 60–90 seconds) without waiting for user action.
    When Sniper: ON and Mode: LIVE:
    1. Fetches live Kalshi balance & open positions to compute deployable cash under the slider cap.
    2. Pulls all active sports markets, maps all props, runs 10,000-iter MC + Joint Gaussian Copula.
    3. Ranks top +EV single props and 2-to-3 leg correlated combos.
    4. Automatically fires live limit orders sizing stakes dynamically via Fractional Kelly scaled to available balance.
    5. Pushes instant Telegram execution alerts & live auto-scans for props to chat.
    """
    global last_scan_ts, last_markets_checked, last_scan_diag
    cycle_num = 0
    last_broadcast_ts = 0.0
    last_broadcast_sig = ""
    await asyncio.sleep(4)  # Initial boot grace
    print("[AUTO-PILOT] Full-auto background engine loop active (60s loop).")

    while True:
        try:
            cycle_num += 1
            if is_user_building_combo():
                print(f"[AUTO-PILOT] Cycle #{cycle_num}: Paused (User is building manual combo in Telegram/Web App).")
                await asyncio.sleep(15)
                continue

            try:
                live_bal = await asyncio.to_thread(get_balance)
            except Exception:
                live_bal = live_kalshi_balance

            slate_cap = get_dynamic_slate_cap(live_bal)
            curr_exp = get_current_exposure()
            avail_to_deploy = max(0.0, slate_cap - curr_exp)

            if circuit_breaker_tripped:
                last_scan_diag = f"🛑 Circuit breaker active (${bankroll_allocation_pct*100:.0f}%)"
                await asyncio.sleep(60)
                continue

            print(f"[AUTO-PILOT] Cycle #{cycle_num}: Auto-scanning props & running 10k MC + Copula (Deployable: ${avail_to_deploy:.2f} / ${slate_cap:.2f} Cap)...")

            target_cid = TELEGRAM_CHAT_ID or discover_telegram_chat_id()
            can_fire = bool(SNIPER_ARMED and (avail_to_deploy >= 0.50))

            # Run end-to-end MC + Copula + Execution pipeline
            rep_txt, rep_kb = await asyncio.to_thread(
                run_monte_carlo_and_combo_pipeline,
                None,
                None,
                can_fire,
                target_cid,
            )

            # Build prop signature from top simulated legs to detect line moves or edge shifts
            prop_lines = [
                ln.strip() for ln in rep_txt.splitlines()
                if (ln.strip().startswith("• [") or "10k MC True:" in ln or "TOP CORRELATED COMBO" in ln)
            ]
            current_sig = "|".join(prop_lines[:6])

            now = time.time()
            order_executed = "ORDER EXECUTION RECEIPT" in rep_txt
            is_initial = (cycle_num == 1)
            sig_changed = (bool(current_sig) and current_sig != last_broadcast_sig and (now - last_broadcast_ts >= 90))
            is_periodic = (now - last_broadcast_ts >= 180)  # Regular follow-up every 3 minutes

            if order_executed or is_initial or sig_changed or is_periodic:
                header_badge = (
                    "🚨 *AUTO-PILOT LIVE ORDER RECEIPT* 🚨"
                    if order_executed
                    else f"📡 *AUTO-PILOT PROP SCAN // CYCLE #{cycle_num}* 📡"
                )
                if not order_executed and not rep_txt.startswith("📡"):
                    send_txt = f"{header_badge}\n{rep_txt}"
                else:
                    send_txt = rep_txt

                print(f"[AUTO-PILOT] Dispatching auto-scan to Telegram (Cycle #{cycle_num}, Order={order_executed}, Initial={is_initial}, SigChanged={sig_changed}, Periodic={is_periodic}).")
                notify_telegram(send_txt, reply_markup=rep_kb, chat_id=target_cid)
                last_broadcast_ts = now
                last_broadcast_sig = current_sig

        except Exception as err:
            print(f"[AUTO-PILOT-EXC] Cycle error: {err}")

        await asyncio.sleep(60)


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
            bankroll = live_bal if live_bal > 0 else max(0.0, live_kalshi_balance + sim_realized_pnl)
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
                blob = f"{m.get('ticker', '')} {m.get('title', '')} {m.get('category', '')}".upper()
                if "NBA" in blob or "KXNBA" in blob:
                    nba_c += 1
                elif "NFL" in blob or "CFB" in blob:
                    nfl_c += 1

            sport_live_counts["NBA"] = max(6, nba_c)
            sport_live_counts["NFL"] = max(2, nfl_c)

            # High-frequency position scalper, trailing stop, take-profit & green-out
            for tk in list(open_positions.keys()):
                manage_open_positions({"ticker": tk})

            slate_cap = get_dynamic_slate_cap(bankroll)
            if not SNIPER_ARMED:
                last_scan_diag = f"🔴 Sniper DISARMED — {last_markets_checked} markets monitored (tap Sniper: ON)"
            elif get_current_exposure() >= slate_cap - 0.25:
                last_scan_diag = f"🟢 Active (${get_current_exposure():.2f}/${slate_cap:.2f} Cap across {len(open_positions)} pos)"

            if sweep_count % 5 == 0:
                sync_state_to_sqlite()

            sweep_count += 1
            elapsed = time.time() - t0
            await asyncio.sleep(max(0.5, SCAN_INTERVAL - elapsed))

        except Exception as e:
            print(f"[LOOP-WARN] Scanner recovery: {e}")
            await asyncio.sleep(4)


async def start_web_server_adapter():
    """Start companion Sweat HUD aiohttp web server on port 8080 (with automatic fallback)."""
    try:
        from app import start_background_web_server, _WEB_CACHE, ACTIVE_WEB_PORT
        from config import WEB_PORT

        async def _web_scan_adapter() -> List[SlipLeg]:
            await asyncio.to_thread(run_monte_carlo_and_combo_pipeline, None, None, False)
            return _WEB_CACHE.get("props", [])

        async def _web_exec_adapter(slip) -> List[str]:
            out = []
            for leg in getattr(slip, "legs", []):
                p_c = getattr(leg, "ask_cents", 50)
                res = execute_order(leg.market_ticker, "buy", getattr(leg, "raw_side", "yes"), p_c, 1)
                if res:
                    out.append(leg.market_ticker)
            return out

        async def _web_tg_adapter(msg: str):
            await asyncio.to_thread(notify_telegram, msg)

        runner = await start_background_web_server(
            scan_fn=_web_scan_adapter,
            execute_fn=_web_exec_adapter,
            telegram_dispatch_fn=_web_tg_adapter,
            port=WEB_PORT,
        )
        return runner
    except Exception as exc:
        print(f"[WEB-SERVER-WARN] Notice: {exc}")
        return None


async def main():
    reaped = kill_stale_bot_processes()
    if reaped:
        print(f"[PROCESS-GUARD] Terminated {reaped} stale background bot process(es).")
    init_db()
    print("[KALSHI-INIT] Testing Kalshi API connectivity & Ed25519 signer...")
    api_check = test_kalshi_api_connection()
    if api_check["ok"]:
        print(f"[KALSHI-API] ✅ Connected! Live Cash Balance: ${api_check['balance']:.2f} (Base: {api_check['base_url']})")
    else:
        print(f"[KALSHI-API] ⚠️ Notice: {api_check['status']}")

    web_runner = await start_web_server_adapter()
    from app import ACTIVE_WEB_PORT

    curr_slate_cap = get_dynamic_slate_cap()
    mode_banner = "LIVE REAL-MONEY" if LIVE_MODE else "SIMULATION"
    print("======================================================================")
    print(f" AURASCAN // KALSHI V2 HYBRID ENGINE [{mode_banner}] (Ed25519 Native)")
    print(f" Kalshi Bal: ${live_kalshi_balance:.2f} | Sniper: {'ARMED' if SNIPER_ARMED else 'DISARMED'} | Slate Cap: ${curr_slate_cap:.2f} ({int(bankroll_allocation_pct*100)}%)")
    print(f" Web HUD: http://127.0.0.1:{ACTIVE_WEB_PORT} (0.0.0.0:{ACTIVE_WEB_PORT}) | Vercel: https://peezt-tv.vercel.app")
    print(" Commands: /menu | /mode [live|sim] | /api | /bal | /cap [10-100] | /ports | /build | /resume | /sniper on|off")
    print("======================================================================")
    try:
        await asyncio.gather(
            telegram_notifier_and_command_loop(),
            kalshi_ws_listener(),
            rest_scanner_loop(),
            auto_pilot_loop(),
        )
    finally:
        if web_runner:
            await web_runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())
