VENV := .venv
PY := $(VENV)/bin/python
BIN := $(VENV)/bin
FFMPEG := $(HOME)/bin/ffmpeg

.PHONY: setup venv test sim lint run web seed smoke e2e clean install-ffmpeg logs audit audit-apply deploy-check e2e-split split-smoke edge-up edge-down edge-status media-smoke mp3-migrate art-backfill art-smoke reel due-backfill image image-smoke pg-test pg-migrate

# --- setup ---------------------------------------------------------------
venv:
	python3.14 -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt

install-ffmpeg:
	mkdir -p $(HOME)/bin && \
	curl -sL -o /tmp/ff.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz && \
	cd /tmp && tar xf ff.tar.xz && F=$$(ls -d ffmpeg-*-static | head -1) && \
	cp $$F/ffmpeg $$F/ffprobe $(HOME)/bin/ && chmod +x $(HOME)/bin/ffmpeg $(HOME)/bin/ffprobe

# --- checks --------------------------------------------------------------
test:
	PATH="$(HOME)/bin:$(PATH)" $(BIN)/pytest -q

lint:
	$(BIN)/ruff check .
	$(BIN)/mypy . --ignore-missing-imports

sim:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tests.sim_run

tui:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tui

e2e:
	cd pilgrim/tests/e2e && npx --yes playwright test

deploy-check:
	bash pilgrim/deploy/local/deploy_check.sh

e2e-split:
	bash pilgrim/deploy/local/e2e_split.sh

split-smoke:
	bash pilgrim/deploy/local/smoke.sh

# --- containers (SCALABILITY.md Step 1). `image` builds the one deployable
# image, the Fargate web tier (pilgrim:web). The station stays native on the
# M5. Fargate runs ARM64 (SCALABILITY.md §6), native on the M5; an x86 host
# needs buildx + QEMU; PLATFORM= (empty) builds for the host. `image-smoke`
# builds the test-only `smoke` stage (web + ffmpeg) and runs the FAKES-ONLY e2e
# server from it, checking health/programme/site (no real backends).
# DOCKER= the CLI when it is not on PATH (Docker Desktop on macOS:
# /Applications/Docker.app/Contents/Resources/bin/docker).
IMAGE ?= pilgrim
PLATFORM ?= linux/arm64
DOCKER ?= docker
DOCKER_PLATFORM := $(if $(PLATFORM),--platform $(PLATFORM),)
image:
	$(DOCKER) build $(DOCKER_PLATFORM) --target web -t $(IMAGE):web .

image-smoke:
	$(DOCKER) build $(DOCKER_PLATFORM) --target smoke -t $(IMAGE):smoke .
	IMAGE=$(IMAGE) DOCKER=$(DOCKER) bash pilgrim/deploy/container/smoke.sh

# --- Postgres store (SCALABILITY.md Step 2). pg-test runs the WHOLE unit suite
# against a throwaway postgres container (fakes only; the DB is local), so the
# engine-neutral store is proven on both engines. Extra pytest args pass through:
#   make pg-test PG_TEST_ARGS="pilgrim/tests/test_requests.py -x"
pg-test:
	DOCKER=$(DOCKER) PY=$(PY) bash pilgrim/deploy/local/pg_test.sh $(PG_TEST_ARGS)

# One-shot station.db -> Postgres copy (ids preserved, sequences set, counts
# verified). Dry run (copies inside a transaction, then rolls back) unless
# MIGRATE_ARGS="--apply". Needs DATABASE_URL (+ DB_PASS_SECRET_ARN or
# DATABASE_PASSWORD) in the environment or .env.
pg-migrate:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tools.pg_migrate $(PG_MIGRATE_ARGS)

# --- EC2 stateless half (edge :8080 behind the ALB). Production runs with
# LOCAL_GATEWAY=0 and the M5's tailnet name as ORIGIN_HOST (pilgrim/deploy/
# README.md "Current production"). Env passes through: LOCAL_GATEWAY, ORIGIN_SCHEME,
# ORIGIN_HOST, ORIGIN_HOSTPORT, MEDIA_CACHE_MAX, TRUSTED_PROXY_CIDR (default:
# this instance's VPC CIDR). See pilgrim/deploy/interim/up.sh.
edge-up:
	bash pilgrim/deploy/interim/up.sh start

edge-down:
	bash pilgrim/deploy/interim/up.sh stop

edge-status:
	bash pilgrim/deploy/interim/up.sh status

# --- run --------------------------------------------------------------
run:
	PATH="$(HOME)/bin:$(PATH)" LITELLM_TOKEN=$$(grep LITELLM_TOKEN .env | cut -d= -f2) \
		$(PY) -m pilgrim.server   # public + admin listeners from config.yaml

# The Fargate web tier, locally (SCALABILITY.md Step 5). Needs REDIS_URL (a
# station publishing to it) and optionally DATABASE_URL in the env or .env.
web:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.web_app

logs:
	tail -f pilgrim/logs/station.log

audit:
	$(PY) -m pilgrim.tools.audit_library

audit-apply:
	$(PY) -m pilgrim.tools.audit_library --apply

seed:
	PATH="$(HOME)/bin:$(PATH)" LITELLM_TOKEN=$$(grep LITELLM_TOKEN .env | cut -d= -f2) $(PY) -m pilgrim.seed

smoke:
	PATH="$(HOME)/bin:$(PATH)" LITELLM_TOKEN=$$(grep LITELLM_TOKEN .env | cut -d= -f2) \
		$(PY) -m pilgrim.tests.smoke $(SMOKE_ARGS)

# Real S3 + CloudFront round trip of one 2 s tone (RADIO §8.4). Only when asked.
media-smoke:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tools.media_smoke

# Cover-art backfill (ALBUM_ART.md Step 9). Dry run by default; the live
# worker renders on its own, this is for deliberate/targeted runs:
#   make art-backfill ART_ARGS="--apply --limit 20"
#   make art-backfill ART_ARGS="--item 132 --force --seed 7"
art-backfill:
	PATH="$(HOME)/bin:$(PATH)" LITELLM_TOKEN=$$(grep LITELLM_TOKEN .env | cut -d= -f2) \
		$(PY) -m pilgrim.tools.art_backfill $(ART_ARGS)

# One real FLUX render through S3 + CloudFront (ALBUM_ART.md Step 10).
# Only when asked: ~40 s of GPU and one S3 object.
art-smoke:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tools.art_smoke

# Instagram Reel builder (ALBUM_ART.md Step 11; ffmpeg only, never posts):
#   make reel REEL_ARGS="--item 132 --start 0 --duration 60"
reel:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tools.make_reel $(REEL_ARGS)

# Library backfill to MP3 + S3 (RADIO §8.4). Dry run by default:
#   make mp3-migrate MIGRATE_ARGS="--apply --upload"
mp3-migrate:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tools.mp3_migrate $(MIGRATE_ARGS)

# Recover requests.due_at (Prep Time cap, RADIO §11) from the station log for
# rows that aired before the column existed. Dry run unless DUE_ARGS="--apply".
due-backfill:
	$(PY) -m pilgrim.tools.due_backfill $(DUE_ARGS)
