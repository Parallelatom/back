"""Ticket 06: knowing what the pool looked like at every moment of a Round."""
import pytest

from strategy_lab.amm import OPENING_RESERVE
from strategy_lab.db import connect, initialise
from strategy_lab.ingest import Ingest, RoundMeta

BTC = "BTC"
GRID = 900
T0 = 1789443000
POOL = "0xce6b261b2770b90e3b007531e3f9cdcdcca607ee"
UP = "0x5e9d5db5a3401bbb"
DOWN = "0x98d957e72f8000bb"


@pytest.fixture
def ingest():
    conn = connect(":memory:")
    initialise(conn)
    return Ingest(conn, code_version="testver")


def a_round(ingest, ending=T0 + GRID):
    ingest.observe_round(
        RoundMeta(symbol=BTC, starting=ending - GRID, ending=ending, strike=100.0,
                  pool_address=POOL, outcome_up=UP, outcome_down=DOWN),
        now=ending - GRID,
    )


def observations(ingest):
    return [dict(r) for r in ingest.conn.execute("SELECT * FROM reserves ORDER BY rowid")]


def mismatches(ingest):
    return [dict(r) for r in ingest.conn.execute("SELECT * FROM reconcile_log")]


def a_trade(outcome=UP, net=983_000, shares=1_314_422, pool=POOL,
            iso="2026-09-15T03:35:00.000000Z", kind="buy"):
    return {"table": "ninelives_buys_and_sells_1",
            "content": {"emitter_addr": pool, "outcome_id": outcome, "type": kind,
                        "from_amount": str(net), "to_amount": str(shares),
                        "created_by": iso}}


class TestSeeding:
    def test_a_newly_seen_round_starts_at_the_even_opening_reserves(self, ingest):
        a_round(ingest)

        (seeded,) = observations(ingest)
        assert (seeded["q_up"], seeded["q_down"]) == (OPENING_RESERVE, OPENING_RESERVE)
        assert seeded["source"] == "seed"

    def test_seeding_costs_no_network_call_and_happens_once(self, ingest):
        a_round(ingest)
        a_round(ingest)

        assert len(observations(ingest)) == 1

    def test_the_seed_is_tied_to_its_round(self, ingest):
        a_round(ingest)

        (seeded,) = observations(ingest)
        assert seeded["symbol"] == BTC
        assert seeded["round_ending"] == T0 + GRID


class TestTradeEvents:
    def test_a_buy_moves_the_reserves(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(outcome=UP))

        latest = observations(ingest)[-1]
        assert (latest["q_up"], latest["q_down"]) == (168_578, 1_483_000)
        assert latest["source"] == "trade"

    def test_buying_the_other_side_moves_the_reserves_the_other_way(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(outcome=DOWN))

        latest = observations(ingest)[-1]
        assert (latest["q_up"], latest["q_down"]) == (1_483_000, 168_578)

    def test_two_buys_compound(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(outcome=UP, iso="2026-09-15T03:35:00Z"))
        ingest.observe_feed_message(a_trade(outcome=UP, iso="2026-09-15T03:36:00Z"))

        assert len(observations(ingest)) == 3
        assert observations(ingest)[-1]["q_up"] < 168_578

    def test_a_trade_on_a_pool_we_do_not_know_is_ignored(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(pool="0xelsewhere"))

        assert len(observations(ingest)) == 1

    def test_a_trade_naming_neither_outcome_is_ignored(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(outcome="0xdeadbeef"))

        assert len(observations(ingest)) == 1

    def test_an_identifier_without_its_leading_prefix_still_matches(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(outcome=UP[2:]))

        assert observations(ingest)[-1]["q_up"] == 168_578

    def test_a_malformed_trade_is_ignored(self, ingest):
        a_round(ingest)
        for junk in ({"emitter_addr": POOL}, {"emitter_addr": POOL, "outcome_id": UP},
                     {"emitter_addr": POOL, "outcome_id": UP, "from_amount": "not a number"}):
            ingest.observe_feed_message({"table": "ninelives_buys_and_sells_1", "content": junk})

        assert len(observations(ingest)) == 1


class TestReconciling:
    def test_reconcile_before_metadata_is_used_to_price_paper_trades(self, ingest):
        from strategy_lab.amm import Reserves, fill
        from strategy_lab.replay import ALWAYS_UP, replay

        ingest.apply_remote_reserves(POOL.upper(), up=168_578, down=1_483_000, ts=T0 + 400)
        a_round(ingest)
        # An unchanged reconciliation must preserve the newly attached Round identity.
        ingest.apply_remote_reserves(POOL, up=168_578, down=1_483_000, ts=T0 + 500)
        ingest.conn.execute("UPDATE rounds SET winner = 'UP', oracle_stale = 0, partial = 0")
        result = replay(ingest.conn, BTC, strategies=[ALWAYS_UP])[ALWAYS_UP.name]

        assert result.trades[0].shares == fill(Reserves(168_578, 1_483_000), "UP", 1_000_000).shares
        assert result.trades[0].shares == 1_050_199
        assert len(observations(ingest)) == 1

    def test_the_exchanges_answer_is_recorded(self, ingest):
        a_round(ingest)
        ingest.apply_remote_reserves(POOL, up=168_578, down=1_483_000, ts=T0 + 400)

        latest = observations(ingest)[-1]
        assert (latest["q_up"], latest["q_down"], latest["source"]) == (168_578, 1_483_000, "graphql")

    def test_agreement_is_not_logged_as_a_mismatch(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(outcome=UP))
        ingest.apply_remote_reserves(POOL, up=168_578, down=1_483_000, ts=T0 + 400)

        assert mismatches(ingest) == []

    def test_a_divergence_is_logged_with_both_answers(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(outcome=UP))
        ingest.apply_remote_reserves(POOL, up=999_999, down=1_483_000, ts=T0 + 400)

        (logged,) = mismatches(ingest)
        assert (logged["local_q_up"], logged["remote_q_up"]) == (168_578, 999_999)

    def test_after_a_divergence_the_exchange_is_what_we_carry_forward(self, ingest):
        a_round(ingest)
        ingest.observe_feed_message(a_trade(outcome=UP))
        ingest.apply_remote_reserves(POOL, up=999_999, down=1_483_000, ts=T0 + 400)

        assert observations(ingest)[-1]["q_up"] == 999_999
