# Record every Round as raw observations, not just the Rounds we would have traded

The upstream API retains roughly 2.5 hours of settled-Round history and exposes no archive,
so anything the Collector fails to write down is gone permanently. Recording only the Rounds
that a Strategy entered would mean that every later change to a Strategy — a different
threshold, a different Trade Window, a fifth Strategy — requires starting data collection
over from zero and waiting days again.

So the Collector is deliberately ignorant of the Strategies. It writes the full observation
record for every Round on every Symbol: the price series through the Round, the Strike, the
market odds over time, and the settled close. Strategies are then scored by replaying that
record offline, which makes a new Strategy answerable in seconds against all history already
collected.

The cost is storage volume and a Collector that does more work than any single Strategy needs.
That is accepted: storage is cheap and recoverable, elapsed calendar time is not.
