# Agent Instructions

Read this entire file before starting any task.

---

## Project Overview

### What this is

A **read-only arbitrage scanner** for MLB prediction markets. It polls Kalshi and Polymarket US every 5 seconds, identifies guaranteed-profit opportunities (total cost of buying both sides < $0.96), and logs them to `arb_log.jsonl`. No automated trade execution — opportunities are surfaced for manual action only.

### Architecture

```
active/mlb_arb_scanner/
  scanner.py        — main loop: moneyline arb (Kalshi YES team A + Polymarket YES team B)
  props_scanner.py  — player props arb (hits, home runs, total bases) across both platforms
  arb_log.jsonl     — append-only JSONL event log (opened/updated/closed/no_match/fetch_error)
  requirements.txt  — requests, python-dotenv
  .env              — KALSHI_API_KEY_ID, KALSHI_PRIVATE_KEY_PATH (for future trade execution)
```

### How arb detection works

1. **Moneyline arb** (`scanner.py`): For each Kalshi game market, find the opposing team's Polymarket side. Buy Kalshi YES (team A wins) + Polymarket YES (team B wins). If `kalshi_ask + poly_ask + both_fees < 0.96`, it's an arb.
2. **Props arb** (`props_scanner.py`): Match Kalshi player prop markets (KXMLBHIT, KXMLBHR, KXMLBTB) to Polymarket equivalents by player name + line. Buy YES on one platform + NO on the other if total < $0.96.
3. **Value plays** (`scanner.py:fetch_polymarket_spreads_totals`): Within Polymarket alone, flag spread/total markets where both sides sum < $0.96 (thin vig).

### Key constants

| Constant                    | Value | Meaning                             |
| --------------------------- | ----- | ----------------------------------- |
| `ARB_THRESHOLD`             | 0.96  | Max combined cost to qualify as arb |
| `POLL_INTERVAL_SECONDS`     | 5     | Main loop cadence                   |
| `KALSHI_TAKER_FEE_RATE`     | 0.01  | 1% of notional per Kalshi leg       |
| `POLYMARKET_TAKER_FEE_RATE` | 0.01  | 1% of notional per Polymarket leg   |

### API endpoints

| Platform                | Endpoint                                                | Auth                                                            |
| ----------------------- | ------------------------------------------------------- | --------------------------------------------------------------- |
| Kalshi markets          | `https://api.elections.kalshi.com/trade-api/v2/markets` | None (read)                                                     |
| Polymarket game markets | `https://gateway.polymarket.us/v2/leagues/mlb/events`   | None                                                            |
| Polymarket trading      | `https://api.polymarket.us`                             | Ed25519 (`X-PM-Access-Key`, `X-PM-Timestamp`, `X-PM-Signature`) |
| Kalshi trading          | `https://api.elections.kalshi.com/trade-api/v2`         | RSA key pair (`.env`)                                           |

### Scope boundaries

- **In scope**: MLB moneyline markets, MLB player props (hits/HR/total bases), Polymarket spreads/totals value plays
- **Out of scope**: Trade execution, other sports, international Polymarket (`gamma-api.polymarket.com`, `clob.polymarket.com` — these are macro futures endpoints, not game markets)
- **Not planned**: Automatic bet placement — this is intentionally read-only

### Running the scanner

```bash
cd active/mlb_arb_scanner
pip install -r requirements.txt
python scanner.py          # moneyline arb + value plays, runs continuously
python props_scanner.py    # player props arb, runs continuously
pytest test_scanner.py     # unit tests
```

---

## Self-Correcting Rules Engine

This file contains a growing ruleset that improves over time. **At session start, read the entire "Learned Rules" section before doing anything.**

### How it works

1. When the user corrects you or you make a mistake, **immediately append a new rule** to the "Learned Rules" section at the bottom of this file.
2. Rules are numbered sequentially and written as clear, imperative instructions.
3. Format: `N. [CATEGORY] Never/Always do X — because Y.`
4. Categories: `[STYLE]`, `[CODE]`, `[ARCH]`, `[TOOL]`, `[PROCESS]`, `[DATA]`, `[UX]`, `[OTHER]`
5. Before starting any task, scan all rules below for relevant constraints.
6. If two rules conflict, the higher-numbered (newer) rule wins.
7. Never delete rules. If a rule becomes obsolete, append a new rule that supersedes it.

### When to add a rule

- User explicitly corrects your output ("no, do it this way")
- User rejects a file, approach, or pattern
- You hit a bug caused by a wrong assumption about this codebase
- User states a preference ("always use X", "never do Y")

### Rule format example

```
14. [CODE] Always use `bun` instead of `npm` — user preference, bun is installed globally.
15. [STYLE] Never add emojis to commit messages — project convention.
16. [ARCH] API routes live in `src/server/routes/`, not `src/api/` — existing codebase pattern.
```

---

## Learned Rules

<!-- New rules are appended below this line. Do not edit above this section. -->

1. [ARCH] Never query `gamma-api.polymarket.com` or `clob.polymarket.com` for daily MLB game markets — those are international exchange endpoints that only hold macro futures. Daily MLB moneyline markets are exclusively on Polymarket US (CFTC-regulated). **Superseded by rule 4.**

2. [ARCH] Polymarket US market data is served from `gateway.polymarket.us` (no auth required). Use `GET https://gateway.polymarket.us/v2/leagues/mlb/events?limit=100` for MLB game discovery. Moneyline markets have `sportsMarketType == "baseball_team_full_game_winner"`. Each market has `marketSides[]` with one side per team; `team.abbreviation` is the standard lowercase code (e.g. `"cin"`, `"pit"`); `quote.value` is the ask price as a USD decimal string. The `api.polymarket.us` domain requires Ed25519 auth (`X-PM-Access-Key`, `X-PM-Timestamp` in ms, `X-PM-Signature`) but is only needed for trading/portfolio, not market data.

3. [CODE] The Kalshi v2 API returns ask prices as `yes_ask_dollars` (string, USD) not `yes_ask` (int cents). Always read `yes_ask_dollars` first and fall back to `yes_ask`. A `None` value means the market has no live quote, not that the field is missing.

4. [CODE] Kalshi market titles use abbreviated team names in `title` (e.g. `"Los Angeles D"`, `"Los Angeles A"`, `"New York M"`, `"New York Y"`, `"Chicago C"`, `"Chicago WS"`, `"A's"`). The `_ALIASES` table must include these short forms so `team_code()` can resolve them. Do not rely solely on full city/nickname strings.

5. [CODE] Never use Kalshi `close_time` to infer the game date or time — it is the settlement expiry (typically 3 days after the game). Extract game datetime from the ticker: `KXMLBGAME-26JUN281340SEACLE-SEA` → segment `26JUN281340` → `datetime.strptime("26JUN281340", "%y%b%d%H%M")`. Kalshi encodes game times in **ET (UTC-4)**, Polymarket `gameStartTime` is **UTC** — add 4 hours to Kalshi time before comparing. Match is valid if times align within 30 minutes after this adjustment. Date-only comparison allows cross-game false matches (same teams play multiple days; teams in same city share abbreviations across games).

6. [CODE] Never filter Polymarket player props by a single UTC date string. Evening ET games (7 PM ET = 23:xx UTC) cross midnight UTC, so `gameStartTime[:10] == today_str` drops all active props for those games. Always accept `today_str` and `yesterday_str`, then gate freshness on `active=True, closed=False` instead. For player props, use `GET /v1/search?query=mlb+will+record+at+least&limit=200` — the `/v2/leagues/mlb/events` endpoint only returns team-level markets (no `baseball_player_*` SMTs).

7. [CODE] `fetch_kalshi_props` must accept yesterday's UTC date in addition to today's. After midnight UTC, an evening ET game (e.g. 7 PM ET = 23:xx UTC) sits on yesterday's UTC date in Kalshi. Polymarket's `gameStartTime` also uses that same UTC date, so restricting Kalshi to `today_utc` only causes 0 matches until the next morning ET. Always pass `valid_dates = {today_utc, today_utc - 1 day}` as the date filter.
