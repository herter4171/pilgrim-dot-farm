"""Art worker (ALBUM_ART.md Step 5): one song at a time, back-off, no raises."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pilgrim.album_art import LEASE_KEY, ArtWorker, art_dir
from pilgrim.pipelines.art import TYPOGRAPHY_STYLES
from pilgrim.server import build_station, create_admin_app, create_public_app
from pilgrim.tests.fakes import FakeLLM
from pilgrim.tests.fakes.fake_flux import FakeFlux

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = {"album_art_scene": (ROOT / "prompts" / "album_art_scene.md").read_text(),
           "album_art_flux": (ROOT / "prompts" / "album_art_flux.md").read_text()}
GOOD_SCENE = ("A rust-orange risograph print of a dented grain silo at dusk, its hatch "
              "propped open on a tangle of extension cords, three crows perched along "
              "the rim; flat limited palette of rust, bone, midnight blue and sage, "
              "heavy paper grain and hard-edged shapes.")


def _cfg(cfg):
    cfg.art.size = "64x64"
    cfg.art.idle_poll_s = 0.01
    cfg.art.retry_after_s = 0.01
    cfg.art.scene_attempts = 2
    return cfg


def _worker(cfg, store, *, flux=None, llm=None, free_gb=None):
    fake = flux or FakeFlux(size=(64, 64))
    return ArtWorker(cfg, store, llm or FakeLLM(responses={"Title:": {"scene": GOOD_SCENE}}),
                     PROMPTS, flux=fake.client(cfg),
                     free_gb=free_gb or (lambda: 100.0))


def _song(store, tmp, *, title="Biscuits and Gravy (Two-Step Flow)",
          artist="The Dirty Gravy Boys", media="song_1.mp3"):
    return store.add_item(type_="song", media_path=str(tmp / media), duration_s=1,
                          title=title, artist=artist, genre="country-americana",
                          meta={"lyrics": "[verse]\nWoke up slow\n[chorus]\nGravy"})


@pytest.mark.asyncio
async def test_process_renders_and_records_everything(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = _song(store, tmp)
    w = _worker(cfg, store)
    item = store.get_item(iid)
    assert await w.process(item) is True

    item = store.get_item(iid)
    jpg = Path(item["art_path"])
    assert jpg.suffix == ".jpg" and jpg.exists()
    assert jpg.parent == art_dir(cfg)
    assert jpg.read_bytes().startswith(b"\xff\xd8")
    assert (jpg.parent / (jpg.stem + ".png")).exists()
    assert item["art_attempted_at"] is None
    meta = json.loads(item["meta_json"])
    assert meta["art"]["scene"] == GOOD_SCENE
    assert meta["art"]["seed"] == iid * 7919 % 2 ** 31
    assert 'reads exactly "Biscuits and Gravy"' in meta["art"]["prompt"]
    assert meta["art"]["artist_text"] == "The Dirty Gravy Boys"
    assert w.backend == "ok" and w.rendered == 1


@pytest.mark.asyncio
async def test_render_brief_renders_before_the_item_exists(tmp_env):
    """Early art: the producer can render a cover from the brief alone."""
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    w = _worker(cfg, store)
    brief = {
        "title": "Early Bird", "artist": "The Dawn Chorus",
        "genre": "country-americana", "seed": 42, "request_id": 999,
        "lyrics": "[verse]\nup before the sun",
        "style_prompt": "Global Emotional Progression: warm and awake",
    }
    res = await w.render_brief(brief, "req999")
    assert res is not None
    jpg, meta = res
    assert Path(jpg).exists() and Path(jpg).suffix == ".jpg"
    assert meta["scene"] == GOOD_SCENE
    assert meta["seed"] == 42
    assert w.rendered == 1
    # no item was created by the early path
    assert store.all_requests() == []


@pytest.mark.asyncio
async def test_render_brief_records_request_and_typography(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    w = _worker(cfg, store)
    brief = {
        "title": "Two Horses", "artist": "The Fence Sitters",
        "genre": "country-americana", "seed": 7, "request_id": 1000,
        "lyrics": "[verse]\ntwo horses looking on",
        "style_prompt": "Global Emotional Progression: warm and curious",
    }
    res = await w.render_brief(brief, "req1000",
                               request_text="two horses looking on", paid=False)
    assert res is not None
    _, meta = res
    assert meta["request_text"] == "two horses looking on"
    assert meta["paid"] is False
    assert meta["typography"] in TYPOGRAPHY_STYLES


@pytest.mark.asyncio
async def test_process_reads_request_and_paid_from_item_meta(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = store.add_item(
        type_="song", media_path=str(tmp / "meta.mp3"), duration_s=1,
        title="Paid Barn", artist="The Haybalers", genre="country-americana",
        meta={"lyrics": "[verse]\nneigh", "request_text": "a neon city",
              "paid": True})
    w = _worker(cfg, store)
    assert await w.process(store.get_item(iid)) is True
    meta = json.loads(store.get_item(iid)["meta_json"])
    assert meta["art"]["request_text"] == "a neon city"
    assert meta["art"]["paid"] is True
    assert meta["art"]["typography"] in TYPOGRAPHY_STYLES


@pytest.mark.asyncio
async def test_non_latin_title_renders_text_free(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = _song(store, tmp, title="サンドイッチの夢戦隊", artist="The Haybale Heroines")
    w = _worker(cfg, store)
    assert await w.process(store.get_item(iid)) is True
    meta = json.loads(store.get_item(iid)["meta_json"])
    assert "No text, no letters, no typography." in meta["art"]["prompt"]
    assert "reads exactly" not in meta["art"]["prompt"]


@pytest.mark.asyncio
async def test_low_memory_skips_without_penalising_the_song(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = _song(store, tmp)
    fake = FakeFlux(size=(64, 64))
    w = _worker(cfg, store, flux=fake, free_gb=lambda: 5.0)
    assert await w.process(store.get_item(iid)) is False
    assert store.get_item(iid)["art_attempted_at"] is None
    assert fake.calls == []          # never called the renderer


@pytest.mark.asyncio
async def test_scene_failure_backs_off(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = _song(store, tmp)
    w = _worker(cfg, store, llm=FakeLLM(responses={"Title:": {"scene": "a barn"}}))
    assert await w.process(store.get_item(iid)) is False
    item = store.get_item(iid)
    assert item["art_path"] is None and item["art_attempted_at"] is not None
    assert w.failed == 1


@pytest.mark.asyncio
async def test_busy_sets_backend_and_backs_off(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = _song(store, tmp)
    fake = FakeFlux(size=(64, 64))
    fake.fail_mode = "memory_pressure"
    w = _worker(cfg, store, flux=fake)
    assert await w.process(store.get_item(iid)) is False
    assert w.backend == "busy"
    assert store.get_item(iid)["art_attempted_at"] is not None


@pytest.mark.asyncio
async def test_down_backend_backs_off(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = _song(store, tmp)
    fake = FakeFlux(size=(64, 64))
    fake.health_up = False
    w = _worker(cfg, store, flux=fake)
    assert await w.process(store.get_item(iid)) is False
    assert w.backend == "down"
    assert store.get_item(iid)["art_attempted_at"] is not None


@pytest.mark.asyncio
async def test_unexpected_errors_never_escape_process(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = _song(store, tmp)
    fake = FakeFlux(size=(64, 64))
    w = _worker(cfg, store, flux=fake)

    async def boom(*a, **k):
        raise RuntimeError("disk on fire")

    w.flux.generate = boom  # type: ignore[method-assign]
    assert await w.process(store.get_item(iid)) is False
    assert store.get_item(iid)["art_attempted_at"] is not None


@pytest.mark.asyncio
async def test_run_loop_renders_the_committed_song_first_and_writes_a_lease(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    newest = _song(store, tmp, media="newest.mp3")
    committed = _song(store, tmp, media="committed.mp3")
    store.append_program(committed, "song", 1.0)
    w = _worker(cfg, store)
    task = asyncio.create_task(w.run())
    for _ in range(200):
        await asyncio.sleep(0.01)
        if store.get_item(committed)["art_path"] and store.get_item(newest)["art_path"]:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.get_item(committed)["art_path"]
    assert store.get_item(newest)["art_path"]
    assert store.meta_get(LEASE_KEY)


@pytest.mark.asyncio
async def test_run_loop_survives_store_errors(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)

    def boom(*a, **k):
        raise RuntimeError("db hiccup")

    store.songs_needing_art = boom  # type: ignore[method-assign]
    w = _worker(cfg, store)
    task = asyncio.create_task(w.run())
    await asyncio.sleep(0.05)
    assert not task.done()          # still looping, never raised
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ------------------------------------------------------------------ health
def _station(tmp_env, cfg):
    station = build_station(cfg)
    station.llm = FakeLLM()  # type: ignore[assignment]
    return station


def test_public_health_reports_cached_backend_only(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    iid = _song(store, tmp)
    station = _station(tmp_env, cfg)
    station.art_worker = _worker(cfg, store)
    station.art_worker.backend = "busy"

    async def _boom(*a, **k):
        raise AssertionError("public health must not query the DB or FLUX")

    station.db.art_counts = _boom  # type: ignore[method-assign]
    c = TestClient(create_public_app(cfg, station))
    body = c.get("/api/health").json()
    assert body["art"] == {"backend": "busy"}
    assert set(body) == {"on_air", "art", "time"}
    assert iid  # silence unused


def test_admin_health_has_counts(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    a = _song(store, tmp, media="a.mp3")
    _song(store, tmp, media="b.mp3")
    store.set_art(a, "/art/a.jpg", {"scene": "s"})
    station = _station(tmp_env, cfg)
    station.art_worker = _worker(cfg, store)
    station.art_worker.backend = "ok"
    body = station.art_health()
    assert body == {"backend": "ok", "with_art": 1, "without_art": 1}
    c = TestClient(create_admin_app(cfg, station))
    admin = c.get("/api/health").json()
    assert admin["art"]["with_art"] == 1


# --------------------------------------------------------------------- API
def _api_station(tmp_env, cfg, store):
    station = build_station(cfg)
    station.llm = FakeLLM()  # type: ignore[assignment]
    station.art_worker = _worker(cfg, store)
    return station


def test_program_and_state_carry_art_url(tmp_env):
    cfg, store, tmp = tmp_env
    _cfg(cfg)
    cfg.media.public_base_url = "https://cdn.example"
    iid = _song(store, tmp, media="song_20.mp3")
    station = _api_station(tmp_env, cfg, store)   # Scheduler clears the program
    store.append_program(iid, "song", 30.0)
    station.scheduler._rebuild_program()

    prog = station.program_response(None)
    assert prog["items"][0]["art_url"] == ""          # not uploaded yet

    # Written through the station's own store, as the live art worker does:
    # its item cache invalidates on the write (a second process's write shows
    # up within store.ITEM_CACHE_TTL_S instead; test_store_cache.py).
    station.db.set_art(iid, "/art/song_20.jpg", {"scene": "s"})
    station.db.set_art_published(iid, "art/song_20-abc123.jpg")
    prog = station.program_response(None)
    assert prog["items"][0]["art_url"] == "https://cdn.example/art/song_20-abc123.jpg"

    # state: on-air item's art (the committed window starts at the playhead)
    state = station.public_state()
    assert state["current_seq"] == prog["items"][0]["seq"]
    assert state["current_art_url"] == "https://cdn.example/art/song_20-abc123.jpg"

    c = TestClient(create_public_app(cfg, station))
    body = c.get("/api/station/program").json()
    assert body["items"][0]["art_url"].endswith(".jpg")
    assert c.get("/api/station/state").json()["current_art_url"].endswith(".jpg")
