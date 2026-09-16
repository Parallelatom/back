"""Local receipts only. No RPC calls, account secrets, signing, or broadcasting."""
from dataclasses import dataclass
from typing import Optional, Protocol

from ..amm import fill


@dataclass(frozen=True)
class Quote:
    shares: int
    observed_at: int


@dataclass(frozen=True)
class Receipt:
    success: bool
    shares: int
    amount: int
    reference: str


class Broker(Protocol):
    """Adapters must supply verified receipts, not merely HTTP success or a hash.

    Submission uses a stable position id. A missing receipt after submission is UNKNOWN,
    never permission to resend. Real adapters must bind receipts to chain, pool, wallet,
    outcome and confirmed transfers, and reconcile nonces/reorgs before exposing a result.
    """
    simulated: bool

    def quote(self, snapshot, side: str, amount: int) -> Quote: ...
    def submit_buy(self, position, snapshot) -> None: ...
    def lookup_buy(self, position) -> Optional[Receipt]: ...
    def submit_redeem(self, position) -> None: ...
    def lookup_redeem(self, position) -> Optional[Receipt]: ...


class PaperBroker:
    simulated = True

    def __init__(self, ledger):
        self.ledger = ledger

    def quote(self, snapshot, side, amount):
        ts, reserves = snapshot.record.reserves[-1]
        return Quote(fill(reserves, side, amount).shares, ts)

    def submit_buy(self, position, snapshot):
        quote = self.quote(snapshot, position["side"], position["amount"])
        success = quote.shares >= position["minimum_shares"]
        self._save(position, "buy", Receipt(success, quote.shares if success else 0,
                                            position["amount"] if success else 0,
                                            "paper:buy:" + position["id"]))

    def submit_redeem(self, position):
        if position["winner"] != position["side"]:
            raise ValueError("cannot redeem a losing or unresolved position")
        self._save(position, "redeem", Receipt(True, position["shares"], position["shares"],
                                               "paper:redeem:" + position["id"]))

    def _save(self, position, operation, receipt):
        with self.ledger.conn:
            self.ledger.conn.execute(
                "INSERT OR IGNORE INTO paper_receipts VALUES (?,?,?,?,?,?)",
                (position["id"], operation, int(receipt.success), receipt.shares,
                 receipt.amount, receipt.reference),
            )

    def _lookup(self, position, operation):
        row = self.ledger.conn.execute(
            "SELECT * FROM paper_receipts WHERE position_id = ? AND operation = ?",
            (position["id"], operation),
        ).fetchone()
        return Receipt(bool(row["success"]), row["shares"], row["amount"], row["reference"]) if row else None

    def lookup_buy(self, position):
        return self._lookup(position, "buy")

    def lookup_redeem(self, position):
        return self._lookup(position, "redeem")
