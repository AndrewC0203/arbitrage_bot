# Architectural Decisions

Log of forks and the reasoning behind each choice.

## [2026-06-29] game-time timezone fix — renamed _kalshi_game_dt to _kalshi_game_dt_utc over patching call sites

The old `_kalshi_game_dt` returned a naive datetime tagged as UTC, and every call site then did `.replace(tzinfo=_ET)` on an already-tz-aware object — a silent no-op that left the value as UTC instead of converting ET→UTC. Two options: (A) fix each call site to call `.replace(tzinfo=_ET)` before the function, or (B) rename the function to `_kalshi_game_dt_utc` and do the ET→UTC conversion inside it. Chose B because it centralises the correct conversion and eliminates the call-site footgun. The old function remains in `ws_manager.py` for the `today_tickers` staleness check, which only needs date-level accuracy and is unaffected.

## [2026-06-30] tennis tickers — switched KXATP/KXWTA to KXATPMATCH/KXWTAMATCH

`KXATP`/`KXWTA` are tournament-winner outright series ("Will Sinner win the US Open?"). Polymarket's `tennis_match_winner` markets are H2H match markets ("Tsitsipas vs Djokovic"). These market types can never cross-match, producing 0 matches and 111 "no players extracted" errors. `KXATPMATCH`/`KXWTAMATCH` are the correct Kalshi H2H series (96 markets each, confirmed live against the API). Title format for the new series is "Will [Player] win the [A vs B]: [Round] match?" — one ticker per player per match. TennisMatcher was rewritten to extract the subject player via regex and match against Polymarket's `displayName` (full name) rather than the 6-char slug abbreviation, with a short-name guard (< 4 char last names require exact full-name equality to avoid false positives).
