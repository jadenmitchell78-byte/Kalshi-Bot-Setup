#!/usr/bin/env python3
"""
live_nfl_atd_engine.py — Unified NFL Live Anytime TD (ATD) & Multi-TD Prediction Engine
Features:
  1. League-Wide Async Multiplexer (tracks all live NFL games concurrently)
  2. Auto-Team Roster Builder + Live Boxscore Auto-Discovery (never misses surprise actives)
  3. Bayesian "Hot-Hand" & Pass/Run Game-Script Updater (catches focal-point spikes like Rashee Rice)
  4. In-Memory RB Drive Fatigue & Goal-Line Vulture Tracker
  5. Drive-Killer & Momentum Engine (sacks, holding penalties, consecutive incompletions, explosive plays)
  6. All-Position Defensive Matchup & Coverage Funnel Matrix (Man vs. Zone, Pass Rush, Stuff Rate, CB1 Shadow)
  7. Poisson Full-Game Remaining ATD + Automatic 2+ TD (Multi-TD) Escalator + Telegram Push Alerts
  8. Shared live_atd_state.json snapshot for sub-5ms /atd command execution in bot.py
"""

import asyncio
import aiohttp
import html
import json
import math
import os
import requests
import sys
import time
from collections import defaultdict
from difflib import get_close_matches
from typing import Any, Dict, List, Optional, Tuple

# ──────────────────────────────────────────────────────────────────────────────
# ENDPOINTS & LOCAL CACHE PATHS
# ──────────────────────────────────────────────────────────────────────────────
SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={event_id}"
ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/teams/{team_id}/roster"

BASE_DIR = os.path.expanduser("~/peezy_bot")
os.makedirs(BASE_DIR, exist_ok=True)
ODDS_CACHE_FILE = os.path.join(BASE_DIR, "live_atd_odds.json")
STATE_SNAPSHOT_FILE = os.path.join(BASE_DIR, "live_atd_state.json")

DEFAULT_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8886914565:AAEAcvdNq4AMsoRGRhBjy4q6MzkrrPxt1aY")
DEFAULT_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "5992267511")


# ──────────────────────────────────────────────────────────────────────────────
# 1. SHARED ODDS STORE (Read/Written by both bot.py and live_nfl_atd_engine.py)
# ──────────────────────────────────────────────────────────────────────────────
class ATDOddsStore:
    @staticmethod
    def load_odds() -> dict[str, int]:
        if not os.path.exists(ODDS_CACHE_FILE):
            return {}
        try:
            with open(ODDS_CACHE_FILE, "r") as f:
                return json.load(f).get("odds", {})
        except Exception:
            return {}

    @staticmethod
    def save_odds(new_odds: dict[str, int], merge: bool = True) -> dict[str, int]:
        current = ATDOddsStore.load_odds() if merge else {}
        current.update(new_odds)
        payload = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "odds": current}
        with open(ODDS_CACHE_FILE, "w") as f:
            json.dump(payload, f, indent=2)
        return current

    @staticmethod
    def clear_odds() -> bool:
        payload = {"updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "odds": {}}
        try:
            with open(ODDS_CACHE_FILE, "w") as f:
                json.dump(payload, f, indent=2)
            return True
        except Exception:
            return False

    @staticmethod
    def implied_pct(american_odds: int) -> float:
        if american_odds > 0:
            return (100.0 / (american_odds + 100.0)) * 100.0
        return ((-american_odds) / ((-american_odds) + 100.0)) * 100.0

    @staticmethod
    def parse_manual_input(raw_arg: str, active_roster_names: list[str]) -> dict[str, int]:
        parsed = {}
        entries = [x.strip() for x in raw_arg.split(",") if x.strip()]
        last_name_map = {name.split()[-1].lower(): name for name in active_roster_names if name}
        full_name_map = {name.lower(): name for name in active_roster_names if name}

        for entry in entries:
            parts = entry.rsplit(" ", 1)
            if len(parts) != 2:
                continue
            name_q, price_str = parts[0].strip().lower(), parts[1].strip()
            try:
                price = int(price_str.replace("+", ""))
            except ValueError:
                continue

            matched = full_name_map.get(name_q) or last_name_map.get(name_q)
            if not matched:
                matches = get_close_matches(name_q, list(last_name_map.keys()), n=1, cutoff=0.6)
                matched = last_name_map[matches[0]] if matches else parts[0].strip().title()
            parsed[matched] = price
        return parsed


# ──────────────────────────────────────────────────────────────────────────────
# 2. DEFENSIVE MATCHUP & COVERAGE FUNNEL ENGINE (All Skill Positions)
# ──────────────────────────────────────────────────────────────────────────────
class DefensiveMatchupEngine:
    DEFENSE_PROFILES = {
        "DEN": {
            "rz_td_multiplier": 0.88,
            "man_coverage_rate": 0.78,
            "pass_rush_win_rate": 0.74,
            "interior_stuff_rate": 0.38,
            "cb1_shadow_lockdown": 0.88,
            "te_coverage_grade": 0.42
        },
        "KC": {
            "rz_td_multiplier": 0.72,
            "man_coverage_rate": 0.68,
            "pass_rush_win_rate": 0.70,
            "interior_stuff_rate": 0.72,
            "cb1_shadow_lockdown": 0.75,
            "te_coverage_grade": 0.55
        },
        "MIN": {
            "rz_td_multiplier": 0.65,
            "man_coverage_rate": 0.35,
            "pass_rush_win_rate": 0.82,
            "interior_stuff_rate": 0.74,
            "cb1_shadow_lockdown": 0.65,
            "te_coverage_grade": 0.78
        },
        "BAL": {
            "rz_td_multiplier": 0.82,
            "man_coverage_rate": 0.45,
            "pass_rush_win_rate": 0.68,
            "interior_stuff_rate": 0.78,
            "cb1_shadow_lockdown": 0.62,
            "te_coverage_grade": 0.60
        },
        "DET": {
            "rz_td_multiplier": 0.85,
            "man_coverage_rate": 0.72,
            "pass_rush_win_rate": 0.65,
            "interior_stuff_rate": 0.75,
            "cb1_shadow_lockdown": 0.55,
            "te_coverage_grade": 0.50
        },
        "CAR": {
            "rz_td_multiplier": 1.25,
            "man_coverage_rate": 0.40,
            "pass_rush_win_rate": 0.30,
            "interior_stuff_rate": 0.22,
            "cb1_shadow_lockdown": 0.40,
            "te_coverage_grade": 0.35
        },
        "DEFAULT": {
            "rz_td_multiplier": 1.00,
            "man_coverage_rate": 0.50,
            "pass_rush_win_rate": 0.50,
            "interior_stuff_rate": 0.50,
            "cb1_shadow_lockdown": 0.50,
            "te_coverage_grade": 0.50
        }
    }

    @classmethod
    def get_profile(cls, opp_abbr: str) -> dict:
        return cls.DEFENSE_PROFILES.get(opp_abbr.upper(), cls.DEFENSE_PROFILES["DEFAULT"])

    @classmethod
    def get_positional_matchup_multipliers(cls, opp_abbr: str, yards_to_goal: int) -> dict[str, float]:
        p = cls.get_profile(opp_abbr)
        man = p["man_coverage_rate"]
        prwr = p["pass_rush_win_rate"]
        stuff = p["interior_stuff_rate"]
        cb1 = p["cb1_shadow_lockdown"]
        te_cov = p["te_coverage_grade"]

        mults = {}

        # QB Scramble / Sneak
        qb_scramble = 1.0 + ((man - 0.50) * 0.65) + ((prwr - 0.50) * 0.45)
        if yards_to_goal <= 2:
            qb_sneak = 1.0 - ((stuff - 0.50) * 0.30)
            mults["QB"] = round(max(0.65, qb_scramble * 0.5 + qb_sneak * 0.5), 3)
        else:
            mults["QB"] = round(max(0.65, min(1.65, qb_scramble)), 3)

        # RB1 & RB2 Vulture (inversely correlated with interior stuff rate)
        run_funnel = 1.0 - ((stuff - 0.50) * 0.85)
        mults["RB1"] = round(max(0.60, min(1.50, run_funnel)), 3)
        mults["RB2_VULTURE"] = round(max(0.55, min(1.60, run_funnel * 1.05)), 3)

        # RB3 Pass-Catching Back (boosted by high pass rush + strong run stuff)
        checkdown = 1.0 + ((prwr - 0.50) * 0.60) + ((stuff - 0.50) * 0.40)
        mults["RB3_PASS"] = round(max(0.70, min(1.55, checkdown)), 3)

        # WR1 (Boundary X — penalized by elite shadow CB1 like Surtain)
        wr1_mod = 1.0 + ((man - 0.50) * 0.35) - ((cb1 - 0.50) * 0.70) + ((stuff - 0.50) * 0.25)
        mults["WR1"] = round(max(0.55, min(1.45, wr1_mod)), 3)

        # WR2 (Slot / Motion Z — absorbs targets funneled away from CB1 shadow)
        wr2_mod = 1.0 + ((cb1 - 0.50) * 0.45) + ((0.50 - man) * 0.30) + ((stuff - 0.50) * 0.20)
        mults["WR2"] = round(max(0.65, min(1.50, wr2_mod)), 3)

        # TE1 (Seam / Red-Zone Pop — thrives vs weak TE coverage, CB1 shadow on WR1, and stuffed runs)
        te_mod = (
            1.0
            - ((te_cov - 0.50) * 0.75)
            + ((cb1 - 0.50) * 0.35)
            + ((stuff - 0.50) * 0.30)
            + ((0.50 - man) * 0.25)
        )
        mults["TE1"] = round(max(0.65, min(1.60, te_mod)), 3)

        # TE2_LEAK (Noah Gray / Isaiah Likely / Colby Parkinson — thrives vs aggressive blitz and heavy box run-commit)
        te2_mod = 1.0 + ((prwr - 0.50) * 0.45) + ((stuff - 0.50) * 0.40) + ((cb1 - 0.50) * 0.25)
        mults["TE2_LEAK"] = round(max(0.65, min(1.80, te2_mod)), 3)

        return mults


# ──────────────────────────────────────────────────────────────────────────────
# 3. IN-MEMORY RB DRIVE FATIGUE & VULTURE TRACKER
# ──────────────────────────────────────────────────────────────────────────────
class DriveFatigueTracker:
    def __init__(self):
        self.current_drive_id = None
        self.reset_drive()

    def reset_drive(self):
        self.touches_in_drive = defaultdict(int)
        self.consecutive_touches = defaultdict(int)
        self.yards_in_drive = defaultdict(int)
        self.processed_play_ids = set()

    def parse_drive_plays(self, summary_json: dict, roster: list[dict]) -> dict:
        drives = summary_json.get("drives", {})
        current_drive = drives.get("current", {})
        drive_id = current_drive.get("id")

        if not drive_id:
            return {"modifiers": {}, "vulture_alert": False}

        if drive_id != self.current_drive_id:
            self.current_drive_id = drive_id
            self.reset_drive()

        roster_names = [p["name"] for p in roster]
        role_map = {p["name"]: p["role"] for p in roster}

        for play in current_drive.get("plays", []):
            play_id = play.get("id")
            if not play_id or play_id in self.processed_play_ids:
                continue

            play_text = play.get("text", "")
            play_type = play.get("type", {}).get("text", "").lower()
            yards = int(play.get("statYardage", 0))

            if any(x in play_type for x in ["timeout", "end period", "kickoff", "penalty"]):
                self.processed_play_ids.add(play_id)
                continue

            touched_player = self._extract_carrier(play_text, roster_names)
            if touched_player:
                self.touches_in_drive[touched_player] += 1
                self.yards_in_drive[touched_player] += max(0, yards)
                for p in list(self.consecutive_touches.keys()):
                    if p != touched_player:
                        self.consecutive_touches[p] = 0
                self.consecutive_touches[touched_player] += 1
            else:
                for p in list(self.consecutive_touches.keys()):
                    self.consecutive_touches[p] = 0

            self.processed_play_ids.add(play_id)

        return self._compute_rb_fatigue(roster_names, role_map)

    @staticmethod
    def _extract_carrier(text: str, roster_names: list[str]) -> str | None:
        target_segment = text.split("pass from")[0] if "pass from" in text else text
        for full_name in roster_names:
            parts = full_name.split()
            if len(parts) >= 2:
                short_name = f"{parts[0][0]}.{parts[-1]}"
                if full_name.lower() in target_segment.lower() or short_name.lower() in target_segment.lower():
                    return full_name
        return None

    def _compute_rb_fatigue(self, roster_names: list[str], role_map: dict[str, str]) -> dict:
        modifiers = {name: 1.0 for name in roster_names}
        rb1_fatigued = False

        for name in roster_names:
            role = role_map.get(name, "")
            if not role.startswith("RB"):
                continue

            consec = self.consecutive_touches[name]
            total_touches = self.touches_in_drive[name]
            drive_yds = self.yards_in_drive[name]

            penalty = 1.0
            if consec >= 3:
                penalty *= 0.60
                if role == "RB1":
                    rb1_fatigued = True
            elif consec == 2:
                penalty *= 0.85

            if drive_yds >= 35 or total_touches >= 5:
                penalty *= 0.70
                if role == "RB1":
                    rb1_fatigued = True

            modifiers[name] = round(max(0.40, penalty), 3)

        return {"modifiers": modifiers, "vulture_alert": rb1_fatigued}


# ──────────────────────────────────────────────────────────────────────────────
# 4. DRIVE MOMENTUM & DRIVE-KILLER ENGINE (Sacks, Penalties, Incompletions)
# ──────────────────────────────────────────────────────────────────────────────
class DriveMomentumAnalyzer:
    @staticmethod
    def analyze_drive_momentum(current_drive: dict, down: int, distance: int) -> dict:
        plays = current_drive.get("plays", [])
        if not plays:
            return {
                "drive_td_mult": 1.0,
                "pos_modifiers": {},
                "is_stalled": False,
                "momentum_state": "⚡ ON SCHEDULE",
                "reason": "Drive Start"
            }

        recent_plays = plays[-4:]
        sacks_recent = 0
        consec_incompletions = 0
        off_penalties_recent = 0
        tfl_recent = 0
        explosive_gains = 0
        consec_completions = 0
        def_bailout_penalty = False

        for play in recent_plays:
            p_type = play.get("type", {}).get("text", "").lower()
            p_text = play.get("text", "").lower()
            yds = int(play.get("statYardage", 0))

            if "timeout" in p_type or "end period" in p_type:
                continue

            # Flag: check if penalty was declined or offsetting
            is_penalty_declined = "declined" in p_text or "offsetting" in p_text

            if "sack" in p_type or "sacked" in p_text:
                sacks_recent += 1
                consec_incompletions = 0
                consec_completions = 0
            elif "incomplete" in p_type or "incomplete" in p_text:
                consec_incompletions += 1
                consec_completions = 0
            elif ("penalty" in p_type or "penalty" in p_text or "flag" in p_text) and not is_penalty_declined:
                if any(k in p_text for k in [
                    "offensive holding", "holding, offense", "false start", "illegal formation",
                    "offensive pass interference", "pass interference, offense",
                    "intentional grounding", "delay of game", "illegal shift", "illegal motion",
                    "chop block", "ineligible receiver", "illegal block"
                ]):
                    off_penalties_recent += 1
                elif any(k in p_text for k in [
                    "defensive pass interference", "pass interference, defense",
                    "defensive holding", "holding, defense",
                    "roughing the passer", "unnecessary roughness", "neutral zone infraction",
                    "encroachment", "illegal contact", "face mask"
                ]):
                    def_bailout_penalty = True
                    consec_incompletions = 0
            elif "rush" in p_type and yds <= -2:
                tfl_recent += 1
                consec_incompletions = 0
            else:
                consec_incompletions = 0
                if "pass" in p_type and yds > 0:
                    consec_completions += 1
                if yds >= 15:
                    explosive_gains += 1

        drive_td_mult = 1.0
        pos_mods = {
            "RB1": 1.0, "RB2_VULTURE": 1.0, "RB3_PASS": 1.0,
            "WR1": 1.0, "WR2": 1.0, "TE1": 1.0, "TE2_LEAK": 1.0, "QB": 1.0
        }
        reasons = []
        is_stalled = False

        if sacks_recent > 0:
            drive_td_mult *= (0.55 ** sacks_recent)
            pos_mods["RB1"] *= 0.30
            pos_mods["RB2_VULTURE"] *= 0.20
            pos_mods["RB3_PASS"] *= 1.55
            pos_mods["TE1"] *= 1.25
            pos_mods["TE2_LEAK"] *= 1.15
            pos_mods["WR1"] *= 1.25
            pos_mods["QB"] *= 1.35
            reasons.append(f"Sack (-{sacks_recent})")
            is_stalled = True

        if off_penalties_recent > 0:
            drive_td_mult *= 0.62
            pos_mods["RB1"] *= 0.35
            pos_mods["RB2_VULTURE"] *= 0.20
            pos_mods["WR1"] *= 1.30
            pos_mods["WR2"] *= 1.35
            pos_mods["TE1"] *= 1.25
            pos_mods["TE2_LEAK"] *= 1.15
            pos_mods["RB3_PASS"] *= 1.55
            reasons.append("Off. Penalty Setback")
            is_stalled = True

        if consec_incompletions >= 2:
            drive_td_mult *= 0.58
            pos_mods["RB1"] *= 0.35
            pos_mods["RB2_VULTURE"] *= 0.15
            pos_mods["RB3_PASS"] *= 1.75
            pos_mods["TE1"] *= 1.35
            pos_mods["TE2_LEAK"] *= 1.20
            pos_mods["QB"] *= 1.30
            reasons.append(f"{consec_incompletions} Straight Incomp.")
            is_stalled = True
        elif consec_incompletions == 1 and down == 3 and distance >= 7:
            drive_td_mult *= 0.72
            reasons.append("3rd & Long after Incomp.")

        if tfl_recent > 0:
            drive_td_mult *= 0.78
            pos_mods["RB1"] *= 0.65
            pos_mods["RB2_VULTURE"] *= 0.65
            pos_mods["TE1"] *= 1.20
            pos_mods["TE2_LEAK"] *= 1.25
            pos_mods["WR1"] *= 1.15
            reasons.append("Run Stuffed (TFL)")

        if not is_stalled:
            if def_bailout_penalty:
                drive_td_mult *= 1.25
                pos_mods["RB1"] *= 1.20
                pos_mods["RB2_VULTURE"] *= 1.15
                pos_mods["TE2_LEAK"] *= 1.15
                reasons.append("Def. Penalty Auto 1st")
            if explosive_gains > 0:
                drive_td_mult *= 1.22
                pos_mods["RB1"] *= 1.15
                pos_mods["WR1"] *= 1.15
                pos_mods["TE2_LEAK"] *= 1.10
                reasons.append("Explosive 15+ Yd Gain")
            if consec_completions >= 3:
                drive_td_mult *= 1.15
                pos_mods["WR1"] *= 1.20
                pos_mods["TE1"] *= 1.20
                pos_mods["TE2_LEAK"] *= 1.15
                reasons.append(f"Hot Passer ({consec_completions} Str. Comp)")

        if is_stalled or (down >= 3 and distance >= 9):
            state = "🛑 STALLED (FG FUNNEL)"
            is_stalled = True
        elif drive_td_mult >= 1.15:
            state = "🔥 SURGING (HURRY-UP)"
        else:
            state = "⚡ ON SCHEDULE"

        return {
            "drive_td_mult": round(max(0.20, min(1.45, drive_td_mult)), 3),
            "pos_modifiers": pos_mods,
            "is_stalled": is_stalled,
            "momentum_state": state,
            "reason": ", ".join(reasons) if reasons else "Standard Progression"
        }


# ──────────────────────────────────────────────────────────────────────────────
# 5. BAYESIAN "HOT-HAND" & PASS/RUN GAME-SCRIPT UPDATER
# ──────────────────────────────────────────────────────────────────────────────
class GamePlanBayesianUpdater:
    @staticmethod
    def apply_live_gameplan_shifts(roster: list[dict], drive_plays: list[dict]) -> list[dict]:
        pass_plays = 0
        rush_plays = 0
        drive_targets = {p["name"]: 0 for p in roster}

        for play in drive_plays:
            p_type = play.get("type", {}).get("text", "").lower()
            p_text = play.get("text", "")
            target_seg = p_text.split("pass from")[0] if "pass from" in p_text else p_text

            if "pass" in p_type or "sack" in p_type:
                pass_plays += 1
                for p in roster:
                    parts = p["name"].split()
                    short = f"{parts[0][0]}.{parts[-1]}" if len(parts) >= 2 else p["name"]
                    if p["name"].lower() in target_seg.lower() or short.lower() in target_seg.lower():
                        drive_targets[p["name"]] += 1
            elif "rush" in p_type:
                rush_plays += 1

        total_scrimmage = max(1, pass_plays + rush_plays)
        drive_pass_rate = pass_plays / total_scrimmage

        updated_roster = []
        total_weight = 0.0

        for p in roster:
            name = p["name"]
            role = p["role"]
            seed_share = p.get("seed_base_share", p["base_share"])

            script_mult = 1.0
            if total_scrimmage >= 3:
                if drive_pass_rate >= 0.68:
                    if role in ("WR1", "WR2", "TE1", "TE2_LEAK", "RB3_PASS"):
                        script_mult = 1.30
                    elif role in ("RB1", "RB2_VULTURE"):
                        script_mult = 0.72
                elif drive_pass_rate <= 0.35:
                    if role in ("RB1", "RB2_VULTURE"):
                        script_mult = 1.35
                    elif role in ("WR1", "WR2"):
                        script_mult = 0.75

            d_tgts = drive_targets.get(name, 0)
            game_tgts = p.get("live_targets", 0)
            game_carries = p.get("live_carries", 0)
            hot_hand_mult = 1.0

            if role in ("WR1", "WR2", "TE1", "TE2_LEAK"):
                hot_hand_mult = 1.0 + (d_tgts * 0.28) + (min(8, game_tgts) * 0.06)
            elif role.startswith("RB") and game_carries >= 4:
                hot_hand_mult = 1.0 + (min(12, game_carries) * 0.04)

            posterior = seed_share * script_mult * hot_hand_mult
            total_weight += posterior
            updated_roster.append({
                **p,
                "seed_base_share": seed_share,
                "dynamic_base_share": posterior,
                "drive_targets": d_tgts,
                "script_mult": round(script_mult * hot_hand_mult, 2)
            })

        for p in updated_roster:
            p["base_share"] = round(p["dynamic_base_share"] / total_weight, 4) if total_weight > 0 else p["base_share"]

        return updated_roster


# ──────────────────────────────────────────────────────────────────────────────
# 5.5 AWS NEXT GEN STATS (NGS) DRIVE TELEMETRY & TE2 LEAK ENGINE
# ──────────────────────────────────────────────────────────────────────────────
class AWSNextGenDriveEngine:
    """
    Simulates NFL Next Gen Stats (AWS NGS) drive telemetry directly from ESPN play streams:
      1. Drive Pace & Velocity: Calculates sec/play & tempo state (⚡ HURRY-UP, ⏱️ BALANCED, ⏳ CLOCK BLEED).
      2. Offensive Personnel Inference: 11 Personnel (3-WR), 12 Personnel (2-TE), 13 Personnel (Jumbo 3-TE), 21/22 Heavy.
      3. Cumulative Drive EPA (Expected Points Added) & Next Gen Expected Touchdown (xTD).
      4. Noah Gray / TE2 Goal-Line Leak Factor: In 12/13 heavy personnel inside the 10-yard line,
         defenses bracket TE1 (Kelce) and stack the box, triggering a 1.95x surge on TE2 leak designs.
      5. Peak Ball-Carrier Top Speed (mph): Physics-calibrated proxy for explosive chunk gains.
    """

    @staticmethod
    def analyze_drive_telemetry(current_drive: dict, yards_to_goal: int, down: int, distance: int, roster: list[dict] = None) -> dict:
        plays = current_drive.get("plays", [])
        play_count = len(plays)
        drive_yards = int(current_drive.get("yards", 0) or 0)

        # 1. Drive Time & Pace (sec/play)
        time_elapsed_obj = current_drive.get("timeElapsed", {})
        display_clock = time_elapsed_obj.get("displayValue", "") if isinstance(time_elapsed_obj, dict) else str(time_elapsed_obj)
        total_seconds = 0
        if ":" in display_clock:
            try:
                m_part, s_part = display_clock.split(":")
                total_seconds = int(m_part) * 60 + int(s_part)
            except Exception:
                total_seconds = 0

        if total_seconds > 0 and play_count > 0:
            sec_per_play = round(total_seconds / play_count, 1)
        elif play_count > 0:
            sec_per_play = 27.5
        else:
            sec_per_play = 0.0

        if sec_per_play > 0 and sec_per_play <= 22.0:
            tempo_state = "⚡ HURRY-UP (2-MIN DRILL)"
            tempo_mult = 1.18
        elif sec_per_play > 34.0:
            tempo_state = "⏳ CLOCK BLEED / BALL CONTROL"
            tempo_mult = 0.88
        else:
            tempo_state = "⏱️ BALANCED TEMPO"
            tempo_mult = 1.00

        # 2. Cumulative Drive EPA & Expected Touchdown (xTD)
        start_ytg = int(current_drive.get("start", {}).get("yardLine", 75) or 75)
        start_ep = max(-1.5, 6.0 - 0.065 * start_ytg)
        current_ep = max(-1.5, 6.0 - 0.065 * yards_to_goal)
        drive_epa = round(current_ep - start_ep, 2)

        if yards_to_goal <= 3:
            xtd_base = 0.82
        elif yards_to_goal <= 10:
            xtd_base = 0.65
        elif yards_to_goal <= 20:
            xtd_base = 0.48
        elif yards_to_goal <= 40:
            xtd_base = 0.32
        else:
            xtd_base = max(0.08, 0.28 - (yards_to_goal - 40) * 0.0035)

        epa_factor = 1.0 + max(-0.25, min(0.35, drive_epa * 0.06))
        xtd = round(min(0.96, max(0.04, xtd_base * epa_factor * tempo_mult)), 3)

        # 3. Personnel Grouping Inference
        text_corpus = " ".join([p.get("text", "") for p in plays[-5:]]).lower()
        if yards_to_goal <= 10:
            if any(k in text_corpus for k in ["jumbo", "heavy", "goal line", "tight end", "fullback"]) or down in (1, 2):
                personnel = "12 Personnel (2-TE Heavy)"
                is_heavy_personnel = True
            else:
                personnel = "11 Personnel (3-WR Spread RedZone)"
                is_heavy_personnel = False
        elif any(k in text_corpus for k in ["shotgun", "no huddle", "empty", "spread"]):
            personnel = "11 Personnel (3-WR Spread)"
            is_heavy_personnel = False
        elif any(k in text_corpus for k in ["tight end", "fullback", "i-formation"]):
            personnel = "12 Personnel (2-TE, 1-RB)"
            is_heavy_personnel = True
        else:
            personnel = "11 Personnel (Standard 1-RB, 1-TE, 3-WR)"
            is_heavy_personnel = False

        # 4. Noah Gray / TE2 Goal-Line Leak Factor
        te2_leak_active = False
        te2_leak_mult = 1.0
        if yards_to_goal <= 10 and is_heavy_personnel:
            te2_leak_active = True
            te2_leak_mult = 1.95
            leak_reason = "Heavy 12/13 personnel keys run/TE1 bracket; TE2 leak open in flat/seam"
        elif yards_to_goal <= 15:
            te2_leak_active = True
            te2_leak_mult = 1.35
            leak_reason = "High-low TE flood concept inside the 15"
        else:
            leak_reason = "Standard field spacing"

        # 5. Peak Ball-Carrier Top Speed (mph)
        top_speed_mph = 0.0
        top_speed_desc = "None"
        for p in plays:
            yds = int(p.get("statYardage", 0) or 0)
            if yds >= 12:
                calc_speed = round(min(22.4, 16.5 + math.log(max(1, yds - 5)) * 1.65), 1)
                if calc_speed > top_speed_mph:
                    top_speed_mph = calc_speed
                    text = p.get("text", "")
                    carrier = text.split("pass from")[0] if "pass from" in text else text
                    carrier_name = carrier.split(" for ")[0].replace("pass complete to ", "").strip()
                    top_speed_carrier = carrier_name[:20] if carrier_name else "Ball Carrier"
                    top_speed_desc = f"{top_speed_carrier} — {calc_speed} mph ({yds}-yd gain)"

        pos_modifiers = {
            "TE2_LEAK": te2_leak_mult
        }

        return {
            "sec_per_play": sec_per_play,
            "tempo_state": tempo_state,
            "personnel": personnel,
            "is_heavy_personnel": is_heavy_personnel,
            "drive_epa": drive_epa,
            "xtd": xtd,
            "te2_leak_active": te2_leak_active,
            "te2_leak_mult": te2_leak_mult,
            "leak_reason": leak_reason,
            "peak_speed_mph": top_speed_mph,
            "peak_speed_desc": top_speed_desc,
            "play_count": play_count,
            "drive_yards": drive_yards,
            "elapsed_time": display_clock or f"{total_seconds}s",
            "pos_modifiers": pos_modifiers
        }


# ──────────────────────────────────────────────────────────────────────────────
# 6. AUTO TEAM ROSTER BUILDER & LIVE BOXSCORE SYNC (All 32 Teams)
# ──────────────────────────────────────────────────────────────────────────────
class AutoTeamRosterBuilder:
    def __init__(self):
        self.team_cache = {}

    async def get_or_build_roster(
        self, session: aiohttp.ClientSession, team_id: str, team_abbr: str, summary_json: dict
    ) -> list[dict]:
        team_abbr = team_abbr.upper()
        if team_abbr not in self.team_cache and team_id:
            self.team_cache[team_abbr] = await self._fetch_initial_team_skill(session, team_id)

        current = self.team_cache.get(team_abbr, [])
        self.team_cache[team_abbr] = self._sync_with_live_boxscore(summary_json, team_abbr, current)
        return self.team_cache[team_abbr]

    async def _fetch_initial_team_skill(self, session: aiohttp.ClientSession, team_id: str) -> list[dict]:
        url = ROSTER_URL.format(team_id=team_id)
        pos_buckets = defaultdict(list)

        try:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    for group in data.get("athletes", []):
                        if group.get("position", "").lower() in ("injuredreserve", "pup", "practicesquad"):
                            continue
                        for ath in group.get("items", []):
                            status = ath.get("status", {}).get("type", "").lower()
                            if status in ("injured-reserve", "out", "pup", "inactive", "suspended"):
                                continue
                            pos = ath.get("position", {}).get("abbreviation", "").upper()
                            if pos in ("QB", "RB", "FB", "WR", "TE"):
                                pos_buckets[pos].append(ath.get("displayName"))
        except Exception:
            pass

        roster = []
        rbs = pos_buckets["RB"] + pos_buckets["FB"]
        wrs = pos_buckets["WR"]
        tes = pos_buckets["TE"]
        qbs = pos_buckets["QB"]

        if len(rbs) >= 1: roster.append({"name": rbs[0], "role": "RB1", "base_share": 0.28, "seed_base_share": 0.28})
        if len(rbs) >= 2: roster.append({"name": rbs[1], "role": "RB2_VULTURE", "base_share": 0.10, "seed_base_share": 0.10})
        if len(rbs) >= 3: roster.append({"name": rbs[2], "role": "RB3_PASS", "base_share": 0.05, "seed_base_share": 0.05})
        if len(wrs) >= 1: roster.append({"name": wrs[0], "role": "WR1", "base_share": 0.20, "seed_base_share": 0.20})
        if len(wrs) >= 2: roster.append({"name": wrs[1], "role": "WR2", "base_share": 0.12, "seed_base_share": 0.12})
        if len(tes) >= 1: roster.append({"name": tes[0], "role": "TE1", "base_share": 0.13, "seed_base_share": 0.13})
        if len(tes) >= 2: roster.append({"name": tes[1], "role": "TE2_LEAK", "base_share": 0.06, "seed_base_share": 0.06})
        if len(qbs) >= 1: roster.append({"name": qbs[0], "role": "QB", "base_share": 0.06, "seed_base_share": 0.06})

        return roster

    def _sync_with_live_boxscore(self, summary_json: dict, team_abbr: str, current_roster: list[dict]) -> list[dict]:
        roster_map = {p["name"]: p for p in current_roster}
        box_players = summary_json.get("boxscore", {}).get("players", [])

        for team_block in box_players:
            if team_block.get("team", {}).get("abbreviation", "").upper() != team_abbr:
                continue

            for stat_cat in team_block.get("statistics", []):
                cat = stat_cat.get("name", "").lower()
                if cat not in ("rushing", "receiving", "passing"):
                    continue

                for idx, entry in enumerate(stat_cat.get("athletes", [])):
                    name = entry.get("athlete", {}).get("displayName")
                    stats = entry.get("stats", [])
                    if not name or not stats:
                        continue

                    touches = int(stats[0]) if str(stats[0]).isdigit() else 1

                    if name not in roster_map and touches > 0:
                        if cat == "passing":
                            role, base = "QB", 0.08
                        elif cat == "rushing":
                            role = "RB1" if idx == 0 else "RB2_VULTURE"
                            base = 0.26 if idx == 0 else 0.10
                        else:
                            role = "WR1" if idx == 0 else ("WR2" if idx == 1 else "TE2_LEAK")
                            base = 0.20 if idx == 0 else (0.12 if idx == 1 else 0.07)

                        roster_map[name] = {
                            "name": name,
                            "role": role,
                            "base_share": base,
                            "seed_base_share": base,
                            "live_targets": touches if cat == "receiving" else 0,
                            "live_carries": touches if cat == "rushing" else 0
                        }
                    elif name in roster_map:
                        if cat == "receiving":
                            roster_map[name]["live_targets"] = touches
                        elif cat == "rushing":
                            roster_map[name]["live_carries"] = touches

        return list(roster_map.values())


# ──────────────────────────────────────────────────────────────────────────────
# 7. MASTER 6-FACTOR SITUATIONAL ATD SHARE CALCULATOR
# ──────────────────────────────────────────────────────────────────────────────
def calculate_situational_atd_shares(
    players: list[dict],
    yards_to_goal: int,
    down: int,
    distance: int,
    fatigue_data: dict,
    opp_abbr: str = "DEFAULT",
    momentum_pos_mods: dict = None
) -> list[dict]:
    if yards_to_goal <= 2:
        zone_weights = {
            "RB1": 1.65, "RB2_VULTURE": 1.85, "RB3_PASS": 0.30,
            "TE1": 0.90, "TE2_LEAK": 1.45, "WR1": 0.55, "WR2": 0.35, "QB": 1.70
        }
    elif yards_to_goal <= 9:
        zone_weights = {
            "RB1": 1.30, "RB2_VULTURE": 1.25, "RB3_PASS": 0.75,
            "TE1": 1.35, "TE2_LEAK": 1.65, "WR1": 1.15, "WR2": 0.70, "QB": 1.15
        }
    elif yards_to_goal <= 20:
        zone_weights = {
            "RB1": 0.95, "RB2_VULTURE": 0.65, "RB3_PASS": 1.15,
            "TE1": 1.30, "TE2_LEAK": 1.10, "WR1": 1.25, "WR2": 1.15, "QB": 0.85
        }
    else:
        zone_weights = {
            "RB1": 0.85, "RB2_VULTURE": 0.40, "RB3_PASS": 1.10,
            "TE1": 1.05, "TE2_LEAK": 0.70, "WR1": 1.20, "WR2": 1.45, "QB": 0.60
        }

    dd_weights = {r: 1.0 for r in zone_weights}
    if down == 3 and distance >= 4:
        dd_weights.update({
            "RB1": 0.45, "RB2_VULTURE": 0.20, "RB3_PASS": 2.10,
            "TE1": 1.45, "TE2_LEAK": 1.25, "WR1": 1.35, "WR2": 1.20, "QB": 1.40
        })
    elif down in (3, 4) and distance <= 2:
        dd_weights.update({
            "RB1": 1.35, "RB2_VULTURE": 1.65, "RB3_PASS": 0.25,
            "TE1": 1.10, "TE2_LEAK": 1.30, "WR1": 0.65, "WR2": 0.50, "QB": 1.80
        })
    elif down == 1 and yards_to_goal <= 5:
        dd_weights.update({"RB1": 1.40, "RB2_VULTURE": 1.25, "TE2_LEAK": 1.40})

    def_weights = DefensiveMatchupEngine.get_positional_matchup_multipliers(opp_abbr, yards_to_goal)
    fatigue_mods = fatigue_data.get("modifiers", {})
    vulture_alert = fatigue_data.get("vulture_alert", False)
    mom_mods = momentum_pos_mods or {}

    raw_scores = []
    total_score = 0.0

    for p in players:
        name = p["name"]
        role = p["role"]
        base = p["base_share"]

        z_mod = zone_weights.get(role, 1.0)
        d_mod = dd_weights.get(role, 1.0)
        f_mod = fatigue_mods.get(name, 1.0)
        m_mod = def_weights.get(role, 1.0)
        k_mod = mom_mods.get(role, 1.0)

        if vulture_alert and role == "RB2_VULTURE":
            f_mod *= 1.55
        elif vulture_alert and role in ("TE1", "TE2_LEAK", "RB3_PASS"):
            f_mod *= 1.20

        adjusted = base * z_mod * d_mod * f_mod * m_mod * k_mod
        total_score += adjusted

        raw_scores.append({
            **p,
            "raw_adjusted": adjusted,
            "fatigue_mult": round(f_mod, 2),
            "matchup_mult": m_mod
        })

    results = []
    for item in raw_scores:
        cond_prob = item["raw_adjusted"] / total_score if total_score > 0 else 0
        results.append({
            **item,
            "conditional_td_share": round(cond_prob, 4)
        })

    return sorted(results, key=lambda x: x["conditional_td_share"], reverse=True)


# ──────────────────────────────────────────────────────────────────────────────
# 8. POISSON FULL-GAME ATD + 2+ TD ESCALATOR & TELEGRAM PUSH ALERTS
# ──────────────────────────────────────────────────────────────────────────────
class FullGameATDAndAlertEngine:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None, team_base_td_per_drive: float = 0.25, alert_cooldown_sec: int = 180):
        self.bot_token = bot_token or DEFAULT_BOT_TOKEN
        self.chat_id = chat_id or DEFAULT_CHAT_ID
        self.team_base_td_rate = team_base_td_per_drive
        self.cooldown_sec = alert_cooldown_sec
        self.last_alert_ts = {}
        self.player_td_counts = defaultdict(int)

    def update_td_counts(self, summary_json: dict, roster_names: list[str]):
        counts = defaultdict(int)
        for play in summary_json.get("scoringPlays", []):
            if play.get("scoringType", {}).get("abbreviation") == "TD":
                text = play.get("text", "")
                scrimmage_seg = text.split("pass from")[0] if "pass from" in text else text
                for name in roster_names:
                    parts = name.split()
                    short = f"{parts[0][0]}.{parts[-1]}" if len(parts) >= 2 else name
                    if name.lower() in scrimmage_seg.lower() or short.lower() in scrimmage_seg.lower():
                        counts[name] += 1
        for name, c in counts.items():
            self.player_td_counts[name] = c

    @staticmethod
    def get_seconds_remaining(summary_json: dict) -> int:
        header = summary_json.get("header", {}).get("competitions", [{}])[0]
        status = header.get("status", {})
        period = int(status.get("period", 1))
        clock_str = status.get("displayClock", "15:00")
        try:
            mins, secs = map(int, clock_str.split(":"))
            period_secs = mins * 60 + secs
        except ValueError:
            period_secs = 0
        rem_quarters = max(0, 4 - period)
        return (rem_quarters * 900) + period_secs

    def compute_full_game_probabilities(
        self,
        summary_json: dict,
        drive_td_prob: float,
        situational_shares: list[dict],
        market_books: dict[str, int] = None
    ) -> list[dict]:
        secs_left = self.get_seconds_remaining(summary_json)
        rem_future_drives = max(0.0, (secs_left / 340.0) - 0.5)

        enriched = []
        for p in situational_shares:
            name = p["name"]
            base_share = p["base_share"]
            cond_share = p["conditional_td_share"]
            tds_already = self.player_td_counts.get(name, 0)

            p_curr_drive = drive_td_prob * cond_share
            lambda_future = rem_future_drives * self.team_base_td_rate * base_share
            p_zero_future = math.exp(-lambda_future)

            p_next_td_full_game = 1.0 - ((1.0 - p_curr_drive) * p_zero_future)
            fair_american = self._prob_to_american(p_next_td_full_game)
            target_market_label = f"{tds_already + 1}+ TD" if tds_already >= 1 else "ATD (1+)"

            edge = 0.0
            book_odds_str = "N/A"
            if market_books and name in market_books:
                book_odds = market_books[name]
                book_odds_str = f"+{book_odds}" if book_odds > 0 else str(book_odds)
                implied_prob = self._american_to_prob(book_odds)
                edge = round((p_next_td_full_game - implied_prob) * 100, 2)

            fair_cents = max(1, min(99, int(round(p_next_td_full_game * 100.0))))

            enriched.append({
                **p,
                "tds_scored_today": tds_already,
                "market_tier": target_market_label,
                "drive_td_prob": round(p_curr_drive, 4),
                "rem_drives_est": round(rem_future_drives, 1),
                "full_game_atd_prob": round(p_next_td_full_game, 4),
                "fair_full_game_odds": fair_american,
                "fair_cents": fair_cents,
                "market_odds": book_odds_str,
                "edge_pct": edge
            })

        return sorted(enriched, key=lambda x: x["full_game_atd_prob"], reverse=True)

    async def evaluate_and_send_alerts(
        self,
        session: aiohttp.ClientSession,
        team_label: str,
        situation_summary: str,
        yards_to_goal: int,
        vulture_flag: bool,
        momentum: dict,
        enriched_players: list[dict]
    ):
        if yards_to_goal > 25:
            return

        now = time.time()
        triggered_lines = []
        is_stalled = momentum.get("is_stalled", False)

        for p in enriched_players:
            name = p["name"]
            last_sent = self.last_alert_ts.get(name, 0)
            if (now - last_sent) < self.cooldown_sec:
                continue

            is_vulture_spike = (not is_stalled and vulture_flag and p["role"] in ("RB2_VULTURE", "RB3_PASS", "TE1", "TE2_LEAK") and p["drive_td_prob"] >= 0.12)
            is_te2_leak = (not is_stalled and p["role"] == "TE2_LEAK" and yards_to_goal <= 12 and p["drive_td_prob"] >= 0.08)
            is_gl_favorite = (not is_stalled and yards_to_goal <= 8 and p["drive_td_prob"] >= 0.26)
            is_high_edge = (p["edge_pct"] >= (8.5 if is_stalled else 6.0))

            if is_vulture_spike or is_te2_leak or is_high_edge or is_gl_favorite:
                self.last_alert_ts[name] = now
                if is_te2_leak:
                    tag = "🎯 TE2 LEAK SPIKE (Noah Gray Effect)"
                elif is_vulture_spike:
                    tag = "🦅 VULTURE SPIKE"
                elif is_high_edge:
                    tag = "💰 +EV EDGE"
                else:
                    tag = "🔥 GOAL LINE"
                edge_str = f" | Edge: <b>+{p['edge_pct']}%</b> (Book: {p['market_odds']})" if p["market_odds"] != "N/A" else ""
                triggered_lines.append(
                    f"<b>{tag}: {name} ({p['role']}) [{p['market_tier']}]</b>\n"
                    f"├ This Drive TD: <code>{p['drive_td_prob']*100:.1f}%</code>\n"
                    f"├ Rem Game {p['market_tier']}: <code>{p['full_game_atd_prob']*100:.1f}%</code> (Fair: <b>{p['fair_full_game_odds']}</b>){edge_str}\n"
                    f"└ Fatigue: <code>{p['fatigue_mult']:.2f}x</code> | Matchup: <code>{p['matchup_mult']:.2f}x</code>"
                )

        if not triggered_lines:
            return

        vulture_banner = "\n⚠️ <b>RB1 FATIGUE DETECTED — VULTURE WINDOW OPEN</b>" if vulture_flag else ""
        msg = (
            f"🚨 <b>LIVE NFL ATD SIGNAL — {team_label}</b> 🚨\n"
            f"📍 <b>Situation:</b> {situation_summary} ({yards_to_goal} yds to EZ)\n"
            f"⚡ <b>Drive State:</b> {momentum.get('momentum_state')} ({momentum.get('reason')})"
            f"{vulture_banner}\n\n"
            + "\n\n".join(triggered_lines)
        )
        await self._send_telegram_message(session, msg)

    async def _send_telegram_message(self, session: aiohttp.ClientSession, html_text: str):
        if not self.bot_token or not self.chat_id:
            return
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        payload = {"chat_id": self.chat_id, "text": html_text, "parse_mode": "HTML", "disable_web_page_preview": True}
        try:
            async with session.post(url, json=payload, timeout=4) as resp:
                pass
        except Exception as e:
            print(f"[!] Telegram webhook notice: {e}")

    @staticmethod
    def _prob_to_american(prob: float) -> str:
        if prob <= 0.005: return "+9900"
        if prob >= 0.99: return "-9900"
        return f"-{int((prob / (1 - prob)) * 100)}" if prob >= 0.5 else f"+{int(((1 - prob) / prob) * 100)}"

    @staticmethod
    def _american_to_prob(odds: int) -> float:
        return ((-odds) / ((-odds) + 100.0)) if odds < 0 else (100.0 / (odds + 100.0))


# ──────────────────────────────────────────────────────────────────────────────
# 9. ISOLATED PER-GAME SESSION WORKER & LEAGUE-WIDE MULTIPLEXER
# ──────────────────────────────────────────────────────────────────────────────
class GameSessionWorker:
    def __init__(self, event_id: str, matchup_name: str, alert_engine: FullGameATDAndAlertEngine, roster_builder: AutoTeamRosterBuilder):
        self.event_id = event_id
        self.matchup_name = matchup_name
        self.alert_engine = alert_engine
        self.roster_builder = roster_builder
        self.fatigue_trackers = defaultdict(DriveFatigueTracker)
        self.last_play_id = None
        self.last_poll_ts = 0.0
        self.current_yards_to_goal = 75
        self.is_redzone = False
        self.latest_board_snapshot = {}

    def needs_poll(self, scoreboard_play_id: str, yards_to_goal: int, now: float) -> bool:
        if scoreboard_play_id and scoreboard_play_id != self.last_play_id:
            return True
        elapsed = now - self.last_poll_ts
        if yards_to_goal <= 20 and elapsed >= 1.5:
            return True
        if yards_to_goal <= 45 and elapsed >= 3.0:
            return True
        return elapsed >= 10.0

    @staticmethod
    def _estimate_base_drive_td_prob(yards_to_goal: int, down: int, distance: int, opp_abbr: str) -> float:
        if yards_to_goal <= 3:
            base = 0.72
        elif yards_to_goal <= 10:
            base = 0.58
        elif yards_to_goal <= 20:
            base = 0.46
        elif yards_to_goal <= 40:
            base = 0.31
        else:
            base = max(0.08, 0.25 - (yards_to_goal - 40) * 0.003)

        if down == 3:
            base *= 0.72 if distance >= 5 else 0.85
        elif down == 4:
            base *= 0.45

        def_profile = DefensiveMatchupEngine.get_profile(opp_abbr)
        base *= def_profile.get("rz_td_multiplier", 1.0)
        return round(min(0.92, max(0.02, base)), 4)

    async def process_tick(self, session: aiohttp.ClientSession, live_market_odds: dict[str, int]):
        self.last_poll_ts = time.time()
        url = SUMMARY_URL.format(event_id=self.event_id)

        async with session.get(url, timeout=5) as resp:
            if resp.status != 200:
                return
            data = await resp.json()

        header = data.get("header", {}).get("competitions", [{}])[0]
        situation = data.get("situation", {}) or header.get("situation", {})
        play_id = situation.get("lastPlay", {}).get("id")

        if play_id and play_id == self.last_play_id:
            return
        self.last_play_id = play_id

        current_drive = data.get("drives", {}).get("current", {})
        off_team_obj = current_drive.get("team", {})
        off_abbr = off_team_obj.get("abbreviation", "").upper()
        off_id = off_team_obj.get("id", "")

        competitors = header.get("competitors", [])
        if not off_abbr and competitors:
            off_abbr = competitors[0].get("team", {}).get("abbreviation", "").upper()
            off_id = competitors[0].get("team", {}).get("id", "")

        opp_abbr = "DEFAULT"
        for comp in competitors:
            t_abbr = comp.get("team", {}).get("abbreviation", "").upper()
            if t_abbr and t_abbr != off_abbr:
                opp_abbr = t_abbr
            elif t_abbr == off_abbr and not off_id:
                off_id = comp.get("team", {}).get("id", "")

        down = int(situation.get("down", 1) or 1)
        distance = int(situation.get("distance", 10) or 10)
        ytg = int(situation.get("yardLine", 65) or 65)
        pos_text = situation.get("possessionText", f"{off_abbr} 35")
        last_play_text = situation.get("lastPlay", {}).get("text", "Drive Active")

        self.current_yards_to_goal = ytg
        self.is_redzone = (ytg <= 20)

        # 1. Auto-Discover & Sync Live Boxscore Roster
        roster = await self.roster_builder.get_or_build_roster(session, off_id, off_abbr, data)
        if not roster:
            return
        roster_names = [p["name"] for p in roster]

        # 2. Update Already-Scored TD Count (for 2+ TD Escalator)
        self.alert_engine.update_td_counts(data, roster_names)

        # 3. Apply Bayesian Hot-Hand & Pass/Run Script Updater
        drive_plays = current_drive.get("plays", [])
        roster = GamePlanBayesianUpdater.apply_live_gameplan_shifts(roster, drive_plays)

        # 4. Update In-Memory RB Fatigue
        fatigue_state = self.fatigue_trackers[off_abbr].parse_drive_plays(data, roster)

        # 5. Analyze Drive Momentum & Drive Killers (Sacks, Penalties, Incompletions)
        momentum = DriveMomentumAnalyzer.analyze_drive_momentum(current_drive, down, distance)

        # 5b. AWS Next Gen Stats Drive Telemetry & TE2 Leak Engine
        ngs = AWSNextGenDriveEngine.analyze_drive_telemetry(current_drive, ytg, down, distance, roster)

        # Combine momentum pos mods with NGS pos mods
        combined_pos_mods = dict(momentum["pos_modifiers"])
        for rk, rm in ngs.get("pos_modifiers", {}).items():
            combined_pos_mods[rk] = combined_pos_mods.get(rk, 1.0) * rm

        # 6. Compute Drive TD Prob & Situational Shares
        base_drive_td = self._estimate_base_drive_td_prob(ytg, down, distance, opp_abbr)
        drive_td_prob = round(min(0.92, max(0.02, base_drive_td * momentum["drive_td_mult"])), 4)

        player_shares = calculate_situational_atd_shares(
            roster, ytg, down, distance, fatigue_state,
            opp_abbr=opp_abbr,
            momentum_pos_mods=combined_pos_mods
        )

        # 7. Full-Game Remaining ATD & 2+ TD Probabilities
        full_game_board = self.alert_engine.compute_full_game_probabilities(
            summary_json=data,
            drive_td_prob=drive_td_prob,
            situational_shares=player_shares,
            market_books=live_market_odds
        )

        self.latest_board_snapshot = {
            "event_id": self.event_id,
            "matchup": self.matchup_name,
            "offense": off_abbr,
            "defense": opp_abbr,
            "situation": f"{down} & {distance} at {pos_text} ({ytg} yds to EZ)",
            "last_play": last_play_text,
            "yards_to_goal": ytg,
            "down": down,
            "distance": distance,
            "pos_text": pos_text,
            "drive_td_prob": drive_td_prob,
            "momentum_state": momentum["momentum_state"],
            "momentum_reason": momentum["reason"],
            "vulture_alert": fatigue_state.get("vulture_alert", False),
            "ngs": ngs,
            "players": full_game_board,
            "updated_at": time.strftime("%H:%M:%S")
        }

        await self.alert_engine.evaluate_and_send_alerts(
            session=session,
            team_label=f"{off_abbr} ({self.matchup_name})",
            situation_summary=f"{down} & {distance} at {pos_text}",
            yards_to_goal=ytg,
            vulture_flag=fatigue_state.get("vulture_alert", False),
            momentum=momentum,
            enriched_players=full_game_board
        )


class LeagueWideATDOrchestrator:
    def __init__(self, bot_token: Optional[str] = None, chat_id: Optional[str] = None, filter_team: Optional[str] = None, max_concurrent: int = 5):
        self.bot_token = bot_token or DEFAULT_BOT_TOKEN
        self.chat_id = chat_id or DEFAULT_CHAT_ID
        self.alert_engine = FullGameATDAndAlertEngine(bot_token=self.bot_token, chat_id=self.chat_id)
        self.roster_builder = AutoTeamRosterBuilder()
        self.workers: dict[str, GameSessionWorker] = {}
        self.filter_team = filter_team.upper() if filter_team else None
        self.sem = asyncio.Semaphore(max_concurrent)

    async def run_sunday_multiplexer(self):
        headers = {"User-Agent": "Mozilla/5.0 (Linux; Android 14; Termux)"}
        connector = aiohttp.TCPConnector(limit=12, ttl_dns_cache=300)

        async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
            target_str = f" (Filtered: {self.filter_team})" if self.filter_team else " (All Live NFL Games)"
            print(f"[🚀] Unified NFL Live ATD Engine Started{target_str}")
            last_sb_sync = 0.0
            live_event_meta = {}

            while True:
                now = time.time()
                try:
                    if (now - last_sb_sync) >= 6.0:
                        last_sb_sync = now
                        async with session.get(SCOREBOARD_URL, timeout=5) as resp:
                            if resp.status == 200:
                                sb_data = await resp.json()
                                live_event_meta = self._parse_scoreboard(sb_data)

                    if not live_event_meta:
                        print(f"[{time.strftime('%H:%M:%S')}] 💤 No matching live NFL games right now. Radar idling (20s)...")
                        await asyncio.sleep(20.0)
                        last_sb_sync = 0.0
                        continue

                    live_market_odds = ATDOddsStore.load_odds()
                    tasks = []
                    for event_id, meta in live_event_meta.items():
                        worker = self.workers[event_id]
                        if worker.needs_poll(meta["last_play_id"], meta["yards_to_goal"], now):
                            tasks.append(self._bounded_tick(session, worker, live_market_odds))

                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                        self._save_and_render_dashboard()

                    any_rz = any(w.is_redzone for w in self.workers.values())
                    await asyncio.sleep(1.5 if any_rz else 3.0)

                except Exception as e:
                    print(f"[!] Orchestrator Error: {e}")
                    await asyncio.sleep(4.0)

    async def _bounded_tick(self, session, worker: GameSessionWorker, odds: dict):
        async with self.sem:
            await worker.process_tick(session, odds)

    def _parse_scoreboard(self, sb_json: dict) -> dict:
        active_meta = {}
        events = sb_json.get("events", [])

        # Safety check: if ESPN returns an empty/invalid scoreboard feed, don't fallback to ghosts
        if not events:
            return active_meta

        for ev in events:
            event_id = str(ev.get("id"))
            short_name = ev.get("shortName", "NFL")

            comp = ev.get("competitions", [{}])[0]
            status_obj = comp.get("status", {}) or ev.get("status", {})
            state = status_obj.get("type", {}).get("state", "pre").lower()

            # STRICT FILTER: Only ingest games explicitly verified as live ("in") right now
            if state == "in":
                if self.filter_team and self.filter_team not in short_name.upper():
                    continue

                sit = comp.get("situation", {})
                ytg = int(sit.get("yardLine", 75) or 75)
                last_play_id = sit.get("lastPlay", {}).get("id", "live_active")

                if event_id not in self.workers:
                    print(f"[🟢 VERIFIED LIVE] Spawning worker for active game: {short_name} (ID: {event_id})")
                    self.workers[event_id] = GameSessionWorker(
                        event_id, short_name, self.alert_engine, self.roster_builder
                    )

                active_meta[event_id] = {
                    "short_name": short_name,
                    "yards_to_goal": ytg,
                    "last_play_id": last_play_id
                }
            elif state == "post" and event_id in self.workers:
                print(f"[🏁 GAME FINAL] Closing worker for {short_name}")
                del self.workers[event_id]

        return active_meta

    def _save_and_render_dashboard(self):
        snapshots = [w.latest_board_snapshot for w in self.workers.values() if w.latest_board_snapshot]
        if not snapshots:
            # Clean up disk snapshot if no live games are active so /atd doesn't show stale drives
            try:
                with open(STATE_SNAPSHOT_FILE, "w") as f:
                    json.dump({"updated_at": time.strftime("%H:%M:%S"), "games": []}, f, indent=2)
            except Exception:
                pass
            return

        snapshots.sort(key=lambda s: s["yards_to_goal"])

        # Export live snapshot JSON so bot.py `/atd` command can read instantly (<5ms)
        try:
            with open(STATE_SNAPSHOT_FILE, "w") as f:
                json.dump({"updated_at": time.strftime("%H:%M:%S"), "games": snapshots}, f, indent=2)
        except Exception:
            pass

        print("\n" + "═" * 78)
        print(f"🏈 NFL REDZONE LIVE ATD RADAR ({len(snapshots)} Active Games) — {time.strftime('%H:%M:%S')}")
        print("═" * 78)

        for snap in snapshots:
            vult = " | 🦅 VULTURE WINDOW" if snap["vulture_alert"] else ""
            print(f"📍 {snap['matchup']} ({snap['offense']} vs {snap['defense']}) | {snap['situation']}")
            print(f"   State: {snap['momentum_state']} ({snap['momentum_reason']}){vult}")
            ngs = snap.get("ngs", {})
            if ngs:
                leak_str = " | 🎯 TE2 LEAK PRIMED" if ngs.get("te2_leak_active") else ""
                speed_str = f" | {ngs.get('peak_speed_desc')}" if ngs.get("peak_speed_mph", 0) > 0 else ""
                print(f"   📡 NGS: {ngs.get('personnel')} | {ngs.get('tempo_state')} ({ngs.get('xtd', 0)*100:.1f}% xTD){leak_str}{speed_str}")
            print(f"   Last Play: {snap['last_play']}")
            print(f"   Drive TD Prob: {snap['drive_td_prob']*100:.1f}%")
            print("-" * 78)

            limit = 7 if self.filter_team or len(snapshots) == 1 else 4
            for p in snap["players"][:limit]:
                edge_str = f" | Edge: {p['edge_pct']:+5.1f}% ({p['market_odds']})" if p["market_odds"] != "N/A" else ""
                print(
                    f"   • {p['name']:<19} [{p['market_tier']:<7}] ({p['role']:<11}) | "
                    f"Drive: {p['drive_td_prob']*100:4.1f}% | "
                    f"Game: {p['full_game_atd_prob']*100:4.1f}% ({p['fair_full_game_odds']:>5}) | "
                    f"Fatigue: {p['fatigue_mult']:.2f}x | Matchup: {p['matchup_mult']:.2f}x{edge_str}"
                )
            print("-" * 78)


# Module-level Global Singleton Orchestrator
ORCHESTRATOR = LeagueWideATDOrchestrator()


# ──────────────────────────────────────────────────────────────────────────────
# 10. INSTANT TELEGRAM /atd COMMAND HANDLER (Sub-5ms via State Snapshot)
# ──────────────────────────────────────────────────────────────────────────────
def scan_live_nfl_atd_board(target_query: Optional[str] = None) -> Tuple[str, dict]:
    """
    Ultra-fast (<5ms) /atd handler:
    1. Reads live_atd_state.json written on every tick by the background orchestrator.
    2. If target_query is specified (/atd kc), formats the deep 7-player card for that team.
    3. If no argument is given, formats the multi-game RedZone Threat Board.
    4. Falls back to verified ESPN scoreboard schedule if no live games are active right now.
    """
    q_clean = (target_query or "").upper().strip()

    # 1. Try reading the high-speed background state snapshot first
    cached_games = []
    if os.path.exists(STATE_SNAPSHOT_FILE):
        try:
            with open(STATE_SNAPSHOT_FILE, "r") as f:
                cached_games = json.load(f).get("games", [])
        except Exception:
            cached_games = []

    if not cached_games:
        # Check active in-memory orchestrator workers
        cached_games = [w.latest_board_snapshot for w in ORCHESTRATOR.workers.values() if w.latest_board_snapshot]

    # Mode A: Single Team Filtered Query (/atd kc or /atd den)
    if q_clean and q_clean not in ("ALL", "BOARD", "SLATE", "REDZONE"):
        for snap in cached_games:
            if q_clean in (snap.get("offense", ""), snap.get("defense", ""), snap.get("matchup", "")):
                return _format_snapshot_to_card(snap)

    # Mode B: Multi-Game Threat Board (/atd)
    if cached_games:
        cached_games.sort(key=lambda s: s.get("yards_to_goal", 99))
        lines = [
            f"🏈 <b>NFL REDZONE MULTI-GAME ATD RADAR</b> ({len(cached_games)} Live) 🏈",
            f"━━━━━━━━━━━━━━━━━━━━━━━━",
        ]
        for s in cached_games[:6]:
            badge = "🔥 <b>[RED ZONE]</b>" if s.get("yards_to_goal", 99) <= 20 else ("⚡ <b>[DRIVING]</b>" if s.get("yards_to_goal", 99) <= 45 else "📡")
            vult = " 🦅 <i>(Vulture Window!)</i>" if s.get("vulture_alert") else ""
            top_targets = " · ".join([
                f"<b>{p['name']}</b> ({p['full_game_atd_prob']*100:.0f}% - {p['fair_full_game_odds']})"
                for p in s.get("players", [])[:2]
            ])
            lines.append(
                f"{badge} <b>{s.get('matchup')}</b> ({s.get('offense')} Ball){vult}\n"
                f"• {s.get('situation')}\n"
                f"• State: <code>{s.get('momentum_state')}</code>\n"
                f"• Drive TD: <code>{s.get('drive_td_prob', 0)*100:.1f}%</code> | Top: {top_targets}\n"
            )

        lines.append("<i>Tip: Type /atd kc or /atd den for full 7-player probability breakdown.</i>")
        kb = {
            "inline_keyboard": [
                [{"text": "⚡ Refresh RedZone", "callback_data": "atd_refresh_board"}],
                [{"text": "📺 Watch Live Wire", "url": "https://streamed.su/watch/american-football"}],
                [{"text": "🔙 Back to HUD", "callback_data": "menu_refresh_hud"}]
            ]
        }
        return "\n".join(lines), kb

    # Mode C: Fallback to Verified ESPN Scoreboard Slate if no live games are active right now
    try:
        r = requests.get(SCOREBOARD_URL, timeout=4)
        if r.status_code == 200:
            events = r.json().get("events", [])
            return _build_standby_slate_card(events, q_clean)
    except Exception:
        pass

    return _build_standby_slate_card([], q_clean)


def _format_snapshot_to_card(snap: dict) -> Tuple[str, dict]:
    vult_banner = "\n⚠️ <b>RB1 Fatigue Active — Vulture Window Open!</b>" if snap.get("vulture_alert") else ""
    momentum_line = f"⚡ <b>Momentum:</b> <code>{snap.get('momentum_state')}</code> ({snap.get('momentum_reason')})\n" if snap.get("momentum_state") else ""
    ngs = snap.get("ngs", {})
    ngs_line = ""
    if ngs:
        leak_icon = " · 🎯 <b>TE2 Leak Active!</b>" if ngs.get("te2_leak_active") else ""
        ngs_line = f"📡 <b>AWS NGS:</b> <code>{ngs.get('personnel', '11 Personnel')}</code> | <code>{ngs.get('tempo_state', 'BALANCED')}</code> ({ngs.get('xtd', 0)*100:.0f}% xTD){leak_icon}\n"

    lines = [
        f"🏈 <b>AURASCAN // LIVE NFL ATD &amp; MULTI-TD ENGINE</b> 🏈",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏟 <b>{snap.get('matchup')}</b> — <code>{snap.get('situation')}</code>",
        momentum_line + ngs_line +
        f"🎯 <b>Drive TD Prob P(DriveTD):</b> <b>{snap.get('drive_td_prob', 0)*100:.1f}%</b>"
        f"{vult_banner}\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <b>LIVE ATD &amp; MULTI-TD ALLOCATIONS:</b>",
    ]

    for idx, p in enumerate(snap.get("players", [])[:7], 1):
        name = p.get("name")
        role = p.get("role")
        drv_prob = p.get("drive_td_prob", 0) * 100
        game_prob = p.get("full_game_atd_prob", 0) * 100
        fair_odds = p.get("fair_full_game_odds", "+0")
        tier = p.get("market_tier", "ATD")
        fatigue_mult = p.get("fatigue_mult", 1.0)
        matchup_mult = p.get("matchup_mult", 1.0)

        tag = ""
        if p.get("tds_scored_today", 0) >= 1:
            tag = f" 🎯 <i>({p['tds_scored_today']} TD Already Scored!)</i>"
        elif "TE2_LEAK" in role:
            tag = " 🎯 <i>(TE2 Leak)</i>"
        elif "VULTURE" in role:
            tag = " 🦅 <i>(Vulture)</i>"
        elif "RB1" in role:
            tag = " 👑 <i>(RB1)</i>"

        edge_str = f" | <b>Edge: {p['edge_pct']:+.1f}%</b> (Book: {p['market_odds']})" if p.get("market_odds") and p["market_odds"] != "N/A" else ""

        lines.append(
            f"{idx}. <b>{name}</b> ({role}) [{tier}]{tag}\n"
            f"   • Goal-Line Share: <code>{p.get('conditional_td_share', 0)*100:.1f}%</code> | Fatigue: <code>{fatigue_mult:.2f}x</code> | Matchup: <code>{matchup_mult:.2f}x</code>\n"
            f"   • Drive TD: <b>{drv_prob:.1f}%</b> | Rem Game {tier}: <b>{game_prob:.1f}%</b> (Fair: <code>{fair_odds}</code>){edge_str}"
        )

    if snap.get("yards_to_goal", 99) <= 35:
        lines.append(
            f"\n⚡ <b>ANTICIPATORY TRIGGER ACTIVE ({snap['yards_to_goal']} ≤ 35 yds):</b>\n"
            f"<i>Offense in scoring strike zone. Front-running broadcast delays before lines freeze!</i>"
        )

    offense_team = snap.get("offense", "KC")
    kb = {
        "inline_keyboard": [
            [
                {"text": "⚡ Refresh", "callback_data": f"atd_refresh_{offense_team}"},
                {"text": "📡 Drive Telemetry", "callback_data": f"drive_refresh_{offense_team}"},
            ],
            [
                {"text": "🏈 All RedZone Games", "callback_data": "atd_refresh_board"},
                {"text": "📺 Watch Live Wire", "url": "https://streamed.su/watch/american-football"},
            ],
            [
                {"text": "🔙 Back to HUD", "callback_data": "menu_refresh_hud"},
            ]
        ]
    }
    return "\n".join(lines), kb


def scan_live_drive_telemetry(target_query: Optional[str] = None) -> Tuple[str, dict]:
    """
    Renders AWS Next Gen Stats drive telemetry:
    - Drive Pace (sec/play) & tempo (Hurry-Up vs Balanced vs Clock Bleed)
    - Personnel Grouping (11 vs 12 vs 13 heavy)
    - Cumulative Drive EPA & Expected Touchdowns (xTD)
    - Peak Carrier Top Speed (mph)
    - Noah Gray / TE2 Goal-Line Leak Radar
    """
    q_clean = (target_query or "").upper().strip()

    cached_games = []
    if os.path.exists(STATE_SNAPSHOT_FILE):
        try:
            with open(STATE_SNAPSHOT_FILE, "r") as f:
                cached_games = json.load(f).get("games", [])
        except Exception:
            cached_games = []

    if not cached_games:
        cached_games = [w.latest_board_snapshot for w in ORCHESTRATOR.workers.values() if w.latest_board_snapshot]

    if q_clean and q_clean not in ("ALL", "BOARD", "SLATE", "REDZONE"):
        for snap in cached_games:
            if q_clean in (snap.get("offense", ""), snap.get("defense", ""), snap.get("matchup", "")):
                return _format_drive_telemetry_card(snap)

    if cached_games:
        cached_games.sort(key=lambda s: s.get("yards_to_goal", 99))
        return _format_drive_telemetry_card(cached_games[0])

    try:
        r = requests.get(SCOREBOARD_URL, timeout=4)
        if r.status_code == 200:
            events = r.json().get("events", [])
            return _build_standby_slate_card(events, q_clean)
    except Exception:
        pass

    return _build_standby_slate_card([], q_clean)


def _format_drive_telemetry_card(snap: dict) -> Tuple[str, dict]:
    ngs = snap.get("ngs", {})
    off = snap.get("offense", "NFL")
    matchup = snap.get("matchup", "NFL Game")
    sit = snap.get("situation", "")
    ytg = snap.get("yards_to_goal", 50)

    speed_line = f"• 🏃 <b>Top Speed:</b> <code>{ngs.get('peak_speed_desc', 'In tracking...')}</code>\n" if ngs.get("peak_speed_mph", 0) > 0 else ""
    leak_banner = f"\n🎯 <b>TE2 LEAK RADAR:</b> <code>PRIMED ({ngs.get('te2_leak_mult', 1.95):.2f}x)</code>\n<i>{ngs.get('leak_reason', 'Heavy personnel goal-line look')}</i>\n" if ngs.get("te2_leak_active") else ""

    lines = [
        f"📡 <b>AWS NEXT GEN STATS // DRIVE TELEMETRY</b> 📡",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🏟 <b>{matchup}</b> ({off} Ball)",
        f"📍 <b>Situation:</b> <code>{sit}</code>",
        f"⚡ <b>Drive State:</b> <code>{snap.get('momentum_state')}</code>\n",
        f"📊 <b>NEXT GEN DRIVE METRICS:</b>",
        f"• ⏱️ <b>Pace & Tempo:</b> <code>{ngs.get('tempo_state', 'BALANCED')}</code> ({ngs.get('sec_per_play', 28.0)}s/play)",
        f"• 👥 <b>Personnel:</b> <code>{ngs.get('personnel', '11 Personnel')}</code>",
        f"• 📈 <b>Cumulative EPA:</b> <code>{'+' if ngs.get('drive_epa', 0) >= 0 else ''}{ngs.get('drive_epa', 0.0)} EPA</code>",
        f"• 🎯 <b>Next Gen xTD:</b> <b>{ngs.get('xtd', 0.50)*100:.1f}% xTD</b> (Drive TD: {snap.get('drive_td_prob', 0)*100:.1f}%)",
        speed_line +
        f"• ⏱️ <b>Drive Velocity:</b> <code>{ngs.get('play_count', 0)} plays, {ngs.get('drive_yards', 0)} yds ({ngs.get('elapsed_time', '0:00')})</code>"
        f"{leak_banner}",
        f"━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🎯 <b>TOP TOUCHDOWN CANDIDATES THIS PLAY:</b>"
    ]
    for idx, p in enumerate(snap.get("players", [])[:5], 1):
        tier = p.get("market_tier", "ATD")
        role_tag = f" 🎯 <i>(TE2 Leak)</i>" if p.get("role") == "TE2_LEAK" else ""
        lines.append(
            f"{idx}. <b>{p['name']}</b> ({p['role']}) [{tier}]{role_tag}\n"
            f"   • Drive P(TD): <b>{p.get('drive_td_prob', 0)*100:.1f}%</b> | Full Game: <b>{p.get('full_game_atd_prob', 0)*100:.1f}%</b> (Fair: <code>{p.get('fair_full_game_odds')}</code>)"
        )

    kb = {
        "inline_keyboard": [
            [
                {"text": "⚡ Refresh Drive", "callback_data": f"drive_refresh_{off}"},
                {"text": "🏈 ATD Allocations", "callback_data": f"atd_refresh_{off}"}
            ],
            [
                {"text": "🏈 All RedZone Games", "callback_data": "atd_refresh_board"},
                {"text": "📺 Watch Live Wire", "url": "https://streamed.su/watch/american-football"}
            ],
            [
                {"text": "🔙 Back to HUD", "callback_data": "menu_refresh_hud"}
            ]
        ]
    }
    return "\n".join(lines), kb


def _build_standby_slate_card(events: list, team_query: Optional[str] = None) -> Tuple[str, dict]:
    """
    Renders actual upcoming and final NFL games from ESPN's scoreboard feed.
    Zero mock games, zero ghost teams, and zero synthetic in-progress drives.
    """
    t_clean = (team_query or "").upper().strip()
    lines = [
        "🏈 <b>NFL REDZONE ATD RADAR — SLATE STANDBY</b> 🏈",
        "━━━━━━━━━━━━━━━━━━━━━━━━",
        "ℹ️ <i>No live NFL games currently in-progress.</i>\n",
    ]

    matched_event = None
    if t_clean:
        for ev in events:
            s_name = ev.get("shortName", "").upper()
            name = ev.get("name", "").upper()
            comps = ev.get("competitions", [{}])[0].get("competitors", [])
            abbrs = [c.get("team", {}).get("abbreviation", "").upper() for c in comps]
            if t_clean in abbrs or any(t_clean in x for x in (s_name, name)):
                matched_event = ev
                break

    if matched_event:
        short_name = matched_event.get("shortName", "NFL")
        comp = matched_event.get("competitions", [{}])[0]
        status_type = comp.get("status", {}).get("type", {})
        detail = status_type.get("detail", status_type.get("shortDetail", "Upcoming"))
        broadcasts = comp.get("broadcasts", [{}])
        b_names = ", ".join([b.get("names", [""])[0] for b in broadcasts if b.get("names")])
        tv_str = f" ({b_names})" if b_names else ""

        lines.append(
            f"📍 <b>Target Matchup:</b> <b>{short_name}</b>{tv_str}\n"
            f"• Status: <code>{detail}</code>\n"
            f"• <i>Radar is armed and will automatically engage sub-second RedZone tracking at kickoff.</i>\n"
        )
    else:
        lines.append("📋 <b>Today's Verified NFL Slate (ESPN):</b>")
        upcoming_count = 0
        for ev in events[:8]:
            short_name = ev.get("shortName", "NFL")
            comp = ev.get("competitions", [{}])[0]
            status_type = comp.get("status", {}).get("type", {})
            detail = status_type.get("detail", status_type.get("shortDetail", "Upcoming"))
            state = status_type.get("state", "pre").lower()
            badge = "⏰" if state == "pre" else ("🏁" if state == "post" else "🟢")
            lines.append(f"• {badge} <b>{short_name}</b> — <code>{detail}</code>")
            upcoming_count += 1

        if upcoming_count == 0:
            lines.append("• <i>No scheduled games found on today's active NFL week.</i>")

    lines.append("\n<i>Tip: Use /atd <team> (e.g. /atd kc) to check a specific matchup.</i>")
    kb = {
        "inline_keyboard": [
            [{"text": "⚡ Refresh Scoreboard", "callback_data": "atd_refresh_board"}],
            [{"text": "📺 Watch Live Wire", "url": "https://streamed.su/watch/american-football"}],
            [{"text": "🔙 Back to HUD", "callback_data": "menu_refresh_hud"}]
        ]
    }
    return "\n".join(lines), kb


# ──────────────────────────────────────────────────────────────────────────────
# CLI ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", DEFAULT_BOT_TOKEN)
    chat_id = os.getenv("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)
    team_filter = sys.argv[1] if len(sys.argv) > 1 else None

    orchestrator = LeagueWideATDOrchestrator(
        bot_token=bot_token,
        chat_id=chat_id,
        filter_team=team_filter
    )
    try:
        asyncio.run(orchestrator.run_sunday_multiplexer())
    except KeyboardInterrupt:
        print("\n[👋] Shutting down NFL Live ATD Engine.")
