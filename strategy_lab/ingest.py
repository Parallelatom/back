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
from typing import Any, Dict, Mapping, Optional

PRICES_TABLE = "oracles_ninelives_prices_2"

# Rounds sit on a fixed grid, and a Round's Strike is the oracle price at its start.
GRID_SECONDS = 900
# How far before a grid boundary the nearest price may sit before the Strike it implies
# stops being evidence and starts being a guess. The feed emits about every 5 seconds.
MAX_BOUNDARY_GAP_SECONDS = 60


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
        self.conn.commit()
        self._open[meta.symbol] = meta

    def observe_price(self, symbol: str, price: float, ts: int) -> None:
        """Record an underlying price, attributed to whichever Round is open for it.

        Every observation is kept, whatever Round happens to be open. The same observation
        arriving again — which happens on every reconnection, since the feed replays its
        snapshot — is stored once.
        """
        self.conn.execute(
            "INSERT OR IGNORE INTO oracle_prices (symbol, ts, price, code_version) VALUES (?, ?, ?, ?)",
            (symbol, ts, price, self.code_version),
        )
        self.conn.commit()

    def reconstruct_rounds(self, symbol: str) -> int:
        """Rebuild past Rounds for a Symbol from its price series alone.

        Rounds fall on a fixed grid and a Round's Strike is the oracle price at its start,
        so a Round is fully determined by two points on the series. A boundary with no
        price close enough to it is left alone rather than interpolated: a guessed Strike
        decides the winner of every Paper Trade in that Round.

        Never disturbs a Round the Collector watched live, and running twice changes nothing.
        """
        series = list(
            self.conn.execute(
                "SELECT ts, price FROM oracle_prices WHERE symbol = ? ORDER BY ts", (symbol,)
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
        self.conn.commit()
        return written

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
        for entry in _price_entries(message):
            self._observe_price_entry(entry)

    def _observe_price_entry(self, entry: Mapping[str, Any]) -> None:
        symbol = entry.get("base")
        amount = entry.get("amount")
        ts = _parse_feed_time(entry.get("created_by"))
        if not isinstance(symbol, str) or ts is None:
            return
        if not isinstance(amount, (int, float)) or isinstance(amount, bool):
            return
        self.observe_price(symbol, float(amount), ts)

    def open_round(self, symbol: str) -> Optional[RoundMeta]:
        return self._open.get(symbol)


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
