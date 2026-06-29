# Stochastic Multi-Agent Consensus Report — Alternative Arb Approaches

**Problem**: What other arbitrage strategies exist beyond Kalshi×Polymarket cross-platform arb?
**Agents**: 10 (framings: neutral, risk-averse, growth, contrarian, first-principles, resource-constrained, long-term, data-driven, systems-thinker, practitioner)
**Date**: 2026-06-25

---

## Aggregated Feasibility Scores (averaged across all agents that mentioned each strategy)

| Strategy | Agents Mentioned | Avg Feasibility | Consensus Level |
|---|---|---|---|
| **YES+NO Intra-Platform Arb** | 10/10 | 8.0 | Strong consensus |
| **Resolution Arbitrage** | 10/10 | 7.4 | Strong consensus |
| **Cross-Platform Same-Event** (current) | 10/10 | 7.2 | Strong consensus |
| **Correlated Contract Pairs** | 10/10 | 5.7 | Moderate consensus |
| **Multi-Outcome Market Sum** | 8/10 | 5.4 | Moderate consensus |
| **PredictIt Cross-Platform** | 10/10 | 5.0 | Moderate consensus |
| **Sports Book vs. PM** | 9/10 | 4.1 | Divergence |
| **Calendar Spread / Expiry Mismatch** | 8/10 | 3.3 | Low feasibility |
| **Manifold/Metaculus Signal** | 9/10 | 2.8 | Consensus: not true arb |
| **Futures/Options Basis Trade** | 5/10 | 4.0 | Outlier |

---

## Strategy Deep-Dives

### 1. YES+NO Intra-Platform Arb
**Consensus: 10/10 agents | Avg feasibility: 8.0 | HIGHEST RATED**

**Mechanism**: On any binary contract, YES + NO must sum to $1.00 (minus fees). When one side gets a large one-sided order or stale quote, the sum drifts above or below $1.00. Buy both sides when sum < $1.00 minus fees — guaranteed profit at resolution regardless of outcome. No cross-platform risk, no matching problem.

**Why it's the top pick**:
- Single platform, single account — no coordination risk
- Outcome is mathematically guaranteed (not prediction-dependent)
- Uses the same API infrastructure you're already building
- No market matching problem — YES and NO are listed together

**Key disagreement**: Agents split on execution window size
- Pessimistic (4/10): "Windows close in seconds, bots already run this"
- Optimistic (6/10): "Prediction markets are thin — windows persist longer than in crypto/equities"

**Biggest obstacle (consensus)**: Atomic dual-leg execution. You need both sides to fill before the market maker re-quotes. REST API round-trips create a gap; WebSocket + parallel async submission is the right architecture.

**Fee note**: Kalshi fees apply to both sides, so you need the combined YES ask + NO ask to be below $1.00 minus ~$0.03 per 100 contracts. Still very achievable on illiquid markets.

---

### 2. Resolution Arbitrage
**Consensus: 10/10 agents | Avg feasibility: 7.4 | SECOND HIGHEST**

**Mechanism**: An event has clearly resolved in the real world (election called, game ended, Fed decision announced, economic data released) but the prediction market contract hasn't officially settled yet. The market often still shows 85–97 cents instead of 100. You buy the winning side and hold 1–48 hours to payout.

**Why agents love this**:
- No counterparty prediction required — you just need to know the outcome faster than the market updates
- No cross-platform coordination
- Structural edge: retail participants don't want capital locked waiting for settlement
- Kalshi resolves faster and more deterministically than Polymarket's oracle system

**Critical distinction from the contrarian agent**: This is NOT true arbitrage — it carries tail risk. If the outcome is disputed (election recount, Fed statement ambiguity, disputed sports ruling), your "near-certain" 95-cent position can go to zero. Most agents flagged this as "high Sharpe, not risk-free."

**Biggest obstacle (consensus)**: Speed of information. You need machine-readable data feeds that precede market repricing:
- **Elections**: AP Elections API (official calls)
- **Fed decisions**: Federal Reserve press release JSON feed
- **Economic data**: BLS, BEA, FRED real-time endpoints
- **Sports**: Official sports data APIs (ESPN, SportRadar)

The contrarian agent (Agent 4) specifically called out `federalregister.gov`, `congress.gov`, and BLS JSON endpoints as providing a 30–120 second lead over market repricing. This is the most actionable specific insight in the entire run.

---

### 3. Correlated Contract Pairs (Intra-Platform)
**Consensus: 10/10 agents | Avg feasibility: 5.7 | UNDEREXPLOITED**

**Mechanism**: Two contracts on the same platform share a logical dependency that must be respected. Examples:
- **Superset/subset**: "Fed raises rates in March" ≤ "Fed raises rates in Q1" — March is a subset of Q1
- **Nested events**: "Candidate A wins state X" ≤ "Candidate A wins presidency"
- **Ordered outcomes**: "Unemployment > 4.0%" ≥ "Unemployment > 4.5%"
- **Impossible orderings**: If P(March hike) > P(Q1 hike), that's mathematically impossible — a guaranteed trade

When these constraints are violated, buy the underpriced contract, sell the overpriced one. No directional risk if the constraint is airtight.

**The solo dev advantage (systems-thinker agent)**: Pure latency bots can't exploit this — it requires semantic understanding of contract language. Institutions don't care — volumes are too small. This is the one strategy where a smart solo developer has a genuine structural moat.

**Biggest obstacle (consensus)**: Building the logical dependency graph across hundreds of markets. Contract naming is inconsistent; requires NLP or manual curation to identify which pairs have hard constraints vs. soft correlations.

---

### 4. Multi-Outcome Market Sum Arb
**Consensus: 8/10 agents | Avg feasibility: 5.4**

**Mechanism**: In a mutually exclusive, exhaustive set (election with 3+ candidates, award with 5+ nominees), all YES prices must sum to exactly $1.00. When they sum below $1.00, buy all outcomes — guaranteed $1.00 payout. When they sum above $1.00, sell all.

**Biggest obstacle (consensus)**: Simultaneous multi-leg execution. By the time you fill legs 1-3, leg 4 may have moved. Thin books on minor outcomes (the "field" or "other" option) make this especially hard. Transaction fees scale with N legs.

---

### 5. PredictIt Cross-Platform
**Consensus: 10/10 agents | Avg feasibility: 5.0 | STRUCTURAL LIMITS**

**Mechanism**: PredictIt is CFTC-regulated with a 10% profit fee + 5% withdrawal fee, creating structural underpricing of YES contracts (retail traders demand a discount for fees). Kalshi and Polymarket price the same political events without this distortion — often creating 3–8 cent gaps.

**Why agents are lukewarm**:
- **$850 per-contract position cap** — max profit per trade is ~$8–16 net of fees, making this a $1K/month ceiling, not a $10K/month ceiling
- **No official API** — must scrape web interface, which is fragile and ToS-violating
- **10% profit fee** requires ~10+ cent raw spreads just to break even
- **Withdrawal delays** (days) kill capital efficiency
- **Regulatory uncertainty** — PredictIt has been in regulatory limbo; platform could close and freeze funds

**Verdict**: Worth monitoring as a supplementary signal, not worth automating unless the spread is >10 cents.

---

### 6. Sports Book vs. Prediction Market
**Consensus: 9/10 agents | Avg feasibility: 4.1 | DIVERGENCE**

Agents split on this one:
- **Optimistic (3/10)**: Sharp sportsbook lines (Pinnacle, Circa) are more efficient than Kalshi sports markets — you can use sportsbook implied probability as a signal to trade the cheaper Kalshi side
- **Pessimistic (6/10)**: Sportsbooks actively ban arbitrageurs. Within weeks your accounts are limited to $20 max bets. The strategy destroys itself.

**Verdict**: Use sharp sportsbook lines as a *signal* for one-sided Kalshi trades (not delta-neutral arb). Don't try to build a true hedge across venues — the sportsbook side will be shut down.

---

### 7. Futures/Options Basis Trade (Outlier)
**Mentioned by: 5/10 agents | Avg feasibility: 4.0 | INSTITUTIONAL BARRIER**

**Mechanism**: Kalshi offers contracts on Fed Funds rate, CPI, GDP, S&P 500 level — the same underlying as CME futures and options. When Kalshi's implied probability diverges from the risk-neutral probability embedded in the futures price, you hedge across markets.

**Why it's hard**: Requires futures account, margin, and derivatives pricing knowledge. CME contract sizes are enormous relative to Kalshi. The synthetic binary construction from options requires multiple legs, adding complexity and slippage. The edge is real but the barrier to entry is institutional.

**Who flagged it**: Growth-oriented and first-principles agents. Risk-averse agents explicitly said to skip it.

---

### What Nobody Recommended

**Manifold/Metaculus as true arb** — all 9 agents that mentioned it explicitly called it a directional signal, not arbitrage. No guaranteed convergence mechanism. Consensus: use it at best as a supplementary signal, never as a primary trade trigger.

**Calendar spreads / expiry mismatch** — 8/10 agents mentioned it; all rated it 2–4. Reason: prediction markets rarely list true calendar spreads on the same event; when they do, thin books prevent execution at meaningful size.

---

## Priority Stack for a Solo Developer

This is where 9/10 agents agreed on a sequencing recommendation:

**Tier 1 — Build now, same infrastructure**
1. YES+NO intra-platform arb (Kalshi only, single account, mechanical)
2. Resolution arb (add data feed monitoring on top of existing scanner)

**Tier 2 — Layer in after Tier 1 is profitable**
3. Cross-platform Kalshi×Polymarket (current plan — already underway)
4. Correlated contract pairs (requires dependency graph, but highest solo-dev moat)

**Tier 3 — Supplementary, not worth automating**
5. Multi-outcome sum arb (execute manually when spotted; too hard to automate multi-leg atomically)
6. PredictIt monitoring (signal only; don't automate unless spreads >10 cents)

**Never**
7. Sportsbook arb (accounts banned too fast)
8. Futures/options basis (institutional barrier)
9. Manifold/Metaculus as primary signal (not arb)

---

## The Most Interesting Outlier Finding

**[Contrarian agent, Agent 4]** — Resolution arb framed not as "buy contracts near expiry" but as a **data pipeline problem**:

> "A bot that monitors `federalregister.gov`, `congress.gov`, and BLS JSON endpoints and triggers immediately on resolution criteria is building a *detection edge*, not a *prediction edge* — which is a fundamentally different and more defensible strategy for a solo developer."

This reframes resolution arb from "being faster than other traders" into "being faster than the market's awareness of official data" — a structural advantage that doesn't require prediction skill and doesn't erode as the market matures, because it's purely about data latency.
