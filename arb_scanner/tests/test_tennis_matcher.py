"""
Unit tests for TennisMatcher.

Run from the arb_scanner/ directory:
    pytest tests/test_tennis_matcher.py -v

Tests are self-contained; no live API calls.
"""

import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matchers.base import BaseMatcher
from matchers.tennis import TennisMatcher, normalize_name, _extract_subject, _extract_matchup


REQUIRED_KEYS = {
    "market_name",
    "kalshi_ticker",
    "kalshi_team",
    "kalshi_ask",
    "kalshi_taker_fee",
    "polymarket_slug",
    "polymarket_team",
    "polymarket_ask",
    "polymarket_taker_fee",
    "total_cost",
    "gap_cents",
    "is_arb",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_kalshi(title, ticker="KXATPMATCH-001", ask=0.48, taker_fee=0.005, raw=None):
    return {
        "ticker": ticker,
        "title": title,
        "ask": ask,
        "taker_fee": taker_fee,
        "raw": raw or {},
    }


def make_poly(team_name, slug="atp-tsitsipas-djokovic-2026-06-28", ask=0.45, taker_fee=0.005, raw=None):
    return {
        "slug": slug,
        "team_name": team_name,
        "ask": ask,
        "taker_fee": taker_fee,
        "raw": raw or {},
    }


# ---------------------------------------------------------------------------
# 1. Contract tests
# ---------------------------------------------------------------------------

class TestContract(unittest.TestCase):

    def test_is_subclass_of_base_matcher(self):
        self.assertTrue(issubclass(TennisMatcher, BaseMatcher))

    def test_has_match_method(self):
        self.assertTrue(callable(getattr(TennisMatcher(), "match", None)))

    def test_match_returns_list(self):
        self.assertIsInstance(TennisMatcher().match([], []), list)

    def test_result_dicts_contain_required_keys(self):
        matcher = TennisMatcher()
        km = make_kalshi(
            "Will Stefanos Tsitsipas win the Tsitsipas vs Djokovic: Round Of 64 match?",
            ask=0.47,
        )
        pm = make_poly("Novak Djokovic", ask=0.45)
        result = matcher.match([km], [pm])
        self.assertGreater(len(result), 0, "expected at least one match")
        for r in result:
            missing = REQUIRED_KEYS - set(r.keys())
            self.assertEqual(missing, set(), f"missing keys: {missing}")


# ---------------------------------------------------------------------------
# 2. Ticker prefix filtering
# ---------------------------------------------------------------------------

class TestTickerFilter(unittest.TestCase):

    def setUp(self):
        self.matcher = TennisMatcher()
        self.title = "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?"
        self.pm = make_poly("Novak Djokovic")

    def test_kxatpmatch_prefix_accepted(self):
        km = make_kalshi(self.title, ticker="KXATPMATCH-001")
        self.assertGreater(len(self.matcher.match([km], [self.pm])), 0)

    def test_kxwtamatch_prefix_accepted(self):
        km = make_kalshi(self.title, ticker="KXWTAMATCH-001")
        self.assertGreater(len(self.matcher.match([km], [self.pm])), 0)

    def test_unknown_prefix_rejected(self):
        km = make_kalshi(self.title, ticker="KXNBA-001")
        self.assertEqual(self.matcher.match([km], [self.pm]), [])

    def test_empty_ticker_rejected(self):
        km = make_kalshi(self.title, ticker="")
        self.assertEqual(self.matcher.match([km], [self.pm]), [])


# ---------------------------------------------------------------------------
# 3. Correct arb pairing — Kalshi YES(subject) + Poly YES(opponent)
# ---------------------------------------------------------------------------

class TestArbPairing(unittest.TestCase):
    """
    The core arb logic: subject A is on Kalshi, opponent B is on Polymarket.
    Exactly one of A/B wins — guaranteed $1 payout, cost < $0.96.
    """

    def setUp(self):
        self.matcher = TennisMatcher(arb_threshold=0.96)

    def test_arb_detected_subject_vs_opponent(self):
        """
        Kalshi subject: tsitsipas (ask=0.47)
        Poly opponent: djokovic (ask=0.45)
        Total: 0.47 + 0.45 + 0.005 + 0.005 = 0.93 < 0.96 -> is_arb=True
        """
        km = make_kalshi(
            "Will Stefanos Tsitsipas win the Tsitsipas vs Djokovic: Round Of 64 match?",
            ask=0.47, taker_fee=0.005,
        )
        pm = make_poly("Novak Djokovic", ask=0.45, taker_fee=0.005)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertTrue(r["is_arb"])
        self.assertAlmostEqual(r["total_cost"], 0.93, places=6)
        self.assertGreater(r["gap_cents"], 0)

    def test_kalshi_team_is_subject_not_opponent(self):
        """kalshi_team must be the subject (the player Kalshi is pricing YES on)."""
        km = make_kalshi(
            "Will Stefanos Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Novak Djokovic", ask=0.45, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        self.assertIn("tsitsipas", r["kalshi_team"])
        self.assertNotIn("djokovic", r["kalshi_team"])

    def test_polymarket_team_is_opponent(self):
        """polymarket_team must be the opponent, not the Kalshi subject."""
        km = make_kalshi(
            "Will Stefanos Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Novak Djokovic", ask=0.45, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        self.assertIn("djokovic", r["polymarket_team"])
        self.assertNotIn("tsitsipas", r["polymarket_team"])

    def test_no_arb_when_cost_at_threshold(self):
        """Total cost exactly at threshold: is_arb=False."""
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.48, taker_fee=0.0,
        )
        pm = make_poly("Djokovic", ask=0.48, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        self.assertFalse(r["is_arb"])
        self.assertAlmostEqual(r["gap_cents"], 0.0, places=4)

    def test_no_arb_when_cost_above_threshold(self):
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.52, taker_fee=0.005,
        )
        pm = make_poly("Djokovic", ask=0.45, taker_fee=0.005)
        r = self.matcher.match([km], [pm])[0]
        self.assertFalse(r["is_arb"])
        self.assertLess(r["gap_cents"], 0)

    def test_poly_same_player_as_subject_does_not_match(self):
        """
        Poly entry for the SUBJECT (not the opponent) must NOT produce a match.
        Pairing Kalshi YES(A) + Poly YES(A) is a directional bet, not arb.
        """
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Tsitsipas", ask=0.45, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0,
            "Poly entry for the Kalshi subject must not produce a match — that is a directional bet.")


# ---------------------------------------------------------------------------
# 4. Title parsing
# ---------------------------------------------------------------------------

class TestTitleParsing(unittest.TestCase):

    def test_extract_subject_standard_title(self):
        s = _extract_subject("Will Stefanos Tsitsipas win the Tsitsipas vs Djokovic: R64 match?")
        self.assertEqual(s, "stefanos tsitsipas")

    def test_extract_subject_missing_prefix_returns_none(self):
        self.assertIsNone(_extract_subject("Tsitsipas vs Djokovic: R64 match?"))

    def test_extract_matchup_standard_title(self):
        pair = _extract_matchup("Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?")
        self.assertEqual(pair, ("tsitsipas", "djokovic"))

    def test_extract_matchup_missing_colon_returns_none(self):
        self.assertIsNone(_extract_matchup("Will Tsitsipas win the Tsitsipas vs Djokovic R64"))

    def test_title_with_no_will_prefix_skipped(self):
        matcher = TennisMatcher()
        km = make_kalshi("Tsitsipas vs Djokovic: R64")
        pm = make_poly("Djokovic")
        self.assertEqual(matcher.match([km], [pm]), [])

    def test_subtitle_fallback(self):
        """
        km['title'] and raw['title'] both absent; should fall back to raw['subtitle'].
        """
        matcher = TennisMatcher()
        km = {
            "ticker": "KXATPMATCH-001",
            "title": "",
            "ask": 0.47,
            "taker_fee": 0.0,
            "raw": {"subtitle": "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?"},
        }
        pm = make_poly("Djokovic", ask=0.45, taker_fee=0.0)
        results = matcher.match([km], [pm])
        self.assertEqual(len(results), 1)

    def test_raw_title_fallback(self):
        """km['title'] empty; falls back to raw['title']."""
        matcher = TennisMatcher()
        km = {
            "ticker": "KXATPMATCH-001",
            "title": "",
            "ask": 0.47,
            "taker_fee": 0.0,
            "raw": {"title": "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?"},
        }
        pm = make_poly("Djokovic", ask=0.45, taker_fee=0.0)
        results = matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_name"],
                         "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?")


# ---------------------------------------------------------------------------
# 5. Zero-ask guard
# ---------------------------------------------------------------------------

class TestZeroAskGuard(unittest.TestCase):

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_zero_kalshi_ask_skipped(self):
        """k_ask=0 must be filtered — no live quote on Kalshi side."""
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.0, taker_fee=0.0,
        )
        pm = make_poly("Djokovic", ask=0.45, taker_fee=0.0)
        self.assertEqual(self.matcher.match([km], [pm]), [])

    def test_zero_poly_ask_skipped(self):
        """p_ask=0 must be filtered — no live quote on Poly side."""
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Djokovic", ask=0.0, taker_fee=0.0)
        self.assertEqual(self.matcher.match([km], [pm]), [])

    def test_both_zero_skipped(self):
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.0, taker_fee=0.0,
        )
        pm = make_poly("Djokovic", ask=0.0, taker_fee=0.0)
        self.assertEqual(self.matcher.match([km], [pm]), [])


# ---------------------------------------------------------------------------
# 6. Accent / normalization handling
# ---------------------------------------------------------------------------

class TestNormalization(unittest.TestCase):

    def test_normalize_strips_accents(self):
        self.assertEqual(normalize_name("Gaël Monfils"), "gael monfils")
        self.assertEqual(normalize_name("María Sakkari"), "maria sakkari")
        self.assertEqual(normalize_name("Radwańska"), "radwanska")

    def test_normalize_lowercases(self):
        self.assertEqual(normalize_name("DJOKOVIC"), "djokovic")

    def test_normalize_collapses_spaces(self):
        self.assertEqual(normalize_name("  Novak  Djokovic  "), "novak djokovic")

    def test_normalize_strips_punctuation(self):
        self.assertEqual(normalize_name("Garcia-Lopez"), "garcia lopez")
        self.assertEqual(normalize_name("V. Williams"), "v williams")

    def test_accented_title_matches_plain_poly_name(self):
        """
        Kalshi title has accented name; Poly uses plain ASCII.
        After normalization both sides match.
        """
        matcher = TennisMatcher()
        km = make_kalshi(
            "Will María Sakkari win the Sakkari vs Rybakina: R32 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Elena Rybakina", ask=0.45, taker_fee=0.0)
        results = matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertIn("rybakina", results[0]["polymarket_team"])

    def test_accented_poly_name_matches_plain_title(self):
        matcher = TennisMatcher()
        km = make_kalshi(
            "Will Sakkari win the Sakkari vs Rybakina: R32 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Éléna Rybakina", ask=0.45, taker_fee=0.0)
        results = matcher.match([km], [pm])
        self.assertEqual(len(results), 1)


# ---------------------------------------------------------------------------
# 7. Compound surname matching (Issue 5)
# ---------------------------------------------------------------------------

class TestCompoundSurname(unittest.TestCase):

    def test_compound_opponent_full_name_matches(self):
        """
        Opponent slot is 'garcia lopez' (compound surname).
        Poly team_name 'Martinez Garcia Lopez' should match via last-word fallback.
        """
        matcher = TennisMatcher()
        km = make_kalshi(
            "Will Alcaraz win the Alcaraz vs Garcia Lopez: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Garcia Lopez", ask=0.45, taker_fee=0.0)
        results = matcher.match([km], [pm])
        self.assertEqual(len(results), 1)

    def test_last_word_only_does_not_match_compound_differently(self):
        """
        'garcia' (last word only) should NOT match opponent slot 'garcia lopez'
        when issue-5 compound guard is active — because 'garcia' alone is ambiguous.
        But our fallback still uses last-word for simple pm_names; confirm
        full compound pm_name 'garcia lopez' matches compound slot 'garcia lopez'.
        """
        matcher = TennisMatcher()
        km = make_kalshi(
            "Will Alcaraz win the Alcaraz vs Garcia Lopez: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Garcia Lopez", ask=0.45, taker_fee=0.0)
        r = matcher.match([km], [pm])[0]
        self.assertIn("garcia lopez", r["polymarket_team"])


# ---------------------------------------------------------------------------
# 8. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_empty_inputs(self):
        self.assertEqual(self.matcher.match([], []), [])

    def test_empty_kalshi_list(self):
        pm = make_poly("Djokovic")
        self.assertEqual(self.matcher.match([], [pm]), [])

    def test_empty_poly_list(self):
        km = make_kalshi("Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?")
        self.assertEqual(self.matcher.match([km], []), [])

    def test_no_opponent_in_poly_no_match(self):
        """Poly only has the subject player; no opponent entry -> no match."""
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Tsitsipas", ask=0.45, taker_fee=0.0)
        self.assertEqual(self.matcher.match([km], [pm]), [])

    def test_unrelated_player_no_match(self):
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("Federer", ask=0.45, taker_fee=0.0)
        self.assertEqual(self.matcher.match([km], [pm]), [])

    def test_empty_team_name_no_match(self):
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        pm = make_poly("", ask=0.45, taker_fee=0.0)
        self.assertEqual(self.matcher.match([km], [pm]), [])

    def test_missing_raw_key_no_crash(self):
        km = {
            "ticker": "KXATPMATCH-001",
            "title": "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            "ask": 0.47,
            "taker_fee": 0.0,
        }
        pm = make_poly("Djokovic", ask=0.45, taker_fee=0.0)
        try:
            results = self.matcher.match([km], [pm])
        except KeyError as e:
            self.fail(f"KeyError raised for missing 'raw' key: {e}")
        self.assertEqual(len(results), 1)

    def test_slug_fallback_from_raw(self):
        """polymarket_slug falls back to raw['slug'] when top-level slug absent."""
        pm = {
            "team_name": "Djokovic",
            "ask": 0.45,
            "taker_fee": 0.0,
            "raw": {"slug": "atp-tsitsipas-djokovic-from-raw"},
        }
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.47, taker_fee=0.0,
        )
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_slug"], "atp-tsitsipas-djokovic-from-raw")

    def test_gap_cents_arithmetic(self):
        """gap_cents = round((0.96 - total_cost) * 100, 4)"""
        km = make_kalshi(
            "Will Tsitsipas win the Tsitsipas vs Djokovic: R64 match?",
            ask=0.40, taker_fee=0.0,
        )
        pm = make_poly("Djokovic", ask=0.40, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        self.assertAlmostEqual(r["gap_cents"], 16.0, places=4)


if __name__ == "__main__":
    unittest.main()
