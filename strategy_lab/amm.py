"""How a 9lives short-term market prices an order.

These Rounds run a constant-product AMM seeded at half a dollar per Side, not the dynamic
pari-mutuel the API's `odds` field describes. Every constant here was verified against
Arbitrum mainnet: the opening reserve, the fee, and the resulting fill for a 1 USD ticket
(ADR-0004).

The pool being only half a dollar deep is the whole story. A 1 USD ticket is most of the
market, so it fills at about 0.75 against a displayed price of 0.50 — which is why a win
returns about +0.31 and why Hit Rate, not Bankroll, is the headline figure (ADR-0003).
"""
from __future__ import annotations

from dataclasses import dataclass

# Micro-units: the chain counts shares and USDC to six decimal places.
SCALE = 1_000_000

# Every Round opens seeded with half a dollar on each Side.
OPENING_RESERVE = 500_000
LIQUIDITY = 500_000

# Observed at 1.7% on live markets. It lives in per-market storage rather than being a
# constant of the protocol, which is why it is checked against the chain daily.
DEFAULT_FEE = 0.017


@dataclass(frozen=True)
class Reserves:
    """The share balances backing each Side of a Round."""

    up: int
    down: int

    @classmethod
    def opening(cls) -> "Reserves":
        return cls(OPENING_RESERVE, OPENING_RESERVE)


@dataclass(frozen=True)
class Fill:
    """What an order of a given size actually gets, as opposed to what the screen says."""

    shares: int
    fees: int
    reserves: Reserves
    gross: int = 0

    @property
    def price_per_share(self) -> float:
        """What one share cost, fee included — the number that decides whether the bet pays.

        Against a Marginal Price of 0.50 on an untouched Round this comes out near 0.76,
        which is the price impact of a 1 USD order on a pool holding one dollar in total.
        """
        return (self.spent / self.payout_on_win) if self.shares else 0.0

    @property
    def spent(self) -> float:
        return self.gross / SCALE

    @property
    def payout_on_win(self) -> float:
        """A winning share redeems for one USD."""
        return self.shares / SCALE

    @property
    def profit_on_win(self) -> float:
        return self.payout_on_win - self.spent


def marginal_price(reserves: Reserves) -> float:
    """The implied probability of UP: what the web UI shows, and what nobody actually pays.

    With two outcomes the constant-product rule collapses to the opposing side's share of
    the pool — a Side is dear precisely when little of it is left.
    """
    total = reserves.up + reserves.down
    if total <= 0:
        return 0.5
    return reserves.down / total


def fill(reserves: Reserves, side: str, gross: int, fee: float = DEFAULT_FEE) -> Fill:
    """What `gross` micro-USDC buys of `side`, at the moment these are the Reserves."""
    if gross <= 0:
        return Fill(shares=0, fees=0, reserves=reserves, gross=0)
    fees = round(gross * fee)
    shares, after = buy_with_net(reserves, side, gross - fees)
    return Fill(shares=shares, fees=fees, reserves=after, gross=gross)


def buy_with_net(reserves: Reserves, side: str, net: int):
    """Apply a buy whose fee has already been taken, returning shares out and new Reserves.

    Mirrors the contract: the net amount is added to every outcome, then the bought outcome
    is pulled back down to the constant-product invariant, and the difference is what the
    buyer receives.

    This is the form the trade feed reports — its `from_amount` is net of the fee, not the
    amount the buyer handed over.
    """
    if net <= 0:
        return 0, reserves
    bought = reserves.up if side == "UP" else reserves.down
    other = reserves.down if side == "UP" else reserves.up

    other_after = other + net
    bought_after = -(-(LIQUIDITY * LIQUIDITY) // other_after)  # integer ceiling
    shares = bought + net - bought_after

    after = (
        Reserves(up=bought_after, down=other_after)
        if side == "UP"
        else Reserves(up=other_after, down=bought_after)
    )
    return shares, after
