"""Checks the pricing rule against every trade the exchange will show us.

The feed replays thousands of real buys. Each one says what the buyer paid and what they
received, so replaying a pool's trades from its opening Reserves and comparing every
predicted share count against the recorded one is as direct a test of the pricing rule as
can be had without spending money.
"""
import asyncio
import collections
import json
import os

import pytest

from strategy_lab import sources
from strategy_lab.amm import Reserves, buy_with_net

pytestmark = pytest.mark.skipif(
    not os.environ.get("STRATEGY_LAB_LIVE"),
    reason="set STRATEGY_LAB_LIVE=1 to run checks against the live feed",
)

TRADES_TABLE = "ninelives_buys_and_sells_1"


async def _snapshot_trades():
    import websockets

    async with websockets.connect(
        sources.WS_URL, origin=sources.WS_ORIGIN, max_size=None
    ) as socket:
        await socket.send(json.dumps({
            "label": TRADES_TABLE,
            "ask_for_snapshot": [{"table": TRADES_TABLE, "fields": []}],
            "add": [{"table": TRADES_TABLE, "fields": []}],
        }))
        rows = []

        async def drain():
            async for raw in socket:
                message = sources.decode(raw)
                if not message:
                    continue
                for block in message.get("snapshot_toplevel") or ():
                    if block.get("table") == TRADES_TABLE:
                        rows.extend(block.get("snapshot") or ())
                if rows:
                    return

        try:
            await asyncio.wait_for(drain(), timeout=30)
        except asyncio.TimeoutError:
            pass
        return rows


@pytest.fixture(scope="module")
def trades():
    rows = asyncio.run(_snapshot_trades())
    assert rows, "the feed returned no trades to check against"
    return rows


def _by_pool(rows):
    pools = collections.defaultdict(list)
    for row in rows:
        if row.get("type") != "buy":
            continue
        pools[row["emitter_addr"]].append(row)
    for trades_in_pool in pools.values():
        trades_in_pool.sort(key=lambda r: (r.get("block_number") or 0, r.get("id") or 0))
    return pools


def test_replaying_real_trades_reproduces_the_shares_the_buyers_received(trades):
    """Every buy, from each pool's opening Reserves forward."""
    checked = agreed = 0
    disagreements = []
    for pool, rows in _by_pool(trades).items():
        outcomes = {r["outcome_id"] for r in rows}
        if len(outcomes) > 2:
            continue
        # Which identifier is UP is irrelevant here: the arithmetic is symmetric, so one
        # side is arbitrarily called UP and the reserves follow consistently.
        first = sorted(outcomes)[0]
        reserves = Reserves.opening()
        for row in rows:
            try:
                net = int(row["from_amount"])
                received = int(row["to_amount"])
            except (KeyError, TypeError, ValueError):
                continue
            side = "UP" if row["outcome_id"] == first else "DOWN"
            predicted, reserves = buy_with_net(reserves, side, net)
            checked += 1
            if predicted == received:
                agreed += 1
            elif len(disagreements) < 5:
                disagreements.append((pool[:10], net, received, predicted))

    assert checked >= 100, f"only {checked} trades to check; not a meaningful test"
    rate = agreed / checked
    assert rate > 0.95, (
        f"the pricing rule reproduced only {agreed}/{checked} real fills ({rate:.1%}); "
        f"examples (pool, net, actual, predicted): {disagreements}"
    )


def test_the_fee_the_exchange_charges_is_still_the_one_we_assume(trades):
    """`from_amount` is net of the fee. A round 1 USD ticket net of 1.7% is 983,000, and
    that is by far the most common size. A change here would skew every Fill silently."""
    sizes = collections.Counter(int(r["from_amount"]) for r in trades if r.get("from_amount"))
    most_common, _ = sizes.most_common(1)[0]

    assert most_common == 983_000, f"the commonest net size is now {most_common}"
