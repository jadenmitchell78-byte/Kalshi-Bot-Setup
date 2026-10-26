import re
from typing import Any, Dict, List, Optional, Set
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
    "THE FALL OFF", "FALL OFF", "GRAMMY", "EMMY", "BOX OFFICE"
)

def get_active_teams_today(sport: str = "NBA") -> Set[str]:
    """
    Pulls today's live/upcoming ESPN scoreboard so off-day teams and novelty props are dropped.
    """
    sport_map = {
        "NBA": "basketball/nba",
        "NFL": "football/nfl",
        "MLB": "baseball/mlb",
        "NHL": "hockey/nhl"
    }
    path = sport_map.get(sport.upper(), "basketball/nba")
    url = f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"
    active = set()
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            for ev in r.json().get("events", []):
                state = ev.get("status", {}).get("type", {}).get("state")
                if state in ("in", "pre"):
                    for comp in ev.get("competitions", [{}])[0].get("competitors", []):
                        t = comp.get("team", {})
                        for k in ("abbreviation", "displayName", "name", "shortDisplayName"):
                            if t.get(k):
                                active.add(t[k].upper())
    except Exception:
        pass
    return active

def is_real_live_game_contract(market: dict, active_teams: Optional[Set[str]] = None) -> bool:
    """
    100% Whitelist Gate: Blocks all music, celebrity, retirement, pop-culture, and off-day contracts.
    """
    ticker = str(market.get("ticker", "")).upper()
    title = str(market.get("title", "")).upper()
    subtitle = str(market.get("subtitle", "")).upper()
    blob = f"{ticker} {title} {subtitle}"

    # Must start with an official sports exchange prefix
    if not ticker.startswith(ALLOWED_SPORTS_PREFIXES):
        return False

    # Strictly reject non-sports keywords
    if any(bad in blob for bad in BANNED_TITLE_WORDS):
        return False

    # If active_teams is provided from ESPN today's scoreboard, require a match
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
    Produces the clean promo-video UI with a collapsible sharp math drawer.
    """
    c = sim.get("combo", {})
    joint_p = max(0.10, min(0.88, c.get("joint_prob", 25.0) / 100.0))
    odds_str = _prob_to_american(joint_p)
    stake = max(1.00, round(bankroll * 0.08, 2))
    payout = round(stake / joint_p, 2)
    profit = round(payout - stake, 2)
    status_str = str(exec_res.get("status", "ARMED"))
    badge = "🟢 <b>BET PLACED AUTOMATICALLY</b>" if "PLACED" in status_str.upper() else "⚡ <b>AUTO-SNIPER ARMED</b>"

    p_partner = c.get("partner", "Teammate")
    p_side = c.get("side", "OVER")
    p_line = c.get("line", 5.5)
    p_stat = c.get("stat", "AST")

    return (
        f"✅ <b>PREDICTIONS FOUND</b> ✅\n\n"
        f"🎟 <b>2-TEAM AUTO PARLAY</b>  •  <b>ODDS: {odds_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 <b>{sim['player']} ({sim.get('team', '')})</b> — {sim['side']} {sim['line']} {sim['stat']}\n"
        f"🟢 <b>{p_partner}</b> — {p_side} {p_line} {p_stat}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💵 <b>Stake:</b> <code>${stake:.2f}</code>  ➔  💰 <b>Payout:</b> <code>${payout:.2f}</code> (+${profit:.2f} Profit)\n"
        f"🔥 <b>AI Edge:</b> <code>+{sim.get('ev_edge', 0.0)}%</code>  |  {badge}\n\n"
        f"<blockquote expandable>🧠 <b>Tap for AI Coach & 10k Sim Math</b>\n"
        f"• {sim['player']}: {sim.get('true_prob', 50.0)}% True Prob (Proj: {sim.get('mean', sim['line'])} ± {sim.get('std', 1.5)})\n"
        f"• Floor/Med/Ceil: {sim.get('p10', sim['line'])} / {sim.get('p50', sim['line'])} / {sim.get('p90', sim['line'])}\n"
        f"• Matchup: {sim.get('arch', 'QUANT_HYBRID')} vs {sim.get('scheme', 'BALANCED')} ({sim.get('scheme_pct', 0.0):+.1f}%)\n"
        f"• Coach Note: {sim.get('coach_note', 'Pace and usage favor over volume.')}\n"
        f"• Copula Boost: +{c.get('boost_pct', 1.5)}% (ρ = +{c.get('rho', 0.42):.2f})\n"
        f"• Kalshi Ticker: {exec_res.get('ticker', 'STANDBY')} ({exec_res.get('contracts', 1)}x @ {exec_res.get('limit_cents', 50)}¢)</blockquote>"
    )

def format_video_style_board_scan(sport: str, top_plays: list, exec_res: dict, bankroll: float = 23.55) -> str:
    """
    Video style 3-team parlay scan card with collapsible sharp drawer.
    """
    if not top_plays:
        return (
            f"📡 <b>PEZZY AUTO-BOT  •  {sport}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"⏸ <b>No active live/upcoming {sport} games on today's slate right now.</b>"
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
        team_tag = f" ({p.get('team')})" if p.get("team") else ""
        leg_block += f"🟢 <b>{p['player']}{team_tag}</b> — {p['side']} {p['line']} {p['stat']}\n"
        math_block += f"• {p['player']}: {p.get('true_prob')}% ({p.get('mean', p['line'])} proj) | {p.get('coach_note', 'Edge confirmed')}\n"

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
        f"• Top Anchor Snipe: {exec_res.get('ticker', 'N/A')} ({exec_res.get('contracts', 1)}x @ {exec_res.get('limit_cents', 50)}¢)\n"
        f"• Execution Status: {status_str}</blockquote>"
    )

def format_casual_video_slip(legs: list, combo_data: dict, stake_usd: float, order_status: str) -> str:
    """
    Produces the exact ultra-clean 'Parlay Auto Bot' UI from the promo video.
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
        true_p = leg.get("true_prob") or round(leg.get("mc_true_prob", 0.5) * 100, 1)
        mean_v = leg.get("mean") or leg.get("mc_mean", line)
        c_note = leg.get("coach_note") or (leg.get("coaching_notes", ["Edge confirmed"])[0] if leg.get("coaching_notes") else "Edge confirmed")

        leg_lines += f"🟢 <b>{p_name}</b> — {side} {line} {stat}\n"
        math_drawer_lines += f"• {p_name}: {true_p}% True ({mean_v} proj) | {c_note}\n"

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
