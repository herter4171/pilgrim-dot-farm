"""OVERHAUL 4.1 — deterministic pre-filter for the request line."""
from __future__ import annotations

import pytest
from pilgrim.pipelines.request_filter import REASON, prefilter


@pytest.mark.parametrize("text", [
    "check out mysite.com",
    "https://x.y",
    "www.farm",
    "foo dot com",
    "call 555-123-4567",
    "me@x.org",
    "follow @pilgrim",
    "visit example-network.io now",
    "email me at mail.us",
])
def test_prefilter_rejects(text):
    assert prefilter(text) == REASON


@pytest.mark.parametrize("text", [
    "play some polka",
    "a song about 3 cows",
    "dedicate one to Dr. Smith",
    "at 10 pm",
    "the barn door needs a tune",
])
def test_prefilter_allows_clean(text):
    assert prefilter(text) is None


def test_api_request_with_url_never_calls_moderator(tmp_env):
    """The LLM must NOT be called for a request that hits the pre-filter."""
    from fastapi.testclient import TestClient
    from pilgrim.server import create_app

    cfg, _, _ = tmp_env
    app = create_app(cfg)
    station = app.state.station

    calls = {"n": 0}

    class _Counting:
        async def moderate(self, text):
            calls["n"] += 1
            return True, "ok"

    station.moderator = _Counting()
    c = TestClient(app)  # no `with`: no background tasks
    r = c.post("/api/requests", json={"text": "check out mysite.com"})
    assert r.status_code == 200
    payload = r.json()
    assert payload["rejected"] is True
    assert calls["n"] == 0
    assert station.db.queued_requests() == []


def test_rate_limited_after_fourth_request(tmp_env):
    """OVERHAUL 4.3: the 4th request from one client is 429 and the fake
    moderator is called at most 3 times."""
    from fastapi.testclient import TestClient
    from pilgrim.server import create_app

    cfg, _, _ = tmp_env
    app = create_app(cfg)
    station = app.state.station
    calls = {"n": 0}

    class _Counting:
        async def moderate(self, text):
            calls["n"] += 1
            return True, "ok"

    station.moderator = _Counting()
    c = TestClient(app)
    codes = []
    for _ in range(4):
        r = c.post("/api/requests", json={"text": "play the accordion"})
        codes.append(r.status_code)
    assert codes[:3] == [200, 200, 200]
    assert codes[3] == 429
    assert calls["n"] <= 3


def test_request_board_in_api(tmp_env):
    """OVERHAUL 4.8: a `ready` request appears in the queue with status; an
    aired one appears in `recent` with its song title."""
    from fastapi.testclient import TestClient
    from pilgrim.server import create_app

    cfg, store, _ = tmp_env
    app = create_app(cfg)

    req = store.add_request("play for mittens", cap=10)
    song_id = store.add_item(type_="song", media_path="/m.flac", duration_s=45.0,
                             title="Mittens at the Barn", artist="The Barn Cats")
    store.mark_request_ready(req["id"], song_id, None)
    c = TestClient(app)
    d = c.get("/api/requests").json()
    assert any(q["id"] == req["id"] and q["status"] == "ready" for q in d["queue"])

    store.mark_request_aired(req["id"])
    d = c.get("/api/requests").json()
    rec = next((r for r in d["recent"] if r["id"] == req["id"]), None)
    assert rec is not None and rec["song_title"] == "Mittens at the Barn"
