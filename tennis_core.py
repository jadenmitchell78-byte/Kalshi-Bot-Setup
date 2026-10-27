# ~/peezy_bot/tennis_core.py
"""
Tennis-Specific Surface & Playstyle Simulation Engine.
Models surface physics (friction, bounce, speed) and tactical playstyle matchups
using 10,000-draw Monte Carlo game & set state Markov chains.
"""

import datetime
import json
import math
import random
import re
import requests
import time
import traceback
import uuid
from typing import Any, Dict, List, Optional, Tuple

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    np = None
    HAS_NUMPY = False

# Surface Speed & Friction Coefficients
SURFACE_MODIFIERS = {
    "CLAY": {"hold_mult": 0.93, "break_mult": 1.12, "rally_factor": 1.25},
    "GRASS": {"hold_mult": 1.08, "break_mult": 0.88, "rally_factor": 0.80},
    "HARD": {"hold_mult": 1.00, "break_mult": 1.00, "rally_factor": 1.00},
    "INDOOR_HARD": {"hold_mult": 1.05, "break_mult": 0.95, "rally_factor": 0.90},
}

# Playstyle Matchup Edge Matrix (Player 1 style vs Player 2 style)
PLAYSTYLE_MATCHUPS = {
    ("SERVE_AND_VOLLEY", "COUNTERPUNCHER"): 0.92,       # Counterpuncher neutralizes S&V
    ("SERVE_AND_VOLLEY", "BASELINE_AGGRESSIVE"): 1.04,
    ("BASELINE_AGGRESSIVE", "COUNTERPUNCHER"): 0.95,     # Wall absorbs power
    ("COUNTERPUNCHER", "BASELINE_AGGRESSIVE"): 1.05,
    ("ALL_COURT", "SERVE_AND_VOLLEY"): 1.03,
}

# Top Tennis Player Database (Surface Ratings & Playstyles)
TENNIS_PLAYER_DB = {
    "CARLOS ALCARAZ": {
        "style": "ALL_COURT",
        "surface_skill": {"CLAY": 1.08, "GRASS": 1.06, "HARD": 1.09, "INDOOR_HARD": 1.04},
        "base_hold": 0.86,
    },
    "JANNIK SINNER": {
        "style": "BASELINE_AGGRESSIVE",
        "surface_skill": {"CLAY": 1.02, "GRASS": 1.05, "HARD": 1.11, "INDOOR_HARD": 1.12},
        "base_hold": 0.89,
    },
    "NOVAK DJOKOVIC": {
        "style": "COUNTERPUNCHER",
        "surface_skill": {"CLAY": 1.09, "GRASS": 1.07, "HARD": 1.12, "INDOOR_HARD": 1.10},
        "base_hold": 0.88,
    },
    "DANIIL MEDVEDEV": {
        "style": "COUNTERPUNCHER",
        "surface_skill": {"CLAY": 0.96, "GRASS": 0.98, "HARD": 1.08, "INDOOR_HARD": 1.09},
        "base_hold": 0.85,
    },
    "ALEXANDER ZVEREV": {
        "style": "BASELINE_AGGRESSIVE",
        "surface_skill": {"CLAY": 1.06, "GRASS": 0.97, "HARD": 1.05, "INDOOR_HARD": 1.06},
        "base_hold": 0.87,
    },
    "TAYLOR FRITZ": {
        "style": "BASELINE_AGGRESSIVE",
        "surface_skill": {"CLAY": 0.98, "GRASS": 1.04, "HARD": 1.06, "INDOOR_HARD": 1.07},
        "base_hold": 0.88,
    },
    "CASPER RUUD": {
        "style": "COUNTERPUNCHER",
        "surface_skill": {"CLAY": 1.10, "GRASS": 0.92, "HARD": 1.01, "INDOOR_HARD": 1.00},
        "base_hold": 0.84,
    },
    "STEFANOS TSITSIPAS": {
        "style": "ALL_COURT",
        "surface_skill": {"CLAY": 1.07, "GRASS": 0.99, "HARD": 1.03, "INDOOR_HARD": 1.04},
        "base_hold": 0.87,
    },
    "ANDREY RUBLEV": {
        "style": "BASELINE_AGGRESSIVE",
        "surface_skill": {"CLAY": 1.03, "GRASS": 1.00, "HARD": 1.05, "INDOOR_HARD": 1.06},
        "base_hold": 0.86,
    },
    "ALEX DE MINAUR": {
        "style": "COUNTERPUNCHER",
        "surface_skill": {"CLAY": 0.97, "GRASS": 1.05, "HARD": 1.04, "INDOOR_HARD": 1.06},
        "base_hold": 0.81,
    },
    "HOLGER RUNE": {
        "style": "ALL_COURT",
        "surface_skill": {"CLAY": 1.04, "GRASS": 0.99, "HARD": 1.03, "INDOOR_HARD": 1.05},
        "base_hold": 0.84,
    },
    "HUBERT HURKACZ": {
        "style": "SERVE_AND_VOLLEY",
        "surface_skill": {"CLAY": 0.96, "GRASS": 1.08, "HARD": 1.05, "INDOOR_HARD": 1.07},
        "base_hold": 0.91,
    },
    "FRANCES TIAFOE": {
        "style": "ALL_COURT",
        "surface_skill": {"CLAY": 0.98, "GRASS": 1.02, "HARD": 1.04, "INDOOR_HARD": 1.03},
        "base_hold": 0.83,
    },
    "IGA SWIATEK": {
        "style": "COUNTERPUNCHER",
        "surface_skill": {"CLAY": 1.15, "GRASS": 0.98, "HARD": 1.07, "INDOOR_HARD": 1.05},
        "base_hold": 0.82,
    },
    "ARYNA SABALENKA": {
        "style": "BASELINE_AGGRESSIVE",
        "surface_skill": {"CLAY": 1.04, "GRASS": 1.06, "HARD": 1.10, "INDOOR_HARD": 1.11},
        "base_hold": 0.84,
    },
    "COCO GAUFF": {
        "style": "COUNTERPUNCHER",
        "surface_skill": {"CLAY": 1.06, "GRASS": 1.01, "HARD": 1.08, "INDOOR_HARD": 1.05},
        "base_hold": 0.79,
    },
}


def resolve_tennis_player(name: str) -> Tuple[str, Dict[str, Any]]:
    """Fuzzy lookup against TENNIS_PLAYER_DB."""
    clean = name.strip().upper()
    if clean in TENNIS_PLAYER_DB:
        return clean, TENNIS_PLAYER_DB[clean]

    for db_name, data in TENNIS_PLAYER_DB.items():
        if clean in db_name or any(tok in clean for tok in db_name.split()):
            return db_name, data

    # Default fallback player profile
    return clean.title(), {
        "style": "ALL_COURT",
        "surface_skill": {"CLAY": 1.0, "GRASS": 1.0, "HARD": 1.0, "INDOOR_HARD": 1.0},
        "base_hold": 0.82,
    }


def simulate_tennis_match(
    p1_name: str,
    p2_name: str,
    surface: str = "HARD",
    best_of: int = 3,
    iterations: int = 10000,
) -> Dict[str, Any]:
    surface_u = surface.upper() if surface.upper() in SURFACE_MODIFIERS else "HARD"

    canonical_p1, p1_data = resolve_tennis_player(p1_name)
    canonical_p2, p2_data = resolve_tennis_player(p2_name)

    # Surface & Playstyle Interaction Adjustments
    surf_cfg = SURFACE_MODIFIERS[surface_u]
    p1_skill = p1_data["surface_skill"].get(surface_u, 1.0)
    p2_skill = p2_data["surface_skill"].get(surface_u, 1.0)

    matchup_key = (p1_data["style"], p2_data["style"])
    matchup_mod = PLAYSTYLE_MATCHUPS.get(matchup_key, 1.00)

    # Calculate Adjusted Hold Probabilities
    p1_hold = min(0.96, max(0.65, p1_data["base_hold"] * surf_cfg["hold_mult"] * p1_skill * matchup_mod))
    p2_hold = min(0.96, max(0.65, p2_data["base_hold"] * surf_cfg["hold_mult"] * p2_skill * (2.0 - matchup_mod)))

    p1_match_wins = 0
    total_games_list: List[int] = []
    sets_to_win = 2 if best_of == 3 else 3

    if HAS_NUMPY:
        rng = np.random.default_rng()
        rand_fn = rng.random
    else:
        rand_fn = random.random

    tb_p1_prob = p1_hold / (p1_hold + p2_hold)

    for _ in range(iterations):
        p1_sets = 0
        p2_sets = 0
        match_games = 0

        while p1_sets < sets_to_win and p2_sets < sets_to_win:
            p1_games = 0
            p2_games = 0

            while True:
                server = 1 if (p1_games + p2_games) % 2 == 0 else 2

                if server == 1:
                    holds = rand_fn() < p1_hold
                    if holds:
                        p1_games += 1
                    else:
                        p2_games += 1
                else:
                    holds = rand_fn() < p2_hold
                    if holds:
                        p2_games += 1
                    else:
                        p1_games += 1

                match_games += 1

                if p1_games >= 6 and (p1_games - p2_games) >= 2:
                    p1_sets += 1
                    break
                elif p2_games >= 6 and (p2_games - p1_games) >= 2:
                    p2_sets += 1
                    break
                elif p1_games == 6 and p2_games == 6:
                    tb_win = rand_fn() < tb_p1_prob
                    if tb_win:
                        p1_sets += 1
                    else:
                        p2_sets += 1
                    match_games += 1
                    break

        if p1_sets > p2_sets:
            p1_match_wins += 1
        total_games_list.append(match_games)

    p1_win_prob = (p1_match_wins / iterations) * 100.0
    p2_win_prob = 100.0 - p1_win_prob

    if HAS_NUMPY:
        avg_total_games = float(np.mean(total_games_list))
        game_std = float(np.std(total_games_list))
    else:
        avg_total_games = sum(total_games_list) / len(total_games_list)
        variance = sum((x - avg_total_games) ** 2 for x in total_games_list) / len(total_games_list)
        game_std = math.sqrt(variance)

    p1_display = canonical_p1.title()
    p2_display = canonical_p2.title()

    if p1_win_prob >= 52.0:
        verdict = f"{p1_display} ML ({p1_win_prob:.1f}%)"
    elif p2_win_prob >= 52.0:
        verdict = f"{p2_display} ML ({p2_win_prob:.1f}%)"
    else:
        verdict = f"PASS / PICK'EM ({p1_win_prob:.1f}% vs {p2_win_prob:.1f}%)"

    return {
        "sport": "TENNIS",
        "surface": surface_u,
        "matchup": f"{p1_display} vs {p2_display}",
        "p1": p1_display,
        "p1_win_prob": round(p1_win_prob, 1),
        "p1_style": p1_data["style"],
        "p1_hold_pct": round(p1_hold * 100, 1),
        "p2": p2_display,
        "p2_win_prob": round(p2_win_prob, 1),
        "p2_style": p2_data["style"],
        "p2_hold_pct": round(p2_hold * 100, 1),
        "avg_total_games": round(avg_total_games, 1),
        "game_std": round(game_std, 1),
        "recommended_side": verdict,
    }


def format_tennis_card(sim_res: Dict[str, Any]) -> str:
    """Renders the HTML quant card for Telegram."""
    return (
        f"🎾 <b>PEZZY QUANT TERMINAL • TENNIS</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⚔️ <b>Match:</b> {sim_res['matchup']}\n"
        f"🏟 <b>Surface:</b> <code>{sim_res['surface']}</code>\n\n"
        f"🧬 <b>Playstyles & Hold Rates:</b>\n"
        f"• {sim_res['p1']}: <code>{sim_res['p1_style']}</code> ({sim_res.get('p1_hold_pct', 82.0)}% hold)\n"
        f"• {sim_res['p2']}: <code>{sim_res['p2_style']}</code> ({sim_res.get('p2_hold_pct', 82.0)}% hold)\n\n"
        f"📊 <b>10,000-Draw Monte Carlo Projections:</b>\n"
        f"• {sim_res['p1']} Win Prob: <b>{sim_res['p1_win_prob']}%</b>\n"
        f"• {sim_res['p2']} Win Prob: <b>{sim_res['p2_win_prob']}%</b>\n"
        f"• Expected Total Games: <code>{sim_res['avg_total_games']} ± {sim_res['game_std']}</code>\n\n"
        f"🔥 <b>Model Verdict:</b> <code>{sim_res['recommended_side']}</code>"
    )
