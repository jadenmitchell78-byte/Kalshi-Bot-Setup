# ~/peezy_bot/smart_placer.py
"""
Smart Placing Engine for Kalshi V2:
1. Full Cursor Pagination + Series Sweep (Resolves deep player prop books)
2. Fuzzy Player Prop Ticker Resolver (Maps "J Randle PTS 21.5" -> Real Kalshi Ticker)
3. Smart Orderbook Imbalance (OBI) & Maker-Limit Pricing Engine
4. Fractional Kelly Sizing & Live Kalshi Order Execution (Ed25519 & RSA Dual Auth)
5. Smart Combo Basket Executor (Places Correlated Copula Legs Together)
"""

import os
import time
import uuid
import json
import base64
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    HAS_CRYPTOGRAPHY = True
except Exception:
    HAS_CRYPTOGRAPHY = False

# Sport + Stat -> Kalshi Series Prefix Mapping
KALSHI_SERIES_PREFIXES: Dict[Tuple[str, str], List[str]] = {
    ("NBA", "PTS"): ["KXNBAPTS", "NBA"],
    ("NBA", "POINTS"): ["KXNBAPTS", "NBA"],
    ("NBA", "REB"): ["KXNBAREB", "NBA"],
    ("NBA", "REBOUNDS"): ["KXNBAREB", "NBA"],
    ("NBA", "AST"): ["KXNBAAST", "NBA"],
    ("NBA", "ASSISTS"): ["KXNBAAST", "NBA"],
    ("NBA", "3PT"): ["KXNBA3PT", "KXNBA3PM", "NBA"],
    ("NBA", "3PM"): ["KXNBA3PT", "KXNBA3PM", "NBA"],
    ("NBA", "3PT MADE"): ["KXNBA3PT", "KXNBA3PM", "NBA"],
    ("NBA", "PRA"): ["KXNBAPRA", "NBA"],
    ("NBA", "BLK"): ["KXNBABLK", "NBA"],
    ("NBA", "BLOCKS"): ["KXNBABLK", "NBA"],
    ("NBA", "STL"): ["KXNBASTL", "NBA"],
    ("NBA", "STEALS"): ["KXNBASTL", "NBA"],
    ("NBA", "POINTS + REBOUNDS"): ["KXNBAPR", "KXNBAPRA", "NBA"],
    ("NBA", "POINTS + ASSISTS"): ["KXNBAPA", "KXNBAPRA", "NBA"],
    ("NBA", "REBOUNDS + ASSISTS"): ["KXNBARA", "KXNBAPRA", "NBA"],
    ("WNBA", "PTS"): ["KXWNBAPTS", "WNBA"],
    ("WNBA", "AST"): ["KXWNBAAST", "WNBA"],
    ("WNBA", "REB"): ["KXWNBAREB", "WNBA"],
    ("WNBA", "3PT"): ["KXWNBA3PT", "WNBA"],
    ("NFL", "PASS_YDS"): ["KXNFLPASSYDS", "KXNFLPASS", "NFL"],
    ("NFL", "PASS YDS"): ["KXNFLPASSYDS", "KXNFLPASS", "NFL"],
    ("NFL", "RUSH_YDS"): ["KXNFLRUSHYDS", "KXNFLRUSH", "NFL"],
    ("NFL", "RUSH YDS"): ["KXNFLRUSHYDS", "KXNFLRUSH", "NFL"],
    ("NFL", "REC_YDS"): ["KXNFLRECYDS", "KXNFLREC", "NFL"],
    ("NFL", "REC YDS"): ["KXNFLRECYDS", "KXNFLREC", "NFL"],
    ("NFL", "TD"): ["KXNFLTD", "NFL"],
    ("NFL", "TOUCHDOWNS"): ["KXNFLTD", "NFL"],
    ("MLB", "K"): ["KXMLBKS", "KXMLBK", "MLB"],
    ("MLB", "STRIKEOUTS"): ["KXMLBKS", "KXMLBK", "MLB"],
    ("MLB", "HITS"): ["KXMLBHITS", "MLB"],
    ("MLB", "HR"): ["KXMLBHR", "MLB"],
    ("MLB", "HOME RUNS"): ["KXMLBHR", "MLB"],
    ("TENNIS", "SETS"): ["KXTENNIS", "KXATPMATCH", "KXWTAMATCH", "TENNIS"],
    ("TENNIS", "GAMES"): ["KXTENNIS", "KXATPMATCH", "KXWTAMATCH", "TENNIS"],
    ("ESPORTS", "KILLS"): ["KXESPORTS", "ESPORTS"],
    ("ESPORTS", "MAP KILLS"): ["KXESPORTS", "ESPORTS"],
    ("NHL", "SOG"): ["KXNHLSOG", "NHL"],
    ("NHL", "GOALS"): ["KXNHLGOALS", "NHL"],
    ("NHL", "SAVES"): ["KXNHLSAVES", "NHL"],
}


class KalshiSmartPlacer:
    def __init__(
        self,
        key_id: Optional[str] = None,
        private_key_pem: Optional[str] = None,
        base_url: str = "https://api.elections.kalshi.com",
        auth_header_fn: Optional[Any] = None,
    ):
        self.key_id = key_id or os.getenv("KALSHI_KEY_ID") or os.getenv("KALSHI_API_KEY") or ""
        self.base_url = base_url.rstrip("/")
        self.auth_header_fn = auth_header_fn
        self.private_key = None
        self.market_cache: List[Dict[str, Any]] = []
        self.last_cache_time: float = 0.0

        if private_key_pem:
            self._load_key(private_key_pem)
        elif not self.auth_header_fn:
            # Attempt loading from default key locations
            for p in (
                Path("/data/data/com.termux/files/home/peezy_bot/kalshi_private.pem"),
                Path.home() / "peezy_bot" / "kalshi_private.pem",
                Path(__file__).resolve().parent / "kalshi_private.pem",
            ):
                if p.exists():
                    try:
                        self._load_key(p.read_text(encoding="utf-8"))
                        break
                    except Exception:
                        pass

    def _load_key(self, pem_text: str):
        if not HAS_CRYPTOGRAPHY or not pem_text:
            return
        clean_pem = pem_text.strip()
        if not clean_pem.startswith("-----BEGIN"):
            clean_b64 = "".join(ln.strip() for ln in clean_pem.splitlines() if ln.strip() and not ln.strip().startswith("-----"))
            clean_pem = f"-----BEGIN PRIVATE KEY-----\n{clean_b64}\n-----END PRIVATE KEY-----"
        try:
            self.private_key = serialization.load_pem_private_key(clean_pem.encode("utf-8"), password=None)
        except Exception as e:
            print(f"[SMART-PLACER-WARN] Could not load private key via cryptography: {e}")

    def sign_headers(self, method: str, path: str) -> Dict[str, str]:
        if self.auth_header_fn:
            try:
                return self.auth_header_fn(method, path)
            except Exception:
                pass

        ts = str(int(time.time() * 1000))
        clean_path = path.split("?")[0]
        msg = f"{ts}{method.upper()}{clean_path}".encode("utf-8")

        if self.private_key is not None and HAS_CRYPTOGRAPHY:
            try:
                if isinstance(self.private_key, Ed25519PrivateKey):
                    sig = self.private_key.sign(msg)
                else:
                    sig = self.private_key.sign(
                        msg,
                        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
                        hashes.SHA256(),
                    )
                return {
                    "KALSHI-ACCESS-KEY": self.key_id,
                    "KALSHI-ACCESS-TIMESTAMP": ts,
                    "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
            except Exception:
                pass

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # -----------------------------------------------------------------
    # 1. FULL CURSOR PAGINATION + SERIES SWEEP (Fixes "No Active Markets")
    # -----------------------------------------------------------------
    def refresh_active_sports_markets(self, sport: str, stat: str, force: bool = False) -> List[Dict[str, Any]]:
        if not force and self.market_cache and (time.time() - self.last_cache_time < 45):
            return self.market_cache

        sp_u = sport.upper()
        st_u = stat.upper()
        prefixes = KALSHI_SERIES_PREFIXES.get((sp_u, st_u)) or KALSHI_SERIES_PREFIXES.get((sp_u, st_u.split()[0])) or [f"KX{sp_u}"]
        collected: List[Dict[str, Any]] = []
        seen_tickers = set()

        for prefix in prefixes:
            cursor = None
            for _ in range(5):  # Up to 1,000 contracts per series
                path = f"/trade-api/v2/markets?status=open&limit=200&series_ticker={prefix}"
                if cursor:
                    path += f"&cursor={cursor}"
                try:
                    resp = requests.get(self.base_url + path, headers=self.sign_headers("GET", path), timeout=5)
                    if resp.status_code == 200:
                        data = resp.json()
                        batch = data.get("markets", [])
                        for m in batch:
                            tk = m.get("ticker", "")
                            if tk and tk not in seen_tickers:
                                seen_tickers.add(tk)
                                collected.append(m)
                        cursor = data.get("cursor")
                        if not cursor or not batch:
                            break
                    else:
                        break
                except Exception:
                    break

        # Fallback: sweep open sports markets with cursor pagination if series_ticker wasn't exact
        if len(collected) < 4:
            cursor = None
            for _ in range(6):
                path = "/trade-api/v2/markets?status=open&limit=200"
                if cursor:
                    path += f"&cursor={cursor}"
                try:
                    resp = requests.get(self.base_url + path, headers=self.sign_headers("GET", path), timeout=5)
                    if resp.status_code != 200:
                        break
                    data = resp.json()
                    batch = data.get("markets", [])
                    for m in batch:
                        tk = m.get("ticker", "")
                        t_str = f"{tk} {m.get('title', '')} {m.get('subtitle', '')} {m.get('category', '')}".upper()
                        if sp_u in t_str or f"KX{sp_u}" in t_str:
                            if tk and tk not in seen_tickers:
                                seen_tickers.add(tk)
                                collected.append(m)
                    cursor = data.get("cursor")
                    if not cursor:
                        break
                except Exception:
                    break

        if collected:
            self.market_cache = collected
            self.last_cache_time = time.time()
        return self.market_cache or collected

    # -----------------------------------------------------------------
    # 2. FUZZY PLAYER PROP TICKER RESOLVER (Maps "J Randle PTS 21.5" -> Real Ticker)
    # -----------------------------------------------------------------
    def resolve_prop_ticker(self, sport: str, player_name: str, stat: str, target_line: float) -> Optional[Dict[str, Any]]:
        markets = self.refresh_active_sports_markets(sport, stat)
        if not markets:
            return None

        # Clean tokens from player name (e.g. "Julius Randle" -> "RANDLE", "JULIUS")
        clean_name = player_name.upper().replace("-", " ").replace(".", " ")
        tokens = [t for t in clean_name.split() if len(t) >= 3 and t not in ("OVER", "UNDER", "PTS", "REB", "AST", "NBA")]
        last_name = tokens[-1] if tokens else player_name.strip().split()[-1].upper()

        best_match = None
        smallest_line_diff = 999.0
        lookup_names = [last_name]
        if last_name in ("HERO", "HERRO"):
            lookup_names = ["HERRO", "HERO"]
        elif last_name in ("ANT", "EDWARDS"):
            lookup_names = ["EDWARDS", "ANT"]
        elif last_name in ("LBJ", "LEBRON", "JAMES"):
            lookup_names = ["LEBRON", "LBJ", "JAMES"]
        elif last_name in ("AD", "DAVIS"):
            lookup_names = ["DAVIS", " AD "]
        elif last_name in ("WEMBY", "WEMBANYAMA"):
            lookup_names = ["WEMBANYAMA", "WEMBY"]

        for m in markets:
            searchable = f"{m.get('ticker', '')} {m.get('title', '')} {m.get('subtitle', '')}".upper()
            if not any(n in searchable for n in lookup_names):
                continue

            # Extract threshold number if present in floor_strike, strike_price, cap_strike, or ticker
            strike = m.get("floor_strike") or m.get("strike_price") or m.get("cap_strike")
            if strike is not None:
                try:
                    diff = abs(float(strike) - float(target_line))
                    if diff < smallest_line_diff:
                        smallest_line_diff = diff
                        best_match = m
                        if diff <= 0.5:
                            return best_match
                except Exception:
                    pass
            elif not best_match:
                best_match = m

        return best_match

    # -----------------------------------------------------------------
    # 3. SMART ORDERBOOK IMBALANCE (OBI) & LIMIT PRICING ENGINE
    # -----------------------------------------------------------------
    def calculate_smart_entry_price(self, ticker: str, side: str, true_prob_pct: float) -> Tuple[int, str, str]:
        """
        Inspects live Kalshi orderbook depth and Orderbook Imbalance (OBI).
        Returns: (limit_price_cents, execution_style, pricing_note)
        """
        path = f"/trade-api/v2/markets/{ticker}/orderbook"
        fair_cents = max(2, min(98, int(round(true_prob_pct))))

        ob: Dict[str, Any] = {}
        try:
            resp = requests.get(self.base_url + path, headers=self.sign_headers("GET", path), timeout=4)
            if resp.status_code == 200:
                ob = resp.json().get("orderbook", {})
        except Exception:
            pass

        yes_bids = ob.get("yes", []) or []  # [[price, qty], ...]
        no_bids = ob.get("no", []) or []

        is_yes = side.lower() in ("yes", "over", "o")
        if is_yes:
            best_bid = yes_bids[-1][0] if yes_bids else max(1, fair_cents - 8)
            best_ask = (100 - no_bids[-1][0]) if no_bids else min(99, fair_cents - 3)
            bid_vol = sum(q for _, q in yes_bids[-3:]) if yes_bids else 10
            ask_vol = sum(q for _, q in no_bids[-3:]) if no_bids else 10
        else:
            best_bid = no_bids[-1][0] if no_bids else max(1, fair_cents - 8)
            best_ask = (100 - yes_bids[-1][0]) if yes_bids else min(99, fair_cents - 3)
            bid_vol = sum(q for _, q in no_bids[-3:]) if no_bids else 10
            ask_vol = sum(q for _, q in yes_bids[-3:]) if yes_bids else 10

        best_bid = max(1, min(98, best_bid))
        best_ask = max(best_bid + 1, min(99, best_ask))
        spread = best_ask - best_bid
        obi = (bid_vol - ask_vol) / max(1, (bid_vol + ask_vol))  # -1.0 to +1.0

        # Never pay more than (Fair Value - 2c minimum edge buffer)
        max_acceptable_price = max(1, fair_cents - 2)

        # Case A: Tight spread (<= 3c) or strong upward buy pressure (OBI > 0.35) -> Snipe Ask immediately
        if (spread <= 3 or obi > 0.35) and best_ask <= max_acceptable_price:
            return best_ask, "SNIPE_TAKER", f"Tight {spread}c spread (OBI {obi:+.2f}) -> Crossing Ask at {best_ask}c"

        # Case B: Wide spread (> 3c) -> Post Smart Maker Limit 1c above Best Bid to capture spread + 0 maker fee
        smart_maker_price = min(max_acceptable_price, best_bid + 1)
        return smart_maker_price, "SMART_MAKER_LIMIT", f"Wide {spread}c spread (OBI {obi:+.2f}) -> Pegging Maker Limit at {smart_maker_price}c (Fair: {fair_cents}c)"

    # -----------------------------------------------------------------
    # 4. FRACTIONAL KELLY SIZING & LIVE KALSHI ORDER EXECUTION
    # -----------------------------------------------------------------
    def smart_place_leg(
        self,
        sport: str,
        player_name: str,
        stat: str,
        line: float,
        pick_dir: str,       # "OVER" or "UNDER"
        true_prob_pct: float,
        ev_edge_pct: float,
        bankroll_usd: float,
        sniper_on: bool = True,
        live_mode: bool = True,
        kelly_fraction: float = 0.25,
        min_ev_gate: float = 2.5,
        existing_ticker: Optional[str] = None,
    ) -> Dict[str, Any]:
        if ev_edge_pct < min_ev_gate:
            return {"status": "SKIPPED", "reason": f"EV {ev_edge_pct:.2f}% below {min_ev_gate}% Smart Gate"}

        ticker = existing_ticker
        market = None
        if not ticker or ticker.startswith("MANUAL-") or "-" not in ticker or len(ticker) < 8:
            market = self.resolve_prop_ticker(sport, player_name, stat, line)
            ticker = market.get("ticker") if market else ticker

        if not ticker or ticker.startswith("MANUAL-"):
            return {
                "status": "NO_TICKER",
                "reason": f"Could not resolve active Kalshi contract for {player_name} {stat} {line}",
                "player": player_name,
                "stat": stat,
                "line": line,
            }

        kalshi_side = "yes" if pick_dir.upper() in ("OVER", "YES", "O") else "no"
        limit_cents, exec_style, pricing_note = self.calculate_smart_entry_price(ticker, kalshi_side, true_prob_pct)

        # Fractional Kelly Contract Sizing based on actual entry price
        p = max(0.05, min(0.95, true_prob_pct / 100.0))
        q = 1.0 - p
        b = (100.0 - limit_cents) / float(max(1, limit_cents))  # Net profit odds per cent risked
        raw_kelly = max(0.0, (b * p - q) / b) if b > 0 else 0.0
        eff_bankroll = bankroll_usd if bankroll_usd > 0 else 12.52
        stake_usd = max(1.00, min(eff_bankroll * 0.40, round(eff_bankroll * max(0.08, raw_kelly) * kelly_fraction, 2)))
        contracts = max(1, min(10, int((stake_usd * 100) // max(1, limit_cents))))
        cost_usd = round((contracts * limit_cents) / 100.0, 2)

        if not (sniper_on and live_mode):
            return {
                "status": "SIM_QUEUED",
                "ticker": ticker,
                "side": kalshi_side.upper(),
                "contracts": contracts,
                "limit_cents": limit_cents,
                "cost_usd": cost_usd,
                "style": exec_style,
                "note": f"Simulated paper execution ({pricing_note})",
            }

        # Execute Real Kalshi V2 Limit Order
        client_oid = str(uuid.uuid4())
        order_path = "/trade-api/v2/portfolio/orders"
        payload = {
            "ticker": ticker,
            "action": "buy",
            "side": kalshi_side,
            "count": contracts,
            "type": "limit",
            "client_order_id": client_oid,
            f"{kalshi_side}_price": limit_cents,
        }

        try:
            resp = requests.post(
                self.base_url + order_path,
                json=payload,
                headers=self.sign_headers("POST", order_path),
                timeout=8,
            )
            if resp.status_code in (200, 201):
                order_data = resp.json().get("order") or resp.json().get("event_order") or {}
                real_oid = order_data.get("order_id") or client_oid
                return {
                    "status": "PLACED",
                    "order_id": real_oid,
                    "ticker": ticker,
                    "side": kalshi_side.upper(),
                    "contracts": contracts,
                    "limit_cents": limit_cents,
                    "cost_usd": cost_usd,
                    "style": exec_style,
                    "note": pricing_note,
                }
            err_msg = resp.text[:160]
            if "insufficient_balance" in err_msg.lower():
                return {"status": "BALANCE_HELD", "code": resp.status_code, "reason": "Resting orders holding cash", "limit_cents": limit_cents, "contracts": contracts, "ticker": ticker}
            return {"status": "API_REJECTED", "code": resp.status_code, "reason": err_msg, "limit_cents": limit_cents, "contracts": contracts, "ticker": ticker}
        except Exception as e:
            return {"status": "ERROR", "reason": str(e), "limit_cents": limit_cents, "contracts": contracts, "ticker": ticker}

    # -----------------------------------------------------------------
    # 5. SMART COMBO BASKET EXECUTOR (Places Correlated Copula Legs Together)
    # -----------------------------------------------------------------
    def smart_place_copula_combo(
        self,
        legs: List[Dict[str, Any]],
        copula_boost_pct: float,
        bankroll_usd: float,
        sniper_on: bool = True,
        live_mode: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        Places a correlated 2-to-3 leg Copula Combo as a synchronized Smart Basket,
        scaling Kelly fraction up proportionally to the positive Copula covariance boost.
        """
        # Boost Kelly fraction slightly when legs have strong positive Gaussian Copula synergy
        synergy_kelly = min(0.35, 0.20 + (max(0.0, copula_boost_pct) / 100.0))
        per_leg_bankroll = bankroll_usd / max(1, len(legs))
        receipts = []
        for leg in legs:
            res = self.smart_place_leg(
                sport=leg.get("sport", "NBA"),
                player_name=leg.get("player_name") or leg.get("player", ""),
                stat=leg.get("stat_type") or leg.get("stat", "Points"),
                line=float(leg.get("line", 21.5)),
                pick_dir=leg.get("side", "OVER"),
                true_prob_pct=float(leg.get("mc_true_prob", leg.get("true_prob", 0.55)) * 100.0 if float(leg.get("mc_true_prob", 0.55)) <= 1.0 else float(leg.get("mc_true_prob", 55.0))),
                ev_edge_pct=float(leg.get("ev_edge", leg.get("ev_pct", 3.5)) * 100.0 if float(leg.get("ev_edge", 0.035)) <= 1.0 else float(leg.get("ev_edge", 3.5))),
                bankroll_usd=per_leg_bankroll,
                sniper_on=sniper_on,
                live_mode=live_mode,
                kelly_fraction=synergy_kelly,
                min_ev_gate=2.0,
                existing_ticker=leg.get("market_ticker") or leg.get("ticker"),
            )
            receipts.append(res)
        return receipts
