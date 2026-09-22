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
# How often to ask the contract what a ticket buys, and how long before settlement to
# start asking. Only the entry window can produce a Paper Trade, so quoting outside it
# would spend calls on moments no Strategy can act in.
QUOTE_EVERY_SECONDS = float(os.environ.get("STRATEGY_LAB_QUOTE_SECONDS", "10"))
QUOTE_WINDOW_SECONDS = float(os.environ.get("STRATEGY_LAB_QUOTE_WINDOW_SECONDS", "330"))
# The ticket every Paper Trade is scored on.
QUOTE_GROSS = 1_000_000
VERIFY_DELAY_SECONDS = 120
SILENCE_CHECK_SECONDS = 300
# The feed publishes about every five seconds; five minutes of nothing is a fault.
SILENCE_ALARM_SECONDS = 300
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
                if meta is not None:
                    ingest.observe_round(meta, now=int(time.time()))
            except Exception:  # a poll must never end the process
                ingest.conn.rollback()
                log.exception("round poll failed for %s", symbol)
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


async def record_chain_quotes(ingest: Ingest, graph: Graph, stop: asyncio.Event) -> None:
    """Ask the pool what a one dollar ticket buys, for both Sides, through the entry window.

    This is the price a Paper Trade is scored at. Asking the exchange's indexer instead
    cannot work: it reports nothing for a Round that has already been traded, and Reserves
    reconstructed from that silence price every Fill as though nobody had traded at all.

    Failing to reach the chain costs nothing. A missing quote falls back to the local rule
    and says so, which is a worse price than the truth but an honest one about itself.
    """
    from .chain import Arbitrum

    chain = Arbitrum()
    while not stop.is_set():
        for symbol in sources.SYMBOLS:
            if stop.is_set():
                return
            try:
                await _quote_one(ingest, chain, symbol)
            except Exception:  # a quote must never end the process
                ingest.conn.rollback()
                log.exception("chain quote failed for %s", symbol)
        await _sleep(stop, sources.jittered(QUOTE_EVERY_SECONDS))


async def _quote_one(ingest: Ingest, chain, symbol: str) -> None:
    now = int(time.time())
    row = ingest.conn.execute(
        """
        SELECT ending, pool_address, outcome_up, outcome_down FROM rounds
         WHERE symbol = ? AND winner IS NULL AND pool_address IS NOT NULL
           AND outcome_up IS NOT NULL AND outcome_down IS NOT NULL
           AND ending > ? AND ending <= ?
         ORDER BY ending LIMIT 1
        """,
        (symbol, now, now + QUOTE_WINDOW_SECONDS),
    ).fetchone()
    if row is None:
        return
    loop = asyncio.get_running_loop()
    for side, outcome in (("UP", row["outcome_up"]), ("DOWN", row["outcome_down"])):
        quoted = await loop.run_in_executor(None, chain.quote, row["pool_address"], outcome,
                                            QUOTE_GROSS)
        if quoted is None:
            continue
        # The marginal price is what a Strategy reading the pool reacts to, and it is a
        # separate question from what a ticket buys. Losing it must not lose the quote.
        price = await loop.run_in_executor(None, chain.price, row["pool_address"], outcome)
        ingest.record_chain_quote(symbol, row["ending"], side, int(time.time()),
                                  QUOTE_GROSS, quoted.shares, quoted.fees, price)


async def verify_pricing(ingest: Ingest, stop: asyncio.Event) -> None:
    """Ask the contract what a ticket really buys, and compare it with what we compute.

    The fee lives in per-market storage rather than being a constant of the protocol, so a
    change to it would skew every Fill silently. Reaching the chain is also the only way to
    catch our own Reserves having drifted. Failing to reach it costs nothing: collection is
    never interrupted for this.
    """
    from .chain import Arbitrum

    chain = Arbitrum()
    await _sleep(stop, VERIFY_DELAY_SECONDS)
    while not stop.is_set():
        for symbol in sources.SYMBOLS:
            try:
                await _verify_one(ingest, chain, symbol)
            except Exception:
                log.exception("pricing check failed for %s", symbol)
        await _sleep(stop, VERIFY_EVERY_SECONDS)


async def _verify_one(ingest: Ingest, chain, symbol: str) -> None:
    from .amm import Reserves, fill

    gross = 1_000_000
    row = ingest.conn.execute(
        """
        SELECT r.pool_address, r.outcome_up, v.q_up, v.q_down, v.source
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
    # Only network I/O leaves this thread. The connection belongs to the event loop.
    quoted = await asyncio.get_running_loop().run_in_executor(
        None, chain.quote, row["pool_address"], row["outcome_up"], gross
    )
    # A Round nobody has reported Reserves for still carries the even pool every Round is
    # seeded with (ADR-0004). Comparing that against the chain tests nothing about the
    # pricing rule — it measures how much the pool has been traded since it opened, which
    # is a different fact and one the page already carries per panel. Recording it as a
    # disagreement would leave the alarm lit permanently, and an alarm that is always on
    # is one nobody reads.
    seeded = row["source"] == "seed"
    note = ("could not reach the chain" if not quoted else
            "local Reserves are the seeded opening pool; nothing observed to compare"
            if seeded else None)
    agreed = ingest.record_quote_check(
        row["pool_address"], gross=gross,
        local_shares=local.shares, local_fees=local.fees,
        chain_shares=None if seeded else (quoted.shares if quoted else None),
        chain_fees=None if seeded else (quoted.fees if quoted else None),
        ts=int(time.time()),
        note=note,
    )
    if quoted and not seeded and not agreed:
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


async def stream_feed(subscription, ingest: Ingest, stop: asyncio.Event) -> None:
    """Follow one subscription on its own connection, reconnecting for as long as we live.

    One connection per subscription is not tidiness. The server lets a later subscription
    to the same table replace an earlier one, so sharing a socket between two Symbols
    silently delivers only the second — with no error, no disconnect, and a snapshot from
    the first to make it look as though it had worked.
    """
    label = subscription.get("label", "feed")
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
                await socket.send(_dumps(subscription))
                log.info("feed connected: %s", label)
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
            log.warning("feed %s dropped (%s); retrying in %ss", label, exc, backoff)
            _note_gap(ingest, f"feed {label} dropped: {exc}")
            await _sleep(stop, backoff)
            backoff = min(backoff * 2, RETRY_MAX)


async def watch_for_silence(ingest: Ingest, stop: asyncio.Event) -> None:
    """Complain when a Symbol stops sending prices.

    The feed can go quiet for one Symbol while the connection stays healthy and the others
    keep arriving, which is exactly how a whole day of BTC was lost once. Nothing about
    that failure announced itself, so this is what announces it.
    """
    while not stop.is_set():
        await _sleep(stop, SILENCE_CHECK_SECONDS)
        if stop.is_set():
            return
        now = int(time.time())
        for symbol in sources.SYMBOLS:
            latest = ingest.conn.execute(
                "SELECT MAX(ts) FROM oracle_prices WHERE symbol = ?", (symbol,)
            ).fetchone()[0]
            if latest is None:
                log.error("no prices at all for %s yet", symbol)
            elif now - latest > SILENCE_ALARM_SECONDS:
                log.error(
                    "%s has sent no price for %d seconds; its Rounds will be unscoreable",
                    symbol, now - latest,
                )
                _note_gap(ingest, f"{symbol} silent for {now - latest}s")


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
        asyncio.ensure_future(stream_feed(subscription, ingest, stop))
        for subscription in sources.all_subscriptions()
    ]
    tasks += [
        asyncio.ensure_future(poll_rounds(ingest, graph, stop)),
        asyncio.ensure_future(watch_for_silence(ingest, stop)),
        asyncio.ensure_future(backfill_rounds(ingest, graph, stop)),
        asyncio.ensure_future(reconcile_reserves(ingest, graph, stop)),
        asyncio.ensure_future(close_out_rounds(ingest, stop)),
        asyncio.ensure_future(record_chain_quotes(ingest, graph, stop)),
        asyncio.ensure_future(verify_pricing(ingest, stop)),
    ]
    if duration is not None:
        tasks.append(asyncio.ensure_future(_stop_after(stop, duration)))
    stopped = asyncio.ensure_future(stop.wait())
    try:
        done, _ = await asyncio.wait(tasks + [stopped], return_when=asyncio.FIRST_COMPLETED)
        if not stop.is_set():
            for task in done:
                task.result()  # Propagate failures so the container restarts visibly.
            raise RuntimeError("a Collector task stopped unexpectedly")
    finally:
        stop.set()
        for task in tasks + [stopped]:
            task.cancel()
        await asyncio.gather(*tasks, stopped, return_exceptions=True)
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
