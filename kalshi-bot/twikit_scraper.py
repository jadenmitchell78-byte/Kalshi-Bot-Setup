"""
twikit_scraper.py — Authenticated Twitter/X scraping via twikit.

Login flow:
  1. First run: logs in with X_USERNAME / X_EMAIL / X_PASSWORD env secrets,
     saves session cookies to twikit_cookies.json.
  2. Subsequent runs: loads saved cookies (no password re-used).

Call fetch_tweets(username) → list of post dicts compatible with social_trail_bot.
"""

import os
import json
import asyncio
import logging

logger = logging.getLogger(__name__)

COOKIE_FILE = os.path.join(os.path.dirname(__file__), "twikit_cookies.json")
_client = None  # module-level singleton


def _patch_client_transaction():
    """
    Monkey-patch twikit's ClientTransaction to skip the broken
    KEY_BYTE JS extraction (Twitter changed their JS obfuscation).
    We bypass it by providing a no-op init and a random transaction ID.
    """
    import base64
    import random

    try:
        import twikit.x_client_transaction.transaction as txn
        import bs4

        async def _noop_init(self, session, headers):
            self.home_page_response = bs4.BeautifulSoup("<html/>", "html.parser")
            self.key = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa="
            self.key_bytes = [0] * 32
            self.animation_key = "0"
            self.DEFAULT_ROW_INDEX = 0
            self.DEFAULT_KEY_BYTES_INDICES = [0] * 16

        def _dummy_tid(self, method, path, **kwargs):
            return base64.b64encode(random.randbytes(32)).decode().rstrip("=")

        txn.ClientTransaction.init = _noop_init
        txn.ClientTransaction.generate_transaction_id = _dummy_tid
        logger.debug("twikit ClientTransaction patched OK")
    except Exception as e:
        logger.warning(f"ClientTransaction patch failed (non-fatal): {e}")


async def _build_client():
    """Create and authenticate a twikit Client. Returns the client."""
    _patch_client_transaction()

    import twikit

    # Residential proxy bypasses Twitter's Cloudflare block on GCP/AWS IPs.
    # Set PROXY_URL as: http://user:pass@host:port  (or socks5://...)
    proxy = os.environ.get("PROXY_URL") or None
    if proxy:
        print(f"🔀 [twikit] Using proxy: {proxy.split('@')[-1]}")  # hide credentials
    else:
        print("⚠️  [twikit] No PROXY_URL set — login may fail from cloud IPs (Twitter blocks GCP/AWS).")

    client = twikit.Client(language="en-US", proxy=proxy)

    if os.path.exists(COOKIE_FILE):
        try:
            client.load_cookies(COOKIE_FILE)
            # Quick verification: fetch own user info
            await client.user()
            print("✅ [twikit] Session cookies loaded — no re-login needed.")
            return client
        except Exception as e:
            print(f"⚠️ [twikit] Saved cookies expired or invalid ({e}). Re-logging in...")

    # Fresh login
    username = os.environ.get("X_USERNAME", "")
    email    = os.environ.get("X_EMAIL", "")
    password = os.environ.get("X_PASSWORD", "")

    if not (username and password):
        raise RuntimeError(
            "twikit login requires X_USERNAME and X_PASSWORD env secrets. "
            "Set them in Replit Secrets."
        )

    print(f"🔐 [twikit] Logging into @{username}…")
    await client.login(
        auth_info_1=username,
        auth_info_2=email,
        password=password,
    )
    client.save_cookies(COOKIE_FILE)
    print(f"✅ [twikit] Login successful. Session saved to {COOKIE_FILE}")
    return client


async def _get_client():
    """Return the module-level singleton client, creating it if needed."""
    global _client
    if _client is None:
        _client = await _build_client()
    return _client


async def _fetch_tweets_async(username: str, count: int = 10) -> list:
    """Fetch recent tweets from @username. Returns list of post dicts."""
    client = await _get_client()

    try:
        user = await client.get_user_by_screen_name(username)
    except Exception as e:
        print(f"⚠️ [twikit] Could not resolve @{username}: {e}")
        return []

    try:
        tweets = await client.get_user_tweets(
            user.id,
            tweet_type="Tweets",
            count=count,
        )
    except Exception as e:
        # Session may have expired mid-run — clear client so next call re-logs in
        global _client
        print(f"⚠️ [twikit] Timeline fetch failed for @{username}: {e}")
        if "401" in str(e) or "Authentication" in str(e) or "cookie" in str(e).lower():
            print("🔄 [twikit] Resetting session — will re-login next poll.")
            _client = None
        return []

    posts = []
    for tweet in tweets:
        posts.append({
            "platform": "x",
            "username": username,
            "post_id": f"x_{tweet.id}",
            "text": tweet.text or tweet.full_text or "",
            "link": f"https://x.com/{username}/status/{tweet.id}",
        })
    return posts


def fetch_tweets(username: str, count: int = 10) -> list:
    """
    Synchronous wrapper around _fetch_tweets_async.
    Safe to call from both sync and async contexts.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # We're inside an async context — schedule on the existing loop
            import concurrent.futures
            future = asyncio.run_coroutine_threadsafe(
                _fetch_tweets_async(username, count), loop
            )
            return future.result(timeout=30)
        else:
            return loop.run_until_complete(_fetch_tweets_async(username, count))
    except Exception as e:
        print(f"⚠️ [twikit] fetch_tweets error for @{username}: {e}")
        return []


def login_once():
    """
    Call this at startup to pre-authenticate before the poll loop starts.
    Blocks until login completes.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_get_client())
        else:
            loop.run_until_complete(_get_client())
        return True
    except Exception as e:
        print(f"❌ [twikit] Startup login failed: {e}")
        return False
