#!/usr/bin/env bash
# make pg-test (SCALABILITY.md Step 2): the whole unit suite against a
# throwaway Postgres container. Fakes only; the only "backend" is this local
# container (AGENTS §1.2). Postgres major pinned to what RDS runs (18).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY="${PY:-$ROOT/.venv/bin/python}"
DOCKER="${DOCKER:-docker}"
IMAGE="postgres:18-alpine"
PORT="${PILGRIM_PG_TEST_PORT:-55432}"
NAME="pilgrim-pg-test-$$"

cleanup() { "$DOCKER" rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

"$DOCKER" run -d --name "$NAME" -e POSTGRES_USER=pilgrim -e POSTGRES_PASSWORD=pilgrim \
    -e POSTGRES_DB=pilgrim -p "127.0.0.1:$PORT:5432" "$IMAGE" >/dev/null
# The official image runs a temporary server during initdb and restarts it,
# so one pg_isready is not enough: wait for two "ready" log lines.
for _ in $(seq 1 300); do
    n=$("$DOCKER" logs "$NAME" 2>&1 | grep -c "database system is ready to accept connections" || true)
    if [ "$n" -ge 2 ] && "$DOCKER" exec "$NAME" pg_isready -U pilgrim -q >/dev/null 2>&1; then break; fi
    sleep 0.2
done
"$DOCKER" exec "$NAME" pg_isready -U pilgrim -q || { echo "postgres never became ready" >&2; exit 1; }

cd "$ROOT"
export PILGRIM_TEST_DATABASE_URL="postgresql://pilgrim@127.0.0.1:$PORT/pilgrim"
export PILGRIM_TEST_DATABASE_PASSWORD="pilgrim"
PATH="$HOME/bin:$PATH" "$PY" -m pytest -q "$@"
