#!/usr/bin/env python3
"""
build_backtest_csv.py - Downloads historical NBA player game logs from ESPN and builds
a leakage-free props.csv ready for sim_backtest.py.

Usage in Termux:
  python build_backtest_csv.py --out props.csv
  python sim_backtest.py --data props.csv --sim sim_core:my_sim --segments stat,position
"""
import argparse
import csv
import datetime
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import requests

# Representative cohort across Guards (G), Wings/Forwards (F), and Centers (C)
DEFAULT_NBA_ATHLETES: List[Tuple[str, str, str, str]] = [
    ("3908809", "Donovan Mitchell", "CLE", "G"),
    ("3975", "Stephen Curry", "GSW", "G"),
    ("3064440", "Zach LaVine", "CHI", "G"),
    ("3945274", "Luka Doncic", "DAL", "G"),
    ("4278073", "Shai Gilgeous-Alexander", "OKC", "G"),
    ("3934672", "Jalen Brunson", "NYK", "G"),
    ("4395725", "Anthony Edwards", "MIN", "G"),
    ("4396907", "Darius Garland", "CLE", "G"),
    ("4395628", "Tyrese Haliburton", "IND", "G"),
    ("4065648", "Jayson Tatum", "BOS", "F"),
    ("3032977", "Giannis Antetokounmpo", "MIL", "F"),
    ("1966", "LeBron James", "LAL", "F"),
    ("3202", "Kevin Durant", "PHX", "F"),
    ("3917376", "Jaylen Brown", "BOS", "G-F"),
    ("4277905", "Evan Mobley", "CLE", "F-C"),
    ("3112335", "Nikola Jokic", "DEN", "C"),
    ("6583", "Anthony Davis", "LAL", "C"),
    ("4066261", "Bam Adebayo", "MIA", "C"),
    ("4066328", "Jarrett Allen", "CLE", "C"),
    ("5104157", "Victor Wembanyama", "SAS", "C"),
]

THRESHOLDS_BY_STAT: Dict[str, List[float]] = {
    "3PM": [1.5, 2.5, 3.5, 4.5],
    "PTS": [14.5, 19.5, 24.5, 29.5],
    "REB": [4.5, 6.5, 8.5, 10.5],
    "AST": [3.5, 5.5, 7.5, 9.5],
    "PRA": [24.5, 29.5, 34.5, 39.5],
}


def _parse_made_stat(raw_v: Any) -> Optional[float]:
    if raw_v is None:
        return None
    s = str(raw_v).strip()
    if not s or s in ("-", "--", "N/A"):
        return None
    if "-" in s and not s.startswith("-"):
        s = s.split("-")[0]
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def fetch_player_season_gamelog(athlete_id: str, season: int) -> List[Dict[str, Any]]:
    """Fetches a player's regular-season game logs for a given ESPN season year."""
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{athlete_id}/gamelog?season={season}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    r = requests.get(url, headers=headers, timeout=8)
    if r.status_code != 200:
        print(f"  [WARN] HTTP {r.status_code} fetching gamelog for athlete_id={athlete_id} season={season}")
        return []
    data = r.json()
    top_names = [str(n).upper() for n in (data.get("names") or data.get("labels") or [])]
    events_meta = data.get("events") or {}

    games: List[Dict[str, Any]] = []
    for st in (data.get("seasonTypes") or []):
        st_name = str(st.get("displayName") or "").lower()
        if "preseason" in st_name:
            continue
        for cat in (st.get("categories") or []):
            cat_names = [str(n).upper() for n in (cat.get("names") or cat.get("labels") or top_names)]
            for ev_row in (cat.get("events") or []):
                ev_id = str(ev_row.get("eventId") or "")
                vals = ev_row.get("stats") or []
                if not cat_names or not vals:
                    continue
                stat_map: Dict[str, float] = {}
                for k_nm, raw_v in zip(cat_names, vals):
                    fv = _parse_made_stat(raw_v)
                    if fv is not None:
                        stat_map[k_nm] = fv

                ev_info = events_meta.get(ev_id, {}) if isinstance(events_meta, dict) else {}
                raw_date = str(ev_info.get("gameDate") or "")
                game_date = raw_date[:10] if len(raw_date) >= 10 else ""
                if not game_date:
                    continue

                at_vs = str(ev_info.get("atVs") or "").strip().lower()
                is_home = True if at_vs == "vs" else (False if at_vs in ("@", "at") else None)
                opp_abbr = str((ev_info.get("opponent") or {}).get("abbreviation") or "OPP").upper()

                mins = stat_map.get("MIN", 0.0)
                if mins <= 0:
                    continue
                pts = stat_map.get("PTS", 0.0)
                reb = stat_map.get("REB", 0.0)
                ast = stat_map.get("AST", 0.0)
                fg3m = stat_map.get("3PT", stat_map.get("3PM", stat_map.get("FG3M", 0.0)))

                games.append(
                    {
                        "date": game_date,
                        "season": season,
                        "opp": opp_abbr,
                        "is_home": is_home,
                        "minutes": round(mins, 1),
                        "PTS": pts,
                        "REB": reb,
                        "AST": ast,
                        "3PM": fg3m,
                        "PRA": pts + reb + ast,
                    }
                )
    games.sort(key=lambda g: g["date"])
    return games


def build_dataset(out_csv: str, seasons: Tuple[int, ...] = (2025, 2026)) -> int:
    all_game_rows: List[Dict[str, Any]] = []

    for ath_id, name, team, pos in DEFAULT_NBA_ATHLETES:
        prior_season_avgs: Dict[str, float] = {}
        for s_idx, yr in enumerate(seasons):
            print(f"Fetching {name} ({team}, {pos}) season={yr}...")
            logs = fetch_player_season_gamelog(ath_id, yr)
            print(f"  -> {len(logs)} regular-season games found")
            if not logs:
                continue

            for g in logs:
                g["player"] = name
                g["team"] = team
                g["position"] = pos
                g["priors"] = dict(prior_season_avgs)
                all_game_rows.append(g)

            # Compute this season's averages to serve as prior for the next season
            for stat_k in ("PTS", "REB", "AST", "3PM", "PRA"):
                vals = [float(x[stat_k]) for x in logs]
                if vals:
                    prior_season_avgs[stat_k] = round(sum(vals) / len(vals), 3)

    # Sort chronologically across all players so opponent multipliers use strictly prior dates
    all_game_rows.sort(key=lambda x: (x["date"], x["player"]))

    # Track walk-forward opponent allowed stats vs league average (no lookahead leakage)
    opp_hist: Dict[Tuple[str, str], List[Tuple[str, float]]] = defaultdict(list)
    league_hist: Dict[str, List[Tuple[str, float]]] = defaultdict(list)

    prop_rows: List[Dict[str, Any]] = []
    for g in all_game_rows:
        g_date = g["date"]
        opp = g["opp"]
        for stat_k, thresholds in THRESHOLDS_BY_STAT.items():
            # Strictly prior games before g_date
            opp_prior = [v for dt, v in opp_hist[(opp, stat_k)] if dt < g_date]
            lg_prior = [v for dt, v in league_hist[stat_k] if dt < g_date]
            if len(opp_prior) >= 5 and len(lg_prior) >= 20:
                lg_mean = max(0.2, sum(lg_prior) / len(lg_prior))
                opp_mean = sum(opp_prior) / len(opp_prior)
                raw_ratio = opp_mean / lg_mean
                k_opp = 15.0
                opp_factor = round(((len(opp_prior) * raw_ratio) + (k_opp * 1.0)) / (len(opp_prior) + k_opp), 4)
                opp_factor = max(0.88, min(1.12, opp_factor))
            else:
                opp_factor = 1.0

            prior_val = g["priors"].get(stat_k)
            actual_val = float(g[stat_k])

            # Pick the 2 thresholds closest to player's baseline so we don't flood far-tail lines
            ref_center = prior_val if prior_val is not None else actual_val
            near_thresholds = sorted(thresholds, key=lambda t: abs(t - ref_center))[:2]

            for th in sorted(near_thresholds):
                prop_rows.append(
                    {
                        "date": g_date,
                        "player": g["player"],
                        "stat": stat_k,
                        "actual": actual_val,
                        "threshold": th,
                        "minutes": g["minutes"],
                        "is_home": "" if g["is_home"] is None else g["is_home"],
                        "opp_factor": opp_factor,
                        "position": g["position"],
                        "prior": "" if prior_val is None else prior_val,
                    }
                )

            opp_hist[(opp, stat_k)].append((g_date, actual_val))
            league_hist[stat_k].append((g_date, actual_val))

    fieldnames = ["date", "player", "stat", "actual", "threshold", "minutes", "is_home", "opp_factor", "position", "prior"]
    with open(out_csv, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(prop_rows)

    print(f"\nWrote {len(prop_rows)} prop rows across {len(all_game_rows)} player-games to {out_csv}")
    return len(prop_rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Download ESPN NBA game logs into props.csv for sim_backtest.py")
    parser.add_argument("--out", default="props.csv", help="Output CSV path")
    args = parser.parse_args()
    build_dataset(args.out)


if __name__ == "__main__":
    main()
