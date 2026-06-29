import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from matchers.soccer import SoccerMatcher

def main():
    m = SoccerMatcher()
    k = [{"ticker": "KX-1", "title": "TeamA vs TeamB", "ask": 0.0, "taker_fee": 0.01, "raw": {}}]
    p = [{"slug": "slug-1", "team_abbr": "teama", "ask": 0.5, "taker_fee": 0.01, "raw": {}}]
    
    results = m.match(k, p)
    for r in results:
        assert r["is_arb"] is False, f"Expected is_arb to be False, got {r['is_arb']}"
    
    print("Arb guard successfully skipped/flagged 0.0 ask as non-arb")

if __name__ == "__main__":
    main()
