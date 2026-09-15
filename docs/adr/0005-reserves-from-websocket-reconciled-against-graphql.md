# Track Reserves from the WebSocket feed, reconciled against GraphQL every 30 seconds

Reserves only move when someone trades, and trades arrive on the WebSocket feed as they
happen. Polling GraphQL to ask "has anyone traded yet?" would spend a request every few
seconds to learn "no" almost every time: median volume is one ticket per Round, and each
GraphQL round-trip costs 1.1-1.2 seconds against an endpoint that does no edge caching.

So the Collector maintains Reserves from the WebSocket feed and recomputes prices locally.
The risk is that a dropped event desynchronises the local Reserves silently and corrupts
every Paper Trade for the rest of the Round. To catch that, the Collector polls GraphQL
every 30 seconds and compares. On a mismatch, GraphQL wins and the divergence is logged.

The logging is the point as much as the correction: after a few days the log says whether
the WebSocket feed is trustworthy on its own. Without a second source there would be no way
to discover that it had been lying.
