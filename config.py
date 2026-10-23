import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
load_dotenv(BASE_DIR.parent / ".env")

# Credentials & API Keys
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
KALSHI_API_KEY: str = os.getenv("KALSHI_API_KEY", "")
KALSHI_PRIVATE_KEY: str = os.getenv("KALSHI_PRIVATE_KEY", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "")
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")

# API Endpoints
KALSHI_BASE_URL: str = os.getenv(
    "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
)
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# ESPN Hidden Public Scoreboard Endpoints
ESPN_ENDPOINTS = {
    "NBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
    "WNBA": "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard",
    "NFL": "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
    "MLB": "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
    "TENNIS": "https://site.api.espn.com/apis/site/v2/sports/tennis/atp/scoreboard",
}

# Core Monte Carlo & Kelly Constants
MC_ITERATIONS: int = int(os.getenv("MC_ITERATIONS", "10000"))
MIN_EV_EDGE: float = float(os.getenv("MIN_EV_EDGE", "0.035"))        # 3.5% minimum edge
HIGH_CONVICTION_EV_PCT: float = float(os.getenv("HIGH_CONVICTION_EV_PCT", "6.0"))
MODERATE_EDGE_EV_PCT: float = float(os.getenv("MODERATE_EDGE_EV_PCT", "3.5"))
KELLY_FRACTION: float = float(os.getenv("KELLY_FRACTION", "0.25"))   # Quarter-Kelly
MAX_SLIP_LEGS: int = int(os.getenv("MAX_SLIP_LEGS", "3"))
MAX_PICK6_LEGS: int = int(os.getenv("MAX_PICK6_LEGS", "6"))
MIN_SLIP_LEGS: int = int(os.getenv("MIN_SLIP_LEGS", "2"))

# Bankroll & Unit Sizing ($1009.00 starting SIM cash, 1.00u = 1% of bankroll = $10.00)
DEFAULT_MODE: str = os.getenv("DEFAULT_MODE", "SIM").upper()
DEFAULT_AUTO: bool = os.getenv("DEFAULT_AUTO", "true").lower() in ("1", "true", "yes", "on")
DEFAULT_SIM_CASH: float = float(os.getenv("DEFAULT_SIM_CASH", "1009.00"))
BANKROLL_USD: float = float(os.getenv("BANKROLL_USD", "1009.00"))
UNIT_SIZE_USD: float = float(os.getenv("UNIT_SIZE_USD", "10.00"))
MAX_STAKE_PCT: float = float(os.getenv("MAX_STAKE_PCT", "0.05"))
MIN_STAKE_USD: float = float(os.getenv("MIN_STAKE_USD", "5.00"))

# Liquidity Floor, Anti-Snipe Shield & RFQ Risk Parameters
MIN_LIQUID_VOLUME: int = int(os.getenv("MIN_LIQUID_VOLUME", "500"))
ANTI_SNIPE_MAX_LATENCY_MS: float = float(os.getenv("ANTI_SNIPE_MAX_LATENCY_MS", "2200.0"))
TELEMETRY_STALE_SEC: float = float(os.getenv("TELEMETRY_STALE_SEC", "90.0"))
STALE_ORDER_TTL_SEC: int = int(os.getenv("STALE_ORDER_TTL_SEC", "45"))
MAX_SPREAD_CENTS: int = int(os.getenv("MAX_SPREAD_CENTS", "8"))

# Web Server Port
WEB_PORT: int = int(os.getenv("PEEZY_WEB_PORT", "8787"))

# Background Polling Intervals
AUTO_SCAN_INTERVAL_SEC: int = int(os.getenv("AUTO_SCAN_INTERVAL_SEC", "35"))
SETTLE_POLL_INTERVAL_SEC: int = int(os.getenv("SETTLE_POLL_INTERVAL_SEC", "60"))

# SQLite Database Path
DB_PATH: Path = Path(os.getenv("PEEZY_DB_PATH", str(BASE_DIR / "picks.db")))

# Sport Emoji Map
SPORT_EMOJIS = {
    "NBA": "🏀",
    "WNBA": "🏀",
    "NFL": "🏈",
    "MLB": "⚾",
    "TENNIS": "🎾",
    "ESPORTS": "🎮",
    "OTHER": "🎯",
}
