"""Results broken out by day, so a Strategy that stops working shows it.

A single running total hides the moment an edge closes: hundreds of good Rounds keep the
average up long after the thing stopped paying. The market's own participants are what
would close it, and they are already visible in the data.
"""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta
from strategy_lab.render import EARLIER, PERIODS_SHOWN, day_of, periods_table, render_page
from strategy_lab.replay import ALWAYS_UP, replay

BTC = "BTC"
GRID = 900
DAY = 86400
# 1789443000 is 15 Sep 03:30 UTC, which is 10:30 on the 15th in UTC+7.
T0 = 1789443000


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def a_round(ingest, ending, close, strike=100.0, symbol=BTC):
    ingest.observe_round(
        RoundMeta(symbol=symbol, starting=ending - GRID, ending=ending, strike=strike,
                  pool_address=f"0xp{ending}", outcome_up="0xup", outcome_down="0xdown"),
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


def rows_for(ingest, strategies=(ALWAYS_UP,)):
    results = replay(ingest.conn, symbol=BTC, strategies=list(strategies))
    return periods_table(results, list(strategies))


class TestGroupingByDay:
    def test_a_round_belongs_to_the_day_it_settled_in_the_readers_zone(self):
        assert day_of(T0) == "15 Sep"

    def test_a_round_late_in_the_utc_day_still_belongs_to_the_local_one(self):
        """20:00 UTC is 03:00 the next morning in UTC+7, and that is the day to show."""
        assert day_of(T0 + 16 * 3600 + 30 * 60) == "16 Sep"

    def test_trades_are_counted_against_their_own_day(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)          # wins, day one
        a_round(ingest, ending=T0 + DAY + GRID, close=99.0)     # loses, day two

        days, cells = rows_for(ingest)
        assert days == ["15 Sep", "16 Sep"]
        assert cells[ALWAYS_UP.name]["15 Sep"][:2] == (1, 1)
        assert cells[ALWAYS_UP.name]["16 Sep"][:2] == (0, 1)

    def test_a_day_the_strategy_sat_out_has_no_entry(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)
        a_round(ingest, ending=T0 + 2 * DAY + GRID, close=101.0)

        days, cells = rows_for(ingest)
        assert "16 Sep" in days
        assert "16 Sep" not in cells[ALWAYS_UP.name]

    def test_days_read_oldest_to_newest(self, ingest):
        for n in range(3):
            a_round(ingest, ending=T0 + n * DAY + GRID, close=101.0)

        days, _ = rows_for(ingest)
        assert days == ["15 Sep", "16 Sep", "17 Sep"]

    def test_only_a_weeks_worth_of_dated_columns_are_kept(self, ingest):
        for n in range(PERIODS_SHOWN + 4):
            a_round(ingest, ending=T0 + n * DAY + GRID, close=101.0)

        days, _ = rows_for(ingest)
        assert len([d for d in days if d != EARLIER]) == PERIODS_SHOWN

    def test_the_days_kept_are_the_latest_ones(self, ingest):
        for n in range(PERIODS_SHOWN + 2):
            a_round(ingest, ending=T0 + n * DAY + GRID, close=101.0)

        days, _ = rows_for(ingest)
        assert days[-1] == day_of(T0 + (PERIODS_SHOWN + 1) * DAY + GRID)


class TestOnThePage:
    def test_the_section_appears(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)

        assert "holding up" in render_page(ingest.conn).lower()

    def test_a_days_hit_rate_is_shown_with_its_trade_count(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)
        a_round(ingest, ending=T0 + 2 * GRID, close=99.0)

        page = render_page(ingest.conn)
        assert "50%" in page
        assert "(2)" in page

    def test_nothing_is_claimed_before_there_is_anything_to_claim(self, ingest):
        page = render_page(ingest.conn)

        assert "No settled Rounds recorded yet." in page

    def test_the_quality_toggles_reach_it_too(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)
        a_round(ingest, ending=T0 + 2 * GRID, close=99.0)
        ingest.conn.execute(
            "UPDATE rounds SET oracle_stale = 1 WHERE ending = ?", (T0 + 2 * GRID,))
        ingest.conn.commit()

        assert "100%" in render_page(ingest.conn)
        assert "50%" in render_page(ingest.conn, include_stale=True)


class TestWhenTheDaysPileUp:
    """Seven columns is all that fits, but dropping what came before loses the baseline
    the recent days are supposed to be compared against."""

    def a_run(self, ingest, days, close=101.0):
        for n in range(days):
            a_round(ingest, ending=T0 + n * DAY + GRID, close=close)

    def test_a_short_run_needs_no_summary_column(self, ingest):
        self.a_run(ingest, PERIODS_SHOWN)

        days, _ = rows_for(ingest)
        assert EARLIER not in days

    def test_a_long_run_keeps_the_older_days_as_one_column(self, ingest):
        self.a_run(ingest, PERIODS_SHOWN + 3)

        days, _ = rows_for(ingest)
        assert days[0] == EARLIER
        assert len(days) == PERIODS_SHOWN + 1

    def test_the_summary_counts_every_older_trade(self, ingest):
        self.a_run(ingest, PERIODS_SHOWN + 3)

        _, cells = rows_for(ingest)
        assert cells[ALWAYS_UP.name][EARLIER][:2] == (3, 3)

    def test_the_summary_mixes_wins_and_losses_from_the_whole_stretch(self, ingest):
        for n in range(PERIODS_SHOWN + 4):
            a_round(ingest, ending=T0 + n * DAY + GRID, close=101.0 if n % 2 else 99.0)

        _, cells = rows_for(ingest)
        won, count, _pnl = cells[ALWAYS_UP.name][EARLIER]
        assert count == 4
        assert won == 2

    def test_the_recent_days_are_still_the_latest_ones(self, ingest):
        self.a_run(ingest, PERIODS_SHOWN + 3)

        days, _ = rows_for(ingest)
        assert days[-1] == day_of(T0 + (PERIODS_SHOWN + 2) * DAY + GRID)

    def test_a_strategy_with_nothing_older_shows_a_gap_there(self, ingest):
        self.a_run(ingest, PERIODS_SHOWN + 2)
        # A Strategy that only ever traded on the final day.
        from strategy_lab.replay import ALWAYS_DOWN

        days, cells = rows_for(ingest, strategies=(ALWAYS_UP, ALWAYS_DOWN))
        assert EARLIER in days
        assert cells[ALWAYS_DOWN.name].get(EARLIER, (0, 0, 0.0))[1] > 0  # it traded then too

    def test_the_page_labels_the_summary_column(self, ingest):
        self.a_run(ingest, PERIODS_SHOWN + 2)

        assert EARLIER in render_page(ingest.conn)
