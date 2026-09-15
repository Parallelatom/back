"""The pricing rule, checked against values the chain itself produced.

These are not self-consistency checks. Every expected number here was read off Arbitrum
mainnet, so a change that breaks one of them has broken agreement with reality.
"""
import pytest

from strategy_lab.amm import OPENING_RESERVE, Reserves, fill, marginal_price

TICKET = 1_000_000  # one 1 USD ticket, in micro-USDC


class TestMarginalPrice:
    def test_an_untouched_round_is_an_even_chance(self):
        assert marginal_price(Reserves(OPENING_RESERVE, OPENING_RESERVE)) == 0.5

    def test_it_matches_the_price_the_chain_reported(self):
        """Pool 0x55a71c… held 1,483,000 / 168,578 and reported 897,929 for UP."""
        assert marginal_price(Reserves(up=168_578, down=1_483_000)) == pytest.approx(0.897929, abs=1e-6)

    def test_buying_a_side_makes_that_side_dearer(self):
        before = marginal_price(Reserves(OPENING_RESERVE, OPENING_RESERVE))
        after = fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "UP", TICKET).reserves

        assert marginal_price(after) > before


class TestFill:
    def test_a_one_dollar_ticket_on_an_untouched_round_matches_the_chain(self):
        """quoteC0E17FC7 on a live pool returned 1,314,422 shares for 1 USDC."""
        got = fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "UP", TICKET)

        assert got.shares == 1_314_422

    def test_the_fee_taken_matches_the_chain(self):
        assert fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "UP", TICKET).fees == 17_000

    def test_the_effective_price_is_far_worse_than_the_marginal_price(self):
        """The pool is half a dollar deep, so a one dollar ticket is most of it."""
        got = fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "UP", TICKET)

        assert got.price_per_share == pytest.approx(0.7608, abs=1e-4)
        assert marginal_price(Reserves(OPENING_RESERVE, OPENING_RESERVE)) == 0.5

    def test_a_winning_one_dollar_ticket_returns_about_a_third_more(self):
        got = fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "UP", TICKET)

        assert got.payout_on_win == pytest.approx(1.314422, abs=1e-6)
        assert got.profit_on_win == pytest.approx(0.314422, abs=1e-6)

    def test_either_side_of_an_untouched_round_fills_identically(self):
        up = fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "UP", TICKET)
        down = fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "DOWN", TICKET)

        assert up.shares == down.shares

    def test_the_resulting_reserves_reproduce_what_the_chain_held(self):
        """Pool 0x55a71c… after one 1 USDC buy held 1,483,000 against 168,578."""
        after = fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "UP", TICKET).reserves

        assert (after.up, after.down) == (168_578, 1_483_000)

    def test_buying_the_dear_side_of_a_moved_pool_buys_fewer_shares(self):
        moved = Reserves(up=168_578, down=1_483_000)  # UP already at 0.898

        assert fill(moved, "UP", TICKET).shares < fill(moved, "DOWN", TICKET).shares

    def test_a_zero_size_order_buys_nothing(self):
        assert fill(Reserves(OPENING_RESERVE, OPENING_RESERVE), "UP", 0).shares == 0
