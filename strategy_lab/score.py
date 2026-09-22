"""Print what the recordings say, for reading in a terminal.

Hit Rate is shown first and Bankroll second, deliberately. The markets are thin enough that
a 1 USD ticket is most of a Round's volume, so the money column is indicative and the
accuracy column is the finding (ADR-0003).
"""
from __future__ import annotations

import os
import sys

from . import sources
from .db import connect
from .replay import ALL_STRATEGIES, STARTING_BANKROLL, replay

BREAK_EVEN_HIT_RATE = 1 / 1.314422  # +0.314 on a win against -1.00 on a loss


def render(conn, strategies=None) -> str:
    strategies = list(strategies if strategies is not None else ALL_STRATEGIES)
    lines = []
    for symbol in sources.SYMBOLS:
        results = replay(conn, symbol=symbol, strategies=strategies)
        available = conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE symbol = ?", (symbol,)
        ).fetchone()[0]
        scoreable = max((len(r.trades) for r in results.values()), default=0)
        rebuilt = conn.execute(
            """
            SELECT COUNT(*) FROM rounds
             WHERE symbol = ? AND source = 'reconstructed'
               AND winner IS NOT NULL AND COALESCE(partial, 0) = 0
               AND COALESCE(oracle_stale, 0) = 0 AND COALESCE(unsettled, 0) = 0
            """,
            (symbol,),
        ).fetchone()[0]
        lines.append(f"\n{symbol}  ({available} Rounds recorded, {scoreable} scoreable)")
        lines.append(f"  {'Strategy':<16}{'Hit Rate':>10}{'Trades':>9}{'Bankroll':>11}   Note")
        for strategy in strategies:
            result = results[strategy.name]
            rate = "  —" if result.hit_rate is None else f"{result.hit_rate:.1%}"
            note = ""
            if result.ruined_at is not None:
                note = f"ruined at Round {result.ruined_at}"
            elif result.trades:
                line = ("" if result.break_even is None
                        else f" (break-even {result.break_even:.1%})")
                note = ("paid" if result.profit > 0 else
                        "level" if result.profit == 0 else "did not pay") + line
            lines.append(
                f"  {strategy.name:<16}{rate:>10}{len(result.trades):>9}"
                f"{result.bankroll:>10.2f}   {note}"
            )
        if rebuilt and scoreable and rebuilt >= scoreable:
            lines.append(
                f"  ! every scored {symbol} Round was rebuilt from the price feed, which"
                f" carries no\n    record of the pool. Each is therefore priced as though"
                f" nobody had traded it —\n    an even 0.50 a Side. That flatters any"
                f" Strategy that backs the favourite."
            )
    lines.append(
        f"\nStake {1.0:.2f} per Round from {STARTING_BANKROLL:.2f}. A win returns the shares"
        " bought and a loss costs 1.00,\nso break-even moves with the price each Fill was"
        " made at and is shown per Strategy.\nHit Rate is the finding; Bankroll is indicative."
    )
    return "\n".join(lines)


def main() -> None:
    path = os.environ.get("STRATEGY_LAB_DB", "data/lab.db")
    if not os.path.exists(path):
        sys.exit(f"no recordings at {path}; run the Collector first")
    print(render(connect(path)))


if __name__ == "__main__":
    main()
