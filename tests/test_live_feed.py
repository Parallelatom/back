"""Checks that every Symbol actually receives live prices.

This is the test that was missing. The Collector once ran for hours delivering BTC's
opening snapshot and then nothing, because a second subscription to the same table on one
connection silently replaces the first. There was no error and no disconnection; the
snapshot alone was enough to make it look as though the feed worked.
"""
import asyncio
import json
import os
import time

import pytest

from strategy_lab import sources

pytestmark = pytest.mark.skipif(
    not os.environ.get("STRATEGY_LAB_LIVE"),
    reason="set STRATEGY_LAB_LIVE=1 to run checks against the live feed",
)

# The feed publishes about every five seconds.
LISTEN_SECONDS = 40


async def _updates_per_symbol(subscriptions):
    """Live updates only — the opening snapshot is deliberately not counted."""
    import websockets

    counts = {symbol: 0 for symbol in sources.SYMBOLS}

    async def listen(subscription):
        async with websockets.connect(
            sources.WS_URL, origin=sources.WS_ORIGIN, max_size=None
        ) as socket:
            await socket.send(json.dumps(subscription))
            async for raw in socket:
                message = sources.decode(raw)
                if not message or message.get("table") != sources.PRICES_TABLE:
                    continue
                content = message.get("content")
                if isinstance(content, dict) and content.get("base") in counts:
                    counts[content["base"]] += 1

    try:
        await asyncio.wait_for(
            asyncio.gather(*(listen(s) for s in subscriptions)), timeout=LISTEN_SECONDS
        )
    except asyncio.TimeoutError:
        pass
    return counts


def test_every_symbol_sends_live_prices_not_just_a_snapshot():
    price_subscriptions = [sources.subscription(symbol) for symbol in sources.SYMBOLS]

    counts = asyncio.run(_updates_per_symbol(price_subscriptions))

    silent = [symbol for symbol, seen in counts.items() if seen == 0]
    assert not silent, (
        f"{silent} sent no live price in {LISTEN_SECONDS}s while the others did: {counts}. "
        "Every subscription needs its own connection."
    )


def test_sharing_one_connection_is_what_breaks_it():
    """Pins down the cause, so a future change back to a shared socket is caught here
    rather than by a day of missing data."""
    import websockets

    async def shared():
        counts = {symbol: 0 for symbol in sources.SYMBOLS}
        async with websockets.connect(
            sources.WS_URL, origin=sources.WS_ORIGIN, max_size=None
        ) as socket:
            for symbol in sources.SYMBOLS:
                await socket.send(json.dumps(sources.subscription(symbol)))
            deadline = time.time() + LISTEN_SECONDS

            async def drain():
                async for raw in socket:
                    message = sources.decode(raw)
                    if message and message.get("table") == sources.PRICES_TABLE:
                        content = message.get("content")
                        if isinstance(content, dict) and content.get("base") in counts:
                            counts[content["base"]] += 1
                    if time.time() > deadline:
                        return

            try:
                await asyncio.wait_for(drain(), timeout=LISTEN_SECONDS)
            except asyncio.TimeoutError:
                pass
        return counts

    counts = asyncio.run(shared())

    assert sum(1 for seen in counts.values() if seen == 0) >= 1, (
        f"a shared connection now serves every Symbol ({counts}); if the server has been "
        "fixed, the per-connection workaround can be simplified"
    )
