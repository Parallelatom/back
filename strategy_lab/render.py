"""Turning the recordings into a page a person can read at a glance.

Two things here are deliberate rather than decorative. Curves break across stretches with
no recordings, because a straight line drawn over an outage reads as a quiet market and is
the easiest way to fool yourself. And Hit Rate is placed ahead of Bankroll, because the
markets are too thin for the money column to survive a real order (ADR-0003).
"""
from __future__ import annotations

import html
import sqlite3
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Sequence, Tuple

from . import sources
from .replay import ALL_STRATEGIES, STARTING_BANKROLL, replay

DISPLAY_OFFSET_HOURS = 7
DISPLAY_ZONE = timezone(timedelta(hours=DISPLAY_OFFSET_HOURS))

# Rounds settle every 900 seconds. Anything wider than a couple of those is an absence.
GAP_SECONDS = 2 * 900

BREAK_EVEN_HIT_RATE = 1 / 1.314422

# Enough to check the scoring by eye without turning the page into a data dump.
RECENT_ROUNDS = 50

# A week is enough to see an edge closing without the table becoming a spreadsheet.
PERIODS_SHOWN = 7

# Everything older than that, rolled into one column rather than dropped. The recent days
# only mean something against a baseline, and the baseline is the stretch before them.
EARLIER = "Earlier"

COLOURS = {
    "Delta Edge": "#2563eb",
    "Lock Rider": "#0891b2",
    "Contrarian Fill": "#ea580c",
    "Always Up": "#16a34a",
    "Always Down": "#dc2626",
    "Flip Follow": "#9333ea",
}

CHART_WIDTH = 520
CHART_HEIGHT = 260
PADDING = 44


def format_number(value: Optional[float]) -> str:
    """A price as written, without inventing or losing digits.

    Not %g: at six significant figures that quietly rounds a BTC price, and a Strike that
    is off by a unit changes which Side won.
    """
    if value is None:
        return "—"
    return f"{value:.6f}".rstrip("0").rstrip(".") or "0"


def format_time(moment: int) -> str:
    return datetime.fromtimestamp(moment, DISPLAY_ZONE).strftime("%d %b %H:%M")


def day_of(moment: int) -> str:
    return _date_of(moment).strftime("%d %b")


def _date_of(moment: int):
    return datetime.fromtimestamp(moment, DISPLAY_ZONE).date()


def periods_table(results, strategies):
    """Each Strategy's record broken out by the day its Rounds settled.

    A running total is the wrong shape for the question that matters here. An edge in this
    market closes when other people start taking it, and when that happens the lifetime
    average keeps looking healthy for a long time while every recent day is a loss.
    """
    seen = set()
    cells = {}
    for strategy in strategies:
        for trade in results[strategy.name].trades:
            date = _date_of(trade.round_ending)
            seen.add(date)
            day = day_of(trade.round_ending)
            won, count = cells.setdefault(strategy.name, {}).get(day, (0, 0))
            cells[strategy.name][day] = (won + (1 if trade.won else 0), count + 1)
    if not seen:
        return [], cells
    # A continuous run of days, so a day nothing was recorded on shows as a gap rather
    # than vanishing — the same reason the Bankroll curves break across an outage.
    span = (max(seen) - min(seen)).days
    days = [
        (min(seen) + timedelta(days=offset)).strftime("%d %b")
        for offset in range(span + 1)
    ]
    recent = days[-PERIODS_SHOWN:]
    older = days[:-PERIODS_SHOWN]
    if not older:
        return recent, cells

    for record in cells.values():
        totals = [record[day] for day in older if day in record]
        if totals:
            record[EARLIER] = (
                sum(won for won, _ in totals),
                sum(count for _, count in totals),
            )
    return [EARLIER] + recent, cells


def segments(
    points: Sequence[Tuple[int, float]],
    timeline: Sequence[int],
) -> List[List[Tuple[int, float]]]:
    """Split a curve wherever the recordings stop, so the drawing never spans an outage.

    `timeline` is every Round that was recorded, which is what decides whether two trades
    have a hole between them. Judging by the distance between the trades themselves would
    break the line every time a Strategy declined to act — making selectivity look
    identical to a Collector that had died, which is exactly the confusion the break was
    introduced to prevent.
    """
    recorded = sorted(timeline)
    found: List[List[Tuple[int, float]]] = []
    for point in points:
        if found and _recorded_throughout(found[-1][-1][0], point[0], recorded):
            found[-1].append(point)
        else:
            found.append([point])
    return found


def _recorded_throughout(earlier: int, later: int, recorded: Sequence[int]) -> bool:
    """Whether Rounds were recorded continuously between two moments."""
    left = bisect_right(recorded, earlier)
    right = bisect_left(recorded, later)
    span = [earlier] + list(recorded[left:right]) + [later]
    return all(b - a <= GAP_SECONDS for a, b in zip(span, span[1:]))


def _chart(symbol: str, results, span: Optional[Tuple[int, int]], timeline) -> str:
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
        for piece in segments(curve, timeline):
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


def _round_filters(include_stale: bool, include_partial: bool) -> str:
    return (
        "WHERE winner IS NOT NULL AND COALESCE(unsettled, 0) = 0"
        + ("" if include_partial else " AND COALESCE(partial, 0) = 0")
        + ("" if include_stale else " AND COALESCE(oracle_stale, 0) = 0")
    )


def _query(include_stale: bool, include_partial: bool, page: Optional[int] = None) -> str:
    """The page's own address, so a link never silently drops the reader's other choices."""
    parts = []
    if include_stale:
        parts.append("stale=on")
    if include_partial:
        parts.append("partial=on")
    if page and page > 1:
        parts.append(f"page={page}")
    return f"/?{'&'.join(parts)}" if parts else "/"


def _pager(page: int, pages: int, total: int, first: int, last: int,
           include_stale: bool, include_partial: bool) -> str:
    if pages <= 1:
        return ""
    links = []
    if page > 1:
        links.append(
            f'<a class="page" href="{_query(include_stale, include_partial, page - 1)}">Newer</a>'
        )
    if page < pages:
        links.append(
            f'<a class="page" href="{_query(include_stale, include_partial, page + 1)}">Older</a>'
        )
    return (
        f'<p class="pager"><span class="meta">Showing {first}-{last} of {total}'
        f" · page {page} of {pages}</span>{''.join(links)}</p>"
    )


def _recent_rounds(conn, entries, include_stale: bool, include_partial: bool,
                   page: int = 1) -> str:
    """The last few Rounds, one per line, so the scoring can be checked rather than trusted.

    Charts hide arithmetic mistakes well. A Round showing its Strike, its close, who won and
    who entered does not.
    """
    where = _round_filters(include_stale, include_partial)
    total = conn.execute(f"SELECT COUNT(*) FROM rounds {where}").fetchone()[0]
    if not total:
        return '<p class="meta">No settled Rounds recorded yet.</p>'

    pages = max(1, -(-total // RECENT_ROUNDS))
    # A page past the end lands on the last one rather than on nothing: the list shortens
    # whenever a filter changes, and an empty screen reads as lost data.
    page = min(max(page, 1), pages)
    offset = (page - 1) * RECENT_ROUNDS

    rows = conn.execute(
        f"SELECT symbol, ending, strike, final_price, winner, source, oracle_stale, partial"
        f"  FROM rounds {where} ORDER BY ending DESC LIMIT ? OFFSET ?",
        (RECENT_ROUNDS, offset),
    ).fetchall()

    lines = []
    for row in rows:
        took = entries.get((row["symbol"], row["ending"]), [])
        entered = " ".join(
            f'<span class="tag" style="border-color:{COLOURS[name]}">'
            f"{html.escape(name)} {side}</span>"
            for name, side in took
        ) or '<span class="meta">none</span>'
        flags = []
        if row["source"] == "reconstructed":
            flags.append("rebuilt")
        if row["oracle_stale"]:
            flags.append("stale")
        if row["partial"]:
            flags.append("partial")
        close = format_number(row["final_price"])
        lines.append(
            f"<tr><td>{html.escape(format_time(row['ending']))}</td>"
            f"<td>{html.escape(row['symbol'])}</td>"
            f'<td class="num">{format_number(row["strike"])}</td>'
            f'<td class="num">{close}</td>'
            f"<td>{html.escape(row['winner'])}</td>"
            f"<td>{entered}</td>"
            f'<td class="meta">{" ".join(flags)}</td></tr>'
        )
    pager = _pager(page, pages, total, offset + 1, offset + len(rows),
                   include_stale, include_partial)
    return (
        '<table class="rounds"><thead><tr><th>Settled</th><th>Symbol</th>'
        "<th class='num'>Strike</th><th class='num'>Close</th><th>Won</th>"
        "<th>Entered</th><th></th></tr></thead>"
        f"<tbody>{''.join(lines)}</tbody></table>{pager}"
    )


def _toggles(include_stale: bool, include_partial: bool) -> str:
    def link(label: str, on: bool, key: str) -> str:
        wanted = {"stale": include_stale, "partial": include_partial}
        wanted[key] = not on
        # Deliberately back to the first page: changing a filter shortens the list, and
        # staying on page nine of a list that now has three would show nothing at all.
        href = _query(wanted["stale"], wanted["partial"])
        state = "on" if on else "off"
        return f'<a class="toggle {state}" href="{href}">{label}: {state}</a>'

    return (
        '<p class="toggles">'
        + link("Oracle Stale Rounds", include_stale, "stale")
        + link("Partial Rounds", include_partial, "partial")
        + "</p>"
    )


def _periods_section(results_by_symbol, symbols=None) -> str:
    panels = []
    for symbol in (sources.ALL_SYMBOLS if symbols is None else symbols):
        results = results_by_symbol[symbol]
        days, cells = periods_table(results, ALL_STRATEGIES)
        if not days:
            continue
        header = "".join(f'<th class="num">{html.escape(day)}</th>' for day in days)
        rows = []
        for strategy in ALL_STRATEGIES:
            record = cells.get(strategy.name, {})
            if not record:
                continue
            columns = []
            for day in days:
                if day not in record:
                    columns.append('<td class="num meta">—</td>')
                    continue
                won, count = record[day]
                rate = won / count
                weak = "" if rate >= BREAK_EVEN_HIT_RATE else " meta"
                columns.append(
                    f'<td class="num{weak}">{rate:.0%} <span class="meta">({count})</span></td>'
                )
            rows.append(
                f'<tr><td><span class="dot" style="background:{COLOURS[strategy.name]}">'
                f"</span>{html.escape(strategy.name)}</td>{''.join(columns)}</tr>"
            )
        panels.append(
            f"<h3>{html.escape(symbol)}</h3>"
            f'<div class="scroller"><table class="periods">'
            f"<thead><tr><th>Strategy</th>{header}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if not panels:
        return '<p class="meta">No settled Rounds recorded yet.</p>'
    return "".join(panels)


def _pricing_alarm(conn) -> str:
    """Say loudly when the local pricing rule last disagreed with the contract.

    A disagreement left in a log is found weeks later, by which time it has skewed every
    figure computed since.
    """
    row = conn.execute(
        "SELECT ts, local_shares, chain_shares, local_fees, chain_fees FROM quote_checks "
        " WHERE agrees IS NOT NULL ORDER BY ts DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return ""
    latest = conn.execute(
        "SELECT agrees FROM quote_checks WHERE agrees IS NOT NULL ORDER BY ts DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if latest["agrees"]:
        return ""
    return (
        '<p class="alarm">The pricing rule and the contract <strong>disagree</strong>. '
        f"Checked {html.escape(format_time(row['ts']))}: a 1 USD ticket comes to "
        f"{row['local_shares']} shares here and {row['chain_shares']} on chain, with fees "
        f"of {row['local_fees']} against {row['chain_fees']}. Every Fill computed since is "
        "suspect until this is understood.</p>"
    )


def _venue_tabs(panels_by_venue, results_by_symbol) -> str:
    """One tab per venue, switched by CSS alone.

    The page is a static file served from a read-only connection, and a tab that needs
    JavaScript to reveal what is already in the document is a way for the reading to fail
    silently. Radio inputs cannot: with styles off, every venue is simply visible.
    """
    venues = list(panels_by_venue)
    inputs, labels, panels, rules = [], [], [], []
    for index, venue in enumerate(venues):
        ident = f"venue-{index}"
        checked = " checked" if index == 0 else ""
        inputs.append(f'<input class="venue-radio" type="radio" name="venue" id="{ident}"{checked}>')
        labels.append(f'<label class="venue-tab" for="{ident}">{html.escape(venue)}</label>')
        panels.append(
            f'<div class="venue-panel">'
            f"{_venue_note(venue)}"
            f"<main>{''.join(panels_by_venue[venue]) or _NO_VENUE_DATA}</main>"
            f'<section class="wide"><h2>How it is holding up</h2>'
            f'<p class="meta">Hit Rate by the day a Round settled, with the number of Paper'
            f" Trades behind it. A lifetime average stays healthy for a long time after an"
            f" edge has closed; a row of recent days does not. Days below the"
            f" {BREAK_EVEN_HIT_RATE:.1%} break-even are dimmed.</p>"
            f"{_periods_section(results_by_symbol, sources.symbols_of(venue))}</section></div>"
        )
        rules.append(
            f"#{ident}:checked ~ .venue-bar label[for={ident}] "
            "{ color: var(--ink); border-color: var(--muted); background: var(--panel); }"
            f"\n  #{ident}:checked ~ .venue-panel:nth-of-type({index + 1}) "
            "{ display: block; }"
        )
    bar = f'<nav class="venue-bar">{"".join(labels)}</nav>'
    return (f'<style>\n  {chr(10).join("  " + rule for rule in rules).strip()}\n</style>'
            f'<div class="venues">{"".join(inputs)}{bar}{"".join(panels)}</div>')


def _venue_note(venue: str) -> str:
    note = sources.VENUE_NOTES.get(venue)
    if not note:
        return ""
    return (f'<section class="wide"><p class="caveat">{html.escape(note)}</p></section>')


_NO_VENUE_DATA = ('<section><p class="meta">Nothing recorded for this venue yet.</p></section>')


def render_page(
    conn: sqlite3.Connection,
    include_stale: bool = False,
    include_partial: bool = False,
    page: int = 1,
) -> str:
    panels_by_venue = {venue: [] for venue in sources.VENUES}
    entries = {}
    results_by_symbol = {}
    for symbol in sources.ALL_SYMBOLS:
        results = replay(conn, symbol=symbol, strategies=ALL_STRATEGIES,
                         include_stale=include_stale, include_partial=include_partial)
        results_by_symbol[symbol] = results
        for name, result in results.items():
            for trade in result.trades:
                entries.setdefault((symbol, trade.round_ending), []).append((name, trade.side))
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
        timeline = [
            row["ending"]
            for row in conn.execute(
                """
                SELECT ending FROM rounds
                 WHERE symbol = ? AND winner IS NOT NULL AND COALESCE(unsettled, 0) = 0
                 ORDER BY ending
                """,
                (symbol,),
            )
        ]
        drawn = [point for result in results.values() for point in result.curve]
        span = (min(p[0] for p in drawn), max(p[0] for p in drawn)) if drawn else None
        panels_by_venue[sources.venue_of(symbol)].append(
            f'<section><h2>{html.escape(symbol)}</h2>'
            f'<p class="meta">{recorded} Rounds recorded · {scoreable} scoreable</p>'
            f"{_table(symbol, results, rebuilt, scoreable)}"
            f"{_chart(symbol, results, span, timeline)}</section>"
        )
    return _DOCUMENT.format(
        venues=_venue_tabs(panels_by_venue, results_by_symbol),
        alarm=_pricing_alarm(conn),
        toggles=_toggles(include_stale, include_partial),
        rounds=_recent_rounds(conn, entries, include_stale, include_partial, page),
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
  .alarm {{ max-width: 1140px; margin: 14px auto 0; padding: 10px 14px; border-radius: 8px;
            background: #7f1d1d; color: #fee2e2; font-size: 13px; }}
  .caveat {{ font-size: 12px; color: var(--warn); background: var(--warn-bg);
             border-radius: 6px; padding: 8px 10px; margin: 0 0 12px; }}
  section.wide {{ max-width: 1140px; margin: 20px auto 0; }}
  .toggles {{ display: flex; gap: 8px; margin: 0 0 12px; }}
  .toggle {{ font-size: 12px; text-decoration: none; padding: 4px 10px; border-radius: 999px;
             border: 1px solid var(--line); color: var(--muted); }}
  .toggle.on {{ color: var(--ink); border-color: var(--muted); }}
  .pager {{ display: flex; align-items: center; gap: 10px; margin: 12px 0 0; }}
  .pager .meta {{ flex: 1; margin: 0; }}
  .page {{ font-size: 12px; text-decoration: none; padding: 4px 12px; border-radius: 6px;
           border: 1px solid var(--line); color: var(--ink); }}
  .rounds td, .rounds th {{ font-size: 12px; }}
  h3 {{ font-size: 13px; margin: 14px 0 6px; }}
  .periods td, .periods th {{ font-size: 12px; }}
  .periods tbody td:first-child, .periods thead th:first-child {{ min-width: 130px; }}
  .periods td:not(:first-child), .periods th:not(:first-child) {{ min-width: 78px; }}
  .scroller {{ overflow-x: auto; }}
  .tag {{ display: inline-block; font-size: 11px; padding: 1px 6px; margin-right: 4px;
          border: 1px solid; border-radius: 4px; }}
  .venue-radio {{ position: absolute; opacity: 0; pointer-events: none; }}
  .venue-bar {{ max-width: 1140px; margin: 18px auto 0; padding: 0 20px; display: flex;
                gap: 8px; flex-wrap: wrap; }}
  .venue-tab {{ font-size: 13px; padding: 6px 14px; border-radius: 8px; cursor: pointer;
                border: 1px solid var(--line); color: var(--muted); }}
  .venue-radio:focus-visible + .venue-bar .venue-tab {{ outline: 2px solid var(--muted); }}
  .venue-panel {{ display: none; }}
  @media (prefers-reduced-motion: no-preference) {{ .venue-tab {{ transition: color .1s; }} }}
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
{alarm}
{venues}
<section class="wide">
  <h2>Recent Rounds</h2>
  <p class="meta">Settled Rounds, newest first, so the scoring can be checked against
     individual Rounds rather than taken on trust.</p>
  {toggles}
  {rounds}
</section>
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
