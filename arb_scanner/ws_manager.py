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
import uuid
from datetime import date, datetime, timezone, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")

import aiohttp
import requests
import websockets
from dotenv import load_dotenv

load_dotenv()

from matchers.baseball import (
    BaseballMatcher,
    normalize_name,
    team_code,
    teams_from_kalshi_title,
    teams_from_kalshi_market,
    kalshi_is_moneyline,
    _kalshi_game_dt_utc,
    _MONEYLINE_REJECT_KEYWORDS,
    _ALIASES,
    _ALIASES_SORTED,
)
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
        "kalshi_tickers": ["KXATPMATCH", "KXWTAMATCH"],
        "poly_slugs": ["atp", "wta"],
        "poly_smt": "tennis_match_winner",
        "matcher_cls": TennisMatcher,
    }
]

GLOBAL_MATCHERS = [cfg["matcher_cls"](arb_threshold=0.96) for cfg in SPORTS_CONFIGS]
ALL_KALSHI_TICKERS = list({t for cfg in SPORTS_CONFIGS for t in cfg["kalshi_tickers"]})


# ─── Config ────────────────────────────────────────────────────────────────────

KALSHI_WS_URL         = "wss://api.elections.kalshi.com/trade-api/ws/v2"
KALSHI_BASE           = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"
ARB_THRESHOLD         = 0.96
KALSHI_TAKER_FEE_RATE = 0.01
POLYMARKET_TAKER_FEE_RATE = 0.01
POLY_POLL_SECONDS     = 2
LOG_FILE              = "arb_log.jsonl"

# Ghost-filter thresholds — Layer 1 quote-sanity filters applied in match_props
# (docs/superpowers/specs/2026-07-02-ghost-free-arbs-design.md)
GHOST_PIN_PROB             = 0.97   # F1: leg implied ≥97% resolved → skip pair
GHOST_MID_DISAGREEMENT_MAX = 0.15   # F2: max venue-mid divergence
GHOST_MAX_GAP_CENTS        = 10.0   # F3: edges fatter than this are presumed fake
GHOST_MAX_SPREAD           = 0.20   # F4: max per-venue bid/ask spread
GHOST_LOG_FILE             = "ghost_log.jsonl"
_GHOST_LOG_COOLDOWN_SECONDS = 60    # per (ticker, direction, reason) dedupe window
_GHOST_SUMMARY_INTERVAL_SECONDS = 3600
_WS_SUBSCRIBE_CHUNK_SIZE = 50
_PROP_RESUB_INTERVAL_SECONDS = 60
_CACHE_STALE_SECONDS  = 300
_MAX_TIMESTAMP_SKEW_SECONDS = 10
REQUEST_TIMEOUT       = aiohttp.ClientTimeout(total=8)
REST_TIMEOUT          = 8

# Kalshi prop series → Polymarket sportsMarketType
SERIES_TO_SMT = {
    "KXMLBHIT": "baseball_player_hits",
    "KXMLBHR":  "baseball_player_home_runs",
    "KXMLBTB":  "baseball_player_total_bases",
    # New MLB series — verified 2026-06-30 against live APIs:
    # Kalshi titles: "Andrew Abbott: 7+ strikeouts?" / "Matt Olson: 5+ hits + runs + RBIs?" /
    #   "Andrew Abbott: 17+ Outs Recorded?" — all match ^(.+?):\s*(\d+)\+ ✓
    # Polymarket SMT strings confirmed via /v1/search?query=mlb+will+record+at+least ✓
    "KXMLBKS":   "baseball_player_strikeouts",
    "KXMLBHRR":  "baseball_player_hits_runs_rbis",
    "KXMLBOUTS": "baseball_player_outs",
    "KXNBAPTS": "basketball_player_points",
    "KXNBAREB": "basketball_player_rebounds",
    "KXNBAAST": "basketball_player_assists",
    "KXNBA3PT": "basketball_player_threes",
}

# ─── Helpers ──────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(obj: dict) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(obj) + "\n")


def _append_ghost_log(obj: dict) -> None:
    # Separate file from arb_log.jsonl: suppressed ghosts are pattern-analysis
    # data, not opportunities — keeping them apart keeps arb_log tradeable-only.
    with open(GHOST_LOG_FILE, "a") as f:
        f.write(json.dumps(obj) + "\n")


# Used only by today_tickers() for date-range filtering (date() comparison only).
# Intentionally NOT ET-corrected — a 4-5h UTC error is irrelevant at date granularity.
def _kalshi_game_dt(ticker: str) -> Optional[datetime]:
    try:
        segment = ticker.split("-")[1]
        return datetime.strptime(segment[:11], "%y%b%d%H%M").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def _kalshi_parse_title(title: str) -> Optional[tuple[str, int]]:
    m = re.match(r"^(.+?):\s*(\d+)\+", title)
    if not m:
        return None
    return m.group(1).strip(), int(m.group(2))


def _kalshi_ask(market: dict, side: str) -> Optional[float]:
    for field, scale in ((f"{side}_ask_dollars", 1.0), (f"{side}_ask", 100.0)):
        raw = market.get(field)
        if raw is not None:
            try:
                val = float(raw) / scale
                return val if 0 < val < 1 else None
            except (TypeError, ValueError):
                return None
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
    In-memory two-sided order book for Kalshi moneyline tickers.
    Kalshi WS v2 book levels are resting BIDS per side (yes_dollars_fp /
    no_dollars_fp as [price_dollars, qty_fp] string pairs) — the best YES ask
    is derived as 1 - max(NO bid). Seq numbers are per WS subscription (sid),
    not per ticker.
    """

    def __init__(self):
        # ticker → {"yes": {price_cents: qty}, "no": {price_cents: qty}}
        self._books: dict[str, dict[str, dict[int, float]]] = {}
        self._best_ask: dict[str, Optional[float]] = {}   # ticker → best ask USD
        self._sid_seq: dict[int, int] = {}                # sid → last seq seen
        self._updated_at: dict[str, datetime] = {}
        self._metadata: dict[str, dict] = {}              # ticker → {title, raw}
        self._prop_tickers: set[str] = set()

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

    def sync_prop_tickers(self, tickers) -> None:
        """Register the live prop-ticker set for the book channel. Tickers
        dropped from the set have their book state evicted; metadata for
        props lives in KalshiPriceCache and is never touched here."""
        new_tickers = set(tickers)
        for ticker in self._prop_tickers - new_tickers:
            self._books.pop(ticker, None)
            self._best_ask.pop(ticker, None)
            self._updated_at.pop(ticker, None)
        self._prop_tickers = new_tickers

    @staticmethod
    def _parse_levels(levels) -> dict[int, float]:
        """Parse [["0.5800", "608.52"], ...] dollar-string levels → {cents: qty}."""
        book: dict[int, float] = {}
        for entry in levels or []:
            try:
                price = round(float(entry[0]) * 100)
                qty = float(entry[1])
            except (IndexError, TypeError, ValueError):
                continue
            if qty > 0 and 0 < price < 100:
                book[price] = qty
        return book

    def reset_connection(self) -> None:
        """Clear per-sid seq state and drop prop book state. Call on every
        (re)connect before subscribing. ML books keep their state across the
        gap — as_kalshi_markets() has the _CACHE_STALE_SECONDS backstop to
        evict anything that goes stale — but props are book-or-nothing (no
        such backstop by design), so a retained book would serve pre-gap
        quotes for up to the reconnect+resubscribe window; dropping it here
        makes props go dark until fresh snapshots arrive."""
        self._sid_seq.clear()
        for ticker in self._prop_tickers:
            self._books.pop(ticker, None)
            self._best_ask.pop(ticker, None)
            self._updated_at.pop(ticker, None)

    def _check_seq(self, sid: Optional[int], seq: Optional[int]) -> bool:
        """Validate + advance the per-sid seq counter. Returns False on a gap."""
        if sid is not None and seq is not None:
            last = self._sid_seq.get(sid)
            if last is not None and seq != last + 1:
                return False
            self._sid_seq[sid] = seq
        return True

    def note_seq(self, sid: Optional[int], seq: Optional[int]) -> bool:
        """Consume a seq number from a non-book frame (subscribed/ok ack)
        that shares the sid's seq stream (rule 19). Returns False on a gap."""
        return self._check_seq(sid, seq)

    def apply_snapshot(self, sid: Optional[int], seq: Optional[int], payload: dict) -> bool:
        """
        Replace book with full snapshot from an orderbook_snapshot payload.
        Returns False on a seq gap — the snapshot only resets THIS ticker's
        book, but seq is per-sid across all tickers, so the missed messages
        may have been deltas for other tickers on the stream.
        """
        if not self._check_seq(sid, seq):
            return False
        ticker = payload.get("market_ticker")
        if not ticker or (ticker not in self._metadata and ticker not in self._prop_tickers):
            return True
        self._books[ticker] = {
            "yes": self._parse_levels(payload.get("yes_dollars_fp")),
            "no": self._parse_levels(payload.get("no_dollars_fp")),
        }
        self._updated_at[ticker] = datetime.now(timezone.utc)
        self._recompute_best_ask(ticker)
        return True

    def apply_delta(self, sid: Optional[int], seq: Optional[int], payload: dict) -> bool:
        """
        Apply a single orderbook_delta payload. Returns False on a seq gap —
        the caller must reconnect for fresh snapshots (a gap on a sid stream
        invalidates every book on that stream, not just this ticker's).
        """
        if not self._check_seq(sid, seq):
            return False

        ticker = payload.get("market_ticker")
        if not ticker or (ticker not in self._metadata and ticker not in self._prop_tickers):
            return True  # unknown ticker, silently skip

        side = payload.get("side")
        if side not in ("yes", "no"):
            return True
        try:
            price = round(float(payload.get("price_dollars")) * 100)
            delta = float(payload.get("delta_fp"))
        except (TypeError, ValueError):
            return True
        if not 0 < price < 100:
            return True

        book = self._books.setdefault(ticker, {"yes": {}, "no": {}})[side]
        new_qty = book.get(price, 0.0) + delta
        if new_qty <= 0:
            book.pop(price, None)
        else:
            book[price] = new_qty

        self._updated_at[ticker] = datetime.now(timezone.utc)
        self._recompute_best_ask(ticker)
        return True

    def _recompute_best_ask(self, ticker: str) -> None:
        # Book sides are resting bids: best YES ask = 1 - best NO bid.
        no_bids = self._books.get(ticker, {}).get("no", {})
        positive = [p for p, q in no_bids.items() if q > 0]
        self._best_ask[ticker] = (100 - max(positive)) / 100.0 if positive else None

    def as_kalshi_markets(self, now: datetime) -> list[dict]:
        """Return normalized market dicts in the format matcher.match() expects."""
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

    def prop_quotes(self) -> dict[str, dict]:
        """Best yes/no ask + resting qty for registered prop tickers, derived
        the same way as ML (ask = 1 - opposing side's best bid) but kept out
        of as_kalshi_markets() — props are matched via match_props, not
        matcher.match()."""
        result = {}
        for ticker in self._prop_tickers:
            book = self._books.get(ticker)
            if not book:
                continue
            yes_levels = book.get("yes", {})
            no_levels = book.get("no", {})
            yes_bid_c = max(yes_levels) if yes_levels else None
            no_bid_c = max(no_levels) if no_levels else None
            yes_ask = (100 - no_bid_c) / 100 if no_bid_c is not None else None
            no_ask = (100 - yes_bid_c) / 100 if yes_bid_c is not None else None
            if yes_ask is None and no_ask is None:
                continue
            result[ticker] = {
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "yes_ask_qty": no_levels.get(no_bid_c) if no_bid_c is not None else None,
                "no_ask_qty": yes_levels.get(yes_bid_c) if yes_bid_c is not None else None,
                "updated_at": self._updated_at.get(ticker),
            }
        return result


# ─── Props Price Cache (inlined from props_scanner.py) ────────────────────────

class KalshiPriceCache:
    """
    Metadata authority for Kalshi props (player/line/game_dt_utc/eviction),
    seeded and refreshed via REST only. The props arb path reads prices/qty
    from the order book (orderbook_delta channel), not from here.
    """

    def __init__(self):
        self._cache: dict[str, dict] = {}
        self._cache_date: Optional["datetime.date"] = None

    def seed_from_rest(self, props: list[dict], authoritative_series: Optional[set] = None) -> None:
        """
        Merge a REST pull into the cache. REST is authoritative for the series
        it successfully fetched: prices are overwritten (None means "no live
        quote", not "keep the old price") and markets that vanished from the
        status=open listing (settled/closed) are evicted — otherwise they
        linger up to _CACHE_STALE_SECONDS serving ghost prices.
        """
        self.purge_old_date(datetime.now(timezone.utc).date())
        now = datetime.now(timezone.utc)
        if authoritative_series:
            new_tickers = {kp["ticker"] for kp in props}
            self._cache = {
                t: e for t, e in self._cache.items()
                if e["series"] not in authoritative_series or t in new_tickers
            }
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
                entry["yes_ask"] = kp["yes_ask"]
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
        # Skip props where the game started more than 4 hours ago — those markets
        # are mid-game/finished and WS prices lag REST, producing ghost arbs.
        stale_game_cutoff = now - timedelta(hours=4)
        # Skip entries whose price hasn't been refreshed recently — protects against
        # "seeded via REST once, WS ticker never arrived" ghost prices.
        stale_price_cutoff = now - timedelta(seconds=_CACHE_STALE_SECONDS)
        return [
            e for e in self._cache.values()
            if not (e["yes_ask"] is None and e["no_ask"] is None)
            and e["game_dt_utc"] >= stale_game_cutoff
            and e.get("updated_at") is not None
            and e["updated_at"] >= stale_price_cutoff
        ]

    def today_tickers(self, today: "datetime.date") -> list[str]:
        yesterday = today - timedelta(days=1)
        return [t for t, e in self._cache.items() if e["game_dt_utc"].date() >= yesterday]

    def metadata_entries(self) -> list[dict]:
        """Raw cached entry dicts — metadata authority (series/ticker/
        player_name/player_norm/line/game_dt_utc). No freshness filtering;
        the arb path reads prices/qty from the order book, not this cache."""
        return list(self._cache.values())


# ─── Arb Logic ────────────────────────────────────────────────────────────────

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
                "kalshi_team": last_m.get("kalshi_team"),
                "kalshi_ask": last_m.get("kalshi_ask"),
                "kalshi_taker_fee": last_m.get("kalshi_taker_fee"),
                "polymarket_slug": last_m.get("polymarket_slug", key[1]),
                "polymarket_team": last_m.get("polymarket_team"),
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
            # ML arb = buy YES on kalshi_team at Kalshi + YES on the opposing
            # polymarket_team at Poly — say which side is bought on which site
            "kalshi_team": m.get("kalshi_team"),
            "kalshi_ask": m["kalshi_ask"],
            "kalshi_taker_fee": m["kalshi_taker_fee"],
            "polymarket_slug": m["polymarket_slug"],
            "polymarket_team": m.get("polymarket_team"),
            "polymarket_ask": m["polymarket_ask"],
            "polymarket_taker_fee": m["polymarket_taker_fee"],
            "total_cost": m["total_cost"],
            "gap_cents": m["gap_cents"],
            "duration_seconds": duration_seconds,
        }


def _ghost_filter_reason(kp: dict, pp: dict) -> Optional[str]:
    """
    Layer-1 quote-sanity filters (F1/F2/F4 of the ghost-free arbs design).
    Returns the first failing reason for a (Kalshi, Poly) pair, or None if the
    pair is executable-grade. F3 (edge cap) is per-direction and lives in
    match_props. Both venues quote the YES side only (rule 18 / marketDataLite):
    yes_bid = 1 - no_ask, mid = (yes_ask + yes_bid) / 2.
    """
    sides = []
    for leg in (kp, pp):
        yes_ask = leg.get("yes_ask")
        no_ask = leg.get("no_ask")
        yes_bid = round(1.0 - no_ask, 4) if no_ask is not None else None
        # F1 — pinned: leg priced as effectively resolved. Checked before the
        # two-sided requirement so a resolved market with a missing side still
        # reads "pinned" (the ghost taxonomy's dominant class), not "one_sided".
        if yes_bid is not None and yes_bid >= GHOST_PIN_PROB:
            return "pinned"
        if yes_ask is not None and yes_ask <= round(1.0 - GHOST_PIN_PROB, 4):
            return "pinned"
        sides.append((yes_ask, yes_bid))
    # F2 precondition — both venues two-sided, else mids aren't computable
    if any(v is None for side in sides for v in side):
        return "one_sided"
    # F4 — spread sanity: crossed (< 0) means stale, canyon-wide means dead book
    for yes_ask, yes_bid in sides:
        spread = round(yes_ask - yes_bid, 4)
        if spread < 0 or spread > GHOST_MAX_SPREAD:
            return "spread"
    # F2 — mid agreement: 30¢+ canyons mean one book is stale; real cross-venue
    # mispricings are small divergences
    mid_k = (sides[0][0] + sides[0][1]) / 2
    mid_p = (sides[1][0] + sides[1][1]) / 2
    if round(abs(mid_k - mid_p), 4) > GHOST_MID_DISAGREEMENT_MAX:
        return "mid_disagreement"
    return None


class GhostFilterStats:
    """
    Per-reason suppression counters + deduped ghost_log.jsonl writer + hourly
    ghost_filter_summary event. Counters count suppressed would-be arbs (pairs
    that beat ARB_THRESHOLD but failed a filter), not pairs scanned — so the
    numbers stay comparable to the old ghost_rejected volume.
    """

    REASONS = ("pinned", "mid_disagreement", "edge_cap", "spread", "one_sided")

    def __init__(self):
        self.totals = {r: 0 for r in self.REASONS}
        self._window = {r: 0 for r in self.REASONS}
        self._last_summary_at = time.time()
        # (kalshi_ticker, direction, reason) → epoch of last ghost_log write
        self._log_cooldown: dict[tuple, float] = {}

    def record(self, reason: str, record: dict) -> None:
        self.totals[reason] += 1
        self._window[reason] += 1
        key = (record.get("kalshi_ticker"), record.get("direction"), reason)
        now = time.time()
        if now - self._log_cooldown.get(key, 0.0) >= _GHOST_LOG_COOLDOWN_SECONDS:
            self._log_cooldown[key] = now
            _append_ghost_log(record)

    def maybe_emit_summary(self) -> None:
        now = time.time()
        if now - self._last_summary_at < _GHOST_SUMMARY_INTERVAL_SECONDS:
            return
        window_seconds = round(now - self._last_summary_at, 1)
        self._last_summary_at = now
        log_event({
            "event": "ghost_filter_summary",
            "timestamp": utc_now(),
            "window_seconds": window_seconds,
            "suppressed": dict(self._window),
            "totals": dict(self.totals),
        })
        self._window = {r: 0 for r in self.REASONS}

    def total_suppressed(self) -> int:
        return sum(self.totals.values())

    def status_summary(self) -> str:
        t = self.totals
        return (f"ghosts {self.total_suppressed()} "
                f"(pin {t['pinned']}/mid {t['mid_disagreement']}/edge {t['edge_cap']}"
                f"/spr {t['spread']}/1side {t['one_sided']})")


_ghost_stats = GhostFilterStats()


def match_props(kalshi_props: list[dict], poly_props: list[dict]) -> list[dict]:
    # Key on date only, keep all candidates per key: doubleheaders repeat the
    # same (player, line, date) — the right game is picked by start time below.
    poly_index: dict[tuple, list[dict]] = {}
    for pp in poly_props:
        game_date = pp["game_start"][:10]
        key = (pp["smt"], pp["player_norm"], pp["line"], game_date)
        poly_index.setdefault(key, []).append(pp)

    matches = []
    for kp in kalshi_props:
        smt = SERIES_TO_SMT[kp["series"]]
        game_date = kp["game_dt_utc"].strftime("%Y-%m-%d")
        key = (smt, kp["player_norm"], kp["line"], game_date)
        pp = None
        best_diff = None
        for cand in poly_index.get(key, []):
            try:
                p_dt = datetime.fromisoformat(cand["game_start"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue
            diff = abs((kp["game_dt_utc"] - p_dt).total_seconds())
            # Same 30-min window the moneyline matcher uses (rule 5)
            if diff <= 30 * 60 and (best_diff is None or diff < best_diff):
                best_diff = diff
                pp = cand
        if pp is None:
            continue

        k_yes, k_no = kp["yes_ask"], kp["no_ask"]
        p_yes, p_no = pp["yes_ask"], pp["no_ask"]
        stat_type = smt.replace("baseball_player_", "").replace("basketball_player_", "")
        # Pair-level filter verdict (F1/F2/F4) — computed lazily at the first
        # sub-threshold direction so clean non-arb pairs cost nothing.
        pair_reason_computed = False
        pair_reason = None

        for direction, leg1_name, leg1_ask, leg1_fee_rate, leg2_name, leg2_ask, leg2_fee_rate, kalshi_qty in [
            ("Poly YES + Kalshi NO", "Poly YES", p_yes, POLYMARKET_TAKER_FEE_RATE,
             "Kalshi NO", k_no, KALSHI_TAKER_FEE_RATE, kp.get("no_ask_qty")),
            ("Kalshi YES + Poly NO", "Kalshi YES", k_yes, KALSHI_TAKER_FEE_RATE,
             "Poly NO", p_no, POLYMARKET_TAKER_FEE_RATE, kp.get("yes_ask_qty")),
        ]:
            if leg1_ask is None or leg2_ask is None:
                continue
            # Per-leg taker fees (CLAUDE.md: total_cost = asks + 1% of each ask)
            total = leg1_ask * (1 + leg1_fee_rate) + leg2_ask * (1 + leg2_fee_rate)
            if total >= ARB_THRESHOLD:
                continue
            gap_cents = round((ARB_THRESHOLD - total) * 100, 2)

            # Ghost filters — gate every would-be arb (replaces the cosmetic
            # `suspicious` flag, which tagged but never suppressed)
            if not pair_reason_computed:
                pair_reason = _ghost_filter_reason(kp, pp)
                pair_reason_computed = True
            reason = pair_reason
            if reason is None and gap_cents > GHOST_MAX_GAP_CENTS:
                reason = "edge_cap"  # F3: too-good-to-be-true edge
            if reason is not None:
                _ghost_stats.record(reason, {
                    "event": "ghost_suppressed",
                    "timestamp": utc_now(),
                    "reason": reason,
                    "direction": direction,
                    "kalshi_ticker": kp["ticker"],
                    "player_name": pp["player_name"],
                    "stat_type": stat_type,
                    "line": kp["line"],
                    "game_start": pp["game_start"],
                    "kalshi_yes_ask": k_yes,
                    "kalshi_no_ask": k_no,
                    "poly_yes_ask": p_yes,
                    "poly_no_ask": p_no,
                    "total_cost": round(total, 4),
                    "gap_cents": gap_cents,
                })
                continue

            matches.append({
                "direction": direction,
                "event_title": pp["event_title"],
                "game_start": pp["game_start"],
                "player_name": pp["player_name"],
                "stat_type": stat_type,
                "line": kp["line"],
                "leg1": leg1_name,
                "leg1_ask": leg1_ask,
                "leg2": leg2_name,
                "leg2_ask": leg2_ask,
                "total_cost": round(total, 4),
                "gap_cents": gap_cents,
                "kalshi_ticker": kp["ticker"],
                "poly_smt": smt,
                # Raw Poly WS prices at match time — logged for post-hoc staleness analysis
                "poly_ws_yes_ask": pp.get("yes_ask"),
                "poly_ws_no_ask": pp.get("no_ask"),
                "kalshi_qty_at_best": kalshi_qty,
            })

    _ghost_stats.maybe_emit_summary()
    matches.sort(key=lambda x: -x["gap_cents"])
    return matches


# ─── Kalshi REST Confirm Gate ─────────────────────────────────────────────────

_KALSHI_CONFIRM_COOLDOWN_SECONDS = 5
# keyed by (kalshi_ticker, direction); value = epoch seconds of last confirm attempt
_kalshi_confirm_cooldown: dict[tuple, float] = {}


async def _rest_confirm_and_emit(arb: dict, timestamp: str) -> None:
    """
    Background task: REST-confirm a new prop arb candidate against Kalshi's single-market
    endpoint. Emits via _emit_prop_arbs if still an arb; logs ghost_rejected otherwise.
    Must not be called inline from the WS handler — always via asyncio.create_task.
    """
    ticker = arb["kalshi_ticker"]
    direction = arb["direction"]
    url = f"{KALSHI_BASE}/markets/{ticker}"
    ws_kalshi_ask = arb["leg1_ask"] if "Kalshi" in arb["leg1"] else arb["leg2_ask"]

    try:
        loop = asyncio.get_running_loop()
        def _fetch():
            resp = requests.get(url, timeout=REST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()
        data = await loop.run_in_executor(None, _fetch)
        market = data.get("market", data)

        # Determine confirmed Kalshi ask for the relevant side
        if "Kalshi YES" in direction:
            confirmed_k_ask = _kalshi_ask(market, "yes")
        else:
            confirmed_k_ask = _kalshi_ask(market, "no")

        if confirmed_k_ask is None:
            log_event({
                "event": "ghost_rejected",
                "timestamp": utc_now(),
                "reason": "no_quote_from_rest",
                "kalshi_ticker": ticker,
                "direction": direction,
                "ws_kalshi_ask": ws_kalshi_ask,
                "rest_kalshi_ask": None,
                "poly_ws_yes_ask": arb.get("poly_ws_yes_ask"),
                "poly_ws_no_ask": arb.get("poly_ws_no_ask"),
            })
            return

        # Recompute with the same fee-inclusive formula match_props uses
        poly_ask = arb["leg2_ask"] if "Kalshi" in arb["leg1"] else arb["leg1_ask"]
        total = (confirmed_k_ask * (1 + KALSHI_TAKER_FEE_RATE)
                 + poly_ask * (1 + POLYMARKET_TAKER_FEE_RATE))

        if total >= ARB_THRESHOLD:
            log_event({
                "event": "ghost_rejected",
                "timestamp": utc_now(),
                "reason": "threshold_not_met_after_rest",
                "kalshi_ticker": ticker,
                "direction": direction,
                "ws_kalshi_ask": ws_kalshi_ask,
                "rest_kalshi_ask": confirmed_k_ask,
                "ws_total": round(ws_kalshi_ask * (1 + KALSHI_TAKER_FEE_RATE)
                                  + poly_ask * (1 + POLYMARKET_TAKER_FEE_RATE), 4),
                "rest_total": round(total, 4),
                "poly_ws_yes_ask": arb.get("poly_ws_yes_ask"),
                "poly_ws_no_ask": arb.get("poly_ws_no_ask"),
            })
            return

        # F3 edge cap on the REST-recomputed gap too — a stale-low REST price
        # must not reopen the fat-edge ghost class the WS-side filters gate
        # (design success criterion 2: no open arb shows a >10¢ gap).
        rest_gap_cents = round((ARB_THRESHOLD - total) * 100, 2)
        if rest_gap_cents > GHOST_MAX_GAP_CENTS:
            log_event({
                "event": "ghost_rejected",
                "timestamp": utc_now(),
                "reason": "edge_cap_after_rest",
                "kalshi_ticker": ticker,
                "direction": direction,
                "ws_kalshi_ask": ws_kalshi_ask,
                "rest_kalshi_ask": confirmed_k_ask,
                "rest_total": round(total, 4),
                "rest_gap_cents": rest_gap_cents,
                "poly_ws_yes_ask": arb.get("poly_ws_yes_ask"),
                "poly_ws_no_ask": arb.get("poly_ws_no_ask"),
            })
            return

        # Confirmed — patch in the REST-verified Kalshi ask, insert directly into
        # tracker without diffing (_emit_prop_arbs([single_arb]) would close all others).
        confirmed_arb = dict(arb)
        if "Kalshi" in arb["leg1"]:
            confirmed_arb["leg1_ask"] = confirmed_k_ask
        else:
            confirmed_arb["leg2_ask"] = confirmed_k_ask
        confirmed_arb["total_cost"] = round(total, 4)
        confirmed_arb["gap_cents"] = rest_gap_cents

        if not _prop_arb_tracker.mark_opened(confirmed_arb, timestamp):
            return  # Race: already tracked from a concurrent WS tick — skip duplicate

        print(f"\n{'='*70}")
        print(f"  PROP ARB [REST-CONFIRMED] — {timestamp}")
        print(f"{'='*70}")
        _print_and_log_prop_open(confirmed_arb, timestamp)

    except Exception as exc:
        log_event({
            "event": "ghost_rejected",
            "timestamp": utc_now(),
            "reason": "rest_error",
            "kalshi_ticker": ticker,
            "direction": direction,
            "ws_kalshi_ask": ws_kalshi_ask,
            "error": str(exc),
            "poly_ws_yes_ask": arb.get("poly_ws_yes_ask"),
            "poly_ws_no_ask": arb.get("poly_ws_no_ask"),
        })


class PropArbTracker:
    """
    Tracks open prop arbs across emit calls.
    Prints CLOSED when an arb disappears; shows still-active summary on request.
    """

    def __init__(self):
        # key → {arb dict, opened_at, last_prices}
        self._open: dict[tuple, dict] = {}

    def _key(self, arb: dict) -> tuple:
        return (arb["kalshi_ticker"], arb["direction"])

    def update(self, arbs: list[dict], timestamp: str) -> tuple[list[dict], list[dict]]:
        """
        Diff current arbs against open set.
        Returns (new_or_changed, closed) lists.
        """
        current_keys = {self._key(a): a for a in arbs}
        new_or_changed = []
        closed = []

        for key, arb in current_keys.items():
            price_sig = (arb["leg1_ask"], arb["leg2_ask"])
            if key not in self._open:
                self._open[key] = {"arb": arb, "opened_at": timestamp, "last_prices": price_sig}
                new_or_changed.append(("opened", arb))
            elif self._open[key]["last_prices"] != price_sig:
                self._open[key]["last_prices"] = price_sig
                self._open[key]["arb"] = arb
                new_or_changed.append(("updated", arb))

        for key in set(self._open) - set(current_keys):
            rec = self._open.pop(key)
            closed.append(rec)

        return new_or_changed, closed

    def mark_opened(self, arb: dict, timestamp: str) -> bool:
        """
        Insert a confirmed arb directly into _open without touching other entries.
        Returns True if newly inserted, False if already tracked (idempotent).
        Use this from async confirm tasks — never route a single confirmed arb through
        update(), which would close every other open entry not present in that call.
        """
        key = self._key(arb)
        if key in self._open:
            return False
        price_sig = (arb["leg1_ask"], arb["leg2_ask"])
        self._open[key] = {"arb": arb, "opened_at": timestamp, "last_prices": price_sig}
        return True

    def active(self) -> list[dict]:
        return [rec["arb"] for rec in self._open.values()]

    def active_count(self) -> int:
        return len(self._open)


_prop_arb_tracker = PropArbTracker()

# Console throttle for [UPD] lines: live WS ticks change an open arb's prices
# every second — print each arb's updates at most this often. Every update is
# still logged to arb_log.jsonl; only stdout is rate-limited.
_PROP_UPDATE_PRINT_SECONDS = 30
# (kalshi_ticker, direction) → epoch of last [NEW]/[UPD] print; pruned on close
_prop_update_last_print: dict[tuple, float] = {}


def _print_and_log_prop_open(arb: dict, timestamp: str) -> None:
    """Shared formatter for a single confirmed prop arb open — print + log_event."""
    _prop_update_last_print[(arb["kalshi_ticker"], arb["direction"])] = time.time()
    print(f"\n--- {arb['event_title']} (game: {arb['game_start']}) ---")
    print(
        f"  [NEW][{arb['gap_cents']:.1f}¢] {arb['player_name']} "
        f"{arb['line']}+ {arb['stat_type']}: "
        f"{arb['leg1']}={arb['leg1_ask']:.2f} + "
        f"{arb['leg2']}={arb['leg2_ask']:.2f} = "
        f"${arb['total_cost']:.2f}"
    )
    print(f"         Kalshi: {arb['kalshi_ticker']}")
    log_event({
        "event": "prop_arb",
        "timestamp": timestamp,
        **arb,
        "poly_ws_yes_ask": arb.get("poly_ws_yes_ask"),
        "poly_ws_no_ask": arb.get("poly_ws_no_ask"),
    })


def _emit_prop_arbs(arbs: list[dict], timestamp: str) -> None:
    """
    Print prop arb changes (new/updated/closed) and log them.
    Also prints a still-active summary when arbs close or prices change.
    """
    new_or_changed, closed = _prop_arb_tracker.update(arbs, timestamp)

    # Log every update, but only print each arb at most once per
    # _PROP_UPDATE_PRINT_SECONDS — WS ticks change prices every second and the
    # terminal shouldn't scroll a block per tick.
    now_epoch = time.time()
    printable = []
    for status, arb in new_or_changed:
        if status == "opened":
            printable.append((status, arb))
            continue
        log_event({
            "event": "prop_arb",
            "timestamp": timestamp,
            **arb,
            "poly_ws_yes_ask": arb.get("poly_ws_yes_ask"),
            "poly_ws_no_ask": arb.get("poly_ws_no_ask"),
        })
        key = (arb["kalshi_ticker"], arb["direction"])
        if now_epoch - _prop_update_last_print.get(key, 0.0) >= _PROP_UPDATE_PRINT_SECONDS:
            _prop_update_last_print[key] = now_epoch
            printable.append((status, arb))

    if printable:
        print(f"\n{'='*70}")
        print(f"  PROP ARB — {timestamp}")
        print(f"{'='*70}")
        for status, arb in printable:
            if status == "opened":
                _print_and_log_prop_open(arb, timestamp)
            else:
                print(f"\n--- {arb['event_title']} (game: {arb['game_start']}) ---")
                print(
                    f"  [UPD][{arb['gap_cents']:.1f}¢] {arb['player_name']} "
                    f"{arb['line']}+ {arb['stat_type']}: "
                    f"{arb['leg1']}={arb['leg1_ask']:.2f} + "
                    f"{arb['leg2']}={arb['leg2_ask']:.2f} = "
                    f"${arb['total_cost']:.2f}"
                )
                print(f"         Kalshi: {arb['kalshi_ticker']}")

    # Print closed arbs
    if closed:
        print(f"\n{'─'*70}")
        print(f"  PROP ARB CLOSED — {timestamp}")
        for rec in closed:
            arb = rec["arb"]
            _prop_update_last_print.pop((arb["kalshi_ticker"], arb["direction"]), None)
            duration = ""
            try:
                dt = (datetime.fromisoformat(timestamp) - datetime.fromisoformat(rec["opened_at"])).total_seconds()
                duration = f" (open {int(dt)}s)"
            except Exception:
                pass
            print(
                f"  [CLOSED{duration}] {arb['player_name']} {arb['line']}+ {arb['stat_type']}: "
                f"{arb['leg1']}={arb['leg1_ask']:.2f} + {arb['leg2']}={arb['leg2_ask']:.2f}"
            )
            print(f"         Kalshi: {arb['kalshi_ticker']}")

        # Show what's still open after closures
        still_active = _prop_arb_tracker.active()
        if still_active:
            print(f"\n  Still active ({len(still_active)}):")
            for arb in still_active:
                print(
                    f"    [{arb['gap_cents']:.1f}¢] {arb['player_name']} {arb['line']}+ "
                    f"{arb['stat_type']} — {arb['leg1']}={arb['leg1_ask']:.2f} + "
                    f"{arb['leg2']}={arb['leg2_ask']:.2f}"
                )
        else:
            print("  No prop arbs remaining.")
        print(f"{'─'*70}")

    # new_or_changed events are already logged by _print_and_log_prop_open (opened)
    # or log_event (updated) above. Only closures need logging here.
    for rec in closed:
        arb = rec["arb"]
        log_event({
            "event": "prop_arb_closed",
            "timestamp": timestamp,
            "opened_at": rec["opened_at"],
            **arb,
            "poly_ws_yes_ask": arb.get("poly_ws_yes_ask"),
            "poly_ws_no_ask": arb.get("poly_ws_no_ask"),
        })


# ─── REST Fetch Functions ──────────────────────────────────────────────────────

async def fetch_kalshi(session: aiohttp.ClientSession) -> tuple[list[dict], str]:
    markets = []
    fetched_at = utc_now()
    for ticker in ALL_KALSHI_TICKERS:
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
                        team_name = normalize_name(
                            team.get("displayName", "") or team.get("name", "") or team_abbr
                        )

                        quote = side.get("quote", {})
                        ask = float(quote.get("value", 0)) if quote else 0.0
                        if ask <= 0 or ask >= 1:
                            continue
                        taker_fee = round(ask * POLYMARKET_TAKER_FEE_RATE, 6)
                        markets.append({
                            "slug": m.get("slug", ""),
                            "title": m.get("question") or m.get("slug", ""),
                            "team_abbr": team_abbr,
                            "team_name": team_name,
                            "ask": ask,
                            "taker_fee": taker_fee,
                            "raw": m,
                        })
                except (TypeError, ValueError, KeyError):
                    continue

    return markets, fetched_at


def fetch_kalshi_props(today_utc: "datetime.date") -> tuple[list[dict], set]:
    """
    Fetch open prop markets for all series. Returns (props, ok_series) where
    ok_series is the set of series fetched completely (all pages) — only those
    are authoritative for cache eviction in seed_from_rest().
    """
    valid_dates = {today_utc, today_utc - timedelta(days=1)}
    results = []
    ok_series: set = set()
    for series in SERIES_TO_SMT:
        markets: list[dict] = []
        cursor = None
        try:
            while True:
                params = {"series_ticker": series, "status": "open", "limit": 200}
                if cursor:
                    params["cursor"] = cursor
                resp = requests.get(f"{KALSHI_BASE}/markets", params=params, timeout=REST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                markets.extend(data.get("markets", []))
                cursor = data.get("cursor")
                if not cursor or not data.get("markets"):
                    break
        except requests.RequestException as exc:
            print(f"[WARN] Kalshi {series} fetch failed: {exc}", file=sys.stderr)
            continue
        ok_series.add(series)
        for m in markets:
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
                "player_norm": normalize_name(player_name),
                "line": line,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "game_dt_utc": game_dt,
            })
    return results, ok_series


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
                "player_norm": normalize_name(player_name),
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

# REST endpoints sit behind a CDN with max-age=30, so a REST quote can be up to
# 30s old. The WS is event-driven (silence = price unchanged), so any entry the
# WS touched within this window is at least as fresh as REST.
_POLY_WS_FRESH_SECONDS = 30


def _carry_ws_fresh_prices(old: Optional[dict], new: dict, now: datetime) -> dict:
    """Keep `old`'s WS prices in `new` if the WS touched them within
    _POLY_WS_FRESH_SECONDS. `ws_at` is stamped only by the WS handler, never by
    REST re-seeds, so it is a reliable provenance marker. Only sides the WS
    actually populated are carried — a side the WS never saw takes the REST
    value instead of being clobbered with None."""
    if old is not None:
        ws_at = old.get("ws_at")
        if ws_at is not None and (now - ws_at).total_seconds() < _POLY_WS_FRESH_SECONDS:
            if old.get("yes_ask") is not None:
                new["yes_ask"] = old["yes_ask"]
            if old.get("no_ask") is not None:
                new["no_ask"] = old["no_ask"]
            new["ws_at"] = ws_at
    return new


def _poly_unsubscribed_slugs(subscribed: set[str]) -> list[str]:
    """Slugs present in either WS map but not yet subscribed on the live connection."""
    return [
        s for s in itertools.chain(_poly_ws_ml_token_map, _poly_ws_props_token_map)
        if s not in subscribed
    ]
# Incremental-update index: slug → {"yes": list_idx, "no": list_idx}
# Built by _rebuild_poly_ml_cache(); patched in O(1) by _patch_poly_ml_entry()
_poly_ml_slot: dict[str, dict[str, int]] = {}
_last_status_log: float = 0.0   # epoch seconds; status lines throttled to once per 60s
_STATUS_LOG_INTERVAL = 60
# keyed by (kalshi_ticker, direction); value is (leg1_ask, leg2_ask) last printed/logged


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
            k_ask = ev.get("kalshi_ask") or 0.0
            p_ask = ev.get("polymarket_ask") or 0.0
            sides = (f"Kalshi YES {ev.get('kalshi_team') or '?'}={k_ask:.2f} + "
                     f"Poly YES {ev.get('polymarket_team') or '?'}={p_ask:.2f}")
            lines.append(f"[WS] {name} — {sides} = ${total:.3f} gap={gap:.1f}¢ open {dur_s}")
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

def _route_book_arb_check(ticker: str) -> None:
    """Fire the arb check matching the book frame's market type: prop series
    (SERIES_TO_SMT prefix) → props check, anything else → moneyline."""
    if ticker.split("-")[0] in SERIES_TO_SMT:
        _run_props_arb_check_from_ws()
    else:
        check_arb_moneyline(utc_now())


def _handle_ws_message(data: dict) -> bool:
    """
    Dispatch a single WS message. Returns True if the caller should reconnect
    (sequence gap on an orderbook_delta sid stream).
    Kalshi WS v2 nests the payload under "msg"; only type/sid/seq live at the
    top level (LEARNED_RULES.md rule 17).
    """
    msg_type = data.get("type")
    payload  = data.get("msg") or {}
    ticker   = payload.get("market_ticker")
    sid      = data.get("sid")
    seq      = data.get("seq")

    if msg_type == "orderbook_snapshot":
        if ticker:
            if not _order_book.apply_snapshot(sid, seq, payload):
                return True
            _route_book_arb_check(ticker)

    elif msg_type == "orderbook_delta":
        if ticker:
            ok = _order_book.apply_delta(sid, seq, payload)
            if not ok:
                return True
            _route_book_arb_check(ticker)

    elif msg_type == "ticker":
        # Props moved to orderbook_delta; this frame can only arrive from a
        # stale server-side subscription — ignore it.
        pass

    elif msg_type in ("subscribed", "ok", "error"):
        if msg_type == "error":
            print(f"[WS] Error from Kalshi: {data}", file=sys.stderr)
        # Ack frames consume seq numbers on the shared sid (rule 19) —
        # must be tracked or the next book frame reads as a phantom gap.
        if sid is not None and seq is not None:
            if not _order_book.note_seq(sid, seq):
                return True

    return False


# ─── Kalshi WS Task (single connection, both channels) ────────────────────────

async def _kalshi_ws_task(api_key_id: str, private_key) -> None:
    backoff = 1

    while True:
        try:
            auth_headers = _ws_auth_headers(api_key_id, private_key)
            private_key = None  # reload on reconnect via _load_ws_credentials

            async with websockets.connect(
                KALSHI_WS_URL, additional_headers=auth_headers
            ) as ws:
                backoff = 1

                today = datetime.now(timezone.utc).date()
                _order_book.reset_connection()
                ml_tickers   = _order_book.today_tickers(today)
                prop_tickers = _price_cache.today_tickers(today)

                next_id = 1
                next_id = await _ws_subscribe_chunks(ws, ml_tickers,   "orderbook_delta", next_id)
                _order_book.sync_prop_tickers(prop_tickers)
                next_id = await _ws_subscribe_chunks(ws, prop_tickers,  "orderbook_delta", next_id)
                subscribed_props_set = set(prop_tickers)
                last_prop_resub_at = time.time()

                # Re-pull props via REST on every (re)connect — no props equivalent of
                # orderbook_snapshot to backfill the gap, so REST is the only catch-up.
                asyncio.create_task(_kalshi_props_reconcile_once())

                print(
                    f"[WS] Connected — {len(ml_tickers)} moneyline tickers (orderbook_delta), "
                    f"{len(prop_tickers)} prop tickers (orderbook_delta).",
                    file=sys.stderr,
                )

                async for message in ws:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    needs_resub = _handle_ws_message(data)

                    if needs_resub:
                        # A seq gap invalidates every book on that sid stream;
                        # reconnect to get fresh snapshots for all tickers.
                        raise RuntimeError("orderbook seq gap — reconnecting for fresh snapshots")

                    # Date rollover: resubscribe to next day's tickers
                    new_today = datetime.now(timezone.utc).date()
                    if new_today != today:
                        today = new_today
                        _price_cache.purge_old_date(today)
                        new_ml   = _order_book.today_tickers(today)
                        new_prop = _price_cache.today_tickers(today)
                        next_id = await _ws_subscribe_chunks(ws, new_ml,   "orderbook_delta", next_id)
                        _order_book.sync_prop_tickers(new_prop)
                        next_id = await _ws_subscribe_chunks(ws, new_prop, "orderbook_delta", next_id)
                        subscribed_props_set = set(new_prop)
                        last_prop_resub_at = time.time()
                        print(
                            f"[WS] Date rollover — resubscribed {len(new_ml)} ML + "
                            f"{len(new_prop)} prop tickers (orderbook_delta).",
                            file=sys.stderr,
                        )

                    # Mid-session prop discovery: the 60s REST reconcile discovers
                    # newly listed props into _price_cache, but nothing subscribes
                    # them until reconnect/rollover — resync periodically so
                    # intra-day listings (e.g. lineup-dependent props posted 1-2h
                    # before first pitch) don't stay invisible all session.
                    now_epoch = time.time()
                    if now_epoch - last_prop_resub_at >= _PROP_RESUB_INTERVAL_SECONDS:
                        last_prop_resub_at = now_epoch
                        current = _price_cache.today_tickers(today)
                        new = set(current) - subscribed_props_set
                        if new:
                            _order_book.sync_prop_tickers(current)
                            next_id = await _ws_subscribe_chunks(ws, sorted(new), "orderbook_delta", next_id)
                            subscribed_props_set = set(current)
                            print(
                                f"[WS] Mid-session prop resync — subscribed {len(new)} newly listed prop tickers.",
                                file=sys.stderr,
                            )

        except Exception as exc:
            print(
                f"[WS] Kalshi disconnected: {exc!r}. Reconnecting in {backoff}s...",
                file=sys.stderr,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

            if private_key is None:
                creds = _load_ws_credentials()
                if creds is None:
                    print("[ERROR] Cannot reload Kalshi credentials for reconnect.", file=sys.stderr)
                    return
                _, private_key = creds


# ─── Kalshi Props Reconciliation Task ────────────────────────────────────────

_KALSHI_PROPS_RECONCILE_INTERVAL = 60  # seconds between background REST re-pulls
# Serializes the periodic reconcile against reconnect-spawned ones — without
# this, an older fetch can land after a newer one and re-seed stale data.
_kalshi_reconcile_lock = asyncio.Lock()

async def _kalshi_props_reconcile_once() -> None:
    """Pull Kalshi props via REST and merge into _price_cache. Non-fatal on error."""
    loop = asyncio.get_running_loop()
    today_utc = datetime.now(timezone.utc).date()
    try:
        async with _kalshi_reconcile_lock:
            props, ok_series = await loop.run_in_executor(None, fetch_kalshi_props, today_utc)
            _price_cache.seed_from_rest(props, authoritative_series=ok_series)
        # Eviction can close arbs — re-check instead of waiting for the next
        # WS tick, which may not come during quiet stretches.
        _run_props_arb_check_from_ws()
    except Exception as exc:
        print(f"[KALSHI-RECONCILE] REST re-seed failed: {exc}", file=sys.stderr)


async def _kalshi_props_reconciliation_task() -> None:
    """Periodically re-pull Kalshi props via REST and merge into the price cache.
    Doubles as a correctness check and a keep-alive that bumps updated_at."""
    while True:
        await asyncio.sleep(_KALSHI_PROPS_RECONCILE_INTERVAL)
        await _kalshi_props_reconcile_once()


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
                    f"\r[{ts}][WS] No props arb — {len(kalshi_props)}K/{len(poly_props)}P props. "
                    f"{_ghost_stats.status_summary()}".ljust(140) + "\r"
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

async def _seed_one_ml_league(
    session: aiohttp.ClientSession,
    league_slug: str,
    smt: str,
    now: datetime,
) -> list[str]:
    """Fetch one Polymarket league's events and populate _poly_ws_ml_token_map. Returns market slugs."""
    slugs: list[str] = []
    parsed: list[dict] = []  # built without the lock; applied under it below
    try:
        async with session.get(
            f"{POLYMARKET_US_GATEWAY}/v2/leagues/{league_slug}/events",
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
                    yes_name = no_name = None
                    for side in m.get("marketSides", []):
                        team = side.get("team", {})
                        abbr = team.get("abbreviation", "")
                        if not abbr:
                            abbr = side.get("participant", "")
                        abbr = abbr.lower()
                        display = normalize_name(
                            team.get("displayName", "") or team.get("name", "") or abbr
                        )
                        quote = side.get("quote") or {}
                        try:
                            ask = float(quote.get("value", 0))
                        except (TypeError, ValueError):
                            ask = 0.0
                        if side.get("long") is True:
                            yes_ask = ask if 0 < ask < 1 else None
                            yes_abbr = abbr
                            yes_name = display
                        else:
                            no_ask = ask if 0 < ask < 1 else None
                            no_abbr = abbr
                            no_name = display

                    parsed.append({
                        "slug": slug_m,
                        "title": m.get("question") or slug_m,
                        "yes_abbr": yes_abbr,
                        "no_abbr": no_abbr,
                        "yes_name": yes_name,
                        "no_name": no_name,
                        "yes_ask": yes_ask,
                        "no_ask": no_ask,
                        "raw": m,
                        "updated_at": now,
                    })
                except (TypeError, ValueError, KeyError):
                    continue

        # _rebuild_poly_ml_cache and the WS handler both work under
        # _poly_ws_lock — map writes must too, or a mid-reconcile WS tick can
        # land on an entry that is being replaced.
        async with _poly_ws_lock:
            for entry in parsed:
                slug_m = entry["slug"]
                _poly_ws_ml_token_map[slug_m] = _carry_ws_fresh_prices(
                    _poly_ws_ml_token_map.get(slug_m), entry, now
                )
                slugs.append(slug_m)
    except Exception as exc:
        print(f"[POLY-WS] ML REST seed error for {league_slug}: {exc}", file=sys.stderr)
    return slugs


def _fetch_poly_props_events() -> list:
    """Blocking REST fetch of props search results. Run in an executor."""
    events = []
    for q in ["mlb will record at least", "nba will record", "player points", "player rebounds"]:
        try:
            resp = requests.get(
                f"{POLYMARKET_US_GATEWAY}/v1/search",
                params={"query": q, "limit": 200},
                timeout=REST_TIMEOUT,
            )
            resp.raise_for_status()
            events.extend(resp.json().get("events", []))
        except Exception:
            pass
    return events


def _update_poly_props_map(events: list) -> list[str]:
    """
    Parse search events into _poly_ws_props_token_map. REST is authoritative:
    markets seen closed/inactive/date-expired are evicted from the map so they
    stop serving ghost prices. Markets merely absent from search results are
    left alone (they may still stream via WS). Returns stored slugs.
    """
    now = datetime.now(timezone.utc)
    today_str = now.date().strftime("%Y-%m-%d")
    supported_smts = set(SERIES_TO_SMT.values())
    today = date.fromisoformat(today_str)
    valid_dates = {today_str, (today - timedelta(days=1)).isoformat()}
    kept: list[str] = []

    for event in events:
        event_title = event.get("title", "")
        for m in event.get("markets", []):
            try:
                smt = m.get("sportsMarketType", "")
                if smt not in supported_smts:
                    continue
                slug = m.get("slug", "")
                if not slug:
                    continue
                game_start = m.get("gameStartTime", "") or ""
                dead = (
                    not m.get("active") or m.get("closed") or m.get("archived")
                    or game_start[:10] not in valid_dates
                )
                if dead:
                    _poly_ws_props_token_map.pop(slug, None)
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
                _poly_ws_props_token_map[slug] = _carry_ws_fresh_prices(
                    _poly_ws_props_token_map.get(slug),
                    {
                        "slug": slug,
                        "smt": smt,
                        "player_name": player_name,
                        "player_norm": normalize_name(player_name),
                        "line": m.get("line"),
                        "game_start": game_start,
                        "event_title": event_title,
                        "yes_ask": yes_ask,
                        "no_ask": no_ask,
                        "updated_at": now,
                    },
                    now,
                )
                kept.append(slug)
            except (TypeError, ValueError, KeyError):
                continue

    # Purge entries that vanished from search results — eviction above only
    # covers markets still PRESENT in results, so old-date leftovers would
    # otherwise accumulate for the process lifetime.
    for slug in [
        s for s, e in _poly_ws_props_token_map.items()
        if (e.get("game_start") or "")[:10] not in valid_dates
    ]:
        _poly_ws_props_token_map.pop(slug, None)
    return kept


async def _poly_ws_seed_from_rest(session: aiohttp.ClientSession) -> tuple[list[str], list[str]]:
    """
    Fetch Polymarket markets via REST, populate slug-keyed WS maps, and return
    (ml_slugs, props_slugs) for WS subscription.
    Called on startup and on every WS reconnect.
    """
    now = datetime.now(timezone.utc)

    # ── Moneyline markets — all league slugs fetched concurrently ──
    slug_to_smt = {
        slug: cfg["poly_smt"]
        for cfg in SPORTS_CONFIGS
        for slug in cfg["poly_slugs"]
    }
    slug_lists = await asyncio.gather(*[
        _seed_one_ml_league(session, league_slug, smt, now)
        for league_slug, smt in slug_to_smt.items()
    ])
    ml_slugs: list[str] = [s for sublist in slug_lists for s in sublist]
    props_slugs: list[str] = []

    # ── Props markets ──
    loop = asyncio.get_running_loop()
    try:
        events = await loop.run_in_executor(None, _fetch_poly_props_events)
        async with _poly_ws_lock:
            props_slugs = _update_poly_props_map(events)
    except Exception as exc:
        print(f"[POLY-WS] Props REST seed error: {exc}", file=sys.stderr)

    return ml_slugs, props_slugs


_POLY_RECONCILE_INTERVAL = 60  # seconds between background REST re-pulls

async def _poly_reconcile_task() -> None:
    """
    Periodically re-pull Polymarket ML + props via REST: refreshes prices/flags
    (WS-fresh prices are preserved via _carry_ws_fresh_prices), evicts props
    that went closed, discovers new markets so the delta-subscribe loop can pick
    them up, and bumps updated_at so live markets pass the staleness filter
    (mirrors _kalshi_props_reconciliation_task).
    """
    while True:
        await asyncio.sleep(_POLY_RECONCILE_INTERVAL)
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                await _poly_ws_seed_from_rest(session)
            async with _poly_ws_lock:
                _rebuild_poly_ml_cache()
            # Eviction can close arbs — re-check instead of waiting for the
            # next WS tick, which may not come during quiet stretches.
            _run_props_arb_check_from_ws()
        except Exception as exc:
            print(f"[POLY-RECONCILE] REST re-seed failed: {exc}", file=sys.stderr)


def _rebuild_poly_ml_cache() -> None:
    """
    Full rebuild of _poly_ml_cache from _poly_ws_ml_token_map. Also rebuilds
    _poly_ml_slot index for O(1) incremental patches.
    Called on startup/reconnect. Hot-path updates use _patch_poly_ml_entry() instead.
    Must be called while holding _poly_ws_lock (or before WS task starts).
    """
    global _poly_ml_cache, _poly_ml_slot
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(seconds=_CACHE_STALE_SECONDS)
    markets: list[dict] = []
    slot: dict[str, dict[str, int]] = {}
    latest_update: Optional[datetime] = None

    for entry in _poly_ws_ml_token_map.values():
        updated = entry.get("updated_at")
        if updated is None or updated < stale_cutoff:
            continue
        slug = entry["slug"]
        slot[slug] = {}
        yes_ask = entry.get("yes_ask")
        if yes_ask and 0 < yes_ask < 1 and entry.get("yes_abbr"):
            slot[slug]["yes"] = len(markets)
            markets.append({
                "slug": slug,
                "title": entry["title"],
                "team_abbr": entry["yes_abbr"],
                "team_name": entry.get("yes_name", ""),
                "ask": yes_ask,
                "taker_fee": round(yes_ask * POLYMARKET_TAKER_FEE_RATE, 6),
                "raw": entry["raw"],
            })
        no_ask = entry.get("no_ask")
        if no_ask and 0 < no_ask < 1 and entry.get("no_abbr"):
            slot[slug]["no"] = len(markets)
            markets.append({
                "slug": slug,
                "title": entry["title"],
                "team_abbr": entry["no_abbr"],
                "team_name": entry.get("no_name", ""),
                "ask": no_ask,
                "taker_fee": round(no_ask * POLYMARKET_TAKER_FEE_RATE, 6),
                "raw": entry["raw"],
            })
        if latest_update is None or (updated and updated > latest_update):
            latest_update = updated

    fetched_at = latest_update.isoformat() if latest_update else utc_now()
    _poly_ml_cache = (markets, fetched_at)
    _poly_ml_slot = slot


def _patch_poly_ml_entry(slug: str, yes_ask: Optional[float], no_ask: Optional[float]) -> None:
    """
    O(1) in-place update of _poly_ml_cache markets for a single slug.
    Patches ask and taker_fee on the existing YES/NO slot entries.
    Falls back to full rebuild if the slug isn't indexed (new market since last seed).
    Must be called while holding _poly_ws_lock.
    """
    global _poly_ml_cache
    if slug not in _poly_ml_slot:
        _rebuild_poly_ml_cache()
        return
    markets, _ = _poly_ml_cache
    slots = _poly_ml_slot[slug]
    if yes_ask is not None and "yes" in slots:
        m = markets[slots["yes"]]
        m["ask"] = yes_ask
        m["taker_fee"] = round(yes_ask * POLYMARKET_TAKER_FEE_RATE, 6)
    if no_ask is not None and "no" in slots:
        m = markets[slots["no"]]
        m["ask"] = no_ask
        m["taker_fee"] = round(no_ask * POLYMARKET_TAKER_FEE_RATE, 6)
    _poly_ml_cache = (markets, utc_now())


def _kalshi_props_from_book(now: datetime) -> list[dict]:
    """Build kalshi_props list from the order book (price/qty authority) +
    KalshiPriceCache (metadata authority) for match_props() consumption.
    Only tickers present in BOTH the cache and the book emit — this is the
    metadata-validated-before-arb-check requirement. No _CACHE_STALE_SECONDS/
    updated_at filtering: book freshness is guaranteed by seq-gap reconnects."""
    stale_game_cutoff = now - timedelta(hours=4)
    quotes = _order_book.prop_quotes()
    result = []
    for entry in _price_cache.metadata_entries():
        quote = quotes.get(entry["ticker"])
        if quote is None:
            continue
        if entry["game_dt_utc"] < stale_game_cutoff:
            continue
        result.append({
            "series": entry["series"],
            "ticker": entry["ticker"],
            "player_name": entry["player_name"],
            "player_norm": entry["player_norm"],
            "line": entry["line"],
            "game_dt_utc": entry["game_dt_utc"],
            "yes_ask": quote["yes_ask"],
            "no_ask": quote["no_ask"],
            "yes_ask_qty": quote["yes_ask_qty"],
            "no_ask_qty": quote["no_ask_qty"],
        })
    return result


def _reconstruct_poly_props_list() -> list[dict]:
    """Build poly_props list from WS cache for match_props() consumption."""
    now = datetime.now(timezone.utc)
    stale_game_cutoff = now - timedelta(hours=4)
    # Same freshness rule as the Kalshi side (rule 8): an entry neither the WS
    # nor the 60s REST reconcile has touched recently is a ghost — skip it.
    stale_price_cutoff = now - timedelta(seconds=_CACHE_STALE_SECONDS)
    result = []
    for entry in _poly_ws_props_token_map.values():
        if entry.get("yes_ask") is None and entry.get("no_ask") is None:
            continue
        updated = entry.get("updated_at")
        if updated is None or updated < stale_price_cutoff:
            continue
        try:
            game_dt = datetime.fromisoformat(entry["game_start"].replace("Z", "+00:00"))
            if game_dt < stale_game_cutoff:
                continue
        except (ValueError, KeyError):
            pass
        result.append({
            "slug": entry.get("slug"),
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
    global _last_status_log
    now_utc = datetime.now(timezone.utc)
    _price_cache.purge_old_date(now_utc.date())
    kalshi_props = _kalshi_props_from_book(now_utc)
    poly_props = _reconstruct_poly_props_list()

    if not kalshi_props:
        if time.time() - _last_status_log >= _STATUS_LOG_INTERVAL:
            _last_status_log = time.time()
            ts = now_utc.strftime("%H:%M:%S")
            sys.stdout.write(
                f"\r[{ts}][POLY-WS] No props arb — 0K/{len(poly_props)}P props (Kalshi cache stale). "
                f"{_ghost_stats.status_summary()}".ljust(140) + "\r"
            )
            sys.stdout.flush()
        return

    arbs = match_props(kalshi_props, poly_props)
    timestamp = now_utc.isoformat()
    if arbs or _prop_arb_tracker.active_count():
        now_epoch = time.time()
        # Partition: new opens (need REST confirm) vs already-open (pass through)
        confirmed_pass_through = []
        for arb in arbs:
            key = (arb["kalshi_ticker"], arb["direction"])
            if key in _prop_arb_tracker._open:
                # Already tracked — update/close logic handled by _emit_prop_arbs
                confirmed_pass_through.append(arb)
            else:
                last_attempt = _kalshi_confirm_cooldown.get(key, 0.0)
                if now_epoch - last_attempt < _KALSHI_CONFIRM_COOLDOWN_SECONDS:
                    # Within cooldown — suppress; accepted false negative
                    continue
                _kalshi_confirm_cooldown[key] = now_epoch
                asyncio.create_task(_rest_confirm_and_emit(arb, timestamp))
        # Drive close detection even when confirmed_pass_through is empty — any open
        # arb absent from confirmed_pass_through will be correctly marked closed.
        _emit_prop_arbs(confirmed_pass_through, timestamp)
    if not arbs:
        if time.time() - _last_status_log >= _STATUS_LOG_INTERVAL:
            _last_status_log = time.time()
            ts = now_utc.strftime("%H:%M:%S")
            sys.stdout.write(
                f"\r[{ts}][POLY-WS] No props arb — {len(kalshi_props)}K/{len(poly_props)}P props. "
                f"{_ghost_stats.status_summary()}".ljust(140) + "\r"
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

        if yes_ask is None and no_ask is None:
            # No usable price — must not stamp ws_at/updated_at, or the frame
            # re-arms the freshness guard and blocks the REST safety net.
            return

        async with _poly_ws_lock:
            if slug in _poly_ws_ml_token_map:
                entry = _poly_ws_ml_token_map[slug]
                if yes_ask is not None:
                    entry["yes_ask"] = yes_ask
                if no_ask is not None:
                    entry["no_ask"] = no_ask
                entry["updated_at"] = now
                entry["ws_at"] = now
                _patch_poly_ml_entry(slug, yes_ask, no_ask)
                is_ml = True
            elif slug in _poly_ws_props_token_map:
                entry = _poly_ws_props_token_map[slug]
                if yes_ask is not None:
                    entry["yes_ask"] = yes_ask
                if no_ask is not None:
                    entry["no_ask"] = no_ask
                entry["updated_at"] = now
                entry["ws_at"] = now
                is_props = True

        if is_ml:
            check_arb_moneyline(utc_now())
        if is_props:
            _run_props_arb_check_from_ws()
        return

    # Full market data (order book) — not subscribed but handle gracefully
    if "marketData" in data or "trade" in data:
        return


_POLY_DELTA_SUB_INTERVAL = 20   # seconds between checks for newly discovered slugs
_POLY_MAX_SUB_FRAMES = 40       # reconnect to consolidate into one subscription after this


async def _poly_delta_subscribe_loop(ws, subscribed: set[str], req_counter) -> None:
    """Subscribe slugs that entered the WS maps after connect (reconcile finds
    new markets every 60s; without this they would only ever get 30s-stale REST
    quotes). Raises to force a consolidating reconnect once the connection has
    accumulated _POLY_MAX_SUB_FRAMES subscriptions."""
    n_frames = 1  # the initial subscribe
    while True:
        await asyncio.sleep(_POLY_DELTA_SUB_INTERVAL)
        async with _poly_ws_lock:
            new_slugs = _poly_unsubscribed_slugs(subscribed)
        if not new_slugs:
            continue
        if n_frames >= _POLY_MAX_SUB_FRAMES:
            raise RuntimeError(
                f"{n_frames} subscribe frames on one connection — reconnecting to consolidate"
            )
        await ws.send(json.dumps({
            "subscribe": {
                "requestId": f"poly-{next(req_counter)}",
                "subscriptionType": "SUBSCRIPTION_TYPE_MARKET_DATA_LITE",
                "marketSlugs": new_slugs,
            }
        }))
        subscribed.update(new_slugs)
        n_frames += 1
        print(f"[POLY-WS] Delta-subscribed {len(new_slugs)} new slugs.", file=sys.stderr)


async def _poly_recv_loop(ws) -> None:
    async for raw_msg in ws:
        try:
            data = json.loads(raw_msg)
        except json.JSONDecodeError:
            continue
        await _handle_poly_ws_message(data)


async def _poly_run_connection(ws, subscribed: set[str], req_counter) -> None:
    """
    Run the recv and delta-subscribe loops until the connection ends. Returns
    on clean server close (recv loop finishes) and re-raises loop errors.
    FIRST_COMPLETED, not FIRST_EXCEPTION: a clean close finishes recv without
    raising, and the delta loop never finishes on its own — waiting for an
    exception would hang forever with all Poly prices silently stale.
    """
    recv_task = asyncio.create_task(_poly_recv_loop(ws))
    delta_task = asyncio.create_task(
        _poly_delta_subscribe_loop(ws, subscribed, req_counter)
    )
    try:
        done, _ = await asyncio.wait(
            {recv_task, delta_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for t in (recv_task, delta_task):
            t.cancel()
        await asyncio.gather(recv_task, delta_task, return_exceptions=True)
    for t in done:
        t.result()  # re-raise so the outer loop reconnects with backoff


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

                await _poly_run_connection(ws, set(all_slugs), _req_counter)

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
        initial_props, initial_ok = await loop.run_in_executor(None, fetch_kalshi_props, today_utc)
        _price_cache.seed_from_rest(initial_props, authoritative_series=initial_ok)
        print(f"[INIT] Seeded {len(initial_props)} prop tickers into price cache.", file=sys.stderr)
    except Exception as exc:
        print(f"[WARN] Initial prop REST seed failed: {exc}. WS will populate cache.", file=sys.stderr)

    print(
        f"ws_manager running — Kalshi WS + Poly WS. "
        f"Logging to {LOG_FILE}. Ctrl+C to stop."
    )

    await asyncio.gather(
        _kalshi_ws_task(api_key_id, private_key),
        _poly_ws_task(),
        _kalshi_props_reconciliation_task(),
        _poly_reconcile_task(),
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
