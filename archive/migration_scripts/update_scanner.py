import re

with open("active/mlb_arb_scanner/scanner.py", "r") as f:
    content = f.read()

# 1. Update imports
new_imports = """from matchers.baseball import (
    BaseballMatcher,
    normalize_name,
    team_code,
    teams_from_kalshi_title,
    teams_from_kalshi_market,
    _kalshi_game_dt,
    kalshi_is_moneyline,
    teams_from_polymarket,
    polymarket_is_moneyline,
    _extract_polymarket_ask,
)
from matchers.basketball import BasketballMatcher
from matchers.soccer import SoccerMatcher
from matchers.tennis import TennisMatcher

SPORTS_CONFIGS = [
    {
        "name": "MLB",
        "kalshi_tickers": ["KXMLBGAME"],
        "poly_slugs": ["mlb"],
        "poly_smt": "baseball_team_full_game_winner",
        "matcher_cls": BaseballMatcher,
    },
    {
        "name": "NBA",
        "kalshi_tickers": ["KXNBA", "KXWNBA", "KXCBB"],
        "poly_slugs": ["nba", "wnba", "ncaab"],
        "poly_smt": "basketball_team_full_game_winner",
        "matcher_cls": BasketballMatcher,
    },
    {
        "name": "Soccer",
        "kalshi_tickers": ["KXEPL", "KXMLS", "KXCHAMPIONS"],
        "poly_slugs": ["epl", "mls", "champions-league"],
        "poly_smt": "soccer_team_full_game_winner",
        "matcher_cls": SoccerMatcher,
    },
    {
        "name": "Tennis",
        "kalshi_tickers": ["KXATP", "KXWTA"],
        "poly_slugs": ["atp", "wta"],
        "poly_smt": "tennis_match_winner",
        "matcher_cls": TennisMatcher,
    }
]
"""
content = re.sub(r'from matchers\.baseball import \([^)]+\)', new_imports, content, flags=re.DOTALL)

# 2. Update fetch_kalshi
new_fetch_kalshi = """async def fetch_kalshi(session: aiohttp.ClientSession) -> tuple[list[dict], str]:
    markets = []
    fetched_at = utc_now()
    tickers = []
    for cfg in SPORTS_CONFIGS:
        tickers.extend(cfg["kalshi_tickers"])
        
    for ticker in set(tickers):
        url = f"{KALSHI_BASE}/markets"
        params = {"series_ticker": ticker, "status": "open", "limit": 200}
        try:
            async with session.get(url, params=params, timeout=REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception:
            continue

        for m in data.get("markets", []):
            try:
                if ticker == "KXMLBGAME" and not kalshi_is_moneyline(m):
                    continue
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
                taker_fee = round(yes_ask * KALSHI_TAKER_FEE_RATE, 6)
                markets.append({
                    "ticker": t_ticker,
                    "title": title,
                    "ask": yes_ask,
                    "taker_fee": taker_fee,
                    "raw": m,
                })
            except (TypeError, ValueError, KeyError):
                continue

    return markets, fetched_at"""
content = re.sub(r'async def fetch_kalshi.*?return markets, fetched_at', new_fetch_kalshi, content, flags=re.DOTALL)

# 3. Update fetch_polymarket
new_fetch_poly = """async def fetch_polymarket(session: aiohttp.ClientSession) -> tuple[list[dict], list[dict], str]:
    fetched_at = utc_now()
    markets = []
    all_events = []
    
    slug_to_smt = {}
    for cfg in SPORTS_CONFIGS:
        for slug in cfg["poly_slugs"]:
            slug_to_smt[slug] = cfg["poly_smt"]

    for slug, smt in slug_to_smt.items():
        try:
            async with session.get(
                f"{POLYMARKET_US_GATEWAY}/v2/leagues/{slug}/events",
                params={"limit": 100},
                timeout=REQUEST_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
        except Exception:
            continue

        events = data.get("events", [])
        if events:
            all_events.extend(events)

        for event in events:
            for m in event.get("markets", []):
                try:
                    if m.get("sportsMarketType") != smt:
                        continue
                    if not m.get("active") or m.get("closed") or m.get("archived"):
                        continue
                    for side in m.get("marketSides", []):
                        team = side.get("team", {})
                        team_abbr = team.get("abbreviation", "")
                        if not team_abbr:
                            team_abbr = side.get("participant", "")
                        team_abbr = team_abbr.lower()
                        
                        quote = side.get("quote", {})
                        ask = float(quote.get("value", 0)) if quote else 0.0
                        if ask <= 0 or ask >= 1:
                            continue
                        taker_fee = round(ask * POLYMARKET_TAKER_FEE_RATE, 6)
                        markets.append({
                            "slug": m.get("slug", ""),
                            "title": m.get("question") or m.get("slug", ""),
                            "team_abbr": team_abbr,
                            "ask": ask,
                            "taker_fee": taker_fee,
                            "raw": m,
                        })
                except (TypeError, ValueError, KeyError):
                    continue

    return markets, all_events, fetched_at"""
content = re.sub(r'async def fetch_polymarket.*?return markets, events, fetched_at', new_fetch_poly, content, flags=re.DOTALL)

# 4. Update Main Loop to use multiple matchers
new_main_setup = """async def _main():
    tracker = OpportunityTracker()
    matchers = [cfg["matcher_cls"](arb_threshold=ARB_THRESHOLD) for cfg in SPORTS_CONFIGS]
    backoff = 1
    print(f"Multi-Sport Arb Scanner — both platforms fetched concurrently every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")"""
content = re.sub(r'async def _main\(\):.*?print\(f"MLB Arb Scanner [^\n]+', new_main_setup, content, flags=re.DOTALL)

new_matching_call = """            matches = []
            for matcher in matchers:
                matches.extend(matcher.match(kalshi_markets, poly_markets))"""
content = re.sub(r'            matches = matcher\.match\(kalshi_markets, poly_markets\)', new_matching_call, content)

with open("active/mlb_arb_scanner/scanner.py", "w") as f:
    f.write(content)
