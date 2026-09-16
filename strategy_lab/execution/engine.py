"""Durable buy/settlement/redeem orchestration for paper and explicit live runners.

The pending state is committed BEFORE calling an adapter. Ambiguous acknowledgements are
reconciled by receipt lookup; they never authorize an automatic second submission.
"""
from dataclasses import replace
from hashlib import sha256
import time

from .paper import Quote, Receipt
from .signals import decide
from .errors import NotSubmitted


class Executor:
    def __init__(self, settings, ledger, broker):
        if (settings.mode == "paper") != (broker.simulated is True):
            raise ValueError("real-money broker and execution mode differ")
        self.settings, self.ledger, self.broker = settings, ledger, broker

    def enter(self, snapshot, now, halted=False):
        if not self.settings.enabled or halted:
            return "new entries disabled"
        snapshot = replace(snapshot, record=replace(
            snapshot.record,
            prices=sorted((ts, p) for ts, p in snapshot.record.prices if ts <= now),
            reserves=sorted(((ts, r) for ts, r in snapshot.record.reserves if ts <= now),
                            key=lambda v: v[0]),
        ))
        entry, reason = decide(snapshot, self.settings, now)
        if entry is None:
            return reason
        identity = sha256(
            f"{self.settings.mode}:{self.settings.wallet_label}:{snapshot.record.symbol}:{snapshot.record.ending}".encode()
        ).hexdigest()
        if self.ledger.get(identity):
            return "already recorded"
        try:
            quote = self.broker.quote(snapshot, entry.side, self.settings.stake)
        except Exception as exc:
            return f"quote unavailable ({type(exc).__name__})"
        if self.settings.mode == "live":
            now = int(time.time())
            if now - entry.at > self.settings.max_signal_age_seconds:
                return "signal expired while fetching live quote"
            if (now - snapshot.metadata_at > self.settings.max_age_seconds
                    or now - snapshot.record.prices[-1][0] > self.settings.max_age_seconds):
                return "market observations expired while fetching live quote"
        if (not isinstance(quote, Quote) or type(quote.shares) is not int or quote.shares <= 0
                or not 0 <= now - quote.observed_at <= self.settings.max_age_seconds):
            return "invalid or stale quote"
        floor = getattr(self.settings, "min_quote_shares_micro", 0) if self.settings.mode == "live" else 0
        if floor and quote.shares <= floor:
            return f"quote shares {quote.shares / 1e6:.6f} must be > {floor / 1e6:.6f} per 1 USDC"
        intent = {
            "id": identity, "symbol": snapshot.record.symbol, "ending": snapshot.record.ending,
            "pool": snapshot.pool, "outcome": snapshot.outcome_up if entry.side == "UP" else snapshot.outcome_down,
            "side": entry.side, "strategy": self.settings.strategy, "amount": self.settings.stake,
            "minimum_shares": max(floor + 1 if floor else 0,
                                  (quote.shares * (10_000 - self.settings.slippage_bps) + 9_999) // 10_000),
            "quoted_shares": quote.shares,
        }
        reason = self.ledger.reserve(intent, self.settings, now)
        if reason != "reserved":
            return reason
        if self.ledger.transition(identity, "BUY_READY", "BUY_PENDING", now):
            try:
                self.broker.submit_buy(self.ledger.get(identity), snapshot)
            except NotSubmitted:
                self.ledger.transition(identity, "BUY_PENDING", "EXPIRED", int(time.time()),
                                       error="buy cancelled before submission; no API request sent")
            except Exception as exc:
                self._unknown(identity, "BUY_PENDING", now, exc)
        return self.ledger.get(identity)["state"]

    def advance(self, now, settlement):
        """Reconcile existing positions even when entries are disabled or halted."""
        for position in self.ledger.active():
            identity = position["id"]
            if position["state"] == "BUY_READY":
                # No call was attempted. Do not buy later on a historical signal.
                self.ledger.transition(identity, "BUY_READY", "EXPIRED", now,
                                       error="unsubmitted intent recovered; wait for a new Round")
                continue
            if position["state"] == "BUY_PENDING":
                try:
                    receipt = self.broker.lookup_buy(position)
                except Exception as exc:
                    self._unknown(identity, "BUY_PENDING", now, exc)
                    continue
                if receipt is None:
                    continue
                if not self._valid_receipt(receipt, "buy", position):
                    self._unknown(identity, "BUY_PENDING", now, ValueError())
                    continue
                state = "OPEN" if receipt.success else "BUY_REJECTED"
                self.ledger.transition(identity, "BUY_PENDING", state, now,
                                       shares=receipt.shares, cost=receipt.amount,
                                       buy_ref=receipt.reference, error=None)
                position = self.ledger.get(identity)
            if position["state"] == "OPEN" and now >= position["ending"]:
                try:
                    winner = settlement(position, now)
                except Exception as exc:
                    self._unknown(identity, "OPEN", now, exc)
                    continue
                if winner not in ("UP", "DOWN"):
                    continue
                state = "REDEEM_READY" if winner == position["side"] else "LOST"
                self.ledger.transition(identity, "OPEN", state, now, winner=winner, error=None)
                position = self.ledger.get(identity)
            if position["state"] == "REDEEM_READY":
                if self.ledger.transition(identity, "REDEEM_READY", "REDEEM_PENDING", now):
                    try:
                        self.broker.submit_redeem(self.ledger.get(identity))
                    except Exception as exc:
                        self._unknown(identity, "REDEEM_PENDING", now, exc)
                position = self.ledger.get(identity)
            if position["state"] == "REDEEM_PENDING":
                try:
                    receipt = self.broker.lookup_redeem(position)
                except Exception as exc:
                    self._unknown(identity, "REDEEM_PENDING", now, exc)
                    continue
                if receipt is None:
                    continue
                if not self._valid_receipt(receipt, "redeem", position):
                    self._unknown(identity, "REDEEM_PENDING", now, ValueError())
                    continue
                self.ledger.transition(identity, "REDEEM_PENDING",
                                       "REDEEMED" if receipt.success else "REDEEM_FAILED", now,
                                       payout=receipt.amount, redeem_ref=receipt.reference,
                                       error=None if receipt.success else "redemption failed; manual review required")

    def _unknown(self, identity, state, now, exc):
        # Exception text from a transport could contain credentials. Keep only its
        # type, and avoid an endless audit row every time the same unresolved call is read.
        error = f"unconfirmed {state.lower()} ({type(exc).__name__}); reconcile before any resend"
        if self.ledger.get(identity)["error"] != error:
            self.ledger.transition(identity, state, state, now, error=error)

    def _valid_receipt(self, receipt, operation, position):
        if self.settings.mode == "live":
            return self.broker.valid_receipt(receipt, operation, position)
        if (not isinstance(receipt, Receipt) or type(receipt.success) is not bool or type(receipt.shares) is not int
                or type(receipt.amount) is not int or receipt.shares < 0 or receipt.amount < 0
                or receipt.reference != f"paper:{operation}:{position['id']}"):
            return False
        if not receipt.success:
            return receipt.amount == receipt.shares == 0
        if operation == "buy":
            return receipt.amount == position["amount"] and receipt.shares >= position["minimum_shares"]
        return receipt.shares == position["shares"] and receipt.amount == position["shares"]
