# ~/peezy_bot/unified_kalshi_engine.py
"""
Unified Dual-Engine Architecture:
Bridges daily sports props, game lines, and macro/futures contracts
(e.g. LeBron Retirement, Team USA Roster, NFL futures).
"""

import time
import requests
from typing import Any, Callable, Dict, List, Optional

FUTURES_SERIES_PREFIXES = [
    "KXNBA",          # General NBA
    "KXBASKETBALL",   # Basketball futures / Team USA rosters
    "KXLEBRON",       # LeBron milestone / retirement markets
    "KXNFL",          # NFL futures
]

class UnifiedKalshiEngine:
    def __init__(
        self,
        base_url: str = "https://api.elections.kalshi.com",
        auth_header_fn: Optional[Callable[[str, str], Dict[str, str]]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.auth_header_fn = auth_header_fn
        self.market_cache: List[Dict[str, Any]] = []
        self.last_cache_time: float = 0.0

    def fetch_all_active_markets(self, force: bool = False, sport_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Unified Fetcher: Sweeps both Short-Term Sports Events AND Macro/Futures Markets
        so nothing is ever missed (whether it's an NBA prop, game line, or a LeBron futures contract).
        """
        if not force and self.market_cache and (time.time() - self.last_cache_time < 30):
            if sport_filter:
                sp_u = sport_filter.upper()
                return [
                    m for m in self.market_cache
                    if sp_u in f"{m.get('ticker', '')} {m.get('title', '')} {m.get('category', '')}".upper()
                ] or self.market_cache
            return self.market_cache

        all_collected: List[Dict[str, Any]] = []
        seen_tickers = set()

        # 1. Sweep Sports Events (Nested Props & Game Lines)
        cursor = None
        for _ in range(6):
            try:
                url = f"{self.base_url}/trade-api/v2/events?status=open&limit=200&with_nested_markets=true"
                if cursor:
                    url += f"&cursor={cursor}"
                resp = requests.get(url, timeout=8)
                if resp.status_code == 200:
                    data = resp.json()
                    events = data.get("events", [])
                    for ev in events:
                        for m in ev.get("markets", []):
                            if m.get("status") in ("active", "open"):
                                t = m.get("ticker")
                                if t and t not in seen_tickers:
                                    seen_tickers.add(t)
                                    all_collected.append(m)
                    cursor = data.get("cursor")
                    if not cursor or not events:
                        break
                else:
                    break
            except Exception:
                break

        # 2. Sweep Macro & Futures Markets (e.g. LeBron Retirement, Team USA, Roster Futures)
        cursor = None
        for _ in range(4):
            try:
                url_macro = f"{self.base_url}/trade-api/v2/markets?status=open&limit=200"
                if cursor:
                    url_macro += f"&cursor={cursor}"
                resp = requests.get(url_macro, timeout=8)
                if resp.status_code == 200:
                    data = resp.json()
                    mkts = data.get("markets", [])
                    for m in mkts:
                        if m.get("status") in ("active", "open"):
                            t = m.get("ticker")
                            if t and t not in seen_tickers:
                                seen_tickers.add(t)
                                all_collected.append(m)
                    cursor = data.get("cursor")
                    if not cursor or not mkts:
                        break
                else:
                    break
            except Exception:
                break

        # 3. Targeted Futures Series sweeps
        for pfx in FUTURES_SERIES_PREFIXES:
            try:
                url_pfx = f"{self.base_url}/trade-api/v2/markets?status=open&limit=100&series_ticker={pfx}"
                resp = requests.get(url_pfx, timeout=4)
                if resp.status_code == 200:
                    for m in resp.json().get("markets", []):
                        if m.get("status") in ("active", "open"):
                            t = m.get("ticker")
                            if t and t not in seen_tickers:
                                seen_tickers.add(t)
                                all_collected.append(m)
            except Exception:
                continue

        if all_collected:
            self.market_cache = all_collected
            self.last_cache_time = time.time()

        if sport_filter:
            sp_u = sport_filter.upper()
            filtered = [
                m for m in all_collected
                if sp_u in f"{m.get('ticker', '')} {m.get('title', '')} {m.get('category', '')}".upper()
            ]
            return filtered or all_collected

        return all_collected

    def smart_resolve_contract(self, query: str) -> Optional[Dict[str, Any]]:
        """
        Universal resolver: Matches user input against sports props, game lines, 
        or macro futures titles (e.g., 'LeBron', 'Chet', 'Randle', 'Team USA', 'Retirement').
        """
        markets = self.fetch_all_active_markets()
        q_upper = query.upper().strip()
        tokens = [t for t in q_upper.replace("-", " ").replace(".", " ").split() if len(t) >= 2]
        if not tokens:
            return None

        best_match = None
        max_score = 0

        for m in markets:
            searchable = f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')} {m.get('category', '')}".upper()
            score = sum(2 if tok == m.get('ticker', '').upper() else (1 if tok in searchable else 0) for tok in tokens)
            if score > max_score:
                max_score = score
                best_match = m

        return best_match if max_score > 0 else None

    def get_portfolio_positions(self, headers: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        """
        Pulls actual live portfolio holdings from Kalshi to display in your HUD/Open Positions.
        """
        url = f"{self.base_url}/trade-api/v2/portfolio/positions"
        auth_h = headers
        if not auth_h and self.auth_header_fn:
            try:
                auth_h = self.auth_header_fn("GET", "/trade-api/v2/portfolio/positions")
            except Exception:
                pass

        if not auth_h:
            auth_h = {"Accept": "application/json"}

        try:
            resp = requests.get(url, headers=auth_h, timeout=8)
            if resp.status_code == 200:
                return resp.json().get("market_positions", [])
        except Exception:
            pass
        return []

unified_engine = UnifiedKalshiEngine()
