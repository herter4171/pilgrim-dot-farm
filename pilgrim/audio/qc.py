"""Audio QC — the "grader" (RADIO.md §8.1).

Rejects items that are silent, clipped, truncated, or out of duration bounds.
Pure functions over a numpy audio array + metadata. No backends here.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class QCVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)
    abrupt_end: bool = False  # song ends on loud energy (OVERHAUL 3.2): recorded, not fatal

    def add(self, reason: str) -> None:
        self.reasons.append(reason)
        self.ok = False


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def duration_bounds(duration_s: float, min_dur: float, max_dur: float) -> bool:
    return min_dur <= duration_s <= max_dur


def check_clipping(x: np.ndarray, tol: float = 1e-4) -> bool:
    """True if a large fraction of samples saturate at full scale (clipped)."""
    if x.size == 0:
        return True
    frac = float(np.mean(np.abs(x) > (1.0 - tol)))
    return frac > 0.02


def check_silence(x: np.ndarray, silence_db: float = -50.0, min_voice_frac: float = 0.6) -> bool:
    """True if the clip is mostly silent (below -silence_db RMS)."""
    r = _rms(x)
    threshold = 10 ** (silence_db / 20.0)
    return r < threshold * min_voice_frac


def check_internal_dropout(x: np.ndarray, sr: int, max_gap_s: float,
                           silence_db: float = -50.0) -> bool:
    """True if an internal dropout (long silence run) exists longer than max_gap_s."""
    amp = np.abs(x.astype(np.float64))
    if amp.ndim > 1:
        amp = amp.max(axis=-1)  # multi-channel (e.g. stereo songs): quiet only if every channel is
    threshold = 10 ** ((silence_db + 20) / 20.0)  # dropout = much quieter than ambient
    low = (amp < threshold).astype(np.int8)
    # longest run of consecutive low samples, vectorized (a Python-level per-sample
    # loop here previously raised on multi-channel audio, since `if v:` on a
    # multi-element row is ambiguous — every song generation hit this and QC
    # crashed instead of passing/failing, so no song ever reached inventory)
    padded = np.concatenate(([0], low, [0]))
    edges = np.diff(padded)
    run_lengths = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
    longest = int(run_lengths.max()) if run_lengths.size else 0
    return (longest / sr) > max_gap_s


def tail_level_db(x: np.ndarray, sr: int, tail_s: float = 0.5) -> float:
    """Tail RMS relative to the median 1-s RMS of the track, in dB (OVERHAUL 3.2).
    A song that still runs at full level in its last half-second is loud relative
    to its own body — compare against the MEDIAN 1-s frame, not the global peak,
    so a soft ballad with one chord peak isn't flagged as truncated."""
    mono = x.mean(axis=1) if x.ndim > 1 else x
    n = len(mono) // sr
    if n < 3:
        return -99.0
    frames = mono[: n * sr].reshape(n, sr).astype(np.float64)
    body = float(np.median(np.sqrt(np.mean(frames ** 2, axis=1)))) or 1e-9
    tail = mono[-int(sr * tail_s):].astype(np.float64)
    return 20 * np.log10(float(np.sqrt(np.mean(tail ** 2))) / body + 1e-12)


def check_truncation(x: np.ndarray, sr: int, threshold_db: float = -6.0) -> bool:
    """True if the last ~0.5 s stays within 6 dB of the track's median level."""
    return tail_level_db(x, sr) > threshold_db


def grade_audio(x: np.ndarray, sr: int, *, duration_s: float,
                min_dur: float, max_dur: float, kind: str,
                max_gap_s: float = 1.0, silence_db: float = -50.0) -> QCVerdict:
    """Grade a decoded track. kind in {song, voice}."""
    v = QCVerdict(ok=True)
    if not duration_bounds(duration_s, min_dur, max_dur):
        v.add(f"duration {duration_s:.1f}s outside [{min_dur},{max_dur}]")
    if check_silence(x, silence_db):
        v.add("mostly silent")
    if check_clipping(x):
        v.add("clipped")
    if check_internal_dropout(x, sr, max_gap_s, silence_db):
        v.add(f"internal dropout >{max_gap_s}s")
    if kind == "song":
        # Record an abrupt ending but NEVER fail the song for it (OVERHAUL 3.2):
        # the producer fades the last seconds instead of throwing the track away.
        v.abrupt_end = check_truncation(x, sr)
    return v
