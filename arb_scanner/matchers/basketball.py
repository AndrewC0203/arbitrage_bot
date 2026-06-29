from typing import Optional
import re
import unicodedata
from matchers.base import BaseMatcher
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

_ET = ZoneInfo("America/New_York")
_VALID_PREFIXES = ("KXNBA", "KXWNBA", "KXCBB")

_ALIASES: list[tuple[str, str]] = [
    # NBA — teams where 3-letter code does NOT substring-match full name
    ("los angeles lakers", "lal"), ("los angeles l", "lal"), ("lakers", "lal"), ("lal", "lal"),
    ("los angeles clippers", "lac"), ("los angeles c", "lac"), ("clippers", "lac"), ("lac", "lac"),
    ("golden state warriors", "gsw"), ("golden state", "gsw"), ("warriors", "gsw"), ("gsw", "gsw"),
    ("brooklyn nets", "bkn"), ("brooklyn", "bkn"), ("nets", "bkn"), ("bkn", "bkn"),
    ("new york knicks", "nyk"), ("new york k", "nyk"), ("knicks", "nyk"), ("nyk", "nyk"),
    ("new orleans pelicans", "nop"), ("new orleans", "nop"), ("pelicans", "nop"), ("nop", "nop"),
    ("oklahoma city thunder", "okc"), ("oklahoma city", "okc"), ("thunder", "okc"), ("okc", "okc"),
    ("san antonio spurs", "sas"), ("san antonio", "sas"), ("spurs", "sas"), ("sas", "sas"),
    ("phoenix suns", "phx"), ("phoenix", "phx"), ("suns", "phx"), ("phx", "phx"),
    # NBA — codes with potential substring collisions; safe to add direct entry for 'den'
    # ('den' in 'golden' = False because alias is checked as alias-in-input, not input-in-alias)
    ("denver nuggets", "den"), ("denver", "den"), ("nuggets", "den"), ("den", "den"),
    ("philadelphia 76ers", "phi"), ("philadelphia", "phi"), ("76ers", "phi"), ("sixers", "phi"),
    ("orlando magic", "orl"), ("orlando", "orl"), ("magic", "orl"),
    # NBA — teams where code naturally substring-matches (long-form aliases for Kalshi titles)
    ("atlanta hawks", "atl"), ("atlanta", "atl"), ("hawks", "atl"),
    ("boston celtics", "bos"), ("boston", "bos"), ("celtics", "bos"), ("bos", "bos"),
    ("charlotte hornets", "cha"), ("charlotte", "cha"), ("hornets", "cha"),
    ("chicago bulls", "chi"), ("chicago", "chi"), ("bulls", "chi"),
    ("cleveland cavaliers", "cle"), ("cleveland", "cle"), ("cavaliers", "cle"), ("cavs", "cle"),
    ("dallas mavericks", "dal"), ("dallas", "dal"), ("mavericks", "dal"), ("mavs", "dal"),
    ("detroit pistons", "det"), ("detroit", "det"), ("pistons", "det"),
    ("houston rockets", "hou"), ("houston", "hou"), ("rockets", "hou"),
    ("indiana pacers", "ind"), ("indiana", "ind"), ("pacers", "ind"),
    ("memphis grizzlies", "mem"), ("memphis", "mem"), ("grizzlies", "mem"),
    ("miami heat", "mia"), ("miami", "mia"), ("heat", "mia"),
    ("milwaukee bucks", "mil"), ("milwaukee", "mil"), ("bucks", "mil"),
    ("minnesota timberwolves", "min"), ("minnesota", "min"), ("timberwolves", "min"), ("wolves", "min"),
    ("portland trail blazers", "por"), ("portland", "por"), ("trail blazers", "por"), ("blazers", "por"),
    ("sacramento kings", "sac"), ("sacramento", "sac"), ("kings", "sac"),
    ("toronto raptors", "tor"), ("toronto", "tor"), ("raptors", "tor"),
    ("utah jazz", "uta"), ("utah", "uta"), ("jazz", "uta"),
    ("washington wizards", "was"), ("washington", "was"), ("wizards", "was"),
    # WNBA (shares city codes with NBA; ticker prefix distinguishes)
    ("las vegas aces", "las"), ("las vegas", "las"), ("aces", "las"),
    ("connecticut sun", "con"), ("connecticut", "con"), ("sun", "con"),
    ("new york liberty", "nyl"), ("liberty", "nyl"), ("nyl", "nyl"),
    ("seattle storm", "sea"), ("seattle", "sea"), ("storm", "sea"),
    ("golden state valkyries", "lav"), ("valkyries", "lav"), ("lav", "lav"),
    # WNBA teams sharing codes with NBA cities (atlanta/chi/dal/ind/min/phx/was resolved above)
    ("atlanta dream", "atl"), ("dream", "atl"),
    ("chicago sky", "chi"), ("sky", "chi"),
    ("dallas wings", "dal"), ("wings", "dal"),
    ("indiana fever", "ind"), ("fever", "ind"),
    ("minnesota lynx", "min"), ("lynx", "min"),
    ("phoenix mercury", "phx"), ("mercury", "phx"),
    ("washington mystics", "was"), ("mystics", "was"),
    # NCAAB coverage intentionally incomplete.
]
_ALIASES_SORTED = sorted(_ALIASES, key=lambda x: -len(x[0]))

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

def _kalshi_game_dt(ticker: str) -> Optional[datetime]:
    try:
        segment = ticker.split("-")[1]
        return datetime.strptime(segment[:11], "%y%b%d%H%M")  # naive ET time
    except (IndexError, ValueError):
        return None

class BasketballMatcher(BaseMatcher):
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
            raw_k = km.get("raw") or {}
            title = km.get("title", "") or raw_k.get("title", "") or raw_k.get("subtitle", "")
            teams_k = teams_from_kalshi_title(title)
            if not teams_k:
                continue
            team_a, team_b = teams_k

            k_game_dt = _kalshi_game_dt(km.get("ticker", ""))

            for pm in polymarket_markets:
                pm_code = team_code(pm.get("team_abbr", "") or pm.get("team", ""))
                if not pm_code:
                    continue
                if pm_code == team_a:
                    k_side, p_side = team_b, team_a
                elif pm_code == team_b:
                    k_side, p_side = team_a, team_b
                else:
                    continue

                if k_game_dt is not None:
                    try:
                        p_start = pm.get("raw", {}).get("gameStartTime", "")
                        p_game_dt = datetime.fromisoformat(p_start.replace("Z", "+00:00"))
                        k_game_dt_utc = k_game_dt.replace(tzinfo=_ET).astimezone(timezone.utc)
                        if abs((k_game_dt_utc - p_game_dt).total_seconds()) > 30 * 60:
                            continue
                    except (ValueError, AttributeError):
                        pass

                k_ask = km.get("ask", 0.0)
                p_ask = pm.get("ask", 0.0)
                k_fee = km.get("taker_fee", 0.0)
                p_fee = pm.get("taker_fee", 0.0)
                total_cost = k_ask + p_ask + k_fee + p_fee
                if total_cost <= 0:
                    continue
                pm_slug = pm.get("slug", "") or (pm.get("raw") or {}).get("slug", "")
                matches.append({
                    "market_name": title,
                    "kalshi_ticker": km.get("ticker", ""),
                    "kalshi_team": k_side,
                    "kalshi_ask": round(k_ask, 6),
                    "kalshi_taker_fee": round(k_fee, 6),
                    "polymarket_slug": pm_slug,
                    "polymarket_team": p_side,
                    "polymarket_ask": round(p_ask, 6),
                    "polymarket_taker_fee": round(p_fee, 6),
                    "total_cost": round(total_cost, 6),
                    "gap_cents": round((self.arb_threshold - total_cost) * 100, 4),
                    "is_arb": total_cost < self.arb_threshold,
                })
        return matches
