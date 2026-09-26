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

### Observed output WAV properties:
- 2 channels (stereo), 16-bit, **44100 Hz** (native; keep it, client resamples).
- Requested 5 s returned **21.5 s** — `duration_s` was evidently **ignored**,
  or the model enforces a minimum segment. ⚠ duration control is UNRESOLVED.
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

---

## 4. Playwright — `localhost:8931`

MCP server responds and lists tools, but the browser backend currently fails at
the network layer: `NS_ERROR_NET_RESET` (even on https://example.com) and
`NS_ERROR_CONNECTION_REFUSED`. Cannot drive a browser yet. Likely a tunnel /
headless-runtime issue to resolve before the client gap test (§15) is possible.

---

## Open gaps (to resolve before depending on these in code)

1. **~~Kokoro TTS not reachable~~ RESOLVED** — tunnel up; verified /health,
   /voices, /tts and the real voice ids (am_liam, am_michael, af_aoede all
   present); 24 kHz mono confirmed.
2. **Song duration control unconfirmed** — `duration_s` ignored on a 5 s
   instrumental (got 21.5 s). Find the correct param, or accept model default
   and plan inventory around it.
3. **Playwright browser backend down** — needed for the §15 client gap test.
4. **LLMs are reasoning models** — budget max_tokens generously and parse only
   `content`; ship a failing-LLM discard path (already planned).
