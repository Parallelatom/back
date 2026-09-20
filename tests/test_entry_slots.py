"""Which open positions hold an entry slot, and which only hold money.

The venue resolves late often enough that this is the difference between trading the
next Round and sitting out the delay. What must not move is the backstop: a Round
waiting on a result still spends cash and still counts against exposure, because that,
and not the slot, is what limits how many of them may accumulate.
"""
import pytest

from strategy_lab.execution.config import Settings
from strategy_lab.execution.store import Ledger

START = 1789443000
END = START + 900


@pytest.fixture
def ledger(tmp_path):
    ledger = Ledger(str(tmp_path / "live.db"), Settings(enabled=True))
    yield ledger
    ledger.close()


def a_position(ledger, identity, state, ending=END, amount=1_000_000, cost=0):
    ledger.conn.execute(
        "INSERT INTO positions (id, symbol, ending, pool, outcome, side, strategy, amount,"
        " minimum_shares, quoted_shares, state, cost, created_at, updated_at)"
        " VALUES (?,'BTC',?,'0xp','0xo','UP','Delta Edge',?,0,0,?,?,?,?)",
        (identity, ending, amount, state, cost, START, START))
    ledger.conn.commit()


def live(**overrides):
    base = {"mode": "live", "max_open_positions": 2}
    base.update(overrides)
    settings = Settings(enabled=True, max_open_positions=base["max_open_positions"])
    return type("S", (), {**settings.__dict__, "mode": base["mode"]})()


class TestAwaitingTheVenue:
    def test_a_finished_round_still_awaiting_a_result_frees_the_slot(self, ledger):
        a_position(ledger, "a" * 64, "OPEN", ending=END)

        assert ledger.entry_active(END + 1) == []
        assert ledger.entry_slot_block(live(), END + 1) is None

    def test_a_running_round_still_holds_its_slot(self, ledger):
        a_position(ledger, "a" * 64, "OPEN", ending=END)

        assert len(ledger.entry_active(END - 1)) == 1
        assert ledger.entry_slot_block(live(), END - 1) is not None

    def test_two_delayed_rounds_no_longer_block_a_third_entry(self, ledger):
        """The state the runner was stuck in: both slots held by undecided Rounds."""
        a_position(ledger, "a" * 64, "OPEN", ending=END)
        a_position(ledger, "b" * 64, "OPEN", ending=END + 900)

        assert ledger.entry_slot_block(live(), END + 901) is None

    def test_an_unconfirmed_purchase_still_blocks(self, ledger):
        """Not knowing whether the last buy landed is not a reason to place another."""
        a_position(ledger, "a" * 64, "BUY_PENDING", ending=END)

        assert ledger.entry_slot_block(live(), END + 1) is not None

    def test_omitting_the_clock_keeps_every_open_position_in_the_slot(self, ledger):
        a_position(ledger, "a" * 64, "OPEN", ending=END)

        assert len(ledger.entry_active()) == 1


class TestTheBackstopIsUntouched:
    def test_a_freed_round_still_reserves_its_stake(self, ledger):
        a_position(ledger, "a" * 64, "OPEN", ending=END, cost=1_000_000)
        settings = live()

        assert ledger.entry_active(END + 1) == []
        assert ledger.summary(settings)["cash_usd"] == 9

    def test_freed_rounds_still_accumulate_against_exposure(self, ledger):
        settings = live()
        for n in range(int(settings.max_exposure // 1_000_000)):
            a_position(ledger, str(n).rjust(64, "0"), "OPEN", ending=END + n * 900,
                       cost=1_000_000)

        last = END + settings.max_exposure // 1_000_000 * 900
        assert ledger.entry_slot_block(settings, last) is None
        intent = {"id": "z" * 64, "symbol": "BTC", "ending": END + 9000, "pool": "0xp",
                  "outcome": "0xo", "side": "UP", "strategy": "Delta Edge",
                  "amount": 1_000_000, "minimum_shares": 0, "quoted_shares": 0}
        assert ledger.reserve(intent, settings, last) == "exposure limit"
