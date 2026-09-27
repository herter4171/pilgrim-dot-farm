# PROGRESS — Pilgrim Dot Farm Radio

Status tracker. Source of truth for *what exists, what's verified, what's
left.* RADIO.md is the plan; this file is the ledger. Update it whenever the
state changes. Last updated: 2026-09-27.

---

## Current state (headline)

- **All application code lives in `pilgrim/`** per RADIO.md §18 (was root-sprawl).
- **`make test` 26 pass · `make lint` clean (ruff + mypy) · 24 h sim passes.**
- **News pipeline works against REAL backends**: searxng search via the LiteLLM
  MCP server + qwen38 (reasoning, 4096-token budget) → attributed bulletin.
- **All 4 backends green** in `make smoke`: litellm, kokoro, mlx, searxng(via MCP).
- **Site is served** (`GET /` + assets) and covered by backend-free API tests.
- **Playwright MCP provisioned and proven headed on VNC :1** with public
  internet egress; **survived a reboot**.

## Milestone status (AGENTS.md §4)

| # | Milestone | Status |
|---|-----------|--------|
| M0 | Skeleton: layout, config loader, Clock/RNG, Makefile, empty suite | ✅ done (`make test` green) |
| M1 | Backend probes → `docs/backends.md` | ✅ done + updated to real topology |
| M2 | Fakes (`fake_minimax`/`kokoro`/`llm`), failure injection | ✅ done |
| M3 | `store.py`: inventory + airplay ledger | ✅ done |
| M4 | Audio QC + normalize (−16 LUFS, FLAC) | ✅ done |
| M5 | Pipelines: songs, voice, news | ✅ done (news verified live) |
| M6 | `producer.py`: inventory targets + workers | ✅ sim shows targets held |
| M7 | `scheduler.py` + `RandomSelector` (§5) | ✅ 24 h sim passes all §15 asserts |
| M8 | `server.py` endpoints (§11) | 🟡 partial — static + program/media/heartbeat tested; full API suite pending |
| M9 | Web client (§9) | 🟡 site served + tested; client e2e gap test scaffolded, not yet green |
| M10 | `seed.py` + real-backend smoke | 🟡 smoke green; news verified live; full seed (songs/commercials) not yet run live |
| M11 | Real listening run (60 min) | ⬜ not started |

## Checks (last run)

| Command | Result |
|---------|--------|
| `make test` | ✅ 26 passed (2.5 s) |
| `make lint` (ruff) | ✅ All checks passed |
| `make lint` (mypy) | ✅ Success, 27 files |
| `make sim` | ✅ ALL SIM ASSERTIONS PASSED (878 items, 87 013 s, min coverage 600 s) |
| `make smoke` | ✅ litellm, kokoro, mlx, searxng(via MCP) all OK |
| `make e2e` | 🟡 scaffolded; needs `npm install` in `pilgrim/tests/e2e` + browser (now available) |

## Git history (11 commits, one feature each)

```
4a84f0d feat(news)    real searxng via LiteLLM MCP + tolerant reasoning LLM client
480a500 docs(agents)  pilgrim/ package layout
4407b29 feat(e2e)     Playwright client gap-test scaffold
b105f52 feat(smoke)   real-backend probe
003f717 feat(bible)   station canon seed
d3d7e91 feat(web)     serve the client site
86b8798 style(lint)   ruff passes
bdef318 fix(types)    mypy passes (2 latent bugs fixed)
c24e73c refactor      move app into pilgrim/ package (RADIO.md §18)
5a7aac8 chore         baseline import + .gitignore hygiene
```

Branch `master`, no remote configured yet.

## Directory layout (satisfies RADIO.md §18)

```
pilgrim/            # the application package (all app + test code)
  config.yaml  config.py  server.py  scheduler.py  selector.py
  producer.py  store.py  seed.py
  audio/  pipelines/  web/  prompts/  bible/  tests/  library/
Makefile  pyproject.toml  requirements.txt  AGENTS.md  RADIO.md
SETUP_PLAYWRIGHT.md  docs/  old_style/   # repo-root meta
```

## Backends — actual topology (verified live)

| Component | Where | State |
|-----------|-------|-------|
| mlx-serve (song gen) | `127.0.0.1:11234` `/v1/models` | ✅ up |
| Kokoro TTS | `localhost:8001` `/health` | ✅ up |
| LiteLLM (LLMs) | `localhost:4000` `/v1/models` | ✅ up |
| searxng | **via LiteLLM MCP** `localhost:4000/mcp` (`web_search-searxng_web_search` tool) — NOT `:8888` | ✅ up |

Key findings baked into code/docs/backends.md:
- searxng is an MCP tool behind LiteLLM (Streamable-HTTP, bearer auth), not an
  HTTP `/search` service.
- qwen38 is a reasoning model: `news` needs `max_tokens=4096`; LLM client
  extracts final text via `content` → `provider_specific_fields` fallback.

## Playwright / host environment (SETUP_PLAYWRIGHT.md)

- GNOME + TigerVNC on **display `:1`** → `127.0.0.1:5901` (localhost-only, 1920×1080).
- **Chrome 154** stable (headed).
- **playwright-mcp 0.0.82** as systemd `playwright-mcp.service` → `127.0.0.1:8931/mcp`,
  persistent profile `--user-data-dir=/home/ec2-user/.local/share/playwright-mcp/profile`,
  `--allowed-hosts '*'`, **not headless**.
- Proved: navigated `example.com` + `google.com`, window visible on VNC :1;
  **both services survived a reboot** (uptime-verified).
- VNC password set (operator changeable with `vncpasswd` as ec2-user).

## Deviations from AGENTS.md / RADIO.md

1. **AGENTS §2 "tests never call real backends"** — operator overrode this for
   verification: news generation and `make smoke` exercised real
   LiteLLM/Kokoro/mlx/searxng to prove functionality. Unit tests still run
   exclusively on fakes.
2. **Ruff profile** scoped to `E,F,I,UP,B,SIM` (pragmatic); pylint-family and
   builtin-shadowing rules excluded (defensive-dispatch style).
3. searxng/reasoning-model config and client now reflect reality, which differs
   from the original `config.yaml` sketch (`:8888`, tight budgets).
4. Emergency-pack (§8.3) is canon-documented only; rendering is a deploy-time
   (M10) step, not in code yet.

## Open work / next steps

1. **M10 live seed** — run `make seed` against real backends to materially hit
   inventory minimums (songs, commercials, liners) and validate the cold-start
   path; emergency-pack rendering.
2. **M9 e2e** — `cd pilgrim/tests/e2e && npm install && npx playwright test` to
   actually green the client gap test (browser + station-egress now available).
3. **M8 API suite** — broaden backend-free server tests (locked to fakes).
4. **M11** — 60-min real listening run, no gaps; verify via client gap log.
5. searxng reachability confirmed; wire live song timing (mlx ignores
   `duration_s`; see backends.md gap).
