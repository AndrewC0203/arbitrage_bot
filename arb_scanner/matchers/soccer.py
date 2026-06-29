from typing import Optional
import re
import unicodedata
from matchers.base import BaseMatcher

def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def extract_teams(title: str) -> Optional[tuple[str, str]]:
    clean = title.replace("@", " vs ")
    normalized = normalize_name(clean)
    for sep in [" vs ", " v ", " at "]:
        parts = normalized.split(sep)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return None

class SoccerMatcher(BaseMatcher):
    API_MAPPING = {
        "kalshi": {
            "endpoint": "https://api.elections.kalshi.com/trade-api/v2/markets",
            "series_tickers": ["KXEPL", "KXMLS", "KXCHAMPIONS"],
            "data_fields": { "market_ticker": "ticker", "team_name": "title (or subtitle)", "ask_price": "yes_ask_dollars" }
        },
        "polymarket": {
            "endpoint": "https://gateway.polymarket.us/v2/leagues/epl/events",
            "league_slugs": ["epl", "mls", "champions-league"],
            "sports_market_type": "soccer_team_full_game_winner",
            "data_fields": { "market_slug": "slug", "team_name": "marketSides[].team.abbreviation", "ask_price": "marketSides[].quote.value" }
        }
    }

    def __init__(self, arb_threshold: float = 0.96):
        self.arb_threshold = arb_threshold

    def match(
        self,
        kalshi_markets: list[dict],
        polymarket_markets: list[dict]
    ) -> list[dict]:
        matches = []
        for km in kalshi_markets:
            raw_k = km.get("raw", {})
            title = km.get("title", "") or raw_k.get("title", "") or raw_k.get("subtitle", "")
            teams_k = extract_teams(title)
            if not teams_k:
                continue

            for pm in polymarket_markets:
                raw_p = pm.get("raw", {})
                pm_slug = pm.get("slug", "") or raw_p.get("slug", "")
                pm_team = normalize_name(pm.get("team_abbr", "") or pm.get("team", ""))
                
                k_team_matched = None
                pm_team_matched = None
                
                if pm_team and pm_team in teams_k[0]:
                    k_team_matched = teams_k[1]
                    pm_team_matched = teams_k[0]
                elif pm_team and pm_team in teams_k[1]:
                    k_team_matched = teams_k[0]
                    pm_team_matched = teams_k[1]
                    
                if not k_team_matched:
                    continue

                k_ask = km.get("ask", 0.0)
                p_ask = pm.get("ask", 0.0)
                k_fee = km.get("taker_fee", 0.0)
                p_fee = pm.get("taker_fee", 0.0)
                
                total_cost = k_ask + p_ask + k_fee + p_fee
                if total_cost > 0:
                    gap_cents = round((self.arb_threshold - total_cost) * 100, 4)
                    matches.append({
                        "market_name": title,
                        "kalshi_ticker": km.get("ticker", ""),
                        "kalshi_team": k_team_matched,
                        "kalshi_ask": round(k_ask, 6),
                        "kalshi_taker_fee": round(k_fee, 6),
                        "polymarket_slug": pm_slug,
                        "polymarket_team": pm_team_matched,
                        "polymarket_ask": round(p_ask, 6),
                        "polymarket_taker_fee": round(p_fee, 6),
                        "total_cost": round(total_cost, 6),
                        "gap_cents": gap_cents,
                        "is_arb": total_cost < self.arb_threshold,
                    })
        return matches
