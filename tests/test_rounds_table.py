"""Ticket 10: looking at individual Rounds, and choosing what counts."""
import re

import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta
from strategy_lab.render import RECENT_ROUNDS, render_page
from strategy_lab.replay import ALWAYS_UP, replay

BTC = "BTC"
GRID = 900
T0 = 1789443000


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def a_round(ingest, ending=T0 + GRID, strike=100.0, close=101.0, symbol=BTC,
            stale=False, partial=False):
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
        """UPDATE rounds SET winner = ?, final_price = ?, oracle_stale = ?, partial = ?
            WHERE symbol = ? AND ending = ?""",
        ("UP" if close > strike else "DOWN", close, 1 if stale else 0, 1 if partial else 0,
         symbol, ending),
    )
    ingest.conn.commit()


def rounds_table(page):
    """Just the recent-Rounds section, so assertions cannot accidentally match the summary."""
    match = re.search(r'<table class="rounds".*?</table>', page, re.S)
    return match.group(0) if match else ""


class TestTheRoundsTable:
    def test_it_shows_a_rounds_strike_and_settled_close(self, ingest):
        a_round(ingest, strike=77690.5, close=77815.5)

        table = rounds_table(render_page(ingest.conn))
        assert "77690.5" in table
        assert "77815.5" in table

    def test_it_shows_which_side_won(self, ingest):
        a_round(ingest, close=99.0)

        assert "DOWN" in rounds_table(render_page(ingest.conn))

    def test_it_shows_when_the_round_settled(self, ingest):
        a_round(ingest)

        assert "10:45" in rounds_table(render_page(ingest.conn))  # 03:45Z in UTC+7

    def test_it_names_the_strategies_that_entered_and_their_side(self, ingest):
        a_round(ingest, close=101.0)

        table = rounds_table(render_page(ingest.conn))
        assert "Always Up" in table

    def test_it_shows_the_most_recent_rounds_first(self, ingest):
        a_round(ingest, ending=T0 + GRID, strike=100.0, close=101.0)
        a_round(ingest, ending=T0 + 2 * GRID, strike=200.0, close=201.0)

        table = rounds_table(render_page(ingest.conn))
        assert table.index("200") < table.index("100")

    def test_it_stops_at_fifty_rounds(self, ingest):
        for n in range(RECENT_ROUNDS + 12):
            a_round(ingest, ending=T0 + (n + 1) * GRID)

        table = rounds_table(render_page(ingest.conn))
        assert table.count("<tr") == RECENT_ROUNDS + 1  # a header row as well

    def test_both_symbols_appear_in_one_list(self, ingest):
        a_round(ingest, symbol=BTC)
        a_round(ingest, symbol="XYZCL", strike=98.0, close=99.0)

        table = rounds_table(render_page(ingest.conn))
        assert "XYZCL" in table and "BTC" in table


class TestToggles:
    def test_doubtful_rounds_are_hidden_to_begin_with(self, ingest):
        a_round(ingest, ending=T0 + GRID, strike=100.0, close=101.0)
        a_round(ingest, ending=T0 + 2 * GRID, strike=555.0, close=556.0, stale=True)

        assert "555" not in rounds_table(render_page(ingest.conn))

    def test_stale_rounds_appear_once_asked_for(self, ingest):
        a_round(ingest, ending=T0 + 2 * GRID, strike=555.0, close=556.0, stale=True)

        assert "555" in rounds_table(render_page(ingest.conn, include_stale=True))

    def test_partial_rounds_appear_once_asked_for(self, ingest):
        a_round(ingest, ending=T0 + 2 * GRID, strike=777.0, close=778.0, partial=True)

        assert "777" in rounds_table(render_page(ingest.conn, include_partial=True))

    def test_the_two_toggles_are_independent(self, ingest):
        a_round(ingest, ending=T0 + GRID, strike=555.0, close=556.0, stale=True)
        a_round(ingest, ending=T0 + 2 * GRID, strike=777.0, close=778.0, partial=True)

        table = rounds_table(render_page(ingest.conn, include_stale=True))
        assert "555" in table and "777" not in table

    def test_the_page_offers_a_way_to_turn_each_one_on(self, ingest):
        a_round(ingest)

        page = render_page(ingest.conn)
        assert "stale=on" in page
        assert "partial=on" in page


class TestTogglesChangeTheScoring:
    """The toggles would be misleading if they only filtered the table: the headline
    numbers would still be computed over a different set of Rounds than the one on screen."""

    def test_including_stale_rounds_changes_the_hit_rate(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)                 # Always Up wins
        a_round(ingest, ending=T0 + 2 * GRID, close=99.0, stale=True)  # would lose

        tight = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])[ALWAYS_UP.name]
        loose = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP],
                       include_stale=True)[ALWAYS_UP.name]

        assert tight.hit_rate == 1.0
        assert loose.hit_rate == 0.5

    def test_including_partial_rounds_changes_the_trade_count(self, ingest):
        a_round(ingest, ending=T0 + GRID)
        a_round(ingest, ending=T0 + 2 * GRID, partial=True)

        loose = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP],
                       include_partial=True)[ALWAYS_UP.name]

        assert len(loose.trades) == 2

    def test_the_summary_on_the_page_moves_with_the_toggle(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)
        a_round(ingest, ending=T0 + 2 * GRID, close=99.0, stale=True)

        assert "100.0%" in render_page(ingest.conn)
        assert "50.0%" in render_page(ingest.conn, include_stale=True)

    def test_an_unsettled_round_stays_out_whatever_is_toggled(self, ingest):
        """No toggle can conjure a result we never learned."""
        a_round(ingest, ending=T0 + GRID)
        ingest.conn.execute("UPDATE rounds SET winner = NULL, unsettled = 1")
        ingest.conn.commit()

        loose = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP],
                       include_stale=True, include_partial=True)[ALWAYS_UP.name]

        assert loose.trades == []


class TestPrecision:
    def test_a_strike_above_a_hundred_thousand_keeps_its_decimal(self, ingest):
        """Six significant figures is not enough for a BTC price, and a Strike rounded by a
        unit changes which Side won."""
        a_round(ingest, strike=100000.5, close=100001.5)

        assert "100000.5" in rounds_table(render_page(ingest.conn))

    def test_a_close_is_shown_to_the_same_precision(self, ingest):
        a_round(ingest, strike=100000.5, close=100001.5)

        assert "100001.5" in rounds_table(render_page(ingest.conn))


class TestPaging:
    """Fifty Rounds is about half a day. The record is meant to outlive that."""

    def many(self, ingest, count):
        for n in range(count):
            a_round(ingest, ending=T0 + (n + 1) * GRID, strike=1000.0 + n, close=1001.0 + n)

    def strikes_on(self, ingest, **kwargs):
        table = rounds_table(render_page(ingest.conn, **kwargs))
        return re.findall(r'<td class="num">(\d+)</td>', table)[::2]  # strike column only

    def test_the_first_page_holds_the_newest_rounds(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        strikes = self.strikes_on(ingest)
        assert len(strikes) == RECENT_ROUNDS
        assert strikes[0] == str(1000 + RECENT_ROUNDS + 9)

    def test_the_second_page_continues_where_the_first_stopped(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        first = self.strikes_on(ingest, page=1)
        second = self.strikes_on(ingest, page=2)
        assert len(second) == 10
        assert not set(first) & set(second)
        assert int(second[0]) < int(first[-1])

    def test_a_next_link_is_offered_while_there_is_more(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        assert "page=2" in render_page(ingest.conn)

    def test_the_last_page_offers_no_next(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        assert "page=3" not in render_page(ingest.conn, page=2)

    def test_the_first_page_offers_no_previous(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        assert "page=0" not in render_page(ingest.conn)

    def test_a_later_page_can_get_back(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        page = render_page(ingest.conn, page=2)
        newer = re.search(r'<a class="page" href="([^"]*)">Newer</a>', page)
        assert newer, "no way back from page two"
        assert "page=" not in newer.group(1)  # page one needs no number

    def test_no_pager_is_shown_when_everything_fits(self, ingest):
        self.many(ingest, 3)

        assert "page=" not in render_page(ingest.conn)

    def test_the_toggles_survive_turning_the_page(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        page = render_page(ingest.conn, include_stale=True)
        assert "stale=on" in page
        assert re.search(r'href="/\?[^"]*page=2[^"]*"', page)
        link = re.search(r'href="(/\?[^"]*page=2[^"]*)"', page).group(1)
        assert "stale=on" in link

    def test_changing_a_toggle_returns_to_the_first_page(self, ingest):
        """Otherwise a filter that shortens the list drops you past the end of it."""
        self.many(ingest, RECENT_ROUNDS + 10)

        page = render_page(ingest.conn, page=2)
        toggle = re.search(r'class="toggle[^"]*" href="([^"]+)"', page).group(1)
        assert "page=" not in toggle

    def test_a_page_beyond_the_end_shows_the_last_one_rather_than_nothing(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        assert len(self.strikes_on(ingest, page=99)) == 10

    def test_the_range_on_show_is_stated(self, ingest):
        self.many(ingest, RECENT_ROUNDS + 10)

        assert f"1-{RECENT_ROUNDS} of {RECENT_ROUNDS + 10}" in render_page(ingest.conn)
