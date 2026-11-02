#!/usr/bin/env bash
# make e2e-split (SPLIT_PLAN §7.2): origin + gateway + edge on loopback, then
# the Playwright gap test with baseURL set to the EDGE. Always tears down.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
STATE="${PILGRIM_SPLIT_STATE:-/tmp/pilgrim-split-local}"
EDGE_PORT="${PILGRIM_EDGE_PORT:-18080}"

# The fixture origin serves seeded covers itself (ALBUM_ART.md Step 8); the
# edge proxies /api/* so the client's art_url path is exercised end to end.
export PILGRIM_E2E_ART_BASE="http://127.0.0.1:$EDGE_PORT"

trap 'bash "$ROOT/pilgrim/deploy/local/stack.sh" stop' EXIT
bash "$ROOT/pilgrim/deploy/local/stack.sh" start

cd "$ROOT/pilgrim/tests/e2e"
PILGRIM_E2E_BASE_URL="http://127.0.0.1:$EDGE_PORT" \
    npx --yes playwright test gap_test.spec.ts
