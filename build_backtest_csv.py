#!/usr/bin/env python3
"""
build_backtest_csv.py - Downloads historical NBA player game logs from ESPN across
all 30 NBA teams (rotation players) and builds a leakage-free props.csv ready for
sim_backtest.py, tune_sim.py, and ab_test_lineup.py.

Features:
- Widened rotation cohort covering all 30 NBA teams (~150 core rotation players by default,
  or `--all-rosters` to scan the full league index and keep players averaging >= 18 MPG).
- Parallel ESPN v3 gamelog fetching with ThreadPoolExecutor (`--workers 8`) and on-disk
  incremental caching (`gamelog_cache.json`) so interrupted runs resume instantly.
- Strictly filters ESPN `seasonTypes` to Regular Season (<= April 13, <= 82 games) and
  deduplicates by (player, date) and eventId.
- Infers each player's exact team on every game date via eventId opponent cross-matching
  (handling mid-season & offseason trades accurately).
- Derives walk-forward "teammate out" features (`teammate_out_count`, `missing_rot_min`,
  `missing_stat_share`) strictly from prior box scores (rotation players >= 20 MPG active
  in recent team games who are absent from today's box score).

Usage in Termux:
  python build_backtest_csv.py --out props.csv --seasons 2025,2026 --workers 8
"""
import argparse
import collections
import csv
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

TEAM_ABBR_MAP: Dict[str, str] = {
    "GS": "GSW",
    "NY": "NYK",
    "NO": "NOP",
    "SA": "SAS",
    "WSH": "WAS",
    "UTAH": "UTA",
}

CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gamelog_cache.json")


def normalize_team_abbr(abbr: Any) -> str:
    s = str(abbr or "").strip().upper()
    return TEAM_ABBR_MAP.get(s, s)


# Comprehensive 30-team NBA rotation cohort (6-7 core rotation players per team across all 30 teams = 200+ players)
DEFAULT_NBA_ATHLETE_IDS: List[Tuple[str, str, str, str]] = [
    # ATL
    ("4277905", "Trae Young", "ATL", "G"),
    ("4701230", "Jalen Johnson", "ATL", "F"),
    ("4869342", "Dyson Daniels", "ATL", "G"),
    ("4431680", "Onyeka Okongwu", "ATL", "C"),
    ("4065732", "De'Andre Hunter", "ATL", "F"),
    ("3102529", "Clint Capela", "ATL", "C"),
    ("5211175", "Zaccharie Risacher", "ATL", "F"),
    ("3037789", "Bogdan Bogdanovic", "ATL", "G"),
    # BOS
    ("4065648", "Jayson Tatum", "BOS", "F"),
    ("3917376", "Jaylen Brown", "BOS", "G-F"),
    ("3078576", "Derrick White", "BOS", "G"),
    ("3102531", "Kristaps Porzingis", "BOS", "C"),
    ("4066354", "Payton Pritchard", "BOS", "G"),
    ("3995", "Jrue Holiday", "BOS", "G"),
    ("3213", "Al Horford", "BOS", "C"),
    ("4065804", "Sam Hauser", "BOS", "F"),
    # BKN
    ("3138196", "Cameron Johnson", "BKN", "F"),
    ("4432174", "Cam Thomas", "BKN", "G"),
    ("4278067", "Nic Claxton", "BKN", "C"),
    ("3032979", "Dennis Schroder", "BKN", "G"),
    ("2578185", "Dorian Finney-Smith", "BKN", "F"),
    ("4712896", "Noah Clowney", "BKN", "F-C"),
    ("4433137", "Ziaire Williams", "BKN", "F"),
    ("4431714", "Jalen Wilson", "BKN", "F"),
    # CHA
    ("4432816", "LaMelo Ball", "CHA", "G"),
    ("4433287", "Brandon Miller", "CHA", "F"),
    ("4066383", "Miles Bridges", "CHA", "F"),
    ("4701232", "Mark Williams", "CHA", "C"),
    ("4432819", "Tre Mann", "CHA", "G"),
    ("4066218", "Grant Williams", "CHA", "F"),
    ("4432811", "Josh Green", "CHA", "G-F"),
    ("4278076", "Nick Richards", "CHA", "C"),
    # CHI
    ("3064440", "Zach LaVine", "CHI", "G"),
    ("6478", "Nikola Vucevic", "CHI", "C"),
    ("4395651", "Coby White", "CHI", "G"),
    ("4871145", "Josh Giddey", "CHI", "G"),
    ("4397002", "Ayo Dosunmu", "CHI", "G"),
    ("4431687", "Patrick Williams", "CHI", "F"),
    ("4711294", "Matas Buzelis", "CHI", "F"),
    # CLE
    ("3908809", "Donovan Mitchell", "CLE", "G"),
    ("4396907", "Darius Garland", "CLE", "G"),
    ("4432158", "Evan Mobley", "CLE", "F-C"),
    ("4066328", "Jarrett Allen", "CLE", "C"),
    ("2991043", "Caris LeVert", "CLE", "G"),
    ("4065778", "Max Strus", "CLE", "G-F"),
    ("4065733", "Ty Jerome", "CLE", "G"),
    ("3912848", "Dean Wade", "CLE", "F"),
    ("2990969", "Georges Niang", "CLE", "F"),
    # DAL
    ("3945274", "Luka Doncic", "DAL", "G"),
    ("6442", "Kyrie Irving", "DAL", "G"),
    ("4278078", "P.J. Washington", "DAL", "F"),
    ("4278049", "Daniel Gafford", "DAL", "C"),
    ("6475", "Klay Thompson", "DAL", "G-F"),
    ("4683688", "Dereck Lively II", "DAL", "C"),
    ("4278594", "Naji Marshall", "DAL", "F"),
    ("4397014", "Quentin Grimes", "DAL", "G"),
    # DEN
    ("3112335", "Nikola Jokic", "DEN", "C"),
    ("3936299", "Jamal Murray", "DEN", "G"),
    ("4278104", "Michael Porter Jr.", "DEN", "F"),
    ("3064290", "Aaron Gordon", "DEN", "F"),
    ("4431767", "Christian Braun", "DEN", "G"),
    ("3468", "Russell Westbrook", "DEN", "G"),
    ("4576087", "Peyton Watson", "DEN", "F"),
    ("4432181", "Julian Strawther", "DEN", "G-F"),
    # DET
    ("4432166", "Cade Cunningham", "DET", "G"),
    ("4433621", "Jalen Duren", "DET", "C"),
    ("6440", "Tobias Harris", "DET", "F"),
    ("4432810", "Isaiah Stewart", "DET", "F-C"),
    ("3914044", "Malik Beasley", "DET", "G"),
    ("2528210", "Tim Hardaway Jr.", "DET", "G-F"),
    ("4684742", "Ausar Thompson", "DET", "G-F"),
    # GSW
    ("3975", "Stephen Curry", "GSW", "G"),
    ("6589", "Draymond Green", "GSW", "F"),
    ("3059319", "Andrew Wiggins", "GSW", "F"),
    ("4433247", "Jonathan Kuminga", "GSW", "F"),
    ("4709138", "Brandin Podziemski", "GSW", "G"),
    ("2990984", "Buddy Hield", "GSW", "G"),
    ("4432171", "Moses Moody", "GSW", "G-F"),
    ("4431684", "Trayce Jackson-Davis", "GSW", "C"),
    # HOU
    ("4871144", "Alperen Sengun", "HOU", "C"),
    ("4437244", "Jalen Green", "HOU", "G"),
    ("2991230", "Fred VanVleet", "HOU", "G"),
    ("4432639", "Jabari Smith Jr.", "HOU", "F"),
    ("4684740", "Amen Thompson", "HOU", "G-F"),
    ("3155526", "Dillon Brooks", "HOU", "F"),
    ("4433192", "Tari Eason", "HOU", "F"),
    # IND
    ("4396993", "Tyrese Haliburton", "IND", "G"),
    ("3149673", "Pascal Siakam", "IND", "F"),
    ("3133628", "Myles Turner", "IND", "C"),
    ("4683634", "Bennedict Mathurin", "IND", "G-F"),
    ("4395712", "Andrew Nembhard", "IND", "G"),
    ("4278355", "Obi Toppin", "IND", "F"),
    ("4396909", "Aaron Nesmith", "IND", "G-F"),
    ("2530530", "T.J. McConnell", "IND", "G"),
    # LAC
    ("3992", "James Harden", "LAC", "G"),
    ("2595516", "Norman Powell", "LAC", "G"),
    ("4017837", "Ivica Zubac", "LAC", "C"),
    ("3936099", "Derrick Jones Jr.", "LAC", "F"),
    ("6450", "Kawhi Leonard", "LAC", "F"),
    ("2991139", "Kris Dunn", "LAC", "G"),
    ("3907823", "Terance Mann", "LAC", "G-F"),
    # LAL
    ("1966", "LeBron James", "LAL", "F"),
    ("6583", "Anthony Davis", "LAL", "C"),
    ("4066457", "Austin Reaves", "LAL", "G"),
    ("4066648", "Rui Hachimura", "LAL", "F"),
    ("3136776", "D'Angelo Russell", "LAL", "G"),
    ("4897943", "Dalton Knecht", "LAL", "G-F"),
    ("4432582", "Max Christie", "LAL", "G"),
    # MEM
    ("4279888", "Ja Morant", "MEM", "G"),
    ("4277961", "Jaren Jackson Jr.", "MEM", "F-C"),
    ("4066320", "Desmond Bane", "MEM", "G"),
    ("4593125", "Santi Aldama", "MEM", "F"),
    ("4600663", "Zach Edey", "MEM", "C"),
    ("4431785", "Scotty Pippen Jr.", "MEM", "G"),
    ("5112087", "Jaylen Wells", "MEM", "G-F"),
    ("2990992", "Marcus Smart", "MEM", "G"),
    # MIA
    ("4066261", "Bam Adebayo", "MIA", "C"),
    ("4395725", "Tyler Herro", "MIA", "G"),
    ("6430", "Jimmy Butler", "MIA", "F"),
    ("3136197", "Terry Rozier", "MIA", "G"),
    ("4432848", "Jaime Jaquez Jr.", "MIA", "G-F"),
    ("3157465", "Duncan Robinson", "MIA", "G-F"),
    ("4997528", "Nikola Jovic", "MIA", "F"),
    ("5105623", "Kel'el Ware", "MIA", "C"),
    # MIL
    ("3032977", "Giannis Antetokounmpo", "MIL", "F"),
    ("6606", "Damian Lillard", "MIL", "G"),
    ("3448", "Brook Lopez", "MIL", "C"),
    ("3064482", "Bobby Portis", "MIL", "F"),
    ("4277843", "Gary Trent Jr.", "MIL", "G"),
    ("2990962", "Taurean Prince", "MIL", "F"),
    ("4397475", "AJ Green", "MIL", "G"),
    # MIN
    ("4594268", "Anthony Edwards", "MIN", "G"),
    ("3064514", "Julius Randle", "MIN", "F"),
    ("3032976", "Rudy Gobert", "MIN", "C"),
    ("4396971", "Naz Reid", "MIN", "F-C"),
    ("4431671", "Jaden McDaniels", "MIN", "F"),
    ("3934673", "Donte DiVincenzo", "MIN", "G"),
    ("3195", "Mike Conley", "MIN", "G"),
    ("4278039", "Nickeil Alexander-Walker", "MIN", "G"),
    # NOP
    ("3913176", "Brandon Ingram", "NOP", "F"),
    ("2490149", "CJ McCollum", "NOP", "G"),
    ("4397688", "Trey Murphy III", "NOP", "F"),
    ("3907497", "Dejounte Murray", "NOP", "G"),
    ("5061589", "Yves Missi", "NOP", "C"),
    ("4395628", "Zion Williamson", "NOP", "F"),
    ("4277813", "Herbert Jones", "NOP", "F"),
    ("4277869", "Jose Alvarado", "NOP", "G"),
    # NYK
    ("3934672", "Jalen Brunson", "NYK", "G"),
    ("3136195", "Karl-Anthony Towns", "NYK", "C"),
    ("3934719", "OG Anunoby", "NYK", "F"),
    ("3147657", "Mikal Bridges", "NYK", "G-F"),
    ("3062679", "Josh Hart", "NYK", "G-F"),
    ("4431823", "Miles McBride", "NYK", "G"),
    ("4431679", "Precious Achiuwa", "NYK", "F-C"),
    # OKC
    ("4278073", "Shai Gilgeous-Alexander", "OKC", "G"),
    ("4593803", "Jalen Williams", "OKC", "G-F"),
    ("4433255", "Chet Holmgren", "OKC", "C"),
    ("4397020", "Luguentz Dort", "OKC", "G"),
    ("4222252", "Isaiah Hartenstein", "OKC", "C"),
    ("4683692", "Cason Wallace", "OKC", "G"),
    ("4397183", "Aaron Wiggins", "OKC", "G-F"),
    ("4395702", "Isaiah Joe", "OKC", "G"),
    ("2991350", "Alex Caruso", "OKC", "G"),
    # ORL
    ("4432573", "Paolo Banchero", "ORL", "F"),
    ("4566434", "Franz Wagner", "ORL", "F"),
    ("4432165", "Jalen Suggs", "ORL", "G"),
    ("4277847", "Wendell Carter Jr.", "ORL", "C"),
    ("4712849", "Anthony Black", "ORL", "G"),
    ("3150844", "Moritz Wagner", "ORL", "C"),
    ("2581018", "Kentavious Caldwell-Pope", "ORL", "G"),
    ("4348700", "Goga Bitadze", "ORL", "C"),
    # PHI
    ("4431678", "Tyrese Maxey", "PHI", "G"),
    ("4251", "Paul George", "PHI", "F"),
    ("3133603", "Kelly Oubre Jr.", "PHI", "G-F"),
    ("3059318", "Joel Embiid", "PHI", "C"),
    ("4683778", "Jared McCain", "PHI", "G"),
    ("3138160", "Caleb Martin", "PHI", "F"),
    ("6585", "Andre Drummond", "PHI", "C"),
    # PHX
    ("3202", "Kevin Durant", "PHX", "F"),
    ("3136193", "Devin Booker", "PHX", "G"),
    ("6580", "Bradley Beal", "PHX", "G"),
    ("3135046", "Tyus Jones", "PHX", "G"),
    ("3102530", "Jusuf Nurkic", "PHX", "C"),
    ("2583632", "Royce O'Neale", "PHX", "F"),
    ("3135045", "Grayson Allen", "PHX", "G"),
    ("4888725", "Ryan Dunn", "PHX", "F"),
    # POR
    ("4351851", "Anfernee Simons", "POR", "G"),
    ("2991070", "Jerami Grant", "POR", "F"),
    ("4278129", "Deandre Ayton", "POR", "C"),
    ("4683021", "Deni Avdija", "POR", "F"),
    ("4914336", "Shaedon Sharpe", "POR", "G"),
    ("4683678", "Scoot Henderson", "POR", "G"),
    ("4431736", "Toumani Camara", "POR", "F"),
    ("5105565", "Donovan Clingan", "POR", "C"),
    # SAC
    ("4066259", "De'Aaron Fox", "SAC", "G"),
    ("3155942", "Domantas Sabonis", "SAC", "C"),
    ("3978", "DeMar DeRozan", "SAC", "G-F"),
    ("4594327", "Keegan Murray", "SAC", "F"),
    ("4066262", "Malik Monk", "SAC", "G"),
    ("4066372", "Kevin Huerter", "SAC", "G-F"),
    ("4702177", "Keon Ellis", "SAC", "G"),
    # SAS
    ("5104157", "Victor Wembanyama", "SAS", "C"),
    ("4395630", "Devin Vassell", "SAS", "G"),
    ("4845367", "Stephon Castle", "SAS", "G"),
    ("6578", "Harrison Barnes", "SAS", "F"),
    ("4395723", "Keldon Johnson", "SAS", "F"),
    ("4592479", "Julian Champagnie", "SAS", "F"),
    ("3005", "Chris Paul", "SAS", "G"),
    ("4395626", "Tre Jones", "SAS", "G"),
    # TOR
    ("4433134", "Scottie Barnes", "TOR", "F"),
    ("4395625", "RJ Barrett", "TOR", "G-F"),
    ("3134908", "Jakob Poeltl", "TOR", "C"),
    ("5106258", "Gradey Dick", "TOR", "G"),
    ("4395724", "Immanuel Quickley", "TOR", "G"),
    ("4278053", "Davion Mitchell", "TOR", "G"),
    ("4397075", "Ochai Agbaji", "TOR", "G-F"),
    # UTA
    ("4066336", "Lauri Markkanen", "UTA", "F"),
    ("4433627", "Keyonte George", "UTA", "G"),
    ("3908845", "John Collins", "UTA", "F"),
    ("4277811", "Collin Sexton", "UTA", "G"),
    ("4433136", "Walker Kessler", "UTA", "C"),
    ("2528426", "Jordan Clarkson", "UTA", "G"),
    ("5105839", "Brice Sensabaugh", "UTA", "F"),
    # WAS
    ("4277956", "Jordan Poole", "WAS", "G"),
    ("3134907", "Kyle Kuzma", "WAS", "F"),
    ("5104155", "Bilal Coulibaly", "WAS", "G-F"),
    ("5160992", "Alex Sarr", "WAS", "C"),
    ("4845374", "Bub Carrington", "WAS", "G"),
    ("3064559", "Jonas Valanciunas", "WAS", "C"),
    ("5174563", "Kyshawn George", "WAS", "G-F"),
    ("4280151", "Corey Kispert", "WAS", "G-F"),
]

THRESHOLDS_BY_STAT: Dict[str, List[float]] = {
    "3PM": [0.5, 1.5, 2.5, 3.5, 4.5],
    "PTS": [14.5, 19.5, 24.5, 29.5, 34.5],
    "REB": [3.5, 5.5, 7.5, 9.5, 11.5],
    "AST": [2.5, 4.5, 6.5, 8.5, 10.5],
    "PRA": [24.5, 29.5, 34.5, 39.5, 44.5],
}


def _resolve_cohort(all_rosters: bool = False) -> List[Tuple[str, str, str, str]]:
    """
    Builds the rotation player cohort across all 30 NBA teams.
    Deduplicates by ESPN athlete_id. If `all_rosters=True`, merges all players from
    `player_index.get_index("NBA")` as well.
    """
    by_id: Dict[str, Dict[str, Any]] = {}
    try:
        import player_index
        for p in player_index.get_index("NBA"):
            aid = str(p.get("id") or "").strip()
            if aid:
                by_id[aid] = p
    except Exception:
        pass

    seen_ids: Set[str] = set()
    cohort: List[Tuple[str, str, str, str]] = []
    for aid, fallback_name, fallback_team, pos in DEFAULT_NBA_ATHLETE_IDS:
        if aid in seen_ids:
            continue
        seen_ids.add(aid)
        meta = by_id.get(aid, {})
        name = str(meta.get("name") or fallback_name)
        team = normalize_team_abbr(meta.get("team") or fallback_team)
        cohort.append((aid, name, team, pos))

    if all_rosters and by_id:
        for aid, meta in by_id.items():
            if aid in seen_ids:
                continue
            seen_ids.add(aid)
            cohort.append((aid, str(meta.get("name") or ""), normalize_team_abbr(meta.get("team") or ""), "G-F"))
    return cohort


def _parse_made_stat(raw_v: Any) -> Optional[float]:
    if raw_v is None:
        return None
    s = str(raw_v).strip()
    if not s or s in ("-", "--", "N/A"):
        return None
    if ":" in s:
        parts = s.split(":")
        try:
            return round(float(parts[0]) + float(parts[1]) / 60.0, 2)
        except (TypeError, ValueError):
            return None
    if "-" in s and not s.startswith("-"):
        s = s.split("-")[0]
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _is_regular_season_type(st: Dict[str, Any]) -> bool:
    """
    Strictly matches ESPN's Regular Season block and rejects Postseason, Play-In,
    Preseason, All-Star, or NBA Cup exhibition games.
    """
    name = str(st.get("displayName") or st.get("name") or "").strip()
    low = name.lower()
    if any(bad in low for bad in ("postseason", "playoff", "play-in", "preseason", "all-star")):
        return False
    st_type = str(st.get("type") or st.get("id") or "").strip()
    return ("regular season" in low) or (low == "regular") or (st_type == "2" and "regular" in low)


def fetch_player_season_gamelog(athlete_id: str, season: Optional[int] = None, verbose: bool = False) -> List[Dict[str, Any]]:
    """
    Fetches a player's regular-season game logs from ESPN v3 gamelog endpoint.
    Strictly filters to Regular Season (and date <= April 13, <= 82 games) and deduplicates by date and eventId.
    """
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{athlete_id}/gamelog"
    params = {"season": season} if season is not None else {}
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=12)
    except Exception as exc:
        if verbose:
            print(f"  [ERROR] Request exception for athlete_id={athlete_id} season={season}: {exc}", flush=True)
        return []

    if r.status_code != 200:
        if verbose:
            print(f"  [ERROR] HTTP {r.status_code} for athlete_id={athlete_id} season={season}", flush=True)
        return []

    data = r.json()
    top_labels = [str(n).upper() for n in (data.get("labels") or data.get("names") or [])]
    top_names = [str(n).upper() for n in (data.get("names") or [])]
    events_meta = data.get("events") if isinstance(data.get("events"), dict) else {}
    season_types = data.get("seasonTypes") or []

    games: List[Dict[str, Any]] = []
    seen_event_ids: Set[str] = set()
    seen_dates: Set[str] = set()

    for st in season_types:
        if not _is_regular_season_type(st):
            continue
        for cat in (st.get("categories") or []):
            cat_labels = [str(n).upper() for n in (cat.get("labels") or top_labels or cat.get("names") or [])]
            cat_names = [str(n).upper() for n in (cat.get("names") or top_names or [])]
            for ev_row in (cat.get("events") or []):
                ev_id = str(ev_row.get("eventId") or ev_row.get("id") or "").strip()
                if ev_id and ev_id in seen_event_ids:
                    continue
                vals = ev_row.get("stats") or []
                if not cat_labels or not vals:
                    continue
                stat_map: Dict[str, float] = {}
                for idx_col, raw_v in enumerate(vals):
                    fv = _parse_made_stat(raw_v)
                    if fv is None:
                        continue
                    if idx_col < len(cat_labels):
                        stat_map[cat_labels[idx_col]] = fv
                    if idx_col < len(cat_names):
                        stat_map[cat_names[idx_col]] = fv

                ev_info = events_meta.get(ev_id, {}) if isinstance(events_meta, dict) else {}
                raw_date = str(ev_info.get("gameDate") or ev_row.get("gameDate") or "")
                if len(raw_date) < 10:
                    continue
                game_date = raw_date[:10]
                m_int, d_int = int(game_date[5:7]), int(game_date[8:10])
                # Hard cutoff against NBA Play-In & Playoffs (mid-April onward)
                if m_int in (5, 6, 7, 8, 9) or (m_int == 4 and d_int > 13):
                    continue
                if game_date in seen_dates:
                    continue

                at_vs = str(ev_info.get("atVs") or "").strip().lower()
                is_home = True if at_vs == "vs" else (False if at_vs in ("@", "at") else None)
                opp_abbr = normalize_team_abbr((ev_info.get("opponent") or {}).get("abbreviation") or "OPP")
                ev_team_abbr = normalize_team_abbr((ev_info.get("team") or {}).get("abbreviation") or "")

                mins = stat_map.get("MIN", stat_map.get("MINUTES", 0.0))
                pts = stat_map.get("PTS", stat_map.get("POINTS"))
                reb = stat_map.get("REB", stat_map.get("REBOUNDS", 0.0))
                ast = stat_map.get("AST", stat_map.get("ASSISTS", 0.0))
                fg3m = stat_map.get(
                    "3PT",
                    stat_map.get(
                        "3PM",
                        stat_map.get(
                            "FG3M",
                            stat_map.get("THREEPOINTFIELDGOALSMADE-THREEPOINTFIELDGOALSATTEMPTED", 0.0),
                        ),
                    ),
                )

                if pts is None or mins <= 0:
                    continue
                pts_f = float(pts)

                if ev_id:
                    seen_event_ids.add(ev_id)
                seen_dates.add(game_date)

                games.append(
                    {
                        "event_id": ev_id,
                        "date": game_date,
                        "season": season or 0,
                        "ev_team": ev_team_abbr,
                        "opp": opp_abbr,
                        "is_home": is_home,
                        "minutes": round(float(mins), 1),
                        "PTS": pts_f,
                        "REB": float(reb),
                        "AST": float(ast),
                        "3PM": float(fg3m),
                        "PRA": pts_f + float(reb) + float(ast),
                    }
                )

    games.sort(key=lambda g: g["date"])
    return games[:82]


def _load_gamelog_cache() -> Dict[str, List[Dict[str, Any]]]:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_gamelog_cache(cache: Dict[str, List[Dict[str, Any]]]) -> None:
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, CACHE_FILE)
    except Exception:
        pass


DNP_CD_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dnp_cd_cache.json")
DNP_CACHE_FILE = Path(DNP_CD_CACHE_FILE)


def _load_dnp_cd_cache() -> Dict[str, List[str]]:
    if os.path.exists(DNP_CD_CACHE_FILE):
        try:
            with open(DNP_CD_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_dnp_cd_cache(cache: Dict[str, List[str]]) -> None:
    try:
        tmp = DNP_CD_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, DNP_CD_CACHE_FILE)
    except Exception:
        pass


def fetch_event_dnp_coach_decision(event_id: str) -> List[str]:
    """
    Queries ESPN's game summary for `event_id` and returns displayNames of any players
    who were dressed on the bench with `DNP - COACH'S DECISION` (so they are excluded
    from pre-game Inactive/OUT counts). Players who were Not Dressed / Inactive / Out
    (Injury/Illness/Rest) are NOT in this list and remain eligible as pre-game OUT.
    """
    if not event_id:
        return []
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={event_id}"
    headers = {"User-Agent": "Mozilla/5.0"}
    dnp_cd_names: List[str] = []
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            return []
        summary = r.json()
        for team_blk in (summary.get("boxscore") or {}).get("players") or []:
            for stat_grp in team_blk.get("statistics") or []:
                for a_entry in stat_grp.get("athletes") or []:
                    if not a_entry.get("didNotPlay"):
                        continue
                    reason = str(a_entry.get("reason") or "").upper()
                    if "COACH" in reason or "CD" in reason:
                        nm = str((a_entry.get("athlete") or {}).get("displayName") or "").strip()
                        if nm:
                            dnp_cd_names.append(nm)
    except Exception:
        pass
    return dnp_cd_names


def build_dataset(
    out_csv: str,
    seasons: Tuple[int, ...] = (2025, 2026),
    workers: int = 8,
    min_mpg: Optional[float] = None,
    min_games: Optional[int] = None,
    all_rosters: bool = False,
) -> int:
    from sport_plugin import ROTATION_MIN_GP, ROTATION_MIN_MPG
    if min_mpg is None:
        min_mpg = ROTATION_MIN_MPG
    if min_games is None:
        min_games = ROTATION_MIN_GP
    cohort = _resolve_cohort(all_rosters=all_rosters)
    cache = _load_gamelog_cache()
    print(f"Building rotation dataset for {len(cohort)} NBA players across seasons {seasons} (workers={workers})...", flush=True)

    # Fetch missing (athlete_id, season) pairs in parallel
    tasks: List[Tuple[str, str, int]] = []
    for ath_id, name, _team, _pos in cohort:
        for yr in seasons:
            ck = f"{ath_id}:{yr}"
            if ck not in cache:
                tasks.append((ath_id, name, yr))

    if tasks:
        print(f"Downloading {len(tasks)} uncached player-season gamelogs from ESPN...", flush=True)
        done_cnt = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            fut_map = {
                ex.submit(fetch_player_season_gamelog, aid, yr): (aid, nm, yr)
                for aid, nm, yr in tasks
            }
            for fut in as_completed(fut_map):
                aid, nm, yr = fut_map[fut]
                done_cnt += 1
                try:
                    logs = fut.result()
                except Exception:
                    logs = []
                cache[f"{aid}:{yr}"] = logs
                if done_cnt % 20 == 0 or done_cnt == len(tasks):
                    print(f"  [{done_cnt}/{len(tasks)}] fetched ({nm} {yr}: {len(logs)} games)", flush=True)
                    _save_gamelog_cache(cache)

    all_game_rows: List[Dict[str, Any]] = []
    seen_player_dates: Set[Tuple[str, str]] = set()
    player_prior_min: Dict[Tuple[str, int], float] = {}
    player_prior_gp: Dict[Tuple[str, int], int] = {}
    player_prior_stats: Dict[Tuple[str, int], Dict[str, float]] = {}

    for ath_id, name, fallback_team, pos in cohort:
        prior_season_avgs: Dict[str, float] = {}
        prev_min: Optional[float] = None
        prev_gp: int = 0
        for yr in seasons:
            logs = cache.get(f"{ath_id}:{yr}", [])
            deduped_logs: List[Dict[str, Any]] = []
            for g in logs:
                pd_key = (name, g["date"])
                if pd_key in seen_player_dates:
                    continue
                seen_player_dates.add(pd_key)
                deduped_logs.append(dict(g))

            if not deduped_logs:
                continue

            # Determine season start year (e.g. 2024 for 2024-25, 2025 for 2025-26)
            d0 = deduped_logs[0]["date"]
            y0, m0 = int(d0[:4]), int(d0[5:7])
            s_yr0 = y0 if m0 >= 8 else y0 - 1
            if prev_min is not None:
                player_prior_min[(name, s_yr0)] = prev_min
                player_prior_gp[(name, s_yr0)] = prev_gp
            if prior_season_avgs:
                player_prior_stats[(name, s_yr0)] = dict(prior_season_avgs)

            # Do NOT filter by full-season MPG/GP here (which would leak future games).
            # Rotation membership is evaluated strictly walk-forward (dt < g_date) below.
            for g in deduped_logs:
                g["athlete_id"] = ath_id
                g["player"] = name
                g["team"] = g.get("ev_team") or fallback_team
                g["position"] = pos
                g["priors"] = dict(prior_season_avgs)
                g["prior_min"] = prev_min
                g["prior_gp"] = prev_gp
                all_game_rows.append(g)

            prev_gp = len(deduped_logs)
            prev_min = sum(float(x["minutes"]) for x in deduped_logs) / prev_gp
            for stat_k in ("PTS", "REB", "AST", "3PM", "PRA"):
                vals = [float(x[stat_k]) for x in deduped_logs]
                if vals:
                    prior_season_avgs[stat_k] = round(sum(vals) / len(vals), 3)

    # Infer exact team on every game date using event_id opponent cross-matching
    # If Player A and Player B share event_id and have different opponents, Player A's team is Player B's opponent!
    ev_opponents: Dict[str, Set[str]] = defaultdict(set)
    date_opp_map: Dict[Tuple[str, str, Optional[bool]], str] = {}
    for g in all_game_rows:
        ev_id = g.get("event_id") or ""
        if ev_id and g.get("opp"):
            ev_opponents[ev_id].add(g["opp"])

    for g in all_game_rows:
        ev_id = g.get("event_id") or ""
        opps = ev_opponents.get(ev_id, set())
        if len(opps) == 2 and g["opp"] in opps:
            other_team = next(t for t in opps if t != g["opp"])
            g["team"] = other_team
            date_opp_map[(g["date"], g["opp"], g.get("is_home"))] = other_team

    for g in all_game_rows:
        inferred = date_opp_map.get((g["date"], g["opp"], g.get("is_home")))
        if inferred:
            g["team"] = inferred

    # Derive walk-forward "teammate out" features per (team, season_year) strictly from PRIOR box scores
    # Group team games chronologically
    team_season_dates: Dict[Tuple[str, int], Dict[str, Dict[str, Dict[str, Any]]]] = defaultdict(lambda: defaultdict(dict))
    for g in all_game_rows:
        d = g["date"]
        y, m = int(d[:4]), int(d[5:7])
        s_yr = y if m >= 8 else y - 1
        g["season_yr"] = s_yr
        team_season_dates[(g["team"], s_yr)][d][g["player"]] = g

    teammate_out_lookup: Dict[Tuple[str, str], Dict[str, Any]] = {}
    from sport_plugin import (
        ACTIVE,
        OUT,
        NBAPlugin,
        PlayerCtx,
        ROTATION_MIN_GP,
        ROTATION_MIN_MPG,
        infer_nba_archetype,
        redistribute_share,
    )
    from sim_core import prior_for
    nba_plug = NBAPlugin(use_affinity=True)
    dnp_cd_cache = _load_dnp_cd_cache()

    # Pre-scan for event_ids where a walk-forward rotation candidate (>= ROTATION_MIN_GP & >= ROTATION_MIN_MPG prior to g_date) is absent
    candidate_ev_dates: Dict[str, str] = {}
    for (tm, s_yr), date_dict in team_season_dates.items():
        sorted_dates = sorted(date_dict.keys())
        tm_p_mins: Dict[str, List[float]] = defaultdict(list)
        for d_idx, g_date in enumerate(sorted_dates):
            active_today = date_dict[g_date]
            ev_id = next((g.get("event_id") or "" for g in active_today.values() if g.get("event_id")), "")
            if ev_id:
                for tm_p, mins_hist in tm_p_mins.items():
                    if tm_p not in active_today and len(mins_hist) >= ROTATION_MIN_GP:
                        if (sum(mins_hist) / len(mins_hist)) >= ROTATION_MIN_MPG:
                            candidate_ev_dates[ev_id] = g_date
                            break
            for p_name, g_obj in active_today.items():
                tm_p_mins[p_name].append(float(g_obj["minutes"]))

    missing_evs = [ev for ev in candidate_ev_dates if ev not in dnp_cd_cache]
    if missing_evs:
        print(f"Checking DNP-Coach's-Decision status on {len(missing_evs)} games with absent rotation players...", flush=True)
        done_ev = 0
        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            fut_ev = {ex.submit(fetch_event_dnp_coach_decision, ev): ev for ev in missing_evs}
            for fut in as_completed(fut_ev):
                ev = fut_ev[fut]
                done_ev += 1
                try:
                    names = fut.result()
                except Exception:
                    names = []
                dnp_cd_cache[ev] = names
                g_dt = candidate_ev_dates.get(ev, "")
                if g_dt and names:
                    dt_key = f"date:{g_dt}"
                    cur_dt_list = set(dnp_cd_cache.get(dt_key, []))
                    cur_dt_list.update(names)
                    dnp_cd_cache[dt_key] = sorted(cur_dt_list)
                if done_ev % 50 == 0 or done_ev == len(missing_evs):
                    _save_dnp_cd_cache(dnp_cd_cache)

    for (tm, s_yr), date_dict in team_season_dates.items():
        sorted_dates = sorted(date_dict.keys())
        # Track each player's prior games on this team in this season: list of (date, mins, pos, stats_dict)
        tm_player_hist: Dict[str, List[Tuple[str, float, str, Dict[str, float]]]] = defaultdict(list)

        for d_idx, g_date in enumerate(sorted_dates):
            active_today = date_dict[g_date]
            sample_ev_id = next((g.get("event_id") or "" for g in active_today.values() if g.get("event_id")), "")
            dnp_cd_today = set(dnp_cd_cache.get(sample_ev_id, [])) | set(dnp_cd_cache.get(f"date:{g_date}", []))
            recent_team_dates = set(sorted_dates[max(0, d_idx - 6): d_idx])
            last5_team_dates = set(sorted_dates[max(0, d_idx - 5): d_idx])

            # Build PlayerCtx list for all rotation players on this team strictly from history prior to g_date
            team_ctxs: Dict[str, PlayerCtx] = {}
            raw_stat_means: Dict[str, Dict[str, float]] = defaultdict(dict)
            absent_names: List[str] = []
            miss_min_total = 0.0

            all_candidate_players = set(tm_player_hist.keys()) | set(active_today.keys())
            for tm_player in all_candidate_players:
                p_hist = tm_player_hist.get(tm_player, [])
                is_active_today = tm_player in active_today
                pos_str = active_today[tm_player]["position"] if is_active_today else (p_hist[-1][2] if p_hist else "G-F")

                # Walk-forward minutes role strictly from games BEFORE g_date (never from today's box score!)
                pr_m = player_prior_min.get((tm_player, s_yr))
                pr_g = player_prior_gp.get((tm_player, s_yr), 0)
                if len(p_hist) >= ROTATION_MIN_GP:
                    avg_m = sum(x[1] for x in p_hist) / len(p_hist)
                    is_wf_rot_teammate = avg_m >= ROTATION_MIN_MPG
                elif pr_m is not None and pr_g >= ROTATION_MIN_GP:
                    avg_m = (sum(x[1] for x in p_hist) + ROTATION_MIN_GP * pr_m) / (len(p_hist) + ROTATION_MIN_GP)
                    is_wf_rot_teammate = (len(p_hist) >= ROTATION_MIN_GP) and (avg_m >= ROTATION_MIN_MPG)
                else:
                    avg_m = (sum(x[1] for x in p_hist) + ROTATION_MIN_GP * 24.0) / (len(p_hist) + ROTATION_MIN_GP)
                    is_wf_rot_teammate = False

                if not is_active_today:
                    # Single canonical walk-forward rotation cutoff prior to g_date:
                    # >= ROTATION_MIN_GP (5) prior games in season and >= ROTATION_MIN_MPG (20.0) walk-forward MPG
                    if not is_wf_rot_teammate:
                        continue
                    # Exclude DNP - Coach's Decision rows (dressed & available at tip-off, not on inactive list)
                    if tm_player in dnp_cd_today:
                        continue
                    recent_app = sum(1 for dt, _, _, _ in p_hist if dt in recent_team_dates)
                    if recent_app < 2:
                        continue
                    l5_played = sum(1 for dt, _, _, _ in p_hist if dt in last5_team_dates)
                    fresh_w = max(0.0, (l5_played - 1.0) / max(1.0, float(len(last5_team_dates))))
                    if fresh_w <= 0.0:
                        continue
                    absent_names.append(tm_player)
                    miss_min_total += avg_m * fresh_w
                    status_str = OUT
                else:
                    # Any player with minutes > 0 (including mid-game injuries) was ACTIVE at tip-off
                    fresh_w = 1.0
                    status_str = ACTIVE

                pr_stats = player_prior_stats.get((tm_player, s_yr), {})
                for sk in ("PTS", "REB", "AST", "3PM", "PRA"):
                    if p_hist:
                        m_val = sum(x[3][sk] for x in p_hist) / len(p_hist)
                    elif sk in pr_stats:
                        m_val = float(pr_stats[sk])
                    else:
                        m_val = prior_for(sk, pos_str)
                    raw_stat_means[sk][tm_player] = max(0.05, m_val * (fresh_w if status_str == OUT else 1.0))

                team_ctxs[tm_player] = PlayerCtx(
                    pid=tm_player,
                    name=tm_player,
                    team=tm,
                    position=pos_str,
                    archetype=infer_nba_archetype(pos_str),
                    status=status_str,
                    confirmed=True,
                    role=avg_m,
                    share={},
                )

            # Normalize stat shares across the team for each stat and compute redistribution multipliers
            mult_uniform_by_stat: Dict[str, Dict[str, float]] = {}
            mult_affinity_by_stat: Dict[str, Dict[str, float]] = {}
            share_by_stat: Dict[str, Dict[str, float]] = {}

            ctx_list = list(team_ctxs.values())
            for sk in ("PTS", "REB", "AST", "3PM", "PRA"):
                tot_sk = sum(raw_stat_means[sk].values()) or 1.0
                for p_ctx in ctx_list:
                    p_ctx.share[sk] = raw_stat_means[sk].get(p_ctx.pid, 0.0) / tot_sk
                share_by_stat[sk] = {p_ctx.pid: round(p_ctx.share[sk], 4) for p_ctx in ctx_list}
                mult_uniform_by_stat[sk] = redistribute_share(ctx_list, sk, affinity=lambda a, b: 1.0, cap=0.12)
                mult_affinity_by_stat[sk] = redistribute_share(ctx_list, sk, affinity=nba_plug.affinity, cap=0.12)

            for p_name in active_today:
                teammate_out_lookup[(p_name, g_date)] = {
                    "teammate_out_count": len(absent_names),
                    "absent_rot_players": "|".join(absent_names),
                    "missing_rot_min": round(miss_min_total, 2),
                    "share": {sk: share_by_stat[sk].get(p_name, 0.15) for sk in ("PTS", "REB", "AST", "3PM", "PRA")},
                    "redist_uniform": {sk: round(mult_uniform_by_stat[sk].get(p_name, 1.0), 4) for sk in ("PTS", "REB", "AST", "3PM", "PRA")},
                    "redist_affinity": {sk: round(mult_affinity_by_stat[sk].get(p_name, 1.0), 4) for sk in ("PTS", "REB", "AST", "3PM", "PRA")},
                }

            # After recording today's pre-game features, append today's box score to history
            for p_name, g_obj in active_today.items():
                tm_player_hist[p_name].append((
                    g_date,
                    float(g_obj["minutes"]),
                    str(g_obj.get("position") or "G-F"),
                    {sk: float(g_obj[sk]) for sk in ("PTS", "REB", "AST", "3PM", "PRA")},
                ))

    # Sort chronologically across all players so opponent multipliers, walk-forward rotation eligibility,
    # and player thresholds use strictly prior dates (dt < g_date)
    all_game_rows.sort(key=lambda x: (x["date"], x["player"]))

    opp_hist: Dict[Tuple[str, str], List[Tuple[str, float]]] = defaultdict(list)
    league_hist: Dict[str, List[Tuple[str, float]]] = defaultdict(list)
    player_hist: Dict[Tuple[str, str, int], List[Tuple[str, float]]] = defaultdict(list)
    player_min_hist: Dict[Tuple[str, int], List[Tuple[str, float]]] = defaultdict(list)
    kept_players_set: Set[str] = set()
    kept_player_games = 0

    prop_rows: List[Dict[str, Any]] = []
    for g in all_game_rows:
        g_date = g["date"]
        s_yr = g["season_yr"]
        opp = g["opp"]
        p_name = g["player"]
        pos = g["position"]

        # Walk-forward rotation eligibility for cohort selection strictly prior to g_date:
        # Uses games played BEFORE g_date in current season, shrunk with completed prior-season MPG
        # when current-season games < min_games. Zero future games are ever inspected.
        prior_mins = [m for dt, m in player_min_hist[(p_name, s_yr)] if dt < g_date]
        pr_min = g.get("prior_min")
        pr_gp = int(g.get("prior_gp") or 0)
        if len(prior_mins) >= min_games:
            wf_player_mpg = sum(prior_mins) / len(prior_mins)
            is_wf_cohort_rot = wf_player_mpg >= min_mpg
        elif pr_min is not None and pr_gp >= min_games:
            wf_player_mpg = (sum(prior_mins) + min_games * float(pr_min)) / (len(prior_mins) + min_games)
            is_wf_cohort_rot = wf_player_mpg >= min_mpg
        else:
            # First < min_games games of the earliest season: use shrunk baseline (24.0 MPG) so early-season
            # history is present in props.csv without peeking at future games.
            wf_player_mpg = (sum(prior_mins) + min_games * 24.0) / (len(prior_mins) + min_games)
            is_wf_cohort_rot = wf_player_mpg >= min_mpg

        player_min_hist[(p_name, s_yr)].append((g_date, float(g["minutes"])))

        if not is_wf_cohort_rot:
            # Still record actuals into walk-forward history so later dates have complete prior game logs
            for stat_k in THRESHOLDS_BY_STAT:
                actual_val = float(g[stat_k])
                opp_hist[(opp, stat_k)].append((g_date, actual_val))
                league_hist[stat_k].append((g_date, actual_val))
                player_hist[(p_name, stat_k, s_yr)].append((g_date, actual_val))
            continue

        kept_players_set.add(p_name)
        kept_player_games += 1
        tm_info = teammate_out_lookup.get(
            (p_name, g_date),
            {
                "teammate_out_count": 0,
                "absent_rot_players": "",
                "missing_rot_min": 0.0,
                "share": {},
                "redist_uniform": {},
                "redist_affinity": {},
            },
        )

        for stat_k, thresholds in THRESHOLDS_BY_STAT.items():
            opp_prior = [v for dt, v in opp_hist[(opp, stat_k)] if dt < g_date]
            lg_prior = [v for dt, v in league_hist[stat_k] if dt < g_date]
            if len(opp_prior) >= 5 and len(lg_prior) >= 20:
                lg_mean = max(0.2, sum(lg_prior) / len(lg_prior))
                opp_mean = sum(opp_prior) / len(opp_prior)
                raw_ratio = opp_mean / lg_mean
                k_opp = 15.0
                opp_factor = round(((len(opp_prior) * raw_ratio) + (k_opp * 1.0)) / (len(opp_prior) + k_opp), 4)
                opp_factor = max(0.88, min(1.12, opp_factor))
            else:
                opp_factor = 1.0

            prior_val = g["priors"].get(stat_k)
            actual_val = float(g[stat_k])

            # Choose thresholds strictly from PRIOR games in current season or prior-season avg — NEVER from actual_val!
            p_prior_vals = [v for dt, v in player_hist[(p_name, stat_k, s_yr)] if dt < g_date]
            if len(p_prior_vals) >= 3:
                curr_m = sum(p_prior_vals) / len(p_prior_vals)
                ref_center = 0.7 * curr_m + 0.3 * prior_val if prior_val is not None else curr_m
            elif prior_val is not None:
                ref_center = prior_val
            else:
                ref_center = prior_for(stat_k, pos)

            near_thresholds = sorted(thresholds, key=lambda t: abs(t - ref_center))[:2]

            for th in sorted(near_thresholds):
                prop_rows.append(
                    {
                        "date": g_date,
                        "player": p_name,
                        "team": g["team"],
                        "opponent": opp,
                        "stat": stat_k,
                        "threshold": th,
                        "actual": actual_val,
                        "is_home": "" if g["is_home"] is None else g["is_home"],
                        "position": pos,
                        "minutes": g["minutes"],
                        "opp_factor": opp_factor,
                        "prior_season_avg": "" if prior_val is None else prior_val,
                        "teammate_out_count": int(tm_info["teammate_out_count"]),
                        "absent_rot_players": tm_info.get("absent_rot_players", ""),
                        "missing_rot_min": tm_info["missing_rot_min"],
                        "usage_share": tm_info.get("share", {}).get(stat_k, 0.15),
                        "redist_mult_uniform": tm_info.get("redist_uniform", {}).get(stat_k, 1.0),
                        "redist_mult_affinity": tm_info.get("redist_affinity", {}).get(stat_k, 1.0),
                    }
                )

            opp_hist[(opp, stat_k)].append((g_date, actual_val))
            league_hist[stat_k].append((g_date, actual_val))
            player_hist[(p_name, stat_k, s_yr)].append((g_date, actual_val))

    fieldnames = [
        "date", "player", "team", "opponent", "stat", "threshold",
        "actual", "is_home", "position", "minutes", "opp_factor",
        "prior_season_avg", "teammate_out_count", "absent_rot_players",
        "missing_rot_min", "usage_share", "redist_mult_uniform", "redist_mult_affinity",
    ]
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(prop_rows)

    print(
        f"Wrote {len(prop_rows)} prop rows across {kept_player_games} walk-forward rotation player-games "
        f"({len(kept_players_set)} rotation players across all 30 NBA teams) to {out_csv}",
        flush=True,
    )
    return len(prop_rows)


def main() -> None:
    from sport_plugin import ROTATION_MIN_MPG
    parser = argparse.ArgumentParser(description="Download ESPN NBA rotation player game logs across all 30 teams into props.csv")
    parser.add_argument("--out", default="props.csv", help="Output CSV path (default: props.csv)")
    parser.add_argument("--seasons", default="2025,2026", help="Comma-separated ESPN season years (default: 2025,2026)")
    parser.add_argument("--workers", type=int, default=8, help="Parallel download workers (default: 8)")
    parser.add_argument("--min-mpg", type=float, default=ROTATION_MIN_MPG, help=f"Minimum season MPG to keep as rotation player (default: {ROTATION_MIN_MPG})")
    parser.add_argument("--all-rosters", action="store_true", help="Scan all 500+ players in player_index_nba.json in addition to the 150-player 30-team rotation list")
    args = parser.parse_args()
    seasons = tuple(int(s.strip()) for s in args.seasons.split(",") if s.strip())
    build_dataset(args.out, seasons=seasons, workers=args.workers, min_mpg=args.min_mpg, all_rosters=args.all_rosters)


if __name__ == "__main__":
    main()
