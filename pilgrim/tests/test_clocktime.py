"""OVERHAUL 5.3 — real local time for the DJ (MCP time tool + zoneinfo fallback)."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from pilgrim.config import RNG, SimClock
from pilgrim.pipelines.clocktime import (
    _TOOL_CACHE,
    _resolve_time_tool,
    current_local_time,
    spoken_time,
)
from pilgrim.scheduler import Scheduler
from pilgrim.selector import RandomSelector


def test_spoken_time_table(cfg):
    assert spoken_time(datetime(2026, 1, 1, 22, 2)) == "just after ten at night"
    assert spoken_time(datetime(2026, 1, 1, 22, 14)) == "about quarter past ten at night"
    assert spoken_time(datetime(2026, 1, 1, 9, 58)) == "coming up on ten in the morning"
    assert "in the morning" in spoken_time(datetime(2026, 1, 1, 10, 0))
    assert "in the afternoon" in spoken_time(datetime(2026, 1, 1, 13, 30))
    assert "in the evening" in spoken_time(datetime(2026, 1, 1, 18, 45))


def test_resolve_time_tool_by_suffix():
    assert _resolve_time_tool(["foo-get_current_time", "x"]) == "foo-get_current_time"


class _FakeSession:
    def __init__(self):
        self.list_tools_called = False

    async def initialize(self):
        pass

    async def list_tools(self):
        self.list_tools_called = True
        return [{"name": "time-get_current_time"}]

    async def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": '{"datetime": "2026-09-29T19:00:00-04:00"}'}]}

    async def close(self):
        pass


def test_current_local_time_parses_mcp_shape(cfg):
    _TOOL_CACHE.clear()
    ses = _FakeSession()
    factory = lambda *a, **k: ses  # noqa: E731
    dt = asyncio.run(current_local_time(cfg, "secret", session_factory=factory))
    assert dt.hour == 19
    assert dt.utcoffset().total_seconds() == -4 * 3600  # -04:00 as recorded
    assert ses.list_tools_called is True
    assert _TOOL_CACHE.get(cfg.hosts.searxng) == "time-get_current_time"


class _BoomSession:
    async def initialize(self):
        raise RuntimeError("mcp down")

    async def list_tools(self):
        raise RuntimeError("mcp down")

    async def call_tool(self, *a, **k):
        raise RuntimeError("mcp down")

    async def close(self):
        pass


def test_current_local_time_falls_back_to_zoneinfo(cfg, caplog):
    _TOOL_CACHE.clear()
    factory = lambda *a, **k: _BoomSession()  # noqa: E731
    with caplog.at_level(logging.WARNING, logger="radio.time"):
        dt = asyncio.run(current_local_time(cfg, "secret", session_factory=factory))
    assert dt.tzinfo is not None
    assert any(r.getMessage() == "time.fallback" for r in caplog.records)


def test_dj_expiry_skips_stale_time_clips(cfg, tmp_env):
    from datetime import datetime

    from conftest import make_item

    # expired-only store -> no pick
    _, s1, _ = tmp_env
    wall = SimClock().wall()
    make_item(cfg, s1, "dj_talk", 15.0, fresh=True,
              expires_at=datetime.fromtimestamp(wall - 100, UTC).isoformat())
    clock = SimClock()
    sch1 = Scheduler(cfg, s1, RandomSelector(RNG(1), cfg), clock, RNG(1))
    assert sch1._pick_dj(sch1.build_state()) == []

    # unexpired-only store -> pick works
    _, s2, _ = tmp_env
    make_item(cfg, s2, "dj_talk", 15.0, fresh=True,
              expires_at=datetime.fromtimestamp(wall + 100_000, UTC).isoformat())
    clock2 = SimClock()
    sch2 = Scheduler(cfg, s2, RandomSelector(RNG(2), cfg), clock2, RNG(2))
    assert len(sch2._pick_dj(sch2.build_state())) == 1
