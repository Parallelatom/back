"""Turning the recordings into a page a person can read at a glance.

Two things here are deliberate rather than decorative. Curves break across stretches with
no recordings, because a straight line drawn over an outage reads as a quiet market and is
the easiest way to fool yourself. And Hit Rate is placed ahead of Bankroll, because the
markets are too thin for the money column to survive a real order (ADR-0003).
"""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from . import sources
from .replay import ALL_STRATEGIES, STARTING_BANKROLL, replay

DISPLAY_OFFSET_HOURS = 7
DISPLAY_ZONE = timezone(timedelta(hours=DISPLAY_OFFSET_HOURS))

# Rounds settle every 900 seconds. Anything wider than a couple of those is an absence.
GAP_SECONDS = 2 * 900

BREAK_EVEN_HIT_RATE = 1 / 1.314422

COLOURS = {
    "Delta Edge": "#2563eb",
    "Always Up": "#16a34a",
    "Always Down": "#dc2626",
    "Flip Follow": "#9333ea",
}

CHART_WIDTH = 520
CHART_HEIGHT = 260
PADDING = 44


def format_time(moment: int) -> str:
    return datetime.fromtimestamp(moment, DISPLAY_ZONE).strftime("%d %b %H:%M")


def segments(points: Sequence[Tuple[int, float]]) -> List[List[Tuple[int, float]]]:
    """Split a curve wherever the recordings stop, so the drawing never spans a gap."""
    found: List[List[Tuple[int, float]]] = []
    for point in points:
        if found and point[0] - found[-1][-1][0] <= GAP_SECONDS:
            found[-1].append(point)
        else:
            found.append([point])
    return found


def _chart(symbol: str, results, span: Optional[Tuple[int, int]]) -> str:
    curves = {name: result.curve for name, result in results.items()}
    drawn = [point for curve in curves.values() for point in curve]
    if not drawn or span is None:
        return (
            f'<div class="empty" style="height:{CHART_HEIGHT}px">'
            f"No scoreable Rounds for {html.escape(symbol)} yet</div>"
        )

    first, last = span
    lowest = min(min(value for _, value in drawn), STARTING_BANKROLL)
    highest = max(max(value for _, value in drawn), STARTING_BANKROLL)
    if highest - lowest < 1:
        lowest, highest = lowest - 1, highest + 1

    def x(moment: int) -> float:
        width = max(last - first, 1)
        return PADDING + (moment - first) / width * (CHART_WIDTH - PADDING * 2)

    def y(value: float) -> float:
        height = max(highest - lowest, 1e-9)
        return CHART_HEIGHT - PADDING - (value - lowest) / height * (CHART_HEIGHT - PADDING * 2)

    parts = [
        f'<svg viewBox="0 0 {CHART_WIDTH} {CHART_HEIGHT}" role="img" '
        f'aria-label="Bankroll over time for {html.escape(symbol)}">'
    ]
    baseline = y(STARTING_BANKROLL)
    parts.append(
        f'<line class="axis" x1="{PADDING}" y1="{baseline:.1f}" '
        f'x2="{CHART_WIDTH - PADDING}" y2="{baseline:.1f}" stroke-dasharray="4 4"/>'
    )
    parts.append(
        f'<text class="tick" x="{CHART_WIDTH - PADDING}" y="{baseline - 6:.1f}" '
        f'text-anchor="end">starting capital {STARTING_BANKROLL:.0f}</text>'
    )
    for value in (lowest, highest):
        parts.append(
            f'<text class="tick" x="{PADDING - 6}" y="{y(value) + 4:.1f}" '
            f'text-anchor="end">{value:.2f}</text>'
        )
    for moment, anchor in ((first, "start"), (last, "end")):
        parts.append(
            f'<text class="tick" x="{x(moment):.1f}" y="{CHART_HEIGHT - PADDING + 18}" '
            f'text-anchor="{anchor}">{html.escape(format_time(moment))}</text>'
        )

    for name, curve in curves.items():
        colour = COLOURS.get(name, "#64748b")
        for piece in segments(curve):
            if len(piece) == 1:
                moment, value = piece[0]
                parts.append(
                    f'<circle cx="{x(moment):.1f}" cy="{y(value):.1f}" r="2.5" fill="{colour}"/>'
                )
                continue
            path = " ".join(f"{x(m):.1f},{y(v):.1f}" for m, v in piece)
            parts.append(f'<polyline points="{path}" fill="none" stroke="{colour}" stroke-width="2"/>')
    parts.append("</svg>")
    return "".join(parts)


def _table(symbol: str, results, rebuilt: int, scoreable: int) -> str:
    rows = []
    for strategy in ALL_STRATEGIES:
        result = results[strategy.name]
        rate = "—" if result.hit_rate is None else f"{result.hit_rate:.1%}"
        note = ""
        if result.ruined_at is not None:
            note = "ruined"
        elif result.hit_rate is not None and result.hit_rate >= BREAK_EVEN_HIT_RATE:
            note = "above break-even"
        rows.append(
            f'<tr><td><span class="dot" style="background:{COLOURS[strategy.name]}"></span>'
            f"{html.escape(strategy.name)}</td>"
            f'<td class="num strong">{rate}</td>'
            f'<td class="num">{len(result.trades)}</td>'
            f'<td class="num">{result.bankroll:.2f}</td>'
            f"<td>{note}</td></tr>"
        )
    caveat = ""
    if rebuilt and scoreable and rebuilt >= scoreable:
        caveat = (
            '<p class="caveat">Every scored Round here was rebuilt from the price feed, '
            "which keeps no record of the pool. Each is priced as though nobody had traded "
            "it, an even 0.50 a Side, which flatters any Strategy that backs the favourite.</p>"
        )
    return (
        "<table><thead><tr><th>Strategy</th><th class='num'>Hit Rate</th>"
        "<th class='num'>Trades</th><th class='num'>Bankroll</th><th></th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>{caveat}"
    )


def render_page(conn: sqlite3.Connection) -> str:
    panels = []
    for symbol in sources.SYMBOLS:
        results = replay(conn, symbol=symbol, strategies=ALL_STRATEGIES)
        scoreable = max((len(r.trades) for r in results.values()), default=0)
        recorded = conn.execute(
            "SELECT COUNT(*) FROM rounds WHERE symbol = ?", (symbol,)
        ).fetchone()[0]
        rebuilt = conn.execute(
            """
            SELECT COUNT(*) FROM rounds
             WHERE symbol = ? AND source = 'reconstructed' AND winner IS NOT NULL
               AND COALESCE(partial, 0) = 0 AND COALESCE(oracle_stale, 0) = 0
               AND COALESCE(unsettled, 0) = 0
            """,
            (symbol,),
        ).fetchone()[0]
        drawn = [point for result in results.values() for point in result.curve]
        span = (min(p[0] for p in drawn), max(p[0] for p in drawn)) if drawn else None
        panels.append(
            f'<section><h2>{html.escape(symbol)}</h2>'
            f'<p class="meta">{recorded} Rounds recorded · {scoreable} scoreable</p>'
            f"{_table(symbol, results, rebuilt, scoreable)}"
            f"{_chart(symbol, results, span)}</section>"
        )
    return _DOCUMENT.format(
        panels="".join(panels),
        break_even=f"{BREAK_EVEN_HIT_RATE:.1%}",
        generated=html.escape(format_time(int(datetime.now(timezone.utc).timestamp()))),
        offset=DISPLAY_OFFSET_HOURS,
    )


_DOCUMENT = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>9lives Strategy Lab</title>
<style>
  :root {{
    --ink: #0f172a; --muted: #64748b; --line: #e2e8f0; --ground: #f8fafc;
    --panel: #ffffff; --warn: #92400e; --warn-bg: #fffbeb;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ink: #e2e8f0; --muted: #94a3b8; --line: #1e293b; --ground: #0b1220;
      --panel: #111827; --warn: #fcd34d; --warn-bg: #1f1707;
    }}
  }}
  body {{ margin: 0; background: var(--ground); color: var(--ink);
         font: 14px/1.5 ui-sans-serif, system-ui, -apple-system, sans-serif; }}
  header, main, footer {{ max-width: 1140px; margin: 0 auto; padding: 0 20px; }}
  header {{ padding-top: 28px; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  h2 {{ font-size: 16px; margin: 0 0 2px; }}
  .meta, .sub {{ color: var(--muted); font-size: 12px; margin: 0 0 14px; }}
  main {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; padding-top: 18px; }}
  @media (max-width: 880px) {{ main {{ grid-template-columns: 1fr; }} }}
  section {{ background: var(--panel); border: 1px solid var(--line);
             border-radius: 10px; padding: 16px; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 10px; }}
  th, td {{ padding: 6px 8px; border-bottom: 1px solid var(--line); text-align: left;
            white-space: nowrap; }}
  th {{ font-size: 11px; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }}
  .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  .strong {{ font-weight: 650; }}
  .dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%;
          margin-right: 7px; vertical-align: middle; }}
  svg {{ width: 100%; height: auto; }}
  .axis {{ stroke: var(--muted); stroke-width: 1; opacity: .5; }}
  .tick {{ fill: var(--muted); font-size: 10px; }}
  .empty {{ display: flex; align-items: center; justify-content: center;
            color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; }}
  .caveat {{ font-size: 12px; color: var(--warn); background: var(--warn-bg);
             border-radius: 6px; padding: 8px 10px; margin: 0 0 12px; }}
  footer {{ padding: 20px 20px 40px; color: var(--muted); font-size: 12px; }}
  footer strong {{ color: var(--ink); }}
</style>
</head>
<body>
<header>
  <h1>9lives Strategy Lab</h1>
  <p class="sub">Paper trading only — no order is ever placed. Times in UTC+{offset}.
     Generated {generated}.</p>
</header>
<main>{panels}</main>
<footer>
  <p><strong>Hit Rate is the finding.</strong> A 1 USD ticket is most of a typical Round's
     volume, so the Bankroll column assumes prices no real order of that size would get.
     A win returns about +0.31 against a loss of 1.00, so break-even needs a Hit Rate of
     {break_even}.</p>
  <p>Curves break wherever nothing was recorded. A line drawn across an outage would look
     like a quiet market.</p>
  <p><strong>On changing the rules:</strong> adjusting a threshold until a curve looks good
     is fitting, not discovery. A Strategy is worth believing when it was chosen before the
     data was seen, not after.</p>
</footer>
</body>
</html>
"""
