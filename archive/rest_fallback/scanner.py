"""
MLB Arbitrage Scanner — Kalshi vs Polymarket US
Runs both platform fetches concurrently via asyncio + aiohttp, evaluates arb
on every completed Polymarket response. No time.sleep().

Architecture:
  - kalshi_fetch_loop: polls Kalshi every KALSHI_POLL_SECONDS, updates shared cache
  - poly_fetch_loop:   polls Polymarket every POLY_POLL_SECONDS, runs match + log on each response
  - Both run concurrently under asyncio.gather() — neither blocks the other

Note: Polymarket US has no public WebSocket API (confirmed by endpoint discovery).
wss://api.polymarket.us/* returns 404 with valid Ed25519 auth; gateway.polymarket.us
serves REST only. Fast async polling at POLY_POLL_SECONDS is the correct architecture.
"""

import asyncio
import json
import os
import re
import sys
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import aiohttp
from dotenv import load_dotenv

from matchers.baseball import (
    BaseballMatcher,
    normalize_name,
    team_code,
    teams_from_kalshi_title,
    teams_from_kalshi_market,
    _kalshi_game_dt,
    kalshi_is_moneyline,
    teams_from_polymarket,
    polymarket_is_moneyline,
    _extract_polymarket_ask,
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
        "kalshi_tickers": ["KXATP", "KXWTA"],
        "poly_slugs": ["atp", "wta"],
        "poly_smt": "tennis_match_winner",
        "matcher_cls": TennisMatcher,
    }
]


load_dotenv()

# ─── Config ────────────────────────────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 2       # Both platforms fetched concurrently every cycle
ARB_THRESHOLD = 0.96
KALSHI_TAKER_FEE_RATE = 0.01
POLYMARKET_TAKER_FEE_RATE = 0.01
MAX_TIMESTAMP_SKEW_SECONDS = 10

LOG_FILE = "arb_log.jsonl"

KALSHI_BASE          = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=8)

# ─── Helpers ───────────────────────────────────────────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def log_event(obj: dict) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(obj) + "\n")


# ─── Async Kalshi fetch ────────────────────────────────────────────────────────

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


# ─── Async Polymarket fetch ────────────────────────────────────────────────────

async def fetch_polymarket(session: aiohttp.ClientSession) -> tuple[list[dict], list[dict], str]:
    fetched_at = utc_now()
    markets = []
    all_events = []
    
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

        events = data.get("events", [])
        if events:
            all_events.extend(events)

        for event in events:
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

    return markets, all_events, fetched_at


def _parse_polymarket_spreads_totals(events: list[dict]) -> list[dict]:
    _SPREAD_TOTAL_TYPES = {
        "baseball_team_full_game_spread",
        "baseball_team_full_game_total",
        "baseball_team_first_five_spread",
        "baseball_team_first_five_total",
    }
    today_utc = datetime.now(timezone.utc).date()
    seen: set[tuple] = set()
    plays: list[dict] = []

    for event in events:
        for m in event.get("markets", []):
            try:
                if m.get("sportsMarketType") not in _SPREAD_TOTAL_TYPES:
                    continue
                if not m.get("active") or m.get("closed") or m.get("archived"):
                    continue
                game_start = m.get("gameStartTime") or event.get("gameStartTime") or ""
                try:
                    game_dt = datetime.fromisoformat(game_start.replace("Z", "+00:00"))
                except (ValueError, AttributeError):
                    continue
                if game_dt.date() != today_utc:
                    continue
                slug = m.get("slug", "")
                line = m.get("line")
                dedup_key = (slug, line)
                if dedup_key in seen:
                    continue
                long_ask = None
                short_ask = None
                for side in m.get("marketSides", []):
                    quote = side.get("quote") or {}
                    raw_val = quote.get("value")
                    if raw_val is None:
                        continue
                    try:
                        val = float(raw_val)
                    except (TypeError, ValueError):
                        continue
                    if val <= 0 or val >= 1:
                        continue
                    if side.get("long") is True:
                        long_ask = val
                    elif side.get("long") is False:
                        short_ask = val
                if long_ask is None or short_ask is None:
                    continue
                total_cost = long_ask + short_ask
                if total_cost >= ARB_THRESHOLD:
                    continue
                seen.add(dedup_key)
                plays.append({
                    "market_type": m["sportsMarketType"],
                    "question": m.get("question") or slug,
                    "slug": slug,
                    "line": line,
                    "long_ask": round(long_ask, 6),
                    "short_ask": round(short_ask, 6),
                    "total_cost": round(total_cost, 6),
                    "gap_cents": round((ARB_THRESHOLD - total_cost) * 100, 4),
                    "game_title": event.get("title"),
                    "game_start": game_start,
                })
            except (TypeError, ValueError, KeyError):
                continue

    return plays


# ─── Matching ─────────────────────────────────────────────────────────────────

def match_markets(
    kalshi_markets: list[dict],
    polymarket_markets: list[dict],
) -> list[dict]:
    return BaseballMatcher(arb_threshold=ARB_THRESHOLD).match(kalshi_markets, polymarket_markets)


# ─── Opportunity lifecycle ────────────────────────────────────────────────────

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
                self._open[key] = {
                    "opportunity_id": opp_id,
                    "opened_at": now,
                    "last_seen": now,
                    "last_match": m,
                }
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

        closed_keys = set(self._open.keys()) - set(current_keys.keys())
        for key in closed_keys:
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
            a = datetime.fromisoformat(opened_at)
            b = datetime.fromisoformat(now)
            return round((b - a).total_seconds(), 2)
        except Exception:
            return 0.0

    @staticmethod
    def _build_event(
        event_type: str,
        opp_id: str,
        m: dict,
        now: str,
        kalshi_fetched_at: str,
        polymarket_fetched_at: str,
        timestamp_skew_warning: bool,
        duration_seconds: Optional[float],
    ) -> dict:
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


# ─── Display ──────────────────────────────────────────────────────────────────

def render_stdout(active_events: list[dict], now_str: str) -> None:
    if not active_events:
        sys.stdout.write("\r" + " " * 120 + "\r")
        sys.stdout.flush()
        return
    lines = []
    for ev in active_events:
        if ev["event"] not in ("opened", "updated"):
            continue
        name = ev.get("market_name", "?")
        total = ev.get("total_cost") or 0.0
        gap = ev.get("gap_cents") or 0.0
        dur = ev.get("duration_seconds")
        dur_str = f"{int(dur)}s" if dur is not None else "<1s"
        lines.append(
            f"[{now_str}] {name} — total_cost=${total:.3f} gap={gap:.1f}¢ open {dur_str}"
        )
    if lines:
        output = " | ".join(lines)
        sys.stdout.write("\r" + output[:200].ljust(200) + "\r")
        sys.stdout.flush()


# ─── Main loop ────────────────────────────────────────────────────────────────

async def _main():
    tracker = OpportunityTracker()
    matchers = [cfg["matcher_cls"](arb_threshold=ARB_THRESHOLD) for cfg in SPORTS_CONFIGS]
    backoff = 1
    print(f"Multi-Sport Arb Scanner — both platforms fetched concurrently every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    print(f"Logging to {LOG_FILE}")

    async with aiohttp.ClientSession() as session:
        loop = asyncio.get_running_loop()
        while True:
            cycle_start = loop.time()
            now = utc_now()

            # Fetch both platforms at the same instant
            kalshi_result, poly_result = await asyncio.gather(
                fetch_kalshi(session),
                fetch_polymarket(session),
                return_exceptions=True,
            )

            kalshi_error = isinstance(kalshi_result, BaseException)
            poly_error   = isinstance(poly_result, BaseException)

            for source, result, had_error in [
                ("kalshi", kalshi_result, kalshi_error),
                ("polymarket", poly_result, poly_error),
            ]:
                if had_error:
                    log_event({"event": "fetch_error", "opportunity_id": None, "timestamp": now,
                               "kalshi_fetched_at": now, "polymarket_fetched_at": now,
                               "timestamp_skew_warning": False,
                               "error_source": source, "error_message": str(result)})
                    print(f"\r[{source}] {result}".ljust(120), file=sys.stderr)

            if kalshi_error or poly_error:
                await asyncio.sleep(min(backoff, 30))
                backoff = min(backoff * 2, 30)
                continue

            backoff = 1
            kalshi_markets, kalshi_fetched_at = kalshi_result
            poly_markets, poly_raw_events, poly_fetched_at = poly_result

            # Both fetches completed at nearly the same wall-clock time, so skew
            # is just network latency difference — warn only if genuinely large.
            try:
                skew = abs((datetime.fromisoformat(kalshi_fetched_at) -
                            datetime.fromisoformat(poly_fetched_at)).total_seconds())
                timestamp_skew_warning = skew > MAX_TIMESTAMP_SKEW_SECONDS
            except Exception:
                timestamp_skew_warning = False

            try:
                poly_value_plays = _parse_polymarket_spreads_totals(poly_raw_events)
            except Exception as exc:
                poly_value_plays = []
                log_event({"event": "fetch_error", "opportunity_id": None, "timestamp": now,
                           "kalshi_fetched_at": kalshi_fetched_at,
                           "polymarket_fetched_at": poly_fetched_at,
                           "timestamp_skew_warning": False,
                           "error_source": "polymarket_spreads_totals",
                           "error_message": str(exc)})

            matches = []
            for matcher in matchers:
                matches.extend(matcher.match(kalshi_markets, poly_markets))

            for play in poly_value_plays:
                log_event({"event": "poly_value", "timestamp": now,
                           "kalshi_fetched_at": kalshi_fetched_at,
                           "polymarket_fetched_at": poly_fetched_at, **play})

            events = tracker.process(matches, kalshi_fetched_at, poly_fetched_at, timestamp_skew_warning)

            for ev in events:
                log_event(ev)
            active_display = [ev for ev in events if ev["event"] in ("opened", "updated")]

            ts = datetime.fromisoformat(now).strftime("%H:%M:%S")
            render_stdout(active_display, ts)

            if not matches and not active_display and not poly_value_plays:
                sys.stdout.write(
                    f"\r[{ts}] No arb — {len(kalshi_markets)}K/{len(poly_markets)}P markets checked.".ljust(120) + "\r"
                )
                sys.stdout.flush()
                log_event({"event": "no_match", "opportunity_id": None, "timestamp": now,
                           "kalshi_fetched_at": kalshi_fetched_at,
                           "polymarket_fetched_at": poly_fetched_at,
                           "timestamp_skew_warning": timestamp_skew_warning})

            elapsed = loop.time() - cycle_start
            await asyncio.sleep(max(0.0, POLL_INTERVAL_SECONDS - elapsed))


def main():
    asyncio.run(_main())


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nScanner stopped.")
        sys.exit(0)
