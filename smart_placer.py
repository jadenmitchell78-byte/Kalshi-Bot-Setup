# ~/peezy_bot/smart_placer.py
"""
Smart Placing Engine for Kalshi V2:
1. Full Cursor Pagination + Series Sweep (Resolves deep player prop books)
2. Fuzzy Player Prop Ticker Resolver (Maps "J Randle PTS 21.5" -> Real Kalshi Ticker)
3. Smart Orderbook Imbalance (OBI) & Maker-Limit Pricing Engine
4. Fractional Kelly Sizing & Live Kalshi Order Execution (Ed25519 & RSA Dual Auth)
5. Smart Combo Basket Executor (Places Correlated Copula Legs Together)
"""

import os
import re
import time
import uuid
import json
import base64
import requests
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Clean Stat Alias Mapping (Strips stat words out of player names!)
STAT_ALIASES = {
    "3PTS": "3PM", "3PT": "3PM", "3PM": "3PM", "THREES": "3PM", "3S": "3PM",
    "PTS": "PTS", "POINTS": "PTS", "POINT": "PTS",
    "REB": "REB", "REBS": "REB", "REBOUNDS": "REB",
    "AST": "AST", "ASTS": "AST", "ASSISTS": "AST",
    "PRA": "PRA", "PR": "PR", "PA": "PA", "RA": "RA",
    "K": "K", "STRIKEOUTS": "K", "SOG": "SOG",
    "REC_YDS": "REC_YDS", "RUSH_YDS": "RUSH_YDS", "PASS_YDS": "PASS_YDS",
    "TD": "TD", "TDS": "TD", "TOUCHDOWNS": "TD", "TOUCHDOWN": "TD",
}

# Real Active Teams for Sunday Oct 25, 2026 Slate
TODAY_ACTIVE_NBA_TEAMS = {"LAL", "UTA", "IND", "BKN", "ORL", "NYK", "LAC", "OKC", "TOR", "MIN", "DET", "PHI", "MEM", "SAC"}

TEAM_SCHEDULE_NOTES = {
    "MIA": "Miami Heat are off today (Next game: Mon Oct 26 vs DAL)",
    "BOS": "Boston Celtics are off today (Next game: Mon Oct 26 vs MIL)",
    "GSW": "Golden State Warriors are off today (Next game: Mon Oct 26 vs PHX)",
    "DEN": "Denver Nuggets are off today (Next game: Mon Oct 26 @ NOP)",
    "HOU": "Houston Rockets are off today (Next game: Mon Oct 26 vs ATL)",
    "SAS": "San Antonio Spurs are off today (Next game: Mon Oct 26 vs CHI)",
    "WAS": "Washington Wizards are off today (Next game: Mon Oct 26 @ CHA)",
    "CHI": "Chicago Bulls are off today (Next game: Mon Oct 26 @ SAS)",
    "ATL": "Atlanta Hawks are off today (Next game: Mon Oct 26 @ HOU)",
    "NOP": "New Orleans Pelicans are off today (Next game: Mon Oct 26 vs DEN)",
    "CLE": "Cleveland Cavaliers are off today (Next game: Mon Oct 26 @ NYK)",
    "MIL": "Milwaukee Bucks are off today (Next game: Mon Oct 26 @ BOS)",
    "PHX": "Phoenix Suns are off today (Next game: Mon Oct 26 @ GSW)",
    "DAL": "Dallas Mavericks are off today (Next game: Mon Oct 26 @ MIA)",
}

PLAYER_CANONICAL_MAP = {
    "TYLER HERO": ("Tyler Herro", "MIA", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "TYLER HERRO": ("Tyler Herro", "MIA", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "HERO": ("Tyler Herro", "MIA", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "HERRO": ("Tyler Herro", "MIA", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "J RANDLE": ("Julius Randle", "MIN", "VOLUME_SCORER"),
    "JULIUS RANDLE": ("Julius Randle", "MIN", "VOLUME_SCORER"),
    "RANDLE": ("Julius Randle", "MIN", "VOLUME_SCORER"),
    "A EDWARDS": ("Anthony Edwards", "MIN", "SLASHING_RIM_PRESSURE"),
    "ANTHONY EDWARDS": ("Anthony Edwards", "MIN", "SLASHING_RIM_PRESSURE"),
    "EDWARDS": ("Anthony Edwards", "MIN", "SLASHING_RIM_PRESSURE"),
    "ANT": ("Anthony Edwards", "MIN", "SLASHING_RIM_PRESSURE"),
    "L DONCIC": ("Luka Doncic", "DAL", "HELIOCENTRIC_PNR_HANDLER"),
    "LUKA DONCIC": ("Luka Doncic", "DAL", "HELIOCENTRIC_PNR_HANDLER"),
    "DONCIC": ("Luka Doncic", "DAL", "HELIOCENTRIC_PNR_HANDLER"),
    "S GILGEOUS-ALEXANDER": ("Shai Gilgeous-Alexander", "OKC", "SLASHING_RIM_PRESSURE"),
    "SHAI GILGEOUS-ALEXANDER": ("Shai Gilgeous-Alexander", "OKC", "SLASHING_RIM_PRESSURE"),
    "SGA": ("Shai Gilgeous-Alexander", "OKC", "SLASHING_RIM_PRESSURE"),
    "SHAI": ("Shai Gilgeous-Alexander", "OKC", "SLASHING_RIM_PRESSURE"),
    "J BRUNSON": ("Jalen Brunson", "NYK", "HELIOCENTRIC_PNR_HANDLER"),
    "JALEN BRUNSON": ("Jalen Brunson", "NYK", "HELIOCENTRIC_PNR_HANDLER"),
    "BRUNSON": ("Jalen Brunson", "NYK", "HELIOCENTRIC_PNR_HANDLER"),
    "C CUNNINGHAM": ("Cade Cunningham", "DET", "HELIOCENTRIC_PNR_HANDLER"),
    "CADE CUNNINGHAM": ("Cade Cunningham", "DET", "HELIOCENTRIC_PNR_HANDLER"),
    "CADE": ("Cade Cunningham", "DET", "HELIOCENTRIC_PNR_HANDLER"),
    "T MAXEY": ("Tyrese Maxey", "PHI", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "TYRESE MAXEY": ("Tyrese Maxey", "PHI", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "MAXEY": ("Tyrese Maxey", "PHI", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "LEBRON JAMES": ("LeBron James", "LAL", "HELIOCENTRIC_PNR_HANDLER"),
    "LEBRON": ("LeBron James", "LAL", "HELIOCENTRIC_PNR_HANDLER"),
    "LBJ": ("LeBron James", "LAL", "HELIOCENTRIC_PNR_HANDLER"),
    "ANTHONY DAVIS": ("Anthony Davis", "LAL", "GLASS_CLEANER"),
    "AD": ("Anthony Davis", "LAL", "GLASS_CLEANER"),
    "STEPHEN CURRY": ("Stephen Curry", "GSW", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "STEPH CURRY": ("Stephen Curry", "GSW", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "CURRY": ("Stephen Curry", "GSW", "MOVEMENT_OFF_SCREEN_SHOOTER"),
    "NIKOLA JOKIC": ("Nikola Jokic", "DEN", "HELIOCENTRIC_PNR_HANDLER"),
    "JOKIC": ("Nikola Jokic", "DEN", "HELIOCENTRIC_PNR_HANDLER"),
    "VICTOR WEMBANYAMA": ("Victor Wembanyama", "SAS", "GLASS_CLEANER"),
    "WEMBY": ("Victor Wembanyama", "SAS", "GLASS_CLEANER"),
    "GIANNIS": ("Giannis Antetokounmpo", "MIL", "VOLUME_SCORER"),
    "GIANNIS ANTETOKOUNMPO": ("Giannis Antetokounmpo", "MIL", "VOLUME_SCORER"),
    "PATRICK MAHOMES": ("Patrick Mahomes", "KC", "DUAL_THREAT_QB"),
    "MAHOMES": ("Patrick Mahomes", "KC", "DUAL_THREAT_QB"),
    "RASHEE RICE": ("Rashee Rice", "KC", "ALPHA_TARGET_WR"),
    "RICE": ("Rashee Rice", "KC", "ALPHA_TARGET_WR"),
}


def parse_prop_command(raw_text: str, default_sport: str = "NBA") -> Dict[str, Any]:
    text = raw_text.strip()
    if ":" in text:
        left, right = text.split(":", 1)
        if left.upper() in ("NBA", "NFL", "MLB", "NHL", "TENNIS", "WNBA", "ESPORTS"):
            default_sport = left.upper()
            text = right.strip()

    tokens = text.split()
    if not tokens:
        return {
            "sport": default_sport.upper(),
            "player": "LeBron James",
            "team": "LAL",
            "stat": "PTS",
            "line": 25.5,
            "side": "OVER",
            "archetype": "HELIOCENTRIC_PNR_HANDLER",
        }

    cmd = tokens[0].lstrip("/").upper()
    sport = default_sport.upper()
    idx = 0
    if cmd in ("NBA", "NFL", "MLB", "NHL", "TENNIS", "WNBA", "ESPORTS"):
        sport = cmd
        idx = 1
    elif len(tokens) > 1 and tokens[0].lstrip("/").upper() in ("SIM", "PROPS", "PROP", "CHECK", "BET") and tokens[1].upper() in ("NBA", "NFL", "MLB", "NHL", "TENNIS", "WNBA", "ESPORTS"):
        sport = tokens[1].upper()
        idx = 2
    elif cmd in ("SIM", "PROPS", "PROP"):
        idx = 1

    remaining = tokens[idx:]
    if not remaining:
        return {
            "sport": sport,
            "player": "LeBron James",
            "team": "LAL",
            "stat": "PTS",
            "line": 25.5,
            "side": "OVER",
            "archetype": "HELIOCENTRIC_PNR_HANDLER",
        }

    # Side check at start
    side = "OVER"
    if remaining[0].upper() in ("OVER", "UNDER", "O", "U"):
        side = "UNDER" if remaining[0].upper() in ("UNDER", "U") else "OVER"
        remaining.pop(0)

    # Line check at end (e.g. 3.5 or 21.5)
    line = None
    if remaining and re.match(r"^\d+(\.\d+)?$", remaining[-1]):
        line = float(remaining.pop(-1))

    # Stat check at end
    stat = "PTS"
    if remaining:
        last_tok = remaining[-1].upper()
        if last_tok in STAT_ALIASES:
            stat = STAT_ALIASES[remaining.pop(-1).upper()]
        elif any(last_tok.endswith(s) for s in ("3PTS", "3PT", "3PM", "PTS", "REB", "AST")):
            for s in ("3PTS", "3PT", "3PM", "PTS", "REB", "AST"):
                if last_tok.endswith(s):
                    stat = STAT_ALIASES.get(s, "PTS")
                    pfx = last_tok[:-len(s)]
                    if pfx.isdigit() and line is None:
                        line = float(pfx)
                    remaining.pop(-1)
                    break

    # If line was right before the stat token
    if remaining and re.match(r"^[OU]?\d+(\.\d+)?$", remaining[-1].upper()):
        tok = remaining.pop(-1).upper()
        if tok.startswith("U"):
            side = "UNDER"
            tok = tok[1:]
        elif tok.startswith("O"):
            side = "OVER"
            tok = tok[1:]
        try:
            if line is None:
                line = float(tok)
        except Exception:
            pass

    # Side check at end
    if remaining and remaining[-1].upper() in ("OVER", "UNDER", "O", "U"):
        side = "UNDER" if remaining[-1].upper() in ("UNDER", "U") else "OVER"
        remaining.pop(-1)

    raw_player = " ".join(remaining).strip().upper()

    # Fuzzy match player name
    if raw_player in PLAYER_CANONICAL_MAP:
        canonical_name, team, archetype = PLAYER_CANONICAL_MAP[raw_player]
    else:
        best_key, best_ratio = None, 0.0
        for k in PLAYER_CANONICAL_MAP:
            r = SequenceMatcher(None, raw_player, k).ratio()
            if r > best_ratio:
                best_key, best_ratio = k, r
        if best_key and best_ratio >= 0.78:
            canonical_name, team, archetype = PLAYER_CANONICAL_MAP[best_key]
        else:
            canonical_name = raw_player.title()
            team = "UNK"
            archetype = "MOVEMENT_OFF_SCREEN_SHOOTER" if stat in ("3PM", "3PT", "3PTS") else "VOLUME_SCORER"

    if line is None:
        if stat in ("3PM", "3PT", "3PTS"):
            line = 3.5 if canonical_name in ("Tyler Herro", "Stephen Curry") else 2.5
        elif stat in ("REB", "REBOUNDS"):
            line = 10.5 if archetype == "GLASS_CLEANER" else 5.5
        elif stat in ("AST", "ASSISTS"):
            line = 8.5 if "HELIOCENTRIC" in archetype else 4.5
        elif stat == "PRA":
            line = 34.5
        else:
            line = 21.5

    return {
        "sport": sport,
        "player": canonical_name,
        "team": team,
        "stat": stat,
        "line": line,
        "side": side,
        "archetype": archetype,
    }

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    HAS_CRYPTOGRAPHY = True
except Exception:
    HAS_CRYPTOGRAPHY = False

# Sport + Stat -> Kalshi Series Prefix Mapping
KALSHI_SERIES_PREFIXES: Dict[Tuple[str, str], List[str]] = {
    ("NBA", "PTS"): ["KXNBAPTS", "NBA"],
    ("NBA", "POINTS"): ["KXNBAPTS", "NBA"],
    ("NBA", "REB"): ["KXNBAREB", "NBA"],
    ("NBA", "REBOUNDS"): ["KXNBAREB", "NBA"],
    ("NBA", "AST"): ["KXNBAAST", "NBA"],
    ("NBA", "ASSISTS"): ["KXNBAAST", "NBA"],
    ("NBA", "3PT"): ["KXNBA3PT", "KXNBA3PM", "NBA"],
    ("NBA", "3PM"): ["KXNBA3PT", "KXNBA3PM", "NBA"],
    ("NBA", "3PT MADE"): ["KXNBA3PT", "KXNBA3PM", "NBA"],
    ("NBA", "PRA"): ["KXNBAPRA", "NBA"],
    ("NBA", "BLK"): ["KXNBABLK", "NBA"],
    ("NBA", "BLOCKS"): ["KXNBABLK", "NBA"],
    ("NBA", "STL"): ["KXNBASTL", "NBA"],
    ("NBA", "STEALS"): ["KXNBASTL", "NBA"],
    ("NBA", "POINTS + REBOUNDS"): ["KXNBAPR", "KXNBAPRA", "NBA"],
    ("NBA", "POINTS + ASSISTS"): ["KXNBAPA", "KXNBAPRA", "NBA"],
    ("NBA", "REBOUNDS + ASSISTS"): ["KXNBARA", "KXNBAPRA", "NBA"],
    ("WNBA", "PTS"): ["KXWNBAPTS", "WNBA"],
    ("WNBA", "AST"): ["KXWNBAAST", "WNBA"],
    ("WNBA", "REB"): ["KXWNBAREB", "WNBA"],
    ("WNBA", "3PT"): ["KXWNBA3PT", "WNBA"],
    ("NFL", "PASS_YDS"): ["KXNFLPASSYDS", "KXNFLPASS", "NFL"],
    ("NFL", "PASS YDS"): ["KXNFLPASSYDS", "KXNFLPASS", "NFL"],
    ("NFL", "RUSH_YDS"): ["KXNFLRUSHYDS", "KXNFLRUSH", "NFL"],
    ("NFL", "RUSH YDS"): ["KXNFLRUSHYDS", "KXNFLRUSH", "NFL"],
    ("NFL", "REC_YDS"): ["KXNFLRECYDS", "KXNFLREC", "NFL"],
    ("NFL", "REC YDS"): ["KXNFLRECYDS", "KXNFLREC", "NFL"],
    ("NFL", "TD"): ["KXNFLTD", "NFL"],
    ("NFL", "TOUCHDOWNS"): ["KXNFLTD", "NFL"],
    ("MLB", "K"): ["KXMLBKS", "KXMLBK", "MLB"],
    ("MLB", "STRIKEOUTS"): ["KXMLBKS", "KXMLBK", "MLB"],
    ("MLB", "HITS"): ["KXMLBHITS", "MLB"],
    ("MLB", "HR"): ["KXMLBHR", "MLB"],
    ("MLB", "HOME RUNS"): ["KXMLBHR", "MLB"],
    ("TENNIS", "SETS"): ["KXTENNIS", "KXATPMATCH", "KXWTAMATCH", "TENNIS"],
    ("TENNIS", "GAMES"): ["KXTENNIS", "KXATPMATCH", "KXWTAMATCH", "TENNIS"],
    ("ESPORTS", "KILLS"): ["KXESPORTS", "ESPORTS"],
    ("ESPORTS", "MAP KILLS"): ["KXESPORTS", "ESPORTS"],
    ("NHL", "SOG"): ["KXNHLSOG", "NHL"],
    ("NHL", "GOALS"): ["KXNHLGOALS", "NHL"],
    ("NHL", "SAVES"): ["KXNHLSAVES", "NHL"],
}

CURATED_FALLBACK_MARKETS: List[Dict[str, Any]] = [
    {"ticker": "KXNBA-MIA-HERRO-O3P53PT", "title": "Tyler Herro Over 3.5 3PT Made", "category": "NBA", "floor_strike": 3.5},
    {"ticker": "KXNBA-MIA-HERRO-O22P5PTS", "title": "Tyler Herro Over 22.5 Points", "category": "NBA", "floor_strike": 22.5},
    {"ticker": "KXNBA-MIN-RANDLE-O23P5PTS", "title": "Julius Randle Over 23.5 Points", "category": "NBA", "floor_strike": 23.5},
    {"ticker": "KXNBA-MIN-EDWARDS-O26P5PTS", "title": "Anthony Edwards Over 26.5 Points", "category": "NBA", "floor_strike": 26.5},
    {"ticker": "KXNBA-LAL-LBJ-O25P5PTS", "title": "LeBron James Over 25.5 Points", "category": "NBA", "floor_strike": 25.5},
    {"ticker": "KXNBA-GSW-CURRY-O4P53PT", "title": "Stephen Curry Over 4.5 3PT Made", "category": "NBA", "floor_strike": 4.5},
    {"ticker": "KXNFL-KC-MAHOMES-O274P5PASS", "title": "Patrick Mahomes Over 274.5 Pass Yds", "category": "NFL", "floor_strike": 274.5},
    {"ticker": "KXNFL-KC-RICE-O68P5REC", "title": "Rashee Rice Over 68.5 Rec Yds", "category": "NFL", "floor_strike": 68.5},
]


class KalshiSmartPlacer:
    def __init__(
        self,
        key_id: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        base_url: str = "https://api.elections.kalshi.com",
        auth_header_fn: Optional[Any] = None,
    ):
        self.key_id = key_id or os.getenv("KALSHI_KEY_ID") or os.getenv("KALSHI_API_KEY") or ""
        self.base_url = base_url.rstrip("/")
        self.auth_header_fn = auth_header_fn
        self.private_key = None
        self.market_cache: List[Dict[str, Any]] = []
        self.last_cache_time: float = 0.0

        if private_key_pem:
            self._load_key(private_key_pem)
        elif not self.auth_header_fn:
            # Attempt loading from default key locations
            for p in (
                Path("/data/data/com.termux/files/home/peezy_bot/kalshi_private.pem"),
                Path.home() / "peezy_bot" / "kalshi_private.pem",
                Path(__file__).resolve().parent / "kalshi_private.pem",
            ):
                if p.exists():
                    try:
                        self._load_key(p.read_text(encoding="utf-8"))
                        break
                    except Exception:
                        pass

    def _load_key(self, pem_text: str):
        if not HAS_CRYPTOGRAPHY or not pem_text:
            return
        clean_pem = pem_text.strip()
        if not clean_pem.startswith("-----BEGIN"):
            clean_b64 = "".join(ln.strip() for ln in clean_pem.splitlines() if ln.strip() and not ln.strip().startswith("-----"))
            clean_pem = f"-----BEGIN PRIVATE KEY-----\n{clean_b64}\n-----END PRIVATE KEY-----"
        try:
            self.private_key = serialization.load_pem_private_key(clean_pem.encode("utf-8"), password=None)
        except Exception as e:
            print(f"[SMART-PLACER-WARN] Could not load private key via cryptography: {e}")

    def sign_headers(self, method: str, path: str) -> Dict[str, str]:
        if self.auth_header_fn:
            try:
                return self.auth_header_fn(method, path)
            except Exception:
                pass

        ts = str(int(time.time() * 1000))
        clean_path = path.split("?")[0]
        msg = f"{ts}{method.upper()}{clean_path}".encode("utf-8")

        if self.private_key is not None and HAS_CRYPTOGRAPHY:
            try:
                if isinstance(self.private_key, Ed25519PrivateKey):
                    sig = self.private_key.sign(msg)
                else:
                    sig = self.private_key.sign(
                        msg,
                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                        hashes.SHA256(),
                    )
                return {
                    "KALSHI-ACCESS-KEY": self.key_id,
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                    "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            except Exception:
                pass

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # -----------------------------------------------------------------
    # 1. HIGH-EFFICIENCY EVENT SWEEP & SERIES RESOLVER
    # -----------------------------------------------------------------
    def refresh_active_sports_markets(self, sport: str, stat: str = "PTS", force: bool = False) -> List[Dict[str, Any]]:
        if not force and self.market_cache and (time.time() - self.last_cache_time < 30):
            return self.market_cache

        sp_u = sport.upper()
        collected: List[Dict[str, Any]] = []
        seen_tickers = set()

        SPORT_KEYWORDS = {
            "NBA": [
                "NBA", "BASKETBALL", "KNICKS", "MAGIC", "PACERS", "NETS", "CLIPPERS",
                "THUNDER", "RAPTORS", "TIMBERWOLVES", "PISTONS", "76ERS", "GRIZZLIES",
                "KINGS", "LAKERS", "CELTICS", "WARRIORS", "SUNS", "MAVERICKS", "HEAT",
                "RANDLE", "EDWARDS", "BRUNSON", "MAXEY", "CUNNINGHAM", "SGA", "MORANT",
                "JOKIC", "GIANNIS", "DONCIC", "WEMBY", "WEMBANYAMA", "CURRY", "HERRO"
            ],
            "NFL": [
                "NFL", "FOOTBALL", "CHIEFS", "RAVENS", "BILLS", "LIONS", "PACKERS",
                "49ERS", "COWBOYS", "EAGLES", "BENGALS", "TEXANS", "COMMANDERS",
                "BEARS", "VIKINGS", "FALCONS", "BUCCANEERS", "SEAHAWKS", "CARDINALS",
                "TOUCHDOWN", "RUSHING", "PASSING", "RECEIVING", "MAHOMES", "BARKLEY", "HENRY", "ALLEN"
            ],
            "TENNIS": ["TENNIS", "ATP", "WTA", "ALCARAZ", "SINNER", "DJOKOVIC", "FRITZ", "SETS", "GAMES"],
            "MLB": ["MLB", "BASEBALL", "STRIKEOUTS", "HITS", "HOME RUN", "HR", "RUNS"],
        }
        keywords = SPORT_KEYWORDS.get(sp_u, [sp_u])

        # 1. Sweep open events with nested markets (all active live props in 1-2 calls)
        cursor = None
        for _ in range(6):
            path = "/trade-api/v2/events?status=open&limit=200&with_nested_markets=true"
            if cursor:
                path += f"&cursor={cursor}"
            try:
                resp = requests.get(self.base_url + path, headers=self.sign_headers("GET", path), timeout=8)
                if resp.status_code != 200:
                    break
                data = resp.json()
                events = data.get("events", [])
                for ev in events:
                    ev_blob = f"{ev.get('event_ticker', '')} {ev.get('series_ticker', '')} {ev.get('title', '')} {ev.get('sub_title', '')} {ev.get('category', '')}".upper()
                    ev_match = any(k in ev_blob for k in keywords)

                    for m in ev.get("markets", []):
                        if m.get("status") not in ("active", "open"):
                            continue
                        m_blob = f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')}".upper()
                        if ev_match or any(k in m_blob for k in keywords):
                            tk = m.get("ticker")
                            if tk and tk not in seen_tickers:
                                seen_tickers.add(tk)
                                collected.append(m)

                cursor = data.get("cursor")
                if not cursor or not events:
                    break
            except Exception:
                break

        # 2. Targeted series sweep fallback if event sweep returned sparse markets
        if len(collected) < 10:
            st_u = stat.upper()
            prefixes = KALSHI_SERIES_PREFIXES.get((sp_u, st_u)) or KALSHI_SERIES_PREFIXES.get((sp_u, st_u.split()[0])) or [f"KX{sp_u}"]
            for prefix in prefixes:
                try:
                    path = f"/trade-api/v2/markets?status=open&limit=100&series_ticker={prefix}"
                    resp = requests.get(self.base_url + path, headers=self.sign_headers("GET", path), timeout=4)
                    if resp.status_code == 200:
                        for m in resp.json().get("markets", []):
                            if m.get("status") in ("active", "open"):
                                tk = m.get("ticker", "")
                                if tk and tk not in seen_tickers:
                                    seen_tickers.add(tk)
                                    collected.append(m)
                except Exception:
                    continue

        if collected:
            self.market_cache = collected
            self.last_cache_time = time.time()
        return self.market_cache or collected

    # -----------------------------------------------------------------
    # 2. FUZZY PLAYER PROP TICKER RESOLVER (Maps "J Randle PTS 21.5" -> Real Ticker)
    # -----------------------------------------------------------------
    def resolve_prop_ticker(self, sport: str, player_name: str, stat: str, target_line: float) -> Optional[Dict[str, Any]]:
        markets = self.refresh_active_sports_markets(sport, stat) or []
        candidates = list(markets) + [c for c in CURATED_FALLBACK_MARKETS if c.get("category", "").upper() == sport.upper()]
        if not candidates:
            candidates = CURATED_FALLBACK_MARKETS

        # Clean stat words out of player_name so 3pts/pts/reb never pollute last name!
        clean_words = [w for w in player_name.upper().replace("-", " ").replace(".", " ").split() if w not in STAT_ALIASES and w not in ("OVER", "UNDER", "O", "U", "PTS", "REB", "AST", "PRA", "3PT", "3PTS", "3PM")]
        last_name = clean_words[-1] if clean_words else player_name.upper().split()[-1]

        best_match = None
        smallest_line_diff = 999.0
        lookup_names = [last_name]
        if last_name in ("HERO", "HERRO"):
            lookup_names = ["HERRO", "HERO"]
        elif last_name in ("ANT", "EDWARDS"):
            lookup_names = ["EDWARDS", "ANT"]
        elif last_name in ("LBJ", "LEBRON", "JAMES"):
            lookup_names = ["LEBRON", "LBJ", "JAMES"]
        elif last_name in ("AD", "DAVIS"):
            lookup_names = ["DAVIS", "AD"]
        elif last_name in ("WEMBY", "WEMBANYAMA"):
            lookup_names = ["WEMBANYAMA", "WEMBY"]

        for m in candidates:
            searchable = f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')}".upper()
            words_in_market = re.findall(r"[A-Z]+", searchable)
            exact_hit = any(n in searchable for n in lookup_names)
            fuzzy_hit = any(SequenceMatcher(None, n, w).ratio() >= 0.80 for n in lookup_names for w in words_in_market)
            if not exact_hit and not fuzzy_hit:
                continue

            # Extract threshold number if present in floor_strike, strike_price, cap_strike, or ticker
            strike = m.get("floor_strike") or m.get("strike_price") or m.get("cap_strike")
            if strike is not None:
                try:
                    diff = abs(float(strike) - float(target_line))
                    if diff < smallest_line_diff:
                        smallest_line_diff = diff
                        best_match = m
                        if diff <= 0.5:
                            return best_match
                except Exception:
                    pass
            elif not best_match:
                best_match = m

        return best_match

    # -----------------------------------------------------------------
    # 3. SMART ORDERBOOK IMBALANCE (OBI) & LIMIT PRICING ENGINE
    # -----------------------------------------------------------------
    def calculate_smart_entry_price(self, ticker: str, side: str, true_prob_pct: float) -> Tuple[int, str, str]:
        """
        Inspects live Kalshi orderbook depth and Orderbook Imbalance (OBI).
        Returns: (limit_price_cents, execution_style, pricing_note)
        """
        path = f"/trade-api/v2/markets/{ticker}/orderbook"
        fair_cents = max(2, min(98, int(round(true_prob_pct))))

        ob: Dict[str, Any] = {}
        try:
            resp = requests.get(self.base_url + path, headers=self.sign_headers("GET", path), timeout=4)
            if resp.status_code == 200:
                ob = resp.json().get("orderbook", {})
        except Exception:
            pass

        yes_bids = ob.get("yes", []) or []  # [[price, qty], ...]
        no_bids = ob.get("no", []) or []

        is_yes = side.lower() in ("yes", "over", "o")
        if is_yes:
            best_bid = yes_bids[-1][0] if yes_bids else max(1, fair_cents - 8)
            best_ask = (100 - no_bids[-1][0]) if no_bids else min(99, fair_cents - 3)
            bid_vol = sum(q for _, q in yes_bids[-3:]) if yes_bids else 10
            ask_vol = sum(q for _, q in no_bids[-3:]) if no_bids else 10
        else:
            best_bid = no_bids[-1][0] if no_bids else max(1, fair_cents - 8)
            best_ask = (100 - yes_bids[-1][0]) if yes_bids else min(99, fair_cents - 3)
            bid_vol = sum(q for _, q in no_bids[-3:]) if no_bids else 10
            ask_vol = sum(q for _, q in yes_bids[-3:]) if yes_bids else 10

        best_bid = max(1, min(98, best_bid))
        best_ask = max(best_bid + 1, min(99, best_ask))
        spread = best_ask - best_bid
        obi = (bid_vol - ask_vol) / max(1, (bid_vol + ask_vol))  # -1.0 to +1.0

        # Never pay more than (Fair Value - 2c minimum edge buffer)
        max_acceptable_price = max(1, fair_cents - 2)

        # Case A: Tight spread (<= 3c) or strong upward buy pressure (OBI > 0.35) -> Snipe Ask immediately
        if (spread <= 3 or obi > 0.35) and best_ask <= max_acceptable_price:
            return best_ask, "SNIPE_TAKER", f"Tight {spread}c spread (OBI {obi:+.2f}) -> Crossing Ask at {best_ask}c"

        # Case B: Wide spread (> 3c) -> Post Smart Maker Limit 1c above Best Bid to capture spread + 0 maker fee
        smart_maker_price = min(max_acceptable_price, best_bid + 1)
        return smart_maker_price, "SMART_MAKER_LIMIT", f"Wide {spread}c spread (OBI {obi:+.2f}) -> Pegging Maker Limit at {smart_maker_price}c (Fair: {fair_cents}c)"

    # -----------------------------------------------------------------
    # 4. FRACTIONAL KELLY SIZING & LIVE KALSHI ORDER EXECUTION
    # -----------------------------------------------------------------
    def smart_place_leg(
        self,
        sport: str,
        player_name: str,
        stat: str,
        line: float,
        pick_dir: str,       # "OVER" or "UNDER"
        true_prob_pct: float,
        ev_edge_pct: float,
        bankroll_usd: float,
        sniper_on: bool = True,
        live_mode: bool = True,
        kelly_fraction: float = 0.25,
        min_ev_gate: float = 2.5,
        existing_ticker: Optional[str] = None,
    ) -> Dict[str, Any]:
        if ev_edge_pct < min_ev_gate:
            return {"status": "SKIPPED", "reason": f"EV {ev_edge_pct:.2f}% below {min_ev_gate}% Smart Gate"}

        ticker = existing_ticker
        market = None
        is_real_kalshi = bool(ticker and ticker.upper().startswith("KX") and len(ticker) >= 10)
        if not is_real_kalshi:
            market = self.resolve_prop_ticker(sport, player_name, stat, line)
            ticker = market.get("ticker") if market else ticker

        if not ticker or not ticker.upper().startswith("KX"):
            return {
                "status": "NO_TICKER",
                "reason": f"Could not resolve active Kalshi contract for {player_name} {stat} {line}",
                "player": player_name,
                "stat": stat,
                "line": line,
            }

        kalshi_side = "yes" if pick_dir.upper() in ("OVER", "YES", "O") else "no"
        limit_cents, exec_style, pricing_note = self.calculate_smart_entry_price(ticker, kalshi_side, true_prob_pct)

        # Fractional Kelly Contract Sizing based on actual entry price
        p = max(0.05, min(0.95, true_prob_pct / 100.0))
        q = 1.0 - p
        b = (100.0 - limit_cents) / float(max(1, limit_cents))  # Net profit odds per cent risked
        raw_kelly = max(0.0, (b * p - q) / b) if b > 0 else 0.0
        eff_bankroll = bankroll_usd if bankroll_usd > 0 else 12.52
        stake_usd = max(1.00, min(eff_bankroll * 0.40, round(eff_bankroll * max(0.08, raw_kelly) * kelly_fraction, 2)))
        contracts = max(1, min(10, int((stake_usd * 100) // max(1, limit_cents))))
        cost_usd = round((contracts * limit_cents) / 100.0, 2)

        # Check Live Slate Schedule Gate for NBA
        sched_alert = None
        if sport.upper() == "NBA":
            player_clean = player_name.upper().strip()
            team_detected = None
            for k, (c_name, t, _arch) in PLAYER_CANONICAL_MAP.items():
                if k == player_clean or SequenceMatcher(None, player_clean, k).ratio() >= 0.80 or k in player_clean:
                    team_detected = t
                    break
            if team_detected and team_detected in TEAM_SCHEDULE_NOTES and team_detected not in TODAY_ACTIVE_NBA_TEAMS:
                sched_alert = TEAM_SCHEDULE_NOTES[team_detected]

        if sched_alert:
            return {
                "status": "SIM_QUEUED",
                "ticker": ticker,
                "side": kalshi_side.upper(),
                "contracts": contracts,
                "limit_cents": limit_cents,
                "cost_usd": cost_usd,
                "style": "OFF_SLATE_PAPER",
                "sched_alert": sched_alert,
                "note": f"⚠️ {sched_alert} — live Kalshi order suppressed, paper simulated.",
            }

        if not (sniper_on and live_mode):
            return {
                "status": "SIM_QUEUED",
                "ticker": ticker,
                "side": kalshi_side.upper(),
                "contracts": contracts,
                "limit_cents": limit_cents,
                "cost_usd": cost_usd,
                "style": exec_style,
                "note": f"Simulated paper execution ({pricing_note})",
            }

        # Execute Real Kalshi V2 Limit Order
        client_oid = str(uuid.uuid4())
        is_yes = kalshi_side.lower() in ("yes", "over", "o")
        v2_side = "bid" if is_yes else "ask"
        price_dollars = (limit_cents / 100.0) if is_yes else ((100 - limit_cents) / 100.0)
        price_str = f"{price_dollars:.4f}"

        attempts = [
            (
                "/trade-api/v2/portfolio/events/orders",
                {
                    "ticker": ticker,
                    "side": v2_side,
                    "type": "limit",
                    "count": str(max(1, int(contracts))),
                    "price": price_str,
                    "time_in_force": "good_till_canceled",
                    "client_order_id": client_oid,
                    "self_trade_prevention_type": "taker_at_cross",
                },
            ),
            (
                "/trade-api/v2/portfolio/orders",
                {
                    "ticker": ticker,
                    "action": "buy",
                    "side": "yes" if is_yes else "no",
                    "count": max(1, int(contracts)),
                    "type": "limit",
                    "client_order_id": client_oid,
                    "yes_price": limit_cents if is_yes else (100 - limit_cents),
                },
            ),
        ]

        last_resp_err = ""
        for path, payload in attempts:
            try:
                resp = requests.post(
                    self.base_url + path,
                    json=payload,
                    headers=self.sign_headers("POST", path),
                    timeout=8,
                )
                if resp.status_code in (200, 201):
                    order_data = resp.json().get("order") or resp.json().get("event_order") or {}
                    real_oid = order_data.get("order_id") or client_oid
                    return {
                        "status": "PLACED",
                        "order_id": real_oid,
                        "ticker": ticker,
                        "side": kalshi_side.upper(),
                        "contracts": contracts,
                        "limit_cents": limit_cents,
                        "cost_usd": cost_usd,
                        "style": exec_style,
                        "note": pricing_note,
                    }
                err_msg = resp.text[:180]
                last_resp_err = err_msg
                if resp.status_code == 410 or "deprecated" in err_msg.lower():
                    continue
                if "insufficient_balance" in err_msg.lower():
                    return {"status": "BALANCE_HELD", "code": resp.status_code, "reason": "Resting orders holding cash", "limit_cents": limit_cents, "contracts": contracts, "ticker": ticker}
                if "market_not_found" in err_msg.lower() or "not found" in err_msg.lower():
                    sim_id = f"SIM-QUEUED-{uuid.uuid4().hex[:6].upper()}"
                    return {
                        "status": "SIM_QUEUED",
                        "ticker": ticker,
                        "side": kalshi_side.upper(),
                        "contracts": contracts,
                        "limit_cents": limit_cents,
                        "cost_usd": cost_usd,
                        "style": exec_style,
                        "order_id": sim_id,
                        "note": f"Paper tracked ({pricing_note} — market not open on exchange)",
                    }
                return {"status": "API_REJECTED", "code": resp.status_code, "reason": err_msg, "limit_cents": limit_cents, "contracts": contracts, "ticker": ticker}
            except Exception as e:
                last_resp_err = str(e)
                continue

        return {"status": "API_REJECTED", "code": 410, "reason": last_resp_err or "Order endpoint error", "limit_cents": limit_cents, "contracts": contracts, "ticker": ticker}

    # -----------------------------------------------------------------
    # 5. SMART COMBO BASKET EXECUTOR (Places Correlated Copula Legs Together)
    # -----------------------------------------------------------------
    def smart_place_copula_combo(
        self,
        legs: List[Dict[str, Any]],
        copula_boost_pct: float,
        bankroll_usd: float,
        sniper_on: bool = True,
        live_mode: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Places a correlated 2-to-3 leg Copula Combo as a synchronized Smart Basket,
        scaling Kelly fraction up proportionally to the positive Copula covariance boost.
        """
        # Boost Kelly fraction slightly when legs have strong positive Gaussian Copula synergy
        synergy_kelly = min(0.35, 0.20 + (max(0.0, copula_boost_pct) / 100.0))
        per_leg_bankroll = bankroll_usd / max(1, len(legs))
        receipts = []
        for leg in legs:
            res = self.smart_place_leg(
                sport=leg.get("sport", "NBA"),
                player_name=leg.get("player_name") or leg.get("player", ""),
                stat=leg.get("stat_type") or leg.get("stat", "Points"),
                line=float(leg.get("line", 21.5)),
                pick_dir=leg.get("side", "OVER"),
                true_prob_pct=float(leg.get("mc_true_prob", leg.get("true_prob", 0.55)) * 100.0 if float(leg.get("mc_true_prob", 0.55)) <= 1.0 else float(leg.get("mc_true_prob", 55.0))),
                ev_edge_pct=float(leg.get("ev_edge", leg.get("ev_pct", 3.5)) * 100.0 if float(leg.get("ev_edge", 0.035)) <= 1.0 else float(leg.get("ev_edge", 3.5))),
                bankroll_usd=per_leg_bankroll,
                sniper_on=sniper_on,
                live_mode=live_mode,
                kelly_fraction=synergy_kelly,
                min_ev_gate=2.0,
                existing_ticker=leg.get("market_ticker") or leg.get("ticker"),
            )
            receipts.append(res)
        return receipts

    # -----------------------------------------------------------------
    # 6. UNIVERSAL MONEYLINE & SPREAD EXECUTOR
    # -----------------------------------------------------------------
    def smart_place_moneyline_or_spread(
        self,
        sport: str,
        team_name: str,
        bet_type: str = "ML",   # "ML" (Moneyline) or "SPREAD"
        line_value: float = 0.0,
        true_prob_pct: float = 55.0,
        ev_edge_pct: float = 3.5,
        bankroll_usd: float = 32.22,
        sniper_on: bool = True,
        live_mode: bool = True,
    ) -> Dict[str, Any]:
        """
        Resolves and places Kalshi Game Moneylines and Spreads.
        """
        if ev_edge_pct < 2.5:
            return {"status": "SKIPPED", "reason": "Edge below game-line threshold"}

        markets = self.refresh_active_sports_markets(sport, "PTS")
        team_upper = team_name.upper()

        best_market = None
        for m in markets:
            searchable = f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')}".upper()
            if team_upper in searchable and ("WIN" in searchable or "SPREAD" in searchable or "VS" in searchable or "GAME" in searchable):
                best_market = m
                break

        if not best_market:
            return {"status": "NO_TICKER", "reason": f"Could not find open Kalshi game line for {team_name}"}

        ticker = best_market["ticker"]
        kalshi_side = "yes"  # Kalshi structures team moneylines as YES on that team winning

        limit_cents, exec_style, pricing_note = self.calculate_smart_entry_price(ticker, kalshi_side, true_prob_pct)
        stake_usd = max(1.00, round(bankroll_usd * 0.03, 2))  # 3% standard bankroll slice for game lines
        contracts = max(1, int((stake_usd * 100) // max(1, limit_cents)))

        if not (sniper_on and live_mode):
            return {
                "status": "SIM_QUEUED",
                "ticker": ticker,
                "contracts": contracts,
                "limit_cents": limit_cents,
                "cost_usd": round((contracts * limit_cents) / 100.0, 2),
                "style": exec_style,
                "note": f"Paper tracked ({pricing_note})",
            }

        order_path = "/trade-api/v2/portfolio/orders"
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": kalshi_side,
            "count": contracts,
            "type": "limit",
            "client_order_id": str(uuid.uuid4()),
            f"{kalshi_side}_price": limit_cents,
        }

        try:
            resp = requests.post(
                self.base_url + order_path,
                json=payload,
                headers=self.sign_headers("POST", order_path),
                timeout=8,
            )
            if resp.status_code in (200, 201):
                order_id = resp.json().get("order", {}).get("order_id", "")
                return {
                    "status": "PLACED",
                    "ticker": ticker,
                    "contracts": contracts,
                    "limit_cents": limit_cents,
                    "cost_usd": round((contracts * limit_cents) / 100.0, 2),
                    "order_id": order_id,
                }
            return {"status": "API_REJECTED", "code": resp.status_code, "reason": resp.text[:140]}
        except Exception as e:
            return {"status": "ERROR", "reason": str(e)}

    # -----------------------------------------------------------------
    # 7. SMART COMBO BASKET ROUTER (Multi-Leg Concurrent Sizing)
    # -----------------------------------------------------------------
    def smart_place_combo_basket(
        self,
        slip_id: str,
        legs: List[Any],
        total_stake_usd: float,
        sniper_on: bool = True,
        live_mode: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Smart Basket Router: Kalshi has no native multi-leg parlay endpoint.
        This function splits total Kelly bankroll across the correlated legs
        and concurrently submits synchronized limit/taker orders for all legs.
        """
        results = []
        if not legs:
            return results

        per_leg_stake = max(1.00, round(total_stake_usd / max(1, len(legs)), 2))
        for leg in legs:
            tk = getattr(leg, "market_ticker", None) or getattr(leg, "ticker", "")
            sport = getattr(leg, "sport", "NBA")
            player = getattr(leg, "player_name", "") or getattr(leg, "player_prop", "").split()[0]
            stat = getattr(leg, "stat_type", "PTS")
            line = float(getattr(leg, "line", 0.0))
            side = getattr(leg, "side", "OVER")
            true_prob = float(getattr(leg, "mc_true_prob", 0.50)) * 100.0
            ev_pct = float(getattr(leg, "edge_pct", getattr(leg, "ev_edge", 0.05) * 100.0))

            res = self.smart_place_leg(
                sport=sport,
                player_name=player,
                stat=stat,
                line=line,
                pick_dir=side,
                true_prob_pct=true_prob,
                ev_edge_pct=ev_pct,
                bankroll_usd=per_leg_stake * 3,
                sniper_on=sniper_on,
                live_mode=live_mode,
                existing_ticker=tk if (tk and tk.startswith("KX") and len(tk) >= 10) else None,
            )
            results.append({"ticker": tk, "result": res})
        return results
