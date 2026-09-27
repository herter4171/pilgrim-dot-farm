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


def test_normalize_preserves_second_half_after_internal_pause(cfg, tmp_path):
    # Regression: silenceremove could stop output at the FIRST >=250 ms quiet
    # segment, truncating any spot/song that has a normal speech pause in the
    # middle. A tone + pause + tone must keep BOTH tones after normalization.
    n0 = int(SR * 0.5)
    n_pause = int(SR * 0.3)
    t1 = np.linspace(0, 0.5, n0, endpoint=False)
    t2 = np.linspace(0, 0.5, n0, endpoint=False)
    x = np.concatenate([
        0.4 * np.sin(2 * np.pi * 330 * t1),          # first tone
        np.zeros(n_pause),                            # internal pause (>0.25s)
        0.4 * np.sin(2 * np.pi * 440 * t2),          # second tone
    ]).astype(np.float32)
    src = tmp_path / "pause_in.wav"
    dst = tmp_path / "pause_out.flac"
    sf.write(str(src), x, SR)
    meta = normalize.normalize(src, dst, cfg)
    data, _ = sf.read(str(dst))
    # second tone must survive: significant energy in the second half
    half = data.shape[0] // 2
    assert float(np.max(np.abs(data[half:]))) > 0.05, "second half was truncated away"
    assert meta["duration_s"] >= (n0 + n_pause + n0) / SR - 0.2  # most of both tones kept
