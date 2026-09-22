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
    if position["state"] == "REDEEM_PENDING":
        # This one stops everything: a position that is neither a confirmed open Round
        # nor finished holds the entry slot, so the runner buys nothing until it clears.
        reason = f" ({position['error']})" if position.get("error") else ""
        return ("claim submitted but not confirmed" + reason + " — BLOCKS ALL NEW ENTRIES."
                " Clear it with --retry-claim, which rebroadcasts the transaction already"
                " signed rather than signing another")
    if position["state"] == "REDEEM_READY":
        return "claim not yet submitted — the runner will send it on its next pass"
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


def break_even(shares_micro):
    """The Hit Rate this fill must beat. A win pays the shares less the stake; a loss
    costs the stake whole, so the price paid per share is the whole economics."""
    shares = shares_micro / 1e6
    return None if shares <= 0 else 1 / shares


def fills(ledger_path):
    """Every confirmed fill against what was quoted for it.

    A fill below the quoted minimum halts the runner, and this is how to judge whether
    that halt should be lifted: not by how far the fill missed, but by what Hit Rate the
    shares it bought would have to beat.
    """
    conn = read_only(ledger_path)
    try:
        lines = ["halt flag:"]
        flags = conn.execute("SELECT name, value FROM live_flags").fetchall()
        lines += [f"  {r['name']}: {r['value']}" for r in flags] or ["  none"]
        lines.append("\nfills below the quoted minimum:")
        short = conn.execute(
            "SELECT id, state, quoted_shares, minimum_shares, shares FROM positions"
            " WHERE shares > 0 AND shares < minimum_shares ORDER BY updated_at").fetchall()
        for row in short:
            quoted, got = row["quoted_shares"] / 1e6, row["shares"] / 1e6
            lines.append(
                f"  {row['id'][:12]} {row['state']:9} quoted {quoted:.6f} "
                f"| got {got:.6f} | {(got / quoted - 1) * 100:+.2f}% vs quote "
                f"| needs {break_even(row['shares']):.1%} to break even")
        if not short:
            lines.append("  none")
        lines.append("\nevery confirmed fill:")
        for row in conn.execute("SELECT shares, COUNT(*) n FROM positions WHERE shares > 0"
                                " GROUP BY shares ORDER BY shares"):
            lines.append(f"  {row['shares'] / 1e6:.6f} shares/USDC  x{row['n']}"
                         f"  (needs {break_even(row['shares']):.1%})")
        return "\n".join(lines)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--fills", action="store_true",
                        help="report fill quality and the halt flag instead")
    args = parser.parse_args()
    print(fills(args.ledger) if args.fills else report(args.ledger))


if __name__ == "__main__":
    main()
