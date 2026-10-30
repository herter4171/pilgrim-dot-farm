#!/usr/bin/env bash
# make split-smoke (SPLIT_PLAN §7.3): start the loopback stack and run the
# scripted PASS/FAIL checks through the edge. Always tears down.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
STATE="${PILGRIM_SPLIT_STATE:-/tmp/pilgrim-split-local}"
EDGE_PORT="${PILGRIM_EDGE_PORT:-18080}"
GATEWAY_PORT="${PILGRIM_GATEWAY_PORT:-18090}"

# The smoke checks run against the REAL public app with the PRODUCTION
# gateway allowlist (SPLIT_PLAN §7.3), never e2e_server.
export PILGRIM_SPLIT_ORIGIN=real
trap 'bash "$ROOT/pilgrim/deploy/local/stack.sh" stop' EXIT
bash "$ROOT/pilgrim/deploy/local/stack.sh" start

# Export the compose paths for split_smoke's docker compose stop/start calls.
# shellcheck disable=SC1090
set -a; . "$STATE/env"; set +a

"$ROOT/.venv/bin/python" -m pilgrim.tools.split_smoke \
    --state-dir "$STATE" \
    --edge "http://127.0.0.1:$EDGE_PORT" \
    --gateway "http://127.0.0.1:$GATEWAY_PORT"
