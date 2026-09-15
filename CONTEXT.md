# 9lives Strategy Lab

A research harness for the 9lives.so 15-minute prediction markets on Arbitrum One.
It observes live rounds, records what happened, and scores several trading strategies
against that record without ever placing an order.

## Language

### Market structure

**Round**:
One 15-minute prediction market on a single Symbol, settling at a fixed timestamp on a
900-second grid. Each Round asks whether the Symbol's price will close above its Strike.
_Avoid_: market, campaign, game, candle

**Symbol**:
The underlying asset a Round is written on. Exactly two are live: `BTC` and `XYZCL`.
_Avoid_: token, asset, pair, coin

**XYZCL**:
The Symbol for WTI crude oil. This is the only spelling that works against the API;
`WTIOIL` is the web UI's display name for the same thing and appears nowhere in the data.
_Avoid_: WTIOIL, WTI, OIL, CL

**Strike**:
The price a Round settles against. It equals the closing price of the immediately
preceding Round on the same Symbol.
_Avoid_: target, priceTargetForUp, strike price, barrier

**Delta**:
How far the current price sits from the Strike, as a percentage of the Strike, signed.
Positive means the Round is currently winning for UP.
_Avoid_: delta weight, distance, dist, deviation

**Side**:
Which outcome a position is on: `UP` or `DOWN`.
_Avoid_: direction, bet, outcome, position

**Trade Window**:
The span within a Round during which a Strategy may enter: from 300 to 60 seconds before
settlement. The 60-second floor is not a preference — the contract rejects any buy after
that point — and shares cannot be sold once bought, so every entry is held to settlement.
_Avoid_: entry window, time window, lock window

**Oracle Stale**:
A Round whose underlying price feed did not move meaningfully while the Round was open,
because the real-world market for that Symbol was closed. Common for `XYZCL` outside
NYMEX hours. Such Rounds are recorded but excluded from headline results by default.
_Avoid_: dead round, weekend round, frozen

### The harness

**Collector**:
The process that watches live Rounds and writes the raw record. It runs continuously and
never decides anything; it only observes. Any gap in its uptime is permanent, because the
upstream API retains only about 2.5 hours of history.
_Avoid_: bot, scraper, poller, logger

**Strategy**:
A named rule that decides, for a given Round, whether to take a Paper Trade and on which
Side. The four Strategies are scored independently against the same record.
_Avoid_: model, algo, signal, config

**Paper Trade**:
A recorded hypothetical position: a Side, an entry moment, and the Fill computed from the
Reserves at that moment. No order is ever sent to the exchange.
_Avoid_: trade, order, position, backtest entry

**Reserves**:
The pair of outcome share balances in a Round's AMM pool, which together determine its
price. Every Round opens at an even 0.5/0.5 and moves only when someone trades. This, not
the API's `odds` field, is the primitive the harness records.
_Avoid_: odds, liquidity, pool, shares

**Marginal Price**:
The instantaneous implied probability of a Side, derived from the Reserves. It is what the
9lives web UI displays as a percentage, and it is what an infinitely small order would pay.
No real order pays it.
_Avoid_: price, odds, probability, implied prob

**Fill**:
The number of shares 1 USD actually buys on a Side at a given moment, after fees and the
price impact of the order itself. A Round that nobody has traded fills a 1 USD ticket at
about 0.75 per share against a Marginal Price of 0.50. The Fill, never the Marginal Price,
determines what a Paper Trade wins.
_Avoid_: execution price, entry price, cost, slippage

**Bankroll**:
The running balance of a single Strategy on a single Symbol, starting at 10 USD and moving
by the result of each Paper Trade at 1 USD of stake per Round. A Bankroll cannot go below
zero; reaching zero is Ruin.
_Avoid_: capital, equity, balance, PnL

**Ruin**:
The moment a Strategy's Bankroll reaches zero on a Symbol. The Strategy stops taking Paper
Trades on that Symbol from then on, and its line on the chart ends there.
_Avoid_: bust, blown up, stopped out, drawdown

**Hit Rate**:
The share of a Strategy's Paper Trades that picked the winning Side. This, not the Bankroll,
is the primary measure of whether a Strategy works, because the markets are too thin for the
money figure to survive contact with real execution.
_Avoid_: win rate, accuracy, winrate, success rate

**Reconstructed Round**:
A past Round rebuilt from the oracle price series rather than watched as it happened. It
carries a Strike, a close and a winning Side, but no Reserves, so it is priced at the even
opening Reserves. Only BTC has the backlog to rebuild from.
_Avoid_: backfilled, historical, synthetic, derived round

**Partial Round**:
A Round the Collector only saw part of, because it started or restarted while the Round was
already open. Everything seen is still recorded, but no Strategy takes a Paper Trade on it.
_Avoid_: incomplete, broken round, half round

### The four Strategies

**Delta Edge**:
Enters only when Delta exceeds the Symbol's threshold, taking the Side that Delta favours.
The only Strategy that filters. Strategy 4.1 in the original brief.

**Always Up**:
Enters `UP` on every Round with no condition. A baseline, not a belief.
Strategy 4.2 in the original brief.

**Always Down**:
Enters `DOWN` on every Round with no condition. A baseline, not a belief.
Strategy 4.3 in the original brief.

**Flip Follow**:
Enters when Delta changes sign during the Trade Window and stays on the new side for three
seconds, taking the Side that Delta flipped to. The hold is what separates a real crossing
from the noise of a price sitting on its Strike. Strategy 4.4 in the original brief.
