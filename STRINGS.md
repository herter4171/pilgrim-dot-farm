# STRINGS.md — Starting/Stopping the Server & How Prompting Works

*Pilgrim Dot Farm Radio. Per AGENTS.md §4, this server is `make run` = uvicorn
on port **5000**. Verified against the running instance on 2026-09-27.*

Two things are documented here, as requested: **(1)** how the station server is
started and stopped, and **(2)** how LLM prompting is wired end-to-end.

---

## 1. Starting & stopping the server

### How it runs

The station is a **single uvicorn process** on `0.0.0.0:5000` that serves both:

- the **FastAPI backend** (`/api/*`: health, program, heartbeat, media, voices), and
- the **web client** (`/`, mounted last from `pilgrim/web/`).

There is **no separate web server**. `pilgrim/server.py`'s `create_app()`
(`--factory`) both mounts the API routes and statically serves the client.

**Start (dev / operator):**

```sh
cd /home/ec2-user/radio
make run
```

`make run` expands to (see `Makefile`):

```sh
PATH="$HOME/bin:$PATH" \
LITELLM_TOKEN=$(grep LITELLM_TOKEN .env | cut -d= -f2) \
.venv/bin/uvicorn pilgrim.server:create_app --factory --host 0.0.0.0 --port 5000
```

Key points:

- `--factory` makes uvicorn call `pilgrim.server:create_app()` → `Station` +
  the app. `LITELLM_TOKEN` is read **from the environment** (not the repo); it
  must be exported or passed inline. Port/host come from the CLI here, matching
  `pilgrim/config.yaml` (`station.port: 5000`, `host: 0.0.0.0`).
- The app's `@app.on_event("startup")` fires `_post_start()` which calls
  `station.startup()` — that spawns the **scheduler**, **producer** (voice
  refills), **news loop**, and **song loop** as background asyncio tasks. The
  station goes on air immediately and stays on air regardless of listeners.

**Stop:**

```sh
# foreground (Ctrl+C in the make run terminal)
ctrl-C

# or, if started in the background:
pkill -f "uvicorn pilgrim.server"        # SIGTERM -> graceful shutdown
```

There is **no `make stop` target** and **no systemd unit for the radio
server**. (An existing `/etc/systemd/system/pilgrim-web.service` points at a
*different, old Flask app* under `/home/ec2-user/site/server.py` and is
`disabled` / `inactive` — it is **not** this server. Don't confuse the two.)

### In this session

The server is currently running under a background monitor, so it stays up
while other work proceeds. Start it the same way (`make run`); stop it by
signalling the uvicorn process.

### The "always on air" model (no-op controls)

`POST /api/station/start` and `POST /api/station/stop` exist but are **no-ops
in Phase 1**: they return `"station is always on air"`. The real on/off is the
uvicorn lifecycle above. The web client's **PLAY / ⏸ STOP button** is
**client-side only** (`pilgrim/web/player.js`): PLAY creates an
`AudioContext` and starts `decodeAudioData` + `AudioBufferSourceNode.start()`,
STOP closes it. Neither button talks to `/api/station/stop`. Don't mistake the
UI button for server control.

### Port note (5000)

`:5000` is the radio server now. The old Flask chat app also used `:5000` but
is disabled, so there's no conflict today. If you ever see a health probe hit a
Flask app, check that `pilgrim-web.service` is still disabled.

---

## 2. How prompting works

### Where prompts live

Prompt **templates** are Markdown files, not inline strings (AGENTS §5 /
RADIO — "so they can be tuned without code changes"):

```
pilgrim/prompts/
  voice.md        # one template, parameterized by Role + Target + Context
  song_brief.md   # song brief -> MiniMax
  news.md         # news bulletin
```

`server.py:_load_prompts(cfg)` reads all three into `{"voice", "song_brief",
"news"}` and hands the dict to every pipeline (`Station.__init__`). Prompts are
resolved from `cfg.library.prompts_dir` (`prompts`).

The **one client that talks to the LLM** is `pilgrim/pipelines/llm.py` (`LLM`).
It calls LiteLLM at `cfg.hosts.litellm` (`http://localhost:4000/v1`) →
`POST /chat/completions` with:

```json
{
  "model": "<cfg.models.*>",
  "messages": [
    {"role": "system", "content": "<the prompt template>"},
    {"role": "user",   "content": "<task-specific instructions + JSON schema>"}
  ],
  "max_tokens": 4096,
  "temperature": 0.8
}
```

The **system prompt is the template**; the **user message is the per-task
context + an explicit "Return JSON only: {...}" schema**. Output is parsed
**strictly** by `parse_json_strict()`: code fences are stripped, first balanced
`{...}` is used as a fallback, and anything non-dict is discarded. LLM output
is **untrusted** — every pipeline re-validates and raises on bad shape. A
failing backend degrades inventory; it never stalls the station.

### Roles drive which model + what schema

Model per role is in `pilgrim/config.yaml` (`models.*`) and `VoicePipeline`.

| Role        | Model (`cfg.models`) | Voice (`cfg.voices`) | JSON schema extras |
|-------------|----------------------|----------------------|--------------------|
| `liner`     | `liners` → Ornith-1.5-9B | `dj` am_liam | `{"text","est_duration_s","evergreen"}` |
| `commercial`| `commercials` → Ornith-1.5-9B | `commercials` af_aoede | `{"text","est_duration_s","evergreen"}` |
| `dj_talk`   | `dj_talk` → qwen38 | `dj` am_liam | `{"text","est_duration_s","evergreen"}` |
| `news`      | `news` → qwen38 | `news` am_michael | `{"text","est_duration_s","gravity":"serious"\|"normal"}` |
| song brief  | `briefs` → qwen38 | — | `{"title","artist","genre","style_prompt","lyrics","target_duration_s"}` |

### The three pipelines

**1. Voice (`pilgrim/pipelines/voice.py`)** — all spoken items (liner,
commercial, dj_talk, news):

1. `write_copy(role, target_s, context?)` — uses the **shared `voice.md`**
   template, prepends `Role: <role desc>. Target Ns.`, appends the role's JSON
   schema, and (for news/DJ) injects context. `_model_for(role)` picks the
   model. Returns `{text, est_duration_s, ...}`.
2. `tts_cleanup(text)` — **mandatory** (Kokoro reads everything literally):
   strips emoji, markdown, `(stage directions)`, URLs (raises if a URL is
   present), converts `H:MM` to spoken time and small integers to words.
3. `render(...)` — `KokoroClient.synth(text, voice, speed)` from `cfg.voices`,
   QC (`grade_audio`), then `normalize` → FLAC (16-bit, −16 LUFS).
4. `produce_item(...)` → a stored library item with `type`, `media_path`,
   `duration_s`, `role`, `evergreen`, `gravity`, and the raw copy in `meta.text`.

**2. Songs (`pilgrim/pipelines/songs.py`)** — `song_brief.md` → MiniMax:

- `brief(previous_genres)` builds the user message from the template + the
  genre list + **recently-aired genres to avoid** (`cfg.playout.genre_no_repeat`)
  + `[60, 90]s` target. Returns `{title, artist, genre, style_prompt, lyrics,
  target_duration_s}`.
- `generate(brief)` → **synchronous** `POST {mlx_serve}/v1/audio/music-generations`
  (returns raw WAV inline; compute-expensive, never called off-worker).
- `produce_song(brief)` → QC → normalize → FLAC, tagged `evergreen`.

**3. News (`pilgrim/pipelines/news.py`)** — `news.md` + search:

- If `cfg.news.enabled`, it gets search snippets through the **real searxng
  tool on the LiteLLM MCP server** (`cfg.hosts.searxng` →
  `http://localhost:4000/mcp` — *not* a direct HTTP `/search`; see
  docs/backends.md). Failure degrades to a model-written bulletin.
- `produce_bulletin()` returns `{type:"news", text, headlines, gravity}`;
  `gravity` feeds the §5.2 adjacency rule (no satire next to serious news).
- News is **never recycled** (`evergreen: False`, expires per `news.refresh_s`);
  verified in `producer.ensure_news`.

### Who drives prompting (the producer loops, `pilgrim/producer.py`)

Production is **demand-driven** by low-water inventory targets from
`cfg.inventory`. Started once by `server.startup()`, these background loops call
the pipelines above and store items:

- `run()` every `6s` → `ensure_commercials`, `ensure_liners`, `ensure_dj`
  (liners refill per duration bucket `[3,5,10,15,30]s`, x `liners_per_bucket`).
- `news_loop()` every `5s` → `ensure_news` (render a fresh bulletin as audio).
- `song_loop()` every `20s` quiet / `2s` active → keep `fresh_songs_ready`
  songs in stock (slow: ~1.6s compute per 1s audio; runs as its own task so
  voice inventory never stalls — AGENTS §1 hard rule).

The **scheduler** (`pilgrim/scheduler.py`) only commits **already-rendered,
QC-passed, normalized items**; playout never waits on production and never
blocks on an LLM/Kokoro/MiniMax call (AGENTS §1 hard rules 1, 6).

### Tuning

- Edit a prompt in `pilgrim/prompts/*.md`, restart the server (`make run`).
- Model/voice/speed/targets all live in `pilgrim/config.yaml`, not in code.
- Prompt text stays TTS-safe: templates already forbid markdown, emoji, URLs,
  parentheticals; `tts_cleanup` enforces it.

---

*Cross-refs: `Makefile` (run target), `pilgrim/server.py` (`create_app`,
`Station`, `_load_prompts`), `pilgrim/pipelines/llm.py`, `voice.py`,
`songs.py`, `news.py`, `pilgrim/producer.py`, `pilgrim/config.yaml`,
`pilgrim/prompts/*`, `pilgrim/web/player.js`, `docs/backends.md`.*
