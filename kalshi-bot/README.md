# Kalshi Bot

An 8-part automated trading bot for Kalshi prediction markets.

## Setup

1. **Environment is ready** — Python 3.11 + all dependencies installed.

2. **API key** — `KALSHI_API_KEY` is stored as a Replit Secret (already done).
   Additional secrets (e.g. `KALSHI_API_SECRET`, `TELEGRAM_BOT_TOKEN`) can be
   added via the Secrets panel in Replit.

3. **Local .env (optional)** — If you want a local `.env` for extra variables,
   copy the template and fill it in:
   ```bash
   cp .env.example .env
   ```
   Then load it at the top of each bot file:
   ```python
   from dotenv import load_dotenv
   load_dotenv()
   ```

## Reading your API key in Python

```python
import os
from dotenv import load_dotenv

load_dotenv()  # loads .env if present; Replit Secret is always available

KALSHI_API_KEY = os.environ["KALSHI_API_KEY"]
```

## Key packages available

| Package | Purpose |
|---|---|
| `kalshi-python` | Official Kalshi REST client |
| `pykalshi` | Lightweight Kalshi wrapper |
| `websockets` | Real-time market data streams |
| `aiohttp` | Async HTTP |
| `fastapi` + `uvicorn` | Web server / webhook receiver |
| `python-telegram-bot` | Trade alerts via Telegram |
| `python-dotenv` | Load `.env` variables locally |
| `requests` | Simple HTTP calls |
| `web3` | On-chain / Polymarket integration |

## Excluded packages

| Package | Reason |
|---|---|
| `aider-chat` | Depends on `litellm` (blocked) |
| `litellm` | Blocked by Replit package firewall |

## Run a bot part

```bash
python3 kalshi-bot/part1.py
```
