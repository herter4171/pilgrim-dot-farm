"""PAYMENT.md Steps 4–6, 9 + 12b: paid requests (staged checks, checkout,
operator comp), webhook, status, admin (fakes only)."""
from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient
from pilgrim.config import Config
from pilgrim.payments import PaymentService
from pilgrim.pipelines.mcp import SearchOutcome
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

    async def _no_match(query: str) -> SearchOutcome:
        return SearchOutcome("empty")

    monkeypatch.setattr(station.moderator, "moderate", _moderate)
    monkeypatch.setattr(station.llm, "chat_text", _summary)
    monkeypatch.setattr(station.clock, "wall", lambda: FAKE_NOW)
    station.copyright.search = _no_match   # never the network (AGENTS §1.2)
    station.override_token = ""
    return cfg, app, station, fake


def _post_paid(client: TestClient, text: str):
    return client.post("/api/paid-requests", json={"text": text})


def _checkout(client: TestClient, token: str):
    return client.post(f"/api/paid-requests/{token}/checkout")


def _start(client: TestClient, text: str) -> dict:
    """Submit, let the staged checks run (TestClient waits for background
    tasks), then ask for Checkout. Returns the token plus the checkout body."""
    post = _post_paid(client, text)
    assert post.status_code == 200 and post.json()["ok"] is True, post.json()
    token = post.json()["token"]
    r = _checkout(client, token)
    return {"token": token, "status_code": r.status_code, **r.json()}


def _post_hook(client: TestClient, payload: bytes, header: str):
    return client.post("/api/stripe/webhook", content=payload,
                       headers={"stripe-signature": header,
                                "content-type": "application/json"})


def test_inactive_is_paid_closed(tmp_env, monkeypatch):
    _, app, station, _ = _build(tmp_env, monkeypatch, active=False)
    r = _post_paid(TestClient(app), "a song")
    assert r.status_code == 503
    body = r.json()
    assert body["paid_closed"] is True
    assert "off_air" not in body
    assert station.db.all_requests() == []


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
    body = _start(TestClient(app), "120 BPM in 4/4, 1970s feel, slow bossa nova")
    assert body["status_code"] == 200 and body["ok"] is True
    assert len(fake.create_calls) == 1


def test_moderation_rejection_creates_no_session(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch, moderate=False)
    c = TestClient(app)
    r = _post_paid(c, "something rude")
    assert r.status_code == 200 and r.json()["ok"] is True  # checks run after
    token = r.json()["token"]
    status = c.get(f"/api/paid-requests/{token}").json()
    assert status["status"] == "rejected"
    assert status["reason"] == "nope, not that"
    assert _checkout(c, token).status_code == 409
    assert fake.create_calls == []


def test_happy_path(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    r = _post_paid(c, "line one\nline two\nline three")
    assert r.status_code == 200
    assert r.json()["stage"] == "moderation"
    assert set(r.json()) == {"ok", "token", "stage"}
    assert fake.create_calls == []   # no Checkout before the checks pass
    token = r.json()["token"]
    status = c.get(f"/api/paid-requests/{token}").json()
    assert status["status"] == "checking" and status["stage"] == "ready"
    assert status["comped"] is False
    co = _checkout(c, token)
    assert co.status_code == 200
    body = {"token": token, **co.json()}
    assert set(co.json()) == {"ok", "client_secret", "publishable_key", "expires_at"}
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
    body = _start(TestClient(app), "a song about a tractor")
    assert body["status_code"] == 424
    assert "not charged" in body["detail"]
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

    async def _summary(model, system, user, max_tokens=4096):
        return "label"

    async def _no_match(query: str) -> SearchOutcome:
        return SearchOutcome("empty")

    monkeypatch.setattr(station.moderator, "moderate", _ok)
    monkeypatch.setattr(station.llm, "chat_text", _summary)
    station.copyright.search = _no_match
    c = TestClient(app)
    assert c.post("/api/requests", json={"text": "free one"}).status_code == 200
    assert c.post("/api/requests", json={"text": "free two"}).status_code == 429
    assert _post_paid(c, "paid one").status_code == 200
    assert _post_paid(c, "paid two").status_code == 429


def test_webhook_happy_path(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    token = _start(c, "a song")["token"]
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
    _start(c, "a song")
    session_id = list(fake.sessions)[0]
    payload, header = fake.pay(session_id)
    assert _post_hook(c, payload, header).json()["outcome"] == "applied"
    r2 = _post_hook(c, payload, header)
    assert r2.status_code == 200 and r2.json()["outcome"] == "duplicate"


def test_webhook_bad_signature(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    _start(c, "a song")
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
    body = _start(c, "a song")
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
    token = _start(c, "a song")["token"]
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
    token = _start(c, "a song")["token"]
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
    token = _start(c, "a song")["token"]
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
    token = _start(c, "a song")["token"]
    session_id = list(fake.sessions)[0]
    fake.pay(session_id, 350)
    c.get(f"/api/paid-requests/{token}")
    c.get(f"/api/paid-requests/{token}")
    assert fake.retrieve_calls == [session_id]


def test_admin_payments_route(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    body = _start(c, "a song")
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
    _start(c, "a song")
    payload, header = fake.pay(list(fake.sessions)[0])
    pretty = json.dumps(json.loads(payload), indent=2).encode()
    assert _post_hook(c, pretty, header).status_code == 400


async def test_summary_falls_back_when_llm_fails(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)

    async def _boom(model, system, user, max_tokens=4096):
        raise RuntimeError("llm down")

    monkeypatch.setattr(station.llm, "chat_text", _boom)
    token = _start(c, "[intro]\nreal prompt about a tractor")["token"]
    session_id = list(fake.sessions)[0]
    payload, header = fake.pay(session_id, 350)
    _post_hook(c, payload, header)
    row = station.db.get_request_by_token(token)
    assert row["status"] == "queued"
    assert row["summary"] == "real prompt about a tractor"
    assert c.get("/api/requests").json()["queue"][0]["text"] == \
        "real prompt about a tractor"


# --------------------------------------------------------------------------- #
# Step 12b: staged checks, copyright screen, checkout claim, operator comp
# --------------------------------------------------------------------------- #
SECRET = "comp-" + "x" * 40


class _Records(logging.Handler):
    """The station's logging setup keeps radio.* off pytest's caplog, so tests
    that assert on logs attach straight to the `radio` logger."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def radio_logs():
    h = _Records()
    lg = logging.getLogger("radio")
    old = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    yield h.records
    lg.removeHandler(h)
    lg.setLevel(old)


def _hit(title: str, description: str) -> SearchOutcome:
    return SearchOutcome("ok", [{"title": title, "description": description,
                                 "url": "https://example.test/lyrics"}])


WALK = ("Lyrics for Walk This Way by Aerosmith. Backstroke lover always hidin' "
        "'neath the covers 'Til I talked to your daddy, he say He said you ain't "
        "seen nothin' till you're down on a muffin")


def test_copied_lyrics_are_rejected_before_checkout(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)

    async def _search(query: str) -> SearchOutcome:
        return _hit("Aerosmith - Walk This Way Lyrics - Musixmatch", WALK)

    station.copyright.search = _search
    c = TestClient(app)
    text = ("Hard rock like the 70s.\n[verse]\n"
            "Backstroke lover always hidin' 'neath the covers\n"
            "'Til I talked to your daddy, he say\n")
    token = _post_paid(c, text).json()["token"]
    status = c.get(f"/api/paid-requests/{token}").json()
    assert status["status"] == "rejected"
    assert "Walk This Way" in status["reason"] and "not charged" in status["reason"]
    assert _checkout(c, token).status_code == 409
    assert fake.create_calls == []


def test_one_famous_line_is_allowed(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)

    async def _search(query: str) -> SearchOutcome:
        if "backstroke" in query.lower():
            return _hit("Aerosmith - Walk This Way Lyrics - Musixmatch", WALK)
        return SearchOutcome("empty")

    station.copyright.search = _search
    text = ("[verse]\nBackstroke lover always hidin' 'neath the covers\n"
            "My tractor sings a rusty song across the frozen field\n")
    body = _start(TestClient(app), text)
    assert body["status_code"] == 200 and len(fake.create_calls) == 1


def test_degraded_search_lets_the_request_proceed(tmp_env, monkeypatch, radio_logs):
    _, app, station, fake = _build(tmp_env, monkeypatch)

    async def _down(query: str) -> SearchOutcome:
        return SearchOutcome("degraded", error="engines suspended")

    station.copyright.search = _down
    body = _start(TestClient(app),
                  "[verse]\nMy tractor sings a rusty song across the frozen field")
    assert body["status_code"] == 200
    assert any(r.getMessage() == "payment.copyright_unchecked" for r in radio_logs)


def test_checkout_is_claimed_once(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    body = _start(c, "a song")
    assert body["status_code"] == 200
    assert _checkout(c, body["token"]).status_code == 409
    assert len(fake.create_calls) == 1


def test_checkout_refuses_a_row_still_checking(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    row = station.db.add_checking_request("a song")
    c = TestClient(app)
    assert _checkout(c, row["public_token"]).status_code == 409
    station.db.set_request_stage(int(row["id"]), "copyright")
    assert _checkout(c, row["public_token"]).status_code == 409
    assert _checkout(c, "A" * 24).status_code == 404
    assert fake.create_calls == []


def test_check_crash_rejects_without_charging(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)

    async def _boom(text: str, request_id=None):
        raise RuntimeError("screen exploded")

    monkeypatch.setattr(station.copyright, "check", _boom)
    c = TestClient(app)
    token = _post_paid(c, "a song").json()["token"]
    status = c.get(f"/api/paid-requests/{token}").json()
    assert status["status"] == "rejected" and "not charged" in status["reason"]
    assert fake.create_calls == []


def test_restart_abandons_orphaned_checks(tmp_env, monkeypatch):
    _, _, station, _ = _build(tmp_env, monkeypatch)
    row = station.db.add_checking_request("a song")
    assert station.db.abandon_checking_requests() == 1
    assert station.db.get_request(int(row["id"]))["status"] == "abandoned"


def test_operator_comp_skips_stripe(tmp_env, monkeypatch, radio_logs):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    station.override_token = SECRET
    c = TestClient(app)
    r = _post_paid(c, SECRET + "\nslow bossa nova about the night bus")
    assert r.status_code == 200 and r.json()["ok"] is True
    token = r.json()["token"]
    status = c.get(f"/api/paid-requests/{token}").json()
    assert status["status"] == "queued" and status["comped"] is True
    assert status["position"] == 1
    assert fake.create_calls == []
    row = station.db.get_request_by_token(token)
    assert row["text"] == "slow bossa nova about the night bus"
    assert row["amount_cents"] == 0 and row["paid_at"] and row["priority"] == 1
    assert row["checkout_session_id"] is None
    board = c.get("/api/requests").json()
    assert board["queue"][0]["paid"] is True
    # the secret never reaches the DB, the logs, or the board
    assert all(SECRET not in str(v) for r_ in station.db.all_requests()
               for v in r_.values())
    assert any(rec.getMessage() == "payment.comped" for rec in radio_logs)
    assert all(SECRET not in str(rec.__dict__) for rec in radio_logs)
    assert SECRET not in json.dumps(board)
    assert _checkout(c, token).status_code == 409   # nothing to pay


def test_operator_comp_works_with_payments_closed(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch, active=False)
    station.override_token = SECRET
    c = TestClient(app)
    assert _post_paid(c, "a normal paid song").status_code == 503
    token = _post_paid(c, SECRET + "\na comped song").json()["token"]
    status = c.get(f"/api/paid-requests/{token}").json()
    assert status["status"] == "queued" and status["comped"] is True


def test_comp_still_runs_moderation(tmp_env, monkeypatch):
    _, app, station, _ = _build(tmp_env, monkeypatch, moderate=False)
    station.override_token = SECRET
    c = TestClient(app)
    token = _post_paid(c, SECRET + "\nsomething rude").json()["token"]
    assert c.get(f"/api/paid-requests/{token}").json()["status"] == "rejected"


def test_wrong_or_short_secret_is_just_text(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    c = TestClient(app)
    station.override_token = "short"           # too short: feature off
    body = _start(c, "short\na song")
    assert body["status_code"] == 200
    station.override_token = SECRET
    body = _start(c, SECRET[:-1] + "y\nanother song")  # near miss
    assert body["status_code"] == 200
    assert len(fake.create_calls) == 2


def test_secret_elsewhere_is_rejected_and_not_stored(tmp_env, monkeypatch):
    _, app, station, fake = _build(tmp_env, monkeypatch)
    station.override_token = SECRET
    c = TestClient(app)
    r = _post_paid(c, "a song\n" + SECRET)
    assert r.json()["rejected"] is True
    r = c.post("/api/requests", json={"text": "hey " + SECRET[:100]})
    assert r.json()["rejected"] is True
    assert station.db.all_requests() == []


def test_status_route_works_without_payments(tmp_env, monkeypatch):
    _, app, station, _ = _build(tmp_env, monkeypatch, active=False)
    row = station.db.add_checking_request("a song", comped=True)
    r = TestClient(app).get(f"/api/paid-requests/{row['public_token']}")
    assert r.status_code == 200
    assert r.json()["stage"] == "moderation" and r.json()["comped"] is True


def test_paid_limit_fits_a_full_minimax_input(tmp_env, monkeypatch):
    """Step 12c: a full caption + lyric sheet (~4,800 chars, 65 lines) fits."""
    cfg, app, _, _ = _build(tmp_env, monkeypatch)
    assert cfg.payments.paid_max_length == 6000
    assert cfg.payments.paid_max_lines == 200
    text = "\n".join(["[verse]"] + ["a tractor hums beneath the moon"] * 150)
    assert len(text) < 6000
    assert _post_paid(TestClient(app), text).status_code == 200


# --------------------------------------------------------------------------- #
# board + free-line cap + summary backfill (Oct 31)
# --------------------------------------------------------------------------- #
def test_free_line_caps_waiting_free_requests_paid_never(tmp_env, monkeypatch):
    cfg, app, station, fake = _build(tmp_env, monkeypatch)
    cfg.requests.free_queue_max = 2
    cfg.requests.per_client_per_10min = 50
    app = create_app(cfg)   # limiters are built at app creation
    station2 = app.state.station
    station2.payments_active, station2.payment_service = True, station.payment_service
    station2.publishable_key = "pk_test_fake"

    async def _ok(_text):
        return True, None

    monkeypatch.setattr(station2.moderator, "moderate", _ok)
    c = TestClient(app)
    assert c.post("/api/requests", json={"text": "one"}).json()["ok"] is True
    assert c.post("/api/requests", json={"text": "two"}).json()["ok"] is True
    full = c.post("/api/requests", json={"text": "three"}).json()
    assert full["ok"] is False and full["line_full"] is True
    assert "full" in full["reason"]
    assert [r["text"] for r in station2.db.all_requests()] == ["two", "one"]  # nobody dropped
    # paid requests are never capped
    station2.override_token = SECRET
    station2.copyright.search = station.copyright.search
    monkeypatch.setattr(station2.llm, "chat_text", station.llm.chat_text)
    r = _post_paid(c, SECRET + "\na paid song while the free line is full")
    assert r.json()["ok"] is True


def test_board_shows_song_title_once_ready(tmp_env, monkeypatch):
    _, app, station, _ = _build(tmp_env, monkeypatch)
    db = station.db
    waiting = db.add_request("still waiting", cap=None)
    ready = db.add_request("made already", cap=None)
    song = db.add_item(type_="song", media_path="/x.flac", duration_s=60,
                       title="The Hen House Heist", artist="The Coop")
    db.mark_request_ready(int(ready["id"]), song, None)
    rows = {r["text"]: r for r in TestClient(app).get("/api/requests").json()["queue"]}
    assert rows["made already"]["song_title"] == "The Hen House Heist"
    assert rows["still waiting"]["song_title"] == ""
    assert waiting["id"]


async def test_backfill_replaces_raw_text_labels(tmp_env, monkeypatch):
    from pilgrim.server import _backfill_paid_summaries, _fallback_summary
    _, _, station, _ = _build(tmp_env, monkeypatch)
    db = station.db
    long_text = "There is still fun to be had here. " * 10
    raw = db.add_paid_request(long_text, summary=_fallback_summary(long_text, 160))
    good = db.add_paid_request("x", summary="A tidy sentence already.")
    for row in (raw, good):
        db.attach_checkout(int(row["id"]), f"cs_{row['id']}")
        db.mark_request_paid(int(row["id"]), session_id=f"cs_{row['id']}",
                             payment_intent_id=None, amount_cents=350,
                             currency="usd", livemode=False)
    await _backfill_paid_summaries(station)
    assert db.get_request(int(raw["id"]))["summary"] == \
        "dreamy bossa nova about the night bus"
    assert db.get_request(int(good["id"]))["summary"] == "A tidy sentence already."


def test_abandon_window_is_72_hours(cfg):
    assert cfg.payments.abandon_after_s == 72 * 3600
    assert cfg.requests.queue_cap == 50 and cfg.requests.free_queue_max == 100
