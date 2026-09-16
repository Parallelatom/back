"""Causal Strategy decisions from observations available at the current moment only."""
from dataclasses import dataclass, replace
import math

from ..amm import Reserves
from ..ingest import _fully_covered
from ..replay import ALL_STRATEGIES, RoundRecord, WINDOW_CLOSES, WINDOW_OPENS


@dataclass(frozen=True)
class Snapshot:
    record: RoundRecord
    pool: str
    outcome_up: str
    outcome_down: str
    metadata_at: int
    reserve_source: str


def decide(snapshot, settings, now):
    record = snapshot.record
    if record.symbol not in settings.symbols:
        return None, "Symbol is not assigned to this wallet"
    if record.winner:
        return None, "Round already resolved"
    if not WINDOW_CLOSES <= record.ending - now <= WINDOW_OPENS:
        return None, "outside Trade Window"
    if not 0 <= now - snapshot.metadata_at <= settings.max_age_seconds:
        return None, "stale Round metadata"
    if not snapshot.pool or not snapshot.outcome_up or not snapshot.outcome_down:
        return None, "missing pool or outcomes"
    if not math.isfinite(record.strike) or record.strike <= 0:
        return None, "invalid Strike"
    prices = sorted((ts, price) for ts, price in record.prices if record.starting <= ts <= now)
    reserves = sorted(((ts, value) for ts, value in record.reserves if ts <= now), key=lambda v: v[0])
    if not prices or now - prices[-1][0] > settings.max_age_seconds:
        return None, "stale oracle price"
    if any(not math.isfinite(price) or price <= 0 for _, price in prices):
        return None, "invalid oracle price"
    if not _fully_covered([ts for ts, _ in prices], record.starting, now):
        return None, "Partial Round at decision time"
    if len({price for _, price in prices}) < 2:
        return None, "Oracle Stale at decision time"
    if settings.mode == "live":
        # Delta Edge uses only prices and Strike. Collector timestamps represent reserve
        # changes, not freshness of a read: unchanged pools may retain old/seed rows.
        # Executor still requires a successful fresh on-chain quote before reserving or
        # submitting. Do not extend this exception to reserve-dependent strategies.
        if settings.strategy != "Delta Edge":
            return None, "live signal supports Delta Edge only"
        reserves = []
    else:
        if (not reserves or snapshot.reserve_source not in ("trade", "graphql")
                or now - reserves[-1][0] > settings.max_age_seconds):
            return None, "no fresh observed Reserves; opening assumptions are not executable quotes"
        if any(value.up <= 0 or value.down <= 0 for _, value in reserves):
            return None, "invalid Reserves"
    causal = replace(record, prices=prices, reserves=reserves)
    strategy = next(s for s in ALL_STRATEGIES if s.name == settings.strategy)
    entry = strategy.decide(causal)
    if entry is None or not 0 <= now - entry.at <= settings.max_signal_age_seconds:
        return None, "no fresh Strategy signal"
    return entry, "signal"


class Recordings:
    """Read Collector evidence without writing it or accepting reconstructed settlements."""

    def __init__(self, conn):
        self.conn = conn

    def current(self, symbol, now):
        row = self.conn.execute(
            "SELECT * FROM rounds WHERE symbol = ? AND source = 'live' AND ending > ? "
            "AND COALESCE(starting, ending - 900) <= ? ORDER BY ending LIMIT 1",
            (symbol, now, now),
        ).fetchone()
        if row is None or row["strike"] is None:
            return None
        starting = row["starting"] or row["ending"] - 900
        prices = [(r["ts"], r["price"]) for r in self.conn.execute(
            "SELECT ts,price FROM oracle_prices WHERE symbol = ? AND ts BETWEEN ? AND ? ORDER BY ts",
            (symbol, starting, now),
        )]
        reserves = list(self.conn.execute(
            "SELECT ts,q_up,q_down,source FROM reserves "
            "WHERE symbol = ? AND round_ending = ? AND ts <= ? ORDER BY ts,rowid",
            (symbol, row["ending"], now),
        ))
        record = RoundRecord(symbol, starting, row["ending"], row["strike"], row["winner"] or "",
                             prices, [(r["ts"], Reserves(r["q_up"], r["q_down"])) for r in reserves])
        return Snapshot(record, row["pool_address"], row["outcome_up"], row["outcome_down"],
                        row["last_seen_at"], reserves[-1]["source"] if reserves else "missing")

    def settlement(self, position, now):
        row = self.conn.execute(
            "SELECT winner,settled_at,settled_source FROM rounds "
            "WHERE symbol = ? AND ending = ? AND LOWER(pool_address) = LOWER(?)",
            (position["symbol"], position["ending"], position["pool"]),
        ).fetchone()
        if (row and row["settled_source"] == "event" and row["winner"] in ("UP", "DOWN")
                and row["settled_at"] is not None and row["settled_at"] <= now):
            return row["winner"]
        return None
