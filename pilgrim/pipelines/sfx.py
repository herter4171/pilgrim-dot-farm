"""Sound-effect pipeline (SFX.md §1, §7.3): prompt -> MOSS-SoundEffect -> QC ->
normalize -> FLAC `sfx` item.

SFX items are never airable on their own: they ride on a host clip as a
sidecar overlay (`meta.sfx` on the host, see `pilgrim/sfx_plan.py`). Rendering
happens in the producer, ahead of air; playout never waits on it (AGENTS rule 1).
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from pathlib import Path
from typing import cast

import httpx
import numpy as np
import soundfile as sf
from pydantic import BaseModel, ValidationError

from pilgrim.audio import normalize
from pilgrim.audio.qc import check_silence, duration_bounds
from pilgrim.config import Config
from pilgrim.logging_setup import err_text

log = logging.getLogger("radio.sfx")

# Host clip types that may carry SFX. News is deliberately absent and must stay
# so: no news clip ever reaches the SFX path (SFX.md §0).
SFX_HOST_TYPES = frozenset({"dj_talk", "field_report", "commercial", "liner"})


class SfxClient:
    """`POST /v1/audio/speech` on the SFX wrapper (docs/backends.md §7)."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(cfg.sfx.timeout_s, connect=5.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def generate(self, prompt: str, duration_s: float, seed: int | None) -> bytes:
        s = self.cfg.sfx
        body: dict[str, object] = {
            "model": s.model, "input": prompt,
            "seconds": min(duration_s, s.max_seconds),
            "num_inference_steps": s.steps, "cfg_scale": s.cfg_scale,
            "sigma_shift": s.sigma_shift, "negative_prompt": s.negative_prompt,
            "response_format": "wav"}
        if seed is not None:
            body["seed"] = seed
        url = self.cfg.hosts.sfx.rstrip("/") + "/v1/audio/speech"
        last: Exception | None = None
        for attempt in range(3):
            try:
                r = await self._client.post(url, json=body)
                r.raise_for_status()
                if not r.content.startswith(b"RIFF"):
                    ctype = r.headers.get("content-type")
                    raise ValueError(f"sfx backend returned non-WAV ({ctype})")
                return r.content
            except Exception as e:
                last = e
                log.warning("sfx.backend_retry", extra={"attempt": attempt + 1,
                                                        "error": err_text(e)})
                await asyncio.sleep(1.0 * (attempt + 1))
        assert last is not None
        raise last


class SfxPipeline:
    """Render one curated cue into a normalized FLAC (not yet stored)."""

    def __init__(self, cfg: Config, client: SfxClient, media_dir: Path) -> None:
        self.cfg = cfg
        self.client = client
        self.media_dir = media_dir

    def approved(self, name: str) -> bool:
        cue = self.cfg.sfx.cues.get(name)
        return bool(self.cfg.sfx.enabled and cue and cue.approved)

    async def render(self, name: str, *, duration_s: float | None = None,
                     seed: int | None = None) -> dict:
        """Render cue `name`. A cue's fixed seed wins over `seed`. Raises on any
        failure; callers treat that as 'this host airs dry'."""
        cue = self.cfg.sfx.cues.get(name)
        if not cue or not cue.approved or not self.cfg.sfx.enabled:
            raise ValueError(f"sfx cue {name!r} is not approved")
        dur = float(duration_s or cue.duration_s)
        use_seed = cue.seed if cue.seed is not None else seed
        t0 = time.monotonic()
        wav = await self.client.generate(cue.prompt, dur, use_seed)
        data, sr = sf.read(io.BytesIO(wav))
        x = np.asarray(data, dtype=np.float32)
        got = len(x) / float(sr)
        # the model's output is often full-scale, so clipping is not graded
        # here: two-pass loudnorm with a true-peak ceiling fixes the level.
        if not duration_bounds(got, 0.5 * dur, 1.5 * dur + 0.1):
            raise ValueError(f"sfx duration {got:.2f}s, asked {dur:.2f}s")
        if check_silence(x, self.cfg.audio.silence_db):
            raise ValueError("sfx mostly silent")
        stamp = int(time.time() * 1000)
        tmp = self.media_dir / f"_sfx_tmp_{stamp}.wav"
        dst = self.media_dir / f"sfx_{name}_{stamp}.flac"
        tmp.write_bytes(wav)
        try:
            meta = normalize.normalize(tmp, dst, self.cfg)
        finally:
            tmp.unlink(missing_ok=True)
        out_dur = cast(float, meta["duration_s"])
        log.info("sfx.produced", extra={
            "cue": name, "duration_s": out_dur, "seed": use_seed,
            "duration_ms": round((time.monotonic() - t0) * 1000)})
        return {"media_path": str(dst), "duration_s": out_dur,
                "sample_rate": meta["sample_rate"], "channels": meta["channels"],
                "meta": {"cue": name, "prompt": cue.prompt, "seed": use_seed,
                         "evergreen": cue.evergreen}}


# --------------------------------------------------------------------------- #
# Script cues (LLM output is untrusted: validate, discard what doesn't fit)
# --------------------------------------------------------------------------- #
class _CueIn(BaseModel):
    cue: str
    after_sentence: int


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE.split(text.strip()) if s]


def parse_cues(obj: dict, text: str, allowed: set[str], joke_ok: bool,
               max_cues: int = 3) -> tuple[list[dict], int | None]:
    """Validate the `sfx` / `joke_after_sentence` fields of a copy JSON.

    Returns (cues, joke_sentence). Sentence numbers are 1-based and must be in
    range; unknown cue names, malformed entries and duplicates are dropped."""
    n = len(sentences(text))
    cues: list[dict] = []
    raw = obj.get("sfx")
    if isinstance(raw, list):
        for entry in raw[:max_cues]:
            try:
                c = _CueIn.model_validate(entry)
            except ValidationError:
                continue
            if c.cue in allowed and 1 <= c.after_sentence <= n and not any(
                    x["after_sentence"] == c.after_sentence for x in cues):
                cues.append({"cue": c.cue, "after_sentence": c.after_sentence})
    joke: int | None = None
    j = obj.get("joke_after_sentence")
    if joke_ok and isinstance(j, int) and not isinstance(j, bool) and 1 <= j <= n:
        joke = j
    return cues, joke


def beat_offset(text: str, after_sentence: int, duration_s: float,
                lead_s: float) -> float:
    """Coarse beat time (s into the clip) right after sentence `after_sentence`,
    by word share of the speech. Deliberately approximate: an intentional beat
    with a little drift is the target, not syllable accuracy (SFX.md §5)."""
    sents = sentences(text)
    total = sum(len(s.split()) for s in sents) or 1
    upto = sum(len(s.split()) for s in sents[:after_sentence])
    speech = max(duration_s - lead_s, 0.0)
    return round(lead_s + speech * upto / total, 3)
