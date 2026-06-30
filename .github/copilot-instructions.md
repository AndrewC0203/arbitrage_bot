# Claude PR Review Instructions — MLB/NBA/Soccer/Tennis Arbitrage Scanner

## Project Overview

This is a **read-only WebSocket arbitrage scanner** for Kalshi + Polymarket US markets.
It detects pricing inefficiencies across MLB, NBA, Soccer, and Tennis moneyline and prop markets.
**No trade execution occurs.** All opportunities are logged to `arb_log.jsonl`.

### Architecture Summary

- `arb_scanner/ws_manager.py` — main entry point; dual WebSocket engine (Kalshi + Polymarket)
- `arb_scanner/matchers/` — per-sport matching logic (baseball, basketball, soccer, tennis)
- `arb_scanner/test_scanner.py` — unit tests
- Kalshi WS: RSA auth, `orderbook_delta` + `ticker` message types
- Polymarket WS: Ed25519 auth, `SUBSCRIPTION_TYPE_MARKET_DATA_LITE`
- Arb fires when `kalshi_ask + poly_ask + fees < 0.96` (moneyline) or equivalent for props

### Key Constants

| Constant | Value | Meaning |
|---|---|---|
| `ARB_THRESHOLD` | 0.96 | Max combined cost for arb signal |
| `KALSHI_TAKER_FEE_RATE` | 0.01 | 1% per Kalshi leg |
| `POLYMARKET_TAKER_FEE_RATE` | 0.01 | 1% per Polymarket leg |
| `_CACHE_STALE_SECONDS` | 300 | Evict price entries older than 5 min |

---

## Review Focus Areas

### 1. Error Handling Detection

Flag any of the following as **high priority**:

- WebSocket message handlers that `return None` / `return` silently on parse errors instead of logging and continuing
- `except Exception: pass` or `except Exception as e: ...` blocks that swallow errors without logging
- Missing reconnect logic or exponential backoff after WebSocket disconnection
- REST seed failures that don't fall back gracefully (the scanner must seed before WS connects)
- Cache writes that can silently overwrite stale data without a staleness check
- Any arb check that fires on `None` or default prices (e.g., `0.0`) due to an upstream failure

**Context**: A silent WebSocket drop means the scanner runs with stale prices and logs false arb signals. Reconnect paths and seed-before-subscribe ordering are critical.

### 2. Type Design Analysis

Flag any of the following:

- Dicts used as ad-hoc message objects where a `dataclass` or `TypedDict` would be safer
- Raw `dict` returns from WebSocket message parsers with no schema enforcement
- `Optional[float]` prices used in arithmetic without `None` guards
- Loose string keys (e.g., `msg["bestAsk"]`) without a defined message schema
- `Any` type annotations on price, token, or market ID fields
- Mixing of decimal probability (0.0–1.0) and percentage (0–100) representations without explicit conversion

**Context**: Price fields use decimal probabilities. `bestAsk` from Polymarket is a YES ask; NO ask is derived as `1 - bestBid`. Type confusion here produces incorrect arb signals.

### 3. Test Coverage Analysis

Flag when:

- New matcher logic has no corresponding test in `test_scanner.py`
- Tests only cover the happy path (matched team, valid price) and skip:
  - Unmatched team aliases
  - `None` / missing price fields
  - Cache staleness / eviction behavior
  - Game date boundary conditions (ET→UTC crossover around midnight)
  - Prop line mismatches (same player, different line)
- Arb threshold boundary is not tested (`< 0.96` passes, `== 0.96` should not)
- Reconnect / backoff logic is not exercised

**Context**: Prop game times are UTC; ET evening games cross midnight UTC. Tests that assume same-day UTC will miss real boundary bugs.

### 4. Comment Accuracy (Implementation Drift)

Flag when:

- A function's docstring describes behavior that no longer matches the implementation (e.g., says "returns None on miss" but now raises)
- Inline comments reference old field names, thresholds, or logic that was refactored
- `# TODO` or `# FIXME` comments reference issues that are already resolved in the diff
- Module-level docstrings don't reflect new message types or sports added in the PR
- `DECISIONS.md` or `LEARNED_RULES.md` entries reference behavior contradicted by the new code

### 5. Arb Logic Correctness

This scanner's core invariant: **an arb signal must only fire on fresh, valid prices from both sides.**

Flag:

- Arb checks that don't verify both Kalshi and Polymarket prices are non-None and non-zero
- Prop matching that ignores `game_date` (could match yesterday's game)
- Moneyline matching that doesn't normalize team names before comparison
- Fee calculation that hardcodes `0.02` instead of using `KALSHI_TAKER_FEE_RATE + POLYMARKET_TAKER_FEE_RATE`
- `arb_log.jsonl` entries written without a timestamp or with a duplicate-detection gap

---

## Severity Guidelines

| Severity | When to use |
|---|---|
| **Critical** | Silent failure in WebSocket handler; arb check fires on stale/null prices; fee math is wrong |
| **High** | Missing `None` guard on price arithmetic; swallowed exception hides a reconnect failure; test covers 0 edge cases |
| **Medium** | Docstring drifted from implementation; loose dict where TypedDict fits; missing game-date boundary test |
| **Low** | Style nit; redundant comment; minor naming inconsistency |

---

## What NOT to flag

- REST polling tasks commented out in `ws_manager.py` — these are intentionally disabled (debug only)
- Use of `gamma-api` or `clob.polymarket.com` is out of scope by design — do not suggest adding them
- `archive/` and `debug/` directories — not production code, not in scope
- Existing dead code not touched by this PR — mention only, never request deletion
- Complexity of RSA/Ed25519 auth boilerplate — this is API-mandated, not a design choice

---

## Response Style

- Lead with a **one-paragraph summary** of the PR's purpose and overall risk level
- Group findings by severity (Critical → High → Medium → Low)
- For each finding: state the file + line, the specific problem, and a concrete fix or question
- When replying to a follow-up question in the PR thread, always ground your answer in the diff or the architecture described above — do not speculate about code not shown
- Keep suggestions surgical: recommend the minimum change that resolves the issue

