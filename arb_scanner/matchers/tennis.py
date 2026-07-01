from datetime import date
from typing import Optional
import re
import unicodedata
from matchers.base import BaseMatcher

_VALID_PREFIXES = ("KXATPMATCH", "KXWTAMATCH")

# Extracts the subject player from "Will <Player> win the <A vs B>: <Round> match?"
_SUBJECT_RE = re.compile(r"^will (.+?) win the ", re.IGNORECASE)

# Extracts both players from the embedded "A vs B:" segment of the same title
_MATCHUP_RE = re.compile(r"win the (.+?) vs (.+?):", re.IGNORECASE)

# --- Disambiguation guards ---
_TICKER_DATE_RE    = re.compile(r"KX[A-Z]+-(\d{2})([A-Z]{3})(\d{2})")
_SLUG_DATE_RE      = re.compile(r"(\d{4}-\d{2}-\d{2})$")
_TICKER_PLAYERS_RE = re.compile(r"KX[A-Z]+-\d{2}[A-Z]{3}\d{2}([A-Z]+)-[A-Z]+$")
_SLUG_PLAYERS_RE   = re.compile(r"aec-(?:atp|wta)-(.+)-\d{4}-\d{2}-\d{2}$")
_MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
           "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def _parse_kalshi_date(ticker: str) -> Optional[date]:
    m = _TICKER_DATE_RE.search(ticker)
    if not m:
        return None
    month = _MONTHS.get(m.group(2))
    if not month:
        return None
    try:
        return date(2000 + int(m.group(1)), month, int(m.group(3)))
    except ValueError:
        return None


def _parse_poly_date(slug: str) -> Optional[date]:
    m = _SLUG_DATE_RE.search(slug)
    if not m:
        return None
    try:
        return date.fromisoformat(m.group(1))
    except ValueError:
        return None


def _kalshi_league(ticker: str) -> str:
    return "wta" if ticker.startswith("KXWTA") else "atp"


def _poly_league(slug: str) -> Optional[str]:
    if "aec-wta" in slug:
        return "wta"
    if "aec-atp" in slug:
        return "atp"
    return None


def _abbr_fingerprint_ok(ticker: str, slug: str) -> bool:
    """Both Poly slug player abbrev suffixes (last 3 chars) must appear in Kalshi's fused player segment."""
    tm = _TICKER_PLAYERS_RE.match(ticker)
    sm = _SLUG_PLAYERS_RE.match(slug)
    if not tm or not sm:
        return True  # can't parse → conservative pass
    t_combined = tm.group(1).lower()
    s_parts = sm.group(1).split("-")
    if len(s_parts) < 2:
        return True
    long_parts = [s for s in s_parts if len(s) >= 3]
    if not long_parts:
        return True  # can't fingerprint → conservative pass
    hits = sum(1 for s in long_parts if s[-3:] in t_combined)
    return hits >= min(2, len(long_parts))


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

                pm_slug = pm.get("slug", "") or pm.get("raw", {}).get("slug", "")
                km_ticker = km.get("ticker", "")

                # Guard 1: dates must be within 1 day (late-night ET matches can cross midnight UTC)
                k_date = _parse_kalshi_date(km_ticker)
                p_date = _parse_poly_date(pm_slug)
                if k_date and p_date and abs((k_date - p_date).days) > 1:
                    continue

                # Guard 2: ATP ticker must not match WTA slug and vice versa
                p_league = _poly_league(pm_slug)
                if p_league and _kalshi_league(km_ticker) != p_league:
                    continue

                # Guard 3: both Poly player abbrev suffixes must appear in Kalshi's fused player segment
                if not _abbr_fingerprint_ok(km_ticker, pm_slug):
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
                    "polymarket_slug": pm_slug,
                    "polymarket_team": pm_name,
                    "polymarket_ask": round(p_ask, 6),
                    "polymarket_taker_fee": round(p_fee, 6),
                    "total_cost": round(total_cost, 6),
                    "gap_cents": round((self.arb_threshold - total_cost) * 100, 4),
                    "is_arb": total_cost < self.arb_threshold,
                })
        return matches
