"""Rebuilding past Rounds from the price series alone.

Rounds sit on a 900-second grid and a Round's Strike is the oracle price at its start, so
a long enough price series is sufficient to say what every past Round asked and how it
resolved. The feed replays hours of such history on every connection.
"""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta

BTC = "BTC"
GRID = 900
# 03:30:00Z, on the grid.
T0 = 1789443000


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def series(ingest, start, end, price_at, step=5):
    """Fill the oracle series between two timestamps, one observation every `step`."""
    ts = start
    while ts <= end:
        ingest.observe_price(BTC, price=price_at(ts), ts=ts)
        ts += step


def rounds(ingest):
    return [dict(r) for r in ingest.conn.execute("SELECT * FROM rounds ORDER BY ending")]


class TestRebuildingRounds:
    def test_a_round_is_rebuilt_with_its_strike_and_settled_close(self, ingest):
        # rises steadily from 100.0 at T0
        series(ingest, T0, T0 + GRID, lambda ts: 100.0 + (ts - T0) * 0.001)
        ingest.reconstruct_rounds(BTC)

        (row,) = rounds(ingest)
        assert row["starting"] == T0
        assert row["ending"] == T0 + GRID
        assert row["strike"] == 100.0
        assert row["final_price"] == pytest.approx(100.9)

    def test_a_round_that_closed_above_its_strike_was_won_by_up(self, ingest):
        series(ingest, T0, T0 + GRID, lambda ts: 100.0 if ts < T0 + GRID else 101.0)
        ingest.reconstruct_rounds(BTC)

        assert rounds(ingest)[0]["winner"] == "UP"

    def test_a_round_that_closed_below_its_strike_was_won_by_down(self, ingest):
        series(ingest, T0, T0 + GRID, lambda ts: 100.0 if ts < T0 + GRID else 99.0)
        ingest.reconstruct_rounds(BTC)

        assert rounds(ingest)[0]["winner"] == "DOWN"

    def test_a_round_that_closed_exactly_at_its_strike_was_won_by_down(self, ingest):
        """The Round asks whether the price ends up *above* the Strike. Level is not above."""
        series(ingest, T0, T0 + GRID, lambda ts: 100.0)
        ingest.reconstruct_rounds(BTC)

        assert rounds(ingest)[0]["winner"] == "DOWN"

    def test_several_consecutive_rounds_are_rebuilt_from_one_series(self, ingest):
        series(ingest, T0, T0 + 3 * GRID, lambda ts: 100.0 + (ts - T0) * 0.001)
        ingest.reconstruct_rounds(BTC)

        assert [r["ending"] for r in rounds(ingest)] == [T0 + GRID, T0 + 2 * GRID, T0 + 3 * GRID]

    def test_one_rounds_close_is_the_next_rounds_strike(self, ingest):
        series(ingest, T0, T0 + 2 * GRID, lambda ts: 100.0 + (ts - T0) * 0.001)
        ingest.reconstruct_rounds(BTC)

        first, second = rounds(ingest)
        assert first["final_price"] == second["strike"]


class TestProvenance:
    def test_a_rebuilt_round_is_marked_as_reconstructed(self, ingest):
        series(ingest, T0, T0 + GRID, lambda ts: 100.0)
        ingest.reconstruct_rounds(BTC)

        assert rounds(ingest)[0]["source"] == "reconstructed"

    def test_rebuilding_never_overwrites_a_round_the_collector_watched_live(self, ingest):
        ingest.observe_round(
            RoundMeta(symbol=BTC, starting=T0, ending=T0 + GRID, strike=12345.0,
                      pool_address="0xpool", outcome_up="0xup", outcome_down="0xdown"),
            now=T0 + 10,
        )
        series(ingest, T0, T0 + GRID, lambda ts: 100.0)
        ingest.reconstruct_rounds(BTC)

        (row,) = rounds(ingest)
        assert row["source"] == "live"
        assert row["strike"] == 12345.0
        assert row["pool_address"] == "0xpool"

    def test_rebuilding_twice_changes_nothing(self, ingest):
        series(ingest, T0, T0 + 2 * GRID, lambda ts: 100.0 + (ts - T0) * 0.001)
        ingest.reconstruct_rounds(BTC)
        before = rounds(ingest)
        ingest.reconstruct_rounds(BTC)

        assert rounds(ingest) == before


class TestRefusingToInvent:
    def test_a_round_whose_series_has_a_hole_at_its_start_is_not_rebuilt(self, ingest):
        """Without a price near the boundary the Strike would be a guess, and a guessed
        Strike decides the winner of every Paper Trade in that Round."""
        series(ingest, T0 - 600, T0 - 300, lambda ts: 100.0)
        series(ingest, T0 + 300, T0 + GRID, lambda ts: 100.0)
        ingest.reconstruct_rounds(BTC)

        assert rounds(ingest) == []

    def test_a_round_with_no_price_at_or_after_its_close_is_not_rebuilt(self, ingest):
        """The most recent Round has not settled yet; there is no close to read."""
        series(ingest, T0, T0 + GRID - 100, lambda ts: 100.0)
        ingest.reconstruct_rounds(BTC)

        assert rounds(ingest) == []

    def test_a_series_shorter_than_one_round_yields_nothing(self, ingest):
        series(ingest, T0 + 100, T0 + 400, lambda ts: 100.0)
        ingest.reconstruct_rounds(BTC)

        assert rounds(ingest) == []

    def test_only_the_requested_symbol_is_rebuilt(self, ingest):
        series(ingest, T0, T0 + GRID, lambda ts: 100.0)
        ingest.observe_price("XYZCL", price=98.0, ts=T0)
        ingest.reconstruct_rounds(BTC)

        assert {r["symbol"] for r in rounds(ingest)} == {BTC}


class TestCorrectingWithAuthoritativeStrikes:
    """A rebuilt Strike is the oracle price nearest the boundary, but the exchange samples
    at the moment the market was created on chain, a few unpredictable seconds earlier. The
    exchange's own answer is therefore better wherever it is still available."""

    def _rebuilt(self, ingest):
        series(ingest, T0, T0 + GRID, lambda ts: 100.0 if ts < T0 + GRID else 130.0)
        ingest.reconstruct_rounds(BTC)
        return rounds(ingest)[0]

    def test_an_authoritative_strike_replaces_the_rebuilt_one(self, ingest):
        self._rebuilt(ingest)
        ingest.apply_authoritative_strikes([(BTC, T0 + GRID, 101.5)])

        assert rounds(ingest)[0]["strike"] == 101.5

    def test_replacing_the_strike_recomputes_the_winner(self, ingest):
        self._rebuilt(ingest)
        assert rounds(ingest)[0]["winner"] == "UP"  # closed at 130 against a rebuilt 100

        ingest.apply_authoritative_strikes([(BTC, T0 + GRID, 140.0)])

        assert rounds(ingest)[0]["winner"] == "DOWN"

    def test_a_corrected_round_records_that_its_strike_came_from_the_exchange(self, ingest):
        self._rebuilt(ingest)
        ingest.apply_authoritative_strikes([(BTC, T0 + GRID, 101.5)])

        assert rounds(ingest)[0]["settled_source"] == "reconstructed+exchange"

    def test_a_round_the_collector_watched_live_is_left_alone(self, ingest):
        ingest.observe_round(
            RoundMeta(symbol=BTC, starting=T0, ending=T0 + GRID, strike=12345.0,
                      pool_address="0xpool", outcome_up="0xup", outcome_down="0xdown"),
            now=T0 + 10,
        )
        ingest.apply_authoritative_strikes([(BTC, T0 + GRID, 999.0)])

        assert rounds(ingest)[0]["strike"] == 12345.0

    def test_a_strike_for_a_round_we_never_rebuilt_is_ignored(self, ingest):
        ingest.apply_authoritative_strikes([(BTC, T0 + GRID, 101.5)])

        assert rounds(ingest) == []
