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
