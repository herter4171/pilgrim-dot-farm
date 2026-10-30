# Deploy — origin (M5) and edges (EC2)

This directory is the deployment contract for the split in `SPLIT_PLAN.md`.
The origin is the single station process; an edge is nginx plus
`pilgrim/web/`. The gateway runs on the origin and is the only network path an
edge may use. The origin's admin listener is never exposed.

Only `make deploy-check` is run by this repo's checks (render + `nginx -t` in a
throwaway container). Everything else is for the operator.

## Files

| File | Who runs it | What it is |
|---|---|---|
| `edge/nginx.conf.template` | Edge (EC2) nginx | Full `nginx.conf`: static site, public API proxy, media/micro caches, edge-generated off-air bodies. Render per edge. |
| `edge/realip-local.conf` | Local split test | Empty include (nobody trusted). Production supplies a Cloudflare `real_ip` include instead. |
| `gateway/nginx.conf.template` | Gateway container (M5) | Path + method allowlist, per-edge rate limits, gateway-generated off-air bodies. The same file is used locally and in production. |
| `gateway/extra-none.conf` | Production render | Empty extra-allow include. |
| `gateway/extra-e2e.conf` | Local e2e render only | Exposes the e2e server's `/api/test/*` endpoints. Never use in production. |
| `gateway/compose.yaml` | Operator, on the M5 | Tailscale sidecar + gateway nginx (Step 6). |
| `gateway/serve.json` | Tailscale serve | HTTPS on 443 -> gateway on 8090 (Step 6). |
| `tailscale/policy.example.hujson` | Operator | Tailnet policy fragment (Step 6). |
| `launchd/farm.pilgrim.station.plist.example` | Operator, on the M5 | launchd unit for the origin (Step 8). |
| `local/compose.yaml` | `make e2e-split` / local proof | Edge + gateway on loopback (Step 7). |
| `local/deploy_check.sh` | `make deploy-check` | Renders both templates with local values and runs `nginx -t`. |
| `RUNBOOK.md` | Operator | Move-the-origin runbook (Step 8). |

## Pinned images

- nginx: **`nginx:1.27-alpine`** (official image). `make deploy-check` uses this
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
them with `nginx -t` in a throwaway `nginx:1.27-alpine` container. The temp dir
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
  --set ORIGIN_PUBLIC_HOSTPORT=host.docker.internal:5000 \
  --set TRUSTED_EDGE_NET=127.0.0.1 \
  --set EXTRA_ALLOW_INCLUDE=/etc/nginx/extra-none.conf
```

`host.docker.internal:5000` is the origin's public listener as seen from the
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
`nginx:1.27-alpine`); do not use `latest`.

### `gateway/serve.json`

The serve config format was verified against `tailscale/tailscale:v1.104.4`
(`containerboot` replaces `${TS_CERT_DOMAIN}` with the node FQDN; see
`ipn.ServeConfig`). If the operator changes the pinned image tag, re-check the
format against that release's docs:
<https://tailscale.com/kb/1589/tailscale-services-configuration-file>.

## Tailnet policy

`tailscale/policy.example.hujson` is a fragment, not a full policy. Merge it
and walk its checklist: remove any default allow-all rule, confirm no other
grant matches the two tags, enroll edges as ephemeral/pre-approved/tagged with
keys from SSM, forbid exit nodes and subnet routes on edges, and enable HTTPS
certificates + MagicDNS. The origin tag must never be a grant source.

## Do not

- Do not run these files against the production `pilgrim-nginx` container.
- Do not expose the admin listener or `/api/admin/*` through an edge.
- Do not put TLS certificates in the edge template; TLS is the operator's IaC.
