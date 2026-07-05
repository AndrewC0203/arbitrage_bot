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

13. [CODE] Never call `_emit_prop_arbs([single_arb], ...)` from a background confirm task — `PropArbTracker.update()` closes everything in `_open` not present in the passed list. Use `PropArbTracker.mark_opened()` for confirmed arbs; it inserts directly into `_open` without touching other entries.

14. [PROCESS] Always verify new Kalshi prop series against live REST before wiring in: (1) fetch 5 titles via `GET /markets?series_ticker=<SERIES>` and check against `^(.+?):\s*(\d+)\+`; (2) confirm Polymarket SMT string via `/v1/search?query=mlb+will+record+at+least` before adding to `SERIES_TO_SMT`. Silent zero-match days are hard to notice — verify at the point of addition, not after merge.

15. [ARCH] When two code paths produce the same log event schema, extract a shared helper rather than duplicating the format inline. Two copies of a print/log block will silently drift. Current precedent: `_print_and_log_prop_open()` is the single source of truth for `prop_arb` open events — both `_emit_prop_arbs` and `_rest_confirm_and_emit` call it.

16. [PROCESS] Always invoke applicable superpowers skills (e.g. systematic-debugging for bug hunts) before starting a task — because the user expects the skills-first workflow and corrected the agent for skipping it.

17. [DATA] Kalshi WS v2 payloads are nested under `msg` — top level carries only `type`/`sid`/`seq`. Never read `market_ticker`, prices, or book levels from the top level. Fields are dollar-strings: books arrive as `yes_dollars_fp`/`no_dollars_fp` (fractional qty strings), deltas as `price_dollars`+`delta_fp`, ticker msgs as `yes_bid_dollars`/`yes_ask_dollars`. Verified live 2026-07-01.

18. [DATA] Kalshi WS orderbook `yes`/`no` arrays are resting BIDS for each side — best YES ask = 1 − max(NO bids), never min(YES levels). The `ticker` channel has no NO-side fields; derive `no_ask = 1 − yes_bid_dollars`. Verified live 2026-07-01: snapshot max(yes)=REST yes_bid, 1−max(no)=REST yes_ask.

19. [DATA] Kalshi WS v2 chunked subscribes to the same channel share ONE sid — chunk 1 acks `type:"subscribed"`, chunks 2..N ack `type:"ok"`, and BOTH ack types consume seq numbers on that sid. Always track seq across every sid+seq-bearing frame (not just book frames), or every chunk boundary / mid-session subscription update shows a phantom gap and forces a needless reconnect. Verified live 2026-07-02 (951-ticker orderbook_delta probe: 12 "gaps" with book-only tracking, 0 with all-frame tracking).

20. [ARCH] Never fire per-frame full sweeps during a WS connect burst — subscribing N books delivers N snapshots back-to-back and each one legitimately changes state, so per-frame gating alone can't help; at 2,793 props the back-to-back sweeps starved the event loop, keepalive pongs were missed, and the socket died with close 1011 (`keepalive ping timeout`) every ~60–90s. Coalesce hot-path sweep triggers (leading+trailing edge, ~100ms). Reproduced and fix verified live 2026-07-05.
