"""
debug_tennis_matches.py — Fetch live Kalshi + Polymarket tennis markets and output all matches.

Run from arb_scanner/:
  python3 debug/debug_tennis_matches.py
"""

import asyncio
import json
import os
import sys
import unicodedata
import re

import aiohttp
from dotenv import load_dotenv

load_dotenv()

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)
KALSHI_TAKER_FEE_RATE = 0.01
POLYMARKET_TAKER_FEE_RATE = 0.01
OUTPUT_FILE = "debug/tennis_matches.json"

_SUBJECT_RE = re.compile(r"^will (.+?) win the ", re.IGNORECASE)


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_subject(title: str):
    m = _SUBJECT_RE.match(title.strip())
    return normalize_name(m.group(1)) if m else None


def _names_match(subject: str, pm_name: str) -> bool:
    if not subject or not pm_name:
        return False
    last_name = subject.split()[-1]
    if len(last_name) < 4:
        return subject == pm_name
    return bool(re.search(r"\b" + re.escape(last_name) + r"\b", pm_name))


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
                    "taker_fee": round(yes_ask * KALSHI_TAKER_FEE_RATE, 6),
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
                            "taker_fee": round(ask * POLYMARKET_TAKER_FEE_RATE, 6),
                        })
                except (TypeError, ValueError, KeyError):
                    continue

    print(f"[INFO] Polymarket tennis: {len(markets)} sides")
    return markets


def match_markets(kalshi_markets: list[dict], poly_markets: list[dict]) -> tuple[list, list]:
    matches = []
    unmatched_kalshi = []

    for km in kalshi_markets:
        subject = _extract_subject(km["title"])
        if not subject:
            unmatched_kalshi.append({"ticker": km["ticker"], "title": km["title"], "reason": "no subject extracted"})
            continue

        matched = False
        for pm in poly_markets:
            pm_name = pm.get("team_name", "") or pm.get("team_abbr", "")
            if not _names_match(subject, pm_name):
                continue

            total_cost = km["ask"] + pm["ask"] + km["taker_fee"] + pm["taker_fee"]
            matches.append({
                "kalshi_ticker": km["ticker"],
                "kalshi_title": km["title"],
                "kalshi_player": subject,
                "kalshi_ask": round(km["ask"], 6),
                "polymarket_slug": pm["slug"],
                "polymarket_title": pm["title"],
                "polymarket_player": pm_name,
                "polymarket_ask": round(pm["ask"], 6),
                "total_cost": round(total_cost, 6),
                "gap_cents": round((0.96 - total_cost) * 100, 4),
                "is_arb": total_cost < 0.96,
            })
            matched = True

        if not matched:
            unmatched_kalshi.append({
                "ticker": km["ticker"],
                "title": km["title"],
                "subject_extracted": subject,
                "reason": "no polymarket side matched",
            })

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
                f"  {m['kalshi_ticker']} ({m['kalshi_player']}) <-> "
                f"{m['polymarket_slug']} ({m['polymarket_player']}) "
                f"cost={m['total_cost']:.4f} gap={m['gap_cents']:+.2f}c{arb_tag}"
            )

    if unmatched:
        print(f"\n--- UNMATCHED KALSHI ({len(unmatched)}) ---")
        for u in unmatched[:20]:
            print(f"  {u['ticker']}: {u.get('subject_extracted', '?')} — {u['reason']}")
        if len(unmatched) > 20:
            print(f"  ... and {len(unmatched) - 20} more")


if __name__ == "__main__":
    asyncio.run(main())
