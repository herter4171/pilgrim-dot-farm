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
| `songs.gen_backends` | `[{name: litellm, slots: 3, auth: litellm}]` (URL derived from `hosts.litellm` + `/vllm`; direct mode lists each box) | `make smoke` prints `music:<name> OK`; `make smoke SMOKE_ARGS=--gen` renders 10 s per slot (only when asked) |
| `hosts.kokoro` | Kokoro host's LAN address, e.g. `http://192.168.68.89:8001` | `curl -s <kokoro>/health` and `curl -s <kokoro>/voices` |
| `hosts.litellm` | `https://litellm.twr.church/v1` | `curl -s -H "Authorization: Bearer $LITELLM_TOKEN" <litellm>/models` |
| `hosts.mcp` | LiteLLM MCP endpoint, `https://litellm.twr.church/mcp` (DJ time tool) | MCP `initialize` + `tools/list` per `docs/backends.md` §5 |
| `hosts.brave` | `https://api.search.brave.com`, with `BRAVE_SEARCH_KEY` in `.env` | `make smoke` prints `brave … OK (ok)` (one 1-result news query) |
| `hosts.sfx` | `http://192.168.68.88:8000` | `curl -s http://192.168.68.88:8000/health` |
| `station.host` | **`127.0.0.1`** — never `0.0.0.0` on the M5 | `lsof -nP -iTCP:5080 -sTCP:LISTEN` shows loopback only |
| `station.port` | `5080` (5000 is macOS AirPlay Receiver) | same |
| `station.admin_host` | `127.0.0.1` | `lsof -nP -iTCP:5081 -sTCP:LISTEN` shows loopback only |
| `station.admin_port` | `5081` | same; must differ from `port` |
| `station.serve_web` | `false` (edges serve `pilgrim/web/`) | `curl -s http://127.0.0.1:5080/` returns 404 |
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

**Keep both trailing slashes on the rsync.** Without them (or with `scp -r
.../library .../library`) the files land in `pilgrim/library/library/`.

**`items.media_path` is absolute** (`/home/ec2-user/radio/pilgrim/library/...`
on the EC2). With the station stopped and after a `.backup`, rewrite the
prefix to the M5 checkout:

```sh
cd <checkout>/pilgrim
sqlite3 station.db ".backup station.db.path-migration-$(date -u +%Y%m%d-%H%M%SZ).bak"
OLD=/home/ec2-user/radio/pilgrim/library/ NEW="$PWD/library/"
sqlite3 station.db "UPDATE items SET media_path = '$NEW' || substr(media_path, length('$OLD')+1)
                    WHERE media_path LIKE '$OLD%'"
```

## 4. Verify the copy

On the M5, run the audit in its **default dry-run** mode. Never use `--apply`
on the migrated copy.

```sh
cd ~/radio
.venv/bin/python -m pilgrim.tools.audit_library
sqlite3 pilgrim/station.db "select count(*) from items"
```

Compare the item count with the EC2. Then check that **every** `media_path`
resolves (prints nothing when all exist):

```sh
sqlite3 pilgrim/station.db "select media_path from items" |
  while read f; do [ -e "$f" ] || echo "MISSING $f"; done | head
```

A missing file is a 404 on `/api/media/{id}` at the origin, so listeners hear
nothing even though `/api/health` says on air.

## 5. Start the station under launchd

**Native variant (current Mac Studio):** `pilgrim/deploy/mac/origin.sh install`
does §5–6 without Docker: a wrapper sourcing the repo's gitignored `.env`,
launchd units `farm.pilgrim.station` and `farm.pilgrim.gateway` (Homebrew
nginx on `127.0.0.1:8090`), and `tailscale serve --bg --https=443` on the
Mac's own tailnet node. The edge then uses that node's MagicDNS name as
`ORIGIN_HOST`, and the tailnet policy must grant the edge tag `tcp:443` to it.
The Docker steps below remain the alternative.

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

Verify: `curl -s http://127.0.0.1:5080/api/health` returns the public
`{on_air, time}` shape; `curl -s http://127.0.0.1:5081/api/health` returns the
full admin health; `/` on 5080 is 404 (`serve_web: false`). Logs:
`~/Library/Logs/pilgrim/station.out.log` / `.err.log`.

## 6. Start the gateway and observe the peer address

Render the gateway config into `pilgrim/deploy/gateway/` (gitignored):

```sh
cd ~/radio
.venv/bin/python -m pilgrim.tools.render_deploy \
  pilgrim/deploy/gateway/nginx.conf.template pilgrim/deploy/gateway/nginx.conf \
  --set GATEWAY_LISTEN=127.0.0.1:8090 \
  --set ORIGIN_PUBLIC_HOSTPORT=host.docker.internal:5080 \
  --set TRUSTED_EDGE_NET=127.0.0.1 \
  --set EXTRA_ALLOW_INCLUDE=/etc/nginx/extra-none.conf
```

`host.docker.internal:5080` is the origin's public listener as seen from the
gateway container. **Verify this on the M5**; replace it if Docker Desktop
exposes the host differently.

```sh
docker compose --env-file /secure/pilgrim-gateway.env up -d
```

The env file (outside the repo, mode 600) holds `TS_AUTHKEY=...`. The origin
key must be tagged `tag:radio-origin`, **not** ephemeral, single use, and
pre-approved. Tagged nodes have key expiry disabled by default, and
`TS_AUTH_ONCE=true` in `gateway/compose.yaml` keeps a restart from
re-authenticating with a spent one-off key.

Do **not** try this from another tailnet machine: the policy lets only
`tag:radio-edge` reach the origin, so the operator's own devices cannot. All
of these run on the M5.

First prove the gateway path from inside its container (the nginx image has
busybox `wget`):

```sh
docker exec pilgrim-gateway wget -qO- http://127.0.0.1:8090/api/health
docker exec pilgrim-gateway wget -qO- http://127.0.0.1:8090/api/admin/dj/state
```

The first must answer through the gateway; the second must be 404.

Then read the peer address the origin sees. The upstream keepalive holds the
connection open, so `lsof` shows it:

```sh
lsof -nP -iTCP:5080 -sTCP:ESTABLISHED
```

The uvicorn access line in `~/Library/Logs/pilgrim/station.out.log` /
`.err.log` shows the same peer.

Do **not** use `visitor_ips` here: with no `X-Real-IP` from an edge, a trusted
peer records nothing.

Decide `visitors.trusted_proxies`:
- If the peer is `127.0.0.1`, the default (`[127.0.0.1, "::1"]`) is already
  right.
- Otherwise add exactly that address as a `/32`, never a Docker range.

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

Enroll each edge with:

```sh
tailscale up --auth-key=<SSM> --advertise-tags=tag:radio-edge \
  --shields-up --accept-routes=false --accept-dns=true
```

Do **not** add `--ssh`, `--advertise-routes`, or `--advertise-exit-node`.
`--shields-up` blocks all inbound to the edge even if a later policy edit
grants something. The auth key comes from SSM, never the AMI.

**Order matters:** nginx resolves the upstream hostname at start, so start the
edge's nginx **after** tailscaled is up and MagicDNS resolves
`pilgrim-origin.<tailnet>.ts.net`.

**Current EC2 edge (interim launcher, no AMI):** on `pf` in
`/home/ec2-user/radio`, with the native M5 origin's node name:

```sh
H=justins-mac-studio.tail063158.ts.net
LOCAL_GATEWAY=0 ORIGIN_SCHEME=https ORIGIN_HOST=$H ORIGIN_HOSTPORT=$H:443 \
  MEDIA_CACHE_MAX=20g make edge-up
```

If the edge reports off-air while the origin answers, read the edge's
`/var/log/nginx/error.log` (`docker exec pilgrim-interim-edge tail ...`): an
`SSL certificate verify error` means `proxy_ssl_verify_depth` is too low
(docs/backends.md §8).

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

Optional tailnet isolation (check 11) proves the edge can reach only the
origin gateway. On the edge:

```sh
ORIGIN_TS_IP=$(tailscale ip -4 pilgrim-origin) \
DENY_TS_IPS="<other tailnet IPs>" \
bash pilgrim/deploy/edge/cutover_check.sh
```

With `ORIGIN_TS_IP` set, TCP `:443` must succeed and ports 22, 80, 5000, 5001, 5080, 5081,
8090 must fail within 3 s each. If `tailscale status --json` lists the origin
peer's `PeerAPIURL`, a curl to it must fail; otherwise the script prints
`SKIP`. `DENY_TS_IPS` (space-separated) checks ports 22, 443, 5000, 5001, 5080 and 5081 on
other tailnet devices (e.g. the M5 host's own Tailscale IP). Without
`ORIGIN_TS_IP`, check 11 prints `SKIP`.

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

## 11. Edge DNS: VPC DNS outage workaround (2026-11-04)

On 2026-11-04 the VPC DNS resolver (`100.100.100.100`) began SERVFAILing
everything from the edge — including local `.compute.internal` names, over
both UDP and TCP. Local names never leave the VPC, so the AWS resolver for
the VPC itself is broken (or VPC DNS support was disabled); nothing on the
box can cause this. Also VPC-side: all public ICMP egress is dropped at the
VPC gateway, and TCP/80 egress to 8.8.8.8 dies at the gateway while
`1.1.1.1:80`, `8.8.8.8:443` and UDP/53 all work — the IGW is present and
healthy. The edge's IAM role (`pg-s3-read`) has no EC2 read, so the NACL / SG
egress / route table cannot be inspected from the box. The station itself
was unaffected (443/53 egress is fine): `on_air` true, `pilgrim.farm` 200
through Cloudflare → ALB → `:8080`, CloudFront reachable.

### Changes made on the edge (pf) to restore DNS

1. **`/etc/resolv.conf` rewritten by hand (temporary).** tailscaled normally
   manages this file. Current content:

   ```
   nameserver 1.1.1.1
   nameserver 8.8.8.8
   nameserver 100.100.100.100
   search tail063158.ts.net us-west-1.compute.internal
   ```

   The IPv6 VPC DNS (`fd7a:115c:a1e0::53`) was dropped: the instance has no
   IPv6 route. **This is not durable**: a reboot — or tailscaled's next DNS
   write — regenerates the file from DHCP + VPC DNS and re-breaks DNS while
   the VPC DNS is still failing.
2. **`/etc/hosts` pins for the AWS API endpoints** (aws CLI works without
   DNS):

   ```
   3.101.233.116   ec2.us-west-1.amazonaws.com
   176.32.118.146  sts.us-west-1.amazonaws.com
   ```

   IPs resolved via 1.1.1.1 on 2026-11-04. Remove them when VPC DNS is
   fixed: a stale pin can point at a different host if the anycast record
   moves.
3. **tailscaled restarted** (tailnet came back cleanly; a restart does not
   fix the DNS issue below) and **tailscale upgraded to 1.104.5 via `yum
   update -y`** (google-chrome also upgraded). Note: on AL2023 tailscaled
   cannot read the OS DNS config — it shells out to `/usr/sbin/resolvconf
   -i`, which on this OS means something else ("option requires an
   argument"). Consequence: tailscaled's local DNS proxy (`127.0.0.1:41641`)
   has no upstream and does not answer; `tailscale status` permanently shows
   *"failed to fetch the DNS configuration of your device"*. The tailnet data
   path (edge → origin) is unaffected; only the 41641 proxy and the health
   warning are affected. Survived the 1.104.5 upgrade — same error.

### Verify (from the edge)

```sh
getent hosts checkip.amazonaws.com && curl -m 5 http://checkip.amazonaws.com
# expect the instance's public IP (52.53.93.0 at the time of writing)
dig @100.100.100.100 ip-172-31-25-15.us-west-1.compute.internal
# NOERROR + A 172.31.25.15 means the VPC DNS has recovered
curl -s http://127.0.0.1:8080/api/health   # station still on air
```

### Revert once the VPC DNS is fixed (AWS side)

1. Confirm the local-name probe above returns NOERROR.
2. Remove the two `/etc/hosts` pins
   (`sudo sed -i '/us-west-1\.amazonaws\.com/d' /etc/hosts`).
3. `sudo systemctl restart tailscaled` so it regenerates `/etc/resolv.conf`
   (tailscale-managed content: VPC DNS + search domain). Re-run the verify
   block.

### Still open (operator — AWS console, account 821856822529, us-west-1)

- AWS Health dashboard (account + us-west-1): any VPC / Route 53 DNS event?
- VPC → *Modify DNS support*: confirm enabled.
- Route table (`0.0.0.0/0` → IGW), the NACL, and the edge SG's **egress**
  rules: look for ICMP / TCP-80 egress blocks and any change since
  ~2026-11-03 20:22 UTC (CloudTrail). That is the window after the last
  known-good DNS (stale yum metadata cache).
- Durable local DNS fix: set a **tailnet DNS nameserver** (e.g. 1.1.1.1) in
  the admin console. tailscaled would then get its upstream from the netmap
  instead of the broken OS-config read, and its 41641 proxy would answer
  public names too.

### Legacy `pilgrim-nginx` container (host :80/:443) — keep running

Nothing listener-side hits the instance's public IP directly
(Cloudflare → ALB → `:8080` is the live path), but **do not stop this
container**: it serves the ACME HTTP-01 webroot (`/var/www/letsencrypt` on
`:80`) used to renew the pilgrim.farm Let's Encrypt certificate in
`/etc/letsencrypt` (config: `authenticator = webroot`,
`webroot_path = /var/www/letsencrypt`). The current cert (archive `cert2`,
issued 2026-08-21, HTTP-01) **expires 2026-11-19**, and the box has **no
certbot timer or cron** (no `certbot` unit in `systemctl list-timers --all`,
no crontab at all) — the 2026-08-21 renewal was not scheduled by anything on
this box. If this cert is the one on the ALB listener, expiry breaks the
public TLS path. Before 2026-11-19: `sudo certbot renew` (container must
keep serving `:80`), or install `certbot.timer`, or move to DNS-01 / ACM.
