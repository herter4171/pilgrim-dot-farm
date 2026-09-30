"""QC unit tests (RADIO.md §8.1, §15). Known-bad audio is rejected, good passes."""
from __future__ import annotations

import numpy as np
from pilgrim.audio.qc import (
    check_clipping,
    check_internal_dropout,
    check_truncation,
    grade_audio,
)

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


def test_internal_dropout_stereo_song_does_not_crash():
    # Songs (MiniMax) are stereo, unlike Kokoro's mono voice clips. The dropout
    # check must not choke on a 2D array, and must find gaps only where every
    # channel is quiet.
    mono = _tone(5.0)
    x = np.stack([mono, mono], axis=-1)
    x[int(SR * 1.0): int(SR * 3.5), :] = 0  # 2.5s silence inside, both channels
    assert check_internal_dropout(x, SR, max_gap_s=1.0, silence_db=-50)


def test_internal_dropout_stereo_no_false_positive_when_only_one_channel_quiet():
    mono = _tone(5.0)
    x = np.stack([mono, mono], axis=-1)
    x[int(SR * 1.0): int(SR * 3.5), 0] = 0  # only the left channel drops out
    assert not check_internal_dropout(x, SR, max_gap_s=1.0, silence_db=-50)


def test_grade_audio_stereo_song_does_not_raise():
    # Regression: grade_audio used to raise ValueError ("truth value of an
    # array...") for any stereo (song) clip, so every song failed QC with an
    # uncaught exception and none ever reached inventory.
    mono = _tone(8.0)
    fade = np.linspace(1.0, 0.0, int(SR * 1.0)) ** 3  # decay the tail so this
    mono[-len(fade):] *= fade                          # isn't flagged as truncated
    x = np.stack([mono, mono], axis=-1)
    v = grade_audio(x, SR, duration_s=8.0, min_dur=0.5, max_dur=60, kind="song")
    assert v.ok


def test_truncation_detected_song_but_not_fatal():
    # loud all the way to the last sample (no decay) => abrupt ending recorded,
    # but the verdict stays passing (OVERHAUL 3.2: producer fades it).
    x = _tone(5.0)  # long enough for the 1-s-frame median (n>=3)
    x[:] = 0.5      # constant level to the very last sample
    assert check_truncation(x, SR)
    v = grade_audio(x, SR, duration_s=5.0, min_dur=0.5, max_dur=60, kind="song")
    assert v.ok
    assert v.abrupt_end


def test_faded_ending_not_truncated():
    # a tone with a 2 s linear fade to zero must NOT be flagged as abrupt
    x = _tone(8.0)
    fade = np.linspace(1.0, 0.0, int(SR * 2.0))
    x[-len(fade):] *= fade
    assert not check_truncation(x, SR)
    v = grade_audio(x, SR, duration_s=8.0, min_dur=0.5, max_dur=60, kind="song")
    assert v.ok
    assert not v.abrupt_end


def test_short_song_rejected_on_duration():
    # 10 s is below the config sanity floor (20 s): rejected for duration
    x = _tone(10.0)
    v = grade_audio(x, SR, duration_s=10.0, min_dur=20.0, max_dur=600, kind="song")
    assert not v.ok
    assert "duration" in " ".join(v.reasons)


def test_quiet_pause_is_not_a_dropout():
    """A 1.5 s pause at -45 dBFS (breath/room tone, a soft song passage) is
    quiet, not a dropout; only near-digital silence below silence_db-20 is."""
    x = _tone(5.0)
    x[int(SR * 1.0): int(SR * 2.5)] = 10 ** (-45 / 20)
    assert not check_internal_dropout(x, SR, max_gap_s=1.0, silence_db=-50)
