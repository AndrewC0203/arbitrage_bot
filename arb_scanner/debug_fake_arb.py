"""
debug_fake_arb.py — one-shot diagnostic for fake_arb matching.
Fetches live data and prints what's coming in and what's (not) matching.
"""
import asyncio
import sys
import os
import re
import unicodedata
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp
from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

# Import helpers from fake_arb
from fake_arb import (
    fetch_kalshi, fetch_polymarket,
    teams_from_kalshi_market, _kalshi_game_dt_utc,
    ARB_THRESHOLD, REQUEST_TIMEOUT,
    GLOBAL_MATCHERS, SPORTS_CONFIGS,
    _poly_ws_seed_from_rest, _rebuild_poly_ml_cache,
    _poly_ws_ml_token_map, _poly_ws_lock,
)

_ET = ZoneInfo("America/New_York")


async def main():
    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        print("=== Fetching Kalshi markets ===")
        kalshi_markets, _ = await fetch_kalshi(session)
        print(f"  Got {len(kalshi_markets)} Kalshi markets")
        for km in kalshi_markets[:10]:
            print(f"    {km['ticker']} | ask={km['ask']:.3f} | title={km['title'][:60]}")
        if len(kalshi_markets) > 10:
            print(f"    ... and {len(kalshi_markets)-10} more")

        print()
        print("=== Fetching Polymarket markets ===")
        poly_markets, _ = await fetch_polymarket(session)
        print(f"  Got {len(poly_markets)} Polymarket market sides")
        for pm in poly_markets[:10]:
            print(f"    {pm['team_abbr']:6s} | ask={pm['ask']:.3f} | slug={pm['slug'][:60]}")
        if len(poly_markets) > 10:
            print(f"    ... and {len(poly_markets)-10} more")

        print()
        print("=== Cross-checking Kalshi vs Poly team abbrevs ===")
        poly_abbrs = set(pm["team_abbr"] for pm in poly_markets)
        kalshi_pairs = set()
        for km in kalshi_markets:
            teams = teams_from_kalshi_market(km.get("raw") or {})
            if teams:
                kalshi_pairs.add(teams[0])
                kalshi_pairs.add(teams[1])
        print(f"  Kalshi team codes seen: {sorted(kalshi_pairs)}")
        print(f"  Poly team abbrevs seen:  {sorted(poly_abbrs)}")
        overlap = kalshi_pairs & poly_abbrs
        print(f"  Overlap: {sorted(overlap)}")

        print()
        print("=== Checking ticker last-segment vs team codes ===")
        for km in kalshi_markets[:20]:
            k_team = km["ticker"].split("-")[-1].lower()
            teams = teams_from_kalshi_market(km.get("raw") or {})
            match = "OK" if teams and k_team in teams else "MISMATCH"
            print(f"  {km['ticker'][-20:]:20s} | last={k_team:4s} | teams={teams} | {match}")

        print()
        print("=== Checking gameStartTime on Poly markets ===")
        for pm in poly_markets[:5]:
            gst = pm["raw"].get("gameStartTime", "MISSING")
            print(f"    {pm['slug'][:50]} | gameStartTime={gst}")

        print()
        print("=== Running matcher.match() calls ===")
        total_matches = []
        for matcher in GLOBAL_MATCHERS:
            matches = matcher.match(kalshi_markets, poly_markets)
            print(f"  {matcher.__class__.__name__}: {len(matches)} pairs found")
            total_matches.extend(matches)

        if total_matches:
            arbs = [m for m in total_matches if m["is_arb"]]
            print(f"\n  {len(arbs)} arbs out of {len(total_matches)} pairs (threshold={ARB_THRESHOLD})")
            for m in sorted(total_matches, key=lambda x: x.get("total_cost", 9)):
                flag = " <<< ARB" if m["is_arb"] else ""
                print(
                    f"    {m['market_name'][:45]:45s} | "
                    f"K={m['kalshi_ask']:.3f} P={m['polymarket_ask']:.3f} "
                    f"total={m['total_cost']:.4f} gap={m['gap_cents']:.1f}¢{flag}"
                )
        else:
            print("\n  No pairs matched at all — drilling into why:")
            # Pick first kalshi market and trace through matching logic
            for km in kalshi_markets[:5]:
                teams = teams_from_kalshi_market(km.get("raw") or {})
                k_team = km["ticker"].split("-")[-1].lower()
                k_dt = _kalshi_game_dt_utc(km["ticker"])
                print(f"\n  Kalshi: {km['ticker']} | teams={teams} | k_team={k_team} | dt={k_dt}")
                if teams is None:
                    print("    -> teams_from_kalshi_market returned None (title parse failed)")
                    continue
                if k_team not in teams:
                    print(f"    -> k_team '{k_team}' not in {teams} (ticker suffix mismatch)")
                    continue
                opponent = teams[1] if k_team == teams[0] else teams[0]
                print(f"    -> opponent={opponent}")
                poly_opps = [pm for pm in poly_markets if pm["team_abbr"] == opponent]
                print(f"    -> {len(poly_opps)} poly markets with team_abbr='{opponent}'")
                for pm in poly_opps[:3]:
                    gst = pm["raw"].get("gameStartTime", "MISSING")
                    try:
                        p_dt = datetime.fromisoformat(gst.replace("Z", "+00:00"))
                        skew = abs((k_dt - p_dt).total_seconds()) if k_dt else 999999
                        print(f"      poly slug={pm['slug'][:40]} | gst={gst} | skew={skew:.0f}s")
                    except Exception as e:
                        print(f"      poly slug={pm['slug'][:40]} | gst parse error: {e}")


asyncio.run(main())
