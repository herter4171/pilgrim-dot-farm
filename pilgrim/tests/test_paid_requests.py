"""PAYMENT.md Steps 4–6 + 9: paid requests, webhook, status, admin (fakes)."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from pilgrim.config import Config
from pilgrim.payments import PaymentService
from pilgrim.server import create_app, create_public_app
from pilgrim.tests.fakes.fake_stripe import FakeStripe, sign_event

TOKEN = "dj-test-token"
FAKE_NOW = 1_800_000_000.0


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def _build(tmp_env, monkeypatch, *, active: bool = True, moderate: bool = True):
    cfg: Config = tmp_env[0]
    cfg.payments.enabled = active
    cfg.payments.price_id_test = "price_test_1"
    cfg.payments.reconcile_after_s = 0
    cfg.payments.reconcile_min_interval_s = 0
    cfg.payments.per_client_per_10min = 5
    monkeypatch.setenv("PILGRIM_DJ_TOKEN", TOKEN)
    app = create_app(cfg)
    station = app.state.station
    fake = FakeStripe(cfg.payments, secret_key="sk_test_fake",
                      webhook_secret="whsec_fake", now=FAKE_NOW)
    station.payments_active = active
    station.publishable_key = "pk_test_fake"
    station.webhook_secret = "whsec_fake"
    station.payment_service = PaymentService(
        cfg.payments, station.db, fake, "sk_test_fake", "pk_test_fake",
        "whsec_fake", now=lambda: FAKE_NOW) if active else None

    async def _moderate(text: str):
        return (True, None) if moderate else (False, "nope, not that")

    async def _summary(model, system, user, max_tokens=4096):
        return "dreamy bossa nova about the night bus"

    monkeypatch.setattr(station.moderator, "moderate", _moderate)
    monkeypatch.setattr(station.llm, "chat_text", _summary)
    monkeypatch.setattr(station.clock, "wall", lambda: FAKE_NOW)
    return cfg, app, station, fake


def _post_paid(client: TestClient, text: str):
    return client.post("/api/paid-requests", json={"text": text})


def _post_hook(client: TestClient, payload: bytes, header: str):
    return client.post("/api/stripe/webhook", content=payload,
                       headers={"stripe-signature": header,
                                "content-type": "application/json"})


def test_inactive_is_paid_closed(tmp_env, monkeypatch):
    _, app, _, _ = _build(tmp_env, monkeypatch, active=False)
    r = _post_paid(TestClient(app), "a song")
    assert r.status_code == 503
    body = r.json()
    assert body["paid_closed"] is True
    assert "off_air" not in body


def test_validation_and_no_store(tmp_env, monkeypatch):
    cfg, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    assert _post_paid(c, "   ").status_code == 422
    assert _post_paid(c, "x" * (cfg.payments.paid_max_length + 1)).status_code == 422
    assert _post_paid(c, "\n".join(["line"] * (cfg.payments.paid_max_lines + 1))).status_code == 422
    assert fake.create_calls == []
    assert station.db.all_requests() == []


def test_prefilter_rejection_creates_no_session(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    r = _post_paid(TestClient(app), "call me 555 123 4567")
    assert r.status_code == 200
    assert r.json()["rejected"] is True
    assert fake.create_calls == []
    rows = station.db.all_requests()
    assert len(rows) == 1 and rows[0]["status"] == "rejected" and rows[0]["priority"] == 1


def test_detailed_numbers_pass_runs_mode(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    r = _post_paid(TestClient(app), "120 BPM in 4/4, 1970s feel, slow bossa nova")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert len(fake.create_calls) == 1


def test_moderation_rejection_creates_no_session(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch, moderate=False)
    r = _post_paid(TestClient(app), "something rude")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "rejected": True, "reason": "nope, not that"}
    assert fake.create_calls == []


def test_happy_path(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    r = _post_paid(TestClient(app), "line one\nline two\nline three")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"ok", "token", "client_secret", "publishable_key", "expires_at"}
    assert body["publishable_key"] == "pk_test_fake"
    row = station.db.get_request_by_token(body["token"])
    assert row["status"] == "awaiting_payment"
    assert row["priority"] == 1
    assert row["checkout_session_id"] == fake.sessions[list(fake.sessions)[0]].id
    # not on the board
    board = TestClient(app).get("/api/requests").json()
    assert board["queue"] == []
    assert board["paid_enabled"] is True


def test_checkout_failure_is_424_and_abandoned(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    fake.fail_next_create = "server"
    r = _post_paid(TestClient(app), "a song about a tractor")
    assert r.status_code == 424
    assert "not charged" in r.json()["detail"]
    rows = station.db.all_requests()
    assert rows[0]["status"] == "abandoned"


def test_paid_rate_limit_is_independent(tmp_env, monkeypatch):
    cfg, app, _, fake = _build(tmp_env, monkeypatch)
    cfg.requests.per_client_per_10min = 1
    cfg.payments.per_client_per_10min = 1
    # the limiter is built at app creation; rebuild with the new limits
    app = create_app(cfg)
    station = app.state.station
    station.payments_active = True
    station.publishable_key = "pk_test_fake"
    station.webhook_secret = "whsec_fake"
    station.payment_service = PaymentService(
        cfg.payments, station.db, fake, "sk_test_fake", "pk_test_fake",
        "whsec_fake", now=lambda: FAKE_NOW)

    async def _ok(_text):
        return True, None

    monkeypatch.setattr(station.moderator, "moderate", _ok)
    c = TestClient(app)
    assert c.post("/api/requests", json={"text": "free one"}).status_code == 200
    assert c.post("/api/requests", json={"text": "free two"}).status_code == 429
    assert _post_paid(c, "paid one").status_code == 200
    assert _post_paid(c, "paid two").status_code == 429


def test_webhook_happy_path(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    token = _post_paid(c, "a song").json()["token"]
    session_id = list(fake.sessions)[0]
    payload, header = fake.pay(session_id, 350)
    r = _post_hook(c, payload, header)
    assert r.status_code == 200 and r.json()["outcome"] == "applied"
    row = station.db.get_request_by_token(token)
    assert row["status"] == "queued"
    assert row["priority"] == 1
    assert row["amount_cents"] == 350
    assert row["payment_intent_id"].startswith("pi_fake_")
    assert row["paid_at"]
    board = c.get("/api/requests").json()
    assert board["queue"][0]["text"] == "dreamy bossa nova about the night bus"
    assert board["queue"][0]["paid"] is True
    assert station.db.get_request_by_token(token)["summary"] == \
        "dreamy bossa nova about the night bus"


def test_webhook_duplicate(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    _post_paid(c, "a song")
    session_id = list(fake.sessions)[0]
    payload, header = fake.pay(session_id)
    assert _post_hook(c, payload, header).json()["outcome"] == "applied"
    r2 = _post_hook(c, payload, header)
    assert r2.status_code == 200 and r2.json()["outcome"] == "duplicate"


def test_webhook_bad_signature(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    _post_paid(c, "a song")
    payload, header = fake.pay(list(fake.sessions)[0])
    r = _post_hook(c, payload, "t=1,v1=deadbeef")
    assert r.status_code == 400
    assert station.db.all_requests()[0]["status"] == "awaiting_payment"


def _manual_event(session_id: str, *, payment_status="paid", livemode=False,
                  request_id="1", token="tok", amount=350, currency="usd"):
    return {
        "id": f"evt_manual_{session_id}_{payment_status}_{amount}",
        "type": "checkout.session.completed",
        "livemode": livemode,
        "data": {"object": {
            "id": session_id, "status": "complete",
            "payment_status": payment_status, "livemode": livemode,
            "amount_total": amount, "currency": currency,
            "payment_intent": "pi_manual",
            "client_reference_id": token,
            "metadata": {"request_id": request_id},
        }},
    }


@pytest.mark.parametrize("overrides", [
    {"amount": 299}, {"amount": 6770}, {"currency": "eur"}, {"livemode": True},
])
def test_webhook_mismatches_change_nothing(tmp_env, monkeypatch, overrides):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    body = _post_paid(c, "a song").json()
    session_id = list(fake.sessions)[0]
    event = _manual_event(session_id, token=body["token"], **overrides)
    payload, header = sign_event(event, "whsec_fake", FAKE_NOW)
    r = _post_hook(c, payload, header)
    assert r.status_code == 200
    assert r.json()["outcome"] in ("mismatch", "ignored")
    assert station.db.get_request_by_token(body["token"])["status"] == "awaiting_payment"


def test_webhook_unknown_session_is_mismatch(tmp_env, monkeypatch):
    _, app, station, _ = _build(tmp_env, monkeypatch)
    event = _manual_event("cs_nope", request_id="1", token="tok")
    payload, header = sign_event(event, "whsec_fake", FAKE_NOW)
    r = _post_hook(TestClient(app), payload, header)
    assert r.status_code == 200 and r.json()["outcome"] == "mismatch"


def test_expired_then_late_paid_wins(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    token = _post_paid(c, "a song").json()["token"]
    session_id = list(fake.sessions)[0]
    payload, header = fake.expire(session_id)
    assert _post_hook(c, payload, header).json()["outcome"] == "applied"
    assert station.db.get_request_by_token(token)["status"] == "abandoned"
    payload, header = fake.pay(session_id)
    assert _post_hook(c, payload, header).json()["outcome"] == "applied"
    assert station.db.get_request_by_token(token)["status"] == "queued"


def test_unpaid_completed_then_async_succeeded(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    token = _post_paid(c, "a song").json()["token"]
    session_id = list(fake.sessions)[0]
    session = fake.sessions[session_id].model_copy(update={"payment_status": "unpaid"})
    payload, header = fake.event("checkout.session.completed", session)
    assert _post_hook(c, payload, header).json()["outcome"] == "ignored"
    assert station.db.get_request_by_token(token)["status"] == "awaiting_payment"
    paid = fake.sessions[session_id].model_copy(
        update={"payment_status": "paid", "amount_total": 350, "payment_intent": "pi_x"})
    payload, header = fake.event("checkout.session.async_payment_succeeded", paid)
    assert _post_hook(c, payload, header).json()["outcome"] == "applied"
    assert station.db.get_request_by_token(token)["status"] == "queued"


def test_status_route_reconciles_without_webhook(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    token = _post_paid(c, "a song").json()["token"]
    session_id = list(fake.sessions)[0]
    fake.pay(session_id, 350)  # paid at Stripe, no event delivered
    r = c.get(f"/api/paid-requests/{token}")
    assert r.status_code == 200
    assert r.json()["status"] == "queued"
    assert r.json()["position"] == 1
    assert r.headers["cache-control"] == "no-store"


def test_status_route_unknown_and_malformed(tmp_env, monkeypatch):
    _, app, _, _ = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    assert c.get("/api/paid-requests/short").status_code == 404
    assert c.get("/api/paid-requests/" + "a" * 24).status_code == 404
    assert c.get("/api/paid-requests/bad$token!!!!!!!!!!!!").status_code == 404


def test_status_reconcile_is_rate_limited(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    station.cfg.payments.reconcile_min_interval_s = 60
    c = TestClient(app)
    token = _post_paid(c, "a song").json()["token"]
    session_id = list(fake.sessions)[0]
    fake.pay(session_id, 350)
    c.get(f"/api/paid-requests/{token}")
    c.get(f"/api/paid-requests/{token}")
    assert fake.retrieve_calls == [session_id]


def test_admin_payments_route(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    body = _post_paid(c, "a song").json()
    assert c.get("/api/admin/payments").status_code == 401
    r = c.get("/api/admin/payments", headers=_auth())
    assert r.status_code == 200
    rows = r.json()["requests"]
    assert rows and rows[0]["id"] == station.db.get_request_by_token(body["token"])["id"]
    assert "client_secret" not in rows[0]
    assert set(rows[0]) == {"id", "status", "priority", "created_at", "paid_at",
                            "amount_cents", "currency", "payment_intent_id",
                            "checkout_session_id", "attempts", "text"}
    assert r.json()["mismatches"] == []


def test_admin_payments_not_on_public_app(tmp_env, monkeypatch):
    cfg, app, station, _ = _build(tmp_env, monkeypatch)
    public = TestClient(create_public_app(cfg, station))
    assert public.get("/api/admin/payments").status_code == 404


def test_webhook_disabled_is_503(tmp_env, monkeypatch):
    _, app, _, _ = _build(tmp_env, monkeypatch, active=False)
    r = TestClient(app).post("/api/stripe/webhook", content=b"{}",
                             headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 503


def test_webhook_oversize_is_413(tmp_env, monkeypatch):
    _, app, _, _ = _build(tmp_env, monkeypatch)
    r = TestClient(app).post("/api/stripe/webhook", content=b"x" * (256 * 1024 + 1),
                             headers={"stripe-signature": "t=1,v1=x"})
    assert r.status_code == 413


def test_webhook_raw_bytes_not_reserialised(tmp_env, monkeypatch):
    _, app, _, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    _post_paid(c, "a song")
    payload, header = fake.pay(list(fake.sessions)[0])
    pretty = json.dumps(json.loads(payload), indent=2).encode()
    assert _post_hook(c, pretty, header).status_code == 400


async def test_summary_falls_back_when_llm_fails(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)

    async def _boom(model, system, user, max_tokens=4096):
        raise RuntimeError("llm down")

    monkeypatch.setattr(station.llm, "chat_text", _boom)
    token = _post_paid(c, "[intro]\nreal prompt about a tractor").json()["token"]
    session_id = list(fake.sessions)[0]
    payload, header = fake.pay(session_id, 350)
    _post_hook(c, payload, header)
    row = station.db.get_request_by_token(token)
    assert row["status"] == "queued"
    assert row["summary"] == "real prompt about a tractor"
    assert c.get("/api/requests").json()["queue"][0]["text"] == \
        "real prompt about a tractor"
