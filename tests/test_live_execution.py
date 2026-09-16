"""No real network: real signing with a throwaway test key, fake Accounts and RPC."""
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import requests
from eth_abi import encode
from eth_account import Account
from eth_utils import keccak

from strategy_lab.amm import Reserves
from strategy_lab.replay import RoundRecord
from strategy_lab.execution.accounts import CLAIMANT, USDC, ZERO, TRANSFER
from strategy_lab.execution.engine import Executor
from strategy_lab.execution.live import LiveBroker, settings_from_profile
from strategy_lab.execution.signals import Snapshot
from strategy_lab.execution.store import Ledger

POOL = "0x" + "22" * 20
SHARE = "0x" + "33" * 20
BUY_HASH = "0x" + "44" * 32
UP, DOWN = "0x1234567890abcdef", "0xfedcba0987654321"
START, NOW, END = 1789443000, 1789443600, 1789443900


def transfer(token, sender, recipient, amount, tx):
    return {"address": token, "transactionHash": tx, "removed": False,
            "topics": [TRANSFER, "0x" + sender[2:].rjust(64, "0"), "0x" + recipient[2:].rjust(64, "0")],
            "data": "0x" + format(amount, "064x")}


def receipt(wallet, tx, operation, shares=1314422):
    if operation == "buy":
        logs = [transfer(USDC, wallet, POOL, 1000000, tx), transfer(SHARE, ZERO, wallet, shares, tx),
                {"address": POOL, "transactionHash": tx, "topics": [
                    "0x" + keccak(text="SharesMinted(bytes8,uint256,address,address,uint256)").hex(),
                    UP.ljust(66, "0"), "0x" + format(shares, "064x"), "0x" + POOL[2:].rjust(64, "0")],
                 "data": "0x" + encode(("address", "uint256"), (wallet, 983000)).hex()}]
    else:
        logs = [transfer(SHARE, wallet, ZERO, shares, tx), transfer(USDC, POOL, wallet, shares, tx),
                {"address": POOL, "transactionHash": tx, "topics": ["0xother"]}]
    return {"transactionHash": tx, "blockNumber": "0x100", "status": "0x1", "from": wallet,
            "_canonical_timestamp": NOW if operation == "buy" else END,
            "to": CLAIMANT, "gasUsed": "0x100", "effectiveGasPrice": "0x10", "logs": logs}


class FakeRPC:
    def __init__(self):
        self.receipts = {}
        self.winner = bytes(8)
        self.shares = 0
        self.broadcasts = []
        self.lose_ack = False
        self.pending_nonce = 0
        self.gas_price = 10000000

    def view(self, target, signature, inputs=(), values=(), outputs=(), block="latest"):
        if signature == "balanceOf(address)": return (10000000 if target == USDC else self.shares,)
        if signature == "shareAddr(bytes8)": return (SHARE,)
        if signature == "timeEnding()": return (END,)
        if signature == "isDppm()": return (False,)
        if signature == "outcomeList()": return ((bytes.fromhex(UP[2:]), bytes.fromhex(DOWN[2:])),)
        if signature == "quoteC0E17FC7(bytes8,uint256)": return (1314422, 17000, 0)
        if signature == "details(bytes8)": return (0, 0, 0, self.winner)
        if signature == "decimals()": return (6,)
        raise AssertionError(signature)

    def call(self, method, params):
        if method == "eth_chainId": return "0xa4b1"
        if method == "eth_getCode": return "0x" if params[0] not in (USDC, CLAIMANT) else "0x1234"
        if method == "eth_getBalance": return hex(10**17)
        if method in ("eth_call", "eth_estimateGas"):
            assert "chainId" not in params[0], "integer signing fields must not leak into RPC calls"
            assert params[0]["value"] == "0x0"
            return "0x" + encode(("uint256[]",), ([1314422],)).hex() if method == "eth_call" else hex(200000)
        if method == "eth_gasPrice": return hex(self.gas_price)
        if method == "eth_getTransactionCount": return hex(self.pending_nonce if params[1] == "pending" else 0)
        if method == "eth_sendRawTransaction":
            self.broadcasts.append(params[0])
            if self.lose_ack: raise ValueError("lost broadcast acknowledgement")
            return "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
        raise AssertionError(method)

    def finalized(self, tx):
        return self.receipts.get(tx)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    # Known test-only key; never fund this wallet.
    account = Account.from_key(bytes.fromhex("01" * 32))
    profile = {"address": account.address, "authorization_env": "NINELIVES_BTC_AUTHORIZATION",
               "enabled": True, "accept_unprotected_slippage": True, "max_trades": 1}
    monkeypatch.setenv("NINELIVES_BTC_AUTHORIZATION", "test-only-authorization")
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: NOW)
    posts = []
    def post(url, **kwargs):
        posts.append((url, kwargs))
        return SimpleNamespace(status_code=200, json=lambda: {"data": {"ninelivesMint": BUY_HASH}})
    monkeypatch.setattr("strategy_lab.execution.live.requests.post", post)
    settings = settings_from_profile(profile, "BTC", account.address.lower())
    path = str(tmp_path / "live.db")
    ledger = Ledger(path, settings)
    rpc = FakeRPC()
    broker = LiveBroker(ledger, profile, "BTC", rpc, account)
    record = RoundRecord("BTC", START, END, 100., "",
                         [(t, 100. if t < NOW else 100.2) for t in range(START, NOW + 1, 5)],
                         [(NOW, Reserves.opening())])
    snapshot = Snapshot(record, POOL, UP, DOWN, NOW, "graphql")
    yield SimpleNamespace(account=account, profile=profile, settings=settings, ledger=ledger,
          broker=broker, rpc=rpc, snapshot=snapshot, posts=posts, path=path)
    ledger.close()


def buy(rig):
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    assert engine.enter(rig.snapshot, NOW) == "BUY_PENDING"
    position = rig.ledger.positions()[0]
    rig.rpc.receipts[BUY_HASH] = receipt(rig.broker.wallet, BUY_HASH, "buy")
    engine.advance(NOW, rig.broker.settlement)
    rig.rpc.shares = 1314422
    assert rig.ledger.get(position["id"])["state"] == "OPEN"
    return engine, position["id"]


def test_full_live_loop_signs_persists_then_reconciles_after_restart(rig):
    engine, identity = buy(rig)
    assert rig.posts[0][1]["headers"]["Authorization"] == "test-only-authorization"
    assert rig.posts[0][1]["allow_redirects"] is False
    assert rig.ledger.summary(rig.settings)["cash_usd"] == 9
    rig.rpc.winner = bytes.fromhex(UP[2:])
    engine.advance(END, rig.broker.settlement)
    p = rig.ledger.get(identity)
    op = rig.broker.op(p, "redeem")
    assert p["state"] == "REDEEM_PENDING"
    assert rig.rpc.broadcasts == [op["raw_tx"]]
    assert Account.recover_transaction(op["raw_tx"]).lower() == rig.broker.wallet
    assert "test-only-authorization" not in json.dumps(op)
    rig.rpc.receipts[op["tx_hash"]] = receipt(rig.broker.wallet, op["tx_hash"], "claim")
    other = Ledger(rig.path, rig.settings)
    try:
        b = LiveBroker(other, rig.profile, "BTC", rig.rpc, rig.account)
        restarted = Executor(rig.settings, other, b)
        restarted.advance(END + 1, b.settlement)
        restarted.advance(END + 2, b.settlement)
        assert other.get(identity)["state"] == "REDEEMED"
        assert other.summary(rig.settings)["cash_usd"] == 10.314422
        assert len(rig.rpc.broadcasts) == len(rig.posts) == 1
    finally:
        other.close()


def test_unknown_api_ack_never_reposts_and_can_attach_verified_hash(rig, monkeypatch):
    calls = []
    def fail(*a, **k):
        calls.append(1)
        raise requests.Timeout("sensitive credential")
    monkeypatch.setattr("strategy_lab.execution.live.requests.post", fail)
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    engine.enter(rig.snapshot, NOW)
    p = rig.ledger.positions()[0]
    for _ in range(3):
        engine.enter(rig.snapshot, NOW)
        engine.advance(NOW, rig.broker.settlement)
    assert len(calls) == 1 and rig.ledger.get(p["id"])["state"] == "BUY_PENDING"
    assert "sensitive" not in rig.ledger.get(p["id"])["error"]
    rig.rpc.receipts[BUY_HASH] = receipt(rig.broker.wallet, BUY_HASH, "buy")
    rig.broker.attach_buy(p["id"], BUY_HASH)
    engine.advance(NOW, rig.broker.settlement)
    assert rig.ledger.get(p["id"])["state"] == "OPEN"


def test_claim_timeout_does_not_sign_again_and_explicit_retry_uses_same_bytes(rig):
    engine, identity = buy(rig)
    rig.rpc.winner = bytes.fromhex(UP[2:])
    rig.rpc.lose_ack = True
    engine.advance(END, rig.broker.settlement)
    engine.advance(END + 1, rig.broker.settlement)
    assert len(rig.rpc.broadcasts) == 1
    rig.rpc.lose_ack = False
    rig.broker.retry_claim(identity)
    assert len(rig.rpc.broadcasts) == 2
    assert rig.rpc.broadcasts[0] == rig.rpc.broadcasts[1]


def test_losing_round_does_not_claim(rig):
    engine, identity = buy(rig)
    rig.rpc.winner = bytes.fromhex(DOWN[2:])
    engine.advance(END, rig.broker.settlement)
    assert rig.ledger.get(identity)["state"] == "LOST"
    assert rig.rpc.broadcasts == []


def test_below_quote_fill_is_accounted_and_halts_future_entries(rig):
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    engine.enter(rig.snapshot, NOW)
    rig.rpc.receipts[BUY_HASH] = receipt(rig.broker.wallet, BUY_HASH, "buy", 1000000)
    engine.advance(NOW, rig.broker.settlement)
    p = rig.ledger.positions()[0]
    assert p["state"] == "OPEN" and p["shares"] == 1000000
    assert rig.broker.entry_block().startswith("actual fill below")


@pytest.mark.parametrize("fault", ["nonce", "gas", "balance"])
def test_claim_prebroadcast_failures_never_send_and_can_be_retried(rig, fault):
    engine, identity = buy(rig)
    rig.rpc.winner = bytes.fromhex(UP[2:])
    if fault == "nonce": rig.rpc.pending_nonce = 1
    if fault == "gas": rig.rpc.gas_price = 10**15
    if fault == "balance": rig.rpc.shares = 0
    engine.advance(END, rig.broker.settlement)
    assert rig.rpc.broadcasts == []
    assert rig.broker.op(rig.ledger.get(identity), "redeem") is None
    rig.broker.retry_claim(identity)
    assert rig.ledger.get(identity)["state"] == "REDEEM_READY"


def test_wrong_signer_rejected(rig):
    with pytest.raises(ValueError, match="signing key"):
        LiveBroker(rig.ledger, rig.profile, "BTC", rig.rpc, Account.from_key(bytes.fromhex("02" * 32)))


@pytest.mark.parametrize("key,code", [("", "PRIVATE_KEY_MISSING"),
    ("sensitive-not-a-key", "PRIVATE_KEY_INVALID"), ("02" * 32, "PRIVATE_KEY_MISMATCH")])
def test_signer_diagnostics_are_specific_without_exposing_key(rig, monkeypatch, key, code):
    monkeypatch.setenv("NINELIVES_BTC_PRIVATE_KEY", key)
    with pytest.raises(ValueError, match=code) as err:
        LiveBroker(rig.ledger, rig.profile, "BTC", rig.rpc)
    assert not key or key not in str(err.value)


def test_missing_authorization_has_safe_diagnostic(rig, monkeypatch):
    monkeypatch.delenv("NINELIVES_BTC_AUTHORIZATION")
    with pytest.raises(ValueError, match="AUTHORIZATION_MISSING"):
        LiveBroker(rig.ledger, rig.profile, "BTC", rig.rpc, rig.account)


def test_no_final_receipt_never_debits_and_wrong_wallet_remains_pending(rig):
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    engine.enter(rig.snapshot, NOW)
    engine.advance(NOW, rig.broker.settlement)
    assert rig.ledger.summary(rig.settings)["cash_usd"] == 10
    rig.rpc.receipts[BUY_HASH] = receipt("0x" + "99" * 20, BUY_HASH, "buy")
    engine.advance(NOW, rig.broker.settlement)
    assert rig.ledger.positions()[0]["state"] == "BUY_PENDING"


def test_slippage_acknowledgement_required_before_api(rig):
    rig.profile["accept_unprotected_slippage"] = False
    reason = Executor(rig.settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    assert reason.startswith("quote unavailable") and not rig.posts


@pytest.mark.parametrize("source,observations", [
    ("graphql", [(START, Reserves.opening())]),
    ("seed", [(START, Reserves.opening())]), ("missing", []),
])
def test_live_delta_uses_chain_quote_when_reserve_record_is_old_or_missing(rig, monkeypatch, source, observations):
    snapshot = replace(rig.snapshot, reserve_source=source,
                       record=replace(rig.snapshot.record, reserves=observations))
    original = rig.rpc.view
    quoted = []
    def view(target, signature, *args, **kwargs):
        if signature == "quoteC0E17FC7(bytes8,uint256)":
            quoted.append(True)
            return (1200000, 17000, 0)
        return original(target, signature, *args, **kwargs)
    monkeypatch.setattr(rig.rpc, "view", view)
    assert Executor(rig.settings, rig.ledger, rig.broker).enter(snapshot, NOW) == "BUY_PENDING"
    assert quoted == [True] and len(rig.posts) == 1
    assert rig.ledger.positions()[0]["quoted_shares"] == 1200000


@pytest.mark.parametrize("kind", ["fails", "zero", "stale"])
def test_missing_reserves_never_falls_back_when_chain_quote_unusable(rig, monkeypatch, kind):
    from strategy_lab.execution.paper import Quote
    snapshot = replace(rig.snapshot, reserve_source="missing",
                       record=replace(rig.snapshot.record, reserves=[]))
    def quote(*args):
        if kind == "fails": raise ValueError("RPC failed")
        return Quote(0 if kind == "zero" else 1314422, NOW - 16 if kind == "stale" else NOW)
    monkeypatch.setattr(rig.broker, "quote", quote)
    Executor(rig.settings, rig.ledger, rig.broker).enter(snapshot, NOW)
    assert not rig.posts and not rig.ledger.positions()


@pytest.mark.parametrize("kind", ["stale-price", "partial", "stale-metadata", "expired-signal"])
def test_live_reserve_change_preserves_signal_guards(rig, kind):
    snapshot = replace(rig.snapshot, reserve_source="missing",
                       record=replace(rig.snapshot.record, reserves=[]))
    now = NOW
    if kind == "stale-price":
        snapshot = replace(snapshot, record=replace(snapshot.record, prices=snapshot.record.prices[:-10]))
    elif kind == "partial":
        snapshot = replace(snapshot, record=replace(snapshot.record, prices=snapshot.record.prices[70:]))
    elif kind == "stale-metadata":
        snapshot = replace(snapshot, metadata_at=START)
    else:
        now += 6
    Executor(rig.settings, rig.ledger, rig.broker).enter(snapshot, now)
    assert not rig.posts and not rig.ledger.positions()


def test_live_reserve_dependent_strategy_is_not_silently_enabled(rig):
    settings = SimpleNamespace(**{**rig.settings.__dict__, "strategy": "Lock Rider"})
    reason = Executor(settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    assert reason == "live signal supports Delta Edge only"
    assert not rig.posts


def test_wrong_outcome_event_is_not_accepted(rig):
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    engine.enter(rig.snapshot, NOW)
    r = receipt(rig.broker.wallet, BUY_HASH, "buy")
    r["logs"][-1]["topics"][1] = DOWN.ljust(66, "0")
    rig.rpc.receipts[BUY_HASH] = r
    engine.advance(NOW, rig.broker.settlement)
    assert rig.ledger.positions()[0]["state"] == "BUY_PENDING"


def test_old_receipt_cannot_be_attached_to_current_intent(rig, monkeypatch):
    def fail(*a, **k): raise requests.Timeout()
    monkeypatch.setattr("strategy_lab.execution.live.requests.post", fail)
    Executor(rig.settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    p = rig.ledger.positions()[0]
    r = receipt(rig.broker.wallet, BUY_HASH, "buy")
    r["_canonical_timestamp"] = NOW - 30
    rig.rpc.receipts[BUY_HASH] = r
    with pytest.raises(ValueError, match="predates"):
        rig.broker.attach_buy(p["id"], BUY_HASH)
    assert rig.broker.op(p, "buy")["tx_hash"] is None


def test_delayed_quote_expires_signal_without_sending(rig, monkeypatch):
    clock = [NOW]
    original = rig.broker.quote
    def delayed(*args):
        q = original(*args)
        clock[0] += 6
        return q
    monkeypatch.setattr(rig.broker, "quote", delayed)
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: clock[0])
    result = Executor(rig.settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    assert "signal expired" in result and rig.posts == []


def test_claim_signed_hash_is_already_durable_when_broadcast_called(rig, monkeypatch):
    engine, identity = buy(rig)
    rig.rpc.winner = bytes.fromhex(UP[2:])
    original = rig.rpc.call
    def call(method, params):
        if method == "eth_sendRawTransaction":
            independent = Ledger(rig.path, rig.settings)
            try:
                row = independent.conn.execute("SELECT tx_hash,raw_tx FROM live_ops WHERE operation='redeem'").fetchone()
                assert row["raw_tx"] == params[0]
                assert row["tx_hash"] == "0x" + keccak(bytes.fromhex(params[0][2:])).hex()
            finally:
                independent.close()
        return original(method, params)
    monkeypatch.setattr(rig.rpc, "call", call)
    engine.advance(END, rig.broker.settlement)
    assert rig.broker.op(rig.ledger.get(identity), "redeem") is not None


def test_default_cli_does_not_enter_or_advance(rig, monkeypatch, tmp_path, capsys):
    from strategy_lab.execution import run_live
    config = {"chain_id": 42161, "accounts_url": "https://arb-accounts.superposition.so/",
              "wallets": {"BTC": {**rig.profile, "stake_micro": 1000000, "budget_micro": 10000000},
                          "XYZCL": {"address": POOL, "authorization_env": "NINELIVES_XYZCL_AUTHORIZATION",
                                    "stake_micro": 1000000, "budget_micro": 10000000}}}
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(config))
    monkeypatch.setattr(run_live, "LiveBroker", lambda *a: rig.broker)
    monkeypatch.setattr(run_live, "Executor", lambda *a: pytest.fail("default CLI must not start an executor"))
    monkeypatch.setattr("sys.argv", ["run_live", "--config", str(path), "--symbol", "BTC",
        "--ledger", str(tmp_path / "other.db"), "--halt-file", str(tmp_path / "HALT"), "--log-format", "json"])
    run_live.main()
    output = capsys.readouterr().out
    assert '"execute": false' in output and "test-only-authorization" not in output
    assert not rig.posts and not rig.rpc.broadcasts
