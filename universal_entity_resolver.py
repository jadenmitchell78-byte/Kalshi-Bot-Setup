# ~/peezy_bot/universal_entity_resolver.py
"""
Universal All-Sport Player Abbreviation, Unicode Normalizer, and Kalshi Ticker Parser:
1. Strips all diacritics/accents (č -> c, ć -> c, ñ -> n, ü -> u, é -> e, etc.)
2. Normalizes punctuation, initials, apostrophes, and suffixes (Jr., Sr., II, III, IV)
3. Generates all 6 canonical match keys (full_clean, first_name, last_name, initial_last, kalshi_slugs, consonant_skeleton)
4. Parses live Kalshi tickers across NBA, NFL, NHL, MLB, Tennis, CFB, CBB, WNBA, Soccer, and MMA:
   e.g. KXNBA3PT-26OCT27PORLAL-LALLDONCIC77-4 -> Luka Dončić 4+ 3PM
   e.g. KXNBAPTS-26OCT27PORLAL-LALLDONCIC77-35 -> Luka Dončić 35+ PTS
5. Resolves multi-rung ladders (25+, 30+, 35+, 40+ PTS or 3+, 4+, 5+ 3PM)
6. Sliding 3-day date window (yesterday, today, tomorrow) accounting for UTC/EST/PST rollover
"""

import datetime
import math
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

# ---------------------------------------------------------------------
# 1. UNIVERSAL UNICODE & PUNCTUATION NORMALIZER
# ---------------------------------------------------------------------
SUFFIXES_REGEX = re.compile(r"\b(jr\.?|sr\.?|ii|iii|iv|v)\b", re.I)
PUNCTUATION_REGEX = re.compile(r"['`\.\-\_]+")
NON_ALPHANUM_REGEX = re.compile(r"[^a-z0-9\s]")


def clean_token(text: Any) -> str:
    """
    Strips all diacritics/accents, lowercases, removes suffixes, and cleans punctuation.
    Examples:
      'Luka Dončić' -> 'luka doncic'
      'L. Dončić' -> 'l doncic'
      'D\'Angelo Russell' -> 'dangelo russell'
      'Shai Gilgeous-Alexander' -> 'shai gilgeous alexander'
      'Nikola Jokić' -> 'nikola jokic'
      'A. Reaves' -> 'a reaves'
    """
    if text is None:
        return ""
    s = str(text)
    # 1. Decompose unicode accents into ASCII base + combining mark
    s_norm = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s_norm = s_norm.lower()

    # 2. Strip suffixes
    s_norm = SUFFIXES_REGEX.sub("", s_norm)

    # 3. Strip apostrophes completely (D'Angelo -> Dangelo)
    s_norm = s_norm.replace("'", "").replace("’", "").replace("`", "")

    # 4. Replace dots, hyphens, and underscores with space
    s_norm = re.sub(r"[\.\-\_]+", " ", s_norm)

    # 5. Clean remaining non-alphanumeric chars
    s_norm = NON_ALPHANUM_REGEX.sub(" ", s_norm)

    return " ".join(s_norm.split())


def get_consonant_skeleton(text: str) -> str:
    """Removes vowels to form a robust skeleton for phonetic/typo matching."""
    cleaned = clean_token(text).replace(" ", "")
    return "".join(c for c in cleaned if c not in "aeiouy")


# ---------------------------------------------------------------------
# 2. SLIDING 3-DAY DATE WINDOW (HANDLES 8:00 PM UTC ROLLOVER)
# ---------------------------------------------------------------------
def get_active_date_codes() -> Set[str]:
    """
    Generates a 3-day set of Kalshi date codes: [yesterday, today, tomorrow].
    Uses US Pacific/Mountain offset (UTC - 7 hours) so an 8:00 PM game (03:00 UTC)
    never rolls over and drops tonight's slate!
    Example: {'26OCT26', '26OCT27', '26OCT28'}
    """
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    # Shift back 7 hours for US Pacific / Mountain game night alignment
    us_game_night = utc_now - datetime.timedelta(hours=7)

    codes = set()
    for day_offset in (-1, 0, 1):
        dt = us_game_night + datetime.timedelta(days=day_offset)
        # Format: 26OCT27 (2-digit year, 3-char upper month, 2-digit day)
        code = dt.strftime("%y%b%d").upper()
        codes.add(code)

    # Also add pure UTC today just in case
    codes.add(utc_now.strftime("%y%b%d").upper())
    return codes


# ---------------------------------------------------------------------
# 3. UNIVERSAL STAT ALIAS MAP (ALL SPORTS)
# ---------------------------------------------------------------------
UNIVERSAL_STAT_ALIASES = {
    # Basketball
    "PTS": "PTS", "POINTS": "PTS", "POINT": "PTS",
    "REB": "REB", "REBS": "REB", "REBOUND": "REB", "REBOUNDS": "REB",
    "AST": "AST", "ASTS": "AST", "ASSIST": "AST", "ASSISTS": "AST",
    "3PT": "3PM", "3PTS": "3PM", "3PM": "3PM", "THREE": "3PM", "THREES": "3PM", "3S": "3PM",
    "2D": "2D", "DOUBLEDOUBLE": "2D", "DOUBLE DOUBLE": "2D", "DD": "2D",
    "3D": "3D", "TRIPLEDOUBLE": "3D", "TRIPLE DOUBLE": "3D", "TD3": "3D",
    "PRA": "PRA", "PR": "PR", "PA": "PA", "RA": "RA",
    "BLK": "BLK", "BLOCK": "BLK", "BLOCKS": "BLK",
    "STL": "STL", "STEAL": "STL", "STEALS": "STL",
    "TOV": "TOV", "TURNOVER": "TOV", "TURNOVERS": "TOV",
    "GAME": "GAME", "MONEYLINE": "GAME", "ML": "GAME", "WIN": "GAME",
    "SPREAD": "SPREAD", "TOTAL": "TOTAL", "TEAMTOTAL": "TEAMTOTAL",
    # Football
    "PASS": "PASS_YDS", "PASSING": "PASS_YDS", "PASSYDS": "PASS_YDS", "PASS YDS": "PASS_YDS", "PASS_YDS": "PASS_YDS",
    "PASSTD": "PASS_TD", "PASSING TD": "PASS_TD", "PASS TDS": "PASS_TD",
    "RUSH": "RUSH_YDS", "RUSHING": "RUSH_YDS", "RUSHYDS": "RUSH_YDS", "RUSH YDS": "RUSH_YDS", "RUSH_YDS": "RUSH_YDS",
    "REC": "REC_YDS", "RECEIVING": "REC_YDS", "RECYDS": "REC_YDS", "REC YDS": "REC_YDS", "REC_YDS": "REC_YDS",
    "RECEPTIONS": "RECEPTIONS",
    "TD": "TD", "TOUCHDOWN": "TD", "TOUCHDOWNS": "TD", "ANYTD": "TD",
    # Hockey / Soccer / Baseball
    "GOALS": "GOALS", "GOAL": "GOALS", "G": "GOALS",
    "SOG": "SOG", "SHOTS": "SOG", "SHOTS ON GOAL": "SOG",
    "SAVES": "SAVES",
    "HITS": "HITS", "HIT": "HITS",
    "STRIKEOUTS": "K", "STRIKEOUT": "K", "K": "K", "SO": "K",
    "HR": "HR", "HOMERUN": "HR", "HOME RUN": "HR",
    # Tennis
    "GAMES": "GAMES", "SETS": "SETS", "ACES": "ACES",
}

# ---------------------------------------------------------------------
# 4. KALSHI ALL-SPORT SERIES WHITELIST
# ---------------------------------------------------------------------
ALL_SPORT_SERIES = [
    # NBA
    "KXNBAPTS", "KXNBAREB", "KXNBAAST", "KXNBA3PT", "KXNBATHREES", "KXNBAPRA",
    "KXNBA2D", "KXNBA3D", "KXNBAGAME", "KXNBASPREAD", "KXNBATOTAL",
    "KXNBATEAMTOTAL", "KXNBA1HSPREAD", "KXNBA2HTOTAL", "KXNBA1QTOTAL",
    # NFL / CFB
    "KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLPASS", "KXNFLRUSH", "KXNFLREC", "KXNFLTD",
    "KXCFBGAME", "KXCFBSPREAD", "KXCFBTOTAL",
    # NHL
    "KXNHLGAME", "KXNHLSPREAD", "KXNHLTOTAL", "KXNHLSOG", "KXNHLPOINTS",
    # Tennis
    "KXTENNIS", "KXATP", "KXWTA",
    # MLB
    "KXMLBGAME", "KXMLBSPREAD", "KXMLBTOTAL", "KXMLBK", "KXMLBHITS",
    # WNBA / CBB / Soccer / MMA
    "KXWNBA", "KXCBB", "KXSOCCER", "KXUFC", "KXMMA",
]


# ---------------------------------------------------------------------
# 5. ALL-SPORT LIVE ESPN ROSTER & ABBREVIATION INDEXER
# ---------------------------------------------------------------------
_UNIVERSAL_ROSTER_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
ROSTER_CACHE_TTL = 900.0  # 15 minutes


def build_universal_roster_index(sport: str = "NBA", force_refresh: bool = False) -> Dict[str, Any]:
    """
    Pulls today's ESPN scoreboards and rosters across active sports.
    Builds a deep player dictionary with all 6 canonical match keys.
    Cached in-memory for 15 minutes.
    """
    sport_u = sport.upper()
    now = time.time()
    if not force_refresh and sport_u in _UNIVERSAL_ROSTER_CACHE:
        ts, cached_data = _UNIVERSAL_ROSTER_CACHE[sport_u]
        if now - ts < ROSTER_CACHE_TTL and cached_data:
            return cached_data

    path_map = {
        "NBA": "basketball/nba",
        "NFL": "football/nfl",
        "NHL": "hockey/nhl",
        "MLB": "baseball/mlb",
        "CFB": "football/college-football",
        "CBB": "basketball/mens-college-basketball",
        "TENNIS": "tennis/atp",
        "WNBA": "basketball/wnba",
        "SOCCER": "soccer/eng.1",
        "UFC": "mma/ufc",
    }
    espn_path = path_map.get(sport_u, "basketball/nba")
    url = f"https://site.api.espn.com/apis/site/v2/sports/{espn_path}/scoreboard"

    players_by_key: Dict[str, Dict[str, Any]] = {}
    active_teams: Set[str] = set()

    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            events = r.json().get("events", [])
            for ev in events:
                ev_id = ev.get("id")
                comps = ev.get("competitions", [{}])[0]
                competitors = comps.get("competitors", [])

                home_abbr, away_abbr = "HOME", "AWAY"
                for comp in competitors:
                    t = comp.get("team", {})
                    abbr = str(t.get("abbreviation") or "").upper()
                    if comp.get("homeAway") == "home":
                        home_abbr = abbr
                    else:
                        away_abbr = abbr
                    if abbr:
                        active_teams.add(abbr)

                matchup = f"{away_abbr} @ {home_abbr}"
                raw_date_iso = ev.get("date", "")
                game_date_str = "Today • Live Slate"
                if raw_date_iso:
                    try:
                        dt_utc = datetime.datetime.fromisoformat(raw_date_iso.replace("Z", "+00:00"))
                        # Shift back 7 hours for US Pacific / Mountain game night alignment
                        dt_local = dt_utc - datetime.timedelta(hours=7)
                        game_date_str = dt_local.strftime("%Y-%m-%d")
                    except Exception:
                        game_date_str = raw_date_iso[:10]

                # Fetch boxscore / team summaries
                sum_url = f"https://site.api.espn.com/apis/site/v2/sports/{espn_path}/summary?event={ev_id}"
                try:
                    sr = requests.get(sum_url, timeout=4)
                    if sr.status_code == 200:
                        s_data = sr.json()
                        box = s_data.get("boxscore", {}).get("players", [])
                        for team_box in box:
                            t_ab = (team_box.get("team", {}).get("abbreviation") or "").upper()
                            stats_blocks = team_box.get("statistics", [])
                            if not stats_blocks:
                                continue
                            for ath in stats_blocks[0].get("athletes", []):
                                a_obj = ath.get("athlete", {})
                                full_name = a_obj.get("displayName") or a_obj.get("fullName") or ""
                                if not full_name:
                                    continue
                                jersey = str(a_obj.get("jersey") or "")
                                pos = str(a_obj.get("position", {}).get("abbreviation") or "")

                                p_entry = _create_athlete_entry(
                                    full_name=full_name,
                                    team_abbr=t_ab,
                                    jersey=jersey,
                                    position=pos,
                                    sport=sport_u,
                                    matchup=matchup,
                                    game_date=game_date_str,
                                )

                                for k in p_entry["search_keys"]:
                                    players_by_key[k] = p_entry
                except Exception:
                    pass
    except Exception:
        pass

    # Merge well-known superstars so off-day lookups still resolve canonical keys
    for super_entry in _KNOWN_SUPERSTARS.get(sport_u, []):
        p_entry = _create_athlete_entry(
            full_name=super_entry["name"],
            team_abbr=super_entry["team"],
            jersey=super_entry.get("jersey", ""),
            position=super_entry.get("pos", ""),
            sport=sport_u,
            matchup=super_entry.get("matchup", f"{super_entry['team']} Game"),
            game_date=super_entry.get("game_date", "Today • Live Slate"),
        )
        for k in p_entry["search_keys"]:
            if k not in players_by_key:
                players_by_key[k] = p_entry

    _UNIVERSAL_ROSTER_CACHE[sport_u] = (now, players_by_key)
    return players_by_key


def _create_athlete_entry(
    full_name: str,
    team_abbr: str,
    jersey: str,
    position: str,
    sport: str,
    matchup: str,
    game_date: str,
) -> Dict[str, Any]:
    """Generates all 6 canonical match keys for an athlete."""
    clean_full = clean_token(full_name)
    parts = clean_full.split()
    first_name = parts[0] if parts else ""
    last_name = parts[-1] if len(parts) > 1 else first_name
    first_initial = first_name[0] if first_name else ""

    # 1. Full clean
    full_clean = clean_full

    # 2. Initial + Last: 'l doncic', 'ldoncic'
    initial_last_space = f"{first_initial} {last_name}".strip()
    initial_last_concat = f"{first_initial}{last_name}".strip()

    # 3. Kalshi Ticker Slugs:
    # Example: LAL + L + DONCIC + 77 -> 'lalldoncic77'
    # Also without team: 'ldoncic77', 'doncic77', 'lalldoncic'
    clean_team = clean_token(team_abbr).replace(" ", "")
    clean_jersey = re.sub(r"[^\d]", "", str(jersey))

    slugs = set()
    if clean_team and first_initial and last_name:
        slugs.add(f"{clean_team}{first_initial}{last_name}{clean_jersey}")
        slugs.add(f"{clean_team}{first_initial}{last_name}")
        slugs.add(f"{clean_team}{last_name}{clean_jersey}")
        slugs.add(f"{clean_team}{last_name}")

    if first_initial and last_name:
        slugs.add(f"{first_initial}{last_name}{clean_jersey}")
        slugs.add(f"{first_initial}{last_name}")

    if last_name:
        slugs.add(f"{last_name}{clean_jersey}")
        slugs.add(last_name)

    # 4. Consonant skeleton
    skeleton = get_consonant_skeleton(clean_full)

    search_keys = set()
    search_keys.add(full_clean)
    if first_name and len(first_name) >= 2:
        search_keys.add(first_name)
    if last_name and len(last_name) >= 2:
        search_keys.add(last_name)
    search_keys.add(initial_last_space)
    search_keys.add(initial_last_concat)
    for s in slugs:
        if s:
            search_keys.add(s)

    return {
        "full_name": full_name,
        "clean_full": clean_full,
        "first_name": first_name,
        "last_name": last_name,
        "team_abbr": team_abbr.upper(),
        "jersey": clean_jersey,
        "position": position,
        "sport": sport.upper(),
        "matchup": matchup,
        "game_date": game_date,
        "search_keys": search_keys,
        "skeleton": skeleton,
        "slugs": list(slugs),
    }


# Fallback table of prominent athletes across all sports
_KNOWN_SUPERSTARS = {
    "NBA": [
        {"name": "Luka Doncic", "team": "LAL", "jersey": "77", "pos": "PG"},
        {"name": "Ja Morant", "team": "MEM", "jersey": "12", "pos": "PG"},
        {"name": "Desmond Bane", "team": "MEM", "jersey": "22", "pos": "SG"},
        {"name": "Jaren Jackson Jr.", "team": "MEM", "jersey": "13", "pos": "PF"},
        {"name": "Stephen Curry", "team": "GSW", "jersey": "30", "pos": "PG"},
        {"name": "LeBron James", "team": "LAL", "jersey": "23", "pos": "SF"},
        {"name": "Anthony Davis", "team": "LAL", "jersey": "3", "pos": "C"},
        {"name": "Austin Reaves", "team": "LAL", "jersey": "15", "pos": "SG"},
        {"name": "Damian Lillard", "team": "MIL", "jersey": "0", "pos": "PG"},
        {"name": "Giannis Antetokounmpo", "team": "MIL", "jersey": "34", "pos": "PF"},
        {"name": "Nikola Jokic", "team": "DEN", "jersey": "15", "pos": "C"},
        {"name": "Anthony Edwards", "team": "MIN", "jersey": "5", "pos": "SG"},
        {"name": "Julius Randle", "team": "MIN", "jersey": "30", "pos": "PF"},
        {"name": "Shai Gilgeous-Alexander", "team": "OKC", "jersey": "2", "pos": "PG"},
        {"name": "Jalen Brunson", "team": "NYK", "jersey": "11", "pos": "PG"},
        {"name": "Victor Wembanyama", "team": "SAS", "jersey": "1", "pos": "C"},
        {"name": "Tyrese Maxey", "team": "PHI", "jersey": "0", "pos": "PG"},
        {"name": "Cade Cunningham", "team": "DET", "jersey": "2", "pos": "PG"},
        {"name": "Draymond Green", "team": "GSW", "jersey": "23", "pos": "PF"},
        {"name": "Tyler Herro", "team": "MIA", "jersey": "14", "pos": "SG"},
        {"name": "Kawhi Leonard", "team": "LAC", "jersey": "2", "pos": "SF"},
        {"name": "Kyrie Irving", "team": "DAL", "jersey": "11", "pos": "PG"},
        {"name": "Devin Booker", "team": "PHX", "jersey": "1", "pos": "SG"},
        {"name": "Kevin Durant", "team": "PHX", "jersey": "35", "pos": "PF"},
        {"name": "Jayson Tatum", "team": "BOS", "jersey": "0", "pos": "SF"},
        {"name": "Jaylen Brown", "team": "BOS", "jersey": "7", "pos": "SG"},
        {"name": "Trae Young", "team": "ATL", "jersey": "11", "pos": "PG"},
        {"name": "Zion Williamson", "team": "NOP", "jersey": "1", "pos": "PF"},
        {"name": "De'Aaron Fox", "team": "SAC", "jersey": "5", "pos": "PG"},
        {"name": "Domantas Sabonis", "team": "SAC", "jersey": "11", "pos": "C"},
    ],
    "NFL": [
        {"name": "Patrick Mahomes", "team": "KC", "jersey": "15", "pos": "QB"},
        {"name": "Lamar Jackson", "team": "BAL", "jersey": "8", "pos": "QB"},
        {"name": "Josh Allen", "team": "BUF", "jersey": "17", "pos": "QB"},
        {"name": "Saquon Barkley", "team": "PHI", "jersey": "26", "pos": "RB"},
        {"name": "Derrick Henry", "team": "BAL", "jersey": "22", "pos": "RB"},
        {"name": "Justin Jefferson", "team": "MIN", "jersey": "18", "pos": "WR"},
        {"name": "CeeDee Lamb", "team": "DAL", "jersey": "88", "pos": "WR"},
        {"name": "Travis Kelce", "team": "KC", "jersey": "87", "pos": "TE"},
    ],
    "NHL": [
        {"name": "Connor McDavid", "team": "EDM", "jersey": "97", "pos": "C"},
        {"name": "Nathan MacKinnon", "team": "COL", "jersey": "29", "pos": "C"},
        {"name": "Auston Matthews", "team": "TOR", "jersey": "34", "pos": "C"},
        {"name": "Nikita Kucherov", "team": "TBL", "jersey": "86", "pos": "RW"},
        {"name": "Jake Evans", "team": "MTL", "jersey": "71", "pos": "C"},
        {"name": "Sebastian Aho", "team": "CAR", "jersey": "20", "pos": "C"},
    ],
    "TENNIS": [
        {"name": "Jannik Sinner", "team": "ATP", "jersey": "", "pos": "ATP"},
        {"name": "Carlos Alcaraz", "team": "ATP", "jersey": "", "pos": "ATP"},
        {"name": "Novak Djokovic", "team": "ATP", "jersey": "", "pos": "ATP"},
        {"name": "Alexander Zverev", "team": "ATP", "jersey": "", "pos": "ATP"},
        {"name": "Daniil Medvedev", "team": "ATP", "jersey": "", "pos": "ATP"},
        {"name": "Taylor Fritz", "team": "ATP", "jersey": "", "pos": "ATP"},
        {"name": "Aryna Sabalenka", "team": "WTA", "jersey": "", "pos": "WTA"},
        {"name": "Iga Swiatek", "team": "WTA", "jersey": "", "pos": "WTA"},
        {"name": "Coco Gauff", "team": "WTA", "jersey": "", "pos": "WTA"},
    ],
}


# ---------------------------------------------------------------------
# 6. UNIVERSAL ATHLETE RESOLVER (FIND ANY PLAYER BY NICKNAME/TYPO)
# ---------------------------------------------------------------------
def resolve_athlete_universal(query_str: str, sport: str = "NBA") -> Dict[str, Any]:
    """
    Given any raw string ('luka', 'doncic', 'L. Dončić', 'kawahi', 'yaxel', 'reaves', 'lillard'),
    resolves the canonical athlete entry with search keys and team info.
    """
    sport_u = sport.upper()
    roster_index = build_universal_roster_index(sport_u)
    clean_q = clean_token(query_str)

    # 1. Exact match in indexed keys
    if clean_q in roster_index:
        return roster_index[clean_q]

    # 2. Check if clean_q matches any sub-token
    for k, entry in roster_index.items():
        if clean_q == entry["last_name"] or clean_q == entry["first_name"]:
            return entry
        if clean_q in entry["clean_full"] or entry["clean_full"] in clean_q:
            return entry

    # 3. Fuzzy match using SequenceMatcher
    best_entry = None
    best_score = 0.0
    q_skeleton = get_consonant_skeleton(clean_q)

    for k, entry in roster_index.items():
        # Test full clean
        sc = SequenceMatcher(None, clean_q, entry["clean_full"]).ratio()
        if sc > best_score:
            best_score = sc
            best_entry = entry

        # Test last name
        sc_last = SequenceMatcher(None, clean_q, entry["last_name"]).ratio()
        if sc_last > best_score:
            best_score = sc_last
            best_entry = entry

        # Skeleton match for typos (e.g. 'kawahi' -> 'kwh', 'kawhi' -> 'kwh')
        if q_skeleton and entry["skeleton"] and q_skeleton == entry["skeleton"]:
            return entry

    if best_entry and best_score >= 0.72:
        return best_entry

    # 4. Fallback to live ESPN athlete search if not found
    try:
        encoded_q = requests.utils.quote(query_str)
        s_url = f"https://site.web.api.espn.com/apis/common/v3/search?query={encoded_q}&limit=5&type=player"
        r = requests.get(s_url, timeout=3.5)
        if r.status_code == 200:
            items = r.json().get("items", [])
            for it in items:
                if it.get("type") == "player" or "athlete" in str(it.get("url", "")):
                    f_name = it.get("displayName") or it.get("title") or query_str
                    entry = _create_athlete_entry(
                        full_name=f_name,
                        team_abbr=sport_u,
                        jersey="",
                        position="",
                        sport=sport_u,
                        matchup=f"{sport_u} Matchup",
                        game_date="Today • Live Slate",
                    )
                    return entry
    except Exception:
        pass

    # Generic fallback
    return _create_athlete_entry(
        full_name=query_str.title(),
        team_abbr=sport_u,
        jersey="",
        position="",
        sport=sport_u,
        matchup=f"{sport_u} Matchup",
        game_date="Today • Live Slate",
    )


# ---------------------------------------------------------------------
# 7. UNIVERSAL KALSHI MARKET EXTRACTOR & LADDER PARSER
# ---------------------------------------------------------------------
# Kalshi Ticker Regex:
# e.g. KXNBA3PT-26OCT27PORLAL-LALLDONCIC77-4
# e.g. KXNBAPTS-26OCT27PORLAL-LALLDONCIC77-35
# e.g. KXNBAGAME-26OCT27PORLAL-LAL
# e.g. KXNBASPREAD-26OCT27PORLAL-LAL4
# e.g. KXNBATOTAL-26OCT27PORLAL-236
TICKER_PARSER_REGEX = re.compile(
    r"^KX(?P<sport>NBA|NFL|NHL|MLB|TENNIS|CFB|CBB|WNBA|UFC|SOCCER)(?P<stat>[A-Z0-9]*)-(?P<date>26[A-Z]{3}\d{2})(?P<matchup>[A-Z]+)-(?P<entity>[A-Z0-9]+)(?:-(?P<line>[\d\.]+))?$",
    re.I,
)


def extract_player_and_stat_from_kalshi_market(m: Dict[str, Any]) -> Dict[str, Any]:
    """
    Inspects EVERY field in a Kalshi market dict (ticker, title, subtitle, yes_sub_title, rules_primary).
    Extracts:
      - sport: 'NBA', 'NFL', etc.
      - stat: 'PTS', '3PM', 'AST', 'REB', 'GAME', 'SPREAD', 'TOTAL'
      - date: '26OCT27'
      - matchup: 'PORLAL'
      - entity: 'LALLDONCIC77'
      - raw_player_extracted: 'LDONCIC' / 'Luka Dončić'
      - line: 34.5 (from milestone 35+ or continuous line)
      - milestone_label: '35+' or '4+'
      - yes_ask, no_ask, yes_bid, no_bid
      - searchable_blob: clean ASCII tokens for fast matching
    """
    tk = str(m.get("ticker") or "").upper()
    title = str(m.get("title") or "")
    subtitle = str(m.get("subtitle") or "")
    yes_sub = str(m.get("yes_sub_title") or "")
    no_sub = str(m.get("no_sub_title") or "")
    rules = str(m.get("rules_primary") or "")

    combined_text = f"{tk} {title} {subtitle} {yes_sub} {no_sub} {rules}".replace(",", "")
    searchable_blob = clean_token(combined_text)

    # Hard block career / season / record / award futures
    is_futures = any(bad in combined_text.upper() for bad in ("CAREER", "SEASON", "RECORD", "AWARD", "LEADER", "HOF", "CHAMP", "HISTORIC", "ALLTIME", "ALL-TIME"))
    if not re.search(r"26(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}", tk):
        is_futures = True

    # 1. Parse Ticker Structure
    sport = "NBA"
    if tk.startswith("KXNHL") or "NHL" in tk:
        sport = "NHL"
    elif tk.startswith(("KXNFL", "KXCFB")) or "NFL" in tk:
        sport = "NFL"
    elif tk.startswith("KXMLB") or "MLB" in tk:
        sport = "MLB"
    elif tk.startswith("KXTENNIS") or "TENNIS" in tk:
        sport = "TENNIS"
    elif tk.startswith("KXWNBA") or "WNBA" in tk:
        sport = "WNBA"

    stat = "POINTS" if sport == "NHL" else "PTS"
    date_code = ""
    matchup = ""
    entity = ""
    line = None
    milestone_label = ""

    tk_match = TICKER_PARSER_REGEX.match(tk)
    if tk_match:
        sport = tk_match.group("sport").upper()
        raw_stat = tk_match.group("stat").upper()
        date_code = tk_match.group("date").upper()
        matchup = tk_match.group("matchup").upper()
        entity = tk_match.group("entity").upper()
        raw_line = tk_match.group("line")

        stat = UNIVERSAL_STAT_ALIASES.get(raw_stat, raw_stat if raw_stat else ("POINTS" if sport == "NHL" else "PTS"))

        if raw_line is not None:
            try:
                line_float = float(raw_line)
                # On Kalshi milestone props, e.g. 35 -> 35+ means Over 34.5
                line = line_float - 0.5 if line_float > 0.5 else line_float
                if line <= 0:
                    line = None
                else:
                    milestone_label = f"{int(line_float)}+" if line_float.is_integer() else f"{line_float}+"
            except Exception:
                pass

    # 2. Extract line from yes_sub_title or title if not yet resolved
    if line is None:
        # Check patterns like "35+" or "35 or more" or "Over 34.5"
        m_plus = re.search(r"(\d+(?:\.\d+)?)\s*(?:\+|or more)", combined_text, re.I)
        if m_plus:
            val = float(m_plus.group(1))
            line = val - 0.5 if val > 0.5 else None
            if line is not None:
                milestone_label = f"{int(val)}+" if val.is_integer() else f"{val}+"
        else:
            m_dec = re.search(r"(?:over|under|o|u)?\s*(\d+\.5)", combined_text, re.I)
            if m_dec:
                val = float(m_dec.group(1))
                if val > 0:
                    line = val
                    milestone_label = str(line)

    # If sport is NHL, ensure stat is strictly NHL stat
    if sport == "NHL":
        if any(k in searchable_blob for k in ("goal", "goals")):
            stat = "GOALS"
        elif any(k in searchable_blob for k in ("assist", "assists")):
            stat = "ASSISTS"
        elif any(k in searchable_blob for k in ("sog", "shot", "shots")):
            stat = "SOG"
        elif any(k in searchable_blob for k in ("save", "saves")):
            stat = "SAVES"
        elif stat in ("PASS_YDS", "RUSH_YDS", "REC_YDS", "PASS", "RUSH", "REC"):
            stat = "POINTS"
        elif not stat or stat == "PTS":
            stat = "POINTS"

    # 3. Resolve Stat from Text if not resolved from ticker
    if stat in ("PTS", "POINTS") and tk_match is None and sport != "NHL":
        for alias_k, canonical_stat in UNIVERSAL_STAT_ALIASES.items():
            if re.search(r"\b" + re.escape(alias_k.lower()) + r"\b", searchable_blob):
                stat = canonical_stat
                break

    # 4. Resolve Prices
    yes_ask = int(m.get("yes_ask") or 0)
    no_ask = int(m.get("no_ask") or 0)
    yes_bid = int(m.get("yes_bid") or 0)
    no_bid = int(m.get("no_bid") or 0)
    last_price = int(m.get("last_price") or 0)

    market_yes_ask = yes_ask if (2 <= yes_ask <= 97) else (last_price if (2 <= last_price <= 97) else 50)
    market_no_ask = no_ask if (2 <= no_ask <= 97) else (100 - market_yes_ask)

    default_line = 1.5 if sport == "NHL" else (245.5 if sport == "NFL" and stat == "PASS_YDS" else (64.5 if sport == "NFL" and stat == "RUSH_YDS" else (2.0 if sport == "TENNIS" else (20.5 if stat in ("PTS", "POINTS") else 5.5))))
    eff_line = line if (line is not None and line > 0) else default_line
    if is_futures:
        eff_line = -1.0

    return {
        "ticker": tk,
        "sport": sport,
        "stat": stat,
        "date_code": date_code,
        "matchup": matchup,
        "entity": entity,
        "line": eff_line,
        "milestone_label": milestone_label or (f"{int(eff_line+0.5)}+" if eff_line > 0 else ""),
        "yes_ask": market_yes_ask,
        "no_ask": market_no_ask,
        "yes_bid": yes_bid,
        "no_bid": no_bid,
        "searchable_blob": searchable_blob,
        "raw_market": m,
    }


def match_player_to_market(athlete_entry: Dict[str, Any], market_info: Dict[str, Any]) -> bool:
    """
    Checks if a Kalshi market matches the resolved athlete entry using all 6 keys.
    Matches across:
      1. full_clean: 'luka doncic' in searchable_blob
      2. initial_last: 'l doncic' in searchable_blob
      3. last_name: 'doncic' in searchable_blob or ticker entity
      4. kalshi ticker slugs: e.g. 'lalldoncic77', 'ldoncic77', 'doncic77' in market ticker or entity!
    """
    blob = market_info["searchable_blob"]
    tk_upper = market_info["ticker"].upper()
    entity_upper = market_info["entity"].upper()
    entity_clean = clean_token(entity_upper).replace(" ", "")

    # 1. Full clean name match
    if athlete_entry["clean_full"] in blob:
        return True

    # 2. Initial + Last match ('l doncic')
    first_init_last = f"{athlete_entry['first_name'][:1]} {athlete_entry['last_name']}".strip()
    if first_init_last in blob:
        return True

    # 3. Ticker Slug Matches (e.g. LALLDONCIC77, LDONCIC, DONCIC)
    for slug in athlete_entry["slugs"]:
        slug_clean = slug.upper()
        if slug_clean in tk_upper or slug_clean in entity_upper:
            return True

    # 4. Check entity string against athlete last name
    last_clean = athlete_entry["last_name"].upper()
    if len(last_clean) >= 4 and last_clean in entity_upper:
        return True

    # 5. Check last name in title/blob
    if len(last_clean) >= 4 and (f" {athlete_entry['last_name']} " in f" {blob} "):
        # Ensure we didn't collide on a super common word
        if athlete_entry["first_name"] in blob or athlete_entry["team_abbr"].lower() in blob:
            return True

    return False


def find_player_ladders(
    player_query: str,
    stat_query: Optional[str] = None,
    markets: Optional[List[Dict[str, Any]]] = None,
    sport: str = "NBA",
) -> List[Dict[str, Any]]:
    """
    Sweeps the market pool for ALL milestone rungs (e.g. 25+, 30+, 35+, 40+ PTS or 3+, 4+, 5+ 3PM)
    for a specific athlete. Returns matched ladders sorted by line ascending.
    """
    athlete = resolve_athlete_universal(player_query, sport=sport)
    sport_u = sport.upper()

    target_stat = None
    if stat_query:
        target_stat = UNIVERSAL_STAT_ALIASES.get(clean_token(stat_query).upper(), clean_token(stat_query).upper())

    if markets is None:
        markets = []
        KALSHI_BASES = [
            "https://external-api.kalshi.com/trade-api/v2",
            "https://api.elections.kalshi.com/trade-api/v2",
        ]
        if sport_u == "NBA":
            primary_series = []
            if target_stat in ("3PM", "3PT"):
                primary_series = ["KXNBA3PT", "KXNBATHREES"]
            elif target_stat == "PTS":
                primary_series = ["KXNBAPTS"]
            elif target_stat == "AST":
                primary_series = ["KXNBAAST"]
            elif target_stat == "REB":
                primary_series = ["KXNBAREB"]
            elif target_stat == "PRA":
                primary_series = ["KXNBAPRA"]

            all_nba_series = ["KXNBA3PT", "KXNBAPTS", "KXNBAAST", "KXNBAREB", "KXNBAPRA", "KXNBATHREES", "KXNBA2D", "KXNBA3D"]
            target_series_list = primary_series + [s for s in all_nba_series if s not in primary_series]
        elif sport_u == "NFL":
            target_series_list = ["KXNFLPASS", "KXNFLRUSH", "KXNFLREC", "KXNFLTD", "KXNFLGAME"]
        elif sport_u == "NHL":
            target_series_list = ["KXNHLSOG", "KXNHLPOINTS", "KXNHLGAME"]
        elif sport_u == "MLB":
            target_series_list = ["KXMLB"]
        elif sport_u == "TENNIS":
            target_series_list = ["KXTENNIS", "KXATP", "KXWTA"]
        else:
            target_series_list = ["KXNBAPTS", "KXNBA3PT", "KXNBAAST", "KXNBAREB", "KXNBAPRA"]

        seen_mkt_tickers = set()
        for base in KALSHI_BASES:
            for st in target_series_list:
                try:
                    r = requests.get(
                        f"{base}/markets",
                        params={"status": "open", "limit": 200, "series_ticker": st},
                        timeout=4,
                    )
                    if r.status_code == 200:
                        m_list = r.json().get("markets", [])
                        for sm in m_list:
                            sm_tk = sm.get("ticker")
                            if sm_tk and sm_tk not in seen_mkt_tickers:
                                seen_mkt_tickers.add(sm_tk)
                                markets.append(sm)
                except Exception:
                    pass
            if markets:
                break

        if not markets:
            try:
                from live_kalshi_scanner import fetch_paginated_kalshi_sports
                markets = fetch_paginated_kalshi_sports(sport=sport_u, max_pages=3)
            except Exception:
                markets = []

    active_date_codes = get_active_date_codes()
    matched_rungs = []
    seen_tickers = set()

    for m in markets:
        tk = str(m.get("ticker") or "").upper()
        if not tk or tk in seen_tickers:
            continue

        # Rule 1: Hard reject non-daily markets and career stats
        if any(bad in tk for bad in ("CAREER", "SEASON", "RECORD", "AWARD", "LEADER", "HOF", "CHAMP")):
            continue
        if not re.search(r"\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}", tk):
            continue

        info = extract_player_and_stat_from_kalshi_market(m)

        # Rule 2: Guard against <= 0 lines
        if float(info.get("line") or 0.0) <= 0.0:
            continue

        # Ensure date code matches our 3-day sliding window if date code present
        if info["date_code"] and info["date_code"] not in active_date_codes:
            continue

        # Stat filter
        if target_stat and target_stat != "ALL":
            if info["stat"] != target_stat:
                continue

        # Match player
        if match_player_to_market(athlete, info):
            seen_tickers.add(tk)
            matched_rungs.append({
                "athlete": athlete,
                "player_name": athlete["full_name"],
                "ticker": tk,
                "stat": info["stat"],
                "line": info["line"],
                "milestone_label": info["milestone_label"],
                "yes_ask": info["yes_ask"],
                "no_ask": info["no_ask"],
                "yes_bid": info["yes_bid"],
                "no_bid": info["no_bid"],
                "matchup": info["matchup"] or athlete["matchup"],
                "date_code": info["date_code"],
                "raw_market": m,
            })

    # Sort rungs by strike/line ascending
    matched_rungs.sort(key=lambda r: r["line"])
    return matched_rungs
