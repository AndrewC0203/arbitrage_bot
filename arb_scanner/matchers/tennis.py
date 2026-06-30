from typing import Optional
import re
import unicodedata
from matchers.base import BaseMatcher

_VALID_PREFIXES = ("KXATPMATCH", "KXWTAMATCH")

# Extracts the subject player from "Will <Player> win the <A vs B>: <Round> match?"
_SUBJECT_RE = re.compile(r"^will (.+?) win the ", re.IGNORECASE)


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_subject(title: str) -> Optional[str]:
    m = _SUBJECT_RE.match(title.strip())
    return normalize_name(m.group(1)) if m else None


def _names_match(subject: str, pm_name: str) -> bool:
    """
    Returns True if the Kalshi subject player name matches the Polymarket full name.
    Uses whole-word last-name matching with a min-length guard:
    - last name < 4 chars: require exact full-name equality
    - last name >= 4 chars: require last name appears as a whole word in pm_name
    """
    if not subject or not pm_name:
        return False
    last_name = subject.split()[-1]
    if len(last_name) < 4:
        return subject == pm_name
    return bool(re.search(r"\b" + re.escape(last_name) + r"\b", pm_name))


class TennisMatcher(BaseMatcher):
    API_MAPPING = {
        "kalshi": {
            "endpoint": "https://api.elections.kalshi.com/trade-api/v2/markets",
            "series_tickers": ["KXATPMATCH", "KXWTAMATCH"],
            "data_fields": {"market_ticker": "ticker", "player_name": "title (subject of 'Will X win...')", "ask_price": "yes_ask_dollars"},
        },
        "polymarket": {
            "endpoint": "https://gateway.polymarket.us/v2/leagues/atp/events",
            "league_slugs": ["atp", "wta"],
            "sports_market_type": "tennis_match_winner",
            "data_fields": {"market_slug": "slug", "player_name": "marketSides[].team.displayName", "ask_price": "marketSides[].quote.value"},
        },
    }

    def __init__(self, arb_threshold: float = 0.96):
        self.arb_threshold = arb_threshold

    def match(
        self,
        kalshi_markets: list[dict],
        polymarket_markets: list[dict],
    ) -> list[dict]:
        matches = []
        for km in kalshi_markets:
            if not any(km.get("ticker", "").startswith(p) for p in _VALID_PREFIXES):
                continue
            title = km.get("title", "") or km.get("raw", {}).get("title", "")
            subject = _extract_subject(title)
            if not subject:
                continue

            for pm in polymarket_markets:
                pm_name = normalize_name(pm.get("team_name", "") or pm.get("team_abbr", ""))
                if not _names_match(subject, pm_name):
                    continue

                k_ask = km.get("ask", 0.0)
                p_ask = pm.get("ask", 0.0)
                k_fee = km.get("taker_fee", 0.0)
                p_fee = pm.get("taker_fee", 0.0)
                total_cost = k_ask + p_ask + k_fee + p_fee
                if total_cost <= 0:
                    continue

                matches.append({
                    "market_name": title,
                    "kalshi_ticker": km.get("ticker", ""),
                    "kalshi_player": subject,
                    "kalshi_ask": round(k_ask, 6),
                    "kalshi_taker_fee": round(k_fee, 6),
                    "polymarket_slug": pm.get("slug", "") or pm.get("raw", {}).get("slug", ""),
                    "polymarket_player": pm_name,
                    "polymarket_ask": round(p_ask, 6),
                    "polymarket_taker_fee": round(p_fee, 6),
                    "total_cost": round(total_cost, 6),
                    "gap_cents": round((self.arb_threshold - total_cost) * 100, 4),
                    "is_arb": total_cost < self.arb_threshold,
                })
        return matches
