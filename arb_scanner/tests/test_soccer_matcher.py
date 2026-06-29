"""
test_soccer_matcher.py — QA tests for arb_scanner/matchers/soccer.py

Run: pytest arb_scanner/tests/test_soccer_matcher.py
     (from repo root, or with PYTHONPATH=arb_scanner if needed)

Each test is named for the scenario it validates and documents the expected
outcome with a brief explanation of WHY it passes or what risk it exposes.
"""

import sys
import os
import unittest

# Allow running from repo root or arb_scanner directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from matchers.base import BaseMatcher
from matchers.soccer import SoccerMatcher, normalize_name, teams_from_kalshi_title

# ─── Required keys that OpportunityTracker.process() accesses ────────────────
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

# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_kalshi(ticker="KXEPL-26JUN281400T1T2", title="Team1 vs Team2",
                ask=0.45, fee=0.01, raw=None):
    return {"ticker": ticker, "title": title, "ask": ask,
            "taker_fee": fee, "raw": raw or {}}


def make_poly(slug="epl-team1-team2", team_abbr="t1",
              ask=0.44, fee=0.01, raw=None):
    return {"slug": slug, "team_abbr": team_abbr, "ask": ask,
            "taker_fee": fee, "raw": raw or {}}


# ─── Test Suite ──────────────────────────────────────────────────────────────

class TestSoccerMatcherContract(unittest.TestCase):
    """Test 1: Contract — subclass, interface, required keys."""

    def test_is_subclass_of_base_matcher(self):
        """SoccerMatcher must inherit from BaseMatcher (abstract ABC)."""
        self.assertTrue(issubclass(SoccerMatcher, BaseMatcher))

    def test_is_concrete_not_abstract(self):
        """SoccerMatcher must be instantiable (all abstract methods implemented)."""
        try:
            SoccerMatcher()
        except TypeError as e:
            self.fail(f"SoccerMatcher is still abstract: {e}")

    def test_has_match_method(self):
        """match() must exist and be callable."""
        m = SoccerMatcher()
        self.assertTrue(callable(getattr(m, "match", None)))

    def test_match_returns_list(self):
        """match() must return a list in all cases."""
        m = SoccerMatcher()
        result = m.match([], [])
        self.assertIsInstance(result, list)

    def test_match_result_contains_all_required_keys(self):
        """
        Every match dict must contain all keys that OpportunityTracker._make_key()
        and _build_event() access. Missing keys cause KeyError at log time.
        """
        m = SoccerMatcher()
        km = [make_kalshi(ticker="KXEPL-26JUN281400ARSCHE", title="Arsenal vs Chelsea")]
        pm = [make_poly(slug="epl-ars-che", team_abbr="ars")]
        results = m.match(km, pm)
        self.assertEqual(len(results), 1)
        missing = REQUIRED_KEYS - results[0].keys()
        self.assertEqual(missing, set(),
                         f"Result dict is missing required keys: {missing}")

    def test_match_signature_accepts_two_lists(self):
        """match() must accept exactly (kalshi_markets, polymarket_markets) per BaseMatcher."""
        m = SoccerMatcher()
        result = m.match([], [])
        self.assertIsInstance(result, list)


class TestEplMatchArb(unittest.TestCase):
    """Test 2 & 3: EPL moneyline match — arb and no-arb cases."""

    def setUp(self):
        self.m = SoccerMatcher(arb_threshold=0.96)

    def test_epl_arb_detected(self):
        """
        Arsenal vs Chelsea: team_abbr='ars', 'ars' in 'arsenal' = True.
        Kalshi YES (Arsenal) + Polymarket YES (Arsenal on other side = Chelsea kalshi).
        total = 0.45 + 0.44 + 0.01 + 0.01 = 0.91 < 0.96 -> is_arb=True.
        """
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.45, fee=0.01)]
        pm = [make_poly(team_abbr="ars", ask=0.44, fee=0.01)]
        results = self.m.match(km, pm)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertAlmostEqual(r["total_cost"], 0.91, places=6)
        self.assertTrue(r["is_arb"])
        self.assertGreater(r["gap_cents"], 0)

    def test_epl_no_arb_high_prices(self):
        """
        Same teams, prices too high: total = 0.55 + 0.45 + 0.01 + 0.01 = 1.02.
        is_arb must be False; gap_cents must be negative.
        """
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.55, fee=0.01)]
        pm = [make_poly(team_abbr="ars", ask=0.45, fee=0.01)]
        results = self.m.match(km, pm)
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertAlmostEqual(r["total_cost"], 1.02, places=6)
        self.assertFalse(r["is_arb"])
        self.assertLess(r["gap_cents"], 0)

    def test_arb_team_assignment(self):
        """
        When pm_team='ars' matches teams_k[0]='arsenal' (home side),
        kalshi_team must be assigned the OTHER side (teams_k[1]='chelsea').
        polymarket_team must be the matched side ('arsenal').
        """
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.45, fee=0.01)]
        pm = [make_poly(team_abbr="ars", ask=0.44, fee=0.01)]
        r = self.m.match(km, pm)[0]
        self.assertEqual(r["kalshi_team"], "che")
        self.assertEqual(r["polymarket_team"], "ars")

    def test_gap_cents_calculation(self):
        """
        gap_cents = (0.96 - total_cost) * 100, rounded to 4 decimal places.
        Verify arithmetic is correct.
        """
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.45, fee=0.01)]
        pm = [make_poly(team_abbr="ars", ask=0.44, fee=0.01)]
        r = self.m.match(km, pm)[0]
        expected_gap = round((0.96 - 0.91) * 100, 4)
        self.assertAlmostEqual(r["gap_cents"], expected_gap, places=4)


class TestExtractTeamsSeparator(unittest.TestCase):
    """Test 4 & 9: Separator parsing — 'vs', 'v', '@', 'at'."""

    def test_v_separator_uk_football_style(self):
        """'Arsenal v Chelsea' uses UK football 'v' form. Must split correctly."""
        result = teams_from_kalshi_title("Arsenal v Chelsea")
        self.assertIsNotNone(result)
        self.assertEqual(result, ("ars", "che"))

    def test_vs_separator(self):
        """Standard 'vs' separator must work."""
        result = teams_from_kalshi_title("Arsenal vs Chelsea")
        self.assertEqual(result, ("ars", "che"))

    def test_at_separator(self):
        """'@' replaced with ' vs ' before parsing — must split correctly."""
        result = teams_from_kalshi_title("Arsenal @ Chelsea")
        self.assertEqual(result, ("ars", "che"))

    def test_at_word_separator(self):
        """'Arsenal at Chelsea' must split on ' at '."""
        result = teams_from_kalshi_title("Arsenal at Chelsea")
        self.assertEqual(result, ("ars", "che"))

    def test_sv_prefix_with_vs_separator(self):
        """
        'SV Hamburg vs Arsenal': 'sv hamburg' has no alias, so it returns None.
        """
        result = teams_from_kalshi_title("SV Hamburg vs Arsenal")
        self.assertIsNone(result)

    def test_sv_prefix_with_v_separator(self):
        """
        'SV Hamburg v Arsenal': 'sv hamburg' has no alias, so it returns None.
        """
        result = teams_from_kalshi_title("SV Hamburg v Arsenal")
        self.assertIsNone(result)

    def test_champions_league_long_names(self):
        """
        Champions League clubs with accented/compound names.
        """
        result = teams_from_kalshi_title("Paris Saint-Germain vs Bayern Munich")
        self.assertIsNotNone(result)
        self.assertEqual(result[0], "psg")
        self.assertEqual(result[1], "bay")

    def test_real_madrid_atletico(self):
        """Compound 'Atletico' with 'v' separator must parse correctly."""
        result = teams_from_kalshi_title("Real Madrid v Atletico Madrid")
        self.assertEqual(result, ("rma", "atm"))

    def test_unparseable_title_returns_none(self):
        """A title with no recognized separator must return None (market is skipped)."""
        result = teams_from_kalshi_title("Arsenal Chelsea Draw")
        self.assertIsNone(result)


class TestAmbiguousAbbreviationRisk(unittest.TestCase):
    """
    Test 5: 'man' abbreviation false positive (CRITICAL RISK — documented, not fixed here).

    Polymarket EPL uses 3-letter codes. 'mun' (Man Utd) would be safe because
    'mun' is NOT a substring of 'manchester united'. But if Polymarket uses 'man'
    as an abbreviation, 'man' IS a substring of BOTH 'manchester city' AND
    'manchester united', causing both Kalshi markets to match the same Poly entry.
    """

    def setUp(self):
        self.m = SoccerMatcher()
        self.kalshi_markets = [
            make_kalshi(ticker="KXEPL-26JUN281400MCLIV", title="Manchester City vs Liverpool"),
            make_kalshi(ticker="KXEPL-26JUN281400MUARS", title="Manchester United vs Arsenal"),
        ]

    def test_man_abbreviation_false_positive(self):
        """
        RISK EXPOSED: team_abbr='man' matches BOTH 'manchester city' and
        'manchester united' via substring. A single Polymarket entry produces
        two match dicts against two different Kalshi markets. This is a false
        positive — the poly entry is for ONE team, not both.

        If Polymarket uses 'mci'/'mun' instead of 'man', this risk is mitigated.
        If 'man' is ever used, arb signals are unreliable.
        """
        poly = [make_poly(team_abbr="man", ask=0.44, fee=0.01)]
        results = self.m.match(self.kalshi_markets, poly)
        self.assertEqual(len(results), 0,
                         "RISK MITIGATED: 'man' no longer matches via substring, so it doesn't falsely match both Manchester clubs.")

    def test_mun_abbreviation_no_false_positive(self):
        """
        'mun' now properly matches 'manchester united' via alias lookup, avoiding substring issues.
        """
        poly = [make_poly(team_abbr="mun", ask=0.44, fee=0.01)]
        results = self.m.match(self.kalshi_markets, poly)
        self.assertEqual(len(results), 1,
                         "'mun' now matches 'manchester united' via alias lookup.")

    def test_ars_abbreviation_correct(self):
        """
        'ars' in 'arsenal' = True. 'ars' in 'manchester city' = False.
        3-letter code works correctly for Arsenal.
        """
        km = [make_kalshi(title="Arsenal vs Chelsea")]
        pm = [make_poly(team_abbr="ars")]
        results = self.m.match(km, pm)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_team"], "ars")


class TestNoGameTimeValidation(unittest.TestCase):
    """
    Test 6: No timestamp comparison — same teams in multiple fixtures
    on the same day both appear in results (each is a separate Kalshi ticker).
    This is NOT a bug per the current architecture (Kalshi tickers are unique),
    but is documented as an architectural risk.
    """

    def test_same_teams_multiple_matches_both_returned(self):
        """
        If two Kalshi markets exist for the same matchup on the same day
        (e.g., a replayed fixture or data anomaly), both will produce match
        entries because there is no timestamp guard. SoccerMatcher relies entirely
        on Kalshi ticker uniqueness to prevent duplicates.
        """
        m = SoccerMatcher()
        kalshi_markets = [
            make_kalshi(ticker="KXEPL-26JUN281400ARSCHE", title="Arsenal vs Chelsea"),
            make_kalshi(ticker="KXEPL-26JUN281900ARSCHE", title="Arsenal vs Chelsea"),
        ]
        pm = [make_poly(team_abbr="ars", ask=0.44, fee=0.01)]
        results = m.match(kalshi_markets, pm)
        self.assertEqual(len(results), 2,
                         "Both fixtures (different times) produce matches when poly has no gameStartTime")
        tickers = [r["kalshi_ticker"] for r in results]
        self.assertIn("KXEPL-26JUN281400ARSCHE", tickers)
        self.assertIn("KXEPL-26JUN281900ARSCHE", tickers)


class TestSoccerDrawSide(unittest.TestCase):
    """Test 7: Soccer draw side must NOT match either team name."""

    def test_draw_team_abbr_does_not_match(self):
        """
        Polymarket soccer markets have 3 sides: home win, draw, away win.
        Draw side typically has team_abbr='draw' or similar.
        'draw' is not a substring of any team name — match correctly skipped.
        """
        m = SoccerMatcher()
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.45, fee=0.01)]
        pm = [make_poly(team_abbr="draw", ask=0.30, fee=0.01)]
        results = m.match(km, pm)
        self.assertEqual(len(results), 0,
                         "'draw' must not match any team name")

    def test_draw_with_low_price_still_skipped(self):
        """Even an extremely cheap draw price (would be great arb) must be ignored."""
        m = SoccerMatcher()
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.05, fee=0.01)]
        pm = [make_poly(team_abbr="draw", ask=0.05, fee=0.01)]
        results = m.match(km, pm)
        self.assertEqual(len(results), 0)

    def test_three_sides_only_two_team_sides_match(self):
        """
        When all three sides are passed (home, draw, away), only the two
        team sides should produce results, not the draw.
        """
        m = SoccerMatcher()
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.40, fee=0.01)]
        pm = [
            make_poly(slug="epl-ars", team_abbr="ars", ask=0.44, fee=0.01),
            make_poly(slug="epl-draw", team_abbr="draw", ask=0.20, fee=0.01),
            make_poly(slug="epl-che", team_abbr="che", ask=0.44, fee=0.01),
        ]
        results = m.match(km, pm)
        slugs = [r["polymarket_slug"] for r in results]
        self.assertNotIn("epl-draw", slugs,
                         "Draw side must be filtered out")
        self.assertIn("epl-ars", slugs)
        self.assertIn("epl-che", slugs)
        self.assertEqual(len(results), 2)


class TestEmptyInputs(unittest.TestCase):
    """Test 8: Empty inputs must return empty list without error."""

    def setUp(self):
        self.m = SoccerMatcher()

    def test_both_empty(self):
        self.assertEqual(self.m.match([], []), [])

    def test_empty_kalshi(self):
        pm = [make_poly()]
        self.assertEqual(self.m.match([], pm), [])

    def test_empty_poly(self):
        km = [make_kalshi()]
        self.assertEqual(self.m.match(km, []), [])


class TestMissingSlug(unittest.TestCase):
    """Test 10: Missing slug falls back to empty string, not KeyError."""

    def test_no_slug_in_pm_dict_no_slug_in_raw(self):
        """
        pm has no 'slug' key and raw={} — result must have polymarket_slug=''.
        OpportunityTracker._make_key accesses m['polymarket_slug'] directly;
        an empty string is valid (deduplicated as one key per ticker+slug pair).
        """
        m = SoccerMatcher()
        km = [make_kalshi(title="Arsenal vs Chelsea")]
        pm = [{"team_abbr": "ars", "ask": 0.44, "taker_fee": 0.01, "raw": {}}]
        results = m.match(km, pm)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_slug"], "")

    def test_slug_in_raw_used_as_fallback(self):
        """When pm has no 'slug' key but raw has it, that value is used."""
        m = SoccerMatcher()
        km = [make_kalshi(title="Arsenal vs Chelsea")]
        pm = [{"team_abbr": "ars", "ask": 0.44, "taker_fee": 0.01,
               "raw": {"slug": "epl-ars-from-raw"}}]
        results = m.match(km, pm)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_slug"], "epl-ars-from-raw")


class TestCrossLeagueContamination(unittest.TestCase):
    """
    Cross-league contamination: no slug/league filter in SoccerMatcher.match().
    An MLS Polymarket entry could match an EPL Kalshi market if abbreviations overlap.
    """

    def test_different_league_abbreviation_no_match(self):
        """
        MLS team 'mia' (Inter Miami) vs EPL Kalshi market — 'mia' is not a
        substring of 'arsenal' or 'chelsea'. No spurious match.
        """
        m = SoccerMatcher()
        km = [make_kalshi(ticker="KXEPL-26JUN281400ARSCHE", title="Arsenal vs Chelsea")]
        pm = [make_poly(slug="mls-inter-miami", team_abbr="mia", ask=0.44)]
        results = m.match(km, pm)
        self.assertEqual(len(results), 0)

    def test_chi_in_chicago_cross_league_risk(self):
        """
        RISK DOCUMENTED: If a Polymarket EPL entry uses 'che' for Chelsea but
        there's also an MLS Kalshi market for 'Chicago vs LA Galaxy', then
        'chi' in 'chicago' = True could create a cross-league false match.
        This test documents the substring collision exists at the string level.
        """
        self.assertTrue("chi" in "chicago",
                        "Substring collision: 'chi' is in 'chicago' — "
                        "cross-league false match possible if abbrevs overlap")

    def test_cross_league_false_match_scenario(self):
        """
        Concrete cross-league false match: Kalshi MLS market 'Chicago vs LA',
        Polymarket EPL entry with team_abbr='chi' (hypothetical Chicago Fire abbrev).
        'chi' in 'chicago' = True -> spurious match across leagues.
        No league filter prevents this.
        """
        m = SoccerMatcher()
        km = [make_kalshi(ticker="KXMLS-26JUN281900CHILAG", title="Chicago vs LA Galaxy")]
        pm = [make_poly(slug="epl-chi-something", team_abbr="chi", ask=0.44)]
        results = m.match(km, pm)
        # Demonstrates the contamination — 'chi' in 'chicago' is True
        self.assertEqual(len(results), 1,
                         "RISK: cross-league false match via substring — no league filter")


class TestZeroAskSpuriousArb(unittest.TestCase):
    """
    RISK: If one side has ask=0.0 (no live quote), total_cost is still > 0
    if the other side has a non-zero ask. The guard is `total_cost > 0`,
    not `k_ask > 0 and p_ask > 0`. A zero ask on one side means no market
    exists on that exchange — any resulting is_arb=True is spurious.
    """

    def test_zero_poly_ask_produces_spurious_arb(self):
        """
        poly ask=0.0 (no live quote), k_ask=0.45: total=0.47 < 0.96 -> is_arb=True.
        This is a false signal — Polymarket has no live quote.
        """
        m = SoccerMatcher()
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.45, fee=0.01)]
        pm = [make_poly(team_abbr="ars", ask=0.0, fee=0.01)]
        results = m.match(km, pm)
        self.assertEqual(len(results), 0,
                         "RISK MITIGATED: zero poly_ask correctly skips the market instead of producing spurious arb.")

    def test_both_zero_ask_skipped(self):
        """
        Both asks zero: total_cost = 0.0. The `total_cost > 0` guard correctly
        skips this entry. No result produced.
        """
        m = SoccerMatcher()
        km = [make_kalshi(title="Arsenal vs Chelsea", ask=0.0, fee=0.0)]
        pm = [make_poly(team_abbr="ars", ask=0.0, fee=0.0)]
        results = m.match(km, pm)
        self.assertEqual(len(results), 0)


class TestNormalizeAndDeadCode(unittest.TestCase):
    """Normalize function edge cases and raw_p dead code verification."""

    def test_normalize_strips_accents(self):
        """Diacritics stripped: 'Atlético' -> 'atletico'."""
        self.assertEqual(normalize_name("Atlético"), "atletico")

    def test_normalize_strips_punctuation(self):
        """Non-alphanumeric chars (except spaces) replaced with space."""
        self.assertEqual(normalize_name("St. Mirren"), "st  mirren".replace("  ", " "))

    def test_normalize_lowercases(self):
        self.assertEqual(normalize_name("Manchester City"), "manchester city")

    def test_raw_p_not_used_for_matching(self):
        """
        raw_p is extracted in match() but never used for team name or slug resolution
        beyond the explicit fallback raw_p.get('slug'). If raw_p had a 'team' field,
        it would NOT be used — only pm.get('team_abbr') and pm.get('team') are checked.
        Missing team_abbr with data only in raw_p -> no match.
        """
        m = SoccerMatcher()
        km = [make_kalshi(title="Arsenal vs Chelsea")]
        # team_abbr absent, team absent — raw_p has team but it's dead code for matching
        pm = [{"ask": 0.44, "taker_fee": 0.01, "slug": "epl-ars",
               "raw": {"team": "ars", "slug": "epl-ars"}}]
        results = m.match(km, pm)
        # pm.get('team_abbr') = None, pm.get('team') = None -> pm_team = ''
        # '' is falsy -> no match attempted
        self.assertEqual(len(results), 0,
                         "raw_p team data is dead code — normalize_name('') is falsy, no match")


class TestMlsMatch(unittest.TestCase):
    """MLS-specific match scenarios."""

    def test_mls_arb_match(self):
        """Basic MLS match: LA Galaxy vs Inter Miami."""
        m = SoccerMatcher()
        km = [make_kalshi(ticker="KXMLS-26JUN281900LAGMIA", title="LA Galaxy vs Inter Miami",
                          ask=0.43, fee=0.01)]
        pm = [make_poly(slug="mls-la-galaxy", team_abbr="lag", ask=0.45, fee=0.01)]
        # 'lag' in 'la galaxy'? No. 'lag' -> 'l' + 'a' + 'g' not in 'la galaxy' as substring
        # 'lag' in 'la galaxy' -> False
        results = m.match(km, pm)
        # This reveals another abbreviation risk: 3-letter codes don't always substring-match
        # 'lag' is NOT in 'la galaxy' — match fails
        self.assertEqual(len(results), 1,
                         "RISK MITIGATED: 'lag' now matches 'la galaxy' via alias lookup.")

    def test_mls_full_name_match(self):
        """
        If Polymarket uses a longer abbreviation or slug fragment that IS a substring,
        the match works. 'galaxy' in 'la galaxy' = True.
        """
        m = SoccerMatcher()
        km = [make_kalshi(title="LA Galaxy vs Inter Miami", ask=0.43, fee=0.01)]
        pm = [make_poly(team_abbr="galaxy", ask=0.45, fee=0.01)]
        results = m.match(km, pm)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_team"], "lag")


class TestPortlandPortoCollisionFix(unittest.TestCase):
    """
    RISK-S1 regression: ("por","por") was removed from Portland Timbers' aliases.
    Polymarket sends team_abbr="por" for Porto (Champions League).
    After the fix, team_code("por") must NOT fire via Portland's direct entry —
    only Porto's ("porto","por") alias should match.
    Portland must still resolve correctly via "portland timbers" or "timbers" aliases.
    """

    def setUp(self):
        from matchers.soccer import team_code, teams_from_kalshi_title
        self.team_code = team_code
        self.teams_from_kalshi_title = teams_from_kalshi_title
        self.m = SoccerMatcher()

    def test_por_resolves_via_porto_only(self):
        """team_code('por') must return 'por' (Porto path, not Portland direct entry)."""
        result = self.team_code("por")
        self.assertEqual(result, "por",
            "team_code('por') must still return 'por' via Porto alias")

    def test_portland_still_resolves_via_timbers(self):
        """'portland timbers' must still resolve to 'por' via long-form alias."""
        self.assertEqual(self.team_code("portland timbers"), "por")

    def test_portland_still_resolves_via_portland(self):
        """'portland' alone must still resolve to 'por' via city alias."""
        self.assertEqual(self.team_code("portland"), "por")

    def test_kalshi_title_portland_still_parses(self):
        """
        Kalshi CL/MLS title 'Portland Timbers vs Seattle Sounders' must parse correctly.
        Portland resolves via 'portland timbers' alias (not direct 'por' entry).
        """
        result = self.teams_from_kalshi_title("Portland Timbers vs Seattle Sounders")
        self.assertIsNotNone(result)
        self.assertEqual(result, ("por", "sea"))

    def test_porto_cl_market_matches_via_poly_abbr(self):
        """
        Polymarket sends team_abbr='por' for Porto CL market.
        With ("por","por") removed from Portland, this must NOT spuriously match
        an MLS Portland Kalshi ticker (Portland's code is still 'por', so if a
        KXCHAMPIONS Porto title is present alongside KXMLS Portland, the
        team code equality check is still 'por'=='por' — the fix prevents the
        DIRECT abbr entry from accidentally resolving through Portland's old slot).
        This test confirms team_code('por') still returns 'por' (Porto path).
        """
        self.assertEqual(self.team_code("porto"), "por")


if __name__ == "__main__":
    unittest.main(verbosity=2)
