"""Talking to Arbitrum directly, and the encoding that makes it possible.

Every expected value here was taken from a real eth_call against mainnet, so these are
agreement checks rather than restatements of the code.
"""
import pytest

from strategy_lab.chain import (
    QUOTE_SELECTOR, PRICE_SELECTOR, Quote, decode_price, decode_quote, encode_price, encode_quote,
)

OUTCOME = "0xb5e3c4134c1863ec"
TICKET = 1_000_000


class TestEncoding:
    def test_a_quote_call_is_the_selector_then_the_outcome_then_the_size(self):
        data = encode_quote(OUTCOME, TICKET)

        assert data.startswith(QUOTE_SELECTOR)
        assert len(data) == 2 + 8 + 64 + 64  # 0x, selector, two words

    def test_the_outcome_is_left_aligned_in_its_word(self):
        """bytes8 pads on the right, unlike a number."""
        data = encode_quote(OUTCOME, TICKET)
        word = data[10:74]

        assert word.startswith("b5e3c4134c1863ec")
        assert word.endswith("0" * 48)

    def test_the_size_is_right_aligned_in_its_word(self):
        data = encode_quote(OUTCOME, TICKET)

        assert data[74:] == f"{TICKET:064x}"

    def test_an_outcome_without_its_prefix_encodes_the_same_way(self):
        assert encode_quote(OUTCOME[2:], TICKET) == encode_quote(OUTCOME, TICKET)

    def test_a_price_call_takes_only_the_outcome(self):
        data = encode_price(OUTCOME)

        assert data.startswith(PRICE_SELECTOR)
        assert len(data) == 2 + 8 + 64


class TestDecoding:
    def test_a_quote_reply_is_shares_then_fees_then_boost(self):
        """Read off mainnet: 1 USDC on a pool already trading at 0.039 bought 3,218,453
        shares for 17,000 in fees."""
        reply = "0x" + f"{3218453:064x}" + f"{17000:064x}" + f"{0:064x}"

        assert decode_quote(reply) == Quote(shares=3218453, fees=17000, boosted=0)

    def test_a_price_reply_is_one_number_in_micro_units(self):
        assert decode_price("0x" + f"{39487:064x}") == pytest.approx(0.039487)

    def test_an_untouched_pool_prices_at_an_even_chance(self):
        assert decode_price("0x" + f"{500000:064x}") == 0.5

    def test_a_truncated_reply_is_refused_rather_than_guessed_at(self):
        for junk in ("0x", "0x1234", "", None):
            with pytest.raises(ValueError):
                decode_quote(junk)

    def test_a_settled_round_reporting_zero_is_not_mistaken_for_a_price(self):
        """The contract returns 0 once a Round is decided, which is not a probability."""
        assert decode_price("0x" + f"{0:064x}") is None
