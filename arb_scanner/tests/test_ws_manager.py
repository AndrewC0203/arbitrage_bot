"""
Unit tests for ws_manager WS message handling — built from REAL Kalshi WS v2
messages captured live on 2026-07-01 (see LEARNED_RULES.md rules 17-18).

Kalshi WS v2 format:
  - payload nested under "msg"; top level carries only type/sid/seq
  - books arrive as yes_dollars_fp / no_dollars_fp (dollar-string levels,
    fractional-quantity strings); deltas as price_dollars + delta_fp
  - book levels are resting BIDS per side: best YES ask = 1 - max(NO bids)
  - ticker msgs carry yes_bid_dollars / yes_ask_dollars only; NO ask must be
    derived as 1 - yes_bid

Run: python -m pytest tests/test_ws_manager.py -v
"""

import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import ws_manager as wm
from ws_manager import KalshiOrderBook, KalshiPriceCache

ML_TICKER = "KXMLBGAME-26JUL041335MINNYY-MIN"
PROP_TICKER = "KXMLBHR-26JUL012010CINMIL-MILJCHOURIO11-1"
PROP_TICKER_NBA = "KXNBAPTS-26JUL021935LALBOS-LALLJAMES25-1"


def _snapshot_msg(sid=1, seq=1, ticker=ML_TICKER, yes=None, no=None):
    """Real captured shape: books as [price_dollars, qty_fp] string pairs."""
    return {
        "type": "orderbook_snapshot", "sid": sid, "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "market_id": "a377d0eb",
            "yes_dollars_fp": yes if yes is not None
                else [["0.0100", "44553.00"], ["0.3700", "134.18"]],
            "no_dollars_fp": no if no is not None
                else [["0.0100", "44454.00"], ["0.5000", "10.00"], ["0.5800", "608.52"]],
        },
    }


def _delta_msg(sid=1, seq=2, ticker=ML_TICKER, price="0.5800", delta="-608.52", side="no"):
    return {
        "type": "orderbook_delta", "sid": sid, "seq": seq,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": price, "delta_fp": delta, "side": side,
            "ts": "2026-07-01T22:29:39.596865Z",
        },
    }


def _ticker_msg(ticker=PROP_TICKER, yes_bid="0.2100", yes_ask="0.2200"):
    return {
        "type": "ticker", "sid": 2,
        "msg": {
            "market_ticker": ticker,
            "price_dollars": "0.2200",
            "yes_bid_dollars": yes_bid, "yes_ask_dollars": yes_ask,
            "volume_fp": "2158.40", "ts": 1782945068,
        },
    }


def _cache_entry(ticker=PROP_TICKER, yes_ask=0.99, no_ask=0.99, age_seconds=200):
    now = datetime.now(timezone.utc)
    return {
        "series": "KXMLBHR", "ticker": ticker,
        "player_name": "Jackson Chourio", "player_norm": "jackson chourio",
        "line": 1, "game_dt_utc": now + timedelta(hours=2),
        "yes_ask": yes_ask, "no_ask": no_ask,
        "updated_at": now - timedelta(seconds=age_seconds),
    }


class TestWsEnvelopeOrderbook(unittest.TestCase):
    """Fix A: msg envelope unwrapped, dollars_fp parsed, ask derived from NO bids."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()
        wm._order_book.seed_from_rest([
            {"ticker": ML_TICKER, "title": "Minnesota @ New York Yankees Winner?", "raw": {}},
        ])
        wm._poly_ml_cache = None  # keep check_arb_moneyline a no-op

    def test_snapshot_populates_book_and_derives_ask_from_no_bids(self):
        wm._handle_ws_message(_snapshot_msg())
        # best YES ask = 1 - max(NO bid) = 1 - 0.58 = 0.42 (verified live vs REST)
        self.assertEqual(wm._order_book._best_ask[ML_TICKER], 0.42)

    def test_delta_on_no_side_moves_derived_yes_ask(self):
        wm._handle_ws_message(_snapshot_msg())
        # remove entire 0.58 NO level -> max NO bid becomes 0.50 -> yes ask 0.50
        wm._handle_ws_message(_delta_msg(seq=2, price="0.5800", delta="-608.52", side="no"))
        self.assertEqual(wm._order_book._best_ask[ML_TICKER], 0.50)

    def test_yes_side_delta_does_not_change_derived_ask(self):
        wm._handle_ws_message(_snapshot_msg())
        wm._handle_ws_message(_delta_msg(seq=2, price="0.3800", delta="5.00", side="yes"))
        self.assertEqual(wm._order_book._best_ask[ML_TICKER], 0.42)

    def test_as_kalshi_markets_serves_derived_ask(self):
        wm._handle_ws_message(_snapshot_msg())
        markets = wm._order_book.as_kalshi_markets(datetime.now(timezone.utc))
        self.assertEqual(len(markets), 1)
        self.assertEqual(markets[0]["ask"], 0.42)

    def test_seq_gap_within_sid_signals_resubscribe(self):
        wm._handle_ws_message(_snapshot_msg(seq=1))
        needs_resub = wm._handle_ws_message(_delta_msg(seq=5))  # gap: 1 -> 5
        self.assertTrue(needs_resub)

    def test_seq_streams_are_tracked_per_sid(self):
        # Two chunked subscriptions = two sids with independent seq streams.
        wm._order_book.seed_from_rest([
            {"ticker": "KXMLBGAME-26JUL041610TORSEA-TOR", "title": "Toronto @ Seattle Winner?", "raw": {}},
        ])
        wm._handle_ws_message(_snapshot_msg(sid=1, seq=1))
        wm._handle_ws_message(_snapshot_msg(sid=2, seq=1, ticker="KXMLBGAME-26JUL041610TORSEA-TOR"))
        needs_resub = wm._handle_ws_message(_delta_msg(sid=2, seq=2,
                                                       ticker="KXMLBGAME-26JUL041610TORSEA-TOR",
                                                       price="0.0100", delta="1.00", side="no"))
        self.assertFalse(needs_resub)

    def test_reset_connection_clears_seq_state(self):
        wm._handle_ws_message(_snapshot_msg(seq=7))
        wm._order_book.reset_connection()
        # fresh connection restarts seq at 1; must not be treated as a gap
        needs_resub = wm._handle_ws_message(_snapshot_msg(seq=1))
        self.assertFalse(needs_resub)
        self.assertEqual(wm._order_book._best_ask[ML_TICKER], 0.42)


class TestTickerFrameIgnored(unittest.TestCase):
    """Task 3: props ride orderbook_delta now — a "ticker" frame can only
    arrive from a stale server-side subscription and must be a no-op: no
    cache mutation, no arb check triggered."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()
        wm._price_cache._cache[PROP_TICKER] = _cache_entry()
        wm._poly_ws_props_token_map = {}
        wm._poly_ml_cache = None

    def test_ticker_frame_does_not_mutate_cache_or_trigger_check(self):
        before = dict(wm._price_cache._cache[PROP_TICKER])
        with patch.object(wm, "_run_props_arb_check_from_ws") as props_check:
            wm._handle_ws_message(_ticker_msg())
        self.assertEqual(wm._price_cache._cache[PROP_TICKER], before)
        props_check.assert_not_called()


def _rest_prop(ticker, series="KXMLBHIT", yes=0.9, no=0.2, game_dt=None):
    """Shape returned by fetch_kalshi_props."""
    return {
        "series": series, "ticker": ticker,
        "player_name": "A B", "player_norm": "a b", "line": 1,
        "yes_ask": yes, "no_ask": no,
        "game_dt_utc": game_dt or datetime.now(timezone.utc) + timedelta(hours=2),
    }


def _poly_event(slug="s1", active=True, closed=False, game_start=None, yes="0.50", no="0.55"):
    """Shape returned by the gateway /v1/search props endpoint."""
    gs = game_start or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "title": "MLB Player Props",
        "markets": [{
            "sportsMarketType": "baseball_player_hits",
            "active": active, "closed": closed, "archived": False,
            "gameStartTime": gs,
            "metadata": {"playerName": "A B"},
            "slug": slug, "line": 2,
            "marketSides": [
                {"long": True, "quote": {"value": yes}},
                {"long": False, "quote": {"value": no}},
            ],
        }],
    }


class TestKalshiCacheEviction(unittest.TestCase):
    """Fix D: REST re-seed is authoritative — closed/quoteless markets must not
    linger in the cache serving ghost prices for up to 300s."""

    def _tickers(self, cache):
        return sorted(e["ticker"] for e in cache.as_props_list(datetime.now(timezone.utc)))

    def test_reseed_evicts_markets_missing_from_fetched_series(self):
        cache = KalshiPriceCache()
        cache.seed_from_rest([_rest_prop("K-A"), _rest_prop("K-B")],
                             authoritative_series={"KXMLBHIT"})
        # K-B settled; the next status=open pull no longer returns it
        cache.seed_from_rest([_rest_prop("K-A")], authoritative_series={"KXMLBHIT"})
        self.assertEqual(self._tickers(cache), ["K-A"])

    def test_reseed_keeps_markets_when_series_fetch_failed(self):
        cache = KalshiPriceCache()
        cache.seed_from_rest([_rest_prop("K-A"), _rest_prop("K-B")],
                             authoritative_series={"KXMLBHIT"})
        # whole fetch failed → nothing authoritative → evict nothing
        cache.seed_from_rest([], authoritative_series=set())
        self.assertEqual(self._tickers(cache), ["K-A", "K-B"])

    def test_reseed_overwrites_prices_authoritatively(self):
        cache = KalshiPriceCache()
        cache.seed_from_rest([_rest_prop("K-A", yes=0.9, no=0.2)],
                             authoritative_series={"KXMLBHIT"})
        # REST now says the YES side has no live quote — must not keep stale 0.9
        cache.seed_from_rest([_rest_prop("K-A", yes=None, no=0.25)],
                             authoritative_series={"KXMLBHIT"})
        entry = cache._cache["K-A"]
        self.assertIsNone(entry["yes_ask"])
        self.assertEqual(entry["no_ask"], 0.25)


class TestPolyPropsMapUpdate(unittest.TestCase):
    """Fix D: poly props map update evicts markets that went closed/inactive,
    and the match list filters out entries with a stale updated_at."""

    def setUp(self):
        wm._poly_ws_props_token_map = {}

    def test_active_market_is_stored(self):
        wm._update_poly_props_map([_poly_event(slug="s1")])
        self.assertIn("s1", wm._poly_ws_props_token_map)

    def test_market_gone_closed_is_evicted_from_map(self):
        wm._update_poly_props_map([_poly_event(slug="s1")])
        wm._update_poly_props_map([_poly_event(slug="s1", closed=True)])
        self.assertNotIn("s1", wm._poly_ws_props_token_map)

    def test_stale_poly_entries_excluded_from_match_list(self):
        wm._update_poly_props_map([_poly_event(slug="fresh"), _poly_event(slug="stale")])
        wm._poly_ws_props_token_map["stale"]["updated_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=400)
        )
        # entries must also carry a slug so staleness can be traced
        listed = {p["slug"] for p in wm._reconstruct_poly_props_list()}
        self.assertEqual(listed, {"fresh"})


def _match_inputs(k_yes=0.50, p_no=0.455):
    # Two-sided quotes with sane spreads and agreeing mids — the ghost filters
    # (F1-F4) require both venues' books to look live; these tests target the
    # fee math, not the filters.
    game = datetime.now(timezone.utc)
    p_yes = round(1 - p_no + 0.02, 3)  # 2¢ above the bid implied by no_ask
    kp = [{"series": "KXMLBHIT", "ticker": "K-T1", "player_name": "A B",
           "player_norm": "a b", "line": 2, "yes_ask": k_yes, "no_ask": round(1.05 - k_yes, 3),
           "game_dt_utc": game, "updated_at": game}]
    pp = [{"smt": "baseball_player_hits", "player_norm": "a b", "player_name": "A B",
           "line": 2, "yes_ask": p_yes, "no_ask": p_no,
           "game_start": game.strftime("%Y-%m-%dT%H:%M:%SZ"), "event_title": "X"}]
    return kp, pp


class TestPropsFees(unittest.TestCase):
    """Fix E: props arb math must include the documented 1%-per-leg taker fees
    (CLAUDE.md: total_cost = 1.01 x (kalshi_ask + poly_ask))."""

    def test_fee_blind_zone_is_not_flagged_as_arb(self):
        # raw 0.955 < 0.96 but fee-inclusive 0.9646 >= 0.96 -> not an arb
        kp, pp = _match_inputs(k_yes=0.50, p_no=0.455)
        self.assertEqual(wm.match_props(kp, pp), [])

    def test_total_cost_includes_fees(self):
        kp, pp = _match_inputs(k_yes=0.50, p_no=0.40)
        arbs = wm.match_props(kp, pp)
        self.assertEqual(len(arbs), 1)
        self.assertAlmostEqual(arbs[0]["total_cost"], round(0.90 * 1.01, 4))

    def test_rest_confirm_applies_fees_too(self):
        # WS and REST agree on 0.50; raw total 0.955 passes the old check but
        # fee-inclusive it must be ghost_rejected, not confirmed.
        import asyncio

        wm._prop_arb_tracker = wm.PropArbTracker()
        arb = {
            "direction": "Kalshi YES + Poly NO", "kalshi_ticker": "KXMLBHIT-TEST-1",
            "leg1": "Kalshi YES", "leg1_ask": 0.50, "leg2": "Poly NO", "leg2_ask": 0.455,
            "event_title": "X", "game_start": "2026-07-01T23:00:00Z",
            "player_name": "A B", "stat_type": "hits", "line": 2,
            "total_cost": 0.955, "gap_cents": 0.5, "poly_smt": "baseball_player_hits",
            "poly_ws_yes_ask": None, "poly_ws_no_ask": 0.455,
        }
        logged = []

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return {"market": {"yes_ask_dollars": "0.5000"}}

        with patch.object(wm, "log_event", logged.append), \
             patch.object(wm.requests, "get", lambda *a, **k: _Resp()):
            asyncio.run(wm._rest_confirm_and_emit(arb, "2026-07-01T22:00:00+00:00"))

        self.assertEqual(wm._prop_arb_tracker.active_count(), 0)
        self.assertEqual([e["event"] for e in logged], ["ghost_rejected"])
        self.assertEqual(logged[0]["reason"], "threshold_not_met_after_rest")


def _upd_arb(leg1_ask=0.45):
    total = round((leg1_ask + 0.40) * 1.01, 4)
    return {"kalshi_ticker": "K-UPD-1", "direction": "Kalshi YES + Poly NO",
            "leg1": "Kalshi YES", "leg1_ask": leg1_ask, "leg2": "Poly NO",
            "leg2_ask": 0.40, "event_title": "X",
            "game_start": "2026-07-02T02:10:00Z", "player_name": "A B",
            "stat_type": "hits", "line": 2, "total_cost": total,
            "gap_cents": round((0.96 - total) * 100, 2),
            "poly_smt": "baseball_player_hits",
            "poly_ws_yes_ask": 0.62, "poly_ws_no_ask": 0.40}


class TestPropUpdatePrintThrottle(unittest.TestCase):
    """Console [UPD] lines are throttled per arb (WS ticks change prices every
    second — the terminal shouldn't scroll a block per tick). Every update is
    still logged to arb_log.jsonl; only the printing is rate-limited."""

    def setUp(self):
        import io
        self.io = io
        wm._prop_arb_tracker = wm.PropArbTracker()
        wm._prop_update_last_print.clear()
        self.logged = []
        p = patch.object(wm, "log_event", self.logged.append)
        p.start()
        self.addCleanup(p.stop)

    def _emit(self, arb):
        import contextlib
        buf = self.io.StringIO()
        with contextlib.redirect_stdout(buf):
            wm._emit_prop_arbs([arb], "2026-07-02T02:00:00+00:00")
        return buf.getvalue()

    def test_rapid_updates_logged_but_not_printed(self):
        out_open = self._emit(_upd_arb(0.45))
        out_upd = self._emit(_upd_arb(0.44))  # tick 1s later — within throttle
        self.assertIn("[NEW]", out_open)
        self.assertNotIn("[UPD]", out_upd)
        # both the open and the silent update landed in the log
        self.assertEqual([e["event"] for e in self.logged], ["prop_arb", "prop_arb"])

    def test_update_prints_again_after_throttle_window(self):
        self._emit(_upd_arb(0.45))
        key = ("K-UPD-1", "Kalshi YES + Poly NO")
        wm._prop_update_last_print[key] -= wm._PROP_UPDATE_PRINT_SECONDS + 1
        out = self._emit(_upd_arb(0.44))
        self.assertIn("[UPD]", out)

    def test_close_prunes_throttle_state(self):
        self._emit(_upd_arb(0.45))
        self._emit_close = self._emit({"kalshi_ticker": "OTHER", "direction": "x",
                                       **{k: v for k, v in _upd_arb().items()
                                          if k not in ("kalshi_ticker", "direction")}})
        wm._emit_prop_arbs([], "2026-07-02T02:01:00+00:00")  # closes everything
        self.assertEqual(wm._prop_update_last_print, {})


class TestDoubleheaderMatching(unittest.TestCase):
    """Fix F: same player+line+date can occur twice (doubleheader) — the match
    must pair markets from the same game (±30 min), like the ML matcher does."""

    @staticmethod
    def _kp(hour=17):
        return [{"series": "KXMLBHIT", "ticker": "K-G1", "player_name": "A B",
                 "player_norm": "a b", "line": 2, "yes_ask": 0.50, "no_ask": 0.55,
                 "game_dt_utc": datetime(2026, 7, 1, hour, 10, tzinfo=timezone.utc)}]

    @staticmethod
    def _pp(game_start, no_ask):
        # Two-sided (ghost filters require both venues' mids computable)
        return {"smt": "baseball_player_hits", "player_norm": "a b", "player_name": "A B",
                "line": 2, "yes_ask": round(1 - no_ask + 0.02, 3), "no_ask": no_ask,
                "game_start": game_start, "event_title": "X"}

    def test_matches_same_game_leg_of_doubleheader(self):
        pps = [self._pp("2026-07-01T17:10:00Z", 0.40),   # game 1 — same game
               self._pp("2026-07-01T23:10:00Z", 0.30)]   # game 2 — better price, wrong game
        arbs = wm.match_props(self._kp(hour=17), pps)
        self.assertEqual(len(arbs), 1)
        self.assertEqual(arbs[0]["game_start"], "2026-07-01T17:10:00Z")

    def test_no_match_when_game_times_differ_beyond_30min(self):
        pps = [self._pp("2026-07-01T23:10:00Z", 0.40)]
        arbs = wm.match_props(self._kp(hour=17), pps)
        self.assertEqual(arbs, [])


class TestSnapshotSeqGap(unittest.TestCase):
    """Review F14.3: seq is per-sid across ALL tickers on the stream — a gap
    crossing an orderbook_snapshot means missed deltas for OTHER tickers, so
    the snapshot must signal reconnect instead of silently resetting seq."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()
        wm._order_book.seed_from_rest([
            {"ticker": ML_TICKER, "title": "Minnesota @ New York Yankees Winner?", "raw": {}},
        ])
        wm._poly_ml_cache = None

    def test_seq_gap_at_snapshot_signals_resubscribe(self):
        wm._handle_ws_message(_snapshot_msg(seq=1))
        wm._handle_ws_message(_delta_msg(seq=2))
        needs_resub = wm._handle_ws_message(_snapshot_msg(seq=9))  # 2 -> 9 gap
        self.assertTrue(needs_resub)

    def test_consecutive_snapshots_do_not_gap(self):
        wm._handle_ws_message(_snapshot_msg(seq=1))
        needs_resub = wm._handle_ws_message(_snapshot_msg(seq=2))
        self.assertFalse(needs_resub)

    def test_first_snapshot_after_reset_is_not_a_gap(self):
        wm._handle_ws_message(_snapshot_msg(seq=7))
        wm._order_book.reset_connection()
        self.assertFalse(wm._handle_ws_message(_snapshot_msg(seq=1)))


class TestKalshiReconcile(unittest.TestCase):
    """Review F14.2/F14.4: the REST reconcile must fire the props arb check
    (eviction can close arbs during quiet stretches), and concurrent reconciles
    must be serialized so stale data can't land after fresher data."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()
        wm._poly_ml_cache = None

    def test_reconcile_once_fires_props_arb_check(self):
        import asyncio
        calls = []
        with patch.object(wm, "fetch_kalshi_props", lambda today: ([], set())), \
             patch.object(wm, "_run_props_arb_check_from_ws", lambda: calls.append(1)):
            asyncio.run(wm._kalshi_props_reconcile_once())
        self.assertEqual(calls, [1])

    def test_concurrent_reconciles_are_serialized(self):
        import asyncio
        import time as _time
        windows = []

        def slow_fetch(today):
            start = _time.monotonic()
            _time.sleep(0.05)
            windows.append((start, _time.monotonic()))
            return [], set()

        async def scenario():
            await asyncio.gather(
                wm._kalshi_props_reconcile_once(),
                wm._kalshi_props_reconcile_once(),
            )

        with patch.object(wm, "fetch_kalshi_props", slow_fetch), \
             patch.object(wm, "_run_props_arb_check_from_ws", lambda: None):
            asyncio.run(scenario())

        self.assertEqual(len(windows), 2)
        (s1, e1), (s2, e2) = sorted(windows)
        self.assertGreaterEqual(s2, e1)  # second fetch starts after first finishes


class TestKalshiOrderBookProps(unittest.TestCase):
    """Task 1 (ghost-free arbs Layer 2): prop tickers ride the same
    snapshot/delta channel as ML but must stay invisible to
    as_kalshi_markets(); seq gaps must also be tracked across non-book
    ack frames (rule 19) via note_seq()."""

    def setUp(self):
        self.book = KalshiOrderBook()

    def test_props_snapshot_computes_quotes_from_resting_bids(self):
        self.book.sync_prop_tickers([PROP_TICKER])
        snap = _snapshot_msg(ticker=PROP_TICKER)
        self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"])
        quotes = self.book.prop_quotes()
        # no bid max = 0.58 -> yes_ask = 1 - 0.58 = 0.42; yes bid max = 0.37 -> no_ask = 0.63
        self.assertEqual(quotes[PROP_TICKER]["yes_ask"], 0.42)
        self.assertEqual(quotes[PROP_TICKER]["no_ask"], 0.63)
        self.assertEqual(quotes[PROP_TICKER]["yes_ask_qty"], 608.52)
        self.assertEqual(quotes[PROP_TICKER]["no_ask_qty"], 134.18)

    def test_props_delta_removing_best_no_level_moves_yes_ask(self):
        self.book.sync_prop_tickers([PROP_TICKER])
        snap = _snapshot_msg(ticker=PROP_TICKER)
        self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"])
        delta = _delta_msg(seq=2, ticker=PROP_TICKER, price="0.5800", delta="-608.52", side="no")
        self.book.apply_delta(delta["sid"], delta["seq"], delta["msg"])
        quotes = self.book.prop_quotes()
        # next-best NO level after removing 0.58 is 0.50 -> yes_ask = 1 - 0.50 = 0.50
        self.assertEqual(quotes[PROP_TICKER]["yes_ask"], 0.50)
        self.assertEqual(quotes[PROP_TICKER]["yes_ask_qty"], 10.00)

    def test_seq_gap_on_prop_ticker_signals_resubscribe(self):
        self.book.sync_prop_tickers([PROP_TICKER])
        snap = _snapshot_msg(ticker=PROP_TICKER, seq=1)
        self.assertTrue(self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"]))
        delta = _delta_msg(sid=1, seq=3, ticker=PROP_TICKER)  # gap: 1 -> 3
        self.assertFalse(self.book.apply_delta(delta["sid"], delta["seq"], delta["msg"]))

    def test_note_seq_absorbs_ack_frames_between_book_frames(self):
        self.book.sync_prop_tickers([PROP_TICKER])
        snap = _snapshot_msg(ticker=PROP_TICKER, seq=1)
        self.assertTrue(self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"]))
        self.assertTrue(self.book.note_seq(snap["sid"], 2))  # subscribed/ok ack consumes seq 2
        delta = _delta_msg(sid=1, seq=3, ticker=PROP_TICKER)
        self.assertTrue(self.book.apply_delta(delta["sid"], delta["seq"], delta["msg"]))

    def test_note_seq_detects_gap(self):
        self.book.sync_prop_tickers([PROP_TICKER])
        snap = _snapshot_msg(ticker=PROP_TICKER, seq=1)
        self.assertTrue(self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"]))
        self.assertFalse(self.book.note_seq(snap["sid"], 5))

    def test_prop_ticker_excluded_from_as_kalshi_markets(self):
        self.book.seed_from_rest([{"ticker": ML_TICKER, "title": "t", "raw": {}}])
        self.book.sync_prop_tickers([PROP_TICKER])
        prop_snap = _snapshot_msg(sid=1, seq=1, ticker=PROP_TICKER)
        self.book.apply_snapshot(prop_snap["sid"], prop_snap["seq"], prop_snap["msg"])
        ml_snap = _snapshot_msg(sid=2, seq=1, ticker=ML_TICKER)
        self.book.apply_snapshot(ml_snap["sid"], ml_snap["seq"], ml_snap["msg"])
        markets = self.book.as_kalshi_markets(datetime.now(timezone.utc))
        self.assertEqual([m["ticker"] for m in markets], [ML_TICKER])

    def test_unregistered_prop_ticker_snapshot_is_silently_skipped(self):
        # Not passed to sync_prop_tickers and not seeded as ML -> ignored.
        snap = _snapshot_msg(ticker=PROP_TICKER_NBA)
        result = self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"])
        self.assertTrue(result)  # not a gap, just skipped
        self.assertNotIn(PROP_TICKER_NBA, self.book.prop_quotes())

    def test_sync_prop_tickers_evicts_dropped_ticker_state(self):
        self.book.sync_prop_tickers([PROP_TICKER])
        snap = _snapshot_msg(ticker=PROP_TICKER)
        self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"])
        self.book.sync_prop_tickers([])
        self.assertEqual(self.book.prop_quotes(), {})
        self.assertNotIn(PROP_TICKER, self.book._books)
        self.assertNotIn(PROP_TICKER, self.book._best_ask)
        self.assertNotIn(PROP_TICKER, self.book._updated_at)

    def test_reset_connection_clears_prop_book_state(self):
        # Props have no _CACHE_STALE_SECONDS backstop, so a retained book from
        # before a disconnect would serve pre-gap quotes until fresh deltas
        # arrive — reset_connection() must drop it so props go dark instead.
        self.book.sync_prop_tickers([PROP_TICKER])
        snap = _snapshot_msg(ticker=PROP_TICKER)
        self.book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"])
        self.assertIn(PROP_TICKER, self.book.prop_quotes())
        self.book.reset_connection()
        self.assertNotIn(PROP_TICKER, self.book.prop_quotes())
        self.assertNotIn(PROP_TICKER, self.book._books)
        self.assertNotIn(PROP_TICKER, self.book._best_ask)
        self.assertNotIn(PROP_TICKER, self.book._updated_at)


class TestWsMessageRoutesPropsToBookChannel(unittest.TestCase):
    """Task 3 (ghost-free arbs Layer 2): props now ride orderbook_snapshot/
    orderbook_delta — route by series prefix (SERIES_TO_SMT) to the props
    check instead of the moneyline check, and ack frames (subscribed/ok/
    error) must consume seq on the shared sid (rule 19) so they don't cause
    a phantom gap on the next book frame."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()
        wm._order_book.seed_from_rest([
            {"ticker": ML_TICKER, "title": "Minnesota @ New York Yankees Winner?", "raw": {}},
        ])
        wm._order_book.sync_prop_tickers([PROP_TICKER])
        wm._poly_ml_cache = None

    def test_prop_snapshot_triggers_props_check_not_moneyline(self):
        with patch.object(wm, "_run_props_arb_check_from_ws") as props_check, \
             patch.object(wm, "check_arb_moneyline") as ml_check:
            wm._handle_ws_message(_snapshot_msg(ticker=PROP_TICKER))
        props_check.assert_called_once()
        ml_check.assert_not_called()

    def test_ml_snapshot_triggers_moneyline_check_not_props(self):
        with patch.object(wm, "_run_props_arb_check_from_ws") as props_check, \
             patch.object(wm, "check_arb_moneyline") as ml_check:
            wm._handle_ws_message(_snapshot_msg(ticker=ML_TICKER))
        ml_check.assert_called_once()
        props_check.assert_not_called()

    def test_prop_delta_seq_gap_signals_resubscribe(self):
        wm._handle_ws_message(_snapshot_msg(ticker=PROP_TICKER, seq=1))
        needs_resub = wm._handle_ws_message(_delta_msg(ticker=PROP_TICKER, seq=5))  # gap: 1 -> 5
        self.assertTrue(needs_resub)

    def test_ack_frame_consumes_seq_no_phantom_gap(self):
        self.assertFalse(wm._handle_ws_message(_snapshot_msg(ticker=PROP_TICKER, seq=1)))
        self.assertFalse(wm._handle_ws_message({"type": "subscribed", "sid": 1, "seq": 2}))
        self.assertFalse(wm._handle_ws_message(_delta_msg(ticker=PROP_TICKER, seq=3)))

    def test_ok_ack_frame_consumes_seq_no_phantom_gap(self):
        # Rule 19: BOTH ack types (subscribed AND ok) consume seq on the sid.
        self.assertFalse(wm._handle_ws_message(_snapshot_msg(ticker=PROP_TICKER, seq=1)))
        self.assertFalse(wm._handle_ws_message({"type": "ok", "sid": 1, "seq": 2}))
        self.assertFalse(wm._handle_ws_message(_delta_msg(ticker=PROP_TICKER, seq=3)))

    def test_ack_frame_gap_signals_resubscribe(self):
        self.assertFalse(wm._handle_ws_message(_snapshot_msg(ticker=PROP_TICKER, seq=1)))
        needs_resub = wm._handle_ws_message({"type": "subscribed", "sid": 1, "seq": 5})  # gap: 1 -> 5
        self.assertTrue(needs_resub)

    def test_prop_delta_changing_best_no_bid_fires_props_check(self):
        # Snapshot's best NO bid is 0.58 (qty 608.52); removing it moves the
        # best NO bid to 0.50 — top-of-book changes, so the sweep must fire.
        wm._handle_ws_message(_snapshot_msg(ticker=PROP_TICKER, seq=1))
        with patch.object(wm, "_run_props_arb_check_from_ws") as props_check:
            wm._handle_ws_message(
                _delta_msg(ticker=PROP_TICKER, seq=2, price="0.5800", delta="-608.52", side="no")
            )
        props_check.assert_called_once()

    def test_prop_delta_on_non_best_deep_level_does_not_fire_props_check(self):
        # Snapshot's NO side has a deep level at 0.50 (qty 10.00) below the
        # best at 0.58 — removing the deep level doesn't move the top of
        # book, so the sweep must NOT fire.
        wm._handle_ws_message(_snapshot_msg(ticker=PROP_TICKER, seq=1))
        with patch.object(wm, "_run_props_arb_check_from_ws") as props_check:
            wm._handle_ws_message(
                _delta_msg(ticker=PROP_TICKER, seq=2, price="0.5000", delta="-10.00", side="no")
            )
        props_check.assert_not_called()

    def test_prop_first_snapshot_fires_props_check(self):
        with patch.object(wm, "_run_props_arb_check_from_ws") as props_check:
            wm._handle_ws_message(_snapshot_msg(ticker=PROP_TICKER, seq=1))
        props_check.assert_called_once()


class TestKalshiPropsFromBook(unittest.TestCase):
    """Task 2 (ghost-free arbs Layer 2): the props arb path reads price/qty
    from the order book and only metadata from KalshiPriceCache — no
    _CACHE_STALE_SECONDS/updated_at filtering here, since book freshness is
    guaranteed by seq-gap reconnects."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()

    def _seed_book(self, ticker=PROP_TICKER):
        wm._order_book.sync_prop_tickers([ticker])
        snap = _snapshot_msg(ticker=ticker)
        wm._order_book.apply_snapshot(snap["sid"], snap["seq"], snap["msg"])

    def test_builder_merges_cache_metadata_and_book_quote(self):
        wm._price_cache._cache[PROP_TICKER] = _cache_entry()
        self._seed_book()
        result = wm._kalshi_props_from_book(datetime.now(timezone.utc))
        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["ticker"], PROP_TICKER)
        self.assertEqual(entry["player_name"], "Jackson Chourio")
        self.assertEqual(entry["player_norm"], "jackson chourio")
        self.assertEqual(entry["series"], "KXMLBHR")
        self.assertEqual(entry["line"], 1)
        # book-derived ask/qty (verified against TestKalshiOrderBookProps)
        self.assertEqual(entry["yes_ask"], 0.42)
        self.assertEqual(entry["no_ask"], 0.63)
        self.assertEqual(entry["yes_ask_qty"], 608.52)
        self.assertEqual(entry["no_ask_qty"], 134.18)

    def test_no_book_quote_excludes_cache_only_entry(self):
        wm._price_cache._cache[PROP_TICKER] = _cache_entry()
        result = wm._kalshi_props_from_book(datetime.now(timezone.utc))
        self.assertEqual(result, [])

    def test_no_cache_metadata_excludes_book_only_quote(self):
        self._seed_book()
        result = wm._kalshi_props_from_book(datetime.now(timezone.utc))
        self.assertEqual(result, [])

    def test_game_started_over_4h_ago_excluded(self):
        entry = _cache_entry()
        entry["game_dt_utc"] = datetime.now(timezone.utc) - timedelta(hours=5)
        wm._price_cache._cache[PROP_TICKER] = entry
        self._seed_book()
        result = wm._kalshi_props_from_book(datetime.now(timezone.utc))
        self.assertEqual(result, [])

    def test_stale_cache_updated_at_does_not_exclude_when_book_is_fresh(self):
        # 20 minutes old cache updated_at (> _CACHE_STALE_SECONDS = 300) but
        # the book quote is live — proves the arb path no longer depends on
        # cache freshness.
        wm._price_cache._cache[PROP_TICKER] = _cache_entry(age_seconds=1200)
        self._seed_book()
        result = wm._kalshi_props_from_book(datetime.now(timezone.utc))
        self.assertEqual(len(result), 1)


class TestMatchPropsQty(unittest.TestCase):
    """Task 2: match_props surfaces the book qty behind the Kalshi leg of
    each direction as kalshi_qty_at_best; None when kalshi_props dicts lack
    qty keys (existing tests, REST-shaped dicts)."""

    def test_kalshi_qty_at_best_matches_kalshi_leg_side(self):
        kp, pp = _match_inputs(k_yes=0.50, p_no=0.40)
        kp[0]["yes_ask_qty"] = 12.5
        kp[0]["no_ask_qty"] = 30.0
        arbs = wm.match_props(kp, pp)
        self.assertEqual(len(arbs), 1)
        self.assertEqual(arbs[0]["leg1"], "Kalshi YES")
        self.assertEqual(arbs[0]["kalshi_qty_at_best"], 12.5)

    def test_kalshi_qty_at_best_none_when_qty_keys_absent(self):
        kp, pp = _match_inputs(k_yes=0.50, p_no=0.40)
        arbs = wm.match_props(kp, pp)
        self.assertEqual(len(arbs), 1)
        self.assertIsNone(arbs[0]["kalshi_qty_at_best"])


if __name__ == "__main__":
    unittest.main()
