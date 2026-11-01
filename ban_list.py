"""
ban_list.py - manual ban list + per-prop record keeping. Standard library only.
Own database (bans.db). It never places orders; it only answers "is this banned?" and keeps score.

Telegram commands to wire (see the notes in the chat):
    /ban Evan Mobley rebs | keeps busting          -> add_ban(*parse_ban_line(text))
    /ban Wendell Carter Jr. 6+ rebounds | minutes  -> bans that one line only (6+ == line 5.5)
    /unban Evan Mobley rebs                        -> unban(...)
    /bans                                          -> list_bans()
    /banimport  (paste your notes, one per line)   -> import_lines(text)
    /record Wendell Carter Jr. 6+ rebounds         -> prop_record(...)

Enforcement points (call check()/filter_candidates() BEFORE a pick is shown or a combo is built):
    risk manager (first veto, code BANNED), sim_scanner, team card, the custom-combo builder.

line convention matches the rest of the project: "6+" means line 5.5 (hit = actual > line).
Run the self-test:  python ban_list.py
"""
import math
import os
import re
import sqlite3
import time
import unicodedata

DB_PATH = os.environ.get("BAN_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bans.db"))

STAT_ALIASES = {
    "pts": "PTS", "point": "PTS", "points": "PTS",
    "reb": "REB", "rebs": "REB", "rebound": "REB", "rebounds": "REB", "boards": "REB",
    "ast": "AST", "assist": "AST", "assists": "AST", "dimes": "AST",
    "3pt": "3PM", "3pts": "3PM", "3pm": "3PM", "3s": "3PM", "three": "3PM", "threes": "3PM",
    "pra": "PRA",
}
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bans (
  id INTEGER PRIMARY KEY AUTOINCREMENT, player_key TEXT, player_name TEXT, athlete_id TEXT,
  stat TEXT, line REAL, reason TEXT, created_ts REAL, expires_ts REAL,
  active INTEGER DEFAULT 1, source TEXT DEFAULT 'manual', last_reviewed_ts REAL);
CREATE TABLE IF NOT EXISTS legs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, game_date TEXT, combo_id TEXT, player_key TEXT,
  player_name TEXT, stat TEXT, line REAL, price REAL, result INTEGER, note TEXT);
"""


def _con():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def player_key(name):
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(w for w in s.split() if w not in _SUFFIXES)


def _fmt_line(line):
    return "all lines" if line is None else f"{math.floor(line) + 1}+ (line {line:g})"


# ----------------------------------------------------------------------------- parsing
def parse_ban_line(text, resolve=None):
    """'Evan Mobley rebs | keeps busting' -> (name, stat, line, reason).
    resolve(name) may return the canonical player name (e.g. via your player index)."""
    reason = ""
    for sep in ("|", " - "):
        if sep in text:
            text, reason = (p.strip() for p in text.split(sep, 1))
            break
    stat, line, name_tokens = None, None, []
    for tok in text.split():
        low = tok.lower().rstrip(",")
        if stat is None and low in STAT_ALIASES:
            stat = STAT_ALIASES[low]
        elif re.fullmatch(r"\d+(\.\d+)?\+?", low):
            v = float(low.rstrip("+"))
            line = v if (v % 1 == 0.5) else v - 0.5          # "6+" and "6" -> 5.5 ; "5.5" -> 5.5
        else:
            name_tokens.append(tok)
    if stat is None:
        raise ValueError(f"no stat found in: {text!r} (try pts, reb, ast, 3pts, pra)")
    name = " ".join(name_tokens).strip()
    if not name:
        raise ValueError(f"no player name in: {text!r}")
    name = (resolve(name) if resolve else None) or name.title()
    return name, stat, line, reason


# ----------------------------------------------------------------------------- bans
def add_ban(player, stat, line=None, reason="", days=None, athlete_id=None, source="manual", now=None):
    now = now or time.time()
    con = _con()
    key = player_key(player)
    row = con.execute("SELECT id FROM bans WHERE active=1 AND player_key=? AND stat=? AND "
                      "((line IS NULL AND ? IS NULL) OR line=?)", (key, stat, line, line)).fetchone()
    if row:
        con.execute("UPDATE bans SET reason=?, expires_ts=? WHERE id=?",
                    (reason, now + days * 86400 if days else None, row["id"]))
        con.commit(); con.close()
        return row["id"]
    cur = con.execute("INSERT INTO bans (player_key,player_name,athlete_id,stat,line,reason,created_ts,expires_ts,source,last_reviewed_ts)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (key, player, athlete_id, stat, line, reason, now, now + days * 86400 if days else None, source, now))
    con.commit(); bid = cur.lastrowid; con.close()
    return bid


def unban(player, stat, line=None):
    con = _con()
    if line is None:
        cur = con.execute("UPDATE bans SET active=0 WHERE active=1 AND player_key=? AND stat=?", (player_key(player), stat))
    else:
        cur = con.execute("UPDATE bans SET active=0 WHERE active=1 AND player_key=? AND stat=? AND line=?",
                          (player_key(player), stat, line))
    con.commit(); n = cur.rowcount; con.close()
    return n


def list_bans(active_only=True, now=None):
    now = now or time.time()
    con = _con()
    rows = [dict(r) for r in con.execute("SELECT * FROM bans" + (" WHERE active=1" if active_only else "") + " ORDER BY created_ts")]
    con.close()
    return [r for r in rows if not active_only or r["expires_ts"] is None or r["expires_ts"] > now]


def check(player, stat, line=None, athlete_id=None, now=None):
    """Return the matching ban (dict) or None. With line=None any ban on that player+stat matches."""
    key, stat = player_key(player), stat.upper()
    for b in list_bans(True, now):
        who = b["player_key"] == key or (athlete_id and b["athlete_id"] and str(athlete_id) == str(b["athlete_id"]))
        if who and b["stat"] == stat and (b["line"] is None or line is None or abs(b["line"] - float(line)) < 1e-9):
            return b
    return None


def filter_candidates(cands, now=None):
    """cands need .player, .stat, .line. Returns (allowed, [(cand, ban), ...])."""
    allowed, banned = [], []
    for c in cands:
        b = check(c.player, c.stat, c.line, now=now)
        (banned if b else allowed).append((c, b) if b else c)
    return allowed, banned


def reminder_text(b):
    since = time.strftime("%b %d", time.localtime(b["created_ts"]))
    why = f" - {b['reason']}" if b["reason"] else ""
    return f"🚫 BANNED: {b['player_name']} {b['stat']} {_fmt_line(b['line'])}{why} (since {since})"


def due_reviews(now=None, review_days=14, warn_days=2):
    """Bans that expire soon or have not been looked at for review_days -> send these as a reminder."""
    now = now or time.time()
    out = []
    for b in list_bans(True, now):
        expiring = b["expires_ts"] is not None and b["expires_ts"] - now <= warn_days * 86400
        stale = now - (b["last_reviewed_ts"] or b["created_ts"]) >= review_days * 86400
        if expiring or stale:
            out.append(b)
    return out


def mark_reviewed(ban_id, now=None):
    con = _con()
    con.execute("UPDATE bans SET last_reviewed_ts=? WHERE id=?", (now or time.time(), ban_id))
    con.commit(); con.close()


def import_lines(text, resolve=None):
    """Paste your notes (one per line). Returns (added_ids, errors)."""
    ids, errs = [], []
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            name, stat, line, reason = parse_ban_line(ln, resolve)
            ids.append(add_ban(name, stat, line, reason))
        except ValueError as e:
            errs.append(str(e))
    return ids, errs


# ----------------------------------------------------------------------------- records
def log_leg(player, stat, line, price=None, combo_id=None, game_date=None, note="", now=None):
    con = _con()
    cur = con.execute("INSERT INTO legs (ts,game_date,combo_id,player_key,player_name,stat,line,price,note) VALUES (?,?,?,?,?,?,?,?,?)",
                      (now or time.time(), game_date, combo_id, player_key(player), player, stat.upper(), line, price, note))
    con.commit(); lid = cur.lastrowid; con.close()
    return lid


def set_leg_result(leg_id, hit):
    con = _con()
    con.execute("UPDATE legs SET result=? WHERE id=?", (1 if hit else 0, leg_id))
    con.commit(); con.close()


def prop_record(player, stat, line=None, since_ts=None):
    """Your own legs on this prop (this season: pass since_ts). Pending legs are counted separately."""
    con = _con()
    q = "SELECT result FROM legs WHERE player_key=? AND stat=?"
    args = [player_key(player), stat.upper()]
    if line is not None:
        q += " AND line=?"; args.append(line)
    if since_ts:
        q += " AND ts>=?"; args.append(since_ts)
    res = [r["result"] for r in con.execute(q, args)]
    con.close()
    hits, misses = sum(1 for r in res if r == 1), sum(1 for r in res if r == 0)
    done = hits + misses
    return {"hits": hits, "misses": misses, "pending": len(res) - done, "n": done, "rate": hits / done if done else None}


def season_hit_rate(values, line):
    """From a player's game-log values: how often did he clear the line this season?"""
    n = len(values)
    hits = sum(1 for v in values if v > line)
    return {"hits": hits, "n": n, "rate": hits / n if n else None}


def bust_review(hits, n, p_sim, min_n=15, alpha=0.05):
    """Is this prop really hitting less often than the sim says, or is it just variance?
    tail_p = chance of this few hits (or fewer) if the sim were right. Flags are for REVIEW, never auto-bans:
    check enough props and some will look bad by luck (about alpha * number of props checked)."""
    tail = sum(math.comb(n, k) * p_sim ** k * (1 - p_sim) ** (n - k) for k in range(0, hits + 1)) if n else 1.0
    return {"hits": hits, "n": n, "sim_p": p_sim, "expected_hits": n * p_sim, "tail_p": tail,
            "flag": bool(n >= min_n and tail < alpha),
            "note": "review only: with many props checked, some look bad by chance"}


# ----------------------------------------------------------------------------- self-test
def _selftest():
    global DB_PATH
    import tempfile
    DB_PATH = os.path.join(tempfile.mkdtemp(), "bans_test.db")

    assert parse_ban_line("Evan mobley rebs") == ("Evan Mobley", "REB", None, "")
    assert parse_ban_line("Stephen Castle 3pts") == ("Stephen Castle", "3PM", None, "")
    assert parse_ban_line("Wendell Carter Jr. 6+ rebounds | keeps busting") == ("Wendell Carter Jr.", "REB", 5.5, "keeps busting")
    assert parse_ban_line("Jayson Tatum 25+ points - minutes")[2:] == (24.5, "minutes")
    assert parse_ban_line("Paul George 4.5 rebs")[2] == 4.5
    for bad in ("Evan Mobley", "rebs"):
        try:
            parse_ban_line(bad); assert False, bad
        except ValueError:
            pass

    ids, errs = import_lines("Evan mobley rebs\nStephen Castle 3pts\n\nnonsense line")
    assert len(ids) == 2 and len(errs) == 1
    assert check("Evan Mobley", "REB") and check("evan mobley", "reb", 5.5)            # any line
    assert check("Stephen Castle", "3PM") and not check("Stephen Castle", "PTS")
    assert check("Wendell Carter", "REB") is None

    add_ban("Wendell Carter Jr.", "REB", 5.5, "minutes")
    assert check("Wendell Carter", "REB", 5.5)                                          # suffix-insensitive
    assert check("Wendell Carter Jr.", "REB", 6.5) is None                              # different line is fine
    assert add_ban("Wendell Carter Jr.", "REB", 5.5, "updated reason") == add_ban("Wendell Carter Jr.", "REB", 5.5)
    assert len(list_bans()) == 3

    class C:
        def __init__(s, p, st, l): s.player, s.stat, s.line = p, st, l
    ok, bad = filter_candidates([C("Evan Mobley", "REB", 7.5), C("Evan Mobley", "AST", 3.5), C("Wendell Carter Jr.", "REB", 5.5)])
    assert [c.stat for c in ok] == ["AST"] and len(bad) == 2
    assert "BANNED" in reminder_text(bad[0][1])

    t0 = 1_000_000.0
    add_ban("Temp Guy", "PTS", None, "return from injury", days=3, now=t0)
    assert check("Temp Guy", "PTS", now=t0 + 86400) and check("Temp Guy", "PTS", now=t0 + 4 * 86400) is None
    assert any(b["player_name"] == "Temp Guy" for b in due_reviews(now=t0 + 2 * 86400 + 1))
    assert unban("Temp Guy", "PTS") == 1

    l1 = log_leg("Wendell Carter Jr.", "REB", 5.5, 0.81, combo_id="c1"); set_leg_result(l1, True)
    l2 = log_leg("Wendell Carter Jr.", "REB", 5.5, 0.81, combo_id="c2"); set_leg_result(l2, False)
    log_leg("Wendell Carter Jr.", "REB", 5.5, 0.80, combo_id="c3")
    r = prop_record("Wendell Carter", "REB", 5.5)
    assert (r["hits"], r["misses"], r["pending"], r["n"]) == (1, 1, 1, 2) and r["rate"] == 0.5

    assert season_hit_rate([7, 5, 8, 9, 4, 6], 5.5) == {"hits": 4, "n": 6, "rate": 4 / 6}
    mild = bust_review(hits=9, n=17, p_sim=0.61)
    harsh = bust_review(hits=5, n=20, p_sim=0.61)
    assert not mild["flag"] and harsh["flag"] and harsh["tail_p"] < 0.05
    assert not bust_review(hits=0, n=5, p_sim=0.61)["flag"]                            # too few games to flag
    print("self-test OK\n")
    for b in list_bans():
        print(" ", reminder_text(b))
    print("  record (example data):", prop_record("Wendell Carter Jr.", "REB", 5.5))


if __name__ == "__main__":
    _selftest()
