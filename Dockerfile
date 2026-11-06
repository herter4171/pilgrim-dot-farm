# Pilgrim Dot Farm — the Fargate web image (SCALABILITY.md Step 1).
#
#   docker build --target web -t pilgrim:web .      # the deployable image
#   docker build --target smoke -t pilgrim:smoke .  # test only: + ffmpeg for the fakes e2e server
#
# The station (scheduler, producer, pipelines) stays a native launchd process
# on the Mac Studio and is NOT containerised. Nothing in this file names a host
# or port (AGENTS §1.4): binds, ports and data paths come from
# pilgrim/config.yaml plus the overlay in pilgrim/deploy/container/overlay.yaml
# ($PILGRIM_CONFIG_OVERLAY). Secrets come from the environment at run time;
# there is no .env in the image.

FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NO_COLOR=1 \
    PILGRIM_CONFIG_OVERLAY=/app/pilgrim/deploy/container/overlay.yaml \
    PILGRIM_ENV_FILE=

WORKDIR /app

# Dependencies first so a code change does not reinstall them.
COPY requirements.txt pyproject.toml ./
RUN pip install -r requirements.txt

# Application + tests (the fakes-only e2e server is the container smoke;
# AGENTS §1.2). The tests package is small and lets `smoke` share every layer. Library, station.db, logs and .env are excluded by .dockerignore.
COPY pilgrim/ pilgrim/

# Non-root. /app/pilgrim/data is the only writable path (overlay: library,
# station.db, logs); everything else is read-only application code.
RUN groupadd -r -g 10001 pilgrim \
 && useradd -r -u 10001 -g pilgrim -d /app -s /usr/sbin/nologin pilgrim \
 && mkdir -p /app/pilgrim/data \
 && chown -R pilgrim:pilgrim /app/pilgrim/data
VOLUME ["/app/pilgrim/data"]

EXPOSE 8080
USER pilgrim

# Public listener liveness. ECS ignores this unless the task definition
# repeats it (pilgrim/deploy/container/task-definition.json does).
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; r = urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=3); sys.exit(0 if r.status == 200 else 1)"]

CMD ["python", "-m", "pilgrim.server"]


# --- smoke: TEST ONLY, never deployed. pilgrim.tests.e2e.e2e_server seeds
# tone MP3s through encode_mp3 (RADIO §8.2), which needs ffmpeg; the web image
# does no audio work and must not carry it.
FROM base AS smoke
USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
USER pilgrim


# --- web: the Fargate front-end and the default target. Until the web tier
# exists (SCALABILITY.md Step 5) this runs today's single-host server, which is
# enough to build, scan and push the image and to shape the task definition.
FROM base AS web
