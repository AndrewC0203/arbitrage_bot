# Architectural Decisions — append-only

## [2026-07-05] Props book quotes accessor — chose separate `prop_quotes()` on KalshiOrderBook over extending `as_kalshi_markets()` because `as_kalshi_markets()` returns ML-matcher-shaped dicts (title/ask/taker_fee/raw) and the Layer-2 contract requires the moneyline path byte-identical; props need a different shape (yes_ask/no_ask/qty_at_best).

## [2026-07-05] Kalshi qty in arb events — chose plumbing real `qty_at_best` from the book through `match_props` into `prop_arb` events now, over a null placeholder, because the contract's failure conditions require qty passed to `match_props`, match dicts flow into events via `**arb` (so the field lands in events either way), and the design doc says only the Poly size is null (LITE has no sizes). Layer 3 keeps the analysis/validation work.

## [2026-07-05] Ticker-channel prop handling — chose removing `KalshiPriceCache.update_from_ws()` and making the WS `ticker` branch a silent drop, over keeping a no-op method, because nothing subscribes props to the ticker channel after Layer 2 and dead write paths invite silent drift (rule 15's spirit). Obsolete ticker-update tests are replaced by ignore-assertions.

## [2026-07-05] Seq tracking on ack frames — per LEARNED_RULES rule 19, chunked subscribes share one sid and both `subscribed`/`ok` acks consume seq numbers; Layer 2 adds `note_seq()` so every sid+seq-bearing frame participates in gap detection, else ~19 prop subscribe chunks phantom-gap the connection.
