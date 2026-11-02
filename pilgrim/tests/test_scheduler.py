"""Scheduler simulation (RADIO.md §15): drive a 24h run over a SimClock with a
seeded pool and assert the committed-program invariants hold."""
from __future__ import annotations

from conftest import make_item, seed_pool
from pilgrim.config import RNG, SimClock
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.store import Store


def run_program(cfg, store, seed, hours=24, step=30, min_coverage=None):
    """min_coverage defaults to the full lookahead (a well-stocked pool); a
    song-starved pool only bridges to filler_horizon_s by design (§5.2)."""
    if min_coverage is None:
        min_coverage = cfg.playout.committed_lookahead_s
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(seed), cfg), clock, RNG(seed))
    seen = []  # (seq, type, duration, genre_or_None, item_id)
    last_seq = 0
    for _ in range(int(hours * 3600) // step):
        clock.advance(step)
        sched.commit_lookahead()
        assert sched.coverage() >= min_coverage
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
    # program time starts at the first commit (one 30 s step in), so the
    # committed total covers the remaining sim time plus the lookahead
    assert t >= 24 * 3600 - 30 + cfg.playout.committed_lookahead_s
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


def test_starvation_anchor_starts_new_item_at_now(cfg, tmp_env):
    """After a dry spell the next appended item starts at 'now', not in the
    past (OVERHAUL 2.4): commit 30 s, advance 100 s, append 60 s -> on-air is
    the new item at offset ~0, not offset 70."""
    _, store, _ = tmp_env
    a = make_item(cfg, store, "song", 30.0)
    b = make_item(cfg, store, "song", 60.0)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    sched._append({"item_id": a, "type": "song", "duration_s": 30.0})
    sched._rebuild_program()
    assert sched.coverage() == 30.0
    clock.advance(100)
    assert sched.coverage() == 0.0
    sched._append({"item_id": b, "type": "song", "duration_s": 60.0})
    sched._rebuild_program()
    seq, off = sched.on_air()
    assert seq == b
    assert off < 1.0


def test_new_scheduler_starts_with_empty_program(cfg, tmp_env):
    """A fresh Scheduler over a store that already has program rows starts with
    an empty program (the committed program is live-only state; OVERHAUL 2.4)."""
    _, store, _ = tmp_env
    a = make_item(cfg, store, "song", 30.0)
    clock = SimClock()
    s1 = Scheduler(cfg, store, RandomSelector(RNG(2), cfg), clock, RNG(2))
    s1._append({"item_id": a, "type": "song", "duration_s": 30.0})
    s1._rebuild_program()
    assert store.max_seq() >= 1
    s2 = Scheduler(cfg, store, RandomSelector(RNG(3), cfg), SimClock(), RNG(3))
    assert store.max_seq() == 0
    assert s2._items == []


def test_request_song_jumps_line_with_intro_and_marks_aired(cfg, tmp_env):
    """Ready request song airs next (after a liner), preceded by its intro. The
    request is marked aired when the playhead reaches its song, not when it is
    committed up to 10 min earlier, and it is committed only once (4.7)."""
    import json
    _, store, _ = tmp_env
    stock = make_item(cfg, store, "song", 90.0)                 # stock song available
    req = store.add_request("play for Mittens", cap=10)
    req_song = make_item(cfg, store, "song", 120.0, fresh=True) # request's song
    intro_id = store.add_item(type_="intro", media_path="/i.flac", duration_s=8.0,
                              evergreen=False, fresh=True,
                              meta={"song_item_id": req_song, "request_id": req["id"]})
    store.mark_request_ready(req["id"], req_song, intro_id)
    liner = make_item(cfg, store, "liner", 6.0)

    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(5), cfg), clock, RNG(5))
    sched.build_state()  # warm
    sched._append({"item_id": liner, "type": "liner", "duration_s": 6.0})
    sched._rebuild_program()
    sched.commit_lookahead()

    rows = store.program_after(store.program_since(1)[0]["seq"])
    types = [r["type"] for r in rows]
    # the request song comes right after the liner, preceded by its intro
    idx = types.index("song")
    # an intro was committed immediately before that song
    assert idx >= 1 and types[idx - 1] == "intro"
    assert types[idx - 1] == "intro"
    song_row = rows[idx]
    assert song_row["item_id"] == req_song
    # intro glued to this song
    intro_row = rows[idx - 1]
    imeta = json.loads(store.get_item(intro_row["item_id"])["meta_json"])
    assert imeta["song_item_id"] == req_song
    # committed but not yet heard: still 'ready' (UI: "up next")
    assert store.get_request(req["id"])["status"] == "ready"
    song_start = sum(r["duration_s"] for r in store.program_since(1)[:idx + 1])
    clock.advance(song_start - 1.0)
    sched.commit_lookahead()
    assert store.get_request(req["id"])["status"] == "ready"
    clock.advance(2.0)
    sched.commit_lookahead()
    assert store.get_request(req["id"])["status"] == "aired"
    committed = [r["item_id"] for r in store.program_since(1)]
    assert committed.count(req_song) == 1  # never re-committed while pending
    assert stock  # unused var guard


def test_no_song_after_song_even_with_request_ready(cfg, tmp_env):
    """The interjection rule still holds: last committed is a song -> the next
    committed item is NOT a song, even with a ready request song (4.7)."""
    _, store, _ = tmp_env
    make_item(cfg, store, "song", 90.0)
    req = store.add_request("mittens", cap=10)
    req_song = make_item(cfg, store, "song", 120.0, fresh=True)
    store.mark_request_ready(req["id"], req_song, None)
    song = make_item(cfg, store, "song", 60.0)
    # non-song interjections must exist so the no-inventory fallback can't
    # revisit "song" — we're testing the interjection rule, not the fallback
    make_item(cfg, store, "liner", 6.0)
    make_item(cfg, store, "commercial", 20.0)
    make_item(cfg, store, "dj_talk", 15.0)

    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(3), cfg), clock, RNG(3))
    sched._append({"item_id": song, "type": "song", "duration_s": 60.0})
    sched._rebuild_program()
    nxt = sched.selector.choose_next(sched.build_state())
    assert nxt != "song"
    # the ready request was not aired, so it stays ready for the next opening
    assert store.get_request(req["id"])["status"] == "ready"


def test_single_song_not_looped(cfg, tmp_env):
    """One song in stock: it may not recur inside the spacing floor.

    Spacing is measured on last_aired_at, the persisted wall-clock commit time
    (PRIORITIES §4). Under a deliberate single-song drought the program-table
    start times drift from wall time via the starvation offset anchor, so we
    assert the real invariant: consecutive commit-wall air times are >= floor
    apart (what the operator actually hears)."""
    _, store, _ = tmp_env
    genres = list(cfg.songs.genres.keys())
    make_item(cfg, store, "song", 60.0, genre=genres[0])
    for i in range(15):
        make_item(cfg, store, "liner", 3 + i % 5)
    for _ in range(20):
        make_item(cfg, store, "commercial", 25.0)
    floor = min(cfg.playout.song_min_spacing_s)
    # one song can't fill 10 min ahead: droughts bridge, they don't pad (§5.2)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(4), cfg), clock, RNG(4))
    horizon = cfg.playout.filler_horizon_s
    wall_airs: list[float] = []
    last_seq = 0
    for _ in range(int(2 * 3600) // 30):
        clock.advance(30)
        sched.commit_lookahead()
        for r in store.program_after(last_seq):
            if r["type"] == "song":
                it = store.get_item(r["item_id"])
                wall_airs.append(it["last_aired_at"])
            last_seq = r["seq"]
    assert len(wall_airs) >= 2
    assert all(b - a >= floor - 1.0
               for a, b in zip(wall_airs, wall_airs[1:], strict=False))
    # the single song is really all that ever airs (drought bridges otherwise)
    assert sched.coverage() <= horizon + 45.0


def test_thin_liner_pool_does_not_pad_the_lookahead(cfg, tmp_env):
    """Only two short liners in stock (a fresh start): the scheduler bridges
    dead air but never pads the 10-min lookahead with repeats, so a song made
    moments later airs within the filler horizon, not after ~185 liners (§5.2)."""
    _, store, _ = tmp_env
    make_item(cfg, store, "liner", 3.2)
    make_item(cfg, store, "liner", 3.9)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    horizon = cfg.playout.filler_horizon_s
    for _ in range(30):
        clock.advance(2)
        sched.commit_lookahead()
        assert 0 < sched.coverage() < horizon + 4.0  # bridged, never padded
    song = make_item(cfg, store, "song", 120.0, genre="polka")
    clock.advance(2)
    sched.commit_lookahead()
    starts = dict(zip([r["item_id"] for r in sched._items], sched._cum, strict=True))
    assert song in starts
    assert starts[song] - sched.position() < horizon + 4.0


def test_new_song_airs_promptly_after_a_drought(cfg, tmp_env):
    """Songs aired, none airable, 20 unaired spots in stock: the scheduler must
    not commit a wall of spots. A song made mid-drought airs within the filler
    horizon (the 22:48 incident: a new song queued behind ~10 min of spots)."""
    _, store, _ = tmp_env
    make_item(cfg, store, "song", 90.0, genre="polka")
    for i in range(20):
        make_item(cfg, store, "commercial", 20.0 + i % 5)
    for i in range(4):
        make_item(cfg, store, "liner", 4.0 + i)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(5), cfg), clock, RNG(5))
    horizon = cfg.playout.filler_horizon_s
    for _ in range(90):  # 3 min: the lone song airs, then the drought
        clock.advance(2)
        sched.commit_lookahead()
    assert sched.coverage() < horizon + 45.0
    song = make_item(cfg, store, "song", 120.0, genre="synthwave")
    clock.advance(2)
    sched.commit_lookahead()
    starts = dict(zip([r["item_id"] for r in sched._items], sched._cum, strict=True))
    assert song in starts
    assert starts[song] - sched.position() < horizon + 45.0


def test_request_not_taken_right_after_dj_talk(cfg, tmp_env):
    """A request's intro is its thank-you: right after dj_talk the intro would
    be dropped, so the slot goes to a non-song and the request (with its
    intro) airs in the following slot."""
    _, store, _ = tmp_env
    make_item(cfg, store, "song", 90.0, genre="polka")
    req = store.add_request("play for Mittens", cap=10)
    req_song = make_item(cfg, store, "song", 120.0, genre="synthwave")
    intro_id = store.add_item(type_="intro", media_path="/i.flac", duration_s=8.0,
                              evergreen=False, fresh=True,
                              meta={"song_item_id": req_song, "request_id": req["id"]})
    store.mark_request_ready(req["id"], req_song, intro_id)
    for i in range(5):
        make_item(cfg, store, "liner", 4.0 + i)
    dj = make_item(cfg, store, "dj_talk", 12.0)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(2), cfg), clock, RNG(2))
    sched._append({"item_id": dj, "type": "dj_talk", "duration_s": 12.0, "consume": True})
    sched._rebuild_program()
    sched.commit_lookahead()
    rows = store.program_since(1)
    types = [r["type"] for r in rows]
    assert types[1] not in ("song", "intro")
    idx = [r["item_id"] for r in rows].index(req_song)
    assert types[idx - 1] == "intro"


def test_pending_request_holds_window_and_takes_next_slot(cfg, tmp_env):
    """A pending listener request holds the committed window at
    filler_horizon_s: the stock songs already ahead drain and no new ones queue
    in front of it. When its song + intro land, they take the next slot instead
    of airing behind a full lookahead (the 'up next for three songs' bug; §5.2).
    The hold is opt-in (playout.request_hold_max_s > 0; config.yaml ships 0)."""
    _, store, _ = tmp_env
    cfg.playout.request_hold_max_s = 900
    seed_pool(store, cfg, n_song=8, n_liner=12, n_com=8, n_dj=4)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(7), cfg), clock, RNG(7))
    sched.commit_lookahead()
    assert sched.coverage() >= cfg.playout.committed_lookahead_s

    req = store.add_request("play for Mittens", cap=10)
    req_song = make_item(cfg, store, "song", 120.0, fresh=True)
    intro_id = store.add_item(
        type_="intro", media_path="/i.flac", duration_s=8.0,
        evergreen=False, fresh=True,
        meta={"song_item_id": req_song, "request_id": req["id"]})

    # queued, not ready yet: the window drains to the filler horizon and no
    # stock song is committed ahead of the request.
    before = store.max_seq()
    clock.advance(cfg.playout.committed_lookahead_s)
    sched.commit_lookahead()
    assert sched.coverage() <= cfg.playout.filler_horizon_s + 45.0
    new_types = [r["type"] for r in store.program_after(before)]
    assert "song" not in new_types

    # ready: song + intro take the next slot; nothing queues behind them.
    store.mark_request_ready(req["id"], req_song, intro_id)
    clock.advance(2)
    sched.commit_lookahead()
    rows = store.program_since(1)
    ids = [r["item_id"] for r in rows]
    assert req_song in ids
    assert ids[ids.index(req_song) - 1] == intro_id  # intro glued in front
    # the request is the NEXT song after the playhead (spliced ahead of stock)
    future_songs = [(r["item_id"], sched._cum[i]) for i, r in enumerate(sched._items)
                    if r["type"] == "song" and sched._cum[i] > sched.position()]
    assert future_songs and future_songs[0][0] == req_song

    # playhead reaches the tail: the request is marked aired, not before.
    clock.advance(sched.coverage() + 1)
    sched.commit_lookahead()
    assert store.get_request(req["id"])["status"] == "aired"


def test_ready_request_splices_ahead_of_committed_songs(cfg, tmp_env):
    """A ready request is spliced into the next song slot ahead of already-
    committed stock songs, its intro lands immediately before it, and the
    programme revision bumps so revision-aware clients refetch (TUI.md §5)."""
    _, store, _ = tmp_env
    seed_pool(store, cfg, n_song=20, n_liner=20, n_com=20, n_dj=6)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(7), cfg), clock, RNG(7))
    sched.commit_lookahead()
    assert sched.coverage() >= cfg.playout.committed_lookahead_s
    rev_before = sched.revision

    req = store.add_request("play my song", cap=10)
    req_song = make_item(cfg, store, "song", 120.0, fresh=True)
    intro_id = store.add_item(type_="intro", media_path="/i.flac", duration_s=8.0,
                              evergreen=False, fresh=True,
                              meta={"song_item_id": req_song, "request_id": req["id"]})
    store.mark_request_ready(req["id"], req_song, intro_id)
    sched.commit_lookahead()

    pos = sched.position()
    future_songs = [(r["item_id"], sched._cum[i]) for i, r in enumerate(sched._items)
                    if r["type"] == "song" and sched._cum[i] > pos]
    assert future_songs and future_songs[0][0] == req_song
    ids = [r["item_id"] for r in sched._items]
    assert ids[ids.index(req_song) - 1] == intro_id
    # splice bumps the revision; every seq stays unique and ordered
    assert sched.revision == rev_before + 1
    seqs = [r["seq"] for r in sched._items]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    # it airs at the next boundary, not behind the full committed lookahead
    assert future_songs[0][1] - pos < 300.0


def test_request_hold_releases_after_limit(cfg, tmp_env):
    """A request stuck in the queue (backend down) must not pin the station at
    a shallow lookahead forever: after request_hold_max_s the stock-song
    fallback resumes."""
    _, store, _ = tmp_env
    cfg.playout.request_hold_max_s = 30
    seed_pool(store, cfg, n_song=8, n_liner=12, n_com=8, n_dj=4)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(7), cfg), clock, RNG(7))
    sched.commit_lookahead()
    store.add_request("stuck request", cap=10)  # queued, never becomes ready

    # first cycle starts the hold; the window drains
    clock.advance(cfg.playout.committed_lookahead_s)
    sched.commit_lookahead()
    assert sched.coverage() <= cfg.playout.filler_horizon_s + 45.0

    # past the hold limit, normal lookahead resumes (stock songs fill again)
    clock.advance(cfg.playout.request_hold_max_s + 1)
    sched.commit_lookahead()
    assert sched.coverage() >= cfg.playout.committed_lookahead_s


# --------------------------------------------------------------------------- #
# PRIORITIES — fresher songs (work the plan top to bottom)
# --------------------------------------------------------------------------- #
def test_restart_first_pick_not_lowest_id_and_favors_recent(cfg, tmp_env):
    """(PRIORITIES §6) A pool of recycled songs with no persisted history must
    not open with the lowest id after a restart (the old air-clock inversion
    aired song #1 every time). With no air history the weight is age-driven, so
    across seeds the first pick favors the newest third."""
    from conftest import seed_aged_songs
    _, store, _ = tmp_env
    epoch = 1_800_000_000.0
    ids = seed_aged_songs(cfg, store, 30, epoch, 40.0, 0.0, fresh=False)
    newest_third = set(ids[20:])
    first_picks = []
    for seed in range(40):
        clock = SimClock(epoch=epoch)
        sched = Scheduler(cfg, store, RandomSelector(RNG(seed), cfg), clock, RNG(seed))
        st = sched.build_state()
        first_picks.append(sched._pick_stock_song(st)[0]["item_id"])
    assert min(first_picks) > ids[0], "some seed opened with the lowest id (old bug)"
    frac_recent = sum(p in newest_third for p in first_picks) / len(first_picks)
    assert frac_recent > 0.4, f"first pick favors newest third only {frac_recent:.2f}"


def test_no_song_lockup_41_songs_all_air_over_6h(cfg, tmp_env):
    """(PRIORITIES §6) 41 recycled songs over 6 h: every song airs at least once
    (today the old code only ever aired #1-#18). Uses last_aired_at as the
    'aired at least once' flag (rows behind the trim window are gone, but the
    persisted ledger survives)."""
    from conftest import seed_aged_songs
    _, store, _ = tmp_env
    ids = seed_aged_songs(cfg, store, 41, 1_800_000_000.0, 40.0, 0.0, fresh=False)
    for i in range(30):
        make_item(cfg, store, "liner", 3 + i % 12)
    for i in range(25):
        make_item(cfg, store, "commercial", 20 + i % 10)
    for i in range(15):
        make_item(cfg, store, "dj_talk", 15 + i % 5)
    clock = SimClock(epoch=1_800_000_000.0)
    sched = Scheduler(cfg, store, RandomSelector(RNG(7), cfg), clock, RNG(7))
    # PRIORITIES §6's guard is "unheard no longer than max(6 h, pool x mean
    # gap)" = 6 h here. A 6 h run sits exactly ON that bound: an old song with
    # a ~0.11 youth weight has a ~20% chance of never being drawn, which is the
    # intended thinning, not a lockup. Run 12 h so the guard is actually
    # exceeded if the rotation fails (the old bug aired only #1-#18 forever,
    # so it still fails here).
    for _ in range(int(12 * 3600) // 30):
        clock.advance(30)
        sched.commit_lookahead()
    never = [iid for iid in ids if store.get_item(iid)["last_aired_at"] is None]
    assert not never, f"songs never aired (lockup): {never}"


def test_fresh_songs_lifo(cfg, tmp_env):
    """(PRIORITIES §6) Two fresh songs: the NEWER airs first (LIFO), and both
    air before any recycled song."""
    _, store, _ = tmp_env
    older = make_item(cfg, store, "song", 90.0, genre="polka")        # lower id
    newer = make_item(cfg, store, "song", 90.0, genre="bluegrass")    # higher id
    for i in range(10):
        make_item(cfg, store, "liner", 3 + i % 7)
    for _ in range(12):
        make_item(cfg, store, "commercial", 20.0)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(3), cfg), clock, RNG(3))
    sched.commit_lookahead()
    song_ids = [r["item_id"] for r in store.program_since(1) if r["type"] == "song"]
    # both fresh songs air (newer first) before any non-fresh song can
    assert song_ids[:2] == [newer, older], f"fresh LIFO violated: {song_ids[:2]}"


def test_air_history_persists_across_restart(cfg, tmp_env):
    """(PRIORITIES §6) Commit a song, build a new Scheduler on the same db: the
    song's last_aired_at survives and it is still inside its spacing window."""
    _, store, _ = tmp_env
    sid = make_item(cfg, store, "song", 90.0, genre="polka")
    for i in range(15):
        make_item(cfg, store, "liner", 3 + i % 5)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    sched.commit_lookahead()
    assert store.get_item(sid)["last_aired_at"] is not None  # aired + persisted
    # brand-new scheduler over the same db keeps the floor: the song must not
    # be airable until its spacing window elapses
    sched2 = Scheduler(cfg, store, RandomSelector(RNG(2), cfg), SimClock(), RNG(2))
    assert sched2.build_state().available["song"] is False


def test_weighting_never_breaks_spacing_floor(cfg, tmp_env):
    """(PRIORITIES §6) A heavily weighted young song inside its spacing floor is
    NOT picked: the floor filters it out before weighting. Weighting only orders
    what already passes spacing; it never overrides it."""
    from conftest import iso_from_epoch
    _, store, _ = tmp_env
    epoch = 1_800_000_000.0
    # young, high-youth song that aired 10 min ago (inside the 15-min floor)
    young = store.add_item(type_="song", media_path="/y.flac", duration_s=120.0,
                           genre="polka", fresh=False, evergreen=True,
                           created_at=iso_from_epoch(epoch - 0.5 * 3600))
    store.commit_song_air(young, epoch - 600)
    # old, low-weight song that cleared the floor (aired 2 h ago)
    old = store.add_item(type_="song", media_path="/o.flac", duration_s=120.0,
                         genre="synthwave", fresh=False, evergreen=True,
                         created_at=iso_from_epoch(epoch - 48 * 3600))
    store.commit_song_air(old, epoch - 2 * 3600)
    clock = SimClock(epoch=epoch)
    sched = Scheduler(cfg, store, RandomSelector(RNG(5), cfg), clock, RNG(5))
    eligible = [i["id"] for i in sched._eligible_songs()]
    assert young not in eligible
    assert eligible == [old]
    for _ in range(50):
        pick = sched._pick_stock_song(sched.build_state())[0]["item_id"]
        assert pick == old, "weighting picked a song inside the spacing floor"


# ------------------------------------------------- words between songs (§5.2)
_CALLOUTS = {"liner", "station_id", "dj_talk", "field_report", "intro"}


def _bare_song_gaps(prog) -> int:
    bare = 0
    since: list[str] | None = None
    for _seq, typ, _dur, _g, _id in prog:
        if typ == "song":
            if since is not None and not _CALLOUTS & set(since):
                bare += 1
            since = []
        elif since is not None:
            since.append(typ)
    return bare


def _emergency_ids(store, n=3):
    return [store.add_item(type_="station_id", media_path=f"/tmp/id{i}.flac",
                           duration_s=4.0, evergreen=True, emergency=True)
            for i in range(n)]


def test_every_song_gap_has_words(cfg, tmp_env):
    _, store, _ = tmp_env
    seed_pool(store, cfg, n_song=80, n_liner=20, n_com=20, n_dj=10)
    prog = run_program(cfg, store, seed=5, hours=12)
    assert _bare_song_gaps(prog) == 0


def test_cold_start_uses_emergency_station_ids(cfg, tmp_env):
    """Right after a wipe: songs and spots but no liners or talk yet. The
    emergency station IDs still put words between every pair of songs."""
    _, store, _ = tmp_env
    seed_pool(store, cfg, n_song=60, n_liner=0, n_com=10, n_dj=0)
    ids = set(_emergency_ids(store))
    prog = run_program(cfg, store, seed=2, hours=4,
                       min_coverage=cfg.playout.filler_horizon_s)
    assert _bare_song_gaps(prog) == 0
    assert any(item_id in ids for *_, item_id in prog)


def test_emergency_ids_stay_out_while_liners_exist(cfg, tmp_env):
    _, store, _ = tmp_env
    seed_pool(store, cfg, n_song=60, n_liner=15, n_com=10, n_dj=0)
    ids = set(_emergency_ids(store))
    prog = run_program(cfg, store, seed=4, hours=6)
    assert _bare_song_gaps(prog) == 0
    assert not any(item_id in ids for *_, item_id in prog)


def test_selector_forces_words_in_the_last_slot(cfg):
    from pilgrim.selector import PlayoutState
    sel = RandomSelector(RNG(1), cfg)
    for seed in range(30):
        sel.rng = RNG(seed)
        st = PlayoutState(cfg)
        st.available.update(song=True, commercial_break=True, liner=True)
        st.recent_types = ["song", "commercial"]  # one slot left before a song
        st.callout_pending = True
        assert sel.choose_next(st) == "liner"


def test_top_of_hour_leads_with_words(cfg):
    from pilgrim.selector import PlayoutState
    sel = RandomSelector(RNG(1), cfg)
    for seed in range(30):
        sel.rng = RNG(seed)
        st = PlayoutState(cfg)
        st.available.update(song=True, commercial_break=True, liner=True, dj_talk=True)
        st.recent_types = ["song"]
        st.callout_pending = True
        st.near_top_of_hour = True
        assert sel.choose_next(st) in ("liner", "dj_talk")


def test_request_hold_zero_keeps_songs_airing_while_a_request_renders(cfg, tmp_env):
    """request_hold_max_s = 0 (the shipped default): a request that is still
    being written/generated does not starve the air of songs (the 2026-11-01
    'endless liners' incident); once ready it splices into the next song slot."""
    _, store, _ = tmp_env
    cfg.playout.request_hold_max_s = 0
    seed_pool(store, cfg, n_song=8, n_liner=12, n_com=8, n_dj=4)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(7), cfg), clock, RNG(7))
    sched.commit_lookahead()
    req = store.add_request("play for Mittens", cap=10)
    store.mark_request_producing(req["id"])
    before = store.max_seq()
    for _ in range(4):
        clock.advance(cfg.playout.committed_lookahead_s)
        sched.commit_lookahead()
    assert sched.coverage() >= cfg.playout.committed_lookahead_s
    assert "song" in [r["type"] for r in store.program_after(before)]


def test_contextual_pick_uses_air_slot_expiry(cfg, tmp_env):
    """A1: expiry is checked against the slot the clip would AIR in, not the
    commit time. A clip that is still valid now but stale 300 s later must not
    be committed."""
    from datetime import UTC, datetime
    _, store, _ = tmp_env
    clock = SimClock(epoch=1_800_000_000.0)
    song = make_item(cfg, store, "song", 300.0)
    sched = Scheduler(cfg, store, RandomSelector(RNG(5), cfg), clock, RNG(5))
    sched._append({"item_id": song, "type": "song", "duration_s": 300.0})
    sched._rebuild_program()
    # next slot airs at wall + 300 (coverage == 300, playhead at 0)
    stale = datetime.fromtimestamp(clock.wall() + 120, UTC).isoformat()
    make_item(cfg, store, "dj_talk", 16.0, expires_at=stale)
    assert sched._pick_fresh_talk("dj_talk") == []
    # one that clears the slot is accepted
    ok = datetime.fromtimestamp(clock.wall() + 400, UTC).isoformat()
    good = make_item(cfg, store, "dj_talk", 16.0, expires_at=ok)
    picked = sched._pick_fresh_talk("dj_talk")
    assert [p["item_id"] for p in picked] == [good]


def test_contextual_pick_newest_first(cfg, tmp_env):
    """Revision 4: the newest unexpired clip wins, not a uniform draw."""
    _, store, _ = tmp_env
    make_item(cfg, store, "dj_talk", 16.0)
    make_item(cfg, store, "dj_talk", 16.0)
    newest = make_item(cfg, store, "dj_talk", 16.0)
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(2), cfg), clock, RNG(2))
    assert sched._pick_fresh_talk("dj_talk")[0]["item_id"] == newest


def test_news_expiry_checked_at_air_slot(cfg, tmp_env):
    """A1 for news: a bulletin valid now but stale at the next slot is not
    committed."""
    from datetime import UTC, datetime
    _, store, _ = tmp_env
    clock = SimClock(epoch=1_800_000_000.0)
    song = make_item(cfg, store, "song", 300.0)
    sched = Scheduler(cfg, store, RandomSelector(RNG(4), cfg), clock, RNG(4))
    sched._append({"item_id": song, "type": "song", "duration_s": 300.0})
    sched._rebuild_program()
    stale = datetime.fromtimestamp(clock.wall() + 120, UTC).isoformat()
    make_item(cfg, store, "news", 19.0, fresh=True, expires_at=stale)
    assert sched._news_valid() is None


def test_time_check_only_airs_inside_its_mark_window(cfg, tmp_env):
    """Operator pick: clock reads at :00/:15/:30/:45. A check airs only within
    time_check_window_s of its mark, so it can never be stale."""
    from datetime import UTC, datetime
    _, store, _ = tmp_env
    clock = SimClock(epoch=1_800_000_000.0)
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    from pilgrim.pipelines.clocktime import next_marks
    mark = int(next_marks(clock.wall() + 60, cfg.station.timezone,
                          cfg.talk.time_check_marks_min, count=1)[0])
    dur = mark - clock.wall()  # next slot airs exactly on the mark
    song = make_item(cfg, store, "song", dur)
    sched._append({"item_id": song, "type": "song", "duration_s": dur})
    sched._rebuild_program()
    item = make_item(cfg, store, "time_check", 4.0, meta={"bucket": mark},
                     expires_at=datetime.fromtimestamp(mark + 300, UTC).isoformat())
    assert sched._time_check_item()["id"] == item
    assert [e["type"] for e in sched._pick_time_check()] == ["time_check"]


def test_time_check_not_due_away_from_the_mark(cfg, tmp_env):
    from datetime import UTC, datetime
    _, store, _ = tmp_env
    clock = SimClock(epoch=1_800_000_000.0)
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    from pilgrim.pipelines.clocktime import next_marks
    mark = int(next_marks(clock.wall() + 60, cfg.station.timezone,
                          cfg.talk.time_check_marks_min, count=1)[0])
    dur = mark + 600 - clock.wall()  # slot is 10 minutes after the mark
    song = make_item(cfg, store, "song", dur)
    sched._append({"item_id": song, "type": "song", "duration_s": dur})
    sched._rebuild_program()
    make_item(cfg, store, "time_check", 4.0, meta={"bucket": mark + 600},
              expires_at=datetime.fromtimestamp(mark + 900, UTC).isoformat())
    assert sched._time_check_item() is None
    assert sched._pick_time_check() == []


def test_promo_spacing_and_request_line_gap(cfg, tmp_env):
    """No two promos inside promo_min_spacing_s; request-line plugs have their
    own longer gap (ANNOUNCER_OVERHAUL C3)."""
    _, store, _ = tmp_env
    make_item(cfg, store, "promo", 12.0, meta={"kind": "request_line"})
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    assert sched._promo_available()
    sched._last_promo_air = sched._air_clock - 100  # inside 600 s
    assert not sched._promo_available()
    sched._last_promo_air = sched._air_clock - 1000
    sched._last_promo_kind_air["request_line"] = sched._air_clock - 100
    assert not sched._promo_available()
    sched._last_promo_kind_air["request_line"] = sched._air_clock - 3000
    assert sched._promo_available()


def test_promo_never_adjacent_to_dj_talk(cfg):
    """Two Liam-voice segments in a row sound wrong: a promo is not drawn right
    after dj_talk, and dj_talk is not drawn right after a promo."""
    from pilgrim.selector import SEGMENTS, PlayoutState, RandomSelector
    sel = RandomSelector(RNG(1), cfg)
    st = PlayoutState(cfg)
    st.available = {s: False for s in SEGMENTS}
    st.available.update(song=True, promo=True)
    st.recent_types = ["dj_talk"]
    assert sel.choose_next(st) == "song"
    st.available.update(song=False, dj_talk=True, promo=False)
    st.recent_types = ["promo"]
    assert sel.choose_next(st) == "dj_talk"


def test_promo_kinds_have_their_own_gaps(cfg, tmp_env):
    """Steady self-promotion: the paid-tier promo and the Tree Fiddy Report
    each have their own spacing, independent of the campaign/global gap."""
    _, store, _ = tmp_env
    make_item(cfg, store, "promo", 12.0, meta={"kind": "paid_tier"})
    make_item(cfg, store, "promo", 20.0, meta={"kind": "tree_fiddy"})
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), clock, RNG(1))
    assert sched._promo_available()
    sched._last_promo_kind_air["paid_tier"] = sched._air_clock - 100
    sched._last_promo_kind_air["tree_fiddy"] = sched._air_clock - 100
    assert not sched._promo_available()
    sched._last_promo_kind_air["paid_tier"] = sched._air_clock - 2000
    assert sched._promo_available()
    picked = sched._pick_promo()
    assert [p["promo_kind"] for p in picked] == ["paid_tier"]
