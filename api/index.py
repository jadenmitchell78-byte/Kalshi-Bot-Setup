import asyncio
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Ensure root package modules are importable on Vercel serverless
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app import _WEB_CACHE, fetch_on_demand_stream, serialize_slip
from config import MAX_PICK6_LEGS, MC_ITERATIONS
from db import (
    clear_idempotency_locks,
    fetch_pending_parlays,
    fetch_settled_slips,
    get_bot_stats,
    get_runtime_state,
    init_db,
    reset_sim_account,
    set_auto_mode,
    set_bankroll_allocation_pct,
    set_mode,
)
from parlay import build_optimal_parlays, evaluate_parlay
from peezy import execute_and_persist_slip, scan_markets_with_telemetry
from slip import clear_cooldown_locks
from telemetry import fetch_all_telemetry


def _run_coro(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            new_loop = asyncio.new_event_loop()
            try:
                return new_loop.run_until_complete(coro)
            finally:
                new_loop.close()
        return loop.run_until_complete(coro)
    except RuntimeError:
        new_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(new_loop)
        return new_loop.run_until_complete(coro)


class handler(BaseHTTPRequestHandler):
    """
    Phase 1 Stateless Vercel Serverless Handler:
    Executes in <300ms per request without persistent background daemons or long-polling,
    preventing Vercel's 10s-60s serverless execution timeout from severing live feeds.
    """

    def _send_json(self, payload: dict, status: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        init_db()
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/api/state", "/api/live-wire", "/api/live", "/api/scores"):
            stats = get_bot_stats()
            now = time.time()
            last_t_ts = _WEB_CACHE.get("telemetry_ts", 0.0)
            if (now - last_t_ts >= 10.0) or not _WEB_CACHE.get("telemetry"):
                try:
                    t_map = _run_coro(fetch_all_telemetry())
                    all_games = []
                    for sp, g_list in t_map.items():
                        for g in g_list:
                            all_games.append(g.to_dict())
                    _WEB_CACHE["telemetry"] = all_games
                    _WEB_CACHE["telemetry_ts"] = now
                except Exception:
                    pass

            if not _WEB_CACHE["props"]:
                legs = _run_coro(scan_markets_with_telemetry())
                _WEB_CACHE["props"] = legs
                _WEB_CACHE["slips"] = build_optimal_parlays(
                    legs, min_legs=2, max_legs=3, bankroll=stats["sim_cash"]
                )[:4]
            return self._send_json(
                {
                    "ok": True,
                    "status": "LIVE",
                    "live_wire": "ONLINE",
                    "stats": stats,
                    "telemetry": _WEB_CACHE["telemetry"],
                    "games": _WEB_CACHE["telemetry"],
                    "props": [l.to_dict() for l in _WEB_CACHE["props"][:16]],
                    "slips": [serialize_slip(s) for s in _WEB_CACHE["slips"][:4]],
                    "open_slips": fetch_pending_parlays(),
                    "settled_slips": fetch_settled_slips(limit=8),
                }
            )

        if path == "/api/stream":
            sport = (qs.get("sport") or ["NBA"])[0]
            query = (qs.get("q") or [""])[0]
            res = _run_coro(fetch_on_demand_stream(sport=sport, query=query))
            return self._send_json(res)

        # Serve index.html UI
        html_path = ROOT_DIR / "templates" / "index.html"
        html_bytes = html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_bytes)))
        self.end_headers()
        self.wfile.write(html_bytes)

    def do_POST(self):
        init_db()
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        body = json.loads(raw_body or "{}")

        if path == "/api/evaluate":
            tickers = body.get("tickers") or []
            selected = [
                l for l in _WEB_CACHE["props"] if l.market_ticker in tickers
            ][:MAX_PICK6_LEGS]
            if len(selected) < 2:
                return self._send_json({"ok": False, "msg": "Select at least 2 legs."})
            stats = get_bot_stats()
            slip = evaluate_parlay(
                selected, bankroll=stats["sim_cash"], iterations=MC_ITERATIONS
            )
            return self._send_json({"ok": True, "slip": serialize_slip(slip)})

        if path == "/api/execute":
            tickers = body.get("tickers") or []
            selected = [
                l for l in _WEB_CACHE["props"] if l.market_ticker in tickers
            ][:MAX_PICK6_LEGS]
            if len(selected) < 2:
                return self._send_json({"ok": False, "msg": "Select at least 2 legs."})
            rt = get_runtime_state()
            slip = evaluate_parlay(
                selected, bankroll=rt["sim_cash"], iterations=MC_ITERATIONS
            )
            _run_coro(execute_and_persist_slip(slip))
            return self._send_json(
                {
                    "ok": True,
                    "msg": f"⚡ Executed {slip.slip_id} (${slip.kelly_stake:.2f} -> ${slip.potential_payout:.2f})!",
                    "slip": serialize_slip(slip),
                }
            )

        if path == "/api/action":
            act = body.get("act")
            if act in ("auto", "sniper"):
                new_auto = set_auto_mode(None)
                return self._send_json(
                    {"ok": True, "msg": f"Auto-Sniper set to {'ON' if new_auto else 'OFF'}"}
                )
            if act == "mode":
                rt = get_runtime_state()
                new_mode = set_mode("LIVE" if rt["mode"] == "SIM" else "SIM")
                return self._send_json({"ok": True, "msg": f"Switched mode to {new_mode}"})
            if act == "force_scan":
                legs = _run_coro(scan_markets_with_telemetry())
                stats = get_bot_stats()
                _WEB_CACHE["props"] = legs
                _WEB_CACHE["slips"] = build_optimal_parlays(
                    legs, min_legs=2, max_legs=3, bankroll=stats["sim_cash"]
                )[:4]
                return self._send_json(
                    {
                        "ok": True,
                        "msg": f"⚡ Force Scan complete ({len(legs)} props) | {stats.get('last_scan_diag', '')}",
                    }
                )
            if act == "clear_locks":
                c_cnt = clear_cooldown_locks()
                db_info = clear_idempotency_locks()
                return self._send_json(
                    {
                        "ok": True,
                        "msg": f"🔓 Flushed {c_cnt + db_info.get('cleared_db_locks', 0)} cooldown & idempotency locks!",
                    }
                )
            if act == "bankroll_cap":
                raw_pct = body.get("pct", 100)
                try:
                    p_val = float(raw_pct)
                    pct_norm = p_val / 100.0 if p_val > 1.0 else p_val
                except Exception:
                    pct_norm = 1.00
                saved_pct = set_bankroll_allocation_pct(pct_norm)
                stats = get_bot_stats()
                cash = stats.get("sim_cash", 32.22)
                new_cap = round(cash * saved_pct, 2)
                return self._send_json(
                    {
                        "ok": True,
                        "bankroll_allocation_pct": saved_pct,
                        "slate_cap": new_cap,
                        "msg": f"🎚️ Bankroll Cap: {int(saved_pct * 100)}% (${new_cap:.2f} Exposure)",
                    }
                )
            if act == "user_building_combo":
                is_active = bool(body.get("active", True))
                return self._send_json(
                    {
                        "ok": True,
                        "msg": "⏸️ Auto-Sniper paused for combo building"
                        if is_active
                        else "▶️ Auto-Sniper resumed",
                        "user_building_combo": is_active,
                    }
                )
            if act == "reset":
                cash = reset_sim_account()
                return self._send_json({"ok": True, "msg": f"Reset Sim Cash to ${cash:.2f}!"})
            return self._send_json({"ok": True})

        return self._send_json({"ok": False, "msg": "Unknown route"}, status=404)
