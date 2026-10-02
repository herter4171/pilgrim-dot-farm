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
