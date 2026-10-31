#!/usr/bin/env python3
"""
sim_backtest.py - Walk-forward backtest harness for player prop Monte Carlo simulations.
Zero external dependencies (runs in Termux without numpy or pandas).

Reports Brier, Baseline Brier, Brier Skill Score (%), ECE (Expected Calibration Error),
LogLoss, Fee-Aware Kalshi PnL, Segment tables, and 10-bin Calibration Deciles.

Usage:
  python sim_backtest.py
"""
import argparse
import csv
import importlib
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from sim_core import DISCRETE, DrawArray, kalshi_fee_prob, make_rng


def season_start_year(date_str: str) -> int:
    """Returns NBA season start year from YYYY-MM-DD (e.g. '2024-10-25' -> 2024, '2025-03-10' -> 2024)."""
    try:
        y = int(date_str[:4])
        m = int(date_str[5:7])
        return y if m >= 8 else y - 1
    except Exception:
        return 2024


class _SeriesView:
    """Lightweight pandas-compatible Series view supporting .to_numpy(dtype=float)."""

    def __init__(self, values: Sequence[Any]) -> None:
        self._values = list(values)

    def to_numpy(self, dtype: Any = float) -> DrawArray:
        out: List[float] = []
        for v in self._values:
            try:
                out.append(float(v))
            except (TypeError, ValueError):
                out.append(0.0)
        return DrawArray(out)

    def __len__(self) -> int:
        return len(self._values)


class HistoryFrame:
    """Lightweight pandas-compatible DataFrame view for prior player game logs."""

    def __init__(
        self,
        rows: List[Dict[str, Any]],
        prior_season_std: Optional[float] = None,
        prior_season_min: Optional[float] = None,
        prior_season_min_std: Optional[float] = None,
    ) -> None:
        self._rows = rows
        self.prior_season_std = prior_season_std
        self.prior_season_min = prior_season_min
        self.prior_season_min_std = prior_season_min_std

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
    no_ask = max(0.02, min(0.98, 1.0 - yes_ask + 0.02))

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


def compute_ece(records: List[Dict[str, Any]], num_bins: int = 10) -> float:
    """Computes Expected Calibration Error (ECE) across `num_bins` equal-width probability bins."""
    if not records:
        return 0.0
    n_total = len(records)
    ece = 0.0
    for i in range(num_bins):
        lo, hi = i / num_bins, (i + 1) / num_bins
        in_bin = [
            r for r in records
            if (lo <= r["pred"] < hi if i < num_bins - 1 else lo <= r["pred"] <= hi)
        ]
        if not in_bin:
            continue
        avg_p = sum(r["pred"] for r in in_bin) / len(in_bin)
        avg_y = sum(r["outcome"] for r in in_bin) / len(in_bin)
        ece += (len(in_bin) / n_total) * abs(avg_p - avg_y)
    return ece


def _load_sim_fn(spec: str) -> Callable[..., Sequence[float]]:
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
        return f"  {label:<16} | n=0"
    n = len(records)
    brier = sum((r["pred"] - r["outcome"]) ** 2 for r in records) / n
    base_brier = sum((r["base"] - r["outcome"]) ** 2 for r in records) / n
    bss = 1.0 - (brier / base_brier) if base_brier > 1e-9 else 0.0
    ece = compute_ece(records)

    eps = 1e-4
    ll_sum = 0.0
    for r in records:
        p_c = max(eps, min(1.0 - eps, r["pred"]))
        y = r["outcome"]
        ll_sum += -(y * math.log(p_c) + (1.0 - y) * math.log(1.0 - p_c))
    logloss = ll_sum / n

    mean_pred = (sum(r["pred"] for r in records) / n) * 100.0
    mean_act = (sum(r["outcome"] for r in records) / n) * 100.0
    cal_gap = mean_pred - mean_act

    bets = [r for r in records if r["action"] in ("YES", "NO")]
    pnl_sum = sum(r["pnl"] for r in bets)
    roi = (pnl_sum / len(bets) * 100.0) if bets else 0.0

    return (
        f"  {label:<14} | n={n:<5d} | Brier={brier:.4f} (Base={base_brier:.4f}, Skill={bss:+.2%}) "
        f"| ECE={ece:.4f} | LogLoss={logloss:.4f} | Pred={mean_pred:5.1f}% vs Act={mean_act:5.1f}% (Gap={cal_gap:+.1f}%) "
        f"| Bets={len(bets):<4d} PnL={pnl_sum:+.2f}u (ROI={roi:+.1f}%)"
    )


def run_backtest(
    csv_path: str = "props.csv",
    sim_spec: str = "sim_core:my_sim",
    segments: Sequence[str] = ("stat", "position"),
    n_draws: int = 2500,
    min_history: int = 1,
    season_filter: Optional[int] = None,
    quiet: bool = False,
) -> Dict[str, Any]:
    sim_fn = _load_sim_fn(sim_spec)
    all_rows = load_rows(csv_path)
    if not all_rows:
        raise RuntimeError(f"No valid prop rows found in {csv_path}")

    game_log_by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    seen_game: set = set()
    for r in all_rows:
        g_key = (r.player, r.stat, r.date)
        if g_key not in seen_game:
            seen_game.add(g_key)
            game_log_by_key[(r.player, r.stat)].append(
                {
                    "date": r.date,
                    "season_year": season_start_year(r.date),
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
        row_s_yr = season_start_year(row.date)
        if season_filter is not None and row_s_yr != season_filter:
            continue

        full_hist = game_log_by_key[(row.player, row.stat)]
        all_prior = [g for g in full_hist if g["date"] < row.date]
        curr_season_prior = [g for g in all_prior if g["season_year"] == row_s_yr]
        prev_season_games = [g for g in all_prior if g["season_year"] < row_s_yr]

        # Populate prior-season stats strictly from earlier seasons if not already set
        if row.prior is None and prev_season_games:
            row.prior = sum(g["actual"] for g in prev_season_games) / len(prev_season_games)

        if len(curr_season_prior) < min_history and row.prior is None:
            continue

        prior_sd = None
        prior_min = None
        prior_min_sd = None
        if len(prev_season_games) >= 4:
            p_acts = DrawArray(g["actual"] for g in prev_season_games)
            p_mins = DrawArray(g["minutes"] for g in prev_season_games)
            prior_sd = p_acts.std()
            prior_min = p_mins.mean()
            prior_min_sd = p_mins.std()

        hist_frame = HistoryFrame(
            curr_season_prior,
            prior_season_std=prior_sd,
            prior_season_min=prior_min,
            prior_season_min_std=prior_min_sd,
        )
        base_frame = HistoryFrame(all_prior if all_prior else curr_season_prior)

        rng = make_rng(f"{row.player}:{row.stat}:{row.date}:{row.threshold}:{idx}")
        draws = sim_fn(row, hist_frame, n_draws, rng)
        n_d = max(1, len(draws))
        hits_over = sum(1 for d in draws if float(d) > row.threshold)
        pred_prob = max(0.005, min(0.995, hits_over / n_d))
        base_p = baseline_prob(row, base_frame)
        hit = row.actual > row.threshold

        mkt_p = row.market_prob if row.market_prob is not None else base_p
        pnl, action = bet_pnl(pred_prob, mkt_p, hit)

        eval_records.append(
            {
                "date": row.date,
                "season_year": row_s_yr,
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

    if not quiet:
        s_lbl = f" | Season: {season_filter}-{str(season_filter+1)[-2:]}" if season_filter else ""
        print("=" * 108)
        print(f"PEEZY BACKTEST REPORT | Data: {csv_path}{s_lbl} | Sim: {sim_spec} | Props: {len(eval_records)}")
        print("=" * 108)
        print(summarize_metrics(eval_records, "OVERALL"))

        for seg in segments:
            seg = seg.strip()
            if not seg:
                continue
            print("-" * 108)
            print(f"SEGMENT BY: {seg.upper()}")
            by_val: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
            for rec in eval_records:
                by_val[str(rec.get(seg, "UNK"))].append(rec)
            for val_k in sorted(by_val.keys()):
                print(summarize_metrics(by_val[val_k], f"{seg}={val_k}"))

        if eval_records:
            print("-" * 108)
            ece_val = compute_ece(eval_records)
            print(f"CALIBRATION DECILES (Overall ECE = {ece_val:.4f}):")
            for i in range(10):
                lo, hi = i * 0.1, (i + 1) * 0.1
                in_bin = [
                    r for r in eval_records
                    if (lo <= r["pred"] < hi if i < 9 else lo <= r["pred"] <= hi)
                ]
                if not in_bin:
                    continue
                avg_p = (sum(r["pred"] for r in in_bin) / len(in_bin)) * 100.0
                avg_y = (sum(r["outcome"] for r in in_bin) / len(in_bin)) * 100.0
                print(
                    f"  [{lo*100:4.0f}% - {hi*100:4.0f}%] | n={len(in_bin):<5d} | "
                    f"Pred={avg_p:5.1f}% | Actual={avg_y:5.1f}% | Diff={avg_p - avg_y:+5.1f}%"
                )
        print("=" * 108)

    return {"records": eval_records, "ece": compute_ece(eval_records)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtest player prop simulation models.")
    parser.add_argument("--data", default="props.csv", help="Path to props CSV file (default: props.csv)")
    parser.add_argument("--sim", default="sim_core:my_sim", help="module:function adapter")
    parser.add_argument("--segments", default="stat,position", help="Comma-separated segment columns")
    parser.add_argument("--n", type=int, default=2500, help="Monte Carlo draws per row")
    parser.add_argument("--min-history", type=int, default=1, help="Minimum prior games required")
    parser.add_argument("--season", type=int, default=None, help="Optional season start year filter (e.g. 2024 or 2025)")
    args = parser.parse_args()

    segs = [s.strip() for s in args.segments.split(",") if s.strip()]
    run_backtest(
        args.data,
        sim_spec=args.sim,
        segments=segs,
        n_draws=args.n,
        min_history=args.min_history,
        season_filter=args.season,
    )


if __name__ == "__main__":
    main()
