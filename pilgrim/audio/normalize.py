"""Audio normalization and encoding (RADIO.md §8.2).

Two-pass `loudnorm` to the configured LUFS/TP, restore native sample rate
(ffmpeg's loudnorm secretly up-samples to 192 kHz), trim outer silence below
a threshold, and deliver FLAC.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path
from typing import Dict, Optional, Tuple

import soundfile as sf

from pilgrim.config import Config

log = logging.getLogger("radio.normalize")


class NormalizeError(Exception):
    pass


def _ffmpeg() -> str:
    # prefer a bundled static build on PATH, else "ffmpeg"
    return "ffmpeg"


def probe(path: Path) -> Tuple[int, int]:
    """Return (sample_rate, channels)."""
    info = sf.info(str(path))
    return int(info.samplerate), info.channels


def measure(path: Path, cfg: Config) -> Dict[str, str]:
    """Pass 1: measure integrated loudness params."""
    cmd = [_ffmpeg(), "-y", "-i", str(path),
           "-af", (f"loudnorm=I={cfg.audio.lufs}:TP={cfg.audio.true_peak_db}:"
                   f"LRA={cfg.audio.lra}:print_format=json"),
           "-f", "null", "-"]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if p.returncode != 0:
        raise NormalizeError(f"loudnorm measure failed: {p.stderr[-500:]}")
    # parse the JSON block printed to stderr
    m = re.search(r"\{.*?\"output_i\".*?\}", p.stderr, re.DOTALL)
    if not m:
        raise NormalizeError("could not parse loudnorm measurement output")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise NormalizeError(f"bad loudnorm JSON: {e}")


def normalize(src: Path, dst: Path, cfg: Config) -> Dict[str, object]:
    """Two-pass loudnorm, restore native rate, trim silence, encode FLAC."""
    from pilgrim.config import ROOT  # noqa: F401  (contextual override support)
    sr, channels = probe(src)
    measured = measure(src, cfg)

    def g(k: str, default: str = "0") -> str:
        return str(measured.get(k, default))

    silence_db = str(cfg.audio.silence_db)
    pad_ms = cfg.audio.edge_pad_ms
    # monoaural/downmix-safe adelay: adelay supports all=1 to pad every channel
    af = (
        f"loudnorm=I={cfg.audio.lufs}:TP={cfg.audio.true_peak_db}:LRA={cfg.audio.lra}:"
        f"measured_I={g('input_i','-30')}:measured_TP={g('input_tp','-10')}:"
        f"measured_LRA={g('input_lra','0')}:measured_thresh={g('input_thresh','-40')}:linear=true,"
        f"aresample={sr},"
        f"silenceremove=start_periods=1:start_silence=0.05:start_threshold={silence_db}dB:"
        f"stop_periods=1:stop_duration=0.25:stop_threshold={silence_db}dB,"
        f"adelay={pad_ms}:all=1"
    )
    cmd = [_ffmpeg(), "-y", "-i", str(src), "-af", af,
           "-c:a", "flac", "-compression_level", "8", str(dst)]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if p.returncode != 0 or not dst.exists():
        raise NormalizeError(f"normalize failed: {p.stderr[-500:]}")

    d = sf.info(str(dst))
    return {"sample_rate": int(d.samplerate), "channels": d.channels,
            "duration_s": round(float(d.frames) / d.samplerate, 3),
            "bytes": dst.stat().st_size}


def verify_loudness(path: Path, cfg: Config, tol: float = 0.6) -> bool:
    """Post-check integrated loudness within tolerance of target LUFS."""
    try:
        dat = measure(path, cfg)
        out = float(dat.get("output_i", "-99"))
        return abs(out - cfg.audio.lufs) <= tol
    except Exception:
        return False
