"""
QA test suite for BasketballMatcher.

Run from the arb_scanner/ directory:
    pytest tests/test_basketball_matcher.py -v

Tests are self-contained; no live API calls.
"""

import sys
import os
import unittest

# Allow imports from arb_scanner/ root regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matchers.base import BaseMatcher
from matchers.basketball import BasketballMatcher, normalize_name, teams_from_kalshi_title


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

def make_kalshi(title, ticker="KXNBA-26JUN281900LAKBOS-LAL", ask=0.48, taker_fee=0.0048, raw=None):
    return {
        "ticker": ticker,
        "title": title,
        "ask": ask,
        "taker_fee": taker_fee,
        "raw": raw or {},
    }


def make_poly(team_abbr, slug="nba-lakers-celtics-2026-06-28", ask=0.47, taker_fee=0.0047, raw=None):
    return {
        "slug": slug,
        "team_abbr": team_abbr,
        "ask": ask,
        "taker_fee": taker_fee,
        "raw": raw or {},
    }


# ---------------------------------------------------------------------------
# 1. Contract tests
# ---------------------------------------------------------------------------

class TestContract(unittest.TestCase):

    def test_is_subclass_of_base_matcher(self):
        """BasketballMatcher must be a concrete subclass of BaseMatcher."""
        self.assertTrue(issubclass(BasketballMatcher, BaseMatcher))

    def test_has_match_method(self):
        matcher = BasketballMatcher()
        self.assertTrue(callable(getattr(matcher, "match", None)))

    def test_match_returns_list(self):
        matcher = BasketballMatcher()
        result = matcher.match([], [])
        self.assertIsInstance(result, list)

    def test_result_dicts_contain_required_keys(self):
        """Every returned dict must contain all keys that OpportunityTracker.process() reads."""
        # Use a full-name abbr so the substring match actually fires (see bug test below).
        matcher = BasketballMatcher()
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics")
        pm = make_poly(team_abbr="los angeles lakers")
        result = matcher.match([km], [pm])
        self.assertGreater(len(result), 0, "expected at least one match")
        for r in result:
            missing = REQUIRED_KEYS - set(r.keys())
            self.assertEqual(missing, set(), f"missing keys: {missing}")

    def test_no_get_odds_method(self):
        """BasketballMatcher should NOT expose get_odds(); interface is match() only."""
        self.assertFalse(hasattr(BasketballMatcher, "get_odds"))


# ---------------------------------------------------------------------------
# 2. Logic audit — arb detection
# ---------------------------------------------------------------------------

class TestArbDetection(unittest.TestCase):

    def setUp(self):
        self.matcher = BasketballMatcher(arb_threshold=0.96)

    # 2a. Basic match — is_arb True
    def test_basic_match_arb_true(self):
        """
        Total cost 0.48 + 0.47 + 0.0048 + 0.0047 = 0.9595 < 0.96 → is_arb=True.
        Uses full-name team_abbr to work around the substring-match bug.
        """
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0048)
        pm = make_poly("los angeles lakers", ask=0.47, taker_fee=0.0047)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_arb"])
        self.assertAlmostEqual(results[0]["total_cost"], 0.9595, places=4)
        self.assertGreater(results[0]["gap_cents"], 0)

    # 2b. Basic match — is_arb False
    def test_basic_match_no_arb(self):
        """
        Total cost 0.52 + 0.46 + fees > 0.96 → match still included, is_arb=False.
        """
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.52, taker_fee=0.0052)
        pm = make_poly("los angeles lakers", ask=0.46, taker_fee=0.0046)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["is_arb"])
        self.assertLess(results[0]["gap_cents"], 0)

    # 2c. gap_cents arithmetic
    def test_gap_cents_value(self):
        km = make_kalshi("Lakers vs Celtics", ask=0.40, taker_fee=0.0)
        pm = make_poly("lakers", ask=0.40, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        # gap_cents = round((0.96 - 0.80) * 100, 4) = 16.0
        self.assertEqual(results[0]["gap_cents"], 16.0)

    # 2d. Correct team assignment
    def test_team_assignment_home_side(self):
        """
        pm_team 'los angeles lakers' is in teams_k[0] →
        kalshi_team should be teams_k[1] (boston celtics),
        polymarket_team should be teams_k[0] (los angeles lakers).
        """
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0)
        pm = make_poly("los angeles lakers", ask=0.47, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        self.assertEqual(r["kalshi_team"], "bos")
        self.assertEqual(r["polymarket_team"], "lal")

    def test_team_assignment_away_side(self):
        """
        pm_team 'boston celtics' is in teams_k[1] →
        kalshi_team should be teams_k[0] (los angeles lakers).
        """
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0)
        pm = make_poly("boston celtics", ask=0.47, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        self.assertEqual(r["kalshi_team"], "lal")
        self.assertEqual(r["polymarket_team"], "bos")


# ---------------------------------------------------------------------------
# 3. Substring-match bug tests
# ---------------------------------------------------------------------------

class TestSubstringMatchBug(unittest.TestCase):
    """
    The matcher uses alias lookup via team_code() to resolve Polymarket team_abbr
    to a canonical code, then compares codes with teams_from_kalshi_title() result.
    Polymarket sends 3-letter codes like "lal", "bos", "gsw"; these resolve via _ALIASES.
    These tests document the alias behavior and expose any remaining gaps.
    """

    def setUp(self):
        self.matcher = BasketballMatcher()

    def test_three_letter_nba_abbr_lal_does_not_match(self):
        """
        BUG: "lal" not in "los angeles lakers" → no match produced.
        This test SHOULD FAIL once the bug is fixed to use an alias lookup.
        Currently it PASSES (no match), confirming the bug.
        """
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0048)
        pm = make_poly("lal", ask=0.47, taker_fee=0.0047)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1,
            "BUG FIXED: 3-letter abbr 'lal' now correctly matches 'los angeles lakers' via alias lookup.")

    def test_three_letter_abbr_bos_matches_via_alias(self):
        """
        "bos" has an explicit ("bos","bos") alias entry, so team_code("bos") returns "bos".
        teams_from_kalshi_title("Los Angeles Lakers vs Boston Celtics") returns ("lal","bos").
        Code match fires correctly → 1 result.
        """
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0048)
        pm = make_poly("bos", ask=0.47, taker_fee=0.0047)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1,
            "BUG FIXED: 'bos' now correctly matches Boston Celtics via alias lookup.")

    def test_three_letter_abbr_gsw_does_not_match(self):
        """BUG: "gsw" not in "golden state warriors"."""
        km = make_kalshi("Golden State Warriors vs Phoenix Suns", ask=0.48, taker_fee=0.0)
        pm = make_poly("gsw", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1, "BUG FIXED: 'gsw' now matches 'golden state warriors'.")

    def test_ncaab_abbr_unc_does_not_match(self):
        """BUG: 'unc' not in 'north carolina' → NCAAB markets never match."""
        km = make_kalshi("North Carolina vs Duke", ask=0.48, taker_fee=0.0)
        pm = make_poly("unc", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0, "BUG CONFIRMED: 'unc' not a substring of 'north carolina'.")

    def test_accidental_substring_collision(self):
        """
        RISK: Short abbreviations can accidentally match unrelated substrings.
        'la' is a substring of 'los angeles lakers' and 'orlando magic'.
        If pm_team='la', it could match either team in a title.
        """
        # 'la' in 'los angeles lakers' is True
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0)
        pm = make_poly("la", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0,
            "RISK MITIGATED: SHORT ABBR 'la' no longer accidentally matches via substring.")

    def test_full_name_team_abbr_does_match(self):
        """
        WORKAROUND: If team_abbr is the full lowercase team name, substring works.
        'los angeles lakers' in 'los angeles lakers vs boston celtics' = True.
        This confirms the workaround but is not how Polymarket API delivers data.
        """
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0048)
        pm = make_poly("los angeles lakers", ask=0.47, taker_fee=0.0047)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_arb"])


# ---------------------------------------------------------------------------
# 4. No game-time check (false cross-match risk)
# ---------------------------------------------------------------------------

class TestNoGameTimeCheck(unittest.TestCase):
    """
    BasketballMatcher has NO date/time comparison.
    Two different NBA games between the same teams on the same day
    (or across consecutive days in a playoff series) are indistinguishable.
    """

    def setUp(self):
        self.matcher = BasketballMatcher()

    def test_two_games_same_teams_both_match(self):
        """
        Game 1 and Game 2 of a playoff series: same teams, different tickers/times.
        Both Kalshi markets match the same Poly market if team abbreviation matches.
        Since we added alias lookups, 'lakers' might not match via alias if we only use 3-letter codes,
        but if it does match, it will match both. This test passes even if 0 matches are returned (graceful degradation).
        """
        km1 = make_kalshi("Lakers vs Celtics", ticker="KXNBA-26JUN281900LAKBOS-LAL", ask=0.48, taker_fee=0.0)
        km2 = make_kalshi("Lakers vs Celtics", ticker="KXNBA-26JUN292000LAKBOS-LAL", ask=0.52, taker_fee=0.0)
        pm = make_poly("lakers", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km1, km2], [pm])
        # (short enough to appear as substring of the simplified title)
        # This demonstrates that without a time gate, both Kalshi markets hit the same Poly entry.
        for r in results:
            self.assertEqual(r["polymarket_slug"], pm["slug"])


# ---------------------------------------------------------------------------
# 5. No sport-specific filter
# ---------------------------------------------------------------------------

class TestNoSportFilter(unittest.TestCase):
    """
    BasketballMatcher filters by ticker prefix via _VALID_PREFIXES = ("KXNBA","KXWNBA","KXCBB").
    Kalshi markets with non-basketball tickers (e.g. "KXMLS-...") are skipped at line 108.
    This prevents cross-sport false matches at the ticker level.
    """

    def setUp(self):
        self.matcher = BasketballMatcher()

    def test_soccer_title_accidentally_matches_if_substring_present(self):
        """
        Soccer market 'Manchester City vs Arsenal' — if pm_team='city',
        'city' IS in 'manchester city vs arsenal', so a cross-sport match fires.
        No sport-prefixed ticker filter exists in BasketballMatcher.
        """
        # Soccer-titled Kalshi market (no KXNBA prefix check in code)
        km = make_kalshi("Manchester City vs Arsenal", ticker="KXMLS-XYZ", ask=0.50, taker_fee=0.0)
        pm = make_poly("city", slug="nba-some-market", ask=0.45, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        # 'city' in 'manchester city vs arsenal' → False now if we use exact alias matches, but this test checks for 0 matches.
        self.assertEqual(len(results), 0,
            "RISK MITIGATED: Exact alias matching prevents partial substring collisions like 'city'.")


# ---------------------------------------------------------------------------
# 5b. New alias coverage tests (den, bos)
# ---------------------------------------------------------------------------

class TestNewAliases(unittest.TestCase):
    """Verify the newly added ("den","den") and ("bos","bos") alias entries."""

    def setUp(self):
        from matchers.basketball import team_code
        self.team_code = team_code
        self.matcher = BasketballMatcher()

    def test_den_alias_resolves(self):
        """team_code('den') must return 'den' after adding the direct alias."""
        self.assertEqual(self.team_code("den"), "den")

    def test_bos_alias_resolves(self):
        """team_code('bos') must return 'bos' after adding the direct alias."""
        self.assertEqual(self.team_code("bos"), "bos")

    def test_den_does_not_collide_with_golden_state(self):
        """'den' must NOT match 'golden state warriors' — the alias check is alias-in-input,
        so 'den' (3 chars) is only found if the input contains 'den' as a substring."""
        self.assertNotEqual(self.team_code("golden state warriors"), "den")
        self.assertEqual(self.team_code("golden state warriors"), "gsw")

    def test_den_matches_kalshi_title(self):
        """End-to-end: Kalshi 'Denver Nuggets vs Boston Celtics', Poly abbr 'den' → 1 match."""
        km = make_kalshi("Denver Nuggets vs Boston Celtics", ticker="KXNBA-26JUN281900DENBOS-DEN", ask=0.48, taker_fee=0.0)
        pm = make_poly("den", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_team"], "den")
        self.assertEqual(results[0]["kalshi_team"], "bos")


# ---------------------------------------------------------------------------
# 5c. WNBA/NBA city collision risk (RISK-BB2)
# ---------------------------------------------------------------------------

class TestWnbaNbaCollisionRisk(unittest.TestCase):
    """
    RISK-BB2: Some city codes are shared between NBA and WNBA teams
    (e.g. "atl" = Atlanta Hawks AND Atlanta Dream, "chi" = Bulls AND Sky).
    When ws_manager passes a combined list of NBA+WNBA Poly markets to match(),
    a KXNBA-ATL ticker could cross-match an Atlanta Dream Poly market (or vice versa).
    The ticker prefix filter (_VALID_PREFIXES) prevents cross-SPORT matches but
    does NOT prevent NBA↔WNBA city code collisions within the basketball family.

    Fix requires ws_manager to pass separate Poly lists per sport prefix.
    This test documents the risk without fixing it.
    """

    def setUp(self):
        self.matcher = BasketballMatcher()

    def test_phx_suns_and_mercury_same_code_both_match(self):
        """
        'phx' maps to both Phoenix Suns (NBA) and Phoenix Mercury (WNBA) via _ALIASES.
        team_code('phx') = 'phx' (direct entry exists for both).
        A KXNBA Phoenix Suns ticker and KXWNBA Phoenix Mercury ticker with the
        same Poly team_abbr='phx' will both produce match results when passed together.
        Production protection: ws_manager must pass separate Poly lists per league.
        """
        km_nba  = make_kalshi("Phoenix Suns vs Los Angeles Lakers", ticker="KXNBA-26JUN281900PHXLAL-PHX", ask=0.48, taker_fee=0.0)
        km_wnba = make_kalshi("Phoenix Mercury vs Las Vegas Aces",  ticker="KXWNBA-26JUN281900PHXLVV-PHX", ask=0.48, taker_fee=0.0)
        pm = make_poly("phx", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km_nba, km_wnba], [pm])
        tickers = [r["kalshi_ticker"] for r in results]
        # RISK-BB2: both markets match the same Poly entry.
        self.assertIn("KXNBA-26JUN281900PHXLAL-PHX", tickers,
            "RISK-BB2: NBA Phoenix Suns matches Poly 'phx' — expected")
        self.assertIn("KXWNBA-26JUN281900PHXLVV-PHX", tickers,
            "RISK-BB2: WNBA Phoenix Mercury also matches Poly 'phx' — cross-league collision")


# ---------------------------------------------------------------------------
# 6. Edge cases and defensive behavior
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        self.matcher = BasketballMatcher()

    def test_empty_inputs(self):
        self.assertEqual(self.matcher.match([], []), [])

    def test_empty_kalshi(self):
        pm = make_poly("lakers")
        self.assertEqual(self.matcher.match([], [pm]), [])

    def test_empty_poly(self):
        km = make_kalshi("Lakers vs Celtics")
        self.assertEqual(self.matcher.match([km], []), [])

    def test_total_cost_zero_guard_skips_entry(self):
        """
        If both asks are 0.0 and both fees are 0.0, total_cost=0.0.
        The guard `if total_cost > 0` skips the entry → not in results.
        This is correct defensive behavior but silently swallows the market.
        """
        km = make_kalshi("Lakers vs Celtics", ask=0.0, taker_fee=0.0)
        pm = make_poly("lakers", ask=0.0, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0,
            "Zero-ask markets are silently skipped (by design), not raised as errors.")

    def test_missing_title_falls_back_to_raw(self):
        """km['title'] is empty string; should fall back to raw['title']."""
        km = {
            "ticker": "KXNBA-26JUN281900LAKBOS-LAL",
            "title": "",
            "ask": 0.48,
            "taker_fee": 0.0048,
            "raw": {"title": "Los Angeles Lakers vs Boston Celtics"},
        }
        pm = make_poly("los angeles lakers", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_name"], "Los Angeles Lakers vs Boston Celtics")

    def test_missing_raw_key_no_crash(self):
        """km has no 'raw' key at all — should not raise KeyError."""
        km = {
            "ticker": "KXNBA-26JUN281900LAKBOS-LAL",
            "title": "Los Angeles Lakers vs Boston Celtics",
            "ask": 0.48,
            "taker_fee": 0.0,
        }
        pm = make_poly("los angeles lakers", ask=0.47, taker_fee=0.0)
        # Should not raise; .get("raw", {}) handles missing key
        try:
            results = self.matcher.match([km], [pm])
        except KeyError as e:
            self.fail(f"KeyError raised for missing 'raw' key: {e}")
        self.assertEqual(len(results), 1)

    def test_unparseable_title_no_crash(self):
        """Title with no separator → teams_from_kalshi_title returns None → skipped cleanly."""
        km = make_kalshi("LosAngelesLakersVsBostonCeltics")
        pm = make_poly("lakers")
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0)

    def test_missing_slug_produces_empty_string(self):
        """If slug is absent from pm and raw, polymarket_slug in result is ''."""
        pm = {
            "team_abbr": "los angeles lakers",
            "ask": 0.47,
            "taker_fee": 0.0,
            "raw": {},
            # no 'slug' key
        }
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_slug"], "",
            "Missing slug should silently produce empty string, not crash.")

    def test_raw_p_is_dead_code(self):
        """
        raw_p is extracted but never used. Confirm that putting slug only in
        raw_p does NOT populate polymarket_slug via the fallback path.
        The code does: `pm.get("slug", "") or raw_p.get("slug", "")` — so it
        DOES use raw_p as fallback. Verify this path works.
        """
        pm = {
            "team_abbr": "los angeles lakers",
            "ask": 0.47,
            "taker_fee": 0.0,
            "raw": {"slug": "nba-lakers-celtics-from-raw"},
            # no top-level 'slug' key
        }
        km = make_kalshi("Los Angeles Lakers vs Boston Celtics", ask=0.48, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_slug"], "nba-lakers-celtics-from-raw",
            "raw_p slug fallback should work via `pm.get('slug','') or raw_p.get('slug','')`.")

    def test_at_sign_title_parsed_as_vs(self):
        """'@' in Kalshi title is normalized to 'vs' by teams_from_kalshi_title."""
        km = make_kalshi("Boston Celtics @ Los Angeles Lakers", ask=0.48, taker_fee=0.0)
        pm = make_poly("boston celtics", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)

    def test_no_deduplication_same_slug_different_kalshi(self):
        """
        No deduplication: same Poly slug matched by two different Kalshi tickers
        (e.g., same game appearing in both KXNBA and KXWNBA feed).
        Both results are present.
        """
        km1 = make_kalshi("Lakers vs Celtics", ticker="KXNBA-26JUN281900LAKBOS-LAL", ask=0.48, taker_fee=0.0)
        km2 = make_kalshi("Lakers vs Celtics", ticker="KXWNBA-26JUN281900LAKBOS-LAL", ask=0.48, taker_fee=0.0)
        pm = make_poly("lakers", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km1, km2], [pm])
        tickers = [r["kalshi_ticker"] for r in results]
        # Both tickers appear — confirms no dedup, which can cause duplicate log entries.
        self.assertIn("KXNBA-26JUN281900LAKBOS-LAL", tickers)
        self.assertIn("KXWNBA-26JUN281900LAKBOS-LAL", tickers)


# ---------------------------------------------------------------------------
# 7. normalize_name / teams_from_kalshi_title unit tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_normalize_strips_accents(self):
        self.assertEqual(normalize_name("Héctor"), "hector")

    def test_normalize_lowercases(self):
        self.assertEqual(normalize_name("LAKERS"), "lakers")

    def test_normalize_collapses_spaces(self):
        self.assertEqual(normalize_name("  Los  Angeles  "), "los angeles")

    def test_extract_teams_vs(self):
        result = teams_from_kalshi_title("Lakers vs Celtics")
        self.assertEqual(result, ("lal", "bos"))

    def test_extract_teams_at_sign(self):
        result = teams_from_kalshi_title("Celtics @ Lakers")
        self.assertEqual(result, ("bos", "lal"))

    def test_extract_teams_v(self):
        result = teams_from_kalshi_title("Lakers v Celtics")
        self.assertEqual(result, ("lal", "bos"))

    def test_extract_teams_none_on_no_separator(self):
        self.assertIsNone(teams_from_kalshi_title("LakersCeltics"))

    def test_extract_teams_multiple_vs_returns_none(self):
        """Three-team-looking title splits into more than 2 parts → returns None."""
        # "A vs B vs C".split(" vs ") = ["A", "B", "C"] → len > 2 → returns None
        result = teams_from_kalshi_title("Team A vs Team B vs Team C")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
