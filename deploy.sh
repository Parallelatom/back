#!/usr/bin/env sh
# Build and (re)start the Collector, stamping the running build with this commit.
set -eu
cd "$(dirname "$0")"
STRATEGY_LAB_VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
export STRATEGY_LAB_VERSION
mkdir -p data

# Load CLOUDFLARE_TUNNEL_TOKEN if it has been set up. Without it the tunnel is skipped and
# the dashboard stays reachable only over an SSH port-forward.
[ -f .env ] && . ./.env

if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
  export CLOUDFLARE_TUNNEL_TOKEN
  docker compose --profile tunnel up -d --build
else
  echo "No CLOUDFLARE_TUNNEL_TOKEN set; starting without the tunnel."
  echo "Reach the dashboard with: ssh -L 8000:localhost:8000 <this-host>"
  docker compose up -d --build
fi

docker compose ps
