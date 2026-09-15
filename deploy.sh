#!/usr/bin/env sh
# Build and (re)start the Collector, stamping the running build with this commit.
set -eu
cd "$(dirname "$0")"
STRATEGY_LAB_VERSION="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
export STRATEGY_LAB_VERSION
mkdir -p data
docker compose up -d --build
docker compose ps
