VENV := .venv
PY := $(VENV)/bin/python
BIN := $(VENV)/bin
FFMPEG := $(HOME)/bin/ffmpeg

.PHONY: setup venv test sim lint run seed smoke e2e clean install-ffmpeg logs audit audit-apply deploy-check e2e-split split-smoke edge-up edge-down edge-status media-smoke mp3-migrate

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

# Library backfill to MP3 + S3 (RADIO §8.4). Dry run by default:
#   make mp3-migrate MIGRATE_ARGS="--apply --upload"
mp3-migrate:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tools.mp3_migrate $(MIGRATE_ARGS)
