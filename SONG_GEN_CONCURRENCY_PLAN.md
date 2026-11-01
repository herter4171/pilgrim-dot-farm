# SONG_GEN_CONCURRENCY_PLAN.md — concurrent song generation (rev 2)

MiniMax Music 3 now has **three deployments** registered in the LiteLLM proxy
under one model group. Today the station generates songs one at a time: a
single `Producer.song_loop` worker blocks inline on
`brief() → produce_song() → QC → intro → store`, so at most one render is ever
in flight and the generator sits idle while a brief is written. This plan
lets the station run up to N renders at once (N = healthy backend slots)
without breaking request queue order, the inventory targets, or the playout
invariants.

Work it top to bottom. Step 0 is a gate: **its outcome picks the transport**,
and Steps 1–3 ship as one change (config schema + RADIO.md, hard rule 8).

## Status: implemented 2026-11-01

- **Transport:** LiteLLM mode via the vLLM pass-through (the only one this
  host can verify): `songs.gen_backends: [{name: litellm, slots: 3, auth:
  litellm}]`. The URL is derived from `hosts.litellm` (minus `/v1`, plus
  `/vllm`), so the M5's LAN-address config just works. Submissions are
  staggered 3 s (`songs.submit_stagger_s`) because least-busy only fans out
  staggered calls. Direct mode is a config-only switch later.
- **Request rule (operator decision, supersedes §2.2):** up to
  `songs.free_request_slots` (2) free requests render at once; paid requests
  take any free slot, several at once. Free requests and stock together never
  occupy every slot (paid jobs don't count), so one stays open for a payer.
  The decider scans the whole queue, not just its head (live incident
  2026-11-01: paid head blocked, two slots idle). A finished request is held outside the library (`Producer._wait_turn`)
  until every in-flight request ahead of it in queue order has ended, so ready
  order (= air order) stays queue order. A paid request is ahead of any free
  one.
- **"Fastest backend for requests"** only takes effect in direct mode. Behind
  one LiteLLM entry the router picks the box.
- **Concurrent sim (Step 4):** covered by `tests/test_song_concurrency.py`,
  which drives the real `song_loop` against the store with gated fake renders,
  instead of a separate multi-hour sim. `make sim` is unchanged and passes.
- **Not run:** `make smoke SMOKE_ARGS=--gen` (3 × 10 s real renders through
  the pool). Run it on the M5 when wanted.

---

## Rev 2: what changed, and why

Rev 1 was reviewed against the code and probed against the live proxy
(read-only, 2026-11-01). It had these flaws:

| # | Rev 1 assumption | What's actually true | Fix in rev 2 |
|---|---|---|---|
| F1 | POST `localhost:4000/v1/audio/music-generations` works through LiteLLM | LiteLLM **1.104.2** has no such route (`/openapi.json` lists only `/v1/audio/speech` and `/transcriptions`), `/config/pass_through_endpoint` returns `[]`, and the three deployments are registered as `vllm/…` chat models | **Update 2026-11-01:** the generic pass-through `POST localhost:4000/vllm/v1/audio/music-generations` (Bearer `LITELLM_TOKEN`, body incl. `"model": "MiniMax-Music-3"`) **works**: 10 s instrumental → HTTP 200, raw WAV inline (pcm_s16le, 44.1 kHz stereo, 9.98 s, peak −4.4 dBFS), 23 s wall. Which box served it, and whether `/vllm/` load-balances across all three, is still unverified (Step 0.4). The design doesn't depend on it either way (§2.1 backend pool) |
| F2 | "3 concurrency slots" means a server-side limit that returns 429/503 when exceeded | It means **3 separate machines** behind `least-busy` routing with no `max_parallel_requests`: `192.168.68.88:8080` and `.56:8080` (`vllm/MiniMax-Music-3`) plus `.51:11234` (today's `MiniMax-Music3-MLX-Serve-8bit`). A 4th call **doubles up on a busy box**. Nothing returns a 429 | The ceiling is enforced **client-side only**. The `BackendBusy`/429 retry design is dropped |
| F3 | Same model everywhere | Different builds (vLLM vs MLX 8-bit), so a seed is only reproducible on the box that made it. Prompt budget, duration cap (MLX's 4096-token cap) and RTF may differ | Probe each backend separately and record which backend made each song in its `meta` |
| F4 | LiteLLM is a transparent pipe | Router has `num_retries: 2`, `timeout: 6000`, `cooldown_time: 5`. A render that fails after 400 s is silently retried up to 2× more, and a station-side timeout leaves the backend still rendering (orphaned slot) | The pool talks to the backends directly (§2.1), or, if LiteLLM is kept, the route must set `num_retries: 0` (Step 0) |
| F5 | `hosts.music_gen: http://localhost:4000/v1` + `"/v1/audio/music-generations"` | That builds `…/v1/v1/…` | URLs in config are bare bases; the path is added once in code |
| F6 | Request spawn checks only "no request in flight" | It never checked the slot count: 3 stock + 1 request = 4 in flight. The backstop semaphore would then queue **the request behind stock**, which inverts its priority | Slot accounting covers every job. Stock never takes the last free slot (§2.3) |
| F7 | Brief written inline by the decider, then render spawned | A new request waits behind a stock brief (up to 170 s), and the decider can't reap or spawn while it waits. `_run_request` "minus the brief" also drops `mark_request_producing` and the attempt accounting for brief failures | Each job is a task that runs brief → render → intro → store. The request path stays today's `_produce_request_song` almost verbatim |
| F8 | "Genre context stays deterministic" | `_recent_genres()` reads the store, and in-flight renders aren't there yet. Two parallel stock songs can share a family, breaking §5.2 `genre_no_repeat` at the source | In-flight genres are appended to `prev_genres`, and stock briefs are serialized under a lock (§2.4) |
| F9 | Requests are FIFO, one at a time | Queue order is `priority DESC, COALESCE(paid_at, created_at)`, so paid goes first. One request at a time makes a **paid** request wait out a whole free render (3–7 min) | At most one in-flight request **per tier** (§2.2). FIFO within a tier still holds |
| F10 | "1 request + 2 stock = 3 is the common case" | `stock_gap_s` (600 s, from last stock *finish* to next *start*) keeps steady-state stock at ≤ 1 in flight. All 3 slots fill only on cold start, after an outage, or when 2+ requests overlap | The slot table (§2.3) is corrected. Running more stock is a config decision, not a code change |
| F11 | (not covered) | `normalize.normalize()` calls `subprocess.run` (two-pass loudnorm, up to 600–900 s timeouts) and `sf.read`/`grade_audio` run **synchronously on the event loop** that the scheduler and HTTP server share. Concurrency means more of these stalls | Step 2 moves the QC/normalize tail into `asyncio.to_thread` (hard rule 1: playout never waits on production) |
| F12 | (not covered) | `dst = song_{int(time.time()*1000)}.flac` can collide when two renders finish together | Add a random suffix |
| F13 | Tests: "add a delay to FakeSong" | The 24 h sim and about 23 producer tests drive `song_step()` under `asyncio.run(...)`. Tasks spawned in there are cancelled when the loop closes | `song_step()` stays a run-to-completion unit built from the same parts as the concurrent loop (§3 Step 3) |
| F14 | Rename covers config + songs.py | `server.py:165` `/health` also probes `hosts.mlx_serve` | The health check now probes each pool backend |
| F15 | No model id in the payload | Hard rule 4: model names come from config | `models.music` is used if the LiteLLM transport is chosen |
| F16 | "M3" | Ambiguous: it could mean milestone M3, a Mac, or MiniMax Music 3 | This plan says "generator" or names the backend |
| F17 | `_last_stock_song_at` set "in the task" and also "on reap" | The two are inconsistent | It is set inside the stock job when the song is stored, as today |

---

## 0. Current state (verified in code, 2026-11-01)

- **One worker, serial.** `Producer.song_loop()` (`pilgrim/producer.py:462`)
  awaits `song_step()` (`:487`): the next queued request (queue order), else
  one stock song. Step = `songs.brief()` (qwen38 @ `.75:8081`, 10–170 s) +
  `songs.produce_song()` (208–427 s for 130–180 s of audio) + sync QC/normalize
  + intro TTS.
- **Transport.** `SongPipeline.generate()` (`pilgrim/pipelines/songs.py:373`):
  one `httpx` POST to `hosts.mlx_serve` + `/v1/audio/music-generations`, no
  auth, 1800 s timeout, raw WAV inline (`docs/backends.md` §2).
- **Gates** (`song_step`, §6.1): requests skip all gates; stock needs
  `fresh < inventory.fresh_songs_ready` (2), library < `songs.stock_max_library`
  (100), and `songs.stock_gap_s` (600 s) since the last stock song finished.
- **Request lifecycle.** `mark_request_producing` happens *before* the brief;
  a brief or render failure → `request_failed_attempt` (3, or
  `payments.max_attempts` for paid); `httpx.TransportError` → `requeue_request`
  (no attempt burned); startup `reset_producing_to_queued()`.
- **Splice.** `Scheduler.place_request_next` splices a `ready` request at the
  next song slot, so ready order = air order. That is why two requests of
  the same tier must not render in parallel.
- **Unused key.** `inventory.briefs_queued: 3` is read by no code.
- **Proxy topology** (probed read-only via `/model/info`, `/router/settings`):
  see F2/F4. The generator hosts do not share GPUs with the station's
  `qwen38` (`.75`) or Ornith (`.89`). `.56` also serves DeepSeek/Nemotron, and
  `.56:8080` is the same host:port as `Qwen3-Omni-30B`, which suggests a
  model-swapping server (cold-load latency, eviction). Step 0 must check.

## 1. Target

- At most **C** renders in flight, where C = sum of `slots` over *healthy*
  backends (3 today). Never more: going over doubles up on a box and halves
  both renders.
- Listener requests: queue order kept (paid before free, FIFO within a
  tier). They are produced ahead of stock and **never wait on a stock render
  or a stock brief**. Splice path untouched.
- Requests render on the **fastest idle backend**; stock prefers the slower
  ones.
- Stock may fill spare slots but never the last free one (§2.3).
- All hosts, slots and model ids come from config (hard rule 4); tests use
  fakes (hard rule 2); real generation only in smoke, ≤ 10 s (hard rule 6).

## 2. Design

### 2.1 Backend pool (transport-agnostic)

`SongPipeline` owns a `MusicPool` of backends from config:

```yaml
songs:
  backends:                    # one entry per generator; slots = concurrent renders it takes
    - {name: m5-mlx,  url: http://192.168.68.51:11234, slots: 1}
    - {name: gpu-88,  url: http://192.168.68.88:8080,  slots: 1}
    - {name: gpu-56,  url: http://192.168.68.56:8080,  slots: 1}
```

- **Direct mode (recommended):** one entry per box, as above. The station
  knows which box rendered each song (seed reproducibility, per-box RTF),
  health-checks each box, drops a dead box from C, and avoids LiteLLM's
  hidden retries (F4).
- **LiteLLM mode:** used only if the operator adds a music route
  (pass-through or custom) that Step 0 verifies, with `num_retries: 0` on
  it. Config is a single entry `{name: litellm, url: <proxy base>, slots: 3,
  auth: litellm}` and the payload adds `model: <models.music>`. Same code
  path; it loses per-box identity unless the response carries
  `x-litellm-model-api-base`, which Step 0 checks.
- `lease(prefer="fast"|"slow")` → an idle backend (async context manager).
  It picks by EWMA real-time factor (audio_s / wall_s), seeded from Step 0
  numbers and updated per render.
- Health: on `httpx.TransportError` or a non-200 the backend is marked down
  for `songs.backend_cooldown_s` (default 120 s, doubling to 600 s). While it
  is down it doesn't count toward C. `/health` reports each backend.
- Each song's `meta` records `backend` (name) next to `seed`.

### 2.2 Jobs and the decider

```
song_loop (decider; the only code that picks work):
  loop:
    reap finished jobs (always .result(); log; outage backoff on transport errors)
    while (job := self._next_job()) is not None:     # pure gate logic, no awaits
        spawn task(self._run_job(job)); track it
    if jobs in flight: await FIRST_COMPLETED or a 20 s tick (new requests)
    else: tick 20 s

_next_job()  (sync; reads store + in-flight table):
    C = pool.capacity()
    1. next queued request whose tier (paid|free) has no job in flight,
       and total jobs < C                     → Job(kind=request, req)
    2. stock gates pass and total jobs < C - 1 (or == 0 when C == 1)
                                              → Job(kind=stock)
    3. else None

_run_job(job):   brief → lease → generate → (thread) QC/normalize → release → intro → store
```

- **Brief inside the job** (F7). A request never waits for a stock brief, and
  the decider never blocks. The request job keeps today's
  `_produce_request_song` order: `mark_request_producing` → brief → render →
  intro → store + `mark_request_ready` with no await in between. Failure
  accounting is unchanged.
- **One in-flight request per tier** (F9). The rev 1 argument still holds
  within a tier: B's brief starts only after A is `ready`, so B can't splice
  ahead of A. Paid vs free in parallel is fine because paid outranks free in
  queue order, so a paid song landing first is correct.
- **Backend lease only around the POST.** A job counts against C from spawn,
  but holds a box only while generating. Because jobs ≤ C, a lease never
  waits in normal operation. If a box goes down mid-flight, the lease waits
  (bounded by the job's timeout) rather than doubling up.
- **No cancellation.** A request never cancels a stock render. Backends don't
  abort on disconnect, so cancelling wastes the compute and orphans the box.

### 2.3 Slot arithmetic (C = 3, `fresh_songs_ready = 2`, `stock_gap_s = 600`)

| Situation | In flight | Notes |
|---|---|---|
| Cold start (fresh = 0, no gap yet) | 2 stock | `fresh + stock_in_flight < 2` and `jobs < C-1`; slot 3 held for a request |
| Request arrives during cold start | 2 stock + 1 request | request takes the reserved slot at once |
| Steady state (fresh = 2) | 0 | target met |
| One song airs (fresh = 1) | ≤ 1 stock | gap gate spaces stock ~1 / 10 min |
| Request rendering, stock due | 1 request + 1 stock | typical saturation is 2/3 (F10) |
| Paid arrives while a free request renders | free + paid (+ ≤ 1 stock) | paid no longer waits 3–7 min |
| 2 stock + 1 free in flight, paid arrives | waits for the first finish | worst case is the shortest *remaining* render, not a full one |
| A box down | C = 2: 1 request + 0–1 stock | stock gives way first |

If the operator wants stock alone to fill more slots, they raise
`inventory.fresh_songs_ready` and/or lower `songs.stock_gap_s`. That is
config, not code. The reserved slot costs nothing at today's targets
(stock ≤ 2 anyway).

### 2.4 Genre context with parallel stock (F8)

- Stock briefs run under `self._stock_brief_lock` (one stock brief at a time;
  request briefs bypass it). qwen38 sees at most 2 concurrent song briefs
  (1 stock + 1 request) plus the voice/news traffic it already gets.
- `prev_genres = self._recent_genres() + [genres of in-flight jobs whose
  brief is done]`, trimmed to `genre_no_repeat`. A finished stock brief
  records its genre in the in-flight table before releasing the lock.
- Request briefs get the same augmented list (a pinned listener genre still
  wins, as today).

## 3. Steps

### Step 0: probe (no code; AGENTS.md §2 / M1 discipline). **Gate.**

All generation calls are **≤ 10 s of audio** (hard rule 6). Run from the
station host (M5), which is on the generator LAN. The EC2 box can't reach
`192.168.68.x`.

1. **Each box directly:** `GET /v1/models` and `GET /health` on `.88:8080`,
   `.56:8080`, `.51:11234`. Then one 10 s POST to
   `/v1/audio/music-generations` with the existing payload (`prompt`,
   `lyrics`, `duration_seconds: 10`, `seed`). Record: same shape as
   `docs/backends.md` §2 or not, auth needed, sample rate/channels, wall time,
   and the prompt-budget / max-duration fields `GET /v1/models` reports
   (the MLX box reports these today, per §2 "Prompt budget").
2. **`.56:8080` swap check:** is it a model-swapping server shared with
   Qwen3-Omni? Time a cold 10 s call against a warm one. If cold-load adds
   > 60 s or evicts something the operator relies on, **stop and ask**.
3. **Parallel scaling:** fire three 10 s calls at once, one per box. All 200,
   and the walls should be about equal to step 1's (separate machines, so no
   slowdown). Separately fire 2 at **one** box and record the behavior
   (queued? slower? error?). This decides whether `slots: 1` is right.
4. **LiteLLM route** (only if the operator wants LiteLLM mode): confirm a
   music route exists (it doesn't on 1.104.2 today: F1), that it
   load-balances across the three, and that it exposes the deployment in a
   response header. Confirm `num_retries` is 0 for that route.
5. QC each box's clip with `grade_audio` + normalize. Listen to one clip from
   each vLLM box against the MLX one (quality parity is a human call).
6. Record everything in `docs/backends.md` as "§2b Song generation — backend
   pool (MiniMax Music 3 ×3)".

**Stop and ask** (AGENTS.md §9) if any box needs a different payload, returns
submit+poll, fails parity, or if the human prefers LiteLLM mode while the
proxy has no route. The rest of the plan assumes the observed shape; a
submit+poll box would hold its lease across submit + bounded poll.

### Step 1: config schema (hard rule 8: RADIO.md in the same change)

- `pilgrim/config.py`:
  - `class MusicBackend(BaseModel)`: `name: str`, `url: str`, `slots: int = 1`
    (validator ≥ 1), `auth: Literal["none", "litellm"] = "none"`,
    `rtf_hint: float | None = None` (from Step 0).
  - `Songs.backends: list[MusicBackend]` (validator: non-empty, unique names),
    `Songs.backend_cooldown_s: float = 120`.
  - Remove `Hosts.mlx_serve`. Add `Models.music: str | None` (LiteLLM mode only).
- `pilgrim/config.yaml`: the `songs.backends` block (§2.1) replaces
  `hosts.mlx_serve`.
- `RADIO.md`, same commit:
  - **§6.2** step 2: "MiniMax Music 3 on the backend pool (`songs.backends`);
    up to C = Σ healthy slots renders run concurrently; requests take the
    fastest idle backend; one in-flight request per tier."
  - **§6.1** song worker: replace "waits … before starting the next one" with
    the job/decider model and the reserved-slot rule. The target row becomes
    "≥ 2, counting stock renders in flight".
  - **§12** config block: `songs.backends`, `songs.backend_cooldown_s`,
    `models.music`; drop `hosts.mlx_serve`.
- `AGENTS.md` §2 env table: "Station server + song generation | M5 |
  mlx-serve…" becomes "Song generation | backend pool (`songs.backends`):
  M5 MLX + two GPU hosts", and §1 rule 6 wording ("~1.6 s per 1 s" was the MLX
  box) is updated with Step 0 RTFs.
- `docs/backends.md` §0 access table: one row per backend.
- No HTTP API (§11) or DB schema change. `/health` JSON gains per-backend keys
  (`mlx` becomes `music: {name: bool}`). The TUI/admin reads it, so check
  `pilgrim/tui` and update any consumer in the same change.

### Step 2: `pilgrim/pipelines/songs.py`

- `MusicPool` (small class in songs.py): `capacity()`, `lease(prefer)`,
  `mark_down(name)`, `record(name, audio_s, wall_s)`. Time in the pool uses an
  injected clock (tests), not `time.time()`.
- `generate(brief, backend)`: URL = `backend.url.rstrip("/") +
  "/v1/audio/music-generations"` (F5). Add a Bearer header only for
  `auth: litellm` (key from the existing `api_key` plumbing, `LITELLM_TOKEN`).
  Add `model` only in LiteLLM mode. Timeout stays 1800 s.
- `produce_song(brief, prefer="slow")`: lease → generate → release, then
  `await asyncio.to_thread(self._qc_and_normalize, …)` for `sf.read`,
  `grade_audio` and `normalize.normalize` (F11). `dst` gets a
  `secrets.token_hex(4)` suffix (F12). `meta["backend"] = name`.
- `generation_s` bookkeeping: append the completed `(duration, wall)` tuple
  once, after the thread returns. The rev 0 "append then patch `[-1]`" pattern
  breaks under concurrency once an await sits between the two.
- `seed.py`: passes through unchanged (it still calls `produce_song` serially;
  parallel reseed is an open question, §6).

### Step 3: `pilgrim/producer.py`

Refactor so the serial and concurrent paths share all logic:

- `self._jobs: dict[asyncio.Task, Job]`, where `Job` is a small dataclass:
  `kind`, `tier`, `request_id`, `genre: str | None`, `started_at`.
- `_next_job() -> Job | None`: sync gate logic per §2.2. It reuses
  `stock_capped()`, and the fresh count becomes `count_fresh_of_type("song") +
  stock jobs in flight`, so in-flight stock counts toward the target. The gap
  gate and the cap log stay the same.
- `_run_job(job)`: dispatches to `_produce_request_song(req, job)` (today's
  body, using the §2.4 augmented `prev_genres` and `prefer="fast"`) or
  `_produce_stock_song(job)` (today's `song_step` stock tail, under
  `_stock_brief_lock` for the brief only, `prefer="slow"`, and
  `_last_stock_song_at = clock.now()` when stored, per F17).
- `song_step()` **keeps its contract** (F13): `job = self._next_job()`, and
  if there is one, `await self._run_job(job)` inline, returning True. The sim
  and the existing producer tests keep working unchanged.
- `song_loop()`: the decider. Reap → spawn while `_next_job()` → wait
  `FIRST_COMPLETED` with a 20 s tick. Outage backoff (15 s doubling to 120 s)
  stops new spawns only when **every** backend is down. One box down just
  lowers C. A QC rejection is not an outage.
- Sleep/wait goes through an injectable `self._sleep` (default
  `asyncio.sleep`), so tests can drive the loop with the fake clock. Today's
  `asyncio.sleep(20.0)` already bends hard rule 3, and this fixes it rather
  than adding more.
- Logs: `song.job.spawned` (`kind`, `tier`, `request_id`, `jobs`, `capacity`),
  `song.job.reaped` (`wall_s`, `outcome`), `song.backend.down` / `.up`
  (`backend`), plus `backend` on `song.produced`. `request.ready` /
  `request.aired` stay as they are.

### Step 4: fakes and tests (hard rule 2)

- `FakeSong` / fake pool: per-backend `render_delay` driven by an
  `asyncio.Event` or a fake-clock await (not wall sleep), a concurrency probe
  (`peak_total`, `peak_per_backend`, timeline), and `fail_next(backend,
  kind="transport"|"qc")`.
- New tests (`tests/test_song_concurrency.py`):
  - `test_capacity_never_exceeded`: cold start, high target →
    `peak_total ≤ C` and `peak_per_backend ≤ slots`.
  - `test_stock_leaves_reserved_slot`: C = 3, stock only → max 2 stock jobs;
    a request queued mid-flight spawns immediately. With C = 1 the behavior
    equals today's serial path.
  - `test_stock_gate_counts_in_flight`: target 2 → exactly 2 stock spawn,
    none after they land.
  - `test_request_tier_serial`: two free requests → the second's brief starts
    after the first is `ready`. Ready order = queue order.
  - `test_paid_overlaps_free`: free rendering, paid queued → paid spawns at
    once (slot permitting) and ends `ready` in queue order.
  - `test_request_takes_fast_backend` / `test_stock_prefers_slow`.
  - `test_backend_down_reduces_capacity`: one box transport-fails → C drops,
    a request is requeued (no attempt burned), and the box returns after
    cooldown.
  - `test_parallel_stock_genres_distinct`: two concurrent stock briefs → the
    second's `previous_genres` includes the first's genre.
  - `test_request_brief_failure_counts_attempt`: brief raises →
    `request_failed_attempt`, as today.
  - `test_qc_runs_off_loop`: `produce_song` normalizes via `to_thread`
    (monkeypatch to assert the call).
- `test_integration_sim.py` is unchanged (it calls `song_step`). Add one
  short **concurrent** sim (2–3 sim hours, `song_loop` with the fake pool and
  injected sleep) that asserts overlapping render windows happened and that
  the existing request-order / intro-glue / genre-spacing assertions still
  hold.

### Step 5: smoke (`pilgrim/tests/smoke.py`, `make smoke`, only when asked)

- Per backend: `/health` + one 10 s generation + QC. Then all backends in
  parallel (one 10 s each). Log wall/RTF per backend into the smoke output so
  `docs/backends.md` can be updated. Don't extrapolate from 10 s clips
  (AGENTS.md §10); `song.produced` logs `backend` + `rtf` from real runs.

## 4. Verification (AGENTS.md §7, in order)

1. `make lint`
2. `make test`
3. `make sim`
4. `make e2e`
- No split/deploy/payment surface → `deploy-check` / `split-smoke` /
  `e2e-split` / `pay-smoke` not required. (Rule 11 holds: nothing here
  imports `payments.py`.)
- Deploy: config + code ship together. An old config without
  `songs.backends` fails loudly at the pydantic loader. That is intended.

## 5. What this does *not* change

- Scheduler/splice (§5.2): request songs still splice at the next song slot.
- Request API, moderation, copyright screen, payments, client.
- `stock_gap_s` semantics (finish → next start). Stock stays spaced in steady
  state; the extra slots buy request latency and catch-up, not a stock flood.
- `inventory.briefs_queued` stays unused. A stock-brief prefetch (write the
  next stock brief while all slots render, so a freed box never idles for a
  brief) is a possible follow-up once the pool is proven. It is not in this
  change.

## 6. Risks / open questions

- **Human decision: transport.** Direct pool (recommended: works today, has
  per-box identity and health, no hidden retries) or LiteLLM mode (needs an
  operator-added route with `num_retries: 0`). This affects the config
  schema, so it is the operator's call before Step 1.
- **Human decision: quality parity.** The vLLM boxes run a different build
  from the MLX box. If they sound worse, give them `slots: 0`, or let them
  take stock only (add `stock_only: true` to `MusicBackend`, a small
  follow-up).
- **Shared generators.** If other clients use the same boxes (the
  `MiniMax-Music-3` group is public on the proxy), a box can be busy without
  the station knowing. Renders get slower but stay correct. Per-box RTF EWMA
  makes requests drift to whichever box is actually fastest.
- **`.56:8080` model swapping** (Step 0.2) may add cold-load time or evict
  Qwen3-Omni/others on that box.
- **Restart:** `reset_producing_to_queued()` now may re-queue up to 2
  requests (one per tier). In-flight stock is lost. Nothing sweeps orphaned
  `media_dir/_song_tmp_*.wav` today, so add a startup sweep of those in
  Step 2 (they are temp files, not library items, so hard rule 5 doesn't
  apply). A backend may keep rendering an
  orphaned job after a restart. The first post-restart lease on that box will
  just be slower.
- **Open:** parallel `seed.py` reseed through the pool (safe once the pool
  exists; optional follow-up).
