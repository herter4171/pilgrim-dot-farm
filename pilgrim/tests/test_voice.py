"""OVERHAUL 2.1 — voice speech-rate QC gate. Fakes only; ffmpeg via make test PATH."""
from __future__ import annotations

import asyncio

import pytest
from pilgrim.pipelines.voice import VoicePipeline
from pilgrim.tests.fakes import FakeKokoro, FakeLLM, tone_wav

# 70 words — the kind of script that produces suspiciously short audio when
# the TTS backend truncates.
LONG_COPY = " ".join(["The quick farmer counted forty pickles by the barn door."] * 5)


class ShortKokoro:
    """Fixed-length clip regardless of text length (simulates truncation)."""

    def __init__(self, sr: int = 24000, fixed_s: float = 2.0) -> None:
        self.sr = sr
        self.fixed_s = fixed_s

    async def synth(self, text: str, voice: str, speed: float = 1.0) -> bytes:
        return tone_wav(self.fixed_s, self.sr)

    async def voices(self):
        return ["am_liam"]


def pipe(cfg, kokoro, tmp_path, *, copy=LONG_COPY,
         est: float = 18.0) -> VoicePipeline:
    from typing import Any
    llm: Any = FakeLLM(responses={"Write the copy": {
        "text": copy, "est_duration_s": est, "evergreen": True}})
    return VoicePipeline(cfg, llm, kokoro, db=None,
                         media_dir=tmp_path, prompts={"voice": "be the dj"})


def test_truncated_render_rejected_no_flac_left(cfg, tmp_path):
    """2 s of tone for a 70-word script -> produce_item raises, no .flac left."""
    vp = pipe(cfg, ShortKokoro(fixed_s=2.0), tmp_path)
    with pytest.raises(ValueError):
        asyncio.run(vp.produce_item("dj_talk", 18.0))
    assert not list(tmp_path.glob("*.flac"))


def test_speech_rate_gate_fires_within_duration_tolerance(cfg, tmp_path):
    """12 s (duration-tolerable for target 18 s) with 70 words: too many words
    per second -> the speech-rate gate rejects and deletes the flac."""
    vp = pipe(cfg, ShortKokoro(fixed_s=12.0), tmp_path)
    with pytest.raises(ValueError, match="speech rate"):
        asyncio.run(vp.produce_item("dj_talk", 18.0))
    assert not list(tmp_path.glob("*.flac"))


def test_normal_rate_passes_and_meta_carries_words_per_s(cfg, tmp_path):
    """~0.45 s/word (bundled FakeKokoro) -> passes; meta has words_per_s in range."""
    vp = pipe(cfg, FakeKokoro(), tmp_path)
    item = asyncio.run(vp.produce_item("dj_talk", 18.0))
    wps = item["meta"]["words_per_s"]
    assert cfg.audio.min_words_per_s <= wps <= cfg.audio.max_words_per_s
    assert item["meta"]["words"] == len(LONG_COPY.split())
    assert list(tmp_path.glob("*.flac"))
