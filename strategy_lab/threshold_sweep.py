"""Sweep Delta Edge's threshold over the recordings, for one Symbol.

Read-only. It answers one question: is there any distance-from-Strike at which Delta Edge
clears its break-even Hit Rate, and is that answer stable enough to act on?

Hit Rate is the figure, not the money (ADR-0003). A win pays +0.314 against -1.00 on a
loss, so 76.1% is the line; a threshold that trades often and wins 60% is not a smaller
edge than one that wins 80% twice, it is a losing rule. Every row therefore carries the
trade count and a 95% interval, because an argmax over thresholds on a few hundred Rounds
will find a peak in noise every time.
"""
from __future__ import annotations

import argparse
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from .db import connect_readonly
from .replay import (STAKE, WINDOW_CLOSES, WINDOW_OPENS, _settle, _within_window,
                     delta_edge, load_rounds)
from .score import BREAK_EVEN_HIT_RATE

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def wilson(wins: int, n: int, z: float = 1.96):
    """A 95% interval that stays sane at small n, where wins/n alone does not."""
    if not n:
        return None
    p = wins / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - spread), min(1.0, centre + spread)


def score(records, symbol, threshold):
    """Every trade the rule would have taken, with no bankroll and so no ruin cutoff."""
    strategy = delta_edge({symbol: threshold})
    trades = []
    for record in records:
        entry = strategy.decide(record)
        if entry is None or not _within_window(record, entry.at):
            continue
        trades.append((record.ending, _settle(record, entry)))
    return trades


def summarise(trades):
    wins = sum(1 for _, t in trades if t.won)
    n = len(trades)
    return {"n": n, "wins": wins, "hit": wins / n if n else None,
            "pnl": sum(t.pnl for _, t in trades),
            "interval": wilson(wins, n)}


def half(trades, second):
    """Split chronologically. A real edge survives being cut in two; a fitted one does not."""
    cut = len(trades) // 2
    return summarise(trades[cut:] if second else trades[:cut])


def by_weekday(trades, offset_hours):
    buckets = defaultdict(list)
    tz = timezone(timedelta(hours=offset_hours))
    for ending, trade in trades:
        buckets[datetime.fromtimestamp(ending, tz).weekday()].append((ending, trade))
    return {day: summarise(buckets[index]) for index, day in enumerate(DAYS)}


def _hit(row):
    if row["hit"] is None:
        return "     -  "
    low, high = row["interval"]
    return "%6.1f%%  [%4.1f-%4.1f]" % (row["hit"] * 100, low * 100, high * 100)


def render(conn, symbol, thresholds, offset_hours, include_stale, include_partial):
    records = load_rounds(conn, symbol, include_stale=include_stale,
                          include_partial=include_partial)
    lines = [
        "Delta Edge threshold sweep | %s | %d scoreable Rounds | window %d-%ds before settlement"
        % (symbol, len(records), WINDOW_OPENS, WINDOW_CLOSES),
        "Break-even Hit Rate is %.1f%%: a win pays +%.3f, a loss costs %.2f."
        % (BREAK_EVEN_HIT_RATE * 100, 1 / BREAK_EVEN_HIT_RATE - 1, STAKE),
        "",
        "  thresh   trades  cover     hit rate  [95% CI]      PnL     1st half   2nd half",
    ]
    if not records:
        return "\n".join(lines + ["", "No scoreable Rounds. Nothing can be concluded."])

    scored = {}
    for threshold in thresholds:
        trades = score(records, symbol, threshold)
        scored[threshold] = trades
        row, first, second = summarise(trades), half(trades, False), half(trades, True)
        lines.append("  %6.3f%%  %6d  %5.1f%%  %s  %+7.2f  %9s  %9s" % (
            threshold, row["n"], 100 * row["n"] / len(records), _hit(row), row["pnl"],
            "-" if first["hit"] is None else "%.1f%%" % (first["hit"] * 100),
            "-" if second["hit"] is None else "%.1f%%" % (second["hit"] * 100),
        ))

    clears = [t for t, trades in scored.items()
              if (s := summarise(trades))["interval"] and s["interval"][0] > BREAK_EVEN_HIT_RATE]
    lines += ["", "Thresholds whose 95%% interval sits entirely above break-even: %s"
              % (", ".join("%.3f%%" % t for t in clears) if clears else "none")]

    lines += ["", "Hit Rate by weekday at each threshold (trade count in brackets):",
              "  thresh   " + "  ".join("%12s" % day for day in DAYS)]
    for threshold, trades in scored.items():
        cells = []
        for day in DAYS:
            row = by_weekday(trades, offset_hours)[day]
            cells.append("%12s" % ("-" if row["hit"] is None
                                   else "%.0f%% (%d)" % (row["hit"] * 100, row["n"])))
        lines.append("  %6.3f%%  " % threshold + "  ".join(cells))
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recordings", default="data/lab.db")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--from", dest="start", type=float, default=0.02)
    parser.add_argument("--to", dest="stop", type=float, default=0.30)
    parser.add_argument("--step", type=float, default=0.02)
    parser.add_argument("--utc-offset-hours", type=float, default=7.0,
                        help="which clock decides the weekday; matches the runner's log timezone")
    parser.add_argument("--include-stale", action="store_true")
    parser.add_argument("--include-partial", action="store_true")
    args = parser.parse_args()
    if not 0 < args.step or not 0 < args.start <= args.stop:
        raise SystemExit("thresholds must be positive with --from no greater than --to")
    steps = int(round((args.stop - args.start) / args.step)) + 1
    thresholds = [round(args.start + index * args.step, 6) for index in range(steps)]
    conn = connect_readonly(args.recordings)
    try:
        print(render(conn, args.symbol, thresholds, args.utc_offset_hours,
                     args.include_stale, args.include_partial))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
