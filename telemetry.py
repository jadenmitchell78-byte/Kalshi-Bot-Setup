import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import aiohttp

from config import (
    ANTI_SNIPE_MAX_LATENCY_MS,
    ESPN_ENDPOINTS,
    TELEMETRY_STALE_SEC,
)

logger = logging.getLogger("peezy.telemetry")


@dataclass
class GameTelemetry:
    event_id: str
    sport: str
    matchup: str
    home_team: str
    away_team: str
    home_score: int
    away_score: int
    period: int
    display_clock: str
    remaining_fraction: float
    pace_multiplier: float
    usage_boost: float
    foul_trouble_flag: bool = False
    injury_spike_flag: bool = False
    suspended_flag: bool = False
    is_live: bool = False
    is_final: bool = False
    latency_ms: float = 0.0
    fetched_at: float = field(default_factory=time.time)
    player_stats: Dict[str, Dict[str, float]] = field(default_factory=dict)

    @property
    def score_diff(self) -> int:
        return abs(self.home_score - self.away_score)

    @property
    def blowout_risk(self) -> float:
        """
        Estimate starter benching / blowout risk in late quarters.
        Regular Season Rule: Blowout benching is ONLY applied if deep in the 4th quarter (Q4)
        with a 20+ point lead. Pre-game, 1Q, 2Q, and 3Q starters play full rotation minutes!
        """
        if self.sport in ("NBA", "WNBA"):
            if self.period >= 4 and self.score_diff >= 20:
                return 0.18
            return 0.0
        if self.sport == "NFL" and self.period >= 4 and self.score_diff >= 24:
            return 0.15
        return 0.0

    def anti_snipe_check(self) -> Dict[str, Any]:
        """
        Evaluate the Anti-Snipe Shield:
        Blocks execution if play is suspended, latency exceeds threshold,
        injury/foul spikes are detected, or feed timestamp is stale.
        For pre-game slates, feed is valid across the pre-game window (2 hours) without expiring.
        """
        age_sec = time.time() - self.fetched_at
        reasons: List[str] = []

        if self.suspended_flag:
            reasons.append("PLAY_SUSPENDED_OR_DELAYED")
        if self.injury_spike_flag:
            reasons.append("INJURY_OR_ROSTER_SPIKE")
        if self.latency_ms > ANTI_SNIPE_MAX_LATENCY_MS:
            reasons.append(f"HIGH_FEED_LATENCY ({self.latency_ms:.0f}ms)")

        # Pre-game games don't update every 90s (game hasn't started); allow full pre-tipoff window
        stale_threshold = 7200.0 if (not self.is_live and not self.is_final) else TELEMETRY_STALE_SEC
        if age_sec > stale_threshold:
            reasons.append(f"STALE_TELEMETRY ({age_sec:.0f}s old)")

        return {
            "safe": len(reasons) == 0,
            "reasons": reasons,
            "latency_ms": round(self.latency_ms, 1),
            "age_sec": round(age_sec, 1),
        }

    def format_badge(self) -> str:
        status_str = (
            f"Q{self.period} {self.display_clock}"
            if self.is_live
            else ("FINAL" if self.is_final else "PRE-GAME")
        )
        return (
            f"{self.away_team} {self.away_score}-{self.home_score} {self.home_team} "
            f"[{status_str} | Pace {self.pace_multiplier:.2f}x]"
        )


def _estimate_remaining_fraction(sport: str, period: int, clock_str: str, state: str) -> float:
    """Estimate fraction of regulation game remaining (0.0 to 1.0) from ESPN period & clock."""
    if state == "post":
        return 0.0
    if state == "pre" or period <= 0:
        return 1.0

    mins = 0.0
    if ":" in clock_str:
        parts = clock_str.split(":")
        try:
            mins = float(parts[0]) + float(parts[1]) / 60.0
        except ValueError:
            mins = 6.0

    if sport in ("NBA", "WNBA"):
        p_len = 12.0 if sport == "NBA" else 10.0
        total_mins = 4.0 * p_len
        periods_left = max(0, 4 - period)
        rem = (periods_left * p_len + min(p_len, mins)) / total_mins
        return round(max(0.02, min(1.0, rem)), 3)
    if sport == "NFL":
        total_mins = 60.0
        periods_left = max(0, 4 - period)
        rem = (periods_left * 15.0 + min(15.0, mins)) / total_mins
        return round(max(0.02, min(1.0, rem)), 3)
    if sport == "MLB":
        rem_innings = max(0, 9 - period + 0.5)
        return round(max(0.05, min(1.0, rem_innings / 9.0)), 3)
    return 0.55


def _parse_espn_event(sport: str, event: Dict[str, Any], latency_ms: float) -> Optional[GameTelemetry]:
    try:
        comp = (event.get("competitions") or [{}])[0]
        status_obj = comp.get("status") or event.get("status") or {}
        status_type = status_obj.get("type") or {}
        state = str(status_type.get("state") or "pre").lower()
        desc = str(status_type.get("description") or "").upper()

        period = int(status_obj.get("period") or 0)
        display_clock = str(status_obj.get("displayClock") or "0:00")

        competitors = comp.get("competitors") or []
        home_team, away_team = "HOME", "AWAY"
        home_score, away_score = 0, 0
        player_stats: Dict[str, Dict[str, float]] = {}

        for c in competitors:
            team_info = c.get("team") or {}
            abbr = str(team_info.get("abbreviation") or team_info.get("shortDisplayName") or "TM")
            score_val = int(float(c.get("score") or 0))
            if c.get("homeAway") == "home":
                home_team = abbr
                home_score = score_val
            else:
                away_team = abbr
                away_score = score_val

            for leader_cat in c.get("leaders") or []:
                cat_name = str(leader_cat.get("name") or "").lower()
                for leader in leader_cat.get("leaders") or []:
                    athlete = leader.get("athlete") or {}
                    full_name = str(athlete.get("displayName") or athlete.get("shortName") or "").upper()
                    val = float(leader.get("value") or 0.0)
                    if full_name:
                        player_stats.setdefault(full_name, {})[cat_name] = val

        rem_frac = _estimate_remaining_fraction(sport, period, display_clock, state)
        elapsed_frac = max(0.15, 1.0 - rem_frac)
        total_pts = home_score + away_score

        # Calculate live pace multiplier vs baseline sport scoring expectation
        baseline_total = {"NBA": 224.0, "WNBA": 164.0, "NFL": 45.5, "MLB": 8.5, "TENNIS": 22.5}.get(
            sport, 100.0
        )
        if state == "in" and elapsed_frac > 0.15:
            projected_total = total_pts / elapsed_frac
            pace_mult = max(0.82, min(1.22, projected_total / baseline_total))
        else:
            pace_mult = 1.02

        score_diff = abs(home_score - away_score)
        # Tight games in 2nd half boost star usage rate
        usage_boost = 1.05 if (state == "in" and score_diff <= 8 and period >= 2) else 1.00
        suspended = any(w in desc for w in ("SUSPENDED", "POSTPONED", "DELAYED", "CANCELED"))

        return GameTelemetry(
            event_id=str(event.get("id") or ""),
            sport=sport,
            matchup=f"{away_team} @ {home_team}",
            home_team=home_team,
            away_team=away_team,
            home_score=home_score,
            away_score=away_score,
            period=period,
            display_clock=display_clock,
            remaining_fraction=rem_frac,
            pace_multiplier=round(pace_mult, 3),
            usage_boost=round(usage_boost, 3),
            foul_trouble_flag=False,
            injury_spike_flag=False,
            suspended_flag=suspended,
            is_live=(state == "in"),
            is_final=(state == "post"),
            latency_ms=round(latency_ms, 1),
            player_stats=player_stats,
        )
    except Exception as exc:
        logger.debug("Failed parsing ESPN event for %s: %s", sport, exc)
        return None


async def fetch_sport_telemetry(
    session: aiohttp.ClientSession, sport: str
) -> List[GameTelemetry]:
    """Fetch live scoreboard & game telemetry from ESPN hidden API for a single sport."""
    url = ESPN_ENDPOINTS.get(sport.upper())
    if not url:
        return []

    t0 = time.perf_counter()
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=6)) as resp:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            if resp.status == 200:
                data = await resp.json()
                events = data.get("events") or []
                out: List[GameTelemetry] = []
                for ev in events:
                    parsed = _parse_espn_event(sport.upper(), ev, latency_ms)
                    if parsed:
                        out.append(parsed)
                return out
    except Exception as exc:
        logger.debug("ESPN telemetry fetch warning (%s): %s", sport, exc)
    return []


async def fetch_all_telemetry() -> Dict[str, List[GameTelemetry]]:
    """Concurrently poll ESPN live scoreboards across NBA, NFL, MLB, WNBA, and Tennis."""
    sports = list(ESPN_ENDPOINTS.keys())
    async with aiohttp.ClientSession() as session:
        tasks = [fetch_sport_telemetry(session, sp) for sp in sports]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    telemetry_map: Dict[str, List[GameTelemetry]] = {}
    for sp, res in zip(sports, results):
        if isinstance(res, list) and res:
            telemetry_map[sp] = res
        else:
            telemetry_map[sp] = [_build_synthetic_telemetry(sp)]
    return telemetry_map


def _build_synthetic_telemetry(sport: str) -> GameTelemetry:
    """Fallback synthetic telemetry snapshot when a league is off-schedule or between slates."""
    defaults = {
        "NBA": ("BOS", "LAL", 58, 61, 2, "4:12", 1.04),
        "WNBA": ("NYL", "IND", 42, 44, 2, "3:45", 1.03),
        "NFL": ("BUF", "KC", 14, 17, 2, "6:20", 1.02),
        "MLB": ("LAD", "NYY", 3, 4, 5, "Mid 5th", 1.01),
        "TENNIS": ("SIN", "ALC", 1, 1, 2, "Set 2", 1.02),
        "ESPORTS": ("NAVI", "FAZE", 9, 10, 2, "Map 2 R19", 1.06),
    }
    away, home, a_sc, h_sc, per, clk, pace = defaults.get(
        sport.upper(), ("AWAY", "HOME", 0, 0, 1, "10:00", 1.01)
    )
    return GameTelemetry(
        event_id=f"SYN-{sport}",
        sport=sport.upper(),
        matchup=f"{away} @ {home}",
        home_team=home,
        away_team=away,
        home_score=h_sc,
        away_score=a_sc,
        period=per,
        display_clock=clk,
        remaining_fraction=0.62,
        pace_multiplier=pace,
        usage_boost=1.03,
        is_live=False,
        is_final=False,
        latency_ms=42.0,
    )


_NBA_TEAM_ALIASES = {
    "NOP": ("NO", "PELICANS", "NEW ORLEANS"),
    "NO": ("NOP", "PELICANS", "NEW ORLEANS"),
    "GSW": ("GS", "WARRIORS", "GOLDEN STATE"),
    "GS": ("GSW", "WARRIORS", "GOLDEN STATE"),
    "SAS": ("SA", "SPURS", "SAN ANTONIO"),
    "SA": ("SAS", "SPURS", "SAN ANTONIO"),
    "NYK": ("NY", "KNICKS", "NEW YORK"),
    "NY": ("NYK", "KNICKS", "NEW YORK"),
    "WAS": ("WSH", "WIZARDS", "WASHINGTON"),
    "WSH": ("WAS", "WIZARDS", "WASHINGTON"),
    "UTA": ("UTAH", "JAZZ"),
    "UTAH": ("UTA", "JAZZ"),
    "BKN": ("NETS", "BROOKLYN"),
    "CLE": ("CAVS", "CAVALIERS", "CLEVELAND"),
    "HOU": ("ROCKETS", "HOUSTON"),
    "ATL": ("HAWKS", "ATLANTA"),
    "DEN": ("NUGGETS", "DENVER"),
    "PHX": ("SUNS", "PHOENIX"),
    "MIL": ("BUCKS", "MILWAUKEE"),
    "PHI": ("76ERS", "SIXERS", "PHILADELPHIA"),
    "CHI": ("BULLS", "CHICAGO"),
    "DAL": ("MAVS", "MAVERICKS", "DALLAS"),
}


def match_telemetry_for_prop(
    sport: str, ticker: str, prop_text: str, telemetry_map: Dict[str, List[GameTelemetry]]
) -> GameTelemetry:
    """Match a prop or market ticker to its corresponding live ESPN GameTelemetry object with alias resolution."""
    games = telemetry_map.get(sport.upper()) or []
    combined = f"{ticker} {prop_text}".upper()

    for g in games:
        h = g.home_team.upper()
        a = g.away_team.upper()
        if h in combined or a in combined:
            return g
        h_aliases = _NBA_TEAM_ALIASES.get(h, ())
        a_aliases = _NBA_TEAM_ALIASES.get(a, ())
        if any(al in combined for al in h_aliases) or any(al in combined for al in a_aliases):
            return g

    if games:
        return games[0]
    return _build_synthetic_telemetry(sport)
