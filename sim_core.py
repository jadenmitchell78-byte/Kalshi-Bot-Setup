"""
sim_core.py - the prop model from run_bridge_player_simulation, with NO network,
no Kalshi, no formatting. Same math, so the live bot and the backtest can share it.

  core_simulate(...)  -> (draws, info)       # use this inside sim_prop_bridge too
  my_sim(row, history, n, rng)               # plugs into sim_backtest.py

Backtest data columns used (all optional except date/player/stat/actual/threshold):
  position  : "G", "F", "C", "G-F" ...         (per row)
  is_home   : True/False                        (per row)
  opp_factor: opponent multiplier for this stat, computed ONLY from games before
              the date (no leakage). Defaults to 1.0.
  minutes   : minutes played in each past game (history rows)
  prior     : player's own prior-season average for this stat (optional)
Run:
  python sim_backtest.py --data props.csv --sim sim_core:my_sim --segments stat,position
"""
import hashlib
import math
from typing import Any, Dict, Optional, Tuple

import numpy as np

K_SHRINK = 6.0           # games of prior weight (same as live model)
GAMMA_SHAPE_DISCRETE = 4.2

DISCRETE = {"3PM", "3PT", "SOG", "REB", "AST", "K", "SO", "STRIKEOUTS"}
DISCRETE_STD_FLOOR = {"3PM": 1.1, "3PT": 1.1, "SOG": 1.1}


def make_rng(seed_key: Any = None) -> np.random.Generator:
    """Deterministic RNG seeded via SHA-256 so results never change across Python restarts."""
    if isinstance(seed_key, np.random.Generator):
        return seed_key
    if seed_key is None:
        return np.random.default_rng()
    if isinstance(seed_key, int):
        return np.random.default_rng(seed_key)
    digest = hashlib.sha256(str(seed_key).encode("utf-8")).digest()
    seed_int = int.from_bytes(digest[:8], "little")
    return np.random.default_rng(seed_int)


def kalshi_fee_prob(price_prob: float) -> float:
    """Kalshi taker fee expressed in probability units (0.07 * P * (1 - P))."""
    p = max(0.01, min(0.99, float(price_prob)))
    return 0.07 * p * (1.0 - p)


def prior_for(stat: str, position: str = "") -> float:
    pos = (position or "").upper()
    c = "C" in pos and "G" not in pos
    g = "G" in pos
    table = {
        "3PM": 0.4 if c else (2.4 if g else 1.6),
        "PTS": 14.5 if c else (20.5 if g else 17.5),
        "REB": 9.2 if c else (4.2 if g else 6.8),
        "AST": 2.8 if c else (5.6 if g else 3.6),
        "PRA": 26.5 if c else (30.3 if g else 27.9),
        "PASS_YDS": 235.0,
        "RUSH_YDS": 60.0,
        "REC_YDS": 52.0,
        "SOG": 2.8,
        "K": 5.5,
    }
    return table.get(stat.upper(), 18.0)


def core_simulate(
    stat: str,
    season_avg: Optional[float],
    games_played: int,
    last5_avg: Optional[float] = None,
    season_min: float = 31.5,
    last5_min: Optional[float] = None,
    is_home: Optional[bool] = None,
    opp_factor: float = 1.0,
    position: str = "",
    n: int = 10000,
    rng: Any = None,
    prior: Optional[float] = None,
    injury_boost: float = 1.0,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Return (draws ndarray, info dict). `prior` overrides the position prior
    (e.g. the player's own last-season average - recommended early in the season)."""
    rng = make_rng(rng)
    stat_u = (stat or "PTS").upper()
    prior_val = float(prior) if prior is not None else prior_for(stat_u, position)
    raw = prior_val if season_avg is None else float(season_avg)

    gp = max(0, int(games_played or 0))
    shrunk = (gp * raw + K_SHRINK * prior_val) / (gp + K_SHRINK) if gp < 20 else raw
    blended = (
        0.75 * shrunk + 0.25 * float(last5_avg)
        if (last5_avg is not None and not math.isnan(float(last5_avg)) and gp >= 2)
        else shrunk
    )

    s_min = float(season_min) if (season_min is not None and float(season_min) > 0) else 31.5
    if last5_min is not None and not math.isnan(float(last5_min)) and float(last5_min) > 0:
        proj_min = 0.65 * s_min + 0.35 * float(last5_min)
    else:
        proj_min = s_min

    # Divide by max(8.0, s_min) so bench players (<18 MPG) are not artificially deflated
    min_factor = max(0.85, min(1.15, proj_min / max(8.0, s_min))) if s_min > 0 else 1.0
    loc = 1.02 if is_home is True else (0.985 if is_home is False else 1.0)
    inj_mult = max(0.90, min(1.12, float(injury_boost or 1.0)))
    mean = max(0.2, blended * min_factor * loc * float(opp_factor or 1.0) * inj_mult)

    if stat_u in DISCRETE:
        lam = rng.gamma(GAMMA_SHAPE_DISCRETE, mean / GAMMA_SHAPE_DISCRETE, n)
        draws = rng.poisson(np.maximum(lam, 0.05)).astype(float)
        if stat_u in ("3PM", "3PT", "SOG"):
            std = max(1.1, math.sqrt(mean) * 0.92)
        else:
            std = max(1.6, math.sqrt(mean) * 0.98)
    else:
        if stat_u in ("PASS_YDS", "PASS"):
            std = max(25.0, mean * 0.16)
        elif stat_u in ("RUSH_YDS", "RUSH", "REC_YDS", "REC"):
            std = max(15.0, mean * 0.28)
        else:  # PTS, PRA
            std = max(4.8, mean * 0.27)
        chi = np.maximum(0.25, rng.gamma(2.5, 2.0 / 5.0, n))
        draws = np.maximum(0.0, mean + rng.normal(0, 1, n) / np.sqrt(chi) * std * 0.88)

    info = dict(
        mean=mean,
        std=std,
        prior=prior_val,
        raw=raw,
        shrunk=shrunk,
        blended=blended,
        proj_min=proj_min,
        min_factor=min_factor,
        loc=loc,
        opp_factor=float(opp_factor or 1.0),
        injury_boost=inj_mult,
        is_discrete=(stat_u in DISCRETE),
    )
    return draws, info


def my_sim(row: Any, history: Any, n: int = 10000, rng: Any = None) -> np.ndarray:
    """Adapter for sim_backtest.py: build inputs from PRIOR games only."""
    a = history["actual"].to_numpy(dtype=float) if len(history) else np.array([], dtype=float)
    gp = len(a)
    last5 = float(a[-5:].mean()) if gp >= 2 else None
    mins = history["minutes"].to_numpy(dtype=float) if ("minutes" in history and len(history)) else None
    season_min = float(mins.mean()) if mins is not None and len(mins) else 31.5
    last5_min = float(mins[-5:].mean()) if mins is not None and len(mins) >= 2 else None

    prior_override = getattr(row, "prior", None)
    if prior_override is not None:
        try:
            if math.isnan(float(prior_override)):
                prior_override = None
        except (TypeError, ValueError):
            prior_override = None

    is_home_val = getattr(row, "is_home", None)
    if isinstance(is_home_val, str):
        is_home_val = is_home_val.strip().lower() in ("true", "1", "home", "yes", "h")
    elif is_home_val is not None:
        try:
            if math.isnan(float(is_home_val)):
                is_home_val = None
            else:
                is_home_val = bool(int(is_home_val)) if isinstance(is_home_val, (int, float)) else bool(is_home_val)
        except (TypeError, ValueError):
            is_home_val = None

    opp_f = getattr(row, "opp_factor", 1.0)
    try:
        opp_f = 1.0 if (opp_f is None or math.isnan(float(opp_f))) else float(opp_f)
    except (TypeError, ValueError):
        opp_f = 1.0

    draws, _ = core_simulate(
        stat=str(getattr(row, "stat", "PTS")),
        season_avg=float(a.mean()) if gp else None,
        games_played=gp,
        last5_avg=last5,
        season_min=season_min,
        last5_min=last5_min,
        is_home=is_home_val,
        opp_factor=opp_f,
        position=str(getattr(row, "position", "") or ""),
        n=n,
        rng=rng,
        prior=prior_override,
    )
    return draws
