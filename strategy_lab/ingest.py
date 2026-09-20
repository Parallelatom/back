"""Turning what the feeds say into what the database holds.

This is the seam the Collector is tested at: messages and API responses go in, database
state comes out, and nothing here opens a socket. The Collector knows nothing about
Strategies — it only records (ADR-0002).
"""
from __future__ import annotations

import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional, Tuple

from .amm import Reserves, buy_with_net

PRICES_TABLE = "oracles_ninelives_prices_2"
DECIDED_TABLE = "ninelives_events_outcome_decided"
TRADES_TABLE = "ninelives_buys_and_sells_1"
# How long a closed Round is given to produce a decision event before we stop expecting one.
SETTLEMENT_GRACE_SECONDS = 1800

# Rounds sit on a fixed grid, and a Round's Strike is the oracle price at its start.
GRID_SECONDS = 900
# How far before a grid boundary the nearest price may sit before the Strike it implies
# stops being evidence and starts being a guess. The feed emits about every 5 seconds.
MAX_BOUNDARY_GAP_SECONDS = 60
# The feed publishes about every 5 seconds and has been seen to stretch to 15. A silence
# longer than this is the Collector having missed something, not the feed being unhurried.
MAX_COVERAGE_GAP_SECONDS = 60


@dataclass(frozen=True)
class RoundMeta:
    """A Round as the API describes it."""

    symbol: str
    starting: int
    ending: int
    strike: float
    pool_address: str
    outcome_up: str
    outcome_down: str


class Ingest:
    def __init__(self, conn: sqlite3.Connection, code_version: str):
        self.conn = conn
        self.code_version = code_version
        self._open: Dict[str, RoundMeta] = {}

    def observe_round(self, meta: RoundMeta, now: int) -> None:
        """Record a Round the API has told us about, and treat it as the open one."""
        self.conn.execute(
            """
            INSERT INTO rounds (
                symbol, ending, starting, strike, pool_address, outcome_up, outcome_down,
                first_seen_at, last_seen_at, code_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (symbol, ending) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                starting     = COALESCE(rounds.starting, excluded.starting),
                strike       = COALESCE(rounds.strike, excluded.strike),
                pool_address = COALESCE(rounds.pool_address, excluded.pool_address),
                outcome_up   = COALESCE(rounds.outcome_up, excluded.outcome_up),
                outcome_down = COALESCE(rounds.outcome_down, excluded.outcome_down)
            """,
            (
                meta.symbol,
                meta.ending,
                meta.starting,
                meta.strike,
                meta.pool_address,
                meta.outcome_up,
                meta.outcome_down,
                now,
                now,
                self.code_version,
            ),
        )
        self.conn.execute(
            "UPDATE rounds SET source = 'live' WHERE symbol = ? AND ending = ?",
            (meta.symbol, meta.ending),
        )
        self._seed_reserves(meta, now)
        self.conn.commit()
        self._open[meta.symbol] = meta

    def _seed_reserves(self, meta: RoundMeta, now: int) -> None:
        """A Round opens with half a dollar on each Side, always. Nothing needs to be asked
        of the network to know that (ADR-0004)."""
        if not meta.pool_address:
            return
        already = self.conn.execute(
            "SELECT 1 FROM reserves WHERE LOWER(pool_address) = LOWER(?) LIMIT 1",
            (meta.pool_address,),
        ).fetchone()
        if already:
            # Reconciliation can beat metadata on startup or at a Round boundary.
            # Keep its observed values and attach them so Replay can actually find them.
            self.conn.execute(
                """UPDATE reserves SET symbol = ?, round_ending = ?
                     WHERE LOWER(pool_address) = LOWER(?)
                       AND (symbol IS NULL OR round_ending IS NULL)""",
                (meta.symbol, meta.ending, meta.pool_address),
            )
            return
        opening = Reserves.opening()
        self._write_reserves(
            meta.pool_address, opening, ts=meta.starting or now,
            source="seed", symbol=meta.symbol, ending=meta.ending,
        )

    def observe_price(self, symbol: str, price: float, ts: int) -> None:
        """Record an underlying price, attributed to whichever Round is open for it.

        Every observation is kept, whatever Round happens to be open. The same observation
        arriving again — which happens on every reconnection, since the feed replays its
        snapshot — is stored once.
        """
        self._insert_price(symbol, ts, price)
        self.conn.commit()

    def _insert_price(self, symbol: str, ts: int, price: float) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO oracle_prices (symbol, ts, price, code_version) VALUES (?, ?, ?, ?)",
            (symbol, ts, price, self.code_version),
        )

    def reconstruct_rounds(self, symbol: str) -> int:
        """Rebuild past Rounds for a Symbol from its price series alone.

        Rounds fall on a fixed grid and a Round's Strike is the oracle price at its start,
        so a Round is fully determined by two points on the series. A boundary with no
        price close enough to it is left alone rather than interpolated: a guessed Strike
        decides the winner of every Paper Trade in that Round.

        Never disturbs a Round the Collector watched live, and running twice changes nothing.
        """
        pending = self.conn.execute(
            "SELECT first_ts, last_ts FROM reconstruction_pending WHERE symbol = ?", (symbol,)
        ).fetchone()
        if pending is None:
            return 0
        # Include the adjacent Round and the price just before its opening boundary.
        # The durable range survives restarts and includes out-of-order snapshot inserts.
        series = list(
            self.conn.execute(
                "SELECT ts, price FROM oracle_prices WHERE symbol = ? AND ts BETWEEN ? AND ? ORDER BY ts",
                (symbol, pending["first_ts"] - GRID_SECONDS - MAX_BOUNDARY_GAP_SECONDS,
                 pending["last_ts"] + GRID_SECONDS),
            )
        )
        if len(series) < 2:
            return 0
        stamps = [row["ts"] for row in series]
        boundaries = _grid_boundaries(stamps[0], stamps[-1])
        written = 0
        for starting, ending in zip(boundaries, boundaries[1:]):
            strike = _price_at(series, stamps, starting)
            final = _price_at(series, stamps, ending)
            if strike is None or final is None:
                continue
            cursor = self.conn.execute(
                """
                INSERT INTO rounds (
                    symbol, ending, starting, strike, first_seen_at, last_seen_at,
                    source, winner, final_price, settled_at, settled_source, code_version
                ) VALUES (?, ?, ?, ?, ?, ?, 'reconstructed', ?, ?, ?, 'reconstructed', ?)
                ON CONFLICT (symbol, ending) DO NOTHING
                """,
                (
                    symbol, ending, starting, strike, ending, ending,
                    "UP" if final > strike else "DOWN", final, ending, self.code_version,
                ),
            )
            written += cursor.rowcount or 0
        self.conn.execute("DELETE FROM reconstruction_pending WHERE symbol = ?", (symbol,))
        self.conn.commit()
        return written

    def _write_reserves(self, pool: str, reserves: Reserves, ts: int, source: str,
                        symbol: Optional[str], ending: Optional[int]) -> None:
        self.conn.execute(
            """
            INSERT INTO reserves (pool_address, ts, q_up, q_down, source, symbol,
                                  round_ending, code_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pool, ts, reserves.up, reserves.down, source, symbol, ending, self.code_version),
        )

    def _latest_reserves(self, pool: str) -> Optional[Tuple[Reserves, Any]]:
        found = self.conn.execute(
            """
            SELECT q_up, q_down, symbol, round_ending FROM reserves
             WHERE LOWER(pool_address) = LOWER(?) ORDER BY rowid DESC LIMIT 1
            """,
            (pool,),
        ).fetchone()
        if found is None:
            return None
        return Reserves(up=found["q_up"], down=found["q_down"]), found

    def _observe_trade(self, content: Mapping[str, Any]) -> None:
        """Move the Reserves by a trade the feed reported.

        Derived locally rather than asked for, because Reserves only change when someone
        trades and trades arrive here as they happen (ADR-0005). The periodic reconcile
        against the exchange is what keeps a dropped event from corrupting the rest of a
        Round in silence — and, until a live trade has been observed end to end, it is also
        what will tell us whether this arithmetic is right at all.
        """
        pool = content.get("emitter_addr")
        outcome = content.get("outcome_id")
        if not isinstance(pool, str) or not isinstance(outcome, str):
            return
        found = self.conn.execute(
            """
            SELECT symbol, ending, outcome_up, outcome_down FROM rounds
             WHERE LOWER(pool_address) = LOWER(?)
            """,
            (pool,),
        ).fetchone()
        if found is None:
            return
        wanted = _bare(outcome)
        if wanted == _bare(found["outcome_up"]):
            side = "UP"
        elif wanted == _bare(found["outcome_down"]):
            side = "DOWN"
        else:
            return
        try:
            # The feed reports the amount after the fee was taken, not what was handed over.
            net = int(float(content.get("from_amount")))
        except (TypeError, ValueError):
            return
        current = self._latest_reserves(pool)
        if current is None:
            return
        ts = _parse_feed_time(content.get("created_by"))
        _, moved = buy_with_net(current[0], side, net)
        self._write_reserves(pool, moved, ts=ts or found["ending"], source="trade",
                             symbol=found["symbol"], ending=found["ending"])

    def apply_remote_reserves(self, pool: str, up: int, down: int, ts: int) -> bool:
        """Record the exchange's own answer, and say whether it disagreed with ours."""
        current = self._latest_reserves(pool)
        remote = Reserves(up=up, down=down)
        disagreed = current is not None and (current[0].up, current[0].down) != (up, down)
        symbol = current[1]["symbol"] if current else None
        ending = current[1]["round_ending"] if current else None
        if disagreed:
            self.conn.execute(
                """
                INSERT INTO reconcile_log (ts, pool_address, symbol, round_ending,
                                           local_q_up, local_q_down, remote_q_up,
                                           remote_q_down, code_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, pool, symbol, ending, current[0].up, current[0].down, up, down,
                 self.code_version),
            )
        if current is None or disagreed:
            self._write_reserves(pool, remote, ts=ts, source="graphql",
                                 symbol=symbol, ending=ending)
        self.conn.commit()
        return disagreed

    def _observe_decision(self, content: Mapping[str, Any]) -> None:
        """Record which Side won, from the chain's own decision event."""
        pool = content.get("emitter_addr")
        identifier = content.get("identifier")
        if not isinstance(pool, str) or not isinstance(identifier, str):
            return
        found = self.conn.execute(
            """
            SELECT symbol, ending, outcome_up, outcome_down FROM rounds
             WHERE LOWER(pool_address) = LOWER(?)
            """,
            (pool,),
        ).fetchone()
        if found is None:
            return
        wanted = _bare(identifier)
        if wanted == _bare(found["outcome_up"]):
            winner = "UP"
        elif wanted == _bare(found["outcome_down"]):
            winner = "DOWN"
        else:
            return
        close = self.conn.execute(
            "SELECT price FROM oracle_prices WHERE symbol = ? AND ts <= ? ORDER BY ts DESC LIMIT 1",
            (found["symbol"], found["ending"]),
        ).fetchone()
        self.conn.execute(
            """
            UPDATE rounds
               SET winner = ?, final_price = COALESCE(?, final_price),
                   settled_at = ?, settled_source = 'event', unsettled = NULL
             WHERE symbol = ? AND ending = ?
            """,
            (winner, close["price"] if close else None, found["ending"],
             found["symbol"], found["ending"]),
        )

    def settle_from_following_rounds(self) -> int:
        """Recover a missed settlement from the next Round's Strike, which is this Round's
        close. Only the immediately following Round on the grid will do: any later one
        closes a different Round, and using it would invent a result."""
        cursor = self.conn.execute(
            """
            UPDATE rounds AS r
               SET final_price    = (SELECT n.strike FROM rounds n
                                      WHERE n.symbol = r.symbol AND n.ending = r.ending + ?),
                   winner         = (SELECT CASE WHEN n.strike > r.strike THEN 'UP' ELSE 'DOWN' END
                                       FROM rounds n
                                      WHERE n.symbol = r.symbol AND n.ending = r.ending + ?),
                   settled_at     = r.ending,
                   settled_source = 'following-strike',
                   unsettled      = NULL
             WHERE r.winner IS NULL
               AND r.strike IS NOT NULL
               AND EXISTS (SELECT 1 FROM rounds n
                            WHERE n.symbol = r.symbol AND n.ending = r.ending + ?
                              AND n.strike IS NOT NULL)
            """,
            (GRID_SECONDS, GRID_SECONDS, GRID_SECONDS),
        )
        self.conn.commit()
        return cursor.rowcount or 0

    def mark_unsettled(self, now: int) -> int:
        """Give up on Rounds that closed long enough ago that no result is coming.

        An unknown outcome must never be quietly counted as a loss, so it is marked and
        left out of every figure rather than guessed.
        """
        cursor = self.conn.execute(
            "UPDATE rounds SET unsettled = 1 WHERE winner IS NULL AND ending <= ?",
            (now - SETTLEMENT_GRACE_SECONDS,),
        )
        self.conn.commit()
        return cursor.rowcount or 0

    def finalise_round(self, symbol: str, ending: int) -> None:
        """Summarise a Round that has closed, and judge whether it is fit to score.

        Partial is decided by the coverage actually held rather than by when the Collector
        started: a restart replays the feed snapshot, which can leave a Round fully covered
        even though nobody was watching while it ran.
        """
        found = self.conn.execute(
            "SELECT starting, strike FROM rounds WHERE symbol = ? AND ending = ?",
            (symbol, ending),
        ).fetchone()
        if found is None:
            return
        starting = found["starting"] or (ending - GRID_SECONDS)
        stamps, prices = [], []
        for observation in self.conn.execute(
            "SELECT ts, price FROM oracle_prices WHERE symbol = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (symbol, starting, ending),
        ):
            stamps.append(observation["ts"])
            prices.append(observation["price"])

        distinct = len(set(prices))
        self.conn.execute(
            """
            UPDATE rounds
               SET tick_count           = ?,
                   distinct_price_count = ?,
                   price_min            = ?,
                   price_max            = ?,
                   oracle_stale         = ?,
                   partial              = ?
             WHERE symbol = ? AND ending = ?
            """,
            (
                len(prices),
                distinct,
                min(prices) if prices else None,
                max(prices) if prices else None,
                1 if distinct <= 1 else 0,
                0 if _fully_covered(stamps, starting, ending) else 1,
                symbol,
                ending,
            ),
        )
        self._record_crossings(symbol, ending, found["strike"], list(zip(stamps, prices)))
        self.conn.commit()

    def _record_crossings(self, symbol: str, ending: int, strike, observations) -> None:
        """Write down every time the price changed sides, with its size and how long it held.

        Derivable from the series, and recorded anyway: trying a different rule for Flip
        Follow later should be a query, not another week of collection.
        """
        self.conn.execute(
            "DELETE FROM delta_flips WHERE symbol = ? AND round_ending = ?", (symbol, ending)
        )
        if not strike:
            return
        previous = None
        crossings = []
        for ts, price in observations:
            delta = (price - strike) / strike * 100.0
            # A Round asks whether the price ends up *above* the Strike, so level is DOWN.
            side = "UP" if delta > 0 else "DOWN"
            if previous is not None and side != previous:
                crossings.append([ts, side, delta, 0])
            if crossings and crossings[-1][1] == side:
                crossings[-1][3] = ts - crossings[-1][0]
            previous = side
        for ts, side, delta, held in crossings:
            self.conn.execute(
                """
                INSERT INTO delta_flips (symbol, round_ending, ts, to_side, delta_pct,
                                         held_seconds, code_version)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, ending, ts, side, delta, held, self.code_version),
            )

    def record_quote_check(self, pool: str, gross: int, local_shares: Optional[int],
                           local_fees: Optional[int], chain_shares: Optional[int],
                           chain_fees: Optional[int], ts: int,
                           note: Optional[str] = None) -> bool:
        """Write down what we think a ticket buys and what the contract says it buys.

        Returns whether the two agreed. A check that never reached the chain agrees with
        nothing and disagrees with nothing: it is recorded with its reason and no verdict.
        """
        agrees = None
        if chain_shares is not None and chain_fees is not None:
            agrees = int(local_shares == chain_shares and local_fees == chain_fees)
        self.conn.execute(
            """
            INSERT INTO quote_checks (ts, pool_address, gross, local_shares, chain_shares,
                                      local_fees, chain_fees, agrees, note, code_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, pool, gross, local_shares, chain_shares, local_fees, chain_fees,
             agrees, note, self.code_version),
        )
        self.conn.commit()
        return bool(agrees)

    def finalise_closed_rounds(self, now: int) -> int:
        """Summarise every closed Round that has not been summarised on any evidence.

        A Round summarised while no prices had arrived for it was judged on nothing, and is
        revisited: the feed's snapshot reaches back hours, so a fault that has since been
        fixed would otherwise go on costing Rounds that are now perfectly good.

        Backfilled Rounds are left alone. They arrive whole from a venue's own record of
        what settled, with no tick history to re-derive anything from, so summarising them
        against this feed's grid would only find them wanting and mark them partial —
        silently dropping every one of them from the scoring.
        """
        pending = self.conn.execute(
            """
            SELECT symbol, ending FROM rounds
             WHERE ending <= ?
               AND (distinct_price_count IS NULL OR tick_count = 0)
               AND source <> 'backfill'
             ORDER BY ending
            """,
            (now,),
        ).fetchall()
        for round_row in pending:
            self.finalise_round(round_row["symbol"], round_row["ending"])
        return len(pending)

    def apply_authoritative_strikes(self, strikes) -> int:
        """Replace rebuilt Strikes with the exchange's own, for Rounds it still remembers.

        A rebuilt Strike is the oracle price nearest the grid boundary, but the exchange
        samples at the moment the market was created on chain — a few seconds earlier, and
        by an amount that varies with block timing. Measured against 22 live Rounds the two
        agreed exactly in 20 and differed by a few price units in the other two, never
        enough to change a winner. Still, where the exchange can answer, it is the answer.

        Rounds the Collector watched live already hold authoritative data and are untouched.
        """
        corrected = 0
        for symbol, ending, strike in strikes:
            cursor = self.conn.execute(
                """
                UPDATE rounds
                   SET strike         = ?,
                       winner         = CASE WHEN final_price > ? THEN 'UP' ELSE 'DOWN' END,
                       settled_source = 'reconstructed+exchange'
                 WHERE symbol = ? AND ending = ? AND source = 'reconstructed'
                """,
                (strike, strike, symbol, ending),
            )
            corrected += cursor.rowcount or 0
        self.conn.commit()
        return corrected

    def observe_feed_message(self, message: Mapping[str, Any]) -> None:
        """Accept one message from the live feed, exactly as it arrives on the wire.

        A message that cannot be understood is discarded. The Collector's job is to keep
        running: a single malformed frame costs one tick, whereas an exception costs every
        tick until someone notices the process died, and those are unrecoverable.
        """
        if not isinstance(message, Mapping):
            return
        # One commit per message, not per price: the opening snapshot carries thousands of
        # observations, and committing each separately turns a burst into minutes of work.
        for entry in _price_entries(message):
            self._observe_price_entry(entry)
        if message.get("table") == TRADES_TABLE:
            content = message.get("content")
            if isinstance(content, Mapping):
                self._observe_trade(content)
        if message.get("table") == DECIDED_TABLE:
            content = message.get("content")
            if isinstance(content, Mapping):
                self._observe_decision(content)
        self.conn.commit()

    def _observe_price_entry(self, entry: Mapping[str, Any]) -> None:
        symbol = entry.get("base")
        amount = entry.get("amount")
        ts = _parse_feed_time(entry.get("created_by"))
        if not isinstance(symbol, str) or ts is None:
            return
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            return
        self._insert_price(symbol, ts, float(amount))

    def open_round(self, symbol: str) -> Optional[RoundMeta]:
        return self._open.get(symbol)


def _bare(identifier: Any) -> str:
    text = str(identifier or "").lower()
    return text[2:] if text.startswith("0x") else text


def _fully_covered(stamps, starting: int, ending: int) -> bool:
    """Whether the observations span the Round with no silence long enough to hide a move."""
    if not stamps:
        return False
    if stamps[0] - starting > MAX_BOUNDARY_GAP_SECONDS:
        return False
    if ending - stamps[-1] > MAX_BOUNDARY_GAP_SECONDS:
        return False
    return all(
        later - earlier <= MAX_COVERAGE_GAP_SECONDS
        for earlier, later in zip(stamps, stamps[1:])
    )


def _grid_boundaries(first: int, last: int):
    start = -(-first // GRID_SECONDS) * GRID_SECONDS  # first boundary at or after `first`
    end = (last // GRID_SECONDS) * GRID_SECONDS
    return list(range(start, end + 1, GRID_SECONDS))


def _price_at(series, stamps, moment: int) -> Optional[float]:
    """The oracle price in force at a moment: the most recent observation at or before it,
    provided one is close enough to stand as evidence."""
    index = bisect_right(stamps, moment) - 1
    if index < 0:
        return None
    if moment - stamps[index] > MAX_BOUNDARY_GAP_SECONDS:
        return None
    return float(series[index]["price"])


def _price_entries(message: Mapping[str, Any]):
    """Both shapes the feed uses: the opening snapshot, and the per-trade delta."""
    for block in message.get("snapshot_toplevel") or ():
        if isinstance(block, Mapping) and block.get("table") == PRICES_TABLE:
            for entry in block.get("snapshot") or ():
                if isinstance(entry, Mapping):
                    yield entry
    if message.get("table") == PRICES_TABLE:
        content = message.get("content")
        if isinstance(content, Mapping):
            yield content


def _parse_feed_time(raw: Any) -> Optional[int]:
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())
