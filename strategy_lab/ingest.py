"""Turning what the feeds say into what the database holds.

This is the seam the Collector is tested at: messages and API responses go in, database
state comes out, and nothing here opens a socket. The Collector knows nothing about
Strategies — it only records (ADR-0002).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

PRICES_TABLE = "oracles_ninelives_prices_2"


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
        self.conn.commit()
        self._open[meta.symbol] = meta

    def observe_price(self, symbol: str, price: float, ts: int) -> None:
        """Record an underlying price, attributed to whichever Round is open for it.

        A price that cannot be placed inside the open Round is dropped rather than guessed
        at: an unattributable tick is worse than a missing one, because it silently distorts
        the Round it was wrongly filed under. The feed's opening snapshot in particular
        replays hours of prices that predate the open Round.
        """
        meta = self._open.get(symbol)
        if meta is None or ts >= meta.ending:
            return
        if meta.starting and ts < meta.starting:
            return
        self.conn.execute(
            "INSERT INTO ticks (symbol, ts, price, round_ending, code_version) VALUES (?, ?, ?, ?, ?)",
            (symbol, ts, price, meta.ending, self.code_version),
        )
        self.conn.commit()

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
