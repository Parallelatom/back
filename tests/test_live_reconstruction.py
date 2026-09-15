"""Checks reconstruction against the live API.

Skipped unless STRATEGY_LAB_LIVE is set, so the ordinary suite stays offline and fast. This
is the only test that can say the grid rule is *true* rather than merely self-consistent:
it rebuilds Strikes from the price feed and compares them against what the exchange says.
"""
import os
import asyncio
import json

import pytest

from strategy_lab import sources
from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest
from strategy_lab.sources import Graph

pytestmark = pytest.mark.skipif(
    not os.environ.get("STRATEGY_LAB_LIVE"),
    reason="set STRATEGY_LAB_LIVE=1 to run checks against the live feed",
)

SNAPSHOT_SECONDS = 20


async def _collect_snapshot(ingest):
    import websockets

    async with websockets.connect(
        sources.WS_URL, origin=sources.WS_ORIGIN, max_size=None
    ) as socket:
        for symbol in sources.SYMBOLS:
            await socket.send(json.dumps(sources.subscription(symbol)))
        try:
            await asyncio.wait_for(_drain(socket, ingest), timeout=SNAPSHOT_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _drain(socket, ingest):
    async for raw in socket:
        message = sources.decode(raw)
        if message is not None:
            ingest.observe_feed_message(message)


@pytest.fixture(scope="module")
def reconstructed(tmp_path_factory):
    conn = connect(str(tmp_path_factory.mktemp("live") / "lab.db"))
    initialise(conn)
    ingest = Ingest(conn, code_version="livetest")
    asyncio.run(_collect_snapshot(ingest))
    for symbol in sources.SYMBOLS:
        ingest.reconstruct_rounds(symbol)
    return ingest


def test_the_snapshot_carries_enough_history_to_rebuild_past_rounds(reconstructed):
    built = reconstructed.conn.execute(
        "SELECT COUNT(*) FROM rounds WHERE symbol = 'BTC'"
    ).fetchone()[0]

    assert built >= 5, f"the feed snapshot yielded only {built} reconstructable BTC Rounds"


def test_rebuilt_strikes_are_never_wrong_enough_to_change_who_won(reconstructed):
    """The exchange samples its Strike when the market is created on chain, a few
    unpredictable seconds before the grid boundary, so exact agreement is not achievable.
    What must hold is that the disagreement is far smaller than the move within a Round —
    otherwise a rebuilt Round could record the wrong winner, which is the one error that
    would quietly corrupt every Hit Rate computed from it."""
    largest_disagreement = 0.0
    smallest_move = None
    overlapping = 0
    for symbol, ending, strike in Graph().past_rounds():
        row = reconstructed.conn.execute(
            "SELECT strike, final_price FROM rounds WHERE symbol = ? AND ending = ?",
            (symbol, ending),
        ).fetchone()
        if row is None or row["final_price"] is None:
            continue
        overlapping += 1
        largest_disagreement = max(largest_disagreement, abs(row["strike"] - strike))
        move = abs(row["final_price"] - strike)
        smallest_move = move if smallest_move is None else min(smallest_move, move)

    assert overlapping >= 5, f"only {overlapping} Rounds overlapped; not a meaningful check"
    assert smallest_move > 2 * largest_disagreement, (
        f"worst Strike disagreement {largest_disagreement} is not comfortably smaller than "
        f"the tightest Round's move {smallest_move}; rebuilt winners cannot be trusted"
    )


def test_correcting_against_the_exchange_makes_the_strikes_exact(reconstructed):
    exchange = Graph().past_rounds()
    reconstructed.apply_authoritative_strikes(exchange)

    for symbol, ending, strike in exchange:
        row = reconstructed.conn.execute(
            "SELECT strike FROM rounds WHERE symbol = ? AND ending = ? AND source = 'reconstructed'",
            (symbol, ending),
        ).fetchone()
        if row is None:
            continue
        assert row["strike"] == pytest.approx(strike, abs=1e-9)
