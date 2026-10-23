import re
import time
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
from sim_engine import PropSimResult, simulate_prop_leg
from telemetry import GameTelemetry, match_telemetry_for_prop

BANNED_PREFIXES = ("KXMVE", "COMBO", "PARLAY", "MULTIGAME", "CROSS")
COOLDOWN_SECONDS = 45

SPORT_PROFILES: Dict[str, Dict[str, Any]] = {
    "NBA": {
        "keywords": ("NBA", "KXNBA", "BASKETBALL", "LAKERS", "CELTICS", "WARRIORS", "BUCKS", "NUGGETS", "SUNS", "KNICKS", "PTS", "REB", "AST", "3PT"),
        "max_spread": 6,
        "min_ev": 0.035,
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
        r"\b(WNBA|KXWNBA|ACES|LIBERTY|FEVER|LYNX|STORM|CLARK|WILSON|STEWART)\b",
        re.IGNORECASE,
    ),
    "NBA": re.compile(
        r"\b(NBA|KXNBA|LAKERS|CELTICS|WARRIORS|BUCKS|NUGGETS|SUNS|KNICKS|LEBRON|CURRY|JOKIC|TATUM|AD|DAVIS|PTS|REB|AST|3PT)\b",
        re.IGNORECASE,
    ),
    "NFL": re.compile(
        r"\b(NFL|KXNFL|CHIEFS|EAGLES|BILLS|RAVENS|49ERS|COWBOYS|MAHOMES|RICE|ALLEN|LAMAR|PASS\s*YDS|RUSH\s*YDS|REC\s*YDS|TD|TOUCHDOWN)\b",
        re.IGNORECASE,
    ),
    "MLB": re.compile(
        r"\b(MLB|KXMLB|YANKEES|DODGERS|BRAVES|ASTROS|PHILLIES|METS|OHTANI|JUDGE|COLE|STRIKEOUTS|HITS|HOME\s*RUN|HR|RBI)\b",
        re.IGNORECASE,
    ),
    "TENNIS": re.compile(
        r"\b(TENNIS|KXTENNIS|ATP|WTA|ALCARAZ|SINNER|DJOKOVIC|SWIATEK|SABALENKA|GAUFF|STRAIGHT\s*SETS|ACES)\b",
        re.IGNORECASE,
    ),
    "ESPORTS": re.compile(
        r"\b(ESPORTS|KXESPORTS|CS2|VALORANT|LOL|DOTA|COD|NAVI|FAZE|VITALITY|SPIRIT|G2|T1|GENG|SENTINELS|FNATIC|ZYWOO|DONK|M0NESY|FAKER|CHOVY|TENZ|ASPAS|KILLS|HEADSHOTS|MAPS)\b",
        re.IGNORECASE,
    ),
}

PROP_LINE_REGEX = re.compile(
    r"(?P<side>OVER|UNDER|YES|NO|O|U)?\s*(?P<line>\d+(?:\.\d+)?)\+?\s*(?P<stat>PTS|POINTS|REB|REBOUNDS|AST|ASSISTS|PRA|3PT|PASS\s*YDS|REC\s*YDS|RUSH\s*YDS|YDS|YARDS|TD|K|STRIKEOUTS|HITS|HR|SETS|GAMES|KILLS|HEADSHOTS|HS|MAPS|ROUNDS)?",
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
    created_ts: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.market_url:
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
        return round(self.mc_true_prob - self.book_prob, 4)

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


def _split_player_and_stat(prop_text: str, line: Optional[float]) -> tuple[str, str]:
    """Extract clean `Player Name` and `Stat Type` (e.g., `LeBron James`, `Pts`) from a prop title."""
    cleaned = re.sub(
        r"\b(OVER|UNDER|YES|NO)\b\s*\d*(?:\.\d+)?\+?\s*",
        " ",
        prop_text,
        flags=re.IGNORECASE,
    )
    stat_match = re.search(
        r"\b(Pass\s*Yds|Rec\s*Yds|Rush\s*Yds|Points|Pts|Rebounds|Reb|Assists|Ast|Strikeouts|K|Hits|HR|Straight\s*Sets|Sets|Yds|TD|Map\s*\d\s*Kills|Kills|Headshots|HS|Maps|Rounds)\b",
        prop_text,
        flags=re.IGNORECASE,
    )
    stat_label = stat_match.group(1) if stat_match else "Prop"
    if stat_match:
        name_part = cleaned[: stat_match.start()].strip(" -—:")
    else:
        name_part = cleaned.strip(" -—:")
    name_part = re.sub(r"\s+", " ", name_part).strip()
    if not name_part:
        name_part = prop_text[:32].strip()
    return name_part, stat_label


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
    for sport in ("ESPORTS", "WNBA", "NBA", "NFL", "MLB", "TENNIS"):
        if SPORT_PATTERNS[sport].search(combined):
            return sport
    return "OTHER"


def parse_prop_and_line(ticker: str, title: str = "") -> Dict[str, Any]:
    text = title.strip() if title else ticker.strip()
    line_val: Optional[float] = None
    side = "OVER"

    ticker_line_match = re.search(r"-([OU])?(\d+(?:P\d+|\.\d+)?)[A-Z]*$", ticker.upper())
    if ticker_line_match:
        prefix = ticker_line_match.group(1)
        raw_num = ticker_line_match.group(2).replace("P", ".")
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
            if line_val is None:
                line_val = float(match.group("line"))
            raw_side = (match.group("side") or "").upper()
            if raw_side in ("U", "UNDER", "NO"):
                side = "UNDER"
            elif raw_side in ("O", "OVER", "YES"):
                side = "OVER"
        except ValueError:
            pass

    if line_val is None:
        line_val = 22.5

    player_name, stat_type = _split_player_and_stat(text, line_val)
    return {
        "player_prop": text[:70],
        "player_name": player_name,
        "stat_type": stat_type,
        "line": line_val,
        "side": side,
    }


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

    parsed = parse_prop_and_line(raw, raw)
    sport = explicit_sport or detect_sport(raw, parsed["player_prop"])
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

    return SlipLeg(
        market_ticker=ticker_slug,
        sport=sport,
        player_prop=parsed["player_prop"],
        player_name=parsed["player_name"],
        stat_type=parsed["stat_type"],
        line=sim_res.line,
        side=sim_res.side,
        book_prob=sim_res.book_prob,
        mc_true_prob=sim_res.mc_true_prob,
        p10=sim_res.p10,
        p50=sim_res.p50,
        p90=sim_res.p90,
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

    now = time.time()
    if now - _COOLDOWN_TRACKER.get(ticker, 0.0) < COOLDOWN_SECONDS:
        return None

    yes_bid = int(market.get("yes_bid") or 0)
    yes_ask = int(market.get("yes_ask") or 0)
    no_bid = int(market.get("no_bid") or 0)
    no_ask = int(market.get("no_ask") or 0)
    last_price = int(market.get("last_price") or 0)

    if yes_ask <= 0:
        yes_ask = last_price if last_price > 0 else 0
    if yes_ask <= 3 or yes_ask >= 95:
        return None
    if no_ask <= 0 and yes_bid > 0:
        no_ask = 100 - yes_bid
    if no_bid <= 0 and yes_ask > 0:
        no_bid = 100 - yes_ask

    title = market.get("title") or market.get("subtitle") or ticker
    category = market.get("category") or ""
    engine_name, profile = classify_sport_engine(ticker, title, category)

    spread = (yes_ask - yes_bid) if yes_bid > 0 else 3
    if spread <= 0 or spread > profile["max_spread"]:
        return None

    volume = int(float(market.get("volume") or 0))
    open_interest = int(float(market.get("open_interest") or 0))
    liquid_vol = volume + open_interest
    if liquid_vol < min(350, MIN_LIQUID_VOLUME):
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
    if sport == "OTHER" and engine_name in ("NBA", "TENNIS", "NFL", "MLB", "WNBA", "ESPORTS"):
        sport = engine_name
    parsed = parse_prop_and_line(ticker, title)
    if chosen_side == "UNDER":
        parsed["side"] = "UNDER"

    telem = (
        match_telemetry_for_prop(sport, ticker, parsed["player_prop"], telemetry_map)
        if telemetry_map
        else None
    )
    if telem:
        shield = telem.anti_snipe_check()
        if not shield["safe"]:
            return None

    micro_edge = min(
        0.065,
        orderbook_edge
        + swing_bonus
        + (volume / 42000.0)
        + (open_interest / 70000.0)
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

    return SlipLeg(
        market_ticker=ticker,
        sport=sport,
        player_prop=parsed["player_prop"],
        player_name=parsed["player_name"],
        stat_type=parsed["stat_type"],
        line=sim_res.line,
        side=sim_res.side,
        book_prob=sim_res.book_prob,
        mc_true_prob=sim_res.mc_true_prob,
        p10=sim_res.p10,
        p50=sim_res.p50,
        p90=sim_res.p90,
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
