"""The recording store.

The Collector writes here and the dashboard reads here, concurrently, which is why the
database runs in WAL mode with a busy timeout: without it a reading dashboard takes a lock
that stalls the writer, and collection is the one thing that cannot be recovered later.

No table holds Paper Trades or Strategy results. Those are derived at read time from these
recordings, so that a Strategy invented next month can be scored against every Round already
collected (ADR-0002).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

BUSY_TIMEOUT_MS = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    symbol               TEXT    NOT NULL,
    ending               INTEGER NOT NULL,
    starting             INTEGER,
    strike               REAL,
    pool_address         TEXT,
    outcome_up           TEXT,
    outcome_down         TEXT,
    first_seen_at        INTEGER NOT NULL,
    last_seen_at         INTEGER NOT NULL,
    source               TEXT    NOT NULL DEFAULT 'live',
    partial              INTEGER NOT NULL DEFAULT 0,
    oracle_stale         INTEGER,
    unsettled            INTEGER,
    winner               TEXT,
    final_price          REAL,
    settled_at           INTEGER,
    settled_source       TEXT,
    tick_count           INTEGER NOT NULL DEFAULT 0,
    distinct_price_count INTEGER,
    price_min            REAL,
    price_max            REAL,
    code_version         TEXT    NOT NULL,
    PRIMARY KEY (symbol, ending)
);
CREATE INDEX IF NOT EXISTS rounds_by_pool ON rounds (LOWER(pool_address));

-- The complete oracle series, kept whole. Which Round a price falls inside is a question
-- answered at read time by joining to `rounds`, never a reason to discard the price: the
-- feed replays hours of history on every connection and that history is what makes past
-- Rounds reconstructable.
CREATE TABLE IF NOT EXISTS oracle_prices (
    symbol       TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    price        REAL    NOT NULL,
    code_version TEXT    NOT NULL,
    PRIMARY KEY (symbol, ts)
);

-- Work waiting to be reconstructed, not a second copy of the observations. Keeping this
-- in the same transaction as a new price makes restart recovery incremental too.
CREATE TABLE IF NOT EXISTS reconstruction_pending (
    symbol   TEXT PRIMARY KEY,
    first_ts INTEGER NOT NULL,
    last_ts  INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS new_price_needs_replay AFTER INSERT ON oracle_prices
BEGIN
    INSERT INTO reconstruction_pending (symbol, first_ts, last_ts)
    VALUES (NEW.symbol, NEW.ts, NEW.ts)
    ON CONFLICT (symbol) DO UPDATE SET
        first_ts = MIN(first_ts, excluded.first_ts),
        last_ts = MAX(last_ts, excluded.last_ts);
    UPDATE rounds SET distinct_price_count = NULL
     WHERE symbol = NEW.symbol AND ending BETWEEN NEW.ts AND NEW.ts + 900
       AND COALESCE(starting, ending - 900) <= NEW.ts
       AND distinct_price_count IS NOT NULL;
END;

CREATE TABLE IF NOT EXISTS reserves (
    pool_address TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    q_up         INTEGER NOT NULL,
    q_down       INTEGER NOT NULL,
    source       TEXT    NOT NULL,
    symbol       TEXT,
    round_ending INTEGER,
    code_version TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS reserves_by_round ON reserves (symbol, round_ending, ts);
CREATE INDEX IF NOT EXISTS reserves_by_pool ON reserves (LOWER(pool_address));

CREATE TABLE IF NOT EXISTS delta_flips (
    symbol        TEXT    NOT NULL,
    round_ending  INTEGER NOT NULL,
    ts            INTEGER NOT NULL,
    to_side       TEXT    NOT NULL,
    delta_pct     REAL    NOT NULL,
    held_seconds  INTEGER,
    code_version  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS flips_by_round ON delta_flips (symbol, round_ending, ts);

CREATE TABLE IF NOT EXISTS reconcile_log (
    ts            INTEGER NOT NULL,
    pool_address  TEXT    NOT NULL,
    symbol        TEXT,
    round_ending  INTEGER,
    local_q_up    INTEGER,
    local_q_down  INTEGER,
    remote_q_up   INTEGER,
    remote_q_down INTEGER,
    code_version  TEXT    NOT NULL
);

-- What the pool contract itself says a one dollar ticket buys, recorded as it is asked.
-- The exchange's indexer reports nothing at all for a Round that has already been traded,
-- so Reserves derived from it silently fall back to an untouched pool and price every Fill
-- at the best price there is. The contract has no such gap: it answers for the pool as it
-- actually stands. Scoring still reads only this database (ADR-0002); what changes is that
-- the price it reads is evidence rather than a model run on absent inputs.
CREATE TABLE IF NOT EXISTS chain_quotes (
    symbol       TEXT    NOT NULL,
    round_ending INTEGER NOT NULL,
    side         TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    gross        INTEGER NOT NULL,
    shares       INTEGER NOT NULL,
    fees         INTEGER NOT NULL,
    -- The Side's marginal price, which is what a Strategy reading the pool reacts to.
    -- Nullable: the quote is the part a Fill needs, and a missed price must not cost it.
    price        REAL,
    code_version TEXT    NOT NULL,
    PRIMARY KEY (symbol, round_ending, side, ts)
);

CREATE TABLE IF NOT EXISTS quote_checks (
    ts             INTEGER NOT NULL,
    pool_address   TEXT    NOT NULL,
    gross          INTEGER NOT NULL,
    local_shares   INTEGER,
    chain_shares   INTEGER,
    local_fees     INTEGER,
    chain_fees     INTEGER,
    agrees         INTEGER,
    note           TEXT,
    code_version   TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS collector_gaps (
    started_at   INTEGER NOT NULL,
    note         TEXT,
    code_version TEXT NOT NULL
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    # An in-memory database has no write-ahead log to keep; everything else does.
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialise(conn: sqlite3.Connection) -> None:
    upgrading = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reconstruction_pending'"
    ).fetchone() is None
    try:
        # Schema and catch-up must commit together: a restart halfway through an upgrade
        # must not mistake the new bookkeeping table for a completed migration.
        conn.executescript("BEGIN;\n" + SCHEMA)
        # CREATE TABLE IF NOT EXISTS leaves an existing table exactly as it was, so a
        # column added to the schema never reaches a database that predates it. Anything
        # reading the new column then fails against the old table — including the
        # dashboard, which cannot repair what it mounts read-only.
        for table, column, kind in (("chain_quotes", "price", "REAL"),):
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone():
                continue
            if column not in {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        if upgrading:
            conn.execute(
                "INSERT INTO reconstruction_pending SELECT symbol, MIN(ts), MAX(ts) "
                "FROM oracle_prices GROUP BY symbol"
            )
            conn.execute("UPDATE rounds SET distinct_price_count = NULL WHERE partial = 1")
            conn.execute(
                """UPDATE reserves SET
                       symbol = (SELECT r.symbol FROM rounds r
                                  WHERE LOWER(r.pool_address) = LOWER(reserves.pool_address)),
                       round_ending = (SELECT r.ending FROM rounds r
                                        WHERE LOWER(r.pool_address) = LOWER(reserves.pool_address))
                     WHERE (symbol IS NULL OR round_ending IS NULL)
                       AND EXISTS (SELECT 1 FROM rounds r
                                    WHERE LOWER(r.pool_address) = LOWER(reserves.pool_address))"""
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def connect_readonly(path: str) -> sqlite3.Connection:
    """Open existing recordings without permission to create or modify a database."""
    uri = Path(path).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn
