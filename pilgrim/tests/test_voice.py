"""OVERHAUL 2.1 — voice speech-rate QC gate. Fakes only; ffmpeg via make test PATH."""
from __future__ import annotations

import asyncio
import io
import re

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


def test_chunk_text_limits_chunk_size():
    from pilgrim.pipelines.voice import KokoroClient
    text = ("The quick farmer counted forty pickles by the barn door. "
            "Then he went to town and bought a new tractor. ") * 8  # ~50 char sentences * 8
    chunks = KokoroClient._chunk_text(text, max_chars=200)
    assert len(chunks) > 1, "long text must split into several chunks"
    assert all(len(c) <= 200 for c in chunks)
    # re.split drops the whitespace that follows sentence punctuation, so join
    # reconstructs the text modulo trailing whitespace; all words must survive.
    joined = re.sub(r"\s+", " ", " ".join(chunks)).strip()
    orig = re.sub(r"\s+", " ", text).strip()
    assert joined == orig


def test_synth_long_text_sends_multiple_requests_and_concatenates(cfg):
    import httpx
    import soundfile as sf
    from pilgrim.pipelines.voice import KokoroClient

    request_texts: list[str] = []

    def handler(request):
        request_texts.append(request.url.params["text"])
        return httpx.Response(200, content=tone_wav(0.5))

    transport = httpx.MockTransport(handler)
    kc = KokoroClient(cfg)
    kc._client = httpx.AsyncClient(transport=transport, timeout=10)

    long_text = "word. " * 120  # far more than 200 chars, no real sentences
    wav = asyncio.run(kc.synth(long_text, "am_liam"))
    assert len(request_texts) > 1, "long text must be sent as several requests"
    assert all(len(t) <= 200 for t in request_texts)
    x, sr = sf.read(io.BytesIO(wav))
    n = len(x) / sr
    assert abs(n - 0.5 * len(request_texts)) < 0.01  # chunks concatenated
    asyncio.run(kc.close())
