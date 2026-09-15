"""The live feeds, kept as thin as they can be.

Everything below this module is network; everything above it is the Ingest seam. These
adapters translate and do not decide, so that the decisions stay testable without a socket.
"""
from __future__ import annotations

import json
import logging
import random
from typing import Any, Dict, Mapping, Optional

import requests

from .ingest import RoundMeta

log = logging.getLogger(__name__)

GRAPH_URL = "https://9lives-arb.stellate.sh"
WS_URL = "wss://arb-websocket.9lives.so"
CATEGORY = "15mins"
SYMBOLS = ("BTC", "XYZCL")

WS_ORIGIN = "https://9lives.so"
PRICES_TABLE = "oracles_ninelives_prices_2"

_ROUND_QUERY = """
query ($symbol: String!, $category: String!) {
  campaignBySymbol(symbol: $symbol, category: $category) {
    poolAddress
    starting
    ending
    priceMetadata { baseAsset priceTargetForUp }
    outcomes { name identifier }
  }
}
"""


def subscription(symbol: str) -> Dict[str, Any]:
    field = [{"name": "base", "filter_constraints": {"et": symbol}}]
    table = [{"table": PRICES_TABLE, "fields": field}]
    return {"label": symbol, "ask_for_snapshot": table, "add": table}


class Graph:
    """Reads Round metadata. Deliberately slow: the endpoint does no edge caching and
    answers in about 1.2 seconds, so polling harder buys nothing and risks the one thing
    that cannot be recovered — collection uptime."""

    def __init__(self, url: str = GRAPH_URL, timeout: float = 15.0):
        self.url = url
        self.timeout = timeout
        self.session = requests.Session()

    def query(self, query: str, variables: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            response = self.session.post(
                self.url,
                json={"query": query, "variables": dict(variables)},
                headers={"content-type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (requests.RequestException, ValueError) as exc:
            log.warning("graph query failed: %s", exc)
            return None
        if body.get("errors"):
            log.warning("graph returned errors: %s", body["errors"])
        return body.get("data") or None

    def current_round(self, symbol: str) -> Optional[RoundMeta]:
        data = self.query(_ROUND_QUERY, {"symbol": symbol, "category": CATEGORY})
        if not data:
            return None
        return parse_round(symbol, data.get("campaignBySymbol"))


def parse_round(symbol: str, campaign: Any) -> Optional[RoundMeta]:
    """A campaign as the API returns it, or None if it is the empty stub."""
    if not isinstance(campaign, Mapping):
        return None
    ending = campaign.get("ending")
    if not ending:  # the defunct-category stub comes back as {"ending": 0}
        return None
    metadata = campaign.get("priceMetadata") or {}
    strike_raw = metadata.get("priceTargetForUp")
    up = down = None
    for outcome in campaign.get("outcomes") or ():
        name = (outcome.get("name") or "").lower()
        if "above" in name:
            up = outcome.get("identifier")
        elif "below" in name:
            down = outcome.get("identifier")
    if strike_raw is None or up is None or down is None:
        return None
    try:
        strike = float(strike_raw)
        ending = int(ending)
        starting = int(campaign.get("starting") or 0)
    except (TypeError, ValueError):
        return None
    return RoundMeta(
        symbol=symbol,
        starting=starting,
        ending=ending,
        strike=strike,
        pool_address=campaign.get("poolAddress") or "",
        outcome_up=up,
        outcome_down=down,
    )


def jittered(seconds: float, spread: float = 0.3) -> float:
    return seconds * (1.0 + random.uniform(-spread, spread))


def decode(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    if not isinstance(raw, str):
        return None
    try:
        message = json.loads(raw)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None
