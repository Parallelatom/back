"""What an open position is waiting on, and that asking never writes.

The tool exists because "clear the stuck order" is the wrong instinct: an unresolved
Round is money in an undecided position, not a bad record. So the tests pin that it
distinguishes the waits, and that it cannot mutate the ledger it reads.
"""
import sqlite3

import pytest

from strategy_lab.execution.positions import CLAIM_DELAY_SECONDS, report, waiting_on

NOW = 1789828800
BUY = {"outcome_up": "0xaa", "outcome_down": "0xbb"}


def position(state="OPEN", ending=NOW - 1000, side="UP"):
    return {"id": "a" * 64, "symbol": "XYZCL", "side": side, "state": state,
            "ending": ending, "amount": 1_000_000, "pool": "0x" + "1" * 40,
            "outcome": "0x" + "a" * 16}


class TestWhatItIsWaitingOn:
    def test_a_running_round_is_not_a_fault(self):
        assert waiting_on(position(ending=NOW + 60), BUY, NOW, None) == "Round still running"

    def test_the_claim_delay_is_reported_as_a_countdown(self):
        ending = NOW - 100
        answer = waiting_on(position(ending=ending), BUY, NOW, None)
        assert f"{CLAIM_DELAY_SECONDS - 100}s left" in answer

    def test_an_unresolved_round_says_there_is_nothing_to_do(self):
        answer = waiting_on(position(), BUY, NOW, None)
        assert "has not resolved" in answer and "wait" in answer

    def test_a_won_round_points_at_the_claim(self):
        assert "claim" in waiting_on(position(side="UP"), BUY, NOW, "UP")

    def test_a_lost_round_is_not_offered_a_claim(self):
        assert "lost" in waiting_on(position(side="UP"), BUY, NOW, "DOWN").lower()

    @pytest.mark.parametrize("state", ["BUY_PENDING", "BUY_UNKNOWN"])
    def test_an_unconfirmed_purchase_is_the_one_that_needs_a_person(self, state):
        assert "attach-buy" in waiting_on(position(state=state), None, NOW, None)


class TestReading:
    @pytest.fixture
    def ledger(self, tmp_path):
        path = tmp_path / "live.db"
        conn = sqlite3.connect(path)
        conn.executescript(
            """CREATE TABLE positions (id TEXT PRIMARY KEY, symbol TEXT, ending INTEGER,
                   pool TEXT, outcome TEXT, side TEXT, state TEXT, amount INTEGER);
               CREATE TABLE live_ops (position_id TEXT, operation TEXT, outcome_up TEXT,
                   outcome_down TEXT);""")
        conn.commit()
        conn.close()
        return str(path)

    def test_an_empty_ledger_says_nothing_is_blocking(self, ledger):
        assert "Nothing is blocking" in report(ledger, rpc=object(), now=NOW)

    def test_a_settled_position_is_not_listed(self, ledger):
        conn = sqlite3.connect(ledger)
        conn.execute("INSERT INTO positions VALUES ('x','XYZCL',?,'0x1','0xa','UP',"
                     "'REDEEMED',1000000)", (NOW - 1000,))
        conn.commit()
        conn.close()
        assert "Nothing is blocking" in report(ledger, rpc=object(), now=NOW)

    def test_it_opens_the_ledger_without_permission_to_write(self, ledger):
        conn = sqlite3.connect(ledger)
        conn.execute("INSERT INTO positions VALUES ('y','XYZCL',?,'0x1','0xa','UP',"
                     "'BUY_PENDING',1000000)", (NOW - 1000,))
        conn.commit()
        conn.close()

        assert "BUY_PENDING" in report(ledger, rpc=object(), now=NOW)
        from strategy_lab.execution.positions import read_only
        with pytest.raises(sqlite3.OperationalError):
            read_only(ledger).execute("DELETE FROM positions")
