"""Seam 1: the two Strategies that decide rather than always act."""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta
from strategy_lab.replay import (
    ALWAYS_DOWN, ALWAYS_UP, DELTA_EDGE, FLIP_FOLLOW, WINDOW_CLOSES, WINDOW_OPENS, replay,
)

BTC = "BTC"
GRID = 900
T0 = 1789443000
STRIKE = 100.0
# 0.1% of a 100.0 Strike is 0.1, so 100.2 is comfortably through the threshold.
THROUGH_UP = 100.2
THROUGH_DOWN = 99.8
FLAT = 100.0


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


BELOW = 99.9
ABOVE = 100.1


def a_round(ingest, prices, ending=T0 + GRID, winner="UP", symbol=BTC, strike=STRIKE,
            baseline=None):
    """`prices` maps seconds-remaining to a price; everything else sits at `baseline`.

    The baseline matters: a price resting exactly on its Strike counts as DOWN, since the
    Round asks whether it ends up *above*. Leaving it there would manufacture a crossing
    the moment any explicit price appeared.
    """
    if baseline is None:
        baseline = strike
    ingest.observe_round(
        RoundMeta(symbol=symbol, starting=ending - GRID, ending=ending, strike=strike,
                  pool_address=f"0xpool{ending}{symbol}", outcome_up="0xup", outcome_down="0xdown"),
        now=ending - GRID,
    )
    ts = ending - GRID
    while ts <= ending:
        remaining = ending - ts
        ingest.observe_price(symbol, price=prices.get(remaining, baseline), ts=ts)
        ts += 1
    ingest.finalise_round(symbol, ending)
    ingest.conn.execute(
        "UPDATE rounds SET winner = ?, final_price = ?, oracle_stale = 0 WHERE symbol = ? AND ending = ?",
        (winner, strike + 1, symbol, ending),
    )
    ingest.conn.commit()


def held(prices_by_remaining, start_remaining, value, seconds):
    """Hold `value` for `seconds` counting down from `start_remaining`."""
    for offset in range(seconds + 1):
        prices_by_remaining[start_remaining - offset] = value
    return prices_by_remaining


def trades_of(ingest, strategy, symbol=BTC):
    return replay(ingest.conn, symbol=symbol, strategies=[strategy])[strategy.name].trades


class TestDeltaEdge:
    def test_it_enters_when_the_price_is_far_enough_above_the_strike(self, ingest):
        a_round(ingest, held({}, 200, THROUGH_UP, 20))

        (trade,) = trades_of(ingest, DELTA_EDGE)
        assert trade.side == "UP"

    def test_it_takes_the_side_the_delta_favours(self, ingest):
        a_round(ingest, held({}, 200, THROUGH_DOWN, 20))

        (trade,) = trades_of(ingest, DELTA_EDGE)
        assert trade.side == "DOWN"

    def test_it_enters_at_the_first_qualifying_moment(self, ingest):
        a_round(ingest, held({}, 250, THROUGH_UP, 100))

        (trade,) = trades_of(ingest, DELTA_EDGE)
        assert trade.entered_at == (T0 + GRID) - 250

    def test_a_delta_that_never_reaches_the_threshold_is_not_traded(self, ingest):
        a_round(ingest, held({}, 200, 100.05, 50))  # 0.05%, under the 0.1% threshold

        assert trades_of(ingest, DELTA_EDGE) == []

    def test_a_qualifying_delta_outside_the_window_is_ignored(self, ingest):
        a_round(ingest, held({}, 800, THROUGH_UP, 100))  # long before the window opens

        assert trades_of(ingest, DELTA_EDGE) == []

    def test_a_qualifying_delta_inside_the_final_minute_is_too_late(self, ingest):
        a_round(ingest, held({}, WINDOW_CLOSES - 5, THROUGH_UP, 30))

        assert trades_of(ingest, DELTA_EDGE) == []

    def test_it_enters_at_most_once_per_round(self, ingest):
        prices = held({}, 250, THROUGH_UP, 30)
        prices = held(prices, 150, THROUGH_DOWN, 30)

        a_round(ingest, prices)

        assert len(trades_of(ingest, DELTA_EDGE)) == 1

    def test_the_threshold_can_differ_between_symbols(self, ingest):
        from strategy_lab.replay import delta_edge

        gentle = delta_edge({BTC: 0.01})
        a_round(ingest, held({}, 200, 100.05, 30))  # 0.05%

        assert trades_of(ingest, DELTA_EDGE) == []
        assert len(trades_of(ingest, gentle)) == 1


class TestFlipFollow:
    def test_it_enters_when_the_price_crosses_the_strike_and_stays_across(self, ingest):
        a_round(ingest, held({}, 240, ABOVE, 240), baseline=BELOW)

        (trade,) = trades_of(ingest, FLIP_FOLLOW)
        assert trade.side == "UP"

    def test_it_follows_the_crossing_rather_than_fading_it(self, ingest):
        a_round(ingest, held({}, 240, BELOW, 240), baseline=ABOVE)

        (trade,) = trades_of(ingest, FLIP_FOLLOW)
        assert trade.side == "DOWN"

    def test_it_waits_three_seconds_before_acting(self, ingest):
        a_round(ingest, held({}, 240, ABOVE, 240), baseline=BELOW)

        (trade,) = trades_of(ingest, FLIP_FOLLOW)
        assert (T0 + GRID) - trade.entered_at == 240 - 3

    def test_a_crossing_that_immediately_recrosses_does_not_count(self, ingest):
        """A price sitting on its Strike flickers across it. That is noise, and the hold is
        exactly what separates it from a real crossing."""
        a_round(ingest, {240: ABOVE, 239: ABOVE}, baseline=BELOW)

        assert trades_of(ingest, FLIP_FOLLOW) == []

    def test_a_round_that_never_crosses_is_not_traded(self, ingest):
        a_round(ingest, {}, baseline=100.5)

        assert trades_of(ingest, FLIP_FOLLOW) == []

    def test_a_crossing_before_the_window_opens_is_not_traded(self, ingest):
        a_round(ingest, held({}, 780, ABOVE, 780), baseline=BELOW)

        assert trades_of(ingest, FLIP_FOLLOW) == []

    def test_a_crossing_too_late_to_confirm_is_not_traded(self, ingest):
        a_round(ingest, held({}, WINDOW_CLOSES - 1, ABOVE, WINDOW_CLOSES - 1), baseline=BELOW)

        assert trades_of(ingest, FLIP_FOLLOW) == []

    def test_it_enters_at_most_once_per_round(self, ingest):
        prices = held({}, 270, ABOVE, 30)
        prices = held(prices, 210, ABOVE, 150)

        a_round(ingest, prices, baseline=BELOW)

        assert len(trades_of(ingest, FLIP_FOLLOW)) == 1

    def test_it_has_no_size_requirement_of_its_own(self, ingest):
        """Unlike Delta Edge, a crossing counts however slight. This is deliberate, and it
        is why the two Strategies differ in more than one respect."""
        a_round(ingest, held({}, 240, 100.001, 240), baseline=99.999)

        assert len(trades_of(ingest, FLIP_FOLLOW)) == 1


class TestAllFourTogether:
    def test_every_strategy_is_confined_to_the_same_window(self, ingest):
        prices = held({}, 260, 99.9, 20)
        prices = held(prices, 240, THROUGH_UP, 200)

        a_round(ingest, prices)

        results = replay(ingest.conn, symbol=BTC,
                         strategies=[ALWAYS_UP, ALWAYS_DOWN, DELTA_EDGE, FLIP_FOLLOW])
        for result in results.values():
            for trade in result.trades:
                remaining = (T0 + GRID) - trade.entered_at
                assert WINDOW_CLOSES <= remaining <= WINDOW_OPENS

    def test_each_strategy_keeps_its_own_bankroll(self, ingest):
        a_round(ingest, held({}, 200, THROUGH_UP, 100), winner="UP")

        results = replay(ingest.conn, symbol=BTC,
                         strategies=[ALWAYS_UP, ALWAYS_DOWN, DELTA_EDGE, FLIP_FOLLOW])
        assert results[ALWAYS_UP.name].bankroll > results[ALWAYS_DOWN.name].bankroll


class TestSparseFeedSampling:
    """The live feed publishes about every five seconds, not every second. A hold rule that
    quietly expects an observation at every second never fires on real data at all."""

    def a_sparse_round(self, ingest, ending=T0 + GRID, step=5):
        ingest.observe_round(
            RoundMeta(symbol=BTC, starting=ending - GRID, ending=ending, strike=STRIKE,
                      pool_address="0xsparse", outcome_up="0xup", outcome_down="0xdown"),
            now=ending - GRID,
        )
        ts = ending - GRID
        while ts <= ending:
            remaining = ending - ts
            ingest.observe_price(BTC, price=(ABOVE if remaining <= 240 else BELOW), ts=ts)
            ts += step
        ingest.finalise_round(BTC, ending)
        ingest.conn.execute(
            "UPDATE rounds SET winner = 'UP', final_price = ?, oracle_stale = 0 "
            "WHERE symbol = ? AND ending = ?", (STRIKE + 1, BTC, ending))
        ingest.conn.commit()

    def test_a_sustained_crossing_is_taken_even_at_five_second_spacing(self, ingest):
        self.a_sparse_round(ingest, step=5)

        assert len(trades_of(ingest, FLIP_FOLLOW)) == 1

    def test_it_still_works_at_fifteen_second_spacing(self, ingest):
        """The feed has been seen to stretch this far."""
        self.a_sparse_round(ingest, step=15)

        assert len(trades_of(ingest, FLIP_FOLLOW)) == 1
