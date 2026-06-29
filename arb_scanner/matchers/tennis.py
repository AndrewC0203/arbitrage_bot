from typing import Optional
import re
import unicodedata
from matchers.base import BaseMatcher

_VALID_PREFIXES = ("KXATP", "KXWTA")

def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def extract_players(title: str) -> Optional[tuple[str, str]]:
    clean = title.replace("@", " vs ")
    normalized = normalize_name(clean)
    for sep in [" vs ", " v ", " at "]:
        parts = normalized.split(sep)
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip()
    return None

class TennisMatcher(BaseMatcher):
    API_MAPPING = {
        "kalshi": {
            "endpoint": "https://api.elections.kalshi.com/trade-api/v2/markets",
            "series_tickers": ["KXATP", "KXWTA"],
            "data_fields": { "market_ticker": "ticker", "player_name": "title (or subtitle)", "ask_price": "yes_ask_dollars" }
        },
        "polymarket": {
            "endpoint": "https://gateway.polymarket.us/v2/leagues/atp/events",
            "league_slugs": ["atp", "wta"],
            "sports_market_type": "tennis_match_winner",
            "data_fields": { "market_slug": "slug", "player_name": "marketSides[].team.abbreviation", "ask_price": "marketSides[].quote.value" }
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
            if not any(km.get("ticker", "").startswith(p) for p in _VALID_PREFIXES):
                continue
            raw_k = km.get("raw", {})
            title = km.get("title", "") or raw_k.get("title", "") or raw_k.get("subtitle", "")
            players_k = extract_players(title)
            if not players_k:
                continue

            for pm in polymarket_markets:
                raw_p = pm.get("raw", {})
                pm_slug = pm.get("slug", "") or raw_p.get("slug", "")
                pm_player = normalize_name(pm.get("team_abbr", "") or pm.get("team", ""))
                
                k_player_matched = None
                pm_player_matched = None
                
                if pm_player and pm_player in players_k[0]:
                    k_player_matched = players_k[1]
                    pm_player_matched = players_k[0]
                elif pm_player and pm_player in players_k[1]:
                    k_player_matched = players_k[0]
                    pm_player_matched = players_k[1]
                    
                if not k_player_matched:
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
                        "kalshi_team": k_player_matched,
                        "kalshi_ask": round(k_ask, 6),
                        "kalshi_taker_fee": round(k_fee, 6),
                        "polymarket_slug": pm_slug,
                        "polymarket_team": pm_player_matched,
                        "polymarket_ask": round(p_ask, 6),
                        "polymarket_taker_fee": round(p_fee, 6),
                        "total_cost": round(total_cost, 6),
                        "gap_cents": gap_cents,
                        "is_arb": total_cost < self.arb_threshold,
                    })
        return matches
