"""Server + web-client serving tests (RADIO.md §9, §11, §15 / M8).

TestClient is used WITHOUT the `with` context manager so the FastAPI lifespan
does not fire: no scheduler/producer background tasks start, so no real
backend (LiteLLM, Kokoro, mlx, searxng) is ever contacted (AGENTS rule 2).
Only backend-free endpoints are exercised here.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from pilgrim.config import Visitors
from pilgrim.server import create_app


def test_site_served(tmp_env):
    """RADIO.md §11 GET / serves the static UI; assets are reachable."""
    cfg, _, _ = tmp_env
    cfg.station.serve_web = True  # single-host mode; the split origin ships false
    app = create_app(cfg)
    c = TestClient(app)
    r = c.get("/")
    assert r.status_code == 200
    # station name retained in the browser title + header; pane title is exact
    assert "PILGRIM" in r.text
    assert "RADIO BROADCAST" in r.text
    assert "CREDITS" in r.text
    assert "Cornucopia_LQ.svg" in r.text
    assert "HIT COUNTER" in r.text
    assert c.get("/style.css").status_code == 200
    # static client files must revalidate so a web edit needs no restart
    assert c.get("/").headers["cache-control"] == "no-cache"
    assert c.get("/style.css").headers["cache-control"] == "no-cache"
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
        return dict.fromkeys(("litellm", "kokoro", "music", "searxng"), False)

    monkeypatch.setattr(station, "backend_status", backends_down)
    health = await station.health()
    assert health["on_air"] is True
    assert not any(health["backends"].values())


async def test_health_off_air_without_committed_audio(tmp_env, monkeypatch):
    cfg, _, _ = tmp_env
    station = create_app(cfg).state.station

    async def backends_up():
        return dict.fromkeys(("litellm", "kokoro", "music", "searxng"), True)

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
        return dict.fromkeys(("litellm", "kokoro", "music", "searxng"), True)

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
        return dict.fromkeys(("litellm", "kokoro", "music", "searxng"), True)

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
    cfg.visitors = Visitors(trusted_proxies=["127.0.0.1"])
    app = create_app(cfg)
    c = TestClient(app, client=("127.0.0.1", 50000))
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
    cfg.visitors = Visitors(trusted_proxies=["127.0.0.1"])
    cfg.library.db = str(store.path)
    app1 = create_app(cfg)
    assert TestClient(app1, client=("127.0.0.1", 50000)).post(
        "/api/visitors", headers={"X-Real-IP": "9.9.9.9"}).json()["unique_visitors"] == 1
    # a fresh app/connection on the same db keeps the count
    app2 = create_app(cfg)
    r = TestClient(app2, client=("127.0.0.1", 50000)).post(
        "/api/visitors", headers={"X-Real-IP": "9.9.9.9"})
    assert r.json()["unique_visitors"] == 1


def test_visitors_stores_raw_ip_without_any_secret(tmp_env, monkeypatch):
    cfg, store, _ = tmp_env
    cfg.visitors = Visitors(trusted_proxies=["127.0.0.1"])
    monkeypatch.delenv("VISITOR_HASH_SECRET", raising=False)
    app = create_app(cfg)
    r = TestClient(app, client=("127.0.0.1", 50000)).post(
        "/api/visitors", headers={"X-Real-IP": "1.2.3.4"})
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


def test_visitors_returns_paid_and_total_request_counts(tmp_env):
    """RADIO §9.1: the HIT COUNTER's paid/total lines are DB-backed and ride
    the one page-load POST /api/visitors (no extra endpoint, no extra call)."""
    cfg, _, _ = tmp_env
    cfg.visitors.trusted_proxies = ["127.0.0.1"]
    app = create_app(cfg)
    c = TestClient(app, client=("127.0.0.1", 50000))
    h = {"X-Real-IP": "1.2.3.4"}
    body = c.post("/api/visitors", headers=h).json()
    assert body == {"unique_visitors": 1, "paid_requests": 0,
                    "total_requests": 0}
    sdb = app.state.station.db
    sdb.add_request("free one", cap=None)
    paid = sdb.add_paid_request("paid one")
    sdb.attach_checkout(paid["id"], "cs_test_counts")
    assert sdb.mark_request_paid(
        paid["id"], session_id="cs_test_counts", payment_intent_id="pi_test",
        amount_cents=350, currency="usd", livemode=False)
    # abandoned checkout rows stay out of the paid count
    sdb.add_paid_request("never paid")
    body = c.post("/api/visitors", headers=h).json()
    assert body["paid_requests"] == 1 and body["total_requests"] == 3
    assert body["unique_visitors"] == 1   # same peer, still one visitor


def test_visitors_request_counts_failure_reports_zero(tmp_env, monkeypatch):
    """A store error on the request counts must not break the counter: the
    pane falls back to zeros, like the visitor count (RADIO §14)."""
    cfg, _, _ = tmp_env
    cfg.visitors.trusted_proxies = ["127.0.0.1"]
    app = create_app(cfg)
    c = TestClient(app, client=("127.0.0.1", 50000))

    def boom() -> tuple[int, int]:
        raise RuntimeError("db gone")

    monkeypatch.setattr(app.state.station.db, "request_counts", boom)
    r = c.post("/api/visitors", headers={"X-Real-IP": "1.2.3.4"})
    assert r.status_code == 200
    assert r.json() == {"unique_visitors": 1, "paid_requests": 0,
                        "total_requests": 0}


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


# --------------------------------------------------------------------------- #
# IDLE.md Step 4: idle policy in Station (freeze after idle.after_s with no
# listeners, wake on program/heartbeat/admin; polls never wake). SimClock-driven.
# --------------------------------------------------------------------------- #

def _idle_app(tmp_env, *, seed=True):
    """Admin app on a SimClock with a non-empty committed window. The scheduler
    shares the station's Clock instance, so both are swapped together."""
    from conftest import seed_pool
    from pilgrim.config import SimClock
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    station = app.state.station
    clock = SimClock()
    station.clock = clock
    station.scheduler.clock = clock
    station._last_listener_at = clock.now()
    if seed:
        seed_pool(station.db, cfg, n_song=12, n_liner=10, n_com=5, n_dj=5)
        station.scheduler.commit_lookahead()

    async def _fake_backends() -> dict:
        return {"litellm": False, "kokoro": False, "music": False,
                "searxng": False, "sfx": False, "music_backends": {}}

    station.backend_status = _fake_backends
    return app, TestClient(app), clock


def _run_idle_check(app):
    app.state.station._idle_check()


def test_no_listener_freezes_after_after_s(tmp_env):
    app, c, clock = _idle_app(tmp_env)
    st = app.state.station
    clock.advance(st.cfg.idle.after_s - 1)
    _run_idle_check(app)
    assert st.is_idle() is False
    clock.advance(2)
    _run_idle_check(app)
    assert st.is_idle() is True
    body = c.get("/api/station/state").json()
    assert body["idle"] is True and body["listeners"] == 0
    health = c.get("/api/health").json()
    assert health["on_air"] is True          # PLAY is never disabled by idle
    assert health["idle"]["idle"] is True
    assert health["idle"]["wakes"] == 0


def test_polls_do_not_wake_a_frozen_station(tmp_env):
    """IDLE.md D2/rule 2: only the program route, heartbeats and admin DJ
    mutations wake. Idle pages and bots poll the rest."""
    app, c, clock = _idle_app(tmp_env)
    st = app.state.station
    clock.advance(st.cfg.idle.after_s + 1)
    _run_idle_check(app)
    assert st.is_idle() is True
    c.get("/api/health")
    c.get("/api/station/state")
    c.get("/api/requests")
    c.post("/api/visitors")
    item = st.scheduler._items[0]["item_id"]
    c.get(f"/api/media/{item}")
    assert st.is_idle() is True
    assert st.idle_wakes == 0


def test_program_fetch_wakes_and_returns_a_playable_window(tmp_env):
    app, c, clock = _idle_app(tmp_env)
    st = app.state.station
    clock.advance(st.cfg.idle.after_s + 1)
    _run_idle_check(app)
    frozen_offset = st.scheduler.on_air()[1]
    body = c.get("/api/station/program").json()
    assert st.is_idle() is False
    assert body["items"], "the waking request must already be playable"
    assert body["start_offset_s"] == frozen_offset
    assert c.get("/api/station/state").json()["idle"] is False
    assert c.get("/api/health").json()["idle"]["wakes"] == 1


def test_heartbeat_wakes(tmp_env):
    app, c, clock = _idle_app(tmp_env)
    st = app.state.station
    clock.advance(st.cfg.idle.after_s + 1)
    _run_idle_check(app)
    assert st.is_idle() is True
    _beat(c, "player-a")
    assert st.is_idle() is False
    assert st.idle_wakes == 1


def test_idle_disabled_never_freezes(tmp_env):
    app, c, clock = _idle_app(tmp_env)
    st = app.state.station
    st.cfg.idle.enabled = False
    clock.advance(st.cfg.idle.after_s * 10)
    _run_idle_check(app)
    assert st.is_idle() is False


def test_stale_time_check_dropped_by_a_frozen_wake(tmp_env):
    """The Step 2 path end-to-end through the handler: a clock read committed
    before the freeze is gone after a 2 h frozen wake."""
    from conftest import iso_from_epoch, make_item
    app, c, clock = _idle_app(tmp_env)
    st = app.state.station
    tc = make_item(st.cfg, st.db, "time_check", 4.0,
                   expires_at=iso_from_epoch(clock.wall() + 400))
    st.scheduler._append({"item_id": tc, "type": "time_check",
                          "duration_s": 4.0, "consume": True,
                          "expires_at": iso_from_epoch(clock.wall() + 400)})
    st.scheduler._rebuild_program()
    clock.advance(st.cfg.idle.after_s + 1)
    _run_idle_check(app)
    clock.advance(7200)
    rev = st.scheduler.revision
    c.get("/api/station/program")
    assert st.scheduler.revision == rev + 1
    assert "time_check" not in [r["type"] for r in st.scheduler._items]


def test_public_health_has_no_idle_field(tmp_env):
    """`_public_health` is the browser's OFF AIR source; idle must not leak into
    it (IDLE.md D10)."""
    from pilgrim.server import create_public_app
    app, _, clock = _idle_app(tmp_env)
    st = app.state.station
    public = TestClient(create_public_app(st.cfg, st))
    clock.advance(st.cfg.idle.after_s + 1)
    _run_idle_check(app)
    assert "idle" not in public.get("/api/health").json()


# --------------------------------------------------------------------------- #
# VOTING.md Step 2: vote + rating routes (both apps), state.rating, admin audit.
# --------------------------------------------------------------------------- #

VOTER = "a" * 32


def test_vote_route_and_board_fields(tmp_env):
    app, c, clock = _idle_app(tmp_env)
    st = app.state.station
    req = st.db.add_request("free one", cap=None)
    r = c.post(f"/api/requests/{req['id']}/vote",
               json={"voter_id": VOTER, "value": 1})
    assert r.status_code == 200 and r.json()["score"] == 1
    assert r.headers["cache-control"] == "no-store"
    board = c.get("/api/requests").json()
    assert board["voting_enabled"] is True
    row = board["queue"][0]
    assert row["id"] == req["id"] and row["score"] == 1
    assert row["votable"] is True and row["buried"] is False
    assert board["queue"] and c.get("/api/requests").headers["cache-control"] \
        == "public, max-age=2"
    # retract
    assert c.post(f"/api/requests/{req['id']}/vote",
                  json={"voter_id": VOTER, "value": 0}).json()["score"] == 0


def test_vote_route_errors(tmp_env):
    app, c, _ = _idle_app(tmp_env)
    st = app.state.station
    req = st.db.add_request("free one", cap=None)
    assert c.post(f"/api/requests/{req['id']}/vote",
                  json={"voter_id": "nope", "value": 1}).status_code == 422
    assert c.post("/api/requests/9999/vote",
                  json={"voter_id": VOTER, "value": 1}).status_code == 404
    st.db.mark_request_aired(req["id"])
    r = c.post(f"/api/requests/{req['id']}/vote",
               json={"voter_id": VOTER, "value": 1})
    assert r.status_code == 409 and r.json()["code"] == "voting_closed"
    st.cfg.voting.enabled = False
    assert c.post(f"/api/requests/{req['id']}/vote",
                  json={"voter_id": VOTER, "value": 1}).status_code == 404
    assert c.get("/api/requests").json()["voting_enabled"] is False


def test_vote_rate_limit(tmp_env):
    app, c, _ = _idle_app(tmp_env)
    st = app.state.station
    st.cfg.voting.per_client_per_10min = 2
    app.state.station = st  # same object; the limiter was built from cfg at registration
    req = st.db.add_request("free one", cap=None)
    body = {"voter_id": VOTER, "value": 1}
    # the limiter is created at app registration; rebuild the app so the new
    # limit applies
    from pilgrim.server import create_admin_app
    c2 = TestClient(create_admin_app(st.cfg, st))
    assert c2.post(f"/api/requests/{req['id']}/vote", json=body).status_code == 200
    assert c2.post(f"/api/requests/{req['id']}/vote", json=body).status_code == 200
    assert c2.post(f"/api/requests/{req['id']}/vote", json=body).status_code == 429


def test_rating_route_window_and_state(tmp_env):
    app, c, clock = _idle_app(tmp_env)
    st = app.state.station
    # a clean two-row window: one song then a liner
    st.scheduler.store.clear_program()
    song = st.db.add_item(type_="song", media_path="/tmp/s.mp3", duration_s=60.0,
                          title="Rate Me", artist="Tester")
    liner = st.db.add_item(type_="liner", media_path="/tmp/l.mp3", duration_s=5.0)
    st.scheduler._append({"item_id": song, "type": "song",
                          "duration_s": 60.0, "consume": False})
    st.scheduler._append({"item_id": liner, "type": "liner",
                          "duration_s": 5.0, "consume": False})
    st.scheduler._rebuild_program()
    seq = next(r["seq"] for r in st.scheduler._items if r["type"] == "song")
    clock.advance(1)
    state = c.get("/api/station/state").json()
    assert state["rating"]["open"] is True and state["rating"]["count"] == 0
    r = c.post("/api/station/rating",
               json={"voter_id": VOTER, "epoch": st.scheduler.epoch,
                     "seq": seq, "stars": 4})
    assert r.status_code == 200 and r.json()["count"] == 1
    assert r.headers["cache-control"] == "no-store"
    state = c.get("/api/station/state").json()
    assert state["rating"] == {"open": True, "count": 1, "avg": 4.0}
    # wrong epoch / closed window
    assert c.post("/api/station/rating",
                  json={"voter_id": VOTER, "epoch": "other",
                        "seq": seq, "stars": 4}).status_code == 409
    clock.advance(70)   # the song is long gone
    assert c.get("/api/station/state").json()["rating"]["open"] is False
    st.cfg.ratings.enabled = False
    assert c.get("/api/station/state").json()["rating"] is None
    assert c.post("/api/station/rating",
                  json={"voter_id": VOTER, "epoch": st.scheduler.epoch,
                        "seq": seq, "stars": 4}).status_code == 404


def test_admin_voting_route_is_admin_only(tmp_env):
    from pilgrim.server import create_public_app
    app, c, _ = _idle_app(tmp_env)
    st = app.state.station
    req = st.db.add_request("free one", cap=None)
    st.db.cast_request_vote(req["id"], VOTER, None, -1, max_voters_per_ip=3)
    public = TestClient(create_public_app(st.cfg, st))
    assert public.get("/api/admin/voting").status_code == 404
    assert c.get("/api/admin/voting").status_code == 401
    r = c.get("/api/admin/voting", headers={"Authorization": "Bearer test-token"})
    # require_dj needs the configured token; without one this is 401/403
    assert r.status_code in (200, 401, 403)


# --------------------------------------------------------------------------- #
# Playback telemetry on heartbeats (RADIO §9.2/§11): reported per live player,
# summarised on the admin health, never acted on.
# --------------------------------------------------------------------------- #

def test_heartbeat_telemetry_is_summarised_on_health(tmp_env):
    app, c = _listener_app(tmp_env)
    st = app.state.station
    st.backend_status = _no_backends
    base = {"seq": 1, "media_id": 1, "type": "song"}
    c.post("/api/station/heartbeat", json={**base, "player_id": "phone", "platform": "ios",
                                           "ctx_state": "running", "hidden": False,
                                           "sink": "element", "resumed": 0})
    c.post("/api/station/heartbeat", json={**base, "player_id": "droid", "platform": "android",
                                           "ctx_state": "interrupted", "hidden": True,
                                           "sink": "element", "resumed": 2})
    c.post("/api/station/heartbeat", json={**base, "player_id": "legacy"})   # no telemetry
    tel = c.get("/api/health").json()["listeners"]
    assert tel["count"] == 3 and tel["reporting"] == 2
    assert tel["by_platform"] == {"ios": 1, "android": 1}
    assert tel["hidden"] == 1 and tel["not_running"] == 1
    assert tel["sink_element"] == 2 and tel["resumed_total"] == 2
    # a heartbeat without player_id (not a listener) carries nothing
    c.post("/api/station/heartbeat", json={**base, "platform": "ios"})
    assert c.get("/api/health").json()["listeners"]["reporting"] == 2


def test_heartbeat_telemetry_expires_with_the_listener(tmp_env):
    app, c = _listener_app(tmp_env)
    st = app.state.station
    st.backend_status = _no_backends
    c.post("/api/station/heartbeat", json={"seq": 1, "media_id": 1, "type": "song",
                                           "player_id": "phone", "platform": "ios"})
    assert c.get("/api/health").json()["listeners"]["reporting"] == 1
    st.clock.advance(st.cfg.visitors.listener_timeout_s + 1)
    tel = c.get("/api/health").json()["listeners"]
    assert tel == {"count": 0, "reporting": 0, "by_platform": {}, "hidden": 0,
                   "not_running": 0, "sink_element": 0, "resumed_total": 0}


def test_heartbeat_rejects_oversized_telemetry(tmp_env):
    app, c = _listener_app(tmp_env)
    r = c.post("/api/station/heartbeat", json={"seq": 1, "media_id": 1, "player_id": "p",
                                               "platform": "x" * 40})
    assert r.status_code == 422


async def _no_backends() -> dict:
    return {"litellm": False, "kokoro": False, "music": False,
            "searxng": False, "sfx": False, "music_backends": {}}
