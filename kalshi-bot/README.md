# Kalshi Bot Suite

Multi-module automated trading system spanning Kalshi prediction markets,
Polymarket, Solana/Base memecoin sniping, and sports betting parlay building.

## File Map

| File | Role |
|---|---|
| `brain.py` | Central telemetry hub (Sentinel Sam) — profit reporting, drawdown guard, Telegram/ClickUp forwarding |
| `kalshi_auto_trader.py` | Kalshi V2 institutional-grade auto-trader with circuit breakers, drawdown kill-switch, idempotency |
| `fomc_minutes_sniper.py` | FOMC minutes event sniper — live RSS polling → sentiment → Kalshi rate-decision orders |
| `social_trail_bot.py` | X/Instagram copy-trading — monitors cappers, extracts contract addresses, triggers buys |
| `social_sentiment_sniper.py` | Sharp tweet sentiment analysis for Kalshi sports betting |
| `poly_trader.py` | Polymarket CLOB client — buy/sell/maker/cancel orders |
| `solana_sniper_bot.py` | Solana new token sniper — mint/freeze safety checks, Jupiter swaps |
| `dex_scanner_bot.py` | Base chain new token scanner — GoPlus safety checks, Uniswap swaps |
| `live_trade_scanner.py` | Live terminal dashboard — polls DexScreener, shows scan results in real time |
| `ai_predictive_model.py` | NEO/BullX-inspired token scoring (0–100) for EVM and Solana |
| `web_dashboard.py` | Flask web dashboard — system health, picks, parlays, bot controls |
| `watchdog.py` | Self-healing watchdog — monitors all processes, auto-restarts crashed bots |
| `betting_db.py` | SQLite layer — cappers, raw posts, parsed picks, parlays |
| `betting_parser.py` | NLP pick parser — Gemini/OpenAI or regex fallback |
| `betting_aggregator.py` | Consensus parlay builder — conflict resolution, odds calculation |
| `betting_trends.py` | Trend generator — OpenAI/Gemini or local template fallback |
| `betting_validation.py` | Pick validator — season guard, roster matching, The Odds API |
| `core_interfaces.py` | Abstract interfaces for swap adapters, alert providers, safety checkers |
| `v2_logger.py` | Logging shim — patches print() with UTC timestamps (replaces Termux logger) |
| `auto_runner.py` | Gemini API connection test |

## Required Secrets (Replit Secrets panel)

| Secret | Used by |
|---|---|
| `KALSHI_API_KEY` ✅ | kalshi_auto_trader, fomc_minutes_sniper |
| `KALSHI_API_SECRET` | kalshi_auto_trader (RSA signing) |
| `TELEGRAM_BOT_TOKEN` | brain, watchdog, social_trail_bot, kalshi_auto_trader |
| `TELEGRAM_CHAT_ID` | brain, watchdog, social_trail_bot |
| `GEMINI_API_KEY` | betting_parser, betting_trends, auto_runner |
| `OPENAI_API_KEY` | betting_parser, betting_trends |
| `THE_ODDS_API_KEY` | betting_validation |
| `POLYMARKET_PRIVATE_KEY` | poly_trader |
| `METAMASK_PRIVATE_KEY` | poly_trader |

See `.env.example` for the full list including optional integrations.

## Running individual bots

All scripts should be run from the `kalshi-bot/` directory so relative paths
(config files, `betting_system.db`, log files) resolve correctly:

```bash
cd kalshi-bot

# Core trading engine
python3 kalshi_auto_trader.py

# Event sniper (FOMC, etc.)
python3 fomc_minutes_sniper.py

# Sports betting pipeline
python3 social_trail_bot.py

# Live scanner dashboard
python3 live_trade_scanner.py

# Web dashboard (runs on port 5000)
python3 web_dashboard.py

# Watchdog (start last — monitors all others)
python3 watchdog.py
```

## Architecture notes

- `brain.py` is the only cross-cutting dependency — imported by `kalshi_auto_trader`,
  `fomc_minutes_sniper`, and `social_sentiment_sniper`.
- Kalshi trading is fully decoupled from Solana/Base sniping — daily drawdowns,
  rate limits, and state are independent.
- SQLite database (`betting_system.db`) is the shared state store for the sports
  betting pipeline (parser → aggregator → validator → Telegram parlay push).
- `v2_logger.py` replaces the Termux-only logging module — no code changes needed.
