"""Pricing a Paper Trade from the contract's own quote rather than from a model.

The local rule is not merely less precise: it is run on Reserves the exchange reports,
and the exchange reports nothing at all for a Round already traded. Its silence becomes
an untouched pool, which is the best price that exists, so the error is one-directional.
These pin that the contract wins wherever it has spoken, that reaching forward in time is
impossible, and that a page mixing the two says so.
"""
import os

import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest
from strategy_lab.render import _pricing_note
from strategy_lab.replay import (ALWAYS_UP, STAKE_MICRO, load_rounds, replay)
from tests.test_replay import BTC, GRID, T0, a_round, ingest  # noqa: F401

ENDING = T0 + GRID
OPENING_FILL = 1_314_422


def a_quote(ingest, side="UP", ts=ENDING - 300, shares=1_050_199, symbol=BTC):
    ingest.record_chain_quote(symbol, ENDING, side, ts, STAKE_MICRO, shares, 17_000)


class TestPreferringTheContract:
    def test_the_local_rule_prices_a_round_the_contract_never_answered(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        trade = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"].trades[0]
        assert trade.priced_by == "model"
        assert trade.shares == OPENING_FILL

    def test_a_recorded_quote_prices_the_fill_instead(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        a_quote(ingest)
        trade = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"].trades[0]
        assert trade.priced_by == "chain"
        assert trade.shares == 1_050_199

    def test_the_worse_price_shows_up_in_the_money(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        modelled = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"]
        a_quote(ingest)
        quoted = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"]
        assert quoted.trades[0].won and modelled.trades[0].won
        assert quoted.trades[0].pnl < modelled.trades[0].pnl

    def test_a_quote_only_counts_for_its_own_side(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        a_quote(ingest, side="DOWN")
        trade = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"].trades[0]
        assert trade.priced_by == "model"


class TestNotReachingForward:
    def test_a_quote_after_the_entry_does_not_price_it(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        record = load_rounds(ingest.conn, BTC)[0]
        a_quote(ingest, ts=ENDING - 10)
        record = load_rounds(ingest.conn, BTC)[0]

        assert record.quote_at("UP", ENDING - 300) is None
        assert record.quote_at("UP", ENDING - 10)[:2] == (1_050_199, 17_000)

    def test_the_quote_in_force_is_the_most_recent_one_before_the_moment(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        a_quote(ingest, ts=ENDING - 300, shares=1_300_000)
        a_quote(ingest, ts=ENDING - 200, shares=1_100_000)
        record = load_rounds(ingest.conn, BTC)[0]

        assert record.quote_at("UP", ENDING - 250)[0] == 1_300_000
        assert record.quote_at("UP", ENDING - 100)[0] == 1_100_000


class TestRecording:
    @pytest.mark.parametrize("bad", [
        {"side": "SIDEWAYS"}, {"gross": 0}, {"shares": -1}, {"fees": -1},
    ])
    def test_a_nonsense_quote_is_refused(self, ingest, bad):
        args = {"symbol": BTC, "round_ending": ENDING, "side": "UP", "ts": 1,
                "gross": STAKE_MICRO, "shares": 1, "fees": 0, **bad}
        with pytest.raises(ValueError):
            ingest.record_chain_quote(**args)

    def test_the_same_moment_is_never_recorded_twice(self, ingest):
        a_quote(ingest)
        a_quote(ingest, shares=999)
        stored = ingest.conn.execute("SELECT shares FROM chain_quotes").fetchall()
        assert [r[0] for r in stored] == [1_050_199]


class TestSayingWhichPricedIt:
    def test_a_page_with_no_contract_prices_says_the_money_is_optimistic(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        results = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])
        assert "the money is the part to doubt" in _pricing_note(results)

    def test_a_page_priced_entirely_by_the_contract_says_nothing(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        a_quote(ingest)
        results = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])
        assert _pricing_note(results) == ""

    def test_a_mixture_is_declared_as_two_measurements(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        a_round(ingest, symbol=BTC, ending=ENDING + GRID, close=101.0)
        a_quote(ingest)
        results = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])
        note = _pricing_note(results)
        assert "1 of 2" in note and "two measurements" in note


@pytest.mark.skipif(not os.environ.get("STRATEGY_LAB_LIVE"),
                    reason="set STRATEGY_LAB_LIVE=1 to run checks against the live feed")
class TestAgainstTheChain:
    def test_the_contract_answers_in_the_shape_a_fill_is_priced_from(self):
        from strategy_lab import sources
        from strategy_lab.chain import Arbitrum

        graph = sources.Graph()
        for symbol in sources.SYMBOLS:
            meta = graph.current_round(symbol)
            if meta is None:
                continue
            quoted = Arbitrum().quote(meta.pool_address, meta.outcome_up, STAKE_MICRO)
            assert quoted is not None
            assert quoted.shares > 0 and quoted.fees >= 0
            return
        pytest.skip("no open Round on either Symbol right now")


class TestTheBreakEvenLineFollowsThePrice:
    """A threshold held as a constant assumes every Fill met an untouched pool."""

    def test_no_trades_has_no_break_even_rather_than_a_default(self, ingest):
        results = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])
        assert results["Always Up"].break_even is None

    def test_the_opening_price_implies_the_familiar_line(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        result = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"]
        assert result.break_even == pytest.approx(1 / 1.314422, abs=1e-6)

    def test_a_worse_fill_raises_the_line(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        a_quote(ingest)
        result = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"]
        assert result.break_even == pytest.approx(1 / 1.050199, abs=1e-6)
        assert result.break_even > 1 / 1.314422

    def test_a_better_fill_lowers_it(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        a_quote(ingest, shares=2_890_330)
        result = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"]
        assert result.break_even == pytest.approx(1 / 2.890330, abs=1e-6)

    def test_the_verdict_quotes_the_line_it_judged_against(self, ingest):
        from strategy_lab.render import _table
        from strategy_lab.replay import ALL_STRATEGIES
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        a_quote(ingest, shares=2_890_330)
        results = replay(ingest.conn, symbol=BTC, strategies=ALL_STRATEGIES)
        assert "above break-even (34.6%)" in _table(BTC, results, 0, 1)


class TestAnUnupgradedDatabase:
    """The dashboard mounts the recordings read-only and cannot create a table."""

    def test_scoring_works_with_no_chain_quotes_table_at_all(self, ingest):
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        ingest.conn.execute("DROP TABLE chain_quotes")
        ingest.conn.commit()

        result = replay(ingest.conn, symbol=BTC, strategies=[ALWAYS_UP])["Always Up"]
        assert len(result.trades) == 1
        assert result.trades[0].priced_by == "model"

    def test_the_page_renders_without_the_table(self, ingest):
        from strategy_lab.render import render_page
        a_round(ingest, symbol=BTC, ending=ENDING, close=101.0)
        ingest.conn.execute("DROP TABLE chain_quotes")
        ingest.conn.commit()

        assert "<!doctype html>" in render_page(ingest.conn)
