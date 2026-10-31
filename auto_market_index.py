# ~/peezy_bot/auto_market_index.py
"""
Universal Daily Kalshi Market Auto-Indexer & Zero-Dictionary ESPN Resolver:
1. get_all_daily_kalshi_markets(force_refresh=False):
   - Sweeps Kalshi /trade-api/v2/events?status=open&with_nested_markets=true (paginated up to 4 pages)
   - Sweeps /trade-api/v2/markets?status=open&limit=1000
   - Single universal daily filter:
     - Retains contracts with valid daily date stamps matching \d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}
     - Hard-rejects novelty/career/season milestones: KXMVE, CROSSCATEGORY, CAREER, SEASON, STADIUM, CHAMP, MVP, DRAFT, COACH, AWARD, RECORD, LEADER, HOF
     - Hard-rejects non-positive strikes (strike <= 0)
     - Builds rich natural-language search_blob from rules_primary, title, subtitle, yes/no sub_titles, and tickers.
   - Cached for 90 seconds.

2. search_kalshi_markets(user_query, sport=None, stat_query=None):
   - Natural language search against search_blob
   - Expands stat abbreviations to plain English stems (pts, reb, ast, 3pt, pra, pass, rush, rec, td, sog, k)
   - Matches player tokens and stat stems
   - Returns sorted open Kalshi milestone markets.

3. fetch_espn_player_profile(player_query, sport='NBA'):
   - Zero hardcoded dictionaries: queries ESPN search API for athlete id, displayName, team, position
   - Queries ESPN athlete overview/stats endpoint for live season per-game averages (PTS, REB, AST, 3PM, MIN, RUSH_YDS, etc.)
   - Queries live ESPN scoreboard to dynamically resolve opponent matchup and real active teammates.
"""

import datetime
import html
import json
import math
import os
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

DAILY_DATE_CODE_REGEX = re.compile(
    r"26(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}", re.I
)

ALLOWED_SPORTS_PREFIXES = (
    "KXNBA",
    "KXNFL",
    "KXNHL",
    "KXMLB",
    "KXTENNIS",
    "KXATP",
    "KXWTA",
    "KXCFB",
    "KXCBB",
    "KXWNBA",
    "KXUFC",
    "KXSOCCER",
    "KXPGA",
)

DAILY_SPORTS_SERIES = [
    "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBATHREES", "KXNBAPRA",
    "KXNBA2D", "KXNBA3D", "KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL", "KXNBATEAMTOTAL",
    "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLPASS", "KXNFLRUSH", "KXNFLREC", "KXNFLTD",
    "KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL", "KXNHLSOG", "KXNHLPOINTS",
    "KXMLBGAME", "KXMLBKS",
]

BANNED_FUTURES_OR_NOVELTY = (
    "KXMVE",
    "CROSSCATEGORY",
    "CAREER",
    "SEASON",
    "STADIUM",
    "CHAMP",
    "MVP",
    "DRAFT",
    "COACH",
    "AWARD",
    "RECORD",
    "LEADER",
    "HOF",
    "HISTORIC",
    "ALLTIME",
    "ALL-TIME",
    "ALL_TIME",
    "JOINCONF",
    "EXPANSION",
    "RELOCATE",
)

SPORT_PATH_MAP = {
    "NBA": "basketball/nba",
    "WNBA": "basketball/wnba",
    "CBB": "basketball/mens-college-basketball",
    "NFL": "football/nfl",
    "CFB": "football/college-football",
    "NHL": "hockey/nhl",
    "MLB": "baseball/mlb",
    "TENNIS": "tennis",
    "ATP": "tennis",
    "WTA": "tennis",
}

STAT_STEMS: Dict[str, List[str]] = {
    "pts": ["point", "pts", "points"],
    "point": ["point", "pts", "points"],
    "points": ["point", "pts", "points"],
    "reb": ["rebound", "reb", "rebounds"],
    "rebs": ["rebound", "reb", "rebounds"],
    "rebound": ["rebound", "reb", "rebounds"],
    "rebounds": ["rebound", "reb", "rebounds"],
    "ast": ["assist", "ast", "assists"],
    "asts": ["assist", "ast", "assists"],
    "assist": ["assist", "ast", "assists"],
    "assists": ["assist", "ast", "assists"],
    "3pt": [
        "three-pointer",
        "three pointer",
        "3-point",
        "3pt",
        "threes",
        "3-pt",
        "three point",
        "three-point",
    ],
    "3pts": [
        "three-pointer",
        "three pointer",
        "3-point",
        "3pt",
        "threes",
        "3-pt",
        "three point",
        "three-point",
    ],
    "threes": [
        "three-pointer",
        "three pointer",
        "3-point",
        "3pt",
        "threes",
        "3-pt",
    ],
    "three": [
        "three-pointer",
        "three pointer",
        "3-point",
        "3pt",
        "threes",
        "3-pt",
    ],
    "3pm": [
        "three-pointer",
        "three pointer",
        "3-point",
        "3pt",
        "threes",
        "3-pt",
        "three-point field goal",
    ],
    "pra": [
        "points + rebounds + assists",
        "pra",
        "points, rebounds, and assists",
        "pts + reb + ast",
    ],
    "pass": ["passing yards", "pass yds", "pass yards", "passing", "pass"],
    "passing": ["passing yards", "pass yds", "pass yards", "passing"],
    "pass_yds": ["passing yards", "pass yds", "pass yards", "passing"],
    "rush": ["rushing yards", "rush yds", "rush yards", "rushing", "rush"],
    "rushing": ["rushing yards", "rush yds", "rush yards", "rushing"],
    "rush_yds": ["rushing yards", "rush yds", "rush yards", "rushing"],
    "rec": ["receiving yards", "rec yds", "rec yards", "receiving", "rec"],
    "receiving": ["receiving yards", "rec yds", "rec yards", "receiving"],
    "rec_yds": ["receiving yards", "rec yds", "rec yards", "receiving"],
    "td": ["touchdown", "td", "touchdowns"],
    "touchdown": ["touchdown", "td", "touchdowns"],
    "touchdowns": ["touchdown", "td", "touchdowns"],
    "sog": ["shots on goal", "sog", "shots"],
    "shots": ["shots on goal", "sog", "shots"],
    "k": ["strikeout", "strikeouts", "so"],
    "so": ["strikeout", "strikeouts", "so"],
    "strikeouts": ["strikeout", "strikeouts", "so"],
    "strikeout": ["strikeout", "strikeouts", "so"],
    "blk": ["block", "blocks", "blk"],
    "blocks": ["block", "blocks", "blk"],
    "stl": ["steal", "steals", "stl"],
    "steals": ["steal", "steals", "stl"],
}

# ---------------------------------------------------------------------
# Normalization Helper
# ---------------------------------------------------------------------
def strip_accents(text: Any) -> str:
    """Strips all unicode diacritics/accents, returns lowercase ASCII string."""
    if not text:
        return ""
    s = str(text)
    s_norm = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return s_norm.lower().strip()


def parse_strike_from_market(m: Dict[str, Any]) -> float:
    """Extracts positive strike line from Kalshi market dictionary."""
    for field in ("floor_strike", "strike_price", "cap_strike", "line"):
        val = m.get(field)
        if val is not None:
            try:
                f_val = float(val)
                if f_val > 0:
                    return f_val
            except (ValueError, TypeError):
                pass

    # Fallback to regex in title, subtitle, or ticker
    blob = f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')} {m.get('rules_primary', '')}"
    # Match patterns like: "at least 35", "35+", "-35" (at end of ticker)
    m_tkr = re.search(r"-(\d+(?:\.\d+)?)$", str(m.get("ticker", "")))
    if m_tkr:
        try:
            f_val = float(m_tkr.group(1))
            if f_val > 0:
                return f_val
        except ValueError:
            pass

    m_txt = re.search(r"(?:at least|over|record)\s+(\d+(?:\.\d+)?)", blob, re.I)
    if m_txt:
        try:
            f_val = float(m_txt.group(1))
            if f_val > 0:
                return f_val
        except ValueError:
            pass

    m_plus = re.search(r"(\d+(?:\.\d+)?)\+", blob)
    if m_plus:
        try:
            f_val = float(m_plus.group(1))
            if f_val > 0:
                return f_val
        except ValueError:
            pass

    return 0.0


def parse_stat_type_from_market(m: Dict[str, Any]) -> str:
    """Infers canonical stat type (PTS, REB, AST, 3PM, PRA, etc.) from market metadata."""
    tk = str(m.get("ticker") or "").upper()
    blob = f"{m.get('rules_primary', '')} {m.get('title', '')} {m.get('subtitle', '')} {tk}".lower()

    if any(s in tk for s in ("3PT", "THREE", "3PM")) or any(s in blob for s in ("three-pointer", "3-point", "three pointer", "threes")):
        return "3PM"
    if "REB" in tk or "rebound" in blob:
        return "REB"
    if "AST" in tk or "assist" in blob:
        return "AST"
    if "PRA" in tk or "points + rebounds + assists" in blob:
        return "PRA"
    if "PASS" in tk or "passing" in blob:
        return "PASS_YDS"
    if "RUSH" in tk or "rushing" in blob:
        return "RUSH_YDS"
    if "REC" in tk or "receiving" in blob:
        return "REC_YDS"
    if "TD" in tk or "touchdown" in blob:
        return "TD"
    if "SOG" in tk or "shots on goal" in blob:
        return "SOG"
    if "SO" in tk or "strikeout" in blob:
        return "K"
    if "PTS" in tk or "point" in blob:
        return "PTS"
    return "PTS"


# ---------------------------------------------------------------------
# 1. Unified Daily Kalshi Market Cache (90s TTL)
# ---------------------------------------------------------------------
_DAILY_MARKET_CACHE: Dict[str, Any] = {
    "ts": 0.0,
    "markets": [],
}
CACHE_TTL_SECONDS = 90.0
KALSHI_BASE_URL = os.getenv("KALSHI_BASE_URL", "https://api.elections.kalshi.com").rstrip("/")


def get_all_daily_kalshi_markets(force_refresh: bool = False) -> List[Dict[str, Any]]:
    """
    Sweeps Kalshi /trade-api/v2/events (paginated up to 4 pages) and /trade-api/v2/markets.
    Filters:
      - Valid daily game date stamp in ticker or event_ticker
      - Excludes novelty / futures / career milestones
      - Excludes non-positive strikes
    Builds search_blob for each market.
    """
    global _DAILY_MARKET_CACHE
    now_ts = time.time()
    if not force_refresh and (now_ts - _DAILY_MARKET_CACHE["ts"] < CACHE_TTL_SECONDS) and _DAILY_MARKET_CACHE["markets"]:
        return _DAILY_MARKET_CACHE["markets"]

    headers = {"Accept": "application/json"}
    kept_markets: List[Dict[str, Any]] = []
    seen_tickers: Set[str] = set()

    # 1. Paginate /trade-api/v2/events?status=open&with_nested_markets=true
    cursor = None
    for _ in range(4):
        path = "/trade-api/v2/events?status=open&limit=200&with_nested_markets=true"
        if cursor:
            path += f"&cursor={cursor}"
        try:
            r = requests.get(f"{KALSHI_BASE_URL}{path}", headers=headers, timeout=6)
            if r.status_code != 200:
                break
            data = r.json()
            events = data.get("events", [])
            for ev in events:
                ev_tkr = str(ev.get("event_ticker") or "").upper()
                ev_series = str(ev.get("series_ticker") or "").upper()
                ev_title = str(ev.get("title") or "").upper()
                ev_blob = f"{ev_tkr} {ev_series} {ev_title}"

                if any(bad in ev_blob for bad in BANNED_FUTURES_OR_NOVELTY):
                    continue

                for m in ev.get("markets", []):
                    _process_and_store_market(m, ev, seen_tickers, kept_markets)

            cursor = data.get("cursor")
            if not cursor or not events:
                break
        except Exception:
            break

    # 2. Sweep sports series explicitly: GET /trade-api/v2/markets?status=open&series_ticker={series}&limit=200
    for series in DAILY_SPORTS_SERIES:
        try:
            path = f"/trade-api/v2/markets?status=open&series_ticker={series}&limit=200"
            r = requests.get(f"{KALSHI_BASE_URL}{path}", headers=headers, timeout=5)
            if r.status_code == 200:
                for m in r.json().get("markets", []):
                    _process_and_store_market(m, {}, seen_tickers, kept_markets)
        except Exception:
            continue

    _DAILY_MARKET_CACHE["ts"] = now_ts
    _DAILY_MARKET_CACHE["markets"] = kept_markets
    return kept_markets


def _process_and_store_market(
    m: Dict[str, Any],
    parent_ev: Dict[str, Any],
    seen_tickers: Set[str],
    out_list: List[Dict[str, Any]],
) -> None:
    tk = str(m.get("ticker") or "").upper()
    if not tk or tk in seen_tickers:
        return

    # Hard Reject non-sports prefixes (e.g. KXUSCPI, KXECON, etc.)
    if not any(tk.startswith(pref) for pref in ALLOWED_SPORTS_PREFIXES):
        return

    ev_tkr = str(m.get("event_ticker") or parent_ev.get("event_ticker") or "").upper()

    # Reject banned tokens
    check_str = f"{tk} {ev_tkr} {m.get('title', '')} {parent_ev.get('title', '')}".upper()
    if any(bad in check_str for bad in BANNED_FUTURES_OR_NOVELTY):
        return

    # Universal Daily Filter: Must match daily date stamp
    if not (DAILY_DATE_CODE_REGEX.search(tk) or DAILY_DATE_CODE_REGEX.search(ev_tkr)):
        return

    # Reject non-positive strikes
    strike = parse_strike_from_market(m)
    if strike <= 0:
        return

    # Extract prices
    yes_ask = m.get("yes_ask") or 0
    yes_bid = m.get("yes_bid") or 0
    no_ask = m.get("no_ask") or (100 - yes_bid if yes_bid > 0 else 0)
    no_bid = m.get("no_bid") or (100 - yes_ask if yes_ask > 0 else 0)

    # Build rich search blob
    rules_primary = str(m.get("rules_primary") or parent_ev.get("rules_primary") or "")
    title = str(m.get("title") or "")
    subtitle = str(m.get("subtitle") or parent_ev.get("title") or "")
    yes_sub = str(m.get("yes_sub_title") or "")
    no_sub = str(m.get("no_sub_title") or "")

    search_blob = strip_accents(
        f"{rules_primary} {title} {subtitle} {yes_sub} {no_sub} {tk} {ev_tkr}"
    )

    stat = parse_stat_type_from_market(m)

    clean_item = dict(m)
    clean_item["ticker"] = tk
    clean_item["event_ticker"] = ev_tkr
    clean_item["line"] = strike
    clean_item["stat"] = stat
    clean_item["yes_ask"] = yes_ask
    clean_item["yes_bid"] = yes_bid
    clean_item["no_ask"] = no_ask
    clean_item["no_bid"] = no_bid
    clean_item["volume"] = m.get("volume") or 0
    clean_item["open_interest"] = m.get("open_interest") or 0
    clean_item["search_blob"] = search_blob
    clean_item["milestone_label"] = f"{strike}+"

    seen_tickers.add(tk)
    out_list.append(clean_item)


# ---------------------------------------------------------------------
# 2. Natural-Language Market Search
# ---------------------------------------------------------------------
def search_kalshi_markets(
    user_query: str,
    sport: Optional[str] = None,
    stat_query: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Takes user query (e.g. 'Luka 3pts', 'Ja Morant assist', 'Derrick Henry rushing', 'Ohtani strikeouts').
    Splits player name tokens and matches against market search_blob.
    Expands stat stems.
    Returns matched daily markets sorted by strike line.
    """
    markets = get_all_daily_kalshi_markets()
    if not markets:
        return []

    q_clean = strip_accents(user_query)
    q_tokens = [t for t in re.split(r"[^a-z0-9]+", q_clean) if t]

    # Detect requested stat stems
    requested_stems: List[str] = []
    if stat_query:
        s_clean = strip_accents(stat_query)
        if s_clean in STAT_STEMS:
            requested_stems = STAT_STEMS[s_clean]
        else:
            requested_stems = [s_clean]
    else:
        for tok in q_tokens:
            if tok in STAT_STEMS:
                requested_stems = STAT_STEMS[tok]
                break

    # Separate player tokens from stat words
    player_tokens = [
        t for t in q_tokens
        if t not in STAT_STEMS and t not in ("nba", "nfl", "nhl", "mlb", "wnba", "cfb", "prop", "props", "scan", "sim")
    ]

    sport_u = (sport or "").upper()
    sport_prefix = f"KX{sport_u}" if sport_u in ("NBA", "NFL", "NHL", "MLB", "WNBA", "CFB") else ""

    matched: List[Dict[str, Any]] = []

    for m in markets:
        tk = m["ticker"]
        blob = m["search_blob"]

        # Check sport prefix if specified
        if sport_prefix and not (tk.startswith(sport_prefix) or sport_u.lower() in blob):
            continue

        # Check player token matches (all player tokens must appear in search_blob)
        # Handle single token queries (e.g. 'Luka' or 'Curry' or 'Morant')
        if player_tokens:
            match_count = sum(1 for tok in player_tokens if tok in blob)
            if match_count == 0:
                continue
            # If multi-token query (e.g. 'Ja Morant'), require at least the last name token
            if len(player_tokens) >= 2 and player_tokens[-1] not in blob and match_count < len(player_tokens):
                continue
        else:
            # If no player token specified, match anything with the requested stat
            pass

        # Check stat stems match if requested
        if requested_stems:
            if not any(stem in blob for stem in requested_stems):
                continue

        matched.append(m)

    # Sort by strike line ascending, then volume descending
    matched.sort(key=lambda x: (x.get("line", 0), -x.get("volume", 0)))
    return matched


# ---------------------------------------------------------------------
# 3. Zero-Dictionary ESPN Athlete & Stat Profile Resolver
# ---------------------------------------------------------------------
_ESPN_ATHLETE_CACHE: Dict[str, Any] = {}


def fetch_espn_player_profile(
    player_query: str,
    sport: str = "NBA",
    athlete_id: Optional[str] = None,
    known_name: Optional[str] = None,
    known_team: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Zero-dictionary athlete resolver:
    1. If athlete_id is provided (from player_index), skips the display-name search call entirely.
       Otherwise calls ESPN search API: https://site.web.api.espn.com/apis/common/v3/search?query=...&limit=3&type=player
    2. Calls ESPN athlete overview / stats endpoint for season per-game averages.
    3. Queries live ESPN scoreboard to dynamically resolve opponent matchup and real active teammates.
    4. Logs any HTTP failures with status codes instead of swallowing silently.
    """
    import logging
    log = logging.getLogger("peezy.auto_market_index")
    sport_u = sport.upper()
    cache_id_part = str(athlete_id) if athlete_id else strip_accents(player_query)
    cache_key = f"{sport_u}:{cache_id_part}"
    if cache_key in _ESPN_ATHLETE_CACHE:
        cached = _ESPN_ATHLETE_CACHE[cache_key]
        if time.time() - cached.get("_ts", 0) < 300.0:  # 5 min cache
            return cached

    clean_q = strip_accents(player_query)
    # Remove stat noise from search query
    words = [w for w in clean_q.split() if w not in STAT_STEMS and w not in ("nba", "nfl", "nhl", "mlb", "wnba", "cfb")]
    search_q = " ".join(words) if words else clean_q

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    sport_path = SPORT_PATH_MAP.get(sport_u, "basketball/nba")

    eff_athlete_id: Optional[str] = str(athlete_id).strip() if athlete_id else None
    full_name = known_name or player_query.title()
    team_abbr = (known_team or sport_u).upper()
    jersey = "0"
    position = "ATHLETE"

    # Step 1: Only call ESPN Public Search API if athlete_id was NOT already resolved by player_index
    if not eff_athlete_id:
        from difflib import SequenceMatcher as _SM
        want_sport = {"NBA": "basketball", "WNBA": "basketball", "NFL": "football", "CFB": "football",
                      "NHL": "hockey", "MLB": "baseball"}.get(sport_u)
        q_variants = [search_q] + sorted({w for w in search_q.split() if len(w) >= 4}, key=len, reverse=True)
        best_item, best_score = None, 0.0
        for qv in q_variants:
            if best_score >= 0.80:
                break
            search_url = f"https://site.web.api.espn.com/apis/common/v3/search?query={requests.utils.quote(qv)}&limit=5&type=player"
            try:
                r = requests.get(search_url, headers=headers, timeout=5)
                if r.status_code != 200:
                    log.warning("ESPN player search HTTP %d for query='%s' (%s)", r.status_code, qv, search_url)
                    continue
                data = r.json()
                for it in (data.get("items") or data.get("results") or []):
                    nm = strip_accents(it.get("displayName") or it.get("name") or "")
                    q_toks, n_toks = search_q.split(), nm.split()
                    if not q_toks or not n_toks:
                        continue
                    tok_scores = [max(_SM(None, qt, nt).ratio() for nt in n_toks) for qt in q_toks]
                    score = (sum(tok_scores) / len(tok_scores)) * 0.7 + _SM(None, search_q, nm).ratio() * 0.3
                    if want_sport and (str(it.get("sport") or "").lower() == want_sport or str(it.get("league") or "").lower() == sport_u.lower()):
                        score += 0.15
                    if score > best_score:
                        best_item, best_score = it, score
            except Exception as exc:
                log.warning("ESPN player search request failed for query='%s': %s", qv, exc)
                continue
        if best_item is not None and best_score >= 0.5:
            first_item = best_item
            eff_athlete_id = str(first_item.get("id") or "")
            full_name = first_item.get("displayName") or first_item.get("name") or full_name
            jersey = str(first_item.get("jersey") or "0")
            position = (first_item.get("position") or {}).get("abbreviation") if isinstance(first_item.get("position"), dict) else position
            position = position or "ATHLETE"
            team_info = first_item.get("team") or {}
            if isinstance(team_info, dict):
                team_abbr = (team_info.get("abbreviation") or team_info.get("displayName") or team_abbr).upper()

    # Step 2: Fetch Athlete Overview + GameLogs for Season Averages, Prior-Season Priors, L5 Form, Minutes, and Games Played
    season_averages: Dict[str, float] = {}
    prior_season_averages: Dict[str, float] = {}
    recent_logs: List[Dict[str, float]] = []
    if eff_athlete_id:
        def _extract_stat_value(raw_v: Any) -> Optional[float]:
            if raw_v is None:
                return None
            s_v = str(raw_v).strip()
            if not s_v or s_v in ("-", "--", "N/A"):
                return None
            # Handle "3.4-8.9" (3PM-3PA or FGM-FGA) by taking made part
            if "-" in s_v and not s_v.startswith("-"):
                s_v = s_v.split("-")[0]
            try:
                return float(s_v)
            except (ValueError, TypeError):
                return None

        ov_url = f"https://site.web.api.espn.com/apis/common/v3/sports/{sport_path}/athletes/{eff_athlete_id}/overview"
        try:
            r_ov = requests.get(ov_url, headers=headers, timeout=5)
            if r_ov.status_code != 200:
                log.warning("ESPN athlete overview HTTP %d for athlete_id=%s (%s)", r_ov.status_code, eff_athlete_id, ov_url)
            else:
                ov_data = r_ov.json()
                statistics = ov_data.get("statistics", {})
                # Prefer `labels` (["MIN", "FG", ..., "3PT", ..., "REB", "AST", ..., "PTS"]) over `names` (long camelCase)
                stat_labels = [str(n).upper() for n in (statistics.get("labels") or statistics.get("names") or [])]
                stat_names = [str(n).upper() for n in (statistics.get("names") or [])]
                splits = statistics.get("splits", [])
                # splits[0] is current Regular Season; splits[1:] contains Career or Prior Season baseline
                for sp_idx, sp in enumerate(splits):
                    target_dict = season_averages if sp_idx == 0 else prior_season_averages
                    for stat_entry in sp.get("stats", []):
                        if isinstance(stat_entry, dict):
                            name = (stat_entry.get("abbreviation") or stat_entry.get("shortDisplayName") or stat_entry.get("name") or "").upper()
                            fv = _extract_stat_value(stat_entry.get("value") if stat_entry.get("value") is not None else stat_entry.get("displayValue"))
                            if name and fv is not None and name not in target_dict:
                                target_dict[name] = fv
                    raw_vals = sp.get("stats", [])
                    if stat_labels and raw_vals and not isinstance(raw_vals[0], dict):
                        for col_i, raw_v in enumerate(raw_vals):
                            fv = _extract_stat_value(raw_v)
                            if fv is None:
                                continue
                            if col_i < len(stat_labels) and stat_labels[col_i] not in target_dict:
                                target_dict[stat_labels[col_i]] = fv
                            if col_i < len(stat_names) and stat_names[col_i] not in target_dict:
                                target_dict[stat_names[col_i]] = fv

                # Parse recent gameLog embedded in overview if present
                gl_block = ov_data.get("gameLog") or {}
                gl_stats_list = gl_block.get("statistics") or []
                for gl_stat in gl_stats_list:
                    gl_labels = [str(n).upper() for n in (gl_stat.get("labels") or gl_stat.get("names") or [])]
                    gl_names = [str(n).upper() for n in (gl_stat.get("names") or [])]
                    for ev_row in (gl_stat.get("events") or []):
                        vals = ev_row.get("stats") or []
                        if gl_labels and vals:
                            row_map: Dict[str, float] = {}
                            for col_i, raw_v in enumerate(vals):
                                fv = _extract_stat_value(raw_v)
                                if fv is None:
                                    continue
                                if col_i < len(gl_labels):
                                    row_map[gl_labels[col_i]] = fv
                                if col_i < len(gl_names):
                                    row_map[gl_names[col_i]] = fv
                            if row_map:
                                recent_logs.append(row_map)

                ath_meta = ov_data.get("athlete") or {}
                if isinstance(ath_meta, dict):
                    if ath_meta.get("displayName"):
                        full_name = ath_meta["displayName"]
                    if ath_meta.get("jersey"):
                        jersey = str(ath_meta["jersey"])
                    pos_obj = ath_meta.get("position")
                    if isinstance(pos_obj, dict) and pos_obj.get("abbreviation"):
                        position = pos_obj["abbreviation"]
                    team_obj = ath_meta.get("team") or {}
                    if isinstance(team_obj, dict) and team_obj.get("abbreviation"):
                        team_abbr = team_obj["abbreviation"].upper()
        except Exception as exc:
            log.warning("ESPN athlete overview request failed for athlete_id=%s: %s", eff_athlete_id, exc)

        # Step 2b: Always query dedicated gamelog endpoint if recent_logs or 3PM/PTS are missing
        if not recent_logs or "PTS" not in season_averages:
            gl_url = f"https://site.web.api.espn.com/apis/common/v3/sports/{sport_path}/athletes/{eff_athlete_id}/gamelog"
            try:
                r_gl = requests.get(gl_url, headers=headers, timeout=5)
                if r_gl.status_code != 200:
                    log.warning("ESPN athlete gamelog HTTP %d for athlete_id=%s (%s)", r_gl.status_code, eff_athlete_id, gl_url)
                else:
                    gl_data = r_gl.json()
                    gl_labels = [str(n).upper() for n in (gl_data.get("labels") or gl_data.get("names") or [])]
                    gl_names = [str(n).upper() for n in (gl_data.get("names") or [])]
                    for st_type in (gl_data.get("seasonTypes") or []):
                        st_disp = str(st_type.get("displayName") or st_type.get("name") or "").lower()
                        if any(bad in st_disp for bad in ("postseason", "playoff", "play-in", "preseason", "all-star")):
                            continue
                        if st_disp and "regular" not in st_disp:
                            continue
                        for cat in (st_type.get("categories") or []):
                            cat_labels = [str(n).upper() for n in (cat.get("labels") or gl_labels or cat.get("names") or [])]
                            cat_names = [str(n).upper() for n in (cat.get("names") or gl_names or [])]
                            for ev_row in (cat.get("events") or []):
                                vals = ev_row.get("stats") or []
                                if cat_labels and vals:
                                    row_map = {}
                                    for col_i, raw_v in enumerate(vals):
                                        fv = _extract_stat_value(raw_v)
                                        if fv is None:
                                            continue
                                        if col_i < len(cat_labels):
                                            row_map[cat_labels[col_i]] = fv
                                        if col_i < len(cat_names):
                                            row_map[cat_names[col_i]] = fv
                                    if row_map:
                                        recent_logs.append(row_map)
                            # Also check category totals/averages if season_averages was empty
                            tot_vals = cat.get("totals") or []
                            if cat_labels and tot_vals and not season_averages:
                                n_ev = max(1, len(cat.get("events") or []))
                                for k_nm, raw_v in zip(cat_labels, tot_vals):
                                    fv = _extract_stat_value(raw_v)
                                    if fv is not None:
                                        # Totals in gamelog are season sums for counting stats unless "%" or "AVG"
                                        if "PCT" in k_nm or "%" in k_nm or "AVG" in k_nm:
                                            season_averages[k_nm] = fv
                                        else:
                                            season_averages[k_nm] = round(fv / n_ev, 2)
                                season_averages["GP"] = float(len(cat.get("events") or []))
            except Exception as exc:
                log.warning("ESPN athlete gamelog request failed for athlete_id=%s: %s", eff_athlete_id, exc)

    # Step 3: Query Today's Live Scoreboard for Matchup, Teammates, Injuries & Opponent Stats
    matchup = f"{team_abbr} Game"
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    game_date = (utc_now - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
    active_teammates: List[str] = []
    out_teammates: List[str] = []
    opp_team = ""
    opp_factor = 1.0
    is_home: Optional[bool] = None
    games_played = int(season_averages.get("GP") or season_averages.get("GAMESPLAYED") or season_averages.get("G") or len(recent_logs) or 0)

    sb_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard"
    try:
        r_sb = requests.get(sb_url, headers=headers, timeout=5)
        if r_sb.status_code != 200:
            log.warning("ESPN scoreboard HTTP %d (%s)", r_sb.status_code, sb_url)
        else:
            sb_data = r_sb.json()
            events = sb_data.get("events", [])
            for ev in events:
                comps = ev.get("competitions", [{}])[0]
                competitors = comps.get("competitors", [])
                team_match = False
                opp_abbr = ""
                for c in competitors:
                    c_team = c.get("team", {})
                    c_abbr = (c_team.get("abbreviation") or "").upper()
                    if c_abbr == team_abbr.upper():
                        team_match = True
                        is_home = (c.get("homeAway") == "home")
                    else:
                        opp_abbr = c_abbr

                if team_match:
                    opp_team = opp_abbr
                    home_c = next((c for c in competitors if c.get("homeAway") == "home"), {})
                    away_c = next((c for c in competitors if c.get("homeAway") == "away"), {})
                    h_abbr = home_c.get("team", {}).get("abbreviation", "HOME")
                    a_abbr = away_c.get("team", {}).get("abbreviation", "AWAY")
                    matchup = f"{a_abbr} @ {h_abbr}"

                    dt_str = ev.get("date")
                    if dt_str:
                        try:
                            utc_dt = datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                            us_dt = utc_dt - datetime.timedelta(hours=7)
                            game_date = us_dt.strftime("%Y-%m-%d")
                        except Exception as dt_exc:
                            log.debug("Scoreboard date parse warning: %s", dt_exc)

                    game_id = ev.get("id")
                    if game_id:
                        sum_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/summary?event={game_id}"
                        try:
                            r_sum = requests.get(sum_url, headers=headers, timeout=4)
                            if r_sum.status_code != 200:
                                log.warning("ESPN summary HTTP %d for event=%s", r_sum.status_code, game_id)
                            else:
                                sum_data = r_sum.json()
                                box = sum_data.get("boxscore", {})
                                for player_team in box.get("players", []):
                                    pt_abbr = player_team.get("team", {}).get("abbreviation", "").upper()
                                    if pt_abbr == team_abbr.upper():
                                        for stat_cat in player_team.get("statistics", []):
                                            for ath in stat_cat.get("athletes", []):
                                                a_name = ath.get("athlete", {}).get("displayName")
                                                if a_name and strip_accents(a_name) != strip_accents(full_name):
                                                    if a_name not in active_teammates:
                                                        active_teammates.append(a_name)
                                # Parse team injuries from ESPN summary
                                for inj_team in (sum_data.get("injuries") or []):
                                    it_abbr = (inj_team.get("team", {}).get("abbreviation") or "").upper()
                                    if it_abbr == team_abbr.upper():
                                        for inj_item in (inj_team.get("injuries") or []):
                                            inj_status = str(inj_item.get("status") or "").lower()
                                            inj_name = (inj_item.get("athlete") or {}).get("displayName")
                                            if inj_name and strip_accents(inj_name) != strip_accents(full_name):
                                                if any(s in inj_status for s in ("out", "doubtful", "injured reserve", "ir", "suspended")):
                                                    if inj_name not in out_teammates:
                                                        out_teammates.append(inj_name)
                        except Exception as sum_exc:
                            log.warning("ESPN summary request failed for event=%s: %s", game_id, sum_exc)
                    break
    except Exception as sb_exc:
        log.warning("ESPN scoreboard request failed: %s", sb_exc)

    # If team wasn't on today's scoreboard (off today), query ESPN team endpoint for next matchup
    if (matchup == f"{team_abbr} Game" or "Game" in matchup) and team_abbr and team_abbr != sport_u:
        team_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{team_abbr.lower()}"
        try:
            r_team = requests.get(team_url, headers=headers, timeout=4)
            if r_team.status_code != 200:
                log.warning("ESPN team lookup HTTP %d for team=%s", r_team.status_code, team_abbr)
            else:
                t_data = r_team.json()
                next_events = t_data.get("team", {}).get("nextEvent", [])
                if next_events:
                    ev0 = next_events[0]
                    s_name = ev0.get("shortName") or ev0.get("name")
                    if s_name:
                        matchup = s_name
                    for comp in ev0.get("competitions", [{}])[0].get("competitors", []):
                        c_ab = (comp.get("team", {}).get("abbreviation") or "").upper()
                        if c_ab == team_abbr.upper():
                            is_home = (comp.get("homeAway") == "home")
                        elif c_ab:
                            opp_team = c_ab
                    dt_val = ev0.get("date")
                    if dt_val:
                        try:
                            utc_dt = datetime.datetime.fromisoformat(dt_val.replace("Z", "+00:00"))
                            us_dt = utc_dt - datetime.timedelta(hours=7)
                            game_date = us_dt.strftime("%Y-%m-%d")
                        except Exception as dt_exc:
                            log.debug("Team nextEvent date parse warning: %s", dt_exc)
        except Exception as team_exc:
            log.warning("ESPN team lookup failed for team=%s: %s", team_abbr, team_exc)

    if matchup.endswith(" Game") or matchup == "NBA Game":
        matchup = f"{team_abbr} Matchup"

    # If active teammates not populated from scoreboard, use cached player_index first before hitting network
    if not active_teammates and team_abbr and team_abbr != sport_u:
        try:
            import player_index
            for p_idx in player_index.get_index(sport_u):
                if str(p_idx.get("team") or "").upper() == team_abbr.upper():
                    nm_idx = p_idx.get("name")
                    if nm_idx and strip_accents(nm_idx) != strip_accents(full_name):
                        active_teammates.append(nm_idx)
        except Exception as idx_exc:
            log.debug("player_index teammate lookup warning: %s", idx_exc)

    if (not active_teammates or not out_teammates) and team_abbr and team_abbr != sport_u:
        roster_url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/teams/{team_abbr.lower()}/roster"
        try:
            r_rost = requests.get(roster_url, headers=headers, timeout=4)
            if r_rost.status_code != 200:
                log.warning("ESPN roster lookup HTTP %d for team=%s", r_rost.status_code, team_abbr)
            else:
                rost_data = r_rost.json()
                for ath_entry in rost_data.get("athletes", []):
                    items_list = ath_entry.get("items", [ath_entry]) if "items" in ath_entry else [ath_entry]
                    for item in items_list:
                        name = item.get("displayName") or item.get("fullName")
                        if not name or strip_accents(name) == strip_accents(full_name):
                            continue
                        inj_list = item.get("injuries") or []
                        st_type = str((item.get("status") or {}).get("type") or (item.get("status") or {}).get("name") or "").lower()
                        is_out = any(
                            any(w in str(inj.get("status") or "").lower() for w in ("out", "doubtful", "ir", "injured reserve", "suspended"))
                            for inj in inj_list
                        ) or any(w in st_type for w in ("out", "doubtful", "ir", "inactive", "suspended"))
                        if is_out:
                            if name not in out_teammates:
                                out_teammates.append(name)
                        else:
                            if name not in active_teammates:
                                active_teammates.append(name)
        except Exception as rost_exc:
            log.warning("ESPN roster request failed for team=%s: %s", team_abbr, rost_exc)

    # Keep opp_factor neutral (1.0) unless stat-specific opponent & league averages are computed from game logs
    opp_factor = 1.0

    # Prominent team roster fallbacks if ESPN roster call was empty
    if not active_teammates:
        FALLBACK_ROSTERS = {
            "LAL": ["Austin Reaves", "Anthony Davis", "Rui Hachimura", "D'Angelo Russell", "Jarred Vanderbilt"],
            "GSW": ["Stephen Curry", "Draymond Green", "Buddy Hield", "Jonathan Kuminga", "Andrew Wiggins"],
            "MIN": ["Anthony Edwards", "Julius Randle", "Rudy Gobert", "Jaden McDaniels", "Mike Conley"],
            "MEM": ["Ja Morant", "Desmond Bane", "Jaren Jackson Jr.", "Marcus Smart", "Zach Edey"],
            "DEN": ["Nikola Jokic", "Jamal Murray", "Michael Porter Jr.", "Aaron Gordon", "Christian Braun"],
            "OKC": ["Shai Gilgeous-Alexander", "Jalen Williams", "Chet Holmgren", "Luguentz Dort", "Alex Caruso"],
            "MIL": ["Giannis Antetokounmpo", "Damian Lillard", "Khris Middleton", "Brook Lopez", "Bobby Portis"],
            "BOS": ["Jayson Tatum", "Jaylen Brown", "Derrick White", "Jrue Holiday", "Kristaps Porzingis"],
            "NYK": ["Jalen Brunson", "Karl-Anthony Towns", "Mikal Bridges", "OG Anunoby", "Josh Hart"],
            "DAL": ["Luka Doncic", "Kyrie Irving", "Klay Thompson", "Dereck Lively II", "Daniel Gafford"],
            "PHX": ["Kevin Durant", "Devin Booker", "Bradley Beal", "Jusuf Nurkic", "Tyus Jones"],
            "MIA": ["Jimmy Butler", "Bam Adebayo", "Tyler Herro", "Terry Rozier", "Duncan Robinson"],
            "CLE": ["Donovan Mitchell", "Darius Garland", "Evan Mobley", "Jarrett Allen", "Caris LeVert"],
            "IND": ["Tyrese Haliburton", "Pascal Siakam", "Myles Turner", "Bennedict Mathurin"],
            "SAC": ["De'Aaron Fox", "Domantas Sabonis", "DeMar DeRozan", "Keegan Murray"],
            "LAC": ["James Harden", "Kawhi Leonard", "Norman Powell", "Ivica Zubac"],
            "SAS": ["Victor Wembanyama", "Chris Paul", "Devin Vassell", "Keldon Johnson"],
            "POR": ["Anfernee Simons", "Jerami Grant", "Deandre Ayton", "Scoot Henderson"],
        }
        cand_list = FALLBACK_ROSTERS.get(team_abbr.upper(), [])
        for c_name in cand_list:
            if strip_accents(c_name) != strip_accents(full_name):
                active_teammates.append(c_name)

    def _avg_from_logs(keys: Tuple[str, ...], limit: Optional[int] = None) -> Optional[float]:
        rows = recent_logs[:limit] if limit else recent_logs
        vals = []
        for r in rows:
            for k in keys:
                if k in r and r[k] is not None:
                    vals.append(float(r[k]))
                    break
        return round(sum(vals) / len(vals), 2) if vals else None

    def _pick_stat(keys: Tuple[str, ...], default_val: float) -> float:
        for k in keys:
            if k in season_averages and season_averages[k] is not None:
                return float(season_averages[k])
        log_avg = _avg_from_logs(keys)
        if log_avg is not None:
            return log_avg
        return default_val

    pos_u = str(position or "").upper()
    default_3pm = 0.3 if "C" in pos_u and "G" not in pos_u else (1.5 if "F" in pos_u and "G" not in pos_u else 2.2)

    pts = _pick_stat(("PTS", "POINTS", "AVG"), 18.5)
    reb = _pick_stat(("REB", "REBOUNDS", "TREB"), 5.5)
    ast = _pick_stat(("AST", "ASSISTS"), 4.2)
    three_pt = _pick_stat(("3PM", "3PT", "FG3M", "3P", "THREES"), default_3pm)
    minutes_avg = _pick_stat(("MIN", "MPG", "MINUTES"), 31.5)
    pass_yds = _pick_stat(("YDS", "PASS_YDS", "PASSINGYARDS"), 235.0)
    rush_yds = _pick_stat(("RUSH_YDS", "RUSHINGYARDS"), 60.0)
    rec_yds = _pick_stat(("REC_YDS", "RECEIVINGYARDS"), 52.0)
    sog = _pick_stat(("SOG", "SHOTS"), 2.8)
    strikeouts = _pick_stat(("SO", "K", "STRIKEOUTS"), 5.5)

    last5_averages = {
        "PTS": _avg_from_logs(("PTS", "POINTS", "AVG"), 5),
        "REB": _avg_from_logs(("REB", "REBOUNDS", "TREB"), 5),
        "AST": _avg_from_logs(("AST", "ASSISTS"), 5),
        "3PM": _avg_from_logs(("3PM", "3PT", "FG3M", "3P", "THREES"), 5),
        "MIN": _avg_from_logs(("MIN", "MPG", "MINUTES"), 5),
        "PRA": None,
    }
    if last5_averages["PTS"] is not None and last5_averages["REB"] is not None and last5_averages["AST"] is not None:
        last5_averages["PRA"] = round(last5_averages["PTS"] + last5_averages["REB"] + last5_averages["AST"], 2)

    def _pick_prior_stat(keys: Tuple[str, ...]) -> Optional[float]:
        for k in keys:
            if k in prior_season_averages and prior_season_averages[k] is not None:
                return float(prior_season_averages[k])
        return None

    prior_pts = _pick_prior_stat(("PTS", "POINTS", "AVG"))
    prior_reb = _pick_prior_stat(("REB", "REBOUNDS", "TREB"))
    prior_ast = _pick_prior_stat(("AST", "ASSISTS"))
    prior_3pm = _pick_prior_stat(("3PM", "3PT", "FG3M", "3P", "THREES"))
    prior_pra = (
        round(prior_pts + prior_reb + prior_ast, 2)
        if (prior_pts is not None and prior_reb is not None and prior_ast is not None)
        else None
    )

    result = {
        "_ts": time.time(),
        "athlete_id": eff_athlete_id,
        "full_name": full_name,
        "clean_full": strip_accents(full_name),
        "team_abbr": team_abbr.upper(),
        "position": position,
        "jersey": jersey,
        "matchup": matchup,
        "opp_team": opp_team or "OPP",
        "opp_factor": opp_factor,
        "is_home": is_home,
        "games_played": games_played,
        "minutes_avg": round(minutes_avg, 1),
        "last5_minutes": last5_averages["MIN"],
        "last5_averages": last5_averages,
        "recent_logs_count": len(recent_logs),
        "game_date": game_date,
        "active_teammates": active_teammates,
        "out_teammates": out_teammates,
        "prior_season_averages": {
            "PTS": prior_pts,
            "REB": prior_reb,
            "AST": prior_ast,
            "3PM": prior_3pm,
            "PRA": prior_pra,
        },
        "season_averages": {
            "PTS": pts,
            "REB": reb,
            "AST": ast,
            "3PM": three_pt,
            "PRA": round(pts + reb + ast, 2),
            "MIN": minutes_avg,
            "PASS_YDS": pass_yds,
            "RUSH_YDS": rush_yds,
            "REC_YDS": rec_yds,
            "SOG": sog,
            "K": strikeouts,
        },
    }

    _ESPN_ATHLETE_CACHE[cache_key] = result
    return result


def get_real_open_markets_summary(
    sport: str = "NBA",
    query: Optional[str] = None,
    limit: int = 3,
) -> List[str]:
    """
    Returns summary lines of real open daily Kalshi milestone markets for Telegram display.
    Strictly filters by sports prefix (KXNBA for NBA) and positive ask prices.
    Never returns 0c, never returns KXUSCPI.
    """
    sport_u = sport.upper()
    sport_prefix = f"KX{sport_u}"

    if query:
        mkts = search_kalshi_markets(query, sport=sport_u)
    else:
        mkts = get_all_daily_kalshi_markets()

    # Filter strictly by prefix and liquid ask price > 0
    liquid_markets = [
        m for m in mkts
        if str(m.get("ticker", "")).upper().startswith(sport_prefix)
        and (int(m.get("yes_ask", 0)) > 0 or int(m.get("no_ask", 0)) > 0)
    ]

    if not liquid_markets:
        # Fallback to all daily open markets for this sport with positive ask
        all_daily = get_all_daily_kalshi_markets()
        liquid_markets = [
            m for m in all_daily
            if str(m.get("ticker", "")).upper().startswith(sport_prefix)
            and (int(m.get("yes_ask", 0)) > 0 or int(m.get("no_ask", 0)) > 0)
        ]

    lines = []
    for m in liquid_markets[:limit]:
        tk = m.get("ticker", "")
        strike = m.get("line", 0)
        stat = m.get("stat", "PTS")
        ask = int(m.get("yes_ask") or m.get("no_ask") or 0)
        if ask <= 0:
            continue
        lines.append(f"• <code>{html.escape(tk)}</code> ({strike}+ {stat} @ {ask}¢)")
    return lines
