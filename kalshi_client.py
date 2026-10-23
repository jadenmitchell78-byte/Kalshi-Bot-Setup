import base64
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


def _load_private_key():
    """
    Load the private key (supports both Ed25519 and RSA PEM keys) for Kalshi v2 signing.
    Checks inline KALSHI_PRIVATE_KEY first, then KALSHI_PRIVATE_KEY_PATH, then BASE_DIR/kalshi_private.pem.
    """
    pem_text = KALSHI_PRIVATE_KEY.strip()
    if not pem_text:
        candidates = []
        if KALSHI_PRIVATE_KEY_PATH:
            candidates.append(Path(KALSHI_PRIVATE_KEY_PATH).expanduser())
        candidates.append(BASE_DIR / "kalshi_private.pem")
        for p in candidates:
            if p.exists():
                pem_text = p.read_text(encoding="utf-8").strip()
                break

    if not pem_text:
        return None

    if "\\n" in pem_text:
        pem_text = pem_text.replace("\\n", "\n")

    try:
        from cryptography.hazmat.primitives import serialization

        return serialization.load_pem_private_key(
            pem_text.encode("utf-8"), password=None
        )
    except Exception as exc:
        logger.warning("Failed to load KALSHI_PRIVATE_KEY: %s", exc)
        return None


def kalshi_Configured() -> bool:
    """Return True if both KALSHI_API_KEY and a valid private key are configured."""
    return bool(KALSHI_API_KEY and _load_private_key() is not None)


def build_kalshi_auth_headers(method: str, url: str) -> Dict[str, str]:
    """
    Generate signed headers required by Kalshi v2 authenticated endpoints:
    KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE.
    Supports both Ed25519 private keys and RSA-PSS private keys.
    """
    priv_key = _load_private_key()
    if not KALSHI_API_KEY or priv_key is None:
        return {}

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ed25519, padding

    timestamp_ms = str(int(time.time() * 1000))
    parsed_path = urlparse(url).path
    msg_bytes = f"{timestamp_ms}{method.upper()}{parsed_path}".encode("utf-8")

    if isinstance(priv_key, ed25519.Ed25519PrivateKey):
        signature = priv_key.sign(msg_bytes)
    else:
        signature = priv_key.sign(
            msg_bytes,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

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

    url = f"{KALSHI_BASE_URL}/portfolio/orders"
    headers = build_kalshi_auth_headers("POST", url)
    norm_side = "no" if side.upper() in ("NO", "UNDER") else "yes"
    norm_action = "sell" if action.lower() == "sell" else "buy"
    price_key = "yes_price" if norm_side == "yes" else "no_price"

    payload = {
        "action": norm_action,
        "client_order_id": str(uuid.uuid4()),
        "count": max(1, int(count)),
        "side": norm_side,
        "ticker": ticker,
        "type": "limit",
        price_key: max(1, min(99, int(price_cents))),
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if resp.status in (200, 201):
                    order_obj = data.get("order", {})
                    return {
                        "ok": True,
                        "order_id": order_obj.get("order_id", payload["client_order_id"]),
                        "msg": f"LIVE order placed: {count}x {norm_side.upper()} {ticker} @ {price_cents}c",
                    }
                return {
                    "ok": False,
                    "msg": f"Kalshi order HTTP {resp.status}: {json.dumps(data)[:160]}",
                }
    except Exception as exc:
        return {"ok": False, "msg": f"Kalshi order exception: {exc}"}


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
