# Paper trading only; the harness holds no private key

9lives issues no API keys. Authenticating to the trading endpoint means holding a funded
wallet's private key and signing a challenge to mint a 30-day session secret, so any
process that can trade is a process that can be drained. We are still trying to establish
whether any Strategy has an edge at all, which is exactly the point of least information
and highest risk. So the harness reads the public GraphQL endpoint only, records Paper
Trades, and sends no orders. Live execution is a separate decision to be made after the
results are in.

A useful consequence: the harness holds no secrets of any kind, so it can be deployed,
copied, and debugged without key-handling ceremony.
