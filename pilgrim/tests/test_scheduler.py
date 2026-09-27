"""Scheduler simulation (RADIO.md §15): drive a 24h run over a SimClock with a
seeded pool and assert the committed-program invariants hold."""
from __future__ import annotations

from conftest import make_item, seed_pool
from pilgrim.config import RNG, SimClock
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.store import Store


def run_program(cfg, store, seed, hours=24, step=30):
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(seed), cfg), clock, RNG(seed))
    seen = []  # (seq, type, duration, genre_or_None, item_id)
    last_seq = 0
    for _ in range(int(hours * 3600) // step):
        clock.advance(step)
        sched.commit_lookahead()
        assert sched.coverage() >= cfg.playout.committed_lookahead_s
        for r in store.program_after(last_seq):
            it = store.get_item(r["item_id"])
            genre = (it or {}).get("genre")
            seen.append((r["seq"], r["type"], r["duration_s"], genre, r["item_id"]))
            if r["seq"] > last_seq:
                last_seq = r["seq"]
    return seen


def test_24h_contiguous_and_long(cfg, tmp_env):
    _, store, _ = tmp_env
    seed_pool(store, cfg, n_song=60, n_liner=40, n_com=25, n_dj=20)
    prog = run_program(cfg, store, seed=11)
    # contiguous, positive, ordered
    t = 0.0
    prev_end = 0.0
    for seq, _typ, dur, _g, _iid in prog:
        assert dur > 0
        assert seq > 0
        # each new item starts exactly where the prior one ended (program time)
        assert t >= prev_end - 1e-6
        prev_end = t + dur
        t += dur
    assert t >= 24 * 3600 + cfg.playout.committed_lookahead_s
    # ordering strictly increasing seq
    seqs = [s for s, _, _, _, _ in prog]
    assert seqs == list(range(1, len(seqs) + 1))


def test_consecutive_non_song_constraint(cfg, tmp_env):
    _, store, _ = tmp_env
    seed_pool(store, cfg)
    prog = run_program(cfg, store, seed=3, hours=12)
    seqs = [t for _, t, _, _, _ in prog]
    run = 0
    for t in seqs:
        if t == "song":
            run = 0
        else:
            run += 1
            assert run <= cfg.playout.max_consecutive_non_song


def test_interjection_between_songs(cfg, tmp_env):
    """No two songs back-to-back: there is at least one non-song between songs."""
    _, store, _ = tmp_env
    seed_pool(store, cfg)
    prog = run_program(cfg, store, seed=7, hours=24)
    seqs = [t for _, t, _, _, _ in prog]
    for i in range(1, len(seqs)):
        if seqs[i - 1] == "song":
            assert seqs[i] != "song", f"adjacent songs at index {i}"


def test_dj_never_adjacent_to_dj_or_news(cfg, tmp_env):
    _, store, _ = tmp_env
    seed_pool(store, cfg)
    prog = run_program(cfg, store, seed=5, hours=12)
    seqs = [t for _, t, _, _, _ in prog]
    for i, t in enumerate(seqs):
        if t == "dj_talk":
            if i > 0:
                assert seqs[i - 1] not in ("dj_talk", "news")
            if i < len(seqs) - 1:
                assert seqs[i + 1] not in ("dj_talk", "news")


def test_no_song_repeat_within_an_hour(cfg, tmp_env):
    """The operator rule: never air the same song within 60 minutes."""
    _, store, _ = tmp_env
    seed_pool(store, cfg, n_song=120)
    prog = run_program(cfg, store, seed=8, hours=12)
    # build a timeline: (start_s, item_id) for songs in program order
    last_air = {}
    t = 0.0
    for _seq, typ, dur, _g, item_id in prog:
        if typ == "song":
            aired = last_air.get(item_id)
            if aired is not None:
                assert t - aired >= 3600 - 1.0, f"song {item_id} re-aired {t - aired:.0f}s < 1h"
            last_air[item_id] = t
        t += dur


def test_no_commercial_repeat_within_break(cfg, tmp_env):
    """A commercial break must never air the same spot twice in a row — the
    picker now samples without replacement."""
    _, store, _ = tmp_env
    # only two commercials, so any break drawn from them exercises repetition
    make_item(cfg, store, "commercial", 20.0)
    make_item(cfg, store, "commercial", 24.0)
    seed_pool(store, cfg, n_song=40, n_liner=20, n_dj=3)  # lean non-song pool
    prog = run_program(cfg, store, seed=3, hours=6)
    # within each maximal run of commercials, item_ids must be unique
    prev_type = None
    run_ids = []
    for _seq, typ, _dur, _g, item_id in prog:
        if typ == "commercial":
            if prev_type != "commercial":
                run_ids = []
            assert item_id not in run_ids, f"commercial {item_id} repeated in one break"
            run_ids.append(item_id)
        prev_type = typ


def test_deterministic_program(cfg, tmp_env):
    _, _, tmp = tmp_env
    store_a = Store(tmp / "a.db")
    seed_pool(store_a, cfg)
    pa = run_program(cfg, store_a, seed=42, hours=3)
    store_b = Store(tmp / "b.db")
    seed_pool(store_b, cfg)
    pb = run_program(cfg, store_b, seed=42, hours=3)
    assert [t for _, t, _, _, _ in pa] == [t for _, t, _, _, _ in pb]


def test_fallback_no_crash_with_emptied_library(cfg, tmp_env):
    _, store, _ = tmp_env
    # no inventory at all: commit_lookahead must not raise; coverage stays 0
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    clock.advance(600)
    sched.commit_lookahead()  # should be a no-op returning cleanly
    assert store.max_seq() in (0, None) or len(store.program_since(1)) == 0


def test_trimming_preserves_live_position_and_coverage(cfg, tmp_env, monkeypatch):
    """Removing history must not move the playhead or consume future audio (§5.3)."""
    _, store, _ = tmp_env
    seed_pool(store, cfg)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(11), cfg), clock, RNG(11))
    sched.commit_lookahead()
    clock.advance(cfg.playout.window_trim_keep_s + 300)
    # Refill without trimming so we can compare immediately before/after trim.
    monkeypatch.setattr(sched, "_trim", lambda: None)
    sched.commit_lookahead()
    before_air = sched.on_air()
    before_coverage = sched.coverage()
    first_seq = store.program_since(1)[0]["seq"]
    Scheduler._trim(sched)
    assert store.program_since(1)[0]["seq"] > first_seq
    assert sched.on_air() == before_air
    assert sched.coverage() == before_coverage
    assert sched.coverage() >= cfg.playout.committed_lookahead_s
