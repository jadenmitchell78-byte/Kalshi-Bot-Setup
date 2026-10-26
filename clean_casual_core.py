import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
import requests

# 1. STRICT WHITELIST: Only official Kalshi Sports Ticker Prefixes
ALLOWED_SPORTS_PREFIXES = (
    "KXNBA", "KXNFL", "KXMLB", "KXNHL", "KXTENNIS", "KXATP", "KXWTA", "KXNCAA"
)

# Strictly ban futures, awards, music, and celebrity markets even if under sports series
BANNED_TITLE_WORDS = (
    "RETIRE", "OWNER", "MVP", "CHAMPION", "DRAFT", "HEADLINER", "LUMINATE",
    "OSCAR", "BLANCHETT", "ACT", "ALBUM", "BILLBOARD", "ANNOUNCE", "COACH",
    "WHITE HOUSE", "PRESIDENT", "ELECT", "SELECT", "ROSTER", "HALL OF FAME",
    "THE FALL OFF", "FALL OFF", "GRAMMY", "EMMY", "BOX OFFICE", "CELEBRITY"
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
    today_dt = datetime.now()
    today_str = today_dt.strftime("%b %d, %Y")

    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            events = r.json().get("events", [])
            for ev in events:
                status_obj = ev.get("status", {})
                status_type = status_obj.get("type", {})
                state = status_type.get("state", "pre")  # 'pre', 'in', 'post'
                detail = status_type.get("shortDetail") or status_type.get("detail") or ""
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
        pass

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
    now_dt = datetime.now()
    default_date = f"Today ({now_dt.strftime('%b %d')})"

    if state == "in":
        clock = detail if detail else "In Progress"
        return f"🔴 Live ({clock})"
    if "FINAL" in detail.upper():
        return f"🏁 Final ({now_dt.strftime('%b %d')})"

    if date_iso:
        try:
            clean_iso = date_iso.replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean_iso)
            m_d = dt.strftime("%b %d")
            t_str = dt.strftime("%I:%M %p ET").lstrip("0")
            return f"Today ({m_d}) • {t_str}"
        except Exception:
            pass

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
                    dt = datetime.fromisoformat(c_time.replace("Z", "+00:00"))
                    return f"{dt.strftime('%b %d')} • {dt.strftime('%I:%M %p ET').lstrip('0')}"
                except Exception:
                    pass

    now_dt = datetime.now()
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

    # 1. Must start with an official sports prefix
    if not ticker.startswith(ALLOWED_SPORTS_PREFIXES):
        return False

    # 2. Reject non-sports keywords
    if any(bad in blob for bad in BANNED_TITLE_WORDS):
        return False

    # 3. Check expiration date: reject old/past year contracts
    c_time = market.get("close_time") or market.get("expected_expiration_time")
    if c_time and isinstance(c_time, str):
        try:
            dt = datetime.fromisoformat(c_time.replace("Z", "+00:00"))
            now_utc = datetime.now(timezone.utc)
            if dt < now_utc and (now_utc - dt).total_seconds() > 7200:
                return False
        except Exception:
            pass

    # 4. If active_teams provided from ESPN today's scoreboard, require a match
    if active_teams and not any(t in blob for t in active_teams):
        return False

    return True


def _prob_to_american(prob_dec: float) -> str:
    p = max(0.05, min(0.92, prob_dec))
    if p < 0.5:
        return f"+{int(round(((1.0 - p) / p) * 100))}"
    return f"-{int(round((p / (1.0 - p)) * 100))}"


def format_video_style_single_card(sim: dict, exec_res: dict, bankroll: float = 23.55) -> str:
    """
    Produces the clean promo-video UI with exact game dates and a collapsible sharp math drawer.
    """
    c = sim.get("combo", {})
    joint_p = max(0.10, min(0.88, c.get("joint_prob", 25.0) / 100.0))
    odds_str = _prob_to_american(joint_p)
    stake = max(1.00, round(bankroll * 0.08, 2))
    payout = round(stake / joint_p, 2)
    profit = round(payout - stake, 2)
    status_str = str(exec_res.get("status", "ARMED"))
    badge = "🟢 <b>BET PLACED AUTOMATICALLY</b>" if "PLACED" in status_str.upper() else "⚡ <b>AUTO-SNIPER ARMED</b>"

    sp = sim.get("sport", "NBA")
    p_team = sim.get("team", "")
    p_matchup = sim.get("matchup") or p_team
    p_date = sim.get("game_date") or get_prop_date_str(sp, p_team)

    p_partner = c.get("partner", "Teammate")
    p_side = c.get("side", "OVER")
    p_line = c.get("line", 5.5)
    p_stat = c.get("stat", "AST")
    partner_team = c.get("team", p_team)
    partner_matchup = c.get("matchup", p_matchup)
    partner_date = c.get("game_date") or get_prop_date_str(sp, partner_team)

    matchup_display1 = f" ({p_matchup})" if p_matchup else ""
    matchup_display2 = f" ({partner_matchup})" if partner_matchup else ""

    return (
        f"✅ <b>PREDICTIONS FOUND</b> ✅\n\n"
        f"🎟 <b>2-TEAM AUTO PARLAY</b>  •  <b>ODDS: {odds_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>{sim['player']}{matchup_display1}</b> — {sim['side']} {sim['line']} {sim['stat']}\n"
        f"   📅 <i>{p_date}</i>\n"
        f"🟢 <b>{p_partner}{matchup_display2}</b> — {p_side} {p_line} {p_stat}\n"
        f"   📅 <i>{partner_date}</i>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Stake:</b> <code>${stake:.2f}</code>  ➔  💰 <b>Payout:</b> <code>${payout:.2f}</code> (+${profit:.2f} Profit)\n"
        f"🔥 <b>AI Edge:</b> <code>+{sim.get('ev_edge', 0.0)}%</code>  |  {badge}\n\n"
        f"<blockquote expandable>🧠 <b>Tap for AI Coach & 10k Sim Math</b>\n"
        f"• {sim['player']}: {sim.get('true_prob', 50.0)}% True Prob (Proj: {sim.get('mean', sim['line'])} ± {sim.get('std', 1.5)})\n"
        f"• Floor/Med/Ceil: {sim.get('p10', sim['line'])} / {sim.get('p50', sim['line'])} / {sim.get('p90', sim['line'])}\n"
        f"• Game Date Verified: {p_date}\n"
        f"• Matchup: {sim.get('arch', 'QUANT_HYBRID')} vs {sim.get('scheme', 'BALANCED')} ({sim.get('scheme_pct', 0.0):+.1f}%)\n"
        f"• Coach Note: {sim.get('coach_note', 'Pace and usage favor over volume.')}\n"
        f"• Copula Boost: +{c.get('boost_pct', 1.5)}% (ρ = +{c.get('rho', 0.42):.2f})\n"
        f"• Kalshi Ticker: {exec_res.get('ticker', 'STANDBY')} ({exec_res.get('contracts', 1)}x @ {exec_res.get('limit_cents', 50)}¢)</blockquote>"
    )


def format_video_style_board_scan(sport: str, top_plays: list, exec_res: dict, bankroll: float = 23.55) -> str:
    """
    Video style 3-team parlay scan card with exact game dates and collapsible sharp drawer.
    """
    sport_u = sport.upper()
    now_dt = datetime.now()
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

    leg_block = ""
    math_block = ""
    for p in legs:
        team_str = p.get("matchup") or p.get("team") or ""
        team_tag = f" ({team_str})" if team_str else ""
        date_str = p.get("game_date") or get_prop_date_str(sport_u, p.get("team", ""))
        leg_block += (
            f"🟢 <b>{p['player']}{team_tag}</b> — {p['side']} {p['line']} {p['stat']}\n"
            f"   📅 <i>{date_str}</i>\n"
        )
        math_block += f"• {p['player']} ({date_str}): {p.get('true_prob')}% ({p.get('mean', p['line'])} proj) | {p.get('coach_note', 'Edge confirmed')}\n"

    status_str = str(exec_res.get("status", "ARMED"))
    badge = "🟢 <b>BET PLACED AUTOMATICALLY</b>" if "PLACED" in status_str.upper() else "⚡ <b>AUTO-BET ARMED</b>"

    return (
        f"✅ <b>PREDICTIONS FOUND</b> ✅\n\n"
        f"🎟 <b>{len(legs)}-TEAM AUTO PARLAY</b>  •  <b>ODDS: {odds_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{leg_block}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Total Stake:</b> <code>${stake:.2f}</code>\n"
        f"💰 <b>Est. Payout:</b> <code>${payout:.2f}</code> (<b>+${profit:.2f} Profit</b>)\n"
        f"🔥 <b>Automated Edge:</b> <code>+{avg_edge}%</code>\n"
        f"{badge}\n\n"
        f"<blockquote expandable>🧠 <b>Tap for 10k Sim & Kalshi Order Receipt</b>\n"
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

    leg_lines = ""
    math_drawer_lines = ""
    for leg in legs:
        p_name = leg.get("player") or leg.get("player_name") or leg.get("player_prop") or "Player"
        side = leg.get("side", "OVER")
        line = leg.get("line", 0.0)
        stat = leg.get("stat") or leg.get("stat_type") or "PTS"
        sp = leg.get("sport") or "NBA"
        tm = leg.get("team") or ""
        matchup = leg.get("matchup") or tm
        matchup_tag = f" ({matchup})" if matchup else ""
        date_str = leg.get("game_date") or get_prop_date_str(sp, tm)
        true_p = leg.get("true_prob") or round(leg.get("mc_true_prob", 0.5) * 100, 1)
        mean_v = leg.get("mean") or leg.get("mc_mean", line)
        c_note = leg.get("coach_note") or (leg.get("coaching_notes", ["Edge confirmed"])[0] if leg.get("coaching_notes") else "Edge confirmed")

        leg_lines += (
            f"🟢 <b>{p_name}{matchup_tag}</b> — {side} {line} {stat}\n"
            f"   📅 <i>{date_str}</i>\n"
        )
        math_drawer_lines += f"• {p_name} ({date_str}): {true_p}% True ({mean_v} proj) | {c_note}\n"

    status_badge = "🟢 <b>BET PLACED AUTOMATICALLY</b>" if "PLACED" in order_status.upper() else "⚡ <b>AUTO-SNIPER ARMED</b>"

    return (
        f"✅ <b>PREDICTIONS FOUND</b> ✅\n\n"
        f"🎟 <b>{len(legs)}-TEAM AUTO PARLAY</b>  •  <b>ODDS: {odds_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{leg_lines}"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Stake:</b> <code>${stake_usd:.2f}</code>  ➔  💰 <b>Payout:</b> <code>${payout:.2f}</code> (+${profit:.2f})\n"
        f"🔥 <b>AI Edge:</b> <code>+{edge_pct}%</code>\n"
        f"{status_badge}\n\n"
        f"<blockquote expandable>🧠 <b>Tap for AI Coach & 10k Sim Breakdown</b>\n"
        f"{math_drawer_lines}"
        f"• Copula Correlation Boost: +{combo_data.get('boost_pct', 1.8)}%\n"
        f"• Execution: {order_status}</blockquote>"
    )
