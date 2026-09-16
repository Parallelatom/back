"""Preparation and receipt inspection for the observed Accounts API.

This module stays read-only. The separately enabled live.py adapter implements POST
and signing; it never replays captured authenticated requests to probe credentials.
"""
import json
import os
import re
from pathlib import Path

import requests
from .errors import SetupError

ENDPOINT = "https://arb-accounts.superposition.so/"
USDC = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
CLAIMANT = "0xf8da8d65120b317331c79092bf65e99bed6e65de"
ZERO = "0x" + "0" * 40
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def hex_value(value, size):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{%d}" % (size * 2), value):
        raise ValueError("invalid hexadecimal identifier")
    return value.lower()


def address(value):
    value = hex_value(value, 20)
    if value == ZERO:
        raise ValueError("zero address is not allowed")
    return value


def read_profiles(path, selected_symbol=None):
    """Public configuration only; secrets live in named environment variables."""
    try:
        data = json.loads(Path(path).read_text())
    except OSError:
        raise SetupError("CONFIG_FILE: cannot read the public JSON configuration") from None
    except (ValueError, UnicodeError):
        raise SetupError("CONFIG_JSON: configuration is not valid UTF-8 JSON") from None
    if not isinstance(data, dict):
        raise SetupError("CONFIG_JSON: configuration must be a JSON object")
    if data.get("chain_id") != 42161 or data.get("accounts_url") != ENDPOINT:
        raise SetupError("CONFIG_NETWORK: expected chain_id 42161 and https://arb-accounts.superposition.so/")
    wallets = data.get("wallets", {})
    if not isinstance(wallets, dict) or set(wallets) != {"BTC", "XYZCL"}:
        raise SetupError("CONFIG_WALLETS: configuration must contain BTC and XYZCL profiles")
    seen = set()
    env_names = set()
    for symbol, profile in wallets.items():
        if not isinstance(profile, dict):
            raise SetupError("CONFIG_PROFILE: each wallet profile must be a JSON object")
        if type(profile.get("enabled", False)) is not bool:
            raise SetupError("CONFIG_ENABLED: enabled must be true or false without quotes")
        # Single-Symbol preflight must not require a wallet the user is not using.
        if (selected_symbol in ("BTC", "XYZCL") and symbol != selected_symbol
                and profile.get("enabled", False) is False and not profile.get("address")):
            continue
        try:
            wallet = address(profile.get("address"))
        except ValueError:
            raise SetupError(f"CONFIG_ADDRESS_{symbol}: fill a nonzero public address (0x plus 40 hex characters)") from None
        if wallet in seen:
            raise SetupError("CONFIG_DUPLICATE_WALLET: BTC and XYZCL must use different wallets")
        seen.add(wallet)
        name = profile.get("authorization_env")
        if not isinstance(name, str) or not re.fullmatch(r"NINELIVES_(BTC|XYZCL)_AUTHORIZATION", name) or name in env_names:
            raise SetupError("CONFIG_AUTH_ENV: use a distinct NINELIVES_<SYMBOL>_AUTHORIZATION variable name")
        env_names.add(name)
        if name != "NINELIVES_" + symbol + "_AUTHORIZATION":
            raise SetupError("CONFIG_AUTH_ENV: authorization environment variable must match Symbol")
        if profile.get("stake_micro") != 1_000_000 or profile.get("budget_micro") != 10_000_000:
            raise SetupError("CONFIG_BUDGET: expected stake_micro 1000000 and budget_micro 10000000")
    return data


def credential_present(name):
    """Report presence only. Do not echo or infer wallet ownership from a prefix."""
    value = os.environ.get(name, "")
    return bool(value.strip()) and not any(c in value for c in "\r\n")


def mint_payload(pool, outcome, timestamp_ms):
    if type(timestamp_ms) is not int or timestamp_ms <= 0:
        raise ValueError("timestamp must be positive integer milliseconds")
    return {
        "query": "mutation ($mint: Mint!) { ninelivesMint(mint: $mint) }",
        "variables": {"mint": {
            "amount": "1000000", "market": address(pool), "referrer": ZERO,
            "ms_ts": str(timestamp_ms), "outcome": hex_value(outcome, 8),
        }},
    }


def mint_hash(reply):
    if not isinstance(reply, dict) or reply.get("errors"):
        raise ValueError("API result is ambiguous; do not resubmit automatically")
    try:
        return hex_value(reply["data"]["ninelivesMint"], 32)
    except (KeyError, TypeError, ValueError):
        raise ValueError("API did not return a transaction hash; reconcile before retry") from None


def claim_transaction(wallet, pool):
    """Unsigned payoff(address[]) for ONE pool; nonce/gas must be fresh at signing."""
    data = "0xeaca3a20" + f"{32:064x}{1:064x}" + address(pool)[2:].rjust(64, "0")
    return {"chainId": 42161, "from": address(wallet), "to": CLAIMANT,
            "value": "0x0", "data": data}


class ReadOnlyRPC:
    def __init__(self):
        self.url = "https://arb1.arbitrum.io/rpc"

    def call(self, method, params):
        if method not in {"eth_chainId", "eth_getTransactionReceipt", "eth_getBlockByNumber"}:
            raise ValueError("only read-only RPC methods are allowed")
        try:
            response = requests.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                                     "method": method, "params": params},
                                     headers={"User-Agent": "strategy-lab/1.0"},
                                     timeout=15, allow_redirects=False)
            if response.status_code != 200:
                raise ValueError("RPC HTTP failure")
            reply = response.json()
            if reply.get("error") or "result" not in reply:
                raise ValueError("RPC returned an error")
            return reply["result"]
        except (requests.RequestException, ValueError):
            raise ValueError("read-only RPC failed") from None

    def finalized_receipt(self, tx_hash):
        tx_hash = hex_value(tx_hash, 32)
        if self.call("eth_chainId", []) != "0xa4b1":
            raise ValueError("wrong RPC chain")
        receipt = self.call("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            raise ValueError("receipt pending or unknown; do not resubmit")
        block_number = receipt["blockNumber"]
        canonical = self.call("eth_getBlockByNumber", [block_number, False])
        finalized = self.call("eth_getBlockByNumber", ["finalized", False])
        if (not canonical or not finalized or canonical["hash"] != receipt["blockHash"]
                or int(finalized["number"], 16) < int(block_number, 16)):
            raise ValueError("receipt is not canonical and finalized yet")
        return receipt


def inspect_receipt(receipt, tx_hash, wallet, pool, share_token, operation):
    """Require caller-supplied share-token mapping; never guess it from any mint log.

    This is an inspection report, not a Broker Receipt and cannot credit a ledger.
    Pool/outcome/share mapping must be independently established before live use.
    """
    wallet, pool, share_token = map(address, (wallet, pool, share_token))
    if share_token in {USDC, pool, wallet} or operation not in {"buy", "claim"}:
        raise ValueError("invalid expected receipt context")
    if hex_value(receipt["transactionHash"], 32) != hex_value(tx_hash, 32):
        raise ValueError("receipt hash mismatch")
    if receipt.get("status") != "0x1":
        raise ValueError("transaction did not succeed")
    if operation == "claim" and (receipt["to"].lower() != CLAIMANT or receipt["from"].lower() != wallet):
        raise ValueError("unexpected claimant or sender")
    amounts = {"usdc_out_micro": 0, "usdc_in_micro": 0, "shares_minted_raw": 0,
               "shares_burned_raw": 0, "pool_received_micro": 0}
    pool_event = False
    for log in receipt["logs"]:
        if log.get("removed") or log.get("transactionHash", "").lower() != tx_hash.lower():
            raise ValueError("removed or unrelated log")
        token = log["address"].lower()
        pool_event |= token == pool
        topics = log["topics"]
        if not topics or topics[0].lower() != TRANSFER:
            continue
        if len(topics) != 3:
            raise ValueError("malformed Transfer")
        sender, recipient = ["0x" + hex_value(t, 32)[-40:] for t in topics[1:]]
        amount = int(hex_value(log["data"], 32), 16)
        if token == USDC:
            if sender == wallet:
                amounts["usdc_out_micro"] += amount
            if sender == pool and recipient == wallet:
                amounts["usdc_in_micro"] += amount
            if recipient == pool:
                amounts["pool_received_micro"] += amount
        if token == share_token:
            if sender == ZERO and recipient == wallet:
                amounts["shares_minted_raw"] += amount
            if sender == wallet and recipient == ZERO:
                amounts["shares_burned_raw"] += amount
    if not pool_event:
        raise ValueError("expected pool emitted no events")
    if operation == "buy" and not (amounts["usdc_out_micro"] == 1_000_000
            and amounts["pool_received_micro"] == 1_000_000 and amounts["shares_minted_raw"] > 0):
        raise ValueError("buy transfers do not match the expected 1 USDC purchase")
    if operation == "claim" and not (amounts["shares_burned_raw"] > 0 and amounts["usdc_in_micro"] > 0):
        raise ValueError("claim has no matching share burn and USDC payout")
    return dict(amounts, gas_wei=int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16))
