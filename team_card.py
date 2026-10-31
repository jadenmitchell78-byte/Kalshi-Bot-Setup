#!/usr/bin/env python3
"""
team_card.py - what `/NBA 76ers` needs: resolve the team, simulate the game
(moneyline / spread / total), and build one card with the best player props.
Standard library only. It places no orders.

Pieces
  resolve_team(query)                 -> list of team abbreviations (1 = clear, 2+ = show buttons)
  build_ratings(results, before_date) -> walk-forward team ratings (recency-weighted, shrunk ridge fit)
  simulate_game(...)                  -> Monte Carlo of margin + total
  blend(p_model, p_market, w)         -> shrink toward the market (moneylines are very efficient)
  format_team_card(...)               -> Telegram HTML: moneyline section + ranked props from risk_manager

STATUS: the game model is UNVALIDATED. Defaults (home edge, game SDs, half-life, shrink, blend weight)
are hypotheses. Backtest it like the player model (walk-forward, Brier vs the market) before trusting it.
Props on the same game are correlated; risk_manager's per-game cap handles that, the sim does not.

Self-test:  python team_card.py
"""
import datetime
from datetime import date
from html import escape
import json
import math
import os
import random
import re
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set, Tuple

# abbr: (city, nickname, extra aliases)  - all uppercase compare
NBA_TEAMS = {
    "ATL": ("Atlanta", "Hawks", []), "BOS": ("Boston", "Celtics", []),
    "BKN": ("Brooklyn", "Nets", ["BRK"]), "CHA": ("Charlotte", "Hornets", []),
    "CHI": ("Chicago", "Bulls", []), "CLE": ("Cleveland", "Cavaliers", ["CAVS"]),
    "DAL": ("Dallas", "Mavericks", ["MAVS"]), "DEN": ("Denver", "Nuggets", []),
    "DET": ("Detroit", "Pistons", []), "GSW": ("Golden State", "Warriors", ["GS", "DUBS"]),
    "HOU": ("Houston", "Rockets", []), "IND": ("Indiana", "Pacers", []),
    "LAC": ("LA", "Clippers", ["CLIPS"]), "LAL": ("Los Angeles", "Lakers", ["LA LAKERS"]),
    "MEM": ("Memphis", "Grizzlies", ["GRIZ"]), "MIA": ("Miami", "Heat", []),
    "MIL": ("Milwaukee", "Bucks", []), "MIN": ("Minnesota", "Timberwolves", ["WOLVES", "TWOLVES"]),
    "NOP": ("New Orleans", "Pelicans", ["NO", "PELS"]), "NYK": ("New York", "Knicks", ["NY"]),
    "OKC": ("Oklahoma City", "Thunder", []), "ORL": ("Orlando", "Magic", []),
    "PHI": ("Philadelphia", "76ers", ["SIXERS", "PHILLY", "SEVENTY SIXERS"]),
    "PHX": ("Phoenix", "Suns", ["PHO"]), "POR": ("Portland", "Trail Blazers", ["BLAZERS", "TRAILBLAZERS"]),
    "SAC": ("Sacramento", "Kings", []), "SAS": ("San Antonio", "Spurs", ["SA"]),
    "TOR": ("Toronto", "Raptors", ["RAPS"]), "UTA": ("Utah", "Jazz", ["UTAH"]),
    "WAS": ("Washington", "Wizards", ["WSH"]),
}

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS_CACHE_FILE = os.path.join(HERE, "nba_results_history.json")


def _norm(s):
    return " ".join("".join(ch if ch.isalnum() else " " for ch in s.upper()).split())


def resolve_team(query):
    """Return a list of team abbreviations. One = confident. Several = ask the user (buttons)."""
    q = _norm(query)
    if not q:
        return []
    exact = []
    for ab, (city, nick, extra) in NBA_TEAMS.items():
        names = {ab, _norm(nick), _norm(city + " " + nick), *[_norm(x) for x in extra]}
        if q in names:
            exact.append(ab)
    if exact:
        return exact
    return [ab for ab, (city, nick, extra) in NBA_TEAMS.items() if _norm(city) == q]   # city only: "LA" -> both LA teams


# ----------------------------------------------------------------------------- ratings
def build_ratings(results, before_date, half_life_days=45.0, shrink=8.0, iters=60):
    """results: [(date 'YYYY-MM-DD', home, away, home_pts, away_pts)]. Uses ONLY games before `before_date`.
    Rating = points better than an average team on a neutral court. Ridge-style fit (shrink = pseudo-games)."""
    games = [r for r in results if r[0] < before_date]
    if not games:
        return None
    d0 = date.fromisoformat(before_date)
    rows = []
    for dt, h, a, hp, ap in games:
        w = 0.5 ** ((d0 - date.fromisoformat(dt)).days / half_life_days)
        rows.append((h, a, hp - ap, hp + ap, w))
    wsum = sum(r[4] for r in rows)
    hfa = sum(m * w for _, _, m, _, w in rows) / wsum
    L = sum(t * w for _, _, _, t, w in rows) / wsum
    teams = {t for r in rows for t in r[:2]}
    rt = {t: 0.0 for t in teams}
    tt = {t: 0.0 for t in teams}
    for _ in range(iters):
        nr = {t: 0.0 for t in teams}; nt = {t: 0.0 for t in teams}; den = {t: 0.0 for t in teams}
        for h, a, m, tot, w in rows:
            nr[h] += w * ((m - hfa) + rt[a]);  nr[a] += w * ((-m + hfa) + rt[h])
            nt[h] += w * ((tot - L) - tt[a]);  nt[a] += w * ((tot - L) - tt[h])
            den[h] += w; den[a] += w
        rt = {t: nr[t] / (den[t] + shrink) for t in teams}
        tt = {t: nt[t] / (den[t] + shrink) for t in teams}
    mean = sum(rt.values()) / len(rt)
    return {"ratings": {t: v - mean for t, v in rt.items()}, "total_tend": tt,
            "hfa": hfa, "league_total": L, "n_games": len(rows)}


# ----------------------------------------------------------------------------- game simulation
def simulate_game(R, home, away, inj_home=0.0, inj_away=0.0, sd_margin=12.0, sd_total=18.0,
                  n=20000, rng=None):
    """inj_*: points of team strength lost to confirmed absences (hypothesis input, default 0)."""
    rng = rng or random.Random(0)
    r = R["ratings"]
    mu_margin = r.get(home, 0.0) - r.get(away, 0.0) + R["hfa"] - inj_home + inj_away
    mu_total = R["league_total"] + R["total_tend"].get(home, 0.0) + R["total_tend"].get(away, 0.0)
    margins = [rng.gauss(mu_margin, sd_margin) for _ in range(n)]
    totals = [rng.gauss(mu_total, sd_total) for _ in range(n)]
    return {"home": home, "away": away, "mu_margin": mu_margin, "mu_total": mu_total,
            "margins": margins, "totals": totals,
            "p_home": sum(1 for m in margins if m > 0) / n}


def spread_cover(sim, home_line):
    """P(home covers) when the home team is given `home_line` points (e.g. -4.5)."""
    return sum(1 for m in sim["margins"] if m + home_line > 0) / len(sim["margins"])


def over_prob(sim, total_line):
    return sum(1 for t in sim["totals"] if t > total_line) / len(sim["totals"])


def devig_two_way(p_home, p_away):
    s = p_home + p_away
    return p_home / s if s > 0 else 0.5


def blend(p_model, p_market, w=0.25):
    """Weight on the model. Moneyline markets are among the most efficient, so start small."""
    return p_market + w * (p_model - p_market)


def _edge(p, ask, fee_rate=0.07):
    return p - ask - fee_rate * ask * (1 - ask)


# ----------------------------------------------------------------------------- the card
def format_team_card(team, game, sim, market=None, items=(), top_n=5, w_model=0.25, min_edge=0.05):
    """game: {'home','away','tip_text'}; market: {'home_bid','home_ask','away_bid','away_ask'} (0-1) or None.
    items: [(Candidate, Decision)] from risk_manager.evaluate()."""
    home, away = game["home"], game["away"]
    L = [f"🏀 <b>{escape(away)} @ {escape(home)}</b> · {escape(game.get('tip_text', ''))}",
         "<b>Moneyline sim</b> (model unvalidated)",
         f"• Model: {escape(home)} {sim['p_home']:.0%} / {escape(away)} {1 - sim['p_home']:.0%} "
         f"(margin {sim['mu_margin']:+.1f}, total {sim['mu_total']:.0f})"]
    if market and None not in (market.get("home_ask"), market.get("away_ask")):
        mh = devig_two_way(_mid(market, "home"), _mid(market, "away"))
        pb = blend(sim["p_home"], mh, w_model)
        L.append(f"• Market (no-vig): {escape(home)} {mh:.0%}; blended {pb:.0%} ({w_model:.0%} weight on model)")
        best = max(((home, pb, market["home_ask"]), (away, 1 - pb, market["away_ask"])),
                   key=lambda t: _edge(t[1], t[2]))
        e = _edge(best[1], best[2])
        L.append(f"• Best side: {escape(best[0])} at {best[2]:.0%} ask, edge {e:+.1%} after fee → "
                 + ("<b>paper candidate</b>" if e >= min_edge else "<b>pass</b>"))
    else:
        L.append("• No Kalshi moneyline price found; projection only")

    approved = sorted([(c, d) for c, d in items if d.action != "VETO"], key=lambda t: -(t[1].edge or 0))
    vetoed = [d for _, d in items if d.action == "VETO"]
    L.append(f"<b>Props</b>: {len(items)} checked, {len(approved)} approved, {len(vetoed)} passed")
    for c, d in approved[:top_n]:
        side = "O" if c.side == "YES" else "U"
        tag = " PRELIM" if "PRELIMINARY" in d.flags else ""
        L.append(f"• {escape(c.player)} {escape(c.stat)} {side}{c.line:g}: sim "
                 f"{(c.p_yes if c.side == 'YES' else 1 - c.p_yes):.0%} vs {d.entry:.0%} · edge {d.edge:+.1%} · "
                 f"${d.stake:.2f} {'PAPER' if d.action == 'APPROVE_PAPER' else 'LIVE'}{tag}")
    if vetoed:
        top = Counter(d.reasons[0][0] for d in vetoed if d.reasons).most_common(3)
        L.append("Passed because: " + ", ".join(f"{k.lower().replace('_', ' ')} ×{v}" for k, v in top))
    L.append("⚠ Props from one game are correlated: size them as a group.")
    return "\n".join(L)


def _mid(m, side):
    b, a = m.get(f"{side}_bid"), m.get(f"{side}_ask")
    return (a + b) / 2.0 if b is not None and a is not None else a


# ----------------------------------------------------------------------------- Data & Calibration Layer
ESPN_TEAM_IDS: Dict[str, str] = {
    "ATL": "1",  "BOS": "2",  "NOP": "3",  "CHI": "4",  "CLE": "5",
    "DAL": "6",  "DEN": "7",  "DET": "8",  "GSW": "9",  "HOU": "10",
    "IND": "11", "LAC": "12", "LAL": "13", "MIA": "14", "MIL": "15",
    "MIN": "16", "BKN": "17", "NYK": "18", "ORL": "19", "PHI": "20",
    "PHX": "21", "POR": "22", "SAC": "23", "SAS": "24", "OKC": "25",
    "UTA": "26", "WAS": "27", "TOR": "28", "MEM": "29", "CHA": "30",
}


def get_espn_nba_team_ids() -> Dict[str, str]:
    """Fetches official NBA team numeric IDs from ESPN teams endpoint, falling back to canonical map."""
    mapping = dict(ESPN_TEAM_IDS)
    try:
        url = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams?limit=50"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        sports = data.get("sports", [{}])[0]
        leagues = sports.get("leagues", [{}])[0]
        teams_list = leagues.get("teams", [])
        for item in teams_list:
            tm = item.get("team", {})
            raw_abbr = (tm.get("abbreviation") or "").upper()
            tid = str(tm.get("id") or "")
            if raw_abbr and tid:
                from build_backtest_csv import normalize_team_abbr
                norm = normalize_team_abbr(raw_abbr)
                if norm in NBA_TEAMS:
                    mapping[norm] = tid
    except Exception:
        pass
    return mapping


def _fetch_team_schedule_events(team_id: str, season: int, team_abbr: str = "") -> List[Dict[str, Any]]:
    """Fetches schedule events for a specific ESPN numeric team ID and regular season (seasontype=2) with retry."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/schedule?season={season}&seasontype=2"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return data.get("events", [])
        except Exception as exc:
            if attempt == 2:
                print(f"  [ERROR] Team {team_abbr} (ID {team_id}) season {season} fetch failed: {exc}", flush=True)
            time.sleep(0.3 * (attempt + 1))
    return []


def parse_completed_event(e: Dict[str, Any], abbr_map: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """
    Parses a single ESPN event into a completed game record.
    Extracts status from competitions[0].status.type.completed, score from value/displayValue,
    and home/away from competitor's homeAway field. Subtracts 5h for local game date.
    """
    comps = e.get("competitions") or [{}]
    c = comps[0] if comps else {}
    if not c.get("status", {}).get("type", {}).get("completed"):
        return None

    home = None
    away = None
    for t in c.get("competitors", []):
        s = t.get("score")
        if s is None:
            continue
        try:
            pts = float(s["value"]) if isinstance(s, dict) else float(s)
        except (ValueError, TypeError):
            continue

        raw_ab = (t.get("team") or {}).get("abbreviation", "")
        ab = abbr_map.get(raw_ab, raw_ab)
        if t.get("homeAway") == "home":
            home = (ab, pts)
        elif t.get("homeAway") == "away":
            away = (ab, pts)

    if not home or not away or home[0] not in NBA_TEAMS or away[0] not in NBA_TEAMS or home[0] == away[0]:
        return None
    if home[1] <= 0 or away[1] <= 0 or home[1] == away[1]:
        return None

    # The date is UTC. Subtract 5 hours before taking date for exact local game date
    raw_dt = str(e.get("date") or c.get("date") or "")
    try:
        dt = datetime.datetime.fromisoformat(raw_dt.replace("Z", "+00:00")) - datetime.timedelta(hours=5)
        dt_str = dt.strftime("%Y-%m-%d")
    except Exception:
        dt_str = raw_dt[:10]

    return {
        "id": str(e["id"]),
        "date": dt_str,
        "home": home[0],
        "away": away[0],
        "home_pts": int(round(home[1])),
        "away_pts": int(round(away[1])),
    }


def print_and_verify_game_counts(results: List[Tuple[str, str, str, int, int]]) -> None:
    """Prints completed regular-season game counts per team and aborts loudly if any team returns zero or total < 1,000."""
    team_counts: Counter = Counter()
    for _, home, away, _, _ in results:
        team_counts[home] += 1
        team_counts[away] += 1

    print("\n" + "=" * 65)
    print("  COMPLETED NBA REGULAR-SEASON GAMES PER TEAM")
    print("=" * 65)
    zero_teams = []
    for tm in sorted(NBA_TEAMS.keys()):
        cnt = team_counts[tm]
        print(f"  {tm:4s}: {cnt:4d} games")
        if cnt == 0:
            zero_teams.append(tm)
    print(f"  Total Unique Completed Games: {len(results)}")
    print("=" * 65 + "\n", flush=True)

    if zero_teams:
        raise RuntimeError(
            f"ABORT: The following {len(zero_teams)} team(s) returned 0 completed games: {', '.join(zero_teams)}. "
            "Data fetch incomplete; cannot run backtest with missing team records."
        )

    if len(results) < 1000:
        raise RuntimeError(
            f"ABORT: Total completed games ({len(results)}) is below the required 1,000 game threshold. "
            "Cannot run backtest on incomplete dataset."
        )


def build_nba_results_table(seasons=(2025, 2026, 2027), verbose=False) -> List[Tuple[str, str, str, int, int]]:
    """
    Builds a historical regular-season game results table across specified seasons (2025=2024-25, 2026=2025-26, 2027=2026-27).
    De-duplicates by event ID across all team schedules and writes cache only if >= 1,000 games.
    Returns: [(date 'YYYY-MM-DD', home_team, away_team, home_pts, away_pts), ...]
    """
    if os.path.exists(RESULTS_CACHE_FILE):
        try:
            with open(RESULTS_CACHE_FILE, "r", encoding="utf-8") as f:
                cached = json.load(f)
                if cached and len(cached) >= 1000:
                    team_counts = Counter(t for r in cached for t in (r[1], r[2]))
                    if all(team_counts.get(tm, 0) > 0 for tm in NBA_TEAMS):
                        if verbose:
                            print(f"Loaded {len(cached)} games from cache ({RESULTS_CACHE_FILE}).")
                        print_and_verify_game_counts(cached)
                        return [tuple(r) for r in cached]
                    elif verbose:
                        print("Cached file contains missing teams. Re-fetching fresh games from ESPN...")
                elif verbose:
                    print(f"Cached file has fewer than 1000 games ({len(cached) if cached else 0}). Re-fetching fresh games from ESPN...")
        except Exception as exc:
            if verbose:
                print(f"Cache read error: {exc}. Re-fetching fresh games from ESPN...")

    from build_backtest_csv import TEAM_ABBR_MAP
    abbr_map = dict(TEAM_ABBR_MAP)
    abbr_map.update({
        "NO": "NOP", "UTAH": "UTA", "NY": "NYK", "GS": "GSW",
        "SA": "SAS", "WSH": "WAS", "BRK": "BKN", "PHO": "PHX"
    })

    all_games_map: Dict[str, Tuple[str, str, str, int, int]] = {}
    team_id_map = get_espn_nba_team_ids()
    tasks = []
    for season in seasons:
        for tm, tid in team_id_map.items():
            tasks.append((tid, season, tm))

    if verbose:
        print(f"Fetching NBA results for seasons {seasons} across {len(tasks)} team-season schedules via ESPN numeric IDs...", flush=True)

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {executor.submit(_fetch_team_schedule_events, tid, season, tm): (tm, season) for tid, season, tm in tasks}
        for fut in as_completed(futures):
            tm, season = futures[fut]
            try:
                events = fut.result()
                for ev in events:
                    parsed = parse_completed_event(ev, abbr_map)
                    if parsed:
                        all_games_map[parsed["id"]] = (
                            parsed["date"],
                            parsed["home"],
                            parsed["away"],
                            parsed["home_pts"],
                            parsed["away_pts"],
                        )
            except Exception as exc:
                print(f"  [ERROR] Failed processing team {tm} season {season}: {exc}", flush=True)

    results = sorted(list(all_games_map.values()), key=lambda r: r[0])

    # Print completed game counts per team and perform loud abort if zero games or < 1,000 games
    print_and_verify_game_counts(results)

    # Only write cache if results contain at least 1,000 games
    if len(results) >= 1000:
        try:
            with open(RESULTS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(results, f)
            if verbose:
                print(f"Saved {len(results)} completed games to cache ({RESULTS_CACHE_FILE}).")
        except Exception as exc:
            print(f"  [CACHE WRITE ERROR] {exc}", flush=True)
    else:
        print(f"Refusing to write cache: results count ({len(results)}) is under 1,000 games.")

    return results


def fetch_kalshi_game_moneyline(home_team: str, away_team: str) -> Optional[Dict[str, float]]:
    """Sweeps open Kalshi KXNBAGAME markets to find current moneyline bid/ask for home vs away."""
    try:
        url = "https://api.elections.kalshi.com/trade-api/v2/markets?status=open&series_ticker=KXNBAGAME&limit=200"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode())
        markets = data.get("markets", [])

        home_clean = home_team.upper()
        away_clean = away_team.upper()

        home_mkt = None
        away_mkt = None

        for m in markets:
            tk = str(m.get("ticker") or "").upper()
            title = str(m.get("title") or "").upper()
            sub = str(m.get("subtitle") or "").upper()
            blob = f"{tk} {title} {sub}"

            if home_clean in blob and away_clean in blob:
                yes_sub = str(m.get("yes_sub_title") or "").upper()
                if home_clean in yes_sub or tk.endswith(f"-{home_clean}"):
                    home_mkt = m
                elif away_clean in yes_sub or tk.endswith(f"-{away_clean}"):
                    away_mkt = m

        if home_mkt:
            hb = home_mkt.get("yes_bid")
            ha = home_mkt.get("yes_ask")
            hb_val = (float(hb) / 100.0) if hb and float(hb) > 1.5 else (float(hb) if hb else None)
            ha_val = (float(ha) / 100.0) if ha and float(ha) > 1.5 else (float(ha) if ha else None)

            if away_mkt:
                ab = away_mkt.get("yes_bid")
                aa = away_mkt.get("yes_ask")
                ab_val = (float(ab) / 100.0) if ab and float(ab) > 1.5 else (float(ab) if ab else None)
                aa_val = (float(aa) / 100.0) if aa and float(aa) > 1.5 else (float(aa) if aa else None)
            else:
                ab_val = round(1.0 - ha_val, 3) if ha_val else None
                aa_val = round(1.0 - hb_val, 3) if hb_val else None

            return {"home_bid": hb_val, "home_ask": ha_val, "away_bid": ab_val, "away_ask": aa_val}
    except Exception:
        pass
    return None


def generate_team_card_response(team_query: str, bankroll: float = 100.0) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Handles `/NBA <team>`:
    1. Resolves team (e.g. '76ers', 'Sixers', 'PHI', 'Boston', 'Celtics').
       If ambiguous (e.g. 'LA'), returns interactive buttons.
    2. Finds today's pre-game matchup from ESPN scoreboard.
    3. Runs walk-forward build_ratings and simulate_game.
    4. Fetches Kalshi moneyline prices from KXNBAGAME.
    5. Gathers rotation player candidates, evaluates them with risk_manager,
       and builds formatted HTML card via format_team_card.
    """
    cands = resolve_team(team_query)
    if not cands:
        return f"❓ Unknown NBA team <b>{escape(team_query)}</b>. Try <code>/NBA 76ers</code>, <code>/NBA Lakers</code>, or <code>/NBA Celtics</code>.", None

    if len(cands) > 1:
        rows = []
        for tm in cands:
            city, nick, _ = NBA_TEAMS[tm]
            rows.append([{"text": f"{city} {nick} ({tm})", "callback_data": f"team|NBA|{tm}"}])
        return f"🤔 Which team did you mean for <b>{escape(team_query)}</b>?", {"inline_keyboard": rows}

    team = cands[0]
    from sim_scanner import fetch_espn_nba_games
    espn_games = fetch_espn_nba_games()
    game_info = espn_games.get(team)

    today_str = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")

    if not game_info:
        city, nick, _ = NBA_TEAMS.get(team, ("", team, []))
        return f"🏀 No scheduled game found for the <b>{city} {nick} ({team})</b> today ({today_str}).", None

    home = game_info.get("home", team)
    away = game_info.get("away", "OPP")
    tip_ts = game_info.get("tipoff_ts")
    tip_text = datetime.datetime.fromtimestamp(tip_ts).strftime("%I:%M %p") if tip_ts else "Today"

    game_dict = {
        "home": home,
        "away": away,
        "tip_text": tip_text,
    }

    # Build historical ratings
    results = build_nba_results_table(seasons=(2025, 2026, 2027))
    R = build_ratings(results, today_str, half_life_days=45.0, shrink=8.0)
    if not R:
        R = {"ratings": {home: 0.0, away: 0.0}, "total_tend": {home: 0.0, away: 0.0}, "hfa": 2.5, "league_total": 224.0, "n_games": 0}

    sim = simulate_game(R, home, away, n=20000)
    mkt = fetch_kalshi_game_moneyline(home, away)

    import player_index
    import risk_manager as rm
    from auto_market_index import search_kalshi_markets, fetch_espn_player_profile
    from sim_core import core_simulate, make_rng, prior_for

    risk_candidates: List[rm.Candidate] = []
    team_players = [p for p in player_index.get_index("NBA") if p.get("team") == team]

    for p in team_players[:7]:
        p_name = p.get("name")
        p_id = str(p.get("id") or "")
        ladders = search_kalshi_markets(p_name, sport="NBA")
        if not ladders:
            continue

        for st in ("PTS", "AST", "REB", "3PM"):
            st_rungs = [r for r in ladders if r.get("stat") == st and float(r.get("line", 0)) > 0]
            if not st_rungs:
                continue

            profile = fetch_espn_player_profile(p_name, sport="NBA", athlete_id=p_id, known_name=p_name, known_team=team)
            avg = float((profile.get("season_averages") or {}).get(st, 15.0))
            rng = make_rng(f"{p_name}:{st}:{today_str}")
            draws_arr, _ = core_simulate(
                stat=st, season_avg=avg, games_played=int(profile.get("games_played") or 20),
                season_min=float(profile.get("minutes_avg") or 30.0), is_home=(home == team),
                position=str(profile.get("position") or "G"), n=5000, rng=rng, prior=prior_for(st, "G")
            )

            for rung in st_rungs[:2]:
                line_val = float(rung["line"])
                p_over = float((draws_arr > line_val).mean())
                cand = rm.Candidate(
                    f"c_{p_id}_{st}_{line_val}",
                    p_name,
                    game_info.get("game_id", f"{home}:{away}"),
                    st,
                    line_val,
                    "YES",
                    p_over,
                    rung.get("yes_bid"),
                    rung.get("yes_ask"),
                    rung.get("ticker", ""),
                    bool(profile.get("confirmed", False)),
                    tip_ts or 0.0,
                    time.time(),
                )
                risk_candidates.append(cand)

    st_dict = {"bankroll": bankroll, "daily_pnl": 0.0, "open_positions": []}
    decisions = rm.evaluate(risk_candidates, st_dict, rm.RiskConfig(live_enabled=False), now=time.time())
    dec_map = {d.candidate.cand_id: d for d in decisions if d.candidate}
    items = [(c, dec_map[c.cand_id]) for c in risk_candidates if c.cand_id in dec_map]

    card_html = format_team_card(team, game_dict, sim, market=mkt, items=items, top_n=5)
    keyboard = {"inline_keyboard": [
        [{"text": "📺 WATCH STREAM", "url": "https://streamed.su/watch/basketball"},
         {"text": "🔙 MENU HUD", "callback_data": "menu_refresh_hud"}]
    ]}
    return card_html, keyboard


def backtest_ratings(results: List[Tuple[str, str, str, int, int]], n_bootstrap: int = 1000, seed: int = 42):
    """
    Backtests walk-forward ratings against two authentic baselines:
    1. Baseline 1: Last season's final ratings (static prior, no within-season updating).
    2. Baseline 2: Season-to-date win percentage with home court edge (Bradley-Terry / Log5).
    Reports Brier score, Skill Score %, and 95% paired bootstrap confidence intervals by season.
    """
    if len(results) < 50:
        print("Not enough game results to backtest (< 50 games).")
        return

    rng = random.Random(seed)

    def _season_of(dt_str: str) -> str:
        if dt_str >= "2026-08-01":
            return "2026-27"
        elif dt_str >= "2025-08-01":
            return "2025-26"
        elif dt_str >= "2024-08-01":
            return "2024-25"
        return "EARLIER"

    # Pre-build Baseline 1 final ratings for earlier seasons
    print("Fitting Baseline 1 (Last Season Final Ratings) priors...", flush=True)
    R_2025_final = build_ratings(results, "2025-08-01", half_life_days=45.0, shrink=8.0, iters=50)
    R_2026_final = build_ratings(results, "2026-08-01", half_life_days=45.0, shrink=8.0, iters=50)

    # Tracking for Baseline 2: Season-to-date win percentage with home edge
    cur_season = None
    wins_std: Counter = Counter()
    games_std: Counter = Counter()
    logit_hfa = math.log(0.58 / (1.0 - 0.58))  # ~0.3228

    eval_records = []
    total_games = len(results)
    print(f"Running walk-forward ratings backtest across {total_games} games...", flush=True)

    for idx, (cur_date, home, away, hp, ap) in enumerate(results):
        actual_home_win = 1.0 if hp > ap else 0.0
        season = _season_of(cur_date)

        if season != cur_season:
            cur_season = season
            wins_std.clear()
            games_std.clear()

        # Baseline 2: Season-to-date win % with home court edge
        # Bayesian shrinkage toward 0.50 using a 10-game prior (5 wins)
        h_w = wins_std[home]; h_n = games_std[home]
        a_w = wins_std[away]; a_n = games_std[away]
        wp_h = (h_w + 5.0) / (h_n + 10.0)
        wp_a = (a_w + 5.0) / (a_n + 10.0)
        logit_h = math.log(wp_h / (1.0 - wp_h))
        logit_a = math.log(wp_a / (1.0 - wp_a))
        logit_p = max(-5.0, min(5.0, (logit_h - logit_a) + logit_hfa))
        p_b2 = 1.0 / (1.0 + math.exp(-logit_p))

        # Update season-to-date records strictly after predicting current matchup
        games_std[home] += 1
        games_std[away] += 1
        if hp > ap:
            wins_std[home] += 1
        else:
            wins_std[away] += 1

        # Only evaluate out-of-sample regular seasons where Baseline 1 is established (2025-26 and 2026-27)
        if season not in ("2025-26", "2026-27"):
            continue

        # Baseline 1: Last season's final static ratings
        R_prior = R_2026_final if season == "2026-27" else R_2025_final
        if R_prior:
            sim_b1 = simulate_game(R_prior, home, away, n=3000, rng=rng)
            p_b1 = sim_b1["p_home"]
        else:
            p_b1 = 0.58

        # Walk-forward model simulation using games strictly before cur_date
        R = build_ratings(results, cur_date, half_life_days=45.0, shrink=8.0, iters=40)
        if not R:
            continue
        sim_model = simulate_game(R, home, away, n=3000, rng=rng)
        p_model = sim_model["p_home"]

        eval_records.append({
            "date": cur_date,
            "season": season,
            "home": home,
            "away": away,
            "y": actual_home_win,
            "p_model": p_model,
            "p_b1": p_b1,
            "p_b2": p_b2,
            "bs_model": (p_model - actual_home_win) ** 2,
            "bs_b1": (p_b1 - actual_home_win) ** 2,
            "bs_b2": (p_b2 - actual_home_win) ** 2,
        })

    def _report_subset(subset: List[Dict[str, Any]], label: str):
        if not subset:
            return
        n = len(subset)
        bs_m = sum(r["bs_model"] for r in subset) / n
        bs_b1 = sum(r["bs_b1"] for r in subset) / n
        bs_b2 = sum(r["bs_b2"] for r in subset) / n

        skill_b1 = 1.0 - (bs_m / bs_b1)
        skill_b2 = 1.0 - (bs_m / bs_b2)

        diffs_b1 = [r["bs_b1"] - r["bs_model"] for r in subset]
        diffs_b2 = [r["bs_b2"] - r["bs_model"] for r in subset]

        def _boot_ci(diffs):
            b_means = []
            for _ in range(n_bootstrap):
                b_means.append(sum(diffs[rng.randrange(n)] for _ in range(n)) / n)
            b_means.sort()
            return b_means[int(0.025 * n_bootstrap)], b_means[int(0.975 * n_bootstrap)]

        ci1_l, ci1_h = _boot_ci(diffs_b1)
        ci2_l, ci2_h = _boot_ci(diffs_b2)

        print(f"\n--- {label} (N = {n} games) ---")
        print(f"• Walk-Forward Ratings Model Brier Score:  {bs_m:.4f}")
        print()
        print("  Baseline 1: Last Season's Final Ratings (Static Prior)")
        print(f"  • Baseline Brier Score:                   {bs_b1:.4f}")
        print(f"  • Skill Score vs Baseline 1:              {skill_b1:+.2%} (positive = beats baseline)")
        print(f"  • 95% Paired Bootstrap CI:                [{ci1_l:+.4f}, {ci1_h:+.4f}] Brier point difference")
        if ci1_l > 0:
            print("    --> Statistically significant outperformance vs Baseline 1 (p < 0.05).")
        else:
            print("    --> Inconclusive vs Baseline 1 (within noise boundary).")
        print()
        print("  Baseline 2: Season-to-Date Win % + Home Edge (Bradley-Terry)")
        print(f"  • Baseline Brier Score:                   {bs_b2:.4f}")
        print(f"  • Skill Score vs Baseline 2:              {skill_b2:+.2%} (positive = beats baseline)")
        print(f"  • 95% Paired Bootstrap CI:                [{ci2_l:+.4f}, {ci2_h:+.4f}] Brier point difference")
        if ci2_l > 0:
            print("    --> Statistically significant outperformance vs Baseline 2 (p < 0.05).")
        else:
            print("    --> Inconclusive vs Baseline 2 (within noise boundary).")

    print("\n" + "=" * 70)
    print("  WALK-FORWARD NBA RATINGS BACKTEST VS AUTHENTIC BASELINES")
    print("=" * 70)

    _report_subset(eval_records, "COMBINED DATASET (2025-26 + 2026-27)")
    _report_subset([r for r in eval_records if r["season"] == "2025-26"], "2025-26 SEASON (OUT-OF-SAMPLE / PRIOR SEASON)")
    _report_subset([r for r in eval_records if r["season"] == "2026-27"], "2026-27 SEASON (CURRENT LIVE SEASON)")
    print("=" * 70 + "\n")


# ----------------------------------------------------------------------------- self-test
def _selftest():
    # team lookup
    for q, want in [("76ers", ["PHI"]), ("Sixers", ["PHI"]), ("phi", ["PHI"]), ("Philadelphia", ["PHI"]),
                    ("lakers", ["LAL"]), ("warriors", ["GSW"]), ("GS", ["GSW"]), ("knicks", ["NYK"]),
                    ("ny", ["NYK"]), ("LA", ["LAC"]), ("Los Angeles", ["LAL"]), ("blazers", ["POR"])]:
        assert resolve_team(q) == want, (q, resolve_team(q))
    assert resolve_team("zzz") == []

    # spot check: parse_completed_event matches real ESPN payload format (Oct 22, 2025: PHI @ BOS 117-116, ID 401809936)
    test_ev = {
        "id": "401809936", "date": "2025-10-23T02:30Z",
        "competitions": [{
            "status": {"type": {"completed": True}},
            "competitors": [
                {"team": {"abbreviation": "BOS"}, "score": {"value": 116.0, "displayValue": "116"}, "homeAway": "home"},
                {"team": {"abbreviation": "PHI"}, "score": {"value": 117.0, "displayValue": "117"}, "homeAway": "away"},
            ]
        }]
    }
    parsed = parse_completed_event(test_ev, {"BOS": "BOS", "PHI": "PHI"})
    assert parsed == {"id": "401809936", "date": "2025-10-22", "home": "BOS", "away": "PHI", "home_pts": 116, "away_pts": 117}, parsed

    # ratings recover a known truth from noisy games
    rng = random.Random(7)
    teams = list(NBA_TEAMS)
    truth = {t: rng.gauss(0, 4) for t in teams}
    results = []
    for i in range(1500):
        h, a = rng.sample(teams, 2)
        m = truth[h] - truth[a] + 2.5 + rng.gauss(0, 12)
        tot = 224 + rng.gauss(0, 18)
        results.append((f"2026-{1 + i // 150:02d}-{1 + i % 28:02d}", h, a, round((tot + m) / 2), round((tot - m) / 2)))
    R = build_ratings(results, "2026-12-30", half_life_days=10_000, shrink=8.0)
    xs = [truth[t] for t in teams]; ys = [R["ratings"][t] for t in teams]
    mx, my = sum(xs) / 30, sum(ys) / 30
    corr = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(
        sum((x - mx) ** 2 for x in xs) * sum((y - my) ** 2 for y in ys))
    assert corr > 0.9 and 1.5 < R["hfa"] < 3.5 and 215 < R["league_total"] < 233, (corr, R["hfa"])
    assert build_ratings(results, "2026-01-01") is None
    early = build_ratings(results, "2026-02-01")
    assert early["n_games"] < R["n_games"]

    # game sim behaves
    best, worst = max(teams, key=lambda t: R["ratings"][t]), min(teams, key=lambda t: R["ratings"][t])
    s = simulate_game(R, best, worst, n=20000)
    flipped = simulate_game(R, worst, best, n=20000)
    assert s["p_home"] > 0.6 and flipped["p_home"] < s["p_home"]
    hurt = simulate_game(R, best, worst, inj_home=5.0, n=20000)
    assert hurt["p_home"] < s["p_home"]
    assert 0.40 < spread_cover(s, -s["mu_margin"]) < 0.60 and 0.40 < over_prob(s, s["mu_total"]) < 0.60
    assert devig_two_way(0.55, 0.50) < 0.55 and blend(0.8, 0.5, 0.25) == 0.575

    # card with risk_manager decisions
    import risk_manager as rm
    now, tip = 1_000_000.0, 1_000_000.0 + 3 * 3600
    cands = [rm.Candidate("c1", "Tyrese Maxey", "g1", "AST", 5.5, "YES", 0.62, 0.49, 0.51, "T-1", True, tip, now - 30),
             rm.Candidate("c2", "Joel Embiid", "g1", "PTS", 24.5, "YES", 0.53, 0.49, 0.51, "T-2", True, tip, now - 30),
             rm.Candidate("c3", "Paul George", "g1", "3PM", 2.5, "NO", 0.37, 0.49, 0.51, "T-3", False, now + 1800, now - 30)]
    st = {"bankroll": 100.0, "daily_pnl": 0.0, "open_positions": []}
    ds = rm.evaluate(cands, st, rm.RiskConfig(), now=now)
    dec_map = {d.candidate.cand_id: d for d in ds if d.candidate}
    items = [(c, dec_map[c.cand_id]) for c in cands if c.cand_id in dec_map]
    card = format_team_card("PHI", {"home": "PHI", "away": "BOS", "tip_text": "7:00 PM"}, simulate_game(R, teams[0], teams[1], n=2000),
                            {"home_bid": 0.55, "home_ask": 0.57, "away_bid": 0.44, "away_ask": 0.46}, items)

    required_substrings = ["Moneyline sim", "Tyrese Maxey", "PAPER", "Passed because"]
    missing = [s for s in required_substrings if s not in card]
    if missing:
        print("=" * 60)
        print("SELF-TEST ASSERTION FAILED: MISSING SUBSTRINGS:", missing)
        print("=" * 60)
        print("FULL CARD PRODUCED:")
        print(card)
        print("=" * 60)
        assert not missing, f"Missing required substrings: {missing}"

    reasons_ok = "edge too small" in card or "unconfirmed near tip" in card
    if not reasons_ok:
        print("=" * 60)
        print("SELF-TEST ASSERTION FAILED: Missing veto reason codes in card")
        print("=" * 60)
        print("FULL CARD PRODUCED:")
        print(card)
        print("=" * 60)
        assert reasons_ok, "Neither 'edge too small' nor 'unconfirmed near tip' found in card"
    print("self-test OK  (team lookup, rating recovery r=%.2f, home edge %.1f pts)\n" % (corr, R["hfa"]))
    print("EXAMPLE CARD (made-up numbers; tags shown raw):")
    print(card)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backtest":
        res = build_nba_results_table(seasons=(2025, 2026, 2027), verbose=True)
        backtest_ratings(res)
    else:
        _selftest()
