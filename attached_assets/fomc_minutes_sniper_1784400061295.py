#!/usr/bin/env python3
"""
fomc_minutes_sniper.py — Macro/News Surfing Event Sniper for Kalshi
Target Catalyst Event: June FOMC Meeting Minutes Release (Wednesday, July 8, 2026, 14:00 EST / 18:00 GMT)

Implements the 3-phase high-velocity news sniping flow:
1. Data Parsing: Live RSS/HTML polling & HTTP webhook listener.
2. Sentiment Mapping: Identifies hawkish/dovish directional triggers.
3. Order Routing: Immediate market-matching limit orders with $0.65 ceil on target interest rate contracts.
"""

import os
import sys
import json
import time
import re
import uuid
import urllib.request
import xml.etree.ElementTree as ET
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import argparse

# Import Kalshi Trader and Sam telemetry
from kalshi_auto_trader import KalshiDefensiveTrader
from brain import send_update_to_sam

LOG_FILE = "fomc_minutes_sniper.log"
SCHEDULED_RELEASE_UTC = datetime(2026, 7, 8, 18, 0, 0, tzinfo=timezone.utc).timestamp()

# Predetermined directional triggers
HAWKISH_KEYWORDS = [
    "further tightening required",
    "inflation risks persistent",
    "3.8% target baseline raised"
]

DOVISH_KEYWORDS = [
    "monetary policy sufficiently restrictive",
    "rate pause prolonged",
    "downside growth risks"
]

def log_event(message):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    msg_formatted = f"[{timestamp}] {message}"
    print(msg_formatted)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(msg_formatted + "\n")
    except Exception as e:
        print(f"⚠️ Failed to write to log file: {e}")

def resolve_fomc_tickers(trader):
    """
    Dynamically fetches and resolves the target hike and pause contract tickers
    for the closest upcoming FOMC meeting under series KXFEDDECISION.
    """
    log_event("Resolving active FOMC contract tickers from Kalshi...")
    
    url = f"{trader.base_url}/trade-api/v2/markets"
    params = {"status": "open", "series_ticker": "KXFEDDECISION", "limit": 100}
    headers = trader._get_auth_headers("GET", "/trade-api/v2/markets", "")
    
    req = urllib.request.Request(f"{url}?status=open&series_ticker=KXFEDDECISION&limit=100", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            res_data = json.loads(r.read().decode('utf-8'))
            markets = res_data.get("markets", [])
    except Exception as e:
        log_event(f"⚠️ Failed to query FOMC markets: {e}. Falling back to hardcoded 26JUL tickers.")
        return "26JUL", {
            "ticker": "KXFEDDECISION-26JUL-H25",
            "title": "Hike rates by 25bps in July 2026",
            "yes_ask_dollars": "0.1600",
            "no_ask_dollars": "0.8500"
        }, {
            "ticker": "KXFEDDECISION-26JUL-H0",
            "title": "Hike rates by 0bps in July 2026",
            "yes_ask_dollars": "0.8500",
            "no_ask_dollars": "0.1600"
        }

    meetings = {}
    for m in markets:
        ticker = m.get("ticker", "")
        parts = ticker.split("-")
        if len(parts) >= 3:
            meeting_id = parts[1] # e.g. "26JUL"
            strike = parts[2] # e.g. "H25", "H0"
            if meeting_id not in meetings:
                meetings[meeting_id] = {
                    "close_time": m.get("close_time", ""),
                    "markets": {}
                }
            meetings[meeting_id]["markets"][strike] = m

    # Sort meetings by date
    sorted_meetings = []
    for mid, info in meetings.items():
        try:
            close_time = datetime.fromisoformat(info["close_time"].replace('Z', ''))
            sorted_meetings.append((close_time, mid, info["markets"]))
        except Exception:
            pass
    sorted_meetings.sort(key=lambda x: x[0])

    now = datetime.utcnow()
    target_mid = None
    target_markets = None
    for close_time, mid, mkts in sorted_meetings:
        # We look for the first future meeting or target the July 2026 meeting directly
        if close_time >= now or mid == "26JUL":
            target_mid = mid
            target_markets = mkts
            break

    if not target_mid and sorted_meetings:
        target_mid = sorted_meetings[0][1]
        target_markets = sorted_meetings[0][2]

    if not target_mid:
        log_event("⚠️ No meetings resolved. Using hardcoded 26JUL fallbacks.")
        return "26JUL", {
            "ticker": "KXFEDDECISION-26JUL-H25",
            "yes_ask_dollars": "0.1600",
            "no_ask_dollars": "0.8500"
        }, {
            "ticker": "KXFEDDECISION-26JUL-H0",
            "yes_ask_dollars": "0.8500",
            "no_ask_dollars": "0.1600"
        }

    hike_m = target_markets.get("H25")
    pause_m = target_markets.get("H0")
    log_event(f"Resolved Target Meeting: {target_mid} | Hike Ticker: {hike_m.get('ticker') if hike_m else 'None'} | Pause Ticker: {pause_m.get('ticker') if pause_m else 'None'}")
    return target_mid, hike_m, pause_m

def get_price_in_cents(market, side):
    """Safely extracts the price in cents from a market object."""
    if not market:
        return 50
    key = side.lower() + "_dollars"
    val = market.get(key) or market.get(side.lower())
    if val is None:
        return 50
    try:
        return int(float(val) * 100)
    except Exception:
        return 50

def _is_verified_catalyst_local_slm(text: str) -> bool:
    """
    Local SLM routing for NLP sentiment screening.
    """
    import subprocess
    prompt = f"Analyze the following text. Does it contain a verified catalyst for markets? Answer only YES or NO.\\n\\nText: {text}"
    model_path = "/data/data/com.termux/files/home/qwen0.5b.gguf"
    if not os.path.exists(model_path):
        model_path = "/data/data/com.termux/files/home/model.gguf"
        
    try:
        result = subprocess.run([
            "/data/data/com.termux/files/usr/bin/llama-cli",
            "-m", model_path,
            "-p", prompt,
            "-n", "10",
            "--log-disable",
            "--simple-io"
        ], input="/exit\\n", capture_output=True, text=True, timeout=30)
        output = result.stdout.strip().upper()
        
        if "YES" in output:
            return True
        return False
    except Exception as e:
        log_event(f"⚠️ Local SLM routing failed: {e}")
        return False

def parse_and_snip_minutes(text, pub_timestamp=None):
    """
    Phase 2: Sentiment analysis & Keyword mapping
    Phase 3: Kalshi Order Execution with circuit breaker & telemetry
    """
    pull_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    log_event("Analyzing minutes release text...")
    
    # 1. Latency & Circuit Breaker Check
    trigger_time = time.time()
    # Fallback to scheduled release if no pub_timestamp is parsed
    effective_pub_time = pub_timestamp if pub_timestamp else SCHEDULED_RELEASE_UTC
    latency_ms = int((trigger_time - effective_pub_time) * 1000)
    
    log_event(f"Latency check: scheduled/pub epoch={effective_pub_time}, current epoch={trigger_time:.3f}, latency={latency_ms}ms")
    
    # Circuit breaker: Hard-stop if latency > 15,000ms (15s)
    if latency_ms > 15000:
        breaker_msg = f"🚨 *[CIRCUIT BREAKER TRIGGERED]* Hard-stop on order submission. Latency `{latency_ms}ms` exceeds threshold of 15,000ms."
        log_event(breaker_msg.replace("*", ""))
        send_update_to_sam(breaker_msg)
        return False, "Latency Circuit Breaker Triggered"
        
    # Run through local SLM first
    if not _is_verified_catalyst_local_slm(text):
        log_event("Local SLM rejected text as non-catalyst.")
        return False, "Not a verified catalyst according to SLM"
        
    # 2. Text Normalization and Keyword Mapping
    text_clean = " ".join(text.split()).lower()
    hawk_score = sum(1 for kw in HAWKISH_KEYWORDS if kw in text_clean)
    dove_score = sum(1 for kw in DOVISH_KEYWORDS if kw in text_clean)
    
    log_event(f"Score matching: Hawkish Keywords={hawk_score}, Dovish Keywords={dove_score}")
    
    if hawk_score == 0 and dove_score == 0:
        log_event("Neutral statement or no keywords matched. Skipping trade.")
        return False, "Neutral / No Keywords Matched"
        
    trader = KalshiDefensiveTrader()
    
    # Drawdown check safeguard
    if not trader.check_daily_drawdown():
        log_event("🛑 Drawdown limit hit. Order cancelled.")
        return False, "Drawdown limit active"

    # Strict Principal Protection Floor Check
    balance = trader.fetch_portfolio_equity()
    if balance is not None and balance <= 33.59:
        floor_msg = f"🛑 CRITICAL HALT: Account balance (${balance:.2f}) hit protection floor of $33.59. Trading halted."
        log_event(floor_msg)
        send_update_to_sam(floor_msg)
        return False, "Protection Floor Hit"

    # Resolve target contracts
    try:
        meeting_mid, hike_m, pause_m = resolve_fomc_tickers(trader)
    except Exception as e:
        log_event(f"⚠️ Error resolving tickers: {e}")
        return False, "Ticker resolution error"

    target_ticker = None
    target_side = None
    expected_price_cents = 50
    order_direction = ""
    
    if hawk_score > dove_score:
        order_direction = "HAWKISH"
        # Buy YES on rate hike contracts
        if not hike_m:
            log_event("❌ Hike market not resolved. Order cancelled.")
            return False, "Hike market not found"
        target_ticker = hike_m.get("ticker")
        target_side = "bid" # YES
        expected_price_cents = get_price_in_cents(hike_m, "yes_ask")
    else:
        order_direction = "DOVISH"
        # Buy NO on hike contracts
        if not hike_m:
            log_event("❌ Hike market not resolved. Order cancelled.")
            return False, "Hike market not found"
        target_ticker = hike_m.get("ticker")
        target_side = "ask" # NO
        expected_price_cents = get_price_in_cents(hike_m, "no_ask")

    log_event(f"Trigger direction: {order_direction} | Target Contract: {target_ticker} | Side: {target_side} | Best Ask: {expected_price_cents}¢")

    # Limit Price Ceiling Check ($0.65 / 65¢)
    if expected_price_cents > 65:
        ceil_msg = f"🛑 Skipped order routing: contract price ({expected_price_cents}¢) exceeds the limit ceiling of 65¢."
        log_event(ceil_msg)
        return False, "Price Exceeds Ceiling"

    # Position Sizing: Layout between $10 and $15 to protect the $50 bankroll
    max_layout = 15.00
    contracts = int(max_layout * 100 / expected_price_cents)
    contracts = max(1, contracts)
    # Ensure layout doesn't exceed $15.00
    if (contracts * expected_price_cents) / 100.0 > max_layout and contracts > 1:
        contracts -= 1
        
    final_layout = (contracts * expected_price_cents) / 100.0
    log_event(f"Order Sizing: placing {contracts} contracts at price {expected_price_cents}¢ (Total layout: ${final_layout:.2f})")

    # Log order submission before hitting the network (Idempotency Layer)
    client_order_id = str(uuid.uuid4())
    db_id = trader.log_order_submitted(
        20260708, client_order_id, target_ticker, target_side, expected_price_cents, contracts
    )

    order_data = {
        "ticker": target_ticker,
        "side": target_side,
        "count": str(contracts),
        # Target limit price ceil set to $0.65 to act as immediate market routing up to ceiling
        "price": "0.65",
        "time_in_force": "good_till_canceled",
        "client_order_id": client_order_id,
        "type": "limit",
        "self_trade_prevention_type": "taker_at_cross"
    }

    # 3. Network Order Routing
    tx_id = "N/A"
    slippage = 0.0
    status_str = "FAILED"
    
    if trader.is_simulated:
        tx_id = f"sim-{client_order_id[:8]}"
        status_str = "FILLED"
        log_event(f"🧪 [SIMULATED EXECUTION] Order successfully filled. Tx: {tx_id}")
    else:
        try:
            r = trader._send_request_with_backoff("POST", "/trade-api/v2/portfolio/orders", order_data)
            if r and not r.get("simulation"):
                tx_id = r.get("order", {}).get("order_id") or r.get("order_id") or "TX-ERROR"
                status_str = "FILLED" # Assume immediate fill for market routing
                
                # Fetch filled order details to compute exact slippage
                time.sleep(0.5)
                details = trader._send_request_with_backoff("GET", f"/trade-api/v2/portfolio/orders/{tx_id}")
                if details:
                    ord_obj = details.get("order", {})
                    avg_fill = ord_obj.get("average_fill_price")
                    if avg_fill:
                        avg_fill_cents = int(float(avg_fill) * 100)
                        slippage = (avg_fill_cents - expected_price_cents) / 100.0
                        log_event(f"Order filled. Avg Fill Price: {avg_fill_cents}¢ | Expected: {expected_price_cents}¢ | Slippage: ${slippage:+.2f}")
            else:
                log_event(f"❌ Order submission returned simulated or invalid response.")
        except Exception as e:
            log_event(f"❌ Order submission execution error: {e}")

    # Log to SQLite DB
    if status_str == "FILLED":
        trader.update_db_order_status_by_id(db_id, "FILLED", tx_id)
        trader.log_legacy_trade_finish(
            20260708, target_ticker, target_side, expected_price_cents, contracts, client_order_id, tx_id, "FILLED"
        )
    else:
        trader.update_db_order_status_by_id(db_id, "FAILED", None)
        trader.log_legacy_trade_finish(
            20260708, target_ticker, target_side, expected_price_cents, contracts, client_order_id, None, "FAILED"
        )

    # 4. Stream Telemetry to "Sentinel Sam" (ClickUp Telegram link)
    telemetry_msg = (
        f"📶 **SENTINEL SAM TELEMETRY**\n"
        f"• Action: FOMC Minutes Sniped\n"
        f"• Exact Pull Time: `{pull_time}`\n"
        f"• Trigger Type: `{order_direction}` (Hawk: `{hawk_score}`, Dove: `{dove_score}`)\n"
        f"• Order Ticker: `{target_ticker}`\n"
        f"• Contracts Placed: `{contracts}`\n"
        f"• Price Ceil Set: `$0.65`\n"
        f"• Expected Price: `${expected_price_cents/100.0:.2f}`\n"
        f"• Order Tx ID: `{tx_id}`\n"
        f"• Slippage: `+${slippage:.2f}`\n"
        f"• Latency: `{latency_ms}ms`\n"
        f"• Status: `{status_str}`"
    )
    send_update_to_sam(telemetry_msg)
    log_event(f"Telemetry sent to ClickUp thread:\n{telemetry_msg}")
    
    return True, status_str

# --- Live Data Scraping & Polling Loop ---
class FedMinutesPoller:
    def __init__(self, interval_seconds=1.0):
        self.interval = interval_seconds
        self.is_running = False
        self.seen_guids = set()

    def fetch_rss_feed(self, url):
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) MacroNewsSniper/1.0"}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.read()
        except Exception as e:
            # Silent fallback / network robustness
            return None

    def poll_once(self):
        # We target both press_monetary.xml and press_all.xml
        urls = [
            "https://www.federalreserve.gov/feeds/press_monetary.xml",
            "https://www.federalreserve.gov/feeds/press_all.xml"
        ]
        
        for url in urls:
            data = self.fetch_rss_feed(url)
            if not data:
                continue
                
            try:
                root = ET.fromstring(data)
                for item in root.findall(".//item"):
                    guid = item.find("guid").text if item.find("guid") is not None else ""
                    if not guid or guid in self.seen_guids:
                        continue
                        
                    title = item.find("title").text if item.find("title") is not None else ""
                    description = item.find("description").text if item.find("description") is not None else ""
                    pub_date_str = item.find("pubDate").text if item.find("pubDate") is not None else ""
                    
                    # Target titles representing June minutes
                    if "minutes" in title.lower() and ("june" in title.lower() or "june" in description.lower()):
                        log_event(f"🔔 [NEW RELEASE DETECTED] Title: {title}")
                        
                        # Parse publication date to compute precise latency
                        pub_ts = None
                        if pub_date_str:
                            try:
                                # Standard RSS format: "Wed, 08 Jul 2026 18:00:02 GMT"
                                dt = datetime.strptime(pub_date_str, "%a, %d %b %Y %H:%M:%S %Z")
                                pub_ts = dt.replace(tzinfo=timezone.utc).timestamp()
                            except Exception:
                                pass
                        
                        # Parse and Snip!
                        content = f"{title} {description}"
                        success, status = parse_and_snip_minutes(content, pub_ts)
                        
                        self.seen_guids.add(guid)
                        if success:
                            # Hard-stop polling loop after execution
                            self.stop()
                            return
            except Exception as e:
                log_event(f"⚠️ RSS parsing error for {url}: {e}")

    def run(self):
        log_event("Starting high-frequency RSS polling loop (1.0s interval)...")
        self.is_running = True
        while self.is_running:
            # Check scheduled time. If we are more than 15s past scheduled release and haven't triggered,
            # we check if we should stop. But we keep polling in case release is delayed.
            self.poll_once()
            time.sleep(self.interval)

    def stop(self):
        self.is_running = False
        log_event("RSS Polling loop stopped.")

# --- Webhook Listener Server ---
class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/webhook/minutes":
            content_length = int(self.headers["Content-Length"])
            post_data = self.rfile.read(content_length)
            
            try:
                payload = json.loads(post_data.decode("utf-8"))
                text = payload.get("text", "")
                pub_timestamp = payload.get("timestamp") # epoch timestamp
                
                log_event(f"🔔 Webhook received trigger with text length: {len(text)}")
                
                # Execute Snipe
                success, status = parse_and_snip_minutes(text, pub_timestamp)
                
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"success": success, "status": status}).encode("utf-8"))
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(f"Bad Request: {e}".encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def start_webhook_server(port=8888):
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    log_event(f"🚀 Webhook listener server active on port {port} at /webhook/minutes")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        log_event("Webhook listener server stopped.")

# --- Command Line Runner ---
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="June FOMC Minutes prediction market sniper bot.")
    parser.add_argument("--poll", action="store_true", help="Start the high-frequency RSS polling loop")
    parser.add_argument("--webhook", action="store_true", help="Start the HTTP webhook listener server")
    parser.add_argument("--test-hawk", action="store_true", help="Run a simulated HAWKISH minutes release test")
    parser.add_argument("--test-dove", action="store_true", help="Run a simulated DOVISH minutes release test")
    parser.add_argument("--port", type=int, default=8888, help="Port to run webhook listener on")
    
    args = parser.parse_args()
    
    # Ensure log file exists and write header
    log_event("Initializing Macro/News Event Sniper...")
    
    if args.test_hawk:
        log_event("🧪 Running HAWKISH minutes release test...")
        mock_text = (
            "The committee observed that inflation risks are persistent and core prints remain sticky. "
            "Consequently, several members noted that further tightening required to bring CPI back to "
            "target. The terminal 3.8% target baseline raised to project higher rates for longer."
        )
        # Set pub_timestamp to now to avoid latency circuit breaker trip
        parse_and_snip_minutes(mock_text, pub_timestamp=time.time())
        
    elif args.test_dove:
        log_event("🧪 Running DOVISH minutes release test...")
        mock_text = (
            "Given recent moderation in spending, the staff concluded that monetary policy sufficiently restrictive. "
            "A rate pause prolonged is warranted to prevent downside growth risks and ensure employment stability."
        )
        parse_and_snip_minutes(mock_text, pub_timestamp=time.time())
        
    elif args.webhook:
        start_webhook_server(args.port)
        
    elif args.poll:
        poller = FedMinutesPoller(interval_seconds=1.0)
        
        # Start webhook server in background thread so both are active simultaneously
        server_thread = threading.Thread(target=start_webhook_server, args=(args.port,), daemon=True)
        server_thread.start()
        
        try:
            poller.run()
        except KeyboardInterrupt:
            poller.stop()
            
    else:
        parser.print_help()
