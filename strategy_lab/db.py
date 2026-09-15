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
    conn.executescript(SCHEMA)
    conn.commit()
