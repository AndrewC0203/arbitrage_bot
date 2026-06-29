# Agent Instructions — read entirely before any task.

## Project: MLB/NBA/Soccer/Tennis Arb Scanner

Read-only WebSocket arbitrage scanner for Kalshi + Polymarket US. Logs opportunities to `arb_log.jsonl`. No trade execution.

### Architecture

```
arb_scanner/
  ws_manager.py       # MAIN ENTRY: dual WebSocket engine
  matchers/
    base.py           # BaseMatcher ABC
    baseball.py       # MLB moneyline + alias resolution
    basketball.py     # NBA/WNBA/NCAAB
    soccer.py         # EPL/MLS/Champions League
    tennis.py         # ATP/WTA
  arb_log.jsonl       # append-only JSONL (opened/updated/closed/prop_arb/fetch_error)
  test_scanner.py     # unit tests
  requirements.txt    # aiohttp, websockets, requests, python-dotenv, cryptography
  .env                # KALSHI_KEY_ID, KALSHI_KEY_PATH, POLYMARKET_US_KEY_ID, POLYMARKET_US_SECRET_KEY
  debug/              # diagnostic one-shot scripts (not in main loop)
active/               # Claude agent reports — not code
archive/rest_fallback/   # REST-polling predecessors
archive/migration_scripts/ # one-time migration scripts
```

### WebSocket Engine

**Kalshi** (`wss://api.elections.kalshi.com/trade-api/ws/v2`, RSA auth):

- `orderbook_delta` — live YES-side order book → arb check on every delta
- `ticker` — prop market prices (KXMLBHIT/HR/TB, KXNBAPTS/REB/AST/3PT) → `KalshiPriceCache`
- Seeded from REST on startup; exponential backoff reconnect; handles date rollover

**Polymarket** (`wss://api.polymarket.us/v1/ws/markets`, Ed25519 auth):

- `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` for all ML + props slugs
- `marketDataLite`: `bestAsk` = YES ask; NO ask = `1 - bestBid`
- Updates `_poly_ws_ml_token_map` or `_poly_ws_props_token_map`; arb check fires immediately
- Seeded from REST before WS connects
- REST polling tasks in `ws_manager.py` are **commented out** (debug only)

### Arb Detection

1. **Moneyline**: Kalshi YES (team A) + Polymarket YES (team B). If `kalshi_ask + poly_ask + fees < 0.96` → arb.
2. **Props**: Match by `(smt, player_norm, line, game_date)`. YES one side + NO other if total < 0.96.
3. **Value plays**: Polymarket MLB spread/total markets where both sides < 0.96 (REST seed only, not WS).

### Sports Configs

| Sport  | Kalshi tickers            | Polymarket slugs           | Matcher           |
| ------ | ------------------------- | -------------------------- | ----------------- |
| MLB    | KXMLBGAME                 | mlb                        | BaseballMatcher   |
| NBA    | KXNBA, KXWNBA, KXCBB      | nba, wnba, ncaab           | BasketballMatcher |
| Soccer | KXEPL, KXMLS, KXCHAMPIONS | epl, mls, champions-league | SoccerMatcher     |
| Tennis | KXATP, KXWTA              | atp, wta                   | TennisMatcher     |

### Key Constants

| Constant                  | Value | Meaning                        |
| ------------------------- | ----- | ------------------------------ |
| ARB_THRESHOLD             | 0.96  | Max combined cost for arb      |
| POLY_POLL_SECONDS         | 2     | REST seed cadence              |
| KALSHI_TAKER_FEE_RATE     | 0.01  | 1% per Kalshi leg              |
| POLYMARKET_TAKER_FEE_RATE | 0.01  | 1% per Polymarket leg          |
| \_CACHE_STALE_SECONDS     | 300   | Evict entries older than 5 min |

### API Endpoints

| Platform           | Endpoint                                                  | Auth                                                      |
| ------------------ | --------------------------------------------------------- | --------------------------------------------------------- |
| Kalshi WS          | `wss://api.elections.kalshi.com/trade-api/ws/v2`          | RSA (KALSHI-ACCESS-KEY, -TIMESTAMP, -SIGNATURE)           |
| Kalshi REST seed   | `https://api.elections.kalshi.com/trade-api/v2/markets`   | None                                                      |
| Polymarket WS      | `wss://api.polymarket.us/v1/ws/markets`                   | Ed25519 (X-PM-Access-Key, X-PM-Timestamp, X-PM-Signature) |
| Polymarket ML seed | `https://gateway.polymarket.us/v2/leagues/{sport}/events` | None                                                      |
| Polymarket props   | `https://gateway.polymarket.us/v1/search?query=...`       | None                                                      |

**Scope**: MLB/NBA/Soccer/Tennis moneyline + MLB/NBA props. No trade execution. `gamma-api` and `clob.polymarket.com` are out of scope (international, macro futures only).

```bash
cd arb_scanner && pip install -r requirements.txt
python ws_manager.py    # main loop
pytest test_scanner.py  # unit tests
```

---

## Self-Correcting Rules Engine

At session start, read **all** Learned Rules before anything else. When corrected or when you make a mistake, immediately append a rule below.

Format: `N. [CATEGORY] Never/Always X — because Y.`
Categories: [STYLE] [CODE] [ARCH] [TOOL] [PROCESS] [DATA] [UX] [OTHER]
Higher-numbered rule wins on conflict. Never delete rules; supersede with a new one.

Add a rule when: user corrects output, rejects a file/approach/pattern, you hit a bug from a wrong assumption, or user states a preference.

### Commit Guidelines

- Atomic commits per logical unit. Conventional Commits format (`feat:`, `fix:`, `refactor:`, `chore:`).
- Subject line ≤ 50 chars. No auto-push.

---

## Learned Rules

<!-- Append new rules below. Do not edit above. -->

1. [ARCH] Never query `gamma-api.polymarket.com` or `clob.polymarket.com` for daily MLB game markets — international exchange endpoints, macro futures only. **Superseded by rule 4.**

2. [ARCH] Polymarket US market data: `GET https://gateway.polymarket.us/v2/leagues/mlb/events?limit=100`. Moneyline markets have `sportsMarketType == "baseball_team_full_game_winner"`. Each market has `marketSides[]` with `team.abbreviation` (lowercase, e.g. `"cin"`) and `quote.value` (ask as USD decimal string). `api.polymarket.us` requires Ed25519 auth but only for trading, not market data.

3. [CODE] Kalshi v2 returns ask prices as `yes_ask_dollars` (string, USD), not `yes_ask` (int cents). Read `yes_ask_dollars` first, fall back to `yes_ask`. `None` means no live quote, not missing field.

4. [CODE] Kalshi titles use abbreviated team names (e.g. `"Los Angeles D"`, `"New York M"`, `"Chicago WS"`, `"A's"`). `_ALIASES` must include these short forms. Don't rely on full city/nickname strings.

5. [CODE] Never use Kalshi `close_time` for game date/time — it's the settlement expiry (~3 days post-game). Extract from ticker: `KXMLBGAME-26JUN281340SEACLE-SEA` → `26JUN281340` → `datetime.strptime(..., "%y%b%d%H%M")`. Kalshi times are **ET (UTC-4)**; Polymarket `gameStartTime` is **UTC** — add 4h before comparing. Match valid if within 30 min. Date-only comparison allows false matches (same teams, multiple days).

6. [CODE] Never filter Polymarket player props by a single UTC date string — evening ET games (7 PM ET = 23:xx UTC) cross midnight UTC and get dropped. Accept both `today_str` and `yesterday_str`; gate freshness on `active=True, closed=False`. For props use `GET /v1/search?query=mlb+will+record+at+least&limit=200` — `/v2/leagues/mlb/events` only returns team-level markets.

7. [CODE] `fetch_kalshi_props` must accept yesterday's UTC date. After midnight UTC, evening ET games sit on yesterday's UTC date in Kalshi. Always pass `valid_dates = {today_utc, today_utc - 1 day}`.
