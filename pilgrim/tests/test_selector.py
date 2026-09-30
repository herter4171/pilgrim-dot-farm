"""RandomSelector constraint tests (RADIO.md §5.2)."""
from __future__ import annotations

import pytest
from pilgrim.config import RNG
from pilgrim.selector import PlayoutState, RandomSelector


def make_state(cfg, available=None, recent=None, secs=None, news_valid=True,
               gravity="normal"):
    st = PlayoutState(cfg)
    st.available = {"song": True, "dj_talk": True, "commercial_break": True,
                    "liner": True, "news": news_valid}
    if available is not None:
        st.available.update(available)
    st.recent_types = recent or []
    st.secs_since = dict(secs or {})
    st.news_valid = news_valid
    st.news_gravity = gravity
    return st


def test_force_song_after_two_non_song(cfg):
    sel = RandomSelector(RNG(1), cfg)
    st = make_state(cfg, recent=["liner", "dj_talk"])
    assert sel.choose_next(st) == "song"


def test_news_blocked_when_invalid(cfg):
    sel = RandomSelector(RNG(1), cfg)
    st = make_state(cfg, news_valid=False, available={"news": True})
    # news must not be chosen
    for _ in range(50):
        assert sel.choose_next(st) != "news"


def test_news_spacing(cfg):
    sel = RandomSelector(RNG(2), cfg)
    st = make_state(cfg, secs={"news": 100})  # below min spacing 1200
    for _ in range(50):
        assert sel.choose_next(st) != "news"


def test_dj_not_after_dj_or_news(cfg):
    sel = RandomSelector(RNG(3), cfg)
    for last in ("dj_talk", "news"):
        st = make_state(cfg, recent=[last])
        for _ in range(50):
            assert sel.choose_next(st) != "dj_talk"


def test_interjection_between_songs(cfg):
    """A song on the tail must never be followed by another song (at least one
    interjection between songs)."""
    sel = RandomSelector(RNG(5), cfg)
    st = make_state(cfg, recent=["song"])  # a song just aired
    for _ in range(200):
        assert sel.choose_next(st) != "song"


def test_interjection_fallback_prefers_nonsong(cfg):
    """Even in the no-inventory fallback, a song-on-tail must pick a non-song
    interjection whenever any non-song stock exists (no back-to-back songs)."""
    sel = RandomSelector(RNG(8), cfg)
    st = make_state(cfg)
    # Only songs are available after discarding (simulating all non-song out of stock),
    # but a liner still has stock behind it -> fallback must choose the liner.
    st.available = {"song": True, "liner": True,
                    "dj_talk": False, "commercial_break": False, "news": False}
    st.recent_types = ["song"]
    for _ in range(50):
        assert sel.choose_next(st) != "song"

    # If literally only a song has stock, the fallback still emits it (nothing else
    # can interject); with any non-song stock present the rule must hold above.
    st2 = make_state(cfg)
    st2.available = {"song": True, "liner": False, "dj_talk": False,
                     "commercial_break": False, "news": False}
    st2.recent_types = ["song"]
    assert sel.choose_next(st2) == "song"


def test_song_allowed_after_interjection(cfg):
    """Once an interjection has aired, another song may follow."""
    sel = RandomSelector(RNG(6), cfg)
    st = make_state(cfg, recent=["liner"])  # interjection just aired
    # song is a valid draw again (it must appear at least once across draws)
    seen = {sel.choose_next(st) for _ in range(200)}
    assert "song" in seen


def test_no_silence_raise_with_empty(cfg):
    sel = RandomSelector(RNG(4), cfg)
    st = make_state(cfg)
    st.available = {k: False for k in st.available}
    with pytest.raises(ValueError):
        sel.choose_next(st)


def test_deterministic_seed(cfg):
    a = RandomSelector(RNG(7), cfg)
    b = RandomSelector(RNG(7), cfg)
    outs_a = [a.choose_next(make_state(cfg)) for _ in range(30)]
    outs_b = [b.choose_next(make_state(cfg)) for _ in range(30)]
    assert outs_a == outs_b


def test_available_types_respected(cfg):
    sel = RandomSelector(RNG(5), cfg)
    st = make_state(cfg, available={"liner": True, "song": False}, recent=[])
    st.available["song"] = False
    for _ in range(50):
        assert sel.choose_next(st) != "song"


def test_seed_is_independent_of_python_hash_seed():
    """A restart with the same station seed must reproduce selection (§15)."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    script = """
from pilgrim.config import RNG, load_config
from pilgrim.selector import PlayoutState, RandomSelector
cfg = load_config()
state = PlayoutState(cfg)
state.available = dict.fromkeys(state.available, True)
state.news_valid = True
selector = RandomSelector(RNG(7), cfg)
print([selector.choose_next(state) for _ in range(100)])
"""
    programs = [subprocess.check_output(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "PYTHONHASHSEED": str(seed)}, text=True,
        timeout=30,
    ) for seed in (1, 2)]
    assert programs[0] == programs[1]


def test_no_liner_after_liner_when_songs_run_dry(cfg):
    """Song drought: liners must not stack while commercials are ready."""
    sel = RandomSelector(RNG(3), cfg)
    st = make_state(cfg, available={"song": False, "dj_talk": False},
                    recent=["commercial_break", "liner"], news_valid=False)
    for _ in range(50):
        assert sel.choose_next(st) == "commercial_break"


def test_liner_after_liner_only_when_nothing_else(cfg):
    sel = RandomSelector(RNG(3), cfg)
    st = make_state(cfg, available={"song": False, "dj_talk": False,
                                    "commercial_break": False},
                    recent=["liner"], news_valid=False)
    assert sel.choose_next(st) == "liner"
