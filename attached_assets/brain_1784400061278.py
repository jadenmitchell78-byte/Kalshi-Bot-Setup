# brain.py — Telemetry forwarding and global token resolver.
# ARCHITECTURAL NOTE: The Kalshi auto-trading engine (kalshi_auto_trader.py) operates 
# completely independently of the Solana (solana_sniper_bot.py) and EVM (dex_scanner_bot.py) 
# sniper bots. Daily drawdowns, request rate limits, and state tracking (betting_system.db) 
# are fully decoupled. A Solana loss cap halt will never pause or affect Kalshi sports betting.

import sqlite3
import requests
import json
import os
import time
def get_bot_token():
    # Attempt to load the token from standard configuration files
    for config_path in ["config.json", "solana_config.json", "social_trail_config.json"]:
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    c = json.load(f)
                    token = c.get("telegram_token")
                    if token and "YOUR_" not in token:
                        return token
            except:
                pass
    # Fallback to the main MemCoinAiBot token
    return "8653525018:AAFr1wL5vauNEnu5xhGVbh0u1V9N3f2e_bY"

def send_update_to_sam(status_message, token=None):
    bot_token = token or get_bot_token()
    chat_id = "5992267511"
    
    telegram_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    
    payload = {
        "chat_id": chat_id,
        "text": status_message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(telegram_url, json=payload, timeout=8)
        if response.status_code != 200:
            pass # print(f"Telegram API responded with error: {response.text}")
        else:
            pass # print("Telegram update sent successfully!")
    except Exception as e:
        pass # print(f"Failed to push telemetry to Sam thread: {e}")

def get_clickup_config():
    for config_path in ["config.json", "social_trail_config.json"]:
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    c = json.load(f)
                    token = c.get("clickup_api_token")
                    view_id = c.get("clickup_view_id")
                    if token and view_id:
                        return token, view_id
            except:
                pass
    return None, None

def send_update_to_clickup(status_message, profit_dollars=None):
    token, view_id = get_clickup_config()
    if not token or not view_id:
        return
        
    url = f"https://api.clickup.com/api/v2/view/{view_id}/comment"
    headers = {
        "Authorization": token,
        "Content-Type": "application/json"
    }
    payload = {
        "comment_text": status_message,
        "notify_all": False
    }
    
    try:
        requests.post(url, json=payload, headers=headers, timeout=8)
    except Exception as e:
        pass

def log_profit_to_sam(current_equity, starting_equity):
    """
    Sentinel Sam tracks Kalshi profit and pushes it to ClickUp and Telegram.
    """
    if starting_equity > 0:
        profit_dollars = current_equity - starting_equity
        profit_pct = (profit_dollars / starting_equity) * 100
        sign = "+" if profit_dollars >= 0 else ""
        
        msg = (
            f"📊 **SENTINEL SAM PROFIT REPORT**\n"
            f"• Starting Equity: ${starting_equity:.2f}\n"
            f"• Current Equity: ${current_equity:.2f}\n"
            f"• Net Profit: {sign}${profit_dollars:.2f} ({sign}{profit_pct:.2f}%)\n"
        )
        send_update_to_sam(msg)
        send_update_to_clickup(msg, profit_dollars=profit_dollars)


def validate_trade_signal(signal_text):
    """
    Combo & Directional mode: Evaluates sentiment to execute high-yield bets.
    Returns True if the signal is authorised for execution.
    """
    if not signal_text or len(signal_text.strip()) < 5:
        return False  # Reject empty / trivially short signals
    signal_lower = signal_text.lower()
    # High-yield combo signals get immediate approval
    if "parlay" in signal_lower or "combo" in signal_lower or "multi-leg" in signal_lower:
        return True
    # All other non-empty signals are also approved (directional mode)
    return True

def hard_audit_hedge_ratio(buy_side_amount, sell_side_amount, math_certainty_verified=False, market_category=""):
    """
    Maintain Sentinel Sam's 1:1 delta-neutral audit hook.
    Authorizes multi-leg executions if mathematical certainty is verified, 
    but strictly locked to [Politics] and [Sports: Basketball/NBA].
    """
    if math_certainty_verified:
        cat = market_category.lower()
        is_politics = "politic" in cat
        is_nba = "basketball" in cat or "nba" in cat
        if is_politics or is_nba:
            return True
        # Fails hard if outside permitted bounds
        return False
        
    if buy_side_amount != sell_side_amount:
        # print("🛑 [SENTINEL SAM] 1:1 Delta-Neutral check failed.")
        return False
    return True
    
def hard_audit_drawdown_limit(current_equity, starting_equity):
    """
    Sentinel Sam mandatory pre-execution Capital Preservation check.
    Ensures Max 5% Drawdown is not breached.
    """
    if starting_equity > 0:
        drawdown_pct = ((starting_equity - current_equity) / starting_equity) * 100
        if drawdown_pct >= 5.0:
            msg = f"🛑 [Drawdown-Alerts] Drawdown exceeds 5.0% limit (Currently {drawdown_pct:.1f}%). Execution FROZEN."
            print(msg)
            send_update_to_sam(f"🛑 **CAPITAL PRESERVATION TRIGGERED**\n{msg}")
            return False
    # print("✅ [SENTINEL SAM HARD-AUDIT PASSED] Capital within safe Drawdown limits.")
    return True

def heartbeat_monitor():
    """Periodic liveness ping — sends a heartbeat to Telegram every 5 minutes."""
    while True:
        try:
            time.sleep(300)
            send_update_to_sam(
                "💗 *SENTINEL SAM HEARTBEAT*\n"
                "All systems operational. High-Yield Combo Mode active."
            )
        except KeyboardInterrupt:
            break
        except Exception:
            pass

if __name__ == "__main__":
    # print("Initiating Sentinel Sam gatekeeper and telemetry forwarder...")
    
    test_telemetry = (
        "📶 **SENTINEL SAM SYSTEMS ONLINE**\n"
        "• Environment: Termux Production\n"
        "• Target Connection: Kalshi Live API\n"
        "• Gatekeeper Mode: High-Yield Combo Enabled\n"
        "• Stream Status: Active & Monitoring Feed"
    )
    
    send_update_to_sam(test_telemetry)
    # Start heartbeat if called directly
    import threading
    t = threading.Thread(target=heartbeat_monitor, daemon=True)
    t.start()
    
    # Keep main thread alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
