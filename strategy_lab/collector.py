"""The Collector: runs forever, records everything, decides nothing.

Uptime is the only thing this project cannot buy back later — the upstream API keeps about
2.5 hours of settled Rounds and no archive — so every loop here is written to survive its
own failures and keep going.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import Optional

import websockets

from . import sources
from .db import connect, initialise
from .ingest import Ingest
from .sources import Graph
from .version import code_version

log = logging.getLogger("collector")

DB_PATH = os.environ.get("STRATEGY_LAB_DB", "data/lab.db")
POLL_SECONDS = float(os.environ.get("STRATEGY_LAB_POLL_SECONDS", "3"))
# Long enough for the opening snapshot to have landed, since that is what there is to
# rebuild from; then repeated, because it also fills Rounds the live poller missed.
BACKFILL_DELAY_SECONDS = 30
BACKFILL_EVERY_SECONDS = float(os.environ.get("STRATEGY_LAB_BACKFILL_SECONDS", "900"))
WS_PING_INTERVAL = 15
WS_PING_TIMEOUT = 10
RETRY_MIN = 1
RETRY_MAX = 30


async def poll_rounds(ingest: Ingest, graph: Graph, stop: asyncio.Event) -> None:
    """Ask the API which Round is open, one Symbol at a time.

    Sequential and jittered on purpose: the endpoint answers in about 1.2 seconds and does
    no edge caching, so a burst would gain nothing and risks being throttled.
    """
    while not stop.is_set():
        for symbol in sources.SYMBOLS:
            if stop.is_set():
                return
            try:
                meta = await asyncio.get_event_loop().run_in_executor(
                    None, graph.current_round, symbol
                )
            except Exception:  # a poll must never end the process
                log.exception("round poll failed for %s", symbol)
                meta = None
            if meta is not None:
                ingest.observe_round(meta, now=int(time.time()))
            await _sleep(stop, sources.jittered(POLL_SECONDS))


async def backfill_rounds(ingest: Ingest, graph: Graph, stop: asyncio.Event) -> None:
    """Rebuild past Rounds from the price series, then correct them against the exchange.

    The feed replays several hours of prices on every connection, which is deeper than the
    exchange's own retention, so this is the only way the experiment starts with history
    rather than waiting days for it.
    """
    await _sleep(stop, BACKFILL_DELAY_SECONDS)
    while not stop.is_set():
        try:
            rebuilt = sum(ingest.reconstruct_rounds(symbol) for symbol in sources.SYMBOLS)
            authoritative = await asyncio.get_event_loop().run_in_executor(
                None, graph.past_rounds
            )
            corrected = ingest.apply_authoritative_strikes(authoritative)
            if rebuilt or corrected:
                log.info("backfill: rebuilt %d Rounds, corrected %d", rebuilt, corrected)
        except Exception:  # backfill must never end collection
            log.exception("backfill failed")
        await _sleep(stop, BACKFILL_EVERY_SECONDS)


async def stream_prices(ingest: Ingest, stop: asyncio.Event) -> None:
    """Follow the price feed, reconnecting for as long as the process lives."""
    backoff = RETRY_MIN
    while not stop.is_set():
        try:
            async with websockets.connect(
                sources.WS_URL,
                origin=sources.WS_ORIGIN,
                ping_interval=WS_PING_INTERVAL,
                ping_timeout=WS_PING_TIMEOUT,
                max_size=None,
            ) as socket:
                for symbol in sources.SYMBOLS:
                    await socket.send(_dumps(sources.subscription(symbol)))
                log.info("price feed connected")
                backoff = RETRY_MIN
                async for raw in socket:
                    message = sources.decode(raw)
                    if message is not None:
                        ingest.observe_feed_message(message)
                    if stop.is_set():
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("price feed dropped (%s); retrying in %ss", exc, backoff)
            _note_gap(ingest, f"price feed dropped: {exc}")
            await _sleep(stop, backoff)
            backoff = min(backoff * 2, RETRY_MAX)


def _dumps(payload) -> str:
    import json

    return json.dumps(payload)


def _note_gap(ingest: Ingest, note: str) -> None:
    """A gap the dashboard must be able to show. Data we never collected is data that
    cannot be distinguished later from a market that simply did nothing."""
    try:
        ingest.conn.execute(
            "INSERT INTO collector_gaps (started_at, note, code_version) VALUES (?, ?, ?)",
            (int(time.time()), note, ingest.code_version),
        )
        ingest.conn.commit()
    except Exception:
        log.exception("could not record collector gap")


async def _sleep(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run(db_path: str = DB_PATH, duration: Optional[float] = None) -> None:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    initialise(conn)
    ingest = Ingest(conn, code_version=code_version())
    _note_gap(ingest, "collector started")
    log.info("collector %s writing to %s", ingest.code_version, db_path)

    stop = asyncio.Event()
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    graph = Graph()
    tasks = [
        asyncio.ensure_future(stream_prices(ingest, stop)),
        asyncio.ensure_future(poll_rounds(ingest, graph, stop)),
        asyncio.ensure_future(backfill_rounds(ingest, graph, stop)),
    ]
    if duration is not None:
        tasks.append(asyncio.ensure_future(_stop_after(stop, duration)))
    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        conn.close()


async def _stop_after(stop: asyncio.Event, seconds: float) -> None:
    await _sleep(stop, seconds)
    stop.set()


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("STRATEGY_LAB_LOG", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    duration = os.environ.get("STRATEGY_LAB_RUN_SECONDS")
    asyncio.get_event_loop().run_until_complete(
        run(duration=float(duration) if duration else None)
    )


if __name__ == "__main__":
    main()
