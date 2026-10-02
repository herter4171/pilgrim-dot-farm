"""SFX overlays + field reporter (SFX.md). Fakes only (AGENTS rule 2); ffmpeg
via the `make test` PATH for the render/normalize path."""
from __future__ import annotations

import json
from typing import Any

import pytest
from conftest import make_item
from fastapi.testclient import TestClient
from pilgrim.config import RNG, SimClock
from pilgrim.pipelines.sfx import SfxPipeline, beat_offset, parse_cues
from pilgrim.pipelines.voice import VoicePipeline
from pilgrim.producer import Producer
from pilgrim.scheduler import Scheduler
from pilgrim.selector import PlayoutState, RandomSelector
from pilgrim.server import create_app
from pilgrim.sfx_plan import plan_overlays
from pilgrim.tests.fakes import FakeKokoro, FakeLLM, FakeSfx

TEXT = ("The roosters trounced the porch light again. Farmer Jenkins says the "
        "pivot is soaking the north forty. I would tell you a tractor joke, but "
        "it is a bit haywire. Anyway, more music.")


# ----------------------------------------------------------------- parsing
def test_parse_cues_keeps_only_valid_allowed_cues():
    obj = {"sfx": [{"cue": "cow", "after_sentence": 1},
                   {"cue": "laser", "after_sentence": 2},     # not allowed
                   {"cue": "pig", "after_sentence": 9},       # out of range
                   {"cue": "pig", "after_sentence": 1},       # duplicate beat
                   {"cue": "pig"},                            # malformed
                   "pig"],
           "joke_after_sentence": 3}
    cues, joke = parse_cues(obj, TEXT, {"cow", "pig"}, joke_ok=True)
    assert cues == [{"cue": "cow", "after_sentence": 1}]
    assert joke == 3
    _, joke = parse_cues(obj, TEXT, {"cow"}, joke_ok=False)
    assert joke is None
    _, joke = parse_cues({"joke_after_sentence": True}, TEXT, set(), joke_ok=True)
    assert joke is None  # bools aren't sentence numbers


def test_beat_offset_is_ordered_and_inside_the_clip():
    offs = [beat_offset(TEXT, k, 12.0, 0.15) for k in range(1, 5)]
    assert offs == sorted(offs)
    assert 0.15 < offs[0] < offs[-1] <= 12.0


# ------------------------------------------------------------------ policy
def _cue(item_id, off, kind="stinger", dur=2.0):
    return {"item_id": item_id, "cue": "cow", "kind": kind, "offset_s": off,
            "duration_s": dur}


def test_plan_never_puts_sfx_on_news(cfg):
    out, added = plan_overlays("news", 0.0, 20.0, [_cue(1, 2.0)], [], RNG(1), cfg.sfx)
    assert out == [] and added == []


def test_plan_joke_is_a_fair_coin(cfg):
    rng = RNG(3)
    hits = sum(bool(plan_overlays("dj_talk", 100.0 * i, 20.0, [_cue(1, 5.0, "joke")],
                                  [], rng, cfg.sfx)[0]) for i in range(2000))
    assert 0.45 < hits / 2000 < 0.55


def test_plan_rate_budget_and_join_safety(cfg):
    cues = [_cue(i, 0.5 + i * 1.0, dur=0.8) for i in range(6)]  # 6 hits in ~6 s
    cues.append(_cue(99, 9.5, dur=2.0))                            # would overrun host
    out, added = plan_overlays("dj_talk", 0.0, 10.0, cues, [], RNG(1), cfg.sfx)
    assert len(out) == cfg.sfx.max_per_window == len(added)
    assert all(o["offset_s"] + o["duration_s"] <= 10.0 for o in out)
    # the budget is rolling across hosts: recent stingers count
    # (stingers at 0.5/1.5/2.5 s; a hit at 3.0 s would be the 4th in 10 s)
    out2, _ = plan_overlays("liner", 2.6, 5.0, [_cue(7, 0.4, dur=1.0)], added,
                            RNG(1), cfg.sfx)
    assert out2 == []
    out3, _ = plan_overlays("liner", 10.0, 5.0, [_cue(7, 0.6, dur=1.0)], added,
                            RNG(1), cfg.sfx)
    assert len(out3) == 1  # 10.6 s: the 0.5 s hit has rolled out of the window


def test_plan_bed_is_exempt_and_clipped_to_host(cfg):
    cues = [_cue(i, 0.5 + i * 0.5, dur=0.4) for i in range(3)] + [
        _cue(50, 0.0, kind="bed", dur=30.0)]
    out, added = plan_overlays("field_report", 0.0, 12.0, cues, [], RNG(1), cfg.sfx)
    bed = [o for o in out if o["kind"] == "bed"]
    assert len(bed) == 1 and bed[0]["duration_s"] == 12.0
    assert bed[0]["gain"] == cfg.sfx.bed_gain and bed[0]["duck"] == 1.0
    assert len(added) == 3


# ------------------------------------------------------------------ render
def _approve_all(cfg):
    for c in cfg.sfx.cues.values():
        c.approved = True


async def test_render_produces_normalized_flac(tmp_env):
    cfg, _, tmp = tmp_env
    fake = FakeSfx()
    pipe = SfxPipeline(cfg, fake, tmp)  # type: ignore[arg-type]
    r = await pipe.render("cow", seed=5)
    assert r["media_path"].endswith(".flac") and r["meta"]["cue"] == "cow"
    assert 1.0 < r["duration_s"] < 3.0
    assert fake.calls[0][2] == 5
    # a cue's own fixed seed wins over the caller's
    await pipe.render("slide_whistle", seed=5)
    assert fake.calls[-1][2] == cfg.sfx.cues["slide_whistle"].seed
    cfg.sfx.cues["rimshot"].approved = False  # pulled by ear
    with pytest.raises(ValueError):
        await pipe.render("rimshot")


# ---------------------------------------------------------------- producer
class CueVoice:
    """produce_item fake whose script asks for cues + a joke."""

    def __init__(self, cues=None, joke=None):
        self.cues = cues or []
        self.joke = joke

    async def produce_item(self, role, target_s, context=None):
        return {"media_path": f"/tmp/{role}.flac", "duration_s": 15.0,
                "sample_rate": 24000, "channels": 1, "role": role,
                "meta": {"text": TEXT, "sfx_cues": list(self.cues),
                         "joke_after_sentence": self.joke}}


def _producer(cfg, store, tmp, voice, sfx_fake=None) -> tuple[Producer, FakeSfx]:
    sink: Any = object()
    fake = sfx_fake or FakeSfx()
    pipe = SfxPipeline(cfg, fake, tmp)  # type: ignore[arg-type]
    prod = Producer(cfg, store, llm=sink, kokoro=sink, voice=voice, songs=sink,
                    clock=SimClock(), prompts={}, media_dir=tmp, api_key="",
                    rng=RNG(1), news_pipeline=sink, sfx=pipe)
    return prod, fake


async def _drain(prod) -> None:
    """Run the SFX worker until it has nothing left to do."""
    for _ in range(100):
        if not await prod.sfx_step():
            return


def _meta(store, type_):
    it = store.list_items(type_)[-1]
    return json.loads(it["meta_json"] or "{}")


async def test_sfx_pool_renders_only_approved_evergreen_once(tmp_env):
    cfg, store, tmp = tmp_env
    for name, c in cfg.sfx.cues.items():               # slide whistle only
        c.approved = name == "slide_whistle" or not c.evergreen
    prod, fake = _producer(cfg, store, tmp, CueVoice())
    assert await prod.ensure_sfx_pool() == 1
    assert await prod.ensure_sfx_pool() == 0          # idempotent
    assert prod.counts()["sfx"] == prod.sfx_stock_target() == 1
    assert len(fake.calls) == 1


async def test_dj_talk_gets_contextual_and_joke_overlays(tmp_env):
    cfg, store, tmp = tmp_env
    _approve_all(cfg)
    voice = CueVoice(cues=[{"cue": "rooster", "after_sentence": 1}], joke=3)
    prod, fake = _producer(cfg, store, tmp, voice)
    await prod.ensure_dj()
    # stored airable before any SFX render: talk never waits on the backend
    assert _meta(store, "dj_talk").get("sfx_pending") and fake.calls == []
    await _drain(prod)                                 # rooster + stock rimshot
    assert "sfx_pending" not in _meta(store, "dj_talk")
    sfx = _meta(store, "dj_talk")["sfx"]
    kinds = {s["kind"]: s for s in sfx}
    assert set(kinds) == {"stinger", "joke"}
    assert kinds["stinger"]["offset_s"] < kinds["joke"]["offset_s"] < 15.0
    rooster = store.get_item(kinds["stinger"]["item_id"])
    assert rooster["type"] == "sfx" and not rooster["evergreen"]  # rendered fresh
    rim = store.get_item(kinds["joke"]["item_id"])
    assert rim["evergreen"]                                       # from stock


async def test_field_report_gets_a_bed_and_news_never_gets_sfx(tmp_env):
    cfg, store, tmp = tmp_env
    _approve_all(cfg)
    prod, _ = _producer(cfg, store, tmp, CueVoice(cues=[{"cue": "pig", "after_sentence": 1}]))
    await prod.ensure_field_reports()
    await _drain(prod)
    sfx = _meta(store, "field_report")["sfx"]
    assert {s["kind"] for s in sfx} == {"stinger", "bed"}
    item: dict = {"duration_s": 15.0, "meta": {"text": TEXT, "sfx_cues": [
        {"cue": "pig", "after_sentence": 1}]}}
    await prod._attach_sfx(item, "news")
    assert "sfx" not in item["meta"]


async def test_sfx_outage_means_the_host_airs_dry(tmp_env):
    cfg, store, tmp = tmp_env
    _approve_all(cfg)
    prod, _ = _producer(cfg, store, tmp,
                        CueVoice(cues=[{"cue": "cow", "after_sentence": 1}]),
                        sfx_fake=FakeSfx(fail=True))
    await prod.ensure_field_reports()
    await _drain(prod)
    assert store.list_items("field_report")
    assert "sfx" not in _meta(store, "field_report")
    assert "sfx_pending" not in _meta(store, "field_report")  # tried once, airs dry


# ------------------------------------------------------------------- voice
async def test_field_report_voice_and_catch_phrase(cfg, tmp_path):
    llm: Any = FakeLLM(responses={"Write the copy": {
        "text": "The south forty is damp. Jenkins has a new pivot.",
        "est_duration_s": 6, "sfx": [{"cue": "cow", "after_sentence": 1}]}})
    kokoro = FakeKokoro()
    vp = VoicePipeline(cfg, llm, kokoro, media_dir=tmp_path,
                       prompts={"field": "You are {name}.", "voice": "dj",
                                "sfx_cues": "Cues: {cues}. {joke}", "sfx_joke": "J"})
    copy = await vp.write_copy("field_report", 20.0)
    name = cfg.talk.field_reporter_name
    assert copy["text"].endswith(
        f"This is {name} signing off with a reminder that I'm outstanding in my field.")
    assert copy["sfx_cues"] == [{"cue": "cow", "after_sentence": 1}]
    assert vp._voice_for("field_report") == cfg.voices.field_reporter
    # news never sees cue instructions and never keeps cues
    assert vp._sfx_instructions("news") == ""
    news = await vp.write_copy("news", 20.0)
    assert news["sfx_cues"] == [] and news["joke_after_sentence"] is None


# --------------------------------------------------------------- playout
def _seed_hosts(cfg, store):
    sfx_id = store.add_item(type_="sfx", media_path="s.flac", duration_s=1.5,
                            meta={"cue": "cow"})
    cue = {"item_id": sfx_id, "cue": "cow", "kind": "stinger", "offset_s": 2.0,
           "duration_s": 1.5}
    store.add_item(type_="field_report", media_path="f.flac", duration_s=20.0,
                   meta={"sfx": [cue]})
    store.add_item(type_="news", media_path="n.flac", duration_s=20.0,
                   meta={"sfx": [cue]})  # must be ignored
    return sfx_id


def test_scheduler_commits_overlays_but_never_on_news(tmp_env):
    cfg, store, _ = tmp_env
    sfx_id = _seed_hosts(cfg, store)
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), SimClock(), RNG(1))
    for type_ in ("field_report", "news"):
        for e in sched._materialize(type_):
            sched._append(e)
    sched._rebuild_program()
    rows = store.program_since(1)
    by_type = {r["type"]: sched.overlays_for(r["seq"]) for r in rows}
    assert [o["media_id"] for o in by_type["field_report"]] == [sfx_id]
    assert by_type["news"] == []


def test_missing_sfx_item_is_skipped_not_a_stall(tmp_env):
    cfg, store, _ = tmp_env
    store.add_item(type_="field_report", media_path="f.flac", duration_s=20.0,
                   meta={"sfx": [{"item_id": 4242, "kind": "stinger", "offset_s": 1.0,
                                  "duration_s": 1.0}]})
    sched = Scheduler(cfg, store, RandomSelector(RNG(1), cfg), SimClock(), RNG(1))
    for e in sched._materialize("field_report"):
        sched._append(e)
    assert store.program_since(1) and sched.overlays_for(store.program_since(1)[0]["seq"]) == []


def test_program_endpoint_exposes_sfx_sidecar(tmp_env):
    cfg, store, _ = tmp_env
    sfx_id = _seed_hosts(cfg, store)
    make_item(cfg, store, "song", 120.0, genre="polka")
    app = create_app(cfg)
    app.state.station.scheduler.commit_lookahead()
    items = TestClient(app).get("/api/station/program").json()["items"]
    assert all("sfx" in it for it in items)
    fr = [it for it in items if it["type"] == "field_report"]
    assert fr and fr[0]["sfx"][0]["media_id"] == sfx_id
    assert set(fr[0]["sfx"][0]) == {"media_id", "cue", "kind", "offset_s",
                                     "duration_s", "gain", "duck"}


# ---------------------------------------------------------------- selector
@pytest.mark.parametrize("last,banned", [("news", "field_report"),
                                         ("field_report", "field_report"),
                                         ("field_report", "news")])
def test_field_report_adjacency(cfg, last, banned):
    sel = RandomSelector(RNG(1), cfg)
    for seed in range(50):
        sel.rng = RNG(seed)
        st = PlayoutState(cfg)
        st.available = {"song": False, "dj_talk": False, "commercial_break": True,
                        "liner": True, "news": True, "field_report": True}
        st.news_valid = True
        st.recent_types = ["song", last]
        assert sel.choose_next(st) != banned


async def test_last_sentence_beat_is_pulled_inside_the_clip(tmp_env):
    """Scripts often cue after their final sentence; the hit must still fit."""
    cfg, store, tmp = tmp_env
    _approve_all(cfg)
    prod, _ = _producer(cfg, store, tmp, CueVoice(cues=[{"cue": "cow", "after_sentence": 4}]))
    await prod.ensure_dj()
    await _drain(prod)
    o = _meta(store, "dj_talk")["sfx"][0]
    assert o["offset_s"] + o["duration_s"] <= 15.0
    assert o["offset_s"] >= cfg.audio.edge_pad_ms / 1000.0


async def test_cue_fields_are_in_the_explicit_schema(cfg, tmp_path):
    """The model follows the user-turn schema, not system prose: without the
    sfx field there it never asked for a cue on the real backend."""
    llm = FakeLLM()
    seen: list[str] = []
    real = llm.chat_json

    async def spy(model, system, user, max_tokens=800):
        seen.append(user)
        return await real(model, system, user, max_tokens)
    llm.chat_json = spy  # type: ignore[method-assign]
    vp = VoicePipeline(cfg, llm, FakeKokoro(), media_dir=tmp_path,
                       prompts={"voice": "dj", "sfx_cues": "Cues: {cues}. {joke}"})
    await vp.write_copy("dj_talk", 18.0)
    await vp.write_copy("news", 18.0)
    assert '"sfx"' in seen[0]
    assert '"sfx"' not in seen[1]
