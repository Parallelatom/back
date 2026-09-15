"""Ticket 12: noticing when the local pricing rule stops matching the contract."""
import pytest

from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta
from strategy_lab.render import render_page

BTC = "BTC"
GRID = 900
T0 = 1789443000
POOL = "0xpool"


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def checks(ingest):
    return [dict(r) for r in ingest.conn.execute("SELECT * FROM quote_checks ORDER BY rowid")]


class TestRecordingTheCheck:
    def test_both_answers_and_whether_they_agreed_are_written_down(self, ingest):
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1_314_422,
                                  local_fees=17_000, chain_shares=1_314_422,
                                  chain_fees=17_000, ts=T0)

        (check,) = checks(ingest)
        assert check["local_shares"] == 1_314_422
        assert check["chain_shares"] == 1_314_422
        assert check["agrees"] == 1

    def test_a_disagreement_is_recorded_as_such(self, ingest):
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1_314_422,
                                  local_fees=17_000, chain_shares=3_218_453,
                                  chain_fees=17_000, ts=T0)

        assert checks(ingest)[0]["agrees"] == 0

    def test_a_changed_fee_counts_as_a_disagreement(self, ingest):
        """The fee is per-market storage, not a constant of the protocol. A change would
        otherwise skew every Fill silently."""
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1_314_422,
                                  local_fees=17_000, chain_shares=1_314_422,
                                  chain_fees=25_000, ts=T0)

        assert checks(ingest)[0]["agrees"] == 0

    def test_a_check_that_could_not_reach_the_chain_records_the_reason(self, ingest):
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1_314_422,
                                  local_fees=17_000, chain_shares=None, chain_fees=None,
                                  ts=T0, note="rpc unreachable")

        check = checks(ingest)[0]
        assert check["chain_shares"] is None
        assert check["note"] == "rpc unreachable"
        assert check["agrees"] is None

    def test_every_check_is_kept_rather_than_overwritten(self, ingest):
        for day in range(3):
            ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1,
                                      local_fees=1, chain_shares=1, chain_fees=1,
                                      ts=T0 + day * 86400)

        assert len(checks(ingest)) == 3


class TestSurfacingIt:
    """Buried in a log, a disagreement is discovered weeks later, after it has already
    skewed everything."""

    def test_a_recent_disagreement_is_shown_on_the_page(self, ingest):
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1_314_422,
                                  local_fees=17_000, chain_shares=3_218_453,
                                  chain_fees=17_000, ts=T0)

        assert "disagree" in render_page(ingest.conn).lower()

    def test_agreement_says_nothing(self, ingest):
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1_314_422,
                                  local_fees=17_000, chain_shares=1_314_422,
                                  chain_fees=17_000, ts=T0)

        assert "disagree" not in render_page(ingest.conn).lower()

    def test_an_unreachable_chain_is_not_reported_as_a_disagreement(self, ingest):
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1, local_fees=1,
                                  chain_shares=None, chain_fees=None, ts=T0,
                                  note="rpc unreachable")

        assert "disagree" not in render_page(ingest.conn).lower()

    def test_a_later_agreement_clears_an_earlier_disagreement(self, ingest):
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1, local_fees=1,
                                  chain_shares=2, chain_fees=1, ts=T0)
        ingest.record_quote_check(POOL, gross=1_000_000, local_shares=1, local_fees=1,
                                  chain_shares=1, chain_fees=1, ts=T0 + 86400)

        assert "disagree" not in render_page(ingest.conn).lower()


class TestReserveReconcileRefusesStaleAnswers:
    """The exchange's indexer lags the chain. A live Round that has been traded can still
    report no shares at all, and taking that as 'untouched' would overwrite correct
    Reserves with the opening ones."""

    def test_an_empty_shares_reply_is_no_answer_rather_than_an_even_pool(self):
        from strategy_lab.sources import Graph

        campaign = {"poolAddress": POOL, "shares": [],
                    "outcomes": [{"name": "BTC will end up above", "identifier": "0xup"},
                                 {"name": "BTC will end up below", "identifier": "0xdown"}]}

        assert Graph.reserves_from_campaign(campaign) is None

    def test_a_populated_shares_reply_is_read(self):
        from strategy_lab.sources import Graph

        campaign = {"poolAddress": POOL,
                    "shares": [{"identifier": "up", "shares": "168578"},
                               {"identifier": "down", "shares": "1483000"}],
                    "outcomes": [{"name": "BTC will end up above", "identifier": "0xup"},
                                 {"name": "BTC will end up below", "identifier": "0xdown"}]}

        assert Graph.reserves_from_campaign(campaign) == (POOL, 168578, 1483000)
