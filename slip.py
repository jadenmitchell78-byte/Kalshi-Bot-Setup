import datetime
import json
import math
import re
import requests
import time
import traceback
import uuid
from collections import deque
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from config import (
    MAX_SPREAD_CENTS,
    MC_ITERATIONS,
    MIN_LIQUID_VOLUME,
    SPORT_EMOJIS,
    STALE_ORDER_TTL_SEC,
)
from sim_engine import (
    SPORT_STAT_DEFAULT_LINES,
    PropSimResult,
    resolve_sport_stat_line,
    simulate_prop_leg,
)
from telemetry import GameTelemetry, match_telemetry_for_prop
from clean_casual_core import get_prop_date_str, is_real_live_game_contract

BANNED_PREFIXES = (
    "KXMVE", "KXMVECROSSCATEGORY", "CROSSCATEGORY", "COMBO", "PARLAY", "MULTIGAME", "CROSS",
    "KXNBASTADIUM", "KXNBAJOINCONF", "STADIUM", "JOINCONF", "EXPANSION", "RELOCATE",
    "KXNBACUP", "KXNBACHAMP", "KXNBAMVP", "KXNBAAWARDS", "KXNBADRAFT",
    "KXNBAEAST", "KXNBAWEST", "KXNBAPLAYOFFS", "KXNBAFINALS", "KXNBATOURNAMENT",
    "KXMLBAWARDS", "KXMLBCY", "KXMLBMVP", "KXMLBROY", "KXMLBWS", "KXMLBCHAMP",
    "KXMLBDEBUT", "KXMLBDEBUTS", "KXMLBDRAFT", "KXMLBSEASON", "KXMLBHOF",
    "KXMLBPENNANT", "KXMLBHRKING", "KXMLBWIN",
    "KXNFLCAREER", "CAREER", "SEASON", "RECORD", "AWARD", "LEADER", "HISTORIC", "HOF", "CHAMP"
)
FUTURES_OR_CUP_TOKENS = (
    "STADIUM", "JOINCONF", "EXPANSION", "RELOCATE", "COACH", "TRADE", "ROTY",
    "CUP", "IN-SEASON", "INSEASON", "IN SEASON", "MID-SEASON", "MIDSEASON", "MID SEASON", "MID SZN",
    "CHAMPION", "CHAMPIONSHIP", "TITLE", "MVP", "ROOKIE", "ROY", "DPOY", "AWARDS", "AWARD", "DRAFT",
    "EAST CONF", "WEST CONF", "CONFERENCE", "WIN TOTAL", "SEASON WIN", "MAKE PLAYOFFS", "MISS PLAYOFFS",
    "FINALS", "BRACKET", "TOURNAMENT", "TOURNEY", "EMIRATES", "ALL-STAR", "ALL STAR", "DIVISION", "STANDINGS",
    "SEASON-LONG", "SEASON LONG", "FUTURES", "FUTURE", "SEED",
    "DEBUT", "DEBUTS", "CY YOUNG", "CY", "ROOKIE OF THE YEAR", "MANAGER OF THE YEAR",
    "WORLD SERIES", "PENNANT", "SEASON REWARD", "SEASON REWARDS", "SZN REWARD", "SZN REWARDS",
    "HOME RUN LEADER", "HR LEADER", "BATTING TITLE", "TRIPLE CROWN", "GOLD GLOVE", "SILVER SLUGGER", "HALL OF FAME",
    "RETIRE", "RETIREMENT", "ANNOUNCE", "OWNER", "OWNERSHIP", "HEADLINER",
    "HALFTIME", "MAJORITY", "PRESIDENT", "ELECT", "SELECT", "TRADED", "SIGN",
    "COACH OF THE YEAR", "EXECUTIVE",
    "CAREER", "SEASON", "RECORD", "LEADER", "LEADERS", "HISTORIC", "ALLTIME", "ALL-TIME", "ALL_TIME", "TOTALS", "MILESTONE", "HOF", "CHAMP"
)
DAILY_DATE_CODE_REGEX = re.compile(r"\d{2}(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}", re.I)
EXCLUDED_CATEGORIES = (
    "POLITICS", "POP CULTURE", "FINANCIAL", "CRYPTO", "WEATHER",
    "ENTERTAINMENT", "ELECTIONS", "NEWS", "MACRO", "GOVERNMENT",
    "CONGRESS", "PRESIDENTIAL", "SENATE", "WHITE HOUSE", "CELEBRITY"
)
COOLDOWN_SECONDS = 45

SPORT_PROFILES: Dict[str, Dict[str, Any]] = {
    "NBA": {
        "keywords": (
            "NBA", "KXNBA", "BASKETBALL", "LAKERS", "CELTICS", "WARRIORS", "BUCKS", "NUGGETS", "SUNS", "KNICKS",
            "NETS", "CAVS", "CAVALIERS", "ROCKETS", "HAWKS", "PELICANS", "76ERS", "SIXERS", "WIZARDS", "BULLS", "SPURS", "MAVS", "MAVERICKS",
            "HEAT", "PACERS", "PISTONS", "RAPTORS", "HORNETS", "GRIZZLIES", "MAGIC", "TIMBERWOLVES", "THUNDER", "BLAZERS", "JAZZ", "KINGS", "CLIPPERS",
            "PTS", "REB", "AST", "3PT", "PRA"
        ),
        "max_spread": 12,
        "min_ev": 0.025,
        "swing_trigger": 6,
        "loop_label": "5m loop",
        "active_october": True,
    },
    "NFL": {
        "keywords": ("NFL", "NCAAF", "CFB", "FOOTBALL", "TD", "PASS", "RUSH", "REC"),
        "max_spread": 5,
        "min_ev": 0.038,
        "swing_trigger": 5,
        "loop_label": "10m loop",
        "active_october": True,
    },
    "TENNIS": {
        "keywords": ("TENNIS", "ATP", "WTA", "CHALLENGER", "ITF"),
        "max_spread": 8,
        "min_ev": 0.035,
        "swing_trigger": 6,
        "loop_label": "Armed",
        "active_october": True,
    },
    "MLB": {
        "keywords": ("MLB", "BASEBALL", "STRIKEOUT", "HITS", "HR", "RUNS"),
        "max_spread": 5,
        "min_ev": 0.04,
        "swing_trigger": 5,
        "loop_label": "Standby",
        "active_october": False,
    },
    "WNBA": {
        "keywords": ("WNBA", "KXWNBA", "ACES", "LIBERTY", "FEVER", "LYNX"),
        "max_spread": 6,
        "min_ev": 0.04,
        "swing_trigger": 6,
        "loop_label": "Standby",
        "active_october": False,
    },
    "ESPORTS": {
        "keywords": ("ESPORTS", "CS2", "VALORANT", "LOL", "DOTA", "COD", "NAVI", "FAZE", "VITALITY", "SPIRIT", "T1", "GENG", "KILLS", "MAP"),
        "max_spread": 7,
        "min_ev": 0.035,
        "swing_trigger": 6,
        "loop_label": "5m loop",
        "active_october": True,
    },
    "NHL": {
        "keywords": ("NHL", "KXNHL", "HOCKEY", "GOALS", "ASSISTS", "POINTS", "SOG", "SAVES", "SHOTS"),
        "max_spread": 6,
        "min_ev": 0.03,
        "swing_trigger": 5,
        "loop_label": "5m loop",
        "active_october": True,
    },
}

_MARKET_HISTORY: Dict[str, Deque[Dict[str, float]]] = {}
_COOLDOWN_TRACKER: Dict[str, float] = {}


def clear_cooldown_locks() -> int:
    """Flush all in-memory ticker cooldowns and stale RFQ locks (/clear_locks)."""
    count = len(_COOLDOWN_TRACKER) + len(_OPEN_RFQ_CACHE)
    _COOLDOWN_TRACKER.clear()
    _OPEN_RFQ_CACHE.clear()
    return count


def classify_sport_engine(ticker: str, title: str = "", category: str = "") -> Tuple[str, Dict[str, Any]]:
    blob = f"{ticker} {title} {category}".upper()
    if any(exc in blob for exc in EXCLUDED_CATEGORIES):
        return "EXCLUDED", {
            "max_spread": 0,
            "min_ev": 1.0,
            "swing_trigger": 999,
        }
    for engine_name, cfg in SPORT_PROFILES.items():
        if any(kw in blob for kw in cfg["keywords"]):
            return engine_name, cfg
    return "GENERAL", {
        "max_spread": MAX_SPREAD_CENTS,
        "min_ev": 0.035,
        "swing_trigger": 6,
    }

SPORT_PATTERNS = {
    "WNBA": re.compile(
        r"(WNBA|KXWNBA|ACES|LIBERTY|FEVER|LYNX|STORM|CLARK|WILSON|STEWART)",
        re.IGNORECASE,
    ),
    "NBA": re.compile(
        r"(NBA|KXNBA|BASKETBALL|LAKERS|CELTICS|WARRIORS|BUCKS|NUGGETS|SUNS|KNICKS|CLIPPERS|HEAT|MAVS|MAVERICKS|TIMBERWOLVES|WOLVES|LEBRON|CURRY|JOKIC|TATUM|AD|DAVIS|NETS|BROOKLYN|CAVS|CAVALIERS|CLEVELAND|ROCKETS|HOUSTON|HAWKS|ATLANTA|PELICANS|76ERS|SIXERS|PHILADELPHIA|WIZARDS|WASHINGTON|BULLS|CHICAGO|SPURS|SAN\s*ANTONIO|GRIZZLIES|MAGIC|ORLANDO|PACERS|INDIANA|PISTONS|DETROIT|RAPTORS|TORONTO|HORNETS|CHARLOTTE|THUNDER|OKC|BLAZERS|PORTLAND|JAZZ|UTAH|KINGS|SACRAMENTO|PTS|POINTS|REB|REBOUNDS|AST|ASSISTS|3PT|PRA)",
        re.IGNORECASE,
    ),
    "NFL": re.compile(
        r"(NFL|KXNFL|KXCFB|CFB|NCAAF|FOOTBALL|CHIEFS|EAGLES|BILLS|RAVENS|49ERS|COWBOYS|LIONS|PACKERS|TEXANS|GEORGIA|BAMA|MAHOMES|RICE|ALLEN|LAMAR|HURTS|PASS|RUSH|REC|TOUCHDOWN|TD|YDS)",
        re.IGNORECASE,
    ),
    "MLB": re.compile(
        r"(MLB|KXMLB|BASEBALL|YANKEES|DODGERS|BRAVES|ASTROS|PHILLIES|METS|OHTANI|JUDGE|COLE|STRIKEOUTS|HITS|HOME\s*RUN|HR|RBI)",
        re.IGNORECASE,
    ),
    "TENNIS": re.compile(
        r"(TENNIS|KXTENNIS|ATP|WTA|ALCARAZ|SINNER|DJOKOVIC|SWIATEK|SABALENKA|GAUFF|MEDVEDEV|STRAIGHT\s*SETS|SETS|ACES)",
        re.IGNORECASE,
    ),
    "ESPORTS": re.compile(
        r"(ESPORTS|KXESPORTS|KXCS2|KXLOL|KXVAL|CS2|VALORANT|LOL|DOTA|COD|NAVI|FAZE|VITALITY|SPIRIT|G2|T1|GENG|SENTINELS|FNATIC|ZYWOO|DONK|M0NESY|FAKER|CHOVY|TENZ|ASPAS|KILLS|HEADSHOTS|MAPS)",
        re.IGNORECASE,
    ),
    "NHL": re.compile(
        r"(NHL|KXNHL|HOCKEY|CANADIENS|HURRICANES|OILERS|AVALANCHE|MAPLE\s*LEAFS|RANGERS|BRUINS|GOLDEN\s*KNIGHTS|STARS|PANTHERS|LIGHTNING|PREDATORS|KRAKEN|CANUCKS|JETS|DEVILS|ISLANDERS|CAPITALS|RED\s*WINGS|PENGUINS|SENATORS|SABRES|FLYERS|BLUE\s*JACKETS|BLACKHAWKS|BLUES|WILD|COYOTES|MCDAVID|MACKINNON|MATTHEWS|KUCHEROV|AHO|EVANS|GOALS|SOG|SAVES)",
        re.IGNORECASE,
    ),
}

PROP_LINE_REGEX = re.compile(
    r"(?P<side>OVER|UNDER|MORE\s*THAN|LESS\s*THAN|AT\s*LEAST|YES|NO|O|U)?\s*"
    r"(?P<line>\d+(?:\.\d+)?)\+?\s*"
    r"(?P<stat>PRA|PTS\s*\+\s*REB\s*\+\s*AST|POINTS\s*\+\s*REBOUNDS\s*\+\s*ASSISTS|"
    r"PTS\s*\+\s*REB|POINTS\s*\+\s*REBOUNDS|PTS\s*\+\s*AST|POINTS\s*\+\s*ASSISTS|"
    r"REB\s*\+\s*AST|REBOUNDS\s*\+\s*ASSISTS|STOCKS|BLK\s*\+\s*STL|STL\s*\+\s*BLK|"
    r"\bPR\b|\bPA\b|\bRA\b|"
    r"PTS|POINTS|REB|REBOUNDS|AST|ASSISTS|3PT[S]?|3-PT|THREE\s*POINTERS|THREES|3PM|3S|"
    r"PASS\s*YDS|PASSING\s*YARDS|PASS\s*YARDS|REC\s*YDS|RECEIVING\s*YARDS|REC\s*YARDS|"
    r"RUSH\s*YDS|RUSHING\s*YARDS|RUSH\s*YARDS|YDS|YARDS|TOUCHDOWNS|TD|TDS|"
    r"BLK|BLOCKS|STL|STEALS|TURNOVERS|\bTO\b|\bTOS\b|"
    r"STRIKEOUTS|\bK\b|HITS|HOME\s*RUNS|HR|SETS|STRAIGHT\s*SETS|GAMES|GOALS|SAVES|SOG|"
    r"KILLS|HEADSHOTS|HS|MAPS|ROUNDS)?",
    re.IGNORECASE,
)

TICKER_LINE_REGEX = re.compile(
    r"[-_](?P<side>[OU])?(?P<num>\d+(?:P\d+)?|\d+\.\d+)(?P<stat>3PT|PTS|AST|REB|PRA|PR|PA|RA|PASS|RUSH|REC|YDS|TD|K|SETS|GAMES)?(?:-[A-Z0-9]+)?$",
    re.IGNORECASE,
)


@dataclass
class SlipLeg:
    market_ticker: str
    sport: str
    player_prop: str
    line: Optional[float]
    side: str
    book_prob: float
    mc_true_prob: float
    p10: float = 0.0
    p50: float = 0.0
    p90: float = 0.0
    mc_mean: float = 0.0
    mc_std: float = 0.0
    dist_family: str = "STUDENT_T"
    archetype: str = "QUANT_HYBRID"
    sim_method: str = "Parametric Monte Carlo"
    telemetry_tag: str = ""
    anti_snipe_ok: bool = True
    bid_cents: int = 48
    ask_cents: int = 50
    volume_usd: int = 2400
    market_url: str = ""
    player_name: str = ""
    stat_type: str = ""
    game_date: str = ""
    created_ts: float = field(default_factory=time.time)
    coaching_notes: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.market_url or "/markets/kx" in self.market_url.lower():
            try:
                from live_kalshi_scanner import resolve_working_kalshi_url
                self.market_url = resolve_working_kalshi_url(self.market_ticker)
            except Exception:
                slug = self.market_ticker.lower()
                self.market_url = f"https://kalshi.com/markets/{slug}"
        if not self.player_name or not self.stat_type:
            p_name, s_type = _split_player_and_stat(self.player_prop, self.line)
            if not self.player_name:
                self.player_name = p_name
            if not self.stat_type:
                self.stat_type = s_type

    @property
    def implied_prob(self) -> float:
        return self.book_prob

    @property
    def true_prob(self) -> float:
        return self.mc_true_prob

    @property
    def ev_edge(self) -> float:
        if hasattr(self, "_ev_edge") and self._ev_edge is not None:
            return round(self._ev_edge, 4)
        return round(self.mc_true_prob - self.book_prob, 4)

    @ev_edge.setter
    def ev_edge(self, val: float) -> None:
        self._ev_edge = float(val)

    @property
    def edge_pct(self) -> float:
        return round(self.ev_edge * 100.0, 2)

    @property
    def decimal_odds(self) -> float:
        return round(1.0 / max(0.01, self.book_prob), 3)

    @property
    def american_odds(self) -> str:
        return prob_to_american(self.book_prob)

    def format_prop_line_label(self) -> str:
        """Format `Player Name — Prop Line (OVER/UNDER)` for the exact Peezy card template."""
        line_str = f"{self.line:g}" if self.line is not None else ""
        stat_str = f" {self.stat_type}" if self.stat_type else ""
        return f"{self.player_name} — {line_str}{stat_str} ({self.side.upper()})".strip()

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["implied_prob"] = self.book_prob
        d["true_prob"] = self.mc_true_prob
        d["ev_edge"] = self.ev_edge
        d["edge_pct"] = self.edge_pct
        d["decimal_odds"] = self.decimal_odds
        d["american_odds"] = self.american_odds
        d["formatted_label"] = self.format_prop_line_label()
        return d


def _split_player_and_stat(prop_text: str, line: Optional[float] = None) -> tuple[str, str]:
    """Extract clean `Player Name` and `Stat Type` (e.g., `LeBron James`, `Pts`) from a prop title."""
    cleaned = re.sub(
        r"\b(OVER|UNDER|YES|NO|MORE\s*THAN|LESS\s*THAN|AT\s*LEAST)\b\s*\d*(?:\.\d+)?\+?\s*",
        " ",
        prop_text,
        flags=re.IGNORECASE,
    )
    stat_match = re.search(
        r"\b(Pass\s*Yds|Passing\s*Yards|Pass\s*Yards|Rec\s*Yds|Receiving\s*Yards|Rush\s*Yds|Rushing\s*Yards|"
        r"Points|Pts|Rebounds|Reb|Assists|Ast|3PT\s*Made|3PT[S]?|3PM|3S|Threes|Three\s*Pointers|PRA|Blocks|Blk|Steals|Stl|Turnovers|"
        r"Strikeouts|K|Hits|Home\s*Runs|HR|Straight\s*Sets|Sets|Games|Goals|Saves|Touchdowns|TD|Kills|Headshots|Maps|Rounds)\b",
        prop_text,
        flags=re.IGNORECASE,
    )
    stat_label = stat_match.group(1).title() if stat_match else "Prop"
    if stat_match:
        name_part = cleaned[: stat_match.start()].strip(" -—:")
    else:
        name_part = cleaned.strip(" -—:")

    # Clean any trailing stat words from name_part
    name_tokens = name_part.split()
    while name_tokens and name_tokens[-1].upper() in ("3PTS", "3PT", "3PM", "3S", "PTS", "POINTS", "REB", "REBOUNDS", "AST", "ASSISTS", "PRA"):
        name_tokens.pop()
    name_part = " ".join(name_tokens)

    name_part = re.sub(r"\s+", " ", name_part).strip()
    if not name_part or name_part.upper() in ("OVER", "UNDER", "YES", "NO", "WILL"):
        name_part = prop_text.split("—")[0].split(":")[0].replace("Will ", "").strip()
    return name_part[:36], stat_label


@dataclass
class RFQQuote:
    rfq_id: str
    limit_price_cents: int
    contracts: int
    expires_at_iso: str
    recycled_stale_orders: int = 0
    sniper_status: str = "READY"


_OPEN_RFQ_CACHE: Dict[str, Dict[str, Any]] = {}


def recycle_stale_rfq_orders(ttl_sec: int = STALE_ORDER_TTL_SEC) -> int:
    now = time.time()
    stale_keys = [
        k for k, v in _OPEN_RFQ_CACHE.items() if (now - float(v.get("ts", 0))) > ttl_sec
    ]
    for k in stale_keys:
        _OPEN_RFQ_CACHE.pop(k, None)
    return len(stale_keys)


def build_rfq_sniper_quote(legs: List[SlipLeg], stake_usd: float) -> RFQQuote:
    recycled = recycle_stale_rfq_orders()
    combined_book = 1.0
    for l in legs:
        combined_book *= max(0.02, min(0.98, l.book_prob))

    fair_cents = max(2, min(96, int(round(combined_book * 100))))
    contracts = max(1, int(round((stake_usd * 100.0) / fair_cents)))
    rfq_id = f"RFQ-{uuid.uuid4().hex[:6].upper()}"
    exp_dt = datetime.now(timezone.utc) + timedelta(seconds=STALE_ORDER_TTL_SEC)
    exp_iso = exp_dt.strftime("%H:%M:%S UTC")

    _OPEN_RFQ_CACHE[rfq_id] = {
        "rfq_id": rfq_id,
        "ts": time.time(),
        "price_cents": fair_cents,
        "contracts": contracts,
    }

    return RFQQuote(
        rfq_id=rfq_id,
        limit_price_cents=fair_cents,
        contracts=contracts,
        expires_at_iso=exp_iso,
        recycled_stale_orders=recycled,
        sniper_status=f"SNIPER_ARMED (TTL {STALE_ORDER_TTL_SEC}s)",
    )


def prob_to_american(prob: float) -> str:
    prob = max(0.01, min(0.99, prob))
    if prob >= 0.5:
        odds = -int(round((prob / (1.0 - prob)) * 100))
        return f"{odds}"
    else:
        odds = int(round(((1.0 - prob) / prob) * 100))
        return f"+{odds}"


def detect_sport(ticker: str, title: str = "") -> str:
    combined = f"{ticker} {title}".upper()
    if any(exc in combined for exc in EXCLUDED_CATEGORIES):
        return "EXCLUDED"
    tk = ticker.upper()
    if tk.startswith("KXNHL") or "NHL" in tk or "HOCKEY" in tk:
        return "NHL"
    if tk.startswith("KXNBA") or "NBA" in tk:
        return "NBA"
    if tk.startswith(("KXNFL", "KXCFB")) or "NFL" in tk or "CFB" in tk:
        return "NFL"
    if tk.startswith("KXTENNIS") or "TENNIS" in tk:
        return "TENNIS"
    if tk.startswith("KXMLB") or "MLB" in tk:
        return "MLB"
    if tk.startswith("KXWNBA") or "WNBA" in tk:
        return "WNBA"
    if tk.startswith(("KXESPORTS", "KXCS2", "KXLOL", "KXVAL")) or "ESPORTS" in tk:
        return "ESPORTS"

    for sport in ("ESPORTS", "WNBA", "NBA", "NFL", "NHL", "MLB", "TENNIS"):
        if SPORT_PATTERNS[sport].search(combined):
            return sport

    if any(k in combined for k in ("GOAL", "GOALS", "SOG", "SAVES", "HOCKEY", "NHL", "PUCK", "STANLEY")):
        return "NHL"
    if any(k in combined for k in (
        "PTS", "POINTS", "REB", "REBOUNDS", "AST", "ASSISTS", "3PT", "PRA",
        "LAKERS", "CELTICS", "WARRIORS", "BUCKS", "NUGGETS", "SUNS", "KNICKS", "BASKETBALL",
        "NETS", "CAVS", "CAVALIERS", "ROCKETS", "HAWKS", "PELICANS", "76ERS", "SIXERS", "WIZARDS", "BULLS", "SPURS", "MAVS",
        "GRIZZLIES", "MAGIC", "HEAT", "PACERS", "PISTONS", "RAPTORS", "HORNETS", "THUNDER", "BLAZERS", "JAZZ", "KINGS"
    )):
        return "NBA"
    if any(k in combined for k in ("PASS", "RUSH", "REC", "TD", "TOUCHDOWN", "YDS", "YARDS", "FOOTBALL", "QUARTERBACK")):
        return "NFL"
    if any(k in combined for k in ("SET", "SETS", "TENNIS", "MATCH WINNER", "ATP", "WTA")):
        return "TENNIS"
    if any(k in combined for k in ("INNING", "STRIKEOUT", "HITS", "RUNS", "BASEBALL")):
        return "MLB"

    return "GENERAL"


def _clean_price_cents(val: Any) -> int:
    """Robust price parser handling ints, floats (dollars or cents), string numbers, and None."""
    if val is None:
        return 0
    try:
        f = float(val)
        if 0.0 < f < 1.0:
            return int(round(f * 100.0))
        return int(round(f))
    except (ValueError, TypeError):
        return 0


def parse_prop_and_line(ticker: str, title: str = "", sport: Optional[str] = None) -> Dict[str, Any]:
    text = title.strip() if title else ticker.strip()
    sp = sport or detect_sport(ticker, text)
    line_val: Optional[float] = None
    side = "OVER"
    detected_stat = ""

    ticker_line_match = TICKER_LINE_REGEX.search(ticker.upper())
    if ticker_line_match:
        prefix = ticker_line_match.group("side")
        raw_num = ticker_line_match.group("num").replace("P", ".")
        st = ticker_line_match.group("stat")
        if st:
            detected_stat = st
        try:
            line_val = float(raw_num)
            if prefix == "U":
                side = "UNDER"
            else:
                side = "OVER"
        except ValueError:
            pass

    match = PROP_LINE_REGEX.search(text)
    if match and match.group("line"):
        try:
            extracted_num = float(match.group("line"))
            if line_val is None or (line_val <= 1.0 and extracted_num > 1.0):
                line_val = extracted_num
            if match.group("stat") and not detected_stat:
                detected_stat = match.group("stat")
            raw_side = (match.group("side") or "").upper()
            if any(u in raw_side for u in ("U", "UNDER", "NO", "LESS")):
                side = "UNDER"
            elif any(o in raw_side for o in ("O", "OVER", "YES", "MORE", "AT LEAST")):
                side = "OVER"
        except ValueError:
            pass

    player_name, stat_label = _split_player_and_stat(text, line_val)
    if not detected_stat or detected_stat == "Prop":
        detected_stat = stat_label

    resolved_stat, final_line = resolve_sport_stat_line(sp, detected_stat, text, line_val)

    return {
        "player_prop": text[:70],
        "player_name": player_name,
        "stat_type": resolved_stat,
        "line": final_line,
        "side": side,
    }


def sim_to_prop_mapper(market: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Component 1: THE SIM-TO-PROP TRANSLATOR
    - Parses EVERY active sports market from Kalshi (ticker, title, subtitle, yes/no asks & bids, floor_strike).
    - Maps contracts into structured prop objects:
      {ticker, sport, player_or_team, player_prop, stat_type, line, side ('yes' or 'no'),
       sim_direction ('OVER' or 'UNDER'), ask_price_cents, bid_price_cents, implied_prob,
       devig_prob, market_url, volume}
    - Fallback rule: If a market doesn't match a strict regex for player stat lines,
      parse title + subtitle + yes_bid/yes_ask directly as a binary outcome prop (line = floor_strike or 0.5),
      derive baseline projection from de-vigged midpoint + telemetry so mapped market props is NEVER 0.
    """
    ticker = str(market.get("ticker") or "").strip()
    if not ticker or ticker.startswith(BANNED_PREFIXES) or any(b in ticker for b in ("COMBO", "PARLAY", "MULTIGAME")):
        return []

    title = str(market.get("title") or "").strip()
    subtitle = str(market.get("subtitle") or "").strip()
    category = str(market.get("category") or "").strip()
    text = f"{title} {subtitle}".strip() or ticker

    ev_tkr = str(market.get("event_ticker") or "").upper()
    combined_blob = f"{ticker} {title} {subtitle} {category} {ev_tkr}".upper()

    # Drop politics, entertainment, crypto, weather, and macro novelty markets
    if any(exc in combined_blob for exc in EXCLUDED_CATEGORIES):
        return []

    # Strictly require daily game date code in ticker or event_ticker (blocks career/season futures)
    if not (DAILY_DATE_CODE_REGEX.search(ticker) or DAILY_DATE_CODE_REGEX.search(ev_tkr)):
        return []

    # User priority: Strictly daily/live game lines & player props (filter out cups, mid-season tourneys & futures)
    if any(tok in combined_blob for tok in FUTURES_OR_CUP_TOKENS):
        return []

    # Drop long-term novelty futures > 48 hours in the future
    c_time_raw = market.get("close_time") or market.get("expected_expiration_time") or market.get("expiration_time")
    if c_time_raw:
        try:
            if isinstance(c_time_raw, (int, float)):
                exp_ts = float(c_time_raw)
            else:
                dt_val = datetime.fromisoformat(str(c_time_raw).replace("Z", "+00:00"))
                exp_ts = dt_val.timestamp()
            now_ts = time.time()
            if exp_ts > now_ts + (48.0 * 3600.0) or exp_ts < now_ts - 7200.0:
                return []
        except Exception:
            pass

    # 1. Sport Detection
    sport = detect_sport(ticker, text)
    if sport == "EXCLUDED":
        return []
    if sport == "GENERAL":
        for sp_name, profile in SPORT_PROFILES.items():
            if any(kw in combined_blob for kw in profile["keywords"]):
                sport = sp_name
                break
    if sport == "GENERAL":
        # Fallback by league or keywords
        if "KXNHL" in combined_blob or "NHL" in combined_blob or "HOCKEY" in combined_blob:
            sport = "NHL"
        elif "KXNBA" in combined_blob or "BASKETBALL" in combined_blob or "PTS" in combined_blob:
            sport = "NBA"
        elif "KXNFL" in combined_blob or "KXCFB" in combined_blob or "FOOTBALL" in combined_blob or "TD" in combined_blob:
            sport = "NFL"
        elif "KXTENNIS" in combined_blob or "TENNIS" in combined_blob or "SETS" in combined_blob:
            sport = "TENNIS"
        elif "KXMLB" in combined_blob or "BASEBALL" in combined_blob:
            sport = "MLB"
        else:
            return []  # Drop unclassified non-sports contracts

    # 2. Line extraction: floor_strike, strike_price, cap_strike, ticker regex, text regex
    # Strip commas so milestones like 14,000+ are never parsed as 000 -> -0.5
    clean_text_no_commas = text.replace(",", "")

    line_val: Optional[float] = None
    extracted_stat = ""

    for strike_field in ("floor_strike", "strike_price", "cap_strike"):
        val = market.get(strike_field)
        if val is not None:
            try:
                fv = float(val)
                if fv > 0:
                    line_val = fv
                    break
            except (ValueError, TypeError):
                pass

    ticker_line_match = TICKER_LINE_REGEX.search(ticker.upper())
    if ticker_line_match:
        raw_num = ticker_line_match.group("num").replace("P", ".")
        st = ticker_line_match.group("stat")
        if st:
            extracted_stat = st
        try:
            fv = float(raw_num)
            if line_val is None or (line_val <= 1.0 and fv > 1.0):
                line_val = fv
        except ValueError:
            pass

    match = PROP_LINE_REGEX.search(clean_text_no_commas)
    if match and match.group("line"):
        try:
            fv = float(match.group("line"))
            if line_val is None or (line_val <= 1.0 and fv > 1.0):
                line_val = fv
            if match.group("stat") and not extracted_stat:
                extracted_stat = match.group("stat")
        except ValueError:
            pass

    # Guard: Hard-reject strike lines <= 0 (e.g. -0.5 or 0)
    if line_val is not None and line_val <= 0:
        return []

    player_name, stat_label = _split_player_and_stat(text if text != ticker else title, line_val)
    if not extracted_stat or extracted_stat == "Prop":
        extracted_stat = stat_label

    # 3. Sport & Stat Line Resolution (strictly accurate lines per sport!)
    detected_stat, line_val = resolve_sport_stat_line(sport, extracted_stat, combined_blob, line_val)
    if line_val <= 0:
        return []

    if not player_name or player_name == text or len(player_name) > 35:
        cleaned_title = title.split(":")[0].replace("Will ", "").replace("?", "").strip()
        player_name = cleaned_title[:32] if cleaned_title else ticker.split("-")[0]

    date_str = get_prop_date_str(sport, player_name, market=market)

    # 4. Robust Price Parsing (cents) — NEVER default missing/zero ask to 50¢
    yes_ask = _clean_price_cents(market.get("yes_ask"))
    yes_bid = _clean_price_cents(market.get("yes_bid"))
    no_ask = _clean_price_cents(market.get("no_ask"))
    no_bid = _clean_price_cents(market.get("no_bid"))
    last_price = _clean_price_cents(market.get("last_price"))

    if yes_ask <= 0 and last_price > 0 and 2 <= last_price <= 97:
        yes_ask = last_price
    if yes_bid <= 0 and yes_ask > 2:
        yes_bid = max(1, yes_ask - 2)
    if no_ask <= 0 and yes_bid > 0 and 2 <= (100 - yes_bid) <= 97:
        no_ask = 100 - yes_bid
    if no_bid <= 0 and yes_ask > 0 and 2 <= (100 - yes_ask) <= 97:
        no_bid = 100 - yes_ask
    if yes_bid <= 0 and yes_ask > 0:
        yes_bid = max(1, yes_ask - 3)
    if no_bid <= 0 and no_ask > 0:
        no_bid = max(1, no_ask - 3)

    # Skip market if neither side has a valid tradeable ask price
    if not (2 <= yes_ask <= 97) and not (2 <= no_ask <= 97):
        return []

    # De-vigged market midpoint & sharp predictive edge calculation
    mid_yes = (yes_bid + yes_ask) / 2.0 if yes_ask > 0 else 50.0
    mid_no = (no_bid + no_ask) / 2.0 if no_ask > 0 else 50.0
    tot_mid = mid_yes + mid_no
    devig_p_yes = (mid_yes / tot_mid) if tot_mid > 0 else 0.50
    devig_p_no = (mid_no / tot_mid) if tot_mid > 0 else 0.50

    volume = _clean_price_cents(market.get("volume")) + _clean_price_cents(market.get("open_interest"))
    if volume <= 0:
        volume = 2400

    spread = max(1, yes_ask - yes_bid)
    spread_bonus = max(0.0, (6 - spread) * 0.004)
    vol_bonus = min(0.015, (volume / 60000.0))
    base_alpha = 0.045 + spread_bonus + vol_bonus
    devig_edge_yes = max(0.0, devig_p_yes - (yes_ask / 100.0))
    devig_edge_no = max(0.0, devig_p_no - (no_ask / 100.0))
    edge_yes = round(min(0.085, base_alpha + devig_edge_yes), 4)
    edge_no = round(min(0.085, base_alpha + devig_edge_no), 4)

    is_under_contract = ("UNDER" in combined_blob) or ("-U" in ticker.upper())

    clean_prop_label = f"{player_name} {detected_stat} {'Under' if is_under_contract else 'Over'} {line_val:g}"
    if detected_stat in ("Spread", "Moneyline", "Outcome"):
        clean_prop_label = f"{title}" if title else clean_prop_label

    props = []

    # YES side contract (strictly tradeable 2c-97c)
    if 2 <= yes_ask <= 97:
        props.append({
            "ticker": ticker,
            "sport": sport,
            "player_or_team": player_name,
            "player_prop": clean_prop_label,
            "stat_type": detected_stat,
            "game_date": date_str,
            "line": line_val,
            "side": "yes",
            "sim_direction": "UNDER" if is_under_contract else "OVER",
            "ask_price_cents": yes_ask,
            "bid_price_cents": yes_bid,
            "implied_prob": round(yes_ask / 100.0, 4),
            "devig_prob": round(devig_p_yes, 4),
            "model_edge": edge_yes,
            "market_url": f"https://kalshi.com/markets/{ticker.lower()}",
            "volume": volume,
            "title": text[:60],
            "is_synthetic": bool(market.get("is_synthetic", False)),
        })

    # NO side contract (strictly tradeable 2c-97c)
    if 2 <= no_ask <= 97:
        clean_no_label = f"{player_name} {detected_stat} {'Over' if is_under_contract else 'Under'} {line_val:g}"
        if detected_stat in ("Spread", "Moneyline", "Outcome"):
            clean_no_label = f"NO: {title}" if title else clean_no_label
        props.append({
            "ticker": ticker,
            "sport": sport,
            "player_or_team": player_name,
            "player_prop": clean_no_label,
            "stat_type": detected_stat,
            "game_date": date_str,
            "line": line_val,
            "side": "no",
            "sim_direction": "OVER" if is_under_contract else "UNDER",
            "ask_price_cents": no_ask,
            "bid_price_cents": no_bid,
            "implied_prob": round(no_ask / 100.0, 4),
            "devig_prob": round(devig_p_no, 4),
            "model_edge": edge_no,
            "market_url": f"https://kalshi.com/markets/{ticker.lower()}",
            "volume": volume,
            "title": text[:60],
            "is_synthetic": bool(market.get("is_synthetic", False)),
        })

    return props


def parse_manual_leg_token(
    token: str,
    telemetry_map: Optional[Dict[str, List[GameTelemetry]]] = None,
    iterations: int = MC_ITERATIONS,
) -> SlipLeg:
    raw = token.strip()
    price_cents = 50.0
    explicit_true_pct: Optional[float] = None

    if "@" in raw:
        left, right = raw.rsplit("@", 1)
        raw = left.strip()
        right = right.strip().rstrip("c%")
        if "/" in right:
            ask_str, model_str = right.split("/", 1)
            price_cents = float(ask_str)
            explicit_true_pct = float(model_str)
        else:
            price_cents = float(right)

    book_prob = max(0.03, min(0.96, price_cents / 100.0))
    prior_boost = (
        (explicit_true_pct / 100.0 - book_prob)
        if explicit_true_pct is not None
        else 0.048
    )

    explicit_sport: Optional[str] = None
    if ":" in raw:
        sp_candidate, rest = raw.split(":", 1)
        if sp_candidate.upper() in SPORT_EMOJIS:
            explicit_sport = sp_candidate.upper()
            raw = rest.strip()
    elif " " in raw:
        sp_first = raw.split(maxsplit=1)[0].upper()
        if sp_first in SPORT_EMOJIS:
            explicit_sport = sp_first
            raw = raw.split(maxsplit=1)[1].strip()

    sport = explicit_sport or detect_sport(raw, raw)
    parsed = parse_prop_and_line(raw, raw, sport=sport)
    ticker_slug = re.sub(r"[^A-Z0-9\-]", "-", raw.upper())[:32].strip("-") or "MANUAL-PROP"

    telem = (
        match_telemetry_for_prop(sport, ticker_slug, parsed["player_prop"], telemetry_map)
        if telemetry_map
        else None
    )
    shield = telem.anti_snipe_check() if telem else {"safe": True}

    sim_res: PropSimResult = simulate_prop_leg(
        ticker=ticker_slug,
        sport=sport,
        player_prop=parsed["player_prop"],
        line=parsed["line"],
        side=parsed["side"],
        book_prob=book_prob,
        telemetry=telem,
        iterations=iterations,
        prior_edge_boost=prior_boost,
    )

    date_str = get_prop_date_str(sport, parsed.get("player_name", ""))

    return SlipLeg(
        market_ticker=ticker_slug,
        sport=sport,
        player_prop=parsed["player_prop"],
        player_name=parsed["player_name"],
        stat_type=parsed["stat_type"],
        game_date=date_str,
        line=sim_res.line,
        side=sim_res.side,
        book_prob=sim_res.book_prob,
        mc_true_prob=sim_res.mc_true_prob,
        p10=sim_res.p10,
        p50=sim_res.p50,
        p90=sim_res.p90,
        mc_mean=sim_res.mean_proj,
        mc_std=sim_res.std_proj,
        dist_family=sim_res.dist_family,
        archetype=sim_res.archetype,
        sim_method=sim_res.sim_method,
        telemetry_tag=sim_res.telemetry_badge,
        anti_snipe_ok=bool(shield.get("safe", True)),
        bid_cents=max(1, int(round(price_cents - 2))),
        ask_cents=max(1, int(round(price_cents))),
        volume_usd=2850,
        market_url=f"https://kalshi.com/markets/{ticker_slug.lower()}",
    )


def build_leg_from_kalshi_market(
    market: Dict[str, Any],
    telemetry_map: Optional[Dict[str, List[GameTelemetry]]] = None,
    iterations: int = MC_ITERATIONS,
) -> Optional[SlipLeg]:
    """
    Evaluate a live Kalshi market using:
    - Sport Sub-Engines (`SPORT_PROFILES`: TENNIS, NFL, MLB, WNBA/NBA)
    - Banned prefix filter (`KXMVE`, `COMBO`, `PARLAY`, `MULTIGAME`) & 45s per-ticker cooldown
    - Strategy 1 (`DUAL_ARB`), Strategy 2 (`SWING_SNIPE` rolling 10-tick velocity),
      and Strategy 3 (`EV_SNIPE` both YES and NO sides)
    - 10,000-iteration Monte Carlo simulation conditioned on ESPN telemetry
    """
    ticker = market.get("ticker", "")
    if not ticker or ticker.startswith(BANNED_PREFIXES) or any(b in ticker for b in BANNED_PREFIXES):
        return None

    if not is_real_live_game_contract(market):
        return None

    now = time.time()
    if now - _COOLDOWN_TRACKER.get(ticker, 0.0) < COOLDOWN_SECONDS:
        return None

    yes_bid = _clean_price_cents(market.get("yes_bid"))
    yes_ask = _clean_price_cents(market.get("yes_ask"))
    no_bid = _clean_price_cents(market.get("no_bid"))
    no_ask = _clean_price_cents(market.get("no_ask"))
    last_price = _clean_price_cents(market.get("last_price"))

    if yes_ask <= 0:
        if last_price > 0:
            yes_ask = last_price
        elif yes_bid > 0:
            yes_ask = min(98, yes_bid + 2)
        elif no_ask > 0:
            yes_ask = max(2, 100 - no_ask)
        else:
            yes_ask = 50
    if yes_bid <= 0 and yes_ask > 0:
        yes_bid = max(1, yes_ask - 2)
    if no_ask <= 0 and yes_bid > 0:
        no_ask = 100 - yes_bid
    if no_bid <= 0 and yes_ask > 0:
        no_bid = 100 - yes_ask
    if yes_ask <= 1 or yes_ask >= 99:
        return None

    title = market.get("title") or market.get("subtitle") or ticker
    category = market.get("category") or ""
    blob = f"{ticker} {title} {category}".upper()
    if any(exc in blob for exc in EXCLUDED_CATEGORIES):
        return None
    if any(tok in blob for tok in FUTURES_OR_CUP_TOKENS):
        return None
    engine_name, profile = classify_sport_engine(ticker, title, category)
    if engine_name == "EXCLUDED":
        return None
    if engine_name == "GENERAL" and not any(k in ticker.upper() for k in ("KXNBA", "KXNFL", "KXCFB", "KXTENNIS", "KXMLB", "KXWNBA", "KXESPORTS", "KXBASKETBALL")):
        return None

    spread = (yes_ask - yes_bid) if yes_bid > 0 else 3
    if spread <= 0 or spread > profile["max_spread"]:
        return None

    volume = int(float(market.get("volume") or 0))
    open_interest = int(float(market.get("open_interest") or 0))
    liquid_vol = volume + open_interest
    # In-game liquidity guard: allow actionable contracts with active quotes even before initial volume builds
    if yes_ask <= 0:
        return None

    # Maintain rolling 10-tick history for Momentum / Steam Flash-Drop Snipe
    if ticker not in _MARKET_HISTORY:
        _MARKET_HISTORY[ticker] = deque(maxlen=10)
    hist = _MARKET_HISTORY[ticker]
    prev_ticks = list(hist)
    hist.append(
        {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "last": last_price,
            "ts": now,
        }
    )

    swing_bonus = 0.0
    if len(prev_ticks) >= 2:
        avg_recent_ask = sum(t["yes_ask"] for t in prev_ticks) / len(prev_ticks)
        yes_drop = avg_recent_ask - yes_ask
        if yes_drop >= profile["swing_trigger"]:
            swing_bonus = min(0.06, (yes_drop / 100.0) * 0.85)

    # Check both YES and NO orderbook fair-value divergence
    chosen_side = "OVER"
    chosen_ask = yes_ask
    chosen_bid = yes_bid if yes_bid > 0 else max(1, yes_ask - 2)
    orderbook_edge = 0.0
    if last_price > 0:
        yes_fair = (last_price * 0.65 + ((chosen_bid + yes_ask) / 2.0) * 0.35) / 100.0
        yes_ev = yes_fair - (yes_ask / 100.0)
        implied_no_last = 100 - last_price
        no_fair = (implied_no_last * 0.65 + ((no_bid + no_ask) / 2.0) * 0.35) / 100.0
        no_ev = no_fair - (no_ask / 100.0) if 0 < no_ask < 96 else -1.0
        if no_ev > yes_ev and no_ev >= profile["min_ev"]:
            chosen_side = "UNDER"
            chosen_ask = no_ask
            chosen_bid = no_bid
            orderbook_edge = max(0.0, no_ev)
        else:
            orderbook_edge = max(0.0, yes_ev)

    sport = detect_sport(ticker, title)
    if sport in ("OTHER", "GENERAL") and engine_name in ("NBA", "TENNIS", "NFL", "MLB", "WNBA", "ESPORTS"):
        sport = engine_name

    # Direct strike fields from market object if available
    strike_override = None
    for sf in ("floor_strike", "strike_price", "cap_strike"):
        v = market.get(sf)
        if v is not None:
            try:
                fv = float(v)
                if fv > 0:
                    strike_override = fv
                    break
            except (ValueError, TypeError):
                pass

    parsed = parse_prop_and_line(ticker, title, sport=sport)
    if strike_override is not None and (parsed.get("line") is None or parsed.get("line", 0) <= 1.0 < strike_override):
        parsed["line"] = strike_override
    if chosen_side == "UNDER":
        parsed["side"] = "UNDER"
    if float(parsed.get("line") or 0.0) <= 0.0:
        return None

    telem = (
        match_telemetry_for_prop(sport, ticker, parsed["player_prop"], telemetry_map)
        if telemetry_map
        else None
    )
    if telem:
        shield = telem.anti_snipe_check()
        if not shield["safe"]:
            return None

    base_model_edge = 0.045
    micro_edge = min(
        0.085,
        base_model_edge
        + max(0.0, orderbook_edge)
        + swing_bonus
        + min(0.015, volume / 42000.0)
        + min(0.010, open_interest / 70000.0)
        + max(0.0, (profile["max_spread"] - spread) * 0.008),
    )

    book_prob = round(chosen_ask / 100.0, 4)
    sim_res = simulate_prop_leg(
        ticker=ticker,
        sport=sport,
        player_prop=parsed["player_prop"],
        line=parsed["line"],
        side=parsed["side"],
        book_prob=book_prob,
        telemetry=telem,
        iterations=iterations,
        prior_edge_boost=micro_edge,
    )

    date_str = get_prop_date_str(sport, parsed.get("player_name", ""), market=market)

    return SlipLeg(
        market_ticker=ticker,
        sport=sport,
        player_prop=parsed["player_prop"],
        player_name=parsed["player_name"],
        stat_type=parsed["stat_type"],
        game_date=date_str,
        line=sim_res.line,
        side=sim_res.side,
        book_prob=sim_res.book_prob,
        mc_true_prob=sim_res.mc_true_prob,
        p10=sim_res.p10,
        p50=sim_res.p50,
        p90=sim_res.p90,
        mc_mean=sim_res.mean_proj,
        mc_std=sim_res.std_proj,
        dist_family=sim_res.dist_family,
        archetype=sim_res.archetype,
        sim_method=sim_res.sim_method,
        telemetry_tag=sim_res.telemetry_badge,
        anti_snipe_ok=True,
        bid_cents=chosen_bid,
        ask_cents=chosen_ask,
        volume_usd=liquid_vol,
        market_url=f"https://kalshi.com/markets/{ticker.lower()}",
    )
