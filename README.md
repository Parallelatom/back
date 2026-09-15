# 9lives Strategy Lab

A research harness for the 9lives 15-minute prediction markets on Arbitrum One. It records
every Round on BTC and XYZCL, and scores trading Strategies against those recordings. It
places no orders and holds no keys.

Start with [CONTEXT.md](CONTEXT.md) for the vocabulary, [docs/specs](docs/specs) for what is
being built, and [docs/adr](docs/adr) for why it is built that way.

## Running the Collector

```sh
./deploy.sh
```

Then watch it work:

```sh
docker compose logs -f collector
```

The database lands in `./data/lab.db`. Copy it off the host with `scp` whenever you want to
look at it locally.

## Why uptime matters more than anything else here

The exchange's API remembers about 2.5 hours of settled Rounds, and the price feed replays
about 5.6 hours of BTC prices when it connects. **XYZCL has no backlog at all.** Any stretch
the Collector is down is a stretch that can never be recovered for oil, and beyond about five
hours it cannot be recovered for BTC either. That is why the Collector restarts unless
explicitly stopped, and why deploying sooner beats deploying tidily.

## Reading the results

```sh
STRATEGY_LAB_DB=data/lab.db .venv/bin/python -m strategy_lab.score
```

Hit Rate is the finding. The Bankroll column is indicative only: a 1 USD ticket is most of a
typical Round's volume, so no real order of that size would fill at the prices it assumes.
Break-even sits at a 76.1% Hit Rate, because a win returns about +0.31 against a loss of 1.00.

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```

Tests that talk to the live exchange are skipped unless you ask for them:

```sh
STRATEGY_LAB_LIVE=1 .venv/bin/python -m pytest
```
