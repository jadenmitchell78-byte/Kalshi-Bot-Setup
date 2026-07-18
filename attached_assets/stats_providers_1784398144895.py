"""
Pluggable stats enrichment.

Default: Liquipedia's public API (free, but requests an API key for
higher rate limits - see https://api.liquipedia.net/). This gives
basic team/roster context, not deep per-map/per-player performance
stats. If you later get a paid provider (PandaScore, GRID, Sportradar,
Bayes, Abios...), implement a new class with the same `.enrich()`
method and swap it in in config.yaml - nothing else needs to change.
"""
from __future__ import annotations

import time
from typing import Optional

import requests

LIQUIPEDIA_WIKIS = {
    "cs2": "counterstrike",
    "lol": "leagueoflegends",
    "valorant": "valorant",
    "dota2": "dota2",
    "overwatch": "overwatch",
    "cod": "callofduty",
}


class NullStatsProvider:
    """Does nothing - use if you want pure Kalshi odds with no enrichment."""

    def enrich(self, game: str, team_name: str) -> dict:
        return {}


class LiquipediaStatsProvider:
    def __init__(self, api_key: Optional[str] = None, timeout: float = 10.0):
        self.api_key = api_key
        self.timeout = timeout
        self.session = requests.Session()
        self._cache: dict[str, dict] = {}

    def _headers(self) -> dict:
        headers = {"User-Agent": "kalshi-esports-tracker/1.0 (personal research tool)"}
        if self.api_key:
            headers["Authorization"] = f"Apikey {self.api_key}"
        return headers

    def enrich(self, game: str, team_name: str) -> dict:
        """Best-effort lookup of a team's Liquipedia page summary. Returns
        {} on any failure - enrichment is optional and must never break
        the main Kalshi data pull."""
        wiki = LIQUIPEDIA_WIKIS.get(game)
        if not wiki or not team_name:
            return {}

        cache_key = f"{wiki}:{team_name.lower()}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        url = f"https://api.liquipedia.net/api/v3/page"
        params = {"wiki": wiki, "page": team_name}
        try:
            resp = self.session.get(url, params=params, headers=self._headers(), timeout=self.timeout)
            time.sleep(1)  # be polite - respect Liquipedia's rate limits
            if resp.status_code != 200:
                return {}
            data = resp.json()
            result = {
                "liquipedia_page": data.get("pagename"),
                "liquipedia_url": f"https://liquipedia.net/{wiki}/{data.get('pagename', team_name).replace(' ', '_')}",
            }
            self._cache[cache_key] = result
            return result
        except requests.RequestException:
            return {}
