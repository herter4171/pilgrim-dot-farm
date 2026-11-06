#!/usr/bin/env bash
# LiteLLM relay on the bastion (pf): start | stop | status | test.
#   MAC_TAILNET_NAME=justins-mac-studio.tail063158.ts.net bash relay.sh start
# Renders the template with the Mac's tailnet name and runs the pinned nginx
# image on host port 4000 (the pg-ec2 security group admits 4000 from itself,
# which is what the Fargate tasks carry).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
NAME=pilgrim-litellm-relay
IMAGE=nginx:1.30.6-alpine
MAC="${MAC_TAILNET_NAME:-justins-mac-studio.tail063158.ts.net}"
STATE="${RELAY_STATE:-$HOME/pilgrim-relay}"
case "${1:-status}" in
  start)
    mkdir -p "$STATE"
    sed "s/@@MAC_TAILNET_NAME@@/$MAC/g" "$HERE/litellm-relay.nginx.conf" > "$STATE/nginx.conf"
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --name "$NAME" --restart unless-stopped --network host \
      -v "$STATE/nginx.conf:/etc/nginx/nginx.conf:ro" "$IMAGE" nginx -g 'daemon off;' >/dev/null
    echo "started $NAME -> https://$MAC (port 4000)";;
  stop)   docker rm -f "$NAME" >/dev/null 2>&1 && echo "stopped" || echo "not running";;
  status) docker ps --filter "name=$NAME" --format '{{.Names}} {{.Status}}';;
  test)
    # 401 from LiteLLM = the whole chain works (no token is sent).
    curl -s -o /dev/null -w 'relay-health %{http_code}\n' http://127.0.0.1:4000/relay-health
    curl -s -o /dev/null -w 'litellm via relay %{http_code} (401 expected without a token)\n' http://127.0.0.1:4000/v1/models;;
  *) echo "usage: relay.sh start|stop|status|test" >&2; exit 2;;
esac
