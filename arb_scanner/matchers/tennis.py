from typing import Optional
import re
import unicodedata
from matchers.base import BaseMatcher

_VALID_PREFIXES = ("KXATPMATCH", "KXWTAMATCH")

# Extracts the subject player from "Will <Player> win the <A vs B>: <Round> match?"
_SUBJECT_RE = re.compile(r"^will (.+?) win the ", re.IGNORECASE)

# Extracts both players from the embedded "A vs B:" segment of the same title
_MATCHUP_RE = re.compile(r"win the (.+?) vs (.+?):", re.IGNORECASE)


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _extract_subject(title: str) -> Optional[str]:
    m = _SUBJECT_RE.match(title.strip())
    return normalize_name(m.group(1)) if m else None


def _extract_matchup(title: str) -> Optional[tuple[str, str]]:
    """Extract both player name segments from 'win the A vs B:' in the title."""
    m = _MATCHUP_RE.search(title.strip())
    if not m:
        return None
    return normalize_name(m.group(1)), normalize_name(m.group(2))


def _names_match(subject: str, pm_name: str) -> bool:
    """
    Returns True if pm_name could refer to the same player as subject.

    For compound surnames (space in subject), use whole-name word-boundary match
    first to avoid 'garcia' matching 'garcia lopez' when 'garcia' was intended
    to match a different player. Falls back to last-word match for simple names.

    - subject contains a space (compound): require full subject to appear as
      consecutive words in pm_name, OR pm_name to appear as consecutive words
      in subject.
    - last name < 4 chars: require exact full-name equality.
    - last name >= 4 chars: require last word appears as a whole word in pm_name.
    """
    if not subject or not pm_name:
        return False
    words = subject.split()
    if len(words) > 1:
        # Compound name: whole-name word-boundary match in either direction
        pattern = r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b"
        if re.search(pattern, pm_name):
            return True
        pm_words = pm_name.split()
        if len(pm_words) > 1:
            pm_pattern = r"\b" + r"\s+".join(re.escape(w) for w in pm_words) + r"\b"
            if re.search(pm_pattern, subject):
                return True
        # Fall back to last-word match (handles "novak djokovic" subject vs "djokovic" pm)
        last_name = words[-1]
        if len(last_name) < 4:
            return subject == pm_name
        return bool(re.search(r"\b" + re.escape(last_name) + r"\b", pm_name))
    # Single-word subject
    last_name = words[-1]
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
            title = (
                km.get("title", "")
                or km.get("raw", {}).get("title", "")
                or km.get("raw", {}).get("subtitle", "")
            )
            subject = _extract_subject(title)
            if not subject:
                continue
            matchup = _extract_matchup(title)
            if not matchup:
                continue
            slot_a, slot_b = matchup

            # Determine which slot is the subject and which is the opponent.
            # subject matches one slot; opponent is the other.
            if _names_match(slot_a, subject) or _names_match(subject, slot_a):
                opponent = slot_b
            elif _names_match(slot_b, subject) or _names_match(subject, slot_b):
                opponent = slot_a
            else:
                continue

            # Find the Polymarket entry for the OPPONENT (arb: Kalshi YES(subject) + Poly YES(opponent))
            for pm in polymarket_markets:
                pm_name = normalize_name(pm.get("team_name", "") or pm.get("team_abbr", ""))
                if not _names_match(opponent, pm_name) and not _names_match(pm_name, opponent):
                    continue

                k_ask = km.get("ask", 0.0)
                p_ask = pm.get("ask", 0.0)
                if k_ask <= 0 or p_ask <= 0:
                    continue

                k_fee = km.get("taker_fee", 0.0)
                p_fee = pm.get("taker_fee", 0.0)
                total_cost = k_ask + p_ask + k_fee + p_fee

                matches.append({
                    "market_name": title,
                    "kalshi_ticker": km.get("ticker", ""),
                    "kalshi_team": subject,
                    "kalshi_ask": round(k_ask, 6),
                    "kalshi_taker_fee": round(k_fee, 6),
                    "polymarket_slug": pm.get("slug", "") or pm.get("raw", {}).get("slug", ""),
                    "polymarket_team": pm_name,
                    "polymarket_ask": round(p_ask, 6),
                    "polymarket_taker_fee": round(p_fee, 6),
                    "total_cost": round(total_cost, 6),
                    "gap_cents": round((self.arb_threshold - total_cost) * 100, 4),
                    "is_arb": total_cost < self.arb_threshold,
                })
        return matches
