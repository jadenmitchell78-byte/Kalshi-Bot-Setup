#!/usr/bin/env python3
"""
sim_logger.py - log every sim probability next to the Kalshi price, settle the
results, and report whether the sim beats the market. Standard library only.

Uses its OWN database (sim_log.db). It never touches picks.db.

HOOK IT IN (inside run_bridge_player_simulation, after prob_ladder / ladders exist):

    try:
        import sim_logger
        sim_logger.log_ladder(
            player=p_name, team=team_abbr, opp=opp_team, stat=stat_norm,
            game_date=game_date, ladder=prob_ladder, kalshi_rungs=ladders,
            inputs=confidence_inputs)
    except Exception:
        pass            # logging must never break a reply

Conventions
  line   = cutoff, hit means actual > line   ("2+" threes -> line 1.5)
  prices = stored as 0-1 (cents are converted automatically)

Commands
  python sim_logger.py settle     # grade finished games
  python sim_logger.py report     # sim vs market, calibration, paper ROI, CLV
  python sim_logger.py demo       # self-test on synthetic data (temp db)
"""
import json
import logging
import os
import random
import sqlite3
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone

log = logging.getLogger("sim_logger")
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("SIM_LOG_DB", os.path.join(HERE, "sim_log.db"))
KALSHI_BASE = os.environ.get("KALSHI_BASE", "https://api.elections.kalshi.com/trade-api/v2")
THROTTLE_SEC = 300          # skip duplicate snapshots of the same prop within 5 min

SCHEMA = """CREATE TABLE IF NOT EXISTS sim_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, game_date TEXT, tipoff_ts REAL,
  player TEXT, team TEXT, opp TEXT, stat TEXT, line REAL, sim_prob REAL,
  ticker TEXT, yes_bid REAL, yes_ask REAL, inputs TEXT,
  actual REAL, hit INTEGER, settled_ts REAL)"""


def _default_get_actual(player: str, stat: str, game_date: str):
    """Fetches actual stat from ESPN v3 gamelog for rows without a settled Kalshi ticker."""
    try:
        import player_index
        from build_backtest_csv import fetch_player_season_gamelog

        verdict, cands = player_index.resolve_decision(player, "NBA")
        if not cands:
            return None
        aid = str(cands[0].get("id") or "")
        if not aid:
            return None
        d_str = str(game_date or "")[:10]
        y, m = int(d_str[:4]), int(d_str[5:7])
        espn_season = y + 1 if m >= 8 else y
        logs = fetch_player_season_gamelog(aid, season=espn_season)
        for g in logs:
            if g.get("date") == d_str:
                st = "3PM" if str(stat).upper() == "3PT" else str(stat).upper()
                return float(g[st]) if st in g else None
    except Exception as exc:
        log.warning("ESPN GET_ACTUAL lookup failed for %s %s %s: %s", player, stat, game_date, exc)
    return None


GET_ACTUAL = _default_get_actual


def _conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    return con


def _p(x):
    """cents or 0-1 -> 0-1 float (None stays None)."""
    if x is None:
        return None
    x = float(x)
    return x / 100.0 if x > 1.5 else x


def _key(r):
    return r["ticker"] or f"{r['player']}|{r['stat']}|{r['line']}|{r['game_date']}"


def log_snapshot(player, stat, line, sim_prob, game_date=None, team=None, opp=None,
                 ticker=None, yes_bid=None, yes_ask=None, inputs=None, tipoff_ts=None, ts=None):
    try:
        ts = ts or time.time()
        if tipoff_ts is not None and ts >= float(tipoff_ts):
            return 0
        bid, ask = _p(yes_bid), _p(yes_ask)
        con = _conn()
        if ticker:
            last = con.execute("SELECT ts,yes_bid,yes_ask FROM sim_log WHERE ticker=? ORDER BY ts DESC LIMIT 1",
                               (ticker,)).fetchone()
        else:
            last = con.execute("SELECT ts,yes_bid,yes_ask FROM sim_log WHERE ticker IS NULL AND player=? "
                               "AND stat=? AND line=? AND game_date=? ORDER BY ts DESC LIMIT 1",
                               (player, stat, float(line), game_date)).fetchone()
        if last and ts - last["ts"] < THROTTLE_SEC and last["yes_bid"] == bid and last["yes_ask"] == ask:
            con.close()
            return 0
        con.execute("INSERT INTO sim_log (ts,game_date,tipoff_ts,player,team,opp,stat,line,sim_prob,ticker,"
                    "yes_bid,yes_ask,inputs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ts, game_date, tipoff_ts, player, team, opp, stat, float(line), float(sim_prob),
                     ticker, bid, ask, json.dumps(inputs, default=str) if inputs else None))
        con.commit()
        con.close()
        return 1
    except Exception as e:                       # never break the bot
        log.warning("sim_logger.log_snapshot failed: %s", e)
        return 0


def log_ladder(player, stat, game_date, ladder, team=None, opp=None, kalshi_rungs=None,
               inputs=None, tipoff_ts=None):
    """ladder: [{'line':1.5,'prob_pct':61.0}, ...]   kalshi_rungs: [{'ticker','line','yes_ask','yes_bid','true_over_prob'|'true_prob'}, ...]"""
    n, priced = 0, set()
    for r in kalshi_rungs or []:
        try:
            # Always log P(actual > line) so sim_prob matches yes_bid / yes_ask (YES contract)
            over_p = r.get("true_over_prob", r.get("true_prob", 0.5))
            n += log_snapshot(player, stat, r["line"], over_p, game_date, team, opp,
                              r.get("ticker"), r.get("yes_bid"), r.get("yes_ask"), inputs, tipoff_ts)
            priced.add(round(float(r["line"]), 2))
        except Exception as e:
            log.warning("bad kalshi rung %s: %s", r, e)
    for r in ladder or []:
        if round(float(r["line"]), 2) in priced:
            continue
        p_val = float(r["prob_pct"]) / 100.0 if "prob_pct" in r else float(r.get("prob", 0.5))
        n += log_snapshot(player, stat, r["line"], p_val, game_date, team, opp,
                          None, None, None, inputs, tipoff_ts)
    return n


# ----------------------------------------------------------------------------- settle
def _settle_key(con, r, actual, hit):
    if r["ticker"]:
        con.execute("UPDATE sim_log SET actual=?,hit=?,settled_ts=? WHERE ticker=?",
                    (actual, hit, time.time(), r["ticker"]))
    else:
        con.execute("UPDATE sim_log SET actual=?,hit=?,settled_ts=? WHERE ticker IS NULL AND player=? "
                    "AND stat=? AND line=? AND game_date=?",
                    (actual, hit, time.time(), r["player"], r["stat"], r["line"], r["game_date"]))


def _kalshi_result(ticker):
    """Public market endpoint, expects 'result': 'yes'|'no'."""
    try:
        with urllib.request.urlopen(f"{KALSHI_BASE}/markets/{ticker}", timeout=15) as f:
            m = json.loads(f.read().decode()).get("market", {})
        res = (m.get("result") or "").lower()
        return {"yes": 1, "no": 0}.get(res)
    except Exception as e:
        log.warning("kalshi result lookup failed for %s: %s", ticker, e)
        return None


def settle():
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=8)).strftime("%Y-%m-%d")   # US Pacific date
    con = _conn()
    rows = con.execute("SELECT * FROM sim_log WHERE hit IS NULL AND game_date < ? GROUP BY "
                       "COALESCE(ticker, player||stat||line||game_date)", (cutoff,)).fetchall()
    done = 0
    for r in rows:
        hit, actual = None, None
        if r["ticker"]:
            hit = _kalshi_result(r["ticker"])
        if hit is None and GET_ACTUAL:
            try:
                actual = GET_ACTUAL(r["player"], r["stat"], r["game_date"])
                if actual is not None:
                    hit = int(float(actual) > r["line"])
            except Exception as e:
                log.warning("GET_ACTUAL failed: %s", e)
        if hit is not None:
            _settle_key(con, r, actual, hit)
            done += 1
    con.commit()
    con.close()
    print(f"settled {done} of {len(rows)} pending props")


# ----------------------------------------------------------------------------- report
def _mid(bid, ask):
    if bid is not None and ask is not None:
        return (bid + ask) / 2.0
    return ask if ask is not None else bid


def report(min_edge=0.05, fee_rate=0.07):
    con = _conn()
    rows = con.execute("SELECT * FROM sim_log ORDER BY ts").fetchall()
    con.close()
    props = {}
    for r in rows:
        props.setdefault(_key(r), []).append(r)
    done = [v for v in props.values() if v[-1]["hit"] is not None]
    print(f"props logged {len(props)} | settled {len(done)} | snapshots {len(rows)}")
    if not done:
        print("nothing settled yet - run `python sim_logger.py settle` after games finish")
        return

    # calibration of the sim at first snapshot
    bins = [[0, 0.0, 0.0] for _ in range(10)]
    for v in done:
        e = v[0]
        b = min(9, int(e["sim_prob"] * 10))
        bins[b][0] += 1
        bins[b][1] += e["sim_prob"]
        bins[b][2] += e["hit"]
    print("\nSIM CALIBRATION (all settled)")
    for i, (n, sp, h) in enumerate(bins):
        if n:
            print(f"  {i/10:.1f}-{(i+1)/10:.1f}  n={n:4d}  pred={sp/n:6.1%}  actual={h/n:6.1%}")

    def _eval_subset(subset, label):
        if not subset:
            print(f"\n{label}: 0 priced props")
            return
        bs = sum((e["sim_prob"] - e["hit"]) ** 2 for e in subset) / len(subset)
        bm = sum((_mid(e["yes_bid"], e["yes_ask"]) - e["hit"]) ** 2 for e in subset) / len(subset)
        outperf = (1.0 - bs / bm) if bm > 0 else 0.0
        status_str = f"sim {'BEATS' if bs < bm else 'LOSES TO'} market by {abs(outperf):.1%}"
        print(f"\n{label}: {len(subset)} props")
        print(f"  Brier sim={bs:.4f}  market={bm:.4f}  -> {status_str}")

        pnl = cost = nb = 0
        clv_sum = 0.0
        nclv = 0
        for e in subset:
            p, ask, bid = e["sim_prob"], e["yes_ask"], e["yes_bid"]
            side = None
            if ask is not None:
                fee = fee_rate * ask * (1 - ask)
                if p - ask - fee >= min_edge:
                    side, c, win = "YES", ask, e["hit"]
            if side is None and bid is not None:
                fee = fee_rate * bid * (1 - bid)
                if (1 - p) - (1 - bid) - fee >= min_edge:
                    side, c, win, fee = "NO", 1 - bid, 1 - e["hit"], fee
            if not side:
                continue
            nb += 1
            cost += c
            pnl += (1 - c if win else -c) - fee

            close = e.get("_close")
            if close:
                cm = _mid(close["yes_bid"], close["yes_ask"])
                if cm is not None:
                    clv_sum += (cm - c) if side == "YES" else (bid - cm)
                    nclv += 1

        if nb:
            print(f"  Paper bets (edge >= {min_edge:.0%} after fee): {nb} | ROI {pnl/cost:+.1%} on {cost:.1f} staked units")
        else:
            print(f"  Paper bets (edge >= {min_edge:.0%} after fee): 0")
        if nclv:
            print(f"  Avg CLV: {clv_sum/nclv*100:+.2f}¢ over {nclv} bets (positive = price moved in your favor)")

    # 1. Overall (First Seen Snapshot)
    first_priced = []
    for v in done:
        close = v[-1]
        for snap in v:
            if _mid(snap["yes_bid"], snap["yes_ask"]) is not None:
                row_d = dict(snap)
                row_d["_close"] = close
                first_priced.append(row_d)
                break

    # 2. Time-to-Tip Buckets
    bucket_over_6h = []
    bucket_1_to_6h = []
    bucket_under_1h = []

    for v in done:
        close = v[-1]
        # Over 6 hours pre-tip
        snaps_over_6 = [s for s in v if s["tipoff_ts"] and (s["tipoff_ts"] - s["ts"]) >= 6 * 3600 and _mid(s["yes_bid"], s["yes_ask"]) is not None]
        if snaps_over_6:
            row_d = dict(snaps_over_6[-1])  # latest in window
            row_d["_close"] = close
            bucket_over_6h.append(row_d)

        # 1 to 6 hours pre-tip (Prime window: use LAST snapshot in this window)
        snaps_1_to_6 = [s for s in v if s["tipoff_ts"] and 1 * 3600 <= (s["tipoff_ts"] - s["ts"]) < 6 * 3600 and _mid(s["yes_bid"], s["yes_ask"]) is not None]
        if snaps_1_to_6:
            row_d = dict(snaps_1_to_6[-1])  # latest snapshot in 1 to 6h window
            row_d["_close"] = close
            bucket_1_to_6h.append(row_d)

        # Under 1 hour pre-tip (Closing window)
        snaps_under_1 = [s for s in v if s["tipoff_ts"] and 0 <= (s["tipoff_ts"] - s["ts"]) < 1 * 3600 and _mid(s["yes_bid"], s["yes_ask"]) is not None]
        if snaps_under_1:
            row_d = dict(snaps_under_1[-1])
            row_d["_close"] = close
            bucket_under_1h.append(row_d)

    print("\n" + "=" * 70)
    print("  SIM VS MARKET EVALUATION (BY TIME-TO-TIP)")
    print("=" * 70)

    _eval_subset(first_priced, "1. ALL PRICED PROPS (First Seen Snapshot)")
    _eval_subset(bucket_over_6h, "2. > 6 HOURS BEFORE TIP-OFF (Early Market)")
    _eval_subset(bucket_1_to_6h, "3. 1 TO 6 HOURS BEFORE TIP-OFF (Prime / Post-Injury News Window)")
    _eval_subset(bucket_under_1h, "4. < 1 HOUR BEFORE TIP-OFF (Closing Line Window)")

    if len(first_priced) < 300:
        print("\n  NOTE: under 300 priced settled props - treat all of this as noise.")


# ----------------------------------------------------------------------------- self-test
def demo():
    global DB_PATH
    DB_PATH = os.path.join(tempfile.mkdtemp(), "demo.db")
    rnd = random.Random(1)
    t0 = time.time() - 86400 * 3
    for i in range(400):
        true_p = rnd.uniform(0.15, 0.85)
        sim_p = min(0.97, max(0.03, true_p + rnd.gauss(0, 0.05)))
        mkt = min(0.97, max(0.03, true_p + rnd.gauss(0, 0.04)))
        tip_time = t0 + 10000
        key = dict(player=f"P{i%25}", stat="PTS", line=14.5 + i, game_date="2026-10-20", tipoff_ts=tip_time)
        for k, (ts, price) in enumerate([(t0 + i, mkt), (t0 + i + 4000, mkt + rnd.gauss(0, 0.02))]):
            log_snapshot(sim_prob=sim_p, ticker=f"DEMO-{i}", yes_bid=(price - 0.02) * 100,
                         yes_ask=(price + 0.02) * 100, ts=ts, **key)
        hit = int(rnd.random() < true_p)
        con = _conn()
        con.execute("UPDATE sim_log SET hit=?,actual=?,settled_ts=? WHERE ticker=?", (hit, hit, time.time(), f"DEMO-{i}"))
        con.commit()
        con.close()
    report()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    {"settle": settle, "report": report, "demo": demo}.get(cmd, report)()
