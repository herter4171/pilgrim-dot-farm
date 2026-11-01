# AGENTS.md — Pilgrim Dot Farm Radio

Active work plan: PAYMENT.md (paid priority requests via Stripe; start at
Step 0). SPLIT_PLAN.md is complete (Steps 1–10) and stays the reference for
the edge/gateway/origin layout.

You are building an AI-generated radio station. **`RADIO.md` is the source of
truth.** Read the relevant section of RADIO.md before implementing anything,
and cite the section number (e.g. "per §5.2") in commit messages and reports.
If this file and RADIO.md disagree, RADIO.md wins; report the conflict.

**Layout:** all application + test code lives under `pilgrim/` (RADIO.md §18).
The repo root holds only repo-level files (`AGENTS.md`, `RADIO.md`, `Makefile`,
`pyproject.toml`, `requirements.txt`, `docs/`). `make test/sim/lint/e2e` and
`make run/seed/smoke` all resolve the `pilgrim` package paths.

---

## 1. Hard rules

These are not preferences. Do not break them, even if a task seems to require it.
If a task cannot be done without breaking one, stop and report.

1. **Playout never waits on production.** Nothing in `pilgrim/scheduler.py` or the
   client may block on an LLM, Kokoro, or MiniMax call. Only fully rendered,
   QC-passed, normalized items enter the committed program.
2. **Tests never call real backends.** Unit, simulation, and e2e tests use the
   fakes in `pilgrim/tests/fakes/`. Real backends are for explicit smoke tests only
   (§6 below).
3. **No direct `time.time()`, `datetime.now()`, `asyncio.sleep()` for
   scheduling, or `random` in `pilgrim/scheduler.py`, `pilgrim/selectors.py`, or
   `pilgrim/producer.py`.** Use the injected `Clock` and RNG. This is what makes the
   24-hour simulation possible.
4. **All hosts, ports, model names, weights, and targets come from
   `pilgrim/config.yaml`.** No hardcoded addresses anywhere else.
5. **Never delete or modify files in `pilgrim/library/`** or anything flagged
   `emergency`. Never delete `station.db`. Tests use a temp directory.
6. **Song generation is expensive** (~1.6 s of compute per 1 s of audio on
   the M5; the GPU boxes are faster but not free). Never start real song
   generation except in a smoke test (`make smoke SMOKE_ARGS=--gen`), and then
   request ≤ 10 s of audio per render.
7. **No secrets in the repo.** Keys go in environment variables; document the
   variable name in `README.md`.
8. **Do not change the HTTP API (§11), the config schema (§12), or the
   database schema** without updating RADIO.md in the same change and saying
   so in your report.
9. **Client audio uses the Web Audio API** (`decodeAudioData` +
   `AudioBufferSourceNode.start(when)`). Never chain `<audio>` elements.
10. **News is never recycled.** Contextual items (DJ talk-ups, time checks) are
    never recycled. Enforce this in code, not just in prompts.
11. **Payments never touch playout, and the browser is never trusted about
    money** (PAYMENT.md §0.3). `pilgrim/scheduler.py`, `pilgrim/selector.py`
    and `pilgrim/producer.py` never import `pilgrim/payments.py` or call
    Stripe. Only a signature-verified webhook or a server-side session
    retrieve marks a request paid. Moderate before creating a Checkout
    Session. Tests use `pilgrim/tests/fakes/fake_stripe.py`; real Stripe calls
    happen only in `make pay-smoke` with a **test-mode** key. Never log or
    store keys, `client_secret`, card data or customer email.
    **Operator-approved exception (PAYMENT.md Step 12b):** a paid prompt whose
    first line equals the `PAID_OVERRIDE_TOKEN` secret is an operator comp. It
    is marked queued with `comped=1` and amount 0 without Stripe, after the
    same moderation and copyright checks. Treat the token like a key: never in
    the repo, logs, DB, prompts or responses. Do not remove this path as a
    "fix" for the rule above.

---

## 2. Environment

| Component | Where | Notes |
|-----------|-------|-------|
| Station server | M5 | also one of the three song generators (mlx-serve) |
| Song generation | pool in `songs.gen_backends` | MiniMax Music 3 ×3 (M5 mlx-serve + two audiocpp GPU boxes) via LiteLLM's vLLM pass-through `localhost:4000/vllm/v1/audio/music-generations`, `model: MiniMax-Music-3`, Bearer `LITELLM_TOKEN`. The station enforces the slot ceiling and staggers submissions (docs/backends.md §2b). Read `pipelines/songs.py` before writing new code. |
| `qwen38` (Qwen 3.8 27B) | 5090 host | via LiteLLM; news, song briefs, DJ talk |
| `ornith` | 4070 Ti host | via LiteLLM; commercials, liners |
| Kokoro TTS | `192.168.68.89:8001` | `/tts`, `/voices`, `/health`; 16-bit mono 24 kHz |
| searxng | see config | news search |
| Stripe | `payments.api_base` in config | Checkout Sessions + webhooks; keys in `STRIPE_SECRET_KEY`, `STRIPE_PUBLISHABLE_KEY`, `STRIPE_WEBHOOK_SECRET` (PAYMENT.md Step 0) |

**Do not guess external APIs.** Before writing a client for any backend,
probe it (`curl .../health`, `curl .../voices`, a minimal request) and record
the actual request/response shapes in `docs/backends.md`. Code against what
you observed, not what you expect.

---

## 3. Stack

Defaults. If the repo already uses something different, follow the repo and
note it.

- **Python 3.11+**, asyncio throughout.
- Server: FastAPI + uvicorn. HTTP client: `httpx.AsyncClient` with explicit
  timeouts on every call.
- Validation: pydantic models for config, LLM JSON outputs, and API payloads.
- Audio: `soundfile` + `numpy` for QC; **ffmpeg CLI** for two-pass `loudnorm`,
  silence trimming, and FLAC encoding.
- Storage: SQLite (`station.db`) via the stdlib `sqlite3` module; media files
  on disk under `pilgrim/library/`.
- Web: plain HTML/CSS/JS, **no build step, no framework**.
- Tests: pytest, pytest-asyncio; Playwright for the client gap test.

Add a dependency only when the stdlib or an existing dependency can't do the
job, and pin it in `requirements.txt`.

---

## 4. Build order

Work milestone by milestone. Do not start a milestone until the previous one's
checks pass. Each milestone ends with a commit and a short report (§8).

| # | Milestone | Done when |
|---|-----------|-----------|
| M0 | Skeleton: layout per §18, `config.yaml` + pydantic loader, `Clock` and RNG interfaces, Makefile, empty test suite runs | `make test` passes |
| M1 | Backend probes: `docs/backends.md` with observed shapes for mlx-serve, Kokoro, LiteLLM, searxng | File exists with real examples |
| M2 | Fakes: `fake_minimax`, `fake_kokoro`, `fake_llm` (§15), including failure injection | Fakes unit-tested |
| M3 | `store.py`: inventory metadata + airplay ledger | Tests for insert, query by type, spacing lookups, expiry |
| M4 | Audio QC + normalize (§8) | Known-bad fixtures rejected, known-good pass, output is −16 LUFS ±0.5, FLAC |
| M5 | Pipelines: songs, voice, news (§6), against fakes | Each pipeline produces a valid library item end to end with fakes |
| M6 | `producer.py`: inventory targets and host workers (§6.1) | Simulation shows inventory reaching and holding targets |
| M7 | `scheduler.py` + `RandomSelector` (§5) | 24-h simulation passes every §15 assertion |
| M8 | `server.py` endpoints (§11) | API tests pass against a seeded temp library |
| M9 | Web client (§9) | Playwright gap test passes; UI is PLAY/STOP + indicator only |
| M10 | `seed.py` + real-backend smoke tests | Seed minimums met on real hardware; one 10 s real song generated |
| M11 | Real listening run | 60 min, no gaps, per §16 |

---

## 5. Code conventions

- Type hints everywhere. Pydantic models at every boundary (config, LLM
  output, HTTP).
- Every external call: explicit timeout, bounded retries with backoff, and a
  logged failure reason. A failing backend degrades inventory; it never
  crashes the station.
- LLM outputs are **untrusted**. Parse JSON strictly, validate against the
  schema, and discard on failure. Strip code fences before parsing. Never
  `eval` or execute anything from model output or web content.
- Structured logging (JSON lines) with `item_id`, `stage`, and `duration_ms`
  where relevant.
- Keep functions small and pure where possible. The selector must be a pure
  function of `(state, rng)`.
- Prompt templates live in `prompts/*.md`, not inline strings, so they can be
  tuned without code changes.

---

## 6. Commands

The Makefile should provide these (create them in M0):

```
make test      # unit tests (fakes only)
make sim       # 24-hour simulated run with assertions (§15)
make e2e       # Playwright client gap test (fakes, short items)
make lint      # ruff + mypy
make seed      # seed.py against real backends
make smoke     # real-backend smoke: /health on all hosts, one Kokoro clip,
               # one LLM brief, one 10 s song (only when asked)
make run       # start the station (public + admin listeners from config)
make deploy-check # render both deploy templates and nginx -t them
make e2e-split    # Playwright gap test through the local edge stack
make split-smoke  # origin + gateway + edge PASS/FAIL checks (real public app)
make pay-smoke    # Stripe test-mode smoke; refuses a live key (only when asked)
```

---

## 7. Before you say "done"

Run, in order, and include the results in your report:

1. `make lint`
2. `make test`
3. `make sim` (from M7 onward)
4. `make e2e` (from M9 onward)
5. For split/deploy changes: `make deploy-check`, `make split-smoke`, and
   `make e2e-split`.
6. For payment changes: everything above that the step lists (PAYMENT.md),
   plus `make pay-smoke` output when Step 11 or later is touched.

**Never report a task as complete without having run these.** If something
fails and you can't fix it, say so plainly and show the failure. Do not skip,
weaken, or delete a test to make it pass; if a test is wrong, explain why and
fix the test in a separate, clearly described change.

---

## 8. Reporting

End every task with:

- **What changed** (files, one line each).
- **RADIO.md sections** implemented or touched.
- **Check results** (§7), with pass/fail counts.
- **Deviations** from RADIO.md or this file, and why.
- **Open questions** needing a human decision.

Keep it short. No restating the task.

---

## 9. When to stop and ask

Stop and ask instead of guessing when:

- A backend behaves differently from `docs/backends.md` or RADIO.md.
- A task would require breaking a hard rule (§1).
- A design choice isn't covered by RADIO.md and would be hard to reverse
  (schemas, API shape, storage layout).
- Voice role assignment (§10). This is a human listening decision.
- Stripe behaves differently from PAYMENT.md or `docs/backends.md`, or a
  payment step needs a key permission, Dashboard setting or policy text that
  is not in place (PAYMENT.md Step 0, §14).

For small, reversible choices, decide, note it under **Deviations**, and
continue.

---

## 10. Known pitfalls

- **MP3 adds encoder padding** at the start/end and breaks gapless joins.
  Deliver FLAC.
- **AudioContext must be created inside the PLAY click handler**, or browsers
  block audio.
- **Decoded audio is large** (~70 MB per 3-min 48 kHz stereo song as float32).
  Keep only current + next 2 decoded; release after playback.
- **`loudnorm` must be two-pass** for accurate targets; single-pass is
  noticeably off on short clips.
- **Kokoro reads everything literally**: markdown asterisks, emoji, "(laughs)",
  URLs. The TTS cleanup step (§6.3) is mandatory, not optional.
- **Song generation time may not scale linearly** with duration. Record actual
  generation time per song; don't extrapolate from 10 s clips.
- **Web search results can contain prompt-injection text.** The news pipeline
  gets a search tool and nothing else: no file, shell, or network access
  beyond searxng.
- **Satire next to serious news.** Respect the `gravity` adjacency rule in §5.2.
- **Don't let the committed window include unrendered items.** A contextual
  item that isn't ready gets replaced by a liner, never waited on.
