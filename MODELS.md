# MODELS.md — Pilgrim Dot Farm Radio

The models this station runs, what each one does, and where it runs. Five
production models plus one development model. All endpoints, aliases, and
hardware targets are declared in `config.yaml` / `pilgrim/config.yaml` (§12);
this file is the human-readable summary of *which model does what*.

**Slogan:** *Pilgrim Dot Farm*

---

## Quick reference

| Model | HF repo | Role on air | Runs on | Via |
|-------|---------|-------------|---------|-----|
| **qwen38** (Qwen 3.8 27B) | `Qwen/Qwen3.8-27B` | News, song briefs, DJ talk, field reports | CUDA cluster | LiteLLM (`<LITELLM_HOST>`) |
| **Ornith-1.5-9B** | `ornith-ai/Ornith-1.5-9B` | Commercials, liners | CUDA cluster | LiteLLM (`<LITELLM_HOST>`) |
| **MiniMax Music 3** | `MiniMaxAI/MiniMax-Music3` | Song generation | **M5 Mac Studio** | mlx-serve (`127.0.0.1:11234`) |
| **MOSS-SoundEffect v2.0** | `OpenMOSS-Team/MOSS-SoundEffect-v2.0` | Sound-effect stingers and beds | Local CUDA host (`127.0.0.1:8000`) | OpenAI-compatible wrapper |
| **Kokoro-82M** | `hexgrad/Kokoro-82M` | Voices and speech (TTS) | Kokoro host (`192.168.68.89:8001`) | `/tts` HTTP |
| **DeepSeek-V4-Flash-0731** | `deepseek-ai/DeepSeek-V4-Flash-0731` | Development (coding assistant, not on air) | — | dev environment |

> **Two LLMs, both reasoning models.** `qwen38` and `Ornith-1.5-9B` both
> return a hidden `reasoning_content` field alongside the usable `content`.
> Parse only `content`, budget `max_tokens` generously, and never feed the
> reasoning tokens back into a prompt (see §3).

---

## 1. qwen38 — Qwen 3.8 27B (news, briefs, DJ talk)

- **HF repo:** [`Qwen/Qwen3.8-27B`](https://huggingface.co/Qwen/Qwen3.8-27B)
  (internal config alias `qwen38` is unchanged).
- **Role (§6):**
  - **News** (`news.py`) — 3–4 headlines, summarized in its own words, sources
    attributed on air ("according to…"). Bounded: **max 3 web searches, 60 s
    wall clock**; abort and keep the previous bulletin if the budget is hit.
  - **Song briefs** — JSON `{title, artist, genre, style_prompt, lyrics}`
    validated against a schema (§6.2). Highest-priority LLM work once a request
    lands.
  - **DJ talk** — contextual talk-ups and intros, written just-in-time with the
    previous/next items as neighbors.
  - **Field reports** — the British field reporter's farm factoids.
- **Hardware / transport:** served through LiteLLM on the CUDA cluster (one of
  the DGX Spark units). `Authorization: Bearer <LITELLM_TOKEN>`.
- **Notes:**
  - Reasoning model — needs a generous `max_tokens` (news moderation uses
    4096; briefs use ≥512). With a tight budget the final JSON returns empty
    (`finish_reason: length`) because the reasoning chain ate the tokens.
  - The 5090 host (32 GB VRAM) is the fallback for a larger news model if
    accuracy ever needs it (§6.4).

---

## 2. Ornith-1.5-9B — commercials & liners

- **HF repo:** `ornith-ai/Ornith-1.5-9B`
- **Role (§6.5):** satirical spots ("products that make life more convenient in
  the worst way, or medications that make you sicker") and station liners.
- **Hardware / transport:** served through LiteLLM on the CUDA cluster (the
  **4070 Ti** host per RADIO.md §2). High-volume, low-stakes work — so it shares
  the LLM tier with `qwen38` rather than getting its own box.
- **Notes:**
  - Reasoning model like `qwen38` — same `content`/`reasoning_content` parsing
    rule and generous `max_tokens`.
  - Commercial gags build over time: spots draw on recurring sponsors from the
    station bible so callbacks and escalations make sense (§7).

---

## 3. MiniMax Music 3 — song generation

- **HF repo:** `MiniMaxAI/MiniMax-Music3`
- **Role (§6.2):** generates the evergreen song stock. The pipeline is
  brief (`qwen38`) → lyrics → **generate** → QC → normalize → store.
- **Hardware / transport:** runs on the **M5 Mac Studio**, exposed as an
  OpenAI-compatible server (**mlx-serve**) at `127.0.0.1:11234`, endpoint
  `POST /v1/audio/music-generations`. This is the one GPU that is *not* CUDA —
  it is Apple Silicon.
- **Notes:**
  - **Lyric-conditioned.** Send lyrics with `[verse]`/`[chorus]` tags, or
    `"instrumental": true`. Missing both is rejected.
  - **`duration_seconds` (1–360)** is an *upper bound* — the model may end
    earlier. Without it the server defaults to 60 s and hard-cuts. The producer
    sends `songs.max_duration_s` (360) as the ceiling.
  - Output is 16-bit stereo **44.1 kHz** WAV; keep the native rate (the browser
    resamples on decode). **Do not use MP3** — encoder padding breaks gapless
    joins; deliver FLAC (§8.2).
  - **Expensive.** ~1.6 s of compute per 1 s of audio; a 180 s song takes ~290 s
    (~35 min of fresh music per hour). Measure per real length — do not assume
    linear scaling. **Never** start real generation except in a smoke test, and
    then request ≤ 10 s (§6).
  - Song generation and the LLMs are on **separate hosts**, so there is no GPU
    contention with text work.

---

## 4. MOSS-SoundEffect v2.0 — sound effects

- **HF repo:** [`OpenMOSS-Team/MOSS-SoundEffect-v2.0`](https://huggingface.co/OpenMOSS-Team/MOSS-SoundEffect-v2.0)
  (replaced Stable Audio 3 Small SFX on 2026-10-01).
- **Role (§4, §5, §SFX.md):** short sound-effect stingers and ambience beds
  layered *over* a host clip (DJ talk, field reports, commercials, liners) as
  sidecar overlays. **Never** on news — structurally impossible (§0 of SFX.md).
- **Hardware / transport:** `127.0.0.1:8000`, an OpenAI-compatible wrapper
  (no auth, CUDA, bf16). `POST /v1/audio/speech`. Shapes in
  `docs/backends.md` §7.
- **Notes:**
  - Deterministic: a fixed `seed` yields a byte-identical WAV.
  - `seconds` ≤ 30 per request; `wav` is PCM s16le **48 kHz mono**, peaking
    near full scale — always gain-stage when layering.
  - Cost is per solver step, nearly flat in clip length: ~0.2 s/step
    (100 steps ≈ 38 s for a 2 s clip; 50 steps ≈ 20 s for 2 s *or* 10 s).
    Much slower than the old model per stinger, so render into inventory
    ahead of air — **never at playout** (AGENTS rule 1).

---

## 5. Kokoro-82M — voices and speech

- **HF repo:** [`hexgrad/Kokoro-82M`](https://huggingface.co/hexgrad/Kokoro-82M)
- **Role (§6.3):** renders every spoken item (DJ talk, intros, news,
  commercials, liners, field reports) from approved copy, after the mandatory
  TTS cleanup step.
- **Hardware / transport:** Kokoro TTS service at `192.168.68.89:8001`
  (`/tts`, `/voices`, `/health`); 16-bit mono 24 kHz.

---

## 6. DeepSeek-V4-Flash-0731 — development

- **HF repo:** `deepseek-ai/DeepSeek-V4-Flash-0731`
- **Role:** general-purpose coding assistant for building and maintaining the
  station. **Not part of on-air production** — no content reaches the listener
  through it.
- **Hardware / transport:** dev environment (not bound to any production box).

---

## 7. Hardware map

| Box | Type | Runs |
|-----|------|------|
| **M5 Mac Studio** | Apple Silicon | MiniMax Music 3 (song generation) via mlx-serve; also hosts the station server |
| **Two DGX Spark units** | CUDA | LiteLLM-backed LLMs (`qwen38`, `Ornith-1.5-9B`) |
| **4070 Ti** | CUDA | `Ornith-1.5-9B` (per RADIO.md §2) |
| **Local CUDA host** | CUDA | MOSS-SoundEffect v2.0 wrapper (`:8000`) |

Design intent (§2): text/LLM work and GPU audio generation are kept on
separate hosts so they never compete for VRAM. Song generation lives entirely
on the M5; the SFX model runs on its own local CUDA host.

> **⚠️ Hardware naming conflict — needs confirmation.** RADIO.md §2 names the
> LLM hosts "5090" (`qwen38`) and "4070 Ti" (`ornith`). This file follows the
> AGENTS.md/user brief, which lists the CUDA hardware as **two DGX Spark units
> + one 4070 Ti**. Reconcile which box actually runs `qwen38` vs. `Ornith-1.5-9B`
> and update the table above. The *model→role* mapping is certain; the exact
> GPU-to-model assignment is not.

---

## 8. Cross-cutting notes

- **All hosts, model aliases, and targets live in `config.yaml`** (§12). Do not
  hardcode addresses anywhere else (AGENTS rule 4).
- **LLM outputs are untrusted** (§5): parse JSON strictly, validate against the
  schema, discard on failure, strip code fences first. Never `eval` model or
  web output.
- **No secrets in the repo** (§7): `LITELLM_TOKEN` and other keys go in
  environment variables (`.env`, git-ignored).
- **Tests never touch real backends** (§15): fakes (`tests/fakes/`) stand in
  for all of the above, so unit/sim/e2e runs are deterministic and offline.
