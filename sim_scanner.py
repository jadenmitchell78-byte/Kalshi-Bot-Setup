#!/usr/bin/env python3
"""
sim_scanner.py - Background Kalshi NBA Prop Scanner (SIM ONLY, NO ORDERS).

Pulls all open Kalshi NBA prop markets (PTS, REB, AST, 3PT, PRA) every 10-15 minutes
before tip-off, runs the live sim math for each prop, and logs snapshots to sim_log.db
via sim_logger.log_ladder.

Key guarantees & fixes:
1. Authoritative Tip-Off & In-Game Guard:
   - Queries ESPN NBA scoreboard for real game start time (tipoff_ts) and live status.
   - Strictly requires ESPN game state == 'pre'.
   - Skips any game that has tipped off (now_ts >= tipoff_ts) or is in-progress ('in'/'post').
2. Daily Player Profile Cache:
   - Caches ESPN player profiles for the date to avoid API rate limits across 10-minute cycles.
3. 3PT Duplicate Deduplication:
   - Deduplicates KXNBA3PT and KXNBATHREES by (player, stat, line) so no prop is logged twice.
4. SIM only: never places an order, never touches picks.db.
5. Respects sim_logger's 5-minute duplicate throttle.
6. Prints exactly one summary line per cycle: (markets scanned, rows written).

Usage:
  python sim_scanner.py --once             # Single scan cycle
  python sim_scanner.py --interval 600     # Continuous loop every 10 minutes (default)
"""

import argparse
import datetime
import json
import logging
import math
import os
import re
import sys
import time
import urllib.request
from typing import Any, Dict, List, Optional, Set, Tuple

import requests

from auto_market_index import (
    DAILY_DATE_CODE_REGEX,
    fetch_espn_player_profile,
    parse_strike_from_market,
    strip_accents,
)
from build_backtest_csv import normalize_team_abbr
import player_index
import risk_manager
from sim_core import core_simulate, make_rng, prior_for
import sim_logger

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("sim_scanner")

KALSHI_BASES = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
]

NBA_PROP_SERIES = [
    "KXNBAPTS",
    "KXNBAREB",
    "KXNBAAST",
    "KXNBA3PT",
    "KXNBATHREES",
    "KXNBAPRA",
]

SERIES_STAT_MAP = {
    "KXNBAPTS": "PTS",
    "KXNBAREB": "REB",
    "KXNBAAST": "AST",
    "KXNBA3PT": "3PM",
    "KXNBATHREES": "3PM",
    "KXNBAPRA": "PRA",
}

# In-memory short cache for ESPN player profiles (300s TTL) so afternoon injury updates are captured
_PLAYER_PROFILE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}

# In-memory short cache for ESPN scoreboard games (120s TTL)
_ESPN_SCOREBOARD_CACHE: Dict[str, Any] = {"ts": 0.0, "games": {}}


def fetch_espn_nba_games() -> Dict[str, Dict[str, Any]]:
    """
    Fetches ESPN NBA scoreboard for today (and adjacent dates) to obtain:
    - tipoff_ts: exact scheduled game start timestamp (from ISO UTC date)
    - state: 'pre' (scheduled), 'in' (live in progress), or 'post' (final)
    - home & away team abbreviations
    Returns mapping: team_abbr -> {"tipoff_ts": float, "state": str, "home": str, "away": str, "game_id": str}
    """
    global _ESPN_SCOREBOARD_CACHE
    now_ts = time.time()
    if now_ts - _ESPN_SCOREBOARD_CACHE["ts"] < 120.0 and _ESPN_SCOREBOARD_CACHE["games"]:
        return _ESPN_SCOREBOARD_CACHE["games"]

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    # Check sliding window: yesterday, today, tomorrow in US game night alignment
    dates_to_check = set()
    for offset_h in (-7, 0, 7):
        d_str = (utc_now + datetime.timedelta(hours=offset_h)).strftime("%Y%m%d")
        dates_to_check.add(d_str)

    team_map: Dict[str, Dict[str, Any]] = {}

    for d_clean in sorted(dates_to_check):
        url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={d_clean}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            log.debug("ESPN Scoreboard error for %s: %s", d_clean, exc)
            continue

        for ev in data.get("events", []):
            gid = str(ev.get("id") or "")
            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors") or []
            home_c = next((c for c in competitors if c.get("homeAway") == "home"), {})
            away_c = next((c for c in competitors if c.get("homeAway") == "away"), {})
            home = normalize_team_abbr((home_c.get("team") or {}).get("abbreviation") or "")
            away = normalize_team_abbr((away_c.get("team") or {}).get("abbreviation") or "")

            tipoff_ts = None
            raw_dt = ev.get("date")
            if raw_dt:
                try:
                    dt_obj = datetime.datetime.fromisoformat(str(raw_dt).replace("Z", "+00:00"))
                    tipoff_ts = dt_obj.timestamp()
                except Exception:
                    pass

            status_obj = ev.get("status") or {}
            type_obj = status_obj.get("type") or {}
            state = str(type_obj.get("state") or "pre").lower()

            game_info = {
                "game_id": gid,
                "tipoff_ts": tipoff_ts,
                "state": state,
                "home": home,
                "away": away,
                "date_clean": d_clean,
            }

            if home:
                team_map[home] = game_info
            if away:
                team_map[away] = game_info

    _ESPN_SCOREBOARD_CACHE = {"ts": now_ts, "games": team_map}
    return team_map


def get_cached_player_profile(p_name: str, athlete_id: str, team_abbr: str, game_date_str: str) -> Dict[str, Any]:
    """Retrieves ESPN player profile with a 300s (5-minute) TTL so fresh injury updates are captured."""
    cache_key = f"{p_name}:{athlete_id}:{game_date_str}"
    now_ts = time.time()
    cached = _PLAYER_PROFILE_CACHE.get(cache_key)
    if cached and (now_ts - cached[0] < 300.0):
        return cached[1]

    profile = fetch_espn_player_profile(
        p_name,
        sport="NBA",
        athlete_id=athlete_id,
        known_name=p_name,
        known_team=team_abbr,
    )
    _PLAYER_PROFILE_CACHE[cache_key] = (now_ts, profile)
    return profile


def fetch_open_nba_markets() -> List[Dict[str, Any]]:
    """Paginates all open NBA prop markets across PTS, REB, AST, 3PT, and PRA."""
    all_markets: List[Dict[str, Any]] = []
    seen_tickers: Set[str] = set()

    for base in KALSHI_BASES:
        success = False
        for series in NBA_PROP_SERIES:
            cursor = None
            for _ in range(5):  # Paginate up to 1000 markets per series
                params: Dict[str, Any] = {"status": "open", "limit": 200, "series_ticker": series}
                if cursor:
                    params["cursor"] = cursor
                try:
                    r = requests.get(f"{base}/markets", params=params, timeout=8)
                    if r.status_code != 200:
                        break
                    data = r.json()
                    mkts = data.get("markets", [])
                    for m in mkts:
                        tk = str(m.get("ticker") or "").upper()
                        if not tk or tk in seen_tickers:
                            continue
                        # Universal Daily Filter: Must match 26OCT... pattern and not futures
                        if not DAILY_DATE_CODE_REGEX.search(tk):
                            continue
                        if any(bad in tk for bad in ("CAREER", "SEASON", "RECORD", "AWARD", "LEADER", "HOF", "CHAMP")):
                            continue
                        seen_tickers.add(tk)
                        all_markets.append(m)
                    cursor = data.get("cursor")
                    if not cursor or not mkts:
                        break
                except Exception as exc:
                    log.debug("Fetch error for %s on %s: %s", series, base, exc)
                    break
            success = len(all_markets) > 0
        if success:
            break

    return all_markets


def extract_player_candidate(market: Dict[str, Any]) -> str:
    """Extracts raw player name candidate from title, rules_primary, or ticker."""
    title = str(market.get("title") or "")
    if ":" in title:
        part = title.split(":", 1)[0].strip()
        if not any(sw in part.lower() for sw in ("point", "pts", "rebound", "reb", "assist", "ast", "three", "3pt", "pra")):
            return part

    # Check rules_primary: 'if Anthony Edwards records at least 25 points'
    rules = str(market.get("rules_primary") or "")
    m_rules = re.search(r"if\s+([A-Z][a-zA-Z\.\'\-\s]+?)\s+(?:records|scores|has|gets|makes)", rules)
    if m_rules:
        cand = m_rules.group(1).strip()
        if len(cand.split()) >= 2:
            return cand

    # Check title without stat words
    cleaned_title = re.sub(r"\b(Points|Rebounds|Assists|Three-Pointers|Threes|3-Point Field Goals|PRA)\b", "", title, flags=re.I).strip()
    if cleaned_title and len(cleaned_title.split()) >= 2:
        return cleaned_title

    # Fallback to ticker entity: KXNBAPTS-26OCT31DENMIN-MINAEDWARDS5-25
    tk = str(market.get("ticker") or "")
    parts = tk.split("-")
    if len(parts) >= 3:
        entity = parts[2]
        m_ent = re.match(r"^[A-Z]{3}([A-Z]+?)\d*$", entity)
        if m_ent:
            return m_ent.group(1)

    return ""


def run_scan_cycle() -> Tuple[int, int]:
    """
    Runs a single scan cycle:
    1. Fetches real ESPN scoreboard start times (tipoff_ts) and live status.
    2. Fetches all open Kalshi NBA markets across PTS, REB, AST, 3PT, PRA.
    3. Resolves player and attaches ESPN game tipoff_ts.
    4. Drops any game that has started (now_ts >= tipoff_ts) or whose ESPN state != 'pre'.
    5. Deduplicates 3PT rungs (KXNBA3PT vs KXNBATHREES) on (player, stat, line).
    6. Runs Monte Carlo simulation once per (player, stat).
    7. Logs snapshots via sim_logger.log_ladder and evaluates risk decisions in risk_log.
    Returns (markets_scanned, rows_written).
    """
    now_ts = time.time()
    espn_games = fetch_espn_nba_games()
    raw_markets = fetch_open_nba_markets()

    if not raw_markets:
        return 0, 0

    utc_now = datetime.datetime.now(datetime.timezone.utc)
    game_date_str = (utc_now - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")

    # Group open markets by (canonical_player_name, stat) with strict tip-off and duplicate checks
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    seen_prop_cuts: Set[Tuple[str, str, float]] = set()  # (player, stat, round(line, 2))
    eligible_markets_count = 0

    for m in raw_markets:
        cand_str = extract_player_candidate(m)
        if not cand_str:
            continue

        verdict, cands = player_index.resolve_decision(cand_str, "NBA")
        if not cands:
            continue

        player_info = cands[0]
        canonical_name = player_info.get("name") or cand_str
        team_abbr = normalize_team_abbr(player_info.get("team") or "")
        athlete_id = str(player_info.get("id") or "")

        # 1. Authoritative Tip-off & Game State Guard from ESPN
        game_info = espn_games.get(team_abbr)
        if not game_info:
            continue

        # Skip if game status is not scheduled pregame (e.g. 'in' live or 'post' final)
        if game_info.get("state") != "pre":
            continue

        espn_tipoff = game_info.get("tipoff_ts")
        if espn_tipoff is None:
            continue

        # Skip if game has already tipped off
        if now_ts >= espn_tipoff:
            continue

        # 2. Canonical Stat Resolution
        series = str(m.get("series_ticker") or "").upper()
        stat = SERIES_STAT_MAP.get(series)
        if not stat:
            for s_pref, s_stat in SERIES_STAT_MAP.items():
                if str(m.get("ticker", "")).startswith(s_pref):
                    stat = s_stat
                    break
        if not stat:
            stat = "PTS"

        # 3. Line Resolution & Deduplication (e.g. KXNBA3PT vs KXNBATHREES)
        strike = parse_strike_from_market(m)
        if strike <= 0:
            continue
        cutoff_line = strike - 0.5 if strike.is_integer() else strike
        line_key = (canonical_name, stat, round(cutoff_line, 2))

        if line_key in seen_prop_cuts:
            continue  # Avoid duplicate 3PT / milestone markets
        seen_prop_cuts.add(line_key)

        group_key = (canonical_name, stat)
        if group_key not in groups:
            groups[group_key] = {
                "player": canonical_name,
                "athlete_id": athlete_id,
                "team": team_abbr,
                "stat": stat,
                "tipoff_ts": espn_tipoff,
                "rungs": [],
            }

        groups[group_key]["rungs"].append({
            "ticker": m.get("ticker"),
            "line": cutoff_line,
            "yes_bid": m.get("yes_bid"),
            "yes_ask": m.get("yes_ask"),
            "raw_strike": strike,
        })
        eligible_markets_count += 1

    if not groups:
        return 0, 0

    total_rows_written = 0

    # Run simulation once per (player, stat)
    for (p_name, stat_norm), g in groups.items():
        athlete_id = g["athlete_id"]
        team_abbr = g["team"]
        rungs = g["rungs"]
        tipoff_ts = g["tipoff_ts"]

        # Fetch athlete ESPN averages with daily caching
        athlete_espn = get_cached_player_profile(p_name, athlete_id, team_abbr, game_date_str)

        season_avg_map = athlete_espn.get("season_averages", {})
        prior_avg_map = athlete_espn.get("prior_season_averages") or {}
        last5_map = athlete_espn.get("last5_averages", {}) or {}
        games_played = int(athlete_espn.get("games_played") or 0)
        season_min = float(athlete_espn.get("minutes_avg") or season_avg_map.get("MIN") or 31.5)
        last5_min = athlete_espn.get("last5_minutes")
        is_home = athlete_espn.get("is_home")
        opp_team = str(athlete_espn.get("opp_team") or "").upper()
        opp_factor = float(athlete_espn.get("opp_factor") or 1.0)
        pos_u = str(athlete_espn.get("position") or "").upper()
        out_teammates = athlete_espn.get("out_teammates") or []

        player_prior = prior_avg_map.get(stat_norm)
        fallback_prior = prior_for(stat_norm, pos_u)
        prior_val = float(player_prior) if player_prior is not None else fallback_prior
        raw_season_val = float(season_avg_map.get(stat_norm, prior_val))
        last5_val = last5_map.get(stat_norm)

        # Teammate usage boost
        injury_boost = 1.0
        if out_teammates:
            injury_boost = min(1.08, 1.0 + 0.03 * min(2, len(out_teammates)))

        # 10,000 Monte Carlo draws
        rng = make_rng(f"{p_name}:{stat_norm}:{game_date_str}")
        draws_arr, sim_info = core_simulate(
            stat=stat_norm,
            season_avg=raw_season_val,
            games_played=games_played,
            last5_avg=float(last5_val) if last5_val is not None else None,
            season_min=season_min,
            last5_min=float(last5_min) if last5_min is not None else None,
            is_home=is_home,
            opp_factor=opp_factor,
            position=pos_u,
            n=10000,
            rng=rng,
            prior=prior_val,
            injury_boost=injury_boost,
        )

        # Score every open Kalshi rung with the sim's true over prob
        for r in rungs:
            line_val = float(r["line"])
            r["true_over_prob"] = float((draws_arr > line_val).mean())

        # Construct probability ladder
        proj_mean = float(sim_info["mean"])
        if stat_norm in ("3PM", "3PT"):
            milestones = [1, 2, 3, 4, 5, 6]
        elif stat_norm in ("REB", "AST"):
            center_m = max(3, int(round(proj_mean)))
            milestones = [max(1, center_m - 3), max(2, center_m - 1), center_m, center_m + 2, center_m + 4]
        else:
            base_5 = max(15, int(round(proj_mean / 5.0) * 5))
            milestones = [max(10, base_5 - 10), max(10, base_5 - 5), base_5, base_5 + 5, base_5 + 10]

        prob_ladder = []
        for m_val in milestones:
            line_cut = float(m_val) - 0.5
            p_val = float((draws_arr > line_cut).mean())
            prob_ladder.append({"line": line_cut, "prob_pct": round(p_val * 100.0, 1)})

        confidence_inputs = {
            "season_avg": round(raw_season_val, 2),
            "proj_mean": round(proj_mean, 2),
            "games_played": games_played,
            "minutes_proj": round(float(sim_info.get("proj_min", season_min)), 1),
            "opp_team": opp_team or "N/A",
            "opp_factor": round(opp_factor, 3),
            "injury_boost": round(injury_boost, 3),
            "scanner": True,
        }

        try:
            written = sim_logger.log_ladder(
                player=p_name,
                team=team_abbr,
                opp=opp_team,
                stat=stat_norm,
                game_date=game_date_str,
                ladder=prob_ladder,
                kalshi_rungs=rungs,
                inputs=confidence_inputs,
                tipoff_ts=tipoff_ts,
            )
            total_rows_written += written
        except Exception as exc:
            log.warning("log_ladder error for %s %s: %s", p_name, stat_norm, exc)

        # Build risk candidates for every priced rung and evaluate decisions
        risk_cands: List[risk_manager.Candidate] = []
        is_confirmed = bool(athlete_espn.get("confirmed", False))
        for r in rungs:
            if r.get("yes_bid") is not None or r.get("yes_ask") is not None:
                cand = risk_manager.Candidate(
                    ticker=r.get("ticker", ""),
                    player=p_name,
                    team=team_abbr,
                    opp=opp_team,
                    stat=stat_norm,
                    line=float(r["line"]),
                    side="YES",
                    sim_prob=float(r.get("true_over_prob", 0.5)),
                    yes_bid=r.get("yes_bid"),
                    yes_ask=r.get("yes_ask"),
                    tipoff_ts=float(tipoff_ts) if tipoff_ts else 0.0,
                    snapshot_ts=now_ts,
                    confirmed=is_confirmed,
                )
                risk_cands.append(cand)

        if risk_cands:
            try:
                risk_manager.evaluate_batch(
                    risk_cands,
                    bankroll=100.0,
                    config=risk_manager.RiskConfig(live_enabled=False),
                    now_ts=now_ts,
                    log_to_db=True,
                )
            except Exception as r_exc:
                log.debug("risk evaluation logging error: %s", r_exc)

    return eligible_markets_count, total_rows_written


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalshi NBA Prop Background Scanner")
    parser.add_argument("--interval", type=int, default=600, help="Loop interval in seconds (default: 600s = 10m)")
    parser.add_argument("--once", action="store_true", help="Run a single scan cycle and exit")
    args = parser.parse_args()

    if args.once:
        scanned, written = run_scan_cycle()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[SIM-SCANNER] {now_str} | Markets scanned: {scanned} | Rows written: {written}", flush=True)
        return

    # Continuous loop
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[SIM-SCANNER] Started background daemon (interval={args.interval}s, SIM-only)...", flush=True)

    while True:
        try:
            scanned, written = run_scan_cycle()
            cycle_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[SIM-SCANNER] {cycle_time} | Markets scanned: {scanned} | Rows written: {written}", flush=True)
        except Exception as exc:
            cycle_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            print(f"[SIM-SCANNER] {cycle_time} | Error during scan cycle: {exc}", flush=True)

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
