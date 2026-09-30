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


async def test_health_stays_on_air_with_rendered_audio_and_backends_down(tmp_env, monkeypatch):
    """Production outages must not tell listeners to stop buffered playback (§5.4)."""
    cfg, store, _ = tmp_env
    store.add_item(type_="liner", media_path="unused.flac", duration_s=10)
    app = create_app(cfg)
    station = app.state.station
    # OVERHAUL 2.4: a fresh Scheduler clears stale program rows, so build
    # coverage from rendered inventory instead of pre-seeding append_program.
    station.scheduler.commit_lookahead()
    assert station.on_air

    async def backends_down():
        return dict.fromkeys(("litellm", "kokoro", "mlx", "searxng"), False)

    monkeypatch.setattr(station, "backend_status", backends_down)
    health = await station.health()
    assert health["on_air"] is True
    assert not any(health["backends"].values())


async def test_health_off_air_without_committed_audio(tmp_env, monkeypatch):
    cfg, _, _ = tmp_env
    station = create_app(cfg).state.station

    async def backends_up():
        return dict.fromkeys(("litellm", "kokoro", "mlx", "searxng"), True)

    monkeypatch.setattr(station, "backend_status", backends_up)
    assert (await station.health())["on_air"] is False


def test_heartbeat_records_media_id_not_seq(tmp_env):
    """OVERHAUL 2.5: the airplay ledger item_id is the inventory media_id, not
    the program seq. TestClient WITHOUT `with` so no background tasks run."""
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    c = TestClient(app)
    r = c.post("/api/station/heartbeat",
               json={"seq": 5, "media_id": 42, "type": "song"})
    assert r.status_code == 200
    row = app.state.station.db.recent_airplay(1)[0]
    assert row["item_id"] == 42
    assert row["seq"] == 5


async def test_health_inventory_matches_producer_counts(tmp_env, monkeypatch):
    """Aired liners/commercials recycle, so the site must count them (the old
    fresh-only count showed 'liners 0' while 15 were on air)."""
    cfg, store, _ = tmp_env
    for _ in range(3):
        store.add_item(type_="liner", media_path="l.flac", duration_s=5, fresh=False)
    store.add_item(type_="commercial", media_path="c.flac", duration_s=25, fresh=False)
    store.add_item(type_="dj_talk", media_path="d.flac", duration_s=15, fresh=False)
    station = create_app(cfg).state.station

    async def backends_up():
        return dict.fromkeys(("litellm", "kokoro", "mlx", "searxng"), True)

    monkeypatch.setattr(station, "backend_status", backends_up)
    inv = (await station.health())["inventory"]
    assert inv["liner"]["have"] == 3
    assert inv["commercial"]["have"] == 1
    assert inv["dj_talk"]["have"] == 0  # aired DJ talk is spent
    assert {k: v["have"] for k, v in inv.items()} == station.producer.counts()
