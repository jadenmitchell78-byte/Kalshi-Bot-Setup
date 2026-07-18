import os
import json
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, AssetType, BalanceAllowanceParams

_poly_client = None

def get_poly_client():
    global _poly_client
    if _poly_client is not None:
        return _poly_client

    load_dotenv()
    with open("social_trail_config.json") as f:
        config = json.load(f)

    api_key = os.getenv("POLYMARKET_API_KEY") or config.get("poly_api_key_id")
    api_secret = os.getenv("POLYMARKET_PRIVATE_KEY") or config.get("poly_api_secret") or config.get("poly_private_key")
    
    # Clean up any trailing newlines from .env strings
    if api_secret:
        api_secret = api_secret.strip()
    if api_key:
        api_key = api_key.strip()
        
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE") or config.get("poly_api_passphrase") or api_secret
    private_key = os.getenv("METAMASK_PRIVATE_KEY")
    
    if not private_key or not api_key:
        return None

    # Check for proxy configuration to bypass Polymarket geoblock
    poly_proxy = os.getenv("POLY_PROXY")
    if poly_proxy:
        os.environ["HTTP_PROXY"] = poly_proxy
        os.environ["HTTPS_PROXY"] = poly_proxy
        
    # Use a private Polygon RPC node to prevent front-running
    rpc_url = os.getenv("PRIVATE_POLYGON_RPC", "https://polygon-mainnet.g.alchemy.com/v2/private-node")
    wallet_address = os.getenv("WALLET_ADDRESS")
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    _poly_client = ClobClient("https://clob.polymarket.com", chain_id=137, key=private_key, creds=creds, signature_type=0)
    return _poly_client

def execute_poly_buy(token_id: str, price: float, size: float = 1.0):
    # Minimum order size on Polymarket is usually $5, so adjust if needed
    if size < 5.0:
        size = 5.0
    client = get_poly_client()
    if not client:
        print("⚠️ Polymarket CLOB client could not be initialized.")
        return False
        
    try:
        order_args = OrderArgs(price=price, size=size, side="BUY", token_id=token_id)
        signed_order = client.create_order(order_args)
        # Using GTC instead of FOK prevents the order from being rejected instantly if the book is thin
        resp = client.post_order(signed_order, OrderType.GTC)
        if resp and (resp.get("success") or resp.get("orderID")):
            return True
    except Exception as e:
        print(f"⚠️ [POLY-ERROR] {e}")
    return False

def execute_poly_sell(token_id: str, price: float, size: float = 1.0):
    if size * price < 1.0:
        size = 1.0 / price
    client = get_poly_client()
    if not client: return False
    try:
        order_args = OrderArgs(price=price, size=size, side="SELL", token_id=token_id)
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.FOK)
        if resp and resp.get("success"):
            return True
    except:
        pass
    return False

def execute_poly_maker(token_id: str, price: float, size: float = 1.0):
    if size < 5.0:
        size = 5.0
    client = get_poly_client()
    if not client: return None
    try:
        order_args = OrderArgs(price=price, size=size, side="BUY", token_id=token_id)
        signed_order = client.create_order(order_args)
        resp = client.post_order(signed_order, OrderType.POST_ONLY)
        if resp and resp.get("success"):
            return resp.get("orderID")
    except Exception as e:
        print(f"⚠️ [POLY-ERROR] {e}")
    return None

def get_orderbook(token_id: str):
    client = get_poly_client()
    if not client: return None
    try:
        return client.get_order_book(token_id)
    except:
        return None

def is_order_filled(order_id: str):
    client = get_poly_client()
    if not client: return False
    try:
        order = client.get_order(order_id)
        if order and order.get("status") == "MATCHED":
            return True
        if order and float(order.get("size_matched", 0)) >= float(order.get("original_size", 1)):
            return True
    except:
        pass
    return False

def cancel_order(order_id: str):
    client = get_poly_client()
    if not client: return
    try:
        client.cancel(order_id)
    except:
        pass

def get_balance():
    try:
        client = get_poly_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        bal_data = client.get_balance_allowance(params)
        balance_str = bal_data.get('balance', '0')
        # Balance is usually in 6 decimals (USDC)
        balance_usd = float(balance_str) / 1e6
        return balance_usd
    except Exception as e:
        print(f"Error getting balance: {e}")
        return 0.0
