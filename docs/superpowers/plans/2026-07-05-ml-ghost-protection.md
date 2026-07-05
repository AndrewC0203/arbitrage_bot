# Moneyline Ghost Protection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the ghost-arb protection that props already have (F1–F4 quote-sanity filters + Kalshi REST-confirm gate) to the moneyline path, so a stale leg quote can no longer open a printed/logged ML arb.

**Architecture:** All changes live in `arb_scanner/ws_manager.py`. The four sport matchers stay untouched — `check_arb_moneyline()` already has everything needed: it regenerates `kalshi_markets` (which will gain `yes_bid`) and holds `poly_markets` (which carries both team sides per slug), so a new `_ml_ghost_filter_reason()` evaluates each would-be arb from lookup indexes built per check. New ML arbs additionally require a REST confirmation against Kalshi's single-market endpoint before the tracker opens them, mirroring `_rest_confirm_and_emit` for props.

**Tech Stack:** Python 3.14, asyncio, unittest/pytest (existing test layout: `arb_scanner/tests/test_ws_manager.py`).

## Global Constraints

- Base branch: stack on `claude/fix-book-dust-levels` (contains the cent-grid book fix; PR #17 branch underneath).
- Conventional Commits, subject ≤ 50 chars, one commit per task.
- Reuse existing constants verbatim: `GHOST_PIN_PROB = 0.97`, `GHOST_MID_DISAGREEMENT_MAX = 0.15`, `GHOST_MAX_GAP_CENTS = 10.0`, `GHOST_MAX_SPREAD = 0.20`, `ARB_THRESHOLD = 0.96`, `_KALSHI_CONFIRM_COOLDOWN_SECONDS = 5`, `REST_TIMEOUT = 8`.
- Never write to production `arb_log.jsonl`/`ghost_log.jsonl` from tests — patch `ws_manager.log_event` and `ws_manager._append_ghost_log` in every test that can reach them.
- **Out of scope (explicitly deferred):** the 1-week Phase-3 soak, REST-confirm *demotion*, validating-size logging, any Poly-leg freshness gate, F2 for three-way (soccer) Poly legs. This plan must be shippable and verifiable from unit tests plus a short (~30–60 min) live spot-check only.

## Known asymmetries this plan encodes (do not "fix" them)

1. **ML legs are opposite teams.** Kalshi leg = team A, Poly leg = team B of the same game. Mid-agreement (F2) therefore compares `mid_k` against `1 − mid_p`, NOT `mid_p`. (Props compare same-outcome legs; copying that verbatim is a bug.)
2. **Soccer is three-way.** `KXEPL`/`KXMLS`/`KXCHAMPIONS` games carry draw probability mass, so the two Poly team asks sum to ≈0.75–0.85, and deriving `yes_bid = 1 − opp_ask` produces a falsely crossed book. For those series the Poly leg gets F1 (pin) only; F2/F4 run on the Kalshi leg alone.
3. **Suppression ≠ close-blocking.** Ghost-suppressed matches get `is_arb = False` *before* the tracker runs, so an already-open arb whose quotes degrade into ghost territory closes normally (key drops out of the arb set).

---

### Task 1: Expose `yes_bid` from the Kalshi order book

**Files:**
- Modify: `arb_scanner/ws_manager.py` — `KalshiOrderBook.as_kalshi_markets()` (~line 419)
- Test: `arb_scanner/tests/test_ws_manager.py`

**Interfaces:**
- Produces: every dict returned by `as_kalshi_markets()` gains `"yes_bid": Optional[float]` — best resting YES bid in dollars (e.g. `0.37`), or `None` when the YES side is empty. Task 2 consumes this key.

- [ ] **Step 1: Write the failing test** (append to `TestKalshiOrderBookProps` or a new class in `tests/test_ws_manager.py`)

```python
class TestAsKalshiMarketsYesBid(unittest.TestCase):
    def setUp(self):
        self.book = KalshiOrderBook()
        self.book.seed_from_rest([{"ticker": ML_TICKER, "title": "t", "raw": {}}])

    def test_yes_bid_is_best_yes_level(self):
        snap = _snapshot_msg()  # default yes levels: 0.01 and 0.37
        self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"])
        m = self.book.as_kalshi_markets(datetime.now(timezone.utc))[0]
        self.assertEqual(m["yes_bid"], 0.37)

    def test_yes_bid_none_when_yes_side_empty(self):
        snap = _snapshot_msg(yes=[], no=[["0.5800", "608.52"]])
        self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"])
        m = self.book.as_kalshi_markets(datetime.now(timezone.utc))[0]
        self.assertIsNone(m["yes_bid"])
```

- [ ] **Step 2: Run to verify failure**

Run: `cd arb_scanner && python -m pytest tests/test_ws_manager.py::TestAsKalshiMarketsYesBid -v`
Expected: FAIL with `KeyError: 'yes_bid'`

- [ ] **Step 3: Implement** — in `as_kalshi_markets()`, inside the loop after the `updated` staleness check:

```python
            yes_levels = self._books.get(ticker, {}).get("yes", {})
            yes_bid_c = max((p for p, q in yes_levels.items() if q > 0), default=None)
            result.append({
                "ticker": ticker,
                "title": meta["title"],
                "ask": ask,
                "yes_bid": yes_bid_c / 100.0 if yes_bid_c is not None else None,
                "taker_fee": round(kalshi_taker_fee(ask), 6),
                "raw": meta["raw"],
            })
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_ws_manager.py -v`
Expected: all pass (existing consumers only read keys they know).

- [ ] **Step 5: Commit** — `git commit -m "feat: expose yes_bid in as_kalshi_markets"`

---

### Task 2: `_ml_ghost_filter_reason()` + scoped `GhostFilterStats`

**Files:**
- Modify: `arb_scanner/ws_manager.py` — add function after `_ghost_filter_reason` (~line 716); add `scope` to `GhostFilterStats.__init__` and its summary event; add module-level `_ml_ghost_stats` next to `_ghost_stats` (~line 769)
- Test: `arb_scanner/tests/test_ws_manager.py`

**Interfaces:**
- Consumes: `kalshi_by_ticker: dict[str, dict]` (dicts from Task 1, keyed by ticker) and `poly_ask_by_side: dict[tuple[str, str], float]` keyed by `(slug, team_abbr)`.
- Produces: `_ml_ghost_filter_reason(m, kalshi_by_ticker, poly_ask_by_side) -> Optional[str]` returning one of `"pinned" | "one_sided" | "spread" | "mid_disagreement" | None`; module global `_ml_ghost_stats = GhostFilterStats(scope="moneyline")`. Task 3 consumes both. `GhostFilterStats(scope: str = "props")` — existing props instance keeps default.

- [ ] **Step 1: Write the failing tests**

```python
class TestMlGhostFilterReason(unittest.TestCase):
    """ML legs are OPPOSITE teams: Kalshi leg team A, Poly leg team B.
    F2 compares mid_k vs 1 - mid_p. Soccer series are three-way: Poly leg
    gets F1 only (complement math is invalid with draw mass)."""

    def _m(self, ticker="KXMLBGAME-26JUL051400CWSCLE-CLE", team="cle",
           slug="aec-mlb-cws-cle-2026-07-05", p_team="cws", p_ask=0.645):
        return {"kalshi_ticker": ticker, "kalshi_team": team,
                "polymarket_slug": slug, "polymarket_team": p_team,
                "polymarket_ask": p_ask}

    def test_clean_two_way_pair_passes(self):
        kbt = {"KXMLBGAME-26JUL051400CWSCLE-CLE": {"ask": 0.30, "yes_bid": 0.28}}
        pas = {("aec-mlb-cws-cle-2026-07-05", "cle"): 0.31}   # opp side ask
        self.assertIsNone(wm._ml_ghost_filter_reason(self._m(p_ask=0.68), kbt, pas))

    def test_kalshi_pinned_leg_suppressed(self):
        kbt = {"KXMLBGAME-26JUL051400CWSCLE-CLE": {"ask": 0.02, "yes_bid": 0.01}}
        pas = {("aec-mlb-cws-cle-2026-07-05", "cle"): 0.03}
        self.assertEqual(wm._ml_ghost_filter_reason(self._m(p_ask=0.97), kbt, pas), "pinned")

    def test_mid_disagreement_uses_complement(self):
        # Incident replay (21:47 CWS ghost): Kalshi cws mid 0.635, Poly cle
        # mid 0.2075 -> 1 - mid_p = 0.7925, |diff| = 0.1575 > 0.15
        kbt = {"KXMLBGAME-26JUL051400CWSCLE-CWS": {"ask": 0.64, "yes_bid": 0.63}}
        pas = {("aec-mlb-cws-cle-2026-07-05", "cws"): 0.79}
        m = self._m(ticker="KXMLBGAME-26JUL051400CWSCLE-CWS", team="cws",
                    p_team="cle", p_ask=0.205)
        self.assertEqual(wm._ml_ghost_filter_reason(m, kbt, pas), "mid_disagreement")

    def test_kalshi_wide_spread_suppressed(self):
        kbt = {"KXMLBGAME-26JUL051400CWSCLE-CLE": {"ask": 0.55, "yes_bid": 0.20}}
        pas = {("aec-mlb-cws-cle-2026-07-05", "cle"): 0.40}
        self.assertEqual(wm._ml_ghost_filter_reason(self._m(p_ask=0.40), kbt, pas), "spread")

    def test_missing_kalshi_yes_bid_is_one_sided(self):
        kbt = {"KXMLBGAME-26JUL051400CWSCLE-CLE": {"ask": 0.30, "yes_bid": None}}
        pas = {("aec-mlb-cws-cle-2026-07-05", "cle"): 0.31}
        self.assertEqual(wm._ml_ghost_filter_reason(self._m(p_ask=0.68), kbt, pas), "one_sided")

    def test_soccer_three_way_skips_poly_complement(self):
        # Poly asks sum to 0.80 (draw mass) — two-way math would call this
        # crossed/spread; soccer must pass when the Kalshi leg is healthy.
        kbt = {"KXEPL-26AUG01ARSCHE-ARS": {"ask": 0.45, "yes_bid": 0.43}}
        pas = {("epl-ars-che-2026-08-01", "ars"): 0.42}
        m = self._m(ticker="KXEPL-26AUG01ARSCHE-ARS", team="ars",
                    slug="epl-ars-che-2026-08-01", p_team="che", p_ask=0.38)
        self.assertIsNone(wm._ml_ghost_filter_reason(m, kbt, pas))


class TestGhostStatsScope(unittest.TestCase):
    def test_summary_event_carries_scope(self):
        stats = wm.GhostFilterStats(scope="moneyline")
        stats._last_summary_at = 0.0  # force emission window open
        with patch("ws_manager.log_event") as mock_log:
            stats.maybe_emit_summary()
        self.assertEqual(mock_log.call_args[0][0]["scope"], "moneyline")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_ws_manager.py::TestMlGhostFilterReason tests/test_ws_manager.py::TestGhostStatsScope -v`
Expected: FAIL with `AttributeError: ... has no attribute '_ml_ghost_filter_reason'` / `TypeError: __init__() got an unexpected keyword argument 'scope'`

- [ ] **Step 3: Implement**

In `GhostFilterStats`:

```python
    def __init__(self, scope: str = "props"):
        self.scope = scope
        ...  # existing body unchanged
```

and in `maybe_emit_summary()` add `"scope": self.scope,` to the logged dict.

After `_ghost_filter_reason` add:

```python
# Soccer games carry draw probability mass, so the two Poly team asks do NOT
# sum to ~1 and yes_bid = 1 - opp_ask is invalid — Poly leg gets F1 only.
_THREE_WAY_SERIES = {"KXEPL", "KXMLS", "KXCHAMPIONS"}


def _ml_ghost_filter_reason(m: dict, kalshi_by_ticker: dict,
                            poly_ask_by_side: dict) -> Optional[str]:
    """
    F1/F2/F4 for a moneyline match dict. ML legs are opposite teams —
    Kalshi leg is m["kalshi_team"], Poly leg is m["polymarket_team"] — so
    mid agreement compares mid_k against 1 - mid_p. F3 (edge cap) stays at
    the call site, same split as match_props.
    """
    km = kalshi_by_ticker.get(m["kalshi_ticker"])
    if km is None:
        return "one_sided"
    k_yes_ask, k_yes_bid = km["ask"], km.get("yes_bid")
    pin_ask = round(1.0 - GHOST_PIN_PROB, 4)
    # F1 — either leg priced as effectively resolved
    if k_yes_bid is not None and k_yes_bid >= GHOST_PIN_PROB:
        return "pinned"
    if k_yes_ask <= pin_ask:
        return "pinned"
    p_yes_ask = m["polymarket_ask"]
    if p_yes_ask <= pin_ask:
        return "pinned"
    p_opp_ask = poly_ask_by_side.get((m["polymarket_slug"], m["kalshi_team"]))
    three_way = m["kalshi_ticker"].split("-")[0] in _THREE_WAY_SERIES
    p_yes_bid = None
    if not three_way and p_opp_ask is not None:
        p_yes_bid = round(1.0 - p_opp_ask, 4)
        if p_yes_bid >= GHOST_PIN_PROB:
            return "pinned"
    if k_yes_bid is None:
        return "one_sided"
    # F4 — spread sanity (crossed or canyon-wide book)
    k_spread = round(k_yes_ask - k_yes_bid, 4)
    if k_spread < 0 or k_spread > GHOST_MAX_SPREAD:
        return "spread"
    if p_yes_bid is not None:
        p_spread = round(p_yes_ask - p_yes_bid, 4)
        if p_spread < 0 or p_spread > GHOST_MAX_SPREAD:
            return "spread"
        # F2 — opposite teams: compare mid_k to the complement of mid_p
        mid_k = (k_yes_ask + k_yes_bid) / 2
        mid_p = (p_yes_ask + p_yes_bid) / 2
        if round(abs(mid_k - (1.0 - mid_p)), 4) > GHOST_MID_DISAGREEMENT_MAX:
            return "mid_disagreement"
    return None
```

Next to `_ghost_stats = GhostFilterStats()` add:

```python
_ml_ghost_stats = GhostFilterStats(scope="moneyline")
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/test_ws_manager.py -v` → all pass.

- [ ] **Step 5: Commit** — `git commit -m "feat: add moneyline ghost filter F1/F2/F4"`

---

### Task 3: Gate `check_arb_moneyline()` on the filters (+F3 edge cap)

**Files:**
- Modify: `arb_scanner/ws_manager.py` — `check_arb_moneyline()` (~line 1444), between `matches` computation and `_ml_tracker.process(...)`
- Test: `arb_scanner/tests/test_ws_manager.py`

**Interfaces:**
- Consumes: `_ml_ghost_filter_reason`, `_ml_ghost_stats` (Task 2), `yes_bid` (Task 1).
- Produces: suppressed matches have `is_arb == False` before the tracker runs; `ghost_log.jsonl` records carry `"scope": "moneyline"` and `"direction": "Kalshi YES + Poly YES"`. Task 5 modifies this same block again (confirm gate) — keep the gate as a separate clearly-delimited paragraph of code.

- [ ] **Step 1: Write the failing test**

```python
class TestMoneylineGhostGate(unittest.TestCase):
    """A would-be ML arb failing F1-F4 (or the F3 edge cap) must be flipped
    to is_arb=False before _ml_tracker.process — no opened event, one
    ghost_log record."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._ml_tracker = wm.MLTracker()
        wm._ml_ghost_stats = wm.GhostFilterStats(scope="moneyline")

    def _run_check(self, matches, kalshi_markets, poly_markets):
        with patch.object(wm, "GLOBAL_MATCHERS", [type("M", (), {
                 "match": staticmethod(lambda k, p: matches)})()]), \
             patch.object(wm._order_book, "as_kalshi_markets",
                          lambda now: kalshi_markets), \
             patch("ws_manager.log_event") as mock_log, \
             patch("ws_manager._append_ghost_log") as mock_ghost:
            wm._poly_ml_cache = (poly_markets, wm.utc_now())
            wm.check_arb_moneyline(wm.utc_now())
        return mock_log, mock_ghost

    def _match(self, gap=1.5, **kw):
        m = {"market_name": "X", "kalshi_ticker": "KXMLBGAME-26JUL051400CWSCLE-CLE",
             "kalshi_team": "cle", "kalshi_ask": 0.29, "kalshi_taker_fee": 0.0144,
             "polymarket_slug": "s", "polymarket_team": "cws",
             "polymarket_ask": 0.645, "polymarket_taker_fee": 0.0134,
             "total_cost": 0.945, "gap_cents": gap, "is_arb": True}
        m.update(kw)
        return m

    def test_ghost_arb_suppressed_not_opened(self):
        kalshi = [{"ticker": "KXMLBGAME-26JUL051400CWSCLE-CLE", "title": "X",
                   "ask": 0.29, "yes_bid": None, "taker_fee": 0.0144, "raw": {}}]
        poly = [{"slug": "s", "team_abbr": "cws", "ask": 0.645, "taker_fee": 0.0134,
                 "title": "X", "raw": {}}]
        mock_log, mock_ghost = self._run_check([self._match()], kalshi, poly)
        opened = [c for c in mock_log.call_args_list
                  if c[0][0].get("event") == "opened"]
        self.assertEqual(opened, [])            # one_sided (yes_bid None)
        self.assertEqual(mock_ghost.call_count, 1)
        self.assertEqual(mock_ghost.call_args[0][0]["scope"], "moneyline")

    def test_fat_edge_suppressed_by_edge_cap(self):
        kalshi = [{"ticker": "KXMLBGAME-26JUL051400CWSCLE-CLE", "title": "X",
                   "ask": 0.29, "yes_bid": 0.28, "taker_fee": 0.0144, "raw": {}}]
        poly = [{"slug": "s", "team_abbr": "cws", "ask": 0.50, "taker_fee": 0.0125,
                 "title": "X", "raw": {}},
                {"slug": "s", "team_abbr": "cle", "ask": 0.31, "taker_fee": 0.0,
                 "title": "X", "raw": {}}]
        m = self._match(gap=12.0, polymarket_ask=0.50, total_cost=0.84)
        _, mock_ghost = self._run_check([m], kalshi, poly)
        self.assertEqual(mock_ghost.call_args[0][0]["reason"], "edge_cap")
        self.assertFalse(m["is_arb"])
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_ws_manager.py::TestMoneylineGhostGate -v`
Expected: FAIL — an `opened` event IS logged / no ghost record written.

- [ ] **Step 3: Implement** — in `check_arb_moneyline()`, directly after the `matches` loop and before the `skew` block:

```python
    # Ghost gate (F1/F2/F4 pair-level, F3 edge cap) — mirror of match_props.
    # Suppressed matches flip to is_arb=False so open arbs still close.
    kalshi_by_ticker = {km["ticker"]: km for km in kalshi_markets}
    poly_ask_by_side = {(pm["slug"], pm["team_abbr"]): pm["ask"]
                        for pm in poly_markets}
    for m in matches:
        if not m["is_arb"]:
            continue
        reason = _ml_ghost_filter_reason(m, kalshi_by_ticker, poly_ask_by_side)
        if reason is None and m["gap_cents"] > GHOST_MAX_GAP_CENTS:
            reason = "edge_cap"
        if reason is not None:
            m["is_arb"] = False
            _ml_ghost_stats.record(reason, {
                "event": "ghost_suppressed",
                "timestamp": utc_now(),
                "scope": "moneyline",
                "reason": reason,
                "direction": "Kalshi YES + Poly YES",
                "kalshi_ticker": m["kalshi_ticker"],
                "market_name": m["market_name"],
                "kalshi_team": m.get("kalshi_team"),
                "polymarket_team": m.get("polymarket_team"),
                "kalshi_ask": m["kalshi_ask"],
                "polymarket_ask": m["polymarket_ask"],
                "total_cost": m["total_cost"],
                "gap_cents": m["gap_cents"],
            })
    _ml_ghost_stats.maybe_emit_summary()
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/test_ws_manager.py -v` → all pass. Also run `python -m pytest tests/ -q` and confirm the only failures are the 3 pre-existing `TestTestMode` date-fixture ones.

- [ ] **Step 5: Commit** — `git commit -m "feat: gate moneyline arbs on ghost filters"`

---

### Task 4: `MLTracker.has_match()` / `mark_opened()`

**Files:**
- Modify: `arb_scanner/ws_manager.py` — `MLTracker` class (~line 575)
- Test: `arb_scanner/tests/test_ws_manager.py`

**Interfaces:**
- Produces: `has_match(m: dict) -> bool`; `mark_opened(m: dict, kalshi_fetched_at: str, polymarket_fetched_at: str) -> Optional[dict]` — inserts the match as open and returns the `opened` event dict, or `None` if the key is already open (race with a concurrent tick). Task 5 consumes both. `process()` is intentionally unchanged.

- [ ] **Step 1: Write the failing test**

```python
class TestMlTrackerMarkOpened(unittest.TestCase):
    def setUp(self):
        self.tr = wm.MLTracker()
        self.m = {"market_name": "X", "kalshi_ticker": "T1", "kalshi_team": "a",
                  "kalshi_ask": 0.29, "kalshi_taker_fee": 0.0144,
                  "polymarket_slug": "s1", "polymarket_team": "b",
                  "polymarket_ask": 0.645, "polymarket_taker_fee": 0.0134,
                  "total_cost": 0.945, "gap_cents": 1.5, "is_arb": True}

    def test_mark_opened_returns_opened_event_once(self):
        ev = self.tr.mark_opened(self.m, wm.utc_now(), wm.utc_now())
        self.assertEqual(ev["event"], "opened")
        self.assertTrue(self.tr.has_match(self.m))
        self.assertIsNone(self.tr.mark_opened(self.m, wm.utc_now(), wm.utc_now()))

    def test_process_updates_then_closes_marked_arb(self):
        self.tr.mark_opened(self.m, wm.utc_now(), wm.utc_now())
        evs = self.tr.process([self.m], wm.utc_now(), wm.utc_now(), False)
        self.assertEqual([e["event"] for e in evs], ["updated"])
        evs = self.tr.process([], wm.utc_now(), wm.utc_now(), False)
        self.assertEqual([e["event"] for e in evs], ["closed"])
```

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_ws_manager.py::TestMlTrackerMarkOpened -v` → FAIL `AttributeError: 'MLTracker' object has no attribute 'mark_opened'`

- [ ] **Step 3: Implement** — add to `MLTracker`:

```python
    def has_match(self, m: dict) -> bool:
        return self._make_key(m) in self._open

    def mark_opened(self, m: dict, kalshi_fetched_at: str,
                    polymarket_fetched_at: str) -> Optional[dict]:
        """Insert a REST-confirmed arb as open. Returns the opened event, or
        None when a concurrent tick already opened the key (caller skips)."""
        key = self._make_key(m)
        if key in self._open:
            return None
        now = utc_now()
        opp_id = str(uuid.uuid4())
        self._open[key] = {"opportunity_id": opp_id, "opened_at": now,
                           "last_seen": now, "last_match": m}
        return self._build_event(
            "opened", opp_id, m, now,
            kalshi_fetched_at, polymarket_fetched_at,
            False, duration_seconds=None,
        )
```

- [ ] **Step 4: Run tests** — `python -m pytest tests/test_ws_manager.py -v` → all pass.

- [ ] **Step 5: Commit** — `git commit -m "feat: add MLTracker.mark_opened confirm hook"`

---

### Task 5: REST-confirm gate for new ML arbs

**Files:**
- Modify: `arb_scanner/ws_manager.py` — new `_ml_rest_confirm_and_open()` next to `_rest_confirm_and_emit` (~line 880); rewire the tracker feed in `check_arb_moneyline()`
- Test: `arb_scanner/tests/test_ws_manager.py`

**Interfaces:**
- Consumes: `mark_opened`/`has_match` (Task 4), the ghost gate (Task 3), `_kalshi_ask(market, "yes")`, `KALSHI_BASE`, `REST_TIMEOUT`, `_KALSHI_CONFIRM_COOLDOWN_SECONDS`.
- Produces: new module globals `_ml_confirm_cooldown: dict[tuple, float]`; coroutine `_ml_rest_confirm_and_open(m, kalshi_fetched_at, polymarket_fetched_at)`. `ghost_rejected` events gain `"scope": "moneyline"` for ML rejections.

- [ ] **Step 1: Write the failing tests**

```python
class TestMlRestConfirm(unittest.TestCase):
    def setUp(self):
        wm._ml_tracker = wm.MLTracker()
        wm._ml_confirm_cooldown.clear()
        self.m = {"market_name": "X", "kalshi_ticker": "T1", "kalshi_team": "a",
                  "kalshi_ask": 0.29, "kalshi_taker_fee": 0.0144,
                  "polymarket_slug": "s1", "polymarket_team": "b",
                  "polymarket_ask": 0.645, "polymarket_taker_fee": 0.0134,
                  "total_cost": 0.9628, "gap_cents": 1.5, "is_arb": True}

    def _confirm(self, rest_yes_ask):
        import asyncio
        resp = type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"market": {"yes_ask_dollars": f"{rest_yes_ask:.4f}"}},
        })()
        with patch("ws_manager.requests.get", return_value=resp), \
             patch("ws_manager.log_event") as mock_log:
            asyncio.run(wm._ml_rest_confirm_and_open(
                self.m, wm.utc_now(), wm.utc_now()))
        return mock_log

    def test_stale_ws_quote_rejected_by_rest(self):
        # The 2026-07-05 incident: WS said 0.29, real book said 0.36
        mock_log = self._confirm(rest_yes_ask=0.36)
        events = [c[0][0]["event"] for c in mock_log.call_args_list]
        self.assertIn("ghost_rejected", events)
        self.assertNotIn("opened", events)
        self.assertFalse(wm._ml_tracker.has_match(self.m))

    def test_live_quote_confirms_and_opens_with_rest_price(self):
        mock_log = self._confirm(rest_yes_ask=0.29)
        opened = [c[0][0] for c in mock_log.call_args_list
                  if c[0][0]["event"] == "opened"]
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0]["kalshi_ask"], 0.29)
        self.assertTrue(wm._ml_tracker.has_match(self.m))
```

Note: `_kalshi_ask(market, "yes")` (verified at `ws_manager.py:174-183`) reads
`yes_ask_dollars` first (dollar float string) and falls back to `yes_ask`
(cents, /100) — the stub's `{"yes_ask_dollars": "0.3600"}` shape is correct
as written.

- [ ] **Step 2: Run to verify failure** — `python -m pytest tests/test_ws_manager.py::TestMlRestConfirm -v` → FAIL `AttributeError: ... no attribute '_ml_confirm_cooldown'`

- [ ] **Step 3: Implement the coroutine** (after `_rest_confirm_and_emit`):

```python
# keyed by (kalshi_ticker, polymarket_slug); epoch seconds of last attempt
_ml_confirm_cooldown: dict[tuple, float] = {}


async def _ml_rest_confirm_and_open(m: dict, kalshi_fetched_at: str,
                                    polymarket_fetched_at: str) -> None:
    """
    Background task: REST-confirm a new moneyline arb candidate before the
    tracker opens it (props parity). Must be spawned via asyncio.create_task,
    never awaited inline from the WS handler.
    """
    ticker = m["kalshi_ticker"]
    url = f"{KALSHI_BASE}/markets/{ticker}"
    try:
        loop = asyncio.get_running_loop()

        def _fetch():
            resp = requests.get(url, timeout=REST_TIMEOUT)
            resp.raise_for_status()
            return resp.json()

        data = await loop.run_in_executor(None, _fetch)
        market = data.get("market", data)
        confirmed = _kalshi_ask(market, "yes")

        def _reject(reason, **extra):
            log_event({"event": "ghost_rejected", "timestamp": utc_now(),
                       "scope": "moneyline", "reason": reason,
                       "kalshi_ticker": ticker,
                       "ws_kalshi_ask": m["kalshi_ask"],
                       "rest_kalshi_ask": confirmed,
                       "polymarket_ask": m["polymarket_ask"], **extra})

        if confirmed is None:
            _reject("no_quote_from_rest")
            return
        p_ask = m["polymarket_ask"]
        total = (confirmed + kalshi_taker_fee(confirmed)
                 + p_ask + polymarket_taker_fee(p_ask))
        if total >= ARB_THRESHOLD:
            _reject("threshold_not_met_after_rest", rest_total=round(total, 4))
            return
        gap = round((ARB_THRESHOLD - total) * 100, 2)
        if gap > GHOST_MAX_GAP_CENTS:
            _reject("edge_cap_after_rest", rest_total=round(total, 4),
                    rest_gap_cents=gap)
            return

        cm = dict(m)
        cm["kalshi_ask"] = confirmed
        cm["kalshi_taker_fee"] = round(kalshi_taker_fee(confirmed), 6)
        cm["total_cost"] = round(total, 6)
        cm["gap_cents"] = gap
        ev = _ml_tracker.mark_opened(cm, kalshi_fetched_at, polymarket_fetched_at)
        if ev is None:
            return  # concurrent tick won the race
        log_event(ev)
        print(f"\n[WS] ML ARB [REST-CONFIRMED] {cm['market_name']} — "
              f"K {cm.get('kalshi_team')}={confirmed:.2f} + "
              f"P {cm.get('polymarket_team')}={p_ask:.2f} "
              f"= ${total:.3f} gap={gap:.1f}¢")
    except Exception as e:
        print(f"[WS] ML REST confirm failed for {ticker}: {e}", file=sys.stderr)
```

- [ ] **Step 4: Rewire the tracker feed** — in `check_arb_moneyline()`, replace the single `events = _ml_tracker.process(matches, ...)` call with:

```python
    # New arbs must pass the REST confirm gate before the tracker opens them;
    # already-open keys flow through process() for updated/closed as before.
    tracker_input = []
    now_epoch = time.time()
    for m in matches:
        if m["is_arb"] and not _ml_tracker.has_match(m):
            key = (m["kalshi_ticker"], m["polymarket_slug"])
            if now_epoch - _ml_confirm_cooldown.get(key, 0.0) >= _KALSHI_CONFIRM_COOLDOWN_SECONDS:
                _ml_confirm_cooldown[key] = now_epoch
                try:
                    asyncio.create_task(_ml_rest_confirm_and_open(
                        m, kalshi_updated_at, poly_fetched_at))
                except RuntimeError:
                    pass  # no running loop (startup seed path) — retried next check
            continue
        tracker_input.append(m)
    events = _ml_tracker.process(tracker_input, kalshi_updated_at,
                                 poly_fetched_at, skew_warn)
```

- [ ] **Step 5: Run the full suite**

Run: `python -m pytest tests/ test_scanner.py -q` (from `arb_scanner/`)
Expected: only the 3 pre-existing `TestTestMode` failures. If any test fails because it drove `check_arb_moneyline` and expected an instant `opened` event, update that test to either (a) stub the gate with `patch("ws_manager._ml_rest_confirm_and_open")` and assert the task was spawned, or (b) pre-open via `wm._ml_tracker.mark_opened(match, wm.utc_now(), wm.utc_now())` and assert `updated` flow instead.

- [ ] **Step 6: Commit** — `git commit -m "feat: REST-confirm gate for new moneyline arbs"`

---

### Task 6: Documentation + verification sweep

**Files:**
- Modify: `CLAUDE.md` (Arb Detection §1), `arb_scanner/DECISIONS.md`

**Interfaces:** none — docs only.

- [ ] **Step 1: Update `CLAUDE.md`** — in "Arb Detection", change item 1 to:

```markdown
1. **Moneyline**: Kalshi YES (team A) + Polymarket YES (team B). Arb when `total_cost < 0.96`. Would-be ML arbs must pass the same Layer-1 ghost filters as props (F1 pinned, F2 mid agreement vs the complement leg, F3 edge cap, F4 spread — soccer's Poly leg is F1-only because of draw mass) AND a Kalshi REST confirm before the tracker opens them. Suppressions go to `ghost_log.jsonl` with `scope: "moneyline"`; hourly `ghost_filter_summary` events are emitted per scope.
```

- [ ] **Step 2: Append to `DECISIONS.md`:**

```markdown
## [DATE] ML ghost gate — chose a lookup-based gate inside check_arb_moneyline over threading two-sided fields through the four matchers, because the matcher output already carries the keys (ticker/slug/team) needed to join against as_kalshi_markets + poly cache, keeping the diff in one file. Soccer Poly legs get F1 only (draw mass invalidates yes_bid = 1 - opp_ask); F2 compares mid_k vs 1 - mid_p because ML legs are opposite teams.
```

- [ ] **Step 3: Full verification** — `python -m pytest tests/ test_scanner.py -q` → only the 3 known failures. Then a live spot-check (NOT the week-long soak — explicitly out of scope): run `python ws_manager.py` for ~30–60 min during live games and confirm (a) `ghost_suppressed` records with `scope: "moneyline"` appear in `ghost_log.jsonl`, (b) every `opened` ML event in `arb_log.jsonl` was preceded by a REST confirm (no more instant opens), (c) no `[WS] ML ARB` line shows a gap > 10¢.

- [ ] **Step 4: Commit** — `git commit -m "docs: document moneyline ghost gate"`
