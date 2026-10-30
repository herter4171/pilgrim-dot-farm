"""Public/admin listener split (SPLIT_PLAN §1, RADIO.md §11, §12).

Fakes only. TestClient is used WITHOUT the `with` context manager so no
lifespan / scheduler / producer task starts and no real backend is ever
contacted (AGENTS §2).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pilgrim.config import Config
from pilgrim.server import build_station, create_admin_app, create_public_app
from pilgrim.tests.fakes import FakeKokoro, FakeLLM
from pydantic import ValidationError

ADMIN_ROUTES = [
    ("get", "/api/admin/dj/state"),
    ("get", "/api/admin/voices"),
    ("post", "/api/station/start"),
    ("post", "/api/station/stop"),
]


def _station(tmp_env, *, serve_web: bool | None = None):
    cfg, _, _ = tmp_env
    if serve_web is not None:
        cfg.station.serve_web = serve_web
    station = build_station(cfg)
    station.llm = FakeLLM()  # type: ignore[assignment]
    station.kokoro = FakeKokoro()  # type: ignore[assignment]
    return cfg, station


def test_public_app_hides_admin_routes(tmp_env):
    """SPLIT_PLAN §1.5: the public listener physically cannot serve control."""
    cfg, station = _station(tmp_env)
    c = TestClient(create_public_app(cfg, station))
    for method, path in ADMIN_ROUTES:
        r = getattr(c, method)(path)
        assert r.status_code == 404, (method, path, r.status_code)


def test_admin_app_serves_admin_routes(tmp_env):
    """The admin app is a superset: TUI + existing tests keep working."""
    cfg, station = _station(tmp_env)
    c = TestClient(create_admin_app(cfg, station))
    for method, path in ADMIN_ROUTES:
        r = getattr(c, method)(path)
        assert r.status_code != 404, (method, path, r.status_code)


def test_public_health_is_minimal_and_probe_free(tmp_env):
    """SPLIT_PLAN §1.3: exactly {on_air, time}, no backend probes."""
    cfg, station = _station(tmp_env)

    async def _boom(*args, **kwargs):
        raise AssertionError("public /api/health probed a backend")

    station.backend_status = _boom
    station.inventory_levels = _boom
    c = TestClient(create_public_app(cfg, station))
    body = c.get("/api/health").json()
    assert set(body) == {"on_air", "time"}
    assert body["on_air"] == station.on_air
    assert station.kokoro.calls == []
    assert station.llm.calls == 0


def test_admin_port_must_differ_from_public(tmp_env):
    """SPLIT_PLAN §1.1: rejected at config load with a clear error."""
    cfg, _, _ = tmp_env
    raw = cfg.model_dump()
    raw["station"]["admin_port"] = raw["station"]["port"]
    with pytest.raises(ValidationError, match="admin_port"):
        Config(**raw)


def test_public_web_mount_follows_serve_web(tmp_env):
    """SPLIT_PLAN §1.5: edges serve the site, so the M5 can turn it off."""
    cfg, station = _station(tmp_env, serve_web=False)
    c = TestClient(create_public_app(cfg, station))
    assert c.get("/").status_code == 404

    cfg.station.serve_web = True
    c = TestClient(create_public_app(cfg, station))
    r = c.get("/")
    assert r.status_code == 200
    assert "PILGRIM" in r.text
