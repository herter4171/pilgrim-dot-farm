#!/usr/bin/env bash
# Render both deploy templates with local values and validate them with
# `nginx -t` (SPLIT_PLAN §5.4). The temp dir is mounted rw because nginx
# validates proxy_cache_path by creating/chowning its cache directories.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../../.." && pwd)
PY="$ROOT/.venv/bin/python"
IMAGE="nginx:1.27-alpine"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$TMP/cache/media" "$TMP/cache/micro" "$TMP/web" \
         "$TMP/edge" "$TMP/gateway"
cp "$ROOT/pilgrim/deploy/edge/realip-local.conf" "$TMP/realip.conf"
cp "$ROOT/pilgrim/deploy/gateway/extra-none.conf" "$TMP/extra-none.conf"
printf '<!doctype html><title>edge</title>\n' > "$TMP/web/index.html"

cd "$ROOT"
"$PY" -m pilgrim.tools.render_deploy \
    pilgrim/deploy/edge/nginx.conf.template "$TMP/edge/nginx.conf" \
    --set EDGE_LISTEN=127.0.0.1:18080 \
    --set WEB_ROOT=/cfg/web \
    --set CACHE_DIR=/cfg/cache \
    --set MEDIA_CACHE_MAX=1g \
    --set ORIGIN_SCHEME=http \
    --set ORIGIN_HOSTPORT=127.0.0.1:18090 \
    --set ORIGIN_HOST=127.0.0.1 \
    --set REALIP_INCLUDE=/cfg/realip.conf \
    --set CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    --set EXTRA_ALLOW_INCLUDE=/cfg/extra-none.conf

"$PY" -m pilgrim.tools.render_deploy \
    pilgrim/deploy/gateway/nginx.conf.template "$TMP/gateway/nginx.conf" \
    --set GATEWAY_LISTEN=127.0.0.1:18090 \
    --set ORIGIN_PUBLIC_HOSTPORT=127.0.0.1:5000 \
    --set TRUSTED_EDGE_NET=127.0.0.1 \
    --set EXTRA_ALLOW_INCLUDE=/cfg/extra-none.conf

docker run --rm -v "$TMP:/cfg" "$IMAGE" nginx -t -c /cfg/edge/nginx.conf
docker run --rm -v "$TMP:/cfg" "$IMAGE" nginx -t -c /cfg/gateway/nginx.conf
