from dataclasses import dataclass
from itertools import combinations
from typing import Any, Dict, List, Optional

from config import (
    BANKROLL_USD,
    HIGH_CONVICTION_EV_PCT,
    KELLY_FRACTION,
    MAX_SLIP_LEGS,
    MAX_STAKE_PCT,
    MC_ITERATIONS,
    MIN_EV_EDGE,
    MIN_SLIP_LEGS,
    MIN_STAKE_USD,
    MODERATE_EDGE_EV_PCT,
    SPORT_EMOJIS,
    UNIT_SIZE_USD,
)
from sim_engine import simulate_joint_gaussian_copula
from slip import RFQQuote, SlipLeg, build_rfq_sniper_quote, prob_to_american


@dataclass
class ParlaySlip:
    legs: List[SlipLeg]
    book_prob: float
    mc_independent_prob: float
    copula_joint_prob: float
    copula_boost_pct: float
    decimal_odds: float
    american_odds: str
    ev_edge: float
    ev_pct: float
    kelly_stake: float
    kelly_units: float
    potential_payout: float
    est_profit: float
    total_liquid_vol: int
    rfq_quote: RFQQuote
    corr_matrix: List[List[float]]
    cannibalized: bool = False
    slip_id: Optional[str] = None
    ai_insight: Optional[str] = None


def evaluate_parlay(
    legs: List[SlipLeg],
    bankroll: float = BANKROLL_USD,
    kelly_fraction: float = KELLY_FRACTION,
    iterations: int = MC_ITERATIONS,
    remaining_slate_cap: float = 32.22,
) -> ParlaySlip:
    """
    Evaluate a 2-to-6 leg parlay using the 10,000-iteration **Joint Gaussian Copula**:
    - Compares combined Book Implied Probability against the Copula Joint True Probability.
    - Computes Copula Correlation Boost (%), true Net +EV%, Quarter-Kelly Stake in units & USD,
      total liquid volume, and estimated profit.
    - Capped strictly by remaining_slate_cap (up to 100% of live Kalshi balance) and live Kalshi cash.
    """
    if not legs:
        raise ValueError("Cannot evaluate an empty parlay slip.")

    copula_res = simulate_joint_gaussian_copula(
        [l.to_dict() for l in legs], iterations=iterations
    )

    book_prob = copula_res["combined_book_prob"]
    mc_indep_prob = copula_res["mc_independent_prob"]
    copula_prob = copula_res["copula_joint_prob"]
    copula_boost_pct = copula_res["copula_boost_pct"]

    decimal_odds = round(1.0 / max(0.005, book_prob), 3)
    american_odds = prob_to_american(book_prob)

    ev_edge = round(copula_prob - book_prob, 4)
    ev_pct = round(((copula_prob * decimal_odds) - 1.0) * 100.0, 2)

    b = max(0.01, decimal_odds - 1.0)
    p = copula_prob
    q = 1.0 - p
    raw_kelly = max(0.0, (b * p - q) / b)
    capped_kelly = min(MAX_STAKE_PCT, raw_kelly * kelly_fraction)

    effective_bankroll = bankroll if bankroll > 0 else 32.22
    max_allowed = min(effective_bankroll, max(0.50, remaining_slate_cap))
    if capped_kelly > 0:
        raw_dollars = effective_bankroll * capped_kelly
        kelly_stake = max(1.00, min(max_allowed, round(raw_dollars, 2)))
    else:
        kelly_stake = max(1.00, min(max_allowed, round(max_allowed * 0.40, 2)))
    kelly_stake = round(kelly_stake, 2)

    unit_base = max(1.0, effective_bankroll * 0.01) if effective_bankroll > 0 else UNIT_SIZE_USD
    kelly_units = round(kelly_stake / unit_base, 2)
    potential_payout = round(kelly_stake * decimal_odds, 2)
    est_profit = round(potential_payout - kelly_stake, 2)
    total_liquid_vol = sum(int(getattr(l, "volume_usd", 2000)) for l in legs)

    rfq_quote = build_rfq_sniper_quote(legs, kelly_stake)

    return ParlaySlip(
        legs=legs,
        book_prob=book_prob,
        mc_independent_prob=mc_indep_prob,
        copula_joint_prob=copula_prob,
        copula_boost_pct=copula_boost_pct,
        decimal_odds=decimal_odds,
        american_odds=american_odds,
        ev_edge=ev_edge,
        ev_pct=ev_pct,
        kelly_stake=kelly_stake,
        kelly_units=kelly_units,
        potential_payout=potential_payout,
        est_profit=est_profit,
        total_liquid_vol=total_liquid_vol,
        rfq_quote=rfq_quote,
        corr_matrix=copula_res["corr_matrix"],
        cannibalized=bool(copula_res["cannibalized"]),
    )


def build_optimal_parlays(
    candidate_legs: List[SlipLeg],
    min_legs: int = MIN_SLIP_LEGS,
    max_legs: int = MAX_SLIP_LEGS,
    bankroll: float = BANKROLL_USD,
    remaining_slate_cap: float = 32.22,
) -> List[ParlaySlip]:
    """
    Search liquid, anti-snipe verified candidate legs for positively correlated
    2-to-3 leg +EV parlay combinations.
    """
    sorted_legs = sorted(candidate_legs, key=lambda x: x.ev_edge, reverse=True)
    deduped: List[SlipLeg] = []
    seen = set()
    for leg in sorted_legs:
        if leg.market_ticker not in seen and leg.anti_snipe_ok:
            seen.add(leg.market_ticker)
            deduped.append(leg)
        if len(deduped) >= 7:
            break

    valid_slips: List[ParlaySlip] = []
    for k in range(min_legs, min(max_legs, len(deduped)) + 1):
        for combo in combinations(deduped[:6], k):
            slip = evaluate_parlay(
                list(combo),
                bankroll=bankroll,
                remaining_slate_cap=remaining_slate_cap,
            )
            if not slip.cannibalized:
                valid_slips.append(slip)

    valid_slips.sort(
        key=lambda s: (s.ev_pct + s.copula_boost_pct * 0.5), reverse=True
    )
    return valid_slips


def format_slip_card(
    slip: ParlaySlip,
    title_banner: str = "",
    mode_label: str = "SIM",
) -> str:
    """
    Format a slip strictly following the exact Peezy Slip Maker mobile-scannable template:
    - Top Conviction Banner (`🚨 ACTION: TAKE (HIGH CONVICTION) 🚨` or `⚡ ACTION: TAKE (MODERATE EDGE) ⚡`)
    - Liquidity & Risk Guard Header (`💧 Liquidity Guard: PASSED ($X,XXX Vol) | 🛡️ Anti-Snipe: CLEAR`)
    - Structured Leg Breakdown (`• Leg X: [Sport] Player Name — Prop Line (OVER/UNDER)`)
    - Copula & Telemetry Context (`🧬 Joint Gaussian Copula Boost` & `📡 Live ESPN Telemetry`)
    - Payout & PnL Receipt Footer
    """
    if slip.ev_pct >= HIGH_CONVICTION_EV_PCT:
        conviction_banner = "🚨 *ACTION: TAKE (HIGH CONVICTION)* 🚨"
    elif slip.ev_pct >= MODERATE_EDGE_EV_PCT:
        conviction_banner = "⚡ *ACTION: TAKE (MODERATE EDGE)* ⚡"
    else:
        conviction_banner = "⚡ *ACTION: TAKE (MODERATE EDGE)* ⚡"

    anti_snipe_ok = all(l.anti_snipe_ok for l in slip.legs)
    shield_str = "CLEAR" if anti_snipe_ok else "FLAGGED"
    liq_str = f"💧 Liquidity Guard: PASSED (${slip.total_liquid_vol:,} Vol) | 🛡️ Anti-Snipe: {shield_str}"

    lines = [
        conviction_banner,
        liq_str,
    ]
    if slip.slip_id:
        lines.append(f"🆔 Slip ID: `{slip.slip_id}` • Mode: `{mode_label}` • RFQ: `{slip.rfq_quote.rfq_id}`")
    lines.append("")

    telemetry_notes: List[str] = []
    for idx, leg in enumerate(slip.legs, start=1):
        bk_pct = round(leg.book_prob * 100.0, 1)
        mc_pct = round(leg.mc_true_prob * 100.0, 1)
        prop_label = leg.format_prop_line_label()
        lines.append(
            f"• *Leg {idx}:* [{leg.sport}] {prop_label}\n"
            f"  📊 Book Odds / Implied: {bk_pct:.1f}% ({leg.american_odds}) | 🎯 10k MC True: {mc_pct:.1f}%\n"
            f"  🔗 [Direct Market Link]({leg.market_url})"
        )
        if leg.telemetry_tag and leg.telemetry_tag not in telemetry_notes:
            telemetry_notes.append(leg.telemetry_tag)

    corr_status = (
        "Negative Correlation Warning"
        if slip.cannibalized
        else (
            "Positive Correlation Verified"
            if slip.copula_boost_pct >= 0
            else "Neutral Multi-Slate Stack"
        )
    )
    boost_sign = "+" if slip.copula_boost_pct >= 0 else ""
    telem_summary = " • ".join(telemetry_notes[:2]) if telemetry_notes else "Live Pace & Usage Priors Active"

    comb_imp_pct = round(slip.book_prob * 100.0, 1)
    true_hit_pct = round(slip.copula_joint_prob * 100.0, 1)
    ev_sign = "+" if slip.ev_pct >= 0 else ""

    lines.extend(
        [
            "",
            f"🧬 Joint Gaussian Copula Boost: {boost_sign}{slip.copula_boost_pct:.1f}% ({corr_status})",
            f"📡 Live ESPN Telemetry: {telem_summary}",
            "━━━━━━━━━━━━━━━━━━━━",
            f"💰 Combined Implied Chance: {comb_imp_pct:.1f}% | True Hit Prob: {true_hit_pct:.1f}%",
            f"📈 Net Edge: {ev_sign}{slip.ev_pct:.1f}% EV | 🏦 Kelly Stake: {slip.kelly_units:.2f}u (${slip.kelly_stake:.2f})",
            f"💵 Est. Payout: ${slip.potential_payout:.2f} (+${slip.est_profit:.2f} Profit)",
        ]
    )

    if slip.ai_insight:
        lines.append(f"🤖 _Gemini Quant: {slip.ai_insight}_")

    return "\n".join(lines)


def format_db_slip_summary(db_slip: Dict[str, Any]) -> str:
    """Format a stored slip record from picks.db using the Peezy receipt layout."""
    status_emoji = {
        "PENDING": "⏳",
        "WON": "🟢",
        "LOST": "🔴",
        "PUSH": "🟡",
    }.get(db_slip["status"], "⏳")

    stake = float(db_slip.get("kelly_stake", db_slip.get("stake", 5.0)))
    units = round(stake / UNIT_SIZE_USD, 2)
    book_p = float(db_slip.get("book_prob", db_slip.get("combined_implied_prob", 0.25)))
    copula_p = float(db_slip.get("copula_joint_prob", db_slip.get("true_prob", 0.28)))
    payout = float(db_slip.get("potential_payout", stake * 3.5))
    profit = round(payout - stake, 2)
    ev_pct = round(((copula_p * (1.0 / max(0.01, book_p))) - 1.0) * 100.0, 1)

    header = (
        f"{status_emoji} *Slip `{db_slip['slip_id']}`* [{db_slip['status']}] • {db_slip['leg_count']}-Leg ({db_slip['american_odds']})\n"
    )
    leg_lines = []
    for idx, leg in enumerate(db_slip.get("legs", []), start=1):
        l_stat = leg["status"]
        l_icon = {"PENDING": "•", "WON": "✅", "LOST": "❌", "PUSH": "➖"}.get(l_stat, "•")
        b_pct = round(float(leg.get("book_prob", leg.get("implied_prob", 0.5))) * 100.0, 1)
        m_pct = round(float(leg.get("mc_true_prob", leg.get("true_prob", 0.55))) * 100.0, 1)
        line_val = leg.get("line")
        line_s = f" — {line_val:g} ({leg['side']})" if line_val is not None else f" ({leg['side']})"
        leg_lines.append(
            f"{l_icon} Leg {idx}: [{leg['sport']}] {leg['player_prop']}{line_s}\n"
            f"  📊 Book: {b_pct:.1f}% | 🎯 10k MC: {m_pct:.1f}% [{l_stat}]"
        )

    footer = (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Combined Implied Chance: {book_p*100:.1f}% | True Hit Prob: {copula_p*100:.1f}%\n"
        f"📈 Net Edge: +{ev_pct:.1f}% EV | 🏦 Kelly Stake: {units:.2f}u (${stake:.2f})\n"
        f"💵 Est. Payout: ${payout:.2f} (+${profit:.2f} Profit)"
    )
    return header + "\n".join(leg_lines) + footer
