# The chain outranks the exchange's indexer on Reserves

ADR-0005 has the Collector derive Reserves from the trade feed and reconcile them against
the exchange's GraphQL every thirty seconds, with GraphQL winning any disagreement. That
was wrong in one specific and damaging way.

The exchange's indexer lags the chain. A live Round that has already been traded can still
report an empty `shares` field — observed directly: the contract priced a pool at 0.039 for
UP, implying Reserves of roughly 2,466,000 against 101,379, while `shares` for the same
Round came back as an empty list. The reconcile read that emptiness as "nobody has traded
this", which ADR-0004 establishes means the even opening Reserves, and would have written
500,000 against 500,000 straight over the correct figures.

So an empty `shares` is now treated as no answer rather than as an answer of zero. Nothing
is reconciled when the exchange has not spoken, and locally tracked Reserves stand.

The chain itself is available as a stronger source. `priceA827ED27` returns a Round's
marginal price and `quoteC0E17FC7` returns the exact fill for a given size, both by
`eth_call` against the Round's own pool, and both were verified against mainnet. They are
not used in the reconcile loop, because that runs every thirty seconds per Symbol and a
public RPC is not something to lean on that hard for a figure the trade feed already
provides. They are used once a day to check that the local pricing rule still matches the
contract, which is the check that would notice the per-market fee being changed.

The rule to carry forward: the exchange's GraphQL is convenient and is the right source for
Round metadata, but on anything the contract itself knows, the contract is the authority and
the indexer is a cache that may be stale.
