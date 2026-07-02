# Architectural Decisions

Log of forks and the reasoning behind each choice.

## [2026-06-29] game-time timezone fix — renamed _kalshi_game_dt to _kalshi_game_dt_utc over patching call sites

The old `_kalshi_game_dt` returned a naive datetime tagged as UTC, and every call site then did `.replace(tzinfo=_ET)` on an already-tz-aware object — a silent no-op that left the value as UTC instead of converting ET→UTC. Two options: (A) fix each call site to call `.replace(tzinfo=_ET)` before the function, or (B) rename the function to `_kalshi_game_dt_utc` and do the ET→UTC conversion inside it. Chose B because it centralises the correct conversion and eliminates the call-site footgun. The old function remains in `ws_manager.py` for the `today_tickers` staleness check, which only needs date-level accuracy and is unaffected.

## [2026-06-30] REST confirm async close bug — mark_opened instead of _emit_prop_arbs

`_rest_confirm_and_emit` originally called `_emit_prop_arbs([single_arb], ...)`. `PropArbTracker.update()` diffs whatever list it receives against `_open` — so passing a one-item list would close every other open arb not in that list. Fixed by adding `PropArbTracker.mark_opened()` which inserts directly into `_open` without touching other entries. The confirm path now calls `mark_opened` + does its own print/log inline; `_emit_prop_arbs` is only ever called from `_run_props_arb_check_from_ws` with the full pass-through set. 6 regression tests added in `TestPropArbTracker` to cover this and the idempotency case.

## [2026-06-30] new MLB series title + SMT verification — confirmed live against both APIs

KXMLBKS, KXMLBHRR, KXMLBOUTS titles all match `^(.+?):\s*(\d+)\+`. Polymarket SMT strings (`baseball_player_strikeouts`, `baseball_player_hits_runs_rbis`, `baseball_player_outs`) confirmed live via `/v1/search?query=mlb+will+record+at+least` — all three appear with real markets. Comment in `SERIES_TO_SMT` updated to reflect confirmed status.

## [2026-06-30] props staleness + REST confirm — Kalshi-side only, Polymarket deferred

Implemented four changes to reduce false positives from stale Kalshi `ticker` WS prices:
1. `KalshiPriceCache.as_props_list()` now filters on `updated_at` freshness (`_CACHE_STALE_SECONDS = 300`) in addition to game-start time. Eliminates REST-seeded entries that were never touched by the WS.
2. Added `_kalshi_props_reconciliation_task()` (60s interval) to periodically re-pull Kalshi props via REST and merge into `_price_cache`. Also called once on every Kalshi WS (re)connect since there's no props-equivalent of `orderbook_snapshot`.
3. Added Kalshi-side REST-confirm gate: new prop arb opens fire a background `asyncio.create_task(_rest_confirm_and_emit)` that GETs `{KALSHI_BASE}/markets/{ticker}`, rechecks the threshold, and either emits or logs `ghost_rejected`. Per-ticker 5s cooldown prevents REST flooding. Only new opens are gated; updates and closes pass through unconfirmed.
4. All emitted arb events (open, update, close) now include `poly_ws_yes_ask`/`poly_ws_no_ask` for post-hoc Polymarket staleness analysis.

Polymarket-side REST confirmation is explicitly deferred — `gateway.polymarket.us/v1/markets?slug=` and `api.polymarket.us/v1/markets/{slug}/bbo` are behind Cloudflare CDN (`max-age=30`), so a confirm call within the arb window would return the same cached value as the WS event. Decision to revisit if `/v1/markets/{slug}/book` proves CDN-bypass-capable.

New Kalshi series added to `SERIES_TO_SMT`: `KXMLBKS` (strikeouts), `KXMLBHRR` (hits+runs+RBIs), `KXMLBOUTS` (outs recorded). Titles and Polymarket SMT strings both verified live before merge — see "new MLB series title + SMT verification" entry above.

## [2026-06-30] tennis tickers — switched KXATP/KXWTA to KXATPMATCH/KXWTAMATCH

`KXATP`/`KXWTA` are tournament-winner outright series ("Will Sinner win the US Open?"). Polymarket's `tennis_match_winner` markets are H2H match markets ("Tsitsipas vs Djokovic"). These market types can never cross-match, producing 0 matches and 111 "no players extracted" errors. `KXATPMATCH`/`KXWTAMATCH` are the correct Kalshi H2H series (96 markets each, confirmed live against the API). Title format for the new series is "Will [Player] win the [A vs B]: [Round] match?" — one ticker per player per match. TennisMatcher was rewritten to extract the subject player via regex and match against Polymarket's `displayName` (full name) rather than the 6-char slug abbreviation, with a short-name guard (< 4 char last names require exact full-name equality to avoid false positives).

## [2026-07-01] Kalshi WS v2 seq gap — chose full reconnect over per-ticker resubscribe

Live captures show `seq` is per subscription (`sid`), not per ticker: one stream numbers snapshots and deltas across every ticker in that subscribe call. A gap therefore invalidates every book on the stream, and the old single-ticker resubscribe could not heal it. On gap the WS task now raises and reconnects, which re-seeds and gets fresh snapshots for everything. Costs a reconnect on what should be a rare event; per-sid resubscribe bookkeeping wasn't worth the complexity.

## [2026-07-01] REST re-seeds are authoritative — chose overwrite+evict over merge-only-non-None

`seed_from_rest` and `_update_poly_props_map` now overwrite prices (None = "no live quote") and evict markets that vanished from a complete `status=open` pull (Kalshi, now paginated) or were seen closed/inactive in search results (Poly). Tradeoff: a REST value can briefly regress a WS-fresh price by a few seconds; the WS re-corrects on the next tick. Chosen because stale/ghost prices were the dominant failure mode (1,362 `no_quote_from_rest` ghost rejections in the log). Poly markets merely *absent* from search results are NOT evicted — search is fuzzy and absence doesn't mean closed.

## [2026-07-01] Doubleheader props — chose nearest-start-time (±30 min) over date-only key

`match_props` keyed on (smt, player, line, date); doubleheaders repeat that key, so both Kalshi markets matched whichever Poly game survived the index overwrite. Candidates are now kept per key and paired by nearest game start within the same 30-minute window the moneyline matcher uses (rule 5). Kalshi prop ticker times match Poly `gameStartTime` exactly in live data, so the window is safe.

## [2026-07-01] Poly WS "30s updates" — root-caused to client pipeline, not the WS; chose delta-resubscribe + freshness guard over feed switch

Live captures (3× parallel connections, 180–240s, in-season evening): `SUBSCRIPTION_TYPE_MARKET_DATA_LITE` is event-driven and sub-second — top-of-book lag vs a simultaneous full `MARKET_DATA` book subscription was p50=0.00s / p90=0.06s, with zero server errors subscribing 990 ML or 2,131 props slugs in a single frame (docs' 100-slug limit is not enforced). The believed ~30s cadence matches the REST/CDN path (`max-age=30`), not the WS. Rejected switching to full `MARKET_DATA` (no freshness benefit, ~2× message volume) and the gRPC streaming API (bigger lift, Auth0, preprod-documented). Actual latency sources fixed instead: (1) subscribe was once-per-connection, so markets discovered by the 60s reconcile never streamed — added a per-connection delta-subscribe loop (20s diff, reconnect-to-consolidate after 40 frames since the server caps subscriptions per connection); (2) REST re-seeds clobbered WS-fresh prices with CDN values up to 30s old — added `ws_at` provenance stamp + `_carry_ws_fresh_prices` guard (REST wins only if the WS hasn't touched the entry in 30s; WS silence means unchanged, and REST overwrite of >30s-quiet entries doubles as a dropped-message safety net since LITE has no seq numbers); (3) Poly ML had no periodic reconcile at all — `_poly_props_reconcile_task` extended to `_poly_reconcile_task` (ML + props + ML cache rebuild).

## [2026-07-01] Stacked branch — claude/fix-poly-ws-latency branched from claude/fix-kalshi-ws-pipeline, not main

The work modifies code (poly reconcile, staleness guards) that only exists in unmerged PR #14. Branching from main would have required cherry-picking or conflicting duplicates. PR #15 is based on the PR #14 branch and should merge after it.

## [2026-07-02] Review fixes — supersedes "REST re-seeds are authoritative" (2026-07-01) for price fields

REST re-seeds remain authoritative for market EXISTENCE (eviction of closed/vanished markets, discovery of new ones) but no longer for PRICES on WS-fresh entries: `_carry_ws_fresh_prices` keeps a side's price when the WS populated it within the last 30s (per-side; a side the WS never saw still takes the REST value). Priceless WS frames no longer stamp `ws_at`/`updated_at`, so the >30s REST-overwrite window stays live as the dropped-message safety net. Other review fixes: Poly WS connection runner now uses FIRST_COMPLETED (clean server close previously hung the task forever with prices silently stale); explicit JSON null in Kalshi ticker fields now clears the side (key-presence check); orderbook snapshots detect per-sid seq gaps and force reconnect; Kalshi reconciles serialize on a lock; both reconcile tasks fire the props arb check after re-seeding; props map purges vanished old-date slugs; fake_arb.py Kalshi WS layer ported to the v2 schema; ML map writes moved under _poly_ws_lock.
