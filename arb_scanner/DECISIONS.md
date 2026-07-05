# Architectural Decisions — append-only

## [2026-07-05] Props book quotes accessor — chose separate `prop_quotes()` on KalshiOrderBook over extending `as_kalshi_markets()` because `as_kalshi_markets()` returns ML-matcher-shaped dicts (title/ask/taker_fee/raw) and the Layer-2 contract requires the moneyline path byte-identical; props need a different shape (yes_ask/no_ask/qty_at_best).

## [2026-07-05] Kalshi qty in arb events — chose plumbing real `qty_at_best` from the book through `match_props` into `prop_arb` events now, over a null placeholder, because the contract's failure conditions require qty passed to `match_props`, match dicts flow into events via `**arb` (so the field lands in events either way), and the design doc says only the Poly size is null (LITE has no sizes). Layer 3 keeps the analysis/validation work.

## [2026-07-05] Ticker-channel prop handling — chose removing `KalshiPriceCache.update_from_ws()` and making the WS `ticker` branch a silent drop, over keeping a no-op method, because nothing subscribes props to the ticker channel after Layer 2 and dead write paths invite silent drift (rule 15's spirit). Obsolete ticker-update tests are replaced by ignore-assertions.

## [2026-07-05] Seq tracking on ack frames — per LEARNED_RULES rule 19, chunked subscribes share one sid and both `subscribed`/`ok` acks consume seq numbers; Layer 2 adds `note_seq()` so every sid+seq-bearing frame participates in gap detection, else ~19 prop subscribe chunks phantom-gap the connection.

## [2026-07-05] Mid-session prop discovery — chose a 60s time-guarded resubscribe diff in the WS message loop over waiting for reconnect/rollover, because book-or-nothing pricing otherwise blinds the scanner to intra-day listings the old ticker-channel path served via 60s REST prices (design says REST reconcile stays for "discovery" — discovery without subscription is broken).

## [2026-07-05] Props sweep trigger — chose gating `_run_props_arb_check_from_ws` on top-of-book signature changes over a time coalescer, because it restores the old ticker-channel trigger semantics (fire on price change, not deep-book churn) and keeps ~241 msg/s of book traffic from burning the shared event loop during the 24h validation.

## [2026-07-05] Sweep coalescer — top-of-book gating alone couldn't survive connect: every first snapshot legitimately changes a signature, so 2,793 subscriptions fired 2,793 back-to-back sweeps and the event loop missed keepalive pongs (observed live, close 1011). Added a 100ms leading+trailing coalescer on the hot trigger paths (Kalshi book frames, Poly WS frames); 100ms added latency is noise next to the REST-confirm RTT already on every new open.
