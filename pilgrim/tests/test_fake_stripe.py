"""PAYMENT.md §2.3 — the fake Stripe must sign like the real one."""
from __future__ import annotations

import json

import pytest
from pilgrim.config import Payments
from pilgrim.payments import StripeError, verify_webhook
from pilgrim.tests.fakes.fake_stripe import FakeStripe, sign_event

NOW = 1_800_000_000.0
SECRET = "whsec_fake"


def _fake(**overrides) -> FakeStripe:
    return FakeStripe(Payments(), webhook_secret=SECRET, now=NOW, **overrides)


async def test_create_and_retrieve():
    fake = _fake()
    session = await fake.create_checkout_session(request_id=3, token="tok", now=NOW)
    assert session.status == "open"
    assert session.payment_status == "unpaid"
    assert session.client_secret.startswith("cs_test_fake")
    assert session.metadata == {"request_id": "3", "token": "tok"}
    assert fake.create_calls == [{"request_id": 3, "token": "tok", "now": NOW}]
    again = await fake.retrieve_checkout_session(session.id)
    assert again.id == session.id
    assert fake.retrieve_calls == [session.id]


async def test_unknown_session_raises():
    fake = _fake()
    with pytest.raises(StripeError) as exc:
        await fake.retrieve_checkout_session("cs_nope")
    assert exc.value.kind == "rejected"


async def test_pay_returns_verifiable_event():
    fake = _fake()
    session = await fake.create_checkout_session(request_id=4, token="tok", now=NOW)
    payload, header = fake.pay(session.id, 500)
    event = verify_webhook(payload, header, SECRET, NOW, 300)
    assert event["type"] == "checkout.session.completed"
    obj = event["data"]["object"]
    assert obj["payment_status"] == "paid"
    assert obj["amount_total"] == 500
    assert obj["client_reference_id"] == "tok"
    assert obj["metadata"]["request_id"] == "4"
    assert fake.sessions[session.id].payment_status == "paid"


async def test_expire_returns_verifiable_event():
    fake = _fake()
    session = await fake.create_checkout_session(request_id=5, token="tok", now=NOW)
    payload, header = fake.expire(session.id)
    event = verify_webhook(payload, header, SECRET, NOW, 300)
    assert event["type"] == "checkout.session.expired"
    assert event["data"]["object"]["status"] == "expired"


async def test_async_failed_event():
    fake = _fake()
    session = await fake.create_checkout_session(request_id=6, token="tok", now=NOW)
    payload, header = fake.async_failed(session.id)
    event = verify_webhook(payload, header, SECRET, NOW, 300)
    assert event["type"] == "checkout.session.async_payment_failed"
    assert event["data"]["object"]["payment_status"] == "unpaid"


async def test_livemode_follows_key():
    fake = _fake(secret_key="sk_live_x")
    session = await fake.create_checkout_session(request_id=1, token="t", now=NOW)
    assert session.livemode is True
    payload, header = fake.pay(session.id)
    event = verify_webhook(payload, header, SECRET, NOW, 300)
    assert event["livemode"] is True


@pytest.mark.parametrize("kind", ["transport", "rejected", "server"])
async def test_create_failure_injection(kind):
    fake = _fake()
    fake.fail_next_create = kind
    with pytest.raises(StripeError) as exc:
        await fake.create_checkout_session(request_id=1, token="t", now=NOW)
    assert exc.value.kind == kind
    # injected once only: the next call succeeds
    session = await fake.create_checkout_session(request_id=1, token="t", now=NOW)
    assert session.status == "open"


@pytest.mark.parametrize("kind", ["transport", "rejected", "server"])
async def test_retrieve_failure_injection(kind):
    fake = _fake()
    session = await fake.create_checkout_session(request_id=1, token="t", now=NOW)
    fake.fail_next_retrieve = kind
    with pytest.raises(StripeError):
        await fake.retrieve_checkout_session(session.id)
    assert (await fake.retrieve_checkout_session(session.id)).id == session.id


def test_sign_event_is_deterministic():
    event = {"id": "evt_1", "type": "x", "livemode": False, "data": {"object": {}}}
    a = sign_event(event, SECRET, NOW)
    b = sign_event(event, SECRET, NOW)
    assert a == b
    assert json.loads(a[0])["id"] == "evt_1"
