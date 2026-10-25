import base64
import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp

from config import (
    BASE_DIR,
    GEMINI_API_KEY,
    GEMINI_MODEL,
    KALSHI_API_KEY,
    KALSHI_BASE_URL,
    KALSHI_PRIVATE_KEY,
    KALSHI_PRIVATE_KEY_PATH,
)

logger = logging.getLogger("peezy.api")

# =========================================================
# PURE-PYTHON RFC 8032 ED25519 SIGNER (No cryptography .so needed)
# =========================================================
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_BY = 46316835694926478169428394003475163141307993866256225615783033603165251855960
_B = (_BX, _BY, 1, (_BX * _BY) % _P)


def _ed_add(pt1, pt2):
    x1, y1, z1, t1 = pt1
    x2, y2, z2, t2 = pt2
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (t1 * 2 * _D * t2) % _P
    d = (z1 * 2 * z2) % _P
    e = (b - a) % _P
    f = (d - c) % _P
    g = (d + c) % _P
    h = (b + a) % _P
    return (e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P


def _ed_scalarmult(pt, e):
    res = (0, 1, 1, 0)
    addend = pt
    while e > 0:
        if e & 1:
            res = _ed_add(res, addend)
        addend = _ed_add(addend, addend)
        e >>= 1
    return res


def _ed_encode(pt):
    x, y, z, _ = pt
    zi = pow(z, _P - 2, _P)
    x = (x * zi) % _P
    y = (y * zi) % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _load_ed25519_seed() -> Optional[bytes]:
    pem_text = KALSHI_PRIVATE_KEY.strip()
    if not pem_text:
        candidates = []
        if KALSHI_PRIVATE_KEY_PATH:
            candidates.append(Path(KALSHI_PRIVATE_KEY_PATH).expanduser())
        candidates.append(BASE_DIR / "kalshi_private.pem")
        candidates.append(BASE_DIR.parent / "kalshi_private_key.pem")
        for p in candidates:
            if p.exists():
                pem_text = p.read_text(encoding="utf-8").strip()
                break
    if not pem_text:
        return None
    if "\\n" in pem_text:
        pem_text = pem_text.replace("\\n", "\n")
    try:
        b64_lines = [
            ln.strip()
            for ln in pem_text.splitlines()
            if ln.strip() and not ln.strip().startswith("-----")
        ]
        der = base64.b64decode("".join(b64_lines))
        return der[-32:] if len(der) >= 32 else None
    except Exception as exc:
        logger.warning("Failed to parse Ed25519 seed: %s", exc)
        return None


def kalshi_Configured() -> bool:
    """Return True if both KALSHI_API_KEY and a valid Ed25519 private key seed are configured."""
    return bool(KALSHI_API_KEY and _load_ed25519_seed() is not None)


def build_kalshi_auth_headers(method: str, url: str) -> Dict[str, str]:
    """
    Generate signed headers required by Kalshi v2 authenticated endpoints:
    KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE.
    Uses Pure-Python RFC 8032 Ed25519 signing (zero C/Rust .so dependencies).
    """
    seed = _load_ed25519_seed()
    if not KALSHI_API_KEY or seed is None:
        return {}

    timestamp_ms = str(int(time.time() * 1000))
    parsed_path = urlparse(url).path
    msg_bytes = f"{timestamp_ms}{method.upper()}{parsed_path}".encode("utf-8")

    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    prefix = h[32:64]
    pub = _ed_encode(_ed_scalarmult(_B, a))

    r = int.from_bytes(hashlib.sha512(prefix + msg_bytes).digest(), "little") % _L
    r_bytes = _ed_encode(_ed_scalarmult(_B, r))
    k = int.from_bytes(hashlib.sha512(r_bytes + pub + msg_bytes).digest(), "little") % _L
    s = (r + k * a) % _L
    signature = r_bytes + s.to_bytes(32, "little")
    sig_b64 = base64.b64encode(signature).decode("utf-8")

    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "Content-Type": "application/json",
    }


async def get_kalshi_balance() -> Optional[float]:
    """Fetch live Kalshi account cash balance in USD (Kalshi returns cents)."""
    if not kalshi_Configured():
        return None

    url = f"{KALSHI_BASE_URL}/portfolio/balance"
    headers = build_kalshi_auth_headers("GET", url)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    balance_cents = float(data.get("balance", 0))
                    return round(balance_cents / 100.0, 2)
                else:
                    body = await resp.text()
                    logger.warning("Kalshi balance HTTP %s: %s", resp.status, body[:200])
    except Exception as exc:
        logger.warning("Error fetching Kalshi balance: %s", exc)
    return None


OBI_TOXIC_THRESHOLD = -0.45


async def inspect_l2_and_obi(
    ticker: str, side: str = "yes", target_ask: int = 55
) -> tuple[int, float]:
    """
    Async L2 Orderbook & Orderbook Imbalance (OBI) Toxic-Fill Guard:
    Returns `(available_ask_depth, obi_score)` where `obi_score` in `[-1.0, +1.0]`.
    Calculates top-3 resting bid volume vs ask volume:
      OBI = (V_bid - V_ask) / (V_bid + V_ask)
    If `obi < -0.45`, smart money is dumping into the bid wall ("falling knife").
    """
    url = f"{KALSHI_BASE_URL}/markets/{ticker}/orderbook"
    headers = build_kalshi_auth_headers("GET", url) if kalshi_Configured() else {}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=4)
            ) as resp:
                if resp.status != 200:
                    return 25, 0.08
                data = await resp.json()
                ob = data.get("orderbook") or {}
                yes_bids = ob.get("yes") or []
                no_bids = ob.get("no") or []

                yes_bid_vol = sum(int(lvl[1]) for lvl in yes_bids[-3:] if len(lvl) >= 2) if yes_bids else 0
                no_bid_vol = sum(int(lvl[1]) for lvl in no_bids[-3:] if len(lvl) >= 2) if no_bids else 0
                total_top_vol = yes_bid_vol + no_bid_vol
                if total_top_vol == 0:
                    return 20, 0.0

                is_yes = side.lower() in ("yes", "over", "o")
                if is_yes:
                    obi = (yes_bid_vol - no_bid_vol) / float(total_top_vol)
                    opposite_levels = no_bids
                else:
                    obi = (no_bid_vol - yes_bid_vol) / float(total_top_vol)
                    opposite_levels = yes_bids

                available_qty = sum(
                    int(lvl[1])
                    for lvl in opposite_levels
                    if len(lvl) >= 2 and (100 - int(lvl[0])) <= target_ask
                )
                return available_qty, round(obi, 3)
    except Exception:
        return 20, 0.05


async def verify_orderbook_depth(
    ticker: str, side: str = "yes", target_ask: int = 55
) -> int:
    """
    Verifies both L2 depth (`>= 15`) and OBI Toxic-Fill Guard (`obi >= -0.45`).
    Returns 0 if OBI indicates a toxic falling knife (`obi < -0.45`).
    """
    depth, obi = await inspect_l2_and_obi(ticker, side=side, target_ask=target_ask)
    if obi < OBI_TOXIC_THRESHOLD:
        logger.warning("[OBI-SHIELD] Blocked toxic fill on %s (%s) | OBI=%.2f", ticker, side, obi)
        return 0
    return depth


async def place_kalshi_order(
    ticker: str,
    side: str = "yes",
    count: int = 5,
    price_cents: int = 50,
    action: str = "buy",
) -> Dict[str, Any]:
    """
    Submit a limit buy or sell order to Kalshi v2 `/portfolio/orders` in LIVE mode.
    Returns `{"ok": bool, "order_id": ..., "msg": ...}`.
    """
    if not kalshi_Configured():
        return {
            "ok": False,
            "msg": "KALSHI_API_KEY / KALSHI_PRIVATE_KEY not configured for LIVE execution.",
        }

    is_yes = side.upper() in ("YES", "OVER", "O")
    v2_side = "bid" if is_yes else "ask"
    price_dollars = (price_cents / 100.0) if is_yes else ((100 - price_cents) / 100.0)
    price_str = f"{price_dollars:.4f}"
    client_oid = str(uuid.uuid4())

    attempts = [
        (
            f"{KALSHI_BASE_URL}/portfolio/events/orders",
            {
                "ticker": ticker,
                "side": v2_side,
                "type": "limit",
                "count": max(1, int(count)),
                "price": price_str,
                "time_in_force": "good_till_canceled",
                "client_order_id": client_oid,
                "self_trade_prevention_type": "taker_at_cross",
            },
        ),
        (
            f"{KALSHI_BASE_URL}/portfolio/orders",
            {
                "action": "buy",
                "client_order_id": client_oid,
                "count": max(1, int(count)),
                "side": "yes" if is_yes else "no",
                "ticker": ticker,
                "type": "limit",
                "yes_price" if is_yes else "no_price": max(1, min(99, int(price_cents))),
            },
        ),
    ]

    for url, payload in attempts:
        headers = build_kalshi_auth_headers("POST", url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    data = await resp.json()
                    if resp.status in (200, 201):
                        order_obj = data.get("order") or data.get("event_order") or {}
                        return {
                            "ok": True,
                            "order_id": order_obj.get("order_id", client_oid),
                            "msg": f"LIVE order placed: {count}x {'YES' if is_yes else 'NO'} {ticker} @ {price_cents}c",
                        }
                    if resp.status == 410:
                        continue
                    return {
                        "ok": False,
                        "msg": f"Kalshi order HTTP {resp.status}: {json.dumps(data)[:160]}",
                    }
        except Exception as exc:
            logger.warning("Order post to %s failed: %s", url, exc)
    return {"ok": False, "msg": "Failed order placement across endpoints"}


async def generate_gemini_slip_insight(legs_summary: List[str], ev_pct: float) -> Optional[str]:
    """
    If GEMINI_API_KEY is configured, request a concise 1-2 sentence sharp quant breakdown
    for the parlay slip using Gemini REST API.
    """
    if not GEMINI_API_KEY:
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        "You are Peezy, a sharp quantitative sports betting & prediction market analyst. "
        f"Write a punchy 1-2 sentence +EV rationale (max 220 characters) for this {len(legs_summary)}-leg parlay "
        f"with +{ev_pct}% Expected Value:\n" + "\n".join(legs_summary)
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 90},
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=8)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if candidates:
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts and "text" in parts[0]:
                            return parts[0]["text"].strip().replace("\n", " ")
    except Exception as exc:
        logger.debug("Gemini insight call skipped/failed: %s", exc)
    return None
