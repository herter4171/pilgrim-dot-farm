"""Normalize test (RADIO.md §8.2): two-pass loudnorm -> 48 kHz stereo MP3 near
-16 LUFS with a consistent gapless (LAME/Info) header."""
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


def test_normalize_to_mp3_near_target(cfg, tmp_path):
    src = _write_wav(tmp_path / "in.wav")
    dst = tmp_path / "out.mp3"
    meta = normalize.normalize(src, dst, cfg)
    # WebKit only honours MP3 gapless info on stereo MPEG-1 (docs/backends.md §9)
    assert meta["channels"] == 2
    assert meta["sample_rate"] == 48000
    assert meta["codec"] == "mp3"
    assert meta["kbps"] == cfg.audio.mp3_mono_source_kbps  # mono source
    ok = normalize.verify_loudness(dst, cfg, tol=0.6)
    assert ok, "expected ~-16 LUFS, got out of tolerance"
    assert not list(tmp_path.glob("_norm_*")), "PCM intermediate left behind"


def test_mp3_decodable_and_gapless(cfg, tmp_path):
    src = _write_wav(tmp_path / "in.wav", 3.0)
    dst = tmp_path / "out.mp3"
    meta = normalize.normalize(src, dst, cfg)
    data, sr = sf.read(str(dst))
    assert sr == 48000
    assert data.shape[1] == 2
    # libsndfile trims delay/padding: decoded length == the true sample count
    assert len(data) == meta["frames"]
    gap = normalize.gapless_info(dst)
    assert gap["delay"] == meta["enc_delay"] > 0
    assert gap["delay"] + meta["frames"] + gap["padding"] == gap["total_frames"]


def test_encode_mp3_exact_length_and_stereo_bitrate(cfg, tmp_path):
    """encode_mp3 (backfill path) keeps the exact sample count of a 48 kHz
    stereo master and uses the stereo bitrate."""
    n = 48000 * 2 + 123
    x = (0.3 * np.sin(2 * np.pi * 220 * np.arange(n) / 48000)).astype(np.float32)
    src = tmp_path / "m.flac"
    sf.write(str(src), np.stack([x, x], axis=1), 48000)
    meta = normalize.encode_mp3(src, tmp_path / "m.mp3", cfg, cfg.audio.mp3_kbps)
    assert meta["frames"] == n
    assert meta["kbps"] == cfg.audio.mp3_kbps


def test_gapless_info_rejects_headerless(tmp_path):
    p = tmp_path / "junk.mp3"
    p.write_bytes(b"\xff\xfb\x90\x00" + b"\x00" * 400)
    with pytest.raises(normalize.NormalizeError):
        normalize.gapless_info(p)


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
    dst = tmp_path / "pause_out.mp3"
    sf.write(str(src), x, SR)
    meta = normalize.normalize(src, dst, cfg)
    data, _ = sf.read(str(dst))
    data = data.mean(axis=1)
    # second tone must survive: significant energy in the second half
    half = data.shape[0] // 2
    assert float(np.max(np.abs(data[half:]))) > 0.05, "second half was truncated away"
    assert meta["duration_s"] >= (n0 + n_pause + n0) / SR - 0.2  # most of both tones kept


def test_fade_out_softens_abrupt_ending(cfg, tmp_path):
    """OVERHAUL 3.2: a 20 s constant-level tone normalized with fade_out_s=2.5
    must end ~12 dB below its own median level (a proper fade-out, not a hard
    cutoff)."""
    dur = 20.0
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    x = (0.4 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)
    src = tmp_path / "abrupt_in.wav"
    dst = tmp_path / "abrupt_out.mp3"
    sf.write(str(src), x, SR)
    normalize.normalize(src, dst, cfg, fade_out_s=2.5)
    data, osr = sf.read(str(dst))
    mono = data.mean(axis=1) if data.ndim > 1 else data
    nframes = len(mono) // osr
    frames = mono[: nframes * osr].reshape(nframes, osr).astype(np.float64)
    median = float(np.median(np.sqrt(np.mean(frames ** 2, axis=1)))) or 1e-9
    tail = mono[-int(osr * 0.5):].astype(np.float64)
    tail_db = 20 * np.log10(float(np.sqrt(np.mean(tail ** 2))) / median + 1e-12)
    assert tail_db <= -12.0
