"""Forward execution rehearsal: cash, causal entries, durable receipts, and recovery."""
from dataclasses import replace
import json

import pytest

from strategy_lab.amm import Reserves
from strategy_lab.db import connect, initialise
from strategy_lab.execution.config import Settings, micro
from strategy_lab.execution.engine import Executor
from strategy_lab.execution.paper import PaperBroker, Quote, Receipt
from strategy_lab.execution.signals import Recordings, Snapshot
from strategy_lab.execution.store import Ledger
from strategy_lab.ingest import Ingest, RoundMeta
from strategy_lab.replay import RoundRecord

START = 1789443000
NOW = START + 600
END = START + 900


def snapshot(symbol="BTC", start=START):
    now = start + 600
    record = RoundRecord(symbol, start, start + 900, 100.0, "",
                         [(ts, 100.0 if ts < now else 100.2) for ts in range(start, now + 1, 5)],
                         [(now, Reserves.opening())])
    return Snapshot(record, f"pool-{symbol}-{start}", "up", "down", now, "graphql")


@pytest.fixture
def rig(tmp_path):
    settings = Settings(enabled=True)
    ledger = Ledger(str(tmp_path / "paper.db"), settings)
    broker = PaperBroker(ledger)
    yield settings, ledger, broker, Executor(settings, ledger, broker)
    ledger.close()


def test_winner_buys_then_redeems_and_cash_changes_only_on_receipts(rig):
    settings, ledger, _, engine = rig
    assert engine.enter(snapshot(), NOW) == "BUY_PENDING"
    assert ledger.summary(settings)["cash_usd"] == 10
    assert ledger.summary(settings)["reserved_usd"] == 1
    engine.advance(NOW, lambda p, ts: None)
    assert ledger.positions()[0]["state"] == "OPEN"
    assert ledger.summary(settings)["cash_usd"] == 9
    # The settlement source must not be consulted before the Round ends.
    engine.advance(NOW + 1, lambda p, ts: pytest.fail("premature settlement"))
    engine.advance(END, lambda p, ts: "UP")
    assert ledger.positions()[0]["state"] == "REDEEMED"
    assert ledger.summary(settings)["cash_usd"] == pytest.approx(10.314422)
    engine.advance(END + 1, lambda p, ts: "UP")
    assert ledger.summary(settings)["cash_usd"] == pytest.approx(10.314422)
    assert ledger.conn.execute("SELECT COUNT(*) FROM paper_receipts").fetchone()[0] == 2


def test_loser_is_closed_without_redeem_and_unresolved_round_is_not_a_loss(rig):
    settings, ledger, _, engine = rig
    engine.enter(snapshot(), NOW)
    engine.advance(END, lambda p, ts: None)
    assert ledger.positions()[0]["state"] == "OPEN"
    engine.advance(END + 1, lambda p, ts: "DOWN")
    assert ledger.positions()[0]["state"] == "LOST"
    assert ledger.summary(settings)["cash_usd"] == 9
    assert ledger.conn.execute("SELECT COUNT(*) FROM paper_receipts WHERE operation='redeem'").fetchone()[0] == 0


@pytest.mark.parametrize("operation", ["buy", "redeem"])
def test_timeout_after_acceptance_recovers_across_restart_without_resubmission(tmp_path, operation):
    path = str(tmp_path / "paper.db")
    settings = Settings(enabled=True)
    ledger = Ledger(path, settings)

    class AcknowledgementLost(PaperBroker):
        def submit_buy(self, position, snap):
            super().submit_buy(position, snap)
            if operation == "buy":
                raise TimeoutError("sensitive transport text must not be saved")

        def submit_redeem(self, position):
            super().submit_redeem(position)
            raise TimeoutError("sensitive transport text must not be saved")

        def lookup_redeem(self, position):
            return None

    engine = Executor(settings, ledger, AcknowledgementLost(ledger))
    engine.enter(snapshot(), NOW)
    if operation == "redeem":
        engine.advance(END, lambda p, ts: "UP")
    assert "sensitive" not in json.dumps(ledger.positions())
    ledger.close()

    ledger = Ledger(path, settings)
    try:
        engine = Executor(replace(settings, enabled=False), ledger, PaperBroker(ledger))
        engine.advance(END + 1, lambda p, ts: "UP")
        assert ledger.positions()[0]["state"] == "REDEEMED"
        assert ledger.summary(settings)["cash_usd"] == pytest.approx(10.314422)
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_receipts").fetchone()[0] == 2
    finally:
        ledger.close()


def test_no_acknowledgement_never_allows_a_second_buy(rig):
    settings, ledger, _, _ = rig
    submitted = []

    class Unknown(PaperBroker):
        def submit_buy(self, position, snap):
            submitted.append(position["id"])
            raise TimeoutError()

    engine = Executor(settings, ledger, Unknown(ledger))
    engine.enter(snapshot(), NOW)
    for _ in range(3):
        engine.advance(END + 1, lambda p, ts: "UP")
        engine.enter(snapshot(), NOW)
    assert len(submitted) == 1
    assert ledger.positions()[0]["state"] == "BUY_PENDING"
    assert ledger.summary(settings)["reserved_usd"] == 1
    assert engine.enter(snapshot(start=START + 900), NOW + 900) == "open-position limit"


@pytest.mark.parametrize("kind", ["future-price", "future-reserve", "stale-price", "stale-reserve",
                                  "seed", "partial", "stale-metadata", "too-late", "old-signal"])
def test_unusable_or_future_evidence_cannot_open_a_position(rig, kind):
    _, ledger, _, engine = rig
    snap = snapshot()
    now = NOW
    if kind == "future-price":
        snap = replace(snap, record=replace(snap.record, prices=[(START, 100), (NOW + 1, 101)]))
    elif kind == "future-reserve":
        snap = replace(snap, record=replace(snap.record, reserves=[(NOW + 1, Reserves.opening())]))
    elif kind == "stale-price":
        snap = replace(snap, record=replace(snap.record, prices=snap.record.prices[:-10]))
    elif kind == "stale-reserve":
        snap = replace(snap, record=replace(snap.record, reserves=[(START, Reserves.opening())]))
    elif kind == "seed":
        snap = replace(snap, reserve_source="seed")
    elif kind == "partial":
        snap = replace(snap, record=replace(snap.record, prices=snap.record.prices[70:]))
    elif kind == "stale-metadata":
        snap = replace(snap, metadata_at=START)
    elif kind == "too-late":
        now = END - 59
    else:
        now = NOW + 6
    assert engine.enter(snap, now) != "BUY_PENDING"
    assert ledger.positions() == []


def test_future_prices_cannot_change_the_side_chosen_now(rig):
    _, ledger, _, engine = rig
    snap = snapshot()
    snap = replace(snap, record=replace(snap.record, prices=[*snap.record.prices, (NOW + 1, 50)],
                                        reserves=[*snap.record.reserves, (NOW + 1, Reserves(1, 9_000_000))]))
    assert engine.enter(snap, NOW) == "BUY_PENDING"
    engine.advance(NOW, lambda p, ts: None)
    assert ledger.positions()[0]["side"] == "UP"
    assert ledger.positions()[0]["shares"] == 1_314_422


def test_price_moves_beyond_slippage_limit_and_buy_is_rejected(rig):
    settings, ledger, _, _ = rig

    class Moved(PaperBroker):
        def submit_buy(self, position, snap):
            changed = replace(snap, record=replace(snap.record, reserves=[(NOW, Reserves(168_578, 1_483_000))]))
            super().submit_buy(position, changed)

    engine = Executor(settings, ledger, Moved(ledger))
    engine.enter(snapshot(), NOW)
    engine.advance(NOW, lambda p, ts: None)
    assert ledger.positions()[0]["state"] == "BUY_REJECTED"
    assert ledger.summary(settings)["cash_usd"] == 10
    assert ledger.summary(settings)["reserved_usd"] == 0


@pytest.mark.parametrize("limit", ["daily_spend", "daily_loss", "bankroll"])
def test_limits_apply_across_rounds_and_restart(rig, limit):
    settings, ledger, broker, engine = rig
    engine.enter(snapshot(), NOW)
    engine.advance(END, lambda p, ts: "DOWN")
    if limit == "bankroll":
        # Reserve the nine remaining dollars with genuine one-dollar positions.
        settings = replace(settings, daily_spend=20_000_000, daily_loss=20_000_000)
        engine = Executor(settings, ledger, broker)
        for i in range(1, 10):
            assert engine.enter(snapshot(start=START + i * 900), NOW + i * 900) == "BUY_PENDING"
            engine.advance(END + i * 900, lambda p, ts: "DOWN")
        assert engine.enter(snapshot(start=START + 9000), NOW + 9000) == "insufficient paper cash"
    else:
        settings = replace(settings, **{limit: 1_000_000})
        engine = Executor(settings, ledger, broker)
        assert engine.enter(snapshot(start=START + 900), NOW + 900) == limit.replace("_", " ") + " limit"


def test_wallets_cannot_share_a_ledger_and_symbols_cannot_cross_wallets(rig, tmp_path):
    settings, ledger, _, engine = rig
    assert engine.enter(snapshot("XYZCL"), NOW) == "Symbol is not assigned to this wallet"
    oil = replace(settings, wallet_label="xyzcl-paper", symbols=("XYZCL",))
    with pytest.raises(ValueError, match="ledger"):
        Ledger(str(tmp_path / "paper.db"), oil)
    other = Ledger(str(tmp_path / "oil.db"), oil)
    try:
        assert Executor(oil, other, PaperBroker(other)).enter(snapshot("XYZCL"), NOW) == "BUY_PENDING"
        assert ledger.positions() == []
    finally:
        other.close()


def test_disabled_and_halt_only_stop_new_entries(rig):
    settings, ledger, broker, engine = rig
    assert engine.enter(snapshot(), NOW, halted=True) == "new entries disabled"
    engine.enter(snapshot(), NOW)
    engine = Executor(replace(settings, enabled=False), ledger, broker)
    assert engine.enter(snapshot(), NOW) == "new entries disabled"
    engine.advance(END, lambda p, ts: "UP")
    assert ledger.positions()[0]["state"] == "REDEEMED"


def test_real_adapter_and_live_mode_are_rejected(rig):
    settings, ledger, broker, _ = rig
    with pytest.raises(ValueError, match="live execution"):
        replace(settings, mode="live")
    broker.simulated = False
    with pytest.raises(ValueError, match="real-money"):
        Executor(settings, ledger, broker)


@pytest.mark.parametrize("operation", ["buy", "redeem"])
def test_graphql_error_object_is_never_a_successful_receipt(rig, operation):
    settings, ledger, _, _ = rig

    class Malformed(PaperBroker):
        def lookup_buy(self, position):
            return {"errors": ["denied"]} if operation == "buy" else super().lookup_buy(position)

        def lookup_redeem(self, position):
            return {"errors": ["denied"]}

    engine = Executor(settings, ledger, Malformed(ledger))
    engine.enter(snapshot(), NOW)
    engine.advance(END, lambda p, ts: "UP")
    assert ledger.positions()[0]["state"] == ("BUY_PENDING" if operation == "buy" else "REDEEM_PENDING")
    assert ledger.positions()[0]["payout"] == 0


def test_invalid_quote_does_not_reserve_cash(rig):
    settings, ledger, _, _ = rig

    class Malformed(PaperBroker):
        def quote(self, *args):
            return {"errors": ["denied"]}

    assert Executor(settings, ledger, Malformed(ledger)).enter(snapshot(), NOW) == "invalid or stale quote"
    assert ledger.positions() == []


def test_unsubmitted_intent_recovered_after_crash_is_expired(rig):
    settings, ledger, _, engine = rig
    engine.enter(snapshot(), NOW)
    # Reproduce a crash before the pending marker and the adapter call.
    with ledger.conn:
        ledger.conn.execute("DELETE FROM paper_receipts")
        ledger.conn.execute("UPDATE positions SET state='BUY_READY'")
    engine.advance(NOW + 1, lambda p, ts: None)
    assert ledger.positions()[0]["state"] == "EXPIRED"
    assert ledger.summary(settings)["reserved_usd"] == 0
    assert ledger.summary(settings)["cash_usd"] == 10


def test_confirmed_redeem_failure_remains_visible_without_crediting_cash(rig):
    settings, ledger, _, _ = rig

    class Failed(PaperBroker):
        def submit_redeem(self, position):
            self._save(position, "redeem", Receipt(False, 0, 0, "paper:redeem:" + position["id"]))

    engine = Executor(settings, ledger, Failed(ledger))
    engine.enter(snapshot(), NOW)
    engine.advance(END, lambda p, ts: "UP")
    assert ledger.positions()[0]["state"] == "REDEEM_FAILED"
    assert ledger.summary(settings)["cash_usd"] == 9
    assert len(ledger.active()) == 1


def test_ledger_refuses_the_collector_database(tmp_path):
    path = str(tmp_path / "lab.db")
    conn = connect(path)
    initialise(conn)
    conn.close()
    with pytest.raises(ValueError, match="Collector"):
        Ledger(path, Settings())


def test_two_connections_cannot_buy_the_same_round(rig, tmp_path):
    settings, ledger, _, engine = rig
    assert engine.enter(snapshot(), NOW) == "BUY_PENDING"
    other = Ledger(str(tmp_path / "paper.db"), settings)
    try:
        second = Executor(settings, other, PaperBroker(other))
        assert second.enter(snapshot(), NOW) == "already recorded"
        second.advance(END, lambda p, ts: "UP")
        engine.advance(END, lambda p, ts: "UP")
        assert len(ledger.positions()) == 1
        assert ledger.conn.execute("SELECT COUNT(*) FROM paper_receipts").fetchone()[0] == 2
        assert ledger.summary(settings)["cash_usd"] == pytest.approx(10.314422)
    finally:
        other.close()


def test_example_configs_match_requested_wallet_budgets():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    btc = Settings.read(root / "execution-btc.example.json")
    oil = Settings.read(root / "execution-xyzcl.example.json")
    assert btc.symbols == ("BTC",) and oil.symbols == ("XYZCL",)
    assert btc.wallet_label != oil.wallet_label
    for config in (btc, oil):
        assert not config.enabled
        assert config.strategy == "Delta Edge"
        assert config.stake == 1_000_000
        assert config.bankroll == 10_000_000


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "Infinity", "0.0000001", "bad"])
def test_bad_amounts_fail_closed(amount):
    with pytest.raises(ValueError):
        micro(amount)


def test_recordings_refuse_inferred_settlement_and_filter_future_prices():
    conn = connect(":memory:")
    initialise(conn)
    ingest = Ingest(conn, "test")
    ingest.observe_round(RoundMeta("BTC", START, END, 100.0, "pool", "up", "down"), NOW)
    ingest.observe_price("BTC", 100.2, NOW)
    ingest.observe_price("BTC", 50, NOW + 1)
    readings = Recordings(conn)
    current = readings.current("BTC", NOW)
    assert current.record.prices == [(NOW, 100.2)]
    position = {"symbol": "BTC", "ending": END, "pool": "pool"}
    conn.execute("UPDATE rounds SET winner='UP', settled_at=?, settled_source='following-strike'", (END,))
    assert readings.settlement(position, END) is None
    conn.execute("UPDATE rounds SET settled_source='event'")
    assert readings.settlement(position, END) == "UP"
    conn.close()
