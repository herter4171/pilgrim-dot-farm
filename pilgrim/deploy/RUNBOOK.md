# RUNBOOK — move the origin to the M5 (SPLIT_PLAN §8)

**For the operator. DeepSeek does not execute any of this.** Everything below
runs on the **M5 Mac Studio** unless marked EC2. Keep the EC2's copy untouched
until cutover is verified; that copy is the rollback.

Prerequisites: the gateway files in `pilgrim/deploy/gateway/`, the edge
template in `pilgrim/deploy/edge/`, and the same git SHA on origin and edges.
The API is split (§11): edges serve only the public routes; the admin listener
is loopback-only.

---

## 1. Prerequisites on the M5

```sh
# Python 3.14 venv from the pinned requirements
cd ~/radio
python3.14 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

# ffmpeg/ffprobe on PATH (Homebrew), and verify
brew install ffmpeg
which ffmpeg ffprobe

# mlx-serve for song generation (reuse the existing working setup)
curl -s http://127.0.0.1:11234/v1/models

# Secrets: LITELLM_TOKEN and PILGRIM_DJ_TOKEN come from an env file outside
# the repo (see §5). Never commit them.
```

Power settings so the Mac never sleeps and restarts after a power failure:

```sh
sudo pmset -a sleep 0 disksleep 0 displaysleep 10 autorestart 1
sudo systemsetup -setrestartpowerfailure on   # if supported on this macOS
```

The repo must be checked out at the **same git SHA** the edges will run.

## 2. Config on the M5

Edit `pilgrim/config.yaml`. Each value and how to verify it:

| Key | Value on the M5 | How to check |
|---|---|---|
| `hosts.mlx_serve` | `http://127.0.0.1:11234` | `curl -s http://127.0.0.1:11234/v1/models` |
| `hosts.kokoro` | Kokoro host's LAN address, e.g. `http://192.168.68.89:8001` | `curl -s <kokoro>/health` and `curl -s <kokoro>/voices` |
| `hosts.litellm` | LiteLLM host's LAN address, e.g. `http://<5090-lan>:4000/v1` | `curl -s -H "Authorization: Bearer $LITELLM_TOKEN" <litellm>/models` |
| `hosts.searxng` | LiteLLM MCP endpoint, e.g. `http://<5090-lan>:4000/mcp` | MCP `initialize` + `tools/list` per `docs/backends.md` §5 |
| `hosts.sfx` | `http://127.0.0.1:8000` | `curl -s http://127.0.0.1:8000/health` |
| `station.host` | **`127.0.0.1`** — never `0.0.0.0` on the M5 | `lsof -nP -iTCP:5000 -sTCP:LISTEN` shows loopback only |
| `station.port` | `5000` | same |
| `station.admin_host` | `127.0.0.1` | `lsof -nP -iTCP:5001 -sTCP:LISTEN` shows loopback only |
| `station.admin_port` | `5001` | same; must differ from `port` |
| `station.serve_web` | `false` (edges serve `pilgrim/web/`) | `curl -s http://127.0.0.1:5000/` returns 404 |
| `visitors.trusted_proxies` | the gateway peer address observed in §6 | see §6 |

## 3. Freeze and copy data (EC2 → M5)

Stop the station on the EC2 first. Then take a **consistent** DB copy — never
`cp` a live WAL database:

```sh
# on the EC2
sqlite3 pilgrim/station.db ".backup /tmp/station.db.migrate"
rsync -a pilgrim/library/ m5:/Users/pilgrim/radio/pilgrim/library/   # ~12 GB
scp /tmp/station.db.migrate m5:/Users/pilgrim/radio/pilgrim/station.db
```

## 4. Verify the copy

On the M5, run the audit in its **default dry-run** mode. Never use `--apply`
on the migrated copy.

```sh
cd ~/radio
.venv/bin/python -m pilgrim.tools.audit_library
sqlite3 pilgrim/station.db "select count(*) from items"
```

Compare the item count with the EC2. Spot-check that a few `media_path` files
exist under `pilgrim/library/`.

## 5. Start the station under launchd

Create the wrapper and env file **outside the repo**:

```sh
sudo tee /usr/local/bin/pilgrim-station >/dev/null <<'EOF'
#!/bin/sh
set -a
. /Users/pilgrim/.config/pilgrim/station.env
set +a
exec /Users/pilgrim/radio/.venv/bin/python -m pilgrim.server
EOF
sudo chmod 755 /usr/local/bin/pilgrim-station

install -m 600 /dev/null /Users/pilgrim/.config/pilgrim/station.env
# edit that file (mode 600):
#   LITELLM_TOKEN=...
#   PILGRIM_DJ_TOKEN=...
#   PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/Users/pilgrim/radio/.venv/bin

mkdir -p /Users/pilgrim/Library/Logs/pilgrim
cp pilgrim/deploy/launchd/farm.pilgrim.station.plist.example \
   ~/Library/LaunchAgents/farm.pilgrim.station.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/farm.pilgrim.station.plist
launchctl kickstart -k gui/$(id -u)/farm.pilgrim.station
```

Verify: `curl -s http://127.0.0.1:5000/api/health` returns the public
`{on_air, time}` shape; `curl -s http://127.0.0.1:5001/api/health` returns the
full admin health; `/` on 5000 is 404 (`serve_web: false`). Logs:
`~/Library/Logs/pilgrim/station.out.log` / `.err.log`.

## 6. Start the gateway and observe the peer address

Render the gateway config into `pilgrim/deploy/gateway/` (gitignored):

```sh
cd ~/radio
.venv/bin/python -m pilgrim.tools.render_deploy \
  pilgrim/deploy/gateway/nginx.conf.template pilgrim/deploy/gateway/nginx.conf \
  --set GATEWAY_LISTEN=127.0.0.1:8090 \
  --set ORIGIN_PUBLIC_HOSTPORT=host.docker.internal:5000 \
  --set TRUSTED_EDGE_NET=127.0.0.1 \
  --set EXTRA_ALLOW_INCLUDE=/etc/nginx/extra-none.conf
```

`host.docker.internal:5000` is the origin's public listener as seen from the
gateway container. **Verify this on the M5**; replace it if Docker Desktop
exposes the host differently.

```sh
docker compose --env-file /secure/pilgrim-gateway.env up -d
```

The env file (outside the repo, mode 600) holds `TS_AUTHKEY=...`.

Make one request through the gateway from another tailnet machine (or via
MagicDNS on the M5):

```sh
curl -s https://pilgrim-origin.<tailnet>.ts.net/api/health
curl -s -X POST https://pilgrim-origin.<tailnet>.ts.net/api/visitors
```

Read the origin's peer address from the visitor row or the access log:

```sh
sqlite3 ~/radio/pilgrim/station.db "select ip from visitor_ips"
```

Put that address (or its CIDR) in `visitors.trusted_proxies` and restart the
station. With the gateway trusted, a request through it records the edge's
`X-Real-IP`, not the gateway peer.

## 7. Edges

Render `pilgrim/deploy/edge/nginx.conf.template` with production values:

```sh
.venv/bin/python -m pilgrim.tools.render_deploy \
  pilgrim/deploy/edge/nginx.conf.template /etc/nginx/nginx.conf \
  --set EDGE_LISTEN=:8080 \
  --set WEB_ROOT=/var/www/pilgrim \
  --set CACHE_DIR=/var/cache/nginx/pilgrim \
  --set MEDIA_CACHE_MAX=20g \
  --set ORIGIN_SCHEME=https \
  --set ORIGIN_HOSTPORT=pilgrim-origin.<tailnet>.ts.net:443 \
  --set ORIGIN_HOST=pilgrim-origin.<tailnet>.ts.net \
  --set REALIP_INCLUDE=/etc/nginx/cloudflare-realip.conf \
  --set CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
  --set EXTRA_ALLOW_INCLUDE=/etc/nginx/extra-none.conf
```

`REALIP_INCLUDE` is the operator's Cloudflare include
(`real_ip_header CF-Connecting-IP;` + `set_real_ip_from` for each published
Cloudflare range, refreshed periodically). TLS is the operator's IaC
(Cloudflare → edge), not this template.

**Order matters:** nginx resolves the upstream hostname at start, so start the
edge's nginx **after** tailscaled is up and MagicDNS resolves
`pilgrim-origin.<tailnet>.ts.net`.

## 8. Cut over

Point Cloudflare at the edges, then run the curl-only cutover check **from one
edge EC2, against that edge's own nginx**. The edge AMI ships nginx,
`pilgrim/web/` and `pilgrim/deploy/` — no Python venv — so the check is
`pilgrim/deploy/edge/cutover_check.sh` (curl + coreutils only).

Why from an edge: the tailnet policy lets only edges reach the gateway
(checks 3–4 hit it directly), and through Cloudflare + the load balancer
check 6's two media fetches can land on different edges or be answered by
Cloudflare, so MISS→HIT is not observable. Use the edge's local listen
address (`EDGE_LISTEN`), not the public hostname:

```sh
# on one edge EC2, from the repo checkout
EDGE=http://127.0.0.1:8080 \
GATEWAY=https://pilgrim-origin.<tailnet>.ts.net \
bash pilgrim/deploy/edge/cutover_check.sh
```

The script prints PASS/FAIL and exits non-zero on any failure. It checks:
`/edge-health` is 200; `/` serves the site; admin, control and `/api/test/*`
paths are 404 at both hops; program has `epoch`; media is `MISS` then `HIT`
with identical bytes and `immutable`; `POST /api/requests` with empty text
returns `422 application/json` from the origin (FastAPI validation; it never
reaches moderation and writes no request row). Run it once right after
deploy: check 6 expects a cold cache.

Checks 8–10 are local-only (8 reads the local test origin's DB; 9 stops
services; 10 floods the gateway), so never run them in prod.

Then, from anywhere, sanity-check the public path through Cloudflare:
`curl -s https://<public host>/edge-health` → `ok`, `curl -sI https://<public host>/`
→ 200, and `curl -s https://<public host>/api/health` → `"on_air": true`.

## 9. Rollback

Restart the station on the EC2 from its untouched copy. Requests and airplay
recorded on the M5 after cutover are lost unless you copy the DB back with the
same `.backup` method:

```sh
sqlite3 ~/radio/pilgrim/station.db ".backup /tmp/station.db.rollback"
scp /tmp/station.db.rollback ec2:radio/pilgrim/station.db
```

## 10. Backups

- `station.db`: Litestream, or a cron `.backup` to the NAS — never copy the
  live DB file directly:

  ```cron
  0 * * * * sqlite3 /Users/pilgrim/radio/pilgrim/station.db \
    ".backup /Volumes/NAS/pilgrim/station.db.bak"
  ```

- `library/`: nightly `rsync -a` to the NAS.
- Restore-test the DB backup at least once before relying on it.
