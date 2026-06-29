"""
Quick diagnostic: show Kalshi prop names + Polymarket raw response.
"""
import json
import re
import unicodedata
from datetime import datetime, timezone, timedelta
import requests

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"
SERIES = ["KXMLBHIT", "KXMLBHR", "KXMLBTB"]
SUPPORTED_SMTS = {"baseball_player_hits", "baseball_player_home_runs", "baseball_player_total_bases"}
TODAY_UTC = datetime.now(timezone.utc).date()
TODAY_STR = TODAY_UTC.strftime("%Y-%m-%d")


def _kalshi_game_dt_utc(ticker: str) -> datetime | None:
    try:
        seg = ticker.split("-")[1]
        dt_et = datetime.strptime(seg[:11], "%y%b%d%H%M")
        return dt_et.replace(tzinfo=timezone.utc) + timedelta(hours=4)
    except (IndexError, ValueError):
        return None


print(f"=== Today UTC: {TODAY_STR} ===\n")

# ── Kalshi ──────────────────────────────────────────────────────────────────
print("── KALSHI PROPS ──")
total_k = 0
skipped_date = 0
skipped_parse = 0
skipped_no_price = 0

for series in SERIES:
    resp = requests.get(
        f"{KALSHI_BASE}/markets",
        params={"series_ticker": series, "status": "open", "limit": 200},
        timeout=8,
    )
    resp.raise_for_status()
    markets = resp.json().get("markets", [])
    print(f"\n  {series}: {len(markets)} raw markets from API")

    for m in markets:
        ticker = m.get("ticker", "")
        title = m.get("title", "")
        game_dt = _kalshi_game_dt_utc(ticker)

        if game_dt is None or game_dt.date() != TODAY_UTC:
            skipped_date += 1
            continue

        parsed = re.match(r"^(.+?):\s*(\d+)\+", title)
        if not parsed:
            skipped_parse += 1
            print(f"    [SKIP unparseable title] {ticker!r} → {title!r}")
            continue

        player, line = parsed.group(1).strip(), int(parsed.group(2))
        yes_ask = m.get("yes_ask_dollars") or m.get("yes_ask")
        no_ask = m.get("no_ask_dollars") or m.get("no_ask")

        if yes_ask is None and no_ask is None:
            skipped_no_price += 1
            continue

        print(f"    {player!r:30s}  line={line}  yes={yes_ask}  no={no_ask}  ticker={ticker}")
        total_k += 1

print(f"\n  Kept: {total_k}  |  Skipped (wrong date): {skipped_date}  |  Skipped (bad title): {skipped_parse}  |  Skipped (no price): {skipped_no_price}")

# ── Polymarket ───────────────────────────────────────────────────────────────
print("\n\n── POLYMARKET PROPS ──")
try:
    resp = requests.get(
        f"{POLYMARKET_US_GATEWAY}/v1/search",
        params={"query": "mlb will record at least", "limit": 100},
        timeout=8,
    )
    resp.raise_for_status()
    data = resp.json()
except Exception as exc:
    print(f"  ERROR: {exc}")
    data = {}

events = data.get("events", [])
print(f"  Raw events returned: {len(events)}")

total_p = 0
skipped_smt = 0
skipped_inactive = 0
skipped_date_p = 0
skipped_no_player = 0

for event in events:
    for m in event.get("markets", []):
        smt = m.get("sportsMarketType", "")
        if smt not in SUPPORTED_SMTS:
            skipped_smt += 1
            continue
        if not m.get("active") or m.get("closed") or m.get("archived"):
            skipped_inactive += 1
            continue
        gs = (m.get("gameStartTime") or "")[:10]
        if gs != TODAY_STR:
            skipped_date_p += 1
            print(f"    [SKIP wrong date {gs!r}] {m.get('metadata', {}).get('playerName', '?')} smt={smt}")
            continue
        player = (m.get("metadata") or {}).get("playerName", "")
        if not player:
            skipped_no_player += 1
            continue

        yes_ask = no_ask = None
        for side in m.get("marketSides", []):
            v = (side.get("quote") or {}).get("value")
            if side.get("long") is True:
                yes_ask = v
            elif side.get("long") is False:
                no_ask = v

        print(f"    {player!r:30s}  line={m.get('line')}  yes={yes_ask}  no={no_ask}  smt={smt}  start={m.get('gameStartTime','')[:16]}")
        total_p += 1

print(f"\n  Kept: {total_p}  |  Wrong SMT: {skipped_smt}  |  Inactive: {skipped_inactive}  |  Wrong date: {skipped_date_p}  |  No player: {skipped_no_player}")

# ── If zero Poly, dump first raw event ──────────────────────────────────────
if total_p == 0 and events:
    print("\n\n── FIRST RAW POLY EVENT (for debugging) ──")
    first = events[0]
    print(json.dumps(first, indent=2)[:3000])
elif total_p == 0:
    print("\n  No events returned at all. Raw response keys:", list(data.keys()))
    print("  Sample:", json.dumps(data)[:500])
