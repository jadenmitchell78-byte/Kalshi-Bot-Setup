"""
social_trail_bot.py — X (Twitter) & Instagram Call Copy-Trading Bot
Monitors custom profiles (such as Locked Lines, DougsPicks, Deshawn's Dubs, Sharpr Sports, etc.)
for Solana/Base contract addresses and triggers buy actions.

Supports:
1. Platforms: X (Twitter) and Instagram.
2. Modes: "scraping_simulation" (mock posts), "rss_feed" (Nitter/RSSHub scraping), and "official_api" (X API v2).
"""

import os
import sys
import json
import time
import re
import requests
import xml.etree.ElementTree as ET
import asyncio
import threading

import builtins
import v2_logger
v2_logger.inject_v2_logger()

# ── Paths & Global Files ──────────────────────────────────────────────────────
CONFIG_FILE = "social_trail_config.json"
STATE_FILE = "social_trail_state.json"
MOCK_FILE = "mock_social_posts.json"
GLOBAL_CONFIG = "config.json"

SOL_BUY_TRIGGER = "sol_buy_trigger.json"
BASE_BUY_TRIGGER = "buy_trigger.json"

# Common Blacklisted Addresses (System tokens/contracts we shouldn't trade)
SYSTEM_BLACKLIST = {
    # Solana
    "So11111111111111111111111111111111111111112", # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", # USDC
    "Es9vMFrzaSVjMKtyG4C3VdK3p2u9C1B272J1cKEXYssy", # USDT
    "HZ1JovNiTYG2kyhuEq1MZyMGe4oN9fRejt7W7p8xuaRx", # PYUSD
    # Base / Ethereum
    "0x4200000000000000000000000000000000000006", # WETH (Base)
    "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2", # WETH (Mainnet)
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bda02913", # USDC (Base)
    "0xfde4C96c8593536E31F229EA8f37b2AD3B55AD7f", # USDT (Base)
}

# ── JSON Helpers ──────────────────────────────────────────────────────────────
def _rj(path, default=None):
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Error reading {path}: {e}")
    return default if default is not None else {}

def _wj(path, data):
    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"⚠️ Error writing {path}: {e}")
        return False

# ── Telegram Helper ───────────────────────────────────────────────────────────
def send_telegram(msg: str, reply_markup=None):
    tc = _rj(CONFIG_FILE)
    token = tc.get("telegram_token")
    chat_id = tc.get("telegram_chat_id")
    
    # Fallback to global config
    if not token or not chat_id:
        gc = _rj(GLOBAL_CONFIG)
        if not token:
            token = gc.get("telegram_token", "8653525018:AAFr1wL5vauNEnu5xhGVbh0u1V9N3f2e_bY")
        if not chat_id:
            chat_id = gc.get("telegram_chat_id", "5992267511")
            
    if not token or not chat_id:
        return
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": msg,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(api_url, json=payload, timeout=10)
    except Exception as e:
        print(f"⚠️ Telegram alert failed: {e}")

def send_discord_webhook(msg: str):
    tc = _rj(CONFIG_FILE)
    webhook_url = tc.get("discord_webhook_url")
    if not webhook_url:
        gc = _rj(GLOBAL_CONFIG)
        webhook_url = gc.get("discord_webhook_url")
        
    if not webhook_url:
        print("ℹ️ Discord Webhook URL not configured. Skipping Discord alert.")
        return
        
    payload = {
        "content": msg
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10)
        print("🚀 Discord Webhook alert sent successfully!")
    except Exception as e:
        print(f"⚠️ Discord Webhook alert failed: {e}")

def send_telegram_webhook(msg: str):
    tc = _rj(CONFIG_FILE)
    webhook_url = tc.get("telegram_webhook_url")
    if not webhook_url:
        return
        
    payload = {
        "text": msg,
        "parse_mode": "Markdown"
    }
    try:
        requests.post(webhook_url, json=payload, timeout=10)
        print("🚀 Telegram Webhook alert sent successfully!")
    except Exception as e:
        print(f"⚠️ Telegram Webhook alert failed: {e}")

# ── Parser & Address Extraction ───────────────────────────────────────────────
def clean_html(raw_html):
    """Strip HTML tags from RSS descriptions."""
    clean_re = re.compile('<.*?>')
    return re.sub(clean_re, '', raw_html)

def extract_addresses(text):
    """Scan text for Solana mint addresses and Base/Ethereum contracts."""
    solana_mints = []
    base_contracts = []
    
    # 1. Solana: Base58 string of length 32-44
    sol_regex = r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b"
    for match in re.findall(sol_regex, text):
        if match not in SYSTEM_BLACKLIST:
            if len(match) <= 44:
                solana_mints.append(match)

    # 2. Base/Ethereum: 0x followed by 40 hex characters
    eth_regex = r"\b0x[a-fA-F0-9]{40}\b"
    for match in re.findall(eth_regex, text):
        if match not in SYSTEM_BLACKLIST:
            base_contracts.append(match)
            
    return list(set(solana_mints)), list(set(base_contracts))

# ── Scraping Engines ──────────────────────────────────────────────────────────
def fetch_mock_posts():
    """Read simulated posts from mock_social_posts.json."""
    if not os.path.exists(MOCK_FILE):
        return []
    return _rj(MOCK_FILE, [])

def fetch_x_rss(username, nitter_instances):
    """Scrape X/Twitter posts using public Nitter RSS feeds with rotating fallbacks."""
    posts = []
    for instance in nitter_instances:
        url = f"{instance.rstrip('/')}/{username}/rss"
        print(f"🔗 Checking Nitter X feed for @{username} on {instance}...")
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.text.strip():
                root = ET.fromstring(r.text)
                items = root.findall(".//item")
                for item in items:
                    title = item.find("title").text or ""
                    desc_elem = item.find("description")
                    desc = desc_elem.text if desc_elem is not None else ""
                    link = item.find("link").text or ""
                    
                    post_id = link
                    match_id = re.search(r"/status/(\d+)", link)
                    if match_id:
                        post_id = match_id.group(1)
                        
                    full_text = f"{title}\n{clean_html(desc)}"
                    posts.append({
                        "platform": "x",
                        "username": username,
                        "post_id": f"x_{post_id}",
                        "text": full_text,
                        "link": link
                    })
                if posts:
                    break
        except Exception as e:
            print(f"⚠️ Nitter X Fetch error on {instance}: {e}")
    return posts

def fetch_instagram_rss(username, rsshub_instances):
    """Scrape Instagram posts using public RSSHub user feeds with rotating fallbacks."""
    posts = []
    for instance in rsshub_instances:
        url = f"{instance.rstrip('/')}/instagram/user/{username}"
        print(f"🔗 Checking Instagram feed for @{username} on {instance}...")
        try:
            r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.text.strip():
                root = ET.fromstring(r.text)
                items = root.findall(".//item")
                for item in items:
                    title = item.find("title").text or ""
                    desc_elem = item.find("description")
                    desc = desc_elem.text if desc_elem is not None else ""
                    link = item.find("link").text or ""
                    
                    # Extract unique identifier from link/guid
                    post_id = link
                    match_id = re.search(r"/p/([a-zA-Z0-9_-]+)", link)
                    if match_id:
                        post_id = match_id.group(1)
                    else:
                        match_guid = item.find("guid")
                        if match_guid is not None and match_guid.text:
                            post_id = match_guid.text.split('/')[-1]
                        
                    full_text = f"{title}\n{clean_html(desc)}"
                    posts.append({
                        "platform": "instagram",
                        "username": username,
                        "post_id": f"ig_{post_id}",
                        "text": full_text,
                        "link": link
                    })
                if posts:
                    break
        except Exception as e:
            print(f"⚠️ RSSHub Instagram Fetch error on {instance}: {e}")
    return posts

def fetch_official_x_api(username, bearer_token):
    """Retrieve tweets using official Twitter/X API v2."""
    if not bearer_token:
        return []
    headers = {"Authorization": f"Bearer {bearer_token}"}
    posts = []
    try:
        user_url = f"https://api.twitter.com/2/users/by/username/{username}"
        r = requests.get(user_url, headers=headers, timeout=10)
        if r.status_code == 200:
            user_data = r.json().get("data")
            if user_data:
                user_id = user_data["id"]
                timeline_url = f"https://api.twitter.com/2/users/{user_id}/tweets"
                params = {"max_results": 10, "tweet.fields": "text"}
                r_tl = requests.get(timeline_url, headers=headers, params=params, timeout=10)
                if r_tl.status_code == 200:
                    data_list = r_tl.json().get("data", [])
                    for item in data_list:
                        posts.append({
                            "platform": "x",
                            "username": username,
                            "post_id": f"x_{item['id']}",
                            "text": item["text"],
                            "link": f"https://x.com/{username}/status/{item['id']}"
                        })
    except Exception as e:
        print(f"⚠️ Official X API exception for @{username}: {e}")
    return posts

# ── Main Trail Processing ─────────────────────────────────────────────────────
def fetch_apify_tweets(username, apify_token, actor="apify/twitter-scraper"):
    """Retrieve tweets using Apify API."""
    if not apify_token:
        print("⚠️ Apify API token not configured.")
        return []
    
    url = f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/runs?token={apify_token}"
    payload = {
        "twitterHandles": [username],
        "tweetsLimit": 3,
        "addUserInfo": False
    }
    
    try:
        print(f"🎬 Starting Apify Twitter Scraper task for @{username}...")
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code in [200, 201]:
            run_data = r.json().get("data", {})
            run_id = run_data.get("id")
            dataset_id = run_data.get("defaultDatasetId")
            
            # Wait for run to finish (poll for up to 60s)
            status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={apify_token}"
            for _ in range(12):
                time.sleep(5)
                r_status = requests.get(status_url, timeout=10)
                if r_status.status_code == 200:
                    status = r_status.json().get("data", {}).get("status")
                    print(f"   Apify status: {status}")
                    if status == "SUCCEEDED":
                        break
                    elif status in ["FAILED", "ABORTED", "TIMED-OUT"]:
                        print(f"❌ Apify run {run_id} failed with status {status}")
                        return []
            
            # Fetch results
            dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={apify_token}"
            r_data = requests.get(dataset_url, timeout=15)
            if r_data.status_code == 200:
                items = r_data.json()
                posts = []
                for item in items:
                    tweet_id = item.get("id") or item.get("tweet_id")
                    text = item.get("text") or item.get("full_text") or ""
                    item_user = item.get("user", {})
                    user_screen_name = item_user.get("screen_name") or username
                    
                    if tweet_id and text:
                        posts.append({
                            "platform": "x",
                            "username": user_screen_name,
                            "post_id": f"x_{tweet_id}",
                            "text": text,
                            "link": f"https://x.com/{user_screen_name}/status/{tweet_id}"
                        })
                return posts
    except Exception as e:
        print(f"⚠️ Apify tweet fetch error for @{username}: {e}")
    return []

def fetch_apify_instagram(username, apify_token, actor="apify/instagram-scraper"):
    """Retrieve instagram posts using Apify API."""
    if not apify_token:
        print("⚠️ Apify API token not configured.")
        return []
    
    url = f"https://api.apify.com/v2/acts/{actor.replace('/', '~')}/runs?token={apify_token}"
    payload = {
        "usernames": [username],
        "resultsLimit": 3
    }
    
    try:
        print(f"🎬 Starting Apify Instagram Scraper task for @{username}...")
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code in [200, 201]:
            run_data = r.json().get("data", {})
            run_id = run_data.get("id")
            dataset_id = run_data.get("defaultDatasetId")
            
            # Wait for run to finish
            status_url = f"https://api.apify.com/v2/actor-runs/{run_id}?token={apify_token}"
            for _ in range(12):
                time.sleep(5)
                r_status = requests.get(status_url, timeout=10)
                if r_status.status_code == 200:
                    status = r_status.json().get("data", {}).get("status")
                    print(f"   Apify status: {status}")
                    if status == "SUCCEEDED":
                        break
                    elif status in ["FAILED", "ABORTED", "TIMED-OUT"]:
                        print(f"❌ Apify run {run_id} failed with status {status}")
                        return []
                        
            # Fetch results
            dataset_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items?token={apify_token}"
            r_data = requests.get(dataset_url, timeout=15)
            if r_data.status_code == 200:
                items = r_data.json()
                posts = []
                for item in items:
                    post_id = item.get("id") or item.get("shortCode")
                    text = item.get("caption") or item.get("text") or ""
                    if post_id and text:
                        posts.append({
                            "platform": "instagram",
                            "username": username,
                            "post_id": f"ig_{post_id}",
                            "text": text,
                            "link": f"https://instagram.com/p/{post_id}"
                        })
                return posts
    except Exception as e:
        print(f"⚠️ Apify instagram fetch error for @{username}: {e}")
    return []

def fetch_playwright_posts(username, platform):
    """Local scraper using headless Playwright."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("⚠️ Playwright library not installed. To run in playwright mode, please install it: pip install playwright && playwright install")
        return []
    
    posts = []
    try:
        with sync_playwright() as p:
            print(f"🎭 Playwright launching browser to scrape @{username} on {platform.upper()}...")
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
            
            if platform == "x":
                url = f"https://nitter.net/{username}"
                page.goto(url, timeout=30000)
                tweets = page.locator(".timeline-item").all()
                for tweet in tweets[:5]:
                    try:
                        text_content = tweet.locator(".tweet-content").text_content() or ""
                        link_elem = tweet.locator(".tweet-link").first
                        link = link_elem.get_attribute("href") if link_elem else ""
                        tweet_id = link.split("/status/")[-1].split("#")[0] if "/status/" in link else str(hash(text_content))
                        
                        posts.append({
                            "platform": "x",
                            "username": username,
                            "post_id": f"x_pw_{tweet_id}",
                            "text": text_content.strip(),
                            "link": f"https://x.com/{username}/status/{tweet_id}"
                        })
                    except Exception:
                        continue
            elif platform == "instagram":
                url = f"https://dumpoir.com/v/{username}"
                page.goto(url, timeout=30000)
                items = page.locator(".photo").all()
                for item in items[:5]:
                    try:
                        caption = item.locator(".photo-info").text_content() or ""
                        link = item.locator("a").first.get_attribute("href") or ""
                        post_id = link.split("/")[-1] if link else str(hash(caption))
                        
                        posts.append({
                            "platform": "instagram",
                            "username": username,
                            "post_id": f"ig_pw_{post_id}",
                            "text": caption.strip(),
                            "link": f"https://instagram.com/p/{post_id}"
                        })
                    except Exception:
                        continue
            browser.close()
    except Exception as e:
        print(f"❌ Playwright scraping error: {e}")
    return posts

def check_goplus_safety(token_addr: str) -> bool:
    """
    Query GoPlus Security API to verify token contract safety.
    """
    url = f"https://api.gopluslabs.io/api/v1/token_security/8453?contract_addresses={token_addr}"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            data = r.json()
            result = data.get("result", {})
            token_data = result.get(token_addr.lower(), {})
            if not token_data:
                print(f"⚠️ GoPlus: No telemetry found for {token_addr}.")
                return False
            
            # GoPlus Security Filters
            if token_data.get("is_open_source") == "0":
                print(f"🚨 GoPlus Check FAILED for {token_addr}: Contract code is NOT verified.")
                return False
            if token_data.get("is_honeypot") == "1" or token_data.get("cannot_sell") == "1":
                print(f"🚨 GoPlus Check FAILED for {token_addr}: Honeypot detected.")
                return False
            if token_data.get("hidden_owner") == "1":
                print(f"🚨 GoPlus Check FAILED for {token_addr}: Hidden owner backdoor detected.")
                return False
            if token_data.get("owner_change_balance") == "1":
                print(f"🚨 GoPlus Check FAILED for {token_addr}: Owner can modify token balances.")
                return False
            if token_data.get("transfer_pausable") == "1":
                print(f"🚨 GoPlus Check FAILED for {token_addr}: Trading can be paused by owner.")
                return False
            if token_data.get("is_blacklisted") == "1":
                print(f"🚨 GoPlus Check FAILED for {token_addr}: Creator can blacklist/freeze holder wallets.")
                return False
            if token_data.get("slippage_modifiable") == "1" or token_data.get("personal_slippage_modifiable") == "1":
                print(f"🚨 GoPlus Check FAILED for {token_addr}: Creator can modify taxes dynamically.")
                return False
            
            buy_tax = float(token_data.get("buy_tax", 0) or 0)
            sell_tax = float(token_data.get("sell_tax", 0) or 0)
            if buy_tax > 0.12 or sell_tax > 0.12:
                print(f"🚨 GoPlus Check FAILED for {token_addr}: High taxes (Buy: {buy_tax:.1%}, Sell: {sell_tax:.1%}).")
                return False
                
            print(f"🟢 GoPlus Check PASSED for {token_addr}")
            return True
    except Exception as e:
        print(f"⚠️ Error checking GoPlus safety: {e}")
    return False

def check_rugcheck_safety(token_addr: str) -> bool:
    """
    Query RugCheck API to verify Solana token security.
    """
    url = f"https://api.rugcheck.xyz/v1/tokens/{token_addr}/report"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            report = r.json()
            score = report.get("score", 0)
            if score > 1200:
                print(f"🚨 RugCheck Check FAILED for {token_addr}: High risk score ({score}).")
                return False
            risks = report.get("risks", [])
            for risk in risks:
                name = risk.get("name", "")
                level = risk.get("level", "")
                if level == "danger":
                    print(f"🚨 RugCheck Check FAILED for {token_addr}: Critical danger risk '{name}'.")
                    return False
            print(f"🟢 RugCheck Check PASSED for {token_addr}")
            return True
    except Exception as e:
        print(f"⚠️ Error checking RugCheck safety: {e}")
    return False

def check_liquidity(token_addr: str, chain: str = "base", min_eth: float = 2.0, min_sol: float = 30.0) -> bool:
    """
    Query DexScreener to check liquidity for the token.
    """
    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_addr}"
    try:
        r = requests.get(url, timeout=5)
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if not pairs:
                print(f"🚨 Liquidity Check FAILED for {token_addr}: No pairs found on DexScreener.")
                return False
            
            # Find pair with highest liquidity
            best_pair = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
            liq_usd = float(best_pair.get("liquidity", {}).get("usd", 0) or 0)
            liq_quote = float(best_pair.get("liquidity", {}).get("quote", 0) or 0)
            quote_symbol = best_pair.get("quoteToken", {}).get("symbol", "").upper()
            
            if chain in ["base", "ethereum"]:
                if quote_symbol in ["WETH", "ETH"]:
                    if liq_quote >= min_eth:
                        print(f"🟢 Liquidity Check PASSED for {token_addr}: {liq_quote:.2f} WETH >= {min_eth} WETH")
                        return True
                # USD fallback (approx $3400 per ETH)
                eth_usd_threshold = min_eth * 3400.0
                if liq_usd >= eth_usd_threshold:
                    print(f"🟢 Liquidity Check PASSED for {token_addr}: ${liq_usd:,.2f} USD >= ${eth_usd_threshold:,.2f} USD equivalent")
                    return True
                print(f"🚨 Liquidity Check FAILED for {token_addr}: Reserves too low (${liq_usd:,.2f} USD vs ${eth_usd_threshold:,.2f} required).")
                return False
            elif chain == "solana":
                if quote_symbol in ["WSOL", "SOL"]:
                    if liq_quote >= min_sol:
                        print(f"🟢 Liquidity Check PASSED for {token_addr}: {liq_quote:.2f} SOL >= {min_sol} SOL")
                        return True
                # USD fallback (approx $150 per SOL)
                sol_usd_threshold = min_sol * 150.0
                if liq_usd >= sol_usd_threshold:
                    print(f"🟢 Liquidity Check PASSED for {token_addr}: ${liq_usd:,.2f} USD >= ${sol_usd_threshold:,.2f} USD equivalent")
                    return True
                print(f"🚨 Liquidity Check FAILED for {token_addr}: Reserves too low (${liq_usd:,.2f} USD vs ${sol_usd_threshold:,.2f} required).")
                return False
    except Exception as e:
        print(f"⚠️ Error checking liquidity: {e}")
    return False

LAST_SCRAPE_TIME = 0
_process_posts_lock = threading.Lock()

def process_posts():
    global LAST_SCRAPE_TIME
    if not _process_posts_lock.acquire(blocking=False):
        print("⏳ process_posts already running. Skipping concurrent run.")
        return
    try:
        _process_posts_internal()
    finally:
        _process_posts_lock.release()

def _process_posts_internal():
    global LAST_SCRAPE_TIME
    config = _rj(CONFIG_FILE)
    if not config.get("scanner_active", True):
        print("⏸ Social Trail Bot is paused in config.")
        return
        
    mode = config.get("twitter_scraping_mode", "scraping_simulation")
    
    state = _rj(STATE_FILE, {"seen_posts": []})
    seen = set(state.get("seen_posts", []))
    new_seen = list(seen)
    
    all_posts = []
    
    # 1. Load Real-Time Webhook Queue Posts (if any exist)
    webhook_queue_file = "social_trail_webhook_queue.json"
    if os.path.exists(webhook_queue_file):
        try:
            webhook_posts = _rj(webhook_queue_file, [])
            if webhook_posts:
                print(f"🔌 Ingesting {len(webhook_posts)} posts from real-time webhook queue...")
                all_posts.extend(webhook_posts)
                _wj(webhook_queue_file, [])  # Clear webhook queue after reading
        except Exception as e:
            print(f"⚠️ Error reading webhook queue file: {e}")
            
    # 2. Fetch posts based on mode (check rate limiting or active interval)
    current_time = time.time()
    scrape_interval = config.get("polling_interval_seconds", 60)
    should_scrape = False
    
    if mode == "scraping_simulation":
        should_scrape = True
    elif mode != "webhook_only":
        if current_time - LAST_SCRAPE_TIME >= scrape_interval:
            should_scrape = True
            LAST_SCRAPE_TIME = current_time
            
    if should_scrape:
        monitored_x = config.get("monitored_accounts_x", [])
        monitored_ig = config.get("monitored_accounts_instagram", [])
        nitter_instances = config.get("nitter_instances", [])
        rsshub_instances = config.get("rsshub_instances", [])
        bearer_token = config.get("bearer_token", "")
        apify_api_token = config.get("apify_api_token", "")
        apify_twitter_actor = config.get("apify_twitter_actor", "apify/twitter-scraper")
        apify_instagram_actor = config.get("apify_instagram_actor", "apify/instagram-scraper")
        
        print(f"🐦 [Social Trail] Mode: {mode} | Active Scraping: Monitoring {len(monitored_x)} X accounts & {len(monitored_ig)} Instagram handles.")
        
        # Fetch X Profiles
        for username in monitored_x:
            if mode == "rss_feed":
                all_posts.extend(fetch_x_rss(username, nitter_instances))
            elif mode == "official_api":
                all_posts.extend(fetch_official_x_api(username, bearer_token))
            elif mode == "apify":
                all_posts.extend(fetch_apify_tweets(username, apify_api_token, apify_twitter_actor))
            elif mode == "playwright":
                all_posts.extend(fetch_playwright_posts(username, "x"))
                
        # Fetch Instagram Handles
        for username in monitored_ig:
            if mode == "rss_feed":
                all_posts.extend(fetch_instagram_rss(username, rsshub_instances))
            elif mode == "apify":
                all_posts.extend(fetch_apify_instagram(username, apify_api_token, apify_instagram_actor))
            elif mode == "playwright":
                all_posts.extend(fetch_playwright_posts(username, "instagram"))
                
    # 2. Process posts
    for p in all_posts:
        p_id = str(p.get("post_id"))
        platform = p.get("platform", "x").upper()
        username = p.get("username")
        text = p.get("text", "")
        link = p.get("link", "")
        if not link:
            link = f"https://x.com/{username}/status/{p_id}" if platform == "X" else f"https://instagram.com/{username}"
        
        if p_id in seen:
            continue
            
        # Check for image post / OCR placeholder integration
        image_url = p.get("image_url") or p.get("media_url")
        if not image_url:
            # Check text for inline image links
            img_match = re.search(r"https?://\S+\.(?:png|jpg|jpeg|gif)", text, re.IGNORECASE)
            if img_match:
                image_url = img_match.group(0)
                
        if image_url and (not text or len(text.strip()) < 10 or "slip" in text.lower() or "bet" in text.lower() or "ticket" in text.lower()):
            try:
                from betting_parser import perform_ocr_on_image
                print(f"🖼️ [OCR] Image post/link detected for @{username}. Extracting text...")
                ocr_text = perform_ocr_on_image(image_url)
                if ocr_text:
                    text = f"{text}\n{ocr_text}".strip()
                    print(f"📖 [OCR Result] Extracted text: {ocr_text}")
            except Exception as ocr_err:
                print(f"⚠️ OCR processing failed: {ocr_err}")
            
        print(f"📣 New post from @{username} on {platform}: {text[:80]}...")
        solana_mints, base_contracts = extract_addresses(text)
        
        # Track that we parsed this post
        new_seen.append(p_id)
        
        # 3. Trigger Buy Actions
        if solana_mints and config.get("auto_buy_solana", True):
            for mint in solana_mints:
                print(f"🟣 Solana Mint Detected: {mint}")
                
                # Verify RugCheck security filters and minimum liquidity
                if not check_rugcheck_safety(mint):
                    print(f"🚫 Solana Mint {mint} failed RugCheck security filters. Skipping trigger.")
                    continue
                if not check_liquidity(mint, chain="solana", min_sol=30.0):
                    print(f"🚫 Solana Mint {mint} failed liquidity verification. Skipping trigger.")
                    continue
                
                trigger_data = {
                    "token_address": mint,
                    "amount_sol": config.get("solana_trade_amount", 0.05),
                    "source": "social_trail",
                    "platform": platform,
                    "caller": username,
                    "timestamp": int(time.time())
                }
                _wj(SOL_BUY_TRIGGER, trigger_data)
                
                msg = (
                    f"📢 *[SOCIAL TRAIL]*\n"
                    f"🔊 Call detected from @{username} on *{platform}*!\n"
                    f"🔗 [View Original Post]({link})\n\n"
                    f"🟣 *Chain*: Solana\n"
                    f"🪙 *Mint Address*: `{mint}`\n"
                    f"🛡️ *Security*: RugCheck PASSED\n"
                    f"⚡ *Action*: Auto-buy trigger created (`{config.get('solana_trade_amount', 0.05)} SOL`)"
                )
                send_telegram(msg)
                send_discord_webhook(msg)
                send_telegram_webhook(msg)
                
        if base_contracts and config.get("auto_buy_base", True):
            for contract in base_contracts:
                print(f"🔷 Base Contract Detected: {contract}")
                
                # Verify GoPlus security filters and minimum liquidity
                if not check_goplus_safety(contract):
                    print(f"🚫 Base Contract {contract} failed GoPlus security filters. Skipping trigger.")
                    continue
                if not check_liquidity(contract, chain="base", min_eth=2.0):
                    print(f"🚫 Base Contract {contract} failed liquidity verification. Skipping trigger.")
                    continue
                
                trigger_data = {
                    "token_address": contract,
                    "amount_eth": config.get("base_trade_amount", 0.002),
                    "source": "social_trail",
                    "platform": platform,
                    "caller": username,
                    "timestamp": int(time.time())
                }
                _wj(BASE_BUY_TRIGGER, trigger_data)
                
                msg = (
                    f"📢 *[SOCIAL TRAIL]*\n"
                    f"🔊 Call detected from @{username} on *{platform}*!\n"
                    f"🔗 [View Original Post]({link})\n\n"
                    f"🔷 *Chain*: Base\n"
                    f"🪙 *Contract*: `{contract}`\n"
                    f"🛡️ *Security*: GoPlus PASSED\n"
                    f"⚡ *Action*: Auto-buy trigger created (`{config.get('base_trade_amount', 0.002)} ETH`)"
                )
                send_telegram(msg)
                send_discord_webhook(msg)
                send_telegram_webhook(msg)
                
        # 4. Check for sports betting pick calls (Module B, C, D)
        if not solana_mints and not base_contracts:
            try:
                from betting_parser import parse_betting_post
                from betting_validation import validate_and_fetch_odds
                from betting_db import log_raw_post, save_parsed_pick
                from betting_aggregator import build_consensus_parlay
                
                parsed_picks = parse_betting_post(text)
                if parsed_picks:
                    print(f"🏈 Extracted {len(parsed_picks)} sports picks from capper @{username}.")
                    raw_post_id = log_raw_post(username, text, int(time.time()))
                    
                    valid_picks = []
                    for pick in parsed_picks:
                        league = pick.get("league")
                        matchup = pick.get("matchup")
                        bet_type = pick.get("bet_type")
                        target_asset = pick.get("target_asset")
                        line_value = pick.get("line_value")
                        
                        is_valid, msg_reason, live_odds, resolved_line = validate_and_fetch_odds(
                            league, matchup, bet_type, target_asset, line_value
                        )
                        
                        if is_valid:
                            resolved_odds = live_odds or pick.get("odds") or -110
                            
                            # Fetch statistical trends for this bet
                            from betting_trends import get_betting_trends
                            trends = get_betting_trends(league, matchup, bet_type, target_asset, resolved_line)
                            
                            pick_id = save_parsed_pick(
                                raw_post_id, league, matchup, bet_type, target_asset,
                                resolved_line, resolved_odds, trends
                            )
                            try:
                                import kalshi_auto_trader
                                kalshi_auto_trader.evaluate_and_trade(
                                    pick_id, league, matchup, bet_type, target_asset,
                                    resolved_line, resolved_odds, trends
                                )
                            except Exception as tr_err:
                                print(f"⚠️ Kalshi auto-trading error: {tr_err}")
                            
                            pick["resolved_line"] = resolved_line
                            pick["trends"] = trends
                            valid_picks.append(pick)
                        else:
                            print(f"⚠️ Pick skipped. Reason: {msg_reason}")
                            
                    if valid_picks:
                        # Build aggregated Underdog format slip alert message
                        alert_msg = f"hey! @{username} just posted a new slip on {platform}! 🔊\n\n"
                        
                        for p in valid_picks:
                            p_bet_type = p.get("bet_type", "")
                            p_target = p.get("target_asset", "")
                            p_line = p.get("resolved_line")
                            if p_line is None:
                                p_line = p.get("line_value")
                                
                            if "Player Prop" in p_bet_type:
                                direction = "OVER" if "Over" in p_bet_type else "UNDER"
                                
                                # Extract stat type
                                stat_type = "PTS"
                                text_lower = text.lower()
                                asset_lower = p_target.lower()
                                alias = asset_lower.split()[-1] if asset_lower.split() else asset_lower
                                context = text_lower
                                if alias in text_lower:
                                    idx = text_lower.find(alias)
                                    next_idx = len(text_lower)
                                    for other_p in ["tatum", "doncic", "brown", "irving", "mahomes", "ohtani"]:
                                        if other_p != alias and other_p in text_lower:
                                            oidx = text_lower.find(other_p, idx)
                                            if oidx != -1 and oidx < next_idx:
                                                next_idx = oidx
                                    context = text_lower[idx:next_idx]
                                    
                                if any(x in context for x in ["points", "pts", "scoring"]):
                                    stat_type = "PTS"
                                elif any(x in context for x in ["assist", "ast"]):
                                    stat_type = "AST"
                                elif any(x in context for x in ["rebound", "reb"]):
                                    stat_type = "REB"
                                elif any(x in context for x in ["three", "3pm", "3pt", "3pts", "3s", "three pointer"]):
                                    stat_type = "3PM"
                                elif any(x in context for x in ["block", "blk"]):
                                    stat_type = "BLK"
                                elif any(x in context for x in ["steal", "stl"]):
                                    stat_type = "STL"
                                elif any(x in context for x in ["turnover", "to"]):
                                    stat_type = "TO"
                                elif any(x in context for x in ["fantasy points", "fantasy", "fpts"]):
                                    stat_type = "FPTS"
                                elif any(x in context for x in ["touchdown", "tds", "td"]):
                                    stat_type = "TDs"
                                elif any(x in context for x in ["passing yards", "pass yards", "pyds"]):
                                    stat_type = "PASS YDS"
                                elif any(x in context for x in ["rushing yards", "rush yards", "ryds"]):
                                    stat_type = "RUSH YDS"
                                elif any(x in context for x in ["receiving yards", "rec yards"]):
                                    stat_type = "REC YDS"
                                elif any(x in context for x in ["strikeout", "ks", "strikeout"]):
                                    stat_type = "Ks"
                                elif any(x in context for x in ["hits", "hit"]):
                                    stat_type = "HITS"
                                elif any(x in context for x in ["home runs", "hrs", "hr"]):
                                    stat_type = "HRs"
                                else:
                                    # Fallback based on league
                                    p_league = p.get("league", "NBA")
                                    if p_league == "NFL":
                                        if any(qb in asset_lower for qb in ["mahomes", "jackson", "allen", "burrow", "prescott", "rodgers", "hurts"]):
                                            stat_type = "PASS YDS"
                                        else:
                                            stat_type = "YDS"
                                    elif p_league == "MLB":
                                        if any(pitcher in asset_lower for pitcher in ["ohtani", "cole", "scherzer", "verlander", "burnes", "snell"]):
                                            stat_type = "Ks"
                                        else:
                                            stat_type = "HITS"
                                        
                                line_str = f" {p_line}" if p_line is not None else ""
                                alert_msg += f"👉 {p_target} - {direction}{line_str} {stat_type}\n"
                            else:
                                # Non player prop format nicely
                                if p_bet_type == "Moneyline":
                                    alert_msg += f"👉 {p_target} - ML\n"
                                elif p_bet_type == "Spread":
                                    line_str = f" ({p_line})" if p_line is not None else ""
                                    alert_msg += f"👉 {p_target} - Spread{line_str}\n"
                                elif "Total" in p_bet_type:
                                    direction = "OVER" if "Over" in p_bet_type else "UNDER"
                                    line_str = f" {p_line}" if p_line is not None else ""
                                    alert_msg += f"👉 Total {direction}{line_str}\n"
                                else:
                                    line_str = f" ({p_line})" if p_line is not None else ""
                                    alert_msg += f"👉 {p_target} - {p_bet_type}{line_str}\n"
                                    
                        # Extract Underdog link if present in capper's original post text
                        underdog_link = config.get("underdog_promo_link", "https://play.underdogfantasy.com/p-popularparlay")
                        ud_match = re.search(r"(https?://)?(?:play\.)?underdogfantasy\.com/(?:es-|p-)[a-zA-Z0-9_-]+", text)
                        if ud_match:
                            underdog_link = ud_match.group(0)
                            if not underdog_link.startswith("http"):
                                underdog_link = "https://" + underdog_link
                                
                        # Lookup matching Kalshi tickers for interactive buttons and links
                        trade_buttons = []
                        kalshi_links_text = ""
                        try:
                            import kalshi_auto_trader
                            config = kalshi_auto_trader._load_config()
                            use_demo = config.get("kalshi_use_demo", True)
                            trader = kalshi_auto_trader.KalshiDefensiveTrader()
                            markets = trader.fetch_active_markets()
                            
                            for p in valid_picks:
                                p_target = p.get("target_asset", "")
                                p_matchup = p.get("matchup", "")
                                mapping = kalshi_auto_trader.map_event_to_prediction_market(p_target, p_matchup, markets=markets)
                                if mapping.get("mapped"):
                                    ticker = mapping.get("ticker")
                                    base_domain = "demo.kalshi.com" if use_demo else "kalshi.com"
                                    link_url = f"https://{base_domain}/markets/{ticker}"
                                    flag_str = " ⚡ [AUTO-READY]" if mapping.get("flagged_for_execution") else ""
                                    kalshi_links_text += f"🔗 *Kalshi Link*: [{ticker}]({link_url}){flag_str}\n"
                                    
                                    p_bet_type = p.get("bet_type", "")
                                    side_choice = "NO" if "under" in p_bet_type.lower() or "no" in p_bet_type.lower() else "YES"
                                    
                                    trade_buttons.append([
                                        {"text": f"🎯 Snipe 100 {side_choice}", "callback_data": f"kalshi_snipe:100:{side_choice}:{ticker}"},
                                        {"text": f"🔥 Snipe 500 {side_choice}", "callback_data": f"kalshi_snipe:500:{side_choice}:{ticker}"}
                                    ])
                                    trade_buttons.append([
                                        {"text": f"🧮 Snipe Max Kelly", "callback_data": f"kalshi_snipe:kelly:{side_choice}:{ticker}"},
                                        {"text": f"🚫 Pass / Ignore", "callback_data": f"kalshi_snipe:ignore:{side_choice}:{ticker}"}
                                    ])
                        except Exception as e:
                            print(f"⚠️ Error building trade buttons: {e}")

                        from betting_db import format_promo_link
                        alert_msg += "\n" + format_promo_link(underdog_link) + "\n"
                        if kalshi_links_text:
                            alert_msg += "\n" + kalshi_links_text
                            
                        keyboard = None
                        if trade_buttons:
                            keyboard = {"inline_keyboard": trade_buttons}
                            
                        send_telegram(alert_msg, reply_markup=keyboard)
                        send_discord_webhook(alert_msg)
                        send_telegram_webhook(alert_msg)
                        
                        # Also build and send the consensus parlay of the day if compiled
                        parlay = build_consensus_parlay()
                        if parlay:
                            legs = parlay.get("legs", [])
                            odds_val = parlay.get("total_odds_american", -110)
                            odds_str = f"+{odds_val}" if odds_val > 0 else str(odds_val)
                            implied_prob = parlay.get("implied_probability_percent", 0.0)
                            consensus = parlay.get("consensus_level", 0.0)
                            
                            p_msg = (
                                f"🏆 *NEW CONSENSUS PARLAY COMPILED* 🏆\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📊 *Parlay Payout Odds*: `{odds_str}`\n"
                                f"🔮 *Implied Probability*: `{implied_prob}%`\n"
                                f"👥 *Avg Consensus Level*: `{consensus} cappers/leg`\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                                f"🔥 *Parlay Legs*:\n"
                            )
                            
                            for idx, leg in enumerate(legs, 1):
                                leg_line_str = f" ({leg['line_value']})" if leg.get("line_value") is not None else ""
                                leg_odds = leg.get("odds", -110)
                                leg_odds_str = f"+{leg_odds}" if leg_odds > 0 else str(leg_odds)
                                cappers_list = []
                                for c, pk_id in zip(leg["cappers"], leg.get("raw_keys", [])):
                                    from betting_db import get_underdog_link_for_pick
                                    ud_link = get_underdog_link_for_pick(pk_id)
                                    if ud_link:
                                        cappers_list.append(f"@{c} ([Slip]({ud_link}))")
                                    else:
                                        cappers_list.append(f"@{c}")
                                cappers_str = ", ".join(cappers_list)
                                
                                p_msg += (
                                    f"*{idx}. {leg['league']}*: {leg['matchup']}\n"
                                    f"   🎯 *Pick*: `{leg['target_asset']} - {leg['bet_type']}{leg_line_str}`\n"
                                    f"   📈 *Odds*: `{leg_odds_str}`\n"
                                )
                                leg_trends = leg.get("trends", [])
                                if leg_trends:
                                    p_msg += f"   📊 *Trend*: {leg_trends[0]}\n"
                                p_msg += f"   Backed by: {cappers_str}\n\n"
                                
                            from betting_db import format_promo_link
                            p_msg += (
                                format_promo_link(underdog_link) + "\n\n"
                                f"⚠️ _Verify odds availability on sportsbooks before placing wagers._"
                            )
                            send_telegram(p_msg)
                            send_discord_webhook(p_msg)
                            send_telegram_webhook(p_msg)
                            
                            # Execute live bet based on high-conviction combo signals
                            try:
                                import kalshi_auto_trader
                                c_matchup = " | ".join(set([leg['matchup'] for leg in parlay.get('legs', [])]))
                                c_assets = ", ".join([leg['target_asset'] for leg in parlay.get('legs', [])])
                                print(f"🤖 TrailTheProsAi Triggering Live Combo: {c_assets}")
                                kalshi_auto_trader.evaluate_and_trade(
                                    pick_id=int(time.time()),
                                    league="Consensus Parlay",
                                    matchup=c_matchup,
                                    bet_type="Parlay",
                                    target_asset=c_assets,
                                    resolved_line=None,
                                    resolved_odds=parlay.get("total_odds_american", -110),
                                    trends="[]"
                                )
                            except Exception as kalshi_err:
                                print(f"⚠️ TrailTheProsAi Execution Failed: {kalshi_err}")
            except Exception as parsing_err:
                print(f"⚠️ Failed to execute sports parsing pipeline: {parsing_err}")
                
    # 5. Save updated seen posts list
    state["seen_posts"] = new_seen[-500:]
    _wj(STATE_FILE, state)

# ── Main Run Loop ─────────────────────────────────────────────────────────────
async def run_async_scraper_loop():
    print("⚡ Starting background Real-Time Timeline Scraping task...")
    while True:
        try:
            config = _rj(CONFIG_FILE)
            if not config.get("scanner_active", True):
                await asyncio.sleep(15)
                continue
                
            monitored_x = config.get("monitored_accounts_x", [])
            mode = config.get("twitter_scraping_mode", "scraping_simulation")
            nitter_instances = config.get("nitter_instances", [])
            bearer_token = config.get("bearer_token", "")
            apify_api_token = config.get("apify_api_token", "")
            apify_twitter_actor = config.get("apify_twitter_actor", "apify/twitter-scraper")
            
            print(f"⏱️ [High-Freq Scraper] Polling {len(monitored_x)} X timelines in mode '{mode}'...")
            
            all_new_posts = []
            if mode == "scraping_simulation":
                all_new_posts.extend(await asyncio.to_thread(fetch_mock_posts))
            elif mode != "webhook_only":
                for username in monitored_x:
                    if mode == "rss_feed":
                        posts = await asyncio.to_thread(fetch_x_rss, username, nitter_instances)
                        all_new_posts.extend(posts)
                    elif mode == "official_api":
                        posts = await asyncio.to_thread(fetch_official_x_api, username, bearer_token)
                        all_new_posts.extend(posts)
                    elif mode == "apify":
                        posts = await asyncio.to_thread(fetch_apify_tweets, username, apify_api_token, apify_twitter_actor)
                        all_new_posts.extend(posts)
                    elif mode == "playwright":
                        posts = await asyncio.to_thread(fetch_playwright_posts, username, "x")
                        all_new_posts.extend(posts)
                        
            if all_new_posts:
                webhook_queue_file = "social_trail_webhook_queue.json"
                existing_queue = _rj(webhook_queue_file, [])
                existing_ids = {str(p.get("post_id")) for p in existing_queue}
                
                added_count = 0
                for p in all_new_posts:
                    p_id = str(p.get("post_id"))
                    if p_id not in existing_ids:
                        existing_queue.append(p)
                        existing_ids.add(p_id)
                        added_count += 1
                        
                if added_count > 0:
                    print(f"📥 [High-Freq Scraper] Enqueued {added_count} new posts into webhook queue.")
                    _wj(webhook_queue_file, existing_queue)
                    
        except Exception as e:
            print(f"❌ Error in background scraping loop: {e}")
            
        await asyncio.sleep(20)

async def main():
    print("==============================================")
    print("📢 X & Instagram Social Trail Bot — LIVE (Async)")
    print("==============================================")
    
    # Start engines in background thread (KalshiDefensiveTrader init is blocking)
    def _start_engines():
        try:
            from kalshi_position_manager import PositionManager
            PositionManager().start()
            print("✅ Kalshi Trailing Take-Profit Manager successfully attached.", flush=True)
        except Exception as e:
            print(f"⚠️ Failed to attach Kalshi Position Manager: {e}", flush=True)
        
        try:
            from kalshi_autonomous_scanner import AutonomousScanner
            AutonomousScanner().start()
            print("✅ Kalshi 24/7 Autonomous ALL-MARKET Scanner successfully attached.", flush=True)
        except Exception as e:
            print(f"⚠️ Failed to attach Autonomous Scanner: {e}", flush=True)
    
    import threading
    threading.Thread(target=_start_engines, daemon=True).start()
    
    # Non-blocking Telegram notification
    try:
        import threading
        threading.Thread(target=send_telegram, args=(
            "🐦 *Social Trail Bot LIVE*\n"
            "🟢 Monitoring X & Instagram for token calls.\n"
            "⚡ Auto-buy triggers ready.\n"
            "🤖 24/7 Autonomous Scanner: ACTIVE\n"
            "🛡️ Trailing Take-Profit Manager: ACTIVE",
        ), daemon=True).start()
    except:
        pass
    
    # Start high frequency timeline scraping task
    asyncio.create_task(run_async_scraper_loop())
    
    while True:
        try:
            await asyncio.to_thread(process_posts)
        except Exception as e:
            print(f"❌ Error in process loop: {e}")
            
        # Poll process_posts every 10 seconds to pick up webhook enqueued posts
        await asyncio.sleep(10)

if __name__ == "__main__":
    asyncio.run(main())
