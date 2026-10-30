# SPLIT_PLAN.md — split the station (origin) from the site (edges)

Operator, 2026-10-30: "What we mainly need is to separate what goes into
creating the station audio from the hosting of the site."

This is the plan only. **No code changes have been made yet.** Work it top to
bottom, one step at a time. Each step ends with a commit and a short report
(AGENTS.md §8). Each step names the RADIO.md sections it must update **in the
same commit**: this plan changes the HTTP API (§11) and the config schema
(§12), so AGENTS.md hard rule 8 applies.

---

## 0. Read this first

### 0.1 The target, in one picture

```
                         ┌── EDGE (EC2, many, stateless) ───────────────────┐
Browser ─▶ Cloudflare ─▶ │ nginx: serves pilgrim/web/ (static site)          │
           (proxy)       │        proxies /api/* to the origin, caches media │
                         └───────────────┬───────────────────────────────────┘
                                         │ one TCP port (Tailscale in prod,
                                         │ loopback in this plan)
                         ┌── GATEWAY (container on the M5) ─▼────────────────┐
                         │ nginx: path + method ALLOWLIST, rate limits       │
                         └───────────────┬───────────────────────────────────┘
                                         │ http, local only
                         ┌── ORIGIN (M5 Mac Studio, exactly one) ─▼──────────┐
                         │ pilgrim.server  PUBLIC listener  (port 5000)      │
                         │                 ADMIN listener   (port 5001, lo)  │
                         │ scheduler + producer + store + library/ + db      │
                         └───────────────────────────────────────────────────┘
```

### 0.2 Facts you must not get wrong

1. **There is no continuous audio stream.** The browser fetches the program
   list (`GET /api/station/program`) and separate FLAC files
   (`GET /api/media/{id}`), then joins them with Web Audio (RADIO.md §9.2).
   **Keep this model.** Do not add Icecast, HLS, MP3, Opus or server-side mixing.
2. **There is exactly one scheduler, on the origin.** Edges never run
   `pilgrim.server`, `pilgrim.producer`, or anything that opens `station.db`.
   An edge is nginx plus the `pilgrim/web/` static files. Nothing else.
3. **SQLite stays.** One process writes `station.db`. Do not add Postgres,
   Redis, SQLAlchemy or any replication. Do not change the database schema in
   this plan.
4. **When the origin is unreachable, the station is off the air.** Edges do not
   replay stale programs, loop audio, or fake state. They say "off air"
   (Step 4).
5. **Heartbeats** (`POST /api/station/heartbeat`, every 5 s per playing
   browser) feed the live listener count and the `airplay` log. They do **not**
   drive the playhead. The playhead runs on the origin's clock
   (`scheduler.py:122`). In this plan heartbeats are proxied through unchanged.
6. **Tailscale is not your job.** Do not install, run, or configure Tailscale.
   Do not add Tailscale code to Python. You only write the gateway files in
   Step 6, and the operator runs them. Everything you build and test runs over
   **loopback on this host**.
7. **Cloudflare is not your job.** Do not call the Cloudflare API. Step 9 lists
   what the operator configures.

### 0.3 Rules for this plan (on top of AGENTS.md §1)

- Do not touch the running production nginx (`pilgrim-nginx` container,
  config in `/home/ec2-user/site/deploy/`). It is outside this repo.
- Do not start, stop, or restart the live station on port 5000 unless a step
  says so. Local split tests use **other ports** (§0.4) and a **temp
  library** (e2e server or fakes), never `pilgrim/station.db`.
- Do not use `docker compose down -v`, `docker system prune`, or remove any
  container or image you did not create in this plan.
- All new deploy files live under `pilgrim/deploy/` (AGENTS.md layout rule:
  the repo root holds only repo-level files).
- Placeholders in deploy templates use the form `@@NAME@@`, never `${NAME}`
  or `$NAME`, because nginx itself uses `$name` variables. The renderer
  (Step 5) must fail if any `@@...@@` is left unreplaced.

### 0.4 Ports used in this plan

| What | Production (M5 / EC2) | Local split test (this host) |
|------|----------------------|------------------------------|
| Origin public listener | `127.0.0.1:5000` on the M5 | e2e server on an OS-chosen free port |
| Origin admin listener | `127.0.0.1:5001` on the M5 | not used by e2e |
| Gateway nginx | `127.0.0.1:8090` inside the container, published by Tailscale serve on `:443` | `127.0.0.1:18090` |
| Edge nginx | `:8080` behind TLS on the EC2 | `127.0.0.1:18080` |
| Edge health (for ALB/IaC) | `GET /edge-health` on the edge | same |

Before using 18080 or 18090, check they are free (`ss -ltn`). If one is
taken, pick another free port and say so in your report.

### 0.5 Before Step 1 (resolved 2026-10-30)

- The request-splice work is committed (`c7eca10`). The tree is clean apart
  from two untracked `pilgrim/station.db.*.bak` files. **Never stage, commit,
  move, or delete those** (AGENTS.md hard rule 5). Use `git add <paths>` with
  explicit paths, never `git add -A` or `git add .`. Never commit files you did
  not change for this plan.
- This plan and the AGENTS.md "Active work plan" pointer are already
  committed as their own docs commit. Start at Step 1.
- The live station is running on port 5000. That is expected and doesn't
  affect this plan, but never stop, restart, or point tests at it (§0.3).

### 0.6 Working agreement

- One step at a time. After each step: commit, then send **one** message with
  the AGENTS.md §8 report and the commit SHA. Then **wait** for review before
  starting the next step.
- Don't paste diffs. The reviewer reads `git show <sha>` in the repo.
- Stop early and ask (don't guess) when you hit an AGENTS.md §9 stop
  condition, a "stop and report" in this plan, or a fact in the code that
  contradicts this plan. Cite the file and line.
- When you find a gap in this plan, say so in the report under
  **Deviations**, with what you did instead. Small reversible choices: decide
  and note. Anything touching the API, config schema, or security boundary:
  ask first.

---

## Step 1. Two listeners: public and admin (RADIO.md §3, §11, §12)

**Why:** the M5 must expose a surface that physically cannot serve DJ or
control routes. A proxy allowlist alone is one mistake away from exposing
them.

### 1.1 Config (`pilgrim/config.py`, `pilgrim/config.yaml`)

Add to the `station` section:

```yaml
station:
  host: "0.0.0.0"          # public listener bind (existing key, unchanged meaning)
  port: 5000               # public listener port (existing key)
  admin_host: "127.0.0.1"  # admin listener bind: loopback only by default
  admin_port: 5001         # admin listener port
  serve_web: true          # mount pilgrim/web/ at "/" on the public listener
```

- Validation: `admin_port != port`; reject at load time with a clear error.
- `serve_web: true` keeps today's single-host behaviour working. On the M5
  the operator sets it to `false` (edges serve the site).

### 1.2 Server (`pilgrim/server.py`)

Today `create_app(cfg)` (line 396) builds one FastAPI app with every route
and creates the `Station` inside. Change it to this shape:

- `build_station(cfg) -> Station`: creates the `Station` (moved out of
  `create_app`).
- `create_public_app(cfg, station) -> FastAPI`: **only** these routes:

  | Method | Path |
  |--------|------|
  | GET | `/api/station/program` |
  | GET | `/api/station/state` |
  | POST | `/api/station/heartbeat` |
  | GET | `/api/media/{item_id}` |
  | GET, POST | `/api/requests` |
  | POST | `/api/visitors` |
  | GET | `/api/health` (**public shape**, §1.3) |
  | GET | `/` static web, only if `station.serve_web` |

- `create_admin_app(cfg, station) -> FastAPI`: **every** route that exists
  today, including the full `/api/health`, `/api/station/start`,
  `/api/station/stop`, all `/api/admin/*`. The admin app is a superset, so
  the TUI and existing tests keep working against it. No static web mount.
- `create_app(cfg=None) -> FastAPI`: keep the name and signature. It returns
  the admin app with the station startup hook attached, exactly as today.
  Existing tests (`test_request_filter.py`, `test_sfx.py`, `test_dj_api.py`)
  call it and must pass unchanged.
- Register each route once, in a helper shared by both apps (for example
  `_add_public_routes(app, station)` and `_add_admin_routes(app, station)`).
  Do not copy-paste handlers.
- `main()`: build one `Station`, build both apps, create two
  `uvicorn.Server(uvicorn.Config(...))` objects (public on
  `station.host:station.port`, admin on `station.admin_host:station.admin_port`),
  run `station.startup()` **once**, then `await asyncio.gather(public.serve(),
  admin.serve())`. Keep `log_config=None` on both.
- The station startup must run **exactly once** per process. Do not attach
  the startup hook to both apps.

### 1.3 Public `/api/health`

Today `/api/health` probes every backend (`Station.health()`, line 156), and
every listening browser calls it every 8 s (`player.js:89`). The client only
reads `on_air`. On the **public** app, `/api/health` returns only:

```json
{"on_air": true, "time": 1793300000.0}
```

It must not call `backend_status()`, `inventory_levels()`, or any network
call. The admin app keeps the full response.

### 1.4 Makefile and TUI

- `make run`: replace the `uvicorn ... --port 5000` line with
  `$(PY) -m pilgrim.server` (ports come from config, AGENTS hard rule 4).
  Keep the `PATH` and `LITELLM_TOKEN` handling.
- TUI (`pilgrim/tui/client.py:29-32`): default base URL becomes
  `http://{admin_host}:{admin_port}`. If `admin_host` is `0.0.0.0`, connect to
  `127.0.0.1`.

### 1.5 Tests (new file `pilgrim/tests/test_server_split.py`, fakes only)

- The public app returns 404 for `GET /api/admin/dj/state`,
  `POST /api/station/start`, `POST /api/station/stop`, `GET /api/admin/voices`.
- The admin app serves all of them (status not 404).
- Public `/api/health` returns exactly the keys `{"on_air", "time"}`, and the
  fake backends record **zero** calls during it.
- Config with `admin_port == port` fails to load.
- With `serve_web: false`, public `GET /` is 404. With `true`, it returns
  `index.html`.

### 1.6 Docs in the same commit

- RADIO.md §11: add a "Listeners" paragraph: public vs admin route lists, the
  public `/api/health` shape, and that `/api/admin/*` is admin-only.
- RADIO.md §12: the three new `station` keys.
- RADIO.md §3: replace "the client reports its playhead via heartbeat, and the
  scheduler keeps the committed window full relative to that playhead" with
  the true behaviour (playhead on the origin clock; heartbeats feed the
  listener count and airplay log). Report this as a doc correction.
- TUI.md: the console connects to the admin listener.
- README.md: `make run` now starts both listeners; list both ports.

---

## Step 2. Cache headers from the origin (RADIO.md §11)

**Why:** edges and Cloudflare decide what to cache from these headers. The
origin states the intent once, so nginx and Cloudflare configs stay simple.

Set `Cache-Control` on these **public** responses (the admin app may send the
same headers; it doesn't matter):

| Route | `Cache-Control` |
|-------|-----------------|
| `GET /api/media/{id}` (200) | `public, max-age=31536000, immutable` |
| `GET /api/media/{id}` (404) | `public, max-age=30` |
| `GET /api/station/program` | `public, max-age=1` |
| `GET /api/station/state` | `public, max-age=1` |
| `GET /api/requests` | `public, max-age=2` |
| `GET /api/health` (public) | `public, max-age=2` |
| every POST | `no-store` |
| static web files | leave as is (edge sets these, Step 5) |

`/api/visitors` already sends `no-store` (server.py:625). Keep it.

**Check before you mark media immutable:** a media id must always map to the
same bytes. Search `pilgrim/` (including `pilgrim/tools/`) for any code that
rewrites the file at an existing item's `media_path` after the item is
created. Retiring an item (keeps the file) and purging (deletes the file) are
fine. If anything overwrites an existing item's file in place, **stop and
report**. Do not mark media immutable until the operator decides.

**Note for the report:** `max-age=1` on `/api/station/state` means
`server_time` can be up to ~1 s old at the client. DJ cutovers (§5.5) use
`cutover_lead_s: 1.0`. Say this in the report. Do not change cutover logic in
this plan.

Tests (`test_server_split.py`): one assertion per row of the table.

Docs: RADIO.md §11 gets a "Caching" paragraph with the table.

---

## Step 3. Trusted proxies by network, and the client-IP chain (RADIO.md §12, §14)

**Why:** per-IP rate limiting (OVERHAUL 4.3) and the visitor counter
(RADIO §14) need the real browser IP. The path becomes
browser → Cloudflare → edge → gateway → origin. Each hop must pass the IP on
safely.

### 3.1 The rule for each hop

| Hop | Trusts | Sets `X-Real-IP` to |
|-----|--------|---------------------|
| Edge nginx | Cloudflare's IP ranges (prod) / nobody (local) | the browser IP (`$remote_addr` after `real_ip` processing). **Always overwrites** whatever the client sent. |
| Gateway nginx | the edge peers that reach it (tailnet range in prod, loopback locally) | `$http_x_real_ip` from the edge, unchanged |
| Origin | the gateway's peer address (`visitors.trusted_proxies`) | n/a (reads `X-Real-IP`) |

### 3.2 Code (`pilgrim/server.py:_client_ip`, `pilgrim/config.py`)

- `visitors.trusted_proxies` accepts single IPs **and** CIDR networks (for
  example `127.0.0.1`, `::1`, `192.168.65.0/24`). Parse each entry with
  `ipaddress.ip_network(entry, strict=False)` at config load. Reject invalid
  entries at load time.
- A peer is trusted if its canonical address (reuse `_canonical_ip`, which
  already unwraps IPv4-mapped IPv6) is inside any trusted network.
- Read only `X-Real-IP` from a trusted peer. If it is missing, fall back to
  the **first** entry of `X-Forwarded-For` (today's behaviour). From an
  untrusted peer, use the peer address and ignore both headers (today's
  behaviour).
- **One trust decision, not two.** uvicorn 0.54 defaults to
  `proxy_headers=True` with `forwarded_allow_ips="127.0.0.1,::1"`. It
  rewrites `request.client.host` from `X-Forwarded-For` **before** our code
  runs, so `_client_ip`'s "peer" is often already the browser IP, and the
  trust list only appears to work. In `main()`, pass `proxy_headers=False` to
  **both** `uvicorn.Config(...)` calls. `visitors.trusted_proxies` is then the
  only place that decides trust. `make run` uses `main()`, so it is covered
  too.
- **The request-line rate limiter must key on the resolved client IP.**
  `_make_rate_limiter` (server.py) keys on `request.client.host`. Once
  `proxy_headers=False` is set, behind the gateway that is the gateway's
  address for everyone, so the whole station would share one
  3-per-10-minutes bucket. Key it on `station._client_ip(request)` (fall
  back to `request.client.host` only when that returns `None`). Test: two
  requests from a trusted peer with different `X-Real-IP` values are limited
  separately, and the same `X-Real-IP` is limited together.

### 3.3 Tests (fakes only)

- A peer inside a trusted CIDR has its `X-Real-IP` used.
- A peer outside every trusted entry: headers ignored, peer IP used.
- An invalid entry such as `"not-an-ip"` fails config load.
- Existing visitor tests still pass.

### 3.4 Docs

- RADIO.md §12: `trusted_proxies` accepts CIDRs.
- RADIO.md §14 "Visitors": replace "nginx on loopback sets X-Real-IP" with
  the hop table from §3.1.

Do **not** guess the peer address Docker Desktop on macOS shows the origin
for gateway traffic. The operator observes it in Step 8 and records it.

---

## Step 4. Off-air behaviour in the client (RADIO.md §9.1, §11)

**Why:** when the origin is unreachable, the edge answers by itself (Step 5).
The page must show OFF AIR cleanly, not hang or throw.

The edge will answer like this when it cannot reach the gateway or origin:

| Request | Edge response |
|---------|---------------|
| `GET /api/health` | `200 {"on_air": false, "off_air": true}` |
| any other `/api/*` | `503 {"off_air": true, "detail": "station is off the air"}` |

Client changes (keep them small; no framework, no build step):

- `player.js`: `pollHealth` already shows OFF AIR when `on_air` is false.
  Leave it. Check the program, state and media fetch paths. A 503 must not
  throw an uncaught error or spin forever. When playing and the station goes
  off air, let the buffered audio finish, then show OFF AIR (the existing
  `pollHealth` path handles this once `playing` ends). Don't add retries
  faster than the existing poll intervals.
- `requests.js`: on a 503 whose JSON has `off_air: true`, show the message
  `The station is off the air. Requests are closed.` in the existing error
  element. On `GET` refresh failures, keep the last rendered board.
- `visitors.js`: no change (a failed poll already keeps the last value).

Tests: extend an existing Playwright spec, or add `offair.spec.ts`. Use
Playwright `page.route` to answer `/api/health` with `{"on_air": false}` and
`POST /api/requests` with the 503 body. Assert the OFF AIR element is visible,
PLAY is disabled, and the request message shows.

Docs: RADIO.md §9.1 (OFF AIR also comes from the edge), §11 (the edge's
off-air responses, marked "edge-generated, not origin").

---

## Step 5. Edge nginx template + renderer (RADIO.md §18)

### 5.1 Files

```
pilgrim/deploy/
  README.md                     # what each file is, who runs it, every @@VAR@@
  edge/nginx.conf.template      # full nginx.conf (not a conf.d snippet)
  edge/realip-local.conf        # empty file (no trusted upstream proxy locally)
  gateway/nginx.conf.template   # Step 6
  gateway/compose.yaml          # Step 6
  gateway/serve.json            # Step 6
  tailscale/policy.example.hujson  # Step 6
  launchd/farm.pilgrim.station.plist.example  # Step 8
  local/compose.yaml            # Step 7: edge + gateway on loopback
pilgrim/tools/render_deploy.py  # template renderer
```

### 5.2 Renderer (`pilgrim/tools/render_deploy.py`)

- CLI: `python -m pilgrim.tools.render_deploy TEMPLATE OUTPUT --set NAME=VALUE ...`
- Replaces each `@@NAME@@` with its value. Exits non-zero, writing nothing,
  if any `@@NAME@@` remains, or if a `--set` name doesn't appear in the
  template.
- No other templating features. Leave `$` alone.
- Tests (`pilgrim/tests/test_render_deploy.py`): replacement works; a missing
  value fails; an unknown name fails; nginx `$remote_addr` passes through
  unchanged; both real templates render with a full set of values and leave no
  `@@`.

### 5.3 Edge template requirements

Placeholders: `@@EDGE_LISTEN@@`, `@@WEB_ROOT@@`, `@@CACHE_DIR@@`,
`@@MEDIA_CACHE_MAX@@`, `@@ORIGIN_SCHEME@@` (`http` or `https`),
`@@ORIGIN_HOSTPORT@@`, `@@ORIGIN_HOST@@`, `@@REALIP_INCLUDE@@`,
`@@CA_BUNDLE@@`.

Required behaviour, each with a comment in the file:

1. `listen @@EDGE_LISTEN@@;` with plain HTTP. TLS is the operator's IaC
   (Cloudflare → edge). Do not add certificates.
2. `include @@REALIP_INCLUDE@@;` near the top of `http {}`. Locally this is
   the empty `realip-local.conf`. In prod the operator supplies a file with
   `real_ip_header CF-Connecting-IP;` and `set_real_ip_from` for each
   Cloudflare range.
3. `location = /edge-health { access_log off; return 200 "ok\n"; }`. This
   **never** contacts the origin, so an origin outage does not make the
   load balancer kill healthy edges.
4. Static site: `root @@WEB_ROOT@@;` for `/`, with `index index.html`.
   `Cache-Control: public, max-age=60` for static files.
5. Defense in depth: `location ^~ /api/admin/ { return 404; }`, and exact
   locations returning 404 for `/api/station/start` and `/api/station/stop`.
6. Upstream:
   `upstream pilgrim_origin { server @@ORIGIN_HOSTPORT@@; keepalive 16; }`.
   `proxy_pass @@ORIGIN_SCHEME@@://pilgrim_origin;`,
   `proxy_http_version 1.1;`, `proxy_set_header Connection "";`,
   `proxy_set_header Host @@ORIGIN_HOST@@;`, `proxy_ssl_server_name on;`,
   `proxy_ssl_name @@ORIGIN_HOST@@;`, `proxy_ssl_verify on;`,
   `proxy_ssl_trusted_certificate @@CA_BUNDLE@@;` (the `ssl` directives do
   nothing over http).
7. Client IP: on every proxied location,
   `proxy_set_header X-Real-IP $remote_addr;` and
   `proxy_set_header X-Forwarded-For $remote_addr;`. This **overwrites** any
   client-supplied value.
8. Timeouts: `proxy_connect_timeout 3s;`. `proxy_read_timeout 30s` by
   default. For `POST /api/requests`, set `proxy_read_timeout 300s` on the
   edge. The gateway uses 270 s (Step 6.1.7). Moderation is
   `httpx.Timeout(120.0)` with one retry (`pipelines/llm.py`,
   `pipelines/moderation.py`), so the worst case is about 240 s. Each hop
   outward must wait **longer** than the hop inside it, so the innermost
   timeout fires first and its answer is the one the browser sees.
9. Media cache (`location ~ ^/api/media/[0-9]+$`): a `proxy_cache_path` at
   `@@CACHE_DIR@@/media` with `max_size=@@MEDIA_CACHE_MAX@@`,
   `inactive=7d`, `use_temp_path=off`. Honor origin headers (no
   `proxy_ignore_headers`). `proxy_cache_lock on;`,
   `proxy_cache_lock_timeout 60s;`, `proxy_cache_lock_age 60s;` (a FLAC can
   take many seconds to cross a home uplink; the 5 s default would send
   duplicate fetches). `proxy_cache_use_stale error timeout updating;` is
   **allowed for media only**. `add_header X-Cache-Status $upstream_cache_status always;`
10. Micro-cache (`/api/station/program`, `/api/station/state`,
    `GET /api/requests`, `/api/health`): a separate small cache zone,
    `proxy_cache_key "$request_method$request_uri";` (the query string matters
    for `program`), honor origin `max-age`, `proxy_cache_lock on;`.
    **No** `proxy_cache_use_stale error`: a dead origin must read as off air,
    not as an old programme. `use_stale updating` is fine. Add
    `X-Cache-Status` too.
11. POSTs (`/api/requests`, `/api/station/heartbeat`, `/api/visitors`) are
    never cached. `client_max_body_size 64k;`
12. Off air: `proxy_intercept_errors off;` (the origin's own 4xx/5xx, such as
    the §5.5 error taxonomy, must pass through untouched). Use `error_page 502
    504 = @offair;` on the `/api/` locations. nginx generates 502/504 itself
    when it can't connect, so this catches only "origin unreachable".
    `location @offair` returns the 503 JSON from Step 4, with `Content-Type:
    application/json` and `Cache-Control: no-store`. For `/api/health`, use a
    separate named location that returns the 200 `{"on_air": false, ...}` body.
    **Exception:** in the `POST /api/requests` location, map 504 to its own
    `504 {"detail": "request timed out"}` JSON, not off air. A slow moderation
    call is not an outage. 502 there still goes to `@offair`.
    The edge only generates these when **it** can't reach the gateway. When the
    gateway is up but the origin is down, the gateway generates the same
    bodies (Step 6.1.9), and the edge passes them through.
13. Anything else under `/api/` that is not listed: `return 404;` at the
    edge. The edge passes only the public route list from Step 1.2.

### 5.4 `make deploy-check`

Add a Makefile target that renders both templates (edge and gateway) into a
temp directory with local values, then runs `nginx -t` on each in a throwaway
container: `docker run --rm -v <tmpdir>:/cfg:ro <image> nginx -t -c /cfg/nginx.conf`.
Use the official `nginx` image pinned to a specific `1.x` tag. Write the tag in
`pilgrim/deploy/README.md`. The target must pass.

Docs: RADIO.md §18 layout gets `pilgrim/deploy/` and
`pilgrim/tools/render_deploy.py`.

---

## Step 6. Gateway files (allowlist) + Tailscale reference files

**You write these files. You do not run Tailscale.** The gateway nginx is
also used, unchanged, in the local split test (Step 7). That is how the
allowlist gets tested before the operator ever brings up Tailscale.

### 6.1 `gateway/nginx.conf.template`

Placeholders: `@@GATEWAY_LISTEN@@`, `@@ORIGIN_PUBLIC_HOSTPORT@@`,
`@@TRUSTED_EDGE_NET@@`, `@@EXTRA_ALLOW_INCLUDE@@`.

1. `listen @@GATEWAY_LISTEN@@;` plain HTTP. In prod, Tailscale serve
   terminates TLS in front of it.
2. `allow @@TRUSTED_EDGE_NET@@; deny all;` at server level. Locally:
   `127.0.0.1`. In prod: `127.0.0.1`, because Tailscale serve proxies from
   inside the container's network namespace. The real edge filtering is the
   tailnet policy (6.4). Explain this in a comment.
3. **Allowlist by exact path and method.** Only these, each with
   `limit_except` for the method:

   | Location | Methods |
   |----------|---------|
   | `= /api/station/program` | GET |
   | `= /api/station/state` | GET |
   | `= /api/station/heartbeat` | POST |
   | `~ ^/api/media/[0-9]+$` | GET |
   | `= /api/requests` | GET POST |
   | `= /api/visitors` | POST |
   | `= /api/health` | GET |

   `location / { return 404; }` for everything else, including `/`, static
   files, and `/api/admin/`.
4. `include @@EXTRA_ALLOW_INCLUDE@@;` inside the server block, for the test
   only. Prod passes an empty file. The local e2e render passes a file that
   allows `^~ /api/test/` (the e2e server's report endpoint). Name the files
   `gateway/extra-none.conf` (empty) and `gateway/extra-e2e.conf`.
5. Pass the edge's IP: `proxy_set_header X-Real-IP $http_x_real_ip;`,
   `proxy_set_header X-Forwarded-For $http_x_real_ip;`.
6. Rate limits keyed by `$remote_addr` (protects the M5 if an edge is
   compromised and floods it with fake client IPs):
   `POST /api/requests` 1 r/s burst 20; `POST /api/visitors` 10 r/s burst 100;
   `POST /api/station/heartbeat` 200 r/s burst 1000. Return 429 when exceeded.
   Put these numbers in comments as starting points for the operator to tune.
7. `client_max_body_size 64k;`, `proxy_connect_timeout 3s;`,
   `proxy_read_timeout 30s` by default, and `270s` for `POST /api/requests`
   (shorter than the edge's 300 s; see Step 5.3.8).
8. `proxy_pass http://@@ORIGIN_PUBLIC_HOSTPORT@@;` with keepalive as on the
   edge.
9. Off air at the gateway: when the **origin** is unreachable (the M5 is up
   but `pilgrim.server` is not), the gateway itself answers, with the same
   rules and bodies as edge item 12 (`proxy_intercept_errors off`; nginx-made
   502/504 → `@offair` 503 JSON; `/api/health` → 200 `{"on_air": false,
   "off_air": true}`; `POST /api/requests` 504 → the "request timed out"
   JSON). Without this, the edge would pass the gateway's HTML 502 page
   straight to the browser, because the edge doesn't intercept upstream
   errors. `split-smoke` check 9 exercises exactly this case.

### 6.2 `gateway/compose.yaml` (for the operator, on the M5)

Two services sharing one network namespace:

- `tailscale`: image `tailscale/tailscale` **pinned to an exact release tag**
  (write it down; don't use `latest`). Environment: `TS_AUTHKEY` from an env
  file that is **not** in the repo (document the variable in
  `pilgrim/deploy/README.md` and the root `README.md`), `TS_HOSTNAME=pilgrim-origin`,
  `TS_EXTRA_ARGS=--advertise-tags=tag:radio-origin`, `TS_USERSPACE=true`,
  `TS_STATE_DIR=/var/lib/tailscale`, `TS_SERVE_CONFIG=/config/serve.json`.
  Named volume for state. No published ports.
- `gateway`: the same pinned `nginx` image, `network_mode: service:tailscale`,
  read-only root filesystem, `tmpfs` for nginx's cache/run dirs, rendered
  config mounted read-only, non-root user if the image supports it.
- The gateway reaches the origin at `host.docker.internal:5000`. Put that in
  the README's example render values, marked "operator verifies on the M5".

### 6.3 `gateway/serve.json`

Tailscale serve config: HTTPS on 443, proxying `/` to
`http://127.0.0.1:8090`. Add a comment in `pilgrim/deploy/README.md` (JSON
has no comments) telling the operator to check the format against the
Tailscale docs for the pinned image tag.

### 6.4 `tailscale/policy.example.hujson`

A **fragment** for the operator to merge into the tailnet policy:

```hujson
{
  "tagOwners": {
    "tag:radio-edge":   ["autogroup:admin"],
    "tag:radio-origin": ["autogroup:admin"],
  },
  "grants": [
    // Edges may reach only the origin gateway, only on 443. Nothing else.
    {"src": ["tag:radio-edge"], "dst": ["tag:radio-origin"], "ip": ["tcp:443"]},
    // No grant has tag:radio-origin as src: the gateway initiates nothing.
    // No edge-to-edge grant. No SSH rules for either tag.
  ],
}
```

Under it, in comments, list the operator's checklist: remove any default
allow-all rule; check that no other grant matches tagged nodes; edges join as
**ephemeral, pre-approved, tagged** nodes, with keys from SSM, not baked into
the AMI; no exit node or subnet routes for `tag:radio-edge`; HTTPS
certificates and MagicDNS enabled for the tailnet.

No tests for 6.2–6.4 beyond `make deploy-check` rendering the gateway
template.

---

## Step 7. Local split test: origin + gateway + edge on this host

**Goal (operator's words):** "ensure we can have audio server and EC2 client
just over local."

### 7.1 `pilgrim/deploy/local/compose.yaml`

- Two services: `gateway` and `edge`, both the pinned `nginx` image, both
  `network_mode: host`, each with a rendered config mounted read-only.
- The edge mounts `pilgrim/web/` read-only as its `WEB_ROOT`.
- Local render values: edge listens on `127.0.0.1:18080`, origin
  `http://127.0.0.1:18090`; gateway listens on `127.0.0.1:18090`, origin
  public at `127.0.0.1:<origin port>`, trusted edge net `127.0.0.1`.
- Project name `pilgrim-split-local`, so it can never collide with
  `pilgrim-nginx`.

### 7.2 `make e2e-split`

1. Start the existing e2e server (`pilgrim.tests.e2e.e2e_server:app`, temp
   library, fakes) on a free port. Never port 5000.
2. Render both templates into a temp dir (gateway with `extra-e2e.conf`) and
   start `local/compose.yaml`.
3. Run the Playwright gap test with `baseURL` set to the edge
   (`http://127.0.0.1:18080`). Change `playwright.config.ts` so that
   `PILGRIM_E2E_BASE_URL`, when set, replaces `baseURL` and the webServer URL
   check. Without it, the config behaves exactly as today.
4. Always tear down the compose project and the e2e server, even on failure
   (`trap` in a script under `pilgrim/deploy/local/`).

`make e2e` must keep working unchanged.

### 7.3 `make split-smoke`: scripted checks through the edge

A script (`pilgrim/deploy/local/smoke.sh`, or Python with httpx under
`pilgrim/tools/`) run against the same running stack. Every check prints
PASS/FAIL, and the script exits non-zero on any FAIL:

1. `GET /edge-health` → 200 `ok`.
2. `GET /` → 200, the body contains the station `index.html`.
3. `GET /api/admin/dj/state` → 404 (edge). Hit the gateway directly on 18090:
   also 404.
4. `GET /api/station/start` and `POST /api/station/start` → 404 at both.
5. `GET /api/station/program` → 200 JSON with `epoch`.
6. Media: take a `media_id` from the program. The first `GET` returns
   `X-Cache-Status: MISS`, the second `HIT`, with an identical body hash.
   The `Cache-Control` header contains `immutable`.
7. `POST /api/requests` returns no Python 500 and no nginx error page. The
   status is from the origin.
8. Spoofing: `POST /api/visitors` with header `X-Real-IP: 203.0.113.9`. The
   origin must record `127.0.0.1`, **not** `203.0.113.9`. Check through the
   e2e server's test report, or a temp-db query if the e2e server exposes
   none. Add the minimal test-only hook to `e2e_server.py` if needed.
9. Off air: stop the e2e server. `GET /api/health` → 200 with
   `on_air: false`. `GET /api/station/program` → 503 with `off_air: true`.
   `GET /edge-health` → still 200. Start it again. Within 5 s,
   `/api/health` shows `on_air: true` again.
   Then repeat with the **gateway** container stopped instead of the e2e server
   (edge-generated off air): same expected responses, then recovery.
10. Gateway rate limit: 30 rapid `POST /api/requests` straight to the gateway
    get at least one 429.

### 7.4 Optional: real origin on this host (only when the operator asks)

Same stack, but the gateway points at the **live** station's public port
(5000), and nothing else changes. You may run this **only if the operator
asks in this session**. It must not modify `station.db`, apart from what a
normal listener does (heartbeats, visitors). Prefer read-only checks 1–6.

### 7.5 Report

Include the full PASS/FAIL list from 7.3 and the `make e2e-split` result, as
well as the §7 checks (`make lint`, `make test`, `make sim`, `make e2e`).

---

## Step 8. Operator runbook: move the origin to the M5 (write it; do not run it)

Write `pilgrim/deploy/RUNBOOK.md` for the **operator**. You do not execute
any of it. It must contain, in order:

1. **Prerequisites on the M5:** Python 3.14 venv from `requirements.txt`;
   ffmpeg/ffprobe on `PATH`; the repo checked out at the **same git SHA** as
   the edges will use; mlx-serve running; `LITELLM_TOKEN` and
   `PILGRIM_DJ_TOKEN` set; power settings so the Mac never sleeps and restarts
   after power loss.
2. **Config on the M5:** the `hosts:` values change from this host's SSH-tunnel
   loopback ports to the M5's LAN/loopback addresses. Also set
   `station.host: 127.0.0.1` (**never 0.0.0.0 on the M5**, which would expose
   the public listener to the LAN), `station.serve_web: false`, and
   `visitors.trusted_proxies` to the gateway peer address **observed** in
   step 6 below. Each value goes in a table with a "how to check" column
   (`curl` probe per AGENTS.md §2).
3. **Freeze and copy data:** stop the station on the EC2. Take a consistent
   DB copy with `sqlite3 pilgrim/station.db ".backup /path/station.db.migrate"`
   (never `cp` a live WAL database). Then `rsync -a` `pilgrim/library/`
   (about 12 GB) and the backup to the M5.
4. **Verify the copy:** run `python -m pilgrim.tools.audit_library` in
   **read-only mode** on the M5 (check its flags first; never use the apply
   flag), and compare the item counts with the EC2.
5. **Start the station** under launchd
   (`launchd/farm.pilgrim.station.plist.example`: `KeepAlive`, log paths,
   working directory, env vars from a file outside the repo).
6. **Start the gateway** (`gateway/compose.yaml`). Make one request through it
   and read the origin's log to see which peer address the gateway's
   connections come from. Put that address in `visitors.trusted_proxies`.
7. **Edges:** render `edge/nginx.conf.template` with prod values
   (`ORIGIN_SCHEME=https`,
   `ORIGIN_HOSTPORT=pilgrim-origin.<tailnet>.ts.net:443`, the Cloudflare
   real-IP include). Note: nginx resolves the upstream hostname at start, so
   the edge's nginx must start **after** tailscaled is up and MagicDNS
   resolves.
8. **Cut over:** point Cloudflare at the edges. Run the `split-smoke` checks
   1–7 against the public hostname.
9. **Rollback:** restart the station on the EC2 from its untouched copy.
   Requests and airplay recorded on the M5 after cutover are lost unless the
   operator copies the DB back with the same `.backup` method.
10. **Backups:** `station.db` via Litestream or a cron
    `sqlite3 ... ".backup"` to the operator's NAS; `library/` via rsync.
    Never copy the live DB file directly.

---

## Step 9. Operator IaC checklist (write into `pilgrim/deploy/README.md`)

A list, not code. The operator owns Packer, Ansible, AWS, Cloudflare and
Tailscale enrollment.

- **AMI:** nginx (same pinned version), the repo at a pinned SHA (only
  `pilgrim/web/` and `pilgrim/deploy/` are used on edges), the rendered edge
  config, `tailscaled` enrolled at boot as an ephemeral `tag:radio-edge` node
  with a key read from SSM, and nginx ordered after Tailscale is up.
- **Load balancer health check:** `GET /edge-health`. Never an `/api/` path.
- **Cloudflare:** proxied DNS; SSL mode Full (strict) with the edge's Let's
  Encrypt cert; a Cache Rule making `/api/media/*` eligible for cache and
  respecting origin `Cache-Control` (Cloudflare does not cache extensionless
  paths by default); `/api/*` otherwise follows origin headers; rate-limit
  rules on `POST /api/requests` and `POST /api/visitors`; real IP via
  `CF-Connecting-IP` (edge `realip` include with Cloudflare's published
  ranges, refreshed periodically). Confirm that Cloudflare's terms allow
  serving FLAC audio at the expected volume.
- **Deploy order:** API changes ship to the M5 first and stay
  backward-compatible with the UI already on the edges. UI changes ship as a
  new AMI plus an instance refresh.

---

## Deferred (do not build now)

- Edge-side heartbeat aggregation (one summary per edge every 5 s). Needs a
  new origin endpoint and a RADIO.md §11 change.
- An API-version field so edge-served UI and origin API can detect mismatches.
- Pre-warming edge media caches when a song is committed.
- Media in R2 or S3 (push model).
- Opus or other non-FLAC delivery (needs gapless verification first; AGENTS.md
  §10 pitfalls).
