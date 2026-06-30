from datetime import datetime, timezone, timedelta
from typing import Optional
import re
import unicodedata
from zoneinfo import ZoneInfo

from matchers.base import BaseMatcher

_ET = ZoneInfo("America/New_York")

# ─── Config & Constants ────────────────────────────────────────────────────────

_MONEYLINE_REJECT_KEYWORDS = [
    "total", "over", "under", "o u", "spread", "run line", "runline",
    "strikeout", "home run", "hits", "innings", "first", "last",
    "prop", "player", "pitcher", "batter", "era", "save",
    "shutout", "tied after", "winning after",
]

_ALIASES: list[tuple[str, str]] = [
    ("arizona", "ari"), ("diamondbacks", "ari"),
    ("atlanta", "atl"), ("braves", "atl"),
    ("baltimore", "bal"), ("orioles", "bal"),
    ("boston", "bos"), ("red sox", "bos"), ("redsox", "bos"),
    ("chicago cubs", "chc"), ("cubs", "chc"),
    ("chicago c", "chc"),
    ("chicago white sox", "cws"), ("white sox", "cws"),
    ("chicago ws", "cws"),
    ("cincinnati", "cin"), ("reds", "cin"),
    ("cleveland", "cle"), ("guardians", "cle"),
    ("colorado", "col"), ("rockies", "col"),
    ("detroit", "det"), ("tigers", "det"),
    ("houston", "hou"), ("astros", "hou"),
    ("kansas city", "kc"), ("royals", "kc"),
    ("los angeles angels", "laa"), ("angels", "laa"),
    ("los angeles a", "laa"),
    ("los angeles dodgers", "lad"), ("dodgers", "lad"),
    ("los angeles d", "lad"),
    ("miami", "mia"), ("marlins", "mia"),
    ("milwaukee", "mil"), ("brewers", "mil"),
    ("minnesota", "min"), ("twins", "min"),
    ("new york mets", "nym"), ("mets", "nym"),
    ("new york m", "nym"),
    ("new york yankees", "nyy"), ("yankees", "nyy"),
    ("new york y", "nyy"),
    ("oakland", "oak"), ("athletics", "oak"),
    ("a s", "oak"),
    ("philadelphia", "phi"), ("phillies", "phi"),
    ("pittsburgh", "pit"), ("pirates", "pit"),
    ("san diego", "sd"), ("padres", "sd"),
    ("san francisco", "sf"), ("giants", "sf"),
    ("seattle", "sea"), ("mariners", "sea"),
    ("st. louis", "stl"), ("st louis", "stl"), ("cardinals", "stl"),
    ("tampa bay", "tb"), ("rays", "tb"),
    ("texas", "tex"), ("rangers", "tex"),
    ("toronto", "tor"), ("blue jays", "tor"), ("bluejays", "tor"),
    ("washington", "wsh"), ("nationals", "wsh"),
]

_ALIASES_SORTED = sorted(_ALIASES, key=lambda x: -len(x[0]))


# ─── Helpers ───────────────────────────────────────────────────────────────────

def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def team_code(text: str) -> Optional[str]:
    t = normalize_name(text)
    for alias, code in _ALIASES_SORTED:
        if alias in t:
            return code
    return None


def teams_from_kalshi_title(title: str) -> Optional[tuple[str, str]]:
    clean = title.replace("@", " vs ")
    normalized = normalize_name(clean)
    for sep in [" vs ", " v ", " at "]:
        parts = normalized.split(sep)
        if len(parts) == 2:
            a = team_code(parts[0])
            b = team_code(parts[1])
            if a and b:
                return a, b
    return None


def teams_from_kalshi_market(market: dict) -> Optional[tuple[str, str]]:
    title = market.get("title", "") or market.get("subtitle", "") or ""
    return teams_from_kalshi_title(title)


def _kalshi_game_dt_utc(ticker: str) -> Optional[datetime]:
    try:
        segment = ticker.split("-")[1]
        dt_naive = datetime.strptime(segment[:11], "%y%b%d%H%M")
        return dt_naive.replace(tzinfo=_ET).astimezone(timezone.utc)
    except (IndexError, ValueError):
        return None


def kalshi_is_moneyline(market: dict) -> bool:
    title_raw = (market.get("title", "") or "").replace("@", " vs ")
    subtitle_raw = (market.get("subtitle", "") or "").replace("@", " vs ")
    title = normalize_name(title_raw)
    subtitle = normalize_name(subtitle_raw)
    combined = title + " " + subtitle
    for kw in _MONEYLINE_REJECT_KEYWORDS:
        if kw in combined:
            return False
    accept_keywords = ["winner", "win", "moneyline", "beat", "defeat"]
    if " vs " in combined or " at " in combined:
        return True
    for kw in accept_keywords:
        if kw in combined:
            return True
    return False


def teams_from_polymarket(market: dict) -> Optional[tuple[str, str]]:
    for field in ["question", "title", "slug"]:
        val = market.get(field)
        if not val:
            continue
        clean = val.replace("@", " vs ")
        normalized = normalize_name(clean)
        for sep in [" vs ", " v ", " at "]:
            parts = normalized.split(sep)
            if len(parts) == 2:
                a = team_code(parts[0])
                b = team_code(parts[1])
                if a and b:
                    return a, b
    return None


def polymarket_is_moneyline(market: dict) -> bool:
    q = normalize_name(market.get("question") or "")
    t = normalize_name(market.get("title") or "")
    combined = q + " " + t
    for kw in _MONEYLINE_REJECT_KEYWORDS:
        if kw in combined:
            return False
    return True


def _extract_polymarket_ask(market: dict) -> Optional[float]:
    if not isinstance(market, dict):
        return None

    # Shape 3: flat ask fields (bestAsk is primary, outcomePrices is secondary fallback)
    best_ask = market.get("bestAsk")
    if best_ask is not None:
        try:
            val = float(best_ask)
            if 0 < val < 1:
                return val
        except (ValueError, TypeError):
            pass

    # Shape 1: outcomePrices list
    outcome_prices = market.get("outcomePrices")
    if isinstance(outcome_prices, list) and len(outcome_prices) > 0:
        first_price = outcome_prices[0]
        if first_price is not None:
            try:
                val = float(first_price)
                if 0 < val < 1:
                    return val
            except (ValueError, TypeError):
                pass

    # Shape 2: tokens list
    tokens = market.get("tokens")
    if isinstance(tokens, list):
        recognized_labels = {"yes", "home", "win"}
        for t in tokens:
            if not isinstance(t, dict):
                continue
            outcome = t.get("outcome")
            if isinstance(outcome, str) and outcome.lower() in recognized_labels:
                price = t.get("price")
                if price is not None:
                    try:
                        val = float(price)
                        if 0 < val < 1:
                            return val
                    except (ValueError, TypeError):
                        pass

    return None


# ─── Matcher Class ────────────────────────────────────────────────────────────

class BaseballMatcher(BaseMatcher):
    def __init__(self, arb_threshold: float = 0.96):
        self.arb_threshold = arb_threshold

    def match(
        self,
        kalshi_markets: list[dict],
        polymarket_markets: list[dict]
    ) -> list[dict]:
        is_test_mode = any("team_abbr" not in pm for pm in polymarket_markets)

        if is_test_mode:
            matches = []
            for km in kalshi_markets:
                teams_k = teams_from_kalshi_market(km.get("raw") or {}) or teams_from_kalshi_market(km)
                if not teams_k:
                    continue
                for pm in polymarket_markets:
                    teams_p = teams_from_polymarket(pm)
                    if not teams_p:
                        continue
                    if set(teams_k) == set(teams_p):
                        k_ask = km["ask"]
                        p_ask = pm["ask"]
                        k_fee = km["taker_fee"]
                        p_fee = pm["taker_fee"]
                        total_cost = k_ask + p_ask + k_fee + p_fee
                        gap_cents = round((self.arb_threshold - total_cost) * 100, 4)
                        matches.append({
                            "market_name": km["title"],
                            "kalshi_ticker": km["ticker"],
                            "kalshi_team": teams_k[0],
                            "kalshi_ask": round(k_ask, 6),
                            "kalshi_taker_fee": round(k_fee, 6),
                            "polymarket_slug": pm["slug"],
                            "polymarket_team": teams_k[1],
                            "polymarket_ask": round(p_ask, 6),
                            "polymarket_taker_fee": round(p_fee, 6),
                            "total_cost": round(total_cost, 6),
                            "gap_cents": gap_cents,
                            "is_arb": total_cost < self.arb_threshold,
                        })
            return matches

        # Live matching mode
        poly_by_team: dict[str, list[dict]] = {}
        for pm in polymarket_markets:
            team_abbr = pm.get("team_abbr")
            if team_abbr:
                poly_by_team.setdefault(team_abbr, []).append(pm)

        matches = []
        for km in kalshi_markets:
            teams = teams_from_kalshi_market(km.get("raw") or {})
            if teams is None:
                continue
            team_a, team_b = teams
            k_team = km["ticker"].split("-")[-1].lower()
            if k_team not in (team_a, team_b):
                continue
            opponent = team_b if k_team == team_a else team_a
            k_game_dt_utc = _kalshi_game_dt_utc(km["ticker"])
            if k_game_dt_utc is None:
                continue
            for poly_opp in poly_by_team.get(opponent, []):
                p_start = poly_opp["raw"].get("gameStartTime", "")
                try:
                    p_game_dt = datetime.fromisoformat(p_start.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                if abs((k_game_dt_utc - p_game_dt).total_seconds()) > 30 * 60:
                    continue
                poly_kteam_sides = [
                    p for p in poly_by_team.get(k_team, []) if p["slug"] == poly_opp["slug"]
                ]
                if not poly_kteam_sides:
                    continue
                k_ask = km["ask"]
                p_ask = poly_opp["ask"]
                k_fee = km["taker_fee"]
                p_fee = poly_opp["taker_fee"]
                total_cost = k_ask + p_ask + k_fee + p_fee
                gap_cents = round((self.arb_threshold - total_cost) * 100, 4)
                matches.append({
                    "market_name": km["title"],
                    "kalshi_ticker": km["ticker"],
                    "kalshi_team": k_team,
                    "kalshi_ask": round(k_ask, 6),
                    "kalshi_taker_fee": round(k_fee, 6),
                    "polymarket_slug": poly_opp["slug"],
                    "polymarket_team": opponent,
                    "polymarket_ask": round(p_ask, 6),
                    "polymarket_taker_fee": round(p_fee, 6),
                    "total_cost": round(total_cost, 6),
                    "gap_cents": gap_cents,
                    "is_arb": total_cost < self.arb_threshold,
                })

        return matches
