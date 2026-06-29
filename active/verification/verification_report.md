# Subagent Verification Report

**Artifact**: scanner.py — MLB arb scanner (Kalshi + Polymarket US)
**Date**: 2026-06-28
**Rounds**: 1

## Review Verdict: FIXED

## Issues Found

| # | Severity | Location | Problem | Status |
|---|----------|----------|---------|--------|
| 1 | critical | `fetch_polymarket()` | Still pointed at `clob.polymarket.com` — wrong endpoint, no active MLB markets | Fixed |
| 2 | critical | `fetch_polymarket()` | HTTP errors not caught with useful context; misleading error message | Fixed |
| 3 | major | `fetch_kalshi()` | `yes_ask` unit detection used magnitude heuristic instead of explicit field check | Fixed |
| 4 | major | Config constants | `POLYMARKET_CLOB_BASE`, `POLYMARKET_GAMMA_BASE`, `POLYMARKET_LOOKAHEAD_DAYS` dead after CLOB removal | Fixed |
| 5 | major | `main()` | No startup validation that credentials are non-empty | Fixed |

## Simplifications Applied

- Removed `POLYMARKET_CLOB_BASE`, `POLYMARKET_GAMMA_BASE`, `POLYMARKET_LOOKAHEAD_DAYS` (unused after endpoint change)
- Kept `MAX_TIMESTAMP_SKEW_SECONDS` — reviewer incorrectly flagged as unused; it is used in `main()` skew check

## Changes Made

1. `fetch_polymarket()` rewritten to target `api.polymarket.us/v1/search?query=MLB&limit=50` with placeholder auth headers (`POLY-API-KEY`, `POLY-API-SECRET`, `POLY-API-PASSPHRASE`) and a TODO comment to replace once header names are confirmed
2. HTTP 401 now raises a specific `RuntimeError` naming the auth failure and including response body
3. Other HTTP errors caught via `requests.HTTPError` and re-raised with status code + body excerpt
4. `fetch_kalshi()` now explicitly reads `yes_ask_dollars` as USD or `yes_ask` as cents — no magnitude guessing
5. `main()` validates all three Polymarket credential env vars at startup, raises `EnvironmentError` with missing names if any are empty
6. Removed dead constants

## One Remaining Blocker

`fetch_polymarket()` auth header names (`POLY-API-KEY` etc.) are placeholder — the correct names for `api.polymarket.us/v1` are not yet confirmed. The scanner will log a clear `HTTP 401` error with instructions until this is resolved.

## Reviewer's Summary

The code had one critical structural failure (wrong API endpoint), a misleading error message, a fragile unit-detection heuristic, dead constants, and no credential validation. All have been addressed. The Kalshi side is fully functional. The Polymarket side is correctly wired to the US API but blocked on auth header name confirmation.
