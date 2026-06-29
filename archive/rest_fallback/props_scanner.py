"""
MLB Player Props Arb Scanner — Kalshi vs Polymarket US
Finds cross-platform arbitrage on player props:
  Buy YES on one platform + NO on the other for < ARB_THRESHOLD ($0.96).
  Both legs pay $1.00 on a correct outcome — guaranteed profit if total < $0.96.

Supported prop types:
  KXMLBHIT  ↔  baseball_player_hits
  KXMLBHR   ↔  baseball_player_home_runs
  KXMLBTB   ↔  baseball_player_total_bases

Ask prices:
  Kalshi: yes_ask_dollars / no_ask_dollars  (order-book ask, USD string)
  Poly:   side.quote.value where long=True (YES ask) / long=False (NO ask)

Modes:
  WebSocket mode (default): Kalshi prices stream via wss:// using RSA auth;
    Polymarket polled every POLL_INTERVAL_SECONDS. Requires KALSHI_API_KEY_ID
    and KALSHI_PRIVATE_KEY_PATH env vars.
  REST fallback: if auth env vars absent, both platforms polled every
    POLL_INTERVAL_SECONDS — identical output to original implementation.

Run: python3 props_scanner.py
"""

import asyncio
import base64
import json
import os
import re
import sys
import time
import unicodedata
import uuid
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

ARB_THRESHOLD = 0.96
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_WS_URL = "wss://api.elections.kalshi.com/trade-api/ws/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"
REQUEST_TIMEOUT = 8
POLL_INTERVAL_SECONDS = 2
LOG_FILE = "arb_log.jsonl"

# Kalshi series → Polymarket sportsMarketType
SERIES_TO_SMT = {
    "KXMLBHIT": "baseball_player_hits",
    "KXMLBHR":  "baseball_player_home_runs",
    "KXMLBTB":  "baseball_player_total_bases",
}

# Kalshi WS: max tickers per subscribe message to avoid per-message limits
_WS_SUBSCRIBE_CHUNK_SIZE = 50

# Arb check skips a Kalshi cache entry stale beyond this many seconds
_CACHE_STALE_SECONDS = 300

# Warn (stderr) when Kalshi cache and Poly fetch differ by more than this
_MAX_TIMESTAMP_SKEW_SECONDS = 60


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Lowercase, strip accents, collapse non-alphanum to space."""
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def _kalshi_game_dt_utc(ticker: str) -> datetime | None:
    """Parse game datetime (UTC) from Kalshi prop ticker. Game time encoded in ET (UTC-4)."""
    try:
        seg = ticker.split("-")[1]  # e.g. '26JUN281920NYYBOS'
        dt_et = datetime.strptime(seg[:11], "%y%b%d%H%M")
        return dt_et.replace(tzinfo=timezone.utc) + timedelta(hours=4)
    except (IndexError, ValueError):
        return None


def _kalshi_parse_title(title: str) -> tuple[str, int] | None:
    """
    Parse 'Byron Buxton: 2+ hits?' → ('Byron Buxton', 2).
    Returns None if unparseable.
    """
    m = re.match(r"^(.+?):\s*(\d+)\+", title)
    if not m:
        return None
    return m.group(1).strip(), int(m.group(2))


def _kalshi_ask(market: dict, side: str) -> float | None:
    """Return YES or NO ask from Kalshi market dict. side='yes' or 'no'."""
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


def _poly_ask(side: dict) -> float | None:
    """Return ask from a Polymarket marketSide (quote.value)."""
    quote = side.get("quote") or {}
    raw = quote.get("value")
    if raw is None:
        return None
    try:
        val = float(raw)
        return val if 0 < val < 1 else None
    except (TypeError, ValueError):
        return None


# ─── Kalshi WebSocket Auth ────────────────────────────────────────────────────

def _load_ws_credentials() -> tuple[str, object] | None:
    """
    Load Kalshi API key ID and RSA private key from env vars.
    Returns (api_key_id, private_key) or None if env vars absent.
    Private key object is not stored beyond the handshake; held only long enough
    to sign the login message.
    """
    api_key_id = (
        os.environ.get("KALSHI_KEY_ID")
        or os.environ.get("KALSHI_API_KEY_ID")
        or os.environ.get("KALSHI_API_KEY")
    )
    key_path = os.environ.get("KALSHI_KEY_PATH") or os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key_id or not key_path:
        return None
    # Resolve relative paths against the .env file's directory so the script
    # can be invoked from any working directory.
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
    """
    Build HTTP headers for the Kalshi WS upgrade request.
    Auth is sent as HTTP headers (same pattern as REST), not as a post-connect
    JSON message — CloudFront rejects the upgrade with 401 if headers are absent.
    Signs: timestamp_ms + "GET" + "/trade-api/ws/v2" using RSA-PSS SHA-256.
    """
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
    sig_b64 = base64.b64encode(signature).decode()

    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
    }


# ─── Kalshi REST Fetch (fallback + initial ticker discovery) ──────────────────

def fetch_kalshi_props(today_utc: datetime.date) -> list[dict]:
    """
    Fetch Kalshi player prop markets for today and yesterday (UTC).
    Yesterday is included because evening ET games (e.g. 7 PM ET = 23:xx UTC)
    may have started on the prior UTC date while Polymarket's gameStartTime
    also reflects that prior UTC date — both sides need the same window.
    Returns list of dicts: {series, ticker, player_name, line, yes_ask, no_ask, game_dt_utc}
    """
    valid_dates = {today_utc, today_utc - timedelta(days=1)}
    results = []
    for series in SERIES_TO_SMT:
        try:
            resp = requests.get(
                f"{KALSHI_BASE}/markets",
                params={"series_ticker": series, "status": "open", "limit": 200},
                timeout=REQUEST_TIMEOUT,
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
            no_ask = _kalshi_ask(m, "no")
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
    """
    Fetch active Polymarket player prop markets for supported stat types.
    Accepts markets whose gameStartTime falls on today OR yesterday (games that
    started in the evening ET and crossed midnight UTC are still valid).
    Returns list of dicts: {smt, player_name, line, yes_ask, no_ask, game_start, event_title}
    """
    try:
        resp = requests.get(
            f"{POLYMARKET_US_GATEWAY}/v1/search",
            params={"query": "mlb will record at least", "limit": 200},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Polymarket props fetch failed: {exc}") from exc

    supported_smts = set(SERIES_TO_SMT.values())
    # Accept today and yesterday to catch evening ET games that crossed midnight UTC
    from datetime import date, timedelta
    today = date.fromisoformat(today_str)
    valid_dates = {today_str, (today - timedelta(days=1)).isoformat()}

    results = []

    for event in resp.json().get("events", []):
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

            yes_ask = None
            no_ask = None
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


# ─── Matching ─────────────────────────────────────────────────────────────────

def match_props(
    kalshi_props: list[dict],
    poly_props: list[dict],
) -> list[dict]:
    """
    Match Kalshi and Poly props by (stat_type, player_name_normalized, line, game_date).
    For each match, compute both arb directions:
      A: Poly YES + Kalshi NO
      B: Kalshi YES + Poly NO
    Returns list of arb opportunities sorted by gap descending.
    """
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

        k_yes = kp["yes_ask"]
        k_no = kp["no_ask"]
        p_yes = pp["yes_ask"]
        p_no = pp["no_ask"]

        for direction, leg1_name, leg1_ask, leg2_name, leg2_ask in [
            ("Poly YES + Kalshi NO", "Poly YES", p_yes, "Kalshi NO", k_no),
            ("Kalshi YES + Poly NO", "Kalshi YES", k_yes, "Poly NO", p_no),
        ]:
            if leg1_ask is None or leg2_ask is None:
                continue
            total = leg1_ask + leg2_ask
            if total >= ARB_THRESHOLD:
                continue
            gap_cents = round((ARB_THRESHOLD - total) * 100, 2)
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
                "gap_cents": gap_cents,
                "kalshi_ticker": kp["ticker"],
                "poly_smt": smt,
            })

    matches.sort(key=lambda x: -x["gap_cents"])
    return matches


# ─── Logging / Printing ───────────────────────────────────────────────────────

def _log_arb(arb: dict, timestamp: str) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({"event": "prop_arb", "timestamp": timestamp, **arb}) + "\n")


def _print_arbs(arbs: list[dict], timestamp: str) -> None:
    print(f"\n{'='*70}")
    print(f"  PROP ARB FOUND — {timestamp}")
    print(f"{'='*70}")
    current_game = None
    for arb in arbs:
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


# ─── REST Fallback Main ───────────────────────────────────────────────────────

def main_rest_fallback():
    """Original REST-polling loop — used when Kalshi WS auth is unavailable."""
    print(f"[WARN] Kalshi WS credentials not found (KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH).", file=sys.stderr)
    print(f"[WARN] Falling back to REST polling every {POLL_INTERVAL_SECONDS}s for both platforms.", file=sys.stderr)
    print(f"MLB Props Arb Scanner (REST mode) — polling every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    print(f"Logging arbs to {LOG_FILE}")

    while True:
        cycle_start = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        today_utc = now_utc.date()
        today_str = today_utc.strftime("%Y-%m-%d")
        timestamp = now_utc.isoformat()

        try:
            kalshi_props = fetch_kalshi_props(today_utc)
        except Exception as exc:
            sys.stdout.write(f"\r[{now_utc.strftime('%H:%M:%S')}] Kalshi fetch error: {exc}".ljust(100) + "\r")
            sys.stdout.flush()
            time.sleep(max(0.0, POLL_INTERVAL_SECONDS - (time.monotonic() - cycle_start)))
            continue

        try:
            poly_props = fetch_poly_props(today_str)
        except Exception as exc:
            sys.stdout.write(f"\r[{now_utc.strftime('%H:%M:%S')}] Poly fetch error: {exc}".ljust(100) + "\r")
            sys.stdout.flush()
            time.sleep(max(0.0, POLL_INTERVAL_SECONDS - (time.monotonic() - cycle_start)))
            continue

        arbs = match_props(kalshi_props, poly_props)

        if arbs:
            _print_arbs(arbs, timestamp)
            for arb in arbs:
                _log_arb(arb, timestamp)
        else:
            ts = now_utc.strftime("%H:%M:%S")
            sys.stdout.write(
                f"\r[{ts}] No arb — {len(kalshi_props)}K/{len(poly_props)}P props, "
                f"best pair checked. Ctrl+C to stop.".ljust(100) + "\r"
            )
            sys.stdout.flush()

        time.sleep(max(0.0, POLL_INTERVAL_SECONDS - (time.monotonic() - cycle_start)))


# ─── WebSocket Mode ───────────────────────────────────────────────────────────

class KalshiPriceCache:
    """
    Thread-safe (asyncio-safe) in-memory cache for Kalshi prop prices.
    Keyed by ticker; entries include updated_at (UTC datetime) and game_date.
    Stale entries (updated_at > _CACHE_STALE_SECONDS ago) are excluded from arb checks.
    Old-date entries are purged at midnight to prevent cross-day false matches.
    """

    def __init__(self):
        # ticker → {yes_ask, no_ask, updated_at, series, player_name, player_norm, line, game_dt_utc}
        self._cache: dict[str, dict] = {}
        self._cache_date: datetime.date | None = None

    def update_from_ws(self, ticker: str, msg: dict) -> None:
        """Update a single ticker's prices from a WS ticker message."""
        raw_yes = msg.get("yes_ask") if msg.get("yes_ask") is not None else msg.get("yes_ask_dollars")
        raw_no = msg.get("no_ask") if msg.get("no_ask") is not None else msg.get("no_ask_dollars")
        yes_ask = self._parse_price(raw_yes)
        no_ask = self._parse_price(raw_no)
        if ticker not in self._cache:
            return
        entry = self._cache[ticker]
        if yes_ask is not None:
            entry["yes_ask"] = yes_ask
        if no_ask is not None:
            entry["no_ask"] = no_ask
        entry["updated_at"] = datetime.now(timezone.utc)

    def seed_from_rest(self, props: list[dict]) -> None:
        """Populate cache from initial REST fetch. Clears stale date entries first."""
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
                # Refresh REST prices but keep ws-updated_at if more recent
                entry = self._cache[ticker]
                if kp["yes_ask"] is not None:
                    entry["yes_ask"] = kp["yes_ask"]
                if kp["no_ask"] is not None:
                    entry["no_ask"] = kp["no_ask"]
                entry["updated_at"] = now

    def purge_old_date(self, today: datetime.date) -> None:
        """Remove entries older than yesterday (UTC). Keeps yesterday for evening ET games."""
        if self._cache_date == today:
            return
        yesterday = today - timedelta(days=1)
        self._cache = {
            t: e for t, e in self._cache.items()
            if e["game_dt_utc"].date() >= yesterday
        }
        self._cache_date = today

    def as_props_list(self, now: datetime) -> list[dict]:
        """
        Return cache entries as the same format fetch_kalshi_props() returns,
        excluding entries stale beyond _CACHE_STALE_SECONDS.
        """
        results = []
        stale_cutoff = now - timedelta(seconds=_CACHE_STALE_SECONDS)
        for entry in self._cache.values():
            if entry["updated_at"] < stale_cutoff:
                continue
            if entry["yes_ask"] is None and entry["no_ask"] is None:
                continue
            results.append(entry)
        return results

    def today_tickers(self, today: datetime.date) -> list[str]:
        yesterday = today - timedelta(days=1)
        return [t for t, e in self._cache.items() if e["game_dt_utc"].date() >= yesterday]

    @staticmethod
    def _parse_price(raw) -> float | None:
        if raw is None:
            return None
        try:
            val = float(raw)
            # Kalshi WS may send integer cents
            if val > 1:
                val /= 100.0
            return val if 0 < val < 1 else None
        except (TypeError, ValueError):
            return None


async def _ws_subscribe_chunks(ws, tickers: list[str], start_id: int) -> int:
    """Send chunked subscribe messages. Returns next available message id."""
    msg_id = start_id
    for i in range(0, len(tickers), _WS_SUBSCRIBE_CHUNK_SIZE):
        chunk = tickers[i: i + _WS_SUBSCRIBE_CHUNK_SIZE]
        await ws.send(json.dumps({
            "id": msg_id,
            "cmd": "subscribe",
            "params": {
                "channels": ["ticker"],
                "market_tickers": chunk,
            },
        }))
        msg_id += 1
    return msg_id


async def _kalshi_ws_task(cache: KalshiPriceCache, api_key_id: str, private_key, today_getter):
    """
    Persistent Kalshi WS reader. Updates cache on every ticker message.
    Reconnects with exponential backoff on disconnect.
    Clears private_key reference after first use to minimize in-memory exposure.
    """
    import websockets

    backoff = 1
    _private_key = private_key
    private_key = None  # drop the outer reference

    while True:
        try:
            auth_headers = _ws_auth_headers(api_key_id, _private_key)
            # Clear private key reference after signing — key material not retained
            _private_key = None

            async with websockets.connect(KALSHI_WS_URL, additional_headers=auth_headers) as ws:
                backoff = 1  # reset on successful connect

                # Subscribe to today's prop tickers
                today = today_getter()
                tickers = cache.today_tickers(today)
                if tickers:
                    await _ws_subscribe_chunks(ws, tickers, start_id=2)
                else:
                    print("[WARN] No Kalshi tickers in cache yet for WS subscription.", file=sys.stderr)

                print(
                    f"[WS] Connected — subscribed to {len(tickers)} Kalshi prop tickers.",
                    file=sys.stderr,
                )

                async for message in ws:
                    try:
                        data = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    msg_type = data.get("type")

                    if msg_type == "ticker":
                        ticker = data.get("market_ticker") or data.get("ticker")
                        if ticker:
                            cache.update_from_ws(ticker, data)

                    elif msg_type == "subscribed":
                        pass  # ack, nothing needed

                    elif msg_type == "error":
                        print(f"[WARN] Kalshi WS error msg: {data}", file=sys.stderr)

                    # Check for date rollover and re-subscribe if needed
                    new_today = today_getter()
                    if new_today != today:
                        today = new_today
                        cache.purge_old_date(today)
                        new_tickers = cache.today_tickers(today)
                        if new_tickers:
                            await _ws_subscribe_chunks(ws, new_tickers, start_id=1000)

        except Exception as exc:
            print(
                f"[WS] Kalshi WebSocket disconnected: {exc!r}. "
                f"Reconnecting in {backoff}s...",
                file=sys.stderr,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            # Reload private key for next login attempt if it was cleared
            if _private_key is None:
                creds = _load_ws_credentials()
                if creds is None:
                    print("[ERROR] Cannot reload Kalshi credentials for reconnect.", file=sys.stderr)
                    return
                _, _private_key = creds


async def _poly_polling_task(cache: KalshiPriceCache):
    """
    Polymarket polling loop. Fetches every POLL_INTERVAL_SECONDS, merges with
    the Kalshi price cache, runs match_props, and logs/prints arbs.
    """
    print(f"MLB Props Arb Scanner (WS mode) — Poly polls every {POLL_INTERVAL_SECONDS}s. Ctrl+C to stop.")
    print(f"Logging arbs to {LOG_FILE}")

    loop = asyncio.get_running_loop()
    while True:
        cycle_start = loop.time()
        now_utc = datetime.now(timezone.utc)
        today_utc = now_utc.date()
        today_str = today_utc.strftime("%Y-%m-%d")
        timestamp = now_utc.isoformat()

        cache.purge_old_date(today_utc)

        try:
            poly_props = await loop.run_in_executor(
                None, fetch_poly_props, today_str
            )
        except Exception as exc:
            sys.stdout.write(f"\r[{now_utc.strftime('%H:%M:%S')}] Poly fetch error: {exc}".ljust(100) + "\r")
            sys.stdout.flush()
            remaining = POLL_INTERVAL_SECONDS - (loop.time() - cycle_start)
            await asyncio.sleep(max(0.0, remaining))
            continue

        poly_fetch_time = datetime.now(timezone.utc)
        kalshi_props = cache.as_props_list(now_utc)

        # Check timestamp skew between oldest Kalshi cache entry used and Poly fetch
        if kalshi_props:
            oldest_updated = min(kp["updated_at"] for kp in kalshi_props)
            skew = (poly_fetch_time - oldest_updated).total_seconds()
            if skew > _MAX_TIMESTAMP_SKEW_SECONDS:
                print(
                    f"\n[WARN] Timestamp skew {skew:.0f}s: oldest Kalshi cache entry "
                    f"at {oldest_updated.strftime('%H:%M:%S')} UTC, "
                    f"Poly fetched at {poly_fetch_time.strftime('%H:%M:%S')} UTC.",
                    file=sys.stderr,
                )

        arbs = match_props(kalshi_props, poly_props)

        if arbs:
            _print_arbs(arbs, timestamp)
            for arb in arbs:
                _log_arb(arb, timestamp)
        else:
            ts = now_utc.strftime("%H:%M:%S")
            sys.stdout.write(
                f"\r[{ts}] No arb — {len(kalshi_props)}K/{len(poly_props)}P props "
                f"(WS live). Ctrl+C to stop.".ljust(100) + "\r"
            )
            sys.stdout.flush()

        remaining = POLL_INTERVAL_SECONDS - (loop.time() - cycle_start)
        await asyncio.sleep(max(0.0, remaining))


async def main_ws(api_key_id: str, private_key):
    """
    WebSocket mode entrypoint.
    1. REST-fetch today's Kalshi props once to populate ticker cache + player metadata.
    2. Start WS reader task (updates prices in real time).
    3. Start Poly polling task (merges + checks arbs every POLL_INTERVAL_SECONDS).
    """
    cache = KalshiPriceCache()

    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()

    print("Fetching initial Kalshi prop list via REST...", file=sys.stderr)
    try:
        initial_props = await asyncio.get_running_loop().run_in_executor(
            None, fetch_kalshi_props, today_utc
        )
        cache.seed_from_rest(initial_props)
        print(f"[WS] Seeded {len(initial_props)} Kalshi props from REST.", file=sys.stderr)
    except Exception as exc:
        print(f"[WARN] Initial Kalshi REST seed failed: {exc}. WS will subscribe when cache populates.", file=sys.stderr)

    today_getter = lambda: datetime.now(timezone.utc).date()

    ws_task = asyncio.create_task(
        _kalshi_ws_task(cache, api_key_id, private_key, today_getter)
    )
    poly_task = asyncio.create_task(
        _poly_polling_task(cache)
    )

    await asyncio.gather(ws_task, poly_task)


# ─── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    creds = _load_ws_credentials()
    if creds is None:
        # Graceful degradation: REST polling for both platforms
        main_rest_fallback()
    else:
        api_key_id, private_key = creds
        try:
            asyncio.run(main_ws(api_key_id, private_key))
        except KeyboardInterrupt:
            print("\nStopped.")
            sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped.")
        sys.exit(0)
