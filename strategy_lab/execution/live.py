"""Accounts API buy + locally signed Arbitrum payoff, with durable at-most-once intent.

Only the explicit live CLI constructs this adapter. HTTP acknowledgements never credit
cash. Unknown API submissions skip their Round while retaining funds for reconciliation.
"""
import json
import os
import time
from types import SimpleNamespace

import requests
from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from .accounts import (CLAIMANT, ENDPOINT, USDC, ZERO, address, claim_transaction,
                       hex_value, inspect_receipt, mint_hash, mint_payload)
from .config import Settings
from .errors import SetupError, NotSubmitted, BuyUncertain
from .paper import Quote, Receipt


def calldata(signature, types=(), values=()):
    return "0x" + (keccak(text=signature)[:4] + encode(types, values)).hex()


class RPC:
    def __init__(self, url="https://arb1.arbitrum.io/rpc"):
        if not url.startswith("https://"):
            raise ValueError("RPC must use HTTPS")
        self.url = url

    def call(self, method, params):
        try:
            r = requests.post(self.url, json={"jsonrpc": "2.0", "id": 1,
                              "method": method, "params": params}, timeout=20,
                              headers={"User-Agent": "strategy-lab/1.0"}, allow_redirects=False)
            if r.status_code != 200:
                raise ValueError()
            reply = r.json()
            if reply.get("error") or "result" not in reply:
                raise ValueError()
            return reply["result"]
        except (requests.RequestException, ValueError):
            raise SetupError("RPC_REQUEST: Arbitrum RPC failed or rejected the call; check connectivity and retry read-only preflight") from None

    def view(self, target, signature, inputs=(), values=(), outputs=("uint256",), block="latest"):
        raw = self.call("eth_call", [{"to": address(target),
                         "data": calldata(signature, inputs, values)}, block])
        return decode(outputs, bytes.fromhex(raw[2:]))

    def mined(self, tx_hash):
        receipt = self.call("eth_getTransactionReceipt", [hex_value(tx_hash, 32)])
        if receipt is None:
            return None
        block = self.call("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if not block or block["hash"] != receipt["blockHash"]:
            return None
        receipt["_canonical_timestamp"] = int(block["timestamp"], 16)
        return receipt



def settings_from_profile(profile, symbol, wallet):
    # Reuse the strict paper risk-value validation, then explicitly select live mode.
    open_limit = profile.get("max_open_positions", 1)
    if type(open_limit) is not int or open_limit not in (1, 2):
        raise SetupError("CONFIG_OPEN_POSITIONS: max_open_positions must be 1 or 2")
    settings = Settings(enabled=profile.get("enabled", False), symbols=(symbol,),
                        wallet_label=wallet, strategy="Delta Edge", stake=1_000_000,
                        bankroll=10_000_000, max_open_positions=open_limit, max_exposure=10_000_000,
                        daily_spend=10_000_000, daily_loss=2_000_000, max_signal_age_seconds=15)
    floor = profile.get("min_quote_shares_micro", 1_300_000)
    if type(floor) is not int or not 1_300_000 <= floor <= 100_000_000:
        raise SetupError("CONFIG_SHARE_FLOOR: min_quote_shares_micro must be an integer >= 1300000")
    window = profile.get("trade_window_open_seconds", 300)
    if type(window) is not int or not 300 <= window <= 840:
        raise SetupError("CONFIG_TRADE_WINDOW: trade_window_open_seconds must be an integer from 300 to 840")
    return SimpleNamespace(**{**settings.__dict__, "mode": "live", "min_quote_shares_micro": floor,
                              "trade_window_open_seconds": window,
                              "trade_window_close_seconds": 75})


class LiveBroker:
    simulated = False

    def __init__(self, ledger, profile, symbol, rpc=None, account=None):
        self.ledger, self.profile = ledger, profile
        self.wallet = address(profile["address"])
        self.rpc = rpc or RPC()
        if account is None:
            key = os.environ.get("NINELIVES_" + symbol + "_PRIVATE_KEY", "")
            if not key.strip():
                raise SetupError("PRIVATE_KEY_MISSING: fill the selected wallet's NINELIVES_<SYMBOL>_PRIVATE_KEY in execution-secrets.env")
            try:
                account = Account.from_key(key)
            except Exception:
                raise SetupError("PRIVATE_KEY_INVALID: expected a 32-byte hex private key, not Authorization or a seed phrase") from None
        self.account = account
        if self.account.address.lower() != self.wallet:
            raise SetupError("PRIVATE_KEY_MISMATCH: claim signing key does not match configured wallet address")
        self.auth = os.environ.get(profile["authorization_env"], "")
        if not self.auth.strip() or any(c in self.auth for c in "\r\n"):
            raise SetupError("AUTHORIZATION_MISSING: fill the selected Authorization environment variable with the full single-line value")
        self.max_trades = profile.get("max_trades", 1)
        self.gas_cap = profile.get("claim_gas_cap_wei", 100_000_000_000_000)
        if type(self.max_trades) is not int or not 1 <= self.max_trades <= 10:
            raise SetupError("CONFIG_MAX_TRADES: max_trades must be an integer 1..10 per ledger")
        if type(self.gas_cap) is not int or not 0 < self.gas_cap <= 10**15:
            raise SetupError("CONFIG_GAS_CAP: claim gas cap must be a positive integer at most 1000000000000000 wei")
        self.recovery_checks = {}
        ledger.conn.executescript("""
            CREATE TABLE IF NOT EXISTS live_ops (
                position_id TEXT NOT NULL, operation TEXT NOT NULL,
                request TEXT NOT NULL, tx_hash TEXT UNIQUE, raw_tx TEXT,
                share_token TEXT NOT NULL, outcome_up TEXT, outcome_down TEXT,
                gas_wei INTEGER, PRIMARY KEY(position_id,operation));
            CREATE TABLE IF NOT EXISTS buy_recovery (
                position_id TEXT PRIMARY KEY, from_block INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS live_flags (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS overnight_session (
                id INTEGER PRIMARY KEY CHECK(id=1), started_at INTEGER NOT NULL,
                ends_at INTEGER NOT NULL, baseline_ids TEXT NOT NULL);
        """)

    def start_continuous(self):
        # Preserve the original timed report window and every existing risk counter.
        now = int(time.time())
        with self.ledger.conn:
            self.ledger.conn.execute("INSERT OR IGNORE INTO overnight_session VALUES (1,?,?,?)",
                (now, now, json.dumps([p["id"] for p in self.ledger.positions()])))
            self.ledger.conn.execute("INSERT OR REPLACE INTO live_flags VALUES ('continuous','1')")
        return self.overnight_status()

    def reset_risk_session(self):
        """Start fresh loss/attempt counters without deleting execution history."""
        session = self.overnight_status()
        if not session or not session["continuous"]:
            raise SetupError("RESET_SESSION_MODE: risk reset requires an existing continuous session")
        if self.ledger.active():
            raise SetupError("RESET_SESSION_ACTIVE: finish or reconcile every pending position before reset")
        now = int(time.time())
        baseline = json.dumps([p["id"] for p in self.ledger.positions()])
        with self.ledger.conn:
            self.ledger.conn.execute(
                "UPDATE overnight_session SET started_at=?, ends_at=?, baseline_ids=? WHERE id=1",
                (now, now, baseline),
            )
        return self.overnight_status()

    def start_overnight(self, hours):
        if type(hours) is not int or not 1 <= hours <= 12:
            raise SetupError("OVERNIGHT_HOURS: use an integer from 1 to 12")
        session = self.overnight_status()
        if session and session["continuous"]:
            raise SetupError("CONTINUOUS_ACTIVE: use the halt file to stop entries")
        if session:
            if session["ends_at"] - session["started_at"] != hours * 3600:
                raise SetupError("OVERNIGHT_EXISTS: resume with the original duration; deadline cannot be extended")
            return session
        return self.start_until(int(time.time()) + hours * 3600)

    def start_until(self, ends_at):
        session = self.overnight_status()
        if session and session["continuous"]:
            raise SetupError("CONTINUOUS_ACTIVE: stop entries with the halt file; timed flags cannot replace continuous mode")
        if session:
            if session["ends_at"] != ends_at:
                raise SetupError("OVERNIGHT_EXISTS: resume the saved deadline; it cannot be extended")
            return session
        now = int(time.time())
        if type(ends_at) is not int or not now < ends_at <= now + 24 * 3600:
            raise SetupError("OVERNIGHT_UNTIL: new deadline must be in the next 24 hours")
        # A restart resumes the original session; it can never extend its deadline.
        with self.ledger.conn:
            self.ledger.conn.execute("INSERT OR IGNORE INTO overnight_session VALUES (1,?,?,?)",
                (now, ends_at, json.dumps([p["id"] for p in self.ledger.positions()])))
        return self.overnight_status()

    def overnight_status(self, exclude_id=None):
        row = self.ledger.conn.execute("SELECT * FROM overnight_session WHERE id=1").fetchone()
        if not row:
            return None
        baseline = set(json.loads(row["baseline_ids"]))
        positions = [p for p in self.ledger.positions() if p["id"] not in baseline and p["id"] != exclude_id]
        spent = sum(p["amount"] for p in positions if p["state"] not in ("EXPIRED", "BUY_REJECTED"))
        loss = sum(max(0, p["cost"] - p["payout"]) for p in positions if p["state"] in ("LOST", "REDEEMED"))
        return {"continuous": self.ledger.conn.execute("SELECT 1 FROM live_flags WHERE name='continuous' AND value='1'").fetchone() is not None,
                "started_at": row["started_at"], "ends_at": row["ends_at"],
                "attempts": len(positions), "max_trades": None, "committed_micro": spent,
                "budget_micro": None, "initial_bankroll_micro": 10_000_000, "loss_micro": loss, "loss_limit_micro": 2_000_000}

    def preflight(self):
        if self.rpc.call("eth_chainId", []) != "0xa4b1":
            raise SetupError("RPC_CHAIN: RPC did not return Arbitrum One chain ID")
        for contract in (USDC, CLAIMANT):
            if self.rpc.call("eth_getCode", [contract, "latest"]) == "0x":
                raise SetupError("RPC_CONTRACT: required USDC or Claimant contract has no code")
        if self.rpc.call("eth_getCode", [self.wallet, "latest"]) != "0x":
            raise SetupError("WALLET_TYPE: wallet has contract code; this signer supports plain EOA wallets only")
        if self.rpc.view(USDC, "decimals()", outputs=("uint8",))[0] != 6:
            raise SetupError("RPC_TOKEN: USDC decimals must be 6")
        return {"wallet": self.wallet, "chain_id": 42161,
                "usdc_micro": self.balance(USDC),
                "eth_wei": int(self.rpc.call("eth_getBalance", [self.wallet, "latest"]), 16),
                "authorization_present": True, "signer_matches": True,
                "api_authentication_verified": False}

    def balance(self, token):
        return self.rpc.view(token, "balanceOf(address)", ("address",), (self.wallet,))[0]

    def op(self, position, operation):
        row = self.ledger.conn.execute("SELECT * FROM live_ops WHERE position_id=? AND operation=?",
                                       (position["id"], operation)).fetchone()
        return dict(row) if row else None

    def stop(self, reason):
        with self.ledger.conn:
            self.ledger.conn.execute("INSERT OR REPLACE INTO live_flags VALUES ('halt',?)", (reason,))

    def entry_block(self, exclude_id=None):
        row = self.ledger.conn.execute("SELECT value FROM live_flags WHERE name='halt'").fetchone()
        if row:
            return row[0]
        unknown = [p for p in self.ledger.active() if p["state"] == "BUY_UNKNOWN" and p["id"] != exclude_id]
        if sum(p["amount"] for p in unknown) >= 2_000_000:
            return "unknown buy exposure limit reached"
        session = self.overnight_status(exclude_id)
        if session:
            if not session["continuous"] and time.time() >= session["ends_at"]:
                return "overnight entry deadline reached"
            if session["loss_micro"] >= session["loss_limit_micro"]:
                return "overnight loss limit reached"
            return None
        if len([p for p in self.ledger.positions() if p["id"] != exclude_id]) >= self.max_trades:
            return "configured lifetime trade count reached"
        return None

    def mapping(self, pool, outcome, block="latest"):
        return address(self.rpc.view(pool, "shareAddr(bytes8)", ("bytes8",),
                       (bytes.fromhex(hex_value(outcome, 8)[2:]),), ("address",), block)[0])

    def quote(self, snapshot, side, amount):
        if self.entry_block():
            raise ValueError("live entries halted")
        if amount != 1_000_000 or self.balance(USDC) < amount:
            raise ValueError("insufficient USDC or wrong stake")
        if int(self.rpc.call("eth_getBalance", [self.wallet, "latest"]), 16) < self.gas_cap:
            raise ValueError("fund claim gas before buying")
        if self.profile.get("accept_unprotected_slippage") is not True:
            raise ValueError("Accounts mint has no verified minimum output; acknowledge in config")
        pool = address(snapshot.pool)
        if self.rpc.view(pool, "timeEnding()")[0] != snapshot.record.ending:
            raise ValueError("pool expiry does not match Round")
        if snapshot.record.ending - time.time() < 75:
            raise ValueError("too near buy cutoff")
        if self.rpc.view(pool, "isDppm()", outputs=("bool",))[0]:
            raise ValueError("DPPM pools are not supported by this binary executor")
        outcomes = self.rpc.view(pool, "outcomeList()", outputs=("bytes8[]",))[0]
        expected = {hex_value(snapshot.outcome_up, 8), hex_value(snapshot.outcome_down, 8)}
        if len(outcomes) != 2 or {"0x" + o.hex() for o in outcomes} != expected or len(expected) != 2:
            raise ValueError("pool outcomes do not match Round")
        outcome = snapshot.outcome_up if side == "UP" else snapshot.outcome_down
        share = self.mapping(pool, outcome)
        if self.balance(share):
            raise ValueError("pool already has shares; use a dedicated wallet")
        shares = self.rpc.view(pool, "quoteC0E17FC7(bytes8,uint256)", ("bytes8", "uint256"),
                     (bytes.fromhex(outcome[2:]), amount), ("uint256", "uint256", "uint256"))[0]
        return Quote(shares, int(time.time()))

    def submit_buy(self, position, snapshot):
        if self.op(position, "buy"):
            return  # a prior attempt exists, including an unknown acknowledgement
        if self.profile.get("enabled") is not True or self.entry_block(exclude_id=position["id"]):
            raise NotSubmitted("live entries disabled or limit reached before sending")
        floor = self.profile.get("min_quote_shares_micro", 1_300_000)
        if position["quoted_shares"] <= floor:
            raise NotSubmitted("quote is not above share minimum")
        if position["ending"] - time.time() < 75:
            raise NotSubmitted("too near cutoff")
        share = self.mapping(position["pool"], position["outcome"])
        if self.entry_block(exclude_id=position["id"]):
            raise NotSubmitted("entry limit reached during pre-submit reads")
        if position["ending"] - time.time() < 75 or time.time() - position["created_at"] > 5:
            raise NotSubmitted("buy timing expired before submission")
        # Do not let pre-submit RPC latency outlive the observed oracle/metadata.
        if (time.time() - snapshot.metadata_at > 15
                or not snapshot.record.prices
                or time.time() - snapshot.record.prices[-1][0] > 15):
            raise NotSubmitted("market observations expired before submission")
        try:
            from_block = int(self.rpc.call("eth_blockNumber", []), 16)
        except Exception:
            raise NotSubmitted("cannot capture recovery block before API submission") from None
        if (time.time() - position["created_at"] > 5 or position["ending"] - time.time() < 75
                or time.time() - snapshot.metadata_at > 15
                or time.time() - snapshot.record.prices[-1][0] > 15):
            raise NotSubmitted("timing expired while capturing recovery block")
        payload = mint_payload(position["pool"], position["outcome"], int(time.time() * 1000))
        # Durable marker before HTTP. No transport retry and no redirect with credentials.
        with self.ledger.conn:
            self.ledger.conn.execute("INSERT INTO buy_recovery VALUES (?,?)", (position["id"], from_block))
            self.ledger.conn.execute("INSERT INTO live_ops(position_id,operation,request,share_token,outcome_up,outcome_down) VALUES (?,'buy',?,?,?,?)",
                (position["id"], json.dumps(payload), share, hex_value(snapshot.outcome_up, 8), hex_value(snapshot.outcome_down, 8)))
        try:
            response = requests.post(ENDPOINT, json=payload, headers={"Authorization": self.auth,
                "Content-Type": "application/json", "Origin": "https://www.9lives.so",
                "Referer": "https://www.9lives.so/"}, timeout=20, allow_redirects=False)
            if response.status_code != 200:
                raise BuyUncertain(f"BUY_HTTP_{int(response.status_code)}: skipped Round; funds reserved for chain reconciliation")
            try:
                tx = mint_hash(response.json())
            except (ValueError, TypeError, KeyError, AttributeError):
                raise BuyUncertain("BUY_RESPONSE_NO_HASH: skipped Round; funds reserved for chain reconciliation") from None
        except requests.Timeout:
            raise BuyUncertain("BUY_TIMEOUT: skipped Round; funds reserved for chain reconciliation") from None
        except requests.RequestException:
            raise BuyUncertain("BUY_NETWORK: skipped Round; funds reserved for chain reconciliation") from None
        with self.ledger.conn:
            self.ledger.conn.execute("UPDATE live_ops SET tx_hash=? WHERE position_id=? AND operation='buy'",
                                     (tx, position["id"]))

    def lookup_buy(self, position):
        op = self.op(position, "buy")
        if op and not op["tx_hash"]:
            if position["state"] == "BUY_PENDING":
                self.ledger.transition(position["id"], "BUY_PENDING", "BUY_UNKNOWN", int(time.time()),
                    error="BUY_ACK_UNKNOWN: skipped Round; funds reserved for chain reconciliation")
                return None
            self.recover_unknown_buy(position, op)
        return self._receipt(position, "buy")

    def recover_unknown_buy(self, position, op):
        now = int(time.time())
        if now - self.recovery_checks.get(position["id"], 0) < 60:
            return
        self.recovery_checks[position["id"]] = now
        marker = self.ledger.conn.execute("SELECT from_block FROM buy_recovery WHERE position_id=?",
                                          (position["id"],)).fetchone()
        if not marker:
            return  # Legacy ambiguity retains its reservation; explicit attach is supported.
        head = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        end = int(head["number"], 16)
        start = marker[0]
        # Bound the read. After an extended outage require operator reconciliation.
        if end < start or end - start > 20000:
            return
        hashes = set()
        for low in range(start, end + 1, 2000):
            logs = self.rpc.call("eth_getLogs", [{"address": op["share_token"],
                "fromBlock": hex(low), "toBlock": hex(min(low + 1999, end)),
                "topics": ["0x" + keccak(text="Transfer(address,address,uint256)").hex(),
                           None, "0x" + self.wallet[2:].rjust(64, "0")]}])
            hashes.update(hex_value(log["transactionHash"], 32) for log in logs if not log.get("removed"))
        if len(hashes) == 1:
            self.attach_buy(position["id"], hashes.pop())
            return
        if hashes:
            return  # Multiple candidates require explicit review.
        # Only release a missing purchase once the chain has finalized beyond the
        # expired Round and both share receipts and wallet USDC debits are absent.
        final = self.rpc.call("eth_getBlockByNumber", ["finalized", False])
        if int(final["timestamp"], 16) < position["ending"] + 300 or int(final["number"], 16) > end:
            return
        for low in range(start, end + 1, 2000):
            debits = self.rpc.call("eth_getLogs", [{"address": USDC,
                "fromBlock": hex(low), "toBlock": hex(min(low + 1999, end)),
                "topics": ["0x" + keccak(text="Transfer(address,address,uint256)").hex(),
                           "0x" + self.wallet[2:].rjust(64, "0")]}])
            for debit in debits:
                if "blockNumber" not in debit:
                    return
                block = self.rpc.call("eth_getBlockByNumber", [debit["blockNumber"], False])
                if int(block["timestamp"], 16) <= position["ending"] + 300:
                    return
        if self.balance(op["share_token"]) != 0:
            return
        self.ledger.transition(position["id"], "BUY_UNKNOWN", "EXPIRED", now,
            error="BUY_NOT_OBSERVED: expired Round; no share receipts or wallet USDC debits during expired Round")


    def settlement(self, position, now):
        if now < position["ending"] + 300:
            return None
        op = self.op(position, "buy")
        winner = self.rpc.view(position["pool"], "details(bytes8)", ("bytes8",),
                    (bytes.fromhex(position["outcome"][2:]),),
                    ("uint256", "uint256", "uint256", "bytes8"), "latest")[3]
        winner = "0x" + winner.hex()
        if winner == op["outcome_up"]:
            return "UP"
        if winner == op["outcome_down"]:
            return "DOWN"
        return None

    def submit_redeem(self, position):
        if self.op(position, "redeem"):
            return
        buy = self.op(position, "buy")
        if self.settlement(position, int(time.time())) != position["side"]:
            raise ValueError("claim delay not elapsed or latest winner not confirmed")
        if self.balance(buy["share_token"]) != position["shares"]:
            raise ValueError("share balance changed; reconcile manual/automatic claim")
        unsigned = claim_transaction(self.wallet, position["pool"])
        # The signing payload uses integer chainId; JSON-RPC TransactionArgs expects
        # hex quantities. Chain is pinned by preflight/signing, so omit it for calls.
        rpc_tx = {key: value for key, value in unsigned.items() if key != "chainId"}
        simulation = self.rpc.call("eth_call", [rpc_tx, "latest"])
        payouts = decode(("uint256[]",), bytes.fromhex(simulation[2:]))[0]
        if len(payouts) != 1 or payouts[0] <= 0:
            raise ValueError("claim simulation returns no payout")
        gas = (int(self.rpc.call("eth_estimateGas", [rpc_tx]), 16) * 120 + 99) // 100
        gas_price = int(self.rpc.call("eth_gasPrice", []), 16) * 2
        if gas_price <= 0 or gas <= 0 or gas * gas_price > self.gas_cap:
            raise ValueError("claim exceeds gas cap")
        if int(self.rpc.call("eth_getBalance", [self.wallet, "latest"]), 16) < gas * gas_price:
            raise ValueError("insufficient ETH")
        nonce = int(self.rpc.call("eth_getTransactionCount", [self.wallet, "pending"]), 16)
        latest = int(self.rpc.call("eth_getTransactionCount", [self.wallet, "latest"]), 16)
        if nonce != latest:
            raise ValueError("wallet already has pending transactions")
        tx = {"chainId": 42161, "to": to_checksum_address(CLAIMANT), "value": 0,
              "data": unsigned["data"], "nonce": nonce, "gas": gas, "gasPrice": gas_price}
        signed = self.account.sign_transaction(tx)
        tx_hash, raw = "0x" + signed.hash.hex(), "0x" + signed.raw_transaction.hex()
        # Persist signed hash/bytes BEFORE broadcasting. A crash never signs a new nonce.
        with self.ledger.conn:
            self.ledger.conn.execute("INSERT INTO live_ops(position_id,operation,request,share_token,tx_hash,raw_tx) VALUES (?,'redeem',?,?,?,?)",
                                    (position["id"], json.dumps(tx), buy["share_token"], tx_hash, raw))
        returned = self.rpc.call("eth_sendRawTransaction", [raw])
        if hex_value(returned, 32) != tx_hash:
            raise ValueError("unexpected broadcast hash; reconcile persisted transaction")

    def lookup_redeem(self, position):
        return self._receipt(position, "redeem")

    def _receipt(self, position, operation, candidate=None):
        op = candidate or self.op(position, operation)
        if not op or not op["tx_hash"]:
            return None
        r = self.rpc.mined(op["tx_hash"])
        if r is None:
            return None
        if hex_value(r["transactionHash"], 32) != op["tx_hash"]:
            raise ValueError("wrong receipt")
        timestamp = r["_canonical_timestamp"]
        if timestamp < position["created_at"] - 5:
            raise ValueError("receipt predates this intent")
        if operation == "buy" and timestamp > position["ending"]:
            raise ValueError("buy receipt belongs to an expired Round")
        if r["status"] == "0x0":
            self.stop("transaction reverted; inspect before further trading")
            return Receipt(False, 0, 0, op["tx_hash"])
        if self.mapping(position["pool"], position["outcome"], r["blockNumber"]) != op["share_token"]:
            raise ValueError("share mapping changed")
        report = inspect_receipt(r, op["tx_hash"], self.wallet, position["pool"], op["share_token"],
                                 "buy" if operation == "buy" else "claim")
        # Bind pool event to the exact outcome and recipient as well as token transfers.
        signature = ("SharesMinted(bytes8,uint256,address,address,uint256)" if operation == "buy"
                     else "PayoffActivated(bytes8,uint256,address,address,uint256)")
        if operation == "buy":
            matches = [log for log in r["logs"] if log["address"].lower() == position["pool"].lower()
                and len(log["topics"]) == 4 and log["topics"][0].lower() == "0x" + keccak(text=signature).hex()
                and log["topics"][1].lower() == "0x" + position["outcome"][2:].lower().ljust(64, "0")]
            if len(matches) != 1:
                raise ValueError("no matching SharesMinted event")
            recipient, spent = decode(("address", "uint256"), bytes.fromhex(matches[0]["data"][2:]))
            if recipient.lower() != self.wallet or int(matches[0]["topics"][2], 16) != report["shares_minted_raw"]:
                raise ValueError("mint event recipient/shares mismatch")
        with self.ledger.conn:
            self.ledger.conn.execute("UPDATE live_ops SET gas_wei=? WHERE position_id=? AND operation=?",
                                     (report["gas_wei"], position["id"], operation))
        shares = report["shares_minted_raw" if operation == "buy" else "shares_burned_raw"]
        amount = report["usdc_out_micro" if operation == "buy" else "usdc_in_micro"]
        if operation == "buy" and shares < position["minimum_shares"]:
            self.stop("actual fill below quoted minimum; acquired shares still reconciled")
        if operation == "redeem" and shares != position["shares"]:
            raise ValueError("claim burned a different position size")
        return Receipt(True, shares, amount, op["tx_hash"])

    def attach_buy(self, identity, tx_hash):
        position = self.ledger.get(identity)
        if not position or position["state"] not in ("BUY_PENDING", "BUY_UNKNOWN"):
            raise ValueError("expected a pending buy")
        op = self.op(position, "buy")
        if not op or op["tx_hash"]:
            raise ValueError("only an unknown API acknowledgement can be attached")
        tx_hash = hex_value(tx_hash, 32)
        if self.ledger.conn.execute("SELECT 1 FROM live_ops WHERE tx_hash=?", (tx_hash,)).fetchone():
            raise ValueError("hash already used")
        receipt = self._receipt(position, "buy", {**op, "tx_hash": tx_hash})
        if receipt is None or not receipt.success:
            raise ValueError("hash must prove a canonical mined matching purchase")
        with self.ledger.conn:
            self.ledger.conn.execute("UPDATE live_ops SET tx_hash=? WHERE position_id=? AND operation='buy' AND tx_hash IS NULL",
                                    (tx_hash, identity))

    def attach_claim(self, identity, tx_hash):
        """Adopt a claim made outside the runner, once the chain proves it.

        submit_redeem refuses while the share balance disagrees with the position and
        tells you to reconcile, but there was no way to. Meanwhile the Round sits in
        REDEEM_PENDING holding the entry slot, and retry_claim would rebroadcast bytes
        that can no longer succeed — a reverted receipt halts the runner outright.

        Nothing is taken on trust. The hash must mine a canonical claim that burned this
        position's shares and paid this wallet, which is the same proof the runner
        demands of its own transaction.
        """
        position = self.ledger.get(identity)
        if not position or position["state"] != "REDEEM_PENDING":
            raise ValueError("expected a pending claim")
        buy = self.op(position, "buy")
        if not buy:
            raise ValueError("no purchase on record to match a claim against")
        tx_hash = hex_value(tx_hash, 32)
        existing = self.op(position, "redeem")
        if existing and existing["tx_hash"]:
            if existing["tx_hash"] == tx_hash:
                raise ValueError("this claim is already attached")
            # Replacing a hash that did mine would discard a real receipt.
            if self.rpc.mined(existing["tx_hash"]) is not None:
                raise ValueError("the recorded claim already mined; let the runner read it")
        elif self.ledger.conn.execute(
                "SELECT 1 FROM live_ops WHERE tx_hash = ?", (tx_hash,)).fetchone():
            raise ValueError("hash already used")
        receipt = self._receipt(position, "redeem",
                               {**(existing or {"share_token": buy["share_token"]}),
                                "tx_hash": tx_hash})
        if receipt is None or not receipt.success:
            raise ValueError("hash must prove a canonical mined matching claim")
        if receipt.shares != position["shares"] or receipt.amount <= 0:
            raise ValueError("claim burned a different position size")
        with self.ledger.conn:
            if existing:
                self.ledger.conn.execute(
                    "UPDATE live_ops SET tx_hash = ?, raw_tx = NULL WHERE position_id = ?"
                    " AND operation = 'redeem'", (tx_hash, identity))
            else:
                self.ledger.conn.execute(
                    "INSERT INTO live_ops(position_id,operation,request,share_token,tx_hash)"
                    " VALUES (?,'redeem',?,?,?)",
                    (identity, json.dumps({"attached": "manual claim"}),
                     buy["share_token"], tx_hash))
        return receipt

    def retry_claim(self, identity):
        position = self.ledger.get(identity)
        if not position or position["state"] != "REDEEM_PENDING":
            raise ValueError("expected a pending claim")
        op = self.op(position, "redeem")
        if op:
            if not op["raw_tx"]:
                raise ValueError("missing signed transaction")
            # Same bytes, same nonce, same hash. Never sign a replacement here.
            returned = self.rpc.call("eth_sendRawTransaction", [op["raw_tx"]])
            if hex_value(returned, 32) != op["tx_hash"]:
                raise ValueError("broadcast hash mismatch")
        else:
            # No live_ops record proves submit_redeem never reached broadcasting.
            self.ledger.transition(identity, "REDEEM_PENDING", "REDEEM_READY", int(time.time()), error=None)

    def valid_receipt(self, receipt, operation, position):
        op = self.op(position, "buy" if operation == "buy" else "redeem")
        if not isinstance(receipt, Receipt) or not op or receipt.reference != op["tx_hash"]:
            return False
        if not receipt.success:
            return receipt.shares == receipt.amount == 0
        return receipt.shares > 0 and (receipt.amount == position["amount"] if operation == "buy"
                    else receipt.shares == position["shares"] and receipt.amount > 0)
