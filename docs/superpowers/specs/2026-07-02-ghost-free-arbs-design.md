# Ghost-Free Arb Detection — Design & Plan

**Date:** 2026-07-02 · **Status:** approved for planning, not yet implemented
**Goal:** every emitted arb is executable-grade — both quotes live, two-sided, sane. Kill the ghost class (36,136 `no_quote_from_rest` rejections + resolved-market phantoms) without overcomplicating.

## Evidence base (from arb_log.jsonl, 2026-06-30 → 07-02)

- 618 closed prop arbs: median lifetime 0.6s, 68% < 2s. Median logged "edge" 9¢ — implausibly fat.
- 36,319 ghost rejections vs 618 tracked arbs (~58:1). Dominant reason: Kalshi WS-cached price had no live book behind it.
- Ghost signature (live capture 2026-07-02 03:33–03:48): in-game props where one leg is pinned (NO ask 0.01 ⇒ YES bid 0.99 ⇒ effectively resolved) while the other venue's cached price is minutes stale (e.g. Kalshi YES=0.58 on a decided market). Every pasted ghost fits this or the mid-disagreement pattern (venue mids 30–50¢ apart).
- The existing `suspicious` flag (`total < 0.80`) is cosmetic — it tags the print line but suppresses nothing.

## Ghost taxonomy → which layer kills it

| Cause | Example | Killed by |
|---|---|---|
| Market resolved in-game, other venue stale | Elly De La Cruz 1+ hits, Poly NO=0.01 + Kalshi YES=0.58 | F1 pinned filter |
| Venue mids wildly apart (one book stale) | Perdomo: Poly mid ~0.45 vs Kalshi mid ~0.92, REST-confirm passed (it only checks Kalshi) | F2 mid-agreement |
| Too-good-to-be-true edge | 50–76¢ "gaps" | F3 edge cap |
| Empty/one-sided real book behind cached price | 36k `no_quote_from_rest` | F2/F4 + Layer 2 (book channel) |
| Dropped ticker-channel message (no seq) | undetectable today | Layer 2 (seq-gapped book channel) |

## Layer 1 — Quote-sanity filters (no new data needed)

All computable from existing cached fields on both venues: `yes_bid = 1 − no_ask`, `mid = (yes_ask + yes_bid)/2`. Applied in `match_props` at pair time (single choke point; both legs in hand). Replaces the cosmetic `suspicious` flag.

- **F1 — pinned market:** skip pair if either leg is effectively resolved: `yes_bid ≥ GHOST_PIN_PROB` or `yes_ask ≤ 1 − GHOST_PIN_PROB`. Default `GHOST_PIN_PROB = 0.97`.
- **F2 — mid agreement:** require both venues' mids computable (both sides present = two-sided requirement for free) and `|mid_K − mid_P| ≤ GHOST_MID_DISAGREEMENT_MAX` (default `0.15`). Real cross-venue mispricings are small divergences; 30¢+ canyons mean one book is stale. Catches what the Kalshi-only REST confirm structurally cannot.
- **F3 — edge cap:** require `gap_cents ≤ GHOST_MAX_GAP_CENTS` (default `10.0`, i.e. total_cost ≥ 0.86). On liquid sports books a 20¢+ risk-free edge is a data error ~always.
- **F4 — spread sanity:** per venue require `0 ≤ (yes_ask − yes_bid) ≤ GHOST_MAX_SPREAD` (default `0.20`). Crossed = stale; canyon-wide = dead book. Nearly free given F2's inputs.

Dropped deliberately: pairwise `updated_at` skew check — both sides get REST-bumped every 60s, so the timestamp no longer carries freshness signal; F1–F4 subsume its value. (Rule 8 staleness filtering stays as-is.)

**Observability:** no per-suppression log events (would recreate the 36k noise). Per-reason counters (`pinned` / `mid_disagreement` / `edge_cap` / `spread` / `one_sided`), emitted as one `ghost_filter_summary` JSONL event per hour + printed on the status line.

**Regression fixtures:** every ghost from the 2026-07-02 paste becomes a test case (must be suppressed); the Bryce Eldridge 26s arb shape (Kalshi YES=0.45 + Poly NO=0.09, mids agreeing) must pass through.

## Layer 2 — Kalshi props on `orderbook_delta`

**Why it's better (verified against live protocol):** the `ticker` channel has no seq numbers (drops indistinguishable from "unchanged" — LEARNED_RULES rule 8) and no sizes. The book channel has both, plus true two-sided quotes: `yes_ask = 1 − max(NO bids)`, `yes_bid = max(YES bids)`, `no_ask = 1 − yes_bid`, with qty at every level. This removes the ambiguity that made the REST-confirm gate necessary and unlocks size-aware arbs.

**Unknown to probe first (Phase 0):** Kalshi per-connection subscription limits and message volume at ~800–2,000 prop tickers on the book channel (ML uses only ~74 today; subscribe chunk size 50). One-shot `debug/` probe script: subscribe increasing ticker counts, record snapshot coverage, errors, msg/s, and whether the existing per-sid seq scheme holds at that scale.

**Migration shape (if probe passes):**
- `KalshiOrderBook` already does everything needed (two-sided, per-sid seq, multi-ticker) — extend seeding/subscription to include prop tickers; keep `KalshiPriceCache` for metadata (player, line, game_dt, eviction) but read prices/sizes from the book.
- `ticker`-channel handling for props goes away; REST reconcile stays (discovery + eviction + safety net).
- Date rollover and reconnect paths already handle books.

**Fallback (if probe fails — caps too low):** stay on the ticker channel; Layer 1 filters + existing REST confirm remain the defense. Layer 1 is designed to stand alone for exactly this case.

## Layer 3 — Consequences & cleanup

- **REST-confirm demotion:** after Layer 2, run a ~1-week validation window logging what the confirm gate would have decided vs. book state at the same instant. If book-sourced opens stop producing `ghost_rejected`, remove the gate (eliminates its 5s cooldown + REST RTT from new-open latency). Until then it stays.
- **Capacity logging:** arb events gain `kalshi_qty_at_best` for both Kalshi legs (from the book). Poly size is null for now (LITE has no sizes) — a week of this data sizes the real opportunity before any execution work.
- **Deferred (not in this plan):** Poly full `MARKET_DATA` for sizes (~2× message volume; adopt only if capacity data demands), execution groundwork, ML-side filters (only 3 ML opens in the whole log — no evidence of an ML ghost problem; revisit if that changes).

## Approaches considered

- **A (chosen): filters + book-channel migration.** Kills ghosts two ways (sanity math now, authoritative data next), each layer independently useful, probe gates the risky part.
- **B: filters only.** Half the work, but staleness stays undetectable (no seq), sizes stay unknown, REST confirm stays forever. Acceptable fallback, not the goal.
- **C: full execution-grade quote layer (both venues full books + unified depth model).** Most capable; overkill before capacity data justifies it. Explicitly rejected for now.

## Config (new constants beside ARB_THRESHOLD)

| Constant | Default | Meaning |
|---|---|---|
| GHOST_PIN_PROB | 0.97 | leg implied ≥97% resolved → skip |
| GHOST_MID_DISAGREEMENT_MAX | 0.15 | max venue-mid divergence |
| GHOST_MAX_GAP_CENTS | 10.0 | edges fatter than this are presumed fake |
| GHOST_MAX_SPREAD | 0.20 | max per-venue bid/ask spread |

## Phased implementation plan

Each phase = its own branch + PR (stacked on #15 until #14/#15 merge), TDD, atomic commits, DECISIONS.md entry at any fork.

- **Phase 0 — probe (no product code, ~half day):** `debug/probe_kalshi_props_book.py`; output: max viable subscription count, msg/s, go/no-go for Phase 2.
- **Phase 1 — ghost filters (~1 day):** pure filter functions + wiring in `match_props` + counters/summary event + fixture tests from the pasted ghosts. Ships value regardless of Phase 2's outcome.
- **Phase 2 — props on the book channel (gated on Phase 0, ~2–3 days):** subscribe props to `orderbook_delta`, price reads from book, ticker-channel removal, tests against live-captured book messages.
- **Phase 3 — confirm-gate demotion + size logging (~1 day + 1 week soak):** validation logging, then gate removal if clean; `kalshi_qty_at_best` in arb events.

## Success criteria

1. All pasted-ghost fixtures suppressed; Eldridge-class real arb passes. (unit)
2. 24h live run: filtered opens produce ~zero `ghost_rejected`; no open arb shows a pinned leg or >10¢ gap. (live)
3. `prop_arb` event volume collapses from ~18k/2days to plausible levels — the log becomes a list of tradeable opportunities, not noise.
4. After Phase 3 soak: new-open emission latency no longer includes REST RTT.
