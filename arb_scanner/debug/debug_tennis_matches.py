"""
debug_tennis_matches.py — Fetch live Kalshi + Polymarket tennis markets and output all matches.

Run from arb_scanner/:
  python3 debug/debug_tennis_matches.py
"""

import asyncio
import json
import os
import sys

import aiohttp
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from matchers.tennis import TennisMatcher, normalize_name
from fees import kalshi_taker_fee, polymarket_taker_fee

load_dotenv()

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)
OUTPUT_FILE = "debug/tennis_matches.json"


async def fetch_kalshi_tennis(session: aiohttp.ClientSession) -> list[dict]:
    markets = []
    for ticker in ("KXATPMATCH", "KXWTAMATCH"):
        url = f"{KALSHI_BASE}/markets"
        params = {"series_ticker": ticker, "status": "open", "limit": 200}
        try:
            async with session.get(url, params=params, timeout=REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as e:
            print(f"[WARN] Kalshi {ticker} fetch failed: {e}", file=sys.stderr)
            continue

        for m in data.get("markets", []):
            try:
                t_ticker = m.get("ticker", "")
                title = m.get("title", "") or m.get("subtitle", "") or t_ticker
                if m.get("yes_ask_dollars") is not None:
                    yes_ask = float(m["yes_ask_dollars"])
                elif m.get("yes_ask") is not None:
                    yes_ask = float(m["yes_ask"]) / 100.0
                else:
                    continue
                if yes_ask <= 0 or yes_ask >= 1:
                    continue
                markets.append({
                    "ticker": t_ticker,
                    "title": title,
                    "ask": yes_ask,
                    "taker_fee": round(kalshi_taker_fee(yes_ask), 6),
                })
            except (TypeError, ValueError, KeyError):
                continue

    print(f"[INFO] Kalshi tennis: {len(markets)} markets")
    return markets


async def fetch_polymarket_tennis(session: aiohttp.ClientSession) -> list[dict]:
    markets = []
    for slug in ("atp", "wta"):
        try:
            async with session.get(
                f"{POLYMARKET_US_GATEWAY}/v2/leagues/{slug}/events",
                params={"limit": 100},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception as e:
            print(f"[WARN] Polymarket {slug} fetch failed: {e}", file=sys.stderr)
            continue

        for event in data.get("events", []):
            for m in event.get("markets", []):
                try:
                    if m.get("sportsMarketType") != "tennis_match_winner":
                        continue
                    if not m.get("active") or m.get("closed") or m.get("archived"):
                        continue
                    for side in m.get("marketSides", []):
                        team = side.get("team", {})
                        team_abbr = (team.get("abbreviation", "") or side.get("participant", "")).lower()
                        team_name = normalize_name(
                            team.get("displayName", "") or team.get("name", "") or team_abbr
                        )
                        quote = side.get("quote", {})
                        ask = float(quote.get("value", 0)) if quote else 0.0
                        if ask <= 0 or ask >= 1:
                            continue
                        markets.append({
                            "slug": m.get("slug", ""),
                            "title": m.get("question") or m.get("slug", ""),
                            "team_abbr": team_abbr,
                            "team_name": team_name,
                            "ask": ask,
                            "taker_fee": round(polymarket_taker_fee(ask), 6),
                        })
                except (TypeError, ValueError, KeyError):
                    continue

    print(f"[INFO] Polymarket tennis: {len(markets)} sides")
    return markets


def match_markets(kalshi_markets: list[dict], poly_markets: list[dict]) -> tuple[list, list]:
    matcher = TennisMatcher()
    matches = matcher.match(kalshi_markets, poly_markets)
    matched_tickers = {m["kalshi_ticker"] for m in matches}
    unmatched_kalshi = [
        {"ticker": km["ticker"], "title": km["title"], "reason": "no polymarket side matched"}
        for km in kalshi_markets
        if km["ticker"] not in matched_tickers
    ]
    return matches, unmatched_kalshi


async def main():
    async with aiohttp.ClientSession() as session:
        kalshi_markets, poly_markets = await asyncio.gather(
            fetch_kalshi_tennis(session),
            fetch_polymarket_tennis(session),
        )

    matches, unmatched = match_markets(kalshi_markets, poly_markets)

    arb_count = sum(1 for m in matches if m["is_arb"])
    print(f"[INFO] Matches found: {len(matches)} | Arbs: {arb_count} | Unmatched Kalshi: {len(unmatched)}")

    output = {
        "kalshi_count": len(kalshi_markets),
        "polymarket_count": len(poly_markets),
        "match_count": len(matches),
        "arb_count": arb_count,
        "matches": matches,
        "unmatched_kalshi": unmatched,
        "kalshi_raw": kalshi_markets,
        "polymarket_raw": poly_markets,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(output, f, indent=2)

    print(f"[INFO] Written to {OUTPUT_FILE}")

    if matches:
        print("\n--- MATCHES ---")
        for m in matches:
            arb_tag = " *** ARB ***" if m["is_arb"] else ""
            print(
                f"  {m['kalshi_ticker']} ({m['kalshi_team']}) <-> "
                f"{m['polymarket_slug']} ({m['polymarket_team']}) "
                f"cost={m['total_cost']:.4f} gap={m['gap_cents']:+.2f}c{arb_tag}"
            )

    if unmatched:
        print(f"\n--- UNMATCHED KALSHI ({len(unmatched)}) ---")
        for u in unmatched[:20]:
            print(f"  {u['ticker']}: {u['reason']}")
        if len(unmatched) > 20:
            print(f"  ... and {len(unmatched) - 20} more")


if __name__ == "__main__":
    asyncio.run(main())
