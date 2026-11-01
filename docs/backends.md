# Backend probes — observed shapes (M1)

Probed 2025-09-26. All hosts reached through SSH tunnels. Do not trust PLAN/AGENTS
assumptions; trust these observed responses. Re-probe if a backend changes.

## 0. Access

Services are NOT hosted on this box — they are forwarded via SSH tunnels:

| Backend | Local endpoint | Tunnel | Status on probe |
|---------|----------------|--------|-----------------|
| LiteLLM (LLMs) | `http://localhost:4000/v1` | up | OK |
| mlx-serve (MiniMax Music 3) | `http://127.0.0.1:11234` | up | OK (now one of three; see §2b) |
| Song pool via LiteLLM | `http://localhost:4000/vllm` | (proxy) | OK, 2026-11-01 (§2b) |
| Kokoro TTS | `http://localhost:8001` | up | OK (55 voices, 24 kHz mono) |
| Playwright | `http://localhost:8931` | up (MCP) | browser backend fails (still) |

Auth: `LITELLM_TOKEN` in `.env`, sent as `Authorization: Bearer <token>`.

---

## 1. LLM — LiteLLM proxy (`localhost:4000/v1`)

### Models (`GET /v1/models`) — observed:

```
qwen38                 (max_input_tokens 262144)   <- news, briefs, dj_talk
qwen38-mlx
Ornith-1.5-9B                                     <- commercials, liners ("ornith")
FastContext-1.0-4B-SFT
deepseek-v4-flash      (mode: chat)
MN-Oblivion-26B
embeddinggemma:300m, nomic-embed-text-v2-moe:latest (mode: embedding)
```

### Chat (`POST /v1/chat/completions`) — observed request:

```json
{"model":"qwen38","messages":[{"role":"user","content":"..."}],"max_tokens":512}
```

### Critical observed behavior — **reasoning models**

Both `qwen38` and `Ornith-1.5-9B` are reasoning models. Responses include a
separate `reasoning_content` field and emit `content` as the final answer:

```json
{
  "choices": [{
    "finish_reason": "length",
    "message": {
      "content": "{\"ok\":true,\"word\":\"radio\"}",
      "role": "assistant",
      "reasoning_content": "<hidden chain of thought>"
    }
  }],
  "usage": {"completion_tokens": ..., "prompt_tokens": ...,
            "completion_tokens_details": {"reasoning_tokens": ...}}
}
```

Implications for the pipelines (AGENTS §5, PLAN §6):
- `reasoning_content` consumes token budget. With `max_tokens: 30` the answer
  was empty (`finish_reason: length`) — all budget went to reasoning.
- Set `max_tokens` generously (≥512 for JSON briefs) so reasoning + final
  JSON fit. Parse only `choices[0].message.content`, never `reasoning_content`.
- A full structured song brief (250–450-word caption + lyrics) needs
  `max_tokens: 16384`: at 4096 the reasoning trace consumes the whole budget and
  `content` comes back empty (verified 2026-10-31; ~125 s wall with streaming).
- **Correction 2026-11-01: omitting `max_tokens` is NOT "no limit".** Probed
  with the station down: no `max_tokens` → `finish_reason: length` at exactly
  **8192** completion tokens (114.7 s); `max_tokens: 16384` → ran to 16384;
  `max_tokens: 261000` is accepted (`stop` on a short answer). Every long brief
  had ended empty or mid-JSON at 118.8–123.3 s. The client now sends
  `max_tokens` = `models.context_tokens[model]` (qwen38: 262144) minus a
  conservative prompt estimate on every call that doesn't set one. `llm.call`
  logs `finish_reason` (`length` = cap hit).
- **No cap at all (earlier finding, short calls only):** with `max_tokens` omitted, both `qwen38`
  (2.6 s, 104 completion tokens incl. 70 reasoning) and `Ornith-1.5-9B`
  returned `finish_reason: stop` and valid JSON (probed 2026-10-31). The
  client omits `max_tokens` by default; moderation at 4096 had failed closed
  on a harmless request after three empty answers. Streaming keeps the read
  timeout an inactivity timeout, so long reasoning does not time out.
- Calls are read as an **SSE stream** (`stream: true`), accumulating
  `delta.content` and ignoring `delta.reasoning_content`. A non-streaming read
  timed out at 120 s / 300 s on long briefs; streaming makes the timeout
  measure inactivity between chunks, not total generation time.
- qwen38 `content` returned exact JSON when budgeted (`{"ok":true,"word":"radio"}`).
- Validate JSON strictly and discard on failure (as planned — still required).

No measure of time-to-first-token or per-token latency for song briefs yet;
`timings` field is echoable in responses (useful for the rate metric).

---

## 2. Song generation — mlx-serve (`127.0.0.1:11234`)

Model id: `MiniMax-Music3-MLX-Serve-8bit` (`GET /v1/models`), capabilities
`["audio","music"]`, input modality text.

### Endpoint — **synchronous, returns raw WAV inline**

`POST /v1/audio/music-generations`  ⇒  HTTP 200 with the WAV bytes as the body
(no job id / no polling). PLAN §6.2 says "poll until the WAV exists" — the
real path returns it directly. This is the path AGENTS.md says to reuse.

### Required fields (discovered via validation errors, in order):

1. `prompt` — style/genre/mood description. Missing ⇒
   `{"error":"missing 'prompt' (style/genre/mood description)"}`
2. `lyrics` — **MiniMax Music 3 is lyric-conditioned**; structure tags like
   `[verse]` on their own lines. **OR** `"instrumental": true` instead of
   lyrics. Missing both ⇒
   `{"error":"missing 'lyrics' (... or send \"instrumental\": true)"}`

Example that produced audio:
```json
{"prompt":"quiet ambient pad","instrumental":true,"duration_s":5}
```

### Duration — `duration_seconds` (verified 2026-09-30)

Per the model card (huggingface.co/ddalcu/MiniMax-Music3-MLX-Serve-8bit):
`"duration_seconds"`, 1–360, an **upper bound** ("the model may end the song
earlier"). Without it the server defaults to **60 s** and hard-cuts there
(server log: `[music3] generating 60s ... (≤1500 frames)`).

Observed: `{"prompt":..., "lyrics":..., "seed":1, "duration_seconds":10}` →
HTTP 200, **10.0 s** of audio in 16.4 s wall. `duration_s` and `max_tokens`
are **silently ignored** (both returned 60 s). The producer sends
`duration_seconds = songs.max_duration_s` (360).

### Observed output WAV properties:
- 2 channels (stereo), 16-bit, **44100 Hz** (native; keep it, client resamples).
- Requested 5 s returned **21.5 s** — `duration_s` is **ignored** (wrong key;
  see `duration_seconds` above — RESOLVED).
  Longer lyric-conditioned requests may also not honor duration. Must measure
  with real prompts; do not assume linear scaling (AGENTS/PLAN warning stands).

### Prompt budget (probed 2026-10-30)

`GET /v1/models` for `MiniMax-Music3-MLX-Serve-8bit` reports
`context_length: 4096`, `max_model_len: 4096`, `model_max_tokens: 4096`. The
model card's 5,000-token prompt limit applies to the reference SGLang build;
our MLX deployment is tighter. A 250–450-word structured caption (~350–650
tokens) plus ~40 lines of lyrics (~400 tokens) fits comfortably; the 631-word
reference templates plus a long lyric sheet are near the edge.

### Prompt adherence probe (2026-10-31, 10 s clips)

Probed because a listener request for reggae produced a track that sounded
country while its stored caption correctly read `Reggae / Roots Reggae`:

- **Deterministic under seed.** The same `prompt` + `lyrics` + `seed` +
  `duration_seconds` returned byte-identical WAVs (same SHA-1), so a stored
  `seed` in `items.meta` really does replay a song.
- **`prompt` is not ignored when lyrics are present.** Same lyrics and seed,
  a death-metal caption vs the reggae caption produced different audio, so the
  caption reaches the model and conditions it.
- **No hard truncation at ~70 words.** Truncating a 404-word caption to its
  first 70 words changed the output, so the tail (Arrangement) is read.
- **The caption is not the only input that decides the feel.** Reusing one
  caption with different lyrics changed the output as much as changing the
  caption did, consistent with the model being lyric-conditioned: country-coded
  lyric vocabulary pulls the track toward country regardless of caption.
  Hence the style lock in `pilgrim/pipelines/songs.py::_validate_brief`
  (RADIO.md §6.2) rejects conflicting signature instruments in lyrics and
  captions. Metadata alone cannot prove a song's audible style; listening is
  still required.

### Notes for producer:
- `prompt` carries the full **structured caption** (Global Metadata / Vocal
  Details / Arrangement), not a one-line style description. See `pilgrim/genres.py`
  for the 18 style families and `pilgrim/prompts/song_brief.md` for the contract.
- This is compute-expensive. Only smoke-test ≤10 s audio (AGENTS §1.6).
- Record wall-clock generation time per request for the rate metric.
- Unknown: whether request is truly synchronous-blocking for long songs or
  streams; measure with a real 150–210 s brief in `make smoke`.

---

## 2b. Song generation — backend pool (MiniMax Music 3 ×3), probed 2026-11-01

Probed from the EC2 dev box through the proxy, with 10 s instrumental clips
only (hard rule 6). This box can't reach `192.168.68.x` directly.

### Topology (`GET /model/info`, `/router/settings`, read-only)

One model group, `MiniMax-Music-3`, made of three deployments:

| Deployment | api_base | LiteLLM model | Response marker |
|---|---|---|---|
| GPU box | `http://192.168.68.88:8080` | `vllm/MiniMax-Music-3` | `x-generated-by: audiocpp/MiniMax-Music-3` |
| GPU box | `http://192.168.68.56:8080` | `vllm/MiniMax-Music-3` | `x-generated-by: audiocpp/MiniMax-Music-3` |
| M5 | `http://192.168.68.51:11234` | `vllm/MiniMax-Music3-MLX-Serve-8bit` | no `x-generated-by`; `access-control-allow-origin: *` |

Router settings: `routing_strategy: least-busy`, `num_retries: 2`,
`timeout: 6000`, `cooldown_time: 5`, `allowed_fails: 3`. No deployment sets
`max_parallel_requests` or `rpm`, so **nothing upstream refuses a 4th
concurrent render**: it goes to an already busy box. The station enforces the
ceiling itself (`MusicPool`, one lease per slot).

### Endpoint

- `POST /v1/audio/music-generations` on the proxy → **404** (LiteLLM 1.104.2
  has no music route; `/openapi.json` lists only `/v1/audio/speech` and
  `/transcriptions`; `/config/pass_through_endpoint` is `[]`).
- `POST /vllm/v1/audio/music-generations` (generic vLLM pass-through) →
  **works**. The router picks the deployment from `model` in the body.

```
POST http://localhost:4000/vllm/v1/audio/music-generations
Authorization: Bearer $LITELLM_TOKEN
{"model": "MiniMax-Music-3", "prompt": "...", "instrumental": true,
 "duration_seconds": 10, "seed": 42}
→ 200, content-type: audio/wav, raw WAV inline (same shape as §2)
```

- Output on every box: `pcm_s16le`, 44.1 kHz, stereo, ~9.98–10.0 s for a 10 s
  request. Single clip: mean −21.6 dB, peak −4.4 dBFS.
- `GET /vllm/v1/models` and `GET /vllm/health` → **500** (the router needs a
  `model` in a body). Health in LiteLLM mode = `models.music` appears in the
  proxy's `GET /v1/models`.
- No `x-litellm-*` headers on pass-through responses. Only `x-generated-by`
  tells audiocpp from mlx-serve; the two audiocpp boxes can't be told apart.

### Concurrency (three 10 s calls)

- Fired at the same instant, least-busy can put them on the same box (the
  router hasn't counted the first as in flight yet; operator observation).
- **Staggered 2 s apart**, all three ran in parallel on separate boxes:
  audiocpp 23.6 s and 27.7 s, mlx-serve 39.2 s (call 2). Hence
  `songs.submit_stagger_s: 5` (operator: at least 5 s).
- 10 s clips carry fixed overhead; don't extrapolate RTF to full songs
  (AGENTS §10). `song.produced` logs `backend`, `generator`, `wall_s`, `rtf`
  for real songs.

### Caveats

- `num_retries: 2` applies to routed calls. Whether it also retries
  pass-through calls is unverified. If it does, a failed render may silently
  run again on another box.
- The vLLM builds differ from the MLX 8-bit build: a seed replays only on the
  generator that made the song (`meta.generator`).
- `.56:8080` is also the api_base of `Qwen3-Omni-30B`, which suggests a
  model-swapping server. Not measured.

---

## 3. Kokoro TTS — `localhost:8001` (verified)

Tunnel up. `GET /health` → `{"status":"ok","device":"cuda","model_loaded":true,"num_voices":55,"default_voice":"af_heart","queue":{"pending":0}}` (re-probed 2026-10-30: 55, was 54 at first probe).

`GET /voices` → 55 ids. Plan §10 voice ids all confirmed present
(`am_justin` is the operator character's reserved voice, RADIO §10):
`am_liam`, `am_michael`, `af_aoede` (plus `af_heart` default, many more).

`/tts` is a **GET with query params** (POST body is rejected with 422
`{"loc":["query","text"],"msg":"Field required"}` — `text` must be a
query param):

```
GET /tts?text=<url-encoded>&voice=am_liam&speed=1.0&format=wav
```

Observed: `"Pilgrim Dot Farm."` @ `am_liam` speed 1.0 → 2.02 s WAV,
**1 channel, 16-bit, 24000 Hz** (matches PLAN §2). 16-bit mono 24 kHz confirmed.

**Length probe (OVERHAUL 2.6, 2026-09-29):** 120 words
("The quick farmer counted forty pickles by the barn door." × 12) →
34.2 s @ **3.51 words/s**. That is above the 3.2 w/s threshold the overhaul
uses to flag truncation, so `KokoroClient.synth` now splits long copy on
sentence boundaries into chunks of ≤ 200 chars, synthesizes each, and
concatenates (same 24 kHz) before writing the WAV.

---

## 5. SearXNG — via LiteLLM MCP (`http://localhost:4000/mcp`)

searxng is **not** a direct HTTP service reachable at `:8888`. It is exposed
as the `web_search-searxng_web_search` tool on the LiteLLM MCP server
(Streamable-HTTP transport). Probed (2026-09-27):

```
POST /mcp
  Authorization: Bearer <LITELLM_TOKEN>
  Content-Type: application/json
  Accept: application/json, text/event-stream
```

- `initialize` (protocolVersion `2025-03-26`) → session id in the
  `mcp-session-id` response header; body is SSE (`event: message` / `data: {...}`).
- `tools/list` (id 3) → `result.tools[].name`; probed 2026-09-29: 71 tools, time
  is `time-get_current_time` (always resolve by suffix `get_current_time` — the
  proxy may prefix tool names).
- `tools/call time-get_current_time {timezone: America/Detroit}` →
  `result.content[0].text` is a JSON string:
  `{"timezone": "America/Detroit", "datetime": "2026-09-29T19:22:02-04:00",
    "day_of_week": "Tuesday", "is_dst": true}` — parse the `datetime` field.
- `tools/call` `web_search-searxng_web_search` with `{query, limit, result_detail:
  "compact"}` → `result.content[].text` = ranked `Title\nDescription\nURL` blocks.
- Live search for "top news headlines today" returned real Google News results.

### Engines and the copied-lyrics screen (probed 2026-10-31)

The tool takes `engines` (comma-separated) and `num_results`. Quoted-phrase
query `"backstroke lover always hidin' neath the covers" lyrics`, per engine:

| engine | result |
|---|---|
| google, duckduckgo, brave, qwant, yahoo | tool error "Search Degraded … engines failed" (suspended: too many requests / CAPTCHA / access denied) |
| bing | answers, but searches only the **first word** ("Backstroke" → swimming pages), quoted or not |
| mojeek | clean "No results found" (small index) |
| **startpage** | correct: Musixmatch / Spotify / YouTube lyric pages naming the song |

Startpage also found two other famous songs from one line each, and still found
the song when one word was changed. A quoted original line gave a clean
`🔍 No results found for …` (not an error). Unquoted, the same original line
returned unrelated pages, so the screen always quotes and decides by its own
word overlap. Three outcomes must stay distinct: results, clean "No results
found" (text, `isError` false), and degraded (`isError: true` / HTTP / no
token). `searxng_lookup` (tri-state) is used for the screen; `searxng_search`
flattens all three to a list and stays for news.

Client: `pilgrim/pipelines/mcp.py` (`MCPSession.searxng_search`, `MCPSession.list_tools`,
`current_local_time` in `pilgrim/pipelines/clocktime.py` for the DJ's real time).
The news pipeline (RADIO.md §6.4) calls it with the bearer token; any failure
degrades to a model-written bulletin.

---

## 7. SFX — MOSS-SoundEffect v2.0 (`127.0.0.1:8000`, verified 2026-10-01)

OpenAI-style wrapper (FastAPI, `/openapi.json` available), no auth, CUDA bf16.
Replaced Stable Audio 3 Small SFX (`:8500`); `SFX.md` §1–§2 hold the old
model's probe notes.

- `GET /health` → `{"status":"ok","model":"moss-soundeffect-v2.0","device":"cuda",
  "dtype":"torch.bfloat16","sample_rate":48000,"max_seconds":30,"error":null}`
- `GET /v1/models` → `{"object":"list","data":[{"id":"moss-soundeffect-v2.0",
  "object":"model","owned_by":"openmoss","type":"text-to-audio"}]}`
- `POST /v1/audio/speech` (also `/audio/speech`; synchronous, raw audio body):
  ```json
  {"model": "moss-soundeffect-v2.0", "input": "<prompt>", "seconds": 2.0,
   "num_inference_steps": 100, "cfg_scale": 4.0, "sigma_shift": 5.0,
   "negative_prompt": "", "response_format": "wav", "seed": 24}
  ```
  Only `input` is required; values shown are the server defaults (`seconds`
  defaults to 10). `voice`/`speed` are accepted and ignored. `seconds` > 30 →
  HTTP 500 `Internal Server Error` (text/plain), so the client clamps.
  `response_format`: wav | mp3 | flac | ogg | pcm (only wav exercised).
  Response headers: `content-type: audio/wav`, `x-audio-sample-rate: 48000`,
  `x-generated-by: moss-soundeffect-v2.0`.
  `wav` → PCM s16le **48 kHz mono**, length exact (2.0 s asked → 2.0 s),
  peak ≈ −0.4 dBFS. Fixed `seed` → byte-identical output.
- Wall time: per step, ~flat in duration. 100 steps / 2 s → 38 s;
  50 steps / 2 s → 20 s; 50 steps / 10 s → 21 s.

Client: `pilgrim/pipelines/sfx.py` (`SfxClient.generate`).

---

## 6. Playwright — `localhost:8931`

MCP server + headed browser (Chrome on VNC display :1) confirmed working
2026-09-27: navigated https://example.com and https://www.google.com, public
internet egress OK, window visible on the VNC display. Systemd unit
`playwright-mcp.service`, localhost-only, persistent profile. (Earlier
"network isolated" note was from a pre-setup state and is resolved.)

---

## 8. Stripe — Checkout Sessions + webhooks (verified 2026-10-31, LIVE)

Account `acct_1UWPQTKOiB5HDXLd` (Pilgrim Dot Farm LLC, US, USD, charges +
payouts enabled, MCC 5815, statement descriptor `PILGRIM DOT FARM`).
API version pinned by this project: **`2026-10-28.endive`** (current Stripe
release train at the time of writing).

Product `prod_VXVIBdZhOh1eD8` "Priority Song Request", price
`price_1UWQHdKOiB5HDXLd4qNMoosI`: `custom_unit_amount` min 300 / preset 350 /
max 6769, `usd`, `one_time`, `tax_behavior=exclusive`, `tax_code=txcd_20030000`,
default price of the product. Product description (the only "policy" text
Stripe holds) reads: *"Priority song requests are queued ahead of free
requests and include expanded, multiline input for detailed musical
direction. Requests are processed first-come, first-served within each queue.
Timing of playback depends on queue length. Paying above the minimum does not
increase priority."*

### Create — observed request (works)

```
POST https://api.stripe.com/v1/checkout/sessions
Stripe-Version: 2026-10-28.endive
Idempotency-Key: pilgrim-checkout-probe-1793422041
Authorization: Bearer <sk_live_…>

ui_mode=embedded_page
mode=payment
line_items[0][price]=price_1UWQHdKOiB5HDXLd4qNMoosI
line_items[0][quantity]=1
managed_payments[enabled]=false
redirect_on_completion=never
client_reference_id=probe-1793422041
metadata[request_id]=0
metadata[token]=probe-1793422041
payment_intent_data[metadata][request_id]=0
payment_intent_data[description]=Pilgrim Dot Farm priority song request
expires_at=<now+1800>
```

### Observed response fields we use

```
id                  cs_live_a1RSet3eYtdLsNHqKZPi3RcShSCMUTX2zcXB0jGD0ct2fiTpdyD3Wc1tcp
ui_mode             embedded_page
mode                payment
status              open
payment_status      unpaid
amount_total        350            # preset; the payer can change it (300..6769)
currency            usd
livemode            true
expires_at          1793423841     # now+1800 honoured
payment_method_types ["card","link"]   # dynamic; not settable per session
client_reference_id probe-1793422041
metadata            {"request_id":"0","token":"probe-1793422041"}
return_url          null
payment_intent      null
managed_payments    {"enabled": false}
automatic_tax       {"enabled": false, ...}
client_secret       cs_live_… (first 8 only; never logged or stored)
```

`GET /v1/checkout/sessions/{id}` returns the same shape (verified: `open`,
`unpaid`, `embedded_page`, `livemode: true`). The probe session is left to
expire on its own; no charge, no customer data.

### Parameter traps found (all verified live, 2026-10-31)

| Attempt | Error | Resolution |
|---|---|---|
| `payment_method_types[0]=card` | *"The `payment_method_types` parameter is no longer supported… Payment methods are now managed from your Dashboard settings"* | Do not send it. Methods are dynamic. |
| `allowed_payment_method_types[0]=card` | *"cannot be used with Managed Payments, which is enabled by default on your account"* | Either drop it or disable Managed Payments for the session. |
| `automatic_payment_methods[enabled]=true` | *"Received unknown parameter"* | Not accepted on this API version. |
| default (Managed Payments on) | *"this product tax code is ineligible for Managed Payments"* (`txcd_20030000`) | Send `managed_payments[enabled]=false` (Pilgrim stays merchant of record). |
| `return_url` + `redirect_on_completion=never` | *"You cannot pass a `return_url` when `ui_mode: embedded_page` and `redirect_on_completion: never`"* | Omit `return_url` entirely for these sessions. |

**Decision needed:** Managed Payments is ON by default for the account. With
the current product tax code it is ineligible anyway, so every session passes
`managed_payments[enabled]=false`. If the operator wants Stripe as merchant of
record (Stripe handles sales tax), the product needs an eligible tax code and
the per-session flag has to go.

### Webhook endpoint (live)

`we_1UWU1pKOiB5HDXLdhsCpnggq` → `https://pilgrim.farm/api/stripe/webhook`,
events `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
`checkout.session.async_payment_failed`, `checkout.session.expired`,
`charge.refunded`, `charge.dispute.created`, `api_version: null` (account
default), status `enabled`. Signature header: `Stripe-Signature:
t=<unix>,v1=<hex hmac-sha256 of "<t>.<raw body>">` (possibly several `v1`).

### Stripe.js (to confirm in Step 8 with Playwright)

Expected `https://js.stripe.com/v3/` then
`stripe.initEmbeddedCheckout({ fetchClientSecret, onComplete })` →
`checkout.mount("#pay-checkout")`. The probe above proves the server side; the
client side is verified against the real page in Step 8.

---

## Open gaps (to verify before depending on these in code)

1. **Song duration control** — `duration_s` ignored on a 5 s instrumental (got
   21.5 s). Find the correct param, or accept model default and plan inventory
   around it.
2. **LLMs are reasoning models** — budget max_tokens generously and parse only
   `content`; ship a failing-LLM discard path (RADIO.md §6.3/§5).
3. **Real seed run** — `make seed` against live backends still to be
   validated end-to-end (M10).
