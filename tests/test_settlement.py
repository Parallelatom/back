"""Ticket 05: establishing which Side won, or admitting that we cannot."""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta

BTC = "BTC"
GRID = 900
T0 = 1789443000
POOL = "0xce6b261b2770b90e3b007531e3f9cdcdcca607ee"
UP = "0x5e9d5db5a3401bbb"
DOWN = "0x98d957e72f8000bb"


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def a_round(ingest, starting=T0, ending=T0 + GRID, strike=100.0, pool=POOL):
    ingest.observe_round(
        RoundMeta(symbol=BTC, starting=starting, ending=ending, strike=strike,
                  pool_address=pool, outcome_up=UP, outcome_down=DOWN),
        now=starting,
    )


def row(ingest, ending=T0 + GRID):
    found = ingest.conn.execute(
        "SELECT * FROM rounds WHERE symbol = ? AND ending = ?", (BTC, ending)
    ).fetchone()
    return dict(found) if found else None


def decided(identifier, pool=POOL, iso="2026-09-15T03:45:02.000000Z"):
    return {"table": "ninelives_events_outcome_decided",
            "content": {"emitter_addr": pool, "identifier": identifier, "created_by": iso}}


class TestSettlingFromTheEventStream:
    def test_the_winning_side_is_recorded_from_the_decision_event(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(decided(UP))

        assert row(ingest)["winner"] == "UP"

    def test_the_losing_outcome_identifier_settles_the_other_way(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(decided(DOWN))

        assert row(ingest)["winner"] == "DOWN"

    def test_the_source_of_the_settlement_is_recorded(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(decided(UP))

        assert row(ingest)["settled_source"] == "event"

    def test_an_identifier_without_its_leading_prefix_still_matches(self, ingest):
        """The feed has been seen to drop the 0x that the API includes."""
        a_round(ingest)
        ingest.observe_feed_message(decided(UP[2:]))

        assert row(ingest)["winner"] == "UP"

    def test_the_close_is_taken_from_the_price_series(self, ingest):
        a_round(ingest)
        ingest.observe_price(BTC, price=123.5, ts=T0 + GRID)
        ingest.observe_feed_message(decided(UP))

        assert row(ingest)["final_price"] == 123.5

    def test_an_event_for_a_pool_we_do_not_know_is_ignored(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(decided(UP, pool="0xsomewhereelse"))

        assert row(ingest)["winner"] is None

    def test_an_identifier_matching_neither_outcome_is_ignored(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(decided("0xdeadbeef"))

        assert row(ingest)["winner"] is None


class TestRecoveringFromTheFollowingStrike:
    """A Round's close is the next Round's Strike, so a missed decision event is not
    necessarily a lost Round."""

    def test_a_round_is_settled_from_the_strike_of_the_round_after_it(self, ingest):
        a_round(ingest, strike=100.0)
        a_round(ingest, starting=T0 + GRID, ending=T0 + 2 * GRID, strike=105.0)
        ingest.settle_from_following_rounds()

        assert row(ingest)["winner"] == "UP"
        assert row(ingest)["final_price"] == 105.0
        assert row(ingest)["settled_source"] == "following-strike"

    def test_a_close_below_the_strike_recovers_as_down(self, ingest):
        a_round(ingest, strike=100.0)
        a_round(ingest, starting=T0 + GRID, ending=T0 + 2 * GRID, strike=95.0)
        ingest.settle_from_following_rounds()

        assert row(ingest)["winner"] == "DOWN"

    def test_recovery_never_overwrites_a_settlement_from_the_event_stream(self, ingest):
        a_round(ingest, strike=100.0)
        ingest.observe_feed_message(decided(DOWN))
        a_round(ingest, starting=T0 + GRID, ending=T0 + 2 * GRID, strike=105.0)
        ingest.settle_from_following_rounds()

        assert row(ingest)["winner"] == "DOWN"
        assert row(ingest)["settled_source"] == "event"

    def test_a_round_with_no_successor_is_left_unsettled(self, ingest):
        a_round(ingest, strike=100.0)
        ingest.settle_from_following_rounds()

        assert row(ingest)["winner"] is None

    def test_the_successor_must_be_the_very_next_round_on_the_grid(self, ingest):
        """A Round two steps later closes a different Round; using it would invent a result."""
        a_round(ingest, strike=100.0)
        a_round(ingest, starting=T0 + 2 * GRID, ending=T0 + 3 * GRID, strike=105.0)
        ingest.settle_from_following_rounds()

        assert row(ingest)["winner"] is None


class TestAdmittingDefeat:
    def test_a_long_closed_round_with_no_outcome_is_marked_unsettled(self, ingest):
        a_round(ingest)
        ingest.mark_unsettled(now=T0 + GRID + 7200)

        assert row(ingest)["unsettled"] == 1

    def test_a_recently_closed_round_is_given_time_to_settle(self, ingest):
        a_round(ingest)
        ingest.mark_unsettled(now=T0 + GRID + 30)

        assert row(ingest)["unsettled"] is None

    def test_a_settled_round_is_never_marked_unsettled(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(decided(UP))
        ingest.mark_unsettled(now=T0 + GRID + 7200)

        assert row(ingest)["unsettled"] is None
