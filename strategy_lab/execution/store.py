"""Separate durable execution ledger; raw Collector recordings stay read-only."""
import json
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS positions (
    id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    ending INTEGER NOT NULL,
    pool TEXT NOT NULL,
    outcome TEXT NOT NULL,
    side TEXT NOT NULL,
    strategy TEXT NOT NULL,
    amount INTEGER NOT NULL,
    minimum_shares INTEGER NOT NULL,
    quoted_shares INTEGER NOT NULL,
    state TEXT NOT NULL,
    shares INTEGER NOT NULL DEFAULT 0,
    cost INTEGER NOT NULL DEFAULT 0,
    payout INTEGER NOT NULL DEFAULT 0,
    buy_ref TEXT,
    redeem_ref TEXT,
    winner TEXT,
    error TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (symbol, ending)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    position_id TEXT NOT NULL,
    ts INTEGER NOT NULL,
    state TEXT NOT NULL,
    note TEXT
);
CREATE TABLE IF NOT EXISTS paper_receipts (
    position_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    success INTEGER NOT NULL,
    shares INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    reference TEXT NOT NULL,
    PRIMARY KEY (position_id, operation)
);
"""

TERMINAL = ("REDEEMED", "LOST", "BUY_REJECTED", "EXPIRED")


class Ledger:
    def __init__(self, path, settings):
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        if self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='oracle_prices'"
        ).fetchone():
            self.conn.close()
            raise ValueError("refusing to add execution tables to Collector recordings")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA)
        # Initial cash and execution mode cannot silently change after a restart. Limits
        # and enabled may change; each position keeps its own strategy and stake.
        payload = json.dumps({"mode": settings.mode, "bankroll": settings.bankroll,
                              "wallet_label": settings.wallet_label, "symbols": settings.symbols},
                             sort_keys=True)
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO settings VALUES (1, ?)", (payload,))
        if self.conn.execute("SELECT payload FROM settings").fetchone()[0] != payload:
            self.conn.close()
            raise ValueError("ledger wallet, Symbol, mode or bankroll differs; use a separate ledger")

    def close(self):
        self.conn.close()

    def get(self, identity):
        row = self.conn.execute("SELECT * FROM positions WHERE id = ?", (identity,)).fetchone()
        return dict(row) if row else None

    def positions(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM positions ORDER BY created_at, id")]

    def active(self):
        return [p for p in self.positions() if p["state"] not in TERMINAL]

    def entry_active(self):
        # Unknown API responses keep cash/exposure reserved but do not occupy the
        # normal entry slot. Late-confirmed purchases become active positions again.
        return [p for p in self.active() if p["state"] != "BUY_UNKNOWN"]

    def transition(self, identity, expected, state, now, **fields):
        allowed = {"shares", "cost", "payout", "buy_ref", "redeem_ref", "winner", "error"}
        if not set(fields) <= allowed:
            raise ValueError("unknown position fields")
        assignments = ", ".join(f"{key} = ?" for key in fields)
        if assignments:
            assignments = ", " + assignments
        with self.conn:
            changed = self.conn.execute(
                "UPDATE positions SET state = ?, updated_at = ?" + assignments
                + " WHERE id = ? AND state = ?",
                (state, now, *fields.values(), identity, expected),
            ).rowcount
            if changed:
                self.conn.execute("INSERT INTO events(position_id,ts,state,note) VALUES (?,?,?,?)",
                                  (identity, now, state, fields.get("error")))
        return bool(changed)

    def reserve(self, intent, settings, now):
        """Reserve cash and a Round atomically, including across competing processes."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            all_positions = self.positions()
            if any(p["symbol"] == intent["symbol"] and p["ending"] == intent["ending"]
                   for p in all_positions):
                return "already recorded"
            active = [p for p in all_positions if p["state"] not in TERMINAL]
            reserved = sum(p["amount"] for p in active if p["cost"] == 0)
            cash = settings.bankroll + sum(p["payout"] - p["cost"] for p in all_positions) - reserved
            # Daily limits reset at UTC midnight. Unconfirmed submissions consume budget too.
            day = now // 86400 * 86400
            spend = sum(p["amount"] for p in all_positions if p["created_at"] >= day
                        and p["state"] not in ("BUY_REJECTED", "EXPIRED"))
            session = None
            if settings.mode == "live" and self.conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='overnight_session'"
            ).fetchone() is not None:
                session = self.conn.execute(
                    "SELECT baseline_ids FROM overnight_session WHERE id=1"
                ).fetchone()
            timed_live = session is not None
            if timed_live:
                baseline = set(json.loads(session["baseline_ids"]))
                loss_positions = [p for p in all_positions if p["id"] not in baseline]
            else:
                loss_positions = [p for p in all_positions if p["updated_at"] >= day]
            loss = sum(max(0, p["cost"] - p["payout"]) for p in loss_positions
                       if p["state"] in TERMINAL)
            if len([p for p in active if p["state"] != "BUY_UNKNOWN"]) >= settings.max_open_positions:
                return "open-position limit"
            if sum(p["amount"] for p in active) + intent["amount"] > settings.max_exposure:
                return "exposure limit"
            if cash < intent["amount"]:
                return "insufficient paper cash"
            # A persisted timed LIVE session recycles confirmed proceeds. Keep the
            # cash/exposure/loss guards; paper and untimed trials retain daily spend.
            if not timed_live and spend + intent["amount"] > settings.daily_spend:
                return "daily spend limit"
            if loss >= settings.daily_loss:
                return "daily loss limit"
            columns = tuple(intent) + ("state", "created_at", "updated_at")
            self.conn.execute(
                f"INSERT INTO positions ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                (*intent.values(), "BUY_READY", now, now),
            )
            self.conn.execute("INSERT INTO events(position_id,ts,state) VALUES (?,?,'BUY_READY')",
                              (intent["id"], now))
            self.conn.commit()
            return "reserved"
        finally:
            if self.conn.in_transaction:
                self.conn.rollback()

    def summary(self, settings):
        positions = self.positions()
        return {
            "mode": settings.mode,
            "cash_usd": (settings.bankroll + sum(p["payout"] - p["cost"] for p in positions)) / 1e6,
            "reserved_usd": sum(p["amount"] for p in self.active() if not p["cost"]) / 1e6,
            "positions": positions,
        }
