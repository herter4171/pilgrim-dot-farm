# Pilgrim Dot Farm Radio

AI-generated radio station (see RADIO.md for the full plan; AGENTS.md for the
working rules).

## Run

```sh
make run      # start the station server (FastAPI/uvicorn on config.port)
make test     # unit tests (fakes only)
make sim      # 24-hour simulated run with assertions
make e2e      # Playwright client gap test (fakes, short items)
make lint     # ruff + mypy
make seed     # seed.py against real backends
make smoke    # real-backend smoke (only when asked)
```

## Environment variables

Secrets and per-deployment keys live in the environment (`.env`, git-ignored),
never in the repo.

| Variable | Required? | Purpose |
|----------|-----------|---------|
| `LITELLM_TOKEN` | yes (LLM/moderation/news) | Bearer token for the LiteLLM backend. Falls back to a `LITELLM_TOKEN=` line in `.env`. |
| `LITELLM_URL` | no (optional) | LiteLLM base URL if not in `config.yaml`. |
| `VISITOR_HASH_SECRET` | for the HIT COUNTER | Stable HMAC-SHA256 key used to derive unique-visitor signatures (RADIO §14). Raw client IPs are never stored. Keep it stable across restarts — rotating it changes deduplication identity. Absent ⇒ the counter shows `Unique visitors: —` and radio/requests keep working. |

## Layout

All application and test code lives under `pilgrim/`; the repo root holds only
repo-level files and `docs/`.
