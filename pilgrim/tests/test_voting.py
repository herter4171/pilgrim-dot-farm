"""Rating window + voting services (VOTING.md §3, RADIO §5.3, §9.1).

Pure over the scheduler's in-memory state; SimClock drives the playhead.
"""
from __future__ import annotations

import pytest
from pilgrim import voting
from pilgrim.config import RNG, SimClock
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.store import Store


def _sched(cfg, store, rows, seed=1):
    """Build a scheduler with an explicit committed program: rows are
    (type, duration_s) and get fresh item ids."""
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(seed), cfg), clock, RNG(seed))
    for type_, dur in rows:
        item = store.add_item(type_=type_, media_path=f"/tmp/{type_}.flac",
                              duration_s=dur, fresh=True)
        sched._append({"item_id": item, "type": type_, "duration_s": dur,
                       "consume": False})
    sched._rebuild_program()
    return sched, clock


def _seqs(sched):
    return [(r["seq"], r["type"], r["item_id"]) for r in sched._items]


def test_epoch_mismatch_and_empty_program_are_closed(cfg, tmp_env, tmp_path):
    _, store, _ = tmp_env
    sched, _ = _sched(cfg, store, [("song", 30.0)])
    seq = sched._items[0]["seq"]
    assert voting.rating_window(sched, "other", seq, 5.0) == (False, None)
    assert voting.rating_window(sched, "", seq, 5.0) == (False, None)
    empty = Scheduler(cfg, Store(tmp_path / "empty.db"),   # a second, empty store
                      RandomSelector(RNG(1), cfg), SimClock(), RNG(1))
    assert voting.rating_window(empty, empty.epoch, 1, 5.0) == (False, None)


def test_on_air_song_is_open(cfg, tmp_env):
    _, store, _ = tmp_env
    sched, clock = _sched(cfg, store, [("song", 30.0), ("liner", 5.0),
                                       ("song", 30.0)])
    seq1, _, item1 = _seqs(sched)[0]
    clock.advance(10)
    assert voting.rating_window(sched, sched.epoch, seq1, 5.0) == (True, item1)


def test_future_and_older_seqs_are_closed(cfg, tmp_env):
    _, store, _ = tmp_env
    sched, clock = _sched(cfg, store, [("song", 30.0), ("liner", 5.0),
                                       ("song", 30.0)])
    seq3 = _seqs(sched)[2][0]
    clock.advance(10)
    assert voting.rating_window(sched, sched.epoch, seq3, 5.0) == (False, None)
    # the liner is on air at 30..35: the song is closed once past its grace
    # (the boundary itself is open, VOTING.md V8)
    clock.advance(20)
    seq1, _, item1 = _seqs(sched)[0]
    assert voting.rating_window(sched, sched.epoch, seq1, 0.0) == (True, item1)
    clock.advance(0.1)
    assert voting.rating_window(sched, sched.epoch, seq1, 0.0) == (False, None)


def test_grace_after_the_song_ends(cfg, tmp_env):
    _, store, _ = tmp_env
    # A row after the liner too, so the playhead can move past the grace window
    # (a starved window clamps at its end and the song stays on air).
    sched, clock = _sched(cfg, store, [("song", 30.0), ("liner", 5.0),
                                       ("liner", 60.0)])
    seq1, _, item1 = _seqs(sched)[0]
    clock.advance(30.0)          # the song just ended; the liner is on air
    assert voting.rating_window(sched, sched.epoch, seq1, 5.0) == (True, item1)
    clock.advance(4.9)           # inside grace
    assert voting.rating_window(sched, sched.epoch, seq1, 5.0) == (True, item1)
    clock.advance(0.2)           # past grace (the next row has started): closed
    assert voting.rating_window(sched, sched.epoch, seq1, 5.0) == (False, None)


def test_non_song_on_air_is_closed(cfg, tmp_env):
    _, store, _ = tmp_env
    sched, clock = _sched(cfg, store, [("liner", 5.0), ("song", 30.0)])
    seq1, _, _ = _seqs(sched)[0]
    clock.advance(1)
    assert voting.rating_window(sched, sched.epoch, seq1, 5.0) == (False, None)


def test_frozen_station_is_closed(cfg, tmp_env):
    """IDLE.md: no listeners while frozen, so a stale tab must not rate an
    airing that ended hours ago."""
    _, store, _ = tmp_env
    sched, clock = _sched(cfg, store, [("song", 30.0)])
    seq1, _, item1 = _seqs(sched)[0]
    clock.advance(5)
    assert voting.rating_window(sched, sched.epoch, seq1, 5.0) == (True, item1)
    sched.freeze()
    clock.advance(7200)
    assert voting.rating_window(sched, sched.epoch, seq1, 5.0) == (False, None)


def test_splice_renumbering_closes_the_old_future_seq(cfg, tmp_env):
    """A revision bump renumbers the future (place_request_next): the on-air
    seq stays valid, the old future seqs are closed."""
    _, store, _ = tmp_env
    sched, clock = _sched(cfg, store, [("song", 30.0), ("liner", 5.0),
                                       ("song", 30.0)])
    on_seq = _seqs(sched)[0][0]
    old_future = _seqs(sched)[2][0]
    clock.advance(1)
    req = store.add_request("a request", cap=None)
    rsong = store.add_item(type_="song", media_path="/tmp/r.flac", duration_s=20.0)
    rintro = store.add_item(type_="intro", media_path="/tmp/i.flac",
                            duration_s=3.0, meta={"song_item_id": rsong,
                                                  "request_id": req["id"]})
    store.mark_request_ready(req["id"], rsong, rintro)
    sched.commit_lookahead()          # splices, bumps revision
    assert sched.revision > 1
    assert voting.rating_window(sched, sched.epoch, on_seq, 5.0)[0] is True
    assert voting.rating_window(sched, sched.epoch, old_future, 5.0) == (False, None)


# --------------------------------------------------------------------------- #
# Services (shared by server.py and e2e_server.py).
# --------------------------------------------------------------------------- #

VOTER = "a" * 32


def test_cast_vote_statuses(cfg, tmp_env):
    _, store, _ = tmp_env
    req = store.add_request("free", cap=None)
    assert voting.cast_vote(store, cfg, req["id"], VOTER, 1, None) == (
        200, {"ok": True, "score": 1, "mine": 1, "open": True})
    assert voting.cast_vote(store, cfg, req["id"], VOTER, 0, None)[1]["score"] == 0
    assert voting.cast_vote(store, cfg, 999, VOTER, 1, None)[0] == 404
    store.mark_request_aired(req["id"])
    code, body = voting.cast_vote(store, cfg, req["id"], VOTER, 1, None)
    assert code == 409 and body["code"] == "voting_closed"
    assert body["score"] == 0


def test_cast_rating_window_then_insert(cfg, tmp_env):
    _, store, _ = tmp_env
    sched, clock = _sched(cfg, store, [("song", 30.0), ("liner", 60.0)])
    seq, _, item = _seqs(sched)[0]
    clock.advance(2)
    code, body = voting.cast_rating(
        store, cfg, sched,
        voting.RatingIn(voter_id=VOTER, epoch=sched.epoch, seq=seq, stars=4),
        None)
    assert code == 200 and body["count"] == 1 and body["avg"] == 4.0
    clock.advance(40)
    code, body = voting.cast_rating(
        store, cfg, sched,
        voting.RatingIn(voter_id=VOTER, epoch=sched.epoch, seq=seq, stars=5),
        None)
    assert code == 409 and body["code"] == "rating_closed"


@pytest.mark.parametrize("bad", [
    {"voter_id": "nope", "epoch": "e", "seq": 1, "stars": 3},
    {"voter_id": VOTER, "epoch": "e", "seq": 1, "stars": 0},
    {"voter_id": VOTER, "epoch": "e", "seq": 1, "stars": 6},
    {"voter_id": VOTER, "epoch": "x" * 65, "seq": 1, "stars": 3},
])
def test_rating_payload_validation(bad):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        voting.RatingIn(**bad)


@pytest.mark.parametrize("bad", [
    {"voter_id": "ABC", "value": 1},
    {"voter_id": VOTER, "value": 2},
])
def test_vote_payload_validation(bad):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        voting.VoteIn(**bad)


# --------------------------------------------------------------------------- #
# VOTING.md Step 8 (ships off): ratings weight the recycled-song draw.
# --------------------------------------------------------------------------- #

def test_rotation_weight_off_by_default_is_unchanged(cfg, tmp_env):
    _, store, _ = tmp_env
    sched, clock = _sched(cfg, store, [("song", 30.0)])
    a = store.add_item(type_="song", media_path="/tmp/a.flac", duration_s=30,
                       created_at="2026-01-01T00:00:00+00:00")
    b = store.add_item(type_="song", media_path="/tmp/b.flac", duration_s=30,
                       created_at="2026-01-01T00:00:00+00:00")
    for i in range(5):
        store.cast_song_rating(item_id=a, epoch="e", seq=i + 1,
                               voter_id=f"{i}" * 32, ip=f"10.0.0.{i}",
                               stars=5, max_voters_per_ip=5)
        store.cast_song_rating(item_id=b, epoch="e", seq=100 + i,
                               voter_id=f"{i}" * 32, ip=f"10.0.0.{i}",
                               stars=1, max_voters_per_ip=5)
    now = clock.wall()
    assert cfg.ratings.rotation_weight is False
    assert sched._song_weight(store.get_item(a), now) == \
           sched._song_weight(store.get_item(b), now)


def test_rotation_weight_favours_high_ratings_when_enabled(cfg, tmp_env):
    _, store, _ = tmp_env
    sched, clock = _sched(cfg, store, [("song", 30.0)])
    cfg.ratings.rotation_weight = True
    a = store.add_item(type_="song", media_path="/tmp/a.flac", duration_s=30,
                       created_at="2026-01-01T00:00:00+00:00")
    b = store.add_item(type_="song", media_path="/tmp/b.flac", duration_s=30,
                       created_at="2026-01-01T00:00:00+00:00")
    for i in range(5):
        store.cast_song_rating(item_id=a, epoch="e", seq=i + 1,
                               voter_id=f"{i}" * 32, ip=f"10.0.0.{i}",
                               stars=5, max_voters_per_ip=5)
        store.cast_song_rating(item_id=b, epoch="e", seq=100 + i,
                               voter_id=f"{i}" * 32, ip=f"10.0.0.{i}",
                               stars=1, max_voters_per_ip=5)
    now = clock.wall()
    wa = sched._song_weight(store.get_item(a), now)
    wb = sched._song_weight(store.get_item(b), now)
    assert wa > wb
    # 5 turkeys = 1.5x, 1 turkey = 0.25x (clamped)
    assert wa / wb == pytest.approx(1.5 / (1 / 3), rel=0.02)
    # below the vote floor nothing changes
    cfg.ratings.rotation_min_votes = 6
    assert sched._song_weight(store.get_item(a), now) == \
           sched._song_weight(store.get_item(b), now)


def test_song_ratings_for_batches_lifetime_aggregates(cfg, tmp_env):
    """FIX_RATINGS.md Step 2: one query, keyed by item, over every airing."""
    _, store, _ = tmp_env
    a = store.add_item(type_="song", media_path="/tmp/a.flac", duration_s=60.0, fresh=True)
    b = store.add_item(type_="song", media_path="/tmp/b.flac", duration_s=60.0, fresh=True)
    c = store.add_item(type_="song", media_path="/tmp/c.flac", duration_s=60.0, fresh=True)
    cap = cfg.ratings.max_voters_per_ip
    store.cast_song_rating(item_id=a, epoch="e", seq=1, voter_id="1" * 32, ip=None,
                           stars=5, max_voters_per_ip=cap)
    store.cast_song_rating(item_id=a, epoch="e", seq=9, voter_id="1" * 32, ip=None,
                           stars=4, max_voters_per_ip=cap)   # a later airing
    store.cast_song_rating(item_id=b, epoch="e", seq=2, voter_id="2" * 32, ip=None,
                           stars=2, max_voters_per_ip=cap)
    aggs = store.song_ratings_for([a, b, c, a])
    assert aggs == {a: {"count": 2, "avg": 4.5}, b: {"count": 1, "avg": 2.0}}
    assert c not in aggs
    assert store.song_ratings_for([]) == {}
    assert aggs[a] == store.song_rating(a)
