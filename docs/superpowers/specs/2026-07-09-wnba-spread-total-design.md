# WNBA Spread + Total Coverage Arbs — Design

Date: 2026-07-09
Status: approved-by-default (autonomous session; per CLAUDE.md "log and proceed")

## Goal

Add a new sport/market class to the scanner: WNBA spread and total markets,
matched between Kalshi (KXWNBASPREAD, KXWNBATOTAL) and Polymarket US
(`basketball_team_full_game_spread`, `basketball_team_full_game_total`).

## Why WNBA

Research across every live Polymarket US league (2026-07-09):

| Candidate | Poly US live? | Markets beyond moneyline on BOTH venues? |
| --------- | ------------- | ---------------------------------------- |
| UFC       | yes (33 mkts) | no — Poly has `ufc_fight_winner` only (Kalshi has MOV/rounds/distance) |
| Golf/F1/NASCAR/boxing/NFL/Indy | league slug exists, 0 events | — |
| Esports (LoL/CS2), cricket (MLC/T20) | yes | no — match winner only |
| WNBA      | yes (13 events) | **yes** — spread + total ladders on both venues |

Kalshi also lists WNBA player props (KXWNBAPTS/REB/AST/3PT) but Polymarket US
carries no WNBA player props (verified via `/v1/search` with prop phrasings and
player names), so player props cannot be matched yet.

## The line-grid problem → coverage matching

Verified live (IND@PHX 2026-07-09):

- Kalshi totals: strikes 171.5 … 186.5 step 3. Poly totals: 161.5 … 185.5 step 3.
  **Offset by 1 — zero exact-line overlap.**
- Kalshi spreads: strikes 1.5/3.5/6.5/9.5. Poly spreads: 1.5/4.5/7.5/10.5.
  Overlap only at 1.5.

So exact-line complement matching is nearly empty. Instead we match **coverage
pairs**: opposite sides at unequal lines chosen in the gap-free direction, so at
least one leg always pays $1 (both pay in the "middle" band — pure upside).

Normalize every market to a *threshold proposition* `over(Q, t)` on quantity Q:

- Total: Q = combined points. Kalshi YES = over(Q, floor_strike); NO = under.
  Poly `long=True` side = over(Q, line); `long=False` = under.
- Spread: Q = winning margin of the "cover team" C named in the market title
  ("Phoenix wins by over 6.5 points"). Kalshi YES = over(margin_C, floor_strike).
  Poly: the side whose team == title team is YES of the title proposition;
  other side is the complement. v1 matches same-frame only (both venues list
  both teams' frames symmetrically — logged in DECISIONS.md).

Coverage rule, for Kalshi strike `k` and Poly line `p` (both half-integer):

- Direction A — Kalshi YES over(k) + Poly under(p): covered iff `p >= k`.
- Direction B — Kalshi NO under(k) + Poly over(p): covered iff `p <= k`.

For each Kalshi market and direction, only the **tightest** valid Poly line is
considered — it is simultaneously the cheapest (an under at a higher line
always costs more) and the most line-adjacent, so nothing better exists.

Cost condition unchanged: `ask1 + fee(ask1) + ask2 + fee(ask2) < ARB_THRESHOLD`.
A guaranteed ≥ $1 payout for < $0.96 total cost is an arb exactly as for
complements; the both-win band is extra.

## Architecture: extend the props pipeline

The props pipeline already provides everything except the join: per-series
Kalshi REST seed + orderbook_delta WS (`SERIES_TO_SMT` prefix routing at
`_apply_book_frame`), Poly slug-keyed token map + marketDataLite WS, ghost
filters, `PropArbTracker`, Kalshi REST confirm gate, coalesced sweeps.

Changes:

1. **`SERIES_TO_SMT`** += `KXWNBASPREAD → basketball_team_full_game_spread`,
   `KXWNBATOTAL → basketball_team_full_game_total`. A `THRESHOLD_SMTS` set marks
   the two team SMTs for branch decisions.
2. **Kalshi parsing** (`fetch_kalshi_props` + reconcile path): for the two new
   series, parse identity from market fields, not the `^name: N+` title regex:
   - line = `floor_strike` (float, half-integer)
   - total: identity = `total:<codeA>-<codeB>` (sorted team codes from title
     "Indiana vs Phoenix" via existing `teams_from_kalshi_title`)
   - spread: identity = `spread:<cover_code>` (title prefix before
     " wins by over" via `team_code`; a WNBA team plays ≤ 1 game/day and the
     ±30-min start-time check disambiguates dates)
   Identity is stored in the existing `player_norm` field; `player_name` holds
   a human label ("IND-PHX total 186.5" / "Phoenix -6.5").
3. **Poly seeding**: team spread/total markets arrive in the *league events*
   payload (`/v2/leagues/wnba/events`), not `/v1/search`. `_update_poly_props_map`
   gains a league-events source feeding the same `_poly_ws_props_token_map`,
   with the same identity scheme (event title → team codes for totals; title
   team → cover code for spreads). yes_ask/no_ask stored in the same frame as
   the identity (yes = over / cover-team covers).
4. **Team-code join** (rule 23): Poly WNBA abbreviations (`lv`, `conn`, `gsv`,
   `por`, `wsh`, `la`, `ny`, …) do not all resolve through `_ALIASES`. Add a
   WNBA abbr→code map + missing aliases (Toronto Tempo, Portland Fire,
   Golden State Valkyries disambiguation) in `matchers/basketball.py`; a
   `wnba_team_code()` helper checks WNBA aliases before the shared table so
   "Golden State" resolves to the Valkyries, not the Warriors, inside this
   WNBA-only pipeline.
5. **`match_props`**: threshold SMTs take a coverage join — Poly entries
   indexed by `(smt, identity, game_date)` → line-sorted list; per Kalshi entry
   and direction, pick the tightest valid line, apply the same fee/threshold
   arithmetic and ghost filters (F1 pinned, F2 mid-agreement, F3 edge cap,
   F4 spread), and emit through the existing tracker/confirm path with
   `stat_type` = `"spread"` / `"total"` and the Kalshi and Poly lines both
   logged (they differ).
6. **No new venue plumbing**: WS subscription, sweeps, tracker, REST confirm,
   ghost logging all work unchanged once the series appear in `SERIES_TO_SMT`
   and the Poly slugs are in the token map.

## Error handling

- Unresolvable team code (new/renamed team) → skip market (existing pattern:
  parse failure = skip, never guess).
- Poly spread markets whose title team matches neither side abbr → skip.
- Kalshi markets without `floor_strike` → skip.

## Testing

Unit tests (pytest, `test_scanner.py`), fixtures validated against fees.py
arithmetic per rule 22:

- coverage-direction legality (p ≥ k / p ≤ k, both spread and total)
- tightest-line selection picks the adjacent line, not a farther-cheaper one
- no match emitted when coverage would leave a gap (the false-arb direction)
- fee arithmetic at unequal lines stays below/above ARB_THRESHOLD as designed
- Kalshi parsing of KXWNBATOTAL/KXWNBASPREAD fixtures (real shapes from live API)
- Poly league-event parsing incl. abbr→code resolution for all 13 current teams
- existing player-prop matching unaffected (regression)

## Out of scope

- WNBA player props (no Poly counterpart yet — revisit when Poly lists them)
- NBA spread/total (off-season; same machinery will apply)
- Cross-frame spread conversion (v1 same-frame only)
- Quarter/half markets (Kalshi has them; Poly does not)
