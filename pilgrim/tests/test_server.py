"""Server + web-client serving tests (RADIO.md §9, §11, §15 / M8).

TestClient is used WITHOUT the `with` context manager so the FastAPI lifespan
does not fire: no scheduler/producer background tasks start, so no real
backend (LiteLLM, Kokoro, mlx, searxng) is ever contacted (AGENTS rule 2).
Only backend-free endpoints are exercised here.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from pilgrim.server import create_app


def test_site_served(tmp_env):
    """RADIO.md §11 GET / serves the static UI; assets are reachable."""
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    assert "Pilgrim" in r.text
    assert c.get("/style.css").status_code == 200
    assert c.get("/player.js").status_code == 200


def test_program_endpoint_empty_inventory(tmp_env):
    """An empty temp library yields an empty program, not an error."""
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    c = TestClient(app)
    r = c.get("/api/station/program")
    assert r.status_code == 200
    assert r.json() == {"items": [], "start_offset_s": 0.0}


def test_media_unknown_item_404(tmp_env):
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    c = TestClient(app)
    assert c.get("/api/media/9999").status_code == 404
