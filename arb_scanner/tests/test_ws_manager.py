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


if __name__ == "__main__":
    unittest.main()
