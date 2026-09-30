# Backend probes — observed shapes (M1)

Probed 2025-09-26. All hosts reached through SSH tunnels. Do not trust PLAN/AGENTS
assumptions; trust these observed responses. Re-probe if a backend changes.

## 0. Access

Services are NOT hosted on this box — they are forwarded via SSH tunnels:

| Backend | Local endpoint | Tunnel | Status on probe |
|---------|----------------|--------|-----------------|
| LiteLLM (LLMs) | `http://localhost:4000/v1` | up | OK |
| mlx-serve (MiniMax Music 3) | `http://127.0.0.1:11234` | up | OK |
| Kokoro TTS | `http://localhost:8001` | up | OK (54 voices, 24 kHz mono) |
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

### Notes for producer:
- This is compute-expensive. Only smoke-test ≤10 s audio (AGENTS §1.6).
- Record wall-clock generation time per request for the rate metric.
- Unknown: whether request is truly synchronous-blocking for long songs or
  streams; measure with a real 150–210 s brief in `make smoke`.

---

## 3. Kokoro TTS — `localhost:8001` (verified)

Tunnel up. `GET /health` → `{"status":"ok","device":"cuda","model_loaded":true,"num_voices":54,"default_voice":"af_heart","queue":{"pending":0}}`.

`GET /voices` → 54 ids. Plan §10 voice ids all confirmed present:
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

Client: `pilgrim/pipelines/mcp.py` (`MCPSession.searxng_search`, `MCPSession.list_tools`,
`current_local_time` in `pilgrim/pipelines/clocktime.py` for the DJ's real time).
The news pipeline (RADIO.md §6.4) calls it with the bearer token; any failure
degrades to a model-written bulletin.

---

## 6. Playwright — `localhost:8931`

MCP server + headed browser (Chrome on VNC display :1) confirmed working
2026-09-27: navigated https://example.com and https://www.google.com, public
internet egress OK, window visible on the VNC display. Systemd unit
`playwright-mcp.service`, localhost-only, persistent profile. (Earlier
"network isolated" note was from a pre-setup state and is resolved.)

---

## Open gaps (to verify before depending on these in code)

1. **Song duration control** — `duration_s` ignored on a 5 s instrumental (got
   21.5 s). Find the correct param, or accept model default and plan inventory
   around it.
2. **LLMs are reasoning models** — budget max_tokens generously and parse only
   `content`; ship a failing-LLM discard path (RADIO.md §6.3/§5).
3. **Real seed run** — `make seed` against live backends still to be
   validated end-to-end (M10).
