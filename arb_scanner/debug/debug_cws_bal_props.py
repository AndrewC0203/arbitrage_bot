"""
Diagnostic: fetch all CWS vs BAL player props from Kalshi + Polymarket for today,
then show which matched and which didn't.
"""
import re
import sys
import unicodedata
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
POLYMARKET_US_GATEWAY = "https://gateway.polymarket.us"
REST_TIMEOUT = 8
_ET = ZoneInfo("America/New_York")

SERIES_TO_SMT = {
    "KXMLBHIT": "baseball_player_hits",
    "KXMLBHR":  "baseball_player_home_runs",
    "KXMLBTB":  "baseball_player_total_bases",
}

TEAMS = {"cws", "bal"}

TODAY_UTC = datetime.now(timezone.utc).date()
YESTERDAY_UTC = TODAY_UTC - timedelta(days=1)
TODAY_STR = TODAY_UTC.isoformat()
YESTERDAY_STR = YESTERDAY_UTC.isoformat()


def _normalize(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", s.lower())).strip()


def _kalshi_game_dt_utc(ticker: str) -> datetime | None:
    # Format: KXMLBHIT-26JUN301835CWSBAL-BALPALONSO25-1
    # The date+time is the first 11 chars of segment[1]: "26JUN301835"
    try:
        seg = ticker.split("-")[1]
        dt_naive = datetime.strptime(seg[:11], "%y%b%d%H%M")
        return dt_naive.replace(tzinfo=_ET).astimezone(timezone.utc)
    except (IndexError, ValueError):
        return None


def _kalshi_ask(market: dict, side: str) -> float | None:
    raw = market.get(f"{side}_ask_dollars")
    if raw is None:
        raw2 = market.get(f"{side}_ask")
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


def _ticker_teams(ticker: str) -> tuple[str, str] | None:
    # e.g. KXMLBHIT-26JUL011905-CWS-PLAYER-3+
    parts = ticker.split("-")
    if len(parts) < 3:
        return None
    # The team code is typically the 3rd segment
    team = parts[2].lower()
    return team, team  # single-team prop, return same for both slots


def _is_cws_or_bal(ticker: str) -> bool:
    upper = ticker.upper()
    return "CWS" in upper or "BAL" in upper


def _poly_ask(side: dict) -> float | None:
    raw = (side.get("quote") or {}).get("value")
    if raw is None:
        return None
    try:
        val = float(raw)
        return val if 0 < val < 1 else None
    except (TypeError, ValueError):
        return None


# ─── Fetch Kalshi props ───────────────────────────────────────────────────────

print(f"=== CWS vs BAL Player Props Debug — {TODAY_STR} ===\n")
print("─── KALSHI ─────────────────────────────────────────────────────────────")

valid_dates = {TODAY_UTC, YESTERDAY_UTC}
kalshi_props: list[dict] = []
skipped_team = 0
skipped_date = 0
skipped_parse = 0
skipped_price = 0

for series in SERIES_TO_SMT:
    try:
        resp = requests.get(
            f"{KALSHI_BASE}/markets",
            params={"series_ticker": series, "status": "open", "limit": 200},
            timeout=REST_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"  ERROR fetching {series}: {exc}", file=sys.stderr)
        continue

    markets = resp.json().get("markets", [])
    print(f"\n  {series} ({SERIES_TO_SMT[series]}): {len(markets)} raw open markets")

    for m in markets:
        ticker = m.get("ticker", "")

        if not _is_cws_or_bal(ticker):
            skipped_team += 1
            continue

        game_dt = _kalshi_game_dt_utc(ticker)
        if game_dt is None or game_dt.date() not in valid_dates:
            skipped_date += 1
            continue

        title = m.get("title", "")
        parsed = re.match(r"^(.+?):\s*(\d+)\+", title)
        if not parsed:
            skipped_parse += 1
            print(f"    [SKIP bad title] {ticker} → {title!r}")
            continue

        player_name = parsed.group(1).strip()
        line = int(parsed.group(2))
        yes_ask = _kalshi_ask(m, "yes")
        no_ask = _kalshi_ask(m, "no")

        if yes_ask is None and no_ask is None:
            skipped_price += 1
            continue

        # Pull team from player segment: e.g. BALPALONSO25 → BAL, CWSCMONTGOMERY8 → CWS
        player_seg = ticker.upper().split("-")[2] if len(ticker.split("-")) > 2 else ""
        team = "cws" if player_seg.startswith("CWS") else "bal" if player_seg.startswith("BAL") else "?"

        entry = {
            "series": series,
            "smt": SERIES_TO_SMT[series],
            "ticker": ticker,
            "team": team,
            "player_name": player_name,
            "player_norm": _normalize(player_name),
            "line": line,
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "game_dt_utc": game_dt,
        }
        kalshi_props.append(entry)
        print(f"    [{team.upper()}] {player_name:28s}  line={line:2d}  yes={yes_ask}  no={no_ask}")

print(f"\n  Total kept: {len(kalshi_props)}")
print(f"  Skipped — wrong team: {skipped_team}  wrong date: {skipped_date}  bad title: {skipped_parse}  no price: {skipped_price}")


# ─── Fetch Polymarket props ───────────────────────────────────────────────────

print("\n\n─── POLYMARKET ─────────────────────────────────────────────────────────")

CWS_BAL_KEYWORDS = ["white sox", "chicago ws", "cws", "orioles", "baltimore", "bal"]
TEAM_SLUG_HINTS = [
    "chicago-white-sox", "baltimore-orioles",
    "white-sox", "orioles",
]

all_events: list[dict] = []
queries = [
    "mlb will record at least",
    "chicago white sox",
    "baltimore orioles",
]

seen_event_ids: set = set()
for q in queries:
    try:
        resp = requests.get(
            f"{POLYMARKET_US_GATEWAY}/v1/search",
            params={"query": q, "limit": 200},
            timeout=REST_TIMEOUT,
        )
        resp.raise_for_status()
        for ev in resp.json().get("events", []):
            eid = ev.get("id") or ev.get("slug") or str(ev)
            if eid not in seen_event_ids:
                seen_event_ids.add(eid)
                all_events.append(ev)
    except requests.RequestException as exc:
        print(f"  ERROR on query {q!r}: {exc}", file=sys.stderr)

print(f"  Total unique events fetched: {len(all_events)}")

supported_smts = set(SERIES_TO_SMT.values())
valid_date_strs = {TODAY_STR, YESTERDAY_STR}

poly_props: list[dict] = []
skipped_smt = 0
skipped_inactive = 0
skipped_date_p = 0
skipped_no_player = 0
skipped_price_p = 0

for event in all_events:
    event_title = event.get("title", "")

    # Check if event is CWS/BAL related
    ev_lower = event_title.lower()
    is_cws_bal = any(kw in ev_lower for kw in CWS_BAL_KEYWORDS)

    for m in event.get("markets", []):
        smt = m.get("sportsMarketType", "")
        if smt not in supported_smts:
            skipped_smt += 1
            continue
        if not m.get("active") or m.get("closed") or m.get("archived"):
            skipped_inactive += 1
            continue

        game_start = m.get("gameStartTime", "") or ""
        if game_start[:10] not in valid_date_strs:
            skipped_date_p += 1
            continue

        metadata = m.get("metadata") or {}
        player_name = metadata.get("playerName", "")
        if not player_name:
            skipped_no_player += 1
            continue

        # Check team from metadata or event title
        player_team = (metadata.get("teamAbbreviation") or metadata.get("team") or "").lower()
        market_title = (m.get("title") or "").lower()
        question = (m.get("question") or "").lower()

        is_cws_or_bal = (
            player_team in ("cws", "chw", "bal")
            or is_cws_bal
            or any(kw in market_title or kw in question for kw in CWS_BAL_KEYWORDS)
        )

        if not is_cws_or_bal:
            continue

        yes_ask = no_ask = None
        for side in m.get("marketSides", []):
            ask = _poly_ask(side)
            if side.get("long") is True:
                yes_ask = ask
            elif side.get("long") is False:
                no_ask = ask

        if yes_ask is None and no_ask is None:
            skipped_price_p += 1
            continue

        entry = {
            "smt": smt,
            "player_name": player_name,
            "player_norm": _normalize(player_name),
            "player_team": player_team,
            "line": m.get("line"),
            "yes_ask": yes_ask,
            "no_ask": no_ask,
            "game_start": game_start,
            "event_title": event_title,
        }
        poly_props.append(entry)
        team_disp = player_team.upper() if player_team else "?"
        print(f"    [{team_disp}] {player_name:28s}  line={str(m.get('line')):4s}  yes={yes_ask}  no={no_ask}  smt={smt.replace('baseball_player_','')}")

print(f"\n  Total kept: {len(poly_props)}")
print(f"  Skipped — wrong SMT: {skipped_smt}  inactive: {skipped_inactive}  wrong date: {skipped_date_p}  no player: {skipped_no_player}  no price: {skipped_price_p}")


# ─── Match ───────────────────────────────────────────────────────────────────

print("\n\n─── MATCH RESULTS ──────────────────────────────────────────────────────")

poly_index: dict[tuple, dict] = {}
for pp in poly_props:
    game_date = pp["game_start"][:10]
    key = (pp["smt"], pp["player_norm"], pp["line"], game_date)
    poly_index[key] = pp

matched_keys: set[tuple] = set()
matched: list[dict] = []
unmatched_kalshi: list[dict] = []

for kp in kalshi_props:
    game_date = kp["game_dt_utc"].strftime("%Y-%m-%d")
    key = (kp["smt"], kp["player_norm"], kp["line"], game_date)
    pp = poly_index.get(key)
    if pp:
        matched_keys.add(key)
        matched.append({"kalshi": kp, "poly": pp})
    else:
        unmatched_kalshi.append({"kalshi": kp, "key": key})

unmatched_poly = [pp for pp in poly_props
                  if (pp["smt"], pp["player_norm"], pp["line"], pp["game_start"][:10]) not in matched_keys]

print(f"\n  MATCHED ({len(matched)}):")
if matched:
    for item in matched:
        kp = item["kalshi"]
        pp = item["poly"]
        k_yes, k_no = kp["yes_ask"], kp["no_ask"]
        p_yes, p_no = pp["yes_ask"], pp["no_ask"]

        arbs = []
        if p_yes is not None and k_no is not None:
            total = p_yes + k_no
            if total < 0.96:
                arbs.append(f"Poly YES({p_yes}) + Kalshi NO({k_no}) = {total:.4f} *** ARB ***")
        if k_yes is not None and p_no is not None:
            total = k_yes + p_no
            if total < 0.96:
                arbs.append(f"Kalshi YES({k_yes}) + Poly NO({p_no}) = {total:.4f} *** ARB ***")

        print(f"    [{kp['team'].upper()}] {kp['player_name']:28s}  {kp['smt'].replace('baseball_player_',''):12s}  line={kp['line']}")
        print(f"      Kalshi  yes={k_yes}  no={k_no}   |  Poly  yes={p_yes}  no={p_no}")
        for arb_str in arbs:
            print(f"      ** {arb_str}")
else:
    print("    (none)")

print(f"\n  UNMATCHED — Kalshi only ({len(unmatched_kalshi)}):")
if unmatched_kalshi:
    for item in unmatched_kalshi:
        kp = item["kalshi"]
        key = item["key"]
        print(f"    [{kp['team'].upper()}] {kp['player_name']:28s}  {kp['smt'].replace('baseball_player_',''):12s}  line={kp['line']}  norm={kp['player_norm']!r}  date={kp['game_dt_utc'].date()}")
        # Find closest poly entry by name to diagnose mismatch
        close = [pp for pp in poly_props if kp["player_norm"] in pp["player_norm"] or pp["player_norm"] in kp["player_norm"]]
        for pp in close:
            print(f"      ~ Poly near-match: {pp['player_name']!r}  norm={pp['player_norm']!r}  line={pp['line']}  date={pp['game_start'][:10]}")
else:
    print("    (none)")

print(f"\n  UNMATCHED — Polymarket only ({len(unmatched_poly)}):")
if unmatched_poly:
    for pp in unmatched_poly:
        print(f"    [{pp['player_team'].upper() if pp['player_team'] else '?'}] {pp['player_name']:28s}  {pp['smt'].replace('baseball_player_',''):12s}  line={pp['line']}  norm={pp['player_norm']!r}  date={pp['game_start'][:10]}")
        close = [kp for kp in kalshi_props if pp["player_norm"] in kp["player_norm"] or kp["player_norm"] in pp["player_norm"]]
        for kp in close:
            print(f"      ~ Kalshi near-match: {kp['player_name']!r}  norm={kp['player_norm']!r}  line={kp['line']}  date={kp['game_dt_utc'].date()}")
else:
    print("    (none)")
