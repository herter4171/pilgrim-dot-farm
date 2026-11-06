"""The Fargate web tier (SCALABILITY.md Step 5; RADIO §11 "Web tier", §14).

A real Station publishes into a FakeRedis; a WebStation serves the public
routes from that same fake and the same store. Fakes only (AGENTS §1.2):
FakeLLM moderation, FakeRedis cache, the test's SQLite/Postgres store."""
from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import seed_pool
from fastapi.testclient import TestClient
from pilgrim.config import Visitors
from pilgrim.db import open_store
from pilgrim.hotstate import HotState
from pilgrim.server import build_station
from pilgrim.tests.fakes import FakeKokoro, FakeLLM
from pilgrim.tests.fakes.fake_redis import FakeRedis
from pilgrim.web_app import OFF_AIR_BODY, OFF_AIR_HEALTH, WebStation, create_web_app


class _AllowLLM:
    """Moderation LLM that allows everything (no real backend, AGENTS §1.2)."""

    async def chat_json(self, model, system, user, max_tokens=800):
        return {"allowed": True, "reason": "ok"}

    async def close(self):
        pass


def _pair(tmp_env, monkeypatch, *, publish: bool = True):
    """(station, web_station, client, fake_redis): station and web tier share
    one FakeRedis and one database."""
    cfg, store, _ = tmp_env
    monkeypatch.setenv("REDIS_URL", "rediss://cache.test:6379")
    import pilgrim.server as srv
    holder = {}

    def _connect(url, c):
        holder["r"] = FakeRedis(lambda: station.clock.wall())
        return holder["r"]
    monkeypatch.setattr(srv, "cache_connect", _connect)
    station = build_station(cfg)
    station.llm = FakeLLM()  # type: ignore[assignment]
    station.kokoro = FakeKokoro()  # type: ignore[assignment]
    seed_pool(station.db, cfg)
    station.scheduler.commit_lookahead()
    r = holder["r"]
    cfg.station.serve_web = True          # the web tier serves the site itself
    web = WebStation(cfg, open_store(cfg), HotState(cfg, r, epoch="web"), api_key="")
    web.llm = FakeLLM()  # type: ignore[assignment]
    web.moderator.llm = _AllowLLM()  # type: ignore[assignment]
    if publish:
        station.hot_tick()
    client = TestClient(create_web_app(cfg, web))
    return station, web, client, r


def test_off_air_without_a_published_station(tmp_env, monkeypatch):
    station, web, c, r = _pair(tmp_env, monkeypatch, publish=False)
    h = c.get("/api/health")
    assert h.status_code == 200 and h.json() == OFF_AIR_HEALTH      # ALB-safe
    for path in ("/api/station/program", "/api/station/state"):
        resp = c.get(path)
        assert resp.status_code == 503 and resp.json() == OFF_AIR_BODY
        assert resp.headers["cache-control"] == "no-store"
    # the board is Postgres/SQLite, not the station: it still answers
    assert c.get("/api/requests").status_code == 200
    # a heartbeat still lands (player key + airplay row) even while off air
    assert c.post("/api/station/heartbeat",
                  json={"player_id": "p-off", "seq": 1, "media_id": 1}).status_code == 200
    assert r.get("pilgrim:player:p-off") is not None


def test_program_and_state_come_from_the_cache(tmp_env, monkeypatch):
    station, web, c, r = _pair(tmp_env, monkeypatch)
    direct = station.program_response(None)
    via = c.get("/api/station/program")
    assert via.status_code == 200
    assert via.headers["cache-control"] == "public, max-age=1"
    body = via.json()
    assert body["epoch"] == station.epoch and body["revision"] == direct["revision"]
    assert [it["seq"] for it in body["items"]] == [it["seq"] for it in direct["items"]]
    assert all(it["url"] for it in body["items"])
    assert all("rating_count" in it and "rating_avg" in it for it in body["items"])
    assert body["reset"] is False
    # incremental cursor: only rows after after_seq, offset 0
    first = body["items"][0]["seq"]
    inc = c.get(f"/api/station/program?after_seq={first}&epoch={body['epoch']}"
                f"&revision={body['revision']}").json()
    assert inc["items"] and all(it["seq"] > first for it in inc["items"])
    assert inc["start_offset_s"] == 0.0 and inc["reset"] is False
    # stale cursor: full window with reset
    stale = c.get(f"/api/station/program?after_seq={first}&epoch=other").json()
    assert stale["reset"] is True and stale["items"][0]["seq"] == first
    st = c.get("/api/station/state").json()
    assert st["epoch"] == station.epoch and st["current_seq"] == first
    assert c.get("/api/health").json()["on_air"] is True


def test_heartbeat_reaches_the_station_and_wake_thaws(tmp_env, monkeypatch):
    station, web, c, r = _pair(tmp_env, monkeypatch)
    resp = c.post("/api/station/heartbeat",
                  json={"player_id": "web-1", "seq": 1, "media_id": 1,
                        "platform": "ios", "hidden": True})
    assert resp.json() == {"ok": True}
    key = json.loads(r.get("pilgrim:player:web-1"))
    assert key["platform"] == "ios" and key["hidden"] is True and "at" in key
    # the station counts the web tier's listener on its next tick
    station.hot_tick()
    assert station.active_listeners() == 1
    # airplay row per heartbeat, in the shared store (operator decision)
    assert station.db.recent_airplay(1)[0]["item_id"] == 1
    # frozen station: a programme fetch through the web tier wakes it
    station.scheduler.freeze()
    station._idle_since = station.clock.now()
    station.hot_tick()                      # publishes idle: true
    assert c.get("/api/station/state").json()["idle"] is True
    assert c.get("/api/station/program").status_code == 200   # frozen window is playable
    assert r.get("pilgrim:wake") is not None
    station.hot_tick()
    assert not station.scheduler.frozen and station.idle_wakes == 1
    station.hot_tick()  # publish the thawed state (hot_loop does this at once after a wake)
    assert c.get("/api/station/state").json()["idle"] is False


def test_board_requests_and_stages_are_shared(tmp_env, monkeypatch):
    station, web, c, r = _pair(tmp_env, monkeypatch)
    resp = c.post("/api/requests", json={"text": "a song about the web tier"})
    assert resp.status_code == 200 and resp.json()["ok"] is True
    rid = resp.json()["request"]["id"]
    # the station's store sees it (same database)
    assert [q["id"] for q in station.db.queued_requests()] == [rid]
    # the producer's in-flight stage travels through the cache
    station.producer.request_stage[rid] = "writing"
    station.db.mark_request_producing(rid)
    station.hot_tick()
    row = next(q for q in c.get("/api/requests").json()["queue"] if q["id"] == rid)
    assert row["stage"] == "writing"


def test_rating_window_follows_the_published_state(tmp_env, monkeypatch):
    station, web, c, r = _pair(tmp_env, monkeypatch)
    st = c.get("/api/station/state").json()
    body = {"voter_id": "a" * 32, "epoch": st["epoch"], "seq": st["current_seq"], "stars": 4}
    resp = c.post("/api/station/rating", json=body)
    if st["current_type"] == "song":
        assert resp.status_code == 200 and resp.json()["ok"] is True
    else:
        assert resp.status_code == 409 and resp.json()["code"] == "rating_closed"
    # wrong epoch is always closed
    assert c.post("/api/station/rating",
                  json={**body, "epoch": "nope"}).status_code == 409


def test_media_admin_and_site(tmp_env, monkeypatch):
    cfg, _, _ = tmp_env
    station, web, c, r = _pair(tmp_env, monkeypatch)
    some = station.db.list_items("song")[0]["id"]
    assert c.get(f"/api/media/{some}").status_code == 404      # no origin media here
    assert c.get("/api/admin/dj/state").status_code == 404
    assert c.post("/api/station/start").status_code == 404
    assert c.get("/").status_code == 200                       # static site (serve_web)


def test_client_ip_prefers_cf_connecting_ip_from_a_trusted_peer(tmp_env, monkeypatch):
    station, web, c, r = _pair(tmp_env, monkeypatch, publish=False)
    web.cfg.visitors = Visitors(trusted_proxies=["172.31.0.0/16"])   # the ALB

    def req(peer, **headers):
        return SimpleNamespace(client=SimpleNamespace(host=peer),
                               headers={k.replace("_", "-"): v for k, v in headers.items()})
    alb = "172.31.25.15"
    assert web._client_ip(req(alb, cf_connecting_ip="203.0.113.9",
                              x_forwarded_for="10.0.0.1, 203.0.113.9")) == "203.0.113.9"
    assert web._client_ip(req(alb, x_forwarded_for="198.51.100.7, 1.2.3.4")) == "198.51.100.7"
    assert web._client_ip(req("8.8.8.8", cf_connecting_ip="203.0.113.9")) == "8.8.8.8"


def test_cached_scheduler_rating_window_grace_and_song_start():
    """FIX_RATINGS.md Step 1 (VOTING.md V8 over the published snapshot): the
    grace branch reads `_cum` and `position()`, which the cached scheduler
    must provide; a rating for a song the snapshot has not reached is closed."""
    from pilgrim.voting import rating_window
    from pilgrim.web_app import CachedScheduler
    program = {"items": [
        {"seq": 10, "type": "song", "media_id": 2928, "duration_s": 180.0},
        {"seq": 11, "type": "liner", "media_id": 55, "duration_s": 8.0},
        {"seq": 12, "type": "song", "media_id": 777, "duration_s": 200.0},
    ]}

    def window(seq, offset, ask_seq):
        st = {"epoch": "e1", "revision": 1, "current_seq": seq,
              "current_offset_s": offset}
        return rating_window(CachedScheduler(st, program), "e1", ask_seq, 5.0)

    assert window(10, 100.0, 10) == (True, 2928)      # the song is on air
    assert window(11, 2.0, 10) == (True, 2928)        # liner on air, 2 s in: grace
    assert window(11, 6.0, 10) == (False, None)       # past the grace
    assert window(12, 1.0, 10) == (False, None)       # two rows later: closed
    assert window(10, 179.5, 12) == (False, None)     # a future song is closed
    assert window(11, 2.0, 11) == (False, None)       # a liner is never ratable
    sched = CachedScheduler({"epoch": "e1", "current_seq": 11,
                             "current_offset_s": 2.0}, program)
    assert sched._cum == [0.0, 180.0, 188.0]
    assert sched.position() == 182.0
    # a frozen station is closed (IDLE.md), whatever the row
    st = {"epoch": "e1", "current_seq": 10, "current_offset_s": 1.0, "idle": True}
    assert rating_window(CachedScheduler(st, program), "e1", 10, 5.0) == (False, None)
