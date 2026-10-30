#!/usr/bin/env python3
"""
sim_backtest.py - Walk-forward backtest harness for player prop Monte Carlo simulations.

Usage:
  python sim_backtest.py --data props.csv --sim sim_core:my_sim --segments stat,position
"""
import argparse
import csv
import importlib
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from sim_core import DISCRETE, kalshi_fee_prob, make_rng


class _SeriesView:
    """Lightweight pandas-compatible Series view supporting .to_numpy(dtype=float)."""

    def __init__(self, values: Sequence[Any]) -> None:
        self._values = list(values)

    def to_numpy(self, dtype: Any = float) -> np.ndarray:
        out: List[float] = []
        for v in self._values:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                out.append(0.0)
        return np.asarray(out, dtype=dtype)

    def __len__(self) -> int:
        return len(self._values)


class HistoryFrame:
    """Lightweight pandas-compatible DataFrame view for prior player game logs."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def __len__(self) -> int:
        return len(self._rows)

    def __contains__(self, col: str) -> bool:
        return bool(self._rows and col in self._rows[0])

    def __getitem__(self, col: str) -> _SeriesView:
        return _SeriesView([r.get(col) for r in self._rows])

    @property
    def actual(self) -> _SeriesView:
        return self["actual"]

    @property
    def minutes(self) -> _SeriesView:
        return self["minutes"]


@dataclass
class PropRow:
    date: str
    player: str
    stat: str
    actual: float
    threshold: float
    minutes: float = 31.5
    is_home: Optional[bool] = None
    opp_factor: float = 1.0
    position: str = ""
    prior: Optional[float] = None
    market_prob: Optional[float] = None


def bet_pnl(pred_prob: float, market_ask_prob: float, hit_over: bool, min_edge: float = 0.03) -> Tuple[float, str]:
    """
    Fee-aware Kalshi 1-contract PnL evaluator checking both YES (OVER) and NO (UNDER).
    Kalshi taker fee = 0.07 * P * (1 - P).
    Returns (pnl_dollars, action_taken: 'YES' | 'NO' | 'PASS').
    """
    yes_ask = max(0.02, min(0.98, float(market_ask_prob)))
    no_ask = max(0.02, min(0.98, 1.0 - yes_ask + 0.02))  # 2c spread assumption if no_ask not explicit

    yes_fee = kalshi_fee_prob(yes_ask)
    no_fee = kalshi_fee_prob(no_ask)

    yes_ev = pred_prob - yes_ask - yes_fee
    no_ev = (1.0 - pred_prob) - no_ask - no_fee

    if yes_ev >= no_ev and yes_ev >= min_edge:
        pnl = (1.0 - yes_ask - yes_fee) if hit_over else (-yes_ask - yes_fee)
        return pnl, "YES"
    if no_ev > yes_ev and no_ev >= min_edge:
        pnl = (1.0 - no_ask - no_fee) if (not hit_over) else (-no_ask - no_fee)
        return pnl, "NO"
    return 0.0, "PASS"


def baseline_prob(row: PropRow, history: HistoryFrame) -> float:
    """Rolling-average Poisson / Normal baseline probability P(X > threshold)."""
    arr = history.actual.to_numpy(dtype=float)
    mean = float(arr.mean()) if len(arr) else (row.prior if row.prior is not None else float(row.threshold))
    mean = max(0.2, mean)
    k_floor = int(math.floor(row.threshold))
    if row.stat.upper() in DISCRETE:
        # Exact Poisson survival P(X > k_floor)
        cdf = 0.0
        term = math.exp(-min(mean, 60.0))
        for k in range(0, max(0, k_floor) + 1):
            if k > 0:
                term *= mean / k
            cdf += term
        return max(0.005, min(0.995, 1.0 - cdf))
    std = max(4.5, mean * 0.28)
    z = (row.threshold - mean) / std
    p_over = 0.5 * math.erfc(z / math.sqrt(2.0))
    return max(0.005, min(0.995, p_over))


def _load_sim_fn(spec: str) -> Callable[..., np.ndarray]:
    if ":" not in spec:
        raise ValueError(f"--sim must be module:function, got '{spec}'")
    mod_name, fn_name = spec.split(":", 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, fn_name)


def _parse_bool(val: Any) -> Optional[bool]:
    if val is None or str(val).strip() == "":
        return None
    s = str(val).strip().lower()
    if s in ("true", "1", "yes", "home", "h"):
        return True
    if s in ("false", "0", "no", "away", "a"):
        return False
    return None


def _parse_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None or str(val).strip() == "":
        return default
    try:
        f = float(val)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def load_rows(csv_path: str) -> List[PropRow]:
    rows: List[PropRow] = []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            actual = _parse_float(r.get("actual"))
            threshold = _parse_float(r.get("threshold") or r.get("line"))
            if actual is None or threshold is None:
                continue
            # Ensure half-point threshold so > threshold is unambiguous
            if float(threshold).is_integer():
                threshold = float(threshold) - 0.5
            rows.append(
                PropRow(
                    date=str(r.get("date") or "").strip(),
                    player=str(r.get("player") or "").strip(),
                    stat=str(r.get("stat") or "PTS").strip().upper(),
                    actual=float(actual),
                    threshold=float(threshold),
                    minutes=float(_parse_float(r.get("minutes"), 31.5) or 31.5),
                    is_home=_parse_bool(r.get("is_home")),
                    opp_factor=float(_parse_float(r.get("opp_factor"), 1.0) or 1.0),
                    position=str(r.get("position") or "").strip().upper(),
                    prior=_parse_float(r.get("prior"), None),
                    market_prob=_parse_float(r.get("market_prob") or r.get("yes_ask_prob"), None),
                )
            )
    rows.sort(key=lambda x: (x.date, x.player, x.stat, x.threshold))
    return rows


def summarize_metrics(records: List[Dict[str, Any]], label: str) -> str:
    if not records:
        return f"  {label:<18} | n=0"
    n = len(records)
    preds = np.array([r["pred"] for r in records], dtype=float)
    base = np.array([r["base"] for r in records], dtype=float)
    outcomes = np.array([r["outcome"] for r in records], dtype=float)

    brier = float(np.mean((preds - outcomes) ** 2))
    base_brier = float(np.mean((base - outcomes) ** 2))
    bss = 1.0 - (brier / base_brier) if base_brier > 1e-9 else 0.0

    eps = 1e-4
    p_clip = np.clip(preds, eps, 1.0 - eps)
    logloss = float(-np.mean(outcomes * np.log(p_clip) + (1.0 - outcomes) * np.log(1.0 - p_clip)))

    mean_pred = float(np.mean(preds)) * 100.0
    mean_act = float(np.mean(outcomes)) * 100.0
    cal_gap = mean_pred - mean_act

    bets = [r for r in records if r["action"] in ("YES", "NO")]
    pnl_sum = sum(r["pnl"] for r in bets)
    roi = (pnl_sum / len(bets) * 100.0) if bets else 0.0

    return (
        f"  {label:<18} | n={n:<5d} | Brier={brier:.4f} (Base={base_brier:.4f}, Skill={bss:+.2%}) "
        f"| LogLoss={logloss:.4f} | Pred={mean_pred:5.1f}% vs Act={mean_act:5.1f}% (Gap={cal_gap:+.1f}%) "
        f"| Bets={len(bets):<4d} PnL={pnl_sum:+.2f}u (ROI={roi:+.1f}%)"
    )


def run_backtest(
    csv_path: str,
    sim_spec: str = "sim_core:my_sim",
    segments: Sequence[str] = ("stat", "position"),
    n_draws: int = 5000,
    min_history: int = 1,
) -> Dict[str, Any]:
    sim_fn = _load_sim_fn(sim_spec)
    all_rows = load_rows(csv_path)
    if not all_rows:
        raise RuntimeError(f"No valid prop rows found in {csv_path}")

    # Deduplicate unique (player, stat, date) games to build leakage-free prior game history
    game_log_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    seen_game: set = set()
    for r in all_rows:
        g_key = (r.player, r.stat, r.date)
        if g_key not in seen_game:
            seen_game.add(g_key)
            game_log_by_key[(r.player, r.stat)].append(
                {
                    "date": r.date,
                    "actual": r.actual,
                    "minutes": r.minutes,
                    "is_home": r.is_home,
                    "opp_factor": r.opp_factor,
                }
            )

    for k in game_log_by_key:
        game_log_by_key[k].sort(key=lambda x: x["date"])

    eval_records: List[Dict[str, Any]] = []
    for idx, row in enumerate(all_rows):
        full_hist = game_log_by_key[(row.player, row.stat)]
        prior_games = [g for g in full_hist if g["date"] < row.date]
        if len(prior_games) < min_history and row.prior is None:
            continue

        hist_frame = HistoryFrame(prior_games)
        rng = make_rng(f"{row.player}:{row.stat}:{row.date}:{row.threshold}:{idx}")
        draws = np.asarray(sim_fn(row, hist_frame, n_draws, rng), dtype=float)
        pred_prob = float(np.mean(draws > row.threshold))
        pred_prob = max(0.005, min(0.995, pred_prob))
        base_p = baseline_prob(row, hist_frame)
        hit = row.actual > row.threshold

        mkt_p = row.market_prob if row.market_prob is not None else base_p
        pnl, action = bet_pnl(pred_prob, mkt_p, hit)

        eval_records.append(
            {
                "date": row.date,
                "player": row.player,
                "stat": row.stat,
                "position": row.position or "UNK",
                "threshold": row.threshold,
                "pred": pred_prob,
                "base": base_p,
                "outcome": 1.0 if hit else 0.0,
                "pnl": pnl,
                "action": action,
            }
        )

    print("=" * 112)
    print(f"PEEZY BACKTEST REPORT | Data: {csv_path} | Sim: {sim_spec} | Evaluated Props: {len(eval_records)}")
    print("=" * 112)
    print(summarize_metrics(eval_records, "OVERALL"))

    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        print("-" * 112)
        print(f"SEGMENT BY: {seg.upper()}")
        by_val: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for rec in eval_records:
            by_val[str(rec.get(seg, "UNK"))].append(rec)
        for val_k in sorted(by_val.keys()):
            print(summarize_metrics(by_val[val_k], f"{seg}={val_k}"))

    # Calibration table across deciles
    if eval_records:
        print("-" * 112)
        print("CALIBRATION DECILES (Predicted Prob vs Observed Hit Rate):")
        bins = np.linspace(0.0, 1.0, 11)
        for i in range(10):
            lo, hi = bins[i], bins[i + 1]
            in_bin = [
                r for r in eval_records
                if (lo <= r["pred"] < hi if i < 9 else lo <= r["pred"] <= hi)
            ]
            if not in_bin:
                continue
            avg_p = np.mean([r["pred"] for r in in_bin]) * 100.0
            avg_y = np.mean([r["outcome"] for r in in_bin]) * 100.0
            print(f"  [{lo*100:4.0f}% - {hi*100:4.0f}%] | n={len(in_bin):<5d} | Pred={avg_p:5.1f}% | Actual={avg_y:5.1f}% | Diff={avg_p - avg_y:+5.1f}%")
    print("=" * 112)
    return {"records": eval_records}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest player prop simulation models.")
    parser.add_argument("--data", required=True, help="Path to props CSV file")
    parser.add_argument("--sim", default="sim_core:my_sim", help="module:function adapter")
    parser.add_argument("--segments", default="stat,position", help="Comma-separated segment columns")
    parser.add_argument("--n", type=int, default=5000, help="Monte Carlo draws per row")
    parser.add_argument("--min-history", type=int, default=1, help="Minimum prior games required")
    args = parser.parse_args()

    segs = [s.strip() for s in args.segments.split(",") if s.strip()]
    run_backtest(args.data, sim_spec=args.sim, segments=segs, n_draws=args.n, min_history=args.min_history)


if __name__ == "__main__":
    main()
