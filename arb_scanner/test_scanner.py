"""
Unit tests for arb_scanner — derived from PICT pairwise test design.

PICT Models covered:
  Domain 1 — normalize_name / team_code
  Domain 2 — teams_from_kalshi_title
  Domain 3 — _extract_polymarket_ask
  Domain 4 — kalshi_is_moneyline / polymarket_is_moneyline
  Domain 5 — OpportunityTracker lifecycle (opened/updated/closed)
  Domain 6 — arb threshold formula (total_cost < ARB_THRESHOLD)

Run: python -m pytest test_scanner.py -v
"""

import time
import unittest
from unittest.mock import patch

from ws_manager import (
    ARB_THRESHOLD,
    OpportunityTracker,
    PropArbTracker,
    _print_and_log_prop_open,
    kalshi_is_moneyline,
    normalize_name,
    team_code,
    teams_from_kalshi_title,
    teams_from_kalshi_market,
)
from fees import (
    KALSHI_TAKER_FEE_THETA,
    POLYMARKET_TAKER_FEE_THETA,
    kalshi_taker_fee,
    polymarket_taker_fee,
)
from matchers.baseball import (
    BaseballMatcher,
    _extract_polymarket_ask,
    polymarket_is_moneyline,
    teams_from_polymarket,
)

def match_markets(kalshi_markets, polymarket_markets):
    return BaseballMatcher(arb_threshold=ARB_THRESHOLD).match(kalshi_markets, polymarket_markets)


# ─── Helpers ──────────────────────────────────────────────────────────────────

# Fixed game datetime used across matching tests: 2026-06-29 19:00 ET = 23:00 UTC
_GAME_START_UTC = "2026-06-29T23:00:00Z"
# Ticker segment for that datetime (ET): 26JUN291900
_GAME_SEGMENT = "26JUN291900"


def _kalshi_market(ticker, title, ask):
    """Build a Kalshi market dict. ticker should use realistic format: KXMLBGAME-{DATE}-{TEAM}."""
    fee = round(kalshi_taker_fee(ask), 6)
    return {"ticker": ticker, "title": title, "ask": ask, "taker_fee": fee,
            "raw": {"title": title}}


def _poly_market(slug, question, ask, team_abbr, game_start=_GAME_START_UTC):
    """Build a Polymarket market dict in live format (with team_abbr and gameStartTime)."""
    fee = round(polymarket_taker_fee(ask), 6)
    return {"slug": slug, "title": question, "ask": ask, "taker_fee": fee,
            "team_abbr": team_abbr, "raw": {"gameStartTime": game_start}}


def _poly_game_pair(slug, question, ask_a, team_a, ask_b, team_b, game_start=_GAME_START_UTC):
    """Return both sides of a Polymarket game (live code needs both sides in the list)."""
    return [
        _poly_market(slug, question, ask_a, team_a, game_start),
        _poly_market(slug, question, ask_b, team_b, game_start),
    ]


def _tracker_process(tracker, matches, k_ts="2026-01-01T00:00:00+00:00",
                     p_ts="2026-01-01T00:00:00+00:00", skew=False):
    return tracker.process(matches, k_ts, p_ts, skew)


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 1 — normalize_name / team_code
# PICT parameters: InputForm, AccentChars, Separator, Casing
# ══════════════════════════════════════════════════════════════════════════════

class TestNormalizeName(unittest.TestCase):
    """TC-N-*: normalize_name produces lowercase ASCII with single spaces."""

    def test_N01_plain_ascii_lowercase_unchanged(self):
        # InputForm=plain_ascii, AccentChars=False, Separator=none, Casing=lower
        assert normalize_name("boston") == "boston"

    def test_N02_plain_ascii_uppercase_lowercased(self):
        # InputForm=plain_ascii, AccentChars=False, Separator=hyphen, Casing=upper
        assert normalize_name("BOSTON") == "boston"

    def test_N03_plain_ascii_mixed_casing_lowercased(self):
        # InputForm=plain_ascii, AccentChars=False, Separator=space, Casing=mixed
        assert normalize_name("BoStOn") == "boston"

    def test_N04_city_name_with_space(self):
        # InputForm=city_name, AccentChars=False, Separator=space, Casing=lower
        assert normalize_name("new york") == "new york"

    def test_N05_accent_chars_stripped(self):
        # InputForm=city_name, AccentChars=True, Separator=none, Casing=upper
        assert normalize_name("Montréal") == "montreal"

    def test_N06_accent_and_mixed_case(self):
        # InputForm=city_name, AccentChars=True, Separator=space, Casing=mixed
        assert normalize_name("São Paulo") == "sao paulo"

    def test_N07_special_chars_collapsed_to_space(self):
        # InputForm=team_nickname, AccentChars=False, Separator=hyphen, Casing=mixed
        assert normalize_name("Red-Sox") == "red sox"

    def test_N08_multiple_spaces_collapsed(self):
        # InputForm=full_name, AccentChars=False, Separator=space, Casing=lower
        assert normalize_name("new  york  yankees") == "new york yankees"

    def test_N09_punctuation_stripped(self):
        # InputForm=team_nickname, AccentChars=False, Separator=none, Casing=lower
        assert normalize_name("St. Louis") == "st louis"

    def test_N10_empty_string_returns_empty(self):
        # InputForm=empty, AccentChars=False, Separator=none, Casing=lower
        assert normalize_name("") == ""

    def test_N11_leading_trailing_whitespace_stripped(self):
        assert normalize_name("  boston  ") == "boston"

    def test_N12_digits_preserved(self):
        assert normalize_name("Game 7") == "game 7"


class TestTeamCode(unittest.TestCase):
    """TC-TC-*: team_code extracts canonical 2-3 char code from raw text."""

    def test_TC01_city_name_lowercase(self):
        # InputForm=city_name, Casing=lower
        assert team_code("boston") == "bos"

    def test_TC02_city_name_uppercase(self):
        # InputForm=city_name, Casing=upper
        assert team_code("BOSTON") == "bos"

    def test_TC03_team_nickname_lowercase(self):
        # InputForm=team_nickname, Casing=lower
        assert team_code("red sox") == "bos"

    def test_TC04_team_nickname_uppercase(self):
        # InputForm=team_nickname, Casing=upper
        assert team_code("RED SOX") == "bos"

    def test_TC05_full_name_mixed(self):
        # InputForm=full_name, Casing=mixed
        assert team_code("New York Yankees") == "nyy"

    def test_TC06_full_name_mets_vs_yankees_longest_match(self):
        # Ensure "new york mets" beats "new york" alias ambiguity
        assert team_code("New York Mets") == "nym"

    def test_TC07_full_name_yankees_longest_match(self):
        assert team_code("New York Yankees") == "nyy"

    def test_TC08_chicago_cubs_disambiguated(self):
        # InputForm=ambiguous — both "chicago cubs" and "chicago white sox" aliases
        assert team_code("Chicago Cubs") == "chc"

    def test_TC09_chicago_white_sox_disambiguated(self):
        assert team_code("Chicago White Sox") == "cws"

    def test_TC10_unknown_team_returns_none(self):
        # InputForm=unknown
        assert team_code("Gotham Knights") is None

    def test_TC11_empty_string_returns_none(self):
        # InputForm=empty
        assert team_code("") is None

    def test_TC12_la_angels_vs_dodgers_longest_match(self):
        assert team_code("Los Angeles Angels") == "laa"
        assert team_code("Los Angeles Dodgers") == "lad"

    def test_TC13_st_louis_with_period(self):
        # AccentChars=False, Separator=period
        assert team_code("St. Louis Cardinals") == "stl"

    def test_TC14_bluejays_no_space(self):
        assert team_code("Bluejays") == "tor"


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 2 — teams_from_kalshi_title
# PICT parameters: Separator, TeamAForm, TeamBForm, BothRecognizable
# ══════════════════════════════════════════════════════════════════════════════

class TestTeamsFromKalshiTitle(unittest.TestCase):
    """TC-KT-*: teams_from_kalshi_title handles all separator variants."""

    def test_KT01_vs_city_city(self):
        # Separator=vs, TeamAForm=city, TeamBForm=city, BothRecognizable=True
        result = teams_from_kalshi_title("New York Yankees vs Boston Red Sox")
        assert result == ("nyy", "bos") or result == ("bos", "nyy")

    def test_KT02_vs_nickname_nickname(self):
        # Separator=vs, TeamAForm=nickname, TeamBForm=nickname
        result = teams_from_kalshi_title("Yankees vs Red Sox")
        assert result is not None
        assert frozenset(result) == frozenset(["nyy", "bos"])

    def test_KT03_vs_full_name(self):
        # Separator=vs, TeamAForm=fullname, TeamBForm=fullname
        result = teams_from_kalshi_title("Houston Astros vs Tampa Bay Rays")
        assert result is not None
        assert frozenset(result) == frozenset(["hou", "tb"])

    def test_KT04_at_separator(self):
        # Separator=@, TeamAForm=city, TeamBForm=city
        result = teams_from_kalshi_title("Yankees @ Red Sox")
        assert result is not None
        assert frozenset(result) == frozenset(["nyy", "bos"])

    def test_KT05_at_word_separator(self):
        # Separator=at, TeamAForm=city, TeamBForm=nickname
        result = teams_from_kalshi_title("Yankees at Fenway Boston")
        # "fenway" not an alias — may return None or partial
        # At minimum should not crash
        assert result is None or isinstance(result, tuple)

    def test_KT06_v_separator(self):
        # Separator=v, TeamAForm=city, TeamBForm=city
        result = teams_from_kalshi_title("Atlanta v Miami")
        assert result is not None
        assert frozenset(result) == frozenset(["atl", "mia"])

    def test_KT07_one_team_unknown_returns_none(self):
        # Separator=vs, TeamBForm=unknown, BothRecognizable=False
        result = teams_from_kalshi_title("Yankees vs Gotham Knights")
        assert result is None

    def test_KT08_both_teams_unknown_returns_none(self):
        # Separator=vs, BothRecognizable=False
        result = teams_from_kalshi_title("Knights vs Dragons")
        assert result is None

    def test_KT09_no_separator_returns_none(self):
        # Separator=none — no recognized delimiter
        result = teams_from_kalshi_title("Yankees Red Sox")
        assert result is None

    def test_KT10_empty_string_returns_none(self):
        result = teams_from_kalshi_title("")
        assert result is None

    def test_KT11_order_preserved(self):
        # TeamA and TeamB order from title preserved in tuple
        result = teams_from_kalshi_title("Dodgers vs Giants")
        assert result is not None
        assert result[0] == "lad"
        assert result[1] == "sf"


class TestTeamsFromPolymarket(unittest.TestCase):
    """TC-PT-*: teams_from_polymarket checks multiple fields in order."""

    def test_PT01_question_field_used(self):
        # "beat" phrasing has no recognized separator, so teams can't be extracted.
        # Use "vs" form to confirm question field is checked.
        m = {"question": "Yankees vs Red Sox game winner", "title": "", "slug": ""}
        result = teams_from_polymarket(m)
        assert result is not None
        assert frozenset(result) == frozenset(["nyy", "bos"])

    def test_PT02_title_field_fallback(self):
        m = {"question": "", "title": "Yankees vs Red Sox", "slug": ""}
        result = teams_from_polymarket(m)
        assert result is not None

    def test_PT03_slug_field_fallback(self):
        m = {"question": "", "title": "", "slug": "new-york-yankees-vs-boston-red-sox"}
        result = teams_from_polymarket(m)
        assert result is not None
        assert frozenset(result) == frozenset(["nyy", "bos"])

    def test_PT04_question_takes_priority_over_title(self):
        # question has Yankees vs Red Sox; title has something else
        m = {"question": "Yankees vs Red Sox game winner", "title": "Random title", "slug": ""}
        result = teams_from_polymarket(m)
        assert result is not None
        assert frozenset(result) == frozenset(["nyy", "bos"])

    def test_PT05_none_values_in_fields_handled(self):
        m = {"question": None, "title": None, "description": None, "slug": None}
        result = teams_from_polymarket(m)
        assert result is None

    def test_PT06_missing_all_fields_returns_none(self):
        result = teams_from_polymarket({})
        assert result is None

    def test_PT07_unrecognized_teams_returns_none(self):
        m = {"question": "Eagles vs Lions football", "title": "", "slug": ""}
        # Football teams not in MLB alias list
        result = teams_from_polymarket(m)
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 3 — _extract_polymarket_ask
# PICT parameters: PriceShape, YesLabel, PriceValue, ValueType
# ══════════════════════════════════════════════════════════════════════════════

class TestExtractPolymarketAsk(unittest.TestCase):
    """TC-PA-*: _extract_polymarket_ask handles all known response shapes."""

    # Shape 1: outcomePrices list
    def test_PA01_outcomePrices_valid_str(self):
        # PriceShape=outcomePrices, PriceValue=0.55, ValueType=str
        m = {"outcomePrices": ["0.55", "0.45"]}
        assert _extract_polymarket_ask(m) == 0.55

    def test_PA02_outcomePrices_float(self):
        m = {"outcomePrices": [0.72, 0.28]}
        assert _extract_polymarket_ask(m) == 0.72

    def test_PA03_outcomePrices_out_of_bounds_falls_through(self):
        # PriceValue=1.1 (oob) — should skip and try next shape
        m = {"outcomePrices": ["1.1", "0.0"]}
        assert _extract_polymarket_ask(m) is None

    def test_PA04_outcomePrices_zero_falls_through(self):
        m = {"outcomePrices": ["0.0", "1.0"]}
        assert _extract_polymarket_ask(m) is None

    def test_PA05_outcomePrices_null_value(self):
        # PriceValue=null — None in list
        m = {"outcomePrices": [None, "0.45"]}
        assert _extract_polymarket_ask(m) is None

    def test_PA06_outcomePrices_empty_list(self):
        m = {"outcomePrices": []}
        assert _extract_polymarket_ask(m) is None

    # Shape 2: tokens list — named YES outcome
    def test_PA07_tokens_yes_label_uppercase(self):
        # YesLabel=Yes
        m = {"tokens": [{"outcome": "Yes", "price": 0.6}, {"outcome": "No", "price": 0.4}]}
        assert _extract_polymarket_ask(m) == 0.6

    def test_PA08_tokens_yes_label_lowercase(self):
        # YesLabel=yes
        m = {"tokens": [{"outcome": "yes", "price": 0.6}]}
        assert _extract_polymarket_ask(m) == 0.6

    def test_PA09_tokens_home_label(self):
        # YesLabel=home
        m = {"tokens": [{"outcome": "home", "price": 0.45}]}
        assert _extract_polymarket_ask(m) == 0.45

    def test_PA10_tokens_win_label(self):
        m = {"tokens": [{"outcome": "win", "price": 0.52}]}
        assert _extract_polymarket_ask(m) == 0.52

    def test_PA11_tokens_unknown_label_returns_none(self):
        # YesLabel=other — no recognized outcome label, no fallback to first token (could be NO leg)
        m = {"tokens": [{"outcome": "TeamA", "price": 0.45}]}
        assert _extract_polymarket_ask(m) is None

    def test_PA12_tokens_price_zero_skipped_for_named_falls_to_first(self):
        # Named outcome has price=0.0 (invalid), first token also 0.0 → None
        m = {"tokens": [{"outcome": "Yes", "price": 0.0}]}
        assert _extract_polymarket_ask(m) is None

    def test_PA13_tokens_price_one_invalid(self):
        m = {"tokens": [{"outcome": "Yes", "price": 1.0}]}
        assert _extract_polymarket_ask(m) is None

    # Shape 3: flat ask fields
    def test_PA14_bestAsk_field(self):
        m = {"bestAsk": 0.72}
        assert _extract_polymarket_ask(m) == 0.72

    def test_PA15_askPrice_field(self):
        # askPrice is not a recognized field — implementation only checks bestAsk
        m = {"askPrice": 0.65}
        assert _extract_polymarket_ask(m) is None

    def test_PA16_best_ask_snake_case(self):
        # best_ask (snake_case) is not a recognized field — only camelCase bestAsk is checked
        m = {"best_ask": 0.58}
        assert _extract_polymarket_ask(m) is None

    def test_PA17_ask_field(self):
        # standalone "ask" is not a recognized field
        m = {"ask": 0.50}
        assert _extract_polymarket_ask(m) is None

    # Shape 4: last trade price fallback
    def test_PA18_lastTradePrice_not_used_as_ask(self):
        # Shape 4 removed: stale execution prices are not valid ask prices
        m = {"lastTradePrice": 0.50}
        assert _extract_polymarket_ask(m) is None

    def test_PA19_lastPrice_not_used_as_ask(self):
        m = {"lastPrice": 0.48}
        assert _extract_polymarket_ask(m) is None

    def test_PA20_price_field_not_used_as_ask(self):
        m = {"price": 0.55}
        assert _extract_polymarket_ask(m) is None

    def test_PA21_no_price_fields_returns_none(self):
        # PriceShape=none
        m = {"question": "Who wins?", "slug": "some-slug"}
        assert _extract_polymarket_ask(m) is None

    def test_PA22_all_fields_null_returns_none(self):
        m = {"outcomePrices": None, "tokens": None, "bestAsk": None, "price": None}
        assert _extract_polymarket_ask(m) is None

    def test_PA23_shape1_takes_precedence_over_shape3(self):
        # bestAsk is primary; outcomePrices is secondary fallback
        m = {"outcomePrices": ["0.55"], "bestAsk": 0.72}
        assert _extract_polymarket_ask(m) == 0.72

    def test_PA24_string_price_fields_parsed(self):
        m = {"bestAsk": "0.63"}
        assert _extract_polymarket_ask(m) == 0.63


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 4 — kalshi_is_moneyline / polymarket_is_moneyline
# PICT parameters: TitleContent, SubtitleContent, HasVsSep, HasRejectKw
# ══════════════════════════════════════════════════════════════════════════════

class TestKalshiIsMoneyline(unittest.TestCase):
    """TC-KM-*: kalshi_is_moneyline accepts game-winner markets only."""

    def test_KM01_team_vs_team_accepted(self):
        # TitleContent=team_vs_team, HasVsSep=True, HasRejectKw=False
        m = {"title": "New York Yankees vs Boston Red Sox", "subtitle": ""}
        assert kalshi_is_moneyline(m) is True

    def test_KM02_winner_keyword_accepted(self):
        # TitleContent=winner_game, HasVsSep=False
        m = {"title": "Who will win the game?", "subtitle": ""}
        assert kalshi_is_moneyline(m) is True

    def test_KM03_moneyline_keyword_accepted(self):
        m = {"title": "Moneyline: Yankees", "subtitle": ""}
        assert kalshi_is_moneyline(m) is True

    def test_KM04_team_at_team_accepted(self):
        # TitleContent=team_at_team
        m = {"title": "Yankees @ Red Sox", "subtitle": ""}
        assert kalshi_is_moneyline(m) is True

    def test_KM05_total_runs_reject_kw_in_title(self):
        # TitleContent=team_vs_team, SubtitleContent=total runs, HasRejectKw=True
        m = {"title": "Yankees vs Red Sox total runs", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False

    def test_KM06_reject_kw_in_subtitle(self):
        # RejectKw in subtitle only
        m = {"title": "Yankees vs Red Sox", "subtitle": "over under 8.5"}
        assert kalshi_is_moneyline(m) is False

    def test_KM07_over_under_rejected(self):
        m = {"title": "Over/Under: Yankees vs Red Sox 8.5", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False

    def test_KM08_strikeout_prop_rejected(self):
        m = {"title": "Gerrit Cole strikeouts", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False

    def test_KM09_spread_rejected(self):
        m = {"title": "Yankees -1.5 spread", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False

    def test_KM10_pitcher_prop_rejected(self):
        m = {"title": "Starting pitcher wins", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False

    def test_KM11_home_run_rejected(self):
        m = {"title": "Home run hit in game", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False

    def test_KM12_innings_rejected(self):
        m = {"title": "First 5 innings winner", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False

    def test_KM13_empty_title_rejected(self):
        # TitleContent=empty, no accept signals
        m = {"title": "", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False

    def test_KM14_none_values_do_not_crash(self):
        m = {"title": None, "subtitle": None}
        assert kalshi_is_moneyline(m) is False

    def test_KM15_beat_keyword_accepted(self):
        m = {"title": "Will the Yankees beat the Red Sox?", "subtitle": ""}
        assert kalshi_is_moneyline(m) is True

    def test_KM16_era_prop_rejected(self):
        m = {"title": "Pitcher ERA under 3", "subtitle": ""}
        assert kalshi_is_moneyline(m) is False


class TestPolymarketIsMoneyline(unittest.TestCase):
    """TC-PM-*: polymarket_is_moneyline rejects prop/spread/total markets."""

    def test_PM01_simple_winner_question_accepted(self):
        m = {"question": "Will the Yankees beat the Red Sox?", "title": "", "description": ""}
        assert polymarket_is_moneyline(m) is True

    def test_PM02_total_in_question_rejected(self):
        m = {"question": "Total runs over 8.5?", "title": "", "description": ""}
        assert polymarket_is_moneyline(m) is False

    def test_PM03_over_in_title_rejected(self):
        m = {"question": "", "title": "Over 7 runs Yankees vs Sox", "description": ""}
        assert polymarket_is_moneyline(m) is False

    def test_PM04_spread_in_description_only_accepted(self):
        # description field is not checked — only question and title are examined
        m = {"question": "", "title": "", "description": "run line spread market"}
        assert polymarket_is_moneyline(m) is True

    def test_PM05_strikeout_rejected(self):
        m = {"question": "Over 9.5 strikeouts by Cole?", "title": "", "description": ""}
        assert polymarket_is_moneyline(m) is False

    def test_PM06_player_prop_rejected(self):
        m = {"question": "Player hits 2+ home runs?", "title": "", "description": ""}
        assert polymarket_is_moneyline(m) is False

    def test_PM07_none_fields_accepted(self):
        # All None/missing — no reject keywords found → accepted
        m = {"question": None, "title": None, "description": None}
        assert polymarket_is_moneyline(m) is True

    def test_PM08_empty_fields_accepted(self):
        m = {"question": "", "title": "", "description": ""}
        assert polymarket_is_moneyline(m) is True

    def test_PM09_first_five_innings_rejected(self):
        m = {"question": "First 5 innings winner", "title": "", "description": ""}
        assert polymarket_is_moneyline(m) is False

    def test_PM10_scoreless_inning_rejected(self):
        m = {"question": "Scoreless first inning?", "title": "", "description": ""}
        assert polymarket_is_moneyline(m) is False


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 5 — OpportunityTracker lifecycle
# PICT parameters: Cycle1State, Cycle2State, Cycle3State
# ══════════════════════════════════════════════════════════════════════════════

class TestOpportunityTrackerLifecycle(unittest.TestCase):
    """TC-OT-*: OpportunityTracker emits correct opened/updated/closed events."""

    def _arb_match(self, ticker="KXMLBGAME-NYYBOS", slug="nyy-vs-bos", k_ask=0.45, p_ask=0.45):
        k_fee = round(kalshi_taker_fee(k_ask), 6)
        p_fee = round(polymarket_taker_fee(p_ask), 6)
        total = round(k_ask + p_ask + k_fee + p_fee, 6)
        return {
            "market_name": "NYY vs BOS",
            "kalshi_ticker": ticker,
            "kalshi_team": "NYY",
            "polymarket_team": "bos",
            "kalshi_ask": k_ask,
            "kalshi_taker_fee": k_fee,
            "polymarket_slug": slug,
            "polymarket_ask": p_ask,
            "polymarket_taker_fee": p_fee,
            "total_cost": total,
            "gap_cents": round((ARB_THRESHOLD - total) * 100, 4),
            "is_arb": total < ARB_THRESHOLD,
        }

    def _no_arb_match(self, ticker="KXMLBGAME-NYYBOS", slug="nyy-vs-bos"):
        m = self._arb_match(ticker, slug, k_ask=0.50, p_ask=0.50)
        m["is_arb"] = False
        return m

    def test_OT01_arb_arb_arb_sequence(self):
        # Cycle1=arb, Cycle2=arb, Cycle3=arb → opened, updated, updated
        t = OpportunityTracker()
        m = self._arb_match()
        e1 = _tracker_process(t, [m])
        e2 = _tracker_process(t, [m])
        e3 = _tracker_process(t, [m])
        assert len(e1) == 1 and e1[0]["event"] == "opened"
        assert len(e2) == 1 and e2[0]["event"] == "updated"
        assert len(e3) == 1 and e3[0]["event"] == "updated"

    def test_OT01b_events_carry_sides_for_both_venues(self):
        # An ML arb is Kalshi YES on kalshi_team + Poly YES on polymarket_team —
        # opened AND closed events must say which side is bought on which site.
        t = OpportunityTracker()
        e1 = _tracker_process(t, [self._arb_match()])
        e2 = _tracker_process(t, [])  # closed
        assert e1[0]["kalshi_team"] == "NYY"
        assert e1[0]["polymarket_team"] == "bos"
        assert e2[0]["kalshi_team"] == "NYY"
        assert e2[0]["polymarket_team"] == "bos"

    def test_OT02_opportunity_id_stable_across_lifecycle(self):
        # opportunity_id must be the same UUID across opened/updated/closed
        t = OpportunityTracker()
        m = self._arb_match()
        e1 = _tracker_process(t, [m])
        e2 = _tracker_process(t, [m])
        e3 = _tracker_process(t, [])  # closed
        opp_id = e1[0]["opportunity_id"]
        assert e2[0]["opportunity_id"] == opp_id
        assert e3[0]["opportunity_id"] == opp_id

    def test_OT03_arb_arb_no_arb_emits_closed(self):
        # Cycle1=arb, Cycle2=arb, Cycle3=no_arb → opened, updated, closed
        t = OpportunityTracker()
        m = self._arb_match()
        e1 = _tracker_process(t, [m])
        e2 = _tracker_process(t, [m])
        e3 = _tracker_process(t, [])
        assert e1[0]["event"] == "opened"
        assert e2[0]["event"] == "updated"
        assert e3[0]["event"] == "closed"

    def test_OT04_arb_no_arb_arb_emits_two_opens(self):
        # Cycle1=arb, Cycle2=no_arb, Cycle3=arb → opened, closed, opened (new UUID)
        t = OpportunityTracker()
        m = self._arb_match()
        e1 = _tracker_process(t, [m])
        e2 = _tracker_process(t, [])
        e3 = _tracker_process(t, [m])
        assert e1[0]["event"] == "opened"
        assert e2[0]["event"] == "closed"
        assert e3[0]["event"] == "opened"
        # New UUID on re-open
        assert e3[0]["opportunity_id"] != e1[0]["opportunity_id"]

    def test_OT05_no_arb_cycles_emit_nothing(self):
        # Cycle1=no_arb, Cycle2=no_arb, Cycle3=no_arb → no events
        t = OpportunityTracker()
        e1 = _tracker_process(t, [])
        e2 = _tracker_process(t, [])
        e3 = _tracker_process(t, [])
        assert e1 == [] and e2 == [] and e3 == []

    def test_OT06_no_arb_then_arb_then_close(self):
        # Cycle1=no_arb, Cycle2=arb, Cycle3=no_arb → none, opened, closed
        t = OpportunityTracker()
        m = self._arb_match()
        e1 = _tracker_process(t, [])
        e2 = _tracker_process(t, [m])
        e3 = _tracker_process(t, [])
        assert e1 == []
        assert e2[0]["event"] == "opened"
        assert e3[0]["event"] == "closed"

    def test_OT07_duration_null_on_opened(self):
        t = OpportunityTracker()
        m = self._arb_match()
        events = _tracker_process(t, [m])
        assert events[0]["duration_ms"] is None

    def test_OT08_duration_nonnull_on_updated(self):
        t = OpportunityTracker()
        m = self._arb_match()
        _tracker_process(t, [m])
        time.sleep(0.05)
        events = _tracker_process(t, [m])
        assert events[0]["duration_ms"] is not None
        assert events[0]["duration_ms"] >= 0

    def test_OT09_duration_nonnull_on_closed(self):
        t = OpportunityTracker()
        m = self._arb_match()
        _tracker_process(t, [m])
        events = _tracker_process(t, [])
        assert events[0]["duration_ms"] is not None

    def test_OT09b_duration_is_integer_milliseconds(self):
        ms = OpportunityTracker._duration_ms(
            "2026-07-05T00:00:00+00:00", "2026-07-05T00:00:01.500000+00:00")
        assert ms == 1500
        assert isinstance(ms, int)

    def test_OT10_two_markets_tracked_independently(self):
        # Two separate arb opportunities — each gets own UUID
        t = OpportunityTracker()
        m1 = self._arb_match("KXMLBGAME-NYYBOS", "nyy-vs-bos")
        m2 = self._arb_match("KXMLBGAME-HOUATL", "hou-vs-atl", k_ask=0.44, p_ask=0.44)
        events = _tracker_process(t, [m1, m2])
        assert len(events) == 2
        assert all(e["event"] == "opened" for e in events)
        ids = {e["opportunity_id"] for e in events}
        assert len(ids) == 2  # distinct UUIDs

    def test_OT11_one_of_two_closes(self):
        # Market 2 disappears — market 1 continues
        t = OpportunityTracker()
        m1 = self._arb_match("KXMLBGAME-NYYBOS", "nyy-vs-bos")
        m2 = self._arb_match("KXMLBGAME-HOUATL", "hou-vs-atl", k_ask=0.44, p_ask=0.44)
        _tracker_process(t, [m1, m2])
        events = _tracker_process(t, [m1])  # m2 gone
        event_types = {e["event"] for e in events}
        assert "updated" in event_types
        assert "closed" in event_types

    def test_OT12_closed_event_has_correct_ticker_and_slug(self):
        # Closed event preserves ticker/slug from the key
        t = OpportunityTracker()
        m = self._arb_match("KXMLBGAME-NYYBOS", "nyy-vs-bos")
        _tracker_process(t, [m])
        events = _tracker_process(t, [])
        closed = events[0]
        assert closed["kalshi_ticker"] == "KXMLBGAME-NYYBOS"
        assert closed["polymarket_slug"] == "nyy-vs-bos"

    def test_OT13_fetched_at_timestamps_present_on_all_events(self):
        t = OpportunityTracker()
        m = self._arb_match()
        k_ts = "2026-01-01T12:00:00+00:00"
        p_ts = "2026-01-01T12:00:05+00:00"
        events = _tracker_process(t, [m], k_ts=k_ts, p_ts=p_ts)
        assert events[0]["kalshi_fetched_at"] == k_ts
        assert events[0]["polymarket_fetched_at"] == p_ts

    def test_OT14_timestamp_skew_warning_propagated(self):
        t = OpportunityTracker()
        m = self._arb_match()
        events = _tracker_process(t, [m], skew=True)
        assert events[0]["timestamp_skew_warning"] is True

    def test_OT15_non_arb_match_not_tracked(self):
        # is_arb=False match → no event, no open window
        t = OpportunityTracker()
        m = self._no_arb_match()
        events = _tracker_process(t, [m])
        assert events == []
        assert t.active_windows() == []


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 6 — arb threshold formula
# PICT parameters: KalshiAsk, PolyAsk, FeeSymmetry, ArbOutcome
# ══════════════════════════════════════════════════════════════════════════════

class TestArbThresholdFormula(unittest.TestCase):
    """TC-AT-*: total_cost = k_ask + p_ask + k_fee + p_fee compared to ARB_THRESHOLD,
    where k_fee/p_fee follow each venue's parabolic taker-fee formula (fees.py)."""

    def _cost(self, k, p):
        return k + p + kalshi_taker_fee(k) + polymarket_taker_fee(p)

    def test_AT01_well_below_threshold_is_arb(self):
        # KalshiAsk=0.45, PolyAsk=0.45, FeeSymmetry=equal, ArbOutcome=True
        assert self._cost(0.45, 0.45) < ARB_THRESHOLD

    def test_AT02_just_below_threshold_is_arb(self):
        # Symmetric boundary (see AT03) sits at k=p≈0.4638; just under it is still arb
        assert self._cost(0.463, 0.463) < ARB_THRESHOLD

    def test_AT03_at_exact_boundary_is_not_arb(self):
        # Binary-search the symmetric price where cost(x, x) == ARB_THRESHOLD to
        # machine precision, then verify the strict "<" comparison excludes it —
        # avoids hand-deriving the (now quadratic) boundary algebraically.
        lo, hi = 0.01, 0.49
        for _ in range(100):
            mid = (lo + hi) / 2
            if self._cost(mid, mid) < ARB_THRESHOLD:
                lo = mid
            else:
                hi = mid
        total = self._cost(hi, hi)
        assert total >= ARB_THRESHOLD
        assert not (total < ARB_THRESHOLD)

    def test_AT04_just_above_threshold_not_arb(self):
        # Just above the ~0.4638 symmetric boundary → total > 0.96
        assert self._cost(0.465, 0.465) >= ARB_THRESHOLD

    def test_AT05_asymmetric_below_threshold_is_arb(self):
        # KalshiAsk=0.60, PolyAsk=0.20 → comfortably under threshold, IS arb
        assert self._cost(0.60, 0.20) < ARB_THRESHOLD

    def test_AT06_asymmetric_moderate_below_threshold(self):
        # KalshiAsk=0.50, PolyAsk=0.30
        assert self._cost(0.50, 0.30) < ARB_THRESHOLD

    def test_AT07_tiny_asks_always_arb(self):
        # KalshiAsk=0.01, PolyAsk=0.01
        assert self._cost(0.01, 0.01) < ARB_THRESHOLD

    def test_AT08_formula_is_additive_not_subtraction(self):
        # Verify formula is sum, not subtraction
        k, p = 0.45, 0.45
        expected = k + p + kalshi_taker_fee(k) + polymarket_taker_fee(p)
        assert abs(self._cost(k, p) - expected) < 1e-10

    def test_AT09_fees_follow_parabolic_formula_not_flat_rate(self):
        # Fee = theta * price * (1 - price) — NOT a flat percentage of ask.
        assert kalshi_taker_fee(0.60) == KALSHI_TAKER_FEE_THETA * 0.60 * 0.40
        assert polymarket_taker_fee(0.30) == POLYMARKET_TAKER_FEE_THETA * 0.30 * 0.70
        # A flat rate would charge 0.10 and 0.90 in the same proportion to price.
        # The parabolic formula gives them the same *absolute* fee (symmetric
        # around $0.50) but wildly different fee-to-price ratios — proof it
        # isn't a fixed percentage.
        assert abs(kalshi_taker_fee(0.10) - kalshi_taker_fee(0.90)) < 1e-12
        assert (kalshi_taker_fee(0.10) / 0.10) > (kalshi_taker_fee(0.90) / 0.90)

    def test_AT10_gap_cents_positive_when_arb(self):
        # gap_cents = (ARB_THRESHOLD - total_cost) * 100 > 0 when arb
        k, p = 0.45, 0.45
        total = self._cost(k, p)
        gap = round((ARB_THRESHOLD - total) * 100, 4)
        assert gap > 0

    def test_AT11_gap_cents_negative_when_not_arb(self):
        k, p = 0.50, 0.50
        total = self._cost(k, p)
        gap = round((ARB_THRESHOLD - total) * 100, 4)
        assert gap < 0

    def test_AT12_match_markets_computes_is_arb_correctly(self):
        # Integration: match_markets uses correct formula
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "Yankees vs Red Sox", 0.45)]
        pm = _poly_game_pair("nyy-vs-bos", "Yankees vs Red Sox", 0.45, "bos", 0.45, "nyy")
        results = match_markets(km, pm)
        assert len(results) == 1
        r = results[0]
        expected_total = 0.45 + 0.45 + kalshi_taker_fee(0.45) + polymarket_taker_fee(0.45)
        assert abs(r["total_cost"] - expected_total) < 1e-6
        assert r["is_arb"] == (expected_total < ARB_THRESHOLD)

    def test_AT13_match_markets_no_arb_marked_correctly(self):
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "Yankees vs Red Sox", 0.50)]
        pm = _poly_game_pair("nyy-vs-bos", "Yankees vs Red Sox", 0.50, "bos", 0.50, "nyy")
        results = match_markets(km, pm)
        assert len(results) == 1
        assert results[0]["is_arb"] is False


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 7 — match_markets team-pair matching
# ══════════════════════════════════════════════════════════════════════════════

class TestMatchMarkets(unittest.TestCase):
    """TC-MM-*: match_markets pairs Kalshi and Polymarket markets by team and game time."""

    def test_MM01_exact_title_match(self):
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "Yankees vs Red Sox", 0.45)]
        pm = _poly_game_pair("nyy-vs-bos", "Yankees vs Red Sox game winner", 0.45, "bos", 0.45, "nyy")
        results = match_markets(km, pm)
        assert len(results) == 1

    def test_MM02_reversed_order_still_matches(self):
        # Kalshi: "Yankees vs Red Sox" with k_team=NYY; Poly opponent side is BOS
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "Yankees vs Red Sox", 0.45)]
        pm = _poly_game_pair("nyy-vs-bos", "Red Sox vs Yankees", 0.45, "bos", 0.45, "nyy")
        results = match_markets(km, pm)
        assert len(results) == 1

    def test_MM03_different_name_forms_match(self):
        # Kalshi uses city names, Poly uses nicknames — team_abbr drives matching
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "New York Yankees vs Boston Red Sox", 0.45)]
        pm = _poly_game_pair("nyy-vs-bos", "Yankees vs Red Sox game winner", 0.45, "bos", 0.45, "nyy")
        results = match_markets(km, pm)
        assert len(results) == 1

    def test_MM04_no_match_different_teams(self):
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "Yankees vs Red Sox", 0.45)]
        pm = _poly_game_pair("hou-vs-atl", "Astros vs Braves", 0.45, "atl", 0.45, "hou")
        results = match_markets(km, pm)
        assert len(results) == 0

    def test_MM05_empty_kalshi_returns_empty(self):
        pm = _poly_game_pair("nyy-vs-bos", "Yankees vs Red Sox", 0.45, "bos", 0.45, "nyy")
        results = match_markets([], pm)
        assert results == []

    def test_MM06_empty_polymarket_returns_empty(self):
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "Yankees vs Red Sox", 0.45)]
        results = match_markets(km, [])
        assert results == []

    def test_MM07_both_empty_returns_empty(self):
        assert match_markets([], []) == []

    def test_MM08_unrecognized_kalshi_title_skipped(self):
        # Ticker with unparseable date segment → _kalshi_game_dt_utc returns None → skipped
        km = [_kalshi_market("KXMLBGAME-BADDATE-NYY", "Random Game Title", 0.45)]
        pm = _poly_game_pair("nyy-vs-bos", "Yankees vs Red Sox", 0.45, "bos", 0.45, "nyy")
        results = match_markets(km, pm)
        assert len(results) == 0

    def test_MM09_market_name_comes_from_kalshi_title(self):
        # market_name in the match result comes from the Kalshi title, not Polymarket
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "Yankees vs Red Sox", 0.45)]
        pm = _poly_game_pair("nyy-vs-bos", "Yankees vs Red Sox", 0.45, "bos", 0.45, "nyy")
        results = match_markets(km, pm)
        assert len(results) == 1
        assert results[0]["market_name"] == "Yankees vs Red Sox"

    def test_MM10_game_time_mismatch_skipped(self):
        # Poly game is 2 hours later than Kalshi → outside 30-min window → no match
        km = [_kalshi_market(f"KXMLBGAME-{_GAME_SEGMENT}-NYY", "Yankees vs Red Sox", 0.45)]
        late_start = "2026-06-30T01:00:00Z"  # 2h after _GAME_START_UTC
        pm = _poly_game_pair("nyy-vs-bos", "Yankees vs Red Sox", 0.45, "bos", 0.45, "nyy",
                             game_start=late_start)
        results = match_markets(km, pm)
        assert len(results) == 0


# ─── Domain 7 — PropArbTracker lifecycle ─────────────────────────────────────

def _prop_arb(ticker, direction, leg1_ask=0.45, leg2_ask=0.46):
    return {
        "kalshi_ticker": ticker,
        "direction": direction,
        "leg1": "Kalshi YES" if "Kalshi YES" in direction else "Poly YES",
        "leg1_ask": leg1_ask,
        "leg2": "Poly NO" if "Kalshi YES" in direction else "Kalshi NO",
        "leg2_ask": leg2_ask,
        "total_cost": round(leg1_ask + leg2_ask, 4),
        "gap_cents": round((0.96 - leg1_ask - leg2_ask) * 100, 2),
        "player_name": "Test Player",
        "stat_type": "hits",
        "line": 1,
        "event_title": "Game A",
        "game_start": "2026-06-30T23:00:00Z",
        "poly_smt": "baseball_player_hits",
        "poly_ws_yes_ask": 0.45,
        "poly_ws_no_ask": 0.46,
    }


class TestPropArbTracker(unittest.TestCase):
    """Domain 7 — PropArbTracker lifecycle and mark_opened isolation."""

    def setUp(self):
        self.tracker = PropArbTracker()
        self.ts = "2026-06-30T12:00:00+00:00"

    def test_PAT01_update_opens_new_arb(self):
        arb = _prop_arb("KXMLBHIT-PLAYER-A", "Kalshi YES + Poly NO")
        new_or_changed, closed = self.tracker.update([arb], self.ts)
        assert len(new_or_changed) == 1
        assert new_or_changed[0][0] == "opened"
        assert len(closed) == 0
        assert self.tracker.active_count() == 1

    def test_PAT02_update_closes_missing_arb(self):
        arb = _prop_arb("KXMLBHIT-PLAYER-A", "Kalshi YES + Poly NO")
        self.tracker.update([arb], self.ts)
        new_or_changed, closed = self.tracker.update([], self.ts)
        assert len(closed) == 1
        assert self.tracker.active_count() == 0

    def test_PAT03_update_emits_updated_on_price_change(self):
        arb = _prop_arb("KXMLBHIT-PLAYER-A", "Kalshi YES + Poly NO", 0.45, 0.46)
        self.tracker.update([arb], self.ts)
        arb2 = _prop_arb("KXMLBHIT-PLAYER-A", "Kalshi YES + Poly NO", 0.44, 0.46)
        new_or_changed, closed = self.tracker.update([arb2], self.ts)
        assert len(new_or_changed) == 1
        assert new_or_changed[0][0] == "updated"

    def test_PAT04_mark_opened_inserts_without_closing_others(self):
        # Core regression: confirming a single arb must not close pre-existing open arbs.
        arb_a = _prop_arb("KXMLBHIT-PLAYER-A", "Kalshi YES + Poly NO")
        arb_b = _prop_arb("KXMLBHIT-PLAYER-B", "Kalshi YES + Poly NO")
        self.tracker.update([arb_a, arb_b], self.ts)
        assert self.tracker.active_count() == 2

        # Simulate REST confirm completing for a 3rd arb — mark_opened, not update()
        arb_c = _prop_arb("KXMLBHIT-PLAYER-C", "Kalshi YES + Poly NO")
        inserted = self.tracker.mark_opened(arb_c, self.ts)
        assert inserted is True
        assert self.tracker.active_count() == 3

        # A and B must still be open
        active_tickers = {a["kalshi_ticker"] for a in self.tracker.active()}
        assert "KXMLBHIT-PLAYER-A" in active_tickers
        assert "KXMLBHIT-PLAYER-B" in active_tickers
        assert "KXMLBHIT-PLAYER-C" in active_tickers

    def test_PAT05_mark_opened_idempotent_if_already_tracked(self):
        arb = _prop_arb("KXMLBHIT-PLAYER-A", "Kalshi YES + Poly NO")
        self.tracker.update([arb], self.ts)
        # mark_opened on an already-tracked key must be a no-op: False return, no log written.
        # The caller (_rest_confirm_and_emit) guards print/log on the return value, so if that
        # guard ever breaks, log_event being called here is the signal.
        with patch("ws_manager.log_event") as mock_log:
            inserted = self.tracker.mark_opened(arb, self.ts)
            assert inserted is False
            assert self.tracker.active_count() == 1
            # _print_and_log_prop_open must NOT be called for the duplicate
            mock_log.assert_not_called()

    def test_PAT05b_print_and_log_prop_open_called_on_fresh_insert(self):
        arb = _prop_arb("KXMLBHIT-PLAYER-A", "Kalshi YES + Poly NO")
        with patch("ws_manager.log_event") as mock_log:
            _print_and_log_prop_open(arb, self.ts)
            mock_log.assert_called_once()
            event = mock_log.call_args[0][0]
            assert event["event"] == "prop_arb"
            assert event["kalshi_ticker"] == "KXMLBHIT-PLAYER-A"

    def test_PAT06_update_after_mark_opened_closes_correctly(self):
        # After mark_opened adds arb_c, the next full update that excludes arb_c should close it.
        arb_a = _prop_arb("KXMLBHIT-PLAYER-A", "Kalshi YES + Poly NO")
        arb_c = _prop_arb("KXMLBHIT-PLAYER-C", "Kalshi YES + Poly NO")
        self.tracker.update([arb_a], self.ts)
        self.tracker.mark_opened(arb_c, self.ts)
        assert self.tracker.active_count() == 2

        # Next tick: only arb_a is still live
        new_or_changed, closed = self.tracker.update([arb_a], self.ts)
        assert len(closed) == 1
        assert closed[0]["arb"]["kalshi_ticker"] == "KXMLBHIT-PLAYER-C"
        assert self.tracker.active_count() == 1


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 8 — Poly WS freshness guard + delta resubscribe
# REST re-seeds are CDN-cached (max-age=30) and must not clobber prices the WS
# touched more recently; new slugs discovered mid-connection must be reported
# for delta subscription.

import asyncio
import itertools
import json
from datetime import datetime, timedelta, timezone

import ws_manager as wm
from ws_manager import (
    _carry_ws_fresh_prices,
    _poly_unsubscribed_slugs,
    _update_poly_props_map,
)


def _props_search_event(slug, player, yes_ask, no_ask, game_start, active=True, closed=False):
    return {
        "title": "MLB props",
        "markets": [{
            "sportsMarketType": "baseball_player_hits",
            "slug": slug,
            "gameStartTime": game_start,
            "active": active,
            "closed": closed,
            "archived": False,
            "line": 1.5,
            "metadata": {"playerName": player},
            "marketSides": [
                {"long": True, "quote": {"value": str(yes_ask)}},
                {"long": False, "quote": {"value": str(no_ask)}},
            ],
        }],
    }


class TestCarryWsFreshPrices(unittest.TestCase):
    """_carry_ws_fresh_prices: REST entry keeps WS prices iff WS touched them recently."""

    def setUp(self):
        self.now = datetime(2026, 7, 1, 23, 0, 0, tzinfo=timezone.utc)
        self.new = {"yes_ask": 0.60, "no_ask": 0.45}

    def test_FG01_ws_fresh_prices_carried(self):
        old = {"yes_ask": 0.55, "no_ask": 0.50, "ws_at": self.now - timedelta(seconds=5)}
        merged = _carry_ws_fresh_prices(old, dict(self.new), self.now)
        assert merged["yes_ask"] == 0.55
        assert merged["no_ask"] == 0.50

    def test_FG02_ws_stale_rest_wins(self):
        old = {"yes_ask": 0.55, "no_ask": 0.50, "ws_at": self.now - timedelta(seconds=60)}
        merged = _carry_ws_fresh_prices(old, dict(self.new), self.now)
        assert merged["yes_ask"] == 0.60
        assert merged["no_ask"] == 0.45

    def test_FG03_no_old_entry_rest_wins(self):
        merged = _carry_ws_fresh_prices(None, dict(self.new), self.now)
        assert merged["yes_ask"] == 0.60

    def test_FG04_old_entry_never_ws_touched_rest_wins(self):
        old = {"yes_ask": 0.55, "no_ask": 0.50}
        merged = _carry_ws_fresh_prices(old, dict(self.new), self.now)
        assert merged["yes_ask"] == 0.60

    def test_FG10_only_ws_populated_sides_are_carried(self):
        # Review F15.2: a side the WS never populated must take the REST value
        # instead of being clobbered with None.
        old = {"yes_ask": 0.55, "no_ask": None, "ws_at": self.now - timedelta(seconds=5)}
        merged = _carry_ws_fresh_prices(old, dict(self.new), self.now)
        assert merged["yes_ask"] == 0.55   # WS-fresh side carried
        assert merged["no_ask"] == 0.45    # REST fills the side WS never saw


class TestPropsMapFreshnessGuard(unittest.TestCase):
    """_update_poly_props_map must respect the WS freshness guard and still evict."""

    def setUp(self):
        self._saved = dict(wm._poly_ws_props_token_map)
        wm._poly_ws_props_token_map.clear()
        now = datetime.now(timezone.utc)
        self.game_start = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self.slug = "hit-mlb-test-player-2026-07-01"

    def tearDown(self):
        wm._poly_ws_props_token_map.clear()
        wm._poly_ws_props_token_map.update(self._saved)

    def _seed_entry(self, ws_at):
        wm._poly_ws_props_token_map[self.slug] = {
            "slug": self.slug, "smt": "baseball_player_hits",
            "player_name": "Test Player", "player_norm": "test player",
            "line": 1.5, "game_start": self.game_start, "event_title": "MLB props",
            "yes_ask": 0.30, "no_ask": 0.75,
            "updated_at": datetime.now(timezone.utc), "ws_at": ws_at,
        }

    def test_FG05_ws_fresh_entry_keeps_ws_prices_on_rest_reseed(self):
        self._seed_entry(ws_at=datetime.now(timezone.utc) - timedelta(seconds=3))
        events = [_props_search_event(self.slug, "Test Player", 0.50, 0.55, self.game_start)]
        kept = _update_poly_props_map(events)
        assert self.slug in kept
        entry = wm._poly_ws_props_token_map[self.slug]
        assert entry["yes_ask"] == 0.30
        assert entry["no_ask"] == 0.75

    def test_FG06_ws_stale_entry_takes_rest_prices(self):
        self._seed_entry(ws_at=datetime.now(timezone.utc) - timedelta(seconds=120))
        events = [_props_search_event(self.slug, "Test Player", 0.50, 0.55, self.game_start)]
        _update_poly_props_map(events)
        entry = wm._poly_ws_props_token_map[self.slug]
        assert entry["yes_ask"] == 0.50
        assert entry["no_ask"] == 0.55

    def test_FG07_dead_market_evicted_even_if_ws_fresh(self):
        self._seed_entry(ws_at=datetime.now(timezone.utc))
        events = [_props_search_event(self.slug, "Test Player", 0.50, 0.55,
                                      self.game_start, closed=True)]
        _update_poly_props_map(events)
        assert self.slug not in wm._poly_ws_props_token_map

    def test_FG11_vanished_old_date_entries_are_purged(self):
        # Review F14.7: slugs that stop appearing in search results were never
        # evicted — the map grew for the process lifetime. Entries with a
        # game_start before yesterday must be purged on re-seed.
        self._seed_entry(ws_at=None)
        entry = wm._poly_ws_props_token_map[self.slug]
        entry["game_start"] = (datetime.now(timezone.utc) - timedelta(days=3)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        _update_poly_props_map([])
        assert self.slug not in wm._poly_ws_props_token_map


class TestPolyWsMessageSetsWsAt(unittest.TestCase):
    """WS lite messages must stamp ws_at so REST re-seeds can detect WS freshness."""

    def setUp(self):
        self._saved = dict(wm._poly_ws_props_token_map)
        wm._poly_ws_props_token_map.clear()
        self.slug = "hit-mlb-wsat-player-2026-07-01"
        wm._poly_ws_props_token_map[self.slug] = {
            "slug": self.slug, "smt": "baseball_player_hits",
            "player_name": "Wsat Player", "player_norm": "wsat player",
            "line": 1.5, "game_start": "2026-07-01T23:00:00Z", "event_title": "MLB props",
            "yes_ask": None, "no_ask": None,
            "updated_at": datetime.now(timezone.utc),
        }

    def tearDown(self):
        wm._poly_ws_props_token_map.clear()
        wm._poly_ws_props_token_map.update(self._saved)

    def test_FG08_lite_message_sets_ws_at_and_prices(self):
        msg = {"requestId": "r1", "marketDataLite": {
            "marketSlug": self.slug,
            "bestBid": {"value": "0.40"}, "bestAsk": {"value": "0.44"},
        }}
        asyncio.run(wm._handle_poly_ws_message(msg))
        entry = wm._poly_ws_props_token_map[self.slug]
        assert entry["yes_ask"] == 0.44
        assert entry["no_ask"] == 0.60
        assert isinstance(entry.get("ws_at"), datetime)

    def test_FG09_priceless_frame_does_not_arm_freshness_window(self):
        # Review F15.2: a frame with no usable price must not stamp ws_at (or
        # bump updated_at) — otherwise it re-arms the 30s guard and blocks the
        # REST dropped-message safety net forever.
        before = wm._poly_ws_props_token_map[self.slug]["updated_at"]
        msg = {"requestId": "r1", "marketDataLite": {"marketSlug": self.slug}}
        asyncio.run(wm._handle_poly_ws_message(msg))
        entry = wm._poly_ws_props_token_map[self.slug]
        assert entry.get("ws_at") is None
        assert entry["updated_at"] == before


class TestPolyUnsubscribedSlugs(unittest.TestCase):
    """_poly_unsubscribed_slugs: slugs in either WS map missing from the live subscription."""

    def setUp(self):
        self._saved_ml = dict(wm._poly_ws_ml_token_map)
        self._saved_props = dict(wm._poly_ws_props_token_map)
        wm._poly_ws_ml_token_map.clear()
        wm._poly_ws_props_token_map.clear()
        wm._poly_ws_ml_token_map["ml-a"] = {"slug": "ml-a"}
        wm._poly_ws_ml_token_map["ml-b"] = {"slug": "ml-b"}
        wm._poly_ws_props_token_map["prop-a"] = {"slug": "prop-a"}

    def tearDown(self):
        wm._poly_ws_ml_token_map.clear()
        wm._poly_ws_ml_token_map.update(self._saved_ml)
        wm._poly_ws_props_token_map.clear()
        wm._poly_ws_props_token_map.update(self._saved_props)

    def test_DS01_all_subscribed_returns_empty(self):
        assert _poly_unsubscribed_slugs({"ml-a", "ml-b", "prop-a"}) == []

    def test_DS02_new_slugs_in_both_maps_returned(self):
        new = _poly_unsubscribed_slugs({"ml-a"})
        assert sorted(new) == ["ml-b", "prop-a"]


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, frame):
        self.sent.append(json.loads(frame))


class TestPolyDeltaSubscribeLoop(unittest.TestCase):
    """_poly_delta_subscribe_loop: subscribes map slugs missing from the live set."""

    def setUp(self):
        self._saved_ml = dict(wm._poly_ws_ml_token_map)
        self._saved_props = dict(wm._poly_ws_props_token_map)
        self._saved_interval = wm._POLY_DELTA_SUB_INTERVAL
        self._saved_max = wm._POLY_MAX_SUB_FRAMES
        wm._POLY_DELTA_SUB_INTERVAL = 0.01
        wm._poly_ws_ml_token_map.clear()
        wm._poly_ws_props_token_map.clear()
        wm._poly_ws_ml_token_map["ml-a"] = {"slug": "ml-a"}

    def tearDown(self):
        wm._POLY_DELTA_SUB_INTERVAL = self._saved_interval
        wm._POLY_MAX_SUB_FRAMES = self._saved_max
        wm._poly_ws_ml_token_map.clear()
        wm._poly_ws_ml_token_map.update(self._saved_ml)
        wm._poly_ws_props_token_map.clear()
        wm._poly_ws_props_token_map.update(self._saved_props)

    def _run(self, coro, timeout=1.0):
        async def bounded():
            task = asyncio.ensure_future(coro)
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        asyncio.run(bounded())

    def test_DS03_sends_subscribe_frame_for_new_slugs(self):
        ws = _FakeWs()
        subscribed = {"ml-a"}

        async def scenario():
            loop_task = asyncio.ensure_future(
                wm._poly_delta_subscribe_loop(ws, subscribed, itertools.count(1))
            )
            await asyncio.sleep(0.05)  # a few idle iterations
            wm._poly_ws_props_token_map["prop-new"] = {"slug": "prop-new"}
            await asyncio.sleep(0.05)
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())
        assert len(ws.sent) == 1
        frame = ws.sent[0]["subscribe"]
        assert frame["subscriptionType"] == "SUBSCRIPTION_TYPE_MARKET_DATA_LITE"
        assert frame["marketSlugs"] == ["prop-new"]
        assert "prop-new" in subscribed

    def test_DS05_clean_server_close_ends_connection_promptly(self):
        # Review F15.1: when the server closes the socket cleanly, the recv
        # loop ends without raising — the connection runner must still return
        # (with the delta loop cancelled) instead of hanging forever.
        class _ClosedWs:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration  # server closed cleanly

            async def send(self, frame):
                pass

        async def scenario():
            await asyncio.wait_for(
                wm._poly_run_connection(_ClosedWs(), {"ml-a"}, itertools.count(1)),
                timeout=2.0,
            )

        asyncio.run(scenario())  # must not raise TimeoutError

    def test_DS06_delta_loop_error_propagates_from_connection(self):
        wm._POLY_MAX_SUB_FRAMES = 1
        wm._poly_ws_props_token_map["prop-new"] = {"slug": "prop-new"}

        class _QuietWs:
            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(3600)  # connection stays open, no messages

            async def send(self, frame):
                pass

        async def scenario():
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(
                    wm._poly_run_connection(_QuietWs(), {"ml-a"}, itertools.count(1)),
                    timeout=2.0,
                )

        asyncio.run(scenario())

    def test_DS04_raises_to_consolidate_at_frame_cap(self):
        wm._POLY_MAX_SUB_FRAMES = 1  # initial subscribe already counts as frame 1
        ws = _FakeWs()
        wm._poly_ws_props_token_map["prop-new"] = {"slug": "prop-new"}

        async def scenario():
            with self.assertRaises(RuntimeError):
                await asyncio.wait_for(
                    wm._poly_delta_subscribe_loop(ws, {"ml-a"}, itertools.count(1)),
                    timeout=1.0,
                )

        asyncio.run(scenario())
        assert ws.sent == []


# ══════════════════════════════════════════════════════════════════════════════
# DOMAIN 8 — WNBA spread/total threshold markets (coverage matching)
# Design: docs/superpowers/specs/2026-07-09-wnba-spread-total-design.md
# ══════════════════════════════════════════════════════════════════════════════

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from matchers import basketball as bball

_ET_TZ = ZoneInfo("America/New_York")

# Kalshi WNBA tickers are date-only (26JUL09INDPHX — no HHMM segment).
# Anchor: midnight ET on game day, converted to UTC.
_WNBA_GAME_DT_UTC = datetime(2026, 7, 9, 0, 0, tzinfo=_ET_TZ).astimezone(timezone.utc)
# Poly start: 10 PM ET on Jul 9 = 02:00 UTC Jul 10 — same ET date, next UTC date.
_WNBA_POLY_START = "2026-07-10T02:00:00Z"


def _kp_threshold(series, ticker, identity, label, line, yes_ask, no_ask):
    return {
        "series": series,
        "ticker": ticker,
        "player_name": label,
        "player_norm": identity,
        "line": line,
        "game_dt_utc": _WNBA_GAME_DT_UTC,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_ask_qty": 10.0,
        "no_ask_qty": 10.0,
    }


def _pp_threshold(smt, identity, label, line, yes_ask, no_ask,
                  yes_is_over=True, game_start=_WNBA_POLY_START, slug="poly-slug"):
    return {
        "slug": slug,
        "smt": smt,
        "player_name": label,
        "player_norm": identity,
        "line": line,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "yes_is_over": yes_is_over,
        "game_start": game_start,
        "event_title": "Indiana vs. Phoenix",
    }


_TOTAL_SMT = "basketball_team_full_game_total"
_SPREAD_SMT = "basketball_team_full_game_spread"


def _cost(a1, a2):
    """Fee-inclusive pair cost, mirroring match_props arithmetic (rule 22)."""
    return a1 + polymarket_taker_fee(a1) + a2 + kalshi_taker_fee(a2)


class TestWnbaTeamCode(unittest.TestCase):
    """Poly WNBA abbreviations and Kalshi title names must resolve to the SAME
    canonical code — rule 23 forbids exact raw-string joins."""

    # (poly_abbr, kalshi_title_name) pairs for all 13 current WNBA teams
    PAIRS = [
        ("sea", "Seattle"), ("atl", "Atlanta"), ("ind", "Indiana"),
        ("phx", "Phoenix"), ("lv", "Las Vegas"), ("por", "Portland"),
        ("gsv", "Golden State"), ("conn", "Connecticut"), ("dal", "Dallas"),
        ("tor", "Toronto"), ("chi", "Chicago"), ("la", "Los Angeles"),
        ("ny", "New York"), ("min", "Minnesota"), ("wsh", "Washington"),
    ]

    def test_W01_all_poly_abbrs_resolve(self):
        for abbr, _ in self.PAIRS:
            self.assertIsNotNone(bball.wnba_team_code(abbr), f"abbr {abbr!r}")

    def test_W02_abbr_and_title_name_agree(self):
        for abbr, name in self.PAIRS:
            self.assertEqual(
                bball.wnba_team_code(abbr), bball.wnba_team_code(name),
                f"{abbr!r} vs {name!r}",
            )

    def test_W03_golden_state_is_valkyries_not_warriors(self):
        self.assertEqual(bball.wnba_team_code("Golden State"), "lav")

    def test_W04_new_york_is_liberty_not_knicks(self):
        self.assertEqual(bball.wnba_team_code("New York"), "nyl")

    def test_W05_unknown_team_returns_none(self):
        self.assertIsNone(bball.wnba_team_code("zz"))


class TestParseKalshiThreshold(unittest.TestCase):
    """_parse_kalshi_threshold: identity/line from floor_strike + title."""

    def test_K01_total_market(self):
        import ws_manager as wm
        parsed = wm._parse_kalshi_threshold("KXWNBATOTAL", {
            "ticker": "KXWNBATOTAL-26JUL09INDPHX-187",
            "title": "Indiana vs Phoenix",
            "floor_strike": 186.5,
            "strike_type": "greater",
        })
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["player_norm"], "total:ind-phx")
        self.assertEqual(parsed["line"], 186.5)

    def test_K02_total_identity_is_order_independent(self):
        import ws_manager as wm
        a = wm._parse_kalshi_threshold("KXWNBATOTAL", {
            "title": "Phoenix vs Indiana", "floor_strike": 186.5})
        self.assertEqual(a["player_norm"], "total:ind-phx")

    def test_K03_spread_market(self):
        import ws_manager as wm
        parsed = wm._parse_kalshi_threshold("KXWNBASPREAD", {
            "ticker": "KXWNBASPREAD-26JUL09INDPHX-PHX7",
            "title": "Phoenix wins by over 6.5 points",
            "floor_strike": 6.5,
            "strike_type": "greater",
        })
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["player_norm"], "spread:phx")
        self.assertEqual(parsed["line"], 6.5)

    def test_K04_missing_floor_strike_skipped(self):
        import ws_manager as wm
        self.assertIsNone(wm._parse_kalshi_threshold("KXWNBATOTAL", {
            "title": "Indiana vs Phoenix"}))

    def test_K05_unresolvable_team_skipped(self):
        import ws_manager as wm
        self.assertIsNone(wm._parse_kalshi_threshold("KXWNBASPREAD", {
            "title": "Springfield wins by over 6.5 points", "floor_strike": 6.5}))

    def test_K06_date_only_ticker_anchor(self):
        dt = bball._kalshi_game_date_only_utc("KXWNBATOTAL-26JUL09INDPHX-187")
        self.assertEqual(dt, _WNBA_GAME_DT_UTC)


class TestParsePolyTeamProp(unittest.TestCase):
    """_parse_poly_team_prop_market: live /v2/leagues/wnba/events shapes."""

    def _parse(self, event_title, market):
        import ws_manager as wm
        now = datetime.now(timezone.utc)
        return wm._parse_poly_team_prop_market(event_title, market, now)

    def test_P01_total_market(self):
        e = self._parse("Indiana vs. Phoenix", {
            "slug": "tsc-wnba-ind-phx-2026-07-09-161pt5",
            "sportsMarketType": _TOTAL_SMT,
            "line": 161.5,
            "title": "Over 161.5 total points",
            "gameStartTime": _WNBA_POLY_START,
            "marketSides": [
                {"long": True, "quote": {"value": "0.7800"}},
                {"long": False, "quote": {"value": "0.27"}},
            ],
        })
        self.assertIsNotNone(e)
        self.assertEqual(e["player_norm"], "total:ind-phx")
        self.assertEqual(e["line"], 161.5)
        self.assertTrue(e["yes_is_over"])
        self.assertEqual(e["yes_ask"], 0.78)
        self.assertEqual(e["no_ask"], 0.27)

    def test_P02_spread_positive_line_cover_is_short_side(self):
        # "Phoenix wins by over 10.5" — cover team phx sits on the long=False
        # side, so the market's native YES (long) is the UNDER of the cover frame.
        e = self._parse("Indiana vs. Phoenix", {
            "slug": "asc-wnba-ind-phx-2026-07-09-pos-10pt5",
            "sportsMarketType": _SPREAD_SMT,
            "line": 10.5,
            "title": "Phoenix wins by over 10.5 points",
            "gameStartTime": _WNBA_POLY_START,
            "marketSides": [
                {"team": {"abbreviation": "ind"}, "long": True, "quote": {"value": "0.8000"}},
                {"team": {"abbreviation": "phx"}, "long": False, "quote": {"value": "0.22"}},
            ],
        })
        self.assertIsNotNone(e)
        self.assertEqual(e["player_norm"], "spread:phx")
        self.assertEqual(e["line"], 10.5)
        self.assertFalse(e["yes_is_over"])
        self.assertEqual(e["yes_ask"], 0.80)
        self.assertEqual(e["no_ask"], 0.22)

    def test_P03_spread_negative_line_cover_is_long_side(self):
        e = self._parse("Indiana vs. Phoenix", {
            "slug": "asc-wnba-ind-phx-2026-07-09-neg-1pt5",
            "sportsMarketType": _SPREAD_SMT,
            "line": -1.5,
            "title": "Indiana wins by over 1.5 points",
            "gameStartTime": _WNBA_POLY_START,
            "marketSides": [
                {"team": {"abbreviation": "ind"}, "long": True, "quote": {"value": "0.47"}},
                {"team": {"abbreviation": "phx"}, "long": False, "quote": {"value": "0.55"}},
            ],
        })
        self.assertIsNotNone(e)
        self.assertEqual(e["player_norm"], "spread:ind")
        self.assertEqual(e["line"], 1.5)
        self.assertTrue(e["yes_is_over"])

    def test_P04_unparseable_title_skipped(self):
        e = self._parse("Indiana vs. Phoenix", {
            "slug": "x", "sportsMarketType": _SPREAD_SMT, "line": 10.5,
            "title": "Alternate spread special",
            "gameStartTime": _WNBA_POLY_START,
            "marketSides": [],
        })
        self.assertIsNone(e)


class TestThresholdCoverageMatching(unittest.TestCase):
    """Coverage matching: opposite sides at unequal lines, gap-free direction only.

    Kalshi over(k) YES + Poly under(p) is covered iff p >= k;
    Poly over(p) YES + Kalshi under(k) NO is covered iff p <= k.
    All price fixtures validated against fees.py and F1/F2/F4 (rule 22).
    Note: F3 (edge cap) is unreachable for threshold pairs while F2 passes —
    a <=0.15 mid gap bounds the fee-inclusive edge below 10 cents — so there
    is deliberately no F3 fixture here.
    """

    def _match(self, kalshi_props, poly_props):
        import ws_manager as wm
        with patch.object(wm, "_append_ghost_log"):
            return wm.match_props(kalshi_props, poly_props)

    def test_T01_total_arb_at_lower_poly_line_kalshi_no_direction(self):
        # Poly over(185.5) + Kalshi NO under(186.5): p <= k, covered (both win at 186).
        kp = _kp_threshold("KXWNBATOTAL", "KXWNBATOTAL-26JUL09INDPHX-187",
                           "total:ind-phx", "Indiana vs Phoenix", 186.5,
                           yes_ask=0.52, no_ask=0.50)
        pp = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                           185.5, yes_ask=0.40, no_ask=0.62)
        matches = self._match([kp], [pp])
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual(m["direction"], "Poly YES + Kalshi NO")
        self.assertEqual(m["kalshi_line"], 186.5)
        self.assertEqual(m["poly_line"], 185.5)
        self.assertEqual(m["stat_type"], "total")
        self.assertAlmostEqual(m["total_cost"], round(_cost(0.40, 0.50), 4))

    def test_T02_gap_direction_suppressed_despite_price(self):
        # Kalshi YES over(186.5) + Poly under(185.5) leaves T=186 losing both
        # legs — must NOT match even though the cost beats ARB_THRESHOLD.
        kp = _kp_threshold("KXWNBATOTAL", "KXWNBATOTAL-26JUL09INDPHX-187",
                           "total:ind-phx", "Indiana vs Phoenix", 186.5,
                           yes_ask=0.30, no_ask=0.72)
        pp = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                           185.5, yes_ask=0.42, no_ask=0.60)
        # sanity: the gapped direction would price as an arb if not gated
        self.assertLess(_cost(0.60, 0.30), ARB_THRESHOLD)
        self.assertEqual(self._match([kp], [pp]), [])

    def test_T03_covered_kalshi_yes_direction_at_higher_poly_line(self):
        # Kalshi YES over(186.5) + Poly under(188.5): p >= k, covered.
        kp = _kp_threshold("KXWNBATOTAL", "KXWNBATOTAL-26JUL09INDPHX-187",
                           "total:ind-phx", "Indiana vs Phoenix", 186.5,
                           yes_ask=0.30, no_ask=0.72)
        pp = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                           188.5, yes_ask=0.40, no_ask=0.62)
        matches = self._match([kp], [pp])
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual(m["direction"], "Kalshi YES + Poly NO")
        self.assertEqual(m["poly_line"], 188.5)
        self.assertAlmostEqual(m["total_cost"], round(
            0.30 + kalshi_taker_fee(0.30) + 0.62 + polymarket_taker_fee(0.62), 4))

    def test_T04_tightest_line_selected_not_cheapest(self):
        # Two legal Poly lines for the Kalshi-NO direction; the tighter 185.5
        # is chosen even though 179.5 is cheaper (irrationally cheap far lines
        # are the stale-quote ghost class).
        kp = _kp_threshold("KXWNBATOTAL", "KXWNBATOTAL-26JUL09INDPHX-187",
                           "total:ind-phx", "Indiana vs Phoenix", 186.5,
                           yes_ask=0.52, no_ask=0.50)
        pp_near = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                                185.5, yes_ask=0.40, no_ask=0.62, slug="near")
        pp_far = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                               179.5, yes_ask=0.38, no_ask=0.64, slug="far")
        matches = self._match([kp], [pp_far, pp_near])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["poly_line"], 185.5)
        self.assertEqual(matches[0]["leg1_ask"], 0.40)

    def test_T05_equal_lines_allow_either_direction(self):
        # p == k satisfies both coverage inequalities (pure complement case).
        # Both directions can never arb SIMULTANEOUSLY with sane books (the
        # four asks sum to ~2.04 > 2*ARB_THRESHOLD), so each is priced in turn.
        kp_a = _kp_threshold("KXWNBASPREAD", "KXWNBASPREAD-26JUL09INDPHX-PHX2",
                             "spread:phx", "Phoenix", 1.5,
                             yes_ask=0.40, no_ask=0.62)
        pp_a = _pp_threshold(_SPREAD_SMT, "spread:phx", "Phoenix", 1.5,
                             yes_ask=0.50, no_ask=0.52, yes_is_over=False)
        matches = self._match([kp_a], [pp_a])
        self.assertEqual([m["direction"] for m in matches],
                         ["Kalshi YES + Poly NO"])
        self.assertEqual(matches[0]["poly_line"], matches[0]["kalshi_line"])

        kp_b = _kp_threshold("KXWNBASPREAD", "KXWNBASPREAD-26JUL09INDPHX-PHX2",
                             "spread:phx", "Phoenix", 1.5,
                             yes_ask=0.56, no_ask=0.52)
        pp_b = _pp_threshold(_SPREAD_SMT, "spread:phx", "Phoenix", 1.5,
                             yes_ask=0.62, no_ask=0.40, yes_is_over=False)
        matches = self._match([kp_b], [pp_b])
        self.assertEqual([m["direction"] for m in matches],
                         ["Poly YES + Kalshi NO"])

    def test_T06_spread_frame_translation(self):
        # Poly market stores native long/short prices; yes_is_over=False means
        # the cover-frame under is the native yes_ask.
        kp = _kp_threshold("KXWNBASPREAD", "KXWNBASPREAD-26JUL09INDPHX-PHX7",
                           "spread:phx", "Phoenix", 6.5,
                           yes_ask=0.38, no_ask=0.64)
        pp = _pp_threshold(_SPREAD_SMT, "spread:phx", "Phoenix", 7.5,
                           yes_ask=0.52, no_ask=0.50, yes_is_over=False)
        matches = self._match([kp], [pp])
        self.assertEqual(len(matches), 1)
        m = matches[0]
        self.assertEqual(m["direction"], "Kalshi YES + Poly NO")
        self.assertEqual(m["leg2_ask"], 0.52)  # native yes_ask, translated to under
        self.assertAlmostEqual(m["total_cost"], round(
            0.38 + kalshi_taker_fee(0.38) + 0.52 + polymarket_taker_fee(0.52), 4))

    def test_T07_different_et_date_no_match(self):
        kp = _kp_threshold("KXWNBATOTAL", "KXWNBATOTAL-26JUL09INDPHX-187",
                           "total:ind-phx", "Indiana vs Phoenix", 186.5,
                           yes_ask=0.52, no_ask=0.50)
        pp = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                           185.5, yes_ask=0.40, no_ask=0.62,
                           game_start="2026-07-11T02:00:00Z")  # ET Jul 10
        self.assertEqual(self._match([kp], [pp]), [])

    def test_T08_different_identity_no_match(self):
        kp = _kp_threshold("KXWNBATOTAL", "KXWNBATOTAL-26JUL09SEAATL-172",
                           "total:atl-sea", "Seattle vs Atlanta", 171.5,
                           yes_ask=0.52, no_ask=0.50)
        pp = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                           170.5, yes_ask=0.40, no_ask=0.62)
        self.assertEqual(self._match([kp], [pp]), [])

    def test_T09_pinned_leg_suppressed(self):
        kp = _kp_threshold("KXWNBATOTAL", "KXWNBATOTAL-26JUL09INDPHX-187",
                           "total:ind-phx", "Indiana vs Phoenix", 186.5,
                           yes_ask=0.52, no_ask=0.50)
        pp = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                           185.5, yes_ask=0.02, no_ask=0.99)
        self.assertEqual(self._match([kp], [pp]), [])

    def test_T10_player_props_unaffected_by_threshold_entries(self):
        kp_player = {
            "series": "KXNBAPTS", "ticker": "KXNBAPTS-26JUL091900INDPHX-XX-20",
            "player_name": "Jane Doe", "player_norm": "jane doe", "line": 20,
            "game_dt_utc": datetime(2026, 7, 9, 23, 0, tzinfo=timezone.utc),
            "yes_ask": 0.40, "no_ask": 0.62, "yes_ask_qty": 5.0, "no_ask_qty": 5.0,
        }
        pp_player = {
            "slug": "pts-slug", "smt": "basketball_player_points",
            "player_name": "Jane Doe", "player_norm": "jane doe", "line": 20,
            "yes_ask": 0.55, "no_ask": 0.52,
            "game_start": "2026-07-09T23:00:00Z", "event_title": "IND @ PHX",
        }
        pp_thresh = _pp_threshold(_TOTAL_SMT, "total:ind-phx", "Indiana vs. Phoenix",
                                  185.5, yes_ask=0.40, no_ask=0.62)
        matches = self._match([kp_player], [pp_player, pp_thresh])
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["direction"], "Kalshi YES + Poly NO")
        self.assertEqual(matches[0]["player_name"], "Jane Doe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
