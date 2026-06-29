# Agent Chatroom Report — Sports Arb Strategy Comparison

**Problem**: Which of three sports-adjacent arb strategies is most viable for a solo developer with $10K–$50K capital?
**Strategies**: A (Polymarket vs. sportsbooks signal), B (Execution speed arb), C (Pure Kalshi×Polymarket cross-platform)
**Agents**: 3 | **Rounds**: 3
**Date**: 2026-06-26

---

## Participants

| Agent   | Role                 | Final Verdict      | Final Confidence |
|---------|----------------------|--------------------|-----------------|
| Agent A | Opportunities Analyst | Strategy B, scoped | 7/10            |
| Agent B | Execution Engineer   | Strategy B, Kalshi-only v1 | 7/10     |
| Agent C | Risk Analyst         | Strategy B > A > C | 6.5/10          |

---

## Verdict: Strategy B (Execution Speed Arb) — Conditional Build

All three agents converged on Strategy B as the only viable path. The convergence was unanimous but the confidence range (6.5–7/10) reflects two unresolved empirical unknowns that cannot be answered by reasoning alone.

---

## Strategy Rankings (Final)

### 1. Strategy B — Execution Speed Arb ✓ Build
**Mechanism**: Connect to fast sports data feeds (SportsDataIO + ESPN as redundant validator). When a game-final event occurs and is classified as "terminal and non-reviewable," buy the winning side on Kalshi before the order book reprices. Edge window: 5–30 seconds. Best target: mid-afternoon weekday MLB games.

**Why it wins**: The edge is structural — it comes from data latency in platform architecture, not from outpredicting the market. The market doesn't need to be wrong about probabilities; it just needs to be slow to receive information you already have. Kalshi's $1–2M MLB volume provides enough book depth for $1–5K positions without slippage. Position duration is minutes, capital turnover is high.

**Viable as**: A part-time income stream generating $9,000–$12,000/year net after infrastructure costs, at $2K max position size and Kalshi-only architecture. Not a scalable business at this capital level without expansion.

### 2. Strategy A — Sportsbook Signal + Polymarket Trade ✗ Do Not Build
**Why it loses**: Not arbitrage — it's directional betting with a good signal. Without the sportsbook hedge leg, every trade carries directional risk. Variance dominates edge at $10K–$50K capital. Sportsbook lines are sharp but the edge degrades as Polymarket incorporates the same information. Unanimous rejection across all three rounds.

### 3. Strategy C — Pure Kalshi×Polymarket Cross-Platform ✗ Do Not Build (passive monitor only)
**Why it loses**: Current cross-platform gaps on MLB moneylines are 1–3 cents. Break-even is 3–4 cents. You are systematically below break-even on the most liquid markets. 4+ cent outlier gaps on props: Agent C's final estimate is 2–6 genuinely exploitable opportunities per month (not "several per week" as Agent A initially claimed), at $200–$500 max position before moving the market. Expected value: ~$18/month. Not a strategy.

**What to do**: Build a passive monitor that alerts on 4+ cent cross-platform gaps. Execute manually when an alert fires. Do not automate. Do not allocate capital specifically to this channel.

---

## Consensus Findings

### 1. NFL is off the table for v1

The debate's most important convergence point. NFL replay reviews take 2–5 minutes to resolve. The on-field ruling pushes to data feeds immediately — both feeds will agree on the score and both will be wrong if the play is overturned. The 10–15 second delay mitigation proposed in Round 1 catches nothing. Any execution on an NFL scoring event before review resolution is directional gambling. NFL deferred to v2 pending sport-specific event classification work.

### 2. Kalshi-only v1 is mandatory

Polymarket's CLOB requires USDC on Polygon with pre-signed transactions, gas estimation, and Polygon network reliability — all of which degrade under load precisely during high-volume events when you most want to execute. Polymarket is also REST-only for order books (no WebSocket), adding 1–2 second polling lag vs. Kalshi's real-time WebSocket. Kalshi has larger book depth on sports ($1–2M vs. Polymarket's ~$100–500K on same markets). Add Polymarket only if 30-day live data shows persistent edge and capacity constraints that require a second venue.

### 3. Two-feed cross-validation does not protect against score reversals

Both SportsDataIO and ESPN pull from the same upstream official league feeds. When a scoring play occurs, both feeds push the same on-field ruling simultaneously. Both feeds will "agree" on a score that is later reversed via replay review. Cross-validation catches feed outages and data corruption — not the most dangerous failure mode. Treat this as the score-reversal risk mitigation gap.

### 4. Market selection is the primary latency competition mitigation

- **Prime-time nationally broadcast games**: 5–15 competing bots, 5-second edge window, difficult.
- **Mid-afternoon weekday MLB, low-viewership**: 1–5 participants, 20–30 second edge window. More importantly: MLB scoring events (strikeouts ending games, force outs) are largely non-reviewable once called — better fit for terminal event classification.

---

## Key Disagreements (Unresolved)

### Score reversal detection: how hard is it really?

- **Agent A**: Sport-specific event classification is solvable engineering. The "wait for game over API event, not last pitch event" distinction is buildable. Confidence: the system can correctly classify terminal events.
- **Agent B**: Buildable with explicit review_pending flag checks and 2-second re-check intervals. Agrees the problem is harder than Round 1 assumed but maintains it's solvable.
- **Agent C**: Most skeptical. Notes that official review/challenge flags in game feeds lag the raw score update. Cannot reliably distinguish "terminal" from "terminal but under review" fast enough to act on. Two-feed cross-validation gives false confidence because both feeds show the same incorrect state.

**The gap**: Agent C says this problem is unsolved engineering. Agents A and B say it's solved-but-hard. This cannot be resolved by debate — it requires 30 days of live feed monitoring with historical challenge data to validate.

### Strategy C opportunistic frequency

- **Agent A**: 4+ cent prop gaps "several times per week" — approximately 100–150 opportunities over 90-day season.
- **Agent C**: 2–6 exploitable opportunities per month — approximately 18–54 over 90-day season.

Agent C's estimate is more conservative and more credible given the context (current 1–3 cent moneyline gaps; prop markets not immune to same compression). No empirical data exists to adjudicate. Expected value under Agent C's estimate is ~$18/month — not worth automating.

---

## Recommended Build Spec (Phase 0 → Live)

### Phase 0 — Benchmark before capital (2 weeks, $0 at risk)

Monitor live MLB games. Measure time from score event in SportsDataIO to observable price change in Kalshi order book. Log every game-final event: event timestamp, your detection timestamp, Kalshi order book state at detection, Kalshi order book state 30 seconds later.

**Gate**: Proceed only if median lag from event to Kalshi repricing exceeds your pipeline latency by at least 5 seconds (i.e., you have a real window). If the market reprices before your feed fires, stop — the edge is gone.

### Phase 1 — Build (4 weeks)

| Week | Deliverable |
|------|-------------|
| 1 | SportsDataIO + ESPN integration, dual-feed cross-validator (500ms agreement window), MLB game state parser, event classification for terminal events |
| 2 | Kalshi WebSocket order book integration, paper trading loop — prints "would have bought X at Y, repriced to Z, edge = N cents" |
| 3 | Live execution at $500/trade minimum, logging every trade with latency stages |
| 4 | Analysis of week 3 data, parameter tuning, kill switches, operational hardening |

Infrastructure: AWS us-east-1 VPS ($50–100/month). SportsDataIO ($50–200/month). Single Python or Go process. No database — in-memory state, flat-file logging. Discord/email alert on crash.

### Phase 2 — Scale (after 30 profitable trades with latency data)

- Scale from $500 to $1,000–$2,000 per trade
- Add NBA final-2-minutes (lower review risk than mid-game NFL, but more complex event classification than MLB)
- Evaluate Polymarket addition only if live data shows consistent edge and depth constraints

### Kill Switches (non-negotiable)

| Trigger | Action |
|---------|--------|
| Both feeds don't agree within 500ms | No trade |
| game_state includes any review/challenge flag | Abort, wait 60 seconds |
| Score event less than 15 seconds old (MLB) | Hold, re-evaluate |
| Two consecutive reversal losses | Pause, audit event classification logic |
| Market reprices before your order arrives (fill price >2 cents worse than expected) | Log as "arrived late," do not count as loss but track frequency |
| Any single loss exceeding $1K | Pause, review |

---

## P&L Model (Agent C, 90-Day MLB Season)

**Assumptions**: 50 trading days, 2 tradeable opportunities/day, 100 total executions, $2K max position, game-final events only, Kalshi-only.

| Scenario | Net P&L |
|----------|---------|
| Expected (65% win, 10% adverse) | +$3,450 |
| 90th percentile | +$6,000–$7,000 |
| 10th percentile | +$800–$1,500 |
| 5th percentile (feed outage + reversals) | -$500 to -$2,000 |

Annualized across other sports (if pattern holds): $12,000–$16,000 gross, $9,000–$12,000 net after infrastructure.

**Ceiling**: This is a part-time income stream at $10K–$50K capital, not a scalable business. Scaling beyond $5K/trade requires either larger book depth or multiple venues (which reintroduces Polymarket CLOB complexity).

---

## Biggest Unresolved Risk

**Score reversal detection is unsolved.** The real fix — event classification that provably distinguishes "terminal and non-reviewable" from "terminal but under review" — requires either:
- Integration with official league review APIs (exist for NFL and MLB, require licensing)
- A 30–60 second hold on all scoring events (eliminates the edge window entirely)
- Human oversight on high-risk event types (breaks full automation)

Two-feed cross-validation does not solve this. Agent B's review_pending flag check is a partial mitigation but feed latency means the flag may not be set when the event first appears.

**The practical solution**: start with only MLB final-out-of-game events (strikeout, fly out, groundout recording the final out) where:
- The game is definitively over (no continuation possible)
- No review mechanism applies to the out call itself (umpire's ball/strike decisions are not reviewable in MLB)
- Win probability jumps from ~0.05 to 1.00 in one step

This is the narrowest possible target but also the cleanest. Run 30 days on this event type only, then expand to other event types as the classification system matures.

---

## What Changed From Round 1 to Round 3

| Round 1 Assumption | Round 3 Finding |
|---|---|
| 10–15 second delay mitigates score reversals | Does not work for NFL; partial for MLB; requires sport-specific event classification |
| Two-feed cross-validation protects against bad data | Protects against feed outages only — not score reversals (both feeds push same upstream data) |
| Strategy C viable at 4+ cent prop gaps "several times per week" | 2–6 opportunities/month at $200–$500 position cap; expected value ~$18/month |
| Polymarket is an execution venue option | Near deal-breaker due to CLOB/Polygon operational fragility; Kalshi-only v1 is mandatory |
| NFL is a target alongside MLB | NFL deferred entirely due to 2–5 minute review windows |
| Market selection doesn't matter much | Mid-afternoon weekday MLB is materially different (20–30 second windows, fewer competitors) |

---

## Full Transcript

See [chat.json](chat.json) for complete round-by-round transcript.
