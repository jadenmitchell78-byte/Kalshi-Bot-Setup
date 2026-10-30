# ~/peezy_bot/player_index.py
"""
Daily full-league player index (every team roster, not just tonight's games) + fuzzy resolver.

resolve_decision(query, sport) ->
    ("confident", [cand])      one clear match -> run the sim
    ("ambiguous", [cand, ...]) several close matches -> show "Did you mean" buttons
    ("none", [])               nothing close -> show an error
    ("unindexed", [])          sport has no roster index (caller falls back to old lookup)
cand = {"name", "id", "team", "score"}
"""

import datetime
import json
import logging
import os
import re
import threading
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Any, Dict, List, Tuple

import requests

try:  # optional: pip install rapidfuzz (faster / better scoring); not required
    from rapidfuzz import fuzz as _rf_fuzz
except Exception:  # pragma: no cover
    _rf_fuzz = None

log = logging.getLogger("peezy.player_index")

ESPN = "https://site.api.espn.com/apis/site/v2/sports"
SPORT_PATHS = {
    "NBA": "basketball/nba",
    "WNBA": "basketball/wnba",
    "NFL": "football/nfl",
    "NHL": "hockey/nhl",
    "MLB": "baseball/mlb",
}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIDENT_SCORE = 90.0
CANDIDATE_SCORE = 70.0
CONFIDENT_GAP = 5.0

_INDEX: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()
_UA = {"User-Agent": "Mozilla/5.0"}


def normalize(name: str) -> str:
    """lowercase, strip accents (Doncic/Jokic), drop Jr./III and punctuation."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    toks = [t for t in s.split() if t not in SUFFIXES]
    return " ".join(toks)


def _today() -> str:
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")


def _cache_file(sport: str) -> str:
    return os.path.join(BASE_DIR, f"player_index_{sport.lower()}.json")


def _fetch_json(url: str) -> Dict[str, Any]:
    r = requests.get(url, headers=_UA, timeout=8)
    r.raise_for_status()
    return r.json()


def _roster(path: str, team_id: str, abbr: str) -> Tuple[List[Dict[str, Any]], bool]:
    """Returns (players, ok). ok=False when the fetch failed so the caller can refuse a partial index."""
    out: List[Dict[str, Any]] = []
    try:
        data = _fetch_json(f"{ESPN}/{path}/teams/{team_id}/roster")
    except Exception as exc:
        log.warning("roster fetch failed for %s/%s: %s", path, abbr, exc)
        return out, False
    for entry in data.get("athletes", []):
        items = entry.get("items") if isinstance(entry, dict) and "items" in entry else [entry]
        for a in items:
            nm = a.get("displayName") or a.get("fullName")
            if nm:
                out.append({"id": str(a.get("id") or ""), "name": nm, "norm": normalize(nm), "team": abbr})
    return out, True


def build_index(sport: str) -> Tuple[List[Dict[str, Any]], int]:
    """Returns (players, failed_team_count)."""
    path = SPORT_PATHS.get(sport.upper())
    if not path:
        return [], 0
    try:
        data = _fetch_json(f"{ESPN}/{path}/teams?limit=100")
        teams = data["sports"][0]["leagues"][0]["teams"]
    except Exception as exc:
        log.warning("team list fetch failed for %s: %s", sport, exc)
        return [], 1
    jobs = [(str(t["team"]["id"]), str(t["team"].get("abbreviation", "")).upper()) for t in teams]
    players: List[Dict[str, Any]] = []
    failed = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        for res, ok in ex.map(lambda j: _roster(path, j[0], j[1]), jobs):
            players.extend(res)
            if not ok:
                failed += 1
    return players, failed


_LAST_FAIL: Dict[str, float] = {}
RETRY_COOLDOWN_SEC = 300.0


def get_index(sport: str, force: bool = False) -> List[Dict[str, Any]]:
    """Today's index. A failed or partial rebuild NEVER replaces a good older cache (memory or file)."""
    import time as _time
    sp = sport.upper()
    with _LOCK:
        cached = _INDEX.get(sp)
        if cached and cached["date"] == _today() and not force:
            return cached["players"]
        fpath = _cache_file(sp)
        stale = cached
        if os.path.exists(fpath):
            try:
                with open(fpath) as f:
                    blob = json.load(f)
                if blob.get("players"):
                    if blob.get("date") == _today() and not force:
                        _INDEX[sp] = blob
                        return blob["players"]
                    if stale is None:
                        stale = blob
            except Exception:
                log.exception("player index cache read failed")

        if not force and _time.time() - _LAST_FAIL.get(sp, 0.0) < RETRY_COOLDOWN_SEC and stale:
            return stale["players"]  # recently failed: serve the old cache, retry later

        players, failed = build_index(sp)
        prev_n = len(stale["players"]) if stale else 0
        # Always log both counts so a legitimate shrink (roster cuts) can be told apart from a bad fetch.
        log.info("%s index rebuild: new=%d players, previous=%d players, failed_teams=%d", sp, len(players), prev_n, failed)
        shrink_warn = bool(prev_n) and len(players) < 0.8 * prev_n
        # Reject: any team failed, or a collapse below 50% of the old size. Accept (with a warning) a 50-80% drop
        # when every team fetched fine, e.g. regular-season roster cuts.
        partial = failed > 0 or (prev_n and len(players) < 0.5 * prev_n)
        if players and not partial:
            if shrink_warn:
                log.warning("%s index shrank %d -> %d with all teams fetched OK; accepting (roster cuts?)", sp, prev_n, len(players))
            blob = {"date": _today(), "players": players}
            _INDEX[sp] = blob
            try:
                tmp = fpath + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(blob, f)
                os.replace(tmp, fpath)
            except Exception:
                log.exception("player index cache write failed")
            log.info("built %s player index: %d players", sp, len(players))
            return players

        _LAST_FAIL[sp] = _time.time()
        if stale:
            log.warning("%s index refresh failed/partial (%d teams failed, %d players); keeping previous cache dated %s",
                        sp, failed, len(players), stale.get("date"))
            return stale["players"]
        if players:
            log.warning("%s index is PARTIAL (%d teams failed) and no older cache exists; using it anyway", sp, failed)
            _INDEX[sp] = {"date": "partial", "players": players}
            return players
        log.error("%s index unavailable (no cache, fetch failed)", sp)
        return []


def lookup_by_id(sport: str, athlete_id: str, rebuild_on_miss: bool = True) -> Dict[str, Any]:
    """Find a player by ESPN id. On a miss, force one index rebuild and retry before giving up."""
    for p in get_index(sport):
        if p["id"] == str(athlete_id):
            return p
    if rebuild_on_miss:
        log.info("id %s not in %s index; forcing rebuild before giving up", athlete_id, sport)
        for p in get_index(sport, force=True):
            if p["id"] == str(athlete_id):
                return p
    return {}


def _score(q: str, n: str) -> float:
    """0-100. All query tokens (typo-tolerant) found in the name -> ~97, else blended fuzzy score."""
    if not q or not n:
        return 0.0
    if q == n:
        return 100.0
    q_toks, n_toks = q.split(), n.split()
    tok_best = [max(SequenceMatcher(None, qt, nt).ratio() for nt in n_toks) for qt in q_toks]
    full = SequenceMatcher(None, q, n).ratio()
    score = 0.6 * (sum(tok_best) / len(tok_best)) * 100 + 0.4 * full * 100
    if min(tok_best) >= 0.92:
        score = max(score, 97.0 - 1.5 * max(0, len(n_toks) - len(q_toks)))
    if _rf_fuzz is not None:
        score = max(score, float(_rf_fuzz.WRatio(q, n)) if min(tok_best) >= 0.85 else score)
    return min(100.0, score)


def resolve(query: str, sport: str, limit: int = 4) -> List[Dict[str, Any]]:
    q = normalize(query)
    idx = get_index(sport)
    scored: List[Dict[str, Any]] = []
    for p in idx:
        s = _score(q, p["norm"])
        if s >= CANDIDATE_SCORE:
            scored.append({"name": p["name"], "id": p["id"], "team": p["team"], "score": round(s, 1)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:limit]


def resolve_decision(query: str, sport: str) -> Tuple[str, List[Dict[str, Any]]]:
    if sport.upper() not in SPORT_PATHS:
        return "unindexed", []
    if not get_index(sport):
        return "unindexed", []  # ESPN unreachable: don't block the user, fall back to old lookup
    cands = resolve(query, sport)
    if not cands:
        return "none", []
    top = cands[0]
    second = cands[1]["score"] if len(cands) > 1 else 0.0
    if top["score"] >= CONFIDENT_SCORE and (top["score"] - second) >= CONFIDENT_GAP:
        return "confident", [top]
    return "ambiguous", cands
