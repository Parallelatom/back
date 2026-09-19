"""Settled XO Pulse cycles in, recorded Rounds out.

The venue is the one that decides what won, so the mapping from its outcome ids is read
from the payload rather than assumed. The rest of these pin the thinness of the data:
two prices per cycle and no invented path between them.
"""
import os

import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.replay import ALL_STRATEGIES, load_rounds, replay
from strategy_lab.xo_backfill import backfill, fetch_page, store, usable, winner_of

SYMBOL = "XO-BTC5"
OUTCOMES = [{"id": 0, "title": "UP"}, {"id": 1, "title": "DOWN"}]


def cycle(start="2026-09-19T14:35:00.000Z", end="2026-09-19T14:40:00.000Z",
          opening="100.0", closing="101.0", outcome=0, status="closed", outcomes=OUTCOMES):
    return {"status": status, "startsAt": start, "expiresAt": end, "openingPrice": opening,
            "closingPrice": closing, "winningOutcome": outcome, "resolvedAt": end,
            "marketMetadata": {"outcomes": outcomes}}


@pytest.fixture
def conn():
    conn = connect(":memory:")
    initialise(conn)
    return conn


class TestReadingTheVenue:
    def test_the_winner_comes_from_the_payloads_own_outcome_map(self):
        flipped = [{"id": 0, "title": "DOWN"}, {"id": 1, "title": "UP"}]
        assert winner_of(cycle(outcome=0)) == "UP"
        assert winner_of(cycle(outcome=0, outcomes=flipped)) == "DOWN"

    def test_an_unresolved_cycle_has_no_winner(self):
        assert winner_of(cycle(outcome=None)) is None

    def test_an_outcome_the_map_does_not_name_is_not_guessed(self):
        assert winner_of(cycle(outcome=7)) is None

    @pytest.mark.parametrize("bad", [
        cycle(status="open"), cycle(outcome=None), cycle(opening=None),
        cycle(closing=None), cycle(start=None),
    ])
    def test_an_incomplete_cycle_is_not_recorded(self, bad):
        assert not usable(bad)


class TestStoring:
    def test_a_cycle_becomes_one_round_and_two_prices(self, conn):
        assert store(conn, SYMBOL, [cycle()], "testver") == 1
        row = conn.execute("SELECT * FROM rounds WHERE symbol = ?", (SYMBOL,)).fetchone()
        assert row["strike"] == 100.0 and row["final_price"] == 101.0
        assert row["winner"] == "UP" and row["source"] == "backfill"
        assert conn.execute("SELECT COUNT(*) FROM oracle_prices").fetchone()[0] == 2

    def test_running_it_twice_records_nothing_new(self, conn):
        store(conn, SYMBOL, [cycle()], "testver")
        assert store(conn, SYMBOL, [cycle()], "testver") == 0
        assert conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 1

    def test_consecutive_cycles_share_the_price_on_their_boundary(self, conn):
        first = cycle()
        second = cycle(start="2026-09-19T14:40:00.000Z", end="2026-09-19T14:45:00.000Z",
                       opening="101.0", closing="99.0", outcome=1)
        assert store(conn, SYMBOL, [first, second], "testver") == 2
        # Two Rounds, three moments: the close of one is the open of the next.
        assert conn.execute("SELECT COUNT(*) FROM oracle_prices").fetchone()[0] == 3

    def test_a_cycle_that_never_moved_is_marked_stale_rather_than_scored(self, conn):
        store(conn, SYMBOL, [cycle(opening="100.0", closing="100.0")], "testver")
        assert conn.execute("SELECT oracle_stale FROM rounds").fetchone()[0] == 1
        assert load_rounds(conn, SYMBOL) == []

    def test_a_cycle_ending_before_it_starts_is_refused(self, conn):
        assert store(conn, SYMBOL, [cycle(start="2026-09-19T14:40:00.000Z",
                                          end="2026-09-19T14:35:00.000Z")], "testver") == 0


class TestScoring:
    def test_a_recorded_cycle_is_scoreable(self, conn):
        store(conn, SYMBOL, [cycle()], "testver")
        assert len(load_rounds(conn, SYMBOL)) == 1

    def test_a_strategy_that_needs_a_price_path_takes_no_trade(self, conn):
        store(conn, SYMBOL, [cycle()], "testver")
        results = replay(conn, symbol=SYMBOL, strategies=ALL_STRATEGIES)
        assert results["Delta Edge"].trades == []
        assert results["Always Up"].trades != []


class TestPaging:
    def test_it_reads_every_page_the_venue_reports(self, conn):
        pages = {1: [cycle()], 2: [cycle(start="2026-09-19T14:40:00.000Z",
                                         end="2026-09-19T14:45:00.000Z")]}
        seen = []

        def fetch(config_id, page, base):
            seen.append(page)
            return {"data": pages[page], "meta": {"totalPages": 2}}

        assert backfill(conn, SYMBOL, fetch=fetch, log=lambda *a: None) == 2
        assert seen == [1, 2]

    def test_it_can_be_asked_for_fewer_pages_than_exist(self, conn):
        def fetch(config_id, page, base):
            return {"data": [cycle()], "meta": {"totalPages": 9}}

        assert backfill(conn, SYMBOL, pages=1, fetch=fetch, log=lambda *a: None) == 1


@pytest.mark.skipif(not os.environ.get("STRATEGY_LAB_LIVE"),
                    reason="set STRATEGY_LAB_LIVE=1 to run checks against the live feed")
class TestTheVenueItself:
    def test_the_live_endpoint_still_answers_in_the_shape_we_parse(self):
        page = fetch_page(2, 1)
        assert page["meta"]["totalPages"] >= 1
        assert any(usable(row) for row in page["data"])
