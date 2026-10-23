import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

import aiohttp
from aiohttp import web

from config import BASE_DIR, MAX_PICK6_LEGS, MC_ITERATIONS, WEB_PORT
from db import (
    clear_idempotency_locks,
    fetch_pending_parlays,
    fetch_settled_slips,
    get_bot_stats,
    get_runtime_state,
    init_db,
    reset_sim_account,
    set_auto_mode,
    set_mode,
)
from parlay import ParlaySlip, build_optimal_parlays, evaluate_parlay, format_slip_card
from settle_engine import run_settlement_cycle
from slip import SlipLeg, clear_cooldown_locks
from telemetry import fetch_all_telemetry

logger = logging.getLogger("peezy.web")

# Shared in-memory cache of latest scanned legs & slips for fast UI rendering
_WEB_CACHE: Dict[str, Any] = {
    "props": [],
    "slips": [],
    "telemetry": [],
    "streams_cache": {},
}

STREAMED_PK_BASE = "https://streamed.pk/api"
SPORT_TO_STREAMED_CAT = {
    "NBA": "basketball",
    "WNBA": "basketball",
    "NFL": "american-football",
    "MLB": "baseball",
    "TENNIS": "tennis",
    "ESPORTS": "other",
}


async def fetch_on_demand_stream(sport: str, query: str = "") -> Dict[str, Any]:
    """
    Phase 3 On-Demand Stream Resolver (streamed.pk API):
    Fetches live match streams ONLY when a user clicks 'Watch Live' on a specific match/slip,
    preventing global page lag and keeping video state isolated from live-wire odds polling.
    """
    cat = SPORT_TO_STREAMED_CAT.get(sport.upper(), "basketball")
    cache_key = f"{cat}:{query.upper().strip()}"
    cached = _WEB_CACHE["streams_cache"].get(cache_key)
    if cached:
        return cached

    matches_url = f"{STREAMED_PK_BASE}/matches/{cat}"
    q_tokens = [t.lower() for t in query.replace("—", " ").split() if len(t) >= 3]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                matches_url, timeout=aiohttp.ClientTimeout(total=4)
            ) as resp:
                if resp.status == 200:
                    matches = await resp.json()
                    chosen = None
                    for m in matches or []:
                        title = str(m.get("title") or "").lower()
                        if q_tokens and any(tok in title for tok in q_tokens):
                            chosen = m
                            break
                    if not chosen and matches:
                        chosen = matches[0]

                    if chosen:
                        sources = chosen.get("sources") or []
                        if sources:
                            src_name = sources[0].get("source")
                            src_id = sources[0].get("id")
                            stream_url = f"{STREAMED_PK_BASE}/stream/{src_name}/{src_id}"
                            async with session.get(
                                stream_url, timeout=aiohttp.ClientTimeout(total=4)
                            ) as s_resp:
                                if s_resp.status == 200:
                                    streams = await s_resp.json()
                                    if streams and isinstance(streams, list):
                                        embed_url = streams[0].get("embedUrl") or ""
                                        if embed_url:
                                            result = {
                                                "ok": True,
                                                "title": chosen.get("title", query or sport),
                                                "sport": sport,
                                                "source": src_name,
                                                "embed_url": embed_url,
                                                "streams": [
                                                    {
                                                        "source": src_name,
                                                        "hd": bool(st.get("hd", True)),
                                                        "language": st.get("language", "EN"),
                                                        "embed_url": st.get("embedUrl"),
                                                    }
                                                    for st in streams[:4]
                                                    if st.get("embedUrl")
                                                ],
                                            }
                                            _WEB_CACHE["streams_cache"][cache_key] = result
                                            return result
    except Exception as exc:
        logger.debug("streamed.pk lookup fallback for %s (%s): %s", sport, query, exc)

    # Resilient fallback embed url from streamed.pk category directory
    fallback_embed = f"https://streamed.pk/watch/{cat}"
    return {
        "ok": True,
        "title": f"{sport} Live Feed • {query or 'Main Broadcast'}",
        "sport": sport,
        "source": "streamed.pk",
        "embed_url": fallback_embed,
        "streams": [
            {
                "source": "alpha-hd",
                "hd": True,
                "language": "EN",
                "embed_url": fallback_embed,
            }
        ],
    }


def serialize_slip(s: ParlaySlip) -> Dict[str, Any]:
    telem_notes: List[str] = []
    for l in s.legs:
        if l.telemetry_tag and l.telemetry_tag not in telem_notes:
            telem_notes.append(l.telemetry_tag)
    return {
        "slip_id": s.slip_id,
        "legs": [l.to_dict() for l in s.legs],
        "book_prob": s.book_prob,
        "book_prob_pct": round(s.book_prob * 100.0, 1),
        "mc_independent_prob": s.mc_independent_prob,
        "copula_joint_prob": s.copula_joint_prob,
        "copula_prob_pct": round(s.copula_joint_prob * 100.0, 1),
        "copula_boost_pct": round(s.copula_boost_pct, 1),
        "decimal_odds": s.decimal_odds,
        "american_odds": s.american_odds,
        "ev_edge": s.ev_edge,
        "ev_pct": round(s.ev_pct, 1),
        "kelly_stake": round(s.kelly_stake, 2),
        "kelly_units": round(s.kelly_units, 2),
        "potential_payout": round(s.potential_payout, 2),
        "est_profit": round(s.est_profit, 2),
        "total_liquid_vol": s.total_liquid_vol,
        "cannibalized": s.cannibalized,
        "telemetry_summary": " • ".join(telem_notes[:2]) if telem_notes else "Live Pace & Usage Priors Active",
        "formatted_card": format_slip_card(s),
    }


def create_web_app(
    scan_fn: Callable[[], Awaitable[List[SlipLeg]]],
    execute_fn: Callable[[ParlaySlip], Awaitable[List[str]]],
    telegram_dispatch_fn: Optional[Callable[[str], Awaitable[None]]] = None,
) -> web.Application:
    """Create the aiohttp companion Web UI server matching the original Peezy Slip Maker mobile design."""
    app = web.Application()

    async def handle_index(request: web.Request) -> web.Response:
        html_path = Path(BASE_DIR) / "templates" / "index.html"
        return web.FileResponse(html_path)

    async def handle_state(request: web.Request) -> web.Response:
        stats = get_bot_stats()
        if not _WEB_CACHE["props"]:
            legs = await scan_fn()
            _WEB_CACHE["props"] = legs
            _WEB_CACHE["slips"] = build_optimal_parlays(
                legs, min_legs=2, max_legs=3, bankroll=stats["sim_cash"]
            )[:4]
            t_map = await fetch_all_telemetry()
            _WEB_CACHE["telemetry"] = [
                {"sport": sp, "badge": g_list[0].format_badge()}
                for sp, g_list in t_map.items()
                if g_list
            ]

        return web.json_response(
            {
                "stats": stats,
                "telemetry": _WEB_CACHE["telemetry"],
                "props": [l.to_dict() for l in _WEB_CACHE["props"][:16]],
                "slips": [serialize_slip(s) for s in _WEB_CACHE["slips"][:4]],
                "open_slips": fetch_pending_parlays(),
                "settled_slips": fetch_settled_slips(limit=8),
            }
        )

    async def handle_evaluate(request: web.Request) -> web.Response:
        body = await request.json()
        tickers: List[str] = body.get("tickers") or []
        selected = [
            l for l in _WEB_CACHE["props"] if l.market_ticker in tickers
        ][:MAX_PICK6_LEGS]
        if len(selected) < 2:
            return web.json_response({"ok": False, "msg": "Select at least 2 legs."})

        stats = get_bot_stats()
        slip = evaluate_parlay(
            selected, bankroll=stats["sim_cash"], iterations=MC_ITERATIONS
        )
        return web.json_response({"ok": True, "slip": serialize_slip(slip)})

    async def handle_execute(request: web.Request) -> web.Response:
        body = await request.json()
        tickers: List[str] = body.get("tickers") or []
        send_tg: bool = bool(body.get("send_telegram", False))

        selected = [
            l for l in _WEB_CACHE["props"] if l.market_ticker in tickers
        ][:MAX_PICK6_LEGS]
        if len(selected) < 2:
            return web.json_response({"ok": False, "msg": "Select at least 2 legs."})

        rt = get_runtime_state()
        slip = evaluate_parlay(
            selected, bankroll=rt["sim_cash"], iterations=MC_ITERATIONS
        )
        await execute_fn(slip)
        card = format_slip_card(slip, mode_label=rt["mode"])

        if send_tg and telegram_dispatch_fn:
            await telegram_dispatch_fn(card)
            msg = f"📲 Logged {slip.slip_id} & dispatched to Telegram!"
        else:
            msg = f"⚡ Executed {slip.slip_id} (${slip.kelly_stake:.2f} -> ${slip.potential_payout:.2f})!"

        return web.json_response({"ok": True, "msg": msg, "slip": serialize_slip(slip)})

    async def handle_action(request: web.Request) -> web.Response:
        body = await request.json()
        act = body.get("act")
        if act in ("auto", "sniper"):
            new_auto = set_auto_mode(None)
            return web.json_response(
                {"ok": True, "msg": f"🎯 Sniper set to {'🟢 ARMED' if new_auto else '🔴 DISARMED'}"}
            )
        elif act == "force_scan":
            legs = await scan_fn()
            stats = get_bot_stats()
            _WEB_CACHE["props"] = legs
            _WEB_CACHE["slips"] = build_optimal_parlays(
                legs, min_legs=2, max_legs=3, bankroll=stats["sim_cash"]
            )[:4]
            return web.json_response(
                {
                    "ok": True,
                    "msg": f"⚡ Force Scan complete ({len(legs)} props) | {stats.get('last_scan_diag', '')}",
                }
            )
        elif act == "clear_locks":
            c_cnt = clear_cooldown_locks()
            db_info = clear_idempotency_locks()
            return web.json_response(
                {
                    "ok": True,
                    "msg": f"🔓 Flushed {c_cnt + db_info.get('cleared_db_locks', 0)} cooldown & idempotency locks!",
                }
            )
        elif act == "mode":
            rt = get_runtime_state()
            new_mode = set_mode("LIVE" if rt["mode"] == "SIM" else "SIM")
            return web.json_response({"ok": True, "msg": f"Switched mode to {new_mode}"})
        elif act == "simsettle":
            cnt = await run_settlement_cycle(simulate_outcomes=True)
            return web.json_response({"ok": True, "msg": f"Simulated settlement for {cnt} slip(s)!"})
        elif act == "reset":
            cash = reset_sim_account()
            return web.json_response({"ok": True, "msg": f"Reset Sim Cash to ${cash:.2f}!"})
        return web.json_response({"ok": True})

    async def handle_stream(request: web.Request) -> web.Response:
        """On-demand streamed.pk resolver for a single clicked match/prop."""
        sport = request.query.get("sport", "NBA")
        query = request.query.get("q", "")
        res = await fetch_on_demand_stream(sport=sport, query=query)
        return web.json_response(res)

    app.router.add_get("/", handle_index)
    app.router.add_get("/api/state", handle_state)
    app.router.add_get("/api/stream", handle_stream)
    app.router.add_post("/api/evaluate", handle_evaluate)
    app.router.add_post("/api/execute", handle_execute)
    app.router.add_post("/api/action", handle_action)
    return app


async def start_background_web_server(
    scan_fn: Callable[[], Awaitable[List[SlipLeg]]],
    execute_fn: Callable[[ParlaySlip], Awaitable[List[str]]],
    telegram_dispatch_fn: Optional[Callable[[str], Awaitable[None]]] = None,
    port: int = WEB_PORT,
) -> web.AppRunner:
    """Start the aiohttp mobile Web UI server inside the running asyncio event loop."""
    app = create_web_app(scan_fn, execute_fn, telegram_dispatch_fn)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Mobile Web UI live at http://127.0.0.1:%d", port)
    return runner


if __name__ == "__main__":
    from peezy import execute_and_persist_slip, scan_markets_with_telemetry

    init_db()

    async def _standalone():
        await start_background_web_server(
            scan_fn=scan_markets_with_telemetry,
            execute_fn=execute_and_persist_slip,
            port=WEB_PORT,
        )
        while True:
            await asyncio.sleep(3600)

    asyncio.run(_standalone())
