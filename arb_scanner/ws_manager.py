"""
ws_manager.py — Unified WebSocket Manager for MLB Arb

Single Kalshi WS connection subscribing to two channels:
  - orderbook_delta: moneyline markets (triggers arb check on every delta)
  - ticker:          player prop markets (prices cached; arb checked on each Poly WS update)

Polymarket: authenticated WebSocket at wss://api.polymarket.us/v1/ws/markets
  - Ed25519 auth via X-PM-Access-Key / X-PM-Timestamp / X-PM-Signature headers
  - Subscribes to MARKET_DATA_LITE events for MLB market token IDs
  - Startup REST seed discovers token IDs; WS then streams all price deltas
  - Arb checks fire on every WS price update (no polling)

REST polling tasks (_poly_ml_polling_task, _poly_props_polling_task) are preserved
but commented out of asyncio.gather() — re-enable for side-by-side comparison.

Run:
  python ws_manager.py
"""

import asyncio
import base64
import itertools
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

import aiohttp
import requests
import websockets
from dotenv import load_dotenv

load_dotenv()

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


# ─── Config ────────────────────────────────────────────────────────────────────

KALSHI_WS_URL         = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_BASE           = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"
ARB_THRESHOLD         = 0.96
KALSHI_TAKER_FEE_RATE = 0.01
POLYMARKET_TAKER_FEE_RATE = 0.01
POLY_POLL_SECONDS     = 2
LOG_FILE              = "arb_log.jsonl"
_WS_SUBSCRIBE_CHUNK_SIZE = 50
_CACHE_STALE_SECONDS  = 300
_MAX_TIMESTAMP_SKEW_SECONDS = 10
REQUEST_TIMEOUT       = aiohttp.ClientTimeout(total=8)
REST_TIMEOUT          = 8

# Kalshi prop series → Polymarket sportsMarketType
SERIES_TO_SMT = {
    "KXMLBHIT": "baseball_player_hits",
    "KXMLBHR":  "baseball_player_home_runs",
    "KXMLBTB":  "baseball_player_total_bases",
    "KXNBAPTS": "basketball_player_points",
    "KXNBAREB": "basketball_player_rebounds",
    "KXNBAAST": "basketball_player_assists",
    "KXNBA3PT": "basketball_player_threes",
}

_MONEYLINE_REJECT_KEYWORDS = [
    "total", "over", "under", "o u", "spread", "run line", "runline",
    "strikeout", "home run", "hits", "innings", "first", "last",
    "prop", "player", "pitcher", "batter", "era", "save",
    "shutout", "tied after", "winning after",
]

# ─── Helpers (inlined from scanner.py) ────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(obj: dict) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(obj) + "\n")


def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


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


def _kalshi_game_dt(ticker: str) -> Optional[datetime]:
    try:
        segment = ticker.split("-")[1]
        return datetime.strptime(segment[:11], "%y%b%d%H%M").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def kalshi_is_moneyline(market: dict) -> bool:
    title = normalize_name((market.get("title", "") or "").replace("@", " vs "))
    subtitle = normalize_name((market.get("subtitle", "") or "").replace("@", " vs "))
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


# ─── Helpers (inlined from props_scanner.py) ──────────────────────────────────

def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def _kalshi_game_dt_utc(ticker: str) -> Optional[datetime]:
    try:
        seg = ticker.split("-")[1]
        dt_naive = datetime.strptime(seg[:11], "%y%b%d%H%M")
        return dt_naive.replace(tzinfo=_ET).astimezone(timezone.utc)
    except (IndexError, ValueError):
        return None


def _kalshi_parse_title(title: str) -> Optional[tuple[str, int]]:
    m = re.match(r"^(.+?):\s*(\d+)\+", title)
    if not m:
        return None
    return m.group(1).strip(), int(m.group(2))


def _kalshi_ask(market: dict, side: str) -> Optional[float]:
    key = f"{side}_ask_dollars"
    raw = market.get(key)
    if raw is None:
        key2 = f"{side}_ask"
        raw2 = market.get(key2)
        if raw2 is not None:
            try:
                val = float(raw2) / 100.0
                return val if 0 < val < 1 else None
            except (TypeError, ValueError):
                return None
        return None
    try:
        val = float(raw)
        return val if 0 < val < 1 else None
    except (TypeError, ValueError):
        return None


def _poly_ask(side: dict) -> Optional[float]:
    quote = side.get("quote") or {}
    raw = quote.get("value")
    if raw is None:
        return None
    try:
        val = float(raw)
        return val if 0 < val < 1 else None
    except (TypeError, ValueError):
        return None


# ─── Auth (inlined from props_scanner.py) ─────────────────────────────────────

def _load_ws_credentials() -> Optional[tuple[str, object]]:
    api_key_id = (
        os.environ.get("KALSHI_KEY_ID")
        or os.environ.get("KALSHI_API_KEY_ID")
        or os.environ.get("KALSHI_API_KEY")
    )
    key_path = os.environ.get("KALSHI_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key_id or not key_path:
        return None
    if not os.path.isabs(key_path):
        env_dir = os.path.dirname(os.path.abspath(__file__))
        key_path = os.path.join(env_dir, key_path)
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        with open(key_path, "rb") as f:
            private_key = load_pem_private_key(f.read(), password=None)
        return api_key_id, private_key
    except Exception as exc:
        print(f"[WARN] Failed to load Kalshi WS credentials: {exc}", file=sys.stderr)
        return None


def _ws_auth_headers(api_key_id: str, private_key) -> dict:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    timestamp_ms = str(int(time.time() * 1000))
    msg = (timestamp_ms + "GET" + "/trade-api/ws/v2").encode()
    signature = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


POLYMARKET_US_WS_URL = "wss://api.polymarket.us/v1/ws/markets"


def _poly_ws_auth_headers() -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key_id = os.environ.get("POLYMARKET_US_KEY_ID", "")
    secret_b64 = os.environ.get("POLYMARKET_US_SECRET_KEY", "")
    if not key_id or not secret_b64:
        raise RuntimeError(
            "POLYMARKET_US_KEY_ID and POLYMARKET_US_SECRET_KEY must be set in .env"
        )
    secret_bytes = base64.b64decode(secret_b64)
    # Standard NaCl 64-byte key = 32-byte seed || 32-byte public; from_private_bytes needs seed only
    seed = secret_bytes[:32]
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    ts = str(int(time.time() * 1000))
    signature = private_key.sign(f"{ts}GET/v1/ws/markets".encode())
    return {
        "X-PM-Access-Key": key_id,
        "X-PM-Timestamp": ts,
        "X-PM-Signature": base64.b64encode(signature).decode(),
    }


# ─── Order Book (moneyline YES-side only) ─────────────────────────────────────

class KalshiOrderBook:
    """
    In-memory YES-side order book for Kalshi moneyline tickers.
    Keyed by ticker; tracks best_ask derived from live orderbook_delta messages.
    Only the YES side is stored — moneyline arb buys YES on Kalshi.
    """

    def __init__(self):
        self._books: dict[str, dict[int, int]] = {}       # ticker → {price_cents: qty}
        self._best_ask: dict[str, Optional[float]] = {}   # ticker → best ask USD
        self._seq: dict[str, int] = {}                    # ticker → last seq number
        self._updated_at: dict[str, datetime] = {}
        self._metadata: dict[str, dict] = {}              # ticker → {title, raw}

    def seed_from_rest(self, markets: list[dict]) -> list[str]:
        """Store metadata from initial REST fetch. Returns ticker list for WS subscription."""
        for m in markets:
            ticker = m["ticker"]
            if ticker not in self._metadata:
                self._metadata[ticker] = {"title": m["title"], "raw": m["raw"]}
        return list(self._metadata.keys())

    def today_tickers(self, today: "datetime.date") -> list[str]:
        yesterday = today - timedelta(days=1)
        result = []
        for ticker in self._metadata:
            gdt = _kalshi_game_dt(ticker)
            if gdt is not None and gdt.date() >= yesterday:
                result.append(ticker)
        return result

    def apply_snapshot(self, ticker: str, seq: int, yes_levels: list) -> None:
        """Replace book with full snapshot from orderbook_snapshot message."""
        if ticker not in self._metadata:
            return
        book: dict[int, int] = {}
        for entry in yes_levels:
            try:
                price, qty = int(entry[0]), int(entry[1])
                if qty > 0:
                    book[price] = qty
            except (IndexError, TypeError, ValueError):
                continue
        self._books[ticker] = book
        self._seq[ticker] = seq
        self._updated_at[ticker] = datetime.now(timezone.utc)
        self._recompute_best_ask(ticker)

    def apply_delta(self, ticker: str, seq: int, side: str, price: int, delta: int) -> bool:
        """
        Apply a single orderbook_delta. Returns False if seq gap detected
        (caller should resubscribe to get a fresh snapshot).
        Only processes the YES side.
        """
        if ticker not in self._metadata:
            return True  # unknown ticker, silently skip

        # Seq gap check — only enforce after we have a baseline
        if ticker in self._seq and seq != self._seq[ticker] + 1:
            return False

        if side != "yes":
            # Track seq even for NO-side deltas so we don't false-gap later
            if ticker in self._seq:
                self._seq[ticker] = seq
            return True

        book = self._books.setdefault(ticker, {})
        try:
            price_int = int(price)
            delta_int = int(delta)
        except (TypeError, ValueError):
            return True

        new_qty = book.get(price_int, 0) + delta_int
        if new_qty <= 0:
            book.pop(price_int, None)
        else:
            book[price_int] = new_qty

        self._seq[ticker] = seq
        self._updated_at[ticker] = datetime.now(timezone.utc)
        self._recompute_best_ask(ticker)
        return True

    def _recompute_best_ask(self, ticker: str) -> None:
        book = self._books.get(ticker, {})
        positive = [p for p, q in book.items() if q > 0]
        self._best_ask[ticker] = min(positive) / 100.0 if positive else None

    def as_kalshi_markets(self, now: datetime) -> list[dict]:
        """Return normalized market dicts in the format match_markets() expects."""
        stale_cutoff = now - timedelta(seconds=_CACHE_STALE_SECONDS)
        result = []
        for ticker, meta in self._metadata.items():
            ask = self._best_ask.get(ticker)
            if ask is None or ask <= 0 or ask >= 1:
                continue
            updated = self._updated_at.get(ticker)
            if updated is None or updated < stale_cutoff:
                continue
            result.append({
                "ticker": ticker,
                "title": meta["title"],
                "ask": ask,
                "taker_fee": round(ask * KALSHI_TAKER_FEE_RATE, 6),
                "raw": meta["raw"],
            })
        return result


# ─── Props Price Cache (inlined from props_scanner.py) ────────────────────────

class KalshiPriceCache:
    """
    In-memory cache for Kalshi prop prices, updated by WS ticker messages.
    Seeded from REST on startup; stale entries excluded from arb checks.
    """

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._cache_date: Optional["datetime.date"] = None

    def update_from_ws(self, ticker: str, msg: dict) -> None:
        raw_yes = msg.get("yes_ask") if msg.get("yes_ask") is not None else msg.get("yes_ask_dollars")
        raw_no  = msg.get("no_ask")  if msg.get("no_ask")  is not None else msg.get("no_ask_dollars")
        yes_ask = self._parse_price(raw_yes)
        no_ask  = self._parse_price(raw_no)
        if ticker not in self._cache:
            return
        entry = self._cache[ticker]
        if yes_ask is not None:
            entry["yes_ask"] = yes_ask
        if no_ask is not None:
            entry["no_ask"] = no_ask
        entry["updated_at"] = datetime.now(timezone.utc)

    def seed_from_rest(self, props: list[dict]) -> None:
        self.purge_old_date(datetime.now(timezone.utc).date())
        now = datetime.now(timezone.utc)
        for kp in props:
            ticker = kp["ticker"]
            if ticker not in self._cache:
                self._cache[ticker] = {
                    "series": kp["series"],
                    "ticker": ticker,
                    "player_name": kp["player_name"],
                    "player_norm": kp["player_norm"],
                    "line": kp["line"],
                    "game_dt_utc": kp["game_dt_utc"],
                    "yes_ask": kp["yes_ask"],
                    "no_ask": kp["no_ask"],
                    "updated_at": now,
                }
            else:
                entry = self._cache[ticker]
                if kp["yes_ask"] is not None:
                    entry["yes_ask"] = kp["yes_ask"]
                if kp["no_ask"] is not None:
                    entry["no_ask"] = kp["no_ask"]
                entry["updated_at"] = now

    def purge_old_date(self, today: "datetime.date") -> None:
        if self._cache_date == today:
            return
        yesterday = today - timedelta(days=1)
        self._cache = {
            t: e for t, e in self._cache.items()
            if e["game_dt_utc"].date() >= yesterday
        }
        self._cache_date = today

    def as_props_list(self, now: datetime) -> list[dict]:
        stale_cutoff = now - timedelta(seconds=_CACHE_STALE_SECONDS)
        return [
            e for e in self._cache.values()
            if e["updated_at"] >= stale_cutoff
            and not (e["yes_ask"] is None and e["no_ask"] is None)
        ]

    def today_tickers(self, today: "datetime.date") -> list[str]:
        yesterday = today - timedelta(days=1)
        return [t for t, e in self._cache.items() if e["game_dt_utc"].date() >= yesterday]

    @staticmethod
    def _parse_price(raw) -> Optional[float]:
        if raw is None:
            return None
        try:
            val = float(raw)
            if val > 1:
                val /= 100.0
            return val if 0 < val < 1 else None
        except (TypeError, ValueError):
            return None


# ─── Arb Logic (inlined from scanner.py / props_scanner.py) ───────────────────

def match_markets(kalshi_markets: list[dict], polymarket_markets: list[dict]) -> list[dict]:
    poly_by_team: dict[str, list[dict]] = {}
    for pm in polymarket_markets:
        poly_by_team.setdefault(pm["team_abbr"], []).append(pm)

    matches = []
    for km in kalshi_markets:
        teams = teams_from_kalshi_market(km["raw"])
        if teams is None:
            continue
        team_a, team_b = teams
        k_team = km["ticker"].split("-")[-1].lower()
        if k_team not in (team_a, team_b):
            continue
        opponent = team_b if k_team == team_a else team_a
        k_game_dt = _kalshi_game_dt(km["ticker"])
        if k_game_dt is None:
            continue
        for poly_opp in poly_by_team.get(opponent, []):
            p_start = poly_opp["raw"].get("gameStartTime", "")
            try:
                p_game_dt = datetime.fromisoformat(p_start.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            k_game_dt_utc = k_game_dt.replace(tzinfo=_ET).astimezone(timezone.utc)
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
                "gap_cents": round((ARB_THRESHOLD - total_cost) * 100, 4),
                "is_arb": total_cost < ARB_THRESHOLD,
            })
    return matches


class OpportunityTracker:
    def __init__(self):
        self._open: dict[tuple, dict] = {}

    def _make_key(self, m: dict) -> tuple:
        return (m["kalshi_ticker"], m["polymarket_slug"])

    def process(
        self,
        matches: list[dict],
        kalshi_fetched_at: str,
        polymarket_fetched_at: str,
        timestamp_skew_warning: bool,
    ) -> list[dict]:
        now = utc_now()
        arb_matches = [m for m in matches if m["is_arb"]]
        current_keys = {self._make_key(m): m for m in arb_matches}
        events = []

        for key, m in current_keys.items():
            if key not in self._open:
                opp_id = str(uuid.uuid4())
                self._open[key] = {"opportunity_id": opp_id, "opened_at": now,
                                   "last_seen": now, "last_match": m}
                events.append(self._build_event(
                    "opened", opp_id, m, now,
                    kalshi_fetched_at, polymarket_fetched_at,
                    timestamp_skew_warning, duration_seconds=None,
                ))
            else:
                rec = self._open[key]
                rec["last_seen"] = now
                rec["last_match"] = m
                duration = self._duration(rec["opened_at"], now)
                events.append(self._build_event(
                    "updated", rec["opportunity_id"], m, now,
                    kalshi_fetched_at, polymarket_fetched_at,
                    timestamp_skew_warning, duration_seconds=duration,
                ))

        for key in set(self._open.keys()) - set(current_keys.keys()):
            rec = self._open.pop(key)
            duration = self._duration(rec["opened_at"], now)
            last_m = rec.get("last_match", {})
            events.append({
                "event": "closed",
                "opportunity_id": rec["opportunity_id"],
                "timestamp": now,
                "kalshi_fetched_at": kalshi_fetched_at,
                "polymarket_fetched_at": polymarket_fetched_at,
                "timestamp_skew_warning": timestamp_skew_warning,
                "market_name": last_m.get("market_name"),
                "kalshi_ticker": last_m.get("kalshi_ticker", key[0]),
                "kalshi_ask": last_m.get("kalshi_ask"),
                "kalshi_taker_fee": last_m.get("kalshi_taker_fee"),
                "polymarket_slug": last_m.get("polymarket_slug", key[1]),
                "polymarket_ask": last_m.get("polymarket_ask"),
                "polymarket_taker_fee": last_m.get("polymarket_taker_fee"),
                "total_cost": last_m.get("total_cost"),
                "gap_cents": last_m.get("gap_cents"),
                "duration_seconds": duration,
            })
        return events

    def active_windows(self) -> list[dict]:
        return list(self._open.values())

    @staticmethod
    def _duration(opened_at: str, now: str) -> float:
        try:
            return round((datetime.fromisoformat(now) - datetime.fromisoformat(opened_at)).total_seconds(), 2)
        except Exception:
            return 0.0

    @staticmethod
    def _build_event(event_type, opp_id, m, now, kalshi_fetched_at,
                     polymarket_fetched_at, timestamp_skew_warning, duration_seconds) -> dict:
        return {
            "event": event_type,
            "opportunity_id": opp_id,
            "timestamp": now,
            "kalshi_fetched_at": kalshi_fetched_at,
            "polymarket_fetched_at": polymarket_fetched_at,
            "timestamp_skew_warning": timestamp_skew_warning,
            "market_name": m["market_name"],
            "kalshi_ticker": m["kalshi_ticker"],
            "kalshi_ask": m["kalshi_ask"],
            "kalshi_taker_fee": m["kalshi_taker_fee"],
            "polymarket_slug": m["polymarket_slug"],
            "polymarket_ask": m["polymarket_ask"],
            "polymarket_taker_fee": m["polymarket_taker_fee"],
            "total_cost": m["total_cost"],
            "gap_cents": m["gap_cents"],
            "duration_seconds": duration_seconds,
        }


def match_props(kalshi_props: list[dict], poly_props: list[dict]) -> list[dict]:
    poly_index: dict[tuple, dict] = {}
    for pp in poly_props:
        game_date = pp["game_start"][:10]
        key = (pp["smt"], pp["player_norm"], pp["line"], game_date)
        poly_index[key] = pp

    matches = []
    for kp in kalshi_props:
        smt = SERIES_TO_SMT[kp["series"]]
        game_date = kp["game_dt_utc"].strftime("%Y-%m-%d")
        key = (smt, kp["player_norm"], kp["line"], game_date)
        pp = poly_index.get(key)
        if pp is None:
            continue

        k_yes, k_no = kp["yes_ask"], kp["no_ask"]
        p_yes, p_no = pp["yes_ask"], pp["no_ask"]

        for direction, leg1_name, leg1_ask, leg2_name, leg2_ask in [
            ("Poly YES + Kalshi NO", "Poly YES", p_yes, "Kalshi NO", k_no),
            ("Kalshi YES + Poly NO", "Kalshi YES", k_yes, "Poly NO", p_no),
        ]:
            if leg1_ask is None or leg2_ask is None:
                continue
            total = leg1_ask + leg2_ask
            if total >= ARB_THRESHOLD:
                continue
            matches.append({
                "direction": direction,
                "event_title": pp["event_title"],
                "game_start": pp["game_start"],
                "player_name": pp["player_name"],
                "stat_type": smt.replace("baseball_player_", ""),
                "line": kp["line"],
                "leg1": leg1_name,
                "leg1_ask": leg1_ask,
                "leg2": leg2_name,
                "leg2_ask": leg2_ask,
                "total_cost": round(total, 4),
                "gap_cents": round((ARB_THRESHOLD - total) * 100, 2),
                "kalshi_ticker": kp["ticker"],
                "poly_smt": smt,
            })

    matches.sort(key=lambda x: -x["gap_cents"])
    return matches


def _emit_prop_arbs(arbs: list[dict], timestamp: str) -> None:
    """Print and log prop arbs, skipping any whose prices haven't changed since last emit.
    Evicts stale keys so an arb that disappears and returns will re-emit."""
    active_keys = {(a["kalshi_ticker"], a["direction"]) for a in arbs}
    for stale in set(_last_arb_prices) - active_keys:
        del _last_arb_prices[stale]

    new_arbs = []
    for arb in arbs:
        key = (arb["kalshi_ticker"], arb["direction"])
        price_sig = (arb["leg1_ask"], arb["leg2_ask"])
        if _last_arb_prices.get(key) == price_sig:
            continue
        _last_arb_prices[key] = price_sig
        new_arbs.append(arb)

    if not new_arbs:
        return

    print(f"\n{'='*70}")
    print(f"  PROP ARB FOUND — {timestamp}")
    print(f"{'='*70}")
    current_game = None
    for arb in new_arbs:
        if arb["event_title"] != current_game:
            current_game = arb["event_title"]
            print(f"\n--- {arb['event_title']} ({arb['game_start']}) ---")
        print(
            f"  [{arb['gap_cents']:.1f}¢] {arb['player_name']} "
            f"{arb['line']}+ {arb['stat_type']}: "
            f"{arb['leg1']}={arb['leg1_ask']:.2f} + "
            f"{arb['leg2']}={arb['leg2_ask']:.2f} = "
            f"${arb['total_cost']:.2f}"
        )
        print(f"         Kalshi: {arb['kalshi_ticker']}")

    with open(LOG_FILE, "a") as f:
        for arb in new_arbs:
            f.write(json.dumps({"event": "prop_arb", "timestamp": timestamp, **arb}) + "\n")


# ─── REST Fetch Functions ──────────────────────────────────────────────────────

async def fetch_kalshi(session: aiohttp.ClientSession) -> tuple[list[dict], str]:
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
    return markets, fetched_at


async def fetch_polymarket(session: aiohttp.ClientSession) -> tuple[list[dict], str]:
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

    return markets, fetched_at


def fetch_kalshi_props(today_utc: "datetime.date") -> list[dict]:
    valid_dates = {today_utc, today_utc - timedelta(days=1)}
    results = []
    for series in SERIES_TO_SMT:
        try:
            resp = requests.get(
                f"{KALSHI_BASE}/markets",
                params={"series_ticker": series, "status": "open", "limit": 200},
                timeout=REST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[WARN] Kalshi {series} fetch failed: {exc}", file=sys.stderr)
            continue
        for m in resp.json().get("markets", []):
            ticker = m.get("ticker", "")
            game_dt = _kalshi_game_dt_utc(ticker)
            if game_dt is None or game_dt.date() not in valid_dates:
                continue
            parsed = _kalshi_parse_title(m.get("title", ""))
            if parsed is None:
                continue
            player_name, line = parsed
            yes_ask = _kalshi_ask(m, "yes")
            no_ask  = _kalshi_ask(m, "no")
            if yes_ask is None and no_ask is None:
                continue
            results.append({
                "series": series,
                "ticker": ticker,
                "player_name": player_name,
                "player_norm": _normalize(player_name),
                "line": line,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "game_dt_utc": game_dt,
            })
    return results


def fetch_poly_props(today_str: str) -> list[dict]:
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
            pass

    supported_smts = set(SERIES_TO_SMT.values())
    from datetime import date
    today = date.fromisoformat(today_str)
    valid_dates = {today_str, (today - timedelta(days=1)).isoformat()}

    results = []
    for event in all_events:
        event_title = event.get("title", "")
        for m in event.get("markets", []):
            smt = m.get("sportsMarketType", "")
            if smt not in supported_smts:
                continue
            if not m.get("active") or m.get("closed") or m.get("archived"):
                continue
            game_start = m.get("gameStartTime", "") or ""
            if game_start[:10] not in valid_dates:
                continue
            metadata = m.get("metadata") or {}
            player_name = metadata.get("playerName", "")
            if not player_name:
                continue
            yes_ask = no_ask = None
            for side in m.get("marketSides", []):
                ask = _poly_ask(side)
                if side.get("long") is True:
                    yes_ask = ask
                elif side.get("long") is False:
                    no_ask = ask
            if yes_ask is None and no_ask is None:
                continue
            results.append({
                "smt": smt,
                "player_name": player_name,
                "player_norm": _normalize(player_name),
                "line": m.get("line"),
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "game_start": game_start,
                "event_title": event_title,
            })
    return results


# ─── Module-Level Shared State ─────────────────────────────────────────────────

_order_book   = KalshiOrderBook()
_price_cache  = KalshiPriceCache()
_poly_ml_cache: Optional[tuple[list[dict], str]] = None    # (markets, fetched_at)
_poly_ml_lock = asyncio.Lock()
_ml_tracker   = OpportunityTracker()
_msg_id_iter  = itertools.count(1)

# Polymarket WS token maps — keyed by token_id, populated by _poly_ws_seed_from_rest
_poly_ws_ml_token_map:    dict[str, dict] = {}
_poly_ws_props_token_map: dict[str, dict] = {}
_poly_ws_lock = asyncio.Lock()
_last_status_log: float = 0.0   # epoch seconds; status lines throttled to once per 60s
_STATUS_LOG_INTERVAL = 60
# keyed by (kalshi_ticker, direction); value is (leg1_ask, leg2_ask) last printed/logged
_last_arb_prices: dict[tuple, tuple] = {}


def _next_id() -> int:
    return next(_msg_id_iter)


# ─── Moneyline Arb Check (called synchronously from WS handler) ───────────────

def check_arb_moneyline(kalshi_updated_at: str) -> None:
    """Run moneyline arb check using current order book + cached Poly data."""
    if _poly_ml_cache is None:
        return
    poly_markets, poly_fetched_at = _poly_ml_cache
    now = datetime.now(timezone.utc)
    kalshi_markets = _order_book.as_kalshi_markets(now)
    if not kalshi_markets:
        return

    matches = []
    for matcher in GLOBAL_MATCHERS:
        matches.extend(matcher.match(kalshi_markets, poly_markets))

    try:
        skew = abs((datetime.fromisoformat(kalshi_updated_at) -
                    datetime.fromisoformat(poly_fetched_at)).total_seconds())
        skew_warn = skew > _MAX_TIMESTAMP_SKEW_SECONDS
    except Exception:
        skew_warn = False

    events = _ml_tracker.process(matches, kalshi_updated_at, poly_fetched_at, skew_warn)
    for ev in events:
        log_event(ev)

    active = [ev for ev in events if ev["event"] in ("opened", "updated")]
    if active:
        lines = []
        for ev in active:
            name  = ev.get("market_name", "?")
            total = ev.get("total_cost") or 0.0
            gap   = ev.get("gap_cents") or 0.0
            dur   = ev.get("duration_seconds")
            dur_s = f"{int(dur)}s" if dur is not None else "<1s"
            lines.append(f"[WS] {name} — total=${total:.3f} gap={gap:.1f}¢ open {dur_s}")
        sys.stdout.write("\r" + " | ".join(lines)[:200].ljust(200) + "\r")
        sys.stdout.flush()

    ts = now.strftime("%H:%M:%S")
    if not matches and not active:
        global _last_status_log
        if time.time() - _last_status_log >= _STATUS_LOG_INTERVAL:
            _last_status_log = time.time()
            sys.stdout.write(
                f"\r[{ts}][WS] No ML arb — {len(kalshi_markets)}K/{len(poly_markets)}P markets.".ljust(120) + "\r"
            )
            sys.stdout.flush()


# ─── WS Subscribe Helper ───────────────────────────────────────────────────────

async def _ws_subscribe_chunks(ws, tickers: list[str], channel: str, start_id: int) -> int:
    msg_id = start_id
    for i in range(0, len(tickers), _WS_SUBSCRIBE_CHUNK_SIZE):
        chunk = tickers[i: i + _WS_SUBSCRIBE_CHUNK_SIZE]
        await ws.send(json.dumps({
            "id": msg_id,
            "cmd": "subscribe",
            "params": {"channels": [channel], "market_tickers": chunk},
        }))
        msg_id += 1
    return msg_id


# ─── WS Message Handler ────────────────────────────────────────────────────────

def _handle_ws_message(data: dict) -> bool:
    """
    Dispatch a single WS message. Returns True if the caller should resubscribe
    that ticker (sequence gap on orderbook_delta).
    """
    msg_type = data.get("type")
    ticker   = data.get("market_ticker")

    if msg_type == "orderbook_snapshot":
        if ticker:
            _order_book.apply_snapshot(ticker, data.get("seq", 0), data.get("yes", []))

    elif msg_type == "orderbook_delta":
        if ticker:
            ok = _order_book.apply_delta(
                ticker,
                data.get("seq"),
                data.get("side", ""),
                data.get("price"),
                data.get("delta"),
            )
            if not ok:
                return True
            check_arb_moneyline(utc_now())

    elif msg_type == "ticker":
        # Props price update — cache only; arb checked in _poly_props_polling_task
        if ticker:
            _price_cache.update_from_ws(ticker, data)

    elif msg_type == "subscribed":
        pass

    elif msg_type == "error":
        print(f"[WS] Error from Kalshi: {data}", file=sys.stderr)

    return False


# ─── Kalshi WS Task (single connection, both channels) ────────────────────────

async def _kalshi_ws_task(api_key_id: str, private_key) -> None:
    backoff    = 1
    _pkey      = private_key
    private_key = None  # drop outer reference after handoff

    while True:
        try:
            auth_headers = _ws_auth_headers(api_key_id, _pkey)
            _pkey = None  # clear after signing; reloaded on reconnect

            async with websockets.connect(
                KALSHI_WS_URL, additional_headers=auth_headers
            ) as ws:
                backoff = 1

                today = datetime.now(timezone.utc).date()
                ml_tickers   = _order_book.today_tickers(today)
                prop_tickers = _price_cache.today_tickers(today)

                next_id = 1
                next_id = await _ws_subscribe_chunks(ws, ml_tickers,   "orderbook_delta", next_id)
                next_id = await _ws_subscribe_chunks(ws, prop_tickers,  "ticker",          next_id)

                print(
                    f"[WS] Connected — {len(ml_tickers)} moneyline tickers (orderbook_delta), "
                    f"{len(prop_tickers)} prop tickers (ticker).",
                    file=sys.stderr,
                )

                async for message in ws:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    needs_resub = _handle_ws_message(data)

                    if needs_resub:
                        bad_ticker = data.get("market_ticker")
                        if bad_ticker:
                            resub_id = _next_id() + 10000
                            await ws.send(json.dumps({
                                "id": resub_id,
                                "cmd": "subscribe",
                                "params": {"channels": ["orderbook_delta"], "market_tickers": [bad_ticker]},
                            }))

                    # Date rollover: resubscribe to next day's tickers
                    new_today = datetime.now(timezone.utc).date()
                    if new_today != today:
                        today = new_today
                        _order_book._metadata  # keep existing metadata
                        _price_cache.purge_old_date(today)
                        new_ml   = _order_book.today_tickers(today)
                        new_prop = _price_cache.today_tickers(today)
                        next_id = await _ws_subscribe_chunks(ws, new_ml,   "orderbook_delta", next_id)
                        next_id = await _ws_subscribe_chunks(ws, new_prop, "ticker",          next_id)
                        print(
                            f"[WS] Date rollover — resubscribed {len(new_ml)} ML + {len(new_prop)} prop tickers.",
                            file=sys.stderr,
                        )

        except Exception as exc:
            print(
                f"[WS] Kalshi disconnected: {exc!r}. Reconnecting in {backoff}s...",
                file=sys.stderr,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

            if _pkey is None:
                creds = _load_ws_credentials()
                if creds is None:
                    print("[ERROR] Cannot reload Kalshi credentials for reconnect.", file=sys.stderr)
                    return
                _, _pkey = creds


# ─── Polymarket Polling Tasks ─────────────────────────────────────────────────

async def _poly_ml_polling_task() -> None:
    """Poll Polymarket moneyline markets every POLY_POLL_SECONDS. Updates _poly_ml_cache."""
    global _poly_ml_cache

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        loop = asyncio.get_running_loop()
        while True:
            cycle_start = loop.time()
            try:
                markets, fetched_at = await fetch_polymarket(session)
                async with _poly_ml_lock:
                    _poly_ml_cache = (markets, fetched_at)
            except Exception as exc:
                print(f"[POLY-ML] Fetch error: {exc}", file=sys.stderr)
            elapsed = loop.time() - cycle_start
            await asyncio.sleep(max(0.0, POLY_POLL_SECONDS - elapsed))


async def _poly_props_polling_task() -> None:
    """
    Poll Polymarket props every POLY_POLL_SECONDS. Merges with Kalshi price cache
    and runs match_props() — this is the trigger point for props arb.
    """
    loop = asyncio.get_running_loop()
    while True:
        cycle_start = loop.time()
        now_utc  = datetime.now(timezone.utc)
        today_str = now_utc.date().strftime("%Y-%m-%d")
        timestamp = now_utc.isoformat()

        _price_cache.purge_old_date(now_utc.date())

        try:
            poly_props = await loop.run_in_executor(None, fetch_poly_props, today_str)
        except Exception as exc:
            sys.stdout.write(f"\r[POLY-PROPS] Fetch error: {exc}".ljust(100) + "\r")
            sys.stdout.flush()
            await asyncio.sleep(max(0.0, POLY_POLL_SECONDS - (loop.time() - cycle_start)))
            continue

        poly_fetch_time = datetime.now(timezone.utc)
        kalshi_props    = _price_cache.as_props_list(now_utc)

        if kalshi_props:
            oldest = min(kp["updated_at"] for kp in kalshi_props)
            skew = (poly_fetch_time - oldest).total_seconds()
            if skew > 60:
                print(
                    f"\n[WARN] Props timestamp skew {skew:.0f}s: oldest Kalshi cache "
                    f"at {oldest.strftime('%H:%M:%S')} UTC, Poly at "
                    f"{poly_fetch_time.strftime('%H:%M:%S')} UTC.",
                    file=sys.stderr,
                )

        arbs = match_props(kalshi_props, poly_props)

        if arbs:
            _emit_prop_arbs(arbs, timestamp)
        else:
            if time.time() - _last_status_log >= _STATUS_LOG_INTERVAL:
                _last_status_log = time.time()
                ts = now_utc.strftime("%H:%M:%S")
                sys.stdout.write(
                    f"\r[{ts}][WS] No props arb — {len(kalshi_props)}K/{len(poly_props)}P props.".ljust(100) + "\r"
                )
                sys.stdout.flush()

        await asyncio.sleep(max(0.0, POLY_POLL_SECONDS - (loop.time() - cycle_start)))


# ─── Polymarket WebSocket Task ────────────────────────────────────────────────
#
# Protocol (from official Polymarket US Python SDK):
#   Subscribe:  {"subscribe": {"requestId": "<id>", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE", "marketSlugs": [...]}}
#   ML update:  {"requestId": "...", "subscriptionType": "...", "marketDataLite": {"marketSlug": "...", "bestBid": "0.54", "bestAsk": "0.46", ...}}
#   Heartbeat:  {"heartbeat": {...}}
#   Error:      {"error": "...", "requestId": "..."}
#
# Maps are keyed by market slug.
# ML map: slug → {slug, title, team_abbr, side, raw, yes_ask, no_ask, updated_at}
# Props map: slug → {smt, player_name, player_norm, line, game_start, event_title, yes_ask, no_ask, updated_at}
# (Each market slug has one YES and one NO side; WS bestAsk is the ask for the YES side,
#  and the NO ask = 1 - bestBid. We store both after each WS update.)

async def _poly_ws_seed_from_rest(session: aiohttp.ClientSession) -> tuple[list[str], list[str]]:
    """
    Fetch Polymarket markets via REST, populate slug-keyed WS maps, and return
    (ml_slugs, props_slugs) for WS subscription.
    Called on startup and on every WS reconnect.
    """
    global _poly_ws_ml_token_map, _poly_ws_props_token_map

    now = datetime.now(timezone.utc)
    ml_slugs: list[str] = []
    props_slugs: list[str] = []

    # ── Moneyline markets ──
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
            print(f"[POLY-WS] ML REST seed error for {slug}: {exc}", file=sys.stderr)

    # ── Props markets ──
    loop = asyncio.get_running_loop()
    today_str = now.date().strftime("%Y-%m-%d")
    supported_smts = set(SERIES_TO_SMT.values())
    from datetime import date as _date
    today = _date.fromisoformat(today_str)
    valid_dates = {today_str, (today - timedelta(days=1)).isoformat()}

    try:
        def _fetch_props_raw() -> list:
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
            return events

        events = await loop.run_in_executor(None, _fetch_props_raw)

        for event in events:
            event_title = event.get("title", "")
            for m in event.get("markets", []):
                try:
                    smt = m.get("sportsMarketType", "")
                    if smt not in supported_smts:
                        continue
                    if not m.get("active") or m.get("closed") or m.get("archived"):
                        continue
                    game_start = m.get("gameStartTime", "") or ""
                    if game_start[:10] not in valid_dates:
                        continue
                    metadata = m.get("metadata") or {}
                    player_name = metadata.get("playerName", "")
                    if not player_name:
                        continue
                    slug = m.get("slug", "")
                    if not slug:
                        continue
                    yes_ask = no_ask = None
                    for side in m.get("marketSides", []):
                        ask = _poly_ask(side)
                        if side.get("long") is True:
                            yes_ask = ask
                        elif side.get("long") is False:
                            no_ask = ask
                    _poly_ws_props_token_map[slug] = {
                        "smt": smt,
                        "player_name": player_name,
                        "player_norm": _normalize(player_name),
                        "line": m.get("line"),
                        "game_start": game_start,
                        "event_title": event_title,
                        "yes_ask": yes_ask,
                        "no_ask": no_ask,
                        "updated_at": now,
                    }
                    props_slugs.append(slug)
                except (TypeError, ValueError, KeyError):
                    continue
    except Exception as exc:
        print(f"[POLY-WS] Props REST seed error: {exc}", file=sys.stderr)

    return ml_slugs, props_slugs


def _rebuild_poly_ml_cache() -> None:
    """
    Rebuild _poly_ml_cache from _poly_ws_ml_token_map (slug-keyed).
    Emits one entry per side (YES team / NO team) in the format match_markets() consumes.
    Must be called while holding _poly_ws_lock (or before WS task starts).
    """
    global _poly_ml_cache
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=_CACHE_STALE_SECONDS)
    markets = []
    latest_update: Optional[datetime] = None

    for entry in _poly_ws_ml_token_map.values():
        updated = entry.get("updated_at")
        if updated is None or updated < stale_cutoff:
            continue
        # Emit YES side (team that wins if YES resolves)
        yes_ask = entry.get("yes_ask")
        if yes_ask and 0 < yes_ask < 1 and entry.get("yes_abbr"):
            markets.append({
                "slug": entry["slug"],
                "title": entry["title"],
                "team_abbr": entry["yes_abbr"],
                "ask": yes_ask,
                "taker_fee": round(yes_ask * POLYMARKET_TAKER_FEE_RATE, 6),
                "raw": entry["raw"],
            })
        # Emit NO side
        no_ask = entry.get("no_ask")
        if no_ask and 0 < no_ask < 1 and entry.get("no_abbr"):
            markets.append({
                "slug": entry["slug"],
                "title": entry["title"],
                "team_abbr": entry["no_abbr"],
                "ask": no_ask,
                "taker_fee": round(no_ask * POLYMARKET_TAKER_FEE_RATE, 6),
                "raw": entry["raw"],
            })
        if latest_update is None or (updated and updated > latest_update):
            latest_update = updated

    fetched_at = latest_update.isoformat() if latest_update else utc_now()
    _poly_ml_cache = (markets, fetched_at)


def _reconstruct_poly_props_list() -> list[dict]:
    """Build poly_props list from WS cache for match_props() consumption."""
    result = []
    for entry in _poly_ws_props_token_map.values():
        if entry.get("yes_ask") is None and entry.get("no_ask") is None:
            continue
        result.append({
            "smt": entry["smt"],
            "player_name": entry["player_name"],
            "player_norm": entry["player_norm"],
            "line": entry["line"],
            "yes_ask": entry.get("yes_ask"),
            "no_ask": entry.get("no_ask"),
            "game_start": entry["game_start"],
            "event_title": entry["event_title"],
        })
    return result


def _run_props_arb_check_from_ws() -> None:
    """Trigger props arb check using current WS cache + Kalshi price cache."""
    now_utc = datetime.now(timezone.utc)
    _price_cache.purge_old_date(now_utc.date())
    kalshi_props = _price_cache.as_props_list(now_utc)
    if not kalshi_props:
        return
    poly_props = _reconstruct_poly_props_list()
    arbs = match_props(kalshi_props, poly_props)
    timestamp = now_utc.isoformat()
    if arbs:
        _emit_prop_arbs(arbs, timestamp)
    else:
        global _last_status_log
        if time.time() - _last_status_log >= _STATUS_LOG_INTERVAL:
            _last_status_log = time.time()
            ts = now_utc.strftime("%H:%M:%S")
            sys.stdout.write(
                f"\r[{ts}][POLY-WS] No props arb — {len(kalshi_props)}K/{len(poly_props)}P props.".ljust(100) + "\r"
            )
            sys.stdout.flush()


async def _handle_poly_ws_message(data: dict) -> None:
    """Dispatch a Polymarket WS message. Expected formats per SDK types.py."""
    if "heartbeat" in data:
        return

    if "error" in data:
        print(f"[POLY-WS] Server error: {data}", file=sys.stderr)
        return

    # Market data lite: {"requestId": ..., "marketDataLite": {"marketSlug": ..., "bestBid": ..., "bestAsk": ...}}
    lite = data.get("marketDataLite")
    if lite:
        slug = lite.get("marketSlug")
        if not slug:
            return
        now = datetime.now(timezone.utc)
        is_ml = is_props = False

        def _extract_price(field) -> Optional[float]:
            if field is None:
                return None
            # field is {"value": "0.1150", "currency": "USD"} or a plain float/str
            if isinstance(field, dict):
                field = field.get("value")
            try:
                v = float(field)
                return v if 0 < v < 1 else None
            except (TypeError, ValueError):
                return None

        yes_ask = _extract_price(lite.get("bestAsk"))
        best_bid = _extract_price(lite.get("bestBid"))
        # NO side ask ≈ 1 - bestBid (the bid on YES is the ask on NO in a binary market)
        no_ask = round(1.0 - best_bid, 6) if best_bid is not None else None
        if no_ask is not None and not (0 < no_ask < 1):
            no_ask = None

        async with _poly_ws_lock:
            if slug in _poly_ws_ml_token_map:
                entry = _poly_ws_ml_token_map[slug]
                if yes_ask is not None:
                    entry["yes_ask"] = yes_ask
                if no_ask is not None:
                    entry["no_ask"] = no_ask
                entry["updated_at"] = now
                _rebuild_poly_ml_cache()
                is_ml = True
            elif slug in _poly_ws_props_token_map:
                entry = _poly_ws_props_token_map[slug]
                if yes_ask is not None:
                    entry["yes_ask"] = yes_ask
                if no_ask is not None:
                    entry["no_ask"] = no_ask
                entry["updated_at"] = now
                is_props = True

        if is_ml:
            check_arb_moneyline(utc_now())
        if is_props:
            _run_props_arb_check_from_ws()
        return

    # Full market data (order book) — not subscribed but handle gracefully
    if "marketData" in data or "trade" in data:
        return


async def _poly_ws_task() -> None:
    """
    Single Polymarket WS connection. Seeds slug maps from REST on every connect,
    then subscribes to SUBSCRIPTION_TYPE_MARKET_DATA_LITE for all MLB market slugs.
    Reconnects with exponential backoff on any error.

    Protocol: {"subscribe": {"requestId": "<id>", "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE", "marketSlugs": [...]}}
    """
    backoff = 1
    _req_counter = itertools.count(1)

    while True:
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                ml_slugs, props_slugs = await _poly_ws_seed_from_rest(session)

            all_slugs = ml_slugs + props_slugs
            print(
                f"[POLY-WS] Seeded {len(ml_slugs)} ML + {len(props_slugs)} props slugs "
                f"({len(all_slugs)} total).",
                file=sys.stderr,
            )

            if not all_slugs:
                print("[POLY-WS] No slugs to subscribe; retrying in 30s.", file=sys.stderr)
                await asyncio.sleep(30)
                continue

            # Prime _poly_ml_cache from REST data before WS arrives
            async with _poly_ws_lock:
                _rebuild_poly_ml_cache()

            auth_headers = _poly_ws_auth_headers()
            async with websockets.connect(
                POLYMARKET_US_WS_URL, additional_headers=auth_headers
            ) as ws:
                backoff = 1

                # Single subscribe to avoid "max subscriptions per connection" errors
                req_id = f"poly-{next(_req_counter)}"
                await ws.send(json.dumps({
                    "subscribe": {
                        "requestId": req_id,
                        "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
                        "marketSlugs": all_slugs,
                    }
                }))

                print(
                    f"[POLY-WS] Connected — subscribed to {len(all_slugs)} slugs.",
                    file=sys.stderr,
                )

                async for raw_msg in ws:
                    try:
                        data = json.loads(raw_msg)
                    except json.JSONDecodeError:
                        continue
                    await _handle_poly_ws_message(data)

        except Exception as exc:
            print(
                f"[POLY-WS] Disconnected: {exc!r}. Reconnecting in {backoff}s...",
                file=sys.stderr,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# ─── Entrypoint ───────────────────────────────────────────────────────────────

async def main_ws(api_key_id: str, private_key) -> None:
    print("Fetching initial Kalshi markets via REST to seed order book + prop cache...", file=sys.stderr)

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        ml_markets, _ = await fetch_kalshi(session)

    ml_tickers = _order_book.seed_from_rest(ml_markets)
    print(f"[INIT] Seeded {len(ml_tickers)} moneyline tickers into order book.", file=sys.stderr)

    loop = asyncio.get_running_loop()
    today_utc = datetime.now(timezone.utc).date()
    try:
        initial_props = await loop.run_in_executor(None, fetch_kalshi_props, today_utc)
        _price_cache.seed_from_rest(initial_props)
        print(f"[INIT] Seeded {len(initial_props)} prop tickers into price cache.", file=sys.stderr)
    except Exception as exc:
        print(f"[WARN] Initial prop REST seed failed: {exc}. WS will populate cache.", file=sys.stderr)

    print(
        f"ws_manager running — Kalshi WS + Poly REST ({POLY_POLL_SECONDS}s). "
        f"Logging to {LOG_FILE}. Ctrl+C to stop."
    )

    await asyncio.gather(
        _kalshi_ws_task(api_key_id, private_key),
        _poly_ws_task(),
        # _poly_ml_polling_task(),    # kept for side-by-side testing; swap with _poly_ws_task() above
        # _poly_props_polling_task(), # kept for side-by-side testing
    )


def main() -> None:
    creds = _load_ws_credentials()
    if creds is None:
        print(
            "[ERROR] Kalshi WS credentials missing.\n"
            "Set KALSHI_KEY_ID and KALSHI_KEY_PATH in .env.\n"
            "For REST fallback, run scanner.py / props_scanner.py instead.",
            file=sys.stderr,
        )
        sys.exit(1)
    api_key_id, private_key = creds
    try:
        asyncio.run(main_ws(api_key_id, private_key))
    except KeyboardInterrupt:
        print("\nws_manager stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
