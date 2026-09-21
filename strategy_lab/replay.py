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
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .amm import SCALE, Reserves, fill, marginal_price

# A Strategy may enter from five minutes before settlement until one minute before it. The
# one-minute floor is the contract's, not a preference: it rejects buys after that point.
WINDOW_OPENS = 300
WINDOW_CLOSES = 60

# How far the price must sit from the Strike, as a percentage of it, before Delta Edge will
# act. Per Symbol because BTC and crude are not the same instrument, even if they start at
# the same number for want of evidence to choose a better one.
DELTA_THRESHOLD_PCT = {"BTC": 0.1, "XYZCL": 0.1}
DEFAULT_DELTA_THRESHOLD_PCT = 0.1

# How long a crossing of the Strike must hold before Flip Follow believes it. A price
# resting on its Strike flickers across it constantly; this is what separates a crossing
# from that flicker.
FLIP_HOLD_SECONDS = 3

# Lock Rider waits until the last legal moment, so it often arrives after someone else:
# measured over 3,570 real trades, 65% land inside this window and 54% of Rounds see more
# than one. An AMM never runs out, it only gets dear, so the guard is a price and not a
# quantity. Above this, a win returns so little that it cannot pay for the losses.
LOCK_RIDER_MAX_PRICE = 0.80

# Contrarian Fill wants the opposite situation: the pool leaning against what the price is
# plainly doing. Below this, the Side that Delta favours is being sold at a discount.
CONTRARIAN_MAX_MARGINAL = 0.35

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
    # What the pool contract said a ticket buys, per Side, oldest first. Empty for every
    # Round recorded before the Collector began asking it.
    quotes: Mapping[str, Sequence[Tuple[int, Tuple[int, int]]]] = field(default_factory=dict)

    def price_at(self, moment: int) -> Optional[float]:
        stamps = [ts for ts, _ in self.prices]
        index = bisect_right(stamps, moment) - 1
        return self.prices[index][1] if index >= 0 else None

    def reserves_at(self, moment: int) -> Reserves:
        """What the pool held then. A Round nobody had traded held its opening Reserves."""
        stamps = [ts for ts, _ in self.reserves]
        index = bisect_right(stamps, moment) - 1
        return self.reserves[index][1] if index >= 0 else Reserves.opening()

    def quote_at(self, side: str, moment: int) -> Optional[Tuple[int, int]]:
        """The contract's answer in force at that moment, as (shares, fees), or None.

        Never the next one: a Strategy that acted at noon was answered by what the pool
        held at noon, and reaching forward would score it on a price it could not get.
        """
        series = self.quotes.get(side) or ()
        stamps = [ts for ts, _ in series]
        index = bisect_right(stamps, moment) - 1
        return series[index][1] if index >= 0 else None

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
    # "chain" when the contract's own quote priced this Fill, "model" when the local
    # pricing rule did. The two are not comparable and a curve must not mix them silently.
    priced_by: str = "model"


@dataclass
class Result:
    strategy: str
    symbol: str
    trades: List[PaperTrade] = field(default_factory=list)
    curve: List[Tuple[int, float]] = field(default_factory=list)
    bankroll: float = STARTING_BANKROLL
    ruined_at: Optional[int] = None

    @property
    def chain_priced(self) -> int:
        """How many Fills the contract priced. The rest ran on the local rule, which
        cannot see a pool that has been traded and so reads it as untouched."""
        return sum(1 for trade in self.trades if trade.priced_by == "chain")

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


def _window(record: RoundRecord) -> Tuple[int, int]:
    return record.ending - WINDOW_OPENS, record.ending - WINDOW_CLOSES


def _side_of(delta: float) -> str:
    """A Round asks whether the price ends up *above* the Strike, so level is not above."""
    return "UP" if delta > 0 else "DOWN"


def delta_edge(thresholds: Optional[Dict[str, float]] = None,
               window_opens: int = WINDOW_OPENS,
               window_closes: int = WINDOW_CLOSES) -> Strategy:
    """Act the moment the price is far enough from the Strike, in the direction it leans."""
    thresholds = DELTA_THRESHOLD_PCT if thresholds is None else thresholds
    if (type(window_opens) is not int or type(window_closes) is not int
            or not 0 < window_closes < window_opens < 900):
        raise ValueError("invalid Delta Edge trade window")

    def decide(record: RoundRecord) -> Optional[Entry]:
        threshold = thresholds.get(record.symbol, DEFAULT_DELTA_THRESHOLD_PCT)
        opens, closes = record.ending - window_opens, record.ending - window_closes
        for ts, _ in record.prices:
            if ts < opens:
                continue
            if ts > closes:
                return None
            delta = record.delta_pct(ts)
            if delta is not None and abs(delta) > threshold:
                return Entry(at=ts, side=_side_of(delta))
        return None

    return Strategy(name="Delta Edge", decide=decide)


def flip_follow(hold_seconds: int = FLIP_HOLD_SECONDS) -> Strategy:
    """Act when the price crosses its Strike and stays across.

    Deliberately carries no size requirement of its own: a crossing counts however slight.
    That makes it differ from Delta Edge in two respects rather than one, which is why the
    Collector also records each crossing's size, so the other variant can be tried later
    against the same recordings.
    """

    def decide(record: RoundRecord) -> Optional[Entry]:
        opens, closes = _window(record)
        for flip in _crossings(record):
            confirmed_at = flip.at + hold_seconds
            if confirmed_at < opens:
                continue
            if confirmed_at > closes:
                return None
            if _sustained(record, flip, hold_seconds):
                return Entry(at=confirmed_at, side=flip.side)
        return None

    return Strategy(name="Flip Follow", decide=decide)


@dataclass(frozen=True)
class Crossing:
    """A moment the price moved from one side of its Strike to the other."""

    at: int
    side: str
    delta_pct: float


def _crossings(record: RoundRecord) -> List[Crossing]:
    found: List[Crossing] = []
    previous: Optional[str] = None
    for ts, _ in record.prices:
        delta = record.delta_pct(ts)
        if delta is None:
            continue
        side = _side_of(delta)
        if previous is not None and side != previous:
            found.append(Crossing(at=ts, side=side, delta_pct=delta))
        previous = side
    return found


def _sustained(record: RoundRecord, flip: Crossing, hold_seconds: int) -> bool:
    """Whether a crossing is real, meaning both sides of it held.

    Checking only the new side is not enough. A price flickering across its Strike produces
    a crossing *and* a recrossing, and the recrossing back to where it already was would
    otherwise qualify — so the flicker would trigger after all, just in the other direction.

    Expressed in elapsed time rather than in observations, because the feed publishes every
    few seconds and has been seen to stretch to fifteen. A rule that wanted an observation
    at every second would simply never fire on real data.
    """
    other = "UP" if flip.side == "DOWN" else "DOWN"
    if record.prices and record.prices[0][0] > flip.at - hold_seconds:
        return False  # the Round did not start early enough to show the old side holding
    if not _side_in_force(record, flip.at - hold_seconds) == other:
        return False
    if not _side_in_force(record, flip.at + hold_seconds) == flip.side:
        return False
    for ts, _ in record.prices:
        if ts < flip.at - hold_seconds or ts > flip.at + hold_seconds:
            continue
        expected = other if ts < flip.at else flip.side
        if _side_in_force(record, ts) != expected:
            return False
    return True


def _side_in_force(record: RoundRecord, moment: int) -> Optional[str]:
    delta = record.delta_pct(moment)
    return None if delta is None else _side_of(delta)


def lock_rider(max_price: float = LOCK_RIDER_MAX_PRICE) -> Strategy:
    """Back whichever Side is ahead at the last moment the contract still allows a buy.

    It carries no minimum distance, because its premise is that by then the answer is
    nearly settled. What it does check is the fill: arriving last means arriving after the
    crowd, and a Side others have already bought costs so much that winning barely pays.
    """

    def decide(record: RoundRecord) -> Optional[Entry]:
        moment = record.ending - WINDOW_CLOSES
        delta = record.delta_pct(moment)
        if delta is None:
            return None
        side = _side_of(delta)
        got = fill(record.reserves_at(moment), side, STAKE_MICRO)
        if not got.shares or got.price_per_share > max_price:
            return None
        return Entry(at=moment, side=side)

    return Strategy(name="Lock Rider", decide=decide)


def contrarian_fill(
    thresholds: Optional[Dict[str, float]] = None,
    max_marginal: float = CONTRARIAN_MAX_MARGINAL,
) -> Strategy:
    """Buy the Side the price favours, but only while the pool is still selling it cheap.

    The same crowding that hurts Lock Rider feeds this one. When earlier buyers took the
    other Side — or took this one before the Round turned — the favoured outcome can be had
    at a fraction of even money, and a single win covers many losses.
    """
    thresholds = DELTA_THRESHOLD_PCT if thresholds is None else thresholds

    def decide(record: RoundRecord) -> Optional[Entry]:
        threshold = thresholds.get(record.symbol, DEFAULT_DELTA_THRESHOLD_PCT)
        opens, closes = _window(record)
        for ts, _ in record.prices:
            if ts < opens:
                continue
            if ts > closes:
                return None
            delta = record.delta_pct(ts)
            if delta is None or abs(delta) <= threshold:
                continue
            side = _side_of(delta)
            reserves = record.reserves_at(ts)
            implied = marginal_price(reserves)
            if side == "DOWN":
                implied = 1 - implied
            if implied < max_marginal:
                return Entry(at=ts, side=side)
        return None

    return Strategy(name="Contrarian Fill", decide=decide)


ALWAYS_UP = Strategy(name="Always Up", decide=_enter_at_window_open("UP"))
ALWAYS_DOWN = Strategy(name="Always Down", decide=_enter_at_window_open("DOWN"))
DELTA_EDGE = delta_edge()
FLIP_FOLLOW = flip_follow()
LOCK_RIDER = lock_rider()
CONTRARIAN_FILL = contrarian_fill()

BASELINES = [ALWAYS_UP, ALWAYS_DOWN]
ALL_STRATEGIES = [
    DELTA_EDGE, LOCK_RIDER, CONTRARIAN_FILL, ALWAYS_UP, ALWAYS_DOWN, FLIP_FOLLOW,
]


def load_rounds(
    conn: sqlite3.Connection,
    symbol: str,
    include_stale: bool = False,
    include_partial: bool = False,
) -> List[RoundRecord]:
    """Every Round fit to be scored, oldest first.

    A Round we only saw part of, or whose oracle never moved, is left out by default and
    can be asked for: both are judgements about quality, and a judgement should be
    reversible. A Round that never resolved is left out regardless — no setting can conjure
    a result we never learned.
    """
    where = (
        "WHERE r.symbol = ? AND r.winner IS NOT NULL AND r.strike IS NOT NULL "
        "AND COALESCE(r.unsettled, 0) = 0 "
        + ("" if include_partial else "AND COALESCE(r.partial, 0) = 0 ")
        + ("" if include_stale else "AND COALESCE(r.oracle_stale, 0) = 0 ")
    )
    rows = conn.execute(
        "SELECT r.symbol, r.starting, r.ending, r.strike, r.winner FROM rounds r "
        + where + "ORDER BY r.ending",
        (symbol,),
    ).fetchall()
    if not rows:
        return []

    # Fetch each series in one indexed join, rather than two extra queries per Round.
    prices_by_round = defaultdict(list)
    for observation in conn.execute(
        "SELECT r.ending, p.ts, p.price FROM rounds r JOIN oracle_prices p "
        "ON p.symbol = r.symbol AND p.ts BETWEEN COALESCE(NULLIF(r.starting, 0), r.ending - 900) "
        "AND r.ending " + where + "ORDER BY r.ending, p.ts",
        (symbol,),
    ):
        prices_by_round[observation["ending"]].append((observation["ts"], observation["price"]))

    reserves_by_round = defaultdict(list)
    for observation in conn.execute(
        "SELECT r.ending, v.ts, v.q_up, v.q_down FROM rounds r JOIN reserves v "
        "ON v.symbol = r.symbol AND v.round_ending = r.ending "
        + where + "ORDER BY r.ending, v.ts, v.rowid",
        (symbol,),
    ):
        reserves_by_round[observation["ending"]].append(
            (observation["ts"], Reserves(up=observation["q_up"], down=observation["q_down"]))
        )

    quotes_by_round = defaultdict(lambda: defaultdict(list))
    for observation in conn.execute(
        "SELECT r.ending, q.side, q.ts, q.shares, q.fees FROM rounds r JOIN chain_quotes q "
        "ON q.symbol = r.symbol AND q.round_ending = r.ending "
        + where + "ORDER BY r.ending, q.ts",
        (symbol,),
    ):
        quotes_by_round[observation["ending"]][observation["side"]].append(
            (observation["ts"], (observation["shares"], observation["fees"]))
        )

    records = []
    for row in rows:
        starting = row["starting"] or (row["ending"] - 900)
        records.append(
            RoundRecord(
                symbol=symbol,
                starting=starting,
                ending=row["ending"],
                strike=row["strike"],
                winner=row["winner"],
                prices=prices_by_round[row["ending"]],
                reserves=reserves_by_round[row["ending"]],
                quotes=dict(quotes_by_round[row["ending"]]),
            )
        )
    return records


def replay(
    conn: sqlite3.Connection,
    symbol: str,
    strategies: Optional[Sequence[Strategy]] = None,
    include_stale: bool = False,
    include_partial: bool = False,
) -> Dict[str, Result]:
    """Score each Strategy over the recorded Rounds for one Symbol."""
    strategies = list(strategies if strategies is not None else BASELINES)
    records = load_rounds(conn, symbol, include_stale=include_stale,
                          include_partial=include_partial)
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
    """Price a Fill from the contract's own quote where one was recorded.

    The local rule stays as the fallback and not as the preference. It is run on Reserves
    the exchange's indexer reports, and that indexer says nothing at all about a Round
    already traded — so its silence reads as an untouched pool and prices the Fill at the
    best price that exists. The contract answers for the pool as it stands.
    """
    reserves = record.reserves_at(entry.at)
    quoted = record.quote_at(entry.side, entry.at)
    won = entry.side == record.winner
    if quoted is not None:
        shares, _fees = quoted
        return PaperTrade(
            round_ending=record.ending,
            entered_at=entry.at,
            side=entry.side,
            shares=shares,
            marginal_price=marginal_price(reserves),
            price_per_share=STAKE_MICRO / shares if shares else 0.0,
            won=won,
            pnl=(shares - STAKE_MICRO) / SCALE if won else -STAKE,
            priced_by="chain",
        )
    got = fill(reserves, entry.side, STAKE_MICRO)
    return PaperTrade(
        round_ending=record.ending,
        entered_at=entry.at,
        side=entry.side,
        shares=got.shares,
        marginal_price=marginal_price(reserves),
        price_per_share=got.price_per_share,
        won=won,
        pnl=got.profit_on_win if won else -STAKE,
        priced_by="model",
    )
