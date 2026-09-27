#!/usr/bin/env bash
# Start the Telegram monitor the same way the runners start: secrets sourced here rather
# than handed to a service manager, and exec so the process is the one being supervised.
set -euo pipefail
set +x
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ ! -f telegram-secrets.env ]]; then
  echo 'Create telegram-secrets.env from its example and fill it locally first.' >&2
  exit 2
fi
source ./telegram-secrets.env
exec .venv/bin/python -m strategy_lab.execution.notify \
  --recordings data/lab.db --config execution-accounts.json \
  --market BTC=data/live-BTC.db:data/BTC-continuous.log \
  --market XYZCL=data/live-XYZCL.db:data/XYZCL-continuous.log "$@"
