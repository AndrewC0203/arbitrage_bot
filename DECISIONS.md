# Architectural Decisions

Log of forks and the reasoning behind each choice.

## [2026-06-29] game-time timezone fix — renamed _kalshi_game_dt to _kalshi_game_dt_utc over patching call sites

The old `_kalshi_game_dt` returned a naive datetime tagged as UTC, and every call site then did `.replace(tzinfo=_ET)` on an already-tz-aware object — a silent no-op that left the value as UTC instead of converting ET→UTC. Two options: (A) fix each call site to call `.replace(tzinfo=_ET)` before the function, or (B) rename the function to `_kalshi_game_dt_utc` and do the ET→UTC conversion inside it. Chose B because it centralises the correct conversion and eliminates the call-site footgun. The old function remains in `ws_manager.py` for the `today_tickers` staleness check, which only needs date-level accuracy and is unaffected.

## [2026-06-30] props staleness + REST confirm — Kalshi-side only, Polymarket deferred

Implemented four changes to reduce false positives from stale Kalshi `ticker` WS prices:
1. `KalshiPriceCache.as_props_list()` now filters on `updated_at` freshness (`_CACHE_STALE_SECONDS = 300`) in addition to game-start time. Eliminates REST-seeded entries that were never touched by the WS.
2. Added `_kalshi_props_reconciliation_task()` (60s interval) to periodically re-pull Kalshi props via REST and merge into `_price_cache`. Also called once on every Kalshi WS (re)connect since there's no props-equivalent of `orderbook_snapshot`.
3. Added Kalshi-side REST-confirm gate: new prop arb opens fire a background `asyncio.create_task(_rest_confirm_and_emit)` that GETs `{KALSHI_BASE}/markets/{ticker}`, rechecks the threshold, and either emits or logs `ghost_rejected`. Per-ticker 5s cooldown prevents REST flooding. Only new opens are gated; updates and closes pass through unconfirmed.
4. All emitted arb events (open, update, close) now include `poly_ws_yes_ask`/`poly_ws_no_ask` for post-hoc Polymarket staleness analysis.

Polymarket-side REST confirmation is explicitly deferred — `gateway.polymarket.us/v1/markets?slug=` and `api.polymarket.us/v1/markets/{slug}/bbo` are behind Cloudflare CDN (`max-age=30`), so a confirm call within the arb window would return the same cached value as the WS event. Decision to revisit if `/v1/markets/{slug}/book` proves CDN-bypass-capable.

New Kalshi series added to `SERIES_TO_SMT`: `KXMLBKS` (strikeouts), `KXMLBHRR` (hits+runs+RBIs), `KXMLBOUTS` (outs recorded). Title format spot-checked against `_kalshi_parse_title()` regex. Polymarket SMT strings are inferred (`baseball_player_strikeouts`, `baseball_player_hits_runs_rbis`, `baseball_player_outs`) — if no matches appear in production, the SMT value is wrong and needs a live `/v1/search` check.

## [2026-06-30] tennis tickers — switched KXATP/KXWTA to KXATPMATCH/KXWTAMATCH

`KXATP`/`KXWTA` are tournament-winner outright series ("Will Sinner win the US Open?"). Polymarket's `tennis_match_winner` markets are H2H match markets ("Tsitsipas vs Djokovic"). These market types can never cross-match, producing 0 matches and 111 "no players extracted" errors. `KXATPMATCH`/`KXWTAMATCH` are the correct Kalshi H2H series (96 markets each, confirmed live against the API). Title format for the new series is "Will [Player] win the [A vs B]: [Round] match?" — one ticker per player per match. TennisMatcher was rewritten to extract the subject player via regex and match against Polymarket's `displayName` (full name) rather than the 6-char slug abbreviation, with a short-name guard (< 4 char last names require exact full-name equality to avoid false positives).
