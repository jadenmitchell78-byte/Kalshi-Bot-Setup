# ~/peezy_bot/sim_prop_bridge.py
"""
Peezy Universal Player Prop Simulation & Bridge Engine:
- Uses universal_entity_resolver to resolve athlete names across all diacritics and Kalshi ticker formats
- Shares core_simulate from sim_core.py so the live bot and sim_backtest.py execute identical math
- Evaluates both YES (OVER) and NO (UNDER) Kalshi contracts with fee-aware EV (0.07 * P * (1 - P))
- When no Kalshi contract is open and no line was given, shows the probability ladder with no artificial OVER/UNDER pick
"""

import datetime
import html
import json
import math
import re
import time
import traceback
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import requests

from clean_casual_core import (
    format_pick_headline,
    format_video_style_single_card,
    get_prop_date_str,
    build_casual_video_buttons,
)
from universal_entity_resolver import (
    clean_token,
    resolve_athlete_universal,
    find_player_ladders,
    extract_player_and_stat_from_kalshi_market,
    UNIVERSAL_STAT_ALIASES,
)
from auto_market_index import (
    search_kalshi_markets,
    fetch_espn_player_profile,
    get_all_daily_kalshi_markets,
    get_real_open_markets_summary,
    strip_accents,
    DAILY_DATE_CODE_REGEX,
    ALLOWED_SPORTS_PREFIXES,
)
from sim_core import (
    core_simulate,
    kalshi_fee_prob,
    make_rng,
    prior_for,
)


DAILY_DATE_CODE_REGEX = re.compile(r"26(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}", re.I)
LAST_SIM_RESULT: Dict[str, Any] = {}
BANNED_FUTURES_WORDS = ("CAREER", "SEASON", "RECORD", "AWARD", "LEADER", "HOF", "CHAMP")

# Live Bot Calibration & Edge Gates (from 2025-26 Out-of-Sample verification)
POSITIVE_SKILL_STATS = {"3PM", "3PT", "AST", "REB", "PTS"}
EXPERIMENTAL_STATS = {"PRA"}
FAIR_PROB_MIN = 0.20            # Only show/bet fair probabilities in [20%, 80%]
FAIR_PROB_MAX = 0.80
MIN_EDGE_AFTER_FEES_PCT = 5.0   # Require >= +5.0 percentage points of edge after Kalshi fees


def normalize_kalshi_market(m: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Initial market ingestion filter:
    1. Check ticker: if it contains CAREER, SEASON, RECORD, AWARD, LEADER, HOF, CHAMP,
       or does NOT contain a valid daily game date stamp matching 26(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\\d{2},
       or does not start with an allowed sports prefix, REJECT IT IMMEDIATELY (returns None).
    2. Check strike_line: if strike_line <= 0 (e.g. -0.5 or 0), DISCARD IT IMMEDIATELY (returns None).
    """
    if not isinstance(m, dict):
        return None
    tk = str(m.get("ticker") or "").upper()
    if not tk:
        return None
    if not any(tk.startswith(pref) for pref in ALLOWED_SPORTS_PREFIXES):
        return None
    if any(bad in tk for bad in BANNED_FUTURES_WORDS):
        return None
    if not DAILY_DATE_CODE_REGEX.search(tk):
        return None
    strike = m.get("line") or m.get("strike_price") or m.get("floor_strike") or m.get("cap_strike")
    if strike is not None:
        try:
            if float(strike) <= 0.0:
                return None
        except (ValueError, TypeError):
            pass
    return m


def resolve_espn_athlete(query_name: str, sport: str = "NBA") -> Dict[str, Any]:
    """Zero-dictionary ESPN athlete resolver."""
    return fetch_espn_player_profile(query_name, sport=sport)


def run_bridge_player_simulation(
    sport: str,
    player_query: str,
    stat_query: str,
    line_query: Optional[float] = None,
    bankroll: float = 32.22,
    sniper_on: bool = False,
    live_mode: bool = False,
    athlete_id: Optional[str] = None,
    known_name: Optional[str] = None,
    known_team: Optional[str] = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Main Player Prop Simulation Entry Point:
    1. Resolves athlete via fetch_espn_player_profile (using known athlete_id/known_name/known_team when provided by player_index, skipping any display-name search).
    2. Calls sim_core.core_simulate with the player's own prior-season average (when available), L5 form blending, minutes projection, home/away split, real opponent defensive factor, and teammate injury boost.
    3. Uses a deterministic SHA-256 RNG seed (no salted Python hash()) and computes a full probability ladder (e.g. 1+, 2+, 3+, 4+).
    4. Evaluates both YES (OVER) and NO (UNDER) Kalshi contracts with fee-aware EV (0.07 * P * (1 - P)).
    5. When no Kalshi market is open and no line was given, outputs the probability ladder with side='PROJECTION' rather than inventing an artificial OVER pick.
    """
    sport_u = sport.upper()
    athlete_espn = fetch_espn_player_profile(
        player_query,
        sport=sport_u,
        athlete_id=athlete_id,
        known_name=known_name,
        known_team=known_team,
    )
    athlete_univ = {} if athlete_id else resolve_athlete_universal(player_query, sport=sport_u)

    p_name = known_name or athlete_espn.get("full_name") or athlete_univ.get("full_name") or player_query.title()
    team_abbr = (known_team or athlete_espn.get("team_abbr") or athlete_univ.get("team_abbr") or sport_u).upper()
    utc_now = datetime.datetime.now(datetime.timezone.utc)
    us_today = (utc_now - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
    game_date = athlete_espn.get("game_date") or athlete_univ.get("game_date") or us_today
    matchup = athlete_espn.get("matchup") or athlete_univ.get("matchup") or f"{team_abbr} Matchup"
    if matchup.endswith(" Game") or matchup == "NBA Game":
        matchup = f"{team_abbr} Matchup"

    stat_norm = UNIVERSAL_STAT_ALIASES.get(clean_token(stat_query).upper(), "PTS")

    # 1. Sweep for real open Kalshi ladders for this player via auto_market_index
    ladders = search_kalshi_markets(f"{p_name} {stat_query}", sport=sport_u, stat_query=stat_norm)
    if not ladders:
        ladders = search_kalshi_markets(p_name, sport=sport_u, stat_query=stat_norm)
    if not ladders:
        ladders = search_kalshi_markets(player_query, sport=sport_u, stat_query=stat_norm)
    if ladders:
        def _real_rung(r: Dict[str, Any]) -> bool:
            try:
                return bool(normalize_kalshi_market(r)) and float(r.get("line", 0)) > 0 and float(r.get("yes_ask") or 0) > 5
            except (TypeError, ValueError):
                return False
        ladders = [r for r in ladders if _real_rung(r)]

    # 2. Extract raw season average, prior-season average, L5 average, games played, minutes, home/away, opponent factor, and injuries
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

    # Use player's own prior-season / career average when available; fall back to position prior
    player_prior = prior_avg_map.get(stat_norm)
    fallback_prior = prior_for(stat_norm, pos_u)
    prior_val = float(player_prior) if player_prior is not None else fallback_prior
    raw_season_val = float(season_avg_map.get(stat_norm, prior_val))
    last5_val = last5_map.get(stat_norm)

    # Only apply teammate-out usage redistribution if validated out-of-sample by ab_test_lineup.py
    from sim_core import PARAMS as _SIM_PARAMS
    redist_w = float(_SIM_PARAMS.get("lineup_rate_redist", 0.0) or 0.0)
    if out_teammates and redist_w > 0.0:
        injury_boost = min(1.08, 1.0 + redist_w * min(2, len(out_teammates)))
        injury_adj_pct = round((injury_boost - 1.0) * 100.0, 1)
    else:
        injury_boost = 1.0
        injury_adj_pct = 0.0

    if is_home is True:
        loc_label = "Home (+1.2%)"
    elif is_home is False:
        loc_label = "Away (-1.2%)"
    else:
        loc_label = "Neutral"

    opp_adj_pct = round((opp_factor - 1.0) * 100.0, 1)

    # 3. Run 10,000-Iteration Monte Carlo Simulation via shared sim_core.core_simulate with deterministic SHA-256 seed
    rng = make_rng(f"{p_name}:{stat_norm}:{game_date}")
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
    proj_mean = float(sim_info["mean"])
    proj_std = float(sim_info["std"])
    shrunk_season = float(sim_info["shrunk"])
    proj_min = round(float(sim_info["proj_min"]), 1)

    draws = sorted(float(x) for x in draws_arr)
    p10 = round(draws[int(10000 * 0.10)], 1)
    p50 = round(draws[int(10000 * 0.50)], 1)
    p90 = round(draws[int(10000 * 0.90)], 1)

    # Build multi-line probability distribution ladder, gated to calibrated [20%, 80%] fair probabilities
    if stat_norm in ("3PM", "3PT", "SOG"):
        center_m = max(1, int(round(proj_mean)))
        cand_milestones = [max(1, center_m - 2), max(1, center_m - 1), center_m, center_m + 1, center_m + 2, center_m + 3]
        cand_milestones = sorted(list(dict.fromkeys(cand_milestones)))
    elif stat_norm in ("REB", "AST", "K"):
        center_m = max(3, int(round(proj_mean)))
        cand_milestones = [max(2, center_m - 4), max(2, center_m - 2), center_m, center_m + 2, center_m + 4]
        cand_milestones = sorted(list(dict.fromkeys(cand_milestones)))
    else:  # PTS, PRA, yards
        base_5 = max(15, int(round(proj_mean / 5.0) * 5))
        cand_milestones = [max(10, base_5 - 10), max(10, base_5 - 5), base_5, base_5 + 5, base_5 + 10]
        cand_milestones = sorted(list(dict.fromkeys(cand_milestones)))

    all_ladder_rungs: List[Dict[str, Any]] = []
    for m_val in cand_milestones:
        line_cut = float(m_val) - 0.5
        hits_m = sum(1 for d in draws if d > line_cut)
        prob_dec = hits_m / 10000.0
        prob_pct = round(prob_dec * 100.0, 1)
        fee_c = int(round(kalshi_fee_prob(prob_dec) * 100.0))
        in_band = (FAIR_PROB_MIN <= prob_dec <= FAIR_PROB_MAX)
        all_ladder_rungs.append({
            "label": f"{m_val}+",
            "line": line_cut,
            "prob_pct": prob_pct,
            "in_prob_band": in_band,
            "fair_cents": max(1, min(99, int(round(prob_pct)))),
            "buy_below_cents": max(1, min(95, int(round(prob_pct - (fee_c + MIN_EDGE_AFTER_FEES_PCT))))),
        })

    # Keep only calibrated 20%-80% fair probability rungs on the card (fallback to closest to 50% if none)
    prob_ladder = [r for r in all_ladder_rungs if r["in_prob_band"]][:4]
    if not prob_ladder and all_ladder_rungs:
        prob_ladder = sorted(all_ladder_rungs, key=lambda r: abs(r["prob_pct"] - 50.0))[:2]
    prob_ladder_str = " · ".join(f"<b>{r['label']}</b> {r['prob_pct']:.0f}%" for r in prob_ladder)

    is_experimental_stat = stat_norm in EXPERIMENTAL_STATS
    is_positive_skill_stat = stat_norm in POSITIVE_SKILL_STATS

    confidence_inputs = {
        "games_played": games_played,
        "season_avg": round(raw_season_val, 2),
        "prior_avg": round(prior_val, 2),
        "prior_source": "prior_season" if player_prior is not None else "position",
        "shrunk_avg": round(shrunk_season, 2),
        "last5_avg": round(float(last5_val), 2) if last5_val is not None else None,
        "minutes_season": round(season_min, 1),
        "minutes_proj": round(proj_min, 1),
        "home_away": loc_label,
        "opp_team": opp_team or "N/A",
        "opp_adj_pct": opp_adj_pct,
        "out_teammates": out_teammates[:3],
        "injury_adj_pct": injury_adj_pct,
        "is_experimental": is_experimental_stat,
    }

    # 4. Evaluate each Kalshi ladder rung against 10k MC draws (Fee-aware + 20-80% prob gate + 5pt min edge gate)
    best_rung = None
    ladder_summary_lines = []

    if ladders:
        ladders = [r for r in ladders if float(r.get("line", 0)) > 0]
        for r in ladders:
            rung_line = float(r["line"])
            over_hits = sum(1 for d in draws if d > rung_line)
            true_over_prob = round(over_hits / 10000.0, 4)
            true_under_prob = round(1.0 - true_over_prob, 4)

            yes_ask = int(r.get("yes_ask") or 50)
            yes_bid = int(r.get("yes_bid") or max(1, yes_ask - 4))
            no_ask = int(r.get("no_ask") or max(1, 100 - yes_bid))
            no_bid = int(r.get("no_bid") or max(1, 100 - yes_ask))

            yes_ask_prob = yes_ask / 100.0
            no_ask_prob = no_ask / 100.0

            yes_ev = round((true_over_prob - yes_ask_prob - kalshi_fee_prob(yes_ask_prob)) * 100.0, 1)
            no_ev = round((true_under_prob - no_ask_prob - kalshi_fee_prob(no_ask_prob)) * 100.0, 1)

            if no_ev > yes_ev and 5 < no_ask < 95:
                r["picked_side"] = "UNDER"
                r["contract_side"] = "NO"
                r["entry_cents"] = no_ask
                r["true_prob"] = true_under_prob
                r["ev_edge"] = no_ev
            else:
                r["picked_side"] = "OVER"
                r["contract_side"] = "YES"
                r["entry_cents"] = yes_ask
                r["true_prob"] = true_over_prob
                r["ev_edge"] = yes_ev

            r["true_over_prob"] = true_over_prob
            r["yes_bid"] = yes_bid
            r["yes_ask"] = yes_ask
            r["no_bid"] = no_bid
            r["no_ask"] = no_ask

            in_prob_band = (FAIR_PROB_MIN <= r["true_prob"] <= FAIR_PROB_MAX)
            meets_min_edge = (r["ev_edge"] >= MIN_EDGE_AFTER_FEES_PCT)

            gate_reasons = []
            if is_experimental_stat:
                gate_reasons.append("EXPERIMENTAL_STAT_PRA")
            elif not is_positive_skill_stat:
                gate_reasons.append("UNVERIFIED_STAT")
            if not in_prob_band:
                gate_reasons.append(f"PROB_{r['true_prob']*100:.0f}%_OUTSIDE_20_80")
            if not meets_min_edge:
                gate_reasons.append(f"EDGE_{r['ev_edge']:+.1f}%_BELOW_5PT")

            r["passes_live_gate"] = (len(gate_reasons) == 0)
            r["gate_status"] = "ACTIONABLE_PASS" if r["passes_live_gate"] else ("GATED:" + ",".join(gate_reasons))

            if in_prob_band:
                ev_str = f"{r['ev_edge']:+.1f}%"
                gate_tag = " ✅" if r["passes_live_gate"] else " (Pass <+5%)"
                ladder_summary_lines.append(
                    f"• <b>{r.get('milestone_label', f'{rung_line}+')} {stat_norm} ({r['picked_side']}):</b> "
                    f"{round(r['true_prob']*100, 1)}% Fair vs {r['entry_cents']}¢ Ask "
                    f"(<code>{ev_str} Fee-Adj EV{gate_tag}</code>) — <code>{r['ticker']}</code>"
                )

        if line_query is not None:
            closest_rungs = sorted(ladders, key=lambda r: abs(float(r["line"]) - line_query))
            best_rung = closest_rungs[0]
        else:
            passing_rungs = [r for r in ladders if r.get("passes_live_gate")]
            if passing_rungs:
                best_rung = max(passing_rungs, key=lambda r: r.get("ev_edge", -999))
            else:
                in_band_rungs = [r for r in ladders if FAIR_PROB_MIN <= r.get("true_prob", 0.5) <= FAIR_PROB_MAX]
                pool = in_band_rungs if in_band_rungs else ladders
                best_rung = max(pool, key=lambda r: r.get("ev_edge", -999))

    # Hook in sim_logger (writes to its own sim_log.db, never touches picks.db)
    try:
        import sim_logger
        sim_logger.log_ladder(
            player=p_name, team=team_abbr, opp=opp_team, stat=stat_norm,
            game_date=str(game_date)[:10], ladder=prob_ladder, kalshi_rungs=ladders,
            inputs=confidence_inputs)
    except Exception:
        pass            # logging must never break a reply

    # Only mark orderable if a Kalshi rung exists AND passes all 3 live gates (20-80% prob, positive-skill stat, >= +5pt post-fee edge)
    has_open_market = bool(best_rung)
    is_orderable = bool(best_rung and best_rung.get("passes_live_gate"))
    contract_side = "YES"
    gate_status = "ACTIONABLE_PASS"
    gate_note = ""

    if has_open_market:
        ticker = best_rung["ticker"]
        entry_cents = best_rung.get("entry_cents", best_rung.get("yes_ask", 50))
        target_line = float(best_rung["line"])
        milestone_label = best_rung.get("milestone_label", f"{target_line}+")
        true_prob = best_rung["true_prob"]
        ev_edge = best_rung["ev_edge"]
        side = best_rung.get("picked_side", "OVER")
        contract_side = best_rung.get("contract_side", "YES")
        book_prob = round(entry_cents / 100.0, 4)
        gate_status = best_rung.get("gate_status", "ACTIONABLE_PASS")
        if not is_orderable:
            if is_experimental_stat:
                gate_note = "🧪 Experimental stat (PRA has +0.3% OOS skill) — shown for tracking only, no live bet."
            elif not (FAIR_PROB_MIN <= true_prob <= FAIR_PROB_MAX):
                gate_note = f"⏸ Gated: Fair prob {true_prob*100:.0f}% is outside the calibrated 20–80% range."
            else:
                gate_note = f"⏸ Pass (No Bet): {ev_edge:+.1f}% post-fee edge is below the +{MIN_EDGE_AFTER_FEES_PCT:.0f}% minimum gate."
    elif line_query is not None and line_query > 0:
        # User explicitly typed a line (e.g. /NBA Donovan Mitchell PTS 27.5): evaluate that specific threshold
        target_line = float(line_query)
        if target_line.is_integer():
            target_line = target_line - 0.5
        over_hits = sum(1 for d in draws if d > target_line)
        over_prob = over_hits / 10000.0
        side = "OVER" if over_prob >= 0.50 else "UNDER"
        contract_side = "YES" if side == "OVER" else "NO"
        true_prob = over_prob if side == "OVER" else (1.0 - over_prob)
        entry_cents = None
        book_prob = None
        ev_edge = None
        ticker = "Projection only (no open Kalshi contract)"
        milestone_label = f"{target_line}"
        if is_experimental_stat:
            gate_status = "GATED:EXPERIMENTAL_STAT_PRA"
            gate_note = "🧪 Experimental stat (PRA has +0.3% OOS skill) — projection only."
        elif not (FAIR_PROB_MIN <= over_prob <= FAIR_PROB_MAX):
            gate_status = f"GATED:PROB_{over_prob*100:.0f}%_OUTSIDE_20_80"
            gate_note = f"⚠️ Fair OVER prob ({over_prob*100:.1f}%) is outside the calibrated 20–80% band."
        else:
            gate_status = "PROJECTION_ONLY"
    else:
        # No Kalshi market and no user-specified line: show the calibrated 20-80% probability ladder
        center_rung = prob_ladder[min(1, len(prob_ladder) - 1)] if prob_ladder else {"line": round(proj_mean, 1), "prob_pct": 50.0, "label": f"{round(proj_mean, 1)}+"}
        target_line = float(center_rung["line"])
        true_prob = float(center_rung["prob_pct"]) / 100.0
        side = "PROJECTION"
        contract_side = "NONE"
        entry_cents = None
        book_prob = None
        ev_edge = None
        ticker = "Projection only (no open Kalshi contract)"
        milestone_label = str(center_rung["label"])
        gate_status = "GATED:EXPERIMENTAL_STAT_PRA" if is_experimental_stat else "PROJECTION_ONLY"
        if is_experimental_stat:
            gate_note = "🧪 Experimental stat (PRA has +0.3% OOS skill) — projection only."

    real_kalshi_ticker = ticker if has_open_market else None
    is_projection_only = not has_open_market

    fair_cents = max(1, min(99, int(round(true_prob * 100.0))))
    fee_margin = int(round(kalshi_fee_prob(true_prob) * 100.0)) + int(MIN_EDGE_AFTER_FEES_PCT)
    buy_below_cents = max(1, min(95, fair_cents - fee_margin))

    # 5. Optional correlated 2nd leg: ONLY a real ESPN teammate with a REAL open Kalshi ladder (ask > 5¢).
    active_teammates = athlete_espn.get("active_teammates") or []
    valid_teammates = [
        t for t in active_teammates
        if strip_accents(t) != strip_accents(p_name) and clean_token(t) != clean_token(p_name)
    ]
    combo_dict: Dict[str, Any] = {}
    for partner_cand in valid_teammates[:8]:
        try:
            partner_ladders = search_kalshi_markets(partner_cand, sport=sport_u, stat_query="PTS")
        except Exception:
            partner_ladders = []
        partner_ladders = [
            r for r in (partner_ladders or [])
            if normalize_kalshi_market(r) and float(r.get("line", 0)) > 0 and float(r.get("yes_ask") or 0) > 5
        ]
        if not partner_ladders:
            continue
        p_rung = partner_ladders[0]
        p_cents = p_rung.get("yes_ask")
        partner_prob = round(float(p_cents), 1)
        rho = 0.0
        combo_dict = {
            "partner": partner_cand,
            "side": "OVER",
            "line": float(p_rung.get("line")),
            "stat": p_rung.get("stat", "PTS"),
            "team": team_abbr,
            "matchup": matchup,
            "game_date": game_date,
            "ticker": p_rung.get("ticker", ""),
            "limit_cents": p_cents,
            "partner_prob": partner_prob,
            "rho": rho,
            "joint_prob": round(true_prob * (partner_prob / 100.0) * 100.0, 1),
            "boost_pct": 0.0,
            "combo_ev": 0.0,
        }
        break

    sim_payload = {
        "status": "OK",
        "sport": sport_u,
        "player": p_name,
        "target_pick": p_name,
        "team": team_abbr,
        "matchup": matchup,
        "game_date": game_date,
        "stat": stat_norm,
        "line": target_line,
        "side": side,
        "contract_side": contract_side,
        "ticker": ticker,
        "market_url": f"https://kalshi.com/markets/{ticker.lower()}" if str(ticker).startswith("KX") else "https://kalshi.com/markets",
        "limit_cents": entry_cents,
        "fair_cents": fair_cents,
        "buy_below_cents": buy_below_cents,
        "true_prob": round(true_prob * 100.0, 1),
        "ev_edge": ev_edge,
        "p10": p10,
        "p50": p50,
        "p90": p90,
        "mean": round(proj_mean, 2),
        "std": round(proj_std, 2),
        "prob_ladder": prob_ladder,
        "prob_ladder_str": prob_ladder_str,
        "confidence_inputs": confidence_inputs,
        "arch": "VOLUME_SCORER" if stat_norm == "PTS" else "MOVEMENT_SHOOTER",
        "scheme": "PACE_AND_SPACE",
        "scheme_pct": opp_adj_pct,
        "coach_note": f"10k MC adjusted proj ({loc_label}, Opp {opp_adj_pct:+.1f}%) • {matchup}",
        "combo": combo_dict,
        "is_live_orderable": is_orderable,
        "has_open_market": has_open_market,
        "is_experimental": is_experimental_stat,
        "gate_status": gate_status,
        "gate_note": gate_note,
        "real_kalshi_ticker": real_kalshi_ticker,
        "is_projection_only": is_projection_only,
    }

    # 6. Sizing & Order Execution Resolution
    if not is_orderable:
        buy_ladder_str = " · ".join(f"{r['label']} ≤{r['buy_below_cents']}¢" for r in prob_ladder[:3])
        status_msg = gate_note if gate_note else f"Projection only. Target entry prices (after fees + 5% min edge): {buy_ladder_str}"
        exec_res = {
            "status": status_msg,
            "ticker": ticker,
            "contracts": 0,
            "limit_cents": entry_cents,
            "side": side,
            "note": status_msg,
        }
    elif not (sniper_on and live_mode) or real_kalshi_ticker is None or not (10 <= int(entry_cents or 0) <= 90):
        exec_res = {
            "status": f"📝 PAPER ORDER RECORDED (Simulated Fill @ {entry_cents}¢)",
            "ticker": ticker,
            "contracts": 1,
            "limit_cents": entry_cents,
            "side": side,
            "note": f"Paper tracked (1x {side} @ {entry_cents}¢)",
        }
    else:
        from smart_placer import place_kalshi_order
        ord_res = place_kalshi_order(ticker, action="buy", side=contract_side.lower(), count=1, price_cents=int(entry_cents))
        if ord_res.get("ok"):
            exec_res = {
                "status": f"✅ LIVE ORDER FILLED (Order ID: {ord_res.get('order_id', 'FILLED')} | 1x {contract_side} @ {entry_cents}¢)",
                "ticker": ticker,
                "contracts": 1,
                "limit_cents": entry_cents,
                "side": contract_side,
                "note": "Live order executed on Kalshi",
            }
        else:
            exec_res = {
                "status": f"⚠️ ORDER STATUS: {ord_res.get('error', 'Limit resting')}",
                "ticker": ticker,
                "contracts": 1,
                "limit_cents": entry_cents,
                "side": contract_side,
                "note": ord_res.get("error", "Resting"),
            }

    global LAST_SIM_RESULT
    LAST_SIM_RESULT = {
        "sim": sim_payload,
        "exec": exec_res,
        "hits": int(round(true_prob * 10000)),
        "season_avg": raw_season_val,
        "confidence_inputs": confidence_inputs,
        "prob_ladder": prob_ladder,
        "prob_ladder_str": prob_ladder_str,
    }

    card_html = format_video_style_single_card(sim_payload, exec_res, bankroll=bankroll)
    stream_cat = "basketball" if sport_u in ("NBA", "WNBA") else ("american-football" if sport_u in ("NFL", "CFB") else "baseball" if sport_u == "MLB" else "tennis" if sport_u in ("TENNIS", "ATP", "WTA") else "basketball")
    clean_kb = build_casual_video_buttons(stream_url=f"https://streamed.su/watch/{stream_cat}")

    if ladder_summary_lines:
        card_html += f"\n\n🏀 <b>Live Kalshi Milestone Ladder ({stat_norm}):</b>\n" + "\n".join(ladder_summary_lines[:5])
    elif not is_orderable:
        closest = []
        try:
            closest = get_real_open_markets_summary(sport=sport_u, limit=3)
        except Exception:
            pass
        if closest:
            card_html += f"\n\n🏀 <b>Closest Open Kalshi Markets ({stat_norm}):</b>\n" + "\n".join(closest)

    return card_html, clean_kb
