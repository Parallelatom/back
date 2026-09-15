"""Scoring Strategies against what was recorded.

This is where every Strategy rule lives, and it is a pure function over the database: no
network, no clock, no Collector. That separation is what lets a Strategy invented next month
be answered in seconds against every Round already collected (ADR-0002).

Hit Rate is the figure to read. The Bankroll curve is kept because it is what a person
actually wants to look at, but the markets are too thin for it to survive contact with a
real order, and it should not be trusted as a forecast of money (ADR-0003).
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .amm import SCALE, Reserves, fill, marginal_price

# A Strategy may enter from five minutes before settlement until one minute before it. The
# one-minute floor is the contract's, not a preference: it rejects buys after that point.
WINDOW_OPENS = 300
WINDOW_CLOSES = 60

STAKE = 1.0
STAKE_MICRO = int(STAKE * SCALE)
STARTING_BANKROLL = 10.0


@dataclass(frozen=True)
class RoundRecord:
    """One Round as it was recorded, with everything a Strategy may look at."""

    symbol: str
    starting: int
    ending: int
    strike: float
    winner: str
    prices: Sequence[Tuple[int, float]]
    reserves: Sequence[Tuple[int, Reserves]]

    def price_at(self, moment: int) -> Optional[float]:
        stamps = [ts for ts, _ in self.prices]
        index = bisect_right(stamps, moment) - 1
        return self.prices[index][1] if index >= 0 else None

    def reserves_at(self, moment: int) -> Reserves:
        """What the pool held then. A Round nobody had traded held its opening Reserves."""
        stamps = [ts for ts, _ in self.reserves]
        index = bisect_right(stamps, moment) - 1
        return self.reserves[index][1] if index >= 0 else Reserves.opening()

    def delta_pct(self, moment: int) -> Optional[float]:
        price = self.price_at(moment)
        if price is None or not self.strike:
            return None
        return (price - self.strike) / self.strike * 100.0


@dataclass(frozen=True)
class Entry:
    """A Strategy's decision to take a position: when, and on which Side."""

    at: int
    side: str


@dataclass(frozen=True)
class Strategy:
    name: str
    decide: Callable[[RoundRecord], Optional[Entry]]


@dataclass(frozen=True)
class PaperTrade:
    round_ending: int
    entered_at: int
    side: str
    shares: int
    marginal_price: float
    price_per_share: float
    won: bool
    pnl: float


@dataclass
class Result:
    strategy: str
    symbol: str
    trades: List[PaperTrade] = field(default_factory=list)
    curve: List[Tuple[int, float]] = field(default_factory=list)
    bankroll: float = STARTING_BANKROLL
    ruined_at: Optional[int] = None

    @property
    def hit_rate(self) -> Optional[float]:
        """The share of Paper Trades that picked the winning Side, or None if none were
        taken. Never a silent zero: no trades and no wins are different facts."""
        if not self.trades:
            return None
        return sum(1 for trade in self.trades if trade.won) / len(self.trades)


def _enter_at_window_open(side: str) -> Callable[[RoundRecord], Optional[Entry]]:
    def decide(record: RoundRecord) -> Optional[Entry]:
        return Entry(at=record.ending - WINDOW_OPENS, side=side)

    return decide


ALWAYS_UP = Strategy(name="Always Up", decide=_enter_at_window_open("UP"))
ALWAYS_DOWN = Strategy(name="Always Down", decide=_enter_at_window_open("DOWN"))
BASELINES = [ALWAYS_UP, ALWAYS_DOWN]


def load_rounds(conn: sqlite3.Connection, symbol: str) -> List[RoundRecord]:
    """Every Round fit to be scored, oldest first.

    A Round is skipped when we cannot say what happened in it: one we only saw part of, one
    whose oracle never moved, or one that never resolved. Each of those would otherwise
    contribute a result that looks like evidence and is not.
    """
    rows = conn.execute(
        """
        SELECT symbol, starting, ending, strike, winner FROM rounds
         WHERE symbol = ?
           AND winner IS NOT NULL
           AND strike IS NOT NULL
           AND COALESCE(partial, 0) = 0
           AND COALESCE(oracle_stale, 0) = 0
           AND COALESCE(unsettled, 0) = 0
         ORDER BY ending
        """,
        (symbol,),
    ).fetchall()

    records = []
    for row in rows:
        starting = row["starting"] or (row["ending"] - 900)
        prices = [
            (observation["ts"], observation["price"])
            for observation in conn.execute(
                "SELECT ts, price FROM oracle_prices WHERE symbol = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (symbol, starting, row["ending"]),
            )
        ]
        reserves = [
            (observation["ts"], Reserves(up=observation["q_up"], down=observation["q_down"]))
            for observation in conn.execute(
                """
                SELECT ts, q_up, q_down FROM reserves
                 WHERE symbol = ? AND round_ending = ? ORDER BY ts, rowid
                """,
                (symbol, row["ending"]),
            )
        ]
        records.append(
            RoundRecord(
                symbol=symbol,
                starting=starting,
                ending=row["ending"],
                strike=row["strike"],
                winner=row["winner"],
                prices=prices,
                reserves=reserves,
            )
        )
    return records


def replay(
    conn: sqlite3.Connection,
    symbol: str,
    strategies: Optional[Sequence[Strategy]] = None,
) -> Dict[str, Result]:
    """Score each Strategy over the recorded Rounds for one Symbol."""
    strategies = list(strategies if strategies is not None else BASELINES)
    records = load_rounds(conn, symbol)
    results = {s.name: Result(strategy=s.name, symbol=symbol) for s in strategies}

    for record in records:
        for strategy in strategies:
            result = results[strategy.name]
            if result.ruined_at is not None:
                continue
            entry = strategy.decide(record)
            if entry is None or not _within_window(record, entry.at):
                continue
            result.trades.append(_settle(record, entry))
            result.bankroll = max(0.0, result.bankroll + result.trades[-1].pnl)
            result.curve.append((record.ending, result.bankroll))
            if result.bankroll <= 0:
                result.ruined_at = record.ending
    return results


def _within_window(record: RoundRecord, moment: int) -> bool:
    remaining = record.ending - moment
    return WINDOW_CLOSES <= remaining <= WINDOW_OPENS


def _settle(record: RoundRecord, entry: Entry) -> PaperTrade:
    reserves = record.reserves_at(entry.at)
    got = fill(reserves, entry.side, STAKE_MICRO)
    won = entry.side == record.winner
    return PaperTrade(
        round_ending=record.ending,
        entered_at=entry.at,
        side=entry.side,
        shares=got.shares,
        marginal_price=marginal_price(reserves),
        price_per_share=got.price_per_share,
        won=won,
        pnl=got.profit_on_win if won else -STAKE,
    )
