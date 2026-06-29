from abc import ABC, abstractmethod

class BaseMatcher(ABC):
    @abstractmethod
    def match(
        self,
        kalshi_markets: list[dict],
        polymarket_markets: list[dict]
    ) -> list[dict]:
        """
        Matches Kalshi markets with Polymarket markets.
        Returns a list of matched opportunities.
        """
        pass
