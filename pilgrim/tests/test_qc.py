"""QC unit tests (RADIO.md §8.1, §15). Known-bad audio is rejected, good passes."""
from __future__ import annotations

import numpy as np

from pilgrim.audio.qc import (check_clipping, check_internal_dropout, check_silence,
                      check_truncation, grade_audio)

SR = 24000


def _tone(dur, freq=440, amp=0.3):
    n = int(SR * dur)
    t = np.linspace(0, dur, n, endpoint=False)
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def test_silent_rejected():
    x = np.zeros(SR, dtype=np.float32)
    v = grade_audio(x, SR, duration_s=1.0, min_dur=0.5, max_dur=60, kind="voice")
    assert not v.ok


def test_good_pass():
    x = _tone(8.0)
    v = grade_audio(x, SR, duration_s=8.0, min_dur=0.5, max_dur=60, kind="voice")
    assert v.ok


def test_duration_out_of_bounds():
    x = _tone(2.0)
    v = grade_audio(x, SR, duration_s=2.0, min_dur=3.0, max_dur=60, kind="voice")
    assert not v.ok


def test_clipping_detected():
    x = np.full(SR, 1.0, dtype=np.float32)
    assert check_clipping(x)


def test_internal_dropout_detected():
    x = _tone(5.0)
    x[int(SR * 1.0): int(SR * 3.5)] = 0  # 2.5s silence inside
    assert check_internal_dropout(x, SR, max_gap_s=1.0, silence_db=-50)


def test_truncation_detected_song():
    # loud all the way to the last sample (no decay) => abrupt ending
    n = SR
    x = np.full(n, 0.5, dtype=np.float32)
    assert check_truncation(x, SR)
