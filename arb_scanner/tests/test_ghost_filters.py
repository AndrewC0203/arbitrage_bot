"""
Ghost-filter regression tests — Layer 1 of the ghost-free arb design
(docs/superpowers/specs/2026-07-02-ghost-free-arbs-design.md).

Suppression fixtures are REAL ghosts pulled from arb_log.jsonl, live capture
2026-07-02 03:31-03:35 UTC (the "pasted ghosts" evidence window). Each one was
emitted as a prop_arb open by the pre-filter scanner and must now be suppressed
by exactly the reason the design's taxonomy assigns it.

Quote conventions (rule 18 / marketDataLite): both venues carry YES-side
quotes only; yes_bid = 1 - no_ask, mid = (yes_ask + yes_bid) / 2.

Run: python -m pytest tests/test_ghost_filters.py -v
"""

import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import ws_manager as wm

_GAME = datetime(2026, 7, 2, 2, 10, tzinfo=timezone.utc)


def _kp(yes_ask, no_ask, series="KXMLBHIT", ticker="K-GHOST-1", line=1):
    return {"series": series, "ticker": ticker, "player_name": "A B",
            "player_norm": "a b", "line": line, "yes_ask": yes_ask,
            "no_ask": no_ask, "game_dt_utc": _GAME, "updated_at": _GAME}


def _pp(yes_ask, no_ask, smt="baseball_player_hits", line=1):
    return {"smt": smt, "player_norm": "a b", "player_name": "A B",
            "line": line, "yes_ask": yes_ask, "no_ask": no_ask,
            "game_start": _GAME.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event_title": "X"}


class GhostFilterBase(unittest.TestCase):
    def setUp(self):
        wm._ghost_stats = wm.GhostFilterStats()
        self.ghost_log = []
        self.events = []
        self._p1 = patch.object(wm, "_append_ghost_log", self.ghost_log.append)
        self._p2 = patch.object(wm, "log_event", self.events.append)
        self._p1.start()
        self._p2.start()
        self.addCleanup(self._p1.stop)
        self.addCleanup(self._p2.stop)

    def assert_suppressed(self, kp, pp, reason):
        arbs = wm.match_props([kp], [pp])
        self.assertEqual(arbs, [])
        self.assertEqual(wm._ghost_stats.totals[reason], 1,
                         f"expected 1 x {reason}, got {wm._ghost_stats.totals}")


class TestPastedGhostsSuppressed(GhostFilterBase):
    """Every ghost from the 2026-07-02 capture must be suppressed (success criterion 1)."""

    def test_elly_de_la_cruz_resolved_poly_leg_is_pinned(self):
        # 03:31:43 KXMLBTB-...-CINEDELACRUZ44-2: Kalshi YES=0.31 + Poly NO=0.01,
        # logged gap 64c. Poly yes_bid = 0.99 => market resolved in-game (F1).
        # Poly yes_ask was null — pinned must win over one_sided so the taxonomy
        # reads "resolved market", not "missing side".
        self.assert_suppressed(
            _kp(0.31, 0.71, series="KXMLBTB", line=2),
            _pp(None, 0.01, smt="baseball_player_total_bases", line=2),
            "pinned")

    def test_eldridge_resolved_kalshi_leg_is_pinned(self):
        # 03:33:48 KXMLBHIT-...-SFBELDRIDGE8-1: Poly YES=0.75 + Kalshi NO=0.01,
        # logged gap 20c. Kalshi yes_bid = 0.99 — game decided, hit recorded.
        self.assert_suppressed(_kp(0.99, 0.01), _pp(0.75, 0.99), "pinned")

    def test_devers_resolved_kalshi_leg_is_pinned(self):
        # 03:34:50 KXMLBHIT-...-SFRDEVERS16-1: Poly YES=0.45 + Kalshi NO=0.01,
        # logged gap 50c. Kalshi pinned fires before Poly's crossed book (F4).
        self.assert_suppressed(_kp(0.95, 0.01), _pp(0.45, 0.11), "pinned")

    def test_low_yes_ask_side_is_pinned_too(self):
        # F1's other arm: yes_ask <= 1 - GHOST_PIN_PROB (effectively resolved NO).
        self.assert_suppressed(_kp(0.02, 0.99), _pp(0.50, 0.50), "pinned")

    def test_marte_stale_kalshi_book_is_mid_disagreement(self):
        # 03:33:15 KXMLBHR-...-AZKMARTE4-1: Kalshi YES=0.06 + Poly NO=0.53.
        # Kalshi mid ~0.055 vs Poly mid ~0.50 — the mid-canyon signature the
        # Kalshi-only REST confirm structurally cannot catch (F2).
        self.assert_suppressed(
            _kp(0.06, 0.95, series="KXMLBHR"),
            _pp(0.53, 0.53, smt="baseball_player_home_runs"),
            "mid_disagreement")

    def test_crossed_book_is_spread(self):
        # Kalshi yes_ask 0.50 < yes_bid 0.55 (no_ask 0.45): crossed = stale (F4).
        self.assert_suppressed(_kp(0.50, 0.45), _pp(0.42, 0.61), "spread")

    def test_canyon_wide_book_is_spread(self):
        # Kalshi spread 0.30 - 0.05 = 0.25 > GHOST_MAX_SPREAD: dead book (F4).
        self.assert_suppressed(_kp(0.30, 0.95), _pp(0.58, 0.44), "spread")

    def test_too_good_edge_is_edge_cap(self):
        # Sane books, mids 0.149 apart (passes F2), but total 0.8595 => gap
        # 10.05c > GHOST_MAX_GAP_CENTS: presumed data error (F3).
        self.assert_suppressed(_kp(0.40, 0.60), _pp(0.549, 0.451), "edge_cap")

    def test_missing_side_is_one_sided(self):
        # Kalshi has no NO-side quote -> Kalshi mid not computable (F2's
        # two-sided requirement).
        self.assert_suppressed(_kp(0.50, None), _pp(0.62, 0.40), "one_sided")


class TestRealArbPassesThrough(GhostFilterBase):
    """Eldridge-class real arb (two-sided, sane spreads, mids agreeing, thin
    edge) must pass — the filters kill ghosts, not opportunities.

    NOTE: the design doc's literal Eldridge prices (Kalshi YES=0.45 + Poly
    NO=0.09) carry a 41c gap, which F3 caps by construction; this fixture keeps
    the shape but uses spec-consistent prices (see DECISIONS.md 2026-07-02)."""

    def test_eldridge_class_arb_emitted(self):
        # Kalshi 0.45/0.58 (bid 0.42, mid 0.435); Poly 0.56/0.48 (bid 0.52,
        # mid 0.54). Mids 0.105 apart, spreads 0.03/0.04, total 0.9393, gap 2.07c.
        arbs = wm.match_props([_kp(0.45, 0.58)], [_pp(0.56, 0.48)])
        self.assertEqual(len(arbs), 1)
        arb = arbs[0]
        self.assertEqual(arb["direction"], "Kalshi YES + Poly NO")
        self.assertAlmostEqual(arb["total_cost"], round((0.45 + 0.48) * 1.01, 4))
        self.assertEqual(wm._ghost_stats.total_suppressed(), 0)
        self.assertEqual(self.ghost_log, [])
        # The cosmetic suspicious flag is replaced by the filters — gone.
        self.assertNotIn("suspicious", arb)


class TestGhostObservability(GhostFilterBase):
    """Counters, ghost_log.jsonl pattern records, and the hourly summary."""

    def test_ghost_log_record_carries_full_quote_context(self):
        wm.match_props([_kp(0.31, 0.71, series="KXMLBTB", ticker="K-ELLY", line=2)],
                       [_pp(None, 0.01, smt="baseball_player_total_bases", line=2)])
        self.assertEqual(len(self.ghost_log), 1)
        rec = self.ghost_log[0]
        self.assertEqual(rec["event"], "ghost_suppressed")
        self.assertEqual(rec["reason"], "pinned")
        self.assertEqual(rec["kalshi_ticker"], "K-ELLY")
        self.assertEqual(rec["kalshi_yes_ask"], 0.31)
        self.assertEqual(rec["kalshi_no_ask"], 0.71)
        self.assertIsNone(rec["poly_yes_ask"])
        self.assertEqual(rec["poly_no_ask"], 0.01)
        self.assertEqual(rec["direction"], "Kalshi YES + Poly NO")
        self.assertIn("gap_cents", rec)
        self.assertIn("timestamp", rec)

    def test_repeat_suppression_counts_but_log_is_cooldown_deduped(self):
        kp, pp = _kp(0.99, 0.01), _pp(0.75, 0.99)
        wm.match_props([kp], [pp])
        wm.match_props([kp], [pp])  # same tick storm, < 60s later
        self.assertEqual(wm._ghost_stats.totals["pinned"], 2)
        self.assertEqual(len(self.ghost_log), 1)

    def test_hourly_summary_event_emitted(self):
        wm.match_props([_kp(0.99, 0.01)], [_pp(0.75, 0.99)])  # one pinned ghost
        wm._ghost_stats._last_summary_at -= 3601
        wm.match_props([_kp(0.45, 0.58)], [_pp(0.56, 0.48)])  # clean pair, triggers emit
        summaries = [e for e in self.events if e.get("event") == "ghost_filter_summary"]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["suppressed"]["pinned"], 1)
        self.assertEqual(summaries[0]["totals"]["pinned"], 1)
        self.assertGreaterEqual(summaries[0]["window_seconds"], 3600)

    def test_status_summary_string_shows_per_reason_counts(self):
        wm.match_props([_kp(0.99, 0.01)], [_pp(0.75, 0.99)])
        s = wm._ghost_stats.status_summary()
        self.assertIn("pin 1", s)


if __name__ == "__main__":
    unittest.main()
