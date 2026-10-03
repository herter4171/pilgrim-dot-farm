"""Operator-phrase API + scheduling (RADIO.md §11, §6.3; TUI.md §6). Fakes only:
rendering uses FakeKokoro + the real (ffmpeg) normalize path; no LLM/news/song
backend is ever contacted (AGENTS rule 2)."""
from __future__ import annotations

import asyncio
import pathlib

import pytest
from fastapi.testclient import TestClient
from pilgrim.config import RNG, Clock
from pilgrim.pipelines.operator_phrase import PhraseManager, phrase_hash
from pilgrim.pipelines.voice import VoicePipeline
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector
from pilgrim.server import create_app
from pilgrim.tests.conftest import make_item
from pilgrim.tests.fakes import FakeKokoro, FakeLLM

TOKEN = "sekrit"
TEXT = ("This is the DJ outside the system telling you that I am grateful "
        "for all of your requests and will fill as many as I can.")


@pytest.fixture
def dj_env(tmp_env, monkeypatch):
    monkeypatch.setenv("PILGRIM_DJ_TOKEN", TOKEN)
    return tmp_env


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def _scheduler(cfg, store):
    return Scheduler(cfg, store, RandomSelector(RNG(1), cfg), Clock(), RNG(1))


def _manager(cfg, store, scheduler, scratch="/tmp/man_scratch", kokoro=None):
    kokoro = kokoro or FakeKokoro()
    voice = VoicePipeline(cfg, FakeLLM(), kokoro)
    md = pathlib.Path(scratch)
    md.mkdir(parents=True, exist_ok=True)
    return PhraseManager(cfg, store, voice, md, scheduler=scheduler)


# ----------------------------------------------------------------------- validate
def test_validate_cleanup_and_hash(cfg, tmp_env):
    _, store, _ = tmp_env
    m = _manager(cfg, store, _scheduler(cfg, store))
    v = m.validate(cfg.dj.character.id, TEXT)
    assert v["ok"] is True
    assert "outside the system" in v["cleaned_text"]  # verbatim, never rewritten
    assert v["text_hash"] == phrase_hash(cfg.dj.character.id, v["cleaned_text"], m._version)


def test_validate_rejects_bad_input(cfg, tmp_env):
    _, store, _ = tmp_env
    m = _manager(cfg, store, _scheduler(cfg, store))
    assert m.validate(cfg.dj.character.id, "visit https://evil.example")["ok"] is False
    assert m.validate(cfg.dj.character.id, "   \n  ")["ok"] is False
    assert m.validate("other_char", TEXT)["rejected_reason"] == "unknown_character"
    v = m.validate(cfg.dj.character.id, "word " * 400)
    assert v["rejected_reason"] == "too_long"


# ------------------------------------------------------------------------- submit
def test_submit_job_lifecycle(cfg, tmp_env):
    _, store, _ = tmp_env
    m = _manager(cfg, store, _scheduler(cfg, store))
    v = m.validate(cfg.dj.character.id, TEXT)
    r = m.submit("cmd-1", cfg.dj.character.id, TEXT, v["text_hash"])
    assert r["status"] == "queued"
    # idempotent retry returns the same job
    assert m.submit("cmd-1", cfg.dj.character.id, TEXT, v["text_hash"])["job_id"] == r["job_id"]
    # different content, same command_id -> conflict
    v2 = m.validate(cfg.dj.character.id, "Totally different words here.")
    with pytest.raises(ValueError, match="conflict"):
        m.submit("cmd-1", cfg.dj.character.id, "Totally different words here.", v2["text_hash"])
    job = m.get(r["job_id"])
    assert job["cleaned_text"] == v["cleaned_text"]
    assert job["voice"] == cfg.dj.character.voice
    # hash mismatch is refused
    with pytest.raises(ValueError, match="hash_mismatch"):
        m.submit("cmd-x", cfg.dj.character.id, TEXT, "bogus-hash")


# ------------------------------------------------------------- render + schedule
def test_render_and_place_end_to_end(cfg, tmp_env):
    _, store, tmp_path = tmp_env
    cfg.playout.committed_lookahead_s = 100
    s1 = make_item(cfg, store, "song", 120.0)
    store.append_program(s1, "song", 120.0)
    sched = _scheduler(cfg, store)
    sched.commit_lookahead()
    rev_before = sched.revision
    m = _manager(cfg, store, sched, scratch=tmp_path / "library",
                 kokoro=FakeKokoro())
    v = m.validate(cfg.dj.character.id, TEXT)
    sid = m.submit("cmd-e2e", cfg.dj.character.id, TEXT, v["text_hash"])
    asyncio.run(m.process_ready())
    job = m.get(sid["job_id"])
    assert job["status"] == "ready"
    assert job["media_id"] is not None
    assert sched.pending_phrases  # handed off, not yet placed
    sched._drain_phrases()
    assert not sched.pending_phrases
    placed = store.program_since(1)
    assert any(r["item_id"] == job["media_id"] and r["type"] == "operator_phrase"
               for r in placed)
    assert sched.revision > rev_before
    it = store.get_item(job["media_id"])
    assert it["fresh"] == 0  # one-shot, consumed on placement
    assert m.get(sid["job_id"])["status"] == "scheduled"
    assert (pathlib.Path(it["media_path"])).exists()
    # FLAC artifact landed in the media dir
    assert list((tmp_path / "library").glob("*.flac"))


def test_place_phrase_deferred_after_news(cfg, tmp_env):
    """A phrase never airs adjacent to news: committed news tail -> held (None)."""
    _, store, _ = tmp_env
    sched = _scheduler(cfg, store)  # constructor clears committed program
    nid = store.add_item(type_="news", media_path="n.flac", duration_s=30,
                         gravity="normal")
    store.append_program(nid, "news", 30.0)
    sched._rebuild_program()
    pid = store.add_item(type_="operator_phrase", media_path="p.flac",
                         duration_s=10, role="operator_phrase")
    assert sched.place_phrase(pid, 10.0) is None


def test_place_phrase_deferred_adjacent_dj_talk(cfg, tmp_env):
    _, store, _ = tmp_env
    sched = _scheduler(cfg, store)
    d1 = store.add_item(type_="dj_talk", media_path="d1.flac", duration_s=15)
    d2 = store.add_item(type_="dj_talk", media_path="d2.flac", duration_s=15)
    store.append_program(d1, "dj_talk", 15.0)
    store.append_program(d2, "dj_talk", 15.0)
    sched._rebuild_program()
    pid = store.add_item(type_="operator_phrase", media_path="p.flac",
                         duration_s=10, role="operator_phrase")
    assert sched.place_phrase(pid, 10.0) is None


def test_place_phrase_tail_only_never_mid_window(cfg, tmp_env):
    """Client-visibility rule (TUI.md §9.2): legacy clients merge fetched rows
    by seq only, so a splice inside rows they already fetched is never heard
    (and shifted rows can double-play). Placement is therefore tail-only: a
    mid-window legal slot must NOT be used, and no later row's seq may shift."""
    _, store, _ = tmp_env
    sched = _scheduler(cfg, store)
    a = store.add_item(type_="song", media_path="a.flac", duration_s=120)
    b = store.add_item(type_="song", media_path="b.flac", duration_s=120)
    c = store.add_item(type_="liner", media_path="c.flac", duration_s=5)
    store.append_program(a, "song", 120.0)   # on air
    store.append_program(b, "song", 120.0)   # old code spliced here (A|B)
    store.append_program(c, "liner", 5.0)    # committed tail
    sched._rebuild_program()
    seq_b = store.program_since(1)[1]["seq"]
    pid = store.add_item(type_="operator_phrase", media_path="p.flac",
                         duration_s=10, role="operator_phrase")
    placed = sched.place_phrase(pid, 10.0)
    # legal mid-window slot (after on-air song A) is refused; tail (liner C)
    # is used instead — a brand-new seq every client receives on next fetch
    assert placed is not None
    assert placed == seq_b + 2  # after C, i.e. max+1, not between A and B
    rows = store.program_since(1)
    assert rows[1]["seq"] == seq_b and rows[1]["item_id"] == b  # no shift
    assert rows[-1]["item_id"] == pid and rows[-1]["type"] == "operator_phrase"


def test_place_phrase_deferred_when_tail_illegal(cfg, tmp_env):
    """Tail pred is news -> held (None); after a legal tail commits, placed."""
    _, store, _ = tmp_env
    sched = _scheduler(cfg, store)
    a = store.add_item(type_="song", media_path="a.flac", duration_s=120)
    n = store.add_item(type_="news", media_path="n.flac", duration_s=30,
                       gravity="normal")
    ln = store.add_item(type_="liner", media_path="l.flac", duration_s=5)
    store.append_program(a, "song", 120.0)
    store.append_program(n, "news", 30.0)    # committed tail: news
    sched._rebuild_program()
    pid = store.add_item(type_="operator_phrase", media_path="p.flac",
                         duration_s=10, role="operator_phrase")
    assert sched.place_phrase(pid, 10.0) is None  # tail pred = news
    store.append_program(ln, "liner", 5.0)         # window extends
    sched._rebuild_program()
    assert sched.place_phrase(pid, 10.0) is not None


def test_phrase_job_aired_when_playhead_passes(cfg, tmp_env):
    """Lifecycle fix: the job reaches the declared terminal 'aired' state when
    its committed row falls behind the playhead (previously nothing ever
    transitioned past 'scheduled')."""
    from pilgrim.config import SimClock
    _, store, tmp_path = tmp_env
    clock = SimClock()
    sched = Scheduler(cfg, store, RandomSelector(RNG(2), cfg), clock, RNG(2))
    s = make_item(cfg, store, "song", 30.0)
    store.append_program(s, "song", 30.0)
    sched._rebuild_program()
    m = _manager(cfg, store, sched, scratch=tmp_path / "library",
                 kokoro=FakeKokoro())
    v = m.validate(cfg.dj.character.id, TEXT)
    sid = m.submit("cmd-aired", cfg.dj.character.id, TEXT, v["text_hash"])
    asyncio.run(m.process_ready())
    assert m.get(sid["job_id"])["status"] == "ready"
    sched.on_phrase_aired = m.mark_item_aired  # server.py wiring
    sched._drain_phrases()
    job = m.get(sid["job_id"])
    assert job["status"] == "scheduled"
    # the committed window keeps extending: add more program so the phrase
    # row is not the last row (the playhead saturates at the program end)
    s2 = make_item(cfg, store, "song", 60.0)
    store.append_program(s2, "song", 60.0)
    sched._rebuild_program()
    cfg.playout.window_trim_keep_s = 5
    # playhead passes song (30s) + phrase (10s) + the keep window
    clock.advance(30 + 10 + 60 + 6)
    sched._trim()
    assert m.get(sid["job_id"])["status"] == "aired"


def test_phrase_job_expires_from_ready_and_scheduled(cfg, tmp_env):
    """A TTL-passed job must never sit 'ready'/'scheduled' forever — a placed
    row that was pruned unheard (client never saw it) still expires."""
    import time as _time
    _, store, tmp_path = tmp_env
    sched = _scheduler(cfg, store)
    s = make_item(cfg, store, "song", 30.0)
    store.append_program(s, "song", 30.0)
    sched._rebuild_program()
    m = _manager(cfg, store, sched, scratch=tmp_path / "library",
                 kokoro=FakeKokoro())
    v = m.validate(cfg.dj.character.id, TEXT)
    # placed ('scheduled'), then TTL passes: still expires, no ghost forever
    sid = m.submit("cmd-ttl", cfg.dj.character.id, TEXT, v["text_hash"])
    asyncio.run(m.process_ready())
    jid = sid["job_id"]
    assert m.get(jid)["status"] == "ready"
    sched._drain_phrases()
    assert m.get(jid)["status"] == "scheduled"
    m._jobs[jid]["expires_at"] = _time.time() - 1
    assert m.get(jid)["status"] == "expired"
    assert m.get(jid)["failure_reason"] == "phrase TTL expired before it aired"
    # unplaced 'ready' (tail now blocked by the placed phrase): also expires
    sid2 = m.submit("cmd-ttl2", cfg.dj.character.id, TEXT, v["text_hash"])
    asyncio.run(m.process_ready())
    jid2 = sid2["job_id"]
    assert m.get(jid2)["status"] == "ready"
    m._jobs[jid2]["expires_at"] = _time.time() - 1
    assert m.get(jid2)["status"] == "expired"


# ----------------------------------------------------------------------- API
def test_character_and_phrases_api(dj_env, monkeypatch):
    cfg, _, _ = dj_env
    app = create_app(cfg)
    c = TestClient(app)
    st = app.state.station

    async def _voices():
        # real Kokoro /voices shape: {voices: [...], default: ...} (probe §2)
        return {"voices": ["am_adam", "am_liam", "am_michael"],
                "default": "af_alloy"}

    monkeypatch.setattr(st.kokoro, "voices", _voices)
    ch = c.get("/api/admin/dj/character", headers=_auth())
    assert ch.status_code == 200
    body = ch.json()
    assert body["name"] == "Jimmany"
    assert body["setup_complete"] is True
    assert "am_adam" in body["available_voices"]
    assert "am_liam" not in body["available_voices"]  # already a role voice

    val = c.post("/api/admin/dj/phrases/validate", headers=_auth(),
                 json={"character_id": cfg.dj.character.id, "text": TEXT})
    assert val.status_code == 200
    vj = val.json()
    assert vj["ok"] is True
    sub = c.post("/api/admin/dj/phrases", headers=_auth(),
                 json={"command_id": "api-1", "character_id": cfg.dj.character.id,
                       "text": TEXT, "cleaned_text_hash": vj["text_hash"]})
    assert sub.status_code == 202
    sj = sub.json()
    assert sj["status"] == "queued"
    got = c.get(f"/api/admin/dj/phrases/{sj['job_id']}", headers=_auth())
    assert got.status_code == 200
    assert got.json()["cleaned_text"] == vj["cleaned_text"]
    # bad hash -> 409; empty -> 422; unauthorized -> 401
    bad = c.post("/api/admin/dj/phrases", headers=_auth(),
                 json={"command_id": "api-2", "character_id": cfg.dj.character.id,
                       "text": TEXT, "cleaned_text_hash": "nope"})
    assert bad.status_code == 409
    assert c.get("/api/admin/dj/character").status_code == 401
    assert c.post("/api/admin/dj/phrases/validate",
                  json={"character_id": "x", "text": "hi"}).status_code == 401
    assert c.get("/api/admin/dj/phrases/999999", headers=_auth()).status_code == 404
