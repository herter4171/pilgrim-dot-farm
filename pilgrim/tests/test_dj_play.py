"""Operator "play this song next" (POST /api/admin/dj/play, RADIO §11)."""
from __future__ import annotations

import pytest
from conftest import seed_pool
from fastapi.testclient import TestClient
from pilgrim.server import build_station, create_admin_app
from pilgrim.tests.fakes import FakeLLM

TOKEN = "sekrit"


@pytest.fixture
def dj_env(tmp_env, monkeypatch):
    monkeypatch.setenv("PILGRIM_DJ_TOKEN", TOKEN)
    return tmp_env


def _station(cfg, store):
    station = build_station(cfg)          # clears the program, like production
    station.llm = FakeLLM()  # type: ignore[assignment]
    station.art_worker = None             # no FLUX in this test
    return station


def _auth():
    return {"Authorization": f"Bearer {TOKEN}"}


def test_play_next_splices_a_chosen_song(dj_env):
    cfg, store, tmp = dj_env
    cfg.dj.enabled = True
    seed_pool(store, cfg, n_song=10, n_liner=10, n_com=6, n_dj=5)
    media = tmp / "pick.mp3"
    media.write_bytes(b"fake mp3")
    pick = store.add_item(type_="song", media_path=str(media), duration_s=90.0,
                          genre=list(cfg.songs.genres)[0], title="Pick",
                          artist="The Fakes")
    station = _station(cfg, store)
    station.scheduler.commit_lookahead()
    rev = station.scheduler.revision

    c = TestClient(create_admin_app(cfg, station))
    r = c.post("/api/admin/dj/play", json={"media_id": pick}, headers=_auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["revision"] == rev + 1

    prog = c.get("/api/station/program").json()["items"]
    assert any(it["media_id"] == pick for it in prog)
    # and the item is reachable through the public snapshot too
    seq = body["seq"]
    assert any(it["seq"] == seq and it["media_id"] == pick for it in prog)


def test_play_next_requires_the_operator_token(dj_env):
    cfg, store, tmp = dj_env
    cfg.dj.enabled = True
    media = tmp / "pick.mp3"
    media.write_bytes(b"fake mp3")
    pick = store.add_item(type_="song", media_path=str(media), duration_s=90.0)
    station = _station(cfg, store)
    c = TestClient(create_admin_app(cfg, station))
    assert c.post("/api/admin/dj/play", json={"media_id": pick}).status_code in (401, 403)
    r = c.post("/api/admin/dj/play", json={"media_id": pick},
               headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_play_next_rejects_non_songs(dj_env):
    cfg, store, tmp = dj_env
    cfg.dj.enabled = True
    com = store.add_item(type_="commercial", media_path=str(tmp / "c.mp3"),
                         duration_s=10.0)
    station = _station(cfg, store)
    station.scheduler.commit_lookahead()
    c = TestClient(create_admin_app(cfg, station))
    r = c.post("/api/admin/dj/play", json={"media_id": com}, headers=_auth())
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "cannot_place"


def test_play_next_is_closed_when_dj_is_disabled(dj_env):
    cfg, store, tmp = dj_env
    cfg.dj.enabled = False
    media = tmp / "pick.mp3"
    media.write_bytes(b"fake mp3")
    pick = store.add_item(type_="song", media_path=str(media), duration_s=90.0)
    station = _station(cfg, store)
    c = TestClient(create_admin_app(cfg, station))
    r = c.post("/api/admin/dj/play", json={"media_id": pick}, headers=_auth())
    assert r.status_code == 403
