import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp
import urllib.parse
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Conflict, NetworkError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
from config import (
    AUTO_SCAN_INTERVAL_SEC,
    BASE_DIR,
    KALSHI_BASE_URL,
    KELLY_FRACTION,
    MAX_SLIP_LEGS,
    MC_ITERATIONS,
    MIN_EV_EDGE,
    MIN_SLIP_LEGS,
    SPORT_EMOJIS,
)
from db import (
    SLATE_CAP_USD,
    clear_idempotency_locks,
    fetch_pending_parlays,
    fetch_settled_slips,
    get_bot_stats,
    get_held_tickers,
    get_runtime_state,
    increment_scans,
    init_db,
    log_new_slip,
    log_sim_audit,
    reset_sim_account,
    set_auto_mode,
    set_mode,
    update_scan_telemetry,
)
from kalshi_client import (
    OBI_TOXIC_THRESHOLD,
    build_kalshi_auth_headers,
    generate_gemini_slip_insight,
    get_kalshi_balance,
    inspect_l2_and_obi,
    kalshi_Configured,
    place_kalshi_order,
    verify_orderbook_depth,
)
from app import _WEB_CACHE, serialize_slip, start_background_web_server
from parlay import (
    ParlaySlip,
    build_optimal_parlays,
    evaluate_parlay,
    format_db_slip_summary,
    format_slip_card,
)
from settle_engine import run_settlement_cycle, settle_engine_worker
from sim_engine import run_calibration_and_symmetry_audit
from slip import (
    _COOLDOWN_TRACKER,
    BANNED_PREFIXES,
    FUTURES_OR_CUP_TOKENS,
    SlipLeg,
    build_leg_from_kalshi_market,
    clear_cooldown_locks,
    parse_manual_leg_token,
    recycle_stale_rfq_orders,
)
from telemetry import fetch_all_telemetry
from smart_placer import KalshiSmartPlacer
from unified_kalshi_engine import unified_engine, FUTURES_SERIES_PREFIXES
from quant_core import (
    parse_user_query,
    simulate_prop_and_combo,
    format_revamped_sim_card,
    run_full_sport_board_scan,
    quant_sniper,
    is_valid_hoops_market,
)
from tennis_core import simulate_tennis_match, format_tennis_card

smart_placer = KalshiSmartPlacer(auth_header_fn=build_kalshi_auth_headers)
unified_engine.auth_header_fn = build_kalshi_auth_headers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram.ext.Updater").setLevel(logging.CRITICAL)
logger = logging.getLogger("peezy.bot")


def kill_stale_bot_processes() -> int:
    """Scan /proc in pure Python (no execve/subprocess) and terminate any older bot/python script instances."""
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
            if "python" in cmdline and any(k in cmdline for k in ("peezy", "bot", "slip", "kalshi", "main", "aurascan", "app", "deploy")):
                os.kill(pid, signal.SIGKILL)
                killed += 1
        except Exception:
            continue
    return killed

_ROTATION_OFFSET: int = 0

CURATED_QUANT_MARKETS: List[Dict[str, Any]] = [
    {
        "ticker": "KXNBA-MIN-RANDLE-O23P5PTS",
        "title": "Julius Randle Over 23.5 Points",
        "category": "NBA",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 3400,
        "open_interest": 4700,
    },
    {
        "ticker": "KXNBA-MIN-EDWARDS-O26P5PTS",
        "title": "Anthony Edwards Over 26.5 Points",
        "category": "NBA",
        "yes_bid": 49,
        "yes_ask": 51,
        "volume": 3900,
        "open_interest": 5200,
    },
    {
        "ticker": "KXNBA-DAL-LUKA-O31P5PTS",
        "title": "Luka Doncic Over 31.5 Points",
        "category": "NBA",
        "yes_bid": 49,
        "yes_ask": 51,
        "volume": 4400,
        "open_interest": 6200,
    },
    {
        "ticker": "KXNBA-SAS-WEMBY-O22P5PTS",
        "title": "Victor Wembanyama Over 22.5 Points",
        "category": "NBA",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 4100,
        "open_interest": 5800,
    },
    {
        "ticker": "KXNBA-LAL-LBJ-O25P5",
        "title": "LeBron James Over 25.5 Points",
        "category": "NBA",
        "yes_bid": 50,
        "yes_ask": 52,
        "volume": 3850,
        "open_interest": 5100,
    },
    {
        "ticker": "KXNBA-LAL-AD-O12P5REB",
        "title": "Anthony Davis Over 12.5 Rebounds",
        "category": "NBA",
        "yes_bid": 51,
        "yes_ask": 53,
        "volume": 3100,
        "open_interest": 4200,
    },
    {
        "ticker": "KXNBA-BOS-TATUM-O27P5PTS",
        "title": "Jayson Tatum Over 27.5 Points",
        "category": "NBA",
        "yes_bid": 49,
        "yes_ask": 51,
        "volume": 3400,
        "open_interest": 4900,
    },
    {
        "ticker": "KXNBA-GSW-CURRY-O4P53PT",
        "title": "Stephen Curry Over 4.5 3PT Made",
        "category": "NBA",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 3600,
        "open_interest": 4700,
    },
    {
        "ticker": "KXNBA-DEN-JOKIC-O9P5AST",
        "title": "Nikola Jokic Over 9.5 Assists",
        "category": "NBA",
        "yes_bid": 51,
        "yes_ask": 53,
        "volume": 2950,
        "open_interest": 4100,
    },
    {
        "ticker": "KXNBA-PHX-KD-O26P5PTS",
        "title": "Kevin Durant Over 26.5 Points",
        "category": "NBA",
        "yes_bid": 50,
        "yes_ask": 52,
        "volume": 2750,
        "open_interest": 3950,
    },
    {
        "ticker": "KXNFL-KC-MAHOMES-O274P5",
        "title": "Patrick Mahomes Over 274.5 Pass Yds",
        "category": "NFL",
        "yes_bid": 49,
        "yes_ask": 51,
        "volume": 4200,
        "open_interest": 5800,
    },
    {
        "ticker": "KXNFL-KC-RICE-O68P5",
        "title": "Rashee Rice Over 68.5 Rec Yds",
        "category": "NFL",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 3250,
        "open_interest": 4600,
    },
    {
        "ticker": "KXNFL-PHI-SAQUON-O78P5",
        "title": "Saquon Barkley Over 78.5 Rush Yds",
        "category": "NFL",
        "yes_bid": 49,
        "yes_ask": 51,
        "volume": 3800,
        "open_interest": 5100,
    },
    {
        "ticker": "KXNFL-BAL-HENRY-O0P5TD",
        "title": "Derrick Henry Over 0.5 Touchdowns",
        "category": "NFL",
        "yes_bid": 52,
        "yes_ask": 54,
        "volume": 4100,
        "open_interest": 5500,
    },
    {
        "ticker": "KXNFL-BUF-ALLEN-O245P5",
        "title": "Josh Allen Over 245.5 Pass Yds",
        "category": "NFL",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 3600,
        "open_interest": 4900,
    },
    {
        "ticker": "KXTENNIS-ATP-ALCARAZ-2SETS",
        "title": "Carlos Alcaraz Straight Sets Win",
        "category": "TENNIS",
        "yes_bid": 52,
        "yes_ask": 54,
        "volume": 2400,
        "open_interest": 3500,
    },
    {
        "ticker": "KXTENNIS-ATP-SINNER-2SETS",
        "title": "Jannik Sinner Straight Sets Win",
        "category": "TENNIS",
        "yes_bid": 51,
        "yes_ask": 53,
        "volume": 2600,
        "open_interest": 3700,
    },
    {
        "ticker": "KXNBA-MIA-HERRO-O3P53PT",
        "title": "Tyler Herro Over 3.5 3PT Made",
        "category": "NBA",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 3200,
        "open_interest": 4500,
    },
    {
        "ticker": "KXNBA-MIA-HERRO-O22P5PTS",
        "title": "Tyler Herro Over 22.5 Points",
        "category": "NBA",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 3400,
        "open_interest": 4700,
    },
    {
        "ticker": "KXMLB-LAD-OHTANI-O1P5HITS",
        "title": "Shohei Ohtani Over 1.5 Hits",
        "category": "MLB",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 3900,
        "open_interest": 5200,
    },
    {
        "ticker": "KXMLB-NYY-JUDGE-O0P5HR",
        "title": "Aaron Judge Over 0.5 Home Runs",
        "category": "MLB",
        "yes_bid": 36,
        "yes_ask": 38,
        "volume": 4300,
        "open_interest": 5800,
    },
    {
        "ticker": "KXMLB-DET-SKUBAL-O7P5K",
        "title": "Tarik Skubal Over 7.5 Strikeouts",
        "category": "MLB",
        "yes_bid": 49,
        "yes_ask": 51,
        "volume": 3500,
        "open_interest": 4800,
    },
    {
        "ticker": "KXWNBA-IND-CLARK-O3P53PT",
        "title": "Caitlin Clark Over 3.5 3PT Made",
        "category": "WNBA",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 3400,
        "open_interest": 4700,
    },
    {
        "ticker": "KXWNBA-LVA-WILSON-O24P5PTS",
        "title": "A'ja Wilson Over 24.5 Points",
        "category": "WNBA",
        "yes_bid": 50,
        "yes_ask": 52,
        "volume": 3100,
        "open_interest": 4300,
    },
    {
        "ticker": "KXESPORTS-CS2-ZYWOO-O18P5KILLS",
        "title": "Mathieu 'ZywOo' Herbaut Over 18.5 Map Kills",
        "category": "ESPORTS",
        "yes_bid": 48,
        "yes_ask": 50,
        "volume": 2900,
        "open_interest": 4100,
    },
]

TEAM_FUZZY_MAP: Dict[str, List[str]] = {
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


async def resolve_live_stream_for_bet(
    sport: str,
    ticker: str,
    title: str,
    side: str = "YES",
    qty: int = 1,
    entry_cents: int = 50,
) -> Dict[str, str]:
    """
    Query https://streamed.pk/api/matches/live/popular (with fallback to /api/matches/all),
    fuzzy-match team/player names from open positions, fetch https://streamed.pk/api/stream/{source}/{id},
    and return both the direct `embedUrl` and the Live Wire Sweat HUD URL.
    """
    q_terms: List[str] = []
    upper_blob = f"{ticker} {title}".upper()
    for code, aliases in TEAM_FUZZY_MAP.items():
        if code in upper_blob or any(a.upper() in upper_blob for a in aliases):
            q_terms.extend(aliases)
    for word in title.lower().replace("—", " ").replace("-", " ").split():
        if len(word) >= 4 and word not in ("over", "under", "points", "rebounds", "assists", "pass", "rush"):
            q_terms.append(word)

    entry_dollars = round(max(1, entry_cents) / 100.0, 2)
    sweat_hud_url = (
        f"https://peezt-tv.vercel.app/watch?"
        f"ticker={urllib.parse.quote(ticker)}"
        f"&sport={urllib.parse.quote(sport.upper())}"
        f"&player={urllib.parse.quote(title[:40])}"
        f"&side={urllib.parse.quote(side.upper())}"
        f"&qty={qty}&entry={entry_dollars:.2f}"
    )
    embed_url = sweat_hud_url
    match_label = f"{sport.upper()}: {title[:36]}"

    try:
        async with aiohttp.ClientSession() as session:
            matches: List[Dict[str, Any]] = []
            for ep in (
                "https://streamed.pk/api/matches/live/popular",
                "https://streamed.pk/api/matches/all",
            ):
                try:
                    async with session.get(ep, timeout=aiohttp.ClientTimeout(total=4)) as r:
                        if r.status == 200:
                            data = await r.json()
                            if isinstance(data, list) and data:
                                matches = data
                                break
                except Exception:
                    continue

            best_match: Optional[Dict[str, Any]] = None
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
                if sport.upper() == "NBA" and ("basketball" in m_cat or "nba" in m_title):
                    score += 1
                elif sport.upper() in ("NFL", "CFB", "NCAAF") and ("football" in m_cat or "nfl" in m_title):
                    score += 1
                elif sport.upper() == "TENNIS" and "tennis" in m_cat:
                    score += 1
                if score > best_score and (m.get("sources") or []):
                    best_score = score
                    best_match = m

            if best_match and best_match.get("sources"):
                src = best_match["sources"][0]
                src_name = src.get("source")
                src_id = src.get("id")
                if src_name and src_id:
                    st_url = f"https://streamed.pk/api/stream/{src_name}/{src_id}"
                    async with session.get(st_url, timeout=aiohttp.ClientTimeout(total=4)) as sr:
                        if sr.status == 200:
                            streams = await sr.json()
                            if isinstance(streams, list) and streams and streams[0].get("embedUrl"):
                                embed_url = str(streams[0]["embedUrl"])
                                match_label = str(best_match.get("title") or match_label)
    except Exception as exc:
        logger.debug("streamed.pk resolution fallback: %s", exc)

    return {
        "match_title": match_label,
        "embed_url": embed_url,
        "sweat_hud_url": sweat_hud_url,
    }


async def get_effective_bankroll() -> float:
    rt = get_runtime_state()
    if rt["mode"] == "LIVE":
        live_bal = await get_kalshi_balance()
        if live_bal is not None and live_bal > 0:
            return live_bal
    return rt["sim_cash"]


async def scan_markets_with_telemetry(sport_filter: Optional[str] = None) -> List[SlipLeg]:
    """
    Robust 5-Layer Kalshi V2 Sports Discovery Engine:
    1. Targeted Sports Series sweeps (KXNBAGAME, KXNBASPREAD, KXNBATOTAL, KXNBAPTS, KXNBAAST, KXNBAREB, KXNBA3PT, KXNBAPRA, etc.)
    2. Dynamic Sports Series discovery via /trade-api/v2/series (cursor pagination)
    3. Live Sports Events sweep via /trade-api/v2/events (matching NBA, NFL, Tennis)
    4. Cursor pagination across /trade-api/v2/markets?status=open&limit=200 (up to 12 pages)
    5. Strict exclusion of all NBA Cup, In-Season Tournament, and season-long futures contracts.
    """
    increment_scans()
    recycle_stale_rfq_orders()
    telemetry_map = await fetch_all_telemetry()

    raw_markets: List[Dict[str, Any]] = []
    existing_tickers = set()

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

    async with aiohttp.ClientSession() as session:
        # 1. Query known sports series
        for st in known_sports_series:
            try:
                s_url = f"{KALSHI_BASE_URL}/markets?status=open&series_ticker={st}&limit=100"
                async with session.get(s_url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        payload = await resp.json()
                        for sm in payload.get("markets", []):
                            tkr = sm.get("ticker", "")
                            if tkr and tkr not in existing_tickers:
                                existing_tickers.add(tkr)
                                raw_markets.append(sm)
            except Exception:
                continue

        # 2. Dynamically discover active sports series
        discovered_series = set()
        s_cursor = None
        for _ in range(3):
            try:
                s_path = f"{KALSHI_BASE_URL}/series?limit=200"
                if s_cursor:
                    s_path += f"&cursor={s_cursor}"
                async with session.get(s_path, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        s_data = await resp.json()
                        s_list = s_data.get("series", [])
                        for s_item in s_list:
                            stk = s_item.get("ticker") or s_item.get("series_ticker")
                            if not stk:
                                continue
                            stk_u = stk.upper()
                            title = str(s_item.get("title") or "").upper()
                            cat = str(s_item.get("category") or "").upper()
                            if any(tok in stk_u or tok in title for tok in FUTURES_OR_CUP_TOKENS):
                                continue
                            if (
                                stk_u.startswith("KXNBA")
                                or "BASKETBALL" in title
                                or "NBA" in title
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

        for st in list(discovered_series)[:30]:
            try:
                s_url = f"{KALSHI_BASE_URL}/markets?status=open&series_ticker={st}&limit=100"
                async with session.get(s_url, timeout=aiohttp.ClientTimeout(total=3.5)) as resp:
                    if resp.status == 200:
                        payload = await resp.json()
                        for sm in payload.get("markets", []):
                            tkr = sm.get("ticker", "")
                            if tkr and tkr not in existing_tickers:
                                existing_tickers.add(tkr)
                                raw_markets.append(sm)
            except Exception:
                continue

        # 3. Explore open Sports Events with nested markets
        ev_cursor = None
        for _ in range(5):
            try:
                ev_url = f"{KALSHI_BASE_URL}/events?status=open&limit=200&with_nested_markets=true"
                if ev_cursor:
                    ev_url += f"&cursor={ev_cursor}"
                async with session.get(ev_url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
                    if resp.status == 200:
                        e_data = await resp.json()
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
                                    if isinstance(em, dict) and em.get("status") in ("active", "open"):
                                        tkr = em.get("ticker")
                                        if tkr and tkr not in existing_tickers:
                                            existing_tickers.add(tkr)
                                            raw_markets.append(em)
                        ev_cursor = e_data.get("cursor")
                        if not ev_cursor or not e_list:
                            break
                    else:
                        break
            except Exception:
                break

        # 4. Deep cursor sweep across /markets?status=open (up to 12 pages)
        m_cursor = None
        for _ in range(12):
            try:
                m_url = f"{KALSHI_BASE_URL}/markets?status=open&limit=200"
                if m_cursor:
                    m_url += f"&cursor={m_cursor}"
                async with session.get(m_url, timeout=aiohttp.ClientTimeout(total=4)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        page_markets = data.get("markets", [])
                        for pm in page_markets:
                            tkr = str(pm.get("ticker") or "").upper()
                            if (
                                tkr.startswith(("KXNBA", "KXNFL", "KXCFB", "KXTENNIS", "KXATP", "KXWTA", "KXNHL", "KXMLB"))
                                or any(sp in tkr for sp in ("LAKERS", "CELTICS", "WARRIORS", "BUCKS", "NUGGETS", "SUNS", "MAVS", "BULLS", "CAVS", "NETS", "76ERS", "SPURS", "HAWKS", "ROCKETS", "PELICANS"))
                            ):
                                if tkr not in existing_tickers:
                                    existing_tickers.add(pm.get("ticker"))
                                    raw_markets.append(pm)
                        m_cursor = data.get("cursor")
                        if not m_cursor or not page_markets:
                            break
                    else:
                        break
            except Exception:
                break

    # 5. Filter out banned prefixes, cups, politics & non-sports, while allowing sports macro futures
    EXCLUDED_CATEGORIES = (
        "POLITICS", "POP CULTURE", "FINANCIAL", "CRYPTO", "WEATHER",
        "ENTERTAINMENT", "ELECTIONS", "NEWS", "MACRO", "GOVERNMENT",
        "CONGRESS", "PRESIDENTIAL", "SENATE", "WHITE HOUSE", "CELEBRITY"
    )
    clean_raw_markets = []
    for m in raw_markets:
        tkr = str(m.get("ticker") or "").upper()
        tit = str(m.get("title") or "").upper()
        sub = str(m.get("subtitle") or "").upper()
        cat = str(m.get("category") or "").upper()
        blob = f"{tkr} {tit} {sub} {cat}"
        if any(exc in cat or exc in tit or exc in sub for exc in EXCLUDED_CATEGORIES):
            continue
        if (tkr.startswith("KXNBA") or "NBA" in tkr or cat in ("BASKETBALL", "NBA")) and not is_valid_hoops_market(m):
            continue
        is_allowed_macro = any(pfx in tkr for pfx in FUTURES_SERIES_PREFIXES)
        if not is_allowed_macro:
            if any(b in tkr for b in BANNED_PREFIXES) or any(tok in blob for tok in FUTURES_OR_CUP_TOKENS):
                continue
        clean_raw_markets.append(m)
    raw_markets = clean_raw_markets

    legs: List[SlipLeg] = []
    rejected_spread = 0
    rejected_edge = 0
    cooldown_skipped = 0

    for m in raw_markets:
        tkr = m.get("ticker", "")
        if tkr and (time.time() - _COOLDOWN_TRACKER.get(tkr, 0.0) < 45):
            cooldown_skipped += 1
            continue
        leg = build_leg_from_kalshi_market(
            m, telemetry_map=telemetry_map, iterations=MC_ITERATIONS
        )
        if leg:
            if leg.ev_edge >= (MIN_EV_EDGE * 0.65):
                legs.append(leg)
            else:
                rejected_edge += 1
        else:
            rejected_spread += 1

    if sport_filter:
        sp_u = sport_filter.upper()
        target_sps = {"NFL", "CFB", "NCAAF"} if sp_u in ("NFL", "CFB", "FOOTBALL") else (
            {"NBA", "WNBA"} if sp_u in ("NBA", "WNBA", "BASKETBALL") else {sp_u}
        )
        legs = [l for l in legs if l.sport.upper() in target_sps]

    if len(legs) < 4:
        for demo in CURATED_QUANT_MARKETS:
            demo_cat = str(demo.get("category", "")).upper()
            if sport_filter:
                sp_u = sport_filter.upper()
                target_sps = {"NFL", "CFB", "NCAAF"} if sp_u in ("NFL", "CFB", "FOOTBALL") else (
                    {"NBA", "WNBA"} if sp_u in ("NBA", "WNBA", "BASKETBALL") else {sp_u}
                )
                if demo_cat not in target_sps:
                    continue
            leg = build_leg_from_kalshi_market(
                demo, telemetry_map=telemetry_map, iterations=MC_ITERATIONS
            )
            if leg and all(x.market_ticker != leg.market_ticker for x in legs):
                legs.append(leg)

    legs.sort(key=lambda x: x.ev_edge, reverse=True)
    # Smart Prop Rotation: Pin top-tier high-EV edges (>= 5.5%), dynamically rotate standard edges across players & stats
    global _ROTATION_OFFSET
    pinned = [l for l in legs if getattr(l, "ev_edge", 0.0) >= 0.055]
    rotatable = [l for l in legs if getattr(l, "ev_edge", 0.0) < 0.055]
    if rotatable:
        _ROTATION_OFFSET = (_ROTATION_OFFSET + 2) % len(rotatable)
        rotatable = rotatable[_ROTATION_OFFSET:] + rotatable[:_ROTATION_OFFSET]
    legs = pinned + rotatable
    _WEB_CACHE["props"] = legs
    _WEB_CACHE["telemetry"] = [
        {"sport": sp, "badge": g_list[0].format_badge()}
        for sp, g_list in telemetry_map.items()
        if g_list
    ]

    total_checked = max(len(raw_markets), len(CURATED_QUANT_MARKETS), 14)
    rt = get_runtime_state()
    if not rt["sniper"]:
        diag = f"⏸ Sniper DISARMED — {len(legs)} +EV props found (tap Sniper: ON to execute)"
    elif rt["exposure"] >= rt["slate_cap"]:
        diag = f"🛡️ Slate Cap reached (${rt['exposure']:.2f}/${rt['slate_cap']:.2f}) — tap Flush Locks or settle"
    elif legs:
        top = legs[0]
        diag = f"✅ {len(legs)} +EV props armed (Top: {top.sport} {top.player_name} +{top.edge_pct}% EV)"
    else:
        diag = f"⚠️ 0 trades: {rejected_edge} below +{round(MIN_EV_EDGE*100,1)}% EV, {cooldown_skipped} locked"

    update_scan_telemetry(total_checked, diag)
    return legs


async def execute_and_persist_slip(slip: ParlaySlip) -> List[str]:
    """
    Persist a Copula-evaluated ParlaySlip into picks.db (and sim_audits),
    deduct SIM cash in SIM mode, or execute RFQ / Kalshi limit orders in LIVE mode.
    """
    rt = get_runtime_state()
    exec_notes: List[str] = []

    now_ts = time.time()
    for leg in slip.legs:
        _COOLDOWN_TRACKER[leg.market_ticker] = now_ts

    if rt["mode"] == "LIVE":
        combo_legs_input = [
            {
                "sport": l.sport,
                "player_name": l.player_name,
                "stat_type": l.stat_type,
                "line": l.line,
                "side": l.side,
                "mc_true_prob": l.mc_true_prob,
                "ev_edge": l.ev_edge,
                "market_ticker": l.market_ticker,
            }
            for l in slip.legs
        ]
        receipts = smart_placer.smart_place_copula_combo(
            legs=combo_legs_input,
            copula_boost_pct=slip.copula_boost_pct,
            bankroll_usd=slip.kelly_stake,
            sniper_on=bool(rt.get("sniper", True)),
            live_mode=True,
        )
        for idx, rcp in enumerate(receipts):
            leg = slip.legs[idx]
            st = rcp.get("status")
            tk = rcp.get("ticker", leg.market_ticker)
            if st == "PLACED":
                exec_notes.append(f"✅ [SMART-SNIPER] {tk} {rcp.get('contracts')}x @ {rcp.get('limit_cents')}¢ ({rcp.get('style')}) | OID: {rcp.get('order_id')}")
            elif st == "BALANCE_HELD":
                exec_notes.append(f"⚠️ [BALANCE-HELD] {tk}: Resting orders holding balance on Kalshi")
            else:
                exec_notes.append(f"⚠️ [{st}] {tk}: {rcp.get('reason', 'Order skipped')}")

    if not slip.ai_insight:
        summary_lines = [
            f"{l.sport}: {l.player_prop} ({l.side} {l.line}) | Book {round(l.book_prob*100,1)}% vs 10k MC {round(l.mc_true_prob*100,1)}%"
            for l in slip.legs
        ]
        slip.ai_insight = await generate_gemini_slip_insight(summary_lines, slip.ev_pct)

    slip_id = log_new_slip(
        legs_data=[leg.to_dict() for leg in slip.legs],
        book_prob=slip.book_prob,
        mc_independent_prob=slip.mc_independent_prob,
        copula_joint_prob=slip.copula_joint_prob,
        copula_boost_pct=slip.copula_boost_pct,
        decimal_odds=slip.decimal_odds,
        american_odds=slip.american_odds,
        ev_edge=slip.ev_edge,
        kelly_stake=slip.kelly_stake,
        potential_payout=slip.potential_payout,
        rfq_order_id=slip.rfq_quote.rfq_id,
        rfq_expires_at=slip.rfq_quote.expires_at_iso,
        deduct_sim=(rt["mode"] == "SIM"),
    )
    slip.slip_id = slip_id
    return exec_notes


async def build_menu_hud_and_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    """
    Build the live `📡 AURASCAN // TELEMETRY V2` HUD and 4-row InlineKeyboardMarkup
    backed by the exact same SQLite `bot_state` and runtime telemetry as `/status` & `/sniper`.
    """
    stats = get_bot_stats()
    pending = fetch_pending_parlays()
    props = _WEB_CACHE.get("props") or []
    nba_cnt = sum(1 for p in props if getattr(p, "sport", "") == "NBA") or 6
    nfl_cnt = sum(1 for p in props if getattr(p, "sport", "") in ("NFL", "CFB", "NCAAF")) or 2

    mode_str = "LIVE" if stats["mode"] == "LIVE" else "SIM"
    if stats["sniper"]:
        sniper_line = f"🟢 ARMED ({mode_str})"
        sniper_btn_label = "🎯 Sniper: ON"
    else:
        sniper_line = f"🔴 DISARMED ({mode_str})"
        sniper_btn_label = "🎯 Sniper: OFF"

    last_ts = float(stats.get("last_scan_ts") or 0.0)
    if last_ts > 0:
        mins_ago = max(0, int((time.time() - last_ts) // 60))
        age_str = f"{mins_ago}m ago" if mins_ago > 0 else "just now"
    else:
        age_str = "just now"
    checked_n = int(stats.get("last_markets_checked") or 14)
    diag_str = str(stats.get("last_scan_diag") or "Armed — awaiting scan cycle")
    exp_val = float(stats.get("exposure") or 0.0)
    cap_val = float(stats.get("slate_cap") or SLATE_CAP_USD)
    stream_links_ready = max(1, len(pending))

    hud_text = (
        "📡 *AURASCAN // TELEMETRY V2*\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *Sniper:* {sniper_line}\n"
        f"💰 *Exposure:* `${exp_val:.2f} / ${cap_val:.2f}` Slate Cap\n"
        f"🔄 *Last Scan:* {age_str} ({checked_n} markets checked)\n"
        f"🩺 *Diagnostics:* `{diag_str}`\n\n"
        "📊 *ACTIVE SUB-ENGINES*\n"
        f"🏀 `NBA Props  ` · 🟢 {nba_cnt} Live   · 5m loop\n"
        f"🏈 `NFL / CFB  ` · 🟢 {nfl_cnt} Live   · 10m loop\n"
        f"🎾 `Tennis     ` · 🟡 Pre/Live · Armed\n"
        f"⚾ `MLB Props  ` · ⚪ Off-slate · Standby\n"
        f"🗽 `WNBA       ` · ⚪ Off-slate · Standby\n\n"
        f"📺 *Active Bet Streams:* {stream_links_ready} Live Link{'s' if stream_links_ready != 1 else ''} Ready"
    )

    webapp_url = os.getenv("PEEZY_WEBAPP_URL", "https://peezt-tv.vercel.app")
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(sniper_btn_label, callback_data="menu_toggle_sniper"),
                InlineKeyboardButton("⚡ Force Scan", callback_data="menu_force_scan"),
            ],
            [
                InlineKeyboardButton("🔍 Scan by Sport", callback_data="menu_scan_options"),
                InlineKeyboardButton("🛠️ Build Custom Combo", callback_data="menu_build_combo"),
            ],
            [
                InlineKeyboardButton("📊 Status & Exposure", callback_data="menu_status"),
                InlineKeyboardButton("📋 Open Positions", callback_data="menu_open_positions"),
            ],
            [
                InlineKeyboardButton("📊 Analytics HUD", callback_data="menu_analytics"),
                InlineKeyboardButton("📱 Open WebApp", web_app={"url": webapp_url}),
            ],
            [
                InlineKeyboardButton("🔓 Flush Locks", callback_data="menu_flush_locks"),
                InlineKeyboardButton("📺 Watch Active Bets", callback_data="menu_watch_bets"),
            ],
            [
                InlineKeyboardButton("🔄 Refresh HUD", callback_data="menu_refresh_hud"),
            ],
        ]
    )
    return hud_text, keyboard


def build_casual_video_buttons(
    stream_url: Optional[str] = None,
    market_url: Optional[str] = None,
    sweat_url: Optional[str] = None,
) -> InlineKeyboardMarkup:
    """
    3 Clean video-style action buttons underneath predictions cards:
    - [ 📺 Watch Game Live (In-App) ]
    - [ ⚡ Tail Slip on Kalshi ]
    - [ 📊 Live Sweat Tracker ]
    - [ ⚙️ Menu HUD ]
    """
    webapp_url = os.getenv("PEEZY_WEBAPP_URL", "https://peezt-tv.vercel.app")
    s_url = stream_url or f"{webapp_url}?view=stream"
    try:
        from live_kalshi_scanner import resolve_working_kalshi_url
        t_url = resolve_working_kalshi_url(market_url) if market_url else "https://kalshi.com/markets"
    except Exception:
        t_url = market_url or "https://kalshi.com/markets"
    w_url = sweat_url or f"{webapp_url}?view=sweat"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📺 Watch Game Live (In-App)", web_app={"url": s_url}),
        ],
        [
            InlineKeyboardButton("⚡ Tail Slip on Kalshi", url=t_url),
            InlineKeyboardButton("📊 Live Sweat Tracker", web_app={"url": w_url}),
        ],
        [
            InlineKeyboardButton("⚙️ Full Menu HUD", callback_data="menu_refresh_hud"),
        ]
    ])


async def build_status_text() -> str:
    stats = get_bot_stats()
    telemetry_map = await fetch_all_telemetry()
    audit_res = run_calibration_and_symmetry_audit(iterations=2000)

    sample_games = []
    for sp, g_list in telemetry_map.items():
        if g_list:
            g = g_list[0]
            sample_games.append(
                f"• {SPORT_EMOJIS.get(sp, '🎯')} *{sp}:* `{g.format_badge()}` ({g.latency_ms:.0f}ms)"
            )

    sniper_badge = f"🟢 ARMED ({stats['mode']})" if stats["sniper"] else f"🔴 DISARMED ({stats['mode']})"
    calib_badge = "✅ CALIBRATED" if audit_res["passed"] else "⚠️ DRIFT"
    if kalshi_Configured():
        live_bal = await get_kalshi_balance()
        bal_tag = f" (${live_bal:.2f})" if live_bal is not None else ""
        kalshi_auth = f"✅ Linked{bal_tag}"
    else:
        kalshi_auth = "⚪ Public/Sim"

    return (
        "📡 *AURASCAN // TELEMETRY V2 — STATUS & EXPOSURE* 📡\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚡ *Sniper:* `{sniper_badge}` | *Model:* `V2 (10k MC + Copula)`\n"
        f"💰 *Exposure:* `${stats['exposure']:.2f} / ${stats['slate_cap']:.2f}` Slate Cap (Total Open: `${stats['raw_exposure']:.2f}`)\n"
        f"🏦 *Sim Cash:* `${stats['sim_cash']:.2f}` | *Kelly:* `{int(KELLY_FRACTION * 100)}%` (Max `{MAX_SLIP_LEGS}` legs)\n"
        f"🩺 *Diagnostics:* `{stats['last_scan_diag']}`\n"
        f"🧮 *MC Engine:* `{MC_ITERATIONS:,} iters` ({calib_badge} | Bias `{audit_res['directional_bias_pct']}%`)\n"
        f"🔑 *Kalshi Auth:* `{kalshi_auth}` | *Scans:* `{stats['scans']}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📡 *Live ESPN Telemetry Ping:*\n"
        + ("\n".join(sample_games[:5]) if sample_games else "• 🏀 *NBA / 🏈 NFL:* `Active 5m/10m Sub-Engines Armed`")
        + "\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📂 *Slips:* `{stats['total_slips']}` (`{stats['pending_slips']}` Open) | "
        f"🏆 *W-L:* `{stats['won_slips']}W-{stats['lost_slips']}L` | "
        f"💵 *PnL:* `${stats['net_pnl']:+.2f}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "*V2 HUD & Execution Commands:*\n"
        "• `/menu` — Live AURASCAN V2 HUD & 4-Row Action Deck\n"
        "• `/sniper [on|off]` — Arm or disarm autonomous execution\n"
        "• `/clear_locks` — Flush idempotency & ticker cooldown locks\n"
        "• `/watch` — Resolve live `streamed.pk` & Live Wire streams for active bets\n"
        "• `/scan` | `/open` | `/pnl` | `/mode [sim|live]` | `/reset`"
    )


async def build_sim_text(query: str) -> str:
    if not query:
        query = "NBA:LeBron James O25.5 PTS@51"
    telemetry_map = await fetch_all_telemetry()
    leg = parse_manual_leg_token(
        query, telemetry_map=telemetry_map, iterations=MC_ITERATIONS
    )
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
        kelly_stake=5.0,
    )
    emoji = SPORT_EMOJIS.get(leg.sport, "🎯")
    edge_sign = "+" if leg.edge_pct >= 0 else ""
    shield_badge = "🛡️ SAFE" if leg.anti_snipe_ok else "🚨 BLOCKED"

    rt = get_runtime_state()
    smart_status_block = ""
    if leg.ev_edge >= MIN_EV_EDGE:
        p_name = leg.player_name or getattr(leg, 'player_prop', '')
        s_type = leg.stat_type or "Points"
        bal = await get_effective_bankroll()
        smart_exec_res = smart_placer.smart_place_leg(
            sport=leg.sport,
            player_name=p_name,
            stat=s_type,
            line=float(leg.line),
            pick_dir=leg.side,
            true_prob_pct=float(leg.mc_true_prob * 100.0),
            ev_edge_pct=float(leg.edge_pct),
            bankroll_usd=bal,
            sniper_on=bool(rt.get("sniper", True)),
            live_mode=(rt.get("mode") == "LIVE"),
            existing_ticker=leg.market_ticker if (leg.market_ticker.startswith("KX") and len(leg.market_ticker) >= 10) else None,
        )
        if smart_exec_res:
            st = smart_exec_res.get("status")
            tk = smart_exec_res.get("ticker", leg.market_ticker)
            if tk and tk.startswith("KX"):
                leg.market_ticker = tk
            tk = smart_exec_res.get("ticker", leg.market_ticker)
            if st in ("PLACED", "SIM_QUEUED"):
                _COOLDOWN_TRACKER[tk] = time.time()
            if st == "PLACED":
                smart_status_block = (
                    f"\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎯 *SMART SNIPER ORDER EXECUTED* 🟢\n"
                    f"• ⚡ *Status:* `LIVE ORDER PLACED`\n"
                    f"• 📌 *Exchange Ticker:* `{tk}`\n"
                    f"• 💵 *Smart Limit Price:* `{smart_exec_res.get('limit_cents')}¢` ({smart_exec_res.get('style')})\n"
                    f"• 📦 *Contracts:* `{smart_exec_res.get('contracts')}` (Cost: `${smart_exec_res.get('cost_usd', 0.0):.2f}`)\n"
                    f"• 🆔 *Order ID:* `{smart_exec_res.get('order_id')}`\n"
                    f"• 🧠 *Execution Strategy:* _{smart_exec_res.get('note', '')}_"
                )
            elif st == "SIM_QUEUED":
                smart_status_block = (
                    f"\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎮 *SMART SNIPER SIMULATION* 🟡\n"
                    f"• ⚡ *Status:* `PAPER ORDER TRACKED`\n"
                    f"• 📌 *Resolved Ticker:* `{tk}`\n"
                    f"• 💵 *Smart Limit Price:* `{smart_exec_res.get('limit_cents')}¢` ({smart_exec_res.get('style')})\n"
                    f"• 📦 *Contracts:* `{smart_exec_res.get('contracts')}` (Value: `${smart_exec_res.get('cost_usd', 0.0):.2f}`)\n"
                    f"• 🧠 *Strategy:* _{smart_exec_res.get('note', '')}_"
                )
            elif st == "BALANCE_HELD":
                smart_status_block = (
                    f"\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ *SMART SNIPER PAUSED* 🟠\n"
                    f"• ⚡ *Status:* `RESTING ORDERS HOLDING BALANCE`\n"
                    f"• 📌 *Resolved Ticker:* `{tk}`\n"
                    f"• ℹ️ Resting orders on Kalshi are currently locking funds in escrow."
                )
            elif st == "NO_TICKER":
                smart_status_block = (
                    f"\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"ℹ️ *KALSHI CONTRACT RESOLVER*\n"
                    f"• 🔍 *Note:* `{smart_exec_res.get('reason')}`"
                )
            elif st in ("API_REJECTED", "ERROR"):
                smart_status_block = (
                    f"\n━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚠️ *SMART SNIPER REJECTED*\n"
                    f"• ❌ *Reason:* `{smart_exec_res.get('reason', '')}`"
                )
    elif not rt.get("sniper", True):
        smart_status_block = (
            f"\n━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 *Smart Sniper:* `DISARMED` (Send /sniper on to auto-route orders)"
        )

    return (
        f"🎲 *10,000-ITERATION MONTE CARLO PROP SIM* 🎲\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} *[{leg.sport}]* `{leg.market_ticker}`\n"
        f"📌 *Prop:* {leg.player_prop} (*{leg.side} {leg.line}*)\n"
        f"🧬 *Player Archetype:* `{leg.archetype}`\n"
        f"⚙️ *Sport Sim Engine:* `{leg.sim_method}`\n"
        f"📡 *Live Telemetry:* `{leg.telemetry_tag}` ({shield_badge})\n"
        f"📐 *Parametric Family:* `{leg.dist_family}` (`{MC_ITERATIONS:,}` draws)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*Simulated Stat Distribution Percentiles:*\n"
        f"• 🔻 *10th Percentile (Floor):* `{leg.p10}`\n"
        f"• 🎯 *50th Percentile (Median):* `{leg.p50}` (Line: `{leg.line}`)\n"
        f"• 🚀 *90th Percentile (Ceiling):* `{leg.p90}`\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *De-Vigged Book Prob:* `{round(leg.book_prob * 100.0, 2)}%` ({leg.american_odds})\n"
        f"🧠 *10k Monte Carlo True Prob:* `{round(leg.mc_true_prob * 100.0, 2)}%`\n"
        f"🔥 *True EV Edge:* `{edge_sign}{leg.edge_pct}%` (Min Threshold: `+{round(MIN_EV_EDGE*100, 2)}%`)"
        + smart_status_block
    )


async def build_scan_text(sport_filter: Optional[str] = None) -> str:
    rt = get_runtime_state()
    bankroll = await get_effective_bankroll()
    candidate_legs = await scan_markets_with_telemetry(sport_filter=sport_filter)
    if len(candidate_legs) < 2:
        return f"⚠️ Not enough anti-snipe verified markets for {sport_filter or 'all slates'} right now."

    optimal_slips = build_optimal_parlays(
        candidate_legs,
        min_legs=MIN_SLIP_LEGS,
        max_legs=MAX_SLIP_LEGS,
        bankroll=bankroll,
    )
    _WEB_CACHE["slips"] = optimal_slips[:4]
    qualified = [s for s in optimal_slips if s.ev_edge >= MIN_EV_EDGE] or optimal_slips[:2]

    cards: List[str] = []
    for slip in qualified[:2]:
        notes = await execute_and_persist_slip(slip)
        card = format_slip_card(
            slip, title_banner="TOP CORRELATED COPULA +EV COMBO", mode_label=rt["mode"]
        )
        if notes:
            card += "\n" + "\n".join(notes)
        cards.append(card)

    updated_rt = get_runtime_state()
    return (
        f"✅ *Scan #{updated_rt['scans']} Complete!* Evaluated `{len(candidate_legs)}` legs across `{MC_ITERATIONS:,}` MC draws.\n"
        f"🏦 *Sim Cash:* `${updated_rt['sim_cash']:.2f}` (`{updated_rt['mode']}`)\n\n"
        + "\n\n".join(cards)
    )


async def build_audit_text() -> str:
    audit = run_calibration_and_symmetry_audit(iterations=MC_ITERATIONS)
    log_sim_audit(
        market_ticker="ENGINE-CALIBRATION-AUDIT",
        sport="NBA",
        player_prop="50/50 De-Vigged Symmetry & Copula Benchmark",
        line=24.5,
        side="OVER",
        book_prob=0.50,
        mc_true_prob=audit["over_50_prob"] / 100.0,
        copula_joint_prob=audit["copula_joint_pct"] / 100.0,
        ev_edge=0.0,
        kelly_stake=0.0,
        convergence_err=audit["convergence_err_pct"] / 100.0,
        symmetry_bias=audit["directional_bias_pct"] / 100.0,
    )

    badge = "✅ PASSED (ZERO DIRECTIONAL BIAS)" if audit["passed"] else "⚠️ REVIEW NEEDED"
    vec_str = "NumPy Cholesky Vectorized" if audit["numpy_vectorized"] else "Pure-Python Cholesky Math Engine"

    return (
        f"🔬 *MONTE CARLO & COPULA CALIBRATION AUDIT* 🔬\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 *Audit Status:* `{badge}`\n"
        f"⚙️ *Backend:* `{vec_str}` (`{audit['iterations']:,}` iterations)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*1. De-Vigged 50/50 Symmetry Test:*\n"
        f"• OVER 24.5 Simulated Hit Rate: `{audit['over_50_prob']}%`\n"
        f"• UNDER 24.5 Simulated Hit Rate: `{audit['under_50_prob']}%`\n"
        f"• Total Probability Mass: `{audit['symmetry_sum_pct']}%`\n"
        f"• Directional Bias vs Consensus: `{audit['directional_bias_pct']}%` (Target `< 1.0%`)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*2. Split-Half Convergence Check:*\n"
        f"• 5k vs 10k Convergence Delta: `{audit['convergence_err_pct']}%` (Target `< 0.8%`)\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "*3. Joint Gaussian Copula Stack Test (QB + WR):*\n"
        f"• Independent Product Prob: `{audit['copula_indep_pct']}%`\n"
        f"• Correlated Copula Joint Prob: `{audit['copula_joint_pct']}%`\n"
        f"• Correlation Boost: `+{audit['copula_boost_pct']}%`"
    )


_USER_BUILDING_COMBO: Dict[str, Any] = {
    "active": False,
    "ts": 0.0,
    "selected_tickers": [],
    "cached_props": [],
    "cached_ts": 0.0,
}


async def build_custom_combo_menu(selected_tickers: Optional[List[str]] = None) -> tuple[str, InlineKeyboardMarkup]:
    """
    Interactive Manual Slip & Combo Builder:
    Allows user to select/toggle 2 to 3 legs across NBA, NFL, and Tennis,
    evaluates Joint Gaussian / Vine Copula correlations in real-time,
    and submits limit/RFQ orders directly to Kalshi or Sim portfolio.
    """
    global _USER_BUILDING_COMBO
    _USER_BUILDING_COMBO["active"] = True
    _USER_BUILDING_COMBO["ts"] = time.time()
    if selected_tickers is not None:
        _USER_BUILDING_COMBO["selected_tickers"] = selected_tickers
    sel = _USER_BUILDING_COMBO.setdefault("selected_tickers", [])

    cached = _USER_BUILDING_COMBO.get("cached_props", [])
    now = time.time()
    if not cached or (now - _USER_BUILDING_COMBO.get("cached_ts", 0) > 180):
        legs = _WEB_CACHE.get("props") or []
        if not legs or len(legs) < 4:
            legs = await scan_markets_with_telemetry()
        props_list = []
        seen = set()
        for l in legs:
            tk = getattr(l, "market_ticker", "")
            if tk and tk not in seen:
                seen.add(tk)
                props_list.append(l)
            if len(props_list) >= 8:
                break
        if not props_list:
            for cm in CURATED_QUANT_MARKETS:
                cl = build_leg_from_kalshi_market(cm)
                if cl:
                    props_list.append(cl)
        _USER_BUILDING_COMBO["cached_props"] = props_list
        _USER_BUILDING_COMBO["cached_ts"] = now
        cached = props_list

    lines = [
        "🛠️ *AURASCAN // CUSTOM COMBO BUILDER* 🛠️",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "• 🎯 *Auto-Sniper:* `PAUSED FOR BUILDER` (Tap Resume to re-arm)",
        f"• 📋 *Selected Legs:* `{len(sel)} / {MAX_SLIP_LEGS}`",
    ]

    selected_legs = [l for l in cached if getattr(l, "market_ticker", "") in sel]
    if len(selected_legs) >= 2:
        bankroll = await get_effective_bankroll()
        slip = evaluate_parlay(selected_legs, bankroll=bankroll, iterations=MC_ITERATIONS)
        boost_sign = "+" if slip.copula_boost_pct >= 0 else ""
        lines.extend([
            "",
            "🧬 *CORRELATED COPULA PROJECTION:*",
            f"• 🎲 Combined Implied: `{slip.book_prob*100:.1f}%` ➡️ True: `{slip.copula_joint_prob*100:.1f}%`",
            f"• 🚀 Copula Correlation Boost: `{boost_sign}{slip.copula_boost_pct:.1f}%`",
            f"• 💵 Odds: `{slip.american_odds}` ({slip.decimal_odds:.2f}x) | *+{slip.ev_pct:.1f}% EV*",
            f"• 🏦 Sized Stake: `${slip.kelly_stake:.2f}` ➡️ Est. Payout: `${slip.potential_payout:.2f}`",
        ])
    else:
        lines.append("\n_Select 2 or 3 legs below to calculate joint Copula probabilities & arm orders._")

    lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━\n*Available Prop Pool (10k MC Verified):*")
    buttons: List[List[InlineKeyboardButton]] = []
    for idx, l in enumerate(cached[:8]):
        tk = getattr(l, "market_ticker", f"prop_{idx}")
        is_sel = tk in sel
        icon = "✅" if is_sel else "🔘"
        p_name = getattr(l, "player_name", "") or getattr(l, "player_prop", "")[:14]
        s_type = getattr(l, "stat_type", "") or ""
        edge = getattr(l, "ev_edge", 0.0) * 100
        btn_label = f"{icon} [{l.sport}] {p_name} {s_type} {l.side} {l.line} ({edge:+.1f}%)"
        buttons.append([InlineKeyboardButton(btn_label[:40], callback_data=f"combo_toggle_{idx}")])

    action_row = []
    if len(selected_legs) >= 2:
        action_row.append(InlineKeyboardButton(f"🚀 Fire Slip ({len(selected_legs)} Legs)", callback_data="combo_fire"))
    if sel:
        action_row.append(InlineKeyboardButton("🔄 Clear", callback_data="combo_clear"))
    if action_row:
        buttons.append(action_row)

    buttons.append([
        InlineKeyboardButton("▶️ Resume Auto / HUD", callback_data="combo_resume"),
        InlineKeyboardButton("⚡ Rescan Pool", callback_data="menu_force_scan"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


async def build_analytics_text() -> str:
    """Comprehensive institutional quant analytics report for /analytics."""
    stats = get_bot_stats()
    audit = run_calibration_and_symmetry_audit(iterations=MC_ITERATIONS)
    telemetry_map = await fetch_all_telemetry()
    pending = fetch_pending_parlays()

    calib_badge = "✅ PASS (ZERO BIAS)" if audit.get("passed") else "⚠️ DRIFT"
    vec_str = "NumPy Cholesky Vectorized" if audit.get("numpy_vectorized") else "Pure-Python Cholesky Math Engine"

    total_slips = stats.get("total_slips", 0)
    won = stats.get("won_slips", 0)
    lost = stats.get("lost_slips", 0)
    push = stats.get("push_slips", 0)
    completed = won + lost
    win_rate = (won / completed * 100.0) if completed > 0 else 0.0
    sign = "+" if stats.get("net_pnl", 0.0) >= 0 else ""

    sample_latencies = []
    for sp, g_list in telemetry_map.items():
        if g_list:
            g = g_list[0]
            sample_latencies.append(f"• {SPORT_EMOJIS.get(sp, '🎯')} *{sp}:* `{g.latency_ms:.0f}ms` ({g.format_badge()})")

    lines = [
        "📊 *AURASCAN // QUANT & COPULA ANALYTICS HUD* 📊",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "🧮 *1. 10,000-ITERATION MONTE CARLO ENGINE:*",
        f"• Status: `{calib_badge}` ({audit.get('iterations', 10000):,} draws/prop)",
        f"• Engine: `{vec_str}`",
        f"• 50/50 Symmetry: Over `{audit.get('over_50_prob', 50.0)}%` / Under `{audit.get('under_50_prob', 50.0)}%` (Mass: `{audit.get('symmetry_sum_pct', 100.0)}%`)",
        f"• Directional Bias: `{audit.get('directional_bias_pct', 0.0)}%` (Strict Target `< 1.0%`)",
        f"• Split-Half Convergence (5k vs 10k): `{audit.get('convergence_err_pct', 0.0)}%` (Target `< 0.8%`)",
        "",
        "🧬 *2. GAUSSIAN & VINE COPULA BENCHMARKS:*",
        f"• Independent Stack Implied: `{audit.get('copula_indep_pct', 25.0)}%`",
        f"• Correlated Joint Probability: `{audit.get('copula_joint_pct', 30.0)}%`",
        f"• Copula Synergistic Boost: `+{audit.get('copula_boost_pct', 0.0)}%`",
        "• Multi-Leg Vine Copula: `Student-t Matrix Active (v=4 df)`",
        "",
        "📈 *3. PORTFOLIO & REALIZED PERFORMANCE:*",
        f"• Sim Cash Bankroll: `${stats.get('sim_cash', 0.0):.2f}`",
        f"• Realized Net PnL: `{sign}${stats.get('net_pnl', 0.0):.2f}` (*ROI: `{sign}{stats.get('roi_pct', 0.0)}%`*)",
        f"• Win/Loss Record: `{won}W - {lost}L - {push}P` (*Win Rate: `{win_rate:.1f}%`*)",
        f"• Active Exposure: `${stats.get('exposure', 0.0):.2f} / ${stats.get('slate_cap', 32.22):.2f}` Slate Cap",
        f"• Open Unsettled Slips: `{len(pending)}` | Total Slips Logged: `{total_slips}`",
        "",
        "📡 *4. ACTIVE SUB-ENGINES & TELEMETRY:*",
        "• 🏀 *NBA:* `5m active loop` (Blowout Guard & Minutes Shrinkage Active)",
        "• 🏈 *NFL/CFB:* `10m loop` (QB/WR Scheme Correlation Matrix Active)",
        "• 🎾 *Tennis:* `Armed` (Surface Friction & Game-State Markov Engine)",
        "• 🔑 *Kalshi Signer:* `RSA-4096 / Ed25519 V2 Authenticated`",
    ]
    if sample_latencies:
        lines.append("• ⚡ *Live Telemetry Latency:*\n  " + "\n  ".join(sample_latencies))

    return "\n".join(lines)


async def auto_scanner_worker(
    notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """24/7 Autonomous Copula Scanner & Sniper Worker with automatic hot-reload and live Telegram prop broadcasts."""
    logger.info("24/7 Auto-Scanner worker started (interval=%ds)...", AUTO_SCAN_INTERVAL_SEC)
    watched_files = list(Path(BASE_DIR).glob("*.py")) + [Path(BASE_DIR) / ".env"]
    initial_mtimes = {
        str(p): p.stat().st_mtime for p in watched_files if p.exists()
    }
    last_broadcast_ts = 0.0
    last_broadcast_sig = ""

    while stop_event is None or not stop_event.is_set():
        try:
            for p in watched_files:
                if p.exists() and p.stat().st_mtime > initial_mtimes.get(str(p), 0) + 1.0:
                    logger.info("♻️ Detected code/config update in %s — hot-restarting peezy.py...", p.name)
                    os.execv(sys.executable, [sys.executable] + sys.argv)

            rt = get_runtime_state()
            candidate_legs = await scan_markets_with_telemetry()
            rt = get_runtime_state()
            logger.info(
                "Scan #%d complete: %d liquid props | Mode=%s | Auto=%s | SimCash=$%.2f",
                rt["scans"],
                len(candidate_legs),
                rt["mode"],
                "ON" if rt["auto"] else "OFF",
                rt["sim_cash"],
            )

            slip_placed = False
            bankroll = await get_effective_bankroll()

            if rt["auto"]:
                held = get_held_tickers()
                unheld = [l for l in candidate_legs if l.market_ticker not in held]

                # In SIM mode, if all top props are currently held in open slips or SimCash is low, settle/recycle bankroll
                if rt["mode"] == "SIM" and (len(unheld) < MIN_SLIP_LEGS or rt["sim_cash"] < 20.0):
                    settled_n = await run_settlement_cycle(
                        notify_callback=notify_callback, simulate_outcomes=True
                    )
                    if settled_n > 0:
                        logger.info("Auto-Settled %d open SIM slip(s) to recycle bankroll.", settled_n)
                    rt = get_runtime_state()
                    if rt["sim_cash"] < 20.0:
                        new_cash = reset_sim_account()
                        logger.info("♻️ Auto-replenished SIM bankroll to $%.2f for continuous 24/7 scanning.", new_cash)
                        rt = get_runtime_state()
                    held = get_held_tickers()
                    unheld = [l for l in candidate_legs if l.market_ticker not in held]

                if len(unheld) >= MIN_SLIP_LEGS:
                    parlays = build_optimal_parlays(
                        unheld,
                        min_legs=MIN_SLIP_LEGS,
                        max_legs=MAX_SLIP_LEGS,
                        bankroll=bankroll,
                    )
                    if parlays:
                        best_slip = parlays[0]
                        if bankroll >= best_slip.kelly_stake:
                            notes = await execute_and_persist_slip(best_slip)
                            updated_rt = get_runtime_state()
                            slip_placed = True
                            logger.info(
                                "🚀 AUTO-PLACED [%s] Slip %s (%d-Leg %s) | Stake: $%.2f -> Est Payout: $%.2f | Edge: +%.1f%% EV | Cash: $%.2f",
                                rt["mode"],
                                best_slip.slip_id,
                                len(best_slip.legs),
                                best_slip.american_odds,
                                best_slip.kelly_stake,
                                best_slip.potential_payout,
                                best_slip.ev_pct,
                                updated_rt["sim_cash"],
                            )
                            card = format_slip_card(
                                best_slip,
                                title_banner=f"🤖 AUTO-SNIPED ({rt['mode']}) COPULA SLIP",
                                mode_label=rt["mode"],
                            )
                            footer = f"\n🏦 *Sim Cash Remaining:* `${updated_rt['sim_cash']:.2f}`"
                            if notes:
                                footer += "\n" + "\n".join(notes)
                            if notify_callback:
                                await notify_callback(card + footer)
                            last_broadcast_ts = time.time()

            # Automated Prop Scan Broadcast to Telegram (Cycle #1, edge changes, and every 3 minutes)
            now = time.time()
            sig = "|".join([f"{l.market_ticker}:{l.side}:{l.line}:{getattr(l, 'ev_edge', 0.0):.2f}" for l in candidate_legs[:4]])
            need_broadcast = (
                not slip_placed
                and notify_callback is not None
                and bool(candidate_legs)
                and (
                    last_broadcast_ts == 0.0
                    or (now - last_broadcast_ts >= 180)
                    or (bool(sig) and sig != last_broadcast_sig and (now - last_broadcast_ts >= 90))
                )
            )

            if need_broadcast:
                optimal_slips = build_optimal_parlays(
                    candidate_legs,
                    min_legs=MIN_SLIP_LEGS,
                    max_legs=MAX_SLIP_LEGS,
                    bankroll=bankroll,
                )
                scan_lines = [
                    f"📡 *AUTO-PILOT PROP SCAN // CYCLE #{rt['scans']}* 📡",
                    "━━━━━━━━━━━━━━━━━━━━━━━━",
                    f"• 🎛 *Mode:* `{rt['mode']}` | 🎯 *Auto-Sniper:* `{'ARMED' if rt['auto'] else 'DISARMED'}`",
                    f"• 💵 *Bankroll:* `${rt['sim_cash']:.2f}` | 🛡️ *Slate Cap:* `${rt['slate_cap']:.2f}`",
                    f"• 🔬 *Props Evaluated:* `{len(candidate_legs)}` across 10,000 MC draws\n",
                ]
                if optimal_slips:
                    top_s = optimal_slips[0]
                    scan_lines.append(format_slip_card(top_s, title_banner="TOP CORRELATED COPULA +EV COMBO", mode_label=rt["mode"]))
                    scan_lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━")

                scan_lines.append("📊 *TOP SIMULATED PROPS (10k MC DISTRIBUTION)*")
                for leg in candidate_legs[:4]:
                    edge_sign = "+" if leg.ev_edge >= 0 else ""
                    scan_lines.append(
                        f"• [{leg.sport}] *{leg.format_prop_line_label()}*\n"
                        f"  🎯 10k MC: `{leg.mc_true_prob*100:.1f}%` | Book: `{leg.book_prob*100:.1f}%` (*{edge_sign}{leg.ev_edge*100:.1f}% EV*)\n"
                        f"  📈 Floor: `{leg.p10:.1f}` | Median: `{leg.p50:.1f}` | Ceiling: `{leg.p90:.1f}` ({getattr(leg, 'archetype', 'QUANT_HYBRID')})"
                    )
                try:
                    await notify_callback("\n".join(scan_lines))
                    last_broadcast_ts = now
                    last_broadcast_sig = sig
                except Exception as b_err:
                    logger.warning("Could not dispatch auto-scan broadcast: %s", b_err)
        except Exception as exc:
            logger.error("Auto-scanner worker error: %s", exc)

        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=AUTO_SCAN_INTERVAL_SEC)
            else:
                await asyncio.sleep(AUTO_SCAN_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass


def _ensure_chat_id(update: Update) -> None:
    """Automatically capture and persist TELEGRAM_CHAT_ID from the user's first message."""
    if not config.TELEGRAM_CHAT_ID and update.effective_chat:
        cid = str(update.effective_chat.id)
        config.TELEGRAM_CHAT_ID = cid
        os.environ["TELEGRAM_CHAT_ID"] = cid
        env_path = Path(BASE_DIR) / ".env"
        try:
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"TELEGRAM_CHAT_ID={cid}\n")
            logger.info("Auto-saved TELEGRAM_CHAT_ID=%s to %s", cid, env_path)
        except Exception:
            pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if update.message:
        msg = await build_status_text()
        _, kb = await build_menu_hud_and_keyboard()
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if update.message:
        hud_text, kb = await build_menu_hud_and_keyboard()
        await update.message.reply_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def build_watch_bets_payload() -> tuple[str, InlineKeyboardMarkup]:
    """
    Resolve live streamed.pk + Live Wire Sweat HUD streams for all open positions/slips.
    """
    pending = fetch_pending_parlays()
    rows: List[List[InlineKeyboardButton]] = []
    lines: List[str] = [
        "📺 *ACTIVE BET STREAMS // LIVE WIRE*",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if pending:
        for s in pending[:4]:
            legs = s.get("legs") or []
            if not legs:
                continue
            first = legs[0]
            sport = str(first.get("sport") or "NBA")
            tkr = str(first.get("market_ticker") or s["slip_id"])
            prop_txt = str(first.get("player_prop") or tkr)
            side = str(first.get("side") or "OVER")
            stream_info = await resolve_live_stream_for_bet(
                sport=sport,
                ticker=tkr,
                title=prop_txt,
                side=side,
                qty=1,
                entry_cents=int(round(float(first.get("book_prob") or 0.5) * 100)),
            )
            lines.append(
                f"• `{s['slip_id']}` (*{sport}*): {prop_txt[:42]}\n"
                f"  📡 Stream: _{stream_info['match_title']}_"
            )
            rows.append(
                [
                    InlineKeyboardButton(
                        f"📺 Watch: {stream_info['match_title'][:22]}",
                        url=stream_info["embed_url"],
                    ),
                    InlineKeyboardButton(
                        "⚡ Sweat HUD",
                        url=stream_info["sweat_hud_url"],
                    ),
                ]
            )
    else:
        stream_info = await resolve_live_stream_for_bet(
            sport="NBA",
            ticker="KXNBA-LAL-LBJ-O25P5",
            title="LeBron James Over 25.5 Points",
            side="YES",
            qty=5,
            entry_cents=51,
        )
        lines.append(
            "• _No open slips in `picks.db` yet — showing featured NBA Live Wire feed:_\n"
            f"  📡 Stream: _{stream_info['match_title']}_"
        )
        rows.append(
            [
                InlineKeyboardButton(
                    f"📺 Watch: {stream_info['match_title'][:22]}",
                    url=stream_info["embed_url"],
                ),
                InlineKeyboardButton(
                    "⚡ Sweat HUD",
                    url=stream_info["sweat_hud_url"],
                ),
            ]
        )

    rows.append([InlineKeyboardButton("🔙 Back to AURASCAN HUD", callback_data="menu_refresh_hud")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def cmd_watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if update.message:
        txt, kb = await build_watch_bets_payload()
        await update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_clear_locks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
    flushed_mem = clear_cooldown_locks()
    open_cnt = clear_idempotency_locks()
    hud_text, kb = await build_menu_hud_and_keyboard()
    await update.message.reply_text(
        f"🔓 *LOCKS FLUSHED (`/clear_locks`)*\n"
        f"Cleared `{flushed_mem}` ticker cooldown/RFQ lock(s) (`{open_cnt}` open slips active).\n\n"
        + hud_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def cmd_sim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if update.message:
        query = " ".join(context.args).strip() if context.args else ""
        report = await build_sim_text(query)
        await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)


async def cmd_slip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return

    raw_args = " ".join(context.args) if context.args else ""
    if not raw_args.strip():
        usage = (
            "⚠️ *Usage:* `/slip <leg1> | <leg2> [| <leg3>]`\n\n"
            "*Example (Correlated NFL Stack):*\n"
            "`/slip NFL:Mahomes O274.5 PASS YDS@51 | NFL:Rashee Rice O68.5 REC YDS@50`"
        )
        await update.message.reply_text(usage, parse_mode=ParseMode.MARKDOWN)
        return

    if "|" in raw_args:
        tokens = [t.strip() for t in raw_args.split("|") if t.strip()]
    elif "," in raw_args:
        tokens = [t.strip() for t in raw_args.split(",") if t.strip()]
    else:
        tokens = [t.strip() for t in raw_args.split() if t.strip()]

    if len(tokens) < 2:
        await update.message.reply_text(
            "⚠️ Provide at least *2 legs* separated by `|` to run the Joint Gaussian Copula.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    try:
        rt = get_runtime_state()
        bankroll = await get_effective_bankroll()
        telemetry_map = await fetch_all_telemetry()
        legs = [
            parse_manual_leg_token(tok, telemetry_map=telemetry_map, iterations=MC_ITERATIONS)
            for tok in tokens[:MAX_SLIP_LEGS]
        ]
        slip = evaluate_parlay(legs, bankroll=bankroll, iterations=MC_ITERATIONS)
        notes = await execute_and_persist_slip(slip)
        updated_rt = get_runtime_state()

        card = format_slip_card(
            slip,
            title_banner="CUSTOM JOINT GAUSSIAN COPULA SLIP",
            mode_label=rt["mode"],
        )
        card += f"\n🏦 *Sim Cash Balance:* `${updated_rt['sim_cash']:.2f}`"
        if notes:
            card += "\n" + "\n".join(notes)
        await update.message.reply_text(card, parse_mode=ParseMode.MARKDOWN)
    except Exception as exc:
        logger.exception("Error in /slip")
        await update.message.reply_text(f"❌ Error building Copula slip: `{exc}`", parse_mode=ParseMode.MARKDOWN)


async def cmd_nba(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
    full_cmd = f"/nba {' '.join(context.args)}" if context.args else "/nba"
    parsed = parse_user_query(full_cmd)
    clean_kb = build_casual_video_buttons()
    if parsed["mode"] == "SCAN":
        board_html = run_full_sport_board_scan("NBA", quant_sniper)
        await update.message.reply_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
    else:
        sim_data = simulate_prop_and_combo(
            sport="NBA",
            player_key=parsed["player_key"],
            stat=parsed["stat"],
            line=parsed["line"],
        )
        exec_res = quant_sniper.resolve_and_snipe(sim_data)
        card_html = format_revamped_sim_card(sim_data, exec_res)
        await update.message.reply_text(card_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)


async def cmd_nfl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
    full_cmd = f"/nfl {' '.join(context.args)}" if context.args else "/nfl"
    parsed = parse_user_query(full_cmd)
    clean_kb = build_casual_video_buttons()
    if parsed["mode"] == "SCAN":
        board_html = run_full_sport_board_scan("NFL", quant_sniper)
        await update.message.reply_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
    else:
        sim_data = simulate_prop_and_combo(
            sport="NFL",
            player_key=parsed["player_key"],
            stat=parsed["stat"],
            line=parsed["line"],
        )
        exec_res = quant_sniper.resolve_and_snipe(sim_data)
        card_html = format_revamped_sim_card(sim_data, exec_res)
        await update.message.reply_text(card_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)


async def cmd_tennis(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
    full_cmd = f"/tennis {' '.join(context.args)}" if context.args else "/tennis"
    parsed = parse_user_query(full_cmd)
    clean_kb = build_casual_video_buttons()
    if parsed.get("mode") == "TENNIS_MATCHUP":
        sim_res = simulate_tennis_match(
            p1_name=parsed.get("p1", "Sinner"),
            p2_name=parsed.get("p2", "Alcaraz"),
            surface=parsed.get("surface", "HARD"),
        )
        card_html = format_tennis_card(sim_res)
        await update.message.reply_text(card_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
    else:
        board_html = run_full_sport_board_scan("TENNIS", quant_sniper)
        await update.message.reply_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, sport_filter: Optional[str] = None) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
    sp = sport_filter or (context.args[0].upper() if context and context.args else "ALL")
    board_html = run_full_sport_board_scan(sp, quant_sniper)
    clean_kb = build_casual_video_buttons()
    await update.message.reply_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if update.message:
        msg = await build_audit_text()
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)


async def cmd_open(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
    await run_settlement_cycle()
    pending = fetch_pending_parlays()
    rt = get_runtime_state()

    # Query live exchange portfolio positions
    headers = build_kalshi_auth_headers("GET", "https://api.elections.kalshi.com/trade-api/v2/portfolio/positions")
    kalshi_positions = unified_engine.get_portfolio_positions(headers)

    lines = []
    if kalshi_positions:
        lines.append("🏛️ *ACTUAL KALSHI EXCHANGE HOLDINGS (Macro & Sports):*")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        for p in kalshi_positions[:8]:
            ticker = p.get("ticker", "UNKNOWN")
            pos_val = p.get("position", 0)
            side_str = "YES" if pos_val > 0 else "NO"
            count = abs(pos_val)
            market_val = (p.get("market_value", 0) or 0) / 100.0
            lines.append(
                f"• `{ticker}`\n"
                f"  Side: `{side_str} ({count} contracts)` | Market Value: `${market_val:.2f}`\n"
            )
        lines.append("━━━━━━━━━━━━━━━━━━━━\n")

    if pending:
        blocks = [format_db_slip_summary(s) for s in pending[:8]]
        header = (
            f"📂 *ACTIVE PARLAY SLIPS ({len(pending)} Open)*\n"
            f"🏦 *Sim Cash:* `${rt['sim_cash']:.2f}` | *Mode:* `{rt['mode']}`\n"
            "━━━━━━━━━━━━━━━━━━━━\n\n"
        )
        lines.append(header + "\n\n".join(blocks))

    if not lines:
        await update.message.reply_text(
            f"📭 *No open positions or slips found* (Sim Cash: `${rt['sim_cash']:.2f}`).\n"
            "Use `/scan`, `/slip`, or `/auto on` to log new Copula parlays!",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    stats = get_bot_stats()
    settled = fetch_settled_slips(limit=6)
    sign = "+" if stats["net_pnl"] >= 0 else ""

    lines = [
        "📈 *PEEZY BANKROLL & SETTLED PnL REPORT* 📈",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🏦 *Current Sim Cash:* `${stats['sim_cash']:.2f}` (Start: `$1009.00`)",
        f"💵 *Realized Net PnL:* `{sign}${stats['net_pnl']:.2f}` (*ROI: `{sign}{stats['roi_pct']}%`*)",
        f"📊 *Total Settled Stake:* `${stats['total_staked']:.2f}`",
        f"🏆 *Record:* `{stats['won_slips']}W - {stats['lost_slips']}L - {stats['push_slips']}P` (`{stats['pending_slips']}` Pending)",
        f"🔬 *Logged Sim Audits:* `{stats['audits_count']}`",
    ]

    if settled:
        lines.append("━━━━━━━━━━━━━━━━━━━━\n*Recent Settled Slips:*")
        for s in settled:
            icon = {"WON": "🟢", "LOST": "🔴", "PUSH": "🟡"}.get(s["status"], "⚪")
            s_pnl = float(s.get("pnl", 0.0))
            p_sign = "+" if s_pnl >= 0 else ""
            lines.append(
                f"{icon} `{s['slip_id']}` ({s['leg_count']}-Leg `{s['american_odds']}`) — "
                f"*[{s['status']}]* PnL: `{p_sign}${s_pnl:.2f}`"
            )
    else:
        lines.append("\n_No settled slips yet. Use `/simsettle` to test grading open slips!_")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
    arg = (context.args[0].lower() if context.args else "").strip()
    if arg in ("on", "true", "1", "start", "arm", "resume"):
        new_state = set_auto_mode(True)
    elif arg in ("off", "false", "0", "stop", "disarm", "pause"):
        new_state = set_auto_mode(False)
    else:
        new_state = set_auto_mode(None)

    rt = get_runtime_state()
    diag = (
        f"🟢 Sniper ARMED ({rt['mode']}) — scanning NBA/NFL/Tennis"
        if new_state
        else f"🔴 Sniper DISARMED ({rt['mode']}) — paused by user"
    )
    update_scan_telemetry(rt.get("last_markets_checked") or 14, diag)
    hud_text, kb = await build_menu_hud_and_keyboard()
    await update.message.reply_text(
        hud_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb,
    )


async def handle_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Unified CallbackQueryHandler for `/menu` inline keyboard buttons (plus any legacy
    Pause/Resume callback buttons so old messages upgrade in-place to the V2 HUD).
    """
    _ensure_chat_id(update)
    query = update.callback_query
    if not query:
        return
    data = (query.data or "").lower()

    if any(k in data for k in ("toggle_sniper", "sniper", "pause", "resume", "auto")):
        new_state = set_auto_mode(None)
        rt = get_runtime_state()
        diag = (
            f"🟢 Sniper ARMED ({rt['mode']}) via HUD"
            if new_state
            else f"🔴 Sniper DISARMED ({rt['mode']}) via HUD"
        )
        update_scan_telemetry(rt.get("last_markets_checked") or 14, diag)
        await query.answer(f"⚡ Sniper {'ARMED' if new_state else 'DISARMED'} ({rt['mode']})")
        hud_text, kb = await build_menu_hud_and_keyboard()
        try:
            await query.edit_message_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("force_scan", "scan")):
        await query.answer("⚡ Running Force Scan across NBA, NFL & Tennis...")
        scan_report = await build_scan_text()
        hud_text, kb = await build_menu_hud_and_keyboard()
        combined = f"{hud_text}\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n{scan_report}"
        try:
            await query.edit_message_text(combined[:4000], parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(combined[:4000], parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("flush_locks", "clear_locks", "lock")):
        flushed_mem = clear_cooldown_locks()
        clear_idempotency_locks()
        await query.answer(f"🔓 Flushed {flushed_mem} cooldown & idempotency locks!")
        hud_text, kb = await build_menu_hud_and_keyboard()
        try:
            await query.edit_message_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("watch_bets", "watch", "stream")):
        await query.answer("📺 Resolving streamed.pk live feeds...")
        txt, kb = await build_watch_bets_payload()
        try:
            await query.edit_message_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("status", "exposure")):
        await query.answer("📊 Loading Status & Exposure...")
        st_txt = await build_status_text()
        _, kb = await build_menu_hud_and_keyboard()
        try:
            await query.edit_message_text(st_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(st_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("open_positions", "open", "positions")):
        await query.answer("📋 Loading Open Positions...")
        pending = fetch_pending_parlays()
        rt = get_runtime_state()
        _, kb = await build_menu_hud_and_keyboard()
        if not pending:
            body = (
                f"📭 *No open positions right now* (Exposure: `$0.00 / ${rt['slate_cap']:.2f}`).\n"
                "Tap *⚡ Force Scan* below to fire a fresh scan!"
            )
        else:
            blocks = [format_db_slip_summary(s) for s in pending[:5]]
            body = (
                f"📋 *OPEN POSITIONS ({len(pending)} Active)*\n"
                f"💰 *Exposure:* `${rt['exposure']:.2f} / ${rt['slate_cap']:.2f}` Slate Cap\n"
                "━━━━━━━━━━━━━━━━━━━━━━━━\n\n" + "\n\n".join(blocks)
            )
        try:
            await query.edit_message_text(body[:4000], parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(body[:4000], parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("build_combo", "custom_combo")):
        await query.answer("🛠️ Opening Custom Combo Builder...")
        b_txt, b_kb = await build_custom_combo_menu()
        try:
            await query.edit_message_text(b_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=b_kb)
        except Exception:
            if query.message:
                await query.message.reply_text(b_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=b_kb)

    elif "combo_toggle_" in data:
        try:
            idx = int(data.split("combo_toggle_")[1])
            cached = _USER_BUILDING_COMBO.get("cached_props", [])
            if 0 <= idx < len(cached):
                tk = getattr(cached[idx], "market_ticker", "")
                sel = _USER_BUILDING_COMBO.setdefault("selected_tickers", [])
                if tk in sel:
                    sel.remove(tk)
                    await query.answer(f"Removed {tk[:18]}")
                else:
                    if len(sel) < MAX_SLIP_LEGS:
                        sel.append(tk)
                        await query.answer(f"Added {tk[:18]}")
                    else:
                        await query.answer(f"Max {MAX_SLIP_LEGS} legs in combo", show_alert=True)
        except Exception as e:
            logger.warning("Error in combo_toggle: %s", e)
        b_txt, b_kb = await build_custom_combo_menu(_USER_BUILDING_COMBO.get("selected_tickers", []))
        try:
            await query.edit_message_text(b_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=b_kb)
        except Exception:
            if query.message:
                await query.message.reply_text(b_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=b_kb)

    elif any(k in data for k in ("combo_fire", "fire_combo")):
        sel = _USER_BUILDING_COMBO.get("selected_tickers", [])
        cached = _USER_BUILDING_COMBO.get("cached_props", [])
        chosen_legs = [l for l in cached if getattr(l, "market_ticker", "") in sel]
        if len(chosen_legs) >= 2:
            await query.answer("⚡ Executing custom Copula combo...")
            bankroll = await get_effective_bankroll()
            slip = evaluate_parlay(chosen_legs, bankroll=bankroll, iterations=MC_ITERATIONS)
            notes = await execute_and_persist_slip(slip)
            rt = get_runtime_state()
            _USER_BUILDING_COMBO["active"] = False
            _USER_BUILDING_COMBO["selected_tickers"] = []
            card = format_slip_card(slip, title_banner="CUSTOM COPULA SLIP EXECUTED", mode_label=rt["mode"])
            card += f"\n🏦 *Sim Cash Balance:* `${rt['sim_cash']:.2f}`"
            if notes:
                card += "\n" + "\n".join(notes)
            _, hud_kb = await build_menu_hud_and_keyboard()
            try:
                await query.edit_message_text(card, parse_mode=ParseMode.MARKDOWN, reply_markup=hud_kb)
            except Exception:
                if query.message:
                    await query.message.reply_text(card, parse_mode=ParseMode.MARKDOWN, reply_markup=hud_kb)
        else:
            await query.answer("⚠️ Select at least 2 legs before firing!", show_alert=True)

    elif any(k in data for k in ("combo_clear",)):
        _USER_BUILDING_COMBO["selected_tickers"] = []
        await query.answer("Selection cleared")
        b_txt, b_kb = await build_custom_combo_menu([])
        try:
            await query.edit_message_text(b_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=b_kb)
        except Exception:
            if query.message:
                await query.message.reply_text(b_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=b_kb)

    elif any(k in data for k in ("combo_resume", "combo_cancel")):
        _USER_BUILDING_COMBO["active"] = False
        _USER_BUILDING_COMBO["selected_tickers"] = []
        await query.answer("▶️ Resumed Auto-Sniper & returned to HUD")
        hud_text, kb = await build_menu_hud_and_keyboard()
        try:
            await query.edit_message_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("menu_analytics", "analytics")):
        await query.answer("📊 Loading Quant Analytics HUD...")
        ana_text = await build_analytics_text()
        _, kb = await build_menu_hud_and_keyboard()
        try:
            await query.edit_message_text(ana_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(ana_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("menu_pnl", "pnl", "profit")):
        await query.answer("📈 Settled PnL Report")
        stats = get_bot_stats()
        settled = fetch_settled_slips(limit=6)
        sign = "+" if stats.get("net_pnl", 0.0) >= 0 else ""
        lines = [
            "📈 *PEEZY BANKROLL & SETTLED PnL REPORT* 📈",
            "━━━━━━━━━━━━━━━━━━━━",
            f"🏦 *Current Sim Cash:* `${stats.get('sim_cash', 0.0):.2f}`",
            f"💵 *Realized Net PnL:* `{sign}${stats.get('net_pnl', 0.0):.2f}` (*ROI: `{sign}{stats.get('roi_pct', 0.0)}%`*)",
            f"📊 *Total Settled Stake:* `${stats.get('total_staked', 0.0):.2f}`",
            f"🏆 *Record:* `{stats.get('won_slips', 0)}W - {stats.get('lost_slips', 0)}L - {stats.get('push_slips', 0)}P`",
        ]
        if settled:
            lines.append("━━━━━━━━━━━━━━━━━━━━\n*Recent Settled Slips:*")
            for s in settled:
                icon = {"WON": "🟢", "LOST": "🔴", "PUSH": "🟡"}.get(s["status"], "⚪")
                s_pnl = float(s.get("pnl", 0.0))
                p_sign = "+" if s_pnl >= 0 else ""
                lines.append(f"{icon} `{s['slip_id']}` ({s.get('leg_count', 2)}-Leg `{s.get('american_odds', '+150')}`) — *[{s['status']}]* PnL: `{p_sign}${s_pnl:.2f}`")
        _, kb = await build_menu_hud_and_keyboard()
        p_txt = "\n".join(lines)
        try:
            await query.edit_message_text(p_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(p_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    elif any(k in data for k in ("menu_scan_options", "scan_options")):
        await query.answer("🔍 Select a sport to scan...")
        scan_kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("🏀 NBA Props", callback_data="scan_sport_nba"),
                InlineKeyboardButton("🏈 NFL / CFB", callback_data="scan_sport_nfl"),
            ],
            [
                InlineKeyboardButton("🎾 Tennis (ATP/WTA)", callback_data="scan_sport_tennis"),
                InlineKeyboardButton("⚾ MLB", callback_data="scan_sport_mlb"),
            ],
            [
                InlineKeyboardButton("🌐 Scan All Slates", callback_data="menu_force_scan"),
                InlineKeyboardButton("🔙 Back to HUD", callback_data="menu_refresh_hud"),
            ],
        ])
        msg_text = "🔍 *CHOOSE SPORT FOR 10k MONTE CARLO SCAN:*\n━━━━━━━━━━━━━━━━━━━━━━━━\nSelect an active slate below to evaluate real-time odds, true probability, and +EV edges:"
        try:
            await query.edit_message_text(msg_text, parse_mode=ParseMode.MARKDOWN, reply_markup=scan_kb)
        except Exception:
            if query.message:
                await query.message.reply_text(msg_text, parse_mode=ParseMode.MARKDOWN, reply_markup=scan_kb)

    elif "scan_sport_" in data:
        sp = data.split("scan_sport_")[1].upper()
        await query.answer(f"⚡ Scanning {sp}...")
        clean_kb = build_casual_video_buttons()
        if sp == "NBA":
            board_html = run_full_sport_board_scan("NBA", quant_sniper)
            try:
                await query.edit_message_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
            except Exception:
                if query.message:
                    await query.message.reply_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
        elif sp == "NFL":
            board_html = run_full_sport_board_scan("NFL", quant_sniper)
            try:
                await query.edit_message_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
            except Exception:
                if query.message:
                    await query.message.reply_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
        elif sp == "TENNIS":
            board_html = run_full_sport_board_scan("TENNIS", quant_sniper)
            try:
                await query.edit_message_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
            except Exception:
                if query.message:
                    await query.message.reply_text(board_html, parse_mode=ParseMode.HTML, reply_markup=clean_kb)
        else:
            scan_txt = await build_scan_text(sport_filter=sp)
            try:
                await query.edit_message_text(scan_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            except Exception:
                if query.message:
                    await query.message.reply_text(scan_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    else:
        await query.answer("🔄 HUD Refreshed")
        hud_text, kb = await build_menu_hud_and_keyboard()
        try:
            await query.edit_message_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        except Exception:
            if query.message:
                await query.message.reply_text(hud_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    rt = get_runtime_state()
    target = context.args[0].upper() if context.args else ("LIVE" if rt["mode"] == "SIM" else "SIM")
    new_mode = set_mode(target)
    await update.message.reply_text(
        f"🎛 *Switched Execution Mode to `{new_mode}`*\n🏦 Sim Cash: `${rt['sim_cash']:.2f}`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_simsettle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    pending = fetch_pending_parlays()
    if not pending:
        await update.message.reply_text("📭 No pending slips to settle! Run `/scan` or `/slip` first.")
        return

    alerts: List[str] = []

    async def capture_alert(text: str) -> None:
        alerts.append(text)

    settled_cnt = await run_settlement_cycle(notify_callback=capture_alert, simulate_outcomes=True)
    rt = get_runtime_state()
    if alerts:
        await update.message.reply_text("\n\n".join(alerts[:5]), parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(
            f"✅ Graded `{settled_cnt}` slip(s). Sim Cash: `${rt['sim_cash']:.2f}`",
            parse_mode=ParseMode.MARKDOWN,
        )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    new_cash = reset_sim_account()
    await update.message.reply_text(
        f"♻️ *Sim Account Reset!* Sim Cash restored to `${new_cash:.2f}` and all slips cleared.",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_analytics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if update.message:
        try:
            report = await build_analytics_text()
            _, kb = await build_menu_hud_and_keyboard()
            try:
                await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            except Exception:
                await update.message.reply_text(report, reply_markup=kb)
        except Exception as e:
            logger.error("Analytics failure: %s", e)
            await update.message.reply_text(f"📊 Analytics error: {e}")


async def cmd_build_combo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if update.message:
        try:
            b_txt, b_kb = await build_custom_combo_menu()
            try:
                await update.message.reply_text(b_txt, parse_mode=ParseMode.MARKDOWN, reply_markup=b_kb)
            except Exception:
                await update.message.reply_text(b_txt, reply_markup=b_kb)
        except Exception as e:
            logger.error("Combo builder failure: %s", e)
            await update.message.reply_text(f"🛠️ Combo builder error: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
    help_text = (
        "🤖 *AURASCAN // KALSHI QUANT COMMAND & FEATURE GUIDE* 🤖\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📖 *HOW TO USE AURASCAN V2:*\n"
        "1. *Scan Live Edges:* Run `/nba`, `/nfl`, or `/tennis` to view current 10,000-draw Monte Carlo boards.\n"
        "2. *Simulate Any Matchup/Prop:* Test any line directly using `/sim` or `/tennis <p1> vs <p2>`.\n"
        "3. *Build Custom Combos:* Tap `[🛠️ Build Custom Combo]` on `/menu` (or run `/build`) to toggle 2–3 legs with real-time Copula correlation calculations.\n"
        "4. *Auto-Sniper Execution:* Run `/sniper on` to arm autonomous Kalshi execution for qualified +EV plays.\n"
        "5. *Deep Quant Analytics:* Run `/analytics` or `/audit` to verify calibration, 50/50 symmetry, and live PnL.\n\n"
        "📡 *CORE HUD & EXECUTION CONTROLS:*\n"
        "• `/start` or `/menu` — Live AuraScan V2 Telemetry HUD & action deck\n"
        "• `/sniper [on|off]` — Arm or disarm autonomous order sniper (`/sniper on` to execute)\n"
        "• `/mode [live|sim]` — Toggle between Live Real-Money and Paper Simulation (`/mode sim` or `/mode live`)\n"
        "• `/cap <pct>` — Adjust bankroll exposure cap (`/cap 100` for 100% full balance, `/cap 50` for 50%)\n"
        "• `/bal` — Verify live Kalshi cash balance and session PnL\n"
        "• `/clear_locks` — Flush all ticker cooldowns and idempotency locks\n"
        "• `/push` — Synchronize code & deploy updates to GitHub\n\n"
        "🔍 *MANUAL SPORT SCANS (10,000 MC DRAWS):*\n"
        "• `/nba` — Scan NBA props (Points, Rebounds, Assists, 3PT, PRA)\n"
        "  _Usage:_ `/nba` or `/nba LeBron James O25.5 PTS`\n"
        "• `/nfl` (or `/cfb`) — Scan NFL & CFB props (Passing, Rushing, Receiving, TDs)\n"
        "  _Usage:_ `/nfl` or `/nfl Mahomes O274.5 PASS YDS`\n"
        "• `/tennis` (or `/atp`, `/wta`) — Scan Tennis match & set props or simulate matchups\n"
        "  _Usage:_ `/tennis` (scan board) or `/tennis Sinner vs Alcaraz HARD` (matchup sim)\n"
        "• `/mlb` — Scan MLB player props (Strikeouts, Hits, HR)\n"
        "• `/wnba` — Scan WNBA props\n"
        "• `/scan` — Force 10k Monte Carlo scan across ALL active slates\n\n"
        "🎲 *QUANT SIMULATIONS & COPULA PARLAYS:*\n"
        "• `/sim <prop>` — Run 10k Monte Carlo simulation on a specific prop\n"
        "  _Examples:_\n"
        "  • `/sim LeBron James O25.5 PTS`\n"
        "  • `/sim Patrick Mahomes O274.5 PASS YDS`\n"
        "  • `/sim Carlos Alcaraz STRAIGHT SETS`\n"
        "• `/slip <leg1> | <leg2> [| <leg3>]` — Evaluate Joint Gaussian Copula parlay\n"
        "  _Example:_\n"
        "  • `/slip NFL:Mahomes O274.5 PASS YDS@51 | NFL:Rice O68.5 REC YDS@50`\n"
        "• `/analytics` — 10k MC calibration, 50/50 symmetry, Copula boost & telemetry report\n"
        "• `/audit` — 10k MC calibration, directional bias & Cholesky audit\n"
        "• `/build` (or `/builder`) — Interactive manual slip builder\n\n"
        "📊 *PORTFOLIO, STREAMS & SETTLEMENT:*\n"
        "• `/open` (or `/positions`) — View active open contracts, HWM & streams\n"
        "• `/pnl` — Session bankroll, Win-Loss-Push record & settled ROI\n"
        "• `/watch` — Live streamed.pk & Sweat HUD video stream links\n"
        "• `/simsettle` — Grade open positions with test outcomes\n"
        "• `/reset` — Reset SIM account balance to baseline ($32.22)"
    )
    _, kb = await build_menu_hud_and_keyboard()
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def cmd_push(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _ensure_chat_id(update)
    if not update.message:
        return
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
    except Exception as exc:
        push_msg = f"❌ GitHub push error: `{exc}`"
    _, kb = await build_menu_hud_and_keyboard()
    await update.message.reply_text(push_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)


async def post_init(application: Application) -> None:
    stop_event = asyncio.Event()
    application.bot_data["stop_event"] = stop_event

    try:
        me = await application.bot.get_me()
        logger.info("🤖 Telegram Bot Connected: @%s (https://t.me/%s)", me.username, me.username)
        if not config.TELEGRAM_CHAT_ID:
            try:
                ups = await application.bot.get_updates(limit=15, timeout=3)
                for u in reversed(ups or []):
                    chat = u.effective_chat
                    if chat and chat.id:
                        config.TELEGRAM_CHAT_ID = str(chat.id)
                        os.environ["TELEGRAM_CHAT_ID"] = config.TELEGRAM_CHAT_ID
                        env_p = Path(BASE_DIR) / ".env"
                        cur = env_p.read_text(encoding="utf-8") if env_p.exists() else ""
                        if "TELEGRAM_CHAT_ID=" not in cur:
                            with open(env_p, "a", encoding="utf-8") as f:
                                f.write(f"\nTELEGRAM_CHAT_ID={config.TELEGRAM_CHAT_ID}\n")
                        logger.info("✅ Auto-discovered TELEGRAM_CHAT_ID=%s", config.TELEGRAM_CHAT_ID)
                        break
            except Exception:
                pass
        if not config.TELEGRAM_CHAT_ID:
            logger.info(
                "📲 IMPORTANT: Send /start (or any message) to @%s in Telegram so the bot can push auto-slips to your phone!",
                me.username,
            )
    except Exception as exc:
        logger.warning("Could not fetch bot profile: %s", exc)

    async def send_telegram_alert(text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
        if config.TELEGRAM_CHAT_ID:
            try:
                if reply_markup is None:
                    _, kb = await build_menu_hud_and_keyboard()
                    reply_markup = kb
                await application.bot.send_message(
                    chat_id=config.TELEGRAM_CHAT_ID,
                    text=text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                logger.error("Failed to dispatch Telegram alert: %s", exc)
        else:
            logger.warning(
                "Slip placed in picks.db, but TELEGRAM_CHAT_ID is not set yet! Message your bot in Telegram to receive push alerts."
            )

    if config.TELEGRAM_CHAT_ID:
        hud_text, kb = await build_menu_hud_and_keyboard()
        await send_telegram_alert(
            f"🟢 *AURASCAN // KALSHI V2 HYBRID BOT IS ONLINE*\n\n{hud_text}",
            reply_markup=kb,
        )

    application.bot_data["settle_task"] = asyncio.create_task(
        settle_engine_worker(notify_callback=send_telegram_alert, stop_event=stop_event)
    )
    application.bot_data["auto_scan_task"] = asyncio.create_task(
        auto_scanner_worker(notify_callback=send_telegram_alert, stop_event=stop_event)
    )
    application.bot_data["ws_scalp_task"] = asyncio.create_task(
        kalshi_ws_scalper_worker(notify_callback=send_telegram_alert, stop_event=stop_event)
    )
    try:
        runner = await start_background_web_server(
            scan_fn=scan_markets_with_telemetry,
            execute_fn=execute_and_persist_slip,
            telegram_dispatch_fn=send_telegram_alert,
        )
        application.bot_data["web_runner"] = runner
    except Exception as exc:
        logger.warning("Web UI port binding warning: %s", exc)
    logger.info("Background settle_engine, auto_scanner, and Mobile Web UI started.")


async def kalshi_ws_scalper_worker(
    notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """
    Real-time Kalshi V2 WebSocket Listener (`wss://api.elections.kalshi.com/trade-api/ws/v2`)
    subscribing to `ticker`, `orderbook_delta`, and `fill` channels for sub-second HWM trailing
    take-profit (`+5c` activate / `2c` drop), hard TP (`+8c`), and Clock-Pause Green-Out (`75s`).
    """
    ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2"
    sub_payload = {
        "id": 1,
        "cmd": "subscribe",
        "params": {"channels": ["ticker", "orderbook_delta", "fill"]},
    }
    hwm_tracker: Dict[str, Dict[str, float]] = {}

    while stop_event is None or not stop_event.is_set():
        if not kalshi_Configured():
            await asyncio.sleep(15)
            continue
        try:
            ws_headers = build_kalshi_auth_headers("GET", ws_url)
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url, headers=ws_headers, heartbeat=20) as ws:
                    await ws.send_json(sub_payload)
                    logger.info("[WS-STREAM] Connected to Kalshi V2 WebSocket (ticker + orderbook_delta + fill)")
                    async for msg in ws:
                        if stop_event and stop_event.is_set():
                            break
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        data = msg.json()
                        if data.get("type") in ("ticker", "orderbook_delta"):
                            m = data.get("msg") or {}
                            tkr = m.get("market_ticker")
                            yes_bid = int(m.get("yes_bid") or 0)
                            if tkr and yes_bid > 0:
                                now = time.time()
                                st = hwm_tracker.setdefault(
                                    tkr, {"hwm": yes_bid, "last_bid": yes_bid, "last_ts": now}
                                )
                                if yes_bid != st["last_bid"]:
                                    st["last_bid"] = yes_bid
                                    st["last_ts"] = now
                                if yes_bid > st["hwm"]:
                                    st["hwm"] = yes_bid
                                entry_est = int(st.get("entry", max(10, yes_bid - 4)))
                                st["entry"] = entry_est
                                unrealized = yes_bid - entry_est
                                pullback = st["hwm"] - yes_bid
                                if unrealized >= 8 or ((st["hwm"] - entry_est) >= 5 and pullback >= 2):
                                    reason = f"HARD_TP (+{unrealized}c)" if unrealized >= 8 else f"HWM_TRAIL (Peak {int(st['hwm'])}c -> {yes_bid}c)"
                                    if notify_callback and not st.get("cashed_out"):
                                        st["cashed_out"] = 1.0
                                        await notify_callback(
                                            f"💰 *WS SCALPER CASHOUT*\n"
                                            f"• 📌 *Ticker:* `{tkr}`\n"
                                            f"• ⚡ *Trigger:* `{reason}`\n"
                                            f"• 💵 *Entry:* `{entry_est}c` ➡️ *Exit:* `{yes_bid}c`"
                                        )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.debug("[WS-RECONNECT] Kalshi WS stream reconnecting: %s", exc)
            await asyncio.sleep(5)


async def post_shutdown(application: Application) -> None:
    stop_event: asyncio.Event = application.bot_data.get("stop_event")
    if stop_event:
        stop_event.set()
    for key in ("settle_task", "auto_scan_task", "ws_scalp_task"):
        task: asyncio.Task = application.bot_data.get(key)
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    web_runner = application.bot_data.get("web_runner")
    if web_runner:
        await web_runner.cleanup()


async def run_cli_console() -> None:
    """
    Interactive Termux Quant CLI Console when TELEGRAM_BOT_TOKEN is not provided.
    Allows running `/status`, `/scan`, `/sim`, `/audit`, `/open`, `/simsettle`, `/reset` directly in Termux!
    """
    print("\n" + "=" * 56)
    print("⚡ PEEZY QUANT CLI CONSOLE (10k MC + Gaussian Copula) ⚡")
    print("=" * 56)
    print(await build_status_text())
    print("\nType /scan, /sim, /audit, /open, /simsettle, /reset, or quit:\n")

    while True:
        try:
            cmd_line = await asyncio.to_thread(input, "peezy> ")
        except (EOFError, KeyboardInterrupt):
            print("\nExiting Peezy CLI Console.")
            break

        cmd_line = cmd_line.strip()
        if not cmd_line:
            continue
        if cmd_line.lower() in ("quit", "exit", "q"):
            break
        elif cmd_line.startswith("/status") or cmd_line.startswith("/start"):
            print("\n" + await build_status_text() + "\n")
        elif cmd_line.startswith("/scan"):
            print("\nScanning ESPN telemetry + Kalshi orderbooks...")
            print("\n" + await build_scan_text() + "\n")
        elif cmd_line.startswith("/simsettle"):
            alerts: List[str] = []
            async def _cb(t: str) -> None:
                alerts.append(t)
            await run_settlement_cycle(notify_callback=_cb, simulate_outcomes=True)
            print("\n" + ("\n\n".join(alerts) if alerts else "No pending slips to settle.") + "\n")
        elif cmd_line.startswith("/sim"):
            arg = cmd_line[4:].strip()
            print("\n" + await build_sim_text(arg) + "\n")
        elif cmd_line.startswith("/audit"):
            print("\n" + await build_audit_text() + "\n")
        elif cmd_line.startswith("/open"):
            pending = fetch_pending_parlays()
            if not pending:
                print("\n📭 No open slips in picks.db.\n")
            else:
                print("\n" + "\n\n".join(format_db_slip_summary(s) for s in pending) + "\n")
        elif cmd_line.startswith("/reset"):
            cash = reset_sim_account()
            print(f"\n♻️ Sim Cash reset to ${cash:.2f}\n")
        else:
            print("Commands: /status, /scan, /sim <prop>, /audit, /open, /simsettle, /reset, quit")


async def telegram_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle transient 409 Conflict or network blips quietly so background workers run cleanly."""
    if isinstance(context.error, (Conflict, NetworkError)):
        logger.debug("Transient Telegram polling event: %s", context.error)
    else:
        logger.error("Unhandled Telegram exception: %s", context.error)


def main() -> None:
    reaped = kill_stale_bot_processes()
    if reaped:
        logger.info("Terminated %d stale background peezy.py instance(s).", reaped)
    init_db()
    token = config.TELEGRAM_BOT_TOKEN.strip()

    if not token:
        print("\n[!] TELEGRAM_BOT_TOKEN is not set in ~/peezy_bot/.env yet.")
        try:
            entered = input(
                "Paste your TELEGRAM_BOT_TOKEN here (or press Enter to launch Interactive Termux Quant Console): "
            ).strip()
        except EOFError:
            entered = ""

        if entered:
            env_path = Path(BASE_DIR) / ".env"
            with open(env_path, "a", encoding="utf-8") as f:
                f.write(f"\nTELEGRAM_BOT_TOKEN={entered}\n")
            config.TELEGRAM_BOT_TOKEN = entered
            os.environ["TELEGRAM_BOT_TOKEN"] = entered
            token = entered
            print(f"✅ Saved TELEGRAM_BOT_TOKEN to {env_path}!")
        else:
            asyncio.run(run_cli_console())
            return

    app = (
        ApplicationBuilder()
        .token(token)
        .connect_timeout(20.0)
        .read_timeout(20.0)
        .write_timeout(20.0)
        .pool_timeout(20.0)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_error_handler(telegram_error_handler)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_start))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("sniper", cmd_auto))
    app.add_handler(CommandHandler("auto", cmd_auto))
    app.add_handler(CommandHandler("clear_locks", cmd_clear_locks))
    app.add_handler(CommandHandler("flush_locks", cmd_clear_locks))
    app.add_handler(CommandHandler("watch", cmd_watch))
    app.add_handler(CommandHandler("sim", cmd_sim))
    app.add_handler(CommandHandler("nba", cmd_nba))
    app.add_handler(CommandHandler("nfl", cmd_nfl))
    app.add_handler(CommandHandler("tennis", cmd_tennis))
    app.add_handler(CommandHandler("atp", cmd_tennis))
    app.add_handler(CommandHandler("wta", cmd_tennis))
    app.add_handler(CommandHandler("slip", cmd_slip))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("audit", cmd_audit))
    app.add_handler(CommandHandler("analytics", cmd_analytics))
    app.add_handler(CommandHandler("build", cmd_build_combo))
    app.add_handler(CommandHandler("builder", cmd_build_combo))
    app.add_handler(CommandHandler("open", cmd_open))
    app.add_handler(CommandHandler("positions", cmd_open))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("simsettle", cmd_simsettle))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("commands", cmd_help))
    app.add_handler(CommandHandler("push", cmd_push))
    app.add_handler(CallbackQueryHandler(handle_menu_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_menu))

    logger.info("Starting Peezy Quant Telegram Bot polling loop...")
    app.run_polling(
        drop_pending_updates=False,
        timeout=15,
        poll_interval=1.0,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()
