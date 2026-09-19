"""The live feeds, kept as thin as they can be.

Everything below this module is network; everything above it is the Ingest seam. These
adapters translate and do not decide, so that the decisions stay testable without a socket.
"""
from __future__ import annotations

import json
import logging
import math
import random
from typing import Any, Dict, Mapping, Optional

import requests

from .ingest import RoundMeta

log = logging.getLogger(__name__)

GRAPH_URL = "https://9lives-arb.stellate.sh"
WS_URL = "wss://arb-websocket.9lives.so"
CATEGORY = "15mins"
SYMBOLS = ("BTC", "XYZCL")

# Which venue each Symbol trades on. A second venue is a second entry here and an adapter
# below; nothing above the Ingest seam should have to learn a venue's name to work.
VENUES = {"9lives": SYMBOLS}


def symbols_of(venue: str):
    return VENUES[venue]


def venue_of(symbol: str) -> str:
    for venue, symbols in VENUES.items():
        if symbol in symbols:
            return venue
    raise KeyError(symbol)

WS_ORIGIN = "https://9lives.so"
PRICES_TABLE = "oracles_ninelives_prices_2"
# Keyed by pool rather than Symbol, so they are subscribed whole and filtered on arrival.
POOL_TABLES = ("ninelives_buys_and_sells_1", "ninelives_events_outcome_decided")

_SHARES_QUERY = """
query ($symbol: String!, $category: String!) {
  campaignBySymbol(symbol: $symbol, category: $category) {
    poolAddress
    shares { identifier shares }
    outcomes { name identifier }
  }
}
"""

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


_PAST_ROUNDS_QUERY = """
query ($category: [String!]) {
  campaigns(category: $category, pageSize: 500) {
    starting
    ending
    priceMetadata { baseAsset priceTargetForUp }
  }
}
"""


def subscription(symbol: str) -> Dict[str, Any]:
    field = [{"name": "base", "filter_constraints": {"et": symbol}}]
    table = [{"table": PRICES_TABLE, "fields": field}]
    return {"label": symbol, "ask_for_snapshot": table, "add": table}


def pool_subscription(table_name: str) -> Dict[str, Any]:
    table = [{"table": table_name, "fields": []}]
    return {"label": table_name, "ask_for_snapshot": table, "add": table}


def all_subscriptions():
    """Every subscription the Collector needs, each to be carried on its own connection.

    A second subscription to the same table on one connection replaces the first: sending
    BTC and then XYZCL over a single socket delivers XYZCL's snapshot and then only XYZCL's
    updates, in silence. The original bot opened a connection per symbol and so never met
    this. One connection per subscription is what is known to work.
    """
    return [subscription(symbol) for symbol in SYMBOLS] + [
        pool_subscription(table_name) for table_name in POOL_TABLES
    ]


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


    def current_reserves(self, symbol: str):
        """The exchange's own view of a Round's Reserves: (pool, up, down), or None."""
        data = self.query(_SHARES_QUERY, {"symbol": symbol, "category": CATEGORY})
        return self.reserves_from_campaign((data or {}).get("campaignBySymbol"))

    @staticmethod
    def reserves_from_campaign(campaign):
        """Reserves as the exchange reports them, or None when it has not said.

        An empty `shares` is *not* an untouched pool. The exchange's indexer lags the
        chain, and a live Round that has already been traded reports no shares at all —
        confirmed against a pool the chain priced at 0.039 while this field was empty.
        Reading that as the even opening Reserves would overwrite correct figures with
        wrong ones, which is worse than having no answer.
        """
        if not isinstance(campaign, Mapping) or not campaign.get("poolAddress"):
            return None
        shares = campaign.get("shares") or ()
        if not shares:
            return None
        up_ids = {
            _bare(outcome.get("identifier"))
            for outcome in campaign.get("outcomes") or ()
            if "above" in (outcome.get("name") or "").lower()
        }
        up = down = None
        for share in shares:
            try:
                amount = int(share.get("shares"))
            except (TypeError, ValueError):
                continue
            if _bare(share.get("identifier")) in up_ids:
                up = amount
            else:
                down = amount
        if up is None or down is None:
            return None
        return campaign["poolAddress"], up, down

    def past_rounds(self):
        """Every Round the API still remembers, which is roughly the last 2.5 hours.

        Only useful as a check on reconstruction: it is far too shallow to test a Strategy
        against, which is the whole reason the harness records forward (ADR-0002).
        """
        data = self.query(_PAST_ROUNDS_QUERY, {"category": [CATEGORY]})
        if not data:
            return []
        found = []
        for campaign in data.get("campaigns") or ():
            metadata = (campaign or {}).get("priceMetadata") or {}
            symbol = metadata.get("baseAsset")
            strike = metadata.get("priceTargetForUp")
            ending = campaign.get("ending")
            if not (symbol and strike and ending):
                continue
            try:
                found.append((symbol, int(ending), float(strike)))
            except (TypeError, ValueError):
                continue
        return found


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
    if not math.isfinite(strike) or strike <= 0 or not 0 <= starting < ending <= 253402300799:
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


def _bare(identifier: Any) -> str:
    text = str(identifier or "").lower()
    return text[2:] if text.startswith("0x") else text


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
