"""Cover-art backfill tool (ALBUM_ART.md Step 9), fakes only."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from pilgrim.album_art import LEASE_KEY
from pilgrim.tests.fakes import FakeLLM
from pilgrim.tests.fakes.fake_flux import FakeFlux
from pilgrim.tools import art_backfill

ROOT = Path(__file__).resolve().parent.parent
PROMPTS = {"album_art_scene": (ROOT / "prompts" / "album_art_scene.md").read_text(),
           "album_art_flux": (ROOT / "prompts" / "album_art_flux.md").read_text()}
GOOD_SCENE = ("A rust-orange risograph print of a dented grain silo at dusk, its hatch "
              "propped open on a tangle of extension cords, three crows perched along "
              "the rim; flat limited palette of rust, bone, midnight blue and sage, "
              "heavy paper grain and hard-edged shapes.")


def _song(store, tmp, n, **kw):
    return store.add_item(type_="song", media_path=str(tmp / f"s{n}.mp3"),
                          duration_s=1, title=f"Song {n}", artist="The Fakes",
                          created_at=f"2026-01-0{n}T00:00:00+00:00", **kw)


def _args(**kw):
    # Mirrors the argparse defaults in tools/art_backfill.py (--scene was added
    # in 724577f; the helper was not updated, so run() crashed on args.scene).
    base = {"apply": False, "limit": 10, "item": None, "force": False,
            "seed": None, "scene": None, "ignore_lease": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_dry_run_lists_without_rendering(tmp_env, capsys):
    cfg, store, tmp = tmp_env
    _song(store, tmp, 1)
    _song(store, tmp, 2)
    rc = art_backfill.asyncio.run(art_backfill.run(cfg, store, _args()))
    out = capsys.readouterr().out
    assert rc == 0 and "2 song(s) would be rendered" in out
    assert store.art_counts() == {"with_art": 0, "without_art": 2}


def test_item_target_must_be_an_eligible_song(tmp_env, capsys):
    cfg, store, tmp = tmp_env
    iid = store.add_item(type_="commercial", media_path=str(tmp / "c.mp3"),
                         duration_s=1, title="C", artist="A")
    import asyncio
    rc = asyncio.run(art_backfill.run(cfg, store, _args(item=iid)))
    assert rc == 0 and "not an eligible song" in capsys.readouterr().err


def test_item_with_art_needs_force(tmp_env, capsys):
    cfg, store, tmp = tmp_env
    iid = _song(store, tmp, 1)
    store.set_art(iid, "/art/s.jpg", {"scene": "s"})
    import asyncio
    asyncio.run(art_backfill.run(cfg, store, _args(item=iid)))
    assert "already has art" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_apply_renders_and_is_resumable(tmp_env, monkeypatch):
    cfg, store, tmp = tmp_env
    cfg.art.size = "64x64"
    cfg.art.scene_attempts = 1
    _song(store, tmp, 1)
    _song(store, tmp, 2)
    _song(store, tmp, 3)

    fake = FakeFlux(size=(64, 64))
    monkeypatch.setattr(art_backfill, "_load_prompts", lambda cfg: PROMPTS)
    monkeypatch.setattr(art_backfill, "_api_key", lambda cfg: "")
    monkeypatch.setattr(art_backfill, "LLM",
                        lambda cfg, api_key: FakeLLM(
                            responses={"Title:": {"scene": GOOD_SCENE}}))
    monkeypatch.setattr(art_backfill, "FluxClient", lambda cfg: fake.client(cfg))

    assert await art_backfill.run(cfg, store, _args(apply=True, limit=2)) == 0
    assert store.art_counts() == {"with_art": 2, "without_art": 1}

    # resumable: the next run picks up exactly the remaining song
    assert await art_backfill.run(cfg, store, _args(apply=True, limit=5)) == 0
    assert store.art_counts() == {"with_art": 3, "without_art": 0}
    assert len(fake.calls) == 3


@pytest.mark.asyncio
async def test_apply_stops_on_a_busy_backend(tmp_env, monkeypatch):
    cfg, store, tmp = tmp_env
    cfg.art.size = "64x64"
    cfg.art.scene_attempts = 1
    for n in (1, 2, 3):
        _song(store, tmp, n)

    fake = FakeFlux(size=(64, 64))
    fake.fail_mode = "memory_pressure"
    monkeypatch.setattr(art_backfill, "_load_prompts", lambda cfg: PROMPTS)
    monkeypatch.setattr(art_backfill, "_api_key", lambda cfg: "")
    monkeypatch.setattr(art_backfill, "LLM",
                        lambda cfg, api_key: FakeLLM(
                            responses={"Title:": {"scene": GOOD_SCENE}}))
    monkeypatch.setattr(art_backfill, "FluxClient", lambda cfg: fake.client(cfg))

    rc = await art_backfill.run(cfg, store, _args(apply=True, limit=3))
    assert rc == 1
    assert len(fake.calls) == 1          # stopped after the first busy answer
    assert store.art_counts()["with_art"] == 0


@pytest.mark.asyncio
async def test_force_rerender_uses_the_given_seed_and_clears_the_key(tmp_env, monkeypatch):
    cfg, store, tmp = tmp_env
    cfg.art.size = "64x64"
    cfg.art.scene_attempts = 1
    iid = _song(store, tmp, 1)
    store.set_art(iid, "/art/old.jpg", {"scene": "old"})
    store.set_art_published(iid, "art/old-abc.jpg")

    fake = FakeFlux(size=(64, 64))
    monkeypatch.setattr(art_backfill, "_load_prompts", lambda cfg: PROMPTS)
    monkeypatch.setattr(art_backfill, "_api_key", lambda cfg: "")
    monkeypatch.setattr(art_backfill, "LLM",
                        lambda cfg, api_key: FakeLLM(
                            responses={"Title:": {"scene": GOOD_SCENE}}))
    monkeypatch.setattr(art_backfill, "FluxClient", lambda cfg: fake.client(cfg))

    assert await art_backfill.run(
        cfg, store, _args(apply=True, item=iid, force=True, seed=7)) == 0
    item = store.get_item(iid)
    assert item["art_s3_key"] is None          # publisher re-uploads
    import json
    assert json.loads(item["meta_json"])["art"]["seed"] == 7
    assert fake.calls[-1]["seed"] == 7


def test_lease_blocks_apply_until_stale(tmp_env, monkeypatch, capsys):
    cfg, store, tmp = tmp_env
    _song(store, tmp, 1)
    import datetime
    store.meta_set(LEASE_KEY, datetime.datetime.now(datetime.UTC).isoformat())
    assert art_backfill.lease_is_fresh(store)
    store.meta_set(LEASE_KEY, (datetime.datetime.now(datetime.UTC)
                               - datetime.timedelta(seconds=600)).isoformat())
    assert not art_backfill.lease_is_fresh(store)
    store.meta_set(LEASE_KEY, "not-a-date")
    assert not art_backfill.lease_is_fresh(store)


def test_lease_check_refuses_apply(tmp_env, monkeypatch):
    cfg, store, tmp = tmp_env
    _song(store, tmp, 1)
    import datetime
    store.meta_set(LEASE_KEY, datetime.datetime.now(datetime.UTC).isoformat())
    monkeypatch.setattr(art_backfill, "load_config", lambda: cfg)
    monkeypatch.setattr(art_backfill, "Store", lambda path: store)
    monkeypatch.setattr(art_backfill, "setup_logging", lambda cfg: None)
    with pytest.raises(SystemExit) as ei:
        art_backfill.main(["--apply", "--limit", "1"])
    assert ei.value.code == 2
