#!/usr/bin/env bash
# Local split stack lifecycle (SPLIT_PLAN §7.1/§7.2): e2e origin + gateway +
# edge on loopback. `start` renders both templates with local values, starts
# the e2e server on an OS-chosen free port, then brings up the compose project.
# `stop` tears everything down (including the origin process).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY="$ROOT/.venv/bin/python"
STATE="${PILGRIM_SPLIT_STATE:-/tmp/pilgrim-split-local}"
EDGE_PORT="${PILGRIM_EDGE_PORT:-18080}"
GATEWAY_PORT="${PILGRIM_GATEWAY_PORT:-18090}"
PROJECT="pilgrim-split-local"
COMPOSE="$ROOT/pilgrim/deploy/local/compose.yaml"

port_free() {
    ! ss -ltnH | awk '{print $4}' | grep -qE "[:.]$1$"
}

pick_port() {
    "$PY" - <<'PY'
import socket
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    print(s.getsockname()[1])
PY
}

wait_url() {
    local url=$1
    for _ in $(seq 1 150); do
        if curl -sf "$url" >/dev/null 2>&1; then return 0; fi
        sleep 0.1
    done
    return 1
}

start_origin() {
    local port=$1
    ( cd "$ROOT" && exec "$PY" -m uvicorn pilgrim.tests.e2e.e2e_server:app \
        --host 127.0.0.1 --port "$port" >> "$STATE/origin.log" 2>&1 ) &
    echo $! > "$STATE/origin.pid"
    wait_url "http://127.0.0.1:$port/api/health" || {
        echo "error: e2e origin failed to start on port $port" >&2
        tail -30 "$STATE/origin.log" >&2
        return 1
    }
}

stop_origin() {
    if [ -f "$STATE/origin.pid" ]; then
        kill "$(cat "$STATE/origin.pid")" 2>/dev/null || true
        rm -f "$STATE/origin.pid"
    fi
}

render() {
    local origin_port=$1
    mkdir -p "$STATE/cache/media" "$STATE/cache/micro" \
             "$STATE/edge" "$STATE/gateway"
    cp "$ROOT/pilgrim/deploy/edge/realip-local.conf" "$STATE/realip.conf"
    cp "$ROOT/pilgrim/deploy/gateway/extra-e2e.conf" "$STATE/extra.conf"

    ( cd "$ROOT" && "$PY" -m pilgrim.tools.render_deploy \
        pilgrim/deploy/edge/nginx.conf.template "$STATE/edge/nginx.conf" \
        --set EDGE_LISTEN="127.0.0.1:$EDGE_PORT" \
        --set WEB_ROOT=/web \
        --set CACHE_DIR=/cache \
        --set MEDIA_CACHE_MAX=1g \
        --set ORIGIN_SCHEME=http \
        --set ORIGIN_HOSTPORT="127.0.0.1:$GATEWAY_PORT" \
        --set ORIGIN_HOST=127.0.0.1 \
        --set REALIP_INCLUDE=/etc/nginx/realip.conf \
        --set CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
        --set EXTRA_ALLOW_INCLUDE=/etc/nginx/extra.conf )

    ( cd "$ROOT" && "$PY" -m pilgrim.tools.render_deploy \
        pilgrim/deploy/gateway/nginx.conf.template "$STATE/gateway/nginx.conf" \
        --set GATEWAY_LISTEN="127.0.0.1:$GATEWAY_PORT" \
        --set ORIGIN_PUBLIC_HOSTPORT="127.0.0.1:$origin_port" \
        --set TRUSTED_EDGE_NET=127.0.0.1 \
        --set EXTRA_ALLOW_INCLUDE=/etc/nginx/extra.conf )
}

write_env() {
    {
        echo "export PILGRIM_EDGE_CONF=$STATE/edge/nginx.conf"
        echo "export PILGRIM_GATEWAY_CONF=$STATE/gateway/nginx.conf"
        echo "export PILGRIM_REALIP_CONF=$STATE/realip.conf"
        echo "export PILGRIM_EXTRA_CONF=$STATE/extra.conf"
        echo "export PILGRIM_WEB_ROOT=$ROOT/pilgrim/web"
        echo "export PILGRIM_CACHE_DIR=$STATE/cache"
        echo "export PILGRIM_UID=$(id -u)"
        echo "export PILGRIM_GID=$(id -g)"
    } > "$STATE/env"
}

case "${1:-start}" in
start)
    for p in "$EDGE_PORT" "$GATEWAY_PORT"; do
        if ! port_free "$p"; then
            echo "error: port $p is busy; set PILGRIM_EDGE_PORT/PILGRIM_GATEWAY_PORT" >&2
            exit 1
        fi
    done
    rm -rf "$STATE" || true
    mkdir -p "$STATE"
    ORIGIN_PORT=$(pick_port)
    echo "$ORIGIN_PORT" > "$STATE/origin_port"
    render "$ORIGIN_PORT"
    write_env
    start_origin "$ORIGIN_PORT"
    # shellcheck disable=SC1090
    set -a; . "$STATE/env"; set +a
    docker compose -f "$COMPOSE" --env-file "$STATE/env" -p "$PROJECT" up -d
    wait_url "http://127.0.0.1:$EDGE_PORT/edge-health" || {
        echo "error: edge did not come up" >&2
        docker compose -f "$COMPOSE" --env-file "$STATE/env" -p "$PROJECT" logs --tail 40 >&2
        exit 1
    }
    wait_url "http://127.0.0.1:$GATEWAY_PORT/api/health" || {
        echo "error: gateway did not come up" >&2
        docker compose -f "$COMPOSE" --env-file "$STATE/env" -p "$PROJECT" logs --tail 40 >&2
        exit 1
    }
    echo "split stack up: origin=$ORIGIN_PORT gateway=$GATEWAY_PORT edge=$EDGE_PORT"
    ;;
stop)
    if [ -f "$STATE/env" ]; then
        # shellcheck disable=SC1090
        set -a; . "$STATE/env"; set +a
        docker compose -f "$COMPOSE" --env-file "$STATE/env" -p "$PROJECT" \
            down --remove-orphans || true
    fi
    stop_origin
    rm -rf "$STATE" || true
    ;;
status)
    cat "$STATE/origin_port" 2>/dev/null || echo "no origin port"
    ;;
*)
    echo "usage: $0 {start|stop|status}" >&2
    exit 2
    ;;
esac
