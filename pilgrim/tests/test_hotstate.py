"""Hot state in Valkey (SCALABILITY.md Step 3; RADIO §3, §11, §14).

Fakes only: `FakeRedis` with an injected clock. Covers the key contract, the
TTLs, failure degradation, the listener union and the wake flag."""
from __future__ import annotations

import json

import pytest
from conftest import seed_pool
from pilgrim.config import Cache
from pilgrim.hotstate import HotState
from pilgrim.server import build_station
from pilgrim.tests.fakes import FakeKokoro, FakeLLM
from pilgrim.tests.fakes.fake_redis import FakeRedis
from pydantic import ValidationError


class _Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def _hot(cfg, clock):
    r = FakeRedis(clock)
    return r, HotState(cfg, r, epoch="e1", now=clock)


def test_tick_publishes_the_contract_with_ttls(cfg):
    clock = _Clock()
    r, hot = _hot(cfg, clock)
    out = hot.tick(program={"items": [1]}, state={"epoch": "e1"}, health={"on_air": True})
    assert out == {"players": set(), "wake": None}
    assert hot.healthy is True and hot.ticks == 1
    for name in ("alive", "program", "state", "health"):
        assert r.ttl(f"pilgrim:{name}") == int(cfg.cache.alive_ttl_s)
    assert hot.read("program") == {"items": [1]}
    assert hot.read("state") == {"epoch": "e1"}
    alive = hot.read("alive")
    assert alive["epoch"] == "e1" and alive["at"] == clock.t
    # expiry: nothing left after the TTL
    clock.t += cfg.cache.alive_ttl_s + 1
    assert hot.read("alive") is None and hot.read("program") is None


def test_players_are_written_once_per_tick_and_expire(cfg):
    clock = _Clock()
    r, hot = _hot(cfg, clock)
    hot.queue_player("p1", {"platform": "ios"})
    hot.queue_player("p1", {"platform": "ios", "hidden": True})   # last write wins
    hot.queue_player("p2", {})
    assert hot.tick(program={}, state={}, health={})["players"] == {"p1", "p2"}
    assert json.loads(r.get("pilgrim:player:p1"))["hidden"] is True
    assert r.ttl("pilgrim:player:p1") == int(cfg.visitors.listener_timeout_s)
    # exactly one pipeline per tick, no per-heartbeat round trip
    assert r.calls.count("pipeline") == 1
    clock.t += cfg.visitors.listener_timeout_s + 1
    assert hot.tick(program={}, state={}, health={})["players"] == set()


def test_player_ids_are_bounded_and_validated(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.visitors.max_listeners = 2
    clock = _Clock()
    r, hot = _hot(cfg, clock)
    for pid in ("a", "b", "c"):
        hot.queue_player(pid, {})
    hot.queue_player("", {})
    hot.queue_player("x" * 65, {})
    assert hot.tick(program={}, state={}, health={})["players"] == {"a", "b"}


def test_wake_flag_is_taken_once(cfg):
    clock = _Clock()
    r, hot = _hot(cfg, clock)
    hot.set_wake("program")
    assert r.ttl("pilgrim:wake") == int(cfg.cache.wake_ttl_s)
    assert hot.tick(program={}, state={}, health={})["wake"] == "program"
    assert hot.tick(program={}, state={}, health={})["wake"] is None


def test_failure_degrades_and_keeps_pending_heartbeats(cfg):
    clock = _Clock()
    r, hot = _hot(cfg, clock)
    hot.queue_player("p1", {})
    r.fail_next = 1
    out = hot.tick(program={}, state={}, health={})
    assert out == {"players": set(), "wake": None}
    assert hot.healthy is False and hot.failures == 1
    assert "injected failure" in (hot.last_error or "")
    assert hot.summary()["pending_players"] == 1          # kept for the next attempt
    # next tick recovers and flushes the kept heartbeat
    assert hot.tick(program={}, state={}, health={})["players"] == {"p1"}
    assert hot.healthy is True and hot.last_error is None


def test_cache_config_validation():
    assert Cache().alive_ttl_s >= 3 * Cache().publish_s
    with pytest.raises(ValidationError):
        Cache(publish_s=5, alive_ttl_s=10)
    with pytest.raises(ValidationError):
        Cache(timeout_s=0)


# ------------------------------------------------------------ Station wiring
def _station(tmp_env, monkeypatch):
    cfg, store, _ = tmp_env
    monkeypatch.setenv("REDIS_URL", "rediss://cache.test:6379")
    import pilgrim.server as srv
    fake = {}

    def _connect(url, c):
        fake["r"] = FakeRedis(lambda: station.clock.wall())
        return fake["r"]
    monkeypatch.setattr(srv, "cache_connect", _connect)
    station = build_station(cfg)
    station.llm = FakeLLM()  # type: ignore[assignment]
    station.kokoro = FakeKokoro()  # type: ignore[assignment]
    seed_pool(station.db, cfg)
    station.scheduler.commit_lookahead()
    return cfg, station, fake["r"]


def test_station_without_redis_url_has_no_publisher(tmp_env, monkeypatch):
    cfg, _, _ = tmp_env
    monkeypatch.delenv("REDIS_URL", raising=False)
    station = build_station(cfg)
    assert station.hot is None and station.hot_tick() is None


def test_station_tick_publishes_program_and_counts_remote_listeners(tmp_env, monkeypatch):
    cfg, station, r = _station(tmp_env, monkeypatch)
    out = station.hot_tick()
    assert out["players"] == set()
    prog = station.hot.read("program")
    assert prog["epoch"] == station.epoch and prog["items"]
    assert all(it["url"] for it in prog["items"])
    assert station.hot.read("state")["epoch"] == station.epoch
    assert station.hot.read("health")["on_air"] is True
    # a heartbeat key written by the web tier counts as a listener here
    r.set("pilgrim:player:web-1", json.dumps({"at": 1}), ex=15)
    station.hot_tick()
    assert station.active_listeners() == 1
    assert station._idle_check() is None and not station.scheduler.frozen


def test_station_heartbeat_reaches_the_cache_and_wake_flag_thaws(tmp_env, monkeypatch):
    cfg, station, r = _station(tmp_env, monkeypatch)
    from pilgrim import dj_contracts as dj
    station.touch_listener("local-1")
    station.record_player_telemetry("local-1", dj.HeartbeatIn(player_id="local-1", platform="ios"))
    station.hot_tick()
    tel = json.loads(r.get("pilgrim:player:local-1"))
    assert tel["platform"] == "ios" and "at" in tel
    # freeze, then a web-tier wake flag thaws on the next tick
    station.scheduler.freeze()
    station._idle_since = station.clock.now()
    assert station.scheduler.frozen
    station.hot.set_wake("program")
    station.hot_tick()
    assert not station.scheduler.frozen
    assert station.idle_wakes == 1


def test_admin_health_reports_the_publisher(tmp_env, monkeypatch):
    import asyncio
    cfg, station, r = _station(tmp_env, monkeypatch)
    station.hot_tick()

    async def _no_probe():   # health() probes backends; tests never do (AGENTS §1.2)
        return {}
    monkeypatch.setattr(station, "backend_status", _no_probe)
    summary = asyncio.run(station.health())["cache"]
    assert summary["enabled"] is True and summary["healthy"] is True and summary["ticks"] == 1
    assert "last_error" in summary and "url" not in json.dumps(summary)
