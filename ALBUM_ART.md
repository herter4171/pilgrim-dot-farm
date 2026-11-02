# ALBUM_ART.md — Cover art for every song (+ Instagram reels)

Work plan for a coding agent. It gives every song a **1024×1024 cover image
with the song title and artist written on it**, made by the local FLUX 3
image model. The image is uploaded to S3, shown in the **RADIO BROADCAST**
pane above the indicator and PLAY/STOP row, and later used with the song's
S3 MP3 to build a vertical Instagram Reel with ffmpeg.

Read this whole file before Step 1. Then read, in order: `AGENTS.md`,
`RADIO.md` §6.2, §8.4, §9.1, §11, §12, §14, and `docs/backends.md` §2b and
§9. `RADIO.md` is still the source of truth. If this file and RADIO.md
disagree, stop and report the conflict.

Do the steps in order. Each step ends with the AGENTS.md §7 checks, a commit
citing the RADIO/ALBUM_ART sections, and the AGENTS.md §8 report.

### Where the work happens (two checkouts of the same repo)

| Host | Checkout | Role | What this plan does there |
|---|---|---|---|
| **Mac Studio (M5)**, local | `/Users/justinherter/prod/pilgrim-dot-farm` (this dir) | **Stateful origin**: `station.db`, `pilgrim/library/`, FLUX server, S3 uploads | Steps 1–7, 9–11: all code, tests, renders, DB migration, S3 publish |
| **EC2 edge**, `ssh pf` | `~/radio` (`/home/ec2-user/radio`) | **Stateless edge**: nginx serves `pilgrim/web/` from its own checkout and proxies `/api/*` to the origin | Step 8 deploy only: `git pull` so the new `index.html`/`player.js`/`style.css` go live |

- Write and commit code **only on the Mac**, then push. On `pf` you only
  `git pull` and run the edge checks. Never edit files on `pf` and never put
  state there (no DB, no art, no AWS keys: SPLIT_PLAN.md).
- Run the art worker, backfill, smoke and reel tools **on the Mac only**,
  because they need `127.0.0.1:8084`, `station.db` and the `mac-studio` AWS
  profile.
- From the Mac, run commands on the edge as `ssh pf 'cd ~/radio && <cmd>'`.

---

## 0. Read this first

### 0.1 Facts already observed (2026-11-01, on the M5)

These come from probing the server and reading its source
(`/Users/justinherter/local_llm/flux3/serve.py`). Re-verify them in Step 1;
code against what you observe.

- **Server:** "FLUX 3 Image Klein Server", FastAPI/uvicorn on port **8084**,
  same machine as the station (the M5). Base URL for us:
  `http://127.0.0.1:8084`. One model is resident: `6bit`
  (`VisualInference/FLUX-3-Image-Klein-MLX-6bit`), ~9 GB RSS, generation peak
  ~9.6 GB.
- **Endpoints:**
  - `GET /health` → `{"status":"ok","port":8084,"model":"6bit","loaded":true,"rss_mb":9053.7,"physical_memory_gb":68.7}`
  - `GET /v1/models` → OpenAI-style list; ids `flux3-6bit`, `8bit`, `6bit`.
    The `model` field in a generation request is **ignored**: whatever is
    loaded renders.
  - `POST /v1/images/generations` (JSON body, all optional except `prompt`):
    `prompt`, `size` (`"WxH"`, default `1024x1024`), `n` (1–4), `steps`
    (default 16), `guidance`, `seed`, `response_format` (`url` |
    `b64_json`), `images`/`boxes` (references; we don't use them).
  - `GET /download/{nbytes}` — **not under `/v1`**. `nbytes` is ignored; it
    always returns whatever the *latest* render wrote to `flux3_output.png`.
- **Response shape** (note the double nesting):
  ```json
  {"object": "list",
   "data": [ { "created": 1793591772,
               "data": [ {"b64_json": "<png base64>", "mime_type": "image/png"} ],
               "model": "flux3-6bit",
               "bfx_revised_prompt": "<prompt as sent>",
               "bfx_record": {"input_prompt": "...", "width": 1024, "height": 1024,
                              "steps": 16, "seed": 7, "upsampled": false,
                              "references": 0, "boxes": null, "prompt": "..."} } ] }
  ```
  The PNG is `body["data"][0]["data"][0]["b64_json"]`, ~1.6 MB, 1024×1024 RGB.
- **Timing:** 40.1 s and 39.9 s per 1024×1024 image at 16 steps, with
  `mlx-serve` (song generation) resident.
- **Errors:** memory guard → **HTTP 503**
  `{"detail":"server_busy_or_out_of_memory: ...","type":"memory_pressure"}`
  when < 15 GB is free, before loading or before each image.
- **Single-flight server.** Generation holds an asyncio lock and runs the
  model synchronously **on the event loop**, so during a render *every*
  request to the server, `/health` included, waits up to ~40 s. A second
  generation queues behind the first.

### 0.2 Rules that follow from those facts (non-negotiable)

1. **Always `response_format: "b64_json"`. Never use the returned `url` or
   `/download`.** That URL serves the most recent render. Any other caller
   between our render and our download gives us their image, which would
   put the wrong cover on a song.
2. **One image at a time, station-wide.** The art worker has exactly one
   in-flight request. No `n > 1`.
3. **Respect memory.** The M5 is also a song generator (mlx-serve). Before
   each render, read free memory the same way `serve.py` does (`vm_stat`:
   free + inactive + "occupied by compressor" pages × page size) and skip if it is below
   `art.min_free_gb`. A 503 `memory_pressure` is a normal back-off, not an
   error: wait `art.retry_after_s` and try again later. Never retry in a
   tight loop.
4. **Timeouts:** `/health` can take ~40 s while a render runs, so the health
   probe timeout is ≥ 60 s and a slow health check is not "down". The
   generation call timeout is `art.timeout_s` (default 180 s).
5. **Art never touches playout** (hard rule 1). A song with no art airs
   exactly as today. Nothing in `scheduler.py`, `selector.py` or
   `producer.py` imports the art code or waits on it.
6. **Real renders only in `make art-smoke` (when asked) and the backfill
   tool run by the operator.** Tests use `pilgrim/tests/fakes/fake_flux.py`
   (hard rule 2).

### 0.3 The "album" problem, and the prompt that fixed it

If you put "album cover", "album art", "record" or "LP" in the prompt, FLUX
draws a **vinyl record / circle** instead of square artwork. Two test renders
with the template below gave clean, full-bleed square art with **exactly
correct** title and artist text, including `Production? That's Just a
Label.` (punctuation and apostrophe intact). The only defect was gibberish
pseudo-text on props in the scene (box labels, a screen). So the scene must
not contain things that carry writing.

**Final FLUX prompt = fixed template (in code-owned `prompts/album_art_flux.md`)
+ one LLM-written scene.** The title and artist strings are inserted by
code, **never** by the LLM:

```
Square 1:1 graphic artwork that fills the entire frame edge to edge, flat and
full-bleed, no border, no frame, no vinyl record, no disc, no circle, no CD,
no mockup, no product photo. {scene} Large clear typography at the top reads
exactly "{title}". Smaller text at the bottom reads exactly "{artist}". No
other text, no signs, no labels, no logos.
```

Banned words in the scene and the template: `album`, `cover`, `record`,
`vinyl`, `LP`, `disc`, `CD`, `single`, `sleeve`, `cassette`. Enforce this in
code: reject the scene and ask the LLM again if any appear (word-boundary,
case-insensitive), the same way `songs.py` validates briefs.

Text sanitising before inserting into the template: strip emoji and control
characters, collapse whitespace, and replace `"` with `'`. For the art
text only, drop a trailing parenthetical such as `(Piano Version)` first.
Then cap title at 70 and artist at 48 characters, truncating at a word
boundary. The DB title is never changed. If the title or artist contains
any **non-Latin-script** character (Japanese, Tamil, …; accented Latin like
Spanish is fine), render that song **text-free**: replace the two
typography sentences with `No text, no letters, no typography.` If either field
is empty, skip the song (no art), because the label rule in RADIO §9.1 needs
both fields too.

---

## Step 1 — Probe and record the backend

Per AGENTS.md §2 ("Do not guess external APIs").

- Re-run the probes in §0.1: `/health`, `/v1/models`, one generation at
  `size: "256x256"`, `steps: 4`, with `b64_json` (cheap). Confirm the double
  nesting and the 503 body if you can trigger it safely (don't eat memory to
  do it; quoting the source is fine).
- Add **`docs/backends.md` §10 "Image generation — FLUX 3 Klein
  (`127.0.0.1:8084`)"** with the observed request/response shapes, timings,
  the `/download` race, the blocking `/health`, and the "album" finding.
- Add `hosts.flux: http://127.0.0.1:8084` to `config.yaml`, next to
  kokoro/litellm/sfx (there is no `endpoints:` block).

## Step 2 — Config, fake, client

- New `art:` config section (pydantic model in `config.py`; update RADIO §12
  in the same change, hard rule 8):
  ```yaml
  art:                        # ALBUM_ART.md; cover art per song
    enabled: true
    size: 1024x1024
    steps: 16                 # server default; 40 s per image observed
    timeout_s: 180
    health_timeout_s: 60      # /health blocks during a render
    min_free_gb: 20           # skip a render below this (server refuses < 15)
    retry_after_s: 300        # after a 503 / failure / low memory
    idle_poll_s: 30           # nothing to do -> look again
    scene_attempts: 3         # LLM retries for a valid scene
    key_prefix: art/          # S3 prefix (bucket/CloudFront from media:)
    jpeg_quality: 3           # ffmpeg -q:v (2..5 is good; ~200-300 KB)
  ```
- `pilgrim/pipelines/art.py`: `FluxClient` (httpx, explicit timeouts) with
  `health()` and `generate(prompt, seed) -> bytes`. It decodes
  `data[0].data[0].b64_json`, checks the PNG signature, and checks the
  dimensions (read the IHDR bytes; no Pillow). It returns a typed failure for
  503 `memory_pressure`, timeouts and malformed bodies. Pydantic model for
  the response.
- `pilgrim/tests/fakes/fake_flux.py`: returns a tiny valid PNG (any size you
  ask for) after a configurable delay. Failure injection: 503
  memory_pressure, timeout, wrong nesting, non-PNG bytes, wrong dimensions.
  Unit-test the fake and the client.

## Step 3 — Scene prompt (LLM)

- `pilgrim/prompts/album_art_scene.md`: given title, artist, genre, a
  **style hint**, and at most the first ~12 lines of lyrics (with section
  tags like `[verse]` stripped), return strict JSON:
  `{"scene": "<one paragraph, 30-70 words: subject, setting, palette, art style>"}`.
  Instructions: a single concrete visual idea, a named art style (risograph,
  woodcut, xerox collage, gouache, 70s airbrush, etc.), a palette. No
  people's names, no real brands or logos, no text, signs, screens, labels,
  newspapers, books or anything with writing on it. None of the banned
  words in §0.3. Pilgrim Dot Farm humour is welcome, but no gore or nudity.
- **Style hint:** use the brief's "Application Scenarios & Imagery" and
  "Global Emotional Progression" lines when present (only 5 of 413 songs
  have them). Otherwise use the one-line `caption` /
  `meta_json.brief.style_prompt`, truncated to ~300 chars. A song with no
  lyrics uses the hint alone; that is enough for a scene.
- Model: `models.art_scene` in config, default `qwen38` (same LLM client and
  token handling as briefs, `pipelines/llm.py`).
- Treat output as untrusted (AGENTS.md §5): strip fences, parse strictly,
  validate length and banned words, retry up to `scene_attempts`, else skip
  the song (logged) and try again after `retry_after_s`.
- Store the scene, the final FLUX prompt and the seed in the item's
  `meta_json.art` so a render is reproducible.
- Seed: derive deterministically from the item id (e.g. `item_id * 7919 %
  2**31`), not from `random` (selectors/producers' RNG rule doesn't apply
  here, but deterministic seeds make re-renders reproducible).

## Step 4 — Storage (schema change: update RADIO.md §14 in the same commit)

Additive, idempotent migration in `store.py`, same pattern as `s3_key`:

| column (items) | meaning |
|---|---|
| `art_path TEXT` | absolute path of the local JPEG (NULL = no art yet) |
| `art_s3_key TEXT` | S3 key once uploaded (NULL = not uploaded) |
| `art_uploaded_at TEXT` | ISO time of the successful PUT |
| `art_attempted_at REAL` | epoch of the last failed attempt (back-off) |

Files:
- Write the master PNG and the JPEG to `pilgrim/library/art/` as **new
  files** named `<song media stem>.png` / `.jpg`. Hard rule 5 still applies:
  never modify or delete existing library files. If a file with that name
  exists, don't overwrite it: add a `-2`, `-3` suffix.
- Convert PNG→JPEG with the **ffmpeg CLI** (`ffmpeg -i x.png -q:v
  {jpeg_quality} x.jpg`), so no new dependency. Write to a temp name and
  rename it into place.
- Songs only (`type='song'`, not retired, not emergency).

## Step 5 — The art worker

`pilgrim/album_art.py`, an `ArtWorker` started next to `MediaPublisher` in
`server.py` (its own asyncio task; never inside scheduler/producer). Loop:

1. Pick the next song needing art: `art_path IS NULL`, both title and artist
   non-empty, and not attempted within `retry_after_s`. **Order:** songs in
   the committed program first (they're about to air), then newest
   `created_at` first. Stop when none are left; sleep `idle_poll_s`.
2. Memory gate (§0.2 rule 3). Below `min_free_gb`, sleep `retry_after_s`.
3. Scene (Step 3) → FLUX prompt → `FluxClient.generate` (one at a time).
4. Validate the PNG, write PNG + JPEG, set `art_path` and `meta_json.art`.
5. Log JSON lines with `item_id`, `stage` (`scene|render|encode|publish`)
   and `duration_ms`. Any failure sets `art_attempted_at` and moves on. It
   never raises out of the loop.

Health: public `/api/health` stays DB-free and probe-free (SPLIT_PLAN §1.3).
At most it adds `art: {"backend": ok|down|busy}` from the worker's
**cached** last result. The counts (`with_art`, `without_art`) go on the
admin health/state only. Nothing ever calls the FLUX server inline, because
that can block for 40 s. Update the §11 rows.

## Step 6 — Publish to S3

Extend `media_publish.py` (reuse `ObjectStore` / `fake_s3.py`; don't build a
second uploader). Rows with `art_path` set and `art_s3_key` NULL are uploaded
to `media.bucket` under
`art.key_prefix + <stem>-<sha256[:16]>.jpg` with `Content-Type: image/jpeg`
and the same immutable `Cache-Control`. Set `art_s3_key` / `art_uploaded_at`
only after a successful PUT. Failures follow the existing 5-minute retry.
Update RADIO §8.4. CloudFront already fronts the whole bucket, so no infra
change is expected. Verify one real object in Step 10.

## Step 7 — API (update RADIO §11 in the same commit)

- `GET /api/station/program` items gain `art_url`: `public_base_url + "/" +
  art_s3_key` when uploaded, else `""`. No origin fallback route: art is
  cosmetic, and an empty string means "no art".
- `GET /api/station/state` gains `current_art_url` (same rule) for DJ/TUI
  clients.

## Step 8 — Client (RADIO §9.1, update it in the same commit)

In `pilgrim/web/index.html`, inside `.win-body.radio`, **above** `.play-row`:
`<div class="art-frame"><img id="cover-art" alt="" hidden></div>`.

- Square, `width: min(100%, 300px)`, `aspect-ratio: 1`, centred, retro-pane
  border like the rest of the UI. When no art is shown, the frame shows the
  station placeholder (see Decision D3) at the same size, so the PLAY button
  **never moves** when art appears or changes.
- `player.js`: drive it from the **same audio-clock playhead logic as
  `songLabel`** (`player.js` ~L321). Audible song with non-empty `art_url` →
  show it; anything else → placeholder. Never show the next song's art
  early.
- Preload: when an item is decoded ahead, also `new Image().src = art_url`,
  so the swap is instant. `alt` = `${title} - ${artist}`.
- An image load error falls back to the placeholder silently. Art never
  affects playback, the indicator or OFF AIR logic.
- e2e: extend the Playwright gap test so fake items carry `art_url`, and
  assert the img swaps with the label and the play row's position doesn't
  shift.
- **Deploy order:** ship Steps 6–7 on the Mac (origin restart via
  `pilgrim/deploy/mac/origin.sh`) **before** the client. An old client
  ignores `art_url`, but a new client facing an old origin gets no art at all.
  Then push, and on the edge: `ssh pf 'cd ~/radio && git pull --ff-only'`.
  The edge reads `pilgrim/web/` straight from that checkout, so restart the
  edge only if `pilgrim/deploy/edge/*` changed (`make edge-up`, per RUNBOOK).
  Then, from the Mac: `make deploy-check`, `make split-smoke`,
  `make e2e-split` (AGENTS.md §7.5).
- Because edges micro-cache `/api/station/program`, a song can briefly
  appear without `art_url` right after its art uploads. That's expected.
  Don't bypass the cache to "fix" it.

## Step 9 — Backfill (~413 songs ≈ 4.6 h of renders)

`pilgrim/tools/art_backfill.py` + `make art-backfill` (dry run by default;
`ART_ARGS="--apply"` to render; `--limit N`; `--item ID`). It reuses the
worker's per-song function and memory gate, is resumable, and processes
newest first. The live worker would get there eventually; the tool is for
running it deliberately overnight. Refuse to run if the station's worker is
enabled and running (check a `meta` lease key), so two renderers never
contend.

## Step 10 — Smoke

`make art-smoke` (only when asked): `/health`, one 1024×1024 render of a
fixed test title, check PNG dimensions, encode JPEG, upload to a
`art/smoke/` key, GET it through CloudFront and check status 200 and
`content-type: image/jpeg`. Save the PNG to `pilgrim/logs/art-smoke.png` so
a human can eyeball it (title spelled right? square, not a disc?).

## Step 11 — Instagram Reel builder (ffmpeg only; no posting)

`pilgrim/tools/make_reel.py --item ID [--start S] [--duration D]` +
`make reel REEL_ARGS=...`. Inputs come **from S3 via CloudFront** (the song's
`s3_key` MP3 and `art_s3_key` JPEG), which proves the published copies are
good. Output: `pilgrim/logs/reels/<stem>.mp4`.

Spec (Instagram Reels): 1080×1920, 9:16, H.264 High, yuv420p, 30 fps, AAC
48 kHz stereo 192 kb/s, `+faststart`. Layout: the art blurred and darkened
to fill the frame, with the sharp 1080×1080 art centred on top. Recipe to
start from (verify the output with `ffprobe`):

```
ffmpeg -y -loop 1 -framerate 30 -i art.jpg -ss {start} -t {dur} -i song.mp3 \
  -filter_complex "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=40:2,eq=brightness=-0.15[bg];[0:v]scale=1080:1080[fg];[bg][fg]overlay=0:(H-h)/2,format=yuv420p[v];[1:a]afade=t=in:d=0.5,afade=t=out:st={dur-2}:d=2[a]" \
  -map "[v]" -map "[a]" -t {dur} -r 30 -c:v libx264 -profile:v high -preset medium \
  -crf 20 -tune stillimage -c:a aac -b:a 192k -ar 48000 -ac 2 -movflags +faststart out.mp4
```

Default `--start 0 --duration 60` (Decision D4). Uploading to Instagram is
**out of scope** for this plan.

---

## Decisions (defaults chosen; operator may override)

- **D1 — Concurrency with song generation.** Default: art renders run
  alongside M5 song renders, gated only by `min_free_gb` and one-at-a-time.
  (Measured: two 40 s renders completed with mlx-serve resident and ~36 GB
  free.) Alternative: a config flag `art.yield_to_local_song: true` that
  waits while the M5 (`mlx-serve`) pool backend holds a lease, which costs
  art throughput but leaves the GPU to the song.
- **D2 — Text accuracy.** Default: trust FLUX's text (2/2 exact in tests),
  with no OCR check. Misspelled art can be fixed with
  `art_backfill.py --item ID --force --seed N`. Alternative: render text-free
  art and overlay title/artist with ffmpeg `drawtext` (always spelled
  right, looks less integrated).
- **D3 — Non-song segments / songs without art.** Default: show a static
  station placeholder image (`web/img/`; operator to supply, or Step 8 adds
  a plain retro "PILGRIM DOT FARM" tile).
- **D4 — Reel audio window.** Default: first 60 s. We don't have chorus
  timestamps.

## Answers to the agent's pre-flight questions (2026-11-02)

The step text above is already updated for 1–5 and 9.

1. Config: `hosts.flux`, not `endpoints.flux`.
2. Health: public `/api/health` gets only the cached `backend` status;
   counts go on admin (option b).
3. Scene input: fall back to the caption/style_prompt (Step 3). Caption-only
   is fine for songs with no lyrics.
4. Caps: drop a trailing parenthetical, then 70/48 at a word boundary. The
   DB is unchanged.
5. Non-Latin titles/artists: render text-free. Accented Latin renders
   normally.
6. D1 stands: no yield flag now. Run alongside, gated by `min_free_gb`,
   one at a time.
7. `art.enabled: true`. The worker chews through the backlog (upcoming
   songs first, then newest). The backfill tool stays for targeted
   re-renders (`--item`, `--force`).
8. Add a plain retro "PILGRIM DOT FARM" placeholder tile in `web/img/`. The
   frame always shows the placeholder (never hides) for non-songs, so PLAY
   never moves.
9. 300 px square.
10. Yes: point AGENTS.md's active work plan at this file (PAYMENT.md
    paused). Commits cite the ALBUM_ART step **and** the RADIO sections
    touched (AGENTS still requires RADIO citations).
11. Confirmed: (a) request/paid songs get art like any song; (b) no origin
    art fallback, so `""` until uploaded, and a micro-cached program may
    briefly lack it; (c) reels only on demand, from S3 copies, never posted.
    The small choices you listed (key shape, column types, `library/art/`,
    suffixes, no OCR) are fine.
