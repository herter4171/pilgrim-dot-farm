"""Normalize test (RADIO.md §8.2): two-pass loudnorm -> FLAC near -16 LUFS, native rate."""
from __future__ import annotations

import shutil
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from pilgrim.audio import normalize

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None,
                                reason="ffmpeg not on PATH")

SR = 24000


def _write_wav(path: Path, dur: float = 6.0):
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    x = (0.35 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    sf.write(str(path), x, SR)
    return path


def test_normalize_to_flac_near_target(cfg, tmp_path):
    src = _write_wav(tmp_path / "in.wav")
    dst = tmp_path / "out.flac"
    meta = normalize.normalize(src, dst, cfg)
    # FLAC, native-ish rate preserved (allowed to stay 24000)
    assert meta["channels"] in (1, 2)
    assert dst.suffix == ".flac"
    # verify measured loudness within tolerance of -16 LUFS
    ok = normalize.verify_loudness(dst, cfg, tol=0.6)
    assert ok, "expected ~-16 LUFS, got out of tolerance"
    assert meta["sample_rate"] == SR  # native rate preserved


def test_flac_decodable(cfg, tmp_path):
    src = _write_wav(tmp_path / "in.wav", 3.0)
    dst = tmp_path / "out.flac"
    normalize.normalize(src, dst, cfg)
    data, sr = sf.read(str(dst))
    assert sr == SR
    assert len(data) > 0
