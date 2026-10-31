"""
sim_core.py - Shared player prop simulation core with two-stage Minutes-Variance +
Per-Minute Rate modeling, stat-specific dispersion/std parameters, and tunable
hyperparameters loaded from sim_params.json (when present).

Uses vectorized NumPy (np.random.Generator: rng.normal / rng.gamma / rng.poisson)
automatically when `python-numpy` is installed (50-100x faster), with a seamless
pure-Python fallback if numpy is not yet installed.

  core_simulate(...)  -> (draws, info)       # used inside sim_prop_bridge & backtest
  my_sim(row, history, n, rng)               # plugs into sim_backtest.py & tune_sim.py
"""
import hashlib
import json
import math
import random
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

try:
    import numpy as np  # type: ignore
    HAS_NUMPY = True
except ImportError:
    np = None  # type: ignore
    HAS_NUMPY = False

DISCRETE = {"3PM", "3PT", "SOG", "REB", "AST", "K", "SO", "STRIKEOUTS"}
DISCRETE_STD_FLOOR = {"3PM": 1.1, "3PT": 1.1, "SOG": 1.1}

DEFAULT_PARAMS: Dict[str, Any] = {
    "k_shrink": 8.0,            # prior weight (games) when player's own prior-season avg is known
    "k_pos_prior": 0.75,        # small prior weight (games) when falling back to generic position avg
    "l5_weight": 0.08,          # weight on minutes-normalized L5 form
    "min_l5_weight": 0.25,      # weight on L5 minutes for projected minutes
    "min_cv": 0.13,             # default minutes coefficient of variation (sigma_min / proj_min)
    "min_var_scale": 1.0,       # multiplier on simulated minutes standard deviation
    "loc_home": 1.012,          # home court multiplier (+1.2%)
    "loc_away": 0.988,          # away court multiplier (-1.2%)
    "opp_scale": 0.65,          # shrinkage on (opp_factor - 1.0)
    "emp_std_weight": 0.45,     # blend weight on player's empirical game-to-game std (when gp >= 4)
    "student_df": 10.0,         # degrees of freedom for normalized Student-t continuous draws
    "gamma_shape": {
        "3PM": 4.2,
        "3PT": 4.2,
        "REB": 11.0,
        "AST": 7.5,
        "SOG": 4.5,
        "K": 6.0,
    },
    "std_mult": {
        "PTS": 0.33,
        "PRA": 0.27,
        "PASS_YDS": 0.22,
        "RUSH_YDS": 0.32,
        "REC_YDS": 0.34,
    },
    "std_floor": {
        "PTS": 5.6,
        "PRA": 6.8,
        "PASS_YDS": 28.0,
        "RUSH_YDS": 16.0,
        "REC_YDS": 16.0,
    },
}

PARAMS: Dict[str, Any] = json.loads(json.dumps(DEFAULT_PARAMS))
_PARAMS_PATH = Path(__file__).resolve().parent / "sim_params.json"


def load_saved_params(path: Optional[Path] = None) -> Dict[str, Any]:
    """Loads tuned parameters from sim_params.json if it exists."""
    global PARAMS
    target = path or _PARAMS_PATH
    if target.exists():
        try:
            loaded = json.loads(target.read_text(encoding="utf-8"))
            for k, v in loaded.items():
                if isinstance(v, dict) and isinstance(PARAMS.get(k), dict):
                    PARAMS[k].update(v)
                else:
                    PARAMS[k] = v
        except Exception:
            pass
    return PARAMS


def set_params(overrides: Dict[str, Any]) -> None:
    """Updates active model parameters in-memory (used by tune_sim.py)."""
    global PARAMS
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(PARAMS.get(k), dict):
            PARAMS[k].update(v)
        else:
            PARAMS[k] = v


load_saved_params()

K_SHRINK = float(PARAMS["k_shrink"])
GAMMA_SHAPE_DISCRETE = 4.2


class DrawArray(list):
    """Lightweight pure-Python float array compatible with numpy-style operations."""

    def __init__(self, iterable: Iterable[float] = ()) -> None:
        super().__init__(float(x) for x in iterable)

    def __getitem__(self, item: Any) -> Any:
        res = super().__getitem__(item)
        if isinstance(item, slice):
            return DrawArray(res)
        return res

    def mean(self) -> float:
        return sum(self) / len(self) if self else 0.0

    def std(self) -> float:
        n = len(self)
        if n < 2:
            return 0.0
        m = sum(self) / n
        return math.sqrt(sum((x - m) ** 2 for x in self) / (n - 1))

    def astype(self, _dtype: Any = float) -> "DrawArray":
        return self

    def to_numpy(self, dtype: Any = float) -> Any:
        if HAS_NUMPY:
            return np.asarray(self, dtype=dtype)
        return self

    def __gt__(self, other: Any) -> "DrawArray":
        thr = float(other)
        return DrawArray(1.0 if x > thr else 0.0 for x in self)

    def __ge__(self, other: Any) -> "DrawArray":
        thr = float(other)
        return DrawArray(1.0 if x >= thr else 0.0 for x in self)

    def __lt__(self, other: Any) -> "DrawArray":
        thr = float(other)
        return DrawArray(1.0 if x < thr else 0.0 for x in self)

    def __le__(self, other: Any) -> "DrawArray":
        thr = float(other)
        return DrawArray(1.0 if x <= thr else 0.0 for x in self)


def _seed_int(seed_key: Any) -> Optional[int]:
    if seed_key is None:
        return None
    if isinstance(seed_key, int):
        return seed_key
    digest = hashlib.sha256(str(seed_key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "little")


def make_rng(seed_key: Any = None) -> Any:
    """
    Deterministic RNG seeded via SHA-256.
    Returns np.random.Generator when numpy is installed, else random.Random.
    """
    if HAS_NUMPY and isinstance(seed_key, np.random.Generator):
        return seed_key
    if isinstance(seed_key, random.Random):
        return seed_key
    s_int = _seed_int(seed_key)
    if HAS_NUMPY:
        return np.random.default_rng(s_int)
    return random.Random(s_int)


def _sample_poisson_py(rng: random.Random, lam: float) -> int:
    """Pure-Python Poisson fallback when numpy is not installed."""
    if lam <= 0.0:
        return 0
    if lam < 30.0:
        L_cut = math.exp(-lam)
        k_cnt = 0
        p_acc = 1.0
        while p_acc > L_cut:
            k_cnt += 1
            p_acc *= rng.random()
        return max(0, k_cnt - 1)
    return max(0, int(round(rng.gauss(lam, math.sqrt(lam)))))


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
    emp_std: Optional[float] = None,
    min_std: Optional[float] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, Dict[str, Any]]:
    """
    Two-stage Minutes-Variance + Per-Minute Rate Monte Carlo simulator.
    Vectorized with NumPy when installed (~50-100x faster), with pure-Python fallback.
    """
    cfg = params or PARAMS
    gen = make_rng(rng)
    stat_u = (stat or "PTS").upper()

    has_player_prior = prior is not None and not (isinstance(prior, float) and math.isnan(prior))
    prior_val = float(prior) if has_player_prior else prior_for(stat_u, position)
    raw = prior_val if (season_avg is None or (isinstance(season_avg, float) and math.isnan(season_avg))) else float(season_avg)

    gp = max(0, int(games_played or 0))
    k_eff = float(cfg.get("k_shrink", 8.0)) if has_player_prior else float(cfg.get("k_pos_prior", 0.75))
    shrunk = (gp * raw + k_eff * prior_val) / (gp + k_eff) if (gp + k_eff) > 0 else raw

    s_min = float(season_min) if (season_min is not None and float(season_min) > 0) else 31.5
    has_l5_min = last5_min is not None and not math.isnan(float(last5_min)) and float(last5_min) > 0
    min_l5_w = float(cfg.get("min_l5_weight", 0.25))
    proj_min = (1.0 - min_l5_w) * s_min + min_l5_w * float(last5_min) if has_l5_min else s_min

    # Minutes-normalize L5 stat average before blending so minutes changes are not double-counted
    l5_w = float(cfg.get("l5_weight", 0.08))
    if last5_avg is not None and not math.isnan(float(last5_avg)) and gp >= 3 and l5_w > 0:
        l5_val = float(last5_avg)
        if has_l5_min and float(last5_min) >= 10.0:
            l5_val = l5_val * max(0.80, min(1.25, s_min / float(last5_min)))
        blended = (1.0 - l5_w) * shrunk + l5_w * l5_val
    else:
        blended = shrunk

    min_factor = max(0.85, min(1.15, proj_min / max(8.0, s_min))) if s_min > 0 else 1.0
    loc_home = float(cfg.get("loc_home", 1.012))
    loc_away = float(cfg.get("loc_away", 0.988))
    loc = loc_home if is_home is True else (loc_away if is_home is False else 1.0)

    opp_scale = float(cfg.get("opp_scale", 0.65))
    eff_opp = 1.0 + opp_scale * (float(opp_factor or 1.0) - 1.0)
    inj_mult = max(0.90, min(1.12, float(injury_boost or 1.0)))

    mean = max(0.2, blended * min_factor * loc * eff_opp * inj_mult)
    rate_per_min = mean / max(6.0, proj_min)

    # Stage 1: Minutes distribution parameters
    min_cv = float(cfg.get("min_cv", 0.13))
    min_var_scale = float(cfg.get("min_var_scale", 1.0))
    default_min_sd = max(2.2, min_cv * proj_min)
    if min_std is not None and not math.isnan(float(min_std)) and float(min_std) > 0 and gp >= 4:
        w_m = min(0.65, gp / (gp + 8.0))
        eff_min_sd = ((1.0 - w_m) * default_min_sd + w_m * float(min_std)) * min_var_scale
    else:
        eff_min_sd = default_min_sd * min_var_scale

    min_lo = max(4.0, 0.30 * proj_min)
    min_hi = min(48.0, max(proj_min + 6.0, 1.40 * proj_min))

    is_disc = stat_u in DISCRETE
    use_np = HAS_NUMPY and isinstance(gen, np.random.Generator)

    if is_disc:
        gamma_map = cfg.get("gamma_shape", DEFAULT_PARAMS["gamma_shape"])
        shape_k = float(gamma_map.get(stat_u, GAMMA_SHAPE_DISCRETE))
        var_tot = mean + (mean ** 2) * ((eff_min_sd / max(8.0, proj_min)) ** 2 + 1.0 / shape_k)
        std = math.sqrt(max(0.5, var_tot))

        if use_np:
            if eff_min_sd > 0.05:
                m_draws = np.clip(gen.normal(proj_min, eff_min_sd, n), min_lo, min_hi)
                mu_arr = np.maximum(0.05, rate_per_min * m_draws)
            else:
                mu_arr = mean
            lam = np.maximum(0.03, gen.gamma(shape_k, mu_arr / shape_k, size=n))
            draws = gen.poisson(lam).astype(float)
        else:
            py_rng = gen if isinstance(gen, random.Random) else random.Random(_seed_int(rng))
            draws_list: List[float] = []
            for _ in range(n):
                if eff_min_sd > 0.05:
                    m_draw = min(min_hi, max(min_lo, py_rng.gauss(proj_min, eff_min_sd)))
                    mu_i = max(0.05, rate_per_min * m_draw)
                else:
                    mu_i = mean
                lam_i = max(0.03, py_rng.gammavariate(shape_k, mu_i / shape_k))
                draws_list.append(float(_sample_poisson_py(py_rng, lam_i)))
            draws = DrawArray(draws_list)
    else:
        std_mult_map = cfg.get("std_mult", DEFAULT_PARAMS["std_mult"])
        std_floor_map = cfg.get("std_floor", DEFAULT_PARAMS["std_floor"])
        s_mult = float(std_mult_map.get(stat_u, 0.32))
        s_floor = float(std_floor_map.get(stat_u, 5.5))
        param_std = max(s_floor, mean * s_mult)

        emp_w_max = float(cfg.get("emp_std_weight", 0.45))
        if emp_std is not None and not math.isnan(float(emp_std)) and float(emp_std) > 1.0 and gp >= 4:
            w_e = emp_w_max * min(1.0, gp / (gp + 8.0))
            target_std = (1.0 - w_e) * param_std + w_e * float(emp_std)
        else:
            target_std = param_std

        min_contrib_sd = mean * (eff_min_sd / max(8.0, proj_min)) if eff_min_sd > 0.05 else 0.0
        cond_var = max((0.55 * target_std) ** 2, (target_std ** 2) - (min_contrib_sd ** 2))
        cond_std = math.sqrt(cond_var)
        std = target_std

        df = max(4.0, float(cfg.get("student_df", 10.0)))
        half_df = 0.5 * df
        t_norm = math.sqrt((df - 2.0) / df)

        if use_np:
            if eff_min_sd > 0.05:
                m_draws = np.clip(gen.normal(proj_min, eff_min_sd, n), min_lo, min_hi)
                mu_arr = np.maximum(0.1, rate_per_min * m_draws)
                scale_arr = cond_std * np.sqrt(m_draws / max(6.0, proj_min))
            else:
                mu_arr = mean
                scale_arr = target_std
            chi = np.maximum(0.15, gen.gamma(half_df, 1.0 / half_df, size=n))
            eps = (gen.normal(0.0, 1.0, n) / np.sqrt(chi)) * t_norm
            draws = np.maximum(0.0, mu_arr + eps * scale_arr)
        else:
            py_rng = gen if isinstance(gen, random.Random) else random.Random(_seed_int(rng))
            draws_list = []
            for _ in range(n):
                if eff_min_sd > 0.05:
                    m_draw = min(min_hi, max(min_lo, py_rng.gauss(proj_min, eff_min_sd)))
                    mu_i = max(0.1, rate_per_min * m_draw)
                    scale_i = cond_std * math.sqrt(m_draw / max(6.0, proj_min))
                else:
                    mu_i = mean
                    scale_i = target_std
                chi_i = max(0.15, py_rng.gammavariate(half_df, 1.0 / half_df))
                eps_i = (py_rng.gauss(0.0, 1.0) / math.sqrt(chi_i)) * t_norm
                draws_list.append(max(0.0, mu_i + eps_i * scale_i))
            draws = DrawArray(draws_list)

    info = dict(
        mean=mean,
        std=std,
        prior=prior_val,
        raw=raw,
        shrunk=shrunk,
        blended=blended,
        proj_min=proj_min,
        min_std=eff_min_sd,
        min_factor=min_factor,
        loc=loc,
        opp_factor=eff_opp,
        injury_boost=inj_mult,
        is_discrete=is_disc,
    )
    return draws, info


def my_sim(row: Any, history: Any, n: int = 10000, rng: Any = None) -> Any:
    """Adapter for sim_backtest.py & tune_sim.py: builds inputs strictly from PRIOR games."""
    a = history.actual.to_numpy(dtype=float) if len(history) else DrawArray()
    gp = len(a)
    last5 = float(a[-5:].mean()) if gp >= 2 else None
    emp_sd = float(a.std()) if gp >= 4 else getattr(history, "prior_season_std", None)

    mins = history["minutes"].to_numpy(dtype=float) if ("minutes" in history and len(history)) else None
    prior_min = getattr(history, "prior_season_min", None)
    if mins is not None and len(mins):
        raw_min = float(mins.mean())
        season_min = (gp * raw_min + 4.0 * float(prior_min)) / (gp + 4.0) if (prior_min and gp < 12) else raw_min
        last5_min = float(mins[-5:].mean()) if len(mins) >= 2 else None
        min_sd = float(mins.std()) if len(mins) >= 4 else getattr(history, "prior_season_min_std", None)
    else:
        season_min = float(prior_min) if prior_min else 31.5
        last5_min = None
        min_sd = getattr(history, "prior_season_min_std", None)

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
        emp_std=emp_sd,
        min_std=min_sd,
    )
    return draws
