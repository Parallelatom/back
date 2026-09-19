"""Ticket 09: what the page must show, and what it must never quietly hide."""
import pytest

from strategy_lab import sources
from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta
from strategy_lab.render import DISPLAY_OFFSET_HOURS, format_time, render_page, segments

BTC = "BTC"
OIL = "XYZCL"
GRID = 900
T0 = 1789443000


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def a_round(ingest, symbol=BTC, ending=T0 + GRID, strike=100.0, close=101.0):
    ingest.observe_round(
        RoundMeta(symbol=symbol, starting=ending - GRID, ending=ending, strike=strike,
                  pool_address=f"0xp{symbol}{ending}", outcome_up="0xup", outcome_down="0xdown"),
        now=ending - GRID,
    )
    ts = ending - GRID
    while ts <= ending:
        ingest.observe_price(symbol, price=(strike if ts < ending else close), ts=ts)
        ts += 5
    ingest.finalise_round(symbol, ending)
    ingest.conn.execute(
        "UPDATE rounds SET winner = ?, final_price = ?, oracle_stale = 0, partial = 0 "
        "WHERE symbol = ? AND ending = ?",
        ("UP" if close > strike else "DOWN", close, symbol, ending),
    )
    ingest.conn.commit()


class TestBreakingTheLineAcrossGaps:
    """A break must mean one thing only: nothing was recorded here.

    A Strategy declining to trade is not a gap in the recordings, and drawing it as one
    would make a selective Strategy look like a broken Collector.
    """

    def timeline(self, count, start=T0):
        return [start + n * GRID for n in range(count)]

    def test_consecutive_rounds_stay_in_one_segment(self):
        points = [(T0 + n * GRID, 10.0) for n in range(4)]

        assert len(segments(points, self.timeline(4))) == 1

    def test_a_missing_stretch_splits_the_curve(self):
        points = [(T0, 10.0), (T0 + GRID, 10.0), (T0 + 20 * GRID, 10.0)]
        recorded = [T0, T0 + GRID, T0 + 20 * GRID]

        assert len(segments(points, recorded)) == 2

    def test_each_side_of_a_gap_keeps_its_own_points(self):
        points = [(T0, 10.0), (T0 + GRID, 9.0), (T0 + 30 * GRID, 8.0), (T0 + 31 * GRID, 7.0)]
        recorded = [p[0] for p in points]

        assert [len(s) for s in segments(points, recorded)] == [2, 2]

    def test_a_strategy_that_skipped_rounds_keeps_one_unbroken_line(self):
        """Delta Edge trades about half the Rounds. Its curve is not full of outages."""
        recorded = self.timeline(10)
        points = [(recorded[n], 10.0) for n in (0, 3, 7, 9)]

        assert len(segments(points, recorded)) == 1

    def test_a_real_outage_still_breaks_a_selective_strategys_line(self):
        recorded = self.timeline(4) + self.timeline(4, start=T0 + 40 * GRID)
        points = [(recorded[0], 10.0), (recorded[2], 10.0), (recorded[5], 10.0)]

        assert len(segments(points, recorded)) == 2

    def test_an_empty_curve_has_no_segments(self):
        assert segments([], []) == []


class TestDisplayedTime:
    def test_times_are_shown_in_the_users_own_zone(self):
        assert DISPLAY_OFFSET_HOURS == 7

    def test_a_timestamp_renders_shifted_by_that_offset(self):
        # 1789443000 is 03:30:00Z, which is 10:30 in UTC+7.
        assert format_time(1789443000).endswith("10:30")


class TestThePage:
    def test_loading_more_rounds_does_not_add_per_round_queries(self, ingest):
        for n in range(10):
            a_round(ingest, ending=T0 + (n + 1) * GRID)
        statements = []
        ingest.conn.set_trace_callback(statements.append)
        page = render_page(ingest.conn)
        ingest.conn.set_trace_callback(None)

        assert "10 Rounds recorded" in page
        assert len(statements) <= 16

    def test_it_names_every_strategy_for_both_symbols(self, ingest):
        a_round(ingest, symbol=BTC)
        a_round(ingest, symbol=OIL, strike=98.0, close=99.0)

        page = render_page(ingest.conn)
        for name in ("Delta Edge", "Always Up", "Always Down", "Flip Follow"):
            assert page.count(name) >= 2

    def test_it_leads_with_hit_rate_rather_than_money(self, ingest):
        a_round(ingest)

        page = render_page(ingest.conn)
        assert page.index("Hit Rate") < page.index("Bankroll")

    def test_btc_is_shown_before_xyzcl(self, ingest):
        a_round(ingest, symbol=BTC)
        a_round(ingest, symbol=OIL, strike=98.0, close=99.0)

        page = render_page(ingest.conn)
        assert page.index(">BTC<") < page.index(">XYZCL<")

    def test_it_draws_the_starting_capital_as_a_reference_line(self, ingest):
        a_round(ingest)

        assert "starting capital" in render_page(ingest.conn).lower()

    def test_it_warns_against_tuning_until_the_curve_looks_good(self, ingest):
        a_round(ingest)

        page = render_page(ingest.conn).lower()
        assert "fitting" in page

    def test_it_renders_with_no_recordings_at_all(self, ingest):
        page = render_page(ingest.conn)

        assert "Always Up" in page

    def test_it_is_a_complete_html_document(self, ingest):
        a_round(ingest)

        page = render_page(ingest.conn)
        assert page.lstrip().startswith("<!doctype html>")
        assert page.rstrip().endswith("</html>")

    def test_a_round_that_was_rebuilt_is_declared_as_such(self, ingest):
        a_round(ingest)
        ingest.conn.execute("UPDATE rounds SET source = 'reconstructed'")
        ingest.conn.commit()

        assert "rebuilt" in render_page(ingest.conn).lower()


class TestVenueTabs:
    """One tab per venue, and no venue's numbers leaking into another's tab."""

    def test_every_venue_gets_a_tab(self, ingest, monkeypatch):
        monkeypatch.setattr(sources, "VENUES", {"9lives": (OIL,), "world.xyz": (BTC,)})
        monkeypatch.setattr(sources, "SYMBOLS", (OIL, BTC))

        page = render_page(ingest.conn)
        assert page.count('class="venue-tab"') == 2
        assert ">9lives</label>" in page and ">world.xyz</label>" in page

    def test_the_first_venue_is_the_one_shown(self, ingest, monkeypatch):
        monkeypatch.setattr(sources, "VENUES", {"9lives": (OIL,), "world.xyz": (BTC,)})
        monkeypatch.setattr(sources, "SYMBOLS", (OIL, BTC))

        page = render_page(ingest.conn)
        assert page.count(" checked>") == 1
        assert 'id="venue-0" checked>' in page

    def test_each_tab_shows_only_its_own_venues_markets(self, ingest, monkeypatch):
        monkeypatch.setattr(sources, "VENUES", {"9lives": (OIL,), "world.xyz": (BTC,)})
        monkeypatch.setattr(sources, "SYMBOLS", (OIL, BTC))
        a_round(ingest, symbol=OIL)

        page = render_page(ingest.conn)
        first = page.split('class="venue-panel"')[1]
        assert f"<h2>{OIL}</h2>" in first and f"<h2>{BTC}</h2>" not in first

    def test_a_venue_with_nothing_recorded_says_so_rather_than_showing_nothing(
            self, ingest, monkeypatch):
        monkeypatch.setattr(sources, "VENUES", {"9lives": (OIL,), "empty": ()})
        monkeypatch.setattr(sources, "SYMBOLS", (OIL,))

        assert "Nothing recorded for this venue yet" in render_page(ingest.conn)

    def test_switching_tabs_needs_no_javascript(self, ingest):
        page = render_page(ingest.conn)
        assert "<script" not in page.lower()
        assert ":checked ~ .venue-panel" in page

    def test_the_holding_up_table_sits_inside_its_venues_tab(self, ingest, monkeypatch):
        monkeypatch.setattr(sources, "VENUES", {"9lives": (OIL,), "world.xyz": (BTC,)})
        monkeypatch.setattr(sources, "SYMBOLS", (OIL, BTC))
        a_round(ingest, symbol=OIL)

        page = render_page(ingest.conn)
        assert page.count("How it is holding up") == 2
        assert page.index('class="venue-panel"') < page.index("How it is holding up")
