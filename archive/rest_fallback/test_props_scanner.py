"""
PICT-derived unit tests for props_scanner.py

PICT Models used:
─────────────────────────────────────────────────────────────────────────────

Model A: _kalshi_ask(market, side)
  YesAskDollars: valid_string, None
  YesAskCents:   valid_int, None
  SideArg:       yes, no
  ValueRange:    in_range, zero, one, negative, above_one
  Constraints:
    IF [ValueRange] <> "in_range" THEN [YesAskDollars] = "valid_string";

Model B: _kalshi_parse_title(title)
  TitleFormat: well_formed, no_colon, no_plus, empty, just_colon, accented_name

Model C: _kalshi_game_dt_utc(ticker)
  TickerFormat: well_formed, no_dash, bad_segment, bad_month, truncated

Model D: _poly_ask(side)
  QuotePresent: present, absent, None_value
  QuoteValue:   in_range, zero, one, negative, invalid_string

Model E: match_props(kalshi_props, poly_props)
  KalshiYesAsk:  valid_float, None
  KalshiNoAsk:   valid_float, None
  PolyYesAsk:    valid_float, None
  PolyNoAsk:     valid_float, None
  TotalCost:     below_threshold, at_threshold, above_threshold
  PlayerMatch:   exact, accent_normalized, no_match
  LineMatch:     same, different
  GameDateMatch: same_date, different_date
  Constraints:
    IF [TotalCost] = "below_threshold" THEN [PlayerMatch] <> "no_match";
    IF [TotalCost] = "below_threshold" THEN [LineMatch] = "same";
    IF [TotalCost] = "below_threshold" THEN [GameDateMatch] = "same_date";
    IF [PlayerMatch] = "no_match"      THEN [TotalCost] <> "below_threshold";
    IF [LineMatch]   = "different"     THEN [TotalCost] <> "below_threshold";
    IF [GameDateMatch] = "different_date" THEN [TotalCost] <> "below_threshold";

Generated test cases: 51
"""

import pytest
from datetime import datetime, timezone, timedelta, date

from props_scanner import (
    _normalize,
    _kalshi_game_dt_utc,
    _kalshi_parse_title,
    _kalshi_ask,
    _poly_ask,
    match_props,
    ARB_THRESHOLD,
    SERIES_TO_SMT,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


def _make_kalshi_prop(
    *,
    series="KXMLBHIT",
    ticker="KXMLBHIT-26JUN281920NYYBOS",
    player_name="Byron Buxton",
    line=2,
    yes_ask=0.45,
    no_ask=0.50,
    game_dt_utc=None,
) -> dict:
    if game_dt_utc is None:
        game_dt_utc = datetime.combine(_today_utc(), datetime.min.time()).replace(tzinfo=timezone.utc)
    return {
        "series": series,
        "ticker": ticker,
        "player_name": player_name,
        "player_norm": _normalize(player_name),
        "line": line,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "game_dt_utc": game_dt_utc,
    }


def _make_poly_prop(
    *,
    smt="baseball_player_hits",
    player_name="Byron Buxton",
    line=2,
    yes_ask=0.44,
    no_ask=0.51,
    game_start=None,
    event_title="NYY vs BOS",
) -> dict:
    if game_start is None:
        game_start = _today_utc().strftime("%Y-%m-%d") + "T20:00:00Z"
    return {
        "smt": smt,
        "player_name": player_name,
        "player_norm": _normalize(player_name),
        "line": line,
        "yes_ask": yes_ask,
        "no_ask": no_ask,
        "game_start": game_start,
        "event_title": event_title,
    }


# ─── Model A: _kalshi_ask ─────────────────────────────────────────────────────

class TestKalshiAsk:
    """PICT Model A — 12 test cases covering yes_ask_dollars / yes_ask fallback and value validity."""

    # A1: yes_ask_dollars present, valid decimal string, side=yes → parsed correctly
    def test_yes_ask_dollars_valid(self):
        assert _kalshi_ask({"yes_ask_dollars": "0.45"}, "yes") == pytest.approx(0.45)

    # A2: no_ask_dollars present, valid decimal string, side=no → parsed correctly
    def test_no_ask_dollars_valid(self):
        assert _kalshi_ask({"no_ask_dollars": "0.52"}, "no") == pytest.approx(0.52)

    # A3: yes_ask_dollars=None, yes_ask fallback present as int cents → converted to dollars
    def test_fallback_cents_int(self):
        assert _kalshi_ask({"yes_ask_dollars": None, "yes_ask": 55}, "yes") == pytest.approx(0.55)

    # A4: yes_ask_dollars=None, yes_ask fallback as string cents → converted to dollars
    def test_fallback_cents_string(self):
        assert _kalshi_ask({"yes_ask_dollars": None, "yes_ask": "72"}, "yes") == pytest.approx(0.72)

    # A5: both fields absent → None
    def test_both_fields_absent(self):
        assert _kalshi_ask({}, "yes") is None

    # A6: yes_ask_dollars="0" → out of range (val <= 0) → None
    def test_zero_value_returns_none(self):
        assert _kalshi_ask({"yes_ask_dollars": "0"}, "yes") is None

    # A7: yes_ask_dollars="1" → out of range (val >= 1) → None
    def test_one_value_returns_none(self):
        assert _kalshi_ask({"yes_ask_dollars": "1"}, "yes") is None

    # A8: yes_ask_dollars="-0.5" → negative → None
    def test_negative_value_returns_none(self):
        assert _kalshi_ask({"yes_ask_dollars": "-0.5"}, "yes") is None

    # A9: yes_ask_dollars="1.5" → above one → None
    def test_above_one_returns_none(self):
        assert _kalshi_ask({"yes_ask_dollars": "1.5"}, "yes") is None

    # A10: yes_ask_dollars="not_a_float" → ValueError → None
    def test_invalid_string_returns_none(self):
        assert _kalshi_ask({"yes_ask_dollars": "not_a_float"}, "yes") is None

    # A11: yes_ask_dollars=None, yes_ask fallback also None → None
    def test_fallback_also_none(self):
        assert _kalshi_ask({"yes_ask_dollars": None, "yes_ask": None}, "yes") is None

    # A12: yes_ask_dollars present, side="no" — looks up no_ask_dollars, which is absent → None
    def test_wrong_side_key_absent(self):
        assert _kalshi_ask({"yes_ask_dollars": "0.45"}, "no") is None


# ─── Model B: _kalshi_parse_title ─────────────────────────────────────────────

class TestKalshiParseTitle:
    """PICT Model B — 6 test cases covering title formats."""

    # B1: Well-formed "Player Name: N+ stat?" → (player, N)
    def test_well_formed(self):
        assert _kalshi_parse_title("Byron Buxton: 2+ hits?") == ("Byron Buxton", 2)

    # B2: Multi-word player name with high line number
    def test_multi_word_name_high_line(self):
        assert _kalshi_parse_title("Shohei Ohtani: 10+ strikeouts?") == ("Shohei Ohtani", 10)

    # B3: Missing colon → no match → None
    def test_no_colon_returns_none(self):
        assert _kalshi_parse_title("Byron Buxton 2+ hits?") is None

    # B4: Missing plus sign after number → no match → None
    def test_no_plus_returns_none(self):
        assert _kalshi_parse_title("Byron Buxton: 2 hits?") is None

    # B5: Empty string → None
    def test_empty_string_returns_none(self):
        assert _kalshi_parse_title("") is None

    # B6: Player name with accented characters parsed correctly
    def test_accented_name_parsed(self):
        result = _kalshi_parse_title("José Abreu: 1+ home runs?")
        assert result is not None
        assert result[0] == "José Abreu"
        assert result[1] == 1


# ─── Model C: _kalshi_game_dt_utc ─────────────────────────────────────────────

class TestKalshiGameDtUtc:
    """PICT Model C — 5 test cases covering ticker datetime parsing."""

    # C1: Well-formed ticker → UTC datetime (ET + 4h)
    def test_well_formed_ticker(self):
        # 26JUN281920 in ET = 23:20 UTC (1920 + 4h = 2320 → next field)
        dt = _kalshi_game_dt_utc("KXMLBHIT-26JUN281920NYYBOS")
        assert dt is not None
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 23
        assert dt.minute == 20

    # C2: Ticker month segment parsed with correct year
    def test_ticker_year_and_month(self):
        dt = _kalshi_game_dt_utc("KXMLBHIT-26JUN281340NYYBOS")
        assert dt is not None
        assert dt.year == 2026
        assert dt.month == 6
        assert dt.day == 28

    # C3: No dash in ticker → IndexError handled → None
    def test_no_dash_returns_none(self):
        assert _kalshi_game_dt_utc("KXMLBHIT26JUN281920") is None

    # C4: Segment too short to parse → ValueError → None
    def test_truncated_segment_returns_none(self):
        assert _kalshi_game_dt_utc("KXMLBHIT-26JUN") is None

    # C5: Invalid month abbreviation → ValueError → None
    def test_bad_month_returns_none(self):
        assert _kalshi_game_dt_utc("KXMLBHIT-26ZZZ281920NYYBOS") is None


# ─── Model D: _poly_ask ───────────────────────────────────────────────────────

class TestPolyAsk:
    """PICT Model D — 9 test cases covering Polymarket marketSide quote parsing."""

    # D1: quote.value in range → parsed correctly
    def test_valid_quote_value(self):
        assert _poly_ask({"quote": {"value": "0.44"}}) == pytest.approx(0.44)

    # D2: quote key absent → None
    def test_no_quote_key(self):
        assert _poly_ask({}) is None

    # D3: quote=None → None
    def test_quote_none(self):
        assert _poly_ask({"quote": None}) is None

    # D4: quote present, value key absent → None
    def test_value_key_absent(self):
        assert _poly_ask({"quote": {}}) is None

    # D5: value="0" → out of range → None
    def test_zero_value(self):
        assert _poly_ask({"quote": {"value": "0"}}) is None

    # D6: value="1" → out of range (not < 1) → None
    def test_one_value(self):
        assert _poly_ask({"quote": {"value": "1"}}) is None

    # D7: value="-0.1" → negative → None
    def test_negative_value(self):
        assert _poly_ask({"quote": {"value": "-0.1"}}) is None

    # D8: value="1.5" → above one → None
    def test_above_one(self):
        assert _poly_ask({"quote": {"value": "1.5"}}) is None

    # D9: value="not_a_float" → ValueError → None
    def test_invalid_string(self):
        assert _poly_ask({"quote": {"value": "not_a_float"}}) is None


# ─── Model E: match_props ─────────────────────────────────────────────────────

class TestMatchProps:
    """
    PICT Model E — 15 test cases (pairwise) for the core arb detection logic.

    Test matrix:
    # | KYes  | KNo   | PYes  | PNo   | TotalCost | PlayerMatch | LineMatch | GameDate   | Expected
    1 | valid | valid | valid | valid | below     | exact       | same      | same_date  | ≥1
    2 | valid | valid | valid | valid | at        | exact       | same      | same_date  | 0
    3 | valid | valid | valid | valid | above     | exact       | same      | same_date  | 0
    4 | valid | valid | valid | valid | below→    | no_match    | same      | same_date  | 0  (constraint: no_match→above)
    5 | valid | valid | valid | valid | below→    | exact       | different | same_date  | 0  (constraint: diff line→above)
    6 | valid | valid | valid | valid | below→    | exact       | same      | diff_date  | 0  (constraint: diff date→above)
    7 | valid | valid | valid | valid | below     | accent      | same      | same_date  | ≥1
    8 | valid | None  | valid | valid | above     | exact       | same      | same_date  | 0
    9 | valid | valid | None  | valid | above     | exact       | same      | same_date  | 0
   10 | None  | valid | valid | valid | above     | exact       | same      | same_date  | 0
   11 | valid | valid | valid | None  | above     | exact       | same      | same_date  | 0
   12 | None  | None  | None  | None  | above     | exact       | same      | same_date  | 0
   13 | valid | valid | valid | valid | at        | exact       | same      | same_date  | 0  (duplicate of T2 at_threshold)
   14 | valid | valid | valid | valid | above     | no_match    | different | diff_date  | 0
   15 | valid | valid | valid | None  | below     | accent      | same      | same_date  | ≥1 (dir A fires: PYes+KNo)
    """

    def _today_dt(self) -> datetime:
        d = _today_utc()
        return datetime(d.year, d.month, d.day, 20, 0, 0, tzinfo=timezone.utc)

    def _yesterday_dt(self) -> datetime:
        return self._today_dt() - timedelta(days=1)

    def _today_str(self) -> str:
        return _today_utc().strftime("%Y-%m-%d")

    def _yesterday_str(self) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    # T1: Both directions available, total below threshold — arb fires
    def test_t1_both_directions_below_threshold(self):
        # Dir A: PYes(0.44) + KNo(0.50) = 0.94 < 0.96 ✓
        # Dir B: KYes(0.45) + PNo(0.51) = 0.96 → not < threshold
        kp = [_make_kalshi_prop(yes_ask=0.45, no_ask=0.50, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.44, no_ask=0.51)]
        arbs = match_props(kp, pp)
        assert len(arbs) >= 1
        directions = {a["direction"] for a in arbs}
        assert "Poly YES + Kalshi NO" in directions

    # T2: Total at threshold (0.96) — no arb
    def test_t2_total_at_threshold_no_arb(self):
        kp = [_make_kalshi_prop(yes_ask=0.48, no_ask=0.48, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.48, no_ask=0.48)]
        arbs = match_props(kp, pp)
        assert arbs == []

    # T3: Total above threshold (1.05) — no arb
    def test_t3_total_above_threshold_no_arb(self):
        kp = [_make_kalshi_prop(yes_ask=0.55, no_ask=0.55, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.55, no_ask=0.55)]
        arbs = match_props(kp, pp)
        assert arbs == []

    # T4: Player name no match — no arb regardless of prices
    def test_t4_player_no_match_no_arb(self):
        kp = [_make_kalshi_prop(player_name="Byron Buxton", yes_ask=0.44, no_ask=0.44, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(player_name="Shohei Ohtani", yes_ask=0.44, no_ask=0.44)]
        arbs = match_props(kp, pp)
        assert arbs == []

    # T5: Line differs (Kalshi=2, Poly=3) — no arb
    def test_t5_line_mismatch_no_arb(self):
        kp = [_make_kalshi_prop(line=2, yes_ask=0.44, no_ask=0.44, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(line=3, yes_ask=0.44, no_ask=0.44)]
        arbs = match_props(kp, pp)
        assert arbs == []

    # T6: Game date differs (Kalshi=today, Poly=yesterday) — no arb
    def test_t6_game_date_mismatch_no_arb(self):
        kp = [_make_kalshi_prop(yes_ask=0.44, no_ask=0.44, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.44, no_ask=0.44,
                              game_start=self._yesterday_str() + "T20:00:00Z")]
        arbs = match_props(kp, pp)
        assert arbs == []

    # T7: Player name with accent normalized — still matches and arb fires
    def test_t7_accent_normalized_match(self):
        kp = [_make_kalshi_prop(player_name="Jose Abreu", yes_ask=0.44, no_ask=0.44, game_dt_utc=self._today_dt())]
        # Poly uses accented version — _normalize strips accents
        pp = [_make_poly_prop(player_name="José Abreu", yes_ask=0.44, no_ask=0.44)]
        arbs = match_props(kp, pp)
        assert len(arbs) >= 1

    # T8: KalshiNoAsk=None — Direction A (Poly YES + Kalshi NO) skipped
    def test_t8_kalshi_no_ask_none_dir_a_skipped(self):
        kp = [_make_kalshi_prop(yes_ask=0.44, no_ask=None, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.44, no_ask=0.44)]
        arbs = match_props(kp, pp)
        directions = {a["direction"] for a in arbs}
        assert "Poly YES + Kalshi NO" not in directions

    # T9: PolyYesAsk=None — Direction A skipped
    def test_t9_poly_yes_ask_none_dir_a_skipped(self):
        kp = [_make_kalshi_prop(yes_ask=0.44, no_ask=0.44, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=None, no_ask=0.44)]
        arbs = match_props(kp, pp)
        directions = {a["direction"] for a in arbs}
        assert "Poly YES + Kalshi NO" not in directions

    # T10: KalshiYesAsk=None — Direction B (Kalshi YES + Poly NO) skipped
    def test_t10_kalshi_yes_ask_none_dir_b_skipped(self):
        kp = [_make_kalshi_prop(yes_ask=None, no_ask=0.44, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.44, no_ask=0.44)]
        arbs = match_props(kp, pp)
        directions = {a["direction"] for a in arbs}
        assert "Kalshi YES + Poly NO" not in directions

    # T11: PolyNoAsk=None — Direction B skipped
    def test_t11_poly_no_ask_none_dir_b_skipped(self):
        kp = [_make_kalshi_prop(yes_ask=0.44, no_ask=0.44, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.44, no_ask=None)]
        arbs = match_props(kp, pp)
        directions = {a["direction"] for a in arbs}
        assert "Kalshi YES + Poly NO" not in directions

    # T12: All asks None — no arb, no crash
    def test_t12_all_asks_none_no_arb(self):
        kp = [_make_kalshi_prop(yes_ask=None, no_ask=None, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=None, no_ask=None)]
        arbs = match_props(kp, pp)
        assert arbs == []

    # T13: Threshold boundary — total exactly 0.96 → no arb (strict <)
    def test_t13_boundary_exactly_threshold(self):
        # Poly YES 0.46 + Kalshi NO 0.50 = 0.96 exactly
        kp = [_make_kalshi_prop(yes_ask=0.50, no_ask=0.50, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.46, no_ask=0.50)]
        arbs = match_props(kp, pp)
        assert arbs == []

    # T14: Multiple mismatches — no arb
    def test_t14_all_mismatches_no_arb(self):
        kp = [_make_kalshi_prop(player_name="Player A", line=1, yes_ask=0.55, no_ask=0.55,
                                game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(player_name="Player B", line=2, yes_ask=0.55, no_ask=0.55,
                              game_start=self._yesterday_str() + "T20:00:00Z")]
        arbs = match_props(kp, pp)
        assert arbs == []

    # T15: Dir A fires when PolyNo=None (Dir B skipped), accent player match
    def test_t15_dir_a_fires_poly_no_absent_accent_match(self):
        # Poly YES(0.44) + Kalshi NO(0.44) = 0.88 < 0.96 → Dir A fires
        # Dir B: KalshiYES + PolyNO=None → skipped
        kp = [_make_kalshi_prop(player_name="Jose Abreu", yes_ask=0.44, no_ask=0.44,
                                game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(player_name="José Abreu", yes_ask=0.44, no_ask=None)]
        arbs = match_props(kp, pp)
        assert len(arbs) == 1
        assert arbs[0]["direction"] == "Poly YES + Kalshi NO"


# ─── match_props output contract ─────────────────────────────────────────────

class TestMatchPropsOutputContract:
    """Verify arb result dict fields and sort order."""

    def _today_dt(self) -> datetime:
        d = _today_utc()
        return datetime(d.year, d.month, d.day, 20, 0, 0, tzinfo=timezone.utc)

    def _base_arb(self):
        # Dir A: 0.44 + 0.44 = 0.88 → gap = 8¢
        kp = [_make_kalshi_prop(yes_ask=0.44, no_ask=0.44, game_dt_utc=self._today_dt())]
        pp = [_make_poly_prop(yes_ask=0.44, no_ask=0.44)]
        return match_props(kp, pp)

    def test_output_contains_required_fields(self):
        arbs = self._base_arb()
        assert arbs
        required = {"direction", "player_name", "stat_type", "line", "leg1", "leg1_ask",
                    "leg2", "leg2_ask", "total_cost", "gap_cents", "kalshi_ticker", "poly_smt"}
        assert required <= arbs[0].keys()

    def test_gap_cents_computed_correctly(self):
        arbs = self._base_arb()
        arb = next(a for a in arbs if a["direction"] == "Poly YES + Kalshi NO")
        expected_gap = round((ARB_THRESHOLD - (0.44 + 0.44)) * 100, 2)
        assert arb["gap_cents"] == pytest.approx(expected_gap)

    def test_total_cost_computed_correctly(self):
        arbs = self._base_arb()
        arb = next(a for a in arbs if a["direction"] == "Poly YES + Kalshi NO")
        assert arb["total_cost"] == pytest.approx(round(0.44 + 0.44, 4))

    def test_results_sorted_by_gap_descending(self):
        # Two arbs with different gaps
        kp = [
            _make_kalshi_prop(player_name="Player A", ticker="KXMLBHIT-T1",
                              yes_ask=0.30, no_ask=0.30, game_dt_utc=self._today_dt()),
            _make_kalshi_prop(player_name="Player B", ticker="KXMLBHIT-T2",
                              yes_ask=0.44, no_ask=0.44, game_dt_utc=self._today_dt()),
        ]
        pp = [
            _make_poly_prop(player_name="Player A", yes_ask=0.30, no_ask=0.30),
            _make_poly_prop(player_name="Player B", yes_ask=0.44, no_ask=0.44),
        ]
        arbs = match_props(kp, pp)
        assert arbs[0]["gap_cents"] >= arbs[-1]["gap_cents"]

    def test_stat_type_stripped_of_prefix(self):
        arbs = self._base_arb()
        assert arbs[0]["stat_type"] == "hits"  # stripped "baseball_player_"

    def test_empty_kalshi_list(self):
        arbs = match_props([], [_make_poly_prop()])
        assert arbs == []

    def test_empty_poly_list(self):
        arbs = match_props([_make_kalshi_prop(yes_ask=0.44, no_ask=0.44,
                                              game_dt_utc=self._today_dt())], [])
        assert arbs == []

    def test_both_empty(self):
        assert match_props([], []) == []


# ─── _normalize ───────────────────────────────────────────────────────────────

class TestNormalize:
    """Spot-check _normalize for the player matching use case."""

    def test_lowercases(self):
        assert _normalize("Byron BUXTON") == "byron buxton"

    def test_strips_accents(self):
        assert _normalize("José Abreu") == "jose abreu"

    def test_collapses_extra_spaces(self):
        assert _normalize("  Byron   Buxton  ") == "byron buxton"

    def test_removes_punctuation(self):
        assert _normalize("Shohei Ohtani.") == "shohei ohtani"

    def test_empty_string(self):
        assert _normalize("") == ""


# ─── SERIES_TO_SMT coverage ──────────────────────────────────────────────────

class TestSeriesMapping:
    """Ensure all supported Kalshi series map to Poly sportsMarketType."""

    def test_hits_series_maps(self):
        assert SERIES_TO_SMT["KXMLBHIT"] == "baseball_player_hits"

    def test_hr_series_maps(self):
        assert SERIES_TO_SMT["KXMLBHR"] == "baseball_player_home_runs"

    def test_tb_series_maps(self):
        assert SERIES_TO_SMT["KXMLBTB"] == "baseball_player_total_bases"

    def test_no_unexpected_series(self):
        assert set(SERIES_TO_SMT.keys()) == {"KXMLBHIT", "KXMLBHR", "KXMLBTB"}
