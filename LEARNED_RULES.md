# Learned Rules

Read this file entirely at session start before any task.
Append here when corrected or when you make a mistake.
Higher-numbered rule wins on conflict. Never delete — supersede with a new rule.

Format: `N. [CATEGORY] Never/Always X — because Y.`
Categories: `[STYLE]` `[CODE]` `[ARCH]` `[TOOL]` `[PROCESS]` `[DATA]` `[UX]` `[OTHER]`

---

1. [ARCH] Never query `gamma-api.polymarket.com` or `clob.polymarket.com` for daily MLB game markets — international exchange endpoints, macro futures only. **Superseded by rule 4.**

2. [ARCH] Polymarket US market data: `GET https://gateway.polymarket.us/v2/leagues/mlb/events?limit=100`. Moneyline markets have `sportsMarketType == "baseball_team_full_game_winner"`. Each market has `marketSides[]` with `team.abbreviation` (lowercase, e.g. `"cin"`) and `quote.value` (ask as USD decimal string). `api.polymarket.us` requires Ed25519 auth but only for trading, not market data.

3. [CODE] Kalshi v2 returns ask prices as `yes_ask_dollars` (string, USD), not `yes_ask` (int cents). Read `yes_ask_dollars` first, fall back to `yes_ask`. `None` means no live quote, not missing field.

4. [CODE] Kalshi titles use abbreviated team names (e.g. `"Los Angeles D"`, `"New York M"`, `"Chicago WS"`, `"A's"`). `_ALIASES` must include these short forms. Don't rely on full city/nickname strings.

5. [CODE] Never use Kalshi `close_time` for game date/time — it's the settlement expiry (~3 days post-game). Extract from ticker: `KXMLBGAME-26JUN281340SEACLE-SEA` → `26JUN281340` → `datetime.strptime(..., "%y%b%d%H%M")`. Kalshi times are **ET (UTC-4)**; Polymarket `gameStartTime` is **UTC** — add 4h before comparing. Match valid if within 30 min. Date-only comparison allows false matches (same teams, multiple days).

6. [CODE] Never filter Polymarket player props by a single UTC date string — evening ET games (7 PM ET = 23:xx UTC) cross midnight UTC and get dropped. Accept both `today_str` and `yesterday_str`; gate freshness on `active=True, closed=False`. For props use `GET /v1/search?query=mlb+will+record+at+least&limit=200` — `/v2/leagues/mlb/events` only returns team-level markets.

7. [CODE] `fetch_kalshi_props` must accept yesterday's UTC date. After midnight UTC, evening ET games sit on yesterday's UTC date in Kalshi. Always pass `valid_dates = {today_utc, today_utc - 1 day}`.

8. [ARCH] Kalshi `ticker` WS channel has no sequence number — a dropped/coalesced message is silently indistinguishable from "price hasn't changed." Always filter `KalshiPriceCache.as_props_list()` on `updated_at` freshness (`_CACHE_STALE_SECONDS`) in addition to game-start staleness. Never treat a REST-seeded price as fresh WS data.

9. [ARCH] Polymarket US single-market REST endpoints (`gateway.polymarket.us/v1/markets?slug=` and `api.polymarket.us/v1/markets/{slug}/bbo`) are behind Cloudflare CDN with `Cache-Control: max-age=30` — a confirm call made immediately after a WS-detected arb likely returns the same cached value. Do NOT use these endpoints for Polymarket-side arb confirmation.

10. [CODE] When adding background `asyncio.create_task` calls from a synchronous function, always ensure the function is called from a running event loop context. `_run_props_arb_check_from_ws` is always invoked from the asyncio WS handler chain, so `create_task` is safe there.

11. [DATA] Kalshi `GET /markets/{ticker}` (single-market endpoint) returns `{"market": {...}}` — unwrap with `data.get("market", data)` to handle both wrapped and bare formats defensively.

12. [ARCH] `PropArbTracker` close detection requires ALL currently-valid arbs (not just new ones) to be passed to `_emit_prop_arbs`. When gating new opens through REST confirmation, still call `_emit_prop_arbs(confirmed_pass_through, ...)` with the already-open arbs so that disappearances are detected and logged as closures.
