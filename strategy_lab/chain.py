"""Reading the market's own arithmetic, straight from Arbitrum.

Used sparingly and never in the hot path. The pricing rule is reproduced locally because
that is fast and free; this is how we find out when the local copy has drifted from what
the contract would actually do — for instance if the per-market fee were changed.

The selectors are not the hex in the function names. 9lives mines low-byte selectors to
save calldata gas, so they are computed from the signatures and were confirmed against
mainnet.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

RPC_URL = os.environ.get("STRATEGY_LAB_RPC_URL", "https://arb1.arbitrum.io/rpc")
USER_AGENT = "strategy-lab/1.0"

QUOTE_SELECTOR = "0x00000208"   # quoteC0E17FC7(bytes8,uint256)
PRICE_SELECTOR = "0x000003e0"   # priceA827ED27(bytes8)

SCALE = 1_000_000


@dataclass(frozen=True)
class Quote:
    shares: int
    fees: int
    boosted: int


def _bare(identifier: str) -> str:
    text = str(identifier or "").lower()
    return text[2:] if text.startswith("0x") else text


def encode_quote(outcome: str, gross: int) -> str:
    # bytes8 is left-aligned in its word; a number is right-aligned in its own.
    return f"{QUOTE_SELECTOR}{_bare(outcome):0<64}{gross:064x}"


def encode_price(outcome: str) -> str:
    return f"{PRICE_SELECTOR}{_bare(outcome):0<64}"


def _words(reply, expected: int):
    if not isinstance(reply, str) or not reply.startswith("0x"):
        raise ValueError(f"not an eth_call reply: {reply!r}")
    body = reply[2:]
    if len(body) < expected * 64:
        raise ValueError(f"reply too short: expected {expected} words, got {len(body) // 64}")
    return [int(body[n * 64:(n + 1) * 64], 16) for n in range(expected)]


def decode_quote(reply) -> Quote:
    shares, fees, boosted = _words(reply, 3)
    return Quote(shares=shares, fees=fees, boosted=boosted)


def decode_price(reply) -> Optional[float]:
    """The implied probability, or None for a Round that has already been decided.

    The contract reports zero once an outcome is settled. Zero is not a probability, and
    treating it as one would say every settled Round was a certainty the other way.
    """
    (raw,) = _words(reply, 1)
    return None if raw == 0 else raw / SCALE


class Arbitrum:
    def __init__(self, url: str = RPC_URL, timeout: float = 15.0):
        self.url = url
        self.timeout = timeout

    def call(self, to: str, data: str) -> Optional[str]:
        payload = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        }).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            # The public endpoint refuses the default Python user agent outright.
            headers={"content-type": "application/json", "user-agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log.warning("eth_call failed: %s", exc)
            return None
        if body.get("error"):
            log.warning("eth_call returned an error: %s", body["error"])
            return None
        return body.get("result")

    def quote(self, pool: str, outcome: str, gross: int) -> Optional[Quote]:
        reply = self.call(pool, encode_quote(outcome, gross))
        if reply is None:
            return None
        try:
            return decode_quote(reply)
        except ValueError as exc:
            log.warning("could not read the quote: %s", exc)
            return None

    def price(self, pool: str, outcome: str) -> Optional[float]:
        reply = self.call(pool, encode_price(outcome))
        if reply is None:
            return None
        try:
            return decode_price(reply)
        except ValueError as exc:
            log.warning("could not read the price: %s", exc)
            return None
