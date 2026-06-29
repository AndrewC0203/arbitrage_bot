"""
QA test suite for BaseballMatcher (matchers/baseball.py).

Run from arb_scanner/:
    pytest tests/test_baseball_matcher.py -v

Covers:
  1. Contract: subclass, method signature, required output keys
  2. Live mode: arb detected, no-arb, time-skew rejection, missing gameStartTime, raw=None
  3. Test mode: team-set equality matching, false-trigger risk (mixed team_abbr)
  4. Empty inputs
  5. Ticker suffix extraction bug exposure
  6. Alias coverage for all 30 MLB teams
  7. ET→UTC offset correctness
"""

import sys
import os
import unittest
from datetime import datetime, timezone, timedelta

# Ensure arb_scanner/ is on path so `matchers.baseball` resolves
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matchers.base import BaseMatcher
from matchers.baseball import (
    BaseballMatcher,
    normalize_name,
    team_code,
    teams_from_kalshi_title,
    teams_from_kalshi_market,
    _kalshi_game_dt,
)

ARB_THRESHOLD = 0.96
KALSHI_TAKER_FEE_RATE = 0.01
POLYMARKET_TAKER_FEE_RATE = 0.01

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


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _kalshi_live(ticker: str, title: str, ask: float, raw: dict = None) -> dict:
    """Construct a Kalshi market dict in the live-mode shape (from as_kalshi_markets)."""
    fee = round(ask * KALSHI_TAKER_FEE_RATE, 6)
    return {
        "ticker": ticker,
        "title": title,
        "ask": ask,
        "taker_fee": fee,
        "raw": raw if raw is not None else {},
    }


def _poly_live(slug: str, team_abbr: str, ask: float, game_start_utc: str, raw_extra: dict = None) -> dict:
    """Construct a Polymarket market dict in the live-mode shape (from fetch_polymarket)."""
    fee = round(ask * POLYMARKET_TAKER_FEE_RATE, 6)
    raw = {"gameStartTime": game_start_utc, "slug": slug}
    if raw_extra:
        raw.update(raw_extra)
    return {
        "slug": slug,
        "title": f"poly_{slug}",
        "team_abbr": team_abbr,
        "ask": ask,
        "taker_fee": fee,
        "raw": raw,
    }


def _kalshi_test(ticker: str, title: str, ask: float) -> dict:
    """Construct a Kalshi market dict for test-mode (no raw, uses title for team extraction)."""
    fee = round(ask * KALSHI_TAKER_FEE_RATE, 6)
    return {"ticker": ticker, "title": title, "ask": ask, "taker_fee": fee, "raw": {}}


def _poly_test(slug: str, question: str, ask: float) -> dict:
    """
    Construct a Polymarket dict WITHOUT team_abbr.
    Absence of team_abbr on ANY market triggers test-mode in BaseballMatcher.
    """
    fee = round(ask * POLYMARKET_TAKER_FEE_RATE, 6)
    return {"slug": slug, "title": question, "ask": ask, "taker_fee": fee, "raw": {}}


# Canonical live-mode fixture: CIN @ MIL, 2026-07-01 20:10 ET (= 00:10 UTC next day)
# Ticker suffix: KXMLBGAME-26JUL012010CINMIL-CIN  (CIN is the Kalshi side)
# Polymarket opponent is MIL, game start UTC: 2026-07-02T00:10:00Z
CIN_TICKER    = "KXMLBGAME-26JUL012010CINMIL-CIN"
CIN_TITLE     = "Cincinnati vs Milwaukee Winner?"
CIN_UTC_START = "2026-07-02T00:10:00Z"   # 20:10 ET + 4h = 00:10 UTC

MIL_TICKER    = "KXMLBGAME-26JUL012010CINMIL-MIL"
MIL_TITLE     = "Cincinnati vs Milwaukee Winner?"


# ══════════════════════════════════════════════════════════════════════════════
# 1. CONTRACT
# ══════════════════════════════════════════════════════════════════════════════

class TestContract(unittest.TestCase):
    """Verify BaseballMatcher correctly implements BaseMatcher."""

    def test_C01_is_subclass_of_base_matcher(self):
        """BaseballMatcher must inherit from BaseMatcher ABC."""
        self.assertTrue(issubclass(BaseballMatcher, BaseMatcher))

    def test_C02_instantiates_with_default_threshold(self):
        m = BaseballMatcher()
        self.assertEqual(m.arb_threshold, 0.96)

    def test_C03_instantiates_with_custom_threshold(self):
        m = BaseballMatcher(arb_threshold=0.90)
        self.assertEqual(m.arb_threshold, 0.90)

    def test_C04_match_is_callable_with_two_positional_args(self):
        m = BaseballMatcher()
        result = m.match([], [])
        self.assertIsInstance(result, list)

    def test_C05_result_contains_all_required_keys(self):
        """Every result dict must have all 12 required keys consumed by ws_manager."""
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                          raw={"title": CIN_TITLE})
        # CIN side of the game; opponent is MIL
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, CIN_UTC_START)
        # CIN side needs to be present as well (slug cross-check in live mode)
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, CIN_UTC_START)

        matcher = BaseballMatcher()
        results = matcher.match([km], [pm_mil, pm_cin])
        self.assertGreater(len(results), 0, "Expected at least one match")
        for r in results:
            self.assertEqual(REQUIRED_KEYS, set(r.keys()),
                             f"Missing or extra keys: {set(r.keys()) ^ REQUIRED_KEYS}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. LIVE MODE
# ══════════════════════════════════════════════════════════════════════════════

class TestLiveModeArbDetected(unittest.TestCase):
    """2a: Valid matching markets with total_cost < 0.96 → is_arb=True."""

    def setUp(self):
        self.matcher = BaseballMatcher()
        # Kalshi CIN ask=0.43; fee=0.0043; Poly MIL ask=0.40; fee=0.0040
        # total = 0.43 + 0.40 + 0.0043 + 0.0040 = 0.8383 < 0.96 → arb
        self.km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                               raw={"title": CIN_TITLE})
        self.pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, CIN_UTC_START)
        self.pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, CIN_UTC_START)

    def test_L01_arb_is_true_when_total_below_threshold(self):
        results = self.matcher.match([self.km], [self.pm_mil, self.pm_cin])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_arb"])

    def test_L02_total_cost_calculated_correctly(self):
        results = self.matcher.match([self.km], [self.pm_mil, self.pm_cin])
        r = results[0]
        expected = round(0.43 + 0.40 + 0.43 * 0.01 + 0.40 * 0.01, 6)
        self.assertAlmostEqual(r["total_cost"], expected, places=5)

    def test_L03_gap_cents_positive_when_arb(self):
        results = self.matcher.match([self.km], [self.pm_mil, self.pm_cin])
        self.assertGreater(results[0]["gap_cents"], 0)

    def test_L04_kalshi_team_is_ticker_suffix(self):
        """kalshi_team must be the lowercase ticker suffix (e.g. 'cin')."""
        results = self.matcher.match([self.km], [self.pm_mil, self.pm_cin])
        self.assertEqual(results[0]["kalshi_team"], "cin")

    def test_L05_polymarket_team_is_opponent(self):
        """polymarket_team is the opponent side of the Kalshi ticker."""
        results = self.matcher.match([self.km], [self.pm_mil, self.pm_cin])
        self.assertEqual(results[0]["polymarket_team"], "mil")

    def test_L06_polymarket_ask_is_opponents_price(self):
        """polymarket_ask is the opponent (MIL) ask, not the Kalshi-side ask."""
        results = self.matcher.match([self.km], [self.pm_mil, self.pm_cin])
        self.assertAlmostEqual(results[0]["polymarket_ask"], 0.40, places=5)


class TestLiveModeNoArb(unittest.TestCase):
    """2b: Markets match but combined price ≥ 0.96 → is_arb=False."""

    def test_L07_no_arb_when_total_at_threshold(self):
        matcher = BaseballMatcher()
        # 0.47 + 0.47 + fees ≈ 0.9494 < 0.96 — adjust to be >= 0.96
        # 0.47 + 0.47 + 0.0047 + 0.0047 = 0.9494 — still arb
        # Use 0.475 + 0.475 + fees = 0.9595 — still arb
        # Use 0.48 + 0.48 + 0.0048 + 0.0048 = 0.9696 >= 0.96 → not arb
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.48,
                          raw={"title": CIN_TITLE})
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.48, CIN_UTC_START)
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.48, CIN_UTC_START)
        results = matcher.match([km], [pm_mil, pm_cin])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["is_arb"])

    def test_L08_no_arb_gap_cents_negative(self):
        matcher = BaseballMatcher()
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.48,
                          raw={"title": CIN_TITLE})
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.48, CIN_UTC_START)
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.48, CIN_UTC_START)
        results = matcher.match([km], [pm_mil, pm_cin])
        self.assertLess(results[0]["gap_cents"], 0)


class TestLiveModeTimeSkew(unittest.TestCase):
    """2c: Game times differ by >30 min → market skipped (no match returned)."""

    def test_L09_time_skew_gt_30min_no_match(self):
        matcher = BaseballMatcher()
        # CIN_UTC_START = 2026-07-02T00:10:00Z
        # Offset Poly by 31 minutes → 2026-07-02T00:41:00Z — beyond the 30 min window
        skewed_start = "2026-07-02T00:41:00Z"
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                          raw={"title": CIN_TITLE})
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, skewed_start)
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, skewed_start)
        results = matcher.match([km], [pm_mil, pm_cin])
        self.assertEqual(results, [],
                         "Markets with >30 min time skew must not match")

    def test_L10_time_skew_exactly_30min_matches(self):
        """Boundary: exactly 30 min skew is within the allowed window."""
        matcher = BaseballMatcher()
        # Kalshi ET 20:10 + 4h = UTC 00:10; 30 min later = 00:40
        boundary_start = "2026-07-02T00:40:00Z"
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                          raw={"title": CIN_TITLE})
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, boundary_start)
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, boundary_start)
        results = matcher.match([km], [pm_mil, pm_cin])
        self.assertEqual(len(results), 1,
                         "Exactly 30 min skew should still match (boundary inclusive)")


class TestLiveModeMissingGameStartTime(unittest.TestCase):
    """2d: poly_opp["raw"] absent or empty gameStartTime → skipped gracefully (no crash)."""

    def test_L11_missing_game_start_time_no_crash(self):
        matcher = BaseballMatcher()
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                          raw={"title": CIN_TITLE})
        # raw exists but no gameStartTime key
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, "")
        pm_mil["raw"] = {}  # Overwrite to have no gameStartTime
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, "")
        pm_cin["raw"] = {}
        results = matcher.match([km], [pm_mil, pm_cin])
        # Should return [] without raising an exception
        self.assertEqual(results, [])

    def test_L12_empty_game_start_time_string_no_crash(self):
        matcher = BaseballMatcher()
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                          raw={"title": CIN_TITLE})
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, "")  # empty string
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, "")
        results = matcher.match([km], [pm_mil, pm_cin])
        self.assertEqual(results, [])

    def test_L13_non_iso_game_start_time_no_crash(self):
        matcher = BaseballMatcher()
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                          raw={"title": CIN_TITLE})
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, "not-a-date")
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, "not-a-date")
        results = matcher.match([km], [pm_mil, pm_cin])
        self.assertEqual(results, [])


class TestLiveModeRawNone(unittest.TestCase):
    """2e: km["raw"] is None → teams_from_kalshi_market gracefully skips → returns []."""

    def test_L14_raw_none_no_crash_returns_empty(self):
        """
        When km["raw"] is None, teams_from_kalshi_market(km.get("raw") or {})
        evaluates to teams_from_kalshi_market({}) → returns None → market skipped.
        No KeyError or AttributeError should be raised.
        """
        matcher = BaseballMatcher()
        km = {
            "ticker": CIN_TICKER,
            "title": CIN_TITLE,
            "ask": 0.43,
            "taker_fee": 0.0043,
            "raw": None,  # explicitly None
        }
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, CIN_UTC_START)
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, CIN_UTC_START)
        # teams_from_kalshi_market(None or {}) → teams_from_kalshi_market({}) → None
        # so no match. But: the title field is on km itself, so fallback may find it.
        # This test asserts no crash occurs.
        try:
            results = matcher.match([km], [pm_mil, pm_cin])
            self.assertIsInstance(results, list)
        except Exception as e:
            self.fail(f"match() raised {type(e).__name__} when raw=None: {e}")

    def test_L15_raw_missing_key_no_crash(self):
        """km without a 'raw' key at all → km.get('raw') returns None → no crash."""
        matcher = BaseballMatcher()
        km = {
            "ticker": CIN_TICKER,
            "title": CIN_TITLE,
            "ask": 0.43,
            "taker_fee": 0.0043,
            # 'raw' key intentionally absent
        }
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, CIN_UTC_START)
        pm_cin = _poly_live("aec-mlb-cin-mil", "cin", 0.43, CIN_UTC_START)
        try:
            results = matcher.match([km], [pm_mil, pm_cin])
            self.assertIsInstance(results, list)
        except Exception as e:
            self.fail(f"match() raised {type(e).__name__} when 'raw' key absent: {e}")


class TestLiveModeNoKteamSideFound(unittest.TestCase):
    """Live mode: when poly_kteam_sides is empty (slug not on Kalshi side) → no match."""

    def test_L16_no_match_when_only_opponent_poly_market(self):
        """If only the opponent's poly side is present (not slug cross-match), skip."""
        matcher = BaseballMatcher()
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                          raw={"title": CIN_TITLE})
        # Only MIL side; CIN poly side (same slug) is absent
        pm_mil = _poly_live("aec-mlb-cin-mil", "mil", 0.40, CIN_UTC_START)
        results = matcher.match([km], [pm_mil])
        self.assertEqual(results, [],
                         "Without the Kalshi-team Poly side, no match should be produced")


# ══════════════════════════════════════════════════════════════════════════════
# 3. TEST MODE
# ══════════════════════════════════════════════════════════════════════════════

class TestTestMode(unittest.TestCase):
    """3a: Any polymarket market without team_abbr → test-mode (set-equality matching)."""

    def test_T01_test_mode_activates_when_no_team_abbr(self):
        """Poly markets without team_abbr trigger test mode; teams matched by set equality."""
        matcher = BaseballMatcher()
        km = _kalshi_test("KXMLBGAME-26JUN281340SEACLE",
                          "Seattle @ Cleveland Winner?", 0.45)
        pm = _poly_test("poly-sea-cle", "Seattle vs Cleveland", 0.42)
        results = matcher.match([km], [pm])
        self.assertEqual(len(results), 1)

    def test_T02_test_mode_match_is_arb_true(self):
        matcher = BaseballMatcher()
        km = _kalshi_test("KXMLBGAME-26JUN281340SEACLE",
                          "Seattle @ Cleveland Winner?", 0.45)
        pm = _poly_test("poly-sea-cle", "Seattle vs Cleveland", 0.42)
        results = matcher.match([km], [pm])
        # 0.45 + 0.42 + 0.0045 + 0.0042 = 0.8787 < 0.96
        self.assertTrue(results[0]["is_arb"])

    def test_T03_test_mode_no_match_different_teams(self):
        """Different team sets in test mode → no match."""
        matcher = BaseballMatcher()
        km = _kalshi_test("KXMLBGAME-26JUN281340SEACLE",
                          "Seattle @ Cleveland Winner?", 0.45)
        pm = _poly_test("poly-bos-nyy", "Boston vs New York Yankees", 0.42)
        results = matcher.match([km], [pm])
        self.assertEqual(results, [])

    def test_T04_test_mode_result_has_all_required_keys(self):
        """Test mode result dict must contain all 12 required keys."""
        matcher = BaseballMatcher()
        km = _kalshi_test("KXMLBGAME-26JUN281340SEACLE",
                          "Seattle @ Cleveland Winner?", 0.45)
        pm = _poly_test("poly-sea-cle", "Seattle vs Cleveland", 0.42)
        results = matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(REQUIRED_KEYS, set(results[0].keys()))

    def test_T05_test_mode_false_trigger_mixed_poly_markets(self):
        """
        RISK DOCUMENTATION TEST: if even ONE poly market lacks team_abbr, ALL
        markets enter test mode. This bypasses time-based alignment entirely.

        Consequence: a Kalshi CIN vs MIL market could false-match a Poly CIN vs MIL
        market from a different game date because test mode only checks set equality.
        This is a known architectural risk. This test documents the behavior.
        """
        matcher = BaseballMatcher()
        # One live market (has team_abbr) + one test-style market (no team_abbr)
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43,
                          raw={"title": CIN_TITLE})
        pm_live = _poly_live("aec-mlb-cin-mil", "mil", 0.40, CIN_UTC_START)
        # This market lacks team_abbr → forces entire match() into test mode
        pm_test = _poly_test("aec-mlb-cin-mil-other", "Cincinnati vs Milwaukee Winner?", 0.39)

        results = matcher.match([km], [pm_live, pm_test])
        # In test mode, km.get("raw") or {} = {} → teams from km["title"] → (cin, mil)
        # pm_test title "Cincinnati vs Milwaukee" → teams (cin, mil) → SET MATCH
        # pm_live has no question/title that reveals teams... but it has a "title" field
        # This test reveals that mixing live + no-team_abbr poly markets is risky.
        # We don't assert a specific count here — we assert no crash and document the risk.
        self.assertIsInstance(results, list,
                              "Mixing team_abbr and non-team_abbr poly markets must not crash")
        # RISK: results may contain matches that bypassed time alignment checks


# ══════════════════════════════════════════════════════════════════════════════
# 4. EMPTY INPUTS
# ══════════════════════════════════════════════════════════════════════════════

class TestEmptyInputs(unittest.TestCase):
    """4: Empty lists in either or both args → returns [], no crash."""

    def test_E01_both_empty(self):
        self.assertEqual(BaseballMatcher().match([], []), [])

    def test_E02_empty_kalshi_markets(self):
        pm = _poly_live("aec-mlb-cin-mil", "mil", 0.40, CIN_UTC_START)
        self.assertEqual(BaseballMatcher().match([], [pm]), [])

    def test_E03_empty_polymarket_markets(self):
        """
        is_test_mode = any('team_abbr' not in pm for pm in []) = False (any() on empty = False)
        → falls into live mode → poly_by_team is empty → returns [].
        """
        km = _kalshi_live(CIN_TICKER, CIN_TITLE, 0.43, raw={"title": CIN_TITLE})
        self.assertEqual(BaseballMatcher().match([km], []), [])

    def test_E04_empty_poly_is_live_mode_not_test_mode(self):
        """
        Confirm that empty polymarket_markets → is_test_mode=False (any() on empty = False).
        This is correct: live mode with no poly markets simply returns [].
        Risk: if caller expected test mode, this could be surprising — but the behavior is safe.
        """
        result = any("team_abbr" not in pm for pm in [])
        self.assertFalse(result, "any() on empty iterable must return False")


# ══════════════════════════════════════════════════════════════════════════════
# 5. TICKER SUFFIX EXTRACTION BUG EXPOSURE
# ══════════════════════════════════════════════════════════════════════════════

class TestTickerSuffixExtraction(unittest.TestCase):
    """
    5: Live mode uses km["ticker"].split("-")[-1].lower() to extract k_team.

    CRITICAL FINDING: The actual Kalshi ticker format (confirmed from arb_log.jsonl)
    is THREE segments: KXMLBGAME-<datetime><teams>-<TEAMCODE>
    e.g. KXMLBGAME-26JUN301905DETNYY-DET

    split("-")[-1].lower() on a 3-segment ticker correctly returns "det".
    split("-")[-1].lower() on a hypothetical 2-segment ticker returns "26jun301905detnyy" — WRONG.

    This suite documents both behaviors and confirms the real format works.
    """

    def test_S01_three_segment_ticker_suffix_is_team_code(self):
        """Real ticker format: 3 segments → suffix is the team code."""
        ticker = "KXMLBGAME-26JUN301905DETNYY-DET"
        suffix = ticker.split("-")[-1].lower()
        self.assertEqual(suffix, "det",
                         "3-segment ticker suffix must be the team code")

    def test_S02_three_segment_opponent_ticker_suffix_correct(self):
        ticker = "KXMLBGAME-26JUN301905DETNYY-NYY"
        suffix = ticker.split("-")[-1].lower()
        self.assertEqual(suffix, "nyy")

    def test_S03_two_segment_ticker_suffix_is_not_team_code(self):
        """
        DOCUMENTS A LATENT BUG: if Kalshi ever returns a 2-segment ticker
        (no trailing -TEAMCODE), split("-")[-1] returns the full datetime+teams string.
        This would cause k_team not in (team_a, team_b) → market silently skipped.

        This would cause ALL live-mode matches to fail with zero output and no error.
        """
        ticker = "KXMLBGAME-26JUN301905DETNYY"
        suffix = ticker.split("-")[-1].lower()
        team_a, team_b = "det", "nyy"
        self.assertNotIn(suffix, (team_a, team_b),
                         "2-segment ticker suffix is NOT a team code — would cause silent match failure")

    def test_S04_live_mode_two_segment_ticker_returns_empty(self):
        """
        Expose the bug: 2-segment ticker in live mode → no matches (silent failure).
        Real tickers have 3 segments, so this is a latent risk if the API changes format.
        """
        matcher = BaseballMatcher()
        # Use a 2-segment ticker (hypothetical / format-changed API)
        km = _kalshi_live(
            "KXMLBGAME-26JUN301905DETNYY",  # 2-segment — no trailing team code
            "Detroit @ New York Yankees Winner?",
            0.44,
            raw={"title": "Detroit @ New York Yankees Winner?"},
        )
        pm_nyy = _poly_live("aec-mlb-det-nyy", "nyy", 0.40, "2026-06-30T23:05:00Z")
        pm_det = _poly_live("aec-mlb-det-nyy", "det", 0.44, "2026-06-30T23:05:00Z")
        results = matcher.match([km], [pm_nyy, pm_det])
        self.assertEqual(results, [],
                         "2-segment ticker silently fails all matches — documents bug risk")

    def test_S05_live_mode_three_segment_ticker_produces_match(self):
        """Confirm real 3-segment ticker works end-to-end in live mode."""
        matcher = BaseballMatcher()
        km = _kalshi_live(
            "KXMLBGAME-26JUN301905DETNYY-DET",
            "Detroit @ New York Yankees Winner?",
            0.44,
            raw={"title": "Detroit @ New York Yankees Winner?"},
        )
        # 19:05 ET + 4h = 23:05 UTC on same date (2026-06-30)
        game_utc = "2026-06-30T23:05:00Z"
        pm_nyy = _poly_live("aec-mlb-det-nyy", "nyy", 0.40, game_utc)
        pm_det = _poly_live("aec-mlb-det-nyy", "det", 0.44, game_utc)
        results = matcher.match([km], [pm_nyy, pm_det])
        self.assertEqual(len(results), 1,
                         "3-segment ticker must produce a match")
        self.assertEqual(results[0]["kalshi_team"], "det")
        self.assertEqual(results[0]["polymarket_team"], "nyy")


# ══════════════════════════════════════════════════════════════════════════════
# 6. ALIAS COVERAGE — 30 MLB TEAMS
# ══════════════════════════════════════════════════════════════════════════════

class TestAliasCovers30Teams(unittest.TestCase):
    """
    6: Verify all 30 MLB team names resolve to the expected canonical code.
    Tests Kalshi abbreviated forms (Learned Rule 4) and Polymarket full names.
    """

    TEAM_SAMPLES = [
        # (input text, expected_code)
        # Kalshi abbreviated forms (Learned Rule 4)
        ("Los Angeles D", "lad"),
        ("Los Angeles A", "laa"),
        ("New York M", "nym"),
        ("New York Y", "nyy"),
        ("Chicago C", "chc"),
        ("Chicago WS", "cws"),
        # Standard city/nickname forms
        ("Arizona", "ari"),
        ("Diamondbacks", "ari"),
        ("Atlanta", "atl"),
        ("Braves", "atl"),
        ("Baltimore", "bal"),
        ("Orioles", "bal"),
        ("Boston", "bos"),
        ("Red Sox", "bos"),
        ("Chicago Cubs", "chc"),
        ("Cubs", "chc"),
        ("Chicago White Sox", "cws"),
        ("White Sox", "cws"),
        ("Cincinnati", "cin"),
        ("Reds", "cin"),
        ("Cleveland", "cle"),
        ("Guardians", "cle"),
        ("Colorado", "col"),
        ("Rockies", "col"),
        ("Detroit", "det"),
        ("Tigers", "det"),
        ("Houston", "hou"),
        ("Astros", "hou"),
        ("Kansas City", "kc"),
        ("Royals", "kc"),
        ("Los Angeles Angels", "laa"),
        ("Angels", "laa"),
        ("Los Angeles Dodgers", "lad"),
        ("Dodgers", "lad"),
        ("Miami", "mia"),
        ("Marlins", "mia"),
        ("Milwaukee", "mil"),
        ("Brewers", "mil"),
        ("Minnesota", "min"),
        ("Twins", "min"),
        ("New York Mets", "nym"),
        ("Mets", "nym"),
        ("New York Yankees", "nyy"),
        ("Yankees", "nyy"),
        ("Oakland", "oak"),
        ("Athletics", "oak"),
        ("Philadelphia", "phi"),
        ("Phillies", "phi"),
        ("Pittsburgh", "pit"),
        ("Pirates", "pit"),
        ("San Diego", "sd"),
        ("Padres", "sd"),
        ("San Francisco", "sf"),
        ("Giants", "sf"),
        ("Seattle", "sea"),
        ("Mariners", "sea"),
        ("St. Louis", "stl"),
        ("Cardinals", "stl"),
        ("Tampa Bay", "tb"),
        ("Rays", "tb"),
        ("Texas", "tex"),
        ("Rangers", "tex"),
        ("Toronto", "tor"),
        ("Blue Jays", "tor"),
        ("Washington", "wsh"),
        ("Nationals", "wsh"),
    ]

    def test_A01_all_alias_forms_resolve_correctly(self):
        """Every alias sample must resolve to the expected code."""
        failures = []
        for text, expected in self.TEAM_SAMPLES:
            got = team_code(text)
            if got != expected:
                failures.append(f"team_code({text!r}) = {got!r}, want {expected!r}")
        if failures:
            self.fail("Alias resolution failures:\n" + "\n".join(failures))

    def test_A02_chicago_cubs_beats_chicago_c_ambiguity(self):
        """'Chicago Cubs' must resolve to chc, not cws (longest-match wins)."""
        self.assertEqual(team_code("Chicago Cubs"), "chc")

    def test_A03_chicago_ws_alias_resolves_to_cws(self):
        """Kalshi abbreviated 'Chicago WS' → cws (Learned Rule 4)."""
        self.assertEqual(team_code("Chicago WS"), "cws")

    def test_A04_mets_not_confused_with_yankees(self):
        """'New York M' → nym; 'New York Y' → nyy (Learned Rule 4)."""
        self.assertEqual(team_code("New York M"), "nym")
        self.assertEqual(team_code("New York Y"), "nyy")

    def test_A05_la_dodgers_not_confused_with_angels(self):
        """'Los Angeles D' → lad; 'Los Angeles A' → laa (Learned Rule 4)."""
        self.assertEqual(team_code("Los Angeles D"), "lad")
        self.assertEqual(team_code("Los Angeles A"), "laa")

    def test_A06_oakland_as_apostrophe_normalized(self):
        """
        Kalshi uses "A's" for Oakland (Learned Rule 4).
        normalize_name strips apostrophe: "A's" → "a s".
        "a s" → oak via alias ("a s", "oak").
        """
        self.assertEqual(team_code("A's"), "oak",
                         "\"A's\" must normalize to \"a s\" and resolve to oak")

    def test_A07_all_30_team_codes_reachable(self):
        """Each of the 30 canonical codes appears at least once in TEAM_SAMPLES."""
        expected_codes = {
            "ari", "atl", "bal", "bos", "chc", "cws", "cin", "cle", "col",
            "det", "hou", "kc", "laa", "lad", "mia", "mil", "min", "nym",
            "nyy", "oak", "phi", "pit", "sd", "sf", "sea", "stl", "tb",
            "tex", "tor", "wsh",
        }
        covered = {code for _, code in self.TEAM_SAMPLES}
        self.assertEqual(expected_codes, covered,
                         f"Missing codes: {expected_codes - covered}")


# ══════════════════════════════════════════════════════════════════════════════
# 7. DATE/TIME OFFSET
# ══════════════════════════════════════════════════════════════════════════════

class TestDateTimeOffset(unittest.TestCase):
    """
    7: Verify the ET→UTC conversion and _kalshi_game_dt behavior.

    Live code uses a hardcoded +4h offset (EDT, summer).
    During EST (winter, UTC-5), the correct offset would be +5h.
    This is a documented risk; this suite confirms current behavior and the edge.
    """

    def test_D01_kalshi_game_dt_parses_correctly(self):
        """_kalshi_game_dt extracts datetime from segment[1][:11]."""
        ticker = "KXMLBGAME-26JUN301905DETNYY-DET"
        dt = _kalshi_game_dt(ticker)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 6)
        self.assertEqual(dt.day, 30)
        self.assertEqual(dt.hour, 19)
        self.assertEqual(dt.minute, 5)

    def test_D02_hardcoded_plus4h_is_correct_for_summer_edt(self):
        """
        Summer (EDT = UTC-4): adding 4h to the parsed ET time gives UTC.
        Kalshi 19:05 ET → UTC 23:05 on same day.
        """
        ticker = "KXMLBGAME-26JUN301905DETNYY-DET"
        dt_et = _kalshi_game_dt(ticker)
        dt_utc = dt_et + timedelta(hours=4)
        self.assertEqual(dt_utc.hour, 23)
        self.assertEqual(dt_utc.minute, 5)

    def test_D03_zoneinfo_dst_aware_summer_and_winter(self):
        """
        The live code uses ZoneInfo("America/New_York") (DST-aware), NOT a hardcoded +4h.
        Verify that _kalshi_game_dt() + replace(tzinfo=_ET).astimezone(UTC) gives
        correct UTC for both a summer (EDT, UTC-4) and winter (EST, UTC-5) game.
        """
        from zoneinfo import ZoneInfo
        from matchers.baseball import _kalshi_game_dt
        _ET = ZoneInfo("America/New_York")

        # Summer: 2026-06-30 19:05 ET (EDT = UTC-4) → 23:05 UTC same day
        summer_ticker = "KXMLBGAME-26JUN301905DETNYY-DET"
        dt_summer = _kalshi_game_dt(summer_ticker)
        self.assertIsNotNone(dt_summer)
        utc_summer = dt_summer.replace(tzinfo=_ET).astimezone(timezone.utc)
        self.assertEqual(utc_summer.hour, 23)
        self.assertEqual(utc_summer.minute, 5)
        self.assertEqual(utc_summer.day, 30)

        # Winter: 2026-01-15 13:05 ET (EST = UTC-5) → 18:05 UTC same day
        winter_ticker = "KXMLBGAME-26JAN151305BOSNYY-BOS"
        dt_winter = _kalshi_game_dt(winter_ticker)
        self.assertIsNotNone(dt_winter, "Winter ticker must parse correctly")
        utc_winter = dt_winter.replace(tzinfo=_ET).astimezone(timezone.utc)
        self.assertEqual(utc_winter.hour, 18)
        self.assertEqual(utc_winter.minute, 5)
        self.assertEqual(utc_winter.day, 15)

    def test_D04_no_separator_ticker_returns_none(self):
        """Ticker with no '-' separator → _kalshi_game_dt returns None gracefully."""
        self.assertIsNone(_kalshi_game_dt("KXMLBGAME"))

    def test_D05_malformed_segment_returns_none(self):
        """Non-parseable segment → _kalshi_game_dt returns None (no crash)."""
        self.assertIsNone(_kalshi_game_dt("KXMLBGAME-BADFORMAT-DET"))


# ══════════════════════════════════════════════════════════════════════════════
# 8. MONEYLINE FILTER
# ══════════════════════════════════════════════════════════════════════════════

class TestMoneylineFilter(unittest.TestCase):
    """
    Note: kalshi_is_moneyline / polymarket_is_moneyline are called by ws_manager
    BEFORE markets are passed to BaseballMatcher.match(). The matcher itself does
    not call these filters internally.

    These tests document the filter behavior but do NOT test matcher integration
    (the matcher trusts its caller to pre-filter).
    """

    def test_M01_reject_keyword_in_title_returns_false(self):
        from matchers.baseball import polymarket_is_moneyline
        market = {"question": "Will over 8.5 total runs be scored?", "title": ""}
        self.assertFalse(polymarket_is_moneyline(market))

    def test_M02_clean_moneyline_question_returns_true(self):
        from matchers.baseball import polymarket_is_moneyline
        market = {"question": "Seattle vs Cleveland", "title": ""}
        self.assertTrue(polymarket_is_moneyline(market))

    def test_M03_kalshi_vs_separator_in_title_is_moneyline(self):
        from matchers.baseball import kalshi_is_moneyline
        market = {"title": "Seattle vs Cleveland Winner?", "subtitle": ""}
        self.assertTrue(kalshi_is_moneyline(market))

    def test_M04_kalshi_reject_keyword_overrides_vs(self):
        """'first' is in reject keywords — title with both 'vs' and 'first' → False."""
        from matchers.baseball import kalshi_is_moneyline
        market = {"title": "Seattle vs Cleveland - First 5 Innings Winner?", "subtitle": ""}
        self.assertFalse(kalshi_is_moneyline(market))


if __name__ == "__main__":
    unittest.main()
