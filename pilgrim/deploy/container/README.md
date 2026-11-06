# Container — the Fargate web image (SCALABILITY.md Step 1)

```
docker build --target web -t pilgrim:web .   # the one deployable image
make image                                   # same, tagged $(IMAGE):web, linux/arm64
make image-smoke                             # test-only `smoke` stage (+ffmpeg) runs the fakes e2e server; PASS/FAIL
```

The station (scheduler, producer, pipelines, S3 publisher) is **not**
containerised: it stays a native launchd process on the Mac Studio (RADIO §2,
`pilgrim/deploy/mac/origin.sh`). Only the public web tier ships as an image:
`python -m pilgrim.web_app` (SCALABILITY.md Step 5, RADIO §11 "Web tier"),
which serves the site and the public API from Valkey (`REDIS_URL`, required)
and Postgres (`DATABASE_URL`) and never reaches the Mac. Without the station's
`alive` key it answers off-air bodies; `/api/health` stays 200.

The image holds application code only: no `.env`, no `station.db`, no
`library/` (`.dockerignore`), and no ffmpeg (the web tier does no audio work).
It runs as uid 10001 and the one writable path is `/app/pilgrim/data`
(library, `station.db`, logs), declared as a volume. The `smoke` stage adds
ffmpeg only because the fakes e2e server encodes tone MP3s; it is never pushed.

## Registry

The image is published to Docker Hub as `pilgrimdotfarm/front-end:latest`
(first push 2026-11-05, linux/arm64). The task definition's `@@ECR_REPO_URL@@`
placeholder can point at this repository instead of ECR; a private Hub repo
needs a `repositoryCredentials` secret on the container definition, a public
one needs nothing.

```
docker tag pilgrim:web pilgrimdotfarm/front-end:latest
docker push pilgrimdotfarm/front-end:latest
```

## Configuration

`pilgrim/config.yaml` is the configuration; the image does not carry a second
one. `overlay.yaml` is deep-merged over it at load time because the Dockerfile
sets `PILGRIM_CONFIG_OVERLAY` to its path. It changes only what differs inside
a container (RADIO §12):

| Key | Value | Why |
|-----|-------|-----|
| `station.host` / `station.port` | `0.0.0.0` / `8080` | reachable from outside; matches the ALB target group |
| `station.admin_host` | `127.0.0.1` | the admin listener never leaves the container |
| `station.serve_web` | `true` | no nginx edge: the app serves `pilgrim/web/` |
| `library.dir` / `library.db` / `logging.dir` | under `data/` | the one writable volume |

Run with another overlay by overriding the variable; unset or empty disables
the merge. The loader rejects overlay keys the `Config` model does not have
(pydantic would silently ignore a typo), then validates the merged mapping
with that same model (`test_config.py`).

## Environment

Secrets and endpoints come from the task environment (README "Secrets"; no
`.env` in the image, and `PILGRIM_ENV_FILE=` disables the fallback):

| Variable | Used by | Notes |
|----------|---------|-------|
| `REDIS_URL` | web (station reads it natively, Step 3) | `rediss://host:port`; Valkey Serverless is TLS-only, "no auth" = no password |
| `DATABASE_URL` | web (station natively, Step 2) | `postgresql://pilgrim@host:5432/pilgrim`, no password in the URL |
| `DB_PASS_SECRET_ARN` | web (station natively, Step 2) | Secrets Manager JSON `{"username","password"}` (RDS-managed master secret) |
| `STRIPE_SECRET_ARN` | web | Secrets Manager JSON `{STRIPE_SECRET_KEY, STRIPE_PUBLISHABLE_KEY, STRIPE_WEBHOOK_SECRET}`; read with the task role at start and exported into the environment (`pilgrim/aws_secrets.py`). The task role needs `GetSecretValue` on it |
| `LITELLM_TOKEN`, `BRAVE_SEARCH_KEY`, `PAID_OVERRIDE_TOKEN` | as today | plain env, ECS `secrets`, or extra keys in the `STRIPE_SECRET_ARN` document (allow-listed) |

`REDIS_URL`, `DATABASE_URL`/`DB_PASS_SECRET_ARN` and `STRIPE_SECRET_ARN` are
read at start. Still to add for full function: `LITELLM_TOKEN` (request
moderation, paid summaries), `BRAVE_SEARCH_KEY` (copied-lyrics screen) and
`PAID_OVERRIDE_TOKEN`, either as more keys in the `STRIPE_SECRET_ARN` document
or as ECS `secrets`. Tasks reach LiteLLM, Brave and Stripe through the NAT.

## Task definition

`task-definition.json` is the Fargate shape with `@@NAME@@` placeholders for
IaC to fill (the same convention as the nginx templates), pinned to ARM64.
Container port 8080 matches the ALB target group. The health path is
`/api/health` (operator decision, SCALABILITY.md §6): the target group's
`health_check.path` moves from `/edge-health` to `/api/health` in
terraform-aws-pilgrim at cutover, not before, because the live EC2 edge only
answers `/edge-health`.

## Local run

```
docker run --rm -p 127.0.0.1:8080:8080 pilgrim:web
```

Until Step 5 this starts today's single-host server over an empty library in
the container's `data/` volume (the media publisher logs
`media.publisher_disabled` without AWS credentials). It proves the image
starts, binds and serves the site; `make image-smoke` is the functional check.

## Building for Fargate

Fargate runs ARM64 (Graviton). `make image` passes `--platform linux/arm64`
by default, which is native on the M5. On an x86 host (the EC2 edge `pf` is
`c7i-flex`) the build needs Docker's QEMU binfmt and buildx; `PLATFORM=` builds
for the host instead, for a local smoke only.
