"""
Sport-Specific In-Game Coaching Brains & Tactical Router
=========================================================
Dynamically adjusts live simulation projections and Monte Carlo distributions
based on authentic in-game coaching decisions across distinct sports:

1. NBA: Possession & Pace Engine
   - Pick-and-roll defense schemes (Drop vs. Blitz/Trap)
   - Foul trouble management & conservative minute restrictions
   - Q4 Blowout Bench Tax (>= 16 pt differential)

2. NFL: Game-Script & Personnel Engine
   - Trailing script (down 10+ in 2H -> Pass attempts spike +18%, Rush taxed -35%)
   - Positive clock-killing script (up 10+ in Q4 -> RB carries +28%, Pass taxed -20%)
   - Two-Minute hurry-up & garbage-time volume inflation

3. MLB: Pitch Count & Times-Through-The-Order Penalty (TTOP)
   - TTOP 3rd-pass through order (Pitches >= 75, Inning >= 5) -> K rate -18%, Hits/ER +20%
   - High pitch fatigue (>= 90-95 pitches) -> Imminent hook hazard, K tax -30%

4. NHL: In-Game Flow & Extra-Attacker Engine
   - P3 trailing 1-2 goals late -> Goalie pulled (6-on-5)
   - Skater SOG & Point rate surge (+40%), Leading team empty-net finish boost (+35%)
"""

import math
import re
from typing import Any, Dict, List, Optional, Tuple

from telemetry import GameTelemetry


class SportCoachingRouter:
    """
    Evaluates real-time game telemetry and context to apply sport-specific
    tactical coaching adjustments to player stat projection means.
    """

    @classmethod
    def apply_sport_tactics(
        cls,
        sport: str,
        stat_type: str,
        baseline_mean: float,
        telemetry: Optional[GameTelemetry] = None,
        live_context: Optional[Dict[str, Any]] = None,
        archetype: str = "",
        player_name: str = "",
    ) -> Dict[str, Any]:
        """
        Calculates coaching multiplier and produces tactical notes.
        Returns:
            {
                "adjusted_mean": float,
                "multiplier": float,
                "coaching_notes": List[str],
                "tactical_state": Dict[str, Any],
            }
        """
        sp = (sport or "OTHER").upper()
        stat_upper = (stat_type or "POINTS").upper()
        ctx = live_context or {}
        notes: List[str] = []
        mult = 1.0

        if sp in ("NBA", "WNBA"):
            mult, notes = cls._nba_coaching_tactics(
                stat_upper=stat_upper,
                baseline_mean=baseline_mean,
                telemetry=telemetry,
                ctx=ctx,
                archetype=archetype,
                player_name=player_name,
            )
        elif sp in ("NFL", "CFB", "FOOTBALL"):
            mult, notes = cls._nfl_coaching_tactics(
                stat_upper=stat_upper,
                baseline_mean=baseline_mean,
                telemetry=telemetry,
                ctx=ctx,
                archetype=archetype,
            )
        elif sp in ("MLB", "BASEBALL"):
            mult, notes = cls._mlb_coaching_tactics(
                stat_upper=stat_upper,
                baseline_mean=baseline_mean,
                telemetry=telemetry,
                ctx=ctx,
                archetype=archetype,
            )
        elif sp in ("NHL", "HOCKEY"):
            mult, notes = cls._nhl_coaching_tactics(
                stat_upper=stat_upper,
                baseline_mean=baseline_mean,
                telemetry=telemetry,
                ctx=ctx,
                archetype=archetype,
            )
        elif sp == "TENNIS":
            mult, notes = cls._tennis_coaching_tactics(
                stat_upper=stat_upper,
                baseline_mean=baseline_mean,
                telemetry=telemetry,
                ctx=ctx,
            )
        else:
            mult = 1.0
            notes = []

        adjusted_mean = max(0.1, round(baseline_mean * mult, 2))
        return {
            "adjusted_mean": adjusted_mean,
            "multiplier": round(mult, 4),
            "coaching_notes": notes,
            "tactical_state": {
                "sport": sp,
                "stat": stat_upper,
                "archetype": archetype,
                "raw_multiplier": mult,
            },
        }

    # -------------------------------------------------------------
    # 1. NBA: Possession & Pace Engine
    # -------------------------------------------------------------
    @classmethod
    def _nba_coaching_tactics(
        cls,
        stat_upper: str,
        baseline_mean: float,
        telemetry: Optional[GameTelemetry],
        ctx: Dict[str, Any],
        archetype: str,
        player_name: str,
    ) -> Tuple[float, List[str]]:
        mult = 1.0
        notes: List[str] = []

        period = getattr(telemetry, "period", 0) if telemetry else int(ctx.get("period", 0))
        score_diff = getattr(telemetry, "score_diff", 0) if telemetry else int(ctx.get("score_diff", 0))
        is_live = getattr(telemetry, "is_live", False) if telemetry else bool(ctx.get("is_live", False))
        foul_flag = getattr(telemetry, "foul_trouble_flag", False) if telemetry else bool(ctx.get("foul_trouble", False))
        fouls_count = int(ctx.get("fouls", 4 if foul_flag else 0))

        # A. Foul Trouble Management
        if foul_flag or (period >= 3 and fouls_count >= 4) or (period <= 2 and fouls_count >= 3):
            tax = 0.80 if period >= 3 else 0.86
            mult *= tax
            notes.append(
                f"⚠️ NBA Foul Trouble: {fouls_count} fouls in Q{period} -> Conservative minute restriction ({int((tax-1.0)*100)}% tax)"
            )

        # B. Q4 Blowout Bench Tax (>= 16 pt differential)
        if is_live and period >= 4 and score_diff >= 16:
            tax = 0.72  # 28% reduction in volume
            mult *= tax
            notes.append(
                f"🚨 NBA Blowout Bench Tax: {score_diff}pt margin in Q4 -> Star starters rested ({int((tax-1.0)*100)}% volume tax)"
            )

        # C. Coverage & Defensive Scheme (Drop vs. Blitz/Trap)
        # Check if defensive scheme is specified in context or derived from matchup
        def_scheme = str(ctx.get("defense_scheme", "")).upper()
        if "BLITZ" in def_scheme or "TRAP" in def_scheme or "DOUBLE" in def_scheme:
            if any(k in stat_upper for k in ("AST", "ASSIST", "RA", "PRA")):
                boost = 1.18
                mult *= boost
                notes.append("🛡️ NBA Scheme: Blitz/Trap Coverage -> Roll-man / kick-out assist surge (+18%)")
            elif any(k in stat_upper for k in ("PTS", "POINTS")):
                tax = 0.90
                mult *= tax
                notes.append("🛡️ NBA Scheme: Blitz/Trap Coverage -> Trapped primary handler scoring tax (-10%)")
        elif "DROP" in def_scheme:
            if any(k in stat_upper for k in ("3PT", "THREES", "3PM", "POINTS", "PTS")):
                boost = 1.12
                mult *= boost
                notes.append("🎯 NBA Scheme: Drop Coverage -> Pull-up shooter spacing boost (+12%)")

        # D. Clutch / High-Pace Incentive in Tight 4Q (diff <= 6)
        if is_live and period >= 4 and score_diff <= 6:
            if "AST" in stat_upper or "PTS" in stat_upper:
                boost = 1.06
                mult *= boost
                notes.append("🔥 NBA Clutch Protocol: Close Q4 (<=6pt margin) -> Star clutch usage boost (+6%)")

        return mult, notes

    # -------------------------------------------------------------
    # 2. NFL: Game-Script & Personnel Engine
    # -------------------------------------------------------------
    @classmethod
    def _nfl_coaching_tactics(
        cls,
        stat_upper: str,
        baseline_mean: float,
        telemetry: Optional[GameTelemetry],
        ctx: Dict[str, Any],
        archetype: str,
    ) -> Tuple[float, List[str]]:
        mult = 1.0
        notes: List[str] = []

        period = getattr(telemetry, "period", 0) if telemetry else int(ctx.get("period", 0))
        score_diff = getattr(telemetry, "score_diff", 0) if telemetry else int(ctx.get("score_diff", 0))
        is_live = getattr(telemetry, "is_live", False) if telemetry else bool(ctx.get("is_live", False))
        team_is_trailing = bool(ctx.get("is_trailing", False))
        team_is_leading = bool(ctx.get("is_leading", False))

        # Check score context
        if telemetry and is_live:
            # If telemetry has home/away score, check deficit
            team_side = str(ctx.get("team_side", "")).upper()
            if team_side == "AWAY":
                team_is_trailing = (telemetry.away_score < telemetry.home_score) and score_diff >= 10
                team_is_leading = (telemetry.away_score > telemetry.home_score) and score_diff >= 10
            elif team_side == "HOME":
                team_is_trailing = (telemetry.home_score < telemetry.away_score) and score_diff >= 10
                team_is_leading = (telemetry.home_score > telemetry.away_score) and score_diff >= 10
            else:
                # Default assume trailing if context says so, or if score_diff >= 10 in 2H
                if score_diff >= 10 and period >= 3:
                    if ctx.get("script") == "LEADING":
                        team_is_leading = True
                    else:
                        team_is_trailing = True

        # A. Trailing Script (Negative Game Script: trailing by 10+ in 2H)
        if team_is_trailing or (is_live and period >= 3 and score_diff >= 10 and not team_is_leading):
            if any(k in stat_upper for k in ("PASS", "REC", "TARGET")):
                boost = 1.18
                mult *= boost
                notes.append(
                    f"🏈 NFL Trailing Game Script (-{max(10, score_diff)} in Q{period}): Pass volume surged (+18%)"
                )
            elif any(k in stat_upper for k in ("RUSH", "CARRIES", "RUSH YDS")):
                tax = 0.65
                mult *= tax
                notes.append(
                    f"🏈 NFL Trailing Game Script (-{max(10, score_diff)} in Q{period}): Abandoned run game (-35% rush tax)"
                )

        # B. Clock-Killing Positive Script (Leading by 10+ in Q4)
        elif team_is_leading or (is_live and period >= 4 and score_diff >= 10 and not team_is_trailing):
            if any(k in stat_upper for k in ("RUSH", "CARRIES", "RUSH YDS")):
                boost = 1.28
                mult *= boost
                notes.append(
                    f"⏰ NFL Clock-Killing Script (+{max(10, score_diff)} in Q4): Ground-and-pound chew clock (+28% rush volume)"
                )
            elif any(k in stat_upper for k in ("PASS", "PASS YDS", "PASSING")):
                tax = 0.80
                mult *= tax
                notes.append(
                    f"⏰ NFL Clock-Killing Script (+{max(10, score_diff)} in Q4): Conservative run-heavy clock drain (-20% pass tax)"
                )

        # C. Two-Minute Hurry-Up / Red-Zone Personnel
        if is_live and (ctx.get("hurry_up") or (period in (2, 4) and score_diff in (1, 2, 3, 4, 5, 6, 7, 8))):
            if "REC" in stat_upper or "PASS" in stat_upper:
                boost = 1.10
                mult *= boost
                notes.append("⏱️ NFL Two-Minute Drill: Up-tempo hurry-up passing rate (+10%)")

        return mult, notes

    # -------------------------------------------------------------
    # 3. MLB: Pitch Count & Times-Through-The-Order Penalty (TTOP)
    # -------------------------------------------------------------
    @classmethod
    def _mlb_coaching_tactics(
        cls,
        stat_upper: str,
        baseline_mean: float,
        telemetry: Optional[GameTelemetry],
        ctx: Dict[str, Any],
        archetype: str,
    ) -> Tuple[float, List[str]]:
        mult = 1.0
        notes: List[str] = []

        pitch_count = float(ctx.get("pitches", 0.0))
        batters_faced = int(ctx.get("batters_faced", 0))
        inning = getattr(telemetry, "period", 0) if telemetry else int(ctx.get("inning", 1))

        # A. High Pitch Count Fatigue (>= 90-95 pitches)
        if pitch_count >= 90.0:
            if any(k in stat_upper for k in ("STRIKEOUT", "K", "OUTS")):
                tax = 0.70
                mult *= tax
                notes.append(
                    f"⚠️ MLB High Pitch Count ({int(pitch_count)} pitches): Imminent hook hazard (-30% K tax)"
                )
            elif any(k in stat_upper for k in ("HITS", "EARNED RUNS", "RUNS", "HR")):
                boost = 1.25
                mult *= boost
                notes.append(
                    f"⚠️ MLB Pitcher Fatigue ({int(pitch_count)} pitches): Hard-hit contact inflation (+25% hits/ER)"
                )

        # B. TTOP: 3rd Time Through The Order (Pitches >= 75 or Batters Faced >= 18 or Inning >= 5)
        elif pitch_count >= 75.0 or batters_faced >= 18 or inning >= 5:
            if any(k in stat_upper for k in ("STRIKEOUT", "K")):
                tax = 0.82
                mult *= tax
                notes.append(
                    f"⚾ MLB TTOP Penalty (3rd Pass, Inn {inning}, {int(pitch_count)}p): Pitcher whiff rate decays (-18% K tax)"
                )
            elif any(k in stat_upper for k in ("HITS", "TOTAL BASES", "HR", "RUNS")):
                boost = 1.20
                mult *= boost
                notes.append(
                    f"⚾ MLB TTOP Penalty (3rd Pass, Inn {inning}): Batter OPS & contact rate surge (+20%)"
                )

        return mult, notes

    # -------------------------------------------------------------
    # 4. NHL: In-Game Flow & Extra-Attacker Engine
    # -------------------------------------------------------------
    @classmethod
    def _nhl_coaching_tactics(
        cls,
        stat_upper: str,
        baseline_mean: float,
        telemetry: Optional[GameTelemetry],
        ctx: Dict[str, Any],
        archetype: str,
    ) -> Tuple[float, List[str]]:
        mult = 1.0
        notes: List[str] = []

        period = getattr(telemetry, "period", 0) if telemetry else int(ctx.get("period", 0))
        score_diff = getattr(telemetry, "score_diff", 0) if telemetry else int(ctx.get("score_diff", 0))
        is_live = getattr(telemetry, "is_live", False) if telemetry else bool(ctx.get("is_live", False))
        team_is_trailing = bool(ctx.get("is_trailing", False))

        # A. Empty Net / 6-on-5 Extra Attacker late in 3rd Period (diff 1 or 2)
        if (is_live and period >= 3 and score_diff in (1, 2)) or ctx.get("empty_net"):
            if team_is_trailing or ctx.get("trailing_skater"):
                if any(k in stat_upper for k in ("SHOTS", "SOG", "POINTS", "PTS", "GOALS", "AST")):
                    boost = 1.40
                    mult *= boost
                    notes.append("🏒 NHL 6-on-5 Extra Attacker: Goalie pulled late P3 -> Trailing SOG/points surge (+40%)")
            else:
                if any(k in stat_upper for k in ("GOALS", "POINTS", "PTS")):
                    boost = 1.35
                    mult *= boost
                    notes.append("🏒 NHL Empty-Net Finish: Leading team top forwards open-net goal probability (+35%)")

        return mult, notes

    # -------------------------------------------------------------
    # 5. TENNIS: Set Fatigue & Break Point Leverage Engine
    # -------------------------------------------------------------
    @classmethod
    def _tennis_coaching_tactics(
        cls,
        stat_upper: str,
        baseline_mean: float,
        telemetry: Optional[GameTelemetry],
        ctx: Dict[str, Any],
    ) -> Tuple[float, List[str]]:
        mult = 1.0
        notes: List[str] = []
        set_num = getattr(telemetry, "period", 0) if telemetry else int(ctx.get("set", 1))

        if set_num >= 3:
            if "ACES" in stat_upper:
                mult *= 0.90
                notes.append(f"🎾 Tennis Deciding Set {set_num}: Serve fatigue tax (-10% aces)")
            elif "GAMES" in stat_upper:
                mult *= 1.15
                notes.append(f"🎾 Tennis Deciding Set {set_num}: Match pushed to distance (+15% games)")

        return mult, notes


sport_coaching_router = SportCoachingRouter()
