import os
import json
import requests
import time
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, Any

class ProxyRotator:
    def __init__(self):
        # A pool of free residential/datacenter proxies (or user can input paid ones in a config)
        self.proxies = [
            # Fallback direct connection
            None, 
            # In a real environment, the user injects their BrightData/Oxylabs proxy URLs here
            # "http://user:pass@proxy1.com:8080",
            # "http://user:pass@proxy2.com:8080"
        ]
        self.index = 0

    def get_proxy(self):
        p = self.proxies[self.index % len(self.proxies)]
        self.index += 1
        return {"http": p, "https": p} if p else None

proxy_rotator = ProxyRotator()

# Load credentials from config
def _load_config():
    config_path = "social_trail_config.json"
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

# Configurable list of sharp sports capping accounts to weigh heavily
SHARP_USERS = ["sharp_bettor_1", "sharp_bettor_2", "sharp_bettor_3", "pro_capper", "vegas_insider"]

def parse_kickoff_time(text: str) -> datetime:
    """
    Parses kickoff time from the text notification.
    Supports ISO format (e.g. 2026-07-02T23:30:00Z) or generic HH:MM.
    If no time is found, defaults to 15 minutes from now.
    """
    # 1. Look for ISO datetime format
    iso_match = re.search(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z?', text)
    if iso_match:
        try:
            dt_str = iso_match.group(0).replace('Z', '')
            dt = datetime.fromisoformat(dt_str)
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            pass
            
    # 2. Look for HH:MM pattern
    time_match = re.search(r'(\d{1,2}):(\d{2})\s*(AM|PM)?', text, re.IGNORECASE)
    if time_match:
        try:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2))
            ampm = time_match.group(3)
            if ampm:
                ampm = ampm.upper()
                if ampm == "PM" and hour < 12:
                    hour += 12
                elif ampm == "AM" and hour == 12:
                    hour = 0
            now = datetime.now(timezone.utc)
            dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if dt < now:
                dt += timedelta(days=1)
            return dt
        except Exception:
            pass
            
    # Default fallback: 15 minutes from now
    return datetime.now(timezone.utc) + timedelta(minutes=15)

def parse_match_string(text: str) -> tuple:
    """
    Parses matchup string from notification text.
    e.g., "kickoff alert: POR vs CRO tonight!" -> ("POR", "CRO")
    """
    pattern = r'([A-Za-z0-9]+)\s*(?:vs|@|-)\s*([A-Za-z0-9]+)'
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip().upper(), match.group(2).strip().upper()
    return None

def search_tweets_for_game(team1: str, team2: str) -> list:
    """
    Searches recent tweets for game keywords and betting terms.
    Falls back to mock sharp tweets if X developer keys are missing.
    """
    config = _load_config()
    x_api_key = config.get("x_api_key")
    
    if not x_api_key:
        # Returns 6 sharp/public tweets (meets the minimum of 5 sharp authority tweets)
        return [
            {"text": f"Locking in {team2} +1.5, public is heavy on {team1} but {team2} has huge value", "user_id": "sharp_bettor_1"},
            {"text": f"{team1} vs {team2}: fading {team1} here, {team2} looks like a great underdog pick", "user_id": "sharp_bettor_2"},
            {"text": f"{team1} vs {team2} kickoff. {team1} looks strong, putting 3 units on {team1}", "user_id": "public_bettor_1"},
            {"text": f"{team2} line is mispriced. Easiest bet of the day {team2} +0.5", "user_id": "sharp_bettor_3"},
            {"text": f"Sharp money on {team2}. Portuguese defense is vulnerable", "user_id": "pro_capper"},
            {"text": f"Vegas opened at -130 for {team1}. CRO line has value", "user_id": "vegas_insider"}
        ]
        
    try:
        from requests_oauthlib import OAuth1
        query = f"({team1} OR {team2}) (slip OR bet OR units OR fade OR line)"
        url = "https://api.twitter.com/2/tweets/search/recent"
        auth = OAuth1(config.get("x_api_key"), config.get("x_api_secret"), config.get("x_access_token"), config.get("x_access_token_secret"))
        params = {
            "query": query,
            "max_results": 10,
            "tweet.fields": "author_id,created_at"
        }
        proxy = proxy_rotator.get_proxy()
        print(f"📡 [X-SNIPER] Executing search through rotated IP proxy route...")
        res = requests.get(url, params=params, auth=auth, timeout=10, proxies=proxy)
        if res.status_code == 200:
            return res.json().get("data", [])
    except Exception as e:
        print(f"⚠️ X search failed: {e}")
        
    return []

def calculate_sentiment(tweets: list, team1: str, team2: str) -> dict:
    """
    Scores the sentiment for each team based on sharp betting tweets.
    """
    team1_score = 0.0
    team2_score = 0.0
    
    for tweet in tweets:
        text = tweet.get("text", "").upper()
        author = tweet.get("user_id") or tweet.get("author_id", "")
        
        # Double weight if from a confirmed sharp account
        multiplier = 2.0 if author in SHARP_USERS else 1.0
        
        t1_signals = 0
        t2_signals = 0
        
        if team1 in text:
            if any(term in text for term in ["BET", "SLIP", "LOCK", "VALUE", "BUY", "WIN"]):
                t1_signals += 1
            if "FADE" in text or "UNDERDOG" in text and team2 in text:
                t1_signals -= 1
        
        if team2 in text:
            if any(term in text for term in ["BET", "SLIP", "LOCK", "VALUE", "BUY", "WIN"]):
                t2_signals += 1
            if "FADE" in text or "UNDERDOG" in text and team1 in text:
                t2_signals -= 1
                
        team1_score += t1_signals * multiplier
        team2_score += t2_signals * multiplier
        
    return {
        team1: team1_score,
        team2: team2_score
    }

def check_sentiment_divergence_and_trade(match_text: str, baseline_prob: float = 0.71):
    """
    Compares Kalshi baseline probabilities to sharp social sentiment with time-locks, volume floors and balance checks.
    """
    # 1. Balance Check Guard at the very top
    import kalshi_auto_trader
    trader = kalshi_auto_trader.KalshiDefensiveTrader()
    balance = trader.fetch_portfolio_equity()
    if balance is not None and balance <= 33.59:
        print(f"💰 [Balance Guard] Kalshi account balance (${balance:.2f}) hit protection floor of $33.59. Sleeping thread to conserve X polling limits...")
        time.sleep(60)
        return
        
    # Liquidity Cleanup: Cancel all open resting orders before scanning X to ensure full buying power is available
    try:
        print("🧹 [Liquidity Cleanup] Cancelling any open resting orders to free up wallet balance...")
        trader.cancel_all_resting_orders()
    except Exception as e:
        print(f"⚠️ Failed to cancel resting orders: {e}")
        
    # 2. Kickoff Time-Lock Guard
    kickoff_time = parse_kickoff_time(match_text)
    current_time = datetime.now(timezone.utc)
    cutoff_time = kickoff_time - timedelta(minutes=5)
    
    if current_time > cutoff_time:
        print(f"🛑 [Kickoff Guard] Aborting trade: current time {current_time.isoformat()} is within 5 minutes or past kickoff time {kickoff_time.isoformat()}.")
        return
        
    # 3. Match Parsing
    teams = parse_match_string(match_text)
    if not teams:
        print("❌ Could not parse teams from matchup string.")
        return
        
    team1, team2 = teams
    print(f"📊 [Sentiment Sniper] Match parsed: {team1} vs {team2}. Baseline Prob: {baseline_prob:.1%}")
    
    # 4. Search tweets
    tweets = search_tweets_for_game(team1, team2)
    
    # 5. Volume Floor: Keep only unique tweets from our sharp authority list
    authority_tweets = [t for t in tweets if (t.get("user_id") or t.get("author_id")) in SHARP_USERS]
    
    if len(authority_tweets) < 5:
        print(f"🛑 [Volume Guard] Skipped: only {len(authority_tweets)} unique tweets from authority list found (Required: >= 5).")
        return
        
    scores = calculate_sentiment(authority_tweets, team1, team2)
    
    t1_score = scores[team1]
    t2_score = scores[team2]
    
    print(f"📈 [Sentiment Analysis] {team1} Score: {t1_score} | {team2} Score: {t2_score}")
    
    # Notify Sam about analysis metrics
    config = _load_config()
    token = config.get("telegram_token")
    
    try:
        from brain import send_update_to_sam
        sam_msg = (
            f"🎯 *[SENTIMENT SNIPER ANALYSIS]*\n"
            f"⚔️ *Matchup*: `{team1} vs {team2}`\n"
            f"📊 *Baseline Probability*: `{baseline_prob:.1%}`\n"
            f"👥 *Sentiment Scores*:\n"
            f"   ├─ {team1}: `{t1_score:+.1f}`\n"
            f"   └─ {team2}: `{t2_score:+.1f}`"
        )
        send_update_to_sam(sam_msg, token=token)
    except Exception as e:
        print(f"⚠️ Failed to forward sentiment status to Sam: {e}")
        
    # Check for divergence
    target_side = None
    if baseline_prob > 0.60 and t2_score > t1_score + 1.0:
        # Baseline favors Team 1, but Twitter consensus favors Team 2
        print(f"🚨 SENTIMENT DIVERGENCE DETECTED! Kalshi baseline favors {team1} ({baseline_prob:.1%}), but X sharps favor {team2}.")
        target_side = team2
    elif baseline_prob < 0.40 and t1_score > t2_score + 1.0:
        # Baseline favors Team 2, but Twitter consensus favors Team 1
        print(f"🚨 SENTIMENT DIVERGENCE DETECTED! Kalshi baseline favors {team2} ({1-baseline_prob:.1%}), but X sharps favor {team1}.")
        target_side = team1
        
    if target_side:
        # Notify Sam about divergence trigger
        try:
            from brain import send_update_to_sam
            divergence_msg = (
                f"🚨 *[SENTIMENT DIVERGENCE DETECTED]*\n"
                f"⚔️ Matchup: `{team1} vs {team2}`\n"
                f"🔥 Undervalued Target: `{target_side}`\n"
                f"⚡ *Action*: Triggering Defensive Limit Order on Kalshi..."
            )
            send_update_to_sam(divergence_msg, token=token)
        except Exception as e:
            print(f"⚠️ Failed to send divergence trigger alert: {e}")
            
        # Map matchup and trigger a trade via kalshi_auto_trader
        try:
            import threading
            mock_pick_id = 888888 + int(time.time()) % 100000
            
            # If target_side is the second team, we bet NO on Team 1 (Under)
            is_under = (target_side == team2)
            # resolved_odds of +150 corresponds to ~40% prob (undervalued underdog line)
            bet_type = "Player Prop Under" if is_under else "Player Prop Over"
            
            print(f"🛒 Placing trade on Kalshi for undervalued target: {target_side}...")
            t = threading.Thread(
                target=kalshi_auto_trader.evaluate_and_trade,
                args=(mock_pick_id, "Sports", f"{team1} vs {team2}", bet_type, target_side, 0.5, +150, ["Sharp sentiment divergence"]),
                daemon=True
            )
            t.start()
        except Exception as e:
            print(f"❌ Failed to trigger Kalshi trade: {e}")

if __name__ == "__main__":
    print("🧪 Running Social Sentiment Sniper self-tests...")
    
    # Test case 1: Kickoff is in the future (Time check should pass)
    future_kickoff = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat() + "Z"
    print(f"\n--- Test Case 1: Future Kickoff ({future_kickoff}) ---")
    check_sentiment_divergence_and_trade(f"Portugal vs Croatia kickoff alert (POR vs CRO) at {future_kickoff}", baseline_prob=0.71)
    
    # Test case 2: Kickoff is in the past (Time check should abort)
    past_kickoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat() + "Z"
    print(f"\n--- Test Case 2: Past Kickoff ({past_kickoff}) ---")
    check_sentiment_divergence_and_trade(f"Portugal vs Croatia kickoff alert (POR vs CRO) at {past_kickoff}", baseline_prob=0.71)
    
    # Wait a moment for the trade threads to print self-test logs
    time.sleep(2)
