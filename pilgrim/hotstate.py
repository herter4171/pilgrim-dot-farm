"""Hot state in Valkey (SCALABILITY.md Step 3; RADIO §3, §14 "Hot state").

The station publishes what the browser polls every 1–5 s so the web tier
never needs the origin or Postgres for them: the committed programme (with
URLs), the public station state, the public health body and a liveness key.
Heartbeats flow the other way: whoever receives one (the station today, the
Fargate web tier after Step 5) writes a `player:<id>` key with the listener
timeout as TTL, and the station counts those keys for the live listener count
and the idle policy. A programme fetch from a frozen station sets `wake`; the
station takes it on its next tick and thaws (RADIO §3: wake becomes "within
`cache.publish_s`" instead of "in the same response").

Design rules:
- Hard rule 1: nothing here is on the playout path. `Station.hot_loop()` is
  its own task; every call has a short socket timeout; a failing cache is
  logged, marks `healthy` false and degrades to today's in-memory behaviour.
  The web tier reads the missing `alive` key as off-air.
- The client is injected (tests use `pilgrim/tests/fakes/fake_redis.py`;
  AGENTS §1.2). Only this module touches the client API.
- Single-key commands only (SET/GET/GETDEL/SCAN and a non-transactional
  pipeline of SETs). Valkey Serverless is cluster-mode behind one endpoint:
  a multi-key command whose keys hash to different slots (`DEL a b`, MGET,
  MULTI) is refused with CROSSSLOT (seen 2026-11-05). The fake enforces it.
- Values are JSON text; keys carry `cache.key_prefix`. Hard rule 7: nothing
  secret is ever written here (no tokens, no IPs beyond what RADIO §14 already
  stores; player keys hold playback telemetry only).

Key contract (web tier reads; RADIO §14):

| key                | value (JSON)                                   | TTL                      |
|--------------------|------------------------------------------------|--------------------------|
| `<p>alive`         | {epoch, pid, host, at}                         | cache.alive_ttl_s        |
| `<p>program`       | GET /api/station/program body (no cursor)      | cache.alive_ttl_s        |
| `<p>state`         | GET /api/station/state body                    | cache.alive_ttl_s        |
| `<p>health`        | public GET /api/health body                    | cache.alive_ttl_s        |
| `<p>player:<id>`   | {at, platform, ctx_state, hidden, sink, resumed} | listener_timeout_s |
| `<p>wake`          | reason string                                  | cache.wake_ttl_s         |
"""
from __future__ import annotations

import json
import logging
import os
import socket
import time
from collections.abc import Callable
from typing import Any, Protocol

from pilgrim.config import Config

log = logging.getLogger("radio.hotstate")

REDIS_URL_ENV = "REDIS_URL"


class CacheClient(Protocol):
    """The slice of redis-py `Redis` this module uses (and the fake implements)."""

    def ping(self) -> Any: ...
    def get(self, name: str) -> Any: ...
    def getdel(self, name: str) -> Any: ...
    def delete(self, *names: str) -> Any: ...
    def scan_iter(self, match: str | None = None, count: int | None = None) -> Any: ...
    def pipeline(self, transaction: bool = True) -> Any: ...
    def set(self, name: str, value: Any, ex: int | None = None) -> Any: ...


def connect(url: str, cfg: Config) -> CacheClient:
    """redis-py client for `url` (`rediss://` = TLS; Valkey Serverless is
    TLS-only). Connects lazily, so constructing never raises."""
    import redis

    return redis.Redis.from_url(
        url, socket_timeout=cfg.cache.timeout_s,
        socket_connect_timeout=cfg.cache.timeout_s,
        health_check_interval=30, decode_responses=False)


class HotState:
    """Publisher + reader for the station process. Sync methods; the station
    runs them in a worker thread from `hot_loop()` so the event loop never
    waits on the network."""

    def __init__(self, cfg: Config, client: CacheClient, *, epoch: str,
                 now: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.client = client
        self.epoch = epoch
        self._now = now
        self.prefix = cfg.cache.key_prefix
        self.healthy: bool | None = None       # None until the first tick
        self.last_error: str | None = None
        self.last_publish_at: float | None = None
        self.ticks = 0
        self.failures = 0
        self._pending_players: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ helpers
    def key(self, name: str) -> str:
        return f"{self.prefix}{name}"

    @staticmethod
    def _loads(raw: Any) -> Any:
        if raw is None:
            return None
        if isinstance(raw, bytes | bytearray):
            raw = raw.decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return raw

    # ------------------------------------------------------------ writers
    def queue_player(self, player_id: str, telemetry: dict[str, Any]) -> None:
        """Remember a heartbeat for the next tick (one pipeline per tick, no
        per-heartbeat round trip). Bounded by `visitors.max_listeners`."""
        if not player_id or len(player_id) > 64:
            return
        if (player_id in self._pending_players
                or len(self._pending_players) < self.cfg.visitors.max_listeners):
            self._pending_players[player_id] = {"at": self._now(), **telemetry}

    def set_wake(self, reason: str) -> None:
        """The web tier's side of a wake (a programme fetch or heartbeat
        reaching a frozen station). Kept here so both tiers share one key."""
        self.client.set(self.key("wake"), reason, ex=int(self.cfg.cache.wake_ttl_s))

    def tick(self, *, program: dict[str, Any], state: dict[str, Any],
             health: dict[str, Any]) -> dict[str, Any]:
        """One publish round trip: alive/program/state/health + the queued
        player keys in one pipeline, then read the live players and take the
        wake flag. Returns {"players": set[str], "wake": str | None}. Never
        raises: a failure marks the publisher unhealthy and returns empties, so
        the station falls back to its in-memory listener map."""
        pending, self._pending_players = self._pending_players, {}
        ttl = int(self.cfg.cache.alive_ttl_s)
        ptl = int(self.cfg.visitors.listener_timeout_s)
        try:
            pipe = self.client.pipeline(transaction=False)
            pipe.set(self.key("alive"), json.dumps({
                "epoch": self.epoch, "pid": os.getpid(),
                "host": socket.gethostname(), "at": self._now()}), ex=ttl)
            pipe.set(self.key("program"), json.dumps(program), ex=ttl)
            pipe.set(self.key("state"), json.dumps(state), ex=ttl)
            pipe.set(self.key("health"), json.dumps(health), ex=ttl)
            for pid, tel in pending.items():
                pipe.set(self.key(f"player:{pid}"), json.dumps(tel), ex=ptl)
            pipe.execute()
            players = self.live_players()
            wake = self._loads(self.client.getdel(self.key("wake")))
        except Exception as e:  # noqa: BLE001 - the cache must never break radio
            self.failures += 1
            self.last_error = f"{type(e).__name__}: {str(e)[:200]}"
            if self.healthy is not False:
                log.warning("hotstate.down", extra={"error": self.last_error})
            self.healthy = False
            # keep the heartbeats for the next attempt so a blip loses nothing
            self._pending_players = {**pending, **self._pending_players}
            return {"players": set(), "wake": None}
        if self.healthy is not True:
            log.info("hotstate.up", extra={"players": len(players)})
        self.healthy = True
        self.last_error = None
        self.last_publish_at = self._now()
        self.ticks += 1
        return {"players": players, "wake": wake if isinstance(wake, str) else None}

    # ------------------------------------------------------------ readers
    def live_players(self) -> set[str]:
        """Player ids with an unexpired heartbeat key (any tier wrote them)."""
        prefix = self.key("player:")
        out: set[str] = set()
        for k in self.client.scan_iter(match=prefix + "*", count=500):
            name = k.decode() if isinstance(k, bytes | bytearray) else str(k)
            out.add(name[len(prefix):])
            if len(out) >= self.cfg.visitors.max_listeners:
                break
        return out

    def read(self, name: str) -> Any:
        """One published document (`program`, `state`, `health`, `alive`), or
        None when missing/expired. The web tier's read path."""
        return self._loads(self.client.get(self.key(name)))

    def summary(self) -> dict[str, Any]:
        """Admin health: never a URL or credential."""
        return {"enabled": True, "healthy": self.healthy, "ticks": self.ticks,
                "failures": self.failures, "last_error": self.last_error,
                "last_publish_at": self.last_publish_at,
                "pending_players": len(self._pending_players)}
