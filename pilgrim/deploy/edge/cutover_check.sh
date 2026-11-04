#!/usr/bin/env bash
# Cutover smoke checks 1-7 plus optional tailnet isolation check 11
# (SPLIT_PLAN §8.8 / Step 10.4), run ON an edge EC2.
#
# curl + coreutils only: the edge AMI ships nginx, pilgrim/web/ and
# pilgrim/deploy/, not a Python venv. Checks 8-10 are local-only and are not
# run here (8 reads a test DB; 9 stops services; 10 floods the gateway).
#
#   EDGE=http://127.0.0.1:8080 \
#   GATEWAY=https://pilgrim-origin.<tailnet>.ts.net \
#   bash pilgrim/deploy/edge/cutover_check.sh
#
# Check 6 expects a COLD media cache (first MISS, second HIT). Run it once
# right after deploy; on a rerun the first fetch may already be HIT.
set -uo pipefail

EDGE="${EDGE:-http://127.0.0.1:8080}"
GATEWAY="${GATEWAY:-}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
FAIL=0

pass() { printf 'PASS  %s\n' "$1"; }
fail() { printf 'FAIL  %s\n' "$1"; FAIL=1; }

# 1. Load-balancer health, no origin contact.
body=$(curl -fsS "$EDGE/edge-health" 2>/dev/null) || body=""
[ "$body" = "ok" ] && pass "1 edge-health 200 ok" || fail "1 edge-health: ${body:-no response}"

# 2. Static site.
if curl -fsS "$EDGE/" 2>/dev/null | grep -q "PILGRIM"; then
    pass "2 GET / serves index.html"
else
    fail "2 GET / does not serve the site"
fi

# 3. Admin route is 404 at the edge and at the gateway.
code=$(curl -s -o /dev/null -w '%{http_code}' "$EDGE/api/admin/dj/state")
[ "$code" = "404" ] && pass "3 edge admin 404" || fail "3 edge admin: $code"
if [ -n "$GATEWAY" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' "$GATEWAY/api/admin/dj/state")
    [ "$code" = "404" ] && pass "3 gateway admin 404" || fail "3 gateway admin: $code"
fi

# 3b. The production allowlist has no test route.
code=$(curl -s -o /dev/null -w '%{http_code}' "$EDGE/api/test/report")
[ "$code" = "404" ] && pass "3b edge test route 404" || fail "3b edge test route: $code"
if [ -n "$GATEWAY" ]; then
    code=$(curl -s -o /dev/null -w '%{http_code}' "$GATEWAY/api/test/report")
    [ "$code" = "404" ] && pass "3b gateway test route 404" || fail "3b gateway test route: $code"
fi

# 4. Station start/stop is 404 at both hops, GET and POST.
for m in GET POST; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -X "$m" "$EDGE/api/station/start")
    [ "$code" = "404" ] && pass "4 edge $m start 404" || fail "4 edge $m start: $code"
    if [ -n "$GATEWAY" ]; then
        code=$(curl -s -o /dev/null -w '%{http_code}' -X "$m" "$GATEWAY/api/station/start")
        [ "$code" = "404" ] && pass "4 gateway $m start 404" || fail "4 gateway $m start: $code"
    fi
done

# 5. Program has an epoch.
prog=$(curl -fsS "$EDGE/api/station/program" 2>/dev/null) || prog=""
if printf '%s' "$prog" | grep -q '"epoch"'; then
    pass "5 program 200 with epoch"
else
    fail "5 program missing epoch"
fi

# 6. Media MISS -> HIT, identical bytes, immutable cache header.
mid=$(printf '%s' "$prog" | sed -n 's/.*"media_id":[[:space:]]*\([0-9][0-9]*\).*/\1/p' | head -1)
if [ -n "$mid" ]; then
    curl -sS -D "$TMP/h1" -o "$TMP/b1" "$EDGE/api/media/$mid"
    curl -sS -D "$TMP/h2" -o "$TMP/b2" "$EDGE/api/media/$mid"
    cs1=$(grep -i '^x-cache-status:' "$TMP/h1" | awk '{print $2}' | tr -d '\r')
    cs2=$(grep -i '^x-cache-status:' "$TMP/h2" | awk '{print $2}' | tr -d '\r')
    h1=$(sha256sum "$TMP/b1" | awk '{print $1}')
    h2=$(sha256sum "$TMP/b2" | awk '{print $1}')
    cc=$(grep -i '^cache-control:' "$TMP/h1" | tr -d '\r')
    if [ "$cs1" = "MISS" ] && [ "$cs2" = "HIT" ] && [ "$h1" = "$h2" ] \
        && printf '%s' "$cc" | grep -q "immutable"; then
        pass "6 media $mid MISS->HIT, identical, immutable"
    else
        fail "6 media $mid: $cs1/$cs2 hashes=$([ "$h1" = "$h2" ] && echo same || echo DIFF) [$cc]"
    fi
else
    fail "6 no media_id in the program"
fi

# 7. POST /api/requests with EMPTY text: FastAPI validation on the origin
#    returns 422 application/json. This proves the full path, never reaches
#    moderation, and writes no request row (do not send a real request here).
code=$(curl -sS -D "$TMP/rh" -o "$TMP/rb" -w '%{http_code}' \
    -H 'Content-Type: application/json' -d '{"text":""}' \
    "$EDGE/api/requests")
ctype=$(grep -i '^content-type:' "$TMP/rh" | tr -d '\r')
if [ "$code" = "422" ] && printf '%s' "$ctype" | grep -qi "application/json" \
    && ! grep -qi "<html" "$TMP/rb"; then
    pass "7 POST /api/requests empty text -> 422 JSON (no row)"
else
    fail "7 POST /api/requests: status=$code [$ctype]"
fi

# 7b. Community voting routes are proxied, not edge-404ed (VOTING.md §2):
#     a vote on an unknown id reaches the origin and returns its 404 JSON,
#     no-store (a POST is never cached).
code=$(curl -sS -D "$TMP/vh" -o "$TMP/vb" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -d '{"voter_id":"00000000000000000000000000000000","value":1}' \
    "$EDGE/api/requests/999999/vote")
cc=$(grep -i '^cache-control:' "$TMP/vh" | tr -d '\r')
if [ "$code" = "404" ] && printf '%s' "$cc" | grep -qi "no-store"; then
    pass "7b POST vote unknown id -> 404 no-store (proxied)"
else
    fail "7b vote: status=$code [$cc]"
fi

# 7c. Rating route proxied; a bad body is the origin's 422 (no row written).
code=$(curl -sS -D "$TMP/rh2" -o "$TMP/rb2" -w '%{http_code}' \
    -H 'Content-Type: application/json' \
    -d '{"voter_id":"00000000000000000000000000000000","epoch":"x","seq":1,"stars":9}' \
    "$EDGE/api/station/rating")
cc=$(grep -i '^cache-control:' "$TMP/rh2" | tr -d '\r')
if [ "$code" = "422" ] && printf '%s' "$cc" | grep -qi "no-store"; then
    pass "7c POST rating bad body -> 422 no-store (proxied)"
else
    fail "7c rating: status=$code [$cc]"
fi

# 11. Optional tailnet isolation (SPLIT_PLAN Step 10.4). Set ORIGIN_TS_IP to
#     the origin's tailnet IP (`tailscale ip -4 pilgrim-origin` on the edge).
#     Proves a compromised edge can reach only the gateway's :443.
if [ -n "${ORIGIN_TS_IP:-}" ]; then
    if timeout 3 bash -c "</dev/tcp/$ORIGIN_TS_IP/443" 2>/dev/null; then
        pass "11 origin $ORIGIN_TS_IP:443 reachable"
    else
        fail "11 origin $ORIGIN_TS_IP:443 unreachable"
    fi
    for port in 22 80 5000 5001 5080 5081 8090; do
        if timeout 3 bash -c "</dev/tcp/$ORIGIN_TS_IP/$port" 2>/dev/null; then
            fail "11 origin $ORIGIN_TS_IP:$port unexpectedly reachable"
        else
            pass "11 origin $ORIGIN_TS_IP:$port blocked"
        fi
    done

    # Origin peer's PeerAPIURL, if `tailscale status --json` lists one.
    # grep only (no jq): a PeerAPIURL is an http://<tailnet IP>:<port> URL.
    origin_peerapi=""
    if command -v tailscale >/dev/null 2>&1; then
        origin_peerapi=$(tailscale status --json 2>/dev/null \
            | grep -o "http://[0-9.]*:[0-9]*" \
            | grep "$ORIGIN_TS_IP" | head -1)
    fi
    if [ -n "$origin_peerapi" ]; then
        if curl -fsS --max-time 3 "$origin_peerapi" >/dev/null 2>&1; then
            fail "11 origin PeerAPIURL reachable: $origin_peerapi"
        else
            pass "11 origin PeerAPIURL blocked"
        fi
    else
        echo "SKIP 11 origin PeerAPIURL not listed"
    fi
else
    echo "SKIP 11 tailnet isolation (set ORIGIN_TS_IP)"
fi

# Optional: other tailnet devices that must be unreachable from an edge
# (space-separated, e.g. the M5 host's own Tailscale IP).
for ip in ${DENY_TS_IPS:-}; do
    for port in 22 443 5000 5001 5080 5081; do
        if timeout 3 bash -c "</dev/tcp/$ip/$port" 2>/dev/null; then
            fail "11 $ip:$port unexpectedly reachable"
        else
            pass "11 $ip:$port blocked"
        fi
    done
done

printf '\n'
if [ "$FAIL" -eq 0 ]; then
    echo "cutover checks 1-7c passed"
    exit 0
fi
echo "cutover checks FAILED"
exit 1
