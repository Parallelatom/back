"""The threshold sweep reports what the recordings support, and no more.

The sweep exists to be argued with, so the properties worth pinning are the honest ones:
a wider threshold never trades more than a narrower one, an interval that straddles
break-even is not reported as clearing it, and no Rounds means no conclusion.
"""
import pytest

from strategy_lab.ingest import RoundMeta
from strategy_lab.replay import load_rounds
from strategy_lab.score import BREAK_EVEN_HIT_RATE
from strategy_lab.threshold_sweep import by_weekday, half, render, score, summarise, wilson
from tests.test_replay import DOWN, GRID, OIL, T0, UP, ingest  # noqa: F401

STRIKE = 100.0


def a_moving_round(ingest, ending, close, symbol=OIL):
    """Delta Edge reads the price *inside* the entry window, so the move must happen there."""
    ingest.observe_round(
        RoundMeta(symbol=symbol, starting=ending - GRID, ending=ending, strike=STRIKE,
                  pool_address=f"0xpool{symbol}{ending}", outcome_up=UP, outcome_down=DOWN),
        now=ending - GRID,
    )
    ts = ending - GRID
    while ts <= ending:
        share = (ts - (ending - GRID)) / GRID
        ingest.observe_price(symbol, price=STRIKE + (close - STRIKE) * share, ts=ts)
        ts += 5
    ingest.finalise_round(symbol, ending)
    ingest.conn.execute(
        "UPDATE rounds SET winner = ?, final_price = ?, oracle_stale = 0, partial = 0, "
        "unsettled = NULL WHERE symbol = ? AND ending = ?",
        ("UP" if close > STRIKE else "DOWN", close, symbol, ending),
    )
    ingest.conn.commit()


def a_run(ingest, closes, symbol=OIL):
    for n, close in enumerate(closes):
        a_moving_round(ingest, T0 + (n + 1) * GRID, close, symbol=symbol)
    return load_rounds(ingest.conn, symbol)


class TestInterval:
    def test_no_trades_yields_no_interval_rather_than_a_zero(self):
        assert wilson(0, 0) is None

    def test_a_single_win_does_not_claim_certainty(self):
        low, high = wilson(1, 1)
        assert low < 1.0 and high == pytest.approx(1.0)

    def test_the_interval_narrows_as_evidence_accumulates(self):
        few = wilson(8, 10)
        many = wilson(800, 1000)
        assert (many[1] - many[0]) < (few[1] - few[0])


class TestScoring:
    def test_a_wider_threshold_never_takes_more_trades(self, ingest):
        records = a_run(ingest, [100.0 + 0.05 * n for n in range(1, 21)])
        counts = [len(score(records, OIL, t)) for t in (0.02, 0.05, 0.10, 0.20)]
        assert counts == sorted(counts, reverse=True)

    def test_a_threshold_no_price_reaches_takes_nothing(self, ingest):
        records = a_run(ingest, [100.02, 100.03, 100.01])
        assert score(records, OIL, 5.0) == []
        assert summarise([])["hit"] is None

    def test_hit_rate_counts_the_side_that_won(self, ingest):
        records = a_run(ingest, [101.0, 101.0, 101.0])
        row = summarise(score(records, OIL, 0.1))
        assert row["n"] == 3 and row["hit"] == 1.0 and row["pnl"] > 0

    def test_the_halves_split_chronologically(self, ingest):
        records = a_run(ingest, [101.0, 101.0, 99.0, 99.0])
        trades = score(records, OIL, 0.1)
        assert half(trades, False)["n"] == half(trades, True)["n"] == 2
        assert half(trades, False)["hit"] == 1.0


class TestReporting:
    def test_no_scoreable_rounds_concludes_nothing(self, ingest):
        report = render(ingest.conn, OIL, [0.1], 7.0, False, False)
        assert "Nothing can be concluded" in report

    def test_a_losing_threshold_is_not_reported_as_clearing_break_even(self, ingest):
        a_run(ingest, [101.0, 99.0, 101.0, 99.0])
        report = render(ingest.conn, OIL, [0.1], 7.0, False, False)
        assert "sits entirely above break-even: none" in report

    def test_the_break_even_line_is_the_one_scoring_uses(self, ingest):
        a_run(ingest, [101.0])
        assert "%.1f%%" % (BREAK_EVEN_HIT_RATE * 100) in render(
            ingest.conn, OIL, [0.1], 7.0, False, False)

    def test_weekday_buckets_follow_the_requested_clock(self, ingest):
        records = a_run(ingest, [101.0])
        trades = score(records, OIL, 0.1)
        assert sum(row["n"] for row in by_weekday(trades, 7.0).values()) == len(trades)
        assert sum(row["n"] for row in by_weekday(trades, -5.0).values()) == len(trades)
