# ~/peezy_bot/air_bridge_core.py
"""
Air-Bridge: single clean entry point for /NBA /NFL /NHL /MLB /CFB /TENNIS commands.
- Background thread caches verified daily Kalshi sports markets + ESPN slates into live_slate_cache.json
- format_casual_slip_card(): short casual card, 10k math tucked in an expandable blockquote
- handle_sport_command(chat_id, text): returns (html, keyboard) ONCE; caller sends it and stops.
"""

import datetime
import html
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_slate_cache.json")
SYNC_INTERVAL_SEC = 60
SPORT_COMMANDS = ("NBA", "NFL", "NHL", "MLB", "CFB", "TENNIS")
SPORT_PREFIXES = ("KXNBA", "KXNFL", "KXNHL", "KXMLB", "KXTENNIS")
BANNED_WORDS = ("CAREER", "SEASON", "CHAMP", "MVP", "STADIUM", "KXUSCPI", "RECORD", "AWARD", "LEADER", "HOF")
ESPN_PATHS = {
    "NBA": "basketball/nba",
    "NFL": "football/nfl",
    "NHL": "hockey/nhl",
    "MLB": "baseball/mlb",
}

_CACHE_LOCK = threading.Lock()
_SYNC_STARTED = False
_CMD_LOCK = threading.Lock()
LEGACY_LOOKUP_COUNTS: Dict[str, int] = {}  # per-sport count of legacy-lookup fallbacks; indexed sports should stay 0


def _daily_regex() -> "re.Pattern":
    """Matches today's / tomorrow's date stamp like 26OCT28 (US Pacific-ish, UTC-7)."""
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=7)
    stamps = []
    for d in (0, 1):
        dt = now + datetime.timedelta(days=d)
        stamps.append(dt.strftime("%y%b%d").upper())
    return re.compile("|".join(stamps))


def _fetch_espn_slate(sport: str, date_str: str) -> List[Dict[str, Any]]:
    path = ESPN_PATHS.get(sport)
    if not path:
        return []
    out: List[Dict[str, Any]] = []
    try:
        r = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={date_str}",
            timeout=6,
        )
        if r.status_code != 200:
            return []
        for ev in r.json().get("events", []):
            comp = (ev.get("competitions") or [{}])[0]
            teams = comp.get("competitors", [])
            away = next((t for t in teams if t.get("homeAway") == "away"), {})
            home = next((t for t in teams if t.get("homeAway") == "home"), {})
            out.append({
                "id": ev.get("id"),
                "date": ev.get("date"),
                "away": (away.get("team") or {}).get("abbreviation"),
                "home": (home.get("team") or {}).get("abbreviation"),
                "status": (ev.get("status") or {}).get("type", {}).get("shortDetail"),
            })
    except Exception:
        pass
    return out


def sync_air_bridge_cache() -> Dict[str, Any]:
    """One sync pass: verified daily sports markets + ESPN schedules -> live_slate_cache.json."""
    stamp_re = _daily_regex()
    markets_out: List[Dict[str, Any]] = []
    try:
        from auto_market_index import get_all_daily_kalshi_markets
        for m in get_all_daily_kalshi_markets(force_refresh=True) or []:
            tk = str(m.get("ticker") or "").upper()
            if not tk.startswith(SPORT_PREFIXES):
                continue
            if any(b in tk for b in BANNED_WORDS) or not stamp_re.search(tk):
                continue
            try:
                ask = float(m.get("yes_ask") or 0)
                strike = float(m.get("line") or m.get("floor_strike") or m.get("strike_price") or 1)
            except (TypeError, ValueError):
                continue
            if ask <= 1 or strike <= 0:
                continue
            markets_out.append({
                "ticker": tk,
                "title": m.get("title"),
                "yes_ask": ask,
                "strike": strike,
            })
    except Exception:
        pass

    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=7)
    slates: Dict[str, Any] = {}
    for sport in ESPN_PATHS:
        slates[sport] = {}
        for d in (0, 1):
            ds = (now + datetime.timedelta(days=d)).strftime("%Y%m%d")
            slates[sport][ds] = _fetch_espn_slate(sport, ds)

    data = {"ts": time.time(), "markets": markets_out, "slates": slates}
    try:
        with _CACHE_LOCK:
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, CACHE_PATH)
    except Exception:
        pass
    return data


def _sync_loop() -> None:
    while True:
        try:
            sync_air_bridge_cache()
        except Exception:
            pass
        time.sleep(SYNC_INTERVAL_SEC)


def start_air_bridge_daemon() -> None:
    global _SYNC_STARTED
    if _SYNC_STARTED:
        return
    _SYNC_STARTED = True
    threading.Thread(target=_sync_loop, daemon=True, name="air_bridge_sync").start()


def _prob_to_american(p: float) -> str:
    p = min(0.99, max(0.01, p))
    if p >= 0.5:
        return f"-{round(p / (1 - p) * 100)}"
    return f"+{round((1 - p) / p * 100)}"


def _leg_block(name: str, team: str, side: str, line: Any, stat: str, matchup: str,
               ask: Any, true_prob: Any, edge: Optional[float],
               buy_below_cents: Optional[int] = None,
               prob_ladder_str: str = "",
               proj_mean: Any = None,
               buy_ladder_str: str = "") -> str:
    edge_s = f" | {edge:+.1f}% Fee-Adj Edge" if edge is not None else ""
    fair = f"{float(true_prob):.0f}" if true_prob is not None else "?"
    m_txt = str(matchup or "")
    if not m_txt or "Matchup" in m_txt or m_txt.endswith(" Game"):
        m_txt = "Next game: schedule not posted yet"
    try:
        real_price = ask is not None and float(ask) > 5 and edge is not None
    except (TypeError, ValueError):
        real_price = False
    if real_price:
        icon = "✅"
        headline = f"{html.escape(str(name))} ({html.escape(str(team))}) — {side} {line} {stat}"
        price_txt = f"{ask}¢ Ask  <i>(Fair Value: {fair}¢{edge_s})</i>"
    elif str(side).upper() == "PROJECTION":
        icon = "📊"
        mean_lbl = f" (Proj Mean: {proj_mean})" if proj_mean is not None else ""
        headline = f"{html.escape(str(name))} ({html.escape(str(team))}) — {stat} Distribution{mean_lbl}"
        if buy_ladder_str:
            price_txt = f"<i>Projection only (no open market). Buy-below targets: {buy_ladder_str}</i>"
        else:
            buy_lim = buy_below_cents if buy_below_cents is not None else max(1, int(round(float(true_prob or 50.0) - 6)))
            price_txt = f"<i>Projection only. Worth buying below ~{buy_lim}¢ if a market opens.</i>"
    else:
        icon = "📊"
        headline = f"{html.escape(str(name))} ({html.escape(str(team))}) — {side} {line} {stat}"
        buy_lim = buy_below_cents if buy_below_cents is not None else max(1, int(round(float(true_prob or 50.0) - 6)))
        price_txt = f"<i>Projection only (fair {fair}¢). Worth buying below ~{buy_lim}¢ if a market opens.</i>"
    ladder_line = f"\n📈 <b>Prob Ladder ({html.escape(str(stat))}):</b> {prob_ladder_str}" if prob_ladder_str else ""
    return (
        f"{icon} <b>{headline}</b>\n"
        f"🏟 <i>{html.escape(m_txt)}</i>{ladder_line}\n"
        f"🏷 <b>Kalshi Price:</b> {price_txt}"
    )


def format_casual_slip_card(sport: str, result: Dict[str, Any], bankroll: float = 15.58) -> str:
    sim = result.get("sim", {})
    exec_res = result.get("exec", {})
    combo = sim.get("combo") or {}
    orderable = bool(sim.get("is_live_orderable"))

    ask1 = sim.get("limit_cents")
    tp1 = sim.get("true_prob", 50.0)
    ev1 = sim.get("ev_edge") if orderable else None
    buy_below = sim.get("buy_below_cents")
    prob_ladder = sim.get("prob_ladder") or result.get("prob_ladder") or []
    prob_ladder_str = str(sim.get("prob_ladder_str") or result.get("prob_ladder_str") or "")
    buy_ladder_str = " · ".join(f"{r['label']} ≤{r['buy_below_cents']}¢" for r in prob_ladder[:3]) if prob_ladder else ""
    conf = sim.get("confidence_inputs") or result.get("confidence_inputs") or {}

    leg1 = _leg_block(
        sim.get("player", "Player"), sim.get("team", sport), sim.get("side", "PROJECTION"),
        sim.get("line", ""), sim.get("stat", ""), sim.get("matchup", ""),
        ask1, tp1, ev1, buy_below_cents=buy_below, prob_ladder_str=prob_ladder_str,
        proj_mean=sim.get("mean"), buy_ladder_str=buy_ladder_str,
    )

    legs_txt = leg1
    if orderable and combo.get("partner"):
        ask2 = combo.get("limit_cents", 50)
        tp2 = combo.get("partner_prob")
        legs_txt += "\n\n" + _leg_block(
            combo["partner"], combo.get("team", sport), combo.get("side", "OVER"),
            combo.get("line", ""), combo.get("stat", ""),
            combo.get("matchup", sim.get("matchup", "")), ask2, tp2, 0.0,
        )

    stat = sim.get("stat", "")
    avg = result.get("season_avg")
    gp = conf.get("games_played", 0)
    min_season = conf.get("minutes_season")
    min_proj = conf.get("minutes_proj")
    l5_avg = conf.get("last5_avg")
    prior_avg = conf.get("prior_avg")
    prior_src = conf.get("prior_source", "position")
    opp_adj = conf.get("opp_adj_pct", 0.0)
    home_away = conf.get("home_away", "Neutral")
    out_tm = conf.get("out_teammates") or []
    inj_adj = conf.get("injury_adj_pct", 0.0)

    conf_parts = [f"GP: {gp}"]
    if min_season is not None:
        conf_parts.append(f"MIN: {min_season:.1f} (Proj {min_proj:.1f})" if min_proj else f"MIN: {min_season:.1f}")
    if isinstance(avg, (int, float)):
        conf_parts.append(f"Season {stat}: {avg:.1f}")
    if prior_avg is not None and gp < 20:
        conf_parts.append(f"Prior ({prior_src}): {prior_avg:.1f}")
    if l5_avg is not None:
        conf_parts.append(f"L5 {stat}: {l5_avg:.1f}")
    if opp_adj:
        conf_parts.append(f"Opp Adj: {opp_adj:+.1f}%")
    if home_away and home_away != "Neutral":
        conf_parts.append(str(home_away))
    if out_tm:
        conf_parts.append(f"Out: {', '.join(out_tm)} ({inj_adj:+.1f}%)")
    conf_line = " · ".join(conf_parts)

    side_u = str(sim.get("side", "PROJECTION")).upper()
    if side_u == "PROJECTION":
        why = (
            f"{sim.get('player', 'Player')} averages {avg:.1f} {stat}"
            + (f" (L5: {l5_avg:.1f})" if l5_avg is not None else "")
            + f"; adjusted sim projects a mean of {sim.get('mean')} {stat}."
            if isinstance(avg, (int, float))
            else f"Adjusted sim projects a mean of {sim.get('mean')} {stat}."
        )
    else:
        why = (
            f"{sim.get('player', 'Player')} averages {avg:.1f} {stat}"
            + (f" (L5: {l5_avg:.1f})" if l5_avg is not None else "")
            + f"; adjusted sim projects {sim.get('mean')} vs a {sim.get('line')} line."
            if isinstance(avg, (int, float))
            else f"Adjusted sim projects {sim.get('mean')} {stat} vs a {sim.get('line')} line."
        )
    hits = result.get("hits", 0)

    if orderable and sim.get("real_kalshi_ticker"):
        header_banner = f"🔥 <b>PEEZY LIVE EDGE // {sport.upper()}</b>"
        p1 = max(0.02, float(ask1 or 50) / 100.0)
        joint = p1 * max(0.02, float(combo.get("limit_cents", 50)) / 100.0) if combo.get("partner") else p1
        stake = max(1.00, round(bankroll * 0.10, 2))
        payout = round(stake / joint, 2)
        stake_section = f"💵 <b>${stake:.2f} ➡️ Wins ${payout:.2f}</b>  <i>({_prob_to_american(joint)})</i>\n"
        status = f"🟢 <code>{html.escape(str(sim.get('real_kalshi_ticker')))}</code>"
        live_note = str(exec_res.get("status", ""))
        if live_note:
            status += f"\n   <i>{html.escape(live_note)}</i>"
        contract_note = str(sim.get("real_kalshi_ticker"))
        hit_rate_label = f"Hit Rate ({side_u} {sim.get('line', '')})"
    else:
        header_banner = f"📊 <b>PEEZY PROJECTION // {sport.upper()}</b>"
        stake_section = ""  # Hide stake/payout when no Kalshi contract is open
        buy_lim = buy_below if buy_below is not None else max(1, int(round(float(tp1 or 50.0) - 6)))
        if side_u == "PROJECTION" and buy_ladder_str:
            status = f"📋 Projection only — target buy-below prices: {buy_ladder_str}"
            contract_note = f"None — projection only ({buy_ladder_str})"
            hit_rate_label = f"Center Rung (> {sim.get('line', '')})"
        else:
            status = f"📋 Projection only — worth buying {side_u} {sim.get('line', '')} below ~{buy_lim}¢ if a market opens"
            contract_note = f"None — projection only (fair {float(tp1):.0f}¢, buy ≤ {buy_lim}¢)"
            hit_rate_label = f"Hit Rate ({side_u} {sim.get('line', '')})"

    return (
        f"{header_banner}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{legs_txt}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{stake_section}"
        f"⚡ <b>Status:</b> {status}\n"
        f"🔍 <b>Sample &amp; Inputs:</b> <code>{html.escape(conf_line)}</code>\n\n"
        f"<blockquote expandable>🧠 <b>Tap to View 10k Sim &amp; AI Breakdown</b>\n"
        f"• <b>Why We Like It:</b> {html.escape(why)}\n"
        f"• <b>Season Avg:</b> {f'{avg:.1f}' if isinstance(avg, (int, float)) else 'n/a'} {stat}"
        + (f" (L5: {l5_avg:.1f})" if l5_avg is not None else "")
        + f" ➡️ <b>Sim Proj:</b> {sim.get('mean')} ± {sim.get('std', 1.5)} {stat}\n"
        f"• <b>Percentiles (P10 / P50 / P90):</b> {sim.get('p10')} / {sim.get('p50')} / {sim.get('p90')}\n"
        f"• <b>{hit_rate_label}:</b> {hits:,}/10,000 Sims ({tp1}%)\n"
        f"• <b>Contract:</b> <code>{html.escape(contract_note)}</code></blockquote>"
    )


_STAT_WORDS = {
    "pts": "PTS", "points": "PTS", "point": "PTS", "pt": "PTS",
    "reb": "REB", "rebs": "REB", "rebounds": "REB", "rebound": "REB", "boards": "REB",
    "ast": "AST", "assist": "AST", "assists": "AST", "ast.": "AST",
    "3pt": "3PM", "3pts": "3PM", "3pm": "3PM", "threes": "3PM", "three": "3PM", "3s": "3PM", "3ptm": "3PM", "3pt's": "3PM",
    "pra": "PRA",
    "pass": "PASS_YDS", "rush": "RUSH_YDS", "rec": "REC_YDS",
    "pass_yds": "PASS_YDS", "rush_yds": "RUSH_YDS", "rec_yds": "REC_YDS",
    "sog": "SOG", "shots": "SOG", "k": "K", "ks": "K", "strikeouts": "K",
}


def parse_sport_command(text: str) -> Tuple[str, str, str, Optional[float]]:
    """'/NBA Ja Morant assist 6.5' -> ('NBA', 'Ja Morant', 'AST', 6.5)
    Also handles spaced '3 pts' / '3 pointers' cleanly without interpreting '3' as line=3.0."""
    parts = text.strip().split()
    sport = parts[0].lstrip("/").split("@")[0].upper()
    stat = "PTS"
    line: Optional[float] = None
    name_tokens: List[str] = []
    idx = 1
    while idx < len(parts):
        tok = parts[idx]
        low = tok.lower().rstrip("+")
        # Handle spaced "3 pts", "3 pt", "3 pointers", "3 pointer"
        if low == "3" and idx + 1 < len(parts) and parts[idx + 1].lower().rstrip("+") in (
            "pt", "pts", "pm", "pointer", "pointers", "point", "points"
        ):
            stat = "3PM"
            idx += 2
            continue
        glued = re.fullmatch(r"(\d+(?:\.\d+)?)(pts|pt|pm|reb|rebs|ast)", low)
        if glued and glued.group(2) in _STAT_WORDS and low not in _STAT_WORDS:
            line = float(glued.group(1))          # "6.5pts" -> line 6.5, stat PTS
            stat = _STAT_WORDS[glued.group(2)]
            idx += 1
            continue
        if low in _STAT_WORDS:
            stat = _STAT_WORDS[low]
        elif re.fullmatch(r"\d+(\.\d+)?", low):
            line = float(low)
        else:
            name_tokens.append(tok)
        idx += 1
    return sport, " ".join(name_tokens), stat, line


def _safe_keyboard(sport: str) -> Dict[str, Any]:
    """Watch + HUD only. No 'PLACE AUTO-BET' (it triggered a forced live sniper scan)."""
    cat = {"NBA": "basketball", "CFB": "american-football", "NFL": "american-football",
           "NHL": "hockey", "MLB": "baseball", "TENNIS": "tennis"}.get(sport.upper(), "basketball")
    return {"inline_keyboard": [[
        {"text": "📺 WATCH LIVE STREAM", "url": f"https://streamed.su/watch/{cat}"},
        {"text": "🔙 MENU HUD", "callback_data": "menu_refresh_hud"},
    ]]}


def handle_sport_command(chat_id: Any, text: str, bankroll: float = 15.58,
                         sniper_on: bool = False, live_mode: bool = False,
                         forced_name: Optional[str] = None,
                         forced_athlete: Optional[Dict[str, Any]] = None) -> Tuple[str, Any]:
    """Runs ONE simulation and returns (html_card, keyboard). Never returns 'No +EV' spam.
    forced_name / forced_athlete: exact player already chosen from the 'Did you mean' buttons (skips fuzzy resolution)."""
    import logging
    start_air_bridge_daemon()
    sport, player, stat, line = parse_sport_command(text)
    matched_athlete: Optional[Dict[str, Any]] = forced_athlete
    if forced_name:
        player = forced_name
    if matched_athlete and matched_athlete.get("name"):
        player = matched_athlete["name"]
    if not player:
        return (f"ℹ️ Usage: <code>/{sport} Player stat [line]</code> e.g. <code>/{sport} Luka 3pts</code>", None)
    fallback_note = ""
    if not forced_name and not matched_athlete:
        try:
            import player_index
            verdict, cands = player_index.resolve_decision(player, sport)
        except Exception:
            logging.exception("player index resolve failed")
            verdict, cands = "unindexed", []
        if verdict == "unindexed":
            try:
                import player_index as _pi
                supported = sport in _pi.SPORT_PATHS
            except Exception:
                supported = False
            LEGACY_LOOKUP_COUNTS[sport] = LEGACY_LOOKUP_COUNTS.get(sport, 0) + 1
            indexed_total = sum(v for k, v in LEGACY_LOOKUP_COUNTS.items() if k in ("NBA", "WNBA", "NFL", "NHL", "MLB"))
            logging.warning("LEGACY LOOKUP USED for %s '%s' (%s) | count[%s]=%d | indexed-sport legacy total=%d (should be 0)",
                            sport, player, "player index FAILED" if supported else "sport has no roster index",
                            sport, LEGACY_LOOKUP_COUNTS[sport], indexed_total)
            if supported:
                fallback_note = "\n⚠️ <i>Player index unavailable — used legacy lookup.</i>"
        if verdict == "none":
            return (f"❓ No {sport} player close to <b>{html.escape(player)}</b>. Check the spelling and try again.", None)
        if verdict == "ambiguous":
            # Ties are never auto-picked. callback_data stays well under Telegram's 64-byte limit: sim|SPORT|ID|STAT|LINE
            rows = []
            for c in cands[:4]:
                cb = f"sim|{sport}|{c['id']}|{stat}|{line if line is not None else ''}"
                rows.append([{"text": f"{c['name']} ({c['team']})", "callback_data": cb}])
            return (f"🤔 Did you mean one of these for <b>{html.escape(player)}</b>?", {"inline_keyboard": rows})
        if verdict == "confident":
            matched_athlete = cands[0]
            player = matched_athlete["name"]
    elif forced_name and not matched_athlete:
        try:
            import player_index
            verdict, cands = player_index.resolve_decision(forced_name, sport)
            if cands:
                matched_athlete = cands[0]
        except Exception:
            logging.exception("forced_name lookup failed")

    matched_id = str((matched_athlete or {}).get("id") or "")
    matched_team = str((matched_athlete or {}).get("team") or "")

    try:
        import sim_prop_bridge as spb
        with _CMD_LOCK:
            card_html, kb = spb.run_bridge_player_simulation(
                sport, player, stat, line_query=line, bankroll=bankroll,
                sniper_on=sniper_on, live_mode=live_mode,
                athlete_id=matched_id or None,
                known_name=player if matched_id else None,
                known_team=matched_team or None,
            )
            result = dict(spb.LAST_SIM_RESULT or {})
        if result:
            _s = result.get("sim", {})
            if not matched_id and (str(_s.get("team", "")).upper() == sport or _s.get("team") in (None, "")):
                return (f"❓ Couldn't match <b>{html.escape(player)}</b> to a {sport} player on ESPN. "
                        f"Try the full name, e.g. <code>/{sport} VJ Edgecombe rebs</code>.", None)
            return format_casual_slip_card(sport, result, bankroll) + fallback_note, _safe_keyboard(sport)
        return card_html, kb
    except Exception as exc:
        logging.exception("handle_sport_command sim failed for %s", player)
        return (f"⚠️ Could not simulate <b>{html.escape(player)}</b>: {html.escape(str(exc)[:120])}", None)
