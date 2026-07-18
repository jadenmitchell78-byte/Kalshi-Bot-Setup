"""
Kalshi Esports Tracker
======================
Pulls live esports markets from Kalshi (CS2, LoL, Valorant, Dota 2,
Overwatch, Call of Duty), optionally enriches with team context, and
writes a JSON snapshot to disk for another program to consume.

This tool ONLY reads public market data. It does not place trades,
does not require an API key, and does not touch your Kalshi account.

Usage:
    python main.py                 # run once
    python main.py --loop 300      # run every 300 seconds, forever
    python main.py --no-enrich     # skip Liquipedia lookups (faster)

Output:
    ./output/snapshot_<UTC timestamp>.json   (one per run)
    ./output/latest.json                      (always overwritten - point your bot at this one)
"""
from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_client import KalshiClient
from stats_providers import LiquipediaStatsProvider, NullStatsProvider

OUTPUT_DIR = Path(__file__).parent / "output"

# Very rough best-effort parser for "Team A vs Team B" / "Team A def. Team B"
# style titles. Kalshi's exact title format can vary by series - if this
# doesn't match, we still keep the raw title so your bot can parse it
# however it needs to.
VS_PATTERN = re.compile(r"(.+?)\s+(?:vs\.?|v\.?|def\.?)\s+(.+)", re.IGNORECASE)


def extract_teams(title: str) -> tuple[str | None, str | None]:
    m = VS_PATTERN.search(title or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def implied_prob(yes_bid: int | None, yes_ask: int | None) -> float | None:
    """Kalshi prices are in cents (1-99). Use mid-price as the implied
    probability estimate. Returns None if there's no live quote."""
    if yes_bid is None or yes_ask is None:
        return None
    if yes_bid == 0 and yes_ask == 0:
        return None
    return round((yes_bid + yes_ask) / 2 / 100, 4)


def build_snapshot(client: KalshiClient, stats_provider, games_filter: set[str] | None) -> dict:
    series_list = client.find_esports_series()
    if games_filter:
        series_list = [s for s in series_list if s["_game"] in games_filter]

    matches = []
    for series in series_list:
        ticker = series.get("ticker")
        game = series["_game"]
        try:
            markets = list(client.iter_markets(series_ticker=ticker, status="open"))
        except RuntimeError as e:
            print(f"[warn] failed to fetch markets for series {ticker}: {e}")
            continue

        for m in markets:
            title = m.get("title", "")
            team_a, team_b = extract_teams(title)

            record = {
                "game": game,
                "series_ticker": ticker,
                "market_ticker": m.get("ticker"),
                "title": title,
                "team_a": team_a,
                "team_b": team_b,
                "status": m.get("status"),
                "close_time": m.get("close_time"),
                "yes_bid_cents": m.get("yes_bid"),
                "yes_ask_cents": m.get("yes_ask"),
                "implied_yes_probability": implied_prob(m.get("yes_bid"), m.get("yes_ask")),
                "volume": m.get("volume"),
                "open_interest": m.get("open_interest"),
                "liquidity": m.get("liquidity"),
            }

            if stats_provider is not None:
                if team_a:
                    record["team_a_context"] = stats_provider.enrich(game, team_a)
                if team_b:
                    record["team_b_context"] = stats_provider.enrich(game, team_b)

            matches.append(record)

    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source": "kalshi_public_market_data",
        "match_count": len(matches),
        "matches": matches,
    }


def run_once(games_filter: set[str] | None, enrich: bool, liquipedia_api_key: str | None):
    OUTPUT_DIR.mkdir(exist_ok=True)
    client = KalshiClient()
    stats_provider = LiquipediaStatsProvider(api_key=liquipedia_api_key) if enrich else NullStatsProvider()

    snapshot = build_snapshot(client, stats_provider, games_filter)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = OUTPUT_DIR / f"snapshot_{ts}.json"
    latest_path = OUTPUT_DIR / "latest.json"

    out_path.write_text(json.dumps(snapshot, indent=2))
    latest_path.write_text(json.dumps(snapshot, indent=2))

    print(f"[ok] wrote {snapshot['match_count']} matches -> {out_path.name} (and latest.json)")


def main():
    parser = argparse.ArgumentParser(description="Kalshi esports market tracker")
    parser.add_argument("--loop", type=int, default=0, help="Repeat every N seconds. 0 = run once.")
    parser.add_argument("--no-enrich", action="store_true", help="Skip Liquipedia team-context lookups.")
    parser.add_argument(
        "--games",
        type=str,
        default="",
        help="Comma-separated subset of: cs2,lol,valorant,dota2,overwatch,cod. Default: all.",
    )
    parser.add_argument("--liquipedia-key", type=str, default=None, help="Optional Liquipedia API key.")
    args = parser.parse_args()

    games_filter = set(g.strip() for g in args.games.split(",") if g.strip()) or None

    if args.loop <= 0:
        run_once(games_filter, enrich=not args.no_enrich, liquipedia_api_key=args.liquipedia_key)
        return

    print(f"[info] looping every {args.loop}s - Ctrl+C to stop")
    while True:
        try:
            run_once(games_filter, enrich=not args.no_enrich, liquipedia_api_key=args.liquipedia_key)
        except Exception as e:
            print(f"[error] run failed: {e}")
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
