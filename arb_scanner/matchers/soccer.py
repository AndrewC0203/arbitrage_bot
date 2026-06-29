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
_VALID_PREFIXES = ("KXEPL", "KXMLS", "KXCHAMPIONS")

_ALIASES: list[tuple[str, str]] = [
    # EPL — codes that do NOT substring-match full name
    ("manchester city", "mci"), ("man city", "mci"), ("mci", "mci"),
    ("manchester united", "mun"), ("man united", "mun"), ("man utd", "mun"), ("mun", "mun"),
    ("aston villa", "avl"), ("villa", "avl"), ("avl", "avl"),
    ("brighton hove albion", "bha"), ("brighton", "bha"), ("bha", "bha"),
    ("brentford", "brf"), ("brf", "brf"),
    ("west ham united", "whu"), ("west ham", "whu"), ("hammers", "whu"), ("whu", "whu"),
    # EPL — codes that DO substring-match (long-form aliases for Kalshi title resolution)
    ("arsenal", "ars"), ("ars", "ars"),
    ("chelsea", "che"), ("che", "che"),
    ("tottenham hotspur", "tot"), ("tottenham", "tot"), ("spurs", "tot"), ("tot", "tot"),
    ("newcastle united", "new"), ("newcastle", "new"), ("new", "new"),
    ("bournemouth", "bou"), ("bou", "bou"),
    ("crystal palace", "cry"), ("cry", "cry"),
    ("everton", "eve"), ("eve", "eve"),
    ("fulham", "ful"), ("ful", "ful"),
    ("ipswich town", "ips"), ("ipswich", "ips"), ("ips", "ips"),
    ("leicester city", "lei"), ("leicester", "lei"), ("lei", "lei"),
    ("liverpool", "liv"), ("liv", "liv"),
    ("nottingham forest", "not"), ("nottingham", "not"), ("not", "not"),
    ("southampton", "sou"), ("sou", "sou"),
    ("wolverhampton wanderers", "wol"), ("wolverhampton", "wol"), ("wolves", "wol"), ("wol", "wol"),
    # MLS — codes that do NOT substring-match
    ("la galaxy", "lag"), ("galaxy", "lag"), ("lag", "lag"),
    ("los angeles fc", "lac"), ("lafc", "lac"), ("lac", "lac"),
    ("sporting kansas city", "skc"), ("sporting kc", "skc"), ("skc", "skc"),
    ("real salt lake", "rsl"), ("rsl", "rsl"),
    ("columbus crew", "cls"), ("crew", "cls"), ("cls", "cls"),
    ("charlotte fc", "clt"), ("clt", "clt"),
    ("dc united", "dcu"), ("dcu", "dcu"),
    ("new england revolution", "nef"), ("new england", "nef"), ("revolution", "nef"), ("nef", "nef"),
    ("nashville sc", "nsh"), ("nashville", "nsh"), ("nsh", "nsh"),
    ("new york city fc", "nyc"), ("nycfc", "nyc"), ("new york city", "nyc"), ("nyc", "nyc"),
    ("new york red bulls", "nyr"), ("red bulls", "nyr"), ("nyr", "nyr"),
    ("cf montreal", "mtl"), ("montreal", "mtl"), ("mtl", "mtl"),
    ("san jose earthquakes", "sju"), ("san jose", "sju"), ("earthquakes", "sju"), ("sju", "sju"),
    ("st louis city sc", "stl"), ("st louis city", "stl"), ("stl", "stl"),
    # MLS — codes that DO substring-match
    ("seattle sounders", "sea"), ("sounders", "sea"), ("sea", "sea"),
    ("portland timbers", "por"), ("timbers", "por"),
    ("vancouver whitecaps", "van"), ("whitecaps", "van"), ("van", "van"),
    ("colorado rapids", "col"), ("rapids", "col"), ("col", "col"),
    ("minnesota united", "min"), ("minnesota", "min"), ("min", "min"),
    ("fc dallas", "dal"), ("dallas", "dal"), ("dal", "dal"),
    ("houston dynamo", "hou"), ("houston", "hou"), ("dynamo", "hou"), ("hou", "hou"),
    ("atlanta united", "atl"), ("atlanta", "atl"), ("atl", "atl"),
    ("orlando city", "orl"), ("orlando", "orl"), ("orl", "orl"),
    ("inter miami", "mia"), ("miami", "mia"), ("mia", "mia"),
    ("toronto fc", "tor"), ("toronto", "tor"), ("tor", "tor"),
    ("chicago fire", "chi"), ("chicago", "chi"), ("chi", "chi"),
    ("fc cincinnati", "cin"), ("cincinnati", "cin"), ("cin", "cin"),
    ("philadelphia union", "phi"), ("philadelphia", "phi"), ("union", "phi"), ("phi", "phi"),
    ("austin fc", "aus"), ("austin", "aus"), ("aus", "aus"),
    # Champions League
    ("paris saint germain", "psg"), ("paris", "psg"), ("psg", "psg"),
    ("real madrid", "rma"), ("madrid", "rma"), ("rma", "rma"),
    ("borussia dortmund", "bvb"), ("dortmund", "bvb"), ("bvb", "bvb"),
    ("fc barcelona", "fcb"), ("barcelona", "fcb"), ("barca", "fcb"), ("fcb", "fcb"),
    ("atletico madrid", "atm"), ("atletico", "atm"), ("atm", "atm"),
    ("ac milan", "acm"), ("milan", "acm"), ("acm", "acm"),
    ("juventus", "juv"), ("juve", "juv"), ("juv", "juv"),
    ("inter milan", "int"), ("inter", "int"), ("int", "int"),
    ("ajax", "ajx"), ("ajx", "ajx"),
    ("bayern munich", "bay"), ("munich", "bay"), ("bayern", "bay"),
    ("napoli", "nap"), ("nap", "nap"),
    ("porto", "por"), ("por", "por"),
    ("benfica", "ben"), ("ben", "ben"),
    ("sevilla", "sev"), ("sev", "sev"),
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

def _kalshi_game_dt_utc(ticker: str) -> Optional[datetime]:
    try:
        segment = ticker.split("-")[1]
        dt_naive = datetime.strptime(segment[:11], "%y%b%d%H%M")
        return dt_naive.replace(tzinfo=_ET).astimezone(timezone.utc)
    except (IndexError, ValueError):
        return None

class SoccerMatcher(BaseMatcher):
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

            k_game_dt_utc = _kalshi_game_dt_utc(km.get("ticker", ""))

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

                if k_game_dt_utc is not None:
                    try:
                        p_start = pm.get("raw", {}).get("gameStartTime", "")
                        p_game_dt = datetime.fromisoformat(p_start.replace("Z", "+00:00"))
                        if abs((k_game_dt_utc - p_game_dt).total_seconds()) > 30 * 60:
                            continue
                    except (ValueError, AttributeError):
                        pass

                k_ask = km.get("ask", 0.0)
                p_ask = pm.get("ask", 0.0)
                if k_ask <= 0 or p_ask <= 0:
                    continue
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
