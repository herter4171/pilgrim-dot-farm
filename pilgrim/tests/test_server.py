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
    # station name retained in the browser title + header; ON AIR title is exact
    assert "PILGRIM" in r.text
    assert "ON AIR" in r.text
    assert "MODEL CREDITS" in r.text
    assert "HIT COUNTER" in r.text
    assert c.get("/style.css").status_code == 200
    assert c.get("/player.js").status_code == 200
    assert c.get("/visitors.js").status_code == 200


def test_program_endpoint_empty_inventory(tmp_env):
    """An empty temp library yields an empty program, not an error, with the
    DJ identity fields (RADIO §11 extension: epoch/revision/reset/server_time)."""
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    c = TestClient(app)
    body = c.get("/api/station/program").json()
    assert body["items"] == [] and body["start_offset_s"] == 0.0
    assert body["reset"] is False
    assert body["revision"] == 1
    assert isinstance(body["epoch"], str) and len(body["epoch"]) > 0
    assert isinstance(body["server_time"], (int, float))


def test_program_endpoint_emits_title_artist(tmp_env):
    """COSMETIC_PATCHING §3: program items carry title/artist from the inventory
    item; absent values normalize to empty strings. (Rows are appended to the
    app's store AFTER create_app — a fresh Scheduler clears stale program rows
    on init, OVERHAUL 2.4.) Both the incremental and the initial (on-air) paths
    join the same metadata."""
    cfg, store, _ = tmp_env
    app = create_app(cfg)
    sdb = app.state.station.db
    sid = sdb.add_item(type_="song", media_path="s.flac", duration_s=120,
                       title="Night Shift", artist="The Night Owls")
    lid = sdb.add_item(type_="liner", media_path="l.flac", duration_s=5)
    sid2 = sdb.add_item(type_="song", media_path="s2.flac", duration_s=120,
                        title="Asteroid", artist="Spring Rods")
    sdb.append_program(sid, "song", 120)
    sdb.append_program(lid, "liner", 5)
    sdb.append_program(sid2, "song", 120)
    c = TestClient(app)
    # incremental: all committed rows after seq 0
    items = c.get("/api/station/program?after_seq=0").json()["items"]
    assert items[0]["title"] == "Night Shift" and items[0]["artist"] == "The Night Owls"
    # liner has no title/artist -> normalized to empty, never None/undefined
    assert items[1]["title"] == "" and items[1]["artist"] == ""
    assert items[2]["title"] == "Asteroid" and items[2]["artist"] == "Spring Rods"
    # initial path (no on-air scheduler state -> the retained tail window)
    initial = c.get("/api/station/program").json()["items"]
    assert initial and initial[-1]["title"] == "Asteroid"
    assert initial[-1]["artist"] == "Spring Rods"
    # the fixture store sees the same committed rows (same db file)
    assert store.max_seq() == 3


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


async def test_health_rotation_counts_aired_songs(tmp_env, monkeypatch):
    """The listener status shows songs in rotation, not just unaired ones."""
    cfg, store, _ = tmp_env
    store.add_item(type_="song", media_path="a.flac", duration_s=120, fresh=False)
    store.add_item(type_="song", media_path="b.flac", duration_s=120, fresh=True)
    gone = store.add_item(type_="song", media_path="c.flac", duration_s=120, fresh=False)
    store.retire_item(gone, "test")
    station = create_app(cfg).state.station

    async def backends_up():
        return dict.fromkeys(("litellm", "kokoro", "mlx", "searxng"), True)

    monkeypatch.setattr(station, "backend_status", backends_up)
    h = await station.health()
    assert h["rotation"]["song"] == 2
    assert h["inventory"]["song"]["have"] == 1


# --------------------------------------------------------------------------- #
# COSMETIC_PATCHING §6 — persistent unique-visitor counter (RADIO §14).
# HTTP-level: trusted-proxy identity resolution, raw-IP dedupe, count from 0,
# unavailable identity, and spoof-ignoring for untrusted peers. Fakes-only.
# --------------------------------------------------------------------------- #

def test_canonical_ip_folds_mapped_v6_and_takes_first_xff():
    Station = __import__("pilgrim.server", fromlist=["Station"]).Station
    canon = Station._canonical_ip
    assert canon("1.2.3.4") == "1.2.3.4"
    assert canon("::ffff:1.2.3.4") == "1.2.3.4"   # IPv4-mapped IPv6 -> IPv4
    assert canon("2001:db8::1") == "2001:db8::1"
    assert canon("1.2.3.4, 5.6.7.8") == "1.2.3.4"  # leftmost of an XFF list
    assert canon("not-an-ip") is None
    assert canon("") is None
    assert canon(None) is None


def test_visitors_trusted_proxy_counts_unique(tmp_env, monkeypatch):
    cfg, store, _ = tmp_env
    cfg.visitors.trusted_proxies = ["testclient"]
    app = create_app(cfg)
    c = TestClient(app)
    def post(ip=None):
        h = {"X-Real-IP": ip} if ip else None
        return c.post("/api/visitors", headers=h)
    r = post("1.2.3.4")
    assert r.status_code == 200 and r.json()["unique_visitors"] == 1
    assert r.headers.get("cache-control") == "no-store"
    # repeat / concurrent-first-seen same IP counts once
    assert post("1.2.3.4").json()["unique_visitors"] == 1
    # a second distinct address increments
    assert post("5.6.7.8").json()["unique_visitors"] == 2
    # equivalent IPv6 spelling of the first dedupes
    assert post("::ffff:1.2.3.4").json()["unique_visitors"] == 2
    # no existing station tables were touched
    assert store.list_items() == []


def test_visitors_persist_across_restart(tmp_env, monkeypatch):
    cfg, store, _ = tmp_env
    cfg.visitors.trusted_proxies = ["testclient"]
    cfg.library.db = str(store.path)
    app1 = create_app(cfg)
    assert TestClient(app1).post(
        "/api/visitors", headers={"X-Real-IP": "9.9.9.9"}).json()["unique_visitors"] == 1
    # a fresh app/connection on the same db keeps the count
    app2 = create_app(cfg)
    r = TestClient(app2).post("/api/visitors", headers={"X-Real-IP": "9.9.9.9"})
    assert r.json()["unique_visitors"] == 1


def test_visitors_stores_raw_ip_without_any_secret(tmp_env, monkeypatch):
    cfg, store, _ = tmp_env
    cfg.visitors.trusted_proxies = ["testclient"]
    monkeypatch.delenv("VISITOR_HASH_SECRET", raising=False)
    app = create_app(cfg)
    r = TestClient(app).post("/api/visitors", headers={"X-Real-IP": "1.2.3.4"})
    assert r.status_code == 200 and r.json()["unique_visitors"] == 1
    rows = store._conn.execute("SELECT ip FROM visitor_ips").fetchall()
    assert [row[0] for row in rows] == ["1.2.3.4"]


def test_visitors_unavailable_identity_is_503(tmp_env, monkeypatch):
    cfg, _, _ = tmp_env
    cfg.visitors.trusted_proxies = ["testclient"]
    app = create_app(cfg)
    # trusted peer but no usable client identity header -> current count (0)
    r = TestClient(app).post("/api/visitors")
    assert r.status_code == 200 and r.json()["unique_visitors"] == 0


def test_visitors_untrusted_proxy_ignores_spoofable_header(tmp_env, monkeypatch):
    cfg, _, _ = tmp_env
    cfg.visitors.trusted_proxies = ["10.0.0.1"]  # NOT the connecting peer
    app = create_app(cfg)
    # peer "testclient" is untrusted; the X-Real-IP header is discarded as
    # spoofable, and "testclient" is not a canonical IP -> nothing registered
    r = TestClient(app).post("/api/visitors", headers={"X-Real-IP": "1.2.3.4"})
    assert r.status_code == 200 and r.json()["unique_visitors"] == 0


def test_visitors_disabled_returns_count_without_registering(tmp_env, monkeypatch):
    cfg, _, _ = tmp_env
    cfg.visitors.enabled = False
    app = create_app(cfg)
    r = TestClient(app).post("/api/visitors", headers={"X-Real-IP": "1.2.3.4"})
    assert r.status_code == 200 and r.json()["unique_visitors"] == 0


# --------------------------------------------------------------------------- #
# Live listener count (RADIO §9.1, §11, §14): heartbeating player_ids within
# visitors.listener_timeout_s, exposed on /api/station/state. SimClock-driven.
# --------------------------------------------------------------------------- #

def _listener_app(tmp_env):
    from pilgrim.config import SimClock
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    app.state.station.clock = SimClock()
    return app, TestClient(app)


def _beat(c, pid=None):
    body = {"seq": 1, "media_id": 1, "type": "song"}
    if pid is not None:
        body["player_id"] = pid
    assert c.post("/api/station/heartbeat", json=body).status_code == 200


def test_listeners_count_distinct_players(tmp_env):
    app, c = _listener_app(tmp_env)
    assert c.get("/api/station/state").json()["listeners"] == 0
    _beat(c, "a")
    _beat(c, "a")
    _beat(c, "b")
    _beat(c)  # legacy heartbeat without player_id is not a listener
    assert c.get("/api/station/state").json()["listeners"] == 2


def test_listeners_expire_without_heartbeat(tmp_env):
    app, c = _listener_app(tmp_env)
    clock = app.state.station.clock
    timeout = app.state.station.cfg.visitors.listener_timeout_s
    _beat(c, "a")
    _beat(c, "b")
    clock.advance(timeout - 1)
    _beat(c, "a")  # a keeps listening, b went quiet
    clock.advance(2)
    assert c.get("/api/station/state").json()["listeners"] == 1
    clock.advance(timeout)
    assert c.get("/api/station/state").json()["listeners"] == 0


def test_listeners_capped_and_long_ids_ignored(tmp_env):
    app, c = _listener_app(tmp_env)
    app.state.station.cfg.visitors.max_listeners = 2
    for pid in ("a", "b", "c", "x" * 65):
        _beat(c, pid)
    assert c.get("/api/station/state").json()["listeners"] == 2
    _beat(c, "a")  # known IDs still refresh at the cap
    assert c.get("/api/station/state").json()["listeners"] == 2
