# SCALABILITY.md — the origin stays home, everything public moves into the VPC

Status: Step 1 (containers) done and pushed as `pilgrimdotfarm/front-end`;
the Fargate service is up behind the ALB (2026-11-05). Step 8's bastion is the
old edge host advertising the VPC route (pilgrim/deploy/README.md "Bastion
subnet router"). Step 2 (Postgres store) is in code: `pilgrim/db.py`,
`make pg-test`, `make pg-migrate`; the production cutover (deploy README
"Postgres cutover") has not been run: the station stays on SQLite until the
web tier can take heartbeats and the programme from Valkey, so Step 3 lands
first and the cutover is one event. Step 3 (hot state, including Step 4's
wake flag) is in code: `pilgrim/hotstate.py`, `REDIS_URL`, `cache:`. Steps 5–7
and 9 have not started. RADIO.md remains the source of truth; every section this plan
changes is listed in §5 and is updated in the same commit as the code.

## 0. Target

```
listener → Cloudflare → ALB :443 → ECS Fargate "web" service (N tasks, :8080)
                                       ├─ ElastiCache Valkey   (REDIS_URL, TLS, no auth)
                                       └─ RDS Postgres         (DATABASE_URL + DB_PASS_SECRET_ARN)

Mac Studio "station" (scheduler + producer + pipelines, LAN backends)
   → tailscale → bastion EC2 (subnet router into the VPC) → Valkey + Postgres
   → S3 → CloudFront                                         (unchanged)
```

Today the EC2 edge dials *into* the Mac (Tailscale serve). Tomorrow the Mac
dials *out* into the VPC and no listener request ever reaches it. The only
state the Mac keeps is what it alone can make: the media files, the FLAC
masters, and the `.env` with the production keys. Everything a browser can
ask for comes from Valkey, Postgres or CloudFront.

## 1. Who owns what

| Tier | Owns | Reads | Writes |
|------|------|-------|--------|
| Browser | nothing | CloudFront media, web API | heartbeats, requests, votes, ratings, visitors |
| Fargate `web` | the public listener + static site; off-air when the station's liveness key expires | Valkey hot state, Postgres board/votes | heartbeats + airplay, requests, votes, ratings, visitors, Stripe webhooks |
| Valkey | hot state with TTLs | | committed window, `state`, epoch/revision, `station:alive`, `player:<id>` heartbeats, wake flag, rate-limit counters |
| Postgres | `station.db`'s tables, unchanged in meaning (RADIO §14) | | both tiers, on disjoint tables except `requests` and `airplay` |
| Mac `station` | scheduler, producer, pipelines, S3 publisher, art, TUI (admin listener stays loopback) | requests, votes, listener keys | items, program, hot state, request lifecycle |
| S3 + CloudFront | every delivered byte, art included | | station publisher (unchanged) |

## 2. The changes, in dependency order

1. **Containers (now).** One deployable image, the Fargate web tier
   (`pilgrim:web`, no ffmpeg). The station is **not** containerised: it stays
   the native launchd process on the Mac, which needs LAN backends, Metal
   and the library on local disk. Config stays `pilgrim/config.yaml`; the
   container passes a small overlay (`PILGRIM_CONFIG_OVERLAY`) for binds,
   ports and data paths, so no address is hardcoded in the image (AGENTS
   §1.4). Until the web tier exists, the image runs today's single-host
   server, which is enough to build, scan and push it and to shape the task
   definition. A test-only `smoke` build stage adds ffmpeg so the fakes e2e
   server can run inside the image. Deliverables: `Dockerfile`,
   `.dockerignore`, `pilgrim/deploy/container/`, `make image`,
   `make image-smoke`.

2. **Postgres store.** `store.py` is SQLite-only: `?` placeholders,
   `executescript` migrations, `PRAGMA table_info`, `INSERT OR IGNORE`,
   `json_extract`, `datetime('now')`. It becomes a `Store` interface with a
   Postgres implementation (psycopg 3, pinned) and versioned migrations;
   SQLite stays for `make test`/`make sim` (AGENTS §1.2) and the
   single-host dev run (decision, §6). A one-shot `tools/pg_migrate.py` copies `station.db`
   into RDS. `items.media_path` stays absolute and Mac-local: only the
   station reads it. Connection details come from `DATABASE_URL`; the
   password is fetched from `DB_PASS_SECRET_ARN` (JSON `username`/`password`)
   at start, never logged (AGENTS §1.7). Both tiers use the same code path:
   the web tier through its task role, the Mac through the `mac-studio`
   profile it already uses for S3 (`media.aws_profile`), which can read the
   secret (verified 2026-11-05: secret → login → `PostgreSQL 18.6`, user
   `pilgrim`, 0 tables). Driver: psycopg 3.3.x, which has binary wheels for
   Python 3.14; pin it in Step 2.

3. **Hot state in Valkey.** The station publishes what the browser polls
   every 1–5 s so no listener request needs Postgres: the committed window
   as `program` already serialises it (urls included), `state`, `epoch`,
   `revision`, `idle`, the rating window, and `station:alive` with a short
   TTL. Fargate writes `player:<id>` keys with `listener_timeout_s` TTLs;
   the station counts them for the live listener count and the idle policy.

4. **Wake becomes asynchronous** (done inside Step 3). Today
   `GET /api/station/program` thaws a frozen station in the same request
   (IDLE.md D2). On the split, Fargate serves the frozen window (always
   playable, `on_air` true) and sets the `wake` key; the station takes it on
   its next publish tick and thaws, bumping the revision so the browser's
   next poll picks up the thawed programme. RADIO §3 now says "within
   `cache.publish_s`"; the station half is in `hotstate.py`, the web half
   (setting the key) lands with Step 5.

5. **The `web` entry point.** `pilgrim/web_app.py` (name per RADIO §18 when
   written): the public routes of `server.py` re-implemented over Valkey +
   Postgres, plus the static mount. It replaces the edge nginx, the gateway
   nginx and Tailscale serve, so their jobs move with it: off-air bodies
   when `station:alive` is missing (RADIO §11 "Edge-generated off-air
   responses"), the voting/rating path allowlist, `Cache-Control` as today,
   and a `/api/health` that never depends on the station (§6).
   Client IP comes from the ALB's `X-Forwarded-For` (Cloudflare puts the
   browser first); `visitors.trusted_proxies` becomes the VPC CIDR. The
   request-line and vote rate limiters move from process memory into Valkey
   so N tasks share one ceiling. `payments.py` moves to this tier unchanged
   (it never touched playout: AGENTS §1.11); the Stripe webhook URL moves
   with it. Moderation (LiteLLM), the copied-lyrics screen (Brave) and the
   paid summary are public APIs reachable from Fargate.

6. **No origin media fallback.** `/api/media/<id>` cannot exist on Fargate.
   `require_published` already holds requests until S3 has the file; the
   same gate applies to every committed item, and the emergency pack is
   uploaded at seed. The browser's origin-fallback branch in `player.js`
   retries CloudFront instead.

7. **Station as a writer.** `server.py` keeps the admin listener (TUI,
   `/api/admin/*`) on loopback and drops the public listener once the web
   tier is live. It reads the request queue and votes from Postgres as it
   reads SQLite now, counts listeners from Valkey, and publishes hot state
   after every scheduler tick (§3). Hard rule 1 still holds: a slow or
   unreachable VPC degrades to stale hot state and an off-air page, never a
   blocked playhead.

8. **Infrastructure (terraform-aws-pilgrim).** Bastion EC2 running
   `tailscale up --advertise-routes=<VPC CIDR>` with source/dest check off,
   and the Mac accepting routes; ECR repository; ECS cluster + Fargate
   service behind the existing target group (replacing the instance
   attachment), task role with `secretsmanager:GetSecretValue` on the DB
   secret, execution role for ECR + CloudWatch Logs; target group health
   path `/api/health` at cutover (§6); cache and DB security
   groups gain ingress from the task SG and the bastion SG (today only the
   EC2 SG). Valkey Serverless is TLS-only, so `REDIS_URL` is `rediss://`
   and "no auth" means no password, not plaintext. The `mac-studio` user
   can read the DB secret (done), so the Mac needs no bastion hop for the
   password; the bastion's instance tags carry the same three variables for
   reference. An AWS account change is expected soon, so nothing Mac-side
   hardcodes the account id: ARNs and endpoints live in `.env`. Tasks sit
   in the default VPC's public subnets with public IPs, so LiteLLM, Brave
   and Stripe are reachable without a NAT gateway.

9. **Cutover.** Run the web service behind the ALB on a second target group
   and test it from a `/etc/hosts` override; then switch the listener rule,
   stop `make edge-up`, remove Tailscale serve from the M5, and retire
   `deploy/edge`, `deploy/gateway`, `deploy/interim` and the EC2 edge.

## 3. What does not change

Scheduler, selector, producer, pipelines, QC, normalisation, S3/CloudFront
delivery, cover art, the TUI, the browser's Web Audio engine, and every
public API shape in RADIO §11 except the two noted (`/api/media` goes away,
wake latency). Tests keep running on fakes and SQLite.

## 4. Environment contract

| Variable | Tier | Meaning |
|----------|------|---------|
| `REDIS_URL` | web, station (native, from `.env`) | `rediss://host:port` for Valkey (TLS, no password) |
| `DATABASE_URL` | web, station (native, from `.env`) | `postgresql://user@host:5432/pilgrim` (no password) |
| `DB_PASS_SECRET_ARN` | web, station (native, from `.env`) | Secrets Manager secret, JSON `{username, password}`; the web task role and the Mac's `mac-studio` profile can both read it |
| `PILGRIM_CONFIG_OVERLAY` | web | YAML merged over `config.yaml` (binds, ports, data paths) |
| `LITELLM_TOKEN`, `BRAVE_SEARCH_KEY`, `STRIPE_*`, `PAID_OVERRIDE_TOKEN`, `PILGRIM_DJ_TOKEN` | as today (README "Secrets") | web gets only the Stripe keys, Brave key and LiteLLM token |

## 5. RADIO.md sections this plan will touch

§2 (hosts), §3 (architecture, wake), §8.4 (no origin fallback), §11 (public
listener moves, `/edge-health`, wake latency), §12 (new env contract and a
`cache:`/`database:` sketch), §14 (Postgres, hot state), §18 (layout).
AGENTS §2 (environment table) and §10 (pitfalls) follow.

## 6. Decisions (operator, 2026-11-05)

- **SQLite stays for tests and the dev run.** `make test`/`make sim` never
  need a Postgres; production uses the Postgres store. Step 2 therefore
  keeps a `Store` interface with two implementations and one shared test
  module, and accepts the dialect split as the price.
- **Health path is `/api/health`.** The web tier does not grow
  `/edge-health`; the ALB target group's `health_check.path` changes to
  `/api/health` in terraform-aws-pilgrim **at cutover** (Step 9), not
  before: today's EC2 edge answers only `/edge-health`, so changing it
  earlier would kill the live edge. Until the web tier exists, the public
  `/api/health` is the station's `{on_air, art, time}` body (RADIO §11);
  the web tier keeps that shape and answers 200 even when the station's
  liveness key is gone (`on_air: false, off_air: true`), so an origin
  outage never makes the ALB drain healthy tasks.
- **Fargate runs ARM64** (Graviton). `make image` defaults to
  `PLATFORM=linux/arm64`, native on the M5; building on an x86 host needs
  Docker's QEMU binfmt (`docker buildx`). The task definition pins
  `cpuArchitecture: ARM64`.

- **Airplay stays one row per heartbeat** (5 s precision is wanted; the
  20 GB volume is not a concern at the current constant rate). On the split
  the web tier inserts those rows into Postgres directly; nothing goes
  through Valkey for airplay.

## 7. Open questions

None at the moment.
