import asyncio
import datetime
import json
import logging
import math
import random
import re
import requests
import time
import traceback
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp

from config import KALSHI_BASE_URL, SETTLE_POLL_INTERVAL_SEC
from db import (
    fetch_pending_parlays,
    fetch_slip_by_id,
    get_runtime_state,
    update_leg_outcome,
    update_slip_settlement,
)
from parlay import format_db_slip_summary
from telemetry import GameTelemetry, fetch_all_telemetry

logger = logging.getLogger("peezy.settle")


async def fetch_market_status(
    session: aiohttp.ClientSession, ticker: str
) -> Optional[Dict[str, Any]]:
    """Query Kalshi v2 API for a specific market ticker's live status and settlement result."""
    url = f"{KALSHI_BASE_URL}/markets/{ticker}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("market")
    except Exception as exc:
        logger.debug("Kalshi market lookup skipped for %s: %s", ticker, exc)
    return None


def grade_leg_from_kalshi(
    leg: Dict[str, Any], market: Dict[str, Any]
) -> Optional[str]:
    """Grade leg as WON, LOST, or PUSH from settled Kalshi market payload."""
    status = str(market.get("status") or "").lower()
    result = str(market.get("result") or "").lower()

    if status not in ("settled", "finalized", "closed") or not result:
        return None

    if result in ("void", "scalar", "push", "canceled"):
        return "PUSH"

    side = str(leg.get("side") or "OVER").upper()
    if side in ("YES", "OVER"):
        return "WON" if result == "yes" else "LOST"
    elif side in ("NO", "UNDER"):
        return "WON" if result == "no" else "LOST"
    return None


def grade_leg_from_espn_boxscore(
    leg: Dict[str, Any], telemetry_map: Dict[str, List[GameTelemetry]]
) -> Optional[str]:
    """
    Cross-check final ESPN box scores / leader telemetry for completed games
    to grade player props automatically when the game status reaches FINAL.
    """
    sport = str(leg.get("sport") or "OTHER").upper()
    games = telemetry_map.get(sport) or []
    prop_upper = str(leg.get("player_prop") or "").upper()
    line = leg.get("line")
    if line is None:
        return None

    eff_line = float(line)
    side = str(leg.get("side") or "OVER").upper()

    for g in games:
        if not g.is_final:
            continue
        for player_name, stat_dict in g.player_stats.items():
            if player_name and player_name in prop_upper:
                # Match the primary leader stat value
                stat_val = next(iter(stat_dict.values()), None)
                if stat_val is not None:
                    if abs(stat_val - eff_line) < 1e-6:
                        return "PUSH"
                    if side in ("OVER", "YES"):
                        return "WON" if stat_val > eff_line else "LOST"
                    else:
                        return "WON" if stat_val < eff_line else "LOST"
    return None


def evaluate_slip_settlement(slip: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Determine if a multi-leg parlay can be settled:
    - Any LOST leg settles the entire parlay as LOST ($0.00 payout).
    - Once all legs are non-PENDING:
      - All PUSH -> PUSH (stake refunded)
      - Mix of WON and PUSH -> WON with compounded multiplier across WON legs
    """
    legs = slip.get("legs", [])
    if not legs:
        return None

    statuses = [l["status"].upper() for l in legs]
    if "LOST" in statuses:
        return {"status": "LOST", "actual_payout": 0.0}

    if "PENDING" in statuses:
        return None

    active_multipliers = [
        (1.0 / float(l.get("book_prob", l.get("implied_prob", 0.5))))
        for l in legs
        if l["status"].upper() == "WON"
        and float(l.get("book_prob", l.get("implied_prob", 0.5))) > 0
    ]

    stake = float(slip.get("kelly_stake", slip.get("stake", 5.0)))
    if not active_multipliers:
        return {"status": "PUSH", "actual_payout": round(stake, 2)}

    combined_multiplier = 1.0
    for mult in active_multipliers:
        combined_multiplier *= mult

    return {
        "status": "WON",
        "actual_payout": round(stake * combined_multiplier, 2),
    }


async def run_settlement_cycle(
    notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    simulate_outcomes: bool = False,
) -> int:
    """
    Cross-check picks.db against final ESPN box scores and Kalshi market settlements,
    grade legs as WON/LOST/PUSH, update bankroll metrics, and push settlement recaps to Telegram.
    """
    pending_slips = fetch_pending_parlays()
    if not pending_slips:
        return 0

    telemetry_map = await fetch_all_telemetry()
    settled_count = 0

    async with aiohttp.ClientSession() as session:
        for slip in pending_slips:
            slip_id = slip["slip_id"]
            updated_any_leg = False

            for leg in slip.get("legs", []):
                if leg["status"] != "PENDING":
                    continue

                outcome: Optional[str] = None
                if simulate_outcomes:
                    roll = random.random()
                    true_p = float(leg.get("mc_true_prob", leg.get("true_prob", 0.55)))
                    if roll < 0.03:
                        outcome = "PUSH"
                    elif roll < true_p:
                        outcome = "WON"
                    else:
                        outcome = "LOST"
                else:
                    market = await fetch_market_status(session, leg["market_ticker"])
                    if market:
                        outcome = grade_leg_from_kalshi(leg, market)
                    if not outcome:
                        outcome = grade_leg_from_espn_boxscore(leg, telemetry_map)

                if outcome:
                    b_prob = float(leg.get("book_prob", leg.get("implied_prob", 0.5)))
                    leg_stake = float(leg.get("kelly_stake", 5.0))
                    mult = (
                        (1.0 / b_prob)
                        if outcome == "WON" and b_prob > 0
                        else (1.0 if outcome == "PUSH" else 0.0)
                    )
                    leg_pnl = (
                        leg_stake * (mult - 1.0)
                        if outcome == "WON"
                        else (0.0 if outcome == "PUSH" else -leg_stake)
                    )
                    update_leg_outcome(leg["leg_id"], outcome, mult, leg_pnl)
                    leg["status"] = outcome
                    updated_any_leg = True

            refreshed = (fetch_slip_by_id(slip_id) or slip) if updated_any_leg else slip
            settlement = evaluate_slip_settlement(refreshed)
            if settlement:
                new_status = settlement["status"]
                payout = settlement["actual_payout"]
                update_slip_settlement(slip_id, new_status, payout)
                settled_count += 1

                final_slip = fetch_slip_by_id(slip_id)
                rt = get_runtime_state()
                if notify_callback and final_slip:
                    stake_val = float(final_slip.get("kelly_stake", final_slip.get("stake", 5.0)))
                    net_profit = round(payout - stake_val, 2)
                    sign = "+" if net_profit >= 0 else ""
                    banner = (
                        f"🔔 *SLIP SETTLED: {new_status}* 🔔\n"
                        f"💵 Realized Payout: `${payout:.2f}` (Net PnL: `{sign}${net_profit:.2f}`)\n"
                        f"🏦 Sim Cash Balance: `${rt['sim_cash']:.2f}`\n\n"
                        + format_db_slip_summary(final_slip)
                    )
                    await notify_callback(banner)

    return settled_count


async def settle_engine_worker(
    notify_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Async background settlement daemon polling ESPN box scores & Kalshi market settlements."""
    logger.info(
        "Settlement daemon started (polling every %ds)...", SETTLE_POLL_INTERVAL_SEC
    )
    while stop_event is None or not stop_event.is_set():
        try:
            settled = await run_settlement_cycle(notify_callback=notify_callback)
            if settled > 0:
                logger.info("Settled %d parlay slip(s).", settled)
        except Exception as exc:
            logger.error("Error in settle_engine_worker cycle: %s", exc)

        try:
            if stop_event is not None:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=SETTLE_POLL_INTERVAL_SEC
                )
            else:
                await asyncio.sleep(SETTLE_POLL_INTERVAL_SEC)
        except asyncio.TimeoutError:
            pass
