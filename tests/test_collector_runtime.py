"""Exercise the actual async boundaries, where direct ingest tests cannot catch failures."""
import asyncio
import sqlite3
import threading

import pytest

from strategy_lab import chain, collector, sources
from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta

T0 = 1789443000


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    yield Ingest(conn, "runtime-test")
    conn.close()


def meta():
    return RoundMeta("BTC", T0, T0 + 900, 100.0, "0xpool", "0xup", "0xdown")


def _one_verification(ingest, monkeypatch, reachable):
    """Drive exactly one pricing check and hand back the thread ids it used."""
    owner = threading.get_ident()
    calls = []

    class Chain:
        def quote(self, pool, outcome, gross):
            calls.append(threading.get_ident())
            return chain.Quote(1_314_422, 17_000, 0) if reachable else None

    monkeypatch.setattr(chain, "Arbitrum", Chain)

    async def one_cycle():
        stop = asyncio.Event()

        async def sleep(stop, seconds):
            if seconds == collector.VERIFY_EVERY_SECONDS:
                stop.set()

        monkeypatch.setattr(collector, "_sleep", sleep)
        await collector.verify_pricing(ingest, stop)

    asyncio.run(one_cycle())
    return owner, calls


def test_a_seeded_pool_is_not_treated_as_a_disagreement(ingest, monkeypatch):
    """Every Round opens with an even pool whether or not anyone looked at it, so the
    check would otherwise measure how far the pool has been traded and call that a fault
    in the pricing rule — lighting the alarm permanently."""
    ingest.observe_round(meta(), now=T0)

    _owner, calls = _one_verification(ingest, monkeypatch, reachable=True)

    assert len(calls) == 1
    row = ingest.conn.execute("SELECT * FROM quote_checks").fetchone()
    assert row["agrees"] is None
    assert "seeded opening pool" in row["note"]


@pytest.mark.parametrize("reachable", [True, False])
def test_daily_verification_records_result_without_moving_sqlite_to_worker(ingest, monkeypatch, reachable):
    ingest.observe_round(meta(), now=T0)
    # An observed pool, so there is something the rule can actually be checked against.
    ingest.apply_remote_reserves(meta().pool_address, up=900_000, down=1_100_000, ts=T0 + 5)
    owner = threading.get_ident()
    calls = []

    class Chain:
        def quote(self, pool, outcome, gross):
            calls.append(threading.get_ident())
            return chain.Quote(1_314_422, 17_000, 0) if reachable else None

    monkeypatch.setattr(chain, "Arbitrum", Chain)

    async def one_cycle():
        stop = asyncio.Event()

        async def sleep(stop, seconds):
            if seconds == collector.VERIFY_EVERY_SECONDS:
                stop.set()

        monkeypatch.setattr(collector, "_sleep", sleep)
        await collector.verify_pricing(ingest, stop)

    asyncio.run(one_cycle())
    assert len(calls) == 1 and calls[0] != owner
    row = ingest.conn.execute("SELECT * FROM quote_checks").fetchone()
    assert row is not None
    # Reaching the chain produces a verdict either way; failing to reach it produces none.
    # Which verdict is the pricing rule's business, not this test's.
    assert (row["agrees"] is not None) == reachable


def test_polling_recovers_after_a_recording_failure(ingest, monkeypatch, caplog):
    monkeypatch.setattr(sources, "SYMBOLS", ("BTC",))
    original = ingest.observe_round
    attempts = []

    class Graph:
        def current_round(self, symbol):
            return meta()

    async def exercise():
        stop = asyncio.Event()

        def observe(value, now):
            attempts.append(value)
            if len(attempts) == 1:
                # A failed transaction must not leak into the next successful poll.
                ingest.conn.execute("INSERT INTO collector_gaps VALUES (1, 'uncommitted', 'test')")
                raise sqlite3.OperationalError("temporary recording failure")
            original(value, now)
            stop.set()

        async def sleep(stop, seconds):
            await asyncio.sleep(0)

        monkeypatch.setattr(ingest, "observe_round", observe)
        monkeypatch.setattr(collector, "_sleep", sleep)
        await asyncio.wait_for(collector.poll_rounds(ingest, Graph(), stop), 2)

    asyncio.run(exercise())
    assert len(attempts) == 2
    assert ingest.conn.execute("SELECT COUNT(*) FROM rounds").fetchone()[0] == 1
    assert ingest.conn.execute("SELECT COUNT(*) FROM collector_gaps").fetchone()[0] == 0
    assert "round poll failed" in caplog.text


@pytest.mark.parametrize("fails", [True, False])
def test_unexpected_task_exit_reaches_process_supervisor(tmp_path, monkeypatch, fails):
    async def stopped(ingest, graph, stop):
        if fails:
            raise RuntimeError("poller failed")

    async def waiting(*args):
        await args[-1].wait()

    monkeypatch.setattr(sources, "all_subscriptions", lambda: [])
    monkeypatch.setattr(collector, "poll_rounds", stopped)
    for name in ("watch_for_silence", "backfill_rounds", "reconcile_reserves",
                 "close_out_rounds", "verify_pricing"):
        monkeypatch.setattr(collector, name, waiting)

    with pytest.raises(RuntimeError, match="poller failed" if fails else "stopped unexpectedly"):
        asyncio.run(asyncio.wait_for(collector.run(str(tmp_path / "lab.db")), 2))


def test_bad_round_numbers_are_rejected_before_recording():
    payload = {"starting": T0, "ending": str(2**63),
               "priceMetadata": {"priceTargetForUp": "100"},
               "outcomes": [{"name": "above", "identifier": "up"},
                            {"name": "below", "identifier": "down"}]}
    assert sources.parse_round("BTC", payload) is None
    payload["ending"] = T0 + 900
    payload["priceMetadata"]["priceTargetForUp"] = "NaN"
    assert sources.parse_round("BTC", payload) is None


class _SilentSocket:
    """Open, answering pings, never sending: how the feed looked after an outage."""
    def __init__(self, connects):
        self.connects = connects

    async def __aenter__(self):
        self.connects.append(1)
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, payload):
        pass

    async def recv(self):
        await asyncio.sleep(3600)


def _run_stream(ingest, monkeypatch, label, seconds):
    connects = []
    monkeypatch.setattr(collector, "FEED_IDLE_SECONDS", 0.05)
    monkeypatch.setattr(collector, "RETRY_MIN", 0.01)
    monkeypatch.setattr(collector.websockets, "connect", lambda *a, **k: _SilentSocket(connects))

    async def drive():
        stop = asyncio.Event()
        task = asyncio.ensure_future(collector.stream_feed({"label": label}, ingest, stop))
        await asyncio.sleep(seconds)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    return connects


def test_a_silent_price_feed_is_reconnected(ingest, monkeypatch):
    assert len(_run_stream(ingest, monkeypatch, "BTC", 0.4)) >= 2
    gaps = [r[0] for r in ingest.conn.execute("SELECT note FROM collector_gaps")]
    assert any("no message for" in g for g in gaps)


def test_a_quiet_pool_table_is_left_alone(ingest, monkeypatch):
    assert len(_run_stream(ingest, monkeypatch, sources.POOL_TABLES[0], 0.3)) == 1
