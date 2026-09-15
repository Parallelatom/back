# Spec 0001: 9lives Strategy Lab

**Status:** ready-for-agent

## Problem Statement

I have a trading bot written for the 9lives 5-minute markets, and it no longer runs. The
markets moved to Arbitrum One, the `5mins` category stopped being created, and all three
endpoints the bot talked to are dead. Even if I repaired it, I would be pointing a bot with
nine stacked filters — several of which I have never verified — at a market I have never
measured, using real money.

What I actually want to know first is narrower: **do any of the entry rules I have in mind
pick the winning Side more often than a coin flip, on either of the two Symbols that are
live?** I cannot answer that today. There is no historical data to test against: the API
retains roughly 2.5 hours of settled Rounds and exposes no archive. Every hour I am not
recording is an hour I can never get back.

I also cannot see anything. The old bot printed to a terminal. I want to look at two charts
and know whether an idea is working.

## Solution

A research harness with two halves.

A **Collector** runs continuously and writes down everything that happens in every Round on
both Symbols — the price series, the Strike, the pool Reserves as they move, and the settled
outcome — without knowing that Strategies exist. It is deliberately ignorant, so that a
Strategy invented next month can be scored against every Round already recorded.

A **dashboard** replays those recordings through four Strategies and shows me, per Symbol,
how often each one picked the winning Side and what a 10 USD Bankroll would have done. BTC on
the left, XYZCL on the right. No order is ever sent; the harness holds no private key.

## User Stories

### Recording

1. As a researcher, I want the Collector to record every Round on both Symbols regardless of whether any Strategy would have entered, so that a Strategy I invent later can be scored against history I already have.
2. As a researcher, I want the Collector to record the underlying price at least every few seconds throughout each Round, so that I can reconstruct what any entry rule would have seen at any moment.
3. As a researcher, I want the Collector to record the Strike of every Round, so that Delta is computable at every recorded moment.
4. As a researcher, I want the Collector to record the Reserves whenever they change, so that the Fill of a Paper Trade can be computed at the exact moment of entry rather than approximated.
5. As a researcher, I want the Collector to record the settled outcome of every Round, so that Paper Trades can be scored.
6. As a researcher, I want the Collector to record the count of distinct prices, the high-low range, and the tick count for each Round, so that I can choose a better Oracle Stale rule later from evidence instead of guessing now.
7. As a researcher, I want the Collector to record every moment Delta changes sign, along with the size of Delta and how long it held, so that I can test variants of Flip Follow without collecting new data.
8. As a researcher, I want every recorded row to carry the version of the code that wrote it, so that I can tell which Rounds were recorded under which rules.
9. As a researcher, I want the Collector to keep running when the upstream feed misbehaves, so that a transient failure costs me seconds of data rather than hours.
10. As a researcher, I want to know when the Collector lost data, so that I never compute a Hit Rate over a window I believe is complete but is not.

### Data integrity

11. As a researcher, I want a Round the Collector joined late to be marked as a Partial Round, so that no Strategy scores a Paper Trade on a Round it saw only part of.
12. As a researcher, I want a Partial Round's observations still written to the database, so that the decision to exclude it stays reversible.
13. As a researcher, I want a Round whose underlying price never changed to be marked Oracle Stale, so that XYZCL Rounds outside oil market hours do not inflate a baseline's results.
14. As a researcher, I want Reserves derived from the live trade feed to be checked against the API every 30 seconds, so that a dropped event cannot silently corrupt the Fill of every Paper Trade for the rest of a Round.
15. As a researcher, I want every mismatch between the two sources logged, so that after a few days I can tell whether the live feed is trustworthy on its own.
16. As a researcher, I want the computed Fill compared against the chain's own quote once a day, so that a change to the market's fee is caught rather than silently skewing every result afterwards.
17. As a researcher, I want a Round whose settlement could not be read to be marked unsettled and excluded from Hit Rate, so that an unknown outcome is never counted as a loss.
18. As a researcher, I want an unsettled Round's outcome recovered from the following Round's Strike where possible, so that a gap in settlement reads does not permanently cost me a scored Round.

### Strategies

19. As a researcher, I want four Strategies scored independently on the same recordings, so that I can compare them on identical Rounds rather than on different samples.
20. As a researcher, I want Delta Edge to enter at the first moment within the Trade Window that Delta exceeds the Symbol's threshold, taking the Side Delta favours, so that I can test whether distance from the Strike predicts the outcome.
21. As a researcher, I want Always Up to enter UP at the start of the Trade Window on every Round with no condition, so that I have a baseline that tells me whether the market is simply biased upward.
22. As a researcher, I want Always Down to enter DOWN at the start of the Trade Window on every Round with no condition, so that I have the mirror baseline.
23. As a researcher, I want Flip Follow to enter when Delta changes sign and stays on the new side for three seconds, taking the Side it flipped to, so that I can test whether a crossing of the Strike carries information.
24. As a researcher, I want every Strategy confined to the same Trade Window, so that differences between them come from their entry rules and not from entering at different prices.
25. As a researcher, I want each Strategy to take at most one Paper Trade per Round, so that Hit Rates are comparable across Strategies that trigger at different frequencies.
26. As a researcher, I want each Strategy's threshold configurable per Symbol, so that BTC and XYZCL can be treated as the different instruments they are.
27. As a researcher, I want to change a Strategy's parameters and see results recomputed from existing recordings, so that testing a new threshold costs seconds rather than another week of collection.

### Scoring

28. As a researcher, I want a Paper Trade priced at the Fill computed from the Reserves at entry, so that the result reflects the roughly 49% price impact a 1 USD ticket has on a pool this shallow, rather than the Marginal Price the UI displays.
29. As a researcher, I want a Round nobody has traded to price at exactly 0.5 per Side, so that the common case is handled without a network call.
30. As a researcher, I want each Strategy's Bankroll to start at 10 USD and stake 1 USD per Paper Trade, so that the curves are comparable and match how I think about the risk.
31. As a researcher, I want a Strategy that reaches a zero Bankroll to stop taking Paper Trades on that Symbol, so that Ruin is visible as a terminal event rather than hidden in a recovering line.
32. As a researcher, I want Hit Rate presented as the primary number, so that I am not misled by a money curve computed from prices no real order of this size could obtain.

### Seeing it

33. As a researcher, I want a summary table of all four Strategies across both Symbols showing Hit Rate, Paper Trade count, and Bankroll, so that I can read the state of the experiment in one glance.
34. As a researcher, I want the BTC chart on the left and the XYZCL chart on the right, so that I can compare the two markets side by side without scrolling.
35. As a researcher, I want each chart to show all four Bankroll curves plus a flat line at the 10 USD starting capital, so that "is this above water" is answerable at a glance.
36. As a researcher, I want the horizontal axis to be real time rather than Round number, so that gaps in collection are visible as gaps rather than compressed away.
37. As a researcher, I want a Bankroll line to break rather than interpolate across a period with no data, so that I never read a straight segment as a quiet market when it was actually an outage.
38. As a researcher, I want a table of the most recent 50 Rounds showing the Strike, the settled price, the winning Side, and which Strategies entered, so that I can check the system is scoring correctly rather than trusting the charts.
39. As a researcher, I want toggles to show or hide Oracle Stale and Partial Rounds, hidden by default, so that the headline numbers are clean but nothing is unexaminable.
40. As a researcher, I want all times displayed in UTC+7, so that I can relate results to my own day.
41. As a researcher, I want a visible warning near any Strategy parameter control noting that tuning until the curve looks good is fitting, not discovery, so that I am reminded of it at the moment of temptation.

### Operating

42. As an operator, I want the Collector and the dashboard to run as two containers sharing a database file, so that the dashboard crashing cannot take down collection.
43. As an operator, I want both containers to restart automatically, so that a reboot does not silently end the experiment.
44. As an operator, I want the database on a bind mount, so that I can copy it to my laptop and inspect it directly.
45. As an operator, I want to reach the dashboard through an authenticated tunnel, so that it is not an open page on the public internet.

## Implementation Decisions

### Shape

Two processes with one shared SQLite database. The Collector writes; the dashboard reads and
replays. No Strategy logic exists in the Collector — this is the central constraint of
ADR-0002 and everything else follows from it.

A third module holds the AMM pricing maths as pure functions, shared by both. It performs no
I/O.

### Data sources

- Underlying price and trade events arrive over the Arbitrum WebSocket feed, which requires no
  authentication. Prices arrive roughly every 5 seconds even when unchanged.
- Round metadata (Strike, pool address, outcome identifiers, settlement timestamps) comes from
  the GraphQL endpoint.
- Settlement is taken from the outcome-decided event stream in preference to the price-history
  query, whose retention is only about 2.5 hours.
- Reserves are maintained from trade events and reconciled against GraphQL every 30 seconds,
  per ADR-0005.
- A once-daily call to the chain's quote function verifies the local Fill computation against
  the contract, since fees are per-market storage rather than a constant.

The `odds` field on a campaign is not used. It applies to DPPM markets only and these are AMM
markets; it is null or one-sided on almost every Round. See ADR-0004.

GraphQL polling stays at roughly one request per 2-3 seconds per Symbol, sequential, with
jitter. Measured endpoint latency is 1.1-1.2 seconds per request and responses are not edge
cached, so faster polling is not achievable and would buy nothing.

### Pricing

The following is the verified constant-product rule, reproduced to the micro-share against
three live pools and the chain's own quote function. It is stated here because prose states it
less precisely.

```
Every Round opens at  q_UP = q_DOWN = 500_000  micro-shares, L = 500_000
Marginal price of UP  = q_DOWN / (q_UP + q_DOWN)

Buying `gross` micro-USDC of outcome i:
    a = gross * (1 - fee)          # fee observed at 1.7%, read from the market
    q_j += a   for every outcome j
    shares_out = q_i - ceil(L^2 / q_other)
    q_i = ceil(L^2 / q_other)
```

At a 1 USD ticket on a fresh Round this yields 1,314,422 micro-shares — an effective 0.7476
per share against a 0.50 Marginal Price, so a win returns about +0.31 and a loss −1.00. This
is why Hit Rate and not Bankroll is the headline figure (ADR-0003).

### Schema

Tables for: Rounds (one row per Symbol and settlement time, carrying Strike, pool address,
outcome identifiers, settled outcome, the Partial and Oracle Stale and unsettled flags, and
the per-Round statistics); price ticks; Reserve observations tagged with their source; sign-change
events with magnitude and hold duration; reconciliation mismatches; and daily quote checks.

No table holds Paper Trades or Strategy results. Those are derived at read time.

Every row carries the code version that wrote it.

SQLite runs in WAL mode with a busy timeout, so that the reading dashboard cannot block the
writing Collector.

### Trade Window

60 to 300 seconds before settlement, for all four Strategies. The 60-second floor is not a
preference: the contract rejects buys after that point. Shares cannot be sold once bought, so
every Paper Trade is held to settlement and there is no exit rule to model.

### Carried over from the old bot

The WebSocket client, the GraphQL query shape, and the rolling price cache are reusable. The
momentum calculation, the trend-confirmation gates, the calibrated-by-guesswork probability
curve, the edge threshold, the expected-value gate, the loss-streak pause, and all order
execution are not carried over. The original file stays in the repository, unmodified, as a
reference.

Two defects in the original are not to be reproduced: the momentum function returned the same
value for all three of its windows, and the market-probability function indexed into an
unordered map, so it could return the probability of the opposite Side.

## Testing Decisions

A good test here asserts on what a researcher would read off the screen — a Hit Rate, a
Bankroll, whether a Strategy entered a given Round and on which Side — and never on how the
code arrived there. Strategy rules and thresholds are behaviour and belong in assertions;
the internal shape of the replay is not.

There is no existing test suite and no prior art in this repository. Both seams below are new.

### Seam 1: the Replayer (primary)

Recorded Rounds, ticks and Reserves in; Paper Trades, Hit Rate and Bankroll per Strategy per
Symbol out. A pure function with no I/O. All Strategy behaviour lives behind it.

Cases to cover: each Strategy entering and declining to enter; the Trade Window boundaries at
both ends; Delta thresholds per Symbol; Flip Follow's three-second hold accepting a sustained
crossing and rejecting an oscillation; one Paper Trade per Strategy per Round; Bankroll
arithmetic and Ruin terminating a Strategy on one Symbol without affecting the other;
Partial and Oracle Stale Rounds excluded; unsettled Rounds excluded from Hit Rate.

Fill computation is tested through this seam against values verified on-chain: a 1 USD ticket
on an untouched Round yielding 1,314,422 micro-shares, and Reserves of 1,483,000 / 168,578
yielding a Marginal Price of 897,929. These are exact and will catch any drift in the formula.

### Seam 2: Collector ingest

A sequence of raw feed messages and API responses in; database state out.

Cases to cover: Reserves reconstructed correctly from a trade event; reconciliation detecting
and correcting a divergence and logging it; a Round joined mid-flight marked Partial while its
observations are still written; a Round with no price change marked Oracle Stale; settlement
recorded from the outcome-decided event; an unsettled Round later recovered from the following
Round's Strike; per-Round statistics accumulated correctly.

## Out of Scope

- Sending any real order, and therefore any handling of private keys, wallet signatures, or the
  session-secret flow. This is ADR-0001 and is a separate decision to be made after results exist.
- Backtesting against history predating the Collector. The API makes it impossible.
- Any Symbol other than BTC and XYZCL, and any category other than the 15-minute markets.
- Modelling partial fills, order cancellation, or exits before settlement. The contract does not
  permit selling.
- Repairing the original 5-minute bot.
- Alerting, notifications, or any push mechanism. The dashboard is pulled, not pushed.
- Automatic Strategy selection or parameter optimisation. A slider that finds the prettiest curve
  is fitting, and the spec deliberately does not provide one.

## Further Notes

The markets are extremely thin. Median total volume across the 26 most recent Rounds was 0.98
USDC, which is one 1 USD ticket. The Bankroll figures this harness produces are therefore
indicative only; a real order of this size is most of the market. This is a measurement of
directional accuracy, and it should keep saying so on screen.

Collection uptime is the one thing that cannot be recovered. Roughly 2.5 hours of downtime is
2.5 hours permanently absent from every future analysis. Every other decision in this spec can
be revisited later; this one cannot.

Flip Follow as specified carries no magnitude requirement, so it can enter on a Delta of
0.001% — effectively at the Strike. It therefore differs from Delta Edge in two ways at once,
which limits what a comparison between them isolates. Recording sign-change magnitudes and hold
durations (story 7) is what keeps the alternative testable later without new collection.
