# Architectural Decisions

Log of forks and the reasoning behind each choice.

## [2026-06-29] game-time timezone fix — renamed _kalshi_game_dt to _kalshi_game_dt_utc over patching call sites

The old `_kalshi_game_dt` returned a naive datetime tagged as UTC, and every call site then did `.replace(tzinfo=_ET)` on an already-tz-aware object — a silent no-op that left the value as UTC instead of converting ET→UTC. Two options: (A) fix each call site to call `.replace(tzinfo=_ET)` before the function, or (B) rename the function to `_kalshi_game_dt_utc` and do the ET→UTC conversion inside it. Chose B because it centralises the correct conversion and eliminates the call-site footgun. The old function remains in `ws_manager.py` for the `today_tickers` staleness check, which only needs date-level accuracy and is unaffected.
