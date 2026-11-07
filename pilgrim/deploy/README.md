# Deploy — origin (M5) and edges (EC2)

This directory is the deployment contract for the split in `SPLIT_PLAN.md`.
The origin is the single station process; an edge is nginx plus
`pilgrim/web/`. The gateway runs on the origin and is the only network path an
edge may use. The origin's admin listener is never exposed.

Only `make deploy-check` is run by this repo's checks (render + `nginx -t` in a
throwaway container). Everything else is for the operator.

## Current production (since 2026-11-01)

```
Cloudflare → ALB → EC2 edge  0.0.0.0:8080   (interim/up.sh, LOCAL_GATEWAY=0; stateless)
          → https://justins-mac-studio.tail063158.ts.net:443   (tailscale serve on the M5)
          → M5 gateway 127.0.0.1:8090   (Homebrew nginx, launchd farm.pilgrim.gateway)
          → M5 station 127.0.0.1:5080   (launchd farm.pilgrim.station; admin 127.0.0.1:5081)
```

No SSH port forwarding. All state (`station.db`, `pilgrim/library/`, `.env`)
is on the M5.

**M5 (origin):** `pilgrim/deploy/mac/origin.sh install | status | uninstall`.
Renders the gateway template for Homebrew nginx into `~/pilgrim-origin/`,
writes a wrapper that sources the repo's `.env`, installs both launchd units,
and runs `tailscale serve --bg --https=443 http://127.0.0.1:8090`. Logs:
`~/Library/Logs/pilgrim/`. Restart the station with
`launchctl kickstart -k gui/$(id -u)/farm.pilgrim.station`.

**EC2 (edge):** from `/home/ec2-user/radio`,

```sh
H=justins-mac-studio.tail063158.ts.net
LOCAL_GATEWAY=0 ORIGIN_SCHEME=https ORIGIN_HOST=$H ORIGIN_HOSTPORT=$H:443 \
  MEDIA_CACHE_MAX=20g make edge-up        # also: make edge-status / edge-down
```

`up.sh` keeps rendered configs and the cache in `~/pilgrim-interim/`; deleting
that directory breaks the running edge (`mkdir() "/cache/micro/..." failed`)
until `make edge-up` is re-run. `TRUSTED_PROXY_CIDR` defaults to the
instance's VPC CIDR via IMDSv2.

**Tailnet policy:** edge tag `tag:radio-relay` → host `pilgrim-origin`
(`100.114.40.70`), `tcp:443` only (docs/backends.md §8).

## Files

| File | Who runs it | What it is |
|---|---|---|
| `edge/nginx.conf.template` | Edge (EC2) nginx | Full `nginx.conf`: static site (missing paths are 404, never an index.html fallback), public API proxy, media/micro caches, edge-generated off-air bodies. Render per edge. |
| `edge/cutover_check.sh` | Operator, on one edge | Curl-only cutover checks 1–7 against the edge's local nginx (no Python venv needed). |
| `edge/realip-local.conf` | Local split test | Empty include (nobody trusted). Production supplies a Cloudflare `real_ip` include instead. |
| `gateway/nginx.conf.template` | Gateway container (M5) | Path + method allowlist, per-edge rate limits, gateway-generated off-air bodies. The same file is used locally and in production. |
| `gateway/extra-none.conf` | Production render | Empty extra-allow include. |
| `gateway/extra-e2e.conf` | Local e2e render only | Exposes the e2e server's `/api/test/*` endpoints. Never use in production. |
| `gateway/compose.yaml` | Operator, on the M5 | Tailscale sidecar + gateway nginx (Step 6). |
| `gateway/serve.json` | Tailscale serve | HTTPS on 443 -> gateway on 8090 (Step 6). |
| `tailscale/policy.example.hujson` | Operator | Tailnet policy fragment (Step 6). Production uses `tag:radio-relay` → host alias, see above. |
| `interim/up.sh`, `interim/compose.yaml` | Operator, on the EC2 (`make edge-up`) | Edge container (and, with `LOCAL_GATEWAY=1`, a local gateway) from the same templates. |
| `mac/origin.sh` | Operator, on the M5 | Native origin: launchd station + Homebrew nginx gateway + `tailscale serve`. |
| `launchd/farm.pilgrim.station.plist.example` | Operator, on the M5 | launchd unit for the origin (Step 8). |
| `local/compose.yaml` | `make e2e-split` / `make split-smoke` | Edge + gateway on loopback (Step 7). The gateway runs with the prod hardening (uid 101, read-only, tmpfs). |
| `local/stack.sh` | both local targets | `PILGRIM_SPLIT_ORIGIN=e2e` (default): `e2e_server` + `extra-e2e.conf`, for the Playwright gap test. `=real`: `pilgrim/tests/e2e/split_origin.py` (the **real** `create_public_app`, fakes, temp library) + the **prod** allowlist (`extra-none.conf`). |
| `local/smoke.sh` | `make split-smoke` | Always `real` mode: the smoke checks must exercise `server.py`, never `e2e_server`. |
| `local/deploy_check.sh` | `make deploy-check` | Renders both templates with local values and runs `nginx -t`. |
| `RUNBOOK.md` | Operator | Move-the-origin runbook (Step 8). |

## Pinned images

- nginx: **`nginx:1.30.6-alpine`** (official image; equals `stable-alpine`, digest
  `sha256:0e52bd05…`, checked 2026-10-30). `make deploy-check` uses this
  exact tag.
- tailscale: pinned to an exact release tag in `gateway/compose.yaml`
  (Step 6). Do not use `latest`.

## Placeholders

Templates use `@@NAME@@`, never `${NAME}`, because nginx uses `$name` itself.
`pilgrim/tools/render_deploy.py` fails (writing nothing) when a value is
missing, when a `--set` name does not appear in the template, or when any
placeholder would remain in the output.

### Edge (`edge/nginx.conf.template`)

| Placeholder | Meaning | Local value |
|---|---|---|
| `@@EDGE_LISTEN@@` | `listen` address:port | `127.0.0.1:18080` |
| `@@WEB_ROOT@@` | directory holding `pilgrim/web/` | `/cfg/web` |
| `@@CACHE_DIR@@` | parent of `media/` and `micro/` cache dirs | `/cfg/cache` |
| `@@MEDIA_CACHE_MAX@@` | media cache `max_size` | `1g` |
| `@@ORIGIN_SCHEME@@` | `http` locally; `https` in production | `http` |
| `@@ORIGIN_HOSTPORT@@` | gateway `host:port` | `127.0.0.1:18090` |
| `@@ORIGIN_HOST@@` | gateway hostname (Host header, TLS SNI/verify) | `127.0.0.1` |
| `@@REALIP_INCLUDE@@` | real-IP include file | `/cfg/realip.conf` |
| `@@CA_BUNDLE@@` | CA bundle for upstream TLS | `/etc/ssl/certs/ca-certificates.crt` |
| `@@EXTRA_ALLOW_INCLUDE@@` | extra locations (test only) | `/cfg/extra-none.conf` |

### Gateway (`gateway/nginx.conf.template`)

| Placeholder | Meaning | Local value |
|---|---|---|
| `@@GATEWAY_LISTEN@@` | `listen` address:port | `127.0.0.1:18090` |
| `@@ORIGIN_PUBLIC_HOSTPORT@@` | origin public listener | `127.0.0.1:<e2e port>` |
| `@@TRUSTED_EDGE_NET@@` | peer allowed to reach the gateway | `127.0.0.1` |
| `@@EXTRA_ALLOW_INCLUDE@@` | extra locations (test only) | `/cfg/extra-none.conf` |

## Render

```sh
.venv/bin/python -m pilgrim.tools.render_deploy \
  pilgrim/deploy/edge/nginx.conf.template /etc/nginx/nginx.conf \
  --set EDGE_LISTEN=... --set WEB_ROOT=... --set CACHE_DIR=... \
  --set MEDIA_CACHE_MAX=... --set ORIGIN_SCHEME=... \
  --set ORIGIN_HOSTPORT=... --set ORIGIN_HOST=... \
  --set REALIP_INCLUDE=... --set CA_BUNDLE=...
```

`make deploy-check` renders both templates into a temp directory and validates
them with `nginx -t` in a throwaway `nginx:1.30.6-alpine` container. The temp dir
is mounted rw because nginx validates `proxy_cache_path` by creating/chowning
its cache directories.

## Secrets

`TS_AUTHKEY` (the Tailscale auth key for the gateway sidecar) comes from an env
file outside the repo. Never commit it. It is documented here and in the root
`README.md`.

## Gateway (origin, M5)

Render the gateway config with production values:

```sh
.venv/bin/python -m pilgrim.tools.render_deploy \
  pilgrim/deploy/gateway/nginx.conf.template gateway/nginx.conf \
  --set GATEWAY_LISTEN=127.0.0.1:8090 \
  --set ORIGIN_PUBLIC_HOSTPORT=host.docker.internal:5080 \
  --set TRUSTED_EDGE_NET=127.0.0.1 \
  --set EXTRA_ALLOW_INCLUDE=/etc/nginx/extra-none.conf
```

(Docker variant. The native M5 setup uses `mac/origin.sh`, which renders
`ORIGIN_PUBLIC_HOSTPORT` from `config.yaml`.) `host.docker.internal:5080` is the origin's public listener as seen from the
gateway container. **The operator verifies this on the M5** and replaces it if
Docker Desktop exposes the host differently. `127.0.0.1` for
`TRUSTED_EDGE_NET` is correct in production because Tailscale serve proxies
from inside the container's network namespace; the real edge filtering is the
tailnet policy.

Run it:

```sh
docker compose --env-file /secure/pilgrim-gateway.env up -d
```

The env file (outside the repo) contains `TS_AUTHKEY=...`. Image tags are
pinned in `gateway/compose.yaml` (`tailscale/tailscale:v1.104.4`,
`nginx:1.30.6-alpine`); do not use `latest`.

Gateway rate limits are **per edge**: production serve overwrites
`X-Forwarded-For` with the connecting tailnet peer IP, so the zones key on
that edge identity (locally they fall back to `$remote_addr` because there is
no serve hop). The request-line limit applies to **POST** only: `GET
/api/requests` (the board refresh) is never throttled.

### `gateway/serve.json`

The serve config format was verified against `tailscale/tailscale:v1.104.4`
(`containerboot` replaces `${TS_CERT_DOMAIN}` with the node FQDN; see
`ipn.ServeConfig`). If the operator changes the pinned image tag, re-check the
format against that release's docs:
<https://tailscale.com/kb/1589/tailscale-services-configuration-file>.

## Bastion subnet router (SCALABILITY.md Step 8)

The Fargate web tier reads Valkey and RDS Postgres inside the VPC; the Mac
must write to them. The bastion EC2 (`ssh pf2`, `172.31.35.80`, security
groups `pg-ec2` + `pg-cache` + `pg-db`) runs a Tailscale **subnet
router** for the VPC CIDR, so the Mac's tailnet carries `172.31.0.0/16`.
Done on 2026-11-05 on the old edge host `pf`; redone on `pf2` in the new account (2026-11-06):

```sh
# on pf (keeps tag:radio-relay; `set` changes only this pref)
sudo tailscale set --advertise-routes=172.31.0.0/16
printf 'net.ipv4.ip_forward = 1\nnet.ipv6.conf.all.forwarding = 1\n' \
  | sudo tee /etc/sysctl.d/99-tailscale.conf && sudo sysctl -p /etc/sysctl.d/99-tailscale.conf
# on the Mac: already true ("Use Tailscale subnets")
tailscale debug prefs | grep RouteAll
```

Then the tailnet policy needs two things, both in
`tailscale/policy.example.hujson`: the `autoApprovers` block (else approve the
route by hand: Machines → `ip-172-31-25-15` → Edit route settings), and the
grant `pilgrim-origin → pilgrim-vpc` on `tcp:5432`/`tcp:6379`. Without the
grant the control plane withholds the route from the Mac: the bastion shows
`PrimaryRoutes: [172.31.0.0/16]` while the Mac's view of that peer lists no
subnet in `AllowedIPs` (seen 2026-11-05). SNAT stays on (default), so the DB and
cache see the bastion's private IP: their security groups already admit
`pg-ec2`, and no source/dest-check change is needed. The instance's tags
`REDIS_URL`, `DATABASE_URL`, `DB_PASS_SECRET_ARN` mirror the ECS task's
environment (IMDS tags are enabled: `curl .../meta-data/tags/instance/`).

Verify from the Mac once approved (hostnames resolve to private IPs from
anywhere; only the route makes them reachable):

```sh
tailscale status --json | python3 -c 'import sys,json; print([p["PrimaryRoutes"] for p in json.load(sys.stdin)["Peer"].values()])'
nc -z -w 5 pg-db.chg6qwekyier.us-west-2.rds.amazonaws.com 5432 && echo postgres tcp ok
printf 'PING\r\n' | openssl s_client -quiet -connect pg-cache-dwusn7.serverless.usw2.cache.amazonaws.com:6379 2>/dev/null | head -1   # +PONG
```

Verified 2026-11-05 from the Mac: route present, Postgres offers TLS, Valkey
answers `+PONG` over TLS 1.3, and a full login (secret read with the
`mac-studio` profile → `psycopg` with `sslmode=require`) reaches
`PostgreSQL 18.6` as user `pilgrim` on an empty database. Nothing on the Mac
reads the three variables yet (Steps 2–3); when it does, they go in the
repo-root `.env` like the other station settings, and the password is fetched
from the secret at start, never written to disk.

## Station cutover: Postgres + Valkey (SCALABILITY.md Steps 2–3)

The station keeps `station.db` and publishes nothing until this is run, and
it must not run before the web tier (Step 5) reads heartbeats and the
programme from Valkey: the two sides switch together. Steps, on the M5:

```sh
launchctl kill SIGTERM gui/$(id -u)/farm.pilgrim.station      # stop the station
sqlite3 pilgrim/station.db ".backup pilgrim/station.db.pre-postgres-$(date -u +%Y%m%d-%H%M%SZ).bak"
# .env gains (no password in it; the secret is read with the mac-studio profile):
#   DATABASE_URL=postgresql://pilgrim@<rds-host>:5432/pilgrim?sslmode=require
#   DB_PASS_SECRET_ARN=arn:aws:secretsmanager:...
#   REDIS_URL=rediss://<cache-host>:6379          # hot state publisher (RADIO §14)
make pg-migrate                                   # dry run: copies in a transaction, rolls back, prints counts
make pg-migrate PG_MIGRATE_ARGS="--apply"         # commit; refuses a non-empty target without --truncate
launchctl kickstart -k gui/$(id -u)/farm.pilgrim.station
curl -s 127.0.0.1:5081/api/health | python3 -c 'import sys,json; h=json.load(sys.stdin); print(h["db"], h["cache"])'
# expect db.engine == "postgres", cache.healthy == true and ticks climbing
```

Rollback: remove the three lines from `.env` and kickstart the station; the
SQLite file was never modified (the tool opens it read-only), and only rows
written between the copy and the rollback are lost. The RDS instance is
reachable only through the bastion route, so the Mac must hold the
`172.31.0.0/16` route (above) before the station starts.

## Tailnet policy

`tailscale/policy.example.hujson` is a fragment, not a full policy. Merge it
and walk its checklist: remove any default allow-all rule, confirm no other
grant matches the two tags, enroll edges as ephemeral/pre-approved/tagged with
keys from SSM, forbid exit nodes, allow exactly one subnet route (the
bastion's VPC CIDR, above), and enable HTTPS certificates + MagicDNS. The
origin tag must never be a grant source.

## Operator IaC checklist (SPLIT_PLAN §9)

The operator owns Packer, Ansible, AWS, Cloudflare and Tailscale enrollment.
This repo does not call the Cloudflare API or install Tailscale.

- **AMI.** nginx at the same pinned version; the repo checked out at a pinned
  SHA (edges use only `pilgrim/web/` and `pilgrim/deploy/`); the rendered edge
  config; `tailscaled` enrolled at boot as an ephemeral `tag:radio-edge` node
  with its key read from SSM (never baked into the AMI), using:

  ```sh
  tailscale up --auth-key=<SSM> --advertise-tags=tag:radio-edge \
    --shields-up --accept-routes=false --accept-dns=true
  ```

  No `--ssh`, `--advertise-routes`, or `--advertise-exit-node`. `--shields-up`
  blocks all inbound to the edge even if a later policy edit grants something.
  Start nginx **after** Tailscale is up and MagicDNS resolves the gateway.
- **Origin key.** Tagged `tag:radio-origin`, **not** ephemeral, single use,
  pre-approved. Tagged nodes have key expiry disabled by default, and the
  gateway compose sets `TS_AUTH_ONCE=true` so a restart does not
  re-authenticate with a spent one-off key.
- **Load balancer health check.** `GET /edge-health`. Never an `/api/` path:
  `/edge-health` never contacts the origin, so an origin outage does not kill
  healthy edges.
- **Cloudflare.** Proxied DNS; SSL mode Full (strict) with the edge's Let's
  Encrypt certificate; a Cache Rule making `/api/media/*` eligible for cache
  and respecting origin `Cache-Control` (Cloudflare does not cache
  extensionless paths by default); `/api/*` otherwise follows origin headers;
  rate-limit rules on `POST /api/requests` and `POST /api/visitors`; real IP
  via `CF-Connecting-IP` (the edge `realip` include with Cloudflare's
  published ranges, refreshed periodically). Confirm Cloudflare's terms allow
  serving FLAC audio at the expected volume.
- **Deploy order.** Ship API changes to the M5 first and keep them
  backward-compatible with the UI already on the edges. Ship UI changes as a
  new AMI plus an instance refresh.

## Do not

- Do not run these files against the production `pilgrim-nginx` container.
- Do not expose the admin listener or `/api/admin/*` through an edge.
- Do not put TLS certificates in the edge template; TLS is the operator's IaC.
