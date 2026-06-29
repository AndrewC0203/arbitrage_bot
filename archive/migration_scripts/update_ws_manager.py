import re

with open("active/mlb_arb_scanner/ws_manager.py", "r") as f:
    content = f.read()

# 1. Imports and Configs
new_imports = """
from matchers.baseball import BaseballMatcher
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

GLOBAL_MATCHERS = [cfg["matcher_cls"](arb_threshold=0.96) for cfg in SPORTS_CONFIGS]

"""
# We will insert it after load_dotenv()
content = content.replace("load_dotenv()\n", "load_dotenv()\n" + new_imports)

# 2. Update SERIES_TO_SMT
new_smt = """SERIES_TO_SMT = {
    "KXMLBHIT": "baseball_player_hits",
    "KXMLBHR":  "baseball_player_home_runs",
    "KXMLBTB":  "baseball_player_total_bases",
    "KXNBAPTS": "basketball_player_points",
    "KXNBAREB": "basketball_player_rebounds",
    "KXNBAAST": "basketball_player_assists",
    "KXNBA3PT": "basketball_player_threes",
}"""
content = re.sub(r'SERIES_TO_SMT = \{[^}]+\}', new_smt, content)


# 3. Update check_arb_moneyline to use GLOBAL_MATCHERS
# Replace: matches = match_markets(kalshi_markets, poly_markets)
# With:
new_match_call = """    matches = []
    for matcher in GLOBAL_MATCHERS:
        matches.extend(matcher.match(kalshi_markets, poly_markets))"""
content = content.replace("    matches = match_markets(kalshi_markets, poly_markets)", new_match_call)

# 4. Update fetch_kalshi
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
content = re.sub(r'async def fetch_kalshi\(session: aiohttp\.ClientSession\) -> tuple\[list\[dict\], str\]:.*?return markets, fetched_at', new_fetch_kalshi, content, flags=re.DOTALL)


# 5. Update fetch_polymarket
new_fetch_poly = """async def fetch_polymarket(session: aiohttp.ClientSession) -> tuple[list[dict], str]:
    fetched_at = utc_now()
    markets = []
    
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

        for event in data.get("events", []):
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

    return markets, fetched_at"""
content = re.sub(r'async def fetch_polymarket\(session: aiohttp\.ClientSession\) -> tuple\[list\[dict\], str\]:.*?return markets, fetched_at', new_fetch_poly, content, flags=re.DOTALL)


# 6. Update fetch_poly_props query
new_fetch_props = """def fetch_poly_props(today_str: str) -> list[dict]:
    all_events = []
    for q in ["mlb will record at least", "nba will record", "player points", "player rebounds"]:
        try:
            resp = requests.get(
                f"{POLYMARKET_US_GATEWAY}/v1/search",
                params={"query": q, "limit": 200},
                timeout=REST_TIMEOUT,
            )
            resp.raise_for_status()
            all_events.extend(resp.json().get("events", []))
        except requests.RequestException:
            pass"""
content = re.sub(r'def fetch_poly_props\(today_str: str\) -> list\[dict\]:.*?except requests.RequestException as exc:\s*raise RuntimeError.*?from exc', new_fetch_props, content, flags=re.DOTALL)
content = content.replace('for event in resp.json().get("events", []):', 'for event in all_events:')

# 7. Update _poly_ws_seed_from_rest ML Seeding
new_poly_ws_seed = """    slug_to_smt = {}
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

            for event in data.get("events", []):
                for m in event.get("markets", []):
                    try:
                        if m.get("sportsMarketType") != smt:
                            continue
                        if not m.get("active") or m.get("closed") or m.get("archived"):
                            continue
                        slug_m = m.get("slug", "")
                        if not slug_m:
                            continue
                        
                        yes_ask = no_ask = None
                        yes_abbr = no_abbr = None
                        for side in m.get("marketSides", []):
                            team = side.get("team", {})
                            abbr = team.get("abbreviation", "")
                            if not abbr:
                                abbr = side.get("participant", "")
                            abbr = abbr.lower()
                            
                            quote = side.get("quote") or {}
                            try:
                                ask = float(quote.get("value", 0))
                            except (TypeError, ValueError):
                                ask = 0.0
                                
                            if side.get("long") is True:
                                yes_ask = ask if 0 < ask < 1 else None
                                yes_abbr = abbr
                            else:
                                no_ask = ask if 0 < ask < 1 else None
                                no_abbr = abbr
                                
                        _poly_ws_ml_token_map[slug_m] = {
                            "slug": slug_m,
                            "title": m.get("question") or slug_m,
                            "yes_abbr": yes_abbr,
                            "no_abbr": no_abbr,
                            "yes_ask": yes_ask,
                            "no_ask": no_ask,
                            "raw": m,
                            "updated_at": now,
                        }
                        ml_slugs.append(slug_m)
                    except (TypeError, ValueError, KeyError):
                        continue
        except Exception as exc:
            print(f"[POLY-WS] ML REST seed error for {slug}: {exc}", file=sys.stderr)"""

content = re.sub(r'    try:\s*async with session.get\(\s*f"\{POLYMARKET_US_GATEWAY\}/v2/leagues/mlb/events".*?except Exception as exc:\s*print\(f"\[POLY-WS\] ML REST seed error: \{exc\}", file=sys.stderr\)', new_poly_ws_seed, content, flags=re.DOTALL)


# 8. Update _poly_ws_seed_from_rest Props Seeding query
new_ws_props_fetch = """        def _fetch_props_raw() -> list:
            events = []
            for q in ["mlb will record at least", "nba will record", "player points", "player rebounds"]:
                try:
                    resp = __import__("requests").get(
                        f"{POLYMARKET_US_GATEWAY}/v1/search",
                        params={"query": q, "limit": 200},
                        timeout=REST_TIMEOUT,
                    )
                    resp.raise_for_status()
                    events.extend(resp.json().get("events", []))
                except Exception:
                    pass
            return events"""
content = re.sub(r'        def _fetch_props_raw\(\) -> list:.*?return resp\.json\(\)\.get\("events", \[\]\)', new_ws_props_fetch, content, flags=re.DOTALL)


with open("active/mlb_arb_scanner/ws_manager.py", "w") as f:
    f.write(content)
