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

## The dashboard

`./deploy.sh` starts it alongside the Collector. It listens on `127.0.0.1:8000` only, so
nothing is exposed to the internet by default and no inbound port needs opening on the VPS
firewall. It mounts the database read-only, so it cannot disturb collection whatever it does.

Running `python -m strategy_lab.dashboard` directly also binds to loopback by default.
Compose explicitly binds the process to `0.0.0.0` inside its container so the tunnel can
reach it; the published host port remains loopback-only. The server permits four concurrent
connections, applies a ten-second socket inactivity timeout, and rejects excess connections
with HTTP 503. Page renders are serialized and cached for up to five seconds, with at most
eight filter/page combinations retained. Cloudflare Access is still required for authentication
when publishing a hostname.

Database indexes and incremental reconstruction bookkeeping are installed automatically by
the Collector on startup. The first upgrade schedules existing price history for one catch-up
pass and rechecks Partial Rounds; subsequent passes read only ranges with newly recorded prices,
including historical prices received after a reconnect.

Before the tunnel exists, reach it over SSH:

```sh
ssh -L 8000:localhost:8000 user@your-vps
```

then open <http://localhost:8000>. If something already holds port 8000 on the host, set
`STRATEGY_LAB_HOST_PORT` in `.env` to publish it elsewhere; the tunnel is unaffected either
way, since it reaches the dashboard over the compose network rather than through the host.

## Publishing it on your own subdomain

The connector dials out to Cloudflare, so the VPS still accepts no inbound connections. Who
may look is decided by Cloudflare Access, not by anything in this repository.

**In the Cloudflare dashboard** (these steps need your login, so they cannot be scripted here):

1. **Zero Trust → Networks → Tunnels → Create a tunnel → Cloudflared.** Name it, then copy
   the token it shows. Ignore the install instructions — `docker compose` runs the connector.
2. On the tunnel's **Public Hostname** tab, add a hostname: pick your subdomain, set the
   service to **HTTP** and the URL to `dashboard:8000`. That name resolves on the compose
   network, which is why no port has to be published.
3. **Zero Trust → Access → Applications → Add an application → Self-hosted.** Point it at
   the same hostname and add a policy allowing your own email address. Do this *before* the
   first deploy: between step 2 and this one the page is open to anyone who finds the name.

**On the VPS:**

```sh
cp .env.example .env    # then paste the token into it
./deploy.sh
```

`deploy.sh` starts the connector only when a token is present, because cloudflared without
one restarts forever. `.env` is gitignored.

To check it took:

```sh
docker compose logs tunnel | grep -i registered
```

## Reading the results

```sh
STRATEGY_LAB_DB=data/lab.db .venv/bin/python -m strategy_lab.score
```

Hit Rate is the finding. The Bankroll column is indicative only: a 1 USD ticket is most of a
typical Round's volume, so no real order of that size would fill at the prices it assumes.
Break-even sits at a 76.1% Hit Rate, because a win returns about +0.31 against a loss of 1.00.

## Backing up

The recordings cannot be rebuilt. The exchange remembers about two and a half hours and the
price feed a few more, so a disk lost on Thursday cannot be re-collected on Friday, however
quickly it is noticed. Everything else here comes back from git in a minute.

```sh
./backup.sh
```

Writes a consistent copy to `~/lab-backups` and keeps the last fourteen. Run it from cron:

```sh
(crontab -l 2>/dev/null; echo "0 * * * * cd $PWD && ./backup.sh >> /tmp/lab-backup.log 2>&1") | crontab -
```

Copy one off the host now and then — a backup on the same disk as the original is not one.

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
