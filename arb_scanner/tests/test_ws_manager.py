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


class TestWsTickerPriceCache(unittest.TestCase):
    """Fix B: ticker msgs parsed from msg envelope with *_dollars fields;
    no_ask derived as 1 - yes_bid; updated_at only bumped on real payload."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()
        wm._price_cache._cache[PROP_TICKER] = _cache_entry()
        wm._poly_ws_props_token_map = {}
        wm._poly_ml_cache = None

    def test_ticker_message_updates_yes_ask(self):
        wm._handle_ws_message(_ticker_msg(yes_ask="0.2200"))
        self.assertEqual(wm._price_cache._cache[PROP_TICKER]["yes_ask"], 0.22)

    def test_ticker_message_derives_no_ask_from_yes_bid(self):
        wm._handle_ws_message(_ticker_msg(yes_bid="0.2100"))
        self.assertAlmostEqual(wm._price_cache._cache[PROP_TICKER]["no_ask"], 0.79)

    def test_zero_yes_bid_clears_no_ask_instead_of_keeping_stale(self):
        wm._handle_ws_message(_ticker_msg(yes_bid="0.0000"))
        self.assertIsNone(wm._price_cache._cache[PROP_TICKER]["no_ask"])

    def test_priceless_message_does_not_refresh_staleness_clock(self):
        before = wm._price_cache._cache[PROP_TICKER]["updated_at"]
        wm._price_cache.update_from_ws(PROP_TICKER, {"volume_fp": "1.00"})
        self.assertEqual(wm._price_cache._cache[PROP_TICKER]["updated_at"], before)

    def test_real_ticker_message_refreshes_staleness_clock(self):
        before = wm._price_cache._cache[PROP_TICKER]["updated_at"]
        wm._handle_ws_message(_ticker_msg())
        self.assertGreater(wm._price_cache._cache[PROP_TICKER]["updated_at"], before)


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
    game = datetime.now(timezone.utc)
    kp = [{"series": "KXMLBHIT", "ticker": "K-T1", "player_name": "A B",
           "player_norm": "a b", "line": 2, "yes_ask": k_yes, "no_ask": None,
           "game_dt_utc": game, "updated_at": game}]
    pp = [{"smt": "baseball_player_hits", "player_norm": "a b", "player_name": "A B",
           "line": 2, "yes_ask": None, "no_ask": p_no,
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
            "suspicious": False, "poly_ws_yes_ask": None, "poly_ws_no_ask": 0.455,
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


class TestDoubleheaderMatching(unittest.TestCase):
    """Fix F: same player+line+date can occur twice (doubleheader) — the match
    must pair markets from the same game (±30 min), like the ML matcher does."""

    @staticmethod
    def _kp(hour=17):
        return [{"series": "KXMLBHIT", "ticker": "K-G1", "player_name": "A B",
                 "player_norm": "a b", "line": 2, "yes_ask": 0.50, "no_ask": None,
                 "game_dt_utc": datetime(2026, 7, 1, hour, 10, tzinfo=timezone.utc)}]

    @staticmethod
    def _pp(game_start, no_ask):
        return {"smt": "baseball_player_hits", "player_norm": "a b", "player_name": "A B",
                "line": 2, "yes_ask": None, "no_ask": no_ask,
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


class TestKalshiTickTriggersPropsCheck(unittest.TestCase):
    """Fix C: a Kalshi-side price change must fire the props arb check
    immediately — not wait up to 30s for the next Poly WS (CDN) update."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()
        wm._poly_ml_cache = None

    def test_ticker_price_change_triggers_props_arb_check(self):
        wm._price_cache._cache[PROP_TICKER] = _cache_entry()
        calls = []
        with patch.object(wm, "_run_props_arb_check_from_ws", lambda: calls.append(1)):
            wm._handle_ws_message(_ticker_msg())
        self.assertEqual(len(calls), 1)

    def test_ticker_for_unknown_market_does_not_trigger_check(self):
        calls = []
        with patch.object(wm, "_run_props_arb_check_from_ws", lambda: calls.append(1)):
            wm._handle_ws_message(_ticker_msg(ticker="KXMLBHIT-UNKNOWN-1"))
        self.assertEqual(calls, [])


class TestExplicitNullClearsPrice(unittest.TestCase):
    """Review F14.1: an explicit JSON null in yes_ask_dollars / yes_bid_dollars
    means "no live quote" and must clear the side — only a MISSING key leaves
    the cached price untouched."""

    def setUp(self):
        wm._order_book = KalshiOrderBook()
        wm._price_cache = KalshiPriceCache()
        wm._price_cache._cache[PROP_TICKER] = _cache_entry(yes_ask=0.99, no_ask=0.79)
        wm._poly_ml_cache = None

    def test_explicit_null_yes_ask_clears_stale_price(self):
        msg = {"type": "ticker", "sid": 2,
               "msg": {"market_ticker": PROP_TICKER,
                       "yes_ask_dollars": None, "yes_bid_dollars": "0.2100"}}
        wm._handle_ws_message(msg)
        self.assertIsNone(wm._price_cache._cache[PROP_TICKER]["yes_ask"])

    def test_explicit_null_yes_bid_clears_no_ask(self):
        msg = {"type": "ticker", "sid": 2,
               "msg": {"market_ticker": PROP_TICKER,
                       "yes_ask_dollars": "0.2200", "yes_bid_dollars": None}}
        wm._handle_ws_message(msg)
        self.assertIsNone(wm._price_cache._cache[PROP_TICKER]["no_ask"])

    def test_missing_keys_leave_prices_untouched(self):
        wm._price_cache.update_from_ws(PROP_TICKER, {"volume_fp": "1.00"})
        entry = wm._price_cache._cache[PROP_TICKER]
        self.assertEqual(entry["yes_ask"], 0.99)
        self.assertEqual(entry["no_ask"], 0.79)


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


if __name__ == "__main__":
    unittest.main()
