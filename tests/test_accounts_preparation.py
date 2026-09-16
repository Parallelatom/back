import json

import pytest

from strategy_lab.execution.accounts import (
    CLAIMANT, ENDPOINT, TRANSFER, USDC, ZERO, ReadOnlyRPC, claim_transaction,
    credential_present, inspect_receipt, mint_hash, mint_payload, read_profiles,
)

WALLET = "0x" + "11" * 20
POOL = "0x" + "22" * 20
SHARE = "0x" + "33" * 20
HASH = "0x" + "44" * 32


def transfer(token, sender, recipient, amount):
    return {"address": token, "transactionHash": HASH, "removed": False,
            "topics": [TRANSFER, "0x" + sender[2:].rjust(64, "0"),
                       "0x" + recipient[2:].rjust(64, "0")],
            "data": "0x" + format(amount, "064x")}


def receipt(operation):
    logs = [transfer(USDC, WALLET, POOL, 1000000),
            transfer(SHARE, ZERO, WALLET, 1314422)] if operation == "buy" else [
                transfer(USDC, POOL, WALLET, 1314422),
                transfer(SHARE, WALLET, ZERO, 1314422)]
    logs.append({"address": POOL, "transactionHash": HASH, "topics": ["0xother"]})
    return {"transactionHash": HASH, "from": WALLET, "to": CLAIMANT,
            "status": "0x1", "gasUsed": "0x10", "effectiveGasPrice": "0x2", "logs": logs}


def test_prepared_requests_have_no_credentials_or_stale_nonce():
    payload = mint_payload(POOL, "0x1234567890abcdef", 1789547334138)
    assert payload["variables"]["mint"]["amount"] == "1000000"
    assert "authorization" not in json.dumps(payload).lower()
    claim = claim_transaction(WALLET, POOL)
    assert len(bytes.fromhex(claim["data"][2:])) == 100
    assert claim["data"].startswith("0xeaca3a20")
    assert "nonce" not in claim and "gas" not in claim


@pytest.mark.parametrize("reply", [{}, {"data": {"ninelivesMint": True}},
    {"errors": [{"message": "secret"}], "data": {"ninelivesMint": HASH}}])
def test_hash_is_not_http_success(reply):
    with pytest.raises(ValueError):
        mint_hash(reply)


def test_hash_parsing():
    assert mint_hash({"data": {"ninelivesMint": HASH}}) == HASH


@pytest.mark.parametrize("operation", ["buy", "claim"])
def test_real_transfer_shape(operation):
    report = inspect_receipt(receipt(operation), HASH, WALLET, POOL, SHARE, operation)
    assert report["gas_wei"] == 32
    assert report["shares_minted_raw" if operation == "buy" else "shares_burned_raw"] == 1314422


@pytest.mark.parametrize("fault", ["wallet", "pool", "share", "hash", "status", "removed", "zero", "no_burn"])
def test_claim_success_alone_never_proves_payment(fault):
    r = receipt("claim")
    wallet, pool, share, tx = WALLET, POOL, SHARE, HASH
    other = "0x" + "99" * 20
    if fault == "wallet": wallet = other
    if fault == "pool": pool = other
    if fault == "share": share = other
    if fault == "hash": tx = "0x" + "99" * 32
    if fault == "status": r["status"] = "0x0"
    if fault == "removed": r["logs"][0]["removed"] = True
    if fault == "zero": r["logs"][0]["data"] = "0x" + "0" * 64
    if fault == "no_burn": del r["logs"][1]
    with pytest.raises(ValueError):
        inspect_receipt(r, tx, wallet, pool, share, "claim")


def test_profiles_require_distinct_wallets_and_environment_names(tmp_path):
    config = json.loads(open("execution-accounts.example.json").read())
    config["wallets"]["BTC"]["address"] = WALLET
    config["wallets"]["XYZCL"]["address"] = POOL
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert read_profiles(path)["accounts_url"] == ENDPOINT
    config["wallets"]["XYZCL"]["address"] = WALLET
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError): read_profiles(path)


def test_secret_is_only_checked_for_presence(monkeypatch):
    monkeypatch.setenv("NINELIVES_BTC_AUTHORIZATION", "sensitive")
    assert credential_present("NINELIVES_BTC_AUTHORIZATION") is True
    monkeypatch.setenv("NINELIVES_BTC_AUTHORIZATION", "a\nb")
    assert credential_present("NINELIVES_BTC_AUTHORIZATION") is False


@pytest.mark.parametrize("fault", ["pending", "unfinalized", "reorg", "chain"])
def test_readonly_rpc_refuses_unfinalized_or_wrong_chain(monkeypatch, fault):
    rpc = ReadOnlyRPC()
    def call(method, params):
        if method == "eth_chainId": return "0x1" if fault == "chain" else "0xa4b1"
        if method == "eth_getTransactionReceipt":
            return None if fault == "pending" else {"blockNumber": "0x10", "blockHash": HASH}
        if params[0] == "finalized": return {"number": "0xf" if fault == "unfinalized" else "0x11"}
        return {"hash": "different" if fault == "reorg" else HASH}
    monkeypatch.setattr(rpc, "call", call)
    with pytest.raises(ValueError): rpc.finalized_receipt(HASH)


def test_rpc_cannot_broadcast():
    with pytest.raises(ValueError): ReadOnlyRPC().call("eth_sendRawTransaction", [])


def test_selected_symbol_allows_unused_disabled_wallet_blank(tmp_path):
    config = json.loads(open("execution-accounts.example.json").read())
    config["wallets"]["XYZCL"]["address"] = WALLET
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    assert read_profiles(path, selected_symbol="XYZCL")["wallets"]["BTC"]["address"] == ""
    with pytest.raises(ValueError, match="CONFIG_ADDRESS_BTC"):
        read_profiles(path, selected_symbol="BTC")
    config["wallets"]["BTC"]["enabled"] = True
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="CONFIG_ADDRESS_BTC"):
        read_profiles(path, selected_symbol="XYZCL")


def test_profile_diagnostics_never_echo_invalid_input(tmp_path):
    config = json.loads(open("execution-accounts.example.json").read())
    config["wallets"]["XYZCL"]["address"] = "sensitive-pasted-in-wrong-field"
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(ValueError, match="CONFIG_ADDRESS_XYZCL") as err:
        read_profiles(path, selected_symbol="XYZCL")
    assert "sensitive" not in str(err.value)
