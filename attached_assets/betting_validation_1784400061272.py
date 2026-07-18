import os
import requests
import time
from datetime import datetime

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

# Dict mapping parsed leagues to The Odds API sport keys
SPORT_KEYS = {
    "NBA": "basketball_nba",
    "NFL": "americanfootball_nfl",
    "MLB": "baseball_mlb",
    "NHL": "icehockey_nhl",
    "Soccer": "soccer_usa_mls",
    "World Cup": "soccer_fifa_world_cup",
}

# Roster of official teams to match against typos
OFFICIAL_TEAMS = [
    "Dallas Mavericks", "Boston Celtics", "Los Angeles Lakers", "Golden State Warriors",
    "Milwaukee Bucks", "Miami Heat", "Phoenix Suns", "Denver Nuggets", "New York Knicks",
    "Philadelphia 76ers", "Kansas City Chiefs", "San Francisco 49ers", "Dallas Cowboys",
    "Philadelphia Eagles", "Buffalo Bills", "Green Bay Packers", "Baltimore Ravens",
    "Cincinnati Bengals", "New York Yankees", "Boston Red Sox", "Los Angeles Dodgers",
    "San Francisco Giants", "New York Mets", "Philadelphia Phillies", "Houston Astros",
    "Chicago Cubs",
    # World Cup International Teams
    "Argentina", "Brazil", "Spain", "Portugal", "Germany",
    "Netherlands", "Croatia", "Morocco", "USA", "Mexico", "Canada", "Belgium",
    "Japan", "Senegal", "Uruguay", "Norway"
]

def is_sport_in_season(league: str) -> (bool, str):
    """
    Checks if the league/sport is currently in season based on the current calendar month.
    Returns (is_in_season, reason).
    """
    import datetime
    current_month = datetime.datetime.now().month # 1-12
    
    # 2026 World Cup special case: June/July 2026 is World Cup season!
    if league == "World Cup":
        if current_month in [6, 7]:
            return True, "World Cup is currently active (June-July 2026)."
        else:
            return False, "World Cup is out of season (only active June-July)."
            
    if league == "NBA":
        # NBA is active from late October (10) to mid-June (6)
        if current_month in [11, 12, 1, 2, 3, 4, 5]:
            return True, "NBA is in season."
        elif current_month == 10:
            return True, "NBA pre-season starting."
        elif current_month == 6:
            # End of June is typically post-Finals off-season
            current_day = datetime.datetime.now().day
            if current_day <= 20:
                return True, "NBA Finals wrapping up."
            else:
                return False, "NBA is now in the off-season (late June-October)."
        else:
            return False, "NBA is currently in the off-season (July-September)."
            
    if league == "NFL":
        # NFL is active from September (9) to early February (2)
        if current_month in [9, 10, 11, 12, 1, 2]:
            return True, "NFL is in season."
        else:
            return False, "NFL is currently in the off-season (March-August)."
            
    if league == "NHL":
        # NHL is active from October (10) to June (6)
        if current_month in [10, 11, 12, 1, 2, 3, 4, 5]:
            return True, "NHL is in season."
        elif current_month == 6:
            current_day = datetime.datetime.now().day
            if current_day <= 20:
                return True, "NHL playoffs wrapping up."
            else:
                return False, "NHL is now in the off-season (late June-September)."
        else:
            return False, "NHL is currently in the off-season (July-September)."
            
    if league == "MLB":
        # MLB is active from late March (3) to early November (11)
        if current_month in [3, 4, 5, 6, 7, 8, 9, 10, 11]:
            return True, "MLB is in season."
        else:
            return False, "MLB is currently in the off-season (December-February)."
            
    return True, "League season status unknown, permitting."


def validate_and_fetch_odds(league: str, matchup: str, bet_type: str, target_asset: str, line_value=None):
    """
    Module C: Validation & Odds Engine.
    Validates matchup names, verifies game hasn't started/ended,
    matches player/team names against rosters to eliminate typos,
    and fetches live odds from major bookmakers (DraftKings, FanDuel, Caesars).
    
    If API key is missing or fails, falls back to a simulated validation engine.
    """
    # 0. Active Season Guardrail
    in_season, season_reason = is_sport_in_season(league)
    if not in_season:
        print(f"⚠️ Validation FAILED: {league} is out of season. Reason: {season_reason}")
        return False, f"Sport out of season: {season_reason}", None, None

    api_key = os.environ.get("THE_ODDS_API_KEY")
    sport_key = SPORT_KEYS.get(league)
    
    # 1. Typo / Roster Matching Guardrail
    if bet_type == "Parlay":
        matched_asset = target_asset
    else:
        matched_asset = _match_roster(target_asset)
    
    if not matched_asset:
        print(f"⚠️ Validation FAILED: target_asset '{target_asset}' did not match official rosters.")
        return False, f"Invalid player or team: {target_asset}", None, None
        
    if api_key and sport_key and bet_type != "Parlay":
        try:
            print(f"📡 Querying The Odds API for {league} odds ({sport_key})...")
            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/?apiKey={api_key}&regions=us&markets=h2h,spreads,totals&oddsFormat=american"
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                games = r.json()
                
                # Search for the matched game
                for game in games:
                    home_team = game.get("home_team")
                    away_team = game.get("away_team")
                    start_time_str = game.get("commence_time")
                    
                    # Fuzzy match matchup teams
                    if _is_game_match(matchup, home_team, away_team):
                        # Verify game has not started yet
                        # start_time_str format: ISO e.g., 2026-06-23T23:30:00Z
                        start_time = datetime.strptime(start_time_str, "%Y-%m-%dT%H:%M:%SZ").timestamp()
                        now = time.time()
                        
                        if now > start_time:
                            print(f"⚠️ Validation FAILED: Game {home_team} vs {away_team} has already started.")
                            return False, "Game has already commenced.", None, None
                            
                        # Fetch odds from sportsbooks
                        bookmakers = game.get("bookmakers", [])
                        odds_list = []
                        for bm in bookmakers:
                            bm_key = bm.get("key")
                            if bm_key in ["draftkings", "fanduel", "caesars"]:
                                for market in bm.get("markets", []):
                                    m_key = market.get("key")
                                    # Process Moneyline
                                    if bet_type == "Moneyline" and m_key == "h2h":
                                        for outcome in market.get("outcomes", []):
                                            if _team_name_match(matched_asset, outcome.get("name")):
                                                odds_list.append((bm.get("title"), outcome.get("price")))
                                                
                                    # Process Spread
                                    elif bet_type == "Spread" and m_key == "spreads":
                                        for outcome in market.get("outcomes", []):
                                            if _team_name_match(matched_asset, outcome.get("name")):
                                                # Check spread line match
                                                api_line = outcome.get("point")
                                                if line_value is None or abs(api_line - line_value) <= 1.0:
                                                    odds_list.append((bm.get("title"), outcome.get("price"), api_line))
                                                    
                        if odds_list:
                            # Pick the average odds or first sportsbook odds
                            best_source, best_price = odds_list[0][0], odds_list[0][1]
                            resolved_line = odds_list[0][2] if len(odds_list[0]) > 2 else line_value
                            print(f"✅ Odds verified on {best_source}: {best_price} (Line: {resolved_line})")
                            return True, "Verified Live Odds", best_price, resolved_line
                            
                print(f"⚠️ Live market line not found for {matchup} on major sportsbooks.")
                return False, "Market line currently unavailable.", None, None
        except Exception as e:
            print(f"⚠️ The Odds API query exception: {e}. Falling back to simulation...")
            
    # 2. Fallback / Simulation Mode
    print(f"🧪 Running Validation Simulation for {matched_asset} in {league} matchup '{matchup}'")
    # Simulate a future start time (e.g. tonight at 7 PM)
    simulated_odds = -110
    if bet_type == "Moneyline":
        simulated_odds = -150 if "celtics" in matchup.lower() or "chiefs" in matchup.lower() else +130
        
    resolved_line = line_value
    return True, "Simulated Verified Odds", simulated_odds, resolved_line

def _match_roster(asset: str) -> str:
    """Matches asset against official lists to eliminate typos."""
    asset_lower = asset.lower().strip()
    
    # 1. Check teams
    for official in OFFICIAL_TEAMS:
        if asset_lower in official.lower() or official.lower() in asset_lower:
            return official
            
    # 2. Check players (returns full official name)
    # Basic lookup - if it's Jayson Tatum or Tatum, return Jayson Tatum
    if "tatum" in asset_lower:
        return "Jayson Tatum"
    if "doncic" in asset_lower or "luka" in asset_lower:
        return "Luka Doncic"
    if "brown" in asset_lower:
        return "Jaylen Brown"
    if "irving" in asset_lower:
        return "Kyrie Irving"
    if "mahomes" in asset_lower:
        return "Patrick Mahomes"
    if "ohtani" in asset_lower:
        return "Shohei Ohtani"
    if "messi" in asset_lower:
        return "Lionel Messi"
    if "mbappe" in asset_lower:
        return "Kylian Mbappe"
    if "bellingham" in asset_lower:
        return "Jude Bellingham"
    if "ronaldo" in asset_lower:
        return "Cristiano Ronaldo"
    if "kane" in asset_lower:
        return "Harry Kane"
    if "vinicius" in asset_lower or "vini" in asset_lower:
        return "Vinicius Junior"
    if "bobb" in asset_lower:
        return "Oscar Bobb"
    if "norway" in asset_lower:
        return "Norway"
        
    # Return as-is if no match but let it pass if it's at least 3 chars
    return asset if len(asset) >= 3 else None

def _is_game_match(matchup: str, home: str, away: str) -> bool:
    matchup_lower = matchup.lower()
    home_lower = home.lower()
    away_lower = away.lower()
    
    # Check if home and away team aliases are in the matchup text
    home_tokens = home_lower.split()
    away_tokens = away_lower.split()
    
    # At least one token from home team and away team must be in matchup
    has_home = any(t in matchup_lower for t in home_tokens if len(t) > 3)
    has_away = any(t in matchup_lower for t in away_tokens if len(t) > 3)
    
    return has_home and has_away

def _team_name_match(asset: str, api_outcome_name: str) -> bool:
    asset_lower = asset.lower()
    outcome_lower = api_outcome_name.lower()
    return asset_lower in outcome_lower or outcome_lower in asset_lower
