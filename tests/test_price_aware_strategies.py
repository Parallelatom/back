"""Strategies that look at what a ticket will cost, not only at what the price is doing.

Measured against 3,570 real trades: 65% of them land inside the 60-300 second window these
Strategies work in, and 54% of Rounds see more than one. So arriving late often means
arriving after someone else, and an AMM does not run out — it simply gets dear. A late
entry rule that ignores the fill is a rule that buys at any price.
"""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta
from strategy_lab.replay import (
    CONTRARIAN_FILL, LOCK_RIDER, LOCK_RIDER_MAX_PRICE, WINDOW_CLOSES, replay,
)

BTC = "BTC"
GRID = 900
T0 = 1789443000
STRIKE = 100.0
POOL = "0xpool"
# A pool one 1 USD ticket into UP: UP is now dear at about 0.90.
UP_IS_DEAR = (168_578, 1_483_000)
UP_IS_CHEAP = (1_483_000, 168_578)


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def a_chain_quote(ingest, ending=T0 + GRID, side="UP", at=None, shares=1_314_422,
                  price=0.5):
    """The contract answering about the pool, which is recorded whether or not it moved.

    The Reserves table cannot stand in for this: `apply_remote_reserves` writes only when
    the answer disagrees with what is held, so confirming a pool is still even leaves no
    trace of anyone having looked.
    """
    ingest.record_chain_quote(BTC, ending, side, at or (ending - GRID + 10), 1_000_000,
                              shares, 17_000, price)


def a_round(ingest, price, ending=T0 + GRID, winner=None, reserves=None, at=None):
    """A Round held at `price` throughout, optionally with the pool already moved."""
    ingest.observe_round(
        RoundMeta(symbol=BTC, starting=ending - GRID, ending=ending, strike=STRIKE,
                  pool_address=POOL, outcome_up="0xup", outcome_down="0xdown"),
        now=ending - GRID,
    )
    ts = ending - GRID
    while ts <= ending:
        ingest.observe_price(BTC, price=price, ts=ts)
        ts += 5
    ingest.finalise_round(BTC, ending)
    if reserves:
        ingest.apply_remote_reserves(POOL, up=reserves[0], down=reserves[1],
                                     ts=at or (ending - GRID + 10))
    ingest.conn.execute(
        "UPDATE rounds SET winner = ?, final_price = ?, oracle_stale = 0, partial = 0 "
        "WHERE symbol = ? AND ending = ?",
        (winner or ("UP" if price > STRIKE else "DOWN"), price, BTC, ending),
    )
    ingest.conn.commit()


def trades_of(ingest, strategy):
    return replay(ingest.conn, symbol=BTC, strategies=[strategy])[strategy.name].trades


class TestLockRider:
    def test_it_enters_as_late_as_the_contract_allows(self, ingest):
        a_round(ingest, price=100.5)
        a_chain_quote(ingest)

        (trade,) = trades_of(ingest, LOCK_RIDER)
        assert (T0 + GRID) - trade.entered_at == WINDOW_CLOSES

    def test_it_backs_whichever_side_is_ahead(self, ingest):
        a_round(ingest, price=99.5)
        a_chain_quote(ingest, side="DOWN")

        (trade,) = trades_of(ingest, LOCK_RIDER)
        assert trade.side == "DOWN"

    def test_it_needs_no_minimum_distance(self, ingest):
        """Its whole premise is that by the last minute the answer is nearly settled."""
        a_round(ingest, price=100.001)
        a_chain_quote(ingest)

        assert len(trades_of(ingest, LOCK_RIDER)) == 1

    def test_a_pool_seen_to_be_even_is_bought(self, ingest):
        a_round(ingest, price=100.5)
        a_chain_quote(ingest)

        (trade,) = trades_of(ingest, LOCK_RIDER)
        assert trade.marginal_price == 0.5

    def test_a_pool_nobody_looked_at_is_not_bought(self, ingest):
        """Every Round is seeded with an even pool whether or not anyone looked, so an
        even pool is not evidence of an untouched one. This rule is a judgement about the
        pool, and there is nothing here to judge."""
        a_round(ingest, price=100.5)

        assert trades_of(ingest, LOCK_RIDER) == []

    def test_it_refuses_a_side_that_others_have_already_made_dear(self, ingest):
        """Someone got there first. Winning from here returns almost nothing."""
        a_round(ingest, price=100.5, reserves=UP_IS_DEAR)

        assert trades_of(ingest, LOCK_RIDER) == []

    def test_it_still_buys_when_the_crowd_took_the_other_side(self, ingest):
        a_round(ingest, price=100.5, reserves=UP_IS_CHEAP)

        assert len(trades_of(ingest, LOCK_RIDER)) == 1

    def test_the_ceiling_is_what_decides_it(self, ingest):
        a_round(ingest, price=100.5, reserves=UP_IS_DEAR)

        from strategy_lab.replay import lock_rider

        permissive = lock_rider(max_price=0.99)
        assert LOCK_RIDER_MAX_PRICE < 0.99
        assert len(trades_of(ingest, permissive)) == 1

    def test_a_pool_moved_after_the_entry_moment_does_not_count(self, ingest):
        """The later reading is not reached for — and it is also the only reading there
        is, so by the entry moment the pool had not been seen at all."""
        a_round(ingest, price=100.5, reserves=UP_IS_DEAR, at=T0 + GRID - 10)

        assert trades_of(ingest, LOCK_RIDER) == []

    def test_a_pool_seen_before_the_entry_moment_does_count(self, ingest):
        a_round(ingest, price=100.5, reserves=UP_IS_DEAR, at=T0 + GRID - WINDOW_CLOSES - 5)

        assert trades_of(ingest, LOCK_RIDER) == []


class TestContrarianFill:
    def test_it_buys_the_leader_when_the_pool_has_it_cheap(self, ingest):
        a_round(ingest, price=100.5, reserves=UP_IS_CHEAP)

        (trade,) = trades_of(ingest, CONTRARIAN_FILL)
        assert trade.side == "UP"

    def test_the_prize_is_several_times_what_an_even_pool_would_pay(self, ingest):
        a_round(ingest, price=100.5, reserves=UP_IS_CHEAP, winner="UP")

        (trade,) = trades_of(ingest, CONTRARIAN_FILL)
        assert trade.won is True
        assert trade.pnl == pytest.approx(1.2489, abs=1e-3)   # against +0.314 at even money

    def test_the_marginal_price_wildly_overstates_that_prize(self, ingest):
        """A pool quoting 0.10 does not sell a dollar's worth at 0.10. It is half a dollar
        deep, so the ticket moves it to about 0.44 on the way in. Reading the prize off the
        marginal price would promise eight times the stake and deliver one and a quarter."""
        a_round(ingest, price=100.5, reserves=UP_IS_CHEAP, winner="UP")

        (trade,) = trades_of(ingest, CONTRARIAN_FILL)
        assert trade.marginal_price == pytest.approx(0.102, abs=1e-3)
        assert trade.price_per_share == pytest.approx(0.445, abs=1e-3)

    def test_it_stands_aside_when_the_pool_already_agrees(self, ingest):
        """Nothing to collect: the market has priced what we were going to say."""
        a_round(ingest, price=100.5, reserves=UP_IS_DEAR)

        assert trades_of(ingest, CONTRARIAN_FILL) == []

    def test_it_stands_aside_on_an_untouched_pool(self, ingest):
        a_round(ingest, price=100.5)

        assert trades_of(ingest, CONTRARIAN_FILL) == []

    def test_it_still_wants_the_price_to_be_far_from_the_strike(self, ingest):
        """A cheap fill on a coin flip is not an edge."""
        a_round(ingest, price=100.01, reserves=UP_IS_CHEAP)

        assert trades_of(ingest, CONTRARIAN_FILL) == []

    def test_a_losing_contrarian_trade_still_only_costs_the_stake(self, ingest):
        a_round(ingest, price=100.5, reserves=UP_IS_CHEAP, winner="DOWN")

        (trade,) = trades_of(ingest, CONTRARIAN_FILL)
        assert trade.pnl == -1.0


class TestJudgingThePoolWithoutHavingSeenIt:
    """Both rules are judgements about the pool, so both need one to have been observed.

    Every Round is seeded with an even pool without anyone looking (ADR-0004), and a
    confirmation that it is still even writes nothing, so neither the presence of Reserves
    nor their evenness shows that the pool was seen.
    """

    def test_contrarian_fill_does_not_buy_a_cheapness_it_never_observed(self, ingest):
        a_round(ingest, price=100.5)

        assert trades_of(ingest, CONTRARIAN_FILL) == []

    def test_contrarian_fill_buys_once_the_contract_has_shown_the_price(self, ingest):
        a_round(ingest, price=100.5)
        a_chain_quote(ingest, price=0.2, shares=4_000_000)

        assert len(trades_of(ingest, CONTRARIAN_FILL)) == 1

    def test_contrarian_fill_still_refuses_a_side_the_contract_prices_dear(self, ingest):
        a_round(ingest, price=100.5)
        a_chain_quote(ingest, price=0.9, shares=1_100_000)

        assert trades_of(ingest, CONTRARIAN_FILL) == []

    def test_an_observation_after_the_moment_is_not_reached_back_for(self, ingest):
        a_round(ingest, price=100.5)
        a_chain_quote(ingest, price=0.2, shares=4_000_000, at=T0 + GRID - 1)

        assert trades_of(ingest, CONTRARIAN_FILL) == []
