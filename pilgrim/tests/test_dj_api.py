"""Read-only DJ console API (TUI.md §2, §4; RADIO §11). Milestone 2 backend.

STATUS: milestone 2 <read-only console backend — frontend shell next>.
Fakes/TestClient WITHOUT the `with` block, so no lifespan/tasks run and no real
backend is contacted (AGENTS rule 2). Control mutations are NOT exercised here
(they land with the scheduler command service).
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from pilgrim.server import create_app

TOKEN = "sekrit"


@pytest.fixture
def dj_env(tmp_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PILGRIM_DJ_TOKEN", TOKEN)
    return tmp_env


def _auth(token_value: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_value}"}


# --------------------------------------------------------------------------- #
# public revision-aware endpoints (§5.3, §11)
# --------------------------------------------------------------------------- #
def test_program_reset_on_epoch_mismatch(tmp_env):
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    sid = app.state.station.db.add_item(type_="song", media_path="s.flac",
                                        duration_s=120, title="Alpha", artist="One")
    app.state.station.db.append_program(sid, "song", 120)
    c = TestClient(app)
    body = c.get("/api/station/program?after_seq=0&epoch=WRONG").json()
    assert body["reset"] is True
    assert any(i["title"] == "Alpha" for i in body["items"])
    assert body["epoch"] == app.state.station.epoch


def test_program_reset_on_revision_mismatch(tmp_env):
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    sdb = app.state.station.db
    sid = sdb.add_item(type_="song", media_path="s.flac", duration_s=120)
    sdb.append_program(sid, "song", 120)
    c = TestClient(app)
    body = c.get("/api/station/program?after_seq=0&revision=9999").json()
    assert body["reset"] is True


def test_program_matching_identity_is_incremental(tmp_env):
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    sdb = app.state.station.db
    sid = sdb.add_item(type_="song", media_path="s.flac", duration_s=120,
                       title="Alpha", artist="One")
    sdb.append_program(sid, "song", 120)
    st = app.state.station
    c = TestClient(app)
    body = c.get(
        f"/api/station/program?after_seq=0&epoch={st.epoch}&revision={st.scheduler.revision}"
    ).json()
    assert body["reset"] is False
    assert body["revision"] == st.scheduler.revision


def test_station_state_empty(tmp_env):
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    body = TestClient(app).get("/api/station/state").json()
    assert body["epoch"] == app.state.station.epoch
    assert body["revision"] == 1
    assert body["current_seq"] is None
    assert body["prepared_cutover"] is None


def test_station_state_after_commit(tmp_env):
    cfg, store, _ = tmp_env
    store.add_item(type_="song", media_path="s.flac", duration_s=120,
                   title="Alpha", artist="One")
    store.add_item(type_="liner", media_path="l.flac", duration_s=5)
    store.add_item(type_="song", media_path="s2.flac", duration_s=120,
                   title="Beta", artist="Two")
    app = create_app(cfg)
    station = app.state.station
    station.scheduler.commit_lookahead()
    body = TestClient(app).get("/api/station/state").json()
    assert body["current_seq"] is not None
    assert body["current_type"] is not None
    assert body["revision"] == 1


# --------------------------------------------------------------------------- #
# auth
# --------------------------------------------------------------------------- #
def test_dj_state_requires_token(dj_env):
    cfg, _, _ = dj_env
    app = create_app(cfg)
    c = TestClient(app)
    assert c.get("/api/admin/dj/state").status_code == 401
    assert c.get("/api/admin/dj/state",
                 headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/admin/dj/state", headers=_auth()).status_code == 200


def test_dj_disabled_when_token_unconfigured(tmp_env, monkeypatch: pytest.MonkeyPatch):
    # isolate from any PILGRIM_DJ_TOKEN in the real repo .env (the token now
    # legitimately lives there); "no token configured" -> dj_disabled (403).
    from pilgrim import dj_security
    monkeypatch.setattr(dj_security, "load_dj_token", lambda env_file=None: "")
    cfg, _, _ = tmp_env
    app = create_app(cfg)
    r = TestClient(app).get("/api/admin/dj/state")
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "dj_disabled"


def test_dj_songs_requires_token(dj_env):
    cfg, _, _ = dj_env
    app = create_app(cfg)
    c = TestClient(app)
    assert c.get("/api/admin/dj/songs").status_code == 401
    assert c.get("/api/admin/dj/songs", headers=_auth()).status_code == 200


# --------------------------------------------------------------------------- #
# dj/state
# --------------------------------------------------------------------------- #
def test_dj_state_off_air_empty(dj_env):
    cfg, _, _ = dj_env
    app = create_app(cfg)
    body = TestClient(app).get("/api/admin/dj/state", headers=_auth()).json()
    assert body["on_air"] is False
    assert body["current"] is None
    assert body["upcoming"] == []
    assert body["pending_commands"] == []
    assert body["catalogue_revision"] == 0
    assert body["player_status"] == "unknown"


def test_dj_state_shows_current_and_upcoming(dj_env):
    cfg, store, _ = dj_env
    store.add_item(type_="song", media_path="s.flac", duration_s=120,
                   title="Alpha", artist="One")
    store.add_item(type_="liner", media_path="l.flac", duration_s=5)
    store.add_item(type_="song", media_path="s2.flac", duration_s=120,
                   title="Beta", artist="Two")
    app = create_app(cfg)
    station = app.state.station
    station.scheduler.commit_lookahead()
    body = TestClient(app).get("/api/admin/dj/state", headers=_auth()).json()
    assert body["on_air"] is True
    assert body["current"] is not None
    # the stream opens with a callout before the first song (words between
    # songs, §5.2), so the current item may be a liner/talk/intro or a song
    assert body["current"]["type"] in {"song", "liner", "station_id",
                                        "dj_talk", "intro", "commercial"}
    assert body["current"]["position_s"] >= 0
    assert body["current"]["remaining_s"] > 0
    assert body["upcoming"], "committed lookahead should show upcoming rows"
    assert body["revision"] == station.scheduler.revision


# --------------------------------------------------------------------------- #
# dj/songs — catalogue, sort, search, cursor pagination
# --------------------------------------------------------------------------- #
def _seed_songs(store, titles_artists):
    ids = []
    for title, artist in titles_artists:
        ids.append(store.add_item(type_="song", media_path=f"{title}.flac",
                                  duration_s=120.0, title=title, artist=artist))
    return ids


def test_dj_songs_empty(dj_env):
    cfg, _, _ = dj_env
    app = create_app(cfg)
    body = TestClient(app).get("/api/admin/dj/songs", headers=_auth()).json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["catalogue_revision"] == 0


def test_dj_songs_sorted_title_case_insensitive(dj_env):
    cfg, store, _ = dj_env
    _seed_songs(store, [("cherry", "X"), ("Banana", "Y"), ("apple", "Z")])
    app = create_app(cfg)
    body = TestClient(app).get("/api/admin/dj/songs", headers=_auth()).json()
    titles = [i["title"] for i in body["items"]]
    assert titles == ["apple", "Banana", "cherry"]  # case-insensitive

    body = TestClient(app).get(
        "/api/admin/dj/songs?direction=desc", headers=_auth()).json()
    assert [i["title"] for i in body["items"]] == ["cherry", "Banana", "apple"]


def test_dj_songs_sort_by_artist(dj_env):
    cfg, store, _ = dj_env
    _seed_songs(store, [("a", "Zebra"), ("b", "Alpha"), ("c", "Mango")])
    app = create_app(cfg)
    body = TestClient(app).get(
        "/api/admin/dj/songs?sort=artist", headers=_auth()).json()
    assert [i["artist"] for i in body["items"]] == ["Alpha", "Mango", "Zebra"]


def test_dj_songs_search(dj_env):
    cfg, store, _ = dj_env
    _seed_songs(store, [("Cornfield Polka", "Barn"), ("Night Tractor", "Furrows"),
                        ("Cowboy Corndog", "Field")])
    app = create_app(cfg)
    body = TestClient(app).get(
        "/api/admin/dj/songs?q=corn", headers=_auth()).json()
    assert {i["title"] for i in body["items"]} == {"Cornfield Polka", "Cowboy Corndog"}
    # artist-match search
    body = TestClient(app).get(
        "/api/admin/dj/songs?q=rods&sort=artist", headers=_auth()).json()
    assert body["items"] == []


def test_dj_songs_cursor_pagination_no_duplicates(dj_env):
    cfg, store, _ = dj_env
    _seed_songs(store, [(f"Song {i:03d}", f"Artist {i % 7}") for i in range(60)])
    app = create_app(cfg)
    c = TestClient(app)
    collected, cursor = [], None
    while True:
        url = "/api/admin/dj/songs?limit=25"
        if cursor:
            url += f"&cursor={cursor}"
        body = c.get(url, headers=_auth()).json()
        collected += [i["id"] for i in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(collected) == 60
    assert len(set(collected)) == 60  # no duplicates across pages
    # whole set is sorted by (title, artist, id) case-insensitively
    all_rows = c.get("/api/admin/dj/songs?limit=500", headers=_auth()).json()["items"]
    keys = [(t["title"].lower(), t["artist"].lower(), t["id"]) for t in all_rows]
    assert keys == sorted(keys)


def test_dj_songs_bad_sort_and_cursor_422(dj_env):
    cfg, _, _ = dj_env
    app = create_app(cfg)
    c = TestClient(app)
    assert c.get("/api/admin/dj/songs?sort=genre", headers=_auth()).status_code == 422
    bad = c.get("/api/admin/dj/songs?cursor=not-a-cursor", headers=_auth())
    assert bad.status_code == 422
    assert bad.json()["detail"]["code"] == "invalid_cursor"


def test_dj_songs_eligibility_advisory(dj_env):
    cfg, store, _ = dj_env
    now = time.time()
    store.add_item(type_="song", media_path="a.flac", duration_s=120,
                   title="Fresh", artist="F", fresh=True)
    recent = store.add_item(type_="song", media_path="b.flac", duration_s=120,
                            title="Recent", artist="R", fresh=False)
    store.commit_song_air(recent, now)  # aired just now -> inside the spacing floor
    old = store.add_item(type_="song", media_path="c.flac", duration_s=120,
                         title="Old", artist="O", fresh=False)
    store.commit_song_air(old, now - 4 * 3600)  # cleared the 1 h floor
    # emergency songs are excluded from the catalogue listing entirely
    store.add_item(type_="song", media_path="g.flac", duration_s=120,
                   title="Gone", artist="G", emergency=True)
    app = create_app(cfg)
    body = TestClient(app).get("/api/admin/dj/songs", headers=_auth()).json()
    by_title = {i["title"]: i for i in body["items"]}
    assert "Gone" not in by_title  # emergency songs never listed
    assert by_title["Fresh"]["eligible"] is True
    assert by_title["Recent"]["eligible"] is False
    assert by_title["Recent"]["reason"] is not None
    assert by_title["Old"]["eligible"] is True


def test_catalogue_revision_bumps_only_on_song_set_changes(dj_env):
    cfg, store, _ = dj_env
    assert store.catalogue_revision() == 0
    sid = store.add_item(type_="song", media_path="s.flac", duration_s=120,
                         title="Alpha", artist="One")
    assert store.catalogue_revision() == 1
    store.add_item(type_="liner", media_path="l.flac", duration_s=5)
    assert store.catalogue_revision() == 1  # a liner isn't part of the catalogue
    store.retire_item(sid, "test")
    assert store.catalogue_revision() == 2
    store.record_airplay(item_id=sid, seq=1, item_type="song",
                         started_at=None, position=None)
    assert store.catalogue_revision() == 2  # airplay is not a set change


def test_dj_songs_page_reports_catalogue_revision(dj_env):
    cfg, store, _ = dj_env
    store.add_item(type_="song", media_path="s.flac", duration_s=120, title="Alpha")
    app = create_app(cfg)
    body = TestClient(app).get("/api/admin/dj/songs", headers=_auth()).json()
    assert body["catalogue_revision"] == 1
