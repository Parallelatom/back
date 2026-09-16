#!/usr/bin/env bash
set -euo pipefail
set +x
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
symbol="${1:-}"
case "$symbol" in
  BTC|XYZCL) shift ;;
  *) echo 'Usage: bash run-live.sh BTC|XYZCL [--execute --watch]' >&2; exit 2 ;;
esac
if [[ ! -f execution-secrets.env ]]; then
  echo 'Create execution-secrets.env from its example and fill it locally first.' >&2
  exit 2
fi
source ./execution-secrets.env
exec .venv/bin/python -m strategy_lab.execution.run_live \
  --symbol "$symbol" --config execution-accounts.json \
  --ledger "data/live-${symbol}.db" --halt-file "data/HALT-${symbol}" "$@"
