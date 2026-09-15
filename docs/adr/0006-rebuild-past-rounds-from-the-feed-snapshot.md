# Rebuild past Rounds from the feed's opening snapshot

ADR-0002 said no backtest was possible, because the exchange's API retains only about 2.5
hours of settled Rounds. That is true of the API. It is not true of the price feed: every
time the WebSocket connects it replays its whole retained series, measured at 5.6 hours of
BTC prices at roughly 5-second resolution — more than twice what the API remembers.

Rounds fall on a fixed 900-second grid, and a Round's Strike is the oracle price at its
start, so two points on that series fully determine what a past Round asked and how it
resolved. The harness therefore rebuilds them on startup, and the experiment begins with
roughly twenty scoreable Rounds instead of none.

Two limits, both measured rather than assumed:

The exchange samples its Strike at the moment the market is created on chain, a few seconds
before the grid boundary and by an amount that varies with block timing, so a rebuilt Strike
is not always identical to it. Across 22 live Rounds, 20 agreed exactly and 2 differed by a
few price units. The tightest Round moved 13 units between Strike and close, so no
disagreement of that size could change which Side won. Where the exchange can still answer,
its Strike replaces the rebuilt one anyway.

The snapshot carries this history for BTC only. XYZCL arrives as live ticks with no
backlog, so for oil the experiment still starts empty and accumulates forward. Results from
the two Symbols are therefore not comparable on day one, and a rebuilt Round is marked as
such so the distinction survives into the analysis.
