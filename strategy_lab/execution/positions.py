"""Say what each open live position is actually waiting on. Reads; never writes.

A Round whose result the venue has not published yet is not a stuck record to be cleared.
The stake is in a real position that has not been decided, and marking it finished here
would invent a result and abandon a claim that is still owed. So this tool only reports,
and the one question it answers is which of the waits is the real one: the contract has
not named a winner, or the claim delay has not elapsed, or the purchase was never
confirmed. Each has a different remedy and only the last of them is a fault.
"""
from __future__ import annotations

import argparse
import sqlite3
import time

from .accounts import hex_value
from .errors import SetupError
from .live import RPC

# submit_redeem refuses before this has elapsed, so a Round that ended moments ago is
# waiting on the clock rather than on the venue.
CLAIM_DELAY_SECONDS = 300
TERMINAL = ("REDEEMED", "LOST", "BUY_REJECTED", "EXPIRED")


def read_only(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def winner_on_chain(rpc, position, buy):
    """What the pool says won, or None while it has named nobody."""
    if not buy:
        return None
    decided = rpc.view(position["pool"], "details(bytes8)", ("bytes8",),
                       (bytes.fromhex(hex_value(position["outcome"], 8)[2:]),),
                       ("uint256", "uint256", "uint256", "bytes8"), "latest")[3]
    decided = "0x" + decided.hex()
    if decided == buy["outcome_up"]:
        return "UP"
    if decided == buy["outcome_down"]:
        return "DOWN"
    return None


def waiting_on(position, buy, now, winner):
    if position["state"] in ("BUY_PENDING", "BUY_UNKNOWN"):
        return ("purchase not confirmed — reconcile with --attach-buy once the "
                "transaction is known")
    if position["ending"] > now:
        return "Round still running"
    remaining = position["ending"] + CLAIM_DELAY_SECONDS - now
    if remaining > 0:
        return f"claim delay: {remaining}s left before a claim is even allowed"
    if winner is None:
        return "venue has not resolved the Round on chain — nothing to do but wait"
    if winner != position["side"]:
        return "Round lost; the runner will record it on its next pass"
    return "won and claimable — the runner will claim it, or force one with --retry-claim"


def report(ledger_path, rpc=None, now=None):
    conn, rpc = read_only(ledger_path), rpc or RPC()
    now = int(time.time()) if now is None else now
    try:
        active = [dict(row) for row in conn.execute(
            "SELECT * FROM positions WHERE state NOT IN (?,?,?,?) ORDER BY ending",
            TERMINAL)]
        if not active:
            return "No open positions. Nothing is blocking a new entry."
        lines = [f"{len(active)} open position(s):"]
        for position in active:
            buy = conn.execute(
                "SELECT * FROM live_ops WHERE position_id = ? AND operation = 'buy'",
                (position["id"],)).fetchone()
            winner = None
            if position["state"] == "OPEN" and position["ending"] <= now:
                try:
                    winner = winner_on_chain(rpc, position, buy)
                except (SetupError, ValueError, KeyError, TypeError):
                    winner = None
            age = now - position["ending"]
            lines.append(
                f"  {position['id'][:12]} | {position['symbol']} {position['side']} "
                f"| {position['state']} | stake {position['amount'] / 1e6:.2f} USDC "
                f"| ended {age // 60}m{age % 60:02d}s ago"
                f"\n      waiting on: {waiting_on(position, buy, now, winner)}")
        return "\n".join(lines)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    args = parser.parse_args()
    print(report(args.ledger))


if __name__ == "__main__":
    main()
