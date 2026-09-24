"""No real network: real signing with a throwaway test key, fake Accounts and RPC."""
import json
from unittest.mock import patch
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
from strategy_lab.execution.signals import Snapshot, decide
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
        if method == "eth_blockNumber": return "0x100"
        if method == "eth_getBlockByNumber": return {"number": "0x100", "timestamp": hex(NOW)}
        if method == "eth_getLogs": return []
        # A hash with a receipt is certainly known to the chain; anything else never
        # reached it, which is what the runner asks about a purchase that will not confirm.
        if method == "eth_getTransactionByHash":
            return {"hash": params[0]} if params[0] in self.receipts else None
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

    def mined(self, tx):
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
    with patch("strategy_lab.execution.live.time.time", return_value=END + 300):
        engine.advance(END + 300, rig.broker.settlement)
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
    assert len(calls) == 1 and rig.ledger.get(p["id"])["state"] == "BUY_UNKNOWN"
    assert "sensitive" not in rig.ledger.get(p["id"])["error"]
    rig.rpc.receipts[BUY_HASH] = receipt(rig.broker.wallet, BUY_HASH, "buy")
    rig.broker.attach_buy(p["id"], BUY_HASH)
    engine.advance(NOW, rig.broker.settlement)
    assert rig.ledger.get(p["id"])["state"] == "OPEN"


def test_claim_timeout_does_not_sign_again_and_explicit_retry_uses_same_bytes(rig):
    engine, identity = buy(rig)
    rig.rpc.winner = bytes.fromhex(UP[2:])
    rig.rpc.lose_ack = True
    with patch("strategy_lab.execution.live.time.time", return_value=END + 300):
        engine.advance(END + 300, rig.broker.settlement)
    engine.advance(END + 1, rig.broker.settlement)
    assert len(rig.rpc.broadcasts) == 1
    rig.rpc.lose_ack = False
    rig.broker.retry_claim(identity)
    assert len(rig.rpc.broadcasts) == 2
    assert rig.rpc.broadcasts[0] == rig.rpc.broadcasts[1]


def test_losing_round_does_not_claim(rig):
    engine, identity = buy(rig)
    rig.rpc.winner = bytes.fromhex(DOWN[2:])
    with patch("strategy_lab.execution.live.time.time", return_value=END + 300):
        engine.advance(END + 300, rig.broker.settlement)
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
    with patch("strategy_lab.execution.live.time.time", return_value=END + 300):
        engine.advance(END + 300, rig.broker.settlement)
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
    # Naming the check that failed is the point: nine causes otherwise read alike.
    assert reason == ("quote refused: Accounts mint has no verified minimum output;"
                      " acknowledge in config")
    assert not rig.posts


def test_live_trade_window_can_open_at_nine_minutes_without_changing_paper(rig):
    early = END - 540
    record = replace(rig.snapshot.record,
                     prices=[(t, 100. if t < early else 100.2)
                             for t in range(START, early + 1, 5)])
    snapshot = replace(rig.snapshot, record=record, metadata_at=early)
    assert decide(snapshot, rig.settings, early)[0] is None
    rig.settings.trade_window_open_seconds = 540
    entry, reason = decide(snapshot, rig.settings, early)
    assert reason == "signal" and entry.at == early and entry.side == "UP"


@pytest.mark.parametrize("value", [True, 299, 841, 540.0])
def test_live_trade_window_rejects_unsafe_config(rig, value):
    rig.profile["trade_window_open_seconds"] = value
    with pytest.raises(ValueError, match="CONFIG_TRADE_WINDOW"):
        settings_from_profile(rig.profile, "BTC", rig.broker.wallet)


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
            return (1320000, 17000, 0)
        return original(target, signature, *args, **kwargs)
    monkeypatch.setattr(rig.rpc, "view", view)
    assert Executor(rig.settings, rig.ledger, rig.broker).enter(snapshot, NOW) == "BUY_PENDING"
    assert quoted == [True] and len(rig.posts) == 1
    assert rig.ledger.positions()[0]["quoted_shares"] == 1320000


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
        now += 16
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


@pytest.mark.parametrize("delay", [6, 15, 16])
def test_delayed_quote_respects_live_signal_budget(rig, monkeypatch, delay):
    clock = [NOW]
    original = rig.broker.quote
    def delayed(*args):
        q = original(*args)
        clock[0] += delay
        return q
    monkeypatch.setattr(rig.broker, "quote", delayed)
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: clock[0])
    result = Executor(rig.settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    if delay <= 15:
        assert result == "BUY_PENDING" and len(rig.posts) == 1
    else:
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
    with patch("strategy_lab.execution.live.time.time", return_value=END + 300):
        engine.advance(END + 300, rig.broker.settlement)
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


@pytest.mark.parametrize("shares,accepted", [(1299999, False), (1300000, False), (1300001, True)])
def test_share_threshold_is_strictly_greater_than_1_30(rig, monkeypatch, shares, accepted):
    from strategy_lab.execution.paper import Quote
    monkeypatch.setattr(rig.broker, "quote", lambda *a: Quote(shares, NOW))
    reason = Executor(rig.settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    assert bool(rig.posts) is accepted
    if accepted:
        assert rig.ledger.positions()[0]["minimum_shares"] >= 1300001
    else:
        assert "must be > 1.300000" in reason
        assert not rig.ledger.positions()


def add_closed(rig, index, now, lost=False):
    intent = {"id": f"test-{index}", "symbol": "BTC", "ending": END + index * 900,
              "pool": POOL, "outcome": UP, "side": "UP", "strategy": "Delta Edge", "amount": 1000000,
              "minimum_shares": 1300001, "quoted_shares": 1314422}
    assert rig.ledger.reserve(intent, rig.settings, now) == "reserved"
    rig.ledger.transition(intent["id"], "BUY_READY", "LOST" if lost else "REDEEMED", now,
                          cost=1000000, payout=0 if lost else 1314422, shares=1314422)


def test_overnight_counts_new_attempts_and_persists_across_restart(rig, monkeypatch):
    add_closed(rig, 0, NOW - 10)
    assert rig.broker.entry_block() == "configured lifetime trade count reached"
    first = rig.broker.start_overnight(8)
    assert first["attempts"] == 0 and rig.broker.entry_block() is None
    add_closed(rig, 1, NOW)
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: NOW + 3600)
    other = Ledger(rig.path, rig.settings)
    try:
        broker = LiveBroker(other, rig.profile, "BTC", rig.rpc, rig.account)
        resumed = broker.start_overnight(8)
        assert resumed["ends_at"] == first["ends_at"] and resumed["attempts"] == 1
        assert resumed["committed_micro"] == 1000000
        with pytest.raises(ValueError, match="OVERNIGHT_EXISTS"):
            broker.start_until(first["ends_at"] + 3600)
    finally:
        other.close()


def test_overnight_recycles_over_ten_buys_across_midnight(rig, monkeypatch):
    midnight = (NOW // 86400 + 1) * 86400
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: midnight - 3600)
    rig.broker.start_overnight(8)
    for i in range(12):
        at = midnight - 1 if i < 11 else midnight + 1
        monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: at)
        assert rig.broker.entry_block() is None
        add_closed(rig, i, at)
    assert rig.broker.overnight_status()["committed_micro"] == 12000000
    assert rig.broker.entry_block() is None
    assert rig.broker.overnight_status()["max_trades"] is None
    other = Ledger(rig.path, rig.settings)
    try:
        resumed = LiveBroker(other, rig.profile, "BTC", rig.rpc, rig.account)
        assert resumed.entry_block() is None
        assert resumed.overnight_status()["attempts"] == 12
    finally:
        other.close()


def test_untimed_live_retains_daily_spend_limit(rig):
    for i in range(10):
        add_closed(rig, i, NOW)
    with pytest.raises(AssertionError, match="daily spend limit"):
        add_closed(rig, 11, NOW)


def test_overnight_loss_stop_does_not_reset_at_midnight(rig, monkeypatch):
    midnight = (NOW // 86400 + 1) * 86400
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: midnight - 3600)
    rig.broker.start_overnight(8)
    add_closed(rig, 0, midnight - 1, lost=True)
    add_closed(rig, 1, midnight + 1, lost=True)
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: midnight + 1)
    assert rig.broker.entry_block() == "overnight loss limit reached"


def test_deadline_stops_entries_but_claims_existing_position(rig, monkeypatch):
    session = rig.broker.start_until(NOW + 3600)
    engine, identity = buy(rig)
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: session["ends_at"])
    assert rig.broker.entry_block() == "overnight entry deadline reached"
    rig.rpc.winner = bytes.fromhex(UP[2:])
    engine.advance(session["ends_at"], rig.broker.settlement)
    assert len(rig.rpc.broadcasts) == 1
    assert rig.ledger.get(identity)["state"] == "REDEEM_PENDING"
    assert rig.broker.start_until(session["ends_at"])["ends_at"] == session["ends_at"]


def test_deadline_during_presubmit_rpc_releases_unsent_reservation(rig, monkeypatch):
    clock = [NOW]
    monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: clock[0])
    rig.broker.start_until(NOW + 4)
    original = rig.broker.mapping
    calls = []
    def mapping(*args):
        calls.append(True)
        if len(calls) == 2: clock[0] = NOW + 4
        return original(*args)
    monkeypatch.setattr(rig.broker, "mapping", mapping)
    result = Executor(rig.settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    assert result == "EXPIRED" and not rig.posts
    assert rig.ledger.summary(rig.settings)["reserved_usd"] == 0


def test_compare_pairs_paper_and_live_without_calling_pending_a_loss(rig, monkeypatch):
    from strategy_lab.execution import compare
    from strategy_lab.replay import PaperTrade
    rig.broker.start_overnight(8)
    Executor(rig.settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    paper = PaperTrade(END, NOW, "UP", 1314422, .5, .76, True, .314422)
    monkeypatch.setattr(compare, "replay", lambda *a, **k: {"Delta Edge": SimpleNamespace(trades=[paper])})
    report = compare.compare(rig.ledger.conn, None, "BTC")
    assert report["rows"][0]["same_side"] is True
    assert report["rows"][0]["live_realized_pnl_usdc"] is None
    assert report["paper_pnl_usdc"] == .314422
    assert report["live_closed"] == 0


def test_compare_explicit_interval_includes_continuous_orders_only_in_that_interval(rig, monkeypatch):
    from strategy_lab.execution import compare
    from strategy_lab.replay import PaperTrade
    rig.broker.start_until(NOW + 3600)
    add_closed(rig, 0, NOW)
    add_closed(rig, 1, NOW + 3601)
    paper = PaperTrade(END + 900, NOW + 3601, "UP", 1314422, .5, .76, True, .314422)
    monkeypatch.setattr(compare, "replay", lambda *a, **k: {"Delta Edge": SimpleNamespace(trades=[paper])})
    report = compare.compare(rig.ledger.conn, None, "BTC", NOW + 3600, NOW + 7200)
    assert report["live_attempts"] == report["paper_trades"] == 1
    assert report["rows"][0]["live_entry_at"] == NOW + 3601


def test_claim_waits_five_minutes_and_retries_unresolved_winner(rig, monkeypatch):
    engine, identity = buy(rig)
    rig.rpc.winner = bytes.fromhex(UP[2:])
    monkeypatch.setattr('strategy_lab.execution.live.time.time', lambda: END + 299)
    engine.advance(END + 299, rig.broker.settlement)
    assert not rig.rpc.broadcasts
    rig.rpc.winner = bytes(8)
    monkeypatch.setattr('strategy_lab.execution.live.time.time', lambda: END + 300)
    engine.advance(END + 300, rig.broker.settlement)
    assert rig.ledger.get(identity)['state'] == 'OPEN'
    rig.rpc.winner = bytes.fromhex(UP[2:])
    engine.advance(END + 300, rig.broker.settlement)
    engine.advance(END + 301, rig.broker.settlement)
    assert len(rig.rpc.broadcasts) == 1


def test_claim_latest_receipt_unlocks_without_finality(rig, monkeypatch):
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    engine.enter(rig.snapshot, NOW)
    identity = rig.ledger.positions()[0]['id']
    mined = receipt(rig.broker.wallet, BUY_HASH, 'buy')
    rig.rpc.receipts[BUY_HASH] = mined
    rig.rpc.shares = 1314422
    original = rig.rpc.view
    def view(target, signature, inputs=(), values=(), outputs=(), block='latest'):
        if signature == 'details(bytes8)':
            return (0, 0, 0, bytes.fromhex(UP[2:]) if block == 'latest' else bytes(8))
        return original(target, signature, inputs, values, outputs, block)
    monkeypatch.setattr(rig.rpc, 'view', view)
    monkeypatch.setattr('strategy_lab.execution.live.time.time', lambda: END + 299)
    engine.advance(END + 299, rig.broker.settlement)
    assert rig.ledger.get(identity)['state'] == 'OPEN'
    monkeypatch.setattr('strategy_lab.execution.live.time.time', lambda: END + 300)
    engine.advance(END + 300, rig.broker.settlement)
    assert rig.ledger.get(identity)['state'] == 'REDEEM_PENDING'
    assert len(rig.rpc.broadcasts) == 1
    assert rig.ledger.get(identity)['payout'] == 0
    engine.advance(END + 301, rig.broker.settlement)
    assert len(rig.rpc.broadcasts) == 1

    claim = rig.broker.op(rig.ledger.get(identity), 'redeem')
    rig.rpc.receipts[claim['tx_hash']] = receipt(rig.broker.wallet, claim['tx_hash'], 'claim')
    engine.advance(END + 302, rig.broker.settlement)
    assert rig.ledger.get(identity)['state'] == 'REDEEMED'
    assert rig.ledger.get(identity)['payout'] == 1314422
    assert not rig.ledger.active()
    assert len(rig.rpc.broadcasts) == 1


@pytest.mark.parametrize('canonical', [True, False])
def test_mined_receipt_checks_canonical_block_without_finality(monkeypatch, canonical):
    from strategy_lab.execution.live import RPC
    rpc = RPC()
    def call(method, params):
        if method == 'eth_getTransactionReceipt':
            return {'blockNumber': '0x100', 'blockHash': '0xabc'}
        assert method == 'eth_getBlockByNumber' and params == ['0x100', False]
        return {'hash': '0xabc' if canonical else '0xdef', 'timestamp': hex(NOW)}
    monkeypatch.setattr(rpc, 'call', call)
    result = rpc.mined(BUY_HASH)
    if canonical:
        assert result['_canonical_timestamp'] == NOW
    else:
        assert result is None


def test_continuous_preserves_deadline_report_and_loss_across_restart(rig, monkeypatch):
    session = rig.broker.start_until(NOW + 3600)
    add_closed(rig, 0, NOW, lost=True)
    rig.broker.start_continuous()
    monkeypatch.setattr('strategy_lab.execution.live.time.time', lambda: NOW + 3601)
    assert rig.broker.entry_block() is None
    other = Ledger(rig.path, rig.settings)
    try:
        broker = LiveBroker(other, rig.profile, 'BTC', rig.rpc, rig.account)
        assert broker.entry_block() is None
        status = broker.start_continuous()
        assert status['ends_at'] == session['ends_at']
        assert status['loss_micro'] == 1000000
        with pytest.raises(ValueError, match='CONTINUOUS_ACTIVE'):
            broker.start_until(NOW + 3600)
        add_closed(rig, 1, NOW + 3601, lost=True)
        assert broker.entry_block() == 'overnight loss limit reached'
    finally:
        other.close()


def test_new_continuous_session_recycles_and_preserves_halt(rig):
    rig.broker.start_continuous()
    for i in range(12):
        assert rig.broker.entry_block() is None
        add_closed(rig, i, NOW)
    rig.broker.stop('manual review required')
    rig.broker.start_continuous()
    assert rig.broker.entry_block() == 'manual review required'


def test_risk_session_reset_aligns_entry_and_reservation_loss_guards(rig):
    rig.broker.start_continuous()
    add_closed(rig, 0, NOW, lost=True)
    add_closed(rig, 1, NOW + 1, lost=True)
    assert rig.broker.entry_block() == 'overnight loss limit reached'

    status = rig.broker.reset_risk_session()
    assert status['attempts'] == 0 and status['loss_micro'] == 0
    assert rig.broker.entry_block() is None
    add_closed(rig, 2, NOW + 2)
    assert rig.ledger.get('test-2')['state'] == 'REDEEMED'


def test_risk_session_reset_refuses_active_position(rig):
    rig.broker.start_continuous()
    intent = {'id': 'active', 'symbol': 'BTC', 'ending': END + 900,
              'pool': POOL, 'outcome': UP, 'side': 'UP', 'strategy': 'Delta Edge',
              'amount': 1000000, 'minimum_shares': 1300001, 'quoted_shares': 1314422}
    assert rig.ledger.reserve(intent, rig.settings, NOW) == 'reserved'
    with pytest.raises(ValueError, match='RESET_SESSION_ACTIVE'):
        rig.broker.reset_risk_session()


def test_continuous_trades_do_not_leak_into_overnight_comparison(rig, monkeypatch):
    from strategy_lab.execution import compare
    rig.broker.start_until(NOW + 3600)
    add_closed(rig, 0, NOW)
    rig.broker.start_continuous()
    add_closed(rig, 1, NOW + 3601)
    monkeypatch.setattr(compare, 'replay', lambda *a, **k: {'Delta Edge': SimpleNamespace(trades=[])})
    report = compare.compare(rig.ledger.conn, None, 'BTC')
    assert report['live_attempts'] == 1


@pytest.mark.parametrize('failure,code', [('timeout','BUY_TIMEOUT'), ('http','BUY_HTTP_401'), ('missing','BUY_RESPONSE_NO_HASH')])
def test_api_failures_skip_round_keep_cash_and_allow_next_slot(rig, monkeypatch, failure, code):
    def post(*a, **k):
        if failure == 'timeout':
            raise requests.Timeout('SECRET')
        return SimpleNamespace(status_code=401 if failure == 'http' else 200,
                               json=lambda: {'errors': [{'message': 'SECRET'}]})
    monkeypatch.setattr('strategy_lab.execution.live.requests.post', post)
    rig.broker.start_continuous()
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    assert engine.enter(rig.snapshot, NOW) == 'BUY_UNKNOWN'
    p = rig.ledger.positions()[0]
    assert code in p['error'] and 'SECRET' not in p['error']
    assert rig.ledger.summary(rig.settings)['reserved_usd'] == 1
    assert not rig.ledger.entry_active()
    assert rig.broker.entry_block() is None
    assert engine.enter(rig.snapshot, NOW) == 'already recorded'
    # A different Round can reserve while the unknown amount remains reserved.
    intent = {k: p[k] for k in ('symbol','pool','outcome','side','strategy','amount','minimum_shares','quoted_shares')}
    intent.update(id='next-round', ending=END+900)
    assert rig.ledger.reserve(intent, rig.settings, NOW) == 'reserved'
    assert rig.ledger.summary(rig.settings)['reserved_usd'] == 2


def test_unknown_timeout_discovers_mined_buy_after_restart_without_repost(rig, monkeypatch):
    def post(*a, **k): raise requests.Timeout()
    monkeypatch.setattr('strategy_lab.execution.live.requests.post', post)
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    engine.enter(rig.snapshot, NOW)
    identity = rig.ledger.positions()[0]['id']
    rig.rpc.receipts[BUY_HASH] = receipt(rig.broker.wallet, BUY_HASH, 'buy')
    original = rig.rpc.call
    def call(method, params):
        if method == 'eth_getLogs': return [{'transactionHash': BUY_HASH, 'removed': False}]
        return original(method, params)
    monkeypatch.setattr(rig.rpc, 'call', call)
    other = Ledger(rig.path, rig.settings)
    try:
        broker = LiveBroker(other, rig.profile, 'BTC', rig.rpc, rig.account)
        Executor(rig.settings, other, broker).advance(NOW, broker.settlement)
        assert other.get(identity)['state'] == 'OPEN'
        assert other.get(identity)['shares'] == 1314422
    finally:
        other.close()


def test_unknown_empty_chain_releases_only_after_expired_round_finality(rig, monkeypatch):
    def post(*a, **k): raise requests.Timeout()
    monkeypatch.setattr('strategy_lab.execution.live.requests.post', post)
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    engine.enter(rig.snapshot, NOW)
    identity = rig.ledger.positions()[0]['id']
    engine.advance(NOW, rig.broker.settlement)
    assert rig.ledger.get(identity)['state'] == 'BUY_UNKNOWN'
    original = rig.rpc.call
    def call(method, params):
        if method == 'eth_getBlockByNumber': return {'number': '0x101', 'timestamp': hex(END+301)}
        return original(method, params)
    monkeypatch.setattr(rig.rpc, 'call', call)
    monkeypatch.setattr('strategy_lab.execution.live.time.time', lambda: END+301)
    engine.advance(END+301, rig.broker.settlement)
    assert rig.ledger.get(identity)['state'] == 'EXPIRED'
    assert rig.ledger.summary(rig.settings)['reserved_usd'] == 0


def test_two_unknown_buys_pause_new_entries_and_preserve_cash(rig, monkeypatch):
    def post(*a, **k): raise requests.Timeout()
    monkeypatch.setattr('strategy_lab.execution.live.requests.post', post)
    rig.broker.start_continuous()
    Executor(rig.settings, rig.ledger, rig.broker).enter(rig.snapshot, NOW)
    p = rig.ledger.positions()[0]
    intent = {k: p[k] for k in ('symbol','pool','outcome','side','strategy','amount','minimum_shares','quoted_shares')}
    intent.update(id='another-unknown', ending=END+900)
    assert rig.ledger.reserve(intent, rig.settings, NOW) == 'reserved'
    rig.ledger.transition('another-unknown', 'BUY_READY', 'BUY_UNKNOWN', NOW)
    assert rig.broker.entry_block() == 'unknown buy exposure limit reached'
    assert rig.ledger.summary(rig.settings)['reserved_usd'] == 2


@pytest.mark.parametrize('fault', ['rpc', 'debit'])
def test_unknown_cash_not_released_on_scan_failure_or_pool_debit(rig, monkeypatch, fault):
    def post(*a, **k): raise requests.Timeout()
    monkeypatch.setattr('strategy_lab.execution.live.requests.post', post)
    engine = Executor(rig.settings, rig.ledger, rig.broker)
    engine.enter(rig.snapshot, NOW)
    original = rig.rpc.call
    def call(method, params):
        if method == 'eth_getBlockByNumber': return {'number':'0x101','timestamp':hex(END+301)}
        if method == 'eth_getLogs':
            if fault == 'rpc': raise ValueError('upstream failure')
            if params[0]['address'] == USDC: return [{'transactionHash': BUY_HASH}]
        return original(method, params)
    monkeypatch.setattr(rig.rpc, 'call', call)
    monkeypatch.setattr('strategy_lab.execution.live.time.time', lambda: END+301)
    engine.advance(END+301, rig.broker.settlement)
    assert rig.ledger.positions()[0]['state'] == 'BUY_UNKNOWN'
    assert rig.ledger.summary(rig.settings)['reserved_usd'] == 1


MANUAL_HASH = "0x" + "77" * 32


def _stuck_claim(rig):
    """A won Round whose claim was submitted and never confirmed."""
    engine, identity = buy(rig)
    rig.rpc.winner = bytes.fromhex(UP[2:])
    rig.rpc.lose_ack = True
    with patch("strategy_lab.execution.live.time.time", return_value=END + 300):
        engine.advance(END + 300, rig.broker.settlement)
    engine.advance(END + 1, rig.broker.settlement)
    assert rig.ledger.get(identity)["state"] == "REDEEM_PENDING"
    return engine, identity


class TestAdoptingAClaimMadeByHand:
    """The runner cannot see a claim it did not send, and waits for a hash that will
    never mine. Its own guard says to reconcile; this is the reconciling."""

    def test_a_proved_claim_is_adopted_and_the_round_closes(self, rig):
        engine, identity = _stuck_claim(rig)
        rig.rpc.receipts[MANUAL_HASH] = receipt(rig.broker.wallet, MANUAL_HASH, "claim")

        rig.broker.attach_claim(identity, MANUAL_HASH)
        engine.advance(END + 2, rig.broker.settlement)

        assert rig.ledger.get(identity)["state"] == "REDEEMED"
        # Adopting a claim must never put a transaction on the wire.
        assert len(rig.rpc.broadcasts) == 1

    def test_a_hash_the_chain_cannot_prove_is_refused(self, rig):
        _engine, identity = _stuck_claim(rig)

        with pytest.raises(ValueError):
            rig.broker.attach_claim(identity, MANUAL_HASH)
        assert rig.ledger.get(identity)["state"] == "REDEEM_PENDING"

    def test_a_claim_of_the_wrong_size_is_refused(self, rig):
        _engine, identity = _stuck_claim(rig)
        rig.rpc.receipts[MANUAL_HASH] = receipt(rig.broker.wallet, MANUAL_HASH, "claim",
                                                shares=999_999)

        with pytest.raises(ValueError):
            rig.broker.attach_claim(identity, MANUAL_HASH)

    def test_it_refuses_a_position_that_is_not_awaiting_a_claim(self, rig):
        engine, identity = buy(rig)

        with pytest.raises(ValueError):
            rig.broker.attach_claim(identity, MANUAL_HASH)

    def test_it_will_not_discard_a_recorded_claim_that_did_mine(self, rig):
        engine, identity = _stuck_claim(rig)
        # The runner's own transaction turns out to have mined after all.
        sent = rig.ledger.conn.execute(
            "SELECT tx_hash FROM live_ops WHERE position_id=? AND operation='redeem'",
            (identity,)).fetchone()[0]
        rig.rpc.receipts[sent] = receipt(rig.broker.wallet, sent, "claim")
        rig.rpc.receipts[MANUAL_HASH] = receipt(rig.broker.wallet, MANUAL_HASH, "claim")

        with pytest.raises(ValueError):
            rig.broker.attach_claim(identity, MANUAL_HASH)


class TestSayingWhyAQuoteWasRefused:
    """Nine different checks refuse a live quote. Reporting only the exception type makes
    a wallet short of gas look exactly like one holding the wrong shares."""

    def _reason(self, rig, monkeypatch, message):
        from strategy_lab.execution.errors import QuoteRefused

        def refuse(*a, **k):
            raise QuoteRefused(message)

        monkeypatch.setattr(rig.broker, "quote", refuse)
        engine = Executor(rig.settings, rig.ledger, rig.broker)
        return engine.enter(rig.snapshot, NOW)

    def test_the_failing_check_is_named(self, rig, monkeypatch):
        reason = self._reason(rig, monkeypatch, "fund claim gas before buying")
        assert reason == "quote refused: fund claim gas before buying"

    def test_anything_else_still_reports_only_its_type(self, rig, monkeypatch):
        def explode(*a, **k):
            raise requests.Timeout("sensitive credential")

        monkeypatch.setattr(rig.broker, "quote", explode)
        engine = Executor(rig.settings, rig.ledger, rig.broker)
        reason = engine.enter(rig.snapshot, NOW)

        assert reason == "quote unavailable (Timeout)"
        assert "sensitive" not in reason

    def test_the_log_translates_the_common_ones(self):
        from strategy_lab.execution.logging import explain

        raw = "quote refused: fund claim gas before buying"
        assert explain(raw, None) != raw
        assert "ETH" in explain(raw, None)

    def test_a_refusal_reserves_nothing(self, rig, monkeypatch):
        self._reason(rig, monkeypatch, "insufficient USDC or wrong stake")
        assert rig.ledger.positions() == []


class TestAHashTheChainNeverSaw:
    """The mint API can answer with a hash for a transaction it never sent.

    Seen live: a BTC buy sat in BUY_PENDING for over an hour while both
    eth_getTransactionByHash and eth_getTransactionReceipt returned null, holding the
    entry slot so every later Round passed untraded.
    """

    def _pending_with_a_phantom_hash(self, rig):
        engine = Executor(rig.settings, rig.ledger, rig.broker)
        engine.enter(rig.snapshot, NOW)
        identity = rig.ledger.positions()[0]["id"]
        assert rig.ledger.get(identity)["state"] == "BUY_PENDING"
        # BUY_HASH is recorded but deliberately absent from rig.rpc.receipts.
        return engine, identity

    def test_it_waits_while_the_round_is_still_running(self, rig):
        engine, identity = self._pending_with_a_phantom_hash(rig)

        engine.advance(NOW, rig.broker.settlement)

        assert rig.ledger.get(identity)["state"] == "BUY_PENDING"

    def test_once_the_round_is_over_it_becomes_ambiguous(self, rig, monkeypatch):
        engine, identity = self._pending_with_a_phantom_hash(rig)
        monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: END + 1)

        engine.advance(END + 1, rig.broker.settlement)

        position = rig.ledger.get(identity)
        assert position["state"] == "BUY_UNKNOWN"
        assert "UNKNOWN_TO_CHAIN" in position["error"]

    def test_it_frees_the_entry_slot_while_keeping_the_stake(self, rig, monkeypatch):
        engine, identity = self._pending_with_a_phantom_hash(rig)
        monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: END + 1)
        engine.advance(END + 1, rig.broker.settlement)

        # Ambiguity reserves cash and exposure but does not occupy the slot.
        assert rig.ledger.entry_active(END + 1) == []
        assert rig.ledger.summary(rig.settings)["reserved_usd"] == 1

    def test_a_hash_the_chain_does_know_is_left_alone(self, rig, monkeypatch):
        engine, identity = self._pending_with_a_phantom_hash(rig)
        rig.rpc.receipts[BUY_HASH] = receipt(rig.broker.wallet, BUY_HASH, "buy")
        monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: END + 1)

        engine.advance(END + 1, rig.broker.settlement)

        assert rig.ledger.get(identity)["state"] == "OPEN"

    def test_the_chain_is_not_asked_more_than_once_a_minute(self, rig, monkeypatch):
        engine, identity = self._pending_with_a_phantom_hash(rig)
        asked = []
        original = rig.rpc.call

        def counted(method, params):
            if method == "eth_getTransactionByHash":
                asked.append(params[0])
            return original(method, params)

        monkeypatch.setattr(rig.rpc, "call", counted)
        monkeypatch.setattr("strategy_lab.execution.live.time.time", lambda: END + 1)
        for _ in range(4):
            engine.advance(END + 1, rig.broker.settlement)

        assert len(asked) == 1
