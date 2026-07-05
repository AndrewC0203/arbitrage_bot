"""
Taker fee formulas for Kalshi and Polymarket US.

Both venues charge a price-dependent ("parabolic") taker fee rather than a
flat percentage of the ask: fee = theta * price * (1 - price), which peaks
at price=$0.50 and shrinks toward the $0.01/$0.99 extremes. This module
returns the fee as a fraction of $1 notional (i.e. already in the same units
as `ask`), ignoring per-contract cent-rounding since the scanner works in
continuous price fractions, not discrete contract counts.

Sources:
  Kalshi:          https://kalshi.com/fee-schedule       (theta = 0.07, taker)
  Polymarket US:   https://docs.polymarket.us/fees.md     (theta = 0.06, taker)
"""

KALSHI_TAKER_FEE_THETA = 0.07
POLYMARKET_TAKER_FEE_THETA = 0.06


def kalshi_taker_fee(price: float) -> float:
    return KALSHI_TAKER_FEE_THETA * price * (1 - price)


def polymarket_taker_fee(price: float) -> float:
    return POLYMARKET_TAKER_FEE_THETA * price * (1 - price)
