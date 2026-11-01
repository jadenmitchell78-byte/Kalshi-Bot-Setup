# ~/peezy_bot/atd_odds_store.py
"""
Shared ATD Odds State Manager.

Maintains a live JSON cache (~/peezy_bot/live_atd_odds.json) of market American odds.
Enables instant hot-reloading by:
1. Manual Shorthand Injection (/atd_odds Hunt -115, Steele +550, Kelce +135)
   - Fuzzy-matches partial/last names against active ESPN roster
2. Auto-Fetch Mode (/atd_odds fetch kc)
   - Scrapes live player_anytime_td odds from The Odds API v4 (FanDuel, DraftKings, BetMGM, Bovada)
3. Hot-reloads within live_nfl_atd_engine.py on every 1.5s red-zone drive tick.
"""

from difflib import get_close_matches
import json
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional

import aiohttp
import requests

BASE_DIR = Path(__file__).resolve().parent
ODDS_CACHE_FILE = os.environ.get("ATD_ODDS_CACHE", str(BASE_DIR / "live_atd_odds.json"))

# Team code to full name lookup for The Odds API
NFL_TEAM_MAP = {
    "kc": "Kansas City Chiefs",
    "den": "Denver Broncos",
    "buf": "Buffalo Bills",
    "bal": "Baltimore Ravens",
    "det": "Detroit Lions",
    "sf": "San Francisco 49ers",
    "phi": "Philadelphia Eagles",
    "cin": "Cincinnati Bengals",
    "hou": "Houston Texans",
    "dal": "Dallas Cowboys",
    "gb": "Green Bay Packers",
    "mia": "Miami Dolphins",
    "min": "Minnesota Vikings",
    "pit": "Pittsburgh Steelers",
    "tb": "Tampa Bay Buccaneers",
    "atl": "Atlanta Falcons",
    "ari": "Arizona Cardinals",
    "sea": "Seattle Seahawks",
    "lar": "Los Angeles Rams",
    "lac": "Los Angeles Chargers",
    "chi": "Chicago Bears",
    "cle": "Cleveland Browns",
    "ind": "Indianapolis Colts",
    "jax": "Jacksonville Jaguars",
    "lv": "Las Vegas Raiders",
    "ne": "New England Patriots",
    "no": "New Orleans Saints",
    "nyg": "New York Giants",
    "nyj": "New York Jets",
    "ten": "Tennessee Titans",
    "wsh": "Washington Commanders",
    "car": "Carolina Panthers",
}

# Active skill player fallback list for fuzzy matching
ACTIVE_ROSTER_NAMES = [
    "Kareem Hunt", "Carson Steele", "Samaje Perine", "Travis Kelce",
    "DeAndre Hopkins", "Xavier Worthy", "Patrick Mahomes",
    "Bo Nix", "Javonte Williams", "Jaleel McLaughlin", "Courtland Sutton"
]


class ATDOddsStore:
    @staticmethod
    def load_odds() -> Dict[str, int]:
        """Reads cached { 'Player Full Name': american_odds_int } from disk."""
        if not os.path.exists(ODDS_CACHE_FILE):
            return {}
        try:
            with open(ODDS_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("odds", {})
        except Exception:
            return {}

    @staticmethod
    def save_odds(new_odds: Dict[str, int], merge: bool = True) -> Dict[str, int]:
        """Writes updated American odds to disk and returns the active board."""
        current = ATDOddsStore.load_odds() if merge else {}
        current.update(new_odds)
        payload = {
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "odds": current
        }
        try:
            os.makedirs(os.path.dirname(os.path.abspath(ODDS_CACHE_FILE)), exist_ok=True)
            with open(ODDS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
        except Exception as e:
            print(f"[ATDOddsStore] Write error: {e}")
        return current

    @staticmethod
    def clear_odds():
        """Wipes the live ATD odds cache."""
        if os.path.exists(ODDS_CACHE_FILE):
            try:
                os.remove(ODDS_CACHE_FILE)
            except Exception:
                pass

    @staticmethod
    def implied_pct(odds: int) -> float:
        """Converts American odds integer to implied probability percentage."""
        if odds < 0:
            return ((-odds) / ((-odds) + 100.0)) * 100.0
        return (100.0 / (odds + 100.0)) * 100.0

    @staticmethod
    def parse_manual_input(raw_arg: str, active_roster_names: List[str]) -> Dict[str, int]:
        """
        Parses shorthand like: 'Hunt -120, Steele +550, Kelce +140'
        Fuzzy-matches against active roster names (e.g., 'Hunt' -> 'Kareem Hunt').
        """
        parsed: Dict[str, int] = {}
        entries = [x.strip() for x in raw_arg.split(",") if x.strip()]

        last_name_map = {name.split()[-1].lower(): name for name in active_roster_names if name}
        full_name_map = {name.lower(): name for name in active_roster_names if name}

        for entry in entries:
            parts = entry.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            name_query, price_str = parts[0].strip().lower(), parts[1].strip()

            try:
                price = int(price_str.replace("+", ""))
            except ValueError:
                continue

            # 1. Exact full or last name match
            matched_name = full_name_map.get(name_query) or last_name_map.get(name_query)

            # 2. Fuzzy fallback if user typed a partial name
            if not matched_name:
                matches = get_close_matches(name_query, list(last_name_map.keys()), n=1, cutoff=0.55)
                if matches:
                    matched_name = last_name_map[matches[0]]
                else:
                    matched_name = parts[0].strip().title()

            parsed[matched_name] = price

        return parsed

    @staticmethod
    async def fetch_live_book_atd(
        odds_api_key: str,
        team_query: str = "Kansas City Chiefs",
        bookmaker_filter: str = "fanduel,draftkings,betmgm,bovada"
    ) -> Dict[str, int]:
        """
        Pulls live `player_anytime_td` odds from The Odds API v4 for the matching game.
        Stores the best available American price for each player.
        """
        base_url = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
        scraped_odds: Dict[str, int] = {}

        async with aiohttp.ClientSession() as session:
            # Step 1: Resolve Event ID
            events_url = f"{base_url}/events?apiKey={odds_api_key}"
            async with session.get(events_url, timeout=6) as resp:
                if resp.status != 200:
                    err_txt = await resp.text()
                    raise RuntimeError(f"Events API HTTP {resp.status}: {err_txt[:100]}")
                events = await resp.json()

            target_event_id = None
            q = team_query.lower()
            for ev in events:
                home = ev.get("home_team", "").lower()
                away = ev.get("away_team", "").lower()
                if q in home or q in away:
                    target_event_id = ev.get("id")
                    break

            if not target_event_id:
                raise ValueError(f"No live/upcoming NFL event found matching '{team_query}'")

            # Step 2: Query player_anytime_td market
            odds_url = (
                f"{base_url}/events/{target_event_id}/odds"
                f"?apiKey={odds_api_key}&regions=us&markets=player_anytime_td"
                f"&oddsFormat=american&bookmakers={bookmaker_filter}"
            )
            async with session.get(odds_url, timeout=8) as resp:
                if resp.status != 200:
                    err_txt = await resp.text()
                    raise RuntimeError(f"Odds API HTTP {resp.status}: {err_txt[:100]}")
                odds_data = await resp.json()

            for book in odds_data.get("bookmakers", []):
                for market in book.get("markets", []):
                    if market.get("key") != "player_anytime_td":
                        continue
                    for outcome in market.get("outcomes", []):
                        player_name = outcome.get("description") or outcome.get("name")
                        if outcome.get("name", "").lower() == "no":
                            continue
                        try:
                            price = int(outcome.get("price", 0))
                        except Exception:
                            continue

                        if player_name and price != 0:
                            if player_name not in scraped_odds or price > scraped_odds[player_name]:
                                scraped_odds[player_name] = price

        return scraped_odds

    @classmethod
    def fetch_live_book_atd_sync(
        cls,
        odds_api_key: str,
        team_query: str = "Kansas City Chiefs",
        bookmaker_filter: str = "fanduel,draftkings,betmgm,bovada"
    ) -> Dict[str, int]:
        """Synchronous wrapper for fetch_live_book_atd."""
        base_url = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
        scraped_odds: Dict[str, int] = {}

        events_url = f"{base_url}/events?apiKey={odds_api_key}"
        r = requests.get(events_url, timeout=6)
        if r.status_code != 200:
            raise RuntimeError(f"Events API HTTP {r.status_code}: {r.text[:100]}")
        events = r.json()

        target_event_id = None
        q = team_query.lower()
        for ev in events:
            home = ev.get("home_team", "").lower()
            away = ev.get("away_team", "").lower()
            if q in home or q in away:
                target_event_id = ev.get("id")
                break

        if not target_event_id:
            raise ValueError(f"No live/upcoming NFL event found matching '{team_query}'")

        odds_url = (
            f"{base_url}/events/{target_event_id}/odds"
            f"?apiKey={odds_api_key}&regions=us&markets=player_anytime_td"
            f"&oddsFormat=american&bookmakers={bookmaker_filter}"
        )
        r2 = requests.get(odds_url, timeout=8)
        if r2.status_code != 200:
            raise RuntimeError(f"Odds API HTTP {r2.status_code}: {r2.text[:100]}")
        odds_data = r2.json()

        for book in odds_data.get("bookmakers", []):
            for market in book.get("markets", []):
                if market.get("key") != "player_anytime_td":
                    continue
                for outcome in market.get("outcomes", []):
                    player_name = outcome.get("description") or outcome.get("name")
                    if outcome.get("name", "").lower() == "no":
                        continue
                    try:
                        price = int(outcome.get("price", 0))
                    except Exception:
                        continue

                    if player_name and price != 0:
                        if player_name not in scraped_odds or price > scraped_odds[player_name]:
                            scraped_odds[player_name] = price

        return scraped_odds
