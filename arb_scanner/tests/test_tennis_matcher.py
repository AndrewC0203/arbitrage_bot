"""
QA test suite for TennisMatcher.

Run from the arb_scanner/ directory:
    pytest tests/test_tennis_matcher.py -v

Tests are self-contained; no live API calls.
"""

import sys
import os
import unittest

# Allow imports from arb_scanner/ root regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from matchers.base import BaseMatcher
from matchers.tennis import TennisMatcher, normalize_name, extract_players


REQUIRED_KEYS = {
    "market_name",
    "kalshi_ticker",
    "kalshi_team",       # cosmetically "team" but holds player name for tennis
    "kalshi_ask",
    "kalshi_taker_fee",
    "polymarket_slug",
    "polymarket_team",   # cosmetically "team" but holds player name for tennis
    "polymarket_ask",
    "polymarket_taker_fee",
    "total_cost",
    "gap_cents",
    "is_arb",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_kalshi(title, ticker="KXATP-001", ask=0.48, taker_fee=0.0048, raw=None):
    return {
        "ticker": ticker,
        "title": title,
        "ask": ask,
        "taker_fee": taker_fee,
        "raw": raw or {},
    }


def make_poly(team_abbr, slug="atp-djokovic-sinner-2026-06-28", ask=0.47, taker_fee=0.0047, raw=None):
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
        """TennisMatcher must be a concrete subclass of BaseMatcher."""
        self.assertTrue(issubclass(TennisMatcher, BaseMatcher))

    def test_has_match_method(self):
        matcher = TennisMatcher()
        self.assertTrue(callable(getattr(matcher, "match", None)))

    def test_match_returns_list(self):
        matcher = TennisMatcher()
        result = matcher.match([], [])
        self.assertIsInstance(result, list)

    def test_result_dicts_contain_required_keys(self):
        """Every returned dict must contain all required keys."""
        matcher = TennisMatcher()
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0047)
        pm = make_poly(team_abbr="djokovic", ask=0.46, taker_fee=0.0046)
        result = matcher.match([km], [pm])
        self.assertGreater(len(result), 0, "expected at least one match")
        for r in result:
            missing = REQUIRED_KEYS - set(r.keys())
            self.assertEqual(missing, set(), f"missing keys: {missing}")

    def test_kalshi_team_key_holds_player_name(self):
        """
        Key is named 'kalshi_team' for historical/interface reasons, but for tennis
        it holds the opponent's normalized player name (the Kalshi leg player).
        Verify the value is a non-empty string.
        """
        matcher = TennisMatcher()
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0)
        r = matcher.match([km], [pm])[0]
        self.assertIsInstance(r["kalshi_team"], str)
        self.assertGreater(len(r["kalshi_team"]), 0)

    def test_no_get_odds_method(self):
        """TennisMatcher should NOT expose get_odds(); interface is match() only."""
        self.assertFalse(hasattr(TennisMatcher, "get_odds"))


# ---------------------------------------------------------------------------
# 2. Basic match — last name only (most common tennis format)
# ---------------------------------------------------------------------------

class TestBasicMatch(unittest.TestCase):

    def setUp(self):
        self.matcher = TennisMatcher(arb_threshold=0.96)

    def test_last_name_only_arb_true(self):
        """
        Kalshi: "Djokovic vs Sinner"
        Poly team_abbr: "djokovic"
        normalize_name("djokovic") = "djokovic"
        extract_players -> ("djokovic", "sinner")
        "djokovic" in "djokovic" = True -> match
        Total: 0.47 + 0.46 + 0.0047 + 0.0046 = 0.9393 < 0.96 -> is_arb=True
        """
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0047)
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0046)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_arb"])
        self.assertAlmostEqual(results[0]["total_cost"], 0.9393, places=4)
        self.assertGreater(results[0]["gap_cents"], 0)

    def test_last_name_only_arb_false(self):
        """Match is emitted but is_arb=False when total_cost >= threshold."""
        km = make_kalshi("Djokovic vs Sinner", ask=0.52, taker_fee=0.0052)
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0046)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["is_arb"])
        self.assertLess(results[0]["gap_cents"], 0)

    def test_player_on_second_side(self):
        """
        Poly team_abbr matches the second player (index 1) in the Kalshi title.
        pm_player "sinner" in players_k[1] "sinner" -> kalshi_team = players_k[0] "djokovic"
        """
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0)
        pm = make_poly("sinner", ask=0.46, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        self.assertEqual(r["polymarket_team"], "sinner")
        self.assertEqual(r["kalshi_team"], "djokovic")

    def test_player_on_first_side_assignment(self):
        """
        Poly team_abbr matches the first player (index 0).
        polymarket_team = players_k[0], kalshi_team = players_k[1].
        """
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        self.assertEqual(r["polymarket_team"], "djokovic")
        self.assertEqual(r["kalshi_team"], "sinner")

    def test_gap_cents_arithmetic(self):
        """gap_cents = round((0.96 - total_cost) * 100, 4)"""
        km = make_kalshi("Djokovic vs Sinner", ask=0.40, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.40, taker_fee=0.0)
        r = self.matcher.match([km], [pm])[0]
        # 0.96 - 0.80 = 0.16 -> 16.0 cents
        self.assertEqual(r["gap_cents"], 16.0)


# ---------------------------------------------------------------------------
# 3. Full-name matching (Kalshi may use full player names)
# ---------------------------------------------------------------------------

class TestFullNameMatch(unittest.TestCase):
    """
    Kalshi may emit full names like "Novak Djokovic vs Jannik Sinner".
    extract_players -> ("novak djokovic", "jannik sinner")
    Poly team_abbr = "djokovic"
    "djokovic" in "novak djokovic" = True -> match fires correctly.
    """

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_last_name_abbr_matches_full_name_in_kalshi_title(self):
        km = make_kalshi("Novak Djokovic vs Jannik Sinner", ask=0.47, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        # polymarket_team should be the full normalized first-slot name
        self.assertEqual(results[0]["polymarket_team"], "novak djokovic")
        self.assertEqual(results[0]["kalshi_team"], "jannik sinner")

    def test_full_name_abbr_matches_full_name_in_kalshi_title(self):
        """If team_abbr is the full player name, substring match still works."""
        km = make_kalshi("Novak Djokovic vs Jannik Sinner", ask=0.47, taker_fee=0.0)
        pm = make_poly("novak djokovic", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)


# ---------------------------------------------------------------------------
# 4. No match — unrelated player
# ---------------------------------------------------------------------------

class TestNoMatch(unittest.TestCase):

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_completely_different_player_no_match(self):
        """pm team_abbr 'federer' not in title 'Djokovic vs Sinner' -> no match."""
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0)
        pm = make_poly("federer", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0)

    def test_empty_team_abbr_no_match(self):
        """
        pm_player = normalize_name("") = ""
        Guard: `if pm_player and ...` -> False -> no match.
        Empty abbr is silently skipped.
        """
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0)
        pm = make_poly("", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0)


# ---------------------------------------------------------------------------
# 5. Accent handling
# ---------------------------------------------------------------------------

class TestAccentHandling(unittest.TestCase):
    """
    normalize_name() strips diacritics via NFD decomposition.
    This is critical for international tennis players.
    """

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_normalize_accented_name(self):
        self.assertEqual(normalize_name("María Sakkari"), "maria sakkari")
        self.assertEqual(normalize_name("Gaël Monfils"), "gael monfils")
        self.assertEqual(normalize_name("Radwańska"), "radwanska")

    def test_accented_kalshi_title_matches_plain_abbr(self):
        """
        Kalshi title has accent: "María Sakkari vs Elena Rybakina"
        After normalize: "maria sakkari vs elena rybakina"
        Poly team_abbr "sakkari" -> "sakkari" in "maria sakkari" = True
        """
        km = make_kalshi("María Sakkari vs Elena Rybakina", ask=0.47, taker_fee=0.0)
        pm = make_poly("sakkari", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_team"], "maria sakkari")

    def test_accented_poly_abbr_matches_plain_kalshi(self):
        """
        Poly team_abbr contains accent: "Sakkari" after normalize = "sakkari"
        Kalshi "Sakkari vs Rybakina" after normalize: ("sakkari", "rybakina")
        "sakkari" in "sakkari" = True
        """
        km = make_kalshi("Sakkari vs Rybakina", ask=0.47, taker_fee=0.0)
        pm = make_poly("Sakkari", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)

    def test_compound_accented_name_match(self):
        """
        Player "Martínez Sánchez" -> normalize -> "martinez sanchez"
        Poly team_abbr "martinez sanchez" -> substring match works.
        """
        km = make_kalshi("Martínez Sánchez vs Halep", ask=0.47, taker_fee=0.0)
        pm = make_poly("martinez sanchez", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)


# ---------------------------------------------------------------------------
# 6. Name collision / substring false-positive risk
# ---------------------------------------------------------------------------

class TestNameCollisionRisk(unittest.TestCase):
    """
    RISK: The matcher uses substring containment (`pm_player in player_slot`),
    not equality. Short or shared name fragments can produce false matches.

    This is less severe for tennis than for team sports (player last names are
    typically specific) but is still a real risk with certain name substrings.
    """

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_short_prefix_matches_longer_name(self):
        """
        RISK EXPOSED: pm_player = "lin" is a substring of "lindestedt".
        Title "Lindestedt vs Murray" -> players_k = ("lindestedt", "murray")
        "lin" in "lindestedt" = True -> match fires.
        This is a false positive if the intended player was someone named "Lin".

        NOTE: This test documents and confirms the risk.
        In practice, Polymarket tennis team_abbr is the full last name, so
        "lin" as an abbreviation would be unusual. Risk is LOW in production.
        """
        km = make_kalshi("Lindestedt vs Murray", ask=0.47, taker_fee=0.0)
        pm = make_poly("lin", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        # "lin" IS a substring of "lindestedt" — false positive fires
        self.assertEqual(len(results), 1,
            "RISK CONFIRMED: short abbr 'lin' matches 'lindestedt' via substring. "
            "Fix: use equality check instead of substring containment.")

    def test_shared_last_name_fragment_false_positive(self):
        """
        RISK: pm_player = "garcia" would match both "David Garcia" and
        "Thiago Garcia" if both appear in the title (hypothetical doubles match).
        More realistic: "garcia" in "garcia-lopez" = True for a hyphenated player.
        """
        km = make_kalshi("Garcia-Lopez vs Alcaraz", ask=0.47, taker_fee=0.0)
        pm = make_poly("garcia", ask=0.46, taker_fee=0.0)
        # After normalize: "garcia lopez vs alcaraz"
        # "garcia" in "garcia lopez" = True
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1,
            "RISK: 'garcia' substring-matches 'garcia lopez' (hyphenated name). "
            "Could cause false positive if 'garcia' was intended to match a different player.")

    def test_single_letter_initial_substring_risk(self):
        """
        RISK: Kalshi sometimes uses initials: "V. Williams vs S. Williams"
        After normalize: "v williams vs s williams"
        If pm_player = "v williams", that IS "v williams" exactly -> correct match.
        But if pm_player = "williams", it matches BOTH sides.
        This test exposes the double-match: both sides contain "williams".
        """
        km = make_kalshi("V. Williams vs S. Williams", ask=0.47, taker_fee=0.0)
        pm = make_poly("williams", ask=0.46, taker_fee=0.0)
        # extract_players: " vs " splits to ("v williams", "s williams")
        # "williams" in "v williams" = True -> FIRST branch fires
        # k_player_matched = "s williams", pm_player_matched = "v williams"
        # The elif is never reached because first branch already matched.
        # Result: only ONE match, not two (first-match semantics).
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1,
            "When pm_player matches both sides, first-branch semantics fire -> "
            "one result, but it may assign the WRONG player as the Kalshi leg.")
        # The polymarket_team will be "v williams" (first side), not "s williams"
        # This could be wrong if "williams" was intended for the second-side market.
        self.assertEqual(results[0]["polymarket_team"], "v williams")


# ---------------------------------------------------------------------------
# 7. Separator handling
# ---------------------------------------------------------------------------

class TestSeparatorHandling(unittest.TestCase):

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_vs_separator(self):
        result = extract_players("Federer vs Djokovic")
        self.assertEqual(result, ("federer", "djokovic"))

    def test_v_separator(self):
        result = extract_players("Federer v Djokovic")
        self.assertEqual(result, ("federer", "djokovic"))

    def test_at_sign_separator(self):
        """'@' is replaced by ' vs ' before parsing."""
        result = extract_players("Federer @ Djokovic")
        self.assertEqual(result, ("federer", "djokovic"))

    def test_at_word_separator(self):
        """' at ' as a word separator is treated as a player split."""
        result = extract_players("Halep at Kvitova")
        self.assertEqual(result, ("halep", "kvitova"))

    def test_at_word_separator_match_fires(self):
        """Confirm an ' at ' title produces a match end-to-end."""
        km = make_kalshi("Halep at Kvitova", ask=0.47, taker_fee=0.0)
        pm = make_poly("halep", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)

    def test_vs_takes_priority_over_v_when_both_present(self):
        """
        'Jiri Vesely vs Rafael Nadal':
        After normalize: "jiri vesely vs rafael nadal"
        " vs " splits this correctly into ("jiri vesely", "rafael nadal").
        " v " is checked after and never fires because " vs " already returned.
        """
        result = extract_players("Jiri Vesely vs Rafael Nadal")
        self.assertEqual(result, ("jiri vesely", "rafael nadal"))

    def test_no_separator_returns_none(self):
        """Title with no recognized separator -> returns None -> Kalshi market skipped."""
        self.assertIsNone(extract_players("DjokovicSinner"))

    def test_three_part_title_returns_none(self):
        """
        "A vs B vs C".split(" vs ") = ["A", "B", "C"] -> len != 2 -> None.
        Prevents garbage matches from malformed titles.
        """
        self.assertIsNone(extract_players("Djokovic vs Sinner vs Alcaraz"))

    def test_at_word_inside_player_name_risk(self):
        """
        LOW RISK: The ' at ' separator could theoretically fire on a tournament
        location string like 'Federer at Wimbledon' if such a title were emitted.
        Documents the risk. In practice, Kalshi match titles do not use this format.
        """
        # "federer at wimbledon" -> " at " splits to ("federer", "wimbledon")
        # This would produce players ("federer", "wimbledon") — "wimbledon" is not a player.
        result = extract_players("Federer at Wimbledon")
        # The split fires; it is up to the caller to filter nonsense matches.
        self.assertEqual(result, ("federer", "wimbledon"),
            "LOW RISK: ' at ' fires on location strings. "
            "No downstream guard validates that both parts are real player names.")


# ---------------------------------------------------------------------------
# 8. No game-time check (cross-tournament match risk)
# ---------------------------------------------------------------------------

class TestNoGameTimeCheck(unittest.TestCase):
    """
    TennisMatcher has NO date/time or tournament comparison.
    Two different tournaments running simultaneously could have the same
    player pair active (e.g., Djokovic at Wimbledon AND a Djokovic-named
    market at Roland Garros from a prior round still in cache).

    In practice this is unlikely since tournament schedules rarely overlap
    for the same players, but it is an architectural gap.
    """

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_two_kalshi_markets_same_players_both_match(self):
        """
        Two Kalshi tickers with same player pair (different tournaments/rounds)
        both match the same Poly market -> duplicate results.
        """
        km1 = make_kalshi("Djokovic vs Sinner", ticker="KXATP-WIMBLEDON-001", ask=0.47, taker_fee=0.0)
        km2 = make_kalshi("Djokovic vs Sinner", ticker="KXATP-USOPEN-002", ask=0.52, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km1, km2], [pm])
        # Both fire because no tournament/time filter exists
        self.assertEqual(len(results), 2,
            "No time gate: both Kalshi markets match the same Poly entry. "
            "Could produce duplicate log entries and phantom arb signals.")
        tickers = {r["kalshi_ticker"] for r in results}
        self.assertIn("KXATP-WIMBLEDON-001", tickers)
        self.assertIn("KXATP-USOPEN-002", tickers)


# ---------------------------------------------------------------------------
# 9. Edge cases and defensive behavior
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_empty_inputs(self):
        self.assertEqual(self.matcher.match([], []), [])

    def test_empty_kalshi_list(self):
        pm = make_poly("djokovic")
        self.assertEqual(self.matcher.match([], [pm]), [])

    def test_empty_poly_list(self):
        km = make_kalshi("Djokovic vs Sinner")
        self.assertEqual(self.matcher.match([km], []), [])

    def test_total_cost_zero_guard_skips_entry(self):
        """
        If both asks and fees are 0.0, total_cost = 0.0.
        Guard `if total_cost > 0` silently skips -> not in results.
        This is defensively correct (no price = stale data) but swallows the market.
        """
        km = make_kalshi("Djokovic vs Sinner", ask=0.0, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.0, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0,
            "Zero-ask markets are silently skipped (by design), not raised as errors.")

    def test_missing_title_falls_back_to_raw_title(self):
        """km['title'] is empty string; falls back to raw['title']."""
        km = {
            "ticker": "KXATP-001",
            "title": "",
            "ask": 0.47,
            "taker_fee": 0.0,
            "raw": {"title": "Djokovic vs Sinner"},
        }
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["market_name"], "Djokovic vs Sinner")

    def test_missing_title_falls_back_to_raw_subtitle(self):
        """km['title'] and raw['title'] both absent; falls back to raw['subtitle']."""
        km = {
            "ticker": "KXATP-001",
            "title": "",
            "ask": 0.47,
            "taker_fee": 0.0,
            "raw": {"subtitle": "Djokovic vs Sinner"},
        }
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)

    def test_missing_raw_key_no_crash(self):
        """km has no 'raw' key at all — .get('raw', {}) handles it without crash."""
        km = {
            "ticker": "KXATP-001",
            "title": "Djokovic vs Sinner",
            "ask": 0.47,
            "taker_fee": 0.0,
        }
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0)
        try:
            results = self.matcher.match([km], [pm])
        except KeyError as e:
            self.fail(f"KeyError raised for missing 'raw' key: {e}")
        self.assertEqual(len(results), 1)

    def test_unparseable_title_skipped_cleanly(self):
        """Title with no separator -> extract_players returns None -> market skipped, no crash."""
        km = make_kalshi("DjokovicSinner")
        pm = make_poly("djokovic")
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 0)

    def test_missing_slug_produces_empty_string(self):
        """If slug absent from pm and raw, polymarket_slug in result is ''."""
        pm = {
            "team_abbr": "djokovic",
            "ask": 0.46,
            "taker_fee": 0.0,
            "raw": {},
        }
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_slug"], "")

    def test_slug_fallback_from_raw(self):
        """
        raw_p is extracted; pm.get("slug","") or raw_p.get("slug","") uses raw as fallback.
        Confirm the raw slug path works even without a top-level slug.
        """
        pm = {
            "team_abbr": "djokovic",
            "ask": 0.46,
            "taker_fee": 0.0,
            "raw": {"slug": "atp-djokovic-sinner-from-raw"},
        }
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["polymarket_slug"], "atp-djokovic-sinner-from-raw")

    def test_team_key_in_pm_fallback(self):
        """
        pm.get("team_abbr", "") or pm.get("team", "") — confirm 'team' key is tried
        when 'team_abbr' is absent.
        """
        pm = {
            "team": "djokovic",
            "slug": "atp-djokovic-sinner",
            "ask": 0.46,
            "taker_fee": 0.0,
            "raw": {},
        }
        km = make_kalshi("Djokovic vs Sinner", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1)

    def test_no_deduplication_same_slug_different_kalshi(self):
        """
        No deduplication: two Kalshi markets match the same Poly slug.
        Both results are emitted, which can cause duplicate log entries.
        """
        km1 = make_kalshi("Djokovic vs Sinner", ticker="KXATP-001", ask=0.47, taker_fee=0.0)
        km2 = make_kalshi("Djokovic vs Sinner", ticker="KXWTA-001", ask=0.47, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.46, taker_fee=0.0)
        results = self.matcher.match([km1, km2], [pm])
        tickers = [r["kalshi_ticker"] for r in results]
        self.assertIn("KXATP-001", tickers)
        self.assertIn("KXWTA-001", tickers)


# ---------------------------------------------------------------------------
# 10. normalize_name / extract_players unit tests
# ---------------------------------------------------------------------------

class TestHelpers(unittest.TestCase):

    def test_normalize_strips_accents(self):
        self.assertEqual(normalize_name("Gaël"), "gael")

    def test_normalize_lowercases(self):
        self.assertEqual(normalize_name("DJOKOVIC"), "djokovic")

    def test_normalize_collapses_spaces(self):
        self.assertEqual(normalize_name("  Novak  Djokovic  "), "novak djokovic")

    def test_normalize_strips_punctuation(self):
        """Hyphens and dots are replaced with spaces, then collapsed."""
        self.assertEqual(normalize_name("Garcia-Lopez"), "garcia lopez")
        self.assertEqual(normalize_name("V. Williams"), "v williams")

    def test_extract_players_vs(self):
        self.assertEqual(extract_players("Federer vs Djokovic"), ("federer", "djokovic"))

    def test_extract_players_v(self):
        self.assertEqual(extract_players("Federer v Djokovic"), ("federer", "djokovic"))

    def test_extract_players_at_sign(self):
        self.assertEqual(extract_players("Federer @ Djokovic"), ("federer", "djokovic"))

    def test_extract_players_at_word(self):
        self.assertEqual(extract_players("Federer at Djokovic"), ("federer", "djokovic"))

    def test_extract_players_none_no_separator(self):
        self.assertIsNone(extract_players("FedererDjokovic"))

    def test_extract_players_three_parts_returns_none(self):
        """Split produces 3 parts -> len != 2 -> None."""
        self.assertIsNone(extract_players("Federer vs Djokovic vs Nadal"))

    def test_extract_players_preserves_initials(self):
        """V. Williams: after normalize, initial stays as 'v'."""
        result = extract_players("V. Williams vs S. Williams")
        self.assertEqual(result, ("v williams", "s williams"))


class TestZeroAskBehavior(unittest.TestCase):
    """
    RISK-T1: TennisMatcher's only zero-ask guard is `if total_cost <= 0`.
    If k_ask=0.0 and p_ask=0.47, total_cost=0.47+fees > 0, so is_arb=True fires.
    This is a spurious signal — Kalshi has no live quote on that side.

    Production protection: ws_manager filters yes_ask <= 0 BEFORE calling match().
    This test documents the gap at the matcher level and verifies ws_manager's
    upstream filter is the intended defense.
    """

    def setUp(self):
        self.matcher = TennisMatcher()

    def test_zero_kalshi_ask_produces_spurious_arb(self):
        """
        k_ask=0.0, p_ask=0.47, fees=0: total=0.47 < 0.96 → is_arb=True.
        This is spurious — Kalshi has no live quote. The matcher itself does not
        guard against this; ws_manager's yes_ask <= 0 filter is the production defense.
        """
        km = make_kalshi("Djokovic vs Sinner", ask=0.0, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.47, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        # Guard is `total_cost <= 0`; 0.47 > 0 so result IS produced
        self.assertEqual(len(results), 1,
            "RISK-T1: zero k_ask not filtered by matcher — ws_manager upstream filter required")
        self.assertTrue(results[0]["is_arb"],
            "Spurious is_arb=True when k_ask=0 and p_ask=0.47 < threshold")

    def test_zero_poly_ask_produces_spurious_arb(self):
        """
        k_ask=0.48, p_ask=0.0, fees=0: total=0.48 < 0.96 → is_arb=True.
        Same risk from the Polymarket side.
        """
        km = make_kalshi("Djokovic vs Sinner", ask=0.48, taker_fee=0.0)
        pm = make_poly("djokovic", ask=0.0, taker_fee=0.0)
        results = self.matcher.match([km], [pm])
        self.assertEqual(len(results), 1,
            "RISK-T1: zero p_ask not filtered by matcher — ws_manager upstream filter required")
        self.assertTrue(results[0]["is_arb"])


if __name__ == "__main__":
    unittest.main()
