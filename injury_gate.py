# ~/peezy_bot/injury_gate.py
import datetime
import json
import math
import re
import requests
import time
import traceback
import uuid
from typing import Dict, Tuple


class RosterInjuryGate:
    def __init__(self) -> None:
        # Cache injury/status checks for 20 minutes (1200s) to respect API rate limits
        self.cache: Dict[str, Tuple[float, bool, str]] = {}

    def check_player_availability(self, player_name: str, sport: str = "NBA") -> Tuple[bool, str]:
        """
        Queries live sports endpoints to verify if a player is ACTIVE, OUT, or on IR.
        Returns: (is_active: bool, status_message: str)
        """
        player_clean = player_name.upper().strip()
        now = time.time()

        # Check in-memory cache
        if player_clean in self.cache:
            ts, is_act, msg = self.cache[player_clean]
            if (now - ts) < 1200:
                return is_act, msg

        sport_path = "basketball/nba" if sport.upper() == "NBA" else "football/nfl"
        url = f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard"

        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                for event in data.get("events", []):
                    competitions = event.get("competitions", [{}])[0]
                    for comp in competitions.get("competitors", []):
                        for athlete in comp.get("roster", []):
                            name = athlete.get("athlete", {}).get("displayName", "").upper()
                            if player_clean in name or name in player_clean:
                                status = str(athlete.get("status", {}).get("type", "active")).lower()
                                if status in ("out", "ir", "inactive", "suspended", "doubtful"):
                                    res = (False, f"INACTIVE / {status.upper()}")
                                    self.cache[player_clean] = (now, res[0], res[1])
                                    return res
        except Exception:
            pass

        # Fallback check against known keyword injury flags
        known_out_keywords = ["OUT", "SUSPENDED", "IR", "INJURED RESERVE"]
        for kw in known_out_keywords:
            if kw in player_clean.split():
                res = (False, "INJURED RESERVE (IR)")
                self.cache[player_clean] = (now, res[0], res[1])
                return res

        res = (True, "ACTIVE 🟢")
        self.cache[player_clean] = (now, res[0], res[1])
        return res
