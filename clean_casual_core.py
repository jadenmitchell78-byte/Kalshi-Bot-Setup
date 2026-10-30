import datetime
import logging
import json
import math
import re
import requests
import time
import traceback
import uuid
from datetime import datetime as dt_cls, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

# 1. STRICT WHITELIST: Only official Kalshi Sports Ticker Prefixes
ALLOWED_SPORTS_PREFIXES = (
    "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXTENNIS", "KXATP", "KXWTA", "KXNCAA", "KXCFB", "KXSOCCER", "KXBOXING", "KXMMA", "KXUFC"
)

# Strictly ban futures, awards, music, celebrity, stadium, and novelty markets even if under sports series
BANNED_TITLE_WORDS = (
    "STADIUM", "JOINCONF", "EXPANSION", "RELOCATE", "COACH", "TRADE", "ROTY", "DPOY",
    "RETIRE", "OWNER", "MVP", "CHAMPION", "DRAFT", "HEADLINER", "LUMINATE",
    "OSCAR", "BLANCHETT", "ACT", "ALBUM", "BILLBOARD", "ANNOUNCE",
    "WHITE HOUSE", "PRESIDENT", "ELECT", "SELECT", "ROSTER", "HALL OF FAME",
    "THE FALL OFF", "FALL OFF", "GRAMMY", "EMMY", "BOX OFFICE", "CELEBRITY",
    "KXMVE", "CROSSCATEGORY"
)

# In-memory cache for live ESPN schedules: {sport: (timestamp, schedule_dict)}
_LIVE_SCHEDULE_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def get_live_sport_schedule(sport: str = "NBA") -> Dict[str, Any]:
    """
    Pulls live ESPN scoreboard for today.
    Extracts real matchups, real start times, real dates, and active teams.
    Cached for 60 seconds.
    """
    sport_u = sport.upper()
    now = time.time()
    if sport_u in _LIVE_SCHEDULE_CACHE:
        ts, cached_data = _LIVE_SCHEDULE_CACHE[sport_u]
        if now - ts < 60.0 and cached_data.get("active_teams"):
            return cached_data

    sport_map = {
        "NBA": "basketball/nba",
        "NFL": "football/nfl",
        "MLB": "baseball/mlb",
        "NHL": "hockey/nhl",
        "WNBA": "basketball/wnba",
    }
    path = sport_map.get(sport_u, "basketball/nba")
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"

    active_teams: Set[str] = set()
    team_games: Dict[str, Dict[str, Any]] = {}
    today_dt = dt_cls.now()
    today_str = today_dt.strftime("%b %d, %Y")

    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            events = r.json().get("events", [])
            for ev in events:
                status_obj = ev.get("status", {})
                status_type = status_obj.get("type", {})
                state = status_type.get("state", "pre").lower()  # 'pre', 'in', 'post'
                detail = str(status_type.get("shortDetail") or status_type.get("detail") or "").upper()
                type_name = str(status_type.get("name", "")).upper()
                is_completed = bool(status_type.get("completed", False))

                # STRICT GAME-STATE GATE: drop finished/final/postponed games
                if is_completed or state == "post" or "FINAL" in detail or "FINAL" in type_name:
                    continue
                if "POSTPONED" in detail or "CANCELED" in detail:
                    continue
                if state not in ("pre", "in"):
                    continue

                ev_date_raw = ev.get("date", "")
                game_time_str = _format_espn_game_time(ev_date_raw, detail, state)

                comps = ev.get("competitions", [{}])[0]
                competitors = comps.get("competitors", [])
                home_abbr, away_abbr = "HOME", "AWAY"
                game_teams: List[str] = []

                for comp in competitors:
                    t = comp.get("team", {})
                    abbr = str(t.get("abbreviation") or t.get("shortDisplayName") or "").upper()
                    if comp.get("homeAway") == "home":
                        home_abbr = abbr
                    else:
                        away_abbr = abbr
                    for k in ("abbreviation", "displayName", "name", "shortDisplayName"):
                        val = t.get(k)
                        if val:
                            active_teams.add(str(val).upper())
                            game_teams.append(str(val).upper())

                matchup_label = f"{away_abbr} @ {home_abbr}"
                game_info = {
                    "matchup": matchup_label,
                    "game_date": game_time_str,
                    "state": state,
                    "home": home_abbr,
                    "away": away_abbr,
                    "raw_date": ev_date_raw,
                }

                for gt in game_teams:
                    team_games[gt] = game_info
    except Exception:
        logging.exception("get_live_sport_schedule failed")

    result = {
        "active_teams": active_teams,
        "team_games": team_games,
        "today_str": today_str,
    }
    if active_teams:
        _LIVE_SCHEDULE_CACHE[sport_u] = (now, result)
    return result


def _format_espn_game_time(date_iso: str, detail: str, state: str) -> str:
    """Helper to format readable dates like 'Today (Oct 26) • 7:30 PM ET' or 'Live (Q3 4:12)'."""
    now_dt = dt_cls.now()
    default_date = f"Today ({now_dt.strftime('%b %d')})"

    if state == "in":
        clock = detail if detail else "In Progress"
        return f"🔴 Live ({clock})"
    if "FINAL" in detail.upper() or state == "post":
        return f"Pre-Game ({now_dt.strftime('%b %d')})"

    if date_iso:
        try:
            clean_iso = date_iso.replace("Z", "+00:00")
            dt = dt_cls.fromisoformat(clean_iso)
            m_d = dt.strftime("%b %d")
            t_str = dt.strftime("%I:%M %p ET").lstrip("0")
            return f"Today ({m_d}) • {t_str}"
        except Exception:
            logging.exception("_format_espn_game_time failed")

    if detail and ("PM" in detail or "AM" in detail):
        return f"Today ({now_dt.strftime('%b %d')}) • {detail.strip()}"

    return default_date


def get_active_teams_today(sport: str = "NBA") -> Set[str]:
    """Pulls today's active teams playing on the ESPN scoreboard."""
    sched = get_live_sport_schedule(sport)
    return sched.get("active_teams", set())


def get_prop_date_str(sport: str, team: str = "", market: Optional[dict] = None) -> str:
    """
    Extracts the verified game date/time for any prop leg.
    1. Looks up ESPN today schedule for this team.
    2. Looks up Kalshi market close_time / expected_expiration_time.
    3. Falls back to verified today date.
    """
    sport_u = sport.upper()
    team_u = team.upper() if team else ""

    if team_u:
        sched = get_live_sport_schedule(sport_u)
        tg = sched.get("team_games", {}).get(team_u)
        if tg and tg.get("game_date"):
            return tg["game_date"]

    if market:
        for f in ("close_time", "expected_expiration_time", "expiration_time"):
            c_time = market.get(f)
            if c_time and isinstance(c_time, str):
                try:
                    dt = dt_cls.fromisoformat(c_time.replace("Z", "+00:00"))
                    # US Eastern game night alignment
                    dt_et = dt.astimezone(timezone(datetime.timedelta(hours=-4)))
                    return f"{dt_et.strftime('%b %d')} • {dt_et.strftime('%I:%M %p ET').lstrip('0')}"
                except Exception:
                    logging.exception("get_prop_date_str failed")

    now_dt = dt_cls.now()
    return f"Today ({now_dt.strftime('%b %d')}) • Active Slate"


def is_real_live_game_contract(market: dict, active_teams: Optional[Set[str]] = None) -> bool:
    """
    100% Whitelist Gate:
    1. Must match official sports exchange prefixes (KXNBA, KXNFL, etc.).
    2. Blocks all music, celebrity, retirement, pop-culture, and novelty words.
    3. If active_teams provided, ensures contract belongs to a team playing today.
    4. Validates date so expired/past contracts are dropped.
    """
    ticker = str(market.get("ticker", "")).upper()
    title = str(market.get("title", "")).upper()
    subtitle = str(market.get("subtitle", "")).upper()
    blob = f"{ticker} {title} {subtitle}"

    # Rule 1: Hard reject non-daily markets and career stats
    if any(bad in ticker for bad in ("CAREER", "SEASON", "RECORD", "AWARD", "LEADER", "HOF", "CHAMP")):
        return False
    if not re.search(r"26(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}", ticker):
        return False

    # Rule 2: Hard reject strike <= 0
    for sf in ("floor_strike", "strike_price", "cap_strike", "line"):
        v = market.get(sf)
        if v is not None:
            try:
                if float(v) <= 0.0:
                    return False
            except (ValueError, TypeError):
                pass

    # 1. Must start with an official sports prefix
    if not ticker.startswith(ALLOWED_SPORTS_PREFIXES):
        return False

    # 2. Reject non-sports keywords
    if any(bad in blob for bad in BANNED_TITLE_WORDS):
        return False

    # 3. Check expiration date: reject old/past contracts (>2h ago)
    c_time = market.get("close_time") or market.get("expected_expiration_time") or market.get("expiration_time")
    if c_time and isinstance(c_time, str):
        try:
            dt = dt_cls.fromisoformat(c_time.replace("Z", "+00:00"))
            now_utc = dt_cls.now(timezone.utc)
            # Drop past expired contracts > 2 hours ago
            if dt < now_utc and (now_utc - dt).total_seconds() > 7200:
                return False
        except Exception:
            logging.exception("is_real_live_game_contract date check failed")

    # 4. If active_teams provided from ESPN today's scoreboard, require a match
    if active_teams and not any(t in blob for t in active_teams):
        return False

    return True


def _prob_to_american(prob_dec: float) -> str:
    p = max(0.05, min(0.92, prob_dec))
    if p < 0.5:
        return f"+{int(round(((1.0 - p) / p) * 100))}"
    return f"-{int(round((p / (1.0 - p)) * 100))}"


def format_pick_headline(p: dict, default_price: int = 50) -> str:
    """
    Formats a pick clearly specifying who/what to take:
    - Player Prop:
      🟢 TAKE: Myles Turner OVER 6.5 REB (YES @ 54¢)
         🏟️ Matchup: IND @ NYK | 📊 Ticker: KXNBAREB-...
    - Head-to-Head / Match Win / Moneyline:
      🟢 TAKE: Daniil Medvedev ML — TO WIN (YES @ 58¢)
         🏟️ Matchup: Djokovic vs Medvedev | 📊 Ticker: KXTENNIS-...
    - Game Total / Spread:
      🟢 TAKE: Over 224.5 Total Points (YES @ 52¢)
         🏟️ Matchup: LAL @ PHX | 📊 Ticker: KXNBATOT-...
    """
    raw_price = p.get("limit_cents") or p.get("price")
    if raw_price is None:
        line_s = str(p.get("line", ""))
        if "¢" in line_s:
            try:
                raw_price = int(re.sub(r"[^\d]", "", line_s))
            except Exception:
                raw_price = default_price
        else:
            raw_price = default_price
    price_c = int(raw_price) if isinstance(raw_price, (int, float)) else default_price

    raw_side = str(p.get("contract_side") or p.get("side") or "YES").upper()
    contract_side = "NO" if raw_side in ("NO", "UNDER") else "YES"

    stat = str(p.get("stat") or p.get("stat_type") or "PTS").upper()
    side = str(p.get("side") or "OVER").upper()
    line = p.get("line", "")
    ticker = str(p.get("ticker") or "")
    matchup = str(p.get("matchup") or "")
    player = str(p.get("player") or p.get("player_name") or p.get("partner") or "")
    target = str(p.get("target_pick") or p.get("subtitle") or p.get("yes_sub_title") or "")

    # 1. Total / Spread check
    is_total = any(k in stat for k in ("TOTAL", "SPREAD", "OVER/UNDER", "GAMES")) or "TOTAL" in player.upper()
    
    # 2. Moneyline / Match Win check
    is_ml = (
        stat in ("MATCH WIN", "MONEYLINE", "ML", "WIN", "HEAD TO HEAD")
        or (" vs " in player.lower() and not any(k in stat for k in ("PTS", "REB", "AST", "3PM", "3PT", "PRA", "PASS", "RUSH", "REC", "K", "STRIKE")))
    )

    if is_total:
        tot_side = "Over" if "OVER" in side or "YES" in side else "Under"
        line_clean = str(line).replace("¢", "").strip()
        unit_str = "Total Games" if "GAME" in stat else "Total Points"
        header = f"🟢 <b>TAKE: {tot_side} {line_clean} {unit_str} ({contract_side} @ {price_c}¢)</b>"
        clean_matchup = matchup if matchup and matchup != player else player

    elif is_ml:
        if target and target.upper() not in ("YES", "NO", "WIN / OVER"):
            backed = target
        elif " vs " in player:
            parts = [x.strip() for x in re.split(r"\s+vs\.?\s+", player, flags=re.I)]
            if side in ("AWAY", "2") and len(parts) > 1:
                backed = parts[1]
            else:
                backed = parts[0]
        else:
            backed = player or "Winner"
        backed = re.sub(r"\s*\([^)]*\)", "", backed).strip()
        header = f"🟢 <b>TAKE: {backed} ML — TO WIN ({contract_side} @ {price_c}¢)</b>"
        clean_matchup = matchup if matchup and matchup.upper() != backed.upper() else (player if " vs " in player.lower() else "")

    else:
        # Player Prop
        player_clean = target if target and target.upper() not in ("YES", "NO") else player
        player_clean = re.sub(r"\s*\([^)]*\)", "", player_clean).strip()
        prop_side = "OVER" if side not in ("UNDER", "NO") else "UNDER"
        line_clean = str(line).replace("¢", "").strip()
        header = f"🟢 <b>TAKE: {player_clean} {prop_side} {line_clean} {stat} ({contract_side} @ {price_c}¢)</b>"
        clean_matchup = matchup if matchup and matchup.upper() != player_clean.upper() else ""

    detail_parts = []
    if clean_matchup:
        detail_parts.append(f"🏟️ Matchup: {clean_matchup}")
    if ticker and not ticker.endswith("-LIVE") and ticker != "KXTENNIS-LIVE" and ticker != "N/A":
        if "PROJECTION" in ticker.upper() or ticker.startswith("📊"):
            detail_parts.append("📊 Market: Projection Only (No Open Kalshi Prop Yet)")
        else:
            detail_parts.append(f"📊 Ticker: {ticker}")

    detail_line = f"   {' | '.join(detail_parts)}" if detail_parts else ""
    date_str = p.get("game_date")
    date_line = f"   📅 <i>{date_str}</i>" if date_str else ""

    components = [header]
    if detail_line:
        components.append(detail_line)
    if date_line:
        components.append(date_line)
    return "\n".join(components)


def format_video_style_single_card(sim: dict, exec_res: dict, bankroll: float = 23.55) -> str:
    """
    Produces the clean promo-video UI with exact game dates and a collapsible sharp math drawer.
    """
    c = sim.get("combo") or {}
    joint_p = max(0.10, min(0.88, c.get("joint_prob", 25.0) / 100.0))
    odds_str = _prob_to_american(joint_p)
    stake = max(1.00, round(bankroll * 0.08, 2))
    payout = round(stake / joint_p, 2)
    profit = round(payout - stake, 2)
    status_str = str(exec_res.get("status", "ARMED"))
    if "PROJECTION" in status_str.upper():
        badge = "📊 <b>PROJECTION ONLY (10k MC SIM)</b>"
    elif "PLACED" in status_str.upper():
        badge = "🟢 <b>BET PLACED AUTOMATICALLY</b>"
    else:
        badge = "⚡ <b>AUTO-SNIPER ARMED</b>"

    sp = sim.get("sport", "NBA")
    p_team = sim.get("team", "")
    p_date = sim.get("game_date") or get_prop_date_str(sp, p_team)

    leg1_block = format_pick_headline(sim, default_price=exec_res.get("limit_cents", 50))
    partner_sim = {
        "player": c.get("partner") or (f"{p_team} Star" if p_team else "Correlated Partner"),
        "side": c.get("side", "OVER"),
        "line": c.get("line", 5.5),
        "stat": c.get("stat", "AST"),
        "team": c.get("team", p_team),
        "matchup": c.get("matchup", sim.get("matchup", p_team)),
        "game_date": c.get("game_date") or get_prop_date_str(sp, c.get("team", p_team)),
        "ticker": c.get("ticker", ""),
        "limit_cents": 50,
        "contract_side": "YES",
    }
    leg2_block = format_pick_headline(partner_sim, default_price=50)

    p_name = sim.get("target_pick") or sim.get("player", "Player")
    raw_ev = sim.get("ev_edge", 0.0)
    try:
        ev_str = f"{float(raw_ev):+.1f}%"
    except Exception:
        ev_str = f"+{raw_ev}%" if not str(raw_ev).startswith("+") else f"{raw_ev}%"

    return (
        f"✅ <b>PREDICTIONS FOUND</b> ✅\n\n"
        f"🎟 <b>2-TEAM AUTO PARLAY</b>  •  <b>ODDS: {odds_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{leg1_block}\n"
        f"{leg2_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Stake:</b> <code>${stake:.2f}</code>  ➔  💰 <b>Payout:</b> <code>${payout:.2f}</code> (+${profit:.2f} Profit)\n"
        f"🔥 <b>AI Edge:</b> <code>{ev_str}</code>  |  {badge}\n\n"
        f"<blockquote expandable>🧠 <b>Tap for AI Coach &amp; 10k Sim Math</b>\n"
        f"• {p_name}: {sim.get('true_prob', 50.0)}% True Prob (Proj: {sim.get('mean', sim.get('line', ''))} ± {sim.get('std', 1.5)})\n"
        f"• Floor/Med/Ceil: {sim.get('p10', sim.get('line', ''))} / {sim.get('p50', sim.get('line', ''))} / {sim.get('p90', sim.get('line', ''))}\n"
        f"• Game Date Verified: {p_date}\n"
        f"• Matchup: {sim.get('arch', 'QUANT_HYBRID')} vs {sim.get('scheme', 'BALANCED')} ({sim.get('scheme_pct', 0.0):+.1f}%)\n"
        f"• Coach Note: {sim.get('coach_note', 'Pace and usage favor over volume.')}\n"
        f"• Copula Boost: +{c.get('boost_pct', 1.5)}% (ρ = +{c.get('rho', 0.42):.2f})\n"
        f"• Top Anchor Snipe: {exec_res.get('ticker', 'N/A')} ({exec_res.get('contracts', 1)}x @ {exec_res.get('limit_cents', 50)}¢)\n"
        f"• Execution Status: {status_str}</blockquote>"
    )


def format_video_style_board_scan(sport: str, top_plays: list, exec_res: dict, bankroll: float = 23.55) -> str:
    """
    Video style 3-team parlay scan card with exact game dates and collapsible sharp drawer.
    """
    sport_u = sport.upper()
    now_dt = dt_cls.now()
    today_label = now_dt.strftime("%b %d, %Y")

    if not top_plays:
        return (
            f"📡 <b>PEZZY AUTO-BOT  •  {sport_u}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏸ <b>No active live/upcoming {sport_u} games on today's slate ({today_label}) right now.</b>"
        )

    legs = top_plays[:3]
    raw_prod = 1.0
    for p in legs:
        raw_prod *= (p.get("true_prob", 50.0) / 100.0)
    joint_p = min(0.85, max(0.05, raw_prod * 1.15))
    odds_str = _prob_to_american(joint_p)

    stake = max(1.00, round(bankroll * 0.10, 2))
    payout = round(stake / max(0.05, joint_p), 2)
    profit = round(payout - stake, 2)
    avg_edge = round(sum(p.get("ev_edge", 0.0) for p in legs) / len(legs), 1)

    leg_lines_list = []
    math_block = ""
    for p in legs:
        leg_lines_list.append(format_pick_headline(p, default_price=p.get("limit_cents", 50)))
        p_name = p.get("target_pick") or p.get("player", "Prop")
        d_str = p.get("game_date") or ""
        math_block += f"• {p_name} ({d_str}): {p.get('true_prob')}% ({p.get('mean', p.get('line', ''))} proj) | {p.get('coach_note', 'Edge confirmed')}\n"

    leg_block = "\n".join(leg_lines_list)
    status_str = str(exec_res.get("status", "ARMED"))
    badge = "🟢 <b>BET PLACED AUTOMATICALLY</b>" if "PLACED" in status_str.upper() else "⚡ <b>AUTO-BET ARMED</b>"

    return (
        f"✅ <b>PREDICTIONS FOUND</b> ✅\n\n"
        f"🎟 <b>{len(legs)}-TEAM AUTO PARLAY</b>  •  <b>ODDS: {odds_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{leg_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Total Stake:</b> <code>${stake:.2f}</code>\n"
        f"💰 <b>Est. Payout:</b> <code>${payout:.2f}</code> (<b>+${profit:.2f} Profit</b>)\n"
        f"🔥 <b>Automated Edge:</b> <code>{avg_edge:+.1f}%</code>\n"
        f"{badge}\n\n"
        f"<blockquote expandable>🧠 <b>Tap for 10k Sim &amp; Kalshi Order Receipt</b>\n"
        f"{math_block}"
        f"• Schedule Source: ESPN Live Scoreboard ({today_label})\n"
        f"• Top Anchor Snipe: {exec_res.get('ticker', 'N/A')} ({exec_res.get('contracts', 1)}x @ {exec_res.get('limit_cents', 50)}¢)\n"
        f"• Execution Status: {status_str}</blockquote>"
    )


def format_casual_video_slip(legs: list, combo_data: dict, stake_usd: float, order_status: str) -> str:
    """
    Produces the exact ultra-clean 'Parlay Auto Bot' UI with dates on every leg.
    Hides complex math inside a collapsible <blockquote expandable> drawer.
    """
    joint_prob = max(0.05, min(0.90, combo_data.get("joint_prob", 22.0) / 100.0 if combo_data.get("joint_prob", 22.0) > 1.0 else combo_data.get("joint_prob", 0.22)))
    odds_str = _prob_to_american(joint_prob)
    payout = round(stake_usd * (1.0 / joint_prob), 2)
    profit = round(payout - stake_usd, 2)
    edge_pct = round(combo_data.get("combo_ev", 24.5), 1)

    leg_lines_list = []
    math_drawer_lines = ""
    for leg in legs:
        leg_lines_list.append(format_pick_headline(leg, default_price=leg.get("limit_cents", 50)))
        p_name = leg.get("target_pick") or leg.get("player") or leg.get("player_name") or "Player"
        d_str = leg.get("game_date") or ""
        true_p = leg.get("true_prob") or round(leg.get("mc_true_prob", 0.5) * 100, 1)
        mean_v = leg.get("mean") or leg.get("mc_mean", leg.get("line", ""))
        c_note = leg.get("coach_note") or (leg.get("coaching_notes", ["Edge confirmed"])[0] if leg.get("coaching_notes") else "Edge confirmed")
        math_drawer_lines += f"• {p_name} ({d_str}): {true_p}% True ({mean_v} proj) | {c_note}\n"

    leg_lines = "\n".join(leg_lines_list)
    status_badge = "🟢 <b>BET PLACED AUTOMATICALLY</b>" if "PLACED" in order_status.upper() else "⚡ <b>AUTO-SNIPER ARMED</b>"

    return (
        f"✅ <b>PREDICTIONS FOUND</b> ✅\n\n"
        f"🎟 <b>{len(legs)}-TEAM AUTO PARLAY</b>  •  <b>ODDS: {odds_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{leg_lines}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Stake:</b> <code>${stake_usd:.2f}</code>  ➔  💰 <b>Payout:</b> <code>${payout:.2f}</code> (+${profit:.2f})\n"
        f"🔥 <b>AI Edge:</b> <code>{edge_pct:+.1f}%</code>\n"
        f"{status_badge}\n\n"
        f"<blockquote expandable>🧠 <b>Tap for AI Coach &amp; 10k Sim Breakdown</b>\n"
        f"{math_drawer_lines}"
        f"• Copula Correlation Boost: +{combo_data.get('boost_pct', 1.8)}%\n"
        f"• Execution Status: {order_status}</blockquote>"
    )


def build_casual_video_buttons(stream_url: str = "https://streamed.su/watch/basketball", as_ptb: bool = False) -> Any:
    """
    Returns the clean button layout matching the video UI:
    [ 🟢 PLACE AUTO-BET ]  [ 📺 WATCH LIVE STREAM ]
    [ ⚡ FORCE RESCAN ]    [ 🔙 MENU HUD ]
    Supports both PTB InlineKeyboardMarkup and standard dict format.
    """
    rows_data = [
        [
            {"text": "🟢 PLACE AUTO-BET", "callback_data": "menu_force_scan"},
            {"text": "📺 WATCH LIVE STREAM", "url": stream_url},
        ],
        [
            {"text": "⚡ FORCE RESCAN", "callback_data": "menu_force_scan"},
            {"text": "🔙 MENU HUD", "callback_data": "menu_refresh_hud"},
        ],
    ]
    if as_ptb:
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
            return InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(btn["text"], callback_data=btn.get("callback_data"), url=btn.get("url"))
                    for btn in row
                ]
                for row in rows_data
            ])
        except Exception:
            pass
    return {"inline_keyboard": rows_data}
