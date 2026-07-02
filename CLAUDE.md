# Agent Instructions — read CLAUDE.md and LEARNED_RULES.md entirely before any task.

## Project: MLB/NBA/Soccer/Tennis Arb Scanner

Read-only WebSocket arbitrage scanner for Kalshi + Polymarket US. Logs opportunities to `arb_log.jsonl`. No trade execution.

### Architecture

```
arb_scanner/
  ws_manager.py          # MAIN ENTRY: dual WebSocket engine
  matchers/
    base.py              # BaseMatcher ABC
    baseball.py          # MLB moneyline + alias resolution
    basketball.py        # NBA/WNBA/NCAAB
    soccer.py            # EPL/MLS/Champions League
    tennis.py            # ATP/WTA
  arb_log.jsonl          # append-only JSONL (opened/updated/closed/prop_arb/ghost_filter_summary/fetch_error)
  ghost_log.jsonl        # suppressed ghost arbs (ghost_suppressed) — pattern analysis, 60s dedupe
  test_scanner.py        # unit tests
  requirements.txt       # aiohttp, websockets, requests, python-dotenv, cryptography
  .env                   # KALSHI_KEY_ID, KALSHI_KEY_PATH, POLYMARKET_US_KEY_ID, POLYMARKET_US_SECRET_KEY
  debug/                 # diagnostic one-shot scripts (not in main loop)
  DECISIONS.md           # architectural decision log (append-only)
active/                  # Claude agent reports — not code
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

Fees are computed per-leg as a fraction of the ask price, then summed:

```
k_fee   = kalshi_ask  × KALSHI_TAKER_FEE_RATE   (0.01)
p_fee   = poly_ask    × POLYMARKET_TAKER_FEE_RATE (0.01)
total_cost = kalshi_ask + poly_ask + k_fee + p_fee
           = 1.01 × (kalshi_ask + poly_ask)
is_arb  = total_cost < ARB_THRESHOLD (0.96)
```

1. **Moneyline**: Kalshi YES (team A) + Polymarket YES (team B). Arb when `total_cost < 0.96`.
2. **Props**: Match by `(smt, player_norm, line, game_date)`. YES one side + NO other; same formula. Would-be prop arbs must then pass the Layer-1 ghost filters (F1 pinned, F2 mid agreement, F3 edge cap, F4 spread/two-sided) — suppressions go to `ghost_log.jsonl`, per-reason counts to hourly `ghost_filter_summary` events.

### Sports Configs

| Sport  | Kalshi tickers            | Polymarket slugs           | Matcher           |
| ------ | ------------------------- | -------------------------- | ----------------- |
| MLB    | KXMLBGAME                 | mlb                        | BaseballMatcher   |
| NBA    | KXNBA, KXWNBA, KXCBB      | nba, wnba, ncaab           | BasketballMatcher |
| Soccer | KXEPL, KXMLS, KXCHAMPIONS | epl, mls, champions-league | SoccerMatcher     |
| Tennis | KXATPMATCH, KXWTAMATCH    | atp, wta                   | TennisMatcher     |

### Key Constants

| Constant                  | Value | Meaning                        |
| ------------------------- | ----- | ------------------------------ |
| ARB_THRESHOLD             | 0.96  | Max combined cost for arb      |
| POLY_POLL_SECONDS         | 2     | REST seed cadence              |
| KALSHI_TAKER_FEE_RATE     | 0.01  | 1% per Kalshi leg              |
| POLYMARKET_TAKER_FEE_RATE | 0.01  | 1% per Polymarket leg          |
| \_CACHE_STALE_SECONDS     | 300   | Evict entries older than 5 min |
| GHOST_PIN_PROB            | 0.97  | F1: leg implied ≥97% resolved → suppress |
| GHOST_MID_DISAGREEMENT_MAX | 0.15 | F2: max venue-mid divergence   |
| GHOST_MAX_GAP_CENTS       | 10.0  | F3: fatter edges presumed fake |
| GHOST_MAX_SPREAD          | 0.20  | F4: max per-venue bid/ask spread |

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

## Git Workflow

- Always branch from main. **Never commit directly to main.**
- Branch naming: `claude/<type>-<short-description>` (e.g. `claude/fix-odds-normalization`)
- Open a **draft PR immediately** when starting any new branch — before writing code
- Commit atomically after each logical unit of change. No mega-commits.
- Conventional Commits: `feat:`, `fix:`, `refactor:`, `chore:`, `test:` — subject ≤ 50 chars
- Fill out `.github/pull_request_template.md` before marking a PR ready-for-review
- No auto-push.
- Full details: `skills/git-workflow.md`

## Decision Logging

When you hit a fork with 2+ reasonable approaches, append to `DECISIONS.md` before proceeding:

```
## [YYYY-MM-DD] [topic] — chose X over Y because Z
```

Don't halt — log and proceed. I'll review at PR time.

## Scripts vs Agents

Data fetching and transformation (fetch → transform → output) stays as scripts. They don't need to reason. Subagents are for tasks requiring judgment or decision-making mid-process. Do not refactor working scripts into agents.

## Skills

| Skill                        | When to invoke                                       |
| ---------------------------- | ---------------------------------------------------- |
| `skills/git-workflow.md`     | Full branch/commit/PR procedure                      |
| `skills/pr-writer.md`        | Generating PR descriptions from diff + commit log    |
| `skills/debug.md`            | Before assuming root cause on any bug                |
| `skills/prompt-contracts.md` | Defining subagent I/O contracts                      |
| `skills/unit-tests.md`       | PICT-based unit test generation                      |
| `skills/subagent-review.md`  | Reviewing subagent output before using it downstream |

---

## Self-Correcting Rules Engine

All learned rules live in **`LEARNED_RULES.md`** — read it at session start, every session.

When corrected or when you make a mistake, immediately append a rule there.

Format: `N. [CATEGORY] Never/Always X — because Y.`
Categories: `[STYLE]` `[CODE]` `[ARCH]` `[TOOL]` `[PROCESS]` `[DATA]` `[UX]` `[OTHER]`

Higher-numbered rule wins on conflict. Never delete rules — supersede with a new one.

Add a rule when: user corrects output, rejects a file/approach/pattern, you hit a bug from a wrong assumption, or user states a preference.
