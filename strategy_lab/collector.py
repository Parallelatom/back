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
RECONCILE_EVERY_SECONDS = float(os.environ.get("STRATEGY_LAB_RECONCILE_SECONDS", "30"))
VERIFY_EVERY_SECONDS = float(os.environ.get("STRATEGY_LAB_VERIFY_SECONDS", "86400"))
VERIFY_DELAY_SECONDS = 120
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
                meta = await asyncio.get_running_loop().run_in_executor(
                    None, graph.current_round, symbol
                )
            except Exception:  # a poll must never end the process
                log.exception("round poll failed for %s", symbol)
                meta = None
            if meta is not None:
                ingest.observe_round(meta, now=int(time.time()))
            await _sleep(stop, sources.jittered(POLL_SECONDS))


async def reconcile_reserves(ingest: Ingest, graph: Graph, stop: asyncio.Event) -> None:
    """Check the Reserves we derived from the trade feed against the exchange's own answer.

    A dropped trade event would otherwise corrupt the Fill of every Paper Trade for the rest
    of a Round, silently. The log of disagreements is as valuable as the correction: it is
    what will eventually say whether the feed can be trusted alone (ADR-0005).
    """
    while not stop.is_set():
        for symbol in sources.SYMBOLS:
            if stop.is_set():
                return
            try:
                answer = await asyncio.get_running_loop().run_in_executor(
                    None, graph.current_reserves, symbol
                )
                if answer:
                    pool, up, down = answer
                    if ingest.apply_remote_reserves(pool, up, down, ts=int(time.time())):
                        log.warning("reserves diverged for %s; took the exchange\'s answer", symbol)
            except Exception:
                log.exception("reserve reconcile failed for %s", symbol)
            await _sleep(stop, sources.jittered(RECONCILE_EVERY_SECONDS / len(sources.SYMBOLS)))


async def verify_pricing(ingest: Ingest, stop: asyncio.Event) -> None:
    """Ask the contract what a ticket really buys, and compare it with what we compute.

    The fee lives in per-market storage rather than being a constant of the protocol, so a
    change to it would skew every Fill silently. Reaching the chain is also the only way to
    catch our own Reserves having drifted. Failing to reach it costs nothing: collection is
    never interrupted for this.
    """
    from .amm import Reserves, fill
    from .chain import Arbitrum

    chain = Arbitrum()
    await _sleep(stop, VERIFY_DELAY_SECONDS)
    while not stop.is_set():
        for symbol in sources.SYMBOLS:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, _verify_one, ingest, chain, symbol
                )
            except Exception:
                log.exception("pricing check failed for %s", symbol)
        await _sleep(stop, VERIFY_EVERY_SECONDS)


def _verify_one(ingest: Ingest, chain, symbol: str) -> None:
    from .amm import Reserves, fill

    gross = 1_000_000
    row = ingest.conn.execute(
        """
        SELECT r.pool_address, r.outcome_up, v.q_up, v.q_down
          FROM rounds r
          JOIN reserves v ON v.symbol = r.symbol AND v.round_ending = r.ending
         WHERE r.symbol = ? AND r.winner IS NULL AND r.pool_address IS NOT NULL
         ORDER BY r.ending DESC, v.rowid DESC LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if row is None:
        return
    local = fill(Reserves(up=row["q_up"], down=row["q_down"]), "UP", gross)
    quoted = chain.quote(row["pool_address"], row["outcome_up"], gross)
    agreed = ingest.record_quote_check(
        row["pool_address"], gross=gross,
        local_shares=local.shares, local_fees=local.fees,
        chain_shares=quoted.shares if quoted else None,
        chain_fees=quoted.fees if quoted else None,
        ts=int(time.time()),
        note=None if quoted else "could not reach the chain",
    )
    if quoted and not agreed:
        log.error(
            "pricing disagrees with the contract for %s: local %s shares / %s fees, "
            "chain %s shares / %s fees",
            symbol, local.shares, local.fees, quoted.shares, quoted.fees,
        )


async def close_out_rounds(ingest: Ingest, stop: asyncio.Event) -> None:
    """Summarise Rounds that have closed, recover any settlement that was missed, and give
    up on the ones that are never going to resolve."""
    while not stop.is_set():
        await _sleep(stop, 60)
        if stop.is_set():
            return
        try:
            now = int(time.time())
            ingest.finalise_closed_rounds(now)
            ingest.settle_from_following_rounds()
            ingest.mark_unsettled(now)
        except Exception:
            log.exception("closing out rounds failed")


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
            authoritative = await asyncio.get_running_loop().run_in_executor(
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
                for table_name in sources.POOL_TABLES:
                    await socket.send(_dumps(sources.pool_subscription(table_name)))
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
    loop = asyncio.get_running_loop()
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
        asyncio.ensure_future(reconcile_reserves(ingest, graph, stop)),
        asyncio.ensure_future(close_out_rounds(ingest, stop)),
        asyncio.ensure_future(verify_pricing(ingest, stop)),
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
    asyncio.run(run(duration=float(duration) if duration else None))


if __name__ == "__main__":
    main()
