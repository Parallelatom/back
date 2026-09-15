"""Seam 2: a sequence of raw feed messages and API responses in, database state out."""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta

BTC = "BTC"
OIL = "XYZCL"

# A Round on the 900-second grid: 03:30:00Z to 03:45:00Z.
STARTING = 1789443000
ENDING = 1789443900


def meta(symbol=BTC, ending=ENDING, starting=STARTING, strike=77815.5):
    return RoundMeta(
        symbol=symbol,
        starting=starting,
        ending=ending,
        strike=strike,
        pool_address="0xce6b261b2770b90e3b007531e3f9cdcdcca607ee",
        outcome_up="0x5e9d5db5a3401bbb",
        outcome_down="0x98d957e72f8000bb",
    )


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def rounds(ingest):
    return [dict(r) for r in ingest.conn.execute("SELECT * FROM rounds ORDER BY symbol, ending")]


def prices(ingest, symbol=None):
    sql = "SELECT * FROM oracle_prices"
    args = ()
    if symbol:
        sql += " WHERE symbol = ?"
        args = (symbol,)
    return [dict(r) for r in ingest.conn.execute(sql + " ORDER BY symbol, ts", args)]


def feed(symbol, price, iso):
    return {"table": "oracles_ninelives_prices_2",
            "content": {"base": symbol, "amount": price, "created_by": iso}}


class TestRoundDiscovery:
    def test_a_discovered_round_is_recorded_with_its_strike_and_settlement_time(self, ingest):
        ingest.observe_round(meta(), now=STARTING + 10)

        (row,) = rounds(ingest)
        assert row["symbol"] == BTC
        assert row["ending"] == ENDING
        assert row["starting"] == STARTING
        assert row["strike"] == 77815.5
        assert row["pool_address"] == "0xce6b261b2770b90e3b007531e3f9cdcdcca607ee"
        assert row["outcome_up"] == "0x5e9d5db5a3401bbb"
        assert row["outcome_down"] == "0x98d957e72f8000bb"

    def test_a_round_the_collector_watched_is_marked_as_seen_live(self, ingest):
        ingest.observe_round(meta(), now=STARTING + 10)

        assert rounds(ingest)[0]["source"] == "live"

    def test_seeing_the_same_round_repeatedly_does_not_duplicate_it(self, ingest):
        for now in (STARTING + 10, STARTING + 20, STARTING + 30):
            ingest.observe_round(meta(), now=now)

        assert len(rounds(ingest)) == 1
        assert rounds(ingest)[0]["last_seen_at"] == STARTING + 30

    def test_the_next_round_is_picked_up_as_a_separate_row(self, ingest):
        ingest.observe_round(meta(), now=STARTING + 10)
        ingest.observe_round(meta(starting=ENDING, ending=ENDING + 900), now=ENDING + 10)

        assert [r["ending"] for r in rounds(ingest)] == [ENDING, ENDING + 900]

    def test_both_symbols_are_tracked_independently(self, ingest):
        ingest.observe_round(meta(symbol=BTC), now=STARTING + 10)
        ingest.observe_round(meta(symbol=OIL, strike=98.286), now=STARTING + 10)

        assert {r["symbol"] for r in rounds(ingest)} == {BTC, OIL}


class TestPriceSeries:
    """The complete oracle series is the raw material. Nothing about which Round happens to
    be open may decide whether a price is kept (ADR-0002)."""

    def test_a_price_is_recorded_even_when_no_round_is_known(self, ingest):
        ingest.observe_price(BTC, price=77820.0, ts=STARTING + 15)

        assert [p["price"] for p in prices(ingest)] == [77820.0]

    def test_a_price_from_long_before_the_open_round_is_still_recorded(self, ingest):
        ingest.observe_round(meta(), now=STARTING + 10)
        ingest.observe_price(BTC, price=78410.5, ts=STARTING - 20000)

        assert len(prices(ingest)) == 1

    def test_each_symbol_keeps_its_own_series(self, ingest):
        ingest.observe_price(BTC, price=77820.0, ts=STARTING + 15)
        ingest.observe_price(OIL, price=98.3, ts=STARTING + 15)

        assert {p["symbol"] for p in prices(ingest)} == {BTC, OIL}

    def test_the_same_observation_arriving_twice_is_stored_once(self, ingest):
        """Every reconnection replays the snapshot, so duplicates are the normal case."""
        for _ in range(3):
            ingest.observe_price(BTC, price=77820.0, ts=STARTING + 15)

        assert len(prices(ingest)) == 1

    def test_repeated_prices_at_different_times_are_all_recorded(self, ingest):
        """An unchanged price repeating is the evidence the oracle was alive."""
        for ts in (STARTING + 15, STARTING + 20, STARTING + 25):
            ingest.observe_price(BTC, price=77820.0, ts=ts)

        assert len(prices(ingest)) == 3


class TestProvenance:
    def test_every_round_row_carries_the_code_version_that_wrote_it(self, ingest):
        ingest.observe_round(meta(), now=STARTING + 10)

        assert rounds(ingest)[0]["code_version"] == "testver"

    def test_every_price_row_carries_the_code_version_that_wrote_it(self, ingest):
        ingest.observe_price(BTC, price=77820.0, ts=STARTING + 15)

        assert prices(ingest)[0]["code_version"] == "testver"


class TestDurability:
    def test_the_database_is_in_wal_mode_so_a_reader_cannot_block_the_writer(self, tmp_path):
        conn = connect(str(tmp_path / "lab.db"))
        initialise(conn)

        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_a_busy_timeout_is_set(self, tmp_path):
        conn = connect(str(tmp_path / "lab.db"))

        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0

    def test_rows_survive_the_connection_being_reopened(self, tmp_path):
        path = str(tmp_path / "lab.db")
        first = connect(path)
        initialise(first)
        Ingest(first, code_version="testver").observe_price(BTC, price=77820.0, ts=STARTING)
        first.close()

        second = connect(path)
        assert second.execute("SELECT COUNT(*) FROM oracle_prices").fetchone()[0] == 1


class TestRawFeedMessages:
    """The feed's own wire format, verbatim, is what the seam accepts."""

    SNAPSHOT = {
        "table": "",
        "snapshot_toplevel": [{
            "table": "oracles_ninelives_prices_2",
            "snapshot": [
                {"amount": 77891.5, "base": "BTC", "created_by": "2026-09-15T02:22:05.337257Z"},
                {"amount": 77890.0, "base": "BTC", "created_by": "2026-09-15T02:22:10.337257Z"},
            ],
        }],
    }

    def test_a_price_delta_message_becomes_an_observation(self, ingest):
        ingest.observe_feed_message(feed(BTC, 77791.5, "2026-09-15T03:41:38.324639Z"))

        (row,) = prices(ingest)
        assert row["price"] == 77791.5
        assert row["symbol"] == BTC

    def test_the_timestamp_comes_from_the_feed_not_from_the_local_clock(self, ingest):
        ingest.observe_feed_message(feed(BTC, 77791.5, "2026-09-15T03:41:38.324639Z"))

        assert prices(ingest)[0]["ts"] == 1789443698

    def test_a_snapshot_message_yields_an_observation_per_entry(self, ingest):
        ingest.observe_feed_message(self.SNAPSHOT)

        assert [p["price"] for p in prices(ingest)] == [77891.5, 77890.0]

    def test_a_message_for_an_unknown_table_is_ignored(self, ingest):
        ingest.observe_feed_message({"table": "ninelives_comments_1", "content": {"foo": 1}})

        assert prices(ingest) == []

    def test_a_malformed_message_is_ignored_rather_than_crashing_the_collector(self, ingest):
        for junk in ({}, {"table": "oracles_ninelives_prices_2"},
                     {"table": "oracles_ninelives_prices_2", "content": {"base": "BTC"}},
                     {"table": "oracles_ninelives_prices_2",
                      "content": {"base": "BTC", "amount": None, "created_by": "nonsense"}},
                     {"table": "oracles_ninelives_prices_2",
                      "content": {"base": "BTC", "amount": True, "created_by": "2026-09-15T03:41:38Z"}}):
            ingest.observe_feed_message(junk)

        assert prices(ingest) == []
