"""Fakes for tests (RADIO.md §15, AGENTS §2). No real backends are ever called."""
from __future__ import annotations

import io
import math

import numpy as np
import soundfile as sf


def tone_wav(duration_s: float, sr: int = 24000, channels: int = 1,
             freq: float = 440.0, silence: bool = False) -> bytes:
    n = int(sr * duration_s)
    t = np.linspace(0, duration_s, n, endpoint=False)
    x = (0.2 * np.sin(2 * math.pi * freq * t)).astype(np.float32)
    if silence:
        x *= 0
    if channels > 1:
        x = np.stack([x, x], axis=-1)
    buf = io.BytesIO()
    sf.write(buf, x, sr, format="WAV")
    return buf.getvalue()


class FakeLLM:
    def __init__(self, responses=None):
        # responses: dict keyed by a substring, or callable(role)->dict
        self.responses = responses or {}
        self.calls = 0
        self.fail_after = None
        self.auto = "canned"

    async def chat_json(self, model, system, user, max_tokens=800):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("fake llm failure")
        for key, val in self.responses.items():
            if key and key in user:
                return val
        return {
            "text": "Welcome to Pilgrim Dot Farm, where the pickles never sleep.",
            "est_duration_s": 8.0, "evergreen": True,
        }

    async def close(self):
        pass


class FakeKokoro:
    def __init__(self, sr=24000, silent=False):
        self.sr = sr
        self.silent = silent
        self.calls = []

    async def synth(self, text, voice, speed=1.0):
        self.calls.append((text, voice, speed))
        words = max(1, len(text.split()))
        dur = words * 0.45 / speed
        return tone_wav(min(dur, 30), self.sr, silence=self.silent)

    async def voices(self):
        return ["am_liam", "am_michael", "af_aoede"]

    async def close(self):
        pass


class FakeMinimax:
    def __init__(self, sr=44100, channels=2, fail=False):
        self.sr = sr
        self.channels = channels
        self.fail = fail

    async def generate(self, brief):
        if self.fail:
            raise RuntimeError("fake mlx down")
        dur = float(brief.get("target_duration_s") or 45)  # fixed default (OVERHAUL 3.1)
        return {"path": None, "wall_s": dur * 0.1}

    async def music(self, brief):
        if self.fail:
            raise RuntimeError("fake mlx down")
        dur = float(brief.get("target_duration_s") or 45)  # fixed default (OVERHAUL 3.1)
        return tone_wav(dur, self.sr, self.channels)

    async def close(self):
        pass


class FakeSfx:
    """Fake of SfxClient (SFX.md §7.5): a 48 kHz mono tone of exactly the
    requested duration, like MOSS-SoundEffect (docs/backends.md §7). `fail=True` injects outages."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, float, int | None]] = []

    async def generate(self, prompt: str, duration_s: float, seed: int | None) -> bytes:
        self.calls.append((prompt, duration_s, seed))
        if self.fail:
            raise RuntimeError("fake sfx down")
        return tone_wav(duration_s, 48000, 1, freq=880.0)

    async def close(self) -> None:
        pass
