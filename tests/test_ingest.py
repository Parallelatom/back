"""Seam 2: a sequence of raw feed messages and API responses in, database state out."""
import sqlite3

import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta

BTC = "BTC"
OIL = "XYZCL"


def meta(symbol=BTC, ending=1789443900, starting=1789443000, strike=77815.5):
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


def ticks(ingest):
    return [dict(r) for r in ingest.conn.execute("SELECT * FROM ticks ORDER BY ts")]


class TestRoundDiscovery:
    def test_a_discovered_round_is_recorded_with_its_strike_and_settlement_time(self, ingest):
        ingest.observe_round(meta(), now=1789443010)

        (row,) = rounds(ingest)
        assert row["symbol"] == BTC
        assert row["ending"] == 1789443900
        assert row["starting"] == 1789443000
        assert row["strike"] == 77815.5
        assert row["pool_address"] == "0xce6b261b2770b90e3b007531e3f9cdcdcca607ee"
        assert row["outcome_up"] == "0x5e9d5db5a3401bbb"
        assert row["outcome_down"] == "0x98d957e72f8000bb"

    def test_seeing_the_same_round_repeatedly_does_not_duplicate_it(self, ingest):
        for now in (1789443010, 1789443020, 1789443030):
            ingest.observe_round(meta(), now=now)

        assert len(rounds(ingest)) == 1
        assert rounds(ingest)[0]["last_seen_at"] == 1789443030

    def test_the_next_round_is_picked_up_as_a_separate_row(self, ingest):
        ingest.observe_round(meta(ending=1789443900, starting=1789443000), now=1789443010)
        ingest.observe_round(meta(ending=1789444800, starting=1789443900), now=1789443910)

        assert [r["ending"] for r in rounds(ingest)] == [1789443900, 1789444800]

    def test_both_symbols_are_tracked_independently(self, ingest):
        ingest.observe_round(meta(symbol=BTC), now=1789443010)
        ingest.observe_round(meta(symbol=OIL, strike=98.286), now=1789443010)

        assert {r["symbol"] for r in rounds(ingest)} == {BTC, OIL}


class TestPriceTicks:
    def test_a_price_is_attached_to_the_open_round_for_that_symbol(self, ingest):
        ingest.observe_round(meta(), now=1789443010)
        ingest.observe_price(BTC, price=77820.0, ts=1789443015)

        (tick,) = ticks(ingest)
        assert tick["symbol"] == BTC
        assert tick["price"] == 77820.0
        assert tick["ts"] == 1789443015
        assert tick["round_ending"] == 1789443900

    def test_a_price_arriving_before_any_round_is_known_is_not_recorded(self, ingest):
        ingest.observe_price(BTC, price=77820.0, ts=1789443015)

        assert ticks(ingest) == []

    def test_a_price_for_one_symbol_is_not_attached_to_the_other_symbols_round(self, ingest):
        ingest.observe_round(meta(symbol=BTC, ending=1789443900), now=1789443010)
        ingest.observe_price(OIL, price=98.3, ts=1789443015)

        assert ticks(ingest) == []

    def test_prices_after_a_round_turns_over_attach_to_the_new_round(self, ingest):
        ingest.observe_round(meta(ending=1789443900, starting=1789443000), now=1789443010)
        ingest.observe_price(BTC, price=77820.0, ts=1789443015)
        ingest.observe_round(meta(ending=1789444800, starting=1789443900), now=1789443910)
        ingest.observe_price(BTC, price=77830.0, ts=1789443915)

        assert [t["round_ending"] for t in ticks(ingest)] == [1789443900, 1789444800]

    def test_repeated_identical_prices_are_all_recorded(self, ingest):
        """The feed repeats an unchanged price every few seconds. That repetition is the
        evidence that the oracle was alive, so it is raw material, not noise."""
        ingest.observe_round(meta(), now=1789443010)
        for ts in (1789443015, 1789443020, 1789443025):
            ingest.observe_price(BTC, price=77820.0, ts=ts)

        assert len(ticks(ingest)) == 3


class TestProvenance:
    def test_every_round_row_carries_the_code_version_that_wrote_it(self, ingest):
        ingest.observe_round(meta(), now=1789443010)

        assert rounds(ingest)[0]["code_version"] == "testver"

    def test_every_tick_row_carries_the_code_version_that_wrote_it(self, ingest):
        ingest.observe_round(meta(), now=1789443010)
        ingest.observe_price(BTC, price=77820.0, ts=1789443015)

        assert ticks(ingest)[0]["code_version"] == "testver"


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
        ingest = Ingest(first, code_version="testver")
        ingest.observe_round(meta(), now=1789443010)
        ingest.observe_price(BTC, price=77820.0, ts=1789443015)
        first.close()

        second = connect(path)
        assert second.execute("SELECT COUNT(*) FROM ticks").fetchone()[0] == 1


class TestRawFeedMessages:
    """The feed's own wire format, verbatim, is what the seam accepts."""

    SNAPSHOT = {
        "table": "",
        "snapshot_toplevel": [
            {
                "table": "oracles_ninelives_prices_2",
                "snapshot": [
                    {
                        "amount": 77891.5,
                        "base": "BTC",
                        "created_by": "2026-09-15T02:22:05.337257Z",
                        "id": 1562208682,
                    },
                    {
                        "amount": 77890.0,
                        "base": "BTC",
                        "created_by": "2026-09-15T02:22:10.337257Z",
                        "id": 1562208683,
                    },
                ],
            }
        ],
    }

    DELTA = {
        "table": "oracles_ninelives_prices_2",
        "content": {
            "amount": 77791.5,
            "base": "BTC",
            "created_by": "2026-09-15T03:41:38.324639Z",
            "id": 1563485486,
        },
    }

    def test_a_price_delta_message_becomes_a_tick(self, ingest):
        ingest.observe_round(meta(ending=1789443900, starting=1789443000), now=1789443010)
        ingest.observe_feed_message(self.DELTA)

        (tick,) = ticks(ingest)
        assert tick["price"] == 77791.5
        assert tick["symbol"] == BTC

    def test_the_tick_timestamp_comes_from_the_feed_not_from_the_local_clock(self, ingest):
        ingest.observe_round(meta(ending=4102444800, starting=0), now=0)
        ingest.observe_feed_message(self.DELTA)

        # 2026-09-15T03:41:38Z
        assert ticks(ingest)[0]["ts"] == 1789443698

    def test_a_snapshot_message_yields_a_tick_per_entry(self, ingest):
        ingest.observe_round(meta(ending=4102444800, starting=0), now=0)
        ingest.observe_feed_message(self.SNAPSHOT)

        assert [t["price"] for t in ticks(ingest)] == [77891.5, 77890.0]

    def test_a_message_for_an_unknown_table_is_ignored(self, ingest):
        ingest.observe_round(meta(), now=1789443010)
        ingest.observe_feed_message({"table": "ninelives_comments_1", "content": {"foo": 1}})

        assert ticks(ingest) == []

    def test_a_malformed_message_is_ignored_rather_than_crashing_the_collector(self, ingest):
        ingest.observe_round(meta(), now=1789443010)

        for junk in ({}, {"table": "oracles_ninelives_prices_2"},
                     {"table": "oracles_ninelives_prices_2", "content": {"base": "BTC"}},
                     {"table": "oracles_ninelives_prices_2",
                      "content": {"base": "BTC", "amount": None, "created_by": "nonsense"}}):
            ingest.observe_feed_message(junk)

        assert ticks(ingest) == []


class TestTickAttribution:
    """The opening snapshot carries hours of prices predating the open Round. Filing those
    under the current Round would invent a price history that never happened in it."""

    def test_a_price_from_before_the_round_started_is_not_attached_to_it(self, ingest):
        ingest.observe_round(meta(starting=1789443000, ending=1789443900), now=1789443010)
        ingest.observe_price(BTC, price=78410.5, ts=1789427505)

        assert ticks(ingest) == []

    def test_a_price_from_after_the_round_settled_is_not_attached_to_it(self, ingest):
        ingest.observe_round(meta(starting=1789443000, ending=1789443900), now=1789443010)
        ingest.observe_price(BTC, price=78410.5, ts=1789443900)

        assert ticks(ingest) == []

    def test_a_price_at_the_first_second_of_the_round_is_attached(self, ingest):
        ingest.observe_round(meta(starting=1789443000, ending=1789443900), now=1789443010)
        ingest.observe_price(BTC, price=77820.0, ts=1789443000)

        assert len(ticks(ingest)) == 1

    def test_a_snapshot_of_mixed_history_keeps_only_what_falls_inside_the_round(self, ingest):
        ingest.observe_round(meta(starting=1789443000, ending=1789443900), now=1789443010)
        ingest.observe_feed_message({
            "table": "",
            "snapshot_toplevel": [{
                "table": "oracles_ninelives_prices_2",
                "snapshot": [
                    # the Round runs 03:30:00Z to 03:45:00Z
                    {"base": "BTC", "amount": 1.0, "created_by": "2026-09-14T20:31:45Z"},
                    {"base": "BTC", "amount": 2.0, "created_by": "2026-09-15T03:29:55Z"},
                    {"base": "BTC", "amount": 3.0, "created_by": "2026-09-15T03:35:00Z"},
                    {"base": "BTC", "amount": 4.0, "created_by": "2026-09-15T03:45:00Z"},
                ],
            }],
        })

        assert [t["price"] for t in ticks(ingest)] == [3.0]
        assert [t["ts"] for t in ticks(ingest)] == [1789443300]
