"""Seam 1: recorded Rounds in, Paper Trades and Hit Rates out.

A pure function over recordings. Every Strategy rule lives behind this seam, and so does
the pricing, which is why the chain-verified fill numbers are asserted here too.
"""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta
from strategy_lab.replay import ALWAYS_DOWN, ALWAYS_UP, STAKE, STARTING_BANKROLL, replay

BTC = "BTC"
OIL = "XYZCL"
GRID = 900
T0 = 1789443000
POOL = "0xpool"
UP = "0xup"
DOWN = "0xdown"


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def a_round(ingest, symbol=BTC, ending=T0 + GRID, strike=100.0, close=101.0,
            winner=None, pool=None, stale=False, partial=False, unsettled=False):
    """A fully covered, settled Round unless told otherwise."""
    pool = pool or f"{POOL}{symbol}{ending}"
    ingest.observe_round(
        RoundMeta(symbol=symbol, starting=ending - GRID, ending=ending, strike=strike,
                  pool_address=pool, outcome_up=UP, outcome_down=DOWN),
        now=ending - GRID,
    )
    ts = ending - GRID
    while ts <= ending:
        ingest.observe_price(symbol, price=(strike if ts < ending else close), ts=ts)
        ts += 5
    ingest.finalise_round(symbol, ending)
    if winner is None:
        winner = "UP" if close > strike else "DOWN"
    ingest.conn.execute(
        """UPDATE rounds SET winner = ?, final_price = ?, oracle_stale = ?, partial = ?,
                             unsettled = ? WHERE symbol = ? AND ending = ?""",
        (None if unsettled else winner, close, 1 if stale else 0, 1 if partial else 0,
         1 if unsettled else None, symbol, ending),
    )
    ingest.conn.commit()
    return pool


def scored(ingest, symbol=BTC, strategies=(ALWAYS_UP, ALWAYS_DOWN)):
    return replay(ingest.conn, symbol=symbol, strategies=list(strategies))


class TestEntering:
    def test_a_baseline_takes_a_paper_trade_on_every_eligible_round(self, ingest):
        for n in range(3):
            a_round(ingest, ending=T0 + (n + 1) * GRID)

        assert len(scored(ingest)[ALWAYS_UP.name].trades) == 3

    def test_a_baseline_enters_at_the_start_of_the_trade_window(self, ingest):
        a_round(ingest)

        (trade,) = scored(ingest)[ALWAYS_UP.name].trades
        assert trade.entered_at == T0 + GRID - 300

    def test_both_baselines_enter_the_same_rounds_on_opposite_sides(self, ingest):
        a_round(ingest)

        results = scored(ingest)
        assert results[ALWAYS_UP.name].trades[0].side == "UP"
        assert results[ALWAYS_DOWN.name].trades[0].side == "DOWN"

    def test_only_the_requested_symbol_is_scored(self, ingest):
        a_round(ingest, symbol=BTC)
        a_round(ingest, symbol=OIL, strike=98.0, close=99.0)

        assert len(scored(ingest, symbol=BTC)[ALWAYS_UP.name].trades) == 1


class TestExcluding:
    def test_a_partial_round_is_not_traded(self, ingest):
        a_round(ingest, partial=True)

        assert scored(ingest)[ALWAYS_UP.name].trades == []

    def test_an_oracle_stale_round_is_not_traded(self, ingest):
        a_round(ingest, stale=True)

        assert scored(ingest)[ALWAYS_UP.name].trades == []

    def test_an_unsettled_round_is_not_traded(self, ingest):
        a_round(ingest, unsettled=True)

        assert scored(ingest)[ALWAYS_UP.name].trades == []

    def test_excluded_rounds_do_not_count_towards_the_hit_rate(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)        # UP wins
        a_round(ingest, ending=T0 + 2 * GRID, close=99.0, stale=True)  # would lose

        assert scored(ingest)[ALWAYS_UP.name].hit_rate == 1.0


class TestPricing:
    def test_a_ticket_on_an_untouched_round_gets_what_the_chain_gives(self, ingest):
        a_round(ingest)

        (trade,) = scored(ingest)[ALWAYS_UP.name].trades
        assert trade.shares == 1_314_422

    def test_the_price_paid_is_the_fill_not_the_marginal_price(self, ingest):
        a_round(ingest)

        (trade,) = scored(ingest)[ALWAYS_UP.name].trades
        assert trade.marginal_price == 0.5
        assert trade.price_per_share == pytest.approx(0.7608, abs=1e-4)

    def test_a_win_returns_about_a_third_more_than_it_risked(self, ingest):
        a_round(ingest, close=101.0)  # UP wins

        (trade,) = scored(ingest)[ALWAYS_UP.name].trades
        assert trade.won is True
        assert trade.pnl == pytest.approx(0.314422, abs=1e-6)

    def test_a_loss_costs_the_whole_stake(self, ingest):
        a_round(ingest, close=99.0)  # UP loses

        (trade,) = scored(ingest)[ALWAYS_UP.name].trades
        assert trade.won is False
        assert trade.pnl == -STAKE

    def test_a_round_someone_already_traded_fills_worse(self, ingest):
        """Reserves at the moment of entry, not the opening ones."""
        pool = a_round(ingest)
        ingest.apply_remote_reserves(pool, up=168_578, down=1_483_000, ts=T0 + 100)

        (trade,) = scored(ingest)[ALWAYS_UP.name].trades
        assert trade.marginal_price == pytest.approx(0.897929, abs=1e-6)
        assert trade.shares < 1_314_422

    def test_reserves_recorded_after_the_entry_moment_are_not_used(self, ingest):
        pool = a_round(ingest)
        ingest.apply_remote_reserves(pool, up=168_578, down=1_483_000, ts=T0 + GRID - 10)

        (trade,) = scored(ingest)[ALWAYS_UP.name].trades
        assert trade.shares == 1_314_422


class TestBankroll:
    def test_a_strategy_starts_with_the_agreed_capital(self, ingest):
        assert scored(ingest)[ALWAYS_UP.name].bankroll == STARTING_BANKROLL

    def test_a_win_and_a_loss_move_the_bankroll_by_the_right_amounts(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)      # UP wins
        a_round(ingest, ending=T0 + 2 * GRID, close=99.0)   # UP loses

        result = scored(ingest)[ALWAYS_UP.name]
        assert result.bankroll == pytest.approx(10 + 0.314422 - 1.0, abs=1e-6)

    def test_the_curve_records_the_bankroll_after_every_trade(self, ingest):
        for n in range(3):
            a_round(ingest, ending=T0 + (n + 1) * GRID, close=101.0)

        assert len(scored(ingest)[ALWAYS_UP.name].curve) == 3

    def test_a_strategy_that_runs_out_of_money_stops_trading(self, ingest):
        for n in range(15):
            a_round(ingest, ending=T0 + (n + 1) * GRID, close=99.0)  # UP loses every time

        result = scored(ingest)[ALWAYS_UP.name]
        assert len(result.trades) == 10
        assert result.bankroll == 0
        assert result.ruined_at == T0 + 10 * GRID

    def test_ruin_on_one_symbol_leaves_the_other_alone(self, ingest):
        for n in range(15):
            a_round(ingest, symbol=BTC, ending=T0 + (n + 1) * GRID, close=99.0)
            a_round(ingest, symbol=OIL, ending=T0 + (n + 1) * GRID, strike=98.0, close=99.0)

        assert scored(ingest, symbol=BTC)[ALWAYS_UP.name].ruined_at is not None
        assert scored(ingest, symbol=OIL)[ALWAYS_UP.name].ruined_at is None

    def test_a_strategy_never_stakes_more_than_it_holds(self, ingest):
        for n in range(15):
            a_round(ingest, ending=T0 + (n + 1) * GRID, close=99.0)

        assert scored(ingest)[ALWAYS_UP.name].bankroll >= 0


class TestHitRate:
    def test_hit_rate_is_the_share_of_trades_that_picked_the_winner(self, ingest):
        a_round(ingest, ending=T0 + GRID, close=101.0)      # UP wins
        a_round(ingest, ending=T0 + 2 * GRID, close=101.0)  # UP wins
        a_round(ingest, ending=T0 + 3 * GRID, close=99.0)   # UP loses

        assert scored(ingest)[ALWAYS_UP.name].hit_rate == pytest.approx(2 / 3)

    def test_the_two_baselines_are_exact_mirrors(self, ingest):
        for n, close in enumerate((101.0, 99.0, 101.0, 99.0, 99.0)):
            a_round(ingest, ending=T0 + (n + 1) * GRID, close=close)

        results = scored(ingest)
        assert results[ALWAYS_UP.name].hit_rate + results[ALWAYS_DOWN.name].hit_rate == 1.0

    def test_a_strategy_that_never_traded_has_no_hit_rate(self, ingest):
        assert scored(ingest)[ALWAYS_UP.name].hit_rate is None

    def test_breaking_even_needs_about_three_quarters_of_trades_to_win(self, ingest):
        """The arithmetic that makes Hit Rate the headline: +0.31 against -1.00."""
        for n in range(13):
            a_round(ingest, ending=T0 + (n + 1) * GRID, close=101.0 if n < 10 else 99.0)

        result = scored(ingest)[ALWAYS_UP.name]
        assert result.hit_rate == pytest.approx(10 / 13, abs=0.01)
        assert result.bankroll == pytest.approx(STARTING_BANKROLL, abs=0.15)
