"""Song pipeline (RADIO.md §6.2): brief -> MiniMax (mlx-serve) -> QC -> normalize.

mlx-serve is SYNCHRONOUS: POST /v1/audio/music-generations returns the raw WAV
inline (no polling). Compute-expensive; never call outside a worker.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import numpy as np
import soundfile as sf

from typing import Optional

from pilgrim.audio import normalize
from pilgrim.audio.qc import grade_audio
from pilgrim.config import Config
from pilgrim.pipelines.llm import LLM, LLMError

log = logging.getLogger("radio.songs")


class SongPipeline:
    def __init__(self, cfg: Config, llm: LLM, media_dir: Optional[Path] = None, prompts=None):
        self.cfg = cfg
        self.llm = llm
        self.media_dir = media_dir or (Path(__file__).resolve().parent.parent / cfg.library.dir)
        self.prompts = prompts or {}
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(1800.0, connect=10.0))
        self.generation_s: list[tuple[float, float]] = []  # (duration_s, wall_clock_s)

    async def close(self) -> None:
        await self._client.aclose()

    async def brief(self, previous_genres: list) -> dict:
        import random
        prompt = self.prompts.get("song_brief")
        if not prompt:
            raise LLMError("song brief prompt template missing")
        tmin, tmax = self.cfg.songs.target_duration_s
        genres = ", ".join(self.cfg.songs.genres.keys())
        avoid = ", ".join(previous_genres[-self.cfg.playout.genre_no_repeat:]) or "none"
        schema = ('Return JSON only: {"title": str, "artist": str, "genre": str, '
                  '"style_prompt": str, "lyrics": str, "target_duration_s": number}')
        user = (f"{prompt}\n\nGenres to pick from: {genres}\nAvoid genres (recently aired): "
                f"{avoid}\nTarget duration {tmin}-{tmax}s.\n{schema}")
        obj = await self.llm.chat_json(self.cfg.models.briefs, prompt, user, max_tokens=1200)
        for k in ("title", "artist", "genre", "style_prompt"):
            obj[k] = str(obj.get(k, "")).strip()
        obj["lyrics"] = str(obj.get("lyrics", "")).strip()
        if not obj["title"] or not obj["style_prompt"]:
            raise LLMError("song brief incomplete")
        obj["target_duration_s"] = int(obj.get("target_duration_s") or tmin)
        return obj

    async def generate(self, brief: dict) -> dict:
        """Synchronous mlx call. Returns dict with tmp wav + wall-clock duration."""
        url = self.cfg.hosts.mlx_serve.rstrip("/") + "/v1/audio/music-generations"
        payload = {"prompt": brief["style_prompt"]}
        if brief.get("lyrics"):
            payload["lyrics"] = brief["lyrics"]
        else:
            payload["instrumental"] = True
        if brief.get("target_duration_s"):
            payload["duration_s"] = brief["target_duration_s"]
        t0 = time.monotonic()
        resp = await self._client.post(url, json=payload)
        wall = time.monotonic() - t0
        if resp.status_code != 200:
            raise LLMError(f"mlx http {resp.status_code}: {resp.text[:300]}")
        tmp = self.media_dir / "_song_tmp.wav"
        tmp.write_bytes(resp.content)
        return {"path": tmp, "wall_s": wall}

    async def produce_song(self, brief: dict) -> dict:
        gen = await self.generate(brief)
        self.generation_s.append((0.0, gen["wall_s"]))
        data, sr = sf.read(str(gen["path"]))
        x = np.asarray(data, dtype=np.float32)
        duration = float(len(x)) / sr
        self.generation_s[-1] = (duration, gen["wall_s"])
        tmin, tmax = self.cfg.songs.target_duration_s
        verdict = grade_audio(x, sr, duration_s=duration, min_dur=5, max_dur=600,
                              kind="song", max_gap_s=2.0, silence_db=self.cfg.audio.silence_db)
        if not verdict.ok:
            gen["path"].unlink(missing_ok=True)
            raise ValueError(f"song QC failed: {verdict.reasons} (len {duration:.0f}s)")
        dst = self.media_dir / f"song_{int(time.time()*1000)}.flac"
        meta = normalize.normalize(gen["path"], dst, self.cfg)
        gen["path"].unlink(missing_ok=True)
        return {
            "type": "song", "media_path": str(dst), "duration_s": meta["duration_s"],
            "sample_rate": meta["sample_rate"], "channels": meta["channels"],
            "title": brief["title"], "artist": brief["artist"], "genre": brief["genre"],
            "evergreen": True, "fresh": True,
            "meta": {"brief": brief, "wall_s": round(gen["wall_s"], 1)},
        }

    @property
    def generation_rate(self) -> float:
        """Average realtime factor (audio seconds / wall seconds)."""
        if not self.generation_s:
            return 0.0
        audio = sum(d for d, _ in self.generation_s)
        wall = sum(w for _, w in self.generation_s)
        return audio / wall if wall else 0.0
