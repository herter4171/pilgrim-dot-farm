"""Audio QC — the "grader" (RADIO.md §8.1).

Rejects items that are silent, clipped, truncated, or out of duration bounds.
Pure functions over a numpy audio array + metadata. No backends here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np


@dataclass
class QCVerdict:
    ok: bool
    reasons: List[str] = field(default_factory=list)

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
    threshold = 10 ** ((silence_db + 20) / 20.0)  # dropout = much quieter than ambient
    low = amp < threshold
    # find longest run of consecutive low samples
    longest = 0
    cur = 0
    for v in low:
        if v:
            cur += 1
            if cur > longest:
                longest = cur
        else:
            cur = 0
    return (longest / sr) > max_gap_s


def check_truncation(x: np.ndarray, sr: int, tail_s: float = 0.5,
                     ratio: float = 0.3) -> bool:
    """True if the song ends on loud energy (abrupt cutoff, no decay)."""
    n_tail = int(sr * tail_s)
    if x.shape[0] < n_tail:
        return False
    tail = x[-n_tail:].astype(np.float64)
    tail_rms = float(np.sqrt(np.mean(tail ** 2)))
    peak = float(np.max(np.abs(x)))
    if peak <= 0:
        return False
    return tail_rms > ratio * peak


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
    if kind == "song" and check_truncation(x, sr):
        v.add("abrupt/truncated ending")
    return v
