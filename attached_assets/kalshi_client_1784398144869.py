"""
Thin client for Kalshi's public (no-auth) market data endpoints.

Docs: https://docs.kalshi.com/api-reference/market
Base URL for public market-data reads (no API key needed): 
    https://api.elections.kalshi.com/trade-api/v2
(Despite the "elections" subdomain, this serves ALL Kalshi markets.)

This client intentionally does NOT implement authenticated endpoints
(orders, portfolio, etc.) - this tool is read-only market research,
not a trading client. Placing orders is a separate, deliberate step
you take with your own signed requests and your own risk controls.
"""
from __future__ import annotations

import time
from typing import Iterator

import requests

BASE_URL = "https://external-api.kalshi.com/trade-api/v2"

# Keywords used to identify esports series/markets, since Kalshi's
# category field for these may just be "Sports" / "Esports" depending
# on how a given series is tagged. We match on both category and
# title keywords to be safe.
GAME_KEYWORDS = {
    "cs2": ["cs2", "counter-strike", "counter strike"],
    "lol": ["league of legends", "lol "],
    "valorant": ["valorant"],
    "dota2": ["dota 2", "dota2", "dota "],
    "overwatch": ["overwatch"],
    "cod": ["call of duty", "cdl"],
}


class KalshiClient:
    def __init__(self, base_url: str = BASE_URL, timeout: float = 15.0, retries: int = 3):
        self.base_url = base_url
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}{path}"
        last_err = None
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"GET {path} failed after {self.retries} attempts: {last_err}")

    def iter_series(self) -> Iterator[dict]:
        """Yield all series (paginated)."""
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/series", params=params)
            for s in data.get("series", []):
                yield s
            cursor = data.get("cursor")
            if not cursor:
                break

    def iter_markets(self, series_ticker: str | None = None, status: str = "open") -> Iterator[dict]:
        """Yield markets, optionally filtered to one series, paginated."""
        cursor = None
        while True:
            params = {"limit": 200, "status": status}
            if series_ticker:
                params["series_ticker"] = series_ticker
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params)
            for m in data.get("markets", []):
                yield m
            cursor = data.get("cursor")
            if not cursor:
                break

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}").get("market", {})

    def get_orderbook(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}/orderbook").get("orderbook", {})

    def match_game(self, text: str) -> str | None:
        """Return which tracked game a series/market title belongs to, if any."""
        t = (text or "").lower()
        for game, keywords in GAME_KEYWORDS.items():
            if any(k in t for k in keywords):
                return game
        return None

    def find_esports_series(self) -> list[dict]:
        """Filter all series down to the ones matching our tracked esports games."""
        out = []
        for s in self.iter_series():
            title = s.get("title", "")
            category = s.get("category", "")
            game = self.match_game(title) or self.match_game(category)
            if game:
                s["_game"] = game
                out.append(s)
        return out
