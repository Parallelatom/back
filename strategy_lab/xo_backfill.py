"""Backfill XO Market's settled Pulse Rounds into the recordings.

XO publishes every cycle it has ever settled, which is the opposite of the 9lives feed and
the reason this is a backfill and not a Collector: there is nothing to miss by being late.

What arrives is thin. A cycle carries its opening price and its closing TWAP and nothing
between them, so two observations are written per Round and no more. That is deliberate:
a Strategy that needs to watch a price move will correctly find nothing here rather than
being handed an invented path between two real numbers.

Read the resolution before reading the Hit Rates. A Pulse cycle asks whether the next
five-minute Chainlink TWAP lands above the previous one, so its Strike is the last cycle's
closing TWAP, not a spot price. It is a different question from the 9lives Rounds sitting
beside it on the page, and the two columns should not be read as a like-for-like race.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Dict, Optional

from . import sources
from .db import connect, initialise
from .version import code_version

PAGE_SIZE = 100
USER_AGENT = "strategy-lab-backfill"
RETRIES = 4


def _epoch(stamp: Optional[str]) -> Optional[int]:
    if not stamp:
        return None
    return int(datetime.fromisoformat(stamp.replace("Z", "+00:00"))
               .astimezone(timezone.utc).timestamp())


def fetch_page(config_id: int, page: int, base: str = sources.XO_API,
               page_size: int = PAGE_SIZE) -> Dict:
    """One page of settled cycles. Retries, because a backfill that dies at page 200 of
    357 leaves a hole that looks exactly like a quiet market."""
    query = urllib.parse.urlencode({"marketConfigId": config_id, "page": page,
                                    "limit": page_size})
    request = urllib.request.Request(f"{base}/api/pulse/markets?{query}",
                                     headers={"User-Agent": USER_AGENT,
                                              "Accept": "application/json"})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, OSError, ValueError):
            if attempt == RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def winner_of(row: Dict) -> Optional[str]:
    """The venue names its own outcomes; do not assume 0 means Up."""
    outcome = row.get("winningOutcome")
    if outcome is None:
        return None
    for candidate in (row.get("marketMetadata") or {}).get("outcomes") or ():
        if candidate.get("id") == outcome:
            title = str(candidate.get("title", "")).upper()
            return title if title in ("UP", "DOWN") else None
    return None


def usable(row: Dict) -> bool:
    return (row.get("status") == "closed" and winner_of(row) is not None
            and row.get("openingPrice") is not None and row.get("closingPrice") is not None
            and _epoch(row.get("startsAt")) is not None
            and _epoch(row.get("expiresAt")) is not None)


def store(conn, symbol: str, rows, version: str, now: Optional[int] = None) -> int:
    """Write whole Rounds, or none of them. Returns how many were new."""
    now = int(time.time()) if now is None else now
    written = 0
    with conn:
        for row in rows:
            if not usable(row):
                continue
            starting, ending = _epoch(row["startsAt"]), _epoch(row["expiresAt"])
            if ending <= starting:
                continue
            strike, final = float(row["openingPrice"]), float(row["closingPrice"])
            # Two observations is all there is, so say so rather than letting a Round that
            # never moved read as a healthy one.
            distinct = 1 if strike == final else 2
            changed = conn.execute(
                """INSERT OR IGNORE INTO rounds
                   (symbol, ending, starting, strike, first_seen_at, last_seen_at, source,
                    partial, oracle_stale, unsettled, winner, final_price, settled_at,
                    settled_source, tick_count, distinct_price_count, price_min, price_max,
                    code_version)
                   VALUES (?,?,?,?,?,?,'backfill',0,?,0,?,?,?,'xo-pulse-api',2,?,?,?,?)""",
                (symbol, ending, starting, strike, now, now, 1 if distinct == 1 else 0,
                 winner_of(row), final, _epoch(row.get("resolvedAt")) or ending,
                 distinct, min(strike, final), max(strike, final), version),
            ).rowcount
            if not changed:
                continue
            # The chain of cycles shares its boundaries: one cycle's close is the next
            # one's open, at the same second and the same number.
            conn.executemany(
                "INSERT OR IGNORE INTO oracle_prices (symbol, ts, price, code_version) "
                "VALUES (?,?,?,?)",
                ((symbol, starting, strike, version), (symbol, ending, final, version)),
            )
            written += 1
        # Recording a price asks the Collector to re-summarise the Rounds around it, and a
        # cycle's open is the previous cycle's close, so each insert clears the neighbour's
        # count as well. Nothing here needs re-deriving: restate what the venue published.
        conn.execute(
            """UPDATE rounds
                  SET tick_count = 2,
                      distinct_price_count = CASE WHEN strike = final_price THEN 1 ELSE 2 END
                WHERE symbol = ? AND source = 'backfill' AND distinct_price_count IS NULL""",
            (symbol,),
        )
    return written


def backfill(conn, symbol: str, pages: Optional[int] = None, base: str = sources.XO_API,
             fetch=fetch_page, log=print) -> int:
    config_id = sources.XO_MARKET_CONFIG[symbol]
    first = fetch(config_id, 1, base)
    total_pages = first.get("meta", {}).get("totalPages", 1)
    last = total_pages if pages is None else min(pages, total_pages)
    written = store(conn, symbol, first.get("data", ()), code_version())
    log(f"{symbol} page 1/{last}: {written} new")
    for page in range(2, last + 1):
        new = store(conn, symbol, fetch(config_id, page, base).get("data", ()),
                    code_version())
        written += new
        log(f"{symbol} page {page}/{last}: {new} new")
    return written


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recordings", default="data/lab.db")
    parser.add_argument("--symbol", default=sources.XO_SYMBOLS[0],
                        choices=sources.XO_SYMBOLS)
    parser.add_argument("--pages", type=int, default=None,
                        help="stop after this many pages; the default reads them all")
    args = parser.parse_args()
    if args.pages is not None and args.pages < 1:
        raise SystemExit("--pages must be at least 1")
    conn = connect(args.recordings)
    try:
        initialise(conn)
        written = backfill(conn, args.symbol, args.pages)
        total = conn.execute("SELECT COUNT(*) FROM rounds WHERE symbol = ?",
                             (args.symbol,)).fetchone()[0]
        print(f"{written} new Rounds; {total} recorded for {args.symbol}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
