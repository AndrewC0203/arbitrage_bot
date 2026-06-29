import unittest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from matchers.soccer import SoccerMatcher

def make_kalshi(ticker="KX-FAKE", title="TeamXYZ vs OtherTeam", ask=0.45, fee=0.01):
    return {"ticker": ticker, "title": title, "ask": ask, "taker_fee": fee, "raw": {}}

def make_poly(slug="fake-slug", team_abbr="fake", ask=0.44, fee=0.01):
    return {"slug": slug, "team_abbr": team_abbr, "ask": ask, "taker_fee": fee, "raw": {}}

class TestPollution(unittest.TestCase):
    def test_negative_space(self):
        m = SoccerMatcher()
        km = [make_kalshi(title="TeamXYZ vs OtherTeam")]
        pm = [make_poly(team_abbr="teamxyz"), make_poly(team_abbr="xyz"), make_poly(team_abbr="team")]
        
        results = m.match(km, pm)
        self.assertEqual(len(results), 0, "Fake team should not match any PM sides")

if __name__ == "__main__":
    unittest.main()
