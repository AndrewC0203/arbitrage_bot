# Subagent Verification Report

**Artifact**: `arb_scanner/matchers/tennis.py` — date/league/abbreviation disambiguation guards
**Date**: 2026-06-30
**Rounds**: 1

## Review Verdict: FIXED

## Issues Found

| # | Severity | Location | Problem | Status |
|---|---|---|---|---|
| 1 | medium | `_parse_kalshi_date` | `_MONTHS[key]` raises `KeyError` on unknown month abbreviation instead of returning `None` | Fixed |
| 2 | medium | `_parse_poly_date` | `date.fromisoformat()` raises `ValueError` on malformed date string instead of returning `None` | Fixed |
| 3 | low | `_abbr_fingerprint_ok` | If all slug parts are <3 chars, `hits=0 < 2` silently returns False (blocks valid market) | Fixed |
| 4 | low | `_kalshi_league` | Returns `"atp"` for any non-KXWTA prefix; relies on outer loop's prefix filter | Declined — outer loop guarantees only valid prefixes reach this function |
| 5 | low | `match()` | Uses `k_ask <= 0 or p_ask <= 0` while other matchers use `total_cost <= 0`; behavior equivalent | Declined — per-leg zero guard is strictly better (prevents 0+0.47 passing as valid) |

## Simplifications Applied

None — code is appropriately concise.

## Changes Made

1. `_parse_kalshi_date`: use `_MONTHS.get()` + `try/except ValueError` around `date()` constructor
2. `_parse_poly_date`: wrap `date.fromisoformat()` in `try/except ValueError`, return `None` on failure
3. `_abbr_fingerprint_ok`: filter `long_parts = [s for s in s_parts if len(s) >= 3]`; if empty, return `True` (conservative); require `hits >= min(2, len(long_parts))`

## Reviewer's Summary

All three guards correctly block the specified false positives (Kypson/NielsMcDonald, WTA/ATP cross-gender, 2-day date gap) and pass the known true positives. The primary actionable findings were two uncaught exception paths in the date parsers — both crash silently under malformed input rather than returning `None` and triggering the conservative pass. Fixed by wrapping with `try/except` and using `.get()` for the month lookup.

## Resolver's Notes

- `_kalshi_league` fragility declined: the outer `_VALID_PREFIXES` filter in `match()` ensures only `KXATPMATCH`/`KXWTAMATCH` tickers ever reach Guard 2. Changing the function would add complexity for a scenario that cannot occur in the current call path.
- `total_cost <= 0` inconsistency declined: the per-leg `k_ask <= 0 or p_ask <= 0` check is actually stricter and more correct — it rejects markets where one leg has no quote even if the other is valid.
