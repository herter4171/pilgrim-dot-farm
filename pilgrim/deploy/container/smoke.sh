#!/usr/bin/env bash
# Container smoke (SCALABILITY.md Step 1): run the FAKES-ONLY e2e server from
# the test-only `smoke` stage (the web image + ffmpeg), then check health, a
# playable programme and the static site through the published port. No real backend is contacted (AGENTS §1.2):
# pilgrim.tests.e2e.e2e_server seeds tone MP3s into a temp library.
#
#   make image-smoke            # IMAGE=pilgrim by default
#   IMAGE=myrepo/pilgrim make image-smoke
set -euo pipefail

IMAGE="${IMAGE:-pilgrim}:smoke"
DOCKER="${DOCKER:-docker}"
PORT="${PILGRIM_SMOKE_PORT:-18088}"
NAME="pilgrim-image-smoke-$$"

cleanup() { "$DOCKER" rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; "$DOCKER" logs "$NAME" 2>&1 | tail -40 >&2 || true; exit 1; }

"$DOCKER" run -d --name "$NAME" -p "127.0.0.1:$PORT:8080" "$IMAGE" \
    python -m uvicorn pilgrim.tests.e2e.e2e_server:app --host 0.0.0.0 --port 8080 >/dev/null

base="http://127.0.0.1:$PORT"
for _ in $(seq 1 300); do
    if curl -sf "$base/api/health" >/dev/null 2>&1; then break; fi
    sleep 0.2
done
curl -sf "$base/api/health" >/dev/null || fail "/api/health never answered 200"
echo "PASS health"

prog=$(curl -sf "$base/api/station/program") || fail "/api/station/program"
first=$(printf '%s' "$prog" | python3 -I -c '
import json, sys
items = json.load(sys.stdin)["items"]
assert items, "no committed items"
assert all(it.get("media_id") and it.get("duration_s") for it in items), "item without media"
print(items[0]["media_id"])
print(f"PASS programme ({len(items)} items)", file=sys.stderr)
') || fail "programme is not playable"
# The e2e server serves media locally (no S3): the first item must stream.
ctype=$(curl -s -o /dev/null -w '%{content_type}' "$base/api/media/$first")
case "$ctype" in audio/*) echo "PASS media ($ctype)";; *) fail "media $first: content-type $ctype";; esac

code=$(curl -s -o /dev/null -w '%{http_code}' "$base/")
[ "$code" = 200 ] || fail "static site: GET / -> $code"
echo "PASS static site"

code=$(curl -s -o /dev/null -w '%{http_code}' "$base/api/admin/dj/state")
[ "$code" = 404 ] || fail "admin route reachable on the public port ($code)"
echo "PASS admin hidden"

uid=$("$DOCKER" exec "$NAME" id -u)
[ "$uid" = 10001 ] || fail "process runs as uid $uid, expected 10001"
echo "PASS non-root"
echo "PASS image smoke: $IMAGE"
