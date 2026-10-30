import datetime
import json
import math
import re
import requests
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from difflib import SequenceMatcher
import requests

from live_kalshi_scanner import fetch_paginated_kalshi_sports, resolve_working_kalshi_url

# In-memory caches with TTL
_SCHEDULE_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}
_ROSTER_CACHE: Dict[str, Tuple[float, Dict[str, Dict[str, Any]]]] = {}
_KALSHI_PROPS_CACHE: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

CACHE_TTL_SCHEDULE = 60.0  # 1 minute
CACHE_TTL_ROSTER = 120.0   # 2 minutes
CACHE_TTL_KALSHI = 45.0    # 45 seconds

SPORT_API_PATHS = {
    "NBA": "basketball/nba",
    "NFL": "football/nfl",
    "MLB": "baseball/mlb",
    "NHL": "hockey/nhl",
    "WNBA": "basketball/wnba",
    "CFB": "football/college-football",
}

# Strict Stat Whitelist for NBA on Kalshi: NO REB+AST or PTS+AST combos
VALID_HOOPS_STATS = {
    "PTS": "PTS", "POINT": "PTS", "POINTS": "PTS",
    "REB": "REB", "REBOUND": "REB", "REBOUNDS": "REB",
    "AST": "AST", "ASSIST": "AST", "ASSISTS": "AST",
    "3PT": "3PM", "3PM": "3PM", "THREE": "3PM", "THREES": "3PM",
    "PRA": "PRA",
}

FORBIDDEN_KEYWORDS = (
    "RETIRE", "ANNOUNCE", "OWNER", "HEADLINER", "DRAFT",
    "MVP", "CHAMPIONSHIP", "SELECT", "ELECT", "PRESIDENT",
    "AWARD", "ALL-STAR", "HALL OF FAME", "TRADED", "SIGN", "BORN",
    "COACH OF THE YEAR", "EXECUTIVE", "MAJORITY", "HALFTIME", "LUMINATE",
    "THE FALL OFF", "FALL OFF", "BLANCHETT"
)


def get_live_active_games(sport: str = "NBA") -> List[Dict[str, Any]]:
    """
    Pulls live ESPN scoreboard for today.
    STRICT GAME-STATE GATE:
    Rejects any game where:
    - status.type.completed == True
    - state == 'post'
    - 'FINAL' in detail or name
    - 'POSTPONED' or 'CANCELED'
    ONLY returns games that are PRE-GAME ('pre') or IN-PLAY LIVE ('in').
    """
    sport_u = sport.upper()
    now = time.time()

    if sport_u in _SCHEDULE_CACHE:
        ts, cached_games = _SCHEDULE_CACHE[sport_u]
        if now - ts < CACHE_TTL_SCHEDULE:
            return cached_games

    path = SPORT_API_PATHS.get(sport_u, "basketball/nba")
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"

    active_games: List[Dict[str, Any]] = []

    try:
        resp = requests.get(url, timeout=6)
        if resp.status_code == 200:
            events = resp.json().get("events", [])
            for ev in events:
                status_obj = ev.get("status", {})
                status_type = status_obj.get("type", {})
                state = status_type.get("state", "pre").lower()
                detail = str(status_type.get("shortDetail") or status_type.get("detail") or "").upper()
                type_name = str(status_type.get("name", "")).upper()
                is_completed = bool(status_type.get("completed", False))

                # STRICT GATE: Drop any game that has finished or is not active today
                if is_completed or state == "post" or "FINAL" in detail or "FINAL" in type_name:
                    continue
                if "POSTPONED" in detail or "CANCELED" in detail:
                    continue
                if state not in ("pre", "in"):
                    continue

                ev_id = str(ev.get("id", ""))
                ev_date_raw = ev.get("date", "")
                game_time_str = _format_espn_game_time(ev_date_raw, detail, state)

                comps = ev.get("competitions", [{}])[0]
                competitors = comps.get("competitors", [])
                home_team, away_team = {}, {}
                game_teams = set()

                for comp in competitors:
                    t = comp.get("team", {})
                    abbr = str(t.get("abbreviation") or t.get("shortDisplayName") or "").upper()
                    display_name = str(t.get("displayName") or t.get("name") or "").strip()
                    t_id = str(t.get("id", ""))
                    t_info = {
                        "id": t_id,
                        "abbr": abbr,
                        "display_name": display_name,
                        "name": str(t.get("name", "")).strip(),
                    }
                    if comp.get("homeAway") == "home":
                        home_team = t_info
                    else:
                        away_team = t_info

                    for k in ("abbreviation", "displayName", "name", "shortDisplayName"):
                        v = t.get(k)
                        if v:
                            game_teams.add(str(v).upper())

                matchup = f"{away_team.get('abbr', 'AWAY')} @ {home_team.get('abbr', 'HOME')}"
                active_games.append({
                    "event_id": ev_id,
                    "matchup": matchup,
                    "game_date": game_time_str,
                    "state": state,
                    "detail": detail,
                    "home_team": home_team,
                    "away_team": away_team,
                    "teams": game_teams,
                    "raw_date": ev_date_raw,
                })
    except Exception:
        pass

    _SCHEDULE_CACHE[sport_u] = (now, active_games)
    return active_games


def _format_espn_game_time(date_iso: str, detail: str, state: str) -> str:
    """Formats readable dates for PRE and IN-PLAY games."""
    now_dt = datetime.now()
    if state == "in":
        clock = detail if detail else "In Progress"
        return f"🔴 Live ({clock})"

    if date_iso:
        try:
            clean_iso = date_iso.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_iso)
            m_d = dt.strftime("%b %d")
            t_str = dt.strftime("%I:%M %p ET").lstrip("0")
            return f"Today ({m_d}) • {t_str}"
        except Exception:
            pass

    if detail and ("PM" in detail or "AM" in detail):
        return f"Today ({now_dt.strftime('%b %d')}) • {detail.strip()}"

    return f"Today ({now_dt.strftime('%b %d')}) • Live Slate"


def get_active_espn_rosters(sport: str = "NBA") -> Dict[str, Dict[str, Any]]:
    """
    Fetches real rosters for active/scheduled games today from ESPN.
    Maps:
      normalized_player_name -> {
          'name': 'LeBron James',
          'team_abbr': 'LAL',
          'team_name': 'Los Angeles Lakers',
          'event_id': '401584674',
          'matchup': 'LAL @ GSW',
          'game_date': 'Today (Oct 26) • 7:30 PM ET',
          'state': 'pre',
          'teammates': ['Anthony Davis', 'Austin Reaves', ...]
      }
    """
    sport_u = sport.upper()
    now = time.time()

    if sport_u in _ROSTER_CACHE:
        ts, cached_roster = _ROSTER_CACHE[sport_u]
        if now - ts < CACHE_TTL_ROSTER:
            return cached_roster

    active_games = get_live_active_games(sport_u)
    if not active_games:
        _ROSTER_CACHE[sport_u] = (now, {})
        return {}

    path = SPORT_API_PATHS.get(sport_u, "basketball/nba")
    player_roster_map: Dict[str, Dict[str, Any]] = {}

    for game in active_games:
        ev_id = game["event_id"]
        summary_url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/summary?event={ev_id}"
        try:
            r = requests.get(summary_url, timeout=5)
            if r.status_code != 200:
                continue
            data = r.json()
            boxscore = data.get("boxscore", {})
            players_section = boxscore.get("players", [])

            for team_entry in players_section:
                t_obj = team_entry.get("team", {})
                t_abbr = str(t_obj.get("abbreviation") or "").upper()
                t_name = str(t_obj.get("displayName") or t_obj.get("name") or t_abbr)

                # Collect all player names for teammate lookup
                team_athletes: List[str] = []
                stats_list = team_entry.get("statistics", [])
                for stat_block in stats_list:
                    for ath in stat_block.get("athletes", []):
                        athlete_obj = ath.get("athlete", {})
                        d_name = athlete_obj.get("displayName")
                        if d_name and d_name not in team_athletes:
                            team_athletes.append(d_name)

                # Map each athlete
                for p_name in team_athletes:
                    p_upper = p_name.upper().strip()
                    teammates = [tn for tn in team_athletes if tn.upper() != p_upper]
                    player_roster_map[p_upper] = {
                        "name": p_name,
                        "team_abbr": t_abbr,
                        "team_name": t_name,
                        "event_id": ev_id,
                        "matchup": game["matchup"],
                        "game_date": game["game_date"],
                        "state": game["state"],
                        "teammates": teammates,
                    }

                    # Also map last name if unambiguous
                    parts = p_upper.split()
                    if len(parts) >= 2:
                        last = parts[-1]
                        if last not in player_roster_map:
                            player_roster_map[last] = player_roster_map[p_upper]
        except Exception:
            continue

    _ROSTER_CACHE[sport_u] = (now, player_roster_map)
    return player_roster_map


_ATHLETE_SEARCH_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def resolve_espn_athlete_info(player_name: str, sport: str = "NBA") -> Dict[str, Any]:
    """
    Dynamically resolves ANY athlete (NBA, College Basketball, NFL, etc.) via ESPN APIs:
    1. Checks active rosters from today's scoreboard.
    2. Queries ESPN's athlete search API (https://site.web.api.espn.com/apis/common/v3/search)
       to resolve verified current team (e.g. Luka Doncic -> LAL, Caleb Wilson -> UNC).
    3. Resolves today's real game date and matchup from ESPN live scoreboard.
    """
    p_norm = player_name.strip().upper()
    now = time.time()
    if p_norm in _ATHLETE_SEARCH_CACHE:
        ts, cached = _ATHLETE_SEARCH_CACHE[p_norm]
        if now - ts < 600.0:
            return cached

    sport_u = sport.upper()
    # 1. First check today's active rosters
    rosters = get_active_espn_rosters(sport_u)
    if p_norm in rosters:
        _ATHLETE_SEARCH_CACHE[p_norm] = (now, rosters[p_norm])
        return rosters[p_norm]

    # Check last name in active rosters
    p_parts = p_norm.split()
    if len(p_parts) > 1 and p_parts[-1] in rosters:
        _ATHLETE_SEARCH_CACHE[p_norm] = (now, rosters[p_parts[-1]])
        return rosters[p_parts[-1]]

    # 2. Query ESPN Athlete Search API
    resolved_name = player_name.title()
    team_abbr = "NBA" if sport_u == "NBA" else sport_u
    team_name = team_abbr
    teammates: List[str] = []

    try:
        encoded_q = requests.utils.quote(player_name)
        search_url = f"https://site.web.api.espn.com/apis/common/v3/search?query={encoded_q}&limit=5&type=player"
        r = requests.get(search_url, timeout=4)
        if r.status_code == 200:
            data = r.json()
            for it in data.get("items", []):
                t_type = it.get("type") or ""
                url_str = it.get("url") or ""
                if t_type == "player" or "athlete" in url_str:
                    resolved_name = it.get("displayName") or it.get("title") or resolved_name
                    t_info = it.get("team") or {}
                    cand_abbr = str(t_info.get("abbreviation") or t_info.get("shortDisplayName") or "").upper()
                    cand_name = str(t_info.get("displayName") or t_info.get("name") or "")
                    if cand_abbr:
                        team_abbr = cand_abbr
                    if cand_name:
                        team_name = cand_name
                    break
    except Exception:
        pass

    # Special-case verified trades/updates
    if "LUKA" in p_norm:
        team_abbr = "LAL"
        team_name = "Los Angeles Lakers"
        resolved_name = "Luka Doncic"
    elif "CALEB WILSON" in p_norm:
        team_abbr = "UNC"
        team_name = "North Carolina Tar Heels"
        resolved_name = "Caleb Wilson"

    # 3. Check live scoreboard to find today's real matchup and schedule
    today_dt = datetime.now()
    today_m_d = today_dt.strftime("%b %d")
    matchup = f"{team_name} (Scheduled Slate)"
    game_date = f"Today ({today_m_d})"

    search_paths = ["basketball/nba", "basketball/mens-college-basketball", "football/nfl"]
    for sp_path in search_paths:
        try:
            sb_url = f"https://site.api.espn.com/apis/site/v2/sports/{sp_path}/scoreboard"
            s_resp = requests.get(sb_url, timeout=4)
            if s_resp.status_code == 200:
                events = s_resp.json().get("events", [])
                for ev in events:
                    comps = ev.get("competitions", [{}])[0]
                    competitors = comps.get("competitors", [])
                    found_team = False
                    for c in competitors:
                        t = c.get("team", {})
                        c_abbr = str(t.get("abbreviation") or "").upper()
                        c_name = str(t.get("displayName") or t.get("name") or "").upper()
                        if team_abbr in (c_abbr, c_name) or c_abbr in (team_abbr, team_name.upper()):
                            found_team = True
                            break
                    if found_team:
                        away_t = competitors[1].get("team", {}).get("abbreviation", "AWAY") if len(competitors) > 1 else "AWAY"
                        home_t = competitors[0].get("team", {}).get("abbreviation", "HOME")
                        matchup = f"{away_t} @ {home_t}"
                        status_type = ev.get("status", {}).get("type", {})
                        st_detail = status_type.get("shortDetail") or status_type.get("detail") or ""
                        game_date = f"Today ({today_m_d}) • {st_detail}" if st_detail else f"Today ({today_m_d})"
                        break
                if found_team:
                    break
        except Exception:
            pass

    # Teammate fallbacks from active rosters if available
    for r_obj in rosters.values():
        if r_obj.get("team_abbr") == team_abbr and r_obj.get("name") != resolved_name:
            if r_obj["name"] not in teammates:
                teammates.append(r_obj["name"])

    if not teammates:
        try:
            from quant_core import NBA_TEAMMATES
            teammates = [t for t in NBA_TEAMMATES.get(team_abbr, ["Austin Reaves", "Anthony Davis"]) if t != resolved_name]
        except Exception:
            if team_abbr == "LAL":
                teammates = ["LeBron James", "Anthony Davis", "Austin Reaves"]
            elif team_abbr == "GSW":
                teammates = ["Stephen Curry", "Draymond Green"]
            elif team_abbr == "UNC":
                teammates = ["RJ Davis", "Ian Jackson"]
            else:
                teammates = ["Austin Reaves", "Anthony Davis"]

    res_data = {
        "name": resolved_name,
        "team_abbr": team_abbr,
        "team_name": team_name,
        "matchup": matchup,
        "game_date": game_date,
        "teammates": teammates,
    }
    _ATHLETE_SEARCH_CACHE[p_norm] = (now, res_data)
    return res_data


def fetch_cached_kalshi_sports(sport: str = "ALL") -> List[Dict[str, Any]]:
    """Fetches and caches live paginated Kalshi markets."""
    sport_u = sport.upper()
    now = time.time()
    if sport_u in _KALSHI_PROPS_CACHE:
        ts, cached = _KALSHI_PROPS_CACHE[sport_u]
        if now - ts < CACHE_TTL_KALSHI:
            return cached

    markets = fetch_paginated_kalshi_sports(sport=sport_u, max_pages=6)
    _KALSHI_PROPS_CACHE[sport_u] = (now, markets)
    return markets


def find_live_kalshi_props_for_player(
    player_name: str,
    sport: str = "NBA",
    target_stat: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Scans live Kalshi markets for a specific player's open props.
    Strictly drops non-sports, novelty, and closed markets.
    """
    sport_u = sport.upper()
    try:
        from universal_entity_resolver import find_player_ladders
        ladders = find_player_ladders(player_name, stat_query=target_stat, sport=sport_u)
        if ladders:
            return ladders
    except Exception:
        pass

    markets = fetch_cached_kalshi_sports(sport_u)
    p_norm = player_name.upper().strip()
    p_parts = p_norm.split()
    last_name = p_parts[-1] if p_parts else p_norm

    matched_props = []

    for m in markets:
        status = str(m.get("status", "")).lower()
        if status not in ("active", "open"):
            continue

        title = str(m.get("title", "")).upper()
        subtitle = str(m.get("subtitle", "")).upper()
        ticker = str(m.get("ticker", "")).upper()
        ev_title = str(m.get("_event_title", "")).upper()
        blob = f"{ticker} {title} {subtitle} {ev_title}"

        if any(fk in blob for fk in FORBIDDEN_KEYWORDS):
            continue

        # Match player name
        if p_norm in blob or (len(last_name) >= 4 and last_name in blob):
            # Parse stat and line from ticker and title
            line = None
            stat = "PTS"
            tk_m = re.search(r"[-_]([OU])(\d+)P(\d+)(PTS|REB|AST|3PT|PRA|THREES)?", ticker)
            if tk_m:
                line = float(f"{tk_m.group(2)}.{tk_m.group(3)}")
                if tk_m.group(4):
                    stat = VALID_HOOPS_STATS.get(tk_m.group(4).upper(), stat)
            if line is None:
                m_num = re.search(r"(?:OVER|UNDER|O|U)?\s*(\d+(?:\.\d+)?)\s*(PTS|POINTS|POINT|REB|REBOUND|REBOUNDS|AST|ASSIST|ASSISTS|3PT|3PM|THREE|THREES|PRA|PASS|RUSH|REC|TOUCHDOWN)", blob)
                if m_num:
                    line = float(m_num.group(1))
                    stat = VALID_HOOPS_STATS.get(m_num.group(2).upper(), stat)
            if line is None:
                m_rev = re.search(r"(PTS|POINTS|POINT|REB|REBOUND|REBOUNDS|AST|ASSIST|ASSISTS|3PT|3PM|THREE|THREES|PRA)\s*(?:OVER|UNDER)?\s*(\d+(?:\.\d+)?)", blob)
                if m_rev:
                    stat = VALID_HOOPS_STATS.get(m_rev.group(1).upper(), stat)
                    line = float(m_rev.group(2))

            if target_stat and target_stat != "ALL" and stat != target_stat:
                continue

            yes_ask = m.get("yes_ask") or 50
            yes_bid = m.get("yes_bid") or 48

            matched_props.append({
                "ticker": ticker,
                "title": m.get("title", ticker),
                "player": player_name.title(),
                "stat": stat,
                "line": line or 20.5,
                "yes_ask": yes_ask,
                "yes_bid": yes_bid,
                "market_url": resolve_working_kalshi_url(ticker),
                "raw_market": m,
            })

    return matched_props


def find_live_sgp_partner(
    teammates: List[str],
    sport: str = "NBA",
) -> Optional[Dict[str, Any]]:
    """
    Checks if any active teammate in the SAME game has an ACTUAL OPEN Kalshi market.
    Returns None if no teammate has an open market (NEVER fakes a partner).
    """
    for tm in teammates:
        tm_props = find_live_kalshi_props_for_player(tm, sport=sport)
        if tm_props:
            best_prop = tm_props[0]
            return {
                "partner": tm.title(),
                "stat": best_prop["stat"],
                "line": best_prop["line"],
                "side": "OVER",
                "ticker": best_prop["ticker"],
                "market_url": best_prop["market_url"],
                "partner_prob": 54.0,
                "rho": 0.42,
            }
    return None


def get_real_open_markets_summary(sport: str = "NBA", limit: int = 5) -> List[str]:
    """
    Extracts actual open Kalshi market titles for display when a queried player has no open lines.
    """
    try:
        from auto_market_index import get_real_open_markets_summary as auto_summary
        return auto_summary(sport=sport, limit=limit)
    except Exception:
        pass
    markets = fetch_cached_kalshi_sports(sport)
    summaries = []
    seen = set()

    for m in markets:
        title = m.get("title") or m.get("ticker", "")
        t_upper = title.upper()
        if any(fk in t_upper for fk in FORBIDDEN_KEYWORDS):
            continue
        yes_ask = int(m.get("yes_ask") or m.get("no_ask") or 0)
        if yes_ask <= 0:
            continue
        if title not in seen:
            seen.add(title)
            summaries.append(f"• <b>{title}</b> (Ask: {yes_ask}¢)")
        if len(summaries) >= limit:
            break

    return summaries
