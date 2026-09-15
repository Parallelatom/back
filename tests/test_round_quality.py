"""Ticket 04: which recorded Rounds are fit to score, and the evidence for saying so."""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta

BTC = "BTC"
GRID = 900
T0 = 1789443000


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def a_round(ingest, symbol=BTC, starting=T0, ending=T0 + GRID, strike=100.0):
    ingest.observe_round(
        RoundMeta(symbol=symbol, starting=starting, ending=ending, strike=strike,
                  pool_address="0xpool", outcome_up="0xup", outcome_down="0xdown"),
        now=starting,
    )


def series(ingest, start, end, price_at, step=5, symbol=BTC):
    ts = start
    while ts <= end:
        ingest.observe_price(symbol, price=price_at(ts), ts=ts)
        ts += step


def row(ingest, symbol=BTC, ending=T0 + GRID):
    return dict(ingest.conn.execute(
        "SELECT * FROM rounds WHERE symbol = ? AND ending = ?", (symbol, ending)
    ).fetchone())


class TestStatistics:
    def test_the_number_of_observations_in_the_round_is_recorded(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0, step=5)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["tick_count"] == 181  # inclusive of both boundaries

    def test_the_number_of_distinct_prices_is_recorded(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0 + (ts // 300) % 3)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["distinct_price_count"] == 3

    def test_the_high_and_low_of_the_round_are_recorded(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0 + (ts - T0) * 0.01)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["price_min"] == 100.0
        assert row(ingest)["price_max"] == pytest.approx(109.0)

    def test_observations_outside_the_round_do_not_count_towards_it(self, ingest):
        a_round(ingest)
        series(ingest, T0 - 600, T0 - 5, lambda ts: 1.0)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0)
        series(ingest, T0 + GRID + 5, T0 + GRID + 600, lambda ts: 999.0)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["price_min"] == 100.0
        assert row(ingest)["price_max"] == 100.0


class TestOracleStale:
    def test_a_round_whose_price_never_changed_is_marked_stale(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 98.5)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["oracle_stale"] == 1

    def test_a_round_whose_price_moved_at_all_is_not_stale(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 98.5 if ts < T0 + GRID else 98.6)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["oracle_stale"] == 0

    def test_staleness_is_about_the_price_not_about_the_feed_being_quiet(self, ingest):
        """The feed republishes an unchanged price every few seconds, so a stale Round is
        busy with messages. Counting messages would find nothing wrong with it."""
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 98.5, step=5)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["tick_count"] > 100
        assert row(ingest)["oracle_stale"] == 1


class TestPartialRounds:
    """Partial is decided by the coverage actually held, not by when the Collector started.
    A restart mid-Round replays the feed snapshot, which can leave BTC fully covered even
    though the Collector was absent for part of it."""

    def test_a_fully_covered_round_is_not_partial(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["partial"] == 0

    def test_a_round_missing_its_opening_prices_is_partial(self, ingest):
        a_round(ingest)
        series(ingest, T0 + 300, T0 + GRID, lambda ts: 100.0)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["partial"] == 1

    def test_a_round_missing_its_closing_prices_is_partial(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID - 300, lambda ts: 100.0)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["partial"] == 1

    def test_a_round_with_a_hole_in_the_middle_is_partial(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + 200, lambda ts: 100.0)
        series(ingest, T0 + 500, T0 + GRID, lambda ts: 100.0)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["partial"] == 1

    def test_the_ordinary_few_second_spacing_of_the_feed_is_not_a_hole(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0, step=15)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["partial"] == 0

    def test_a_round_with_no_observations_at_all_is_partial(self, ingest):
        a_round(ingest)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest)["partial"] == 1
        assert row(ingest)["tick_count"] == 0


class TestFinalising:
    def test_finalising_an_unknown_round_does_nothing(self, ingest):
        ingest.finalise_round(BTC, T0 + GRID)

        assert ingest.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 0

    def test_finalising_twice_gives_the_same_answer(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0 + (ts - T0) * 0.01)
        ingest.finalise_round(BTC, T0 + GRID)
        first = row(ingest)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest) == first

    def test_only_the_named_round_is_touched(self, ingest):
        a_round(ingest, ending=T0 + GRID)
        a_round(ingest, starting=T0 + GRID, ending=T0 + 2 * GRID)
        series(ingest, T0, T0 + 2 * GRID, lambda ts: 100.0)
        ingest.finalise_round(BTC, T0 + GRID)

        assert row(ingest, ending=T0 + 2 * GRID)["tick_count"] == 0


class TestFinalisingWhatHasClosed:
    def test_a_round_that_has_closed_is_finalised(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0)
        ingest.finalise_closed_rounds(now=T0 + GRID + 1)

        assert row(ingest)["distinct_price_count"] == 1

    def test_a_round_still_running_is_left_alone(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + 300, lambda ts: 100.0)
        ingest.finalise_closed_rounds(now=T0 + 400)

        assert row(ingest)["distinct_price_count"] is None

    def test_a_round_already_finalised_is_not_redone(self, ingest):
        a_round(ingest)
        series(ingest, T0, T0 + GRID, lambda ts: 100.0)
        assert ingest.finalise_closed_rounds(now=T0 + GRID + 1) == 1

        assert ingest.finalise_closed_rounds(now=T0 + GRID + 1) == 0


class TestRecordingCrossings:
    """Every crossing of the Strike is written down with its size and how long it held, so
    a different rule for Flip Follow can be tried later against the same recordings."""

    def flips(self, ingest):
        return [dict(r) for r in ingest.conn.execute(
            "SELECT * FROM delta_flips ORDER BY ts")]

    def test_a_crossing_is_recorded(self, ingest):
        a_round(ingest, strike=100.0)
        series(ingest, T0, T0 + 400, lambda ts: 99.0, step=5)
        series(ingest, T0 + 405, T0 + GRID, lambda ts: 101.0, step=5)
        ingest.finalise_round(BTC, T0 + GRID)

        (flip,) = self.flips(ingest)
        assert flip["to_side"] == "UP"
        assert flip["ts"] == T0 + 405

    def test_the_size_of_the_move_is_recorded(self, ingest):
        a_round(ingest, strike=100.0)
        series(ingest, T0, T0 + 400, lambda ts: 99.0, step=5)
        series(ingest, T0 + 405, T0 + GRID, lambda ts: 100.5, step=5)
        ingest.finalise_round(BTC, T0 + GRID)

        assert self.flips(ingest)[0]["delta_pct"] == pytest.approx(0.5)

    def test_how_long_the_new_side_held_is_recorded(self, ingest):
        a_round(ingest, strike=100.0)
        series(ingest, T0, T0 + 400, lambda ts: 99.0, step=5)
        series(ingest, T0 + 405, T0 + 500, lambda ts: 101.0, step=5)
        series(ingest, T0 + 505, T0 + GRID, lambda ts: 99.0, step=5)
        ingest.finalise_round(BTC, T0 + GRID)

        assert self.flips(ingest)[0]["held_seconds"] == 95

    def test_a_round_that_never_crosses_records_nothing(self, ingest):
        a_round(ingest, strike=100.0)
        series(ingest, T0, T0 + GRID, lambda ts: 101.0)
        ingest.finalise_round(BTC, T0 + GRID)

        assert self.flips(ingest) == []

    def test_finalising_twice_does_not_duplicate_the_crossings(self, ingest):
        a_round(ingest, strike=100.0)
        series(ingest, T0, T0 + 400, lambda ts: 99.0, step=5)
        series(ingest, T0 + 405, T0 + GRID, lambda ts: 101.0, step=5)
        ingest.finalise_round(BTC, T0 + GRID)
        ingest.finalise_round(BTC, T0 + GRID)

        assert len(self.flips(ingest)) == 1
