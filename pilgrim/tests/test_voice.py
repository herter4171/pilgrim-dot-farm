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
    """9 s (duration-tolerable for target 18 s) with 50 words (~5.6 w/s, a
    truncation, well above legit Kokoro reads of 3.6-4.0 w/s): the speech-rate
    gate rejects and deletes the flac."""
    vp = pipe(cfg, ShortKokoro(fixed_s=9.0), tmp_path)
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
    gap = cfg.audio.tts_join_gap_ms / 1000
    # tone chunks have no silent edges: n chunks + (n-1) fixed pauses
    assert abs(n - (0.5 * len(request_texts) + gap * (len(request_texts) - 1))) < 0.01
    asyncio.run(kc.close())


def test_synth_trims_chunk_edges_so_joins_are_not_dropouts(cfg):
    """Kokoro pads each chunk with silence; joined raw, two tails + a lead-in
    exceeded QC's 1 s dropout limit. After trimming, a join is one fixed gap."""
    import httpx
    import numpy as np
    import soundfile as sf
    from pilgrim.audio.qc import check_internal_dropout
    from pilgrim.pipelines.voice import KokoroClient

    def padded_tone():
        sr = 24000
        t = np.arange(int(sr * 1.0)) / sr
        tone = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
        pad = np.zeros(int(sr * 0.6), dtype=np.float32)
        buf = io.BytesIO()
        sf.write(buf, np.concatenate([pad, tone, pad]), sr, format="WAV")
        return buf.getvalue()

    kc = KokoroClient(cfg)
    kc._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=padded_tone())),
        timeout=10)
    wav = asyncio.run(kc.synth("word. " * 120, "am_liam"))
    x, sr = sf.read(io.BytesIO(wav))
    assert not check_internal_dropout(x, sr, max_gap_s=1.0, silence_db=cfg.audio.silence_db)
    asyncio.run(kc.close())

# ---------------------------------------------------------------------------
# tts_cleanup number-to-words (RADIO §6.3). Regression: the tens-word index was
# off by 2, so every 20-99 was spoken wrong (91 -> "seventy one", 30 ->
# "nineteen", 42 -> "twenty two"), which garbles usernames like jwhh91 on air.
# ---------------------------------------------------------------------------


def test_cleanup_numbers_spoken_correctly():
    from pilgrim.pipelines.voice import tts_cleanup
    cases = {
        "1": "one",
        "19": "nineteen",
        "21": "twenty one",
        "30": "thirty",
        "42": "forty two",
        "91": "ninety one",
        "99": "ninety nine",
    }
    for n, want in cases.items():
        got = tts_cleanup(f"it is number {n}")
        assert got == f"it is number {want}", (n, got)


def test_cleanup_username_digits_not_mangled():
    """A username's trailing two-digit number must spell its real value."""
    from pilgrim.pipelines.voice import tts_cleanup
    got = tts_cleanup("upvote posts by reddit user jwhh91")
    assert got == "upvote posts by reddit user jwhhninety one", got


def test_cleanup_large_numbers_left_alone():
    from pilgrim.pipelines.voice import tts_cleanup
    got = tts_cleanup("there are 1234 songs")
    assert got == "there are 1234 songs", got
