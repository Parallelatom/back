# Price Paper Trades from the AMM Reserves, not from the API's `odds` field

The 15-minute markets are a constant-product AMM, not a dynamic pari-mutuel market. The
GraphQL `Campaign.odds` field is documented in the schema as applying to DPPM markets only,
and these Rounds report `isDppm: false` — which is why `odds` came back null or one-sided on
25 of the 26 Rounds we sampled. It is vestigial here and scoring against it would be wrong.

The real primitive is the pool's Reserves. Every Round is seeded at 0.5 USDC per side, so an
untraded Round has a Marginal Price of exactly 0.50/0.50, and both the Marginal Price and the
exact Fill for any order size follow in closed form from the Reserves, offline, with no RPC
call. This was verified against three live pools to the micro-share, and against the chain's
own quote function.

The consequence that matters: the pool is only ~0.5 USDC deep, so a 1 USD ticket moves it
hard. At a displayed 50% price, 1 USD actually buys about 1.314 shares after the 1.7% fee —
an effective 0.75 per share, roughly 49% price impact. So a winning 1 USD Paper Trade returns
about +0.31, and a losing one −1.00, requiring roughly 76% accuracy to break even.

Scoring Paper Trades at the displayed Marginal Price would therefore be badly optimistic. We
compute the Fill from the Reserves instead.
