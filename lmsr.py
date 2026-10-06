"""Logarithmic Market Scoring Rule (LMSR) automated market maker.

Every contract has a single number `q`. Its YES price comes from a pool:
  * independent contract -> pool is [q, 0]           (its own yes/no market)
  * exclusive market      -> pool is [q1, q2, ...]   (prices sum to 100%)

Buying YES raises q, buying NO lowers q (LMSR is shift-invariant, so a NO
share is equivalent to one share of every *other* outcome). Each function
below only needs the contract's current YES price `p` and liquidity `b`.
"""
import math


def _logsumexp(xs):
    m = max(xs)
    return m + math.log(sum(math.exp(x - m) for x in xs))


def prices(qs, b):
    m = max(qs)
    es = [math.exp((q - m) / b) for q in qs]
    s = sum(es)
    return [e / s for e in es]


def side_price(p, side):
    return p if side == "YES" else 1.0 - p


def shares_for_amount(p, b, amount, side):
    """Shares received for spending `amount` dollars on `side`."""
    ps = side_price(p, side)
    x = amount / b
    if x < 30:
        return b * math.log1p(math.expm1(x) / ps)
    return b * (x - math.log(ps) + math.log1p((ps - 1) * math.exp(-x)))


def proceeds_for_shares(p, b, shares, side):
    """Dollars received for selling `shares` of `side`."""
    ps = side_price(p, side)
    return -b * math.log(1 - ps + ps * math.exp(-shares / b))


def q_for_binary(p, b):
    """q that puts an independent contract at YES price p."""
    return b * math.log(p / (1 - p))


def q_for_new_outcome(p, b, existing_qs):
    """q for a new outcome joining an exclusive pool at price p (others shrink proportionally)."""
    base = b * _logsumexp([q / b for q in existing_qs]) if existing_qs else 0.0
    return b * math.log(p / (1 - p)) + base


def max_subsidy(b, start_prices):
    """Worst-case house loss for one pool, given the opening prices of its outcomes."""
    return b * math.log(1 / min(start_prices))
