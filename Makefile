VENV := .venv
PY := $(VENV)/bin/python
BIN := $(VENV)/bin
FFMPEG := $(HOME)/bin/ffmpeg

.PHONY: setup venv test sim lint run seed smoke e2e clean install-ffmpeg

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

e2e:
	cd pilgrim/tests/e2e && npx --yes playwright test

# --- run --------------------------------------------------------------
run:
	PATH="$(HOME)/bin:$(PATH)" LITELLM_TOKEN=$$(grep LITELLM_TOKEN .env | cut -d= -f2) \
		$(BIN)/uvicorn pilgrim.server:create_app --factory --host 0.0.0.0 --port 5000

seed:
	PATH="$(HOME)/bin:$(PATH)" LITELLM_TOKEN=$$(grep LITELLM_TOKEN .env | cut -d= -f2) $(PY) -m pilgrim.seed

smoke:
	PATH="$(HOME)/bin:$(PATH)" $(PY) -m pilgrim.tests.smoke
