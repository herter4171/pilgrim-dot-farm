"""PAYMENT.md §2.4 — signature, client, config (fakes only, no Stripe calls)."""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pilgrim.config import Payments
from pilgrim.payments import (
    CheckoutSession,
    StripeClient,
    StripeError,
    WebhookError,
    verify_webhook,
)
from pilgrim.tests.fakes.fake_stripe import sign_event
from pydantic import ValidationError

SECRET = "whsec_test_value"
NOW = 1_800_000_000.0
TOLERANCE = 300


def _event(**overrides):
    event = {
        "id": "evt_1",
        "type": "checkout.session.completed",
        "livemode": False,
        "data": {"object": {"id": "cs_1"}},
    }
    event.update(overrides)
    return event


def _signed(event=None, secret=SECRET, now=NOW):
    return sign_event(event or _event(), secret, now)


# ------------------------------------------------------------------ signature
def test_valid_signature_passes():
    payload, header = _signed()
    event = verify_webhook(payload, header, SECRET, NOW, TOLERANCE)
    assert event["id"] == "evt_1"
    assert event["type"] == "checkout.session.completed"


def test_wrong_secret_rejected():
    payload, header = _signed(secret="whsec_other")
    with pytest.raises(WebhookError):
        verify_webhook(payload, header, SECRET, NOW, TOLERANCE)


def test_tampered_body_rejected():
    payload, header = _signed()
    tampered = payload.replace(b"cs_1", b"cs_2")
    with pytest.raises(WebhookError):
        verify_webhook(tampered, header, SECRET, NOW, TOLERANCE)


def test_reserialised_body_rejected():
    """Same JSON, different bytes: proves we verify the raw body."""
    payload, header = _signed()
    pretty = json.dumps(json.loads(payload), indent=2).encode()
    with pytest.raises(WebhookError):
        verify_webhook(pretty, header, SECRET, NOW, TOLERANCE)


def test_missing_v1_rejected():
    _, header = _signed()
    timestamp = header.split(",")[0]
    with pytest.raises(WebhookError):
        verify_webhook(b"{}", timestamp, SECRET, NOW, TOLERANCE)


def test_stale_timestamp_rejected():
    payload, header = _signed(now=NOW - TOLERANCE - 1)
    with pytest.raises(WebhookError):
        verify_webhook(payload, header, SECRET, NOW, TOLERANCE)


def test_future_timestamp_rejected():
    payload, header = _signed(now=NOW + TOLERANCE + 1)
    with pytest.raises(WebhookError):
        verify_webhook(payload, header, SECRET, NOW, TOLERANCE)


def test_malformed_header_rejected():
    with pytest.raises(WebhookError):
        verify_webhook(b"{}", "garbage", SECRET, NOW, TOLERANCE)
    with pytest.raises(WebhookError):
        verify_webhook(b"{}", "", SECRET, NOW, TOLERANCE)
    with pytest.raises(WebhookError):
        verify_webhook(b"{}", "t=abc,v1=deadbeef", SECRET, NOW, TOLERANCE)


def test_any_matching_v1_passes():
    payload, header = _signed()
    good = header.split("v1=")[1]
    event = verify_webhook(
        payload, f"t={int(NOW)},v1=deadbeef,v1={good}", SECRET, NOW, TOLERANCE
    )
    assert event["id"] == "evt_1"


def test_missing_event_fields_rejected():
    payload, header = _signed({"id": "evt_1", "type": "x", "data": {"object": {}}})
    with pytest.raises(WebhookError):
        verify_webhook(payload, header, SECRET, NOW, TOLERANCE)


# ---------------------------------------------------------------------- client
def _payments(**overrides: Any) -> Payments:
    base: dict[str, Any] = {
        "enabled": True,
        "price_id_live": "price_live_1",
        "price_id_test": "price_test_1",
        "timeout_s": 7,
        "retries": 2,
    }
    base.update(overrides)
    return Payments(**base)


def _session_body(**overrides):
    body = {
        "id": "cs_live_1",
        "object": "checkout.session",
        "status": "open",
        "payment_status": "unpaid",
        "livemode": True,
        "client_secret": "cs_live_1_secret_abcdef",
        "amount_total": 350,
        "currency": "usd",
        "client_reference_id": "tok",
        "metadata": {"request_id": "7", "token": "tok"},
        "expires_at": 1_800_001_800,
        "unknown_field": "ignored",
    }
    body.update(overrides)
    return body


class _Recorder:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.responses.pop(0) if self.responses else (200, _session_body())
        return httpx.Response(status, json=body)


def _client(recorder, cfg=None, key="sk_live_TESTKEY") -> StripeClient:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(recorder),
        base_url=(cfg or _payments()).api_base,
        timeout=(cfg or _payments()).timeout_s,
        headers={"Stripe-Version": (cfg or _payments()).api_version},
    )
    return StripeClient(cfg or _payments(), key, http=http)


async def test_create_sends_exact_fields():
    cfg = _payments()
    rec = _Recorder([(200, _session_body())])
    client = _client(rec, cfg)
    session = await client.create_checkout_session(request_id=7, token="tok", now=NOW)
    await client.aclose()

    assert isinstance(session, CheckoutSession)
    assert session.id == "cs_live_1"
    form = parse_qs(rec.requests[0].content.decode())
    assert form["ui_mode"] == ["embedded_page"]
    assert form["mode"] == ["payment"]
    assert form["line_items[0][price]"] == ["price_live_1"]
    assert form["line_items[0][quantity]"] == ["1"]
    assert form["redirect_on_completion"] == ["never"]
    assert form["client_reference_id"] == ["tok"]
    assert form["metadata[request_id]"] == ["7"]
    assert form["metadata[token]"] == ["tok"]
    assert form["payment_intent_data[metadata][request_id]"] == ["7"]
    assert form["managed_payments[enabled]"] == ["false"]
    assert form["expires_at"] == [str(int(NOW + cfg.checkout_expiry_s))]
    assert "payment_method_types[0]" not in form
    assert "return_url" not in form
    assert rec.requests[0].headers["Idempotency-Key"] == "pilgrim-checkout-7"
    assert rec.requests[0].headers["Stripe-Version"] == cfg.api_version


async def test_test_key_uses_test_price():
    cfg = _payments()
    rec = _Recorder([(200, _session_body())])
    client = _client(rec, cfg, key="rk_test_TESTKEY")
    await client.create_checkout_session(request_id=1, token="t", now=NOW)
    form = parse_qs(rec.requests[0].content.decode())
    assert form["line_items[0][price]"] == ["price_test_1"]


async def test_no_price_for_mode_raises():
    cfg = _payments(price_id_test="")
    client = _client(_Recorder([]), cfg, key="sk_test_X")
    with pytest.raises(StripeError) as exc:
        await client.create_checkout_session(request_id=1, token="t", now=NOW)
    assert exc.value.kind == "rejected"


async def test_retries_on_server_error_then_succeeds():
    rec = _Recorder([(500, {"error": {"message": "boom", "type": "api_error"}}),
                     (200, _session_body())])
    client = _client(rec)
    session = await client.create_checkout_session(request_id=1, token="t", now=NOW)
    assert session.id == "cs_live_1"
    assert len(rec.requests) == 2


async def test_no_retry_on_rejected():
    rec = _Recorder([(400, {"error": {"message": "bad", "type": "invalid_request_error",
                                      "code": "parameter_unknown"}})])
    client = _client(rec)
    with pytest.raises(StripeError) as exc:
        await client.create_checkout_session(request_id=1, token="t", now=NOW)
    assert exc.value.kind == "rejected"
    assert len(rec.requests) == 1


async def test_retries_exhausted_on_transport():
    def boom(_request):
        raise httpx.ConnectError("no route")

    cfg = _payments(retries=2)
    http = httpx.AsyncClient(transport=httpx.MockTransport(boom), base_url=cfg.api_base,
                             timeout=cfg.timeout_s)
    client = StripeClient(cfg, "sk_live_X", http=http)
    with pytest.raises(StripeError) as exc:
        await client.create_checkout_session(request_id=1, token="t", now=NOW)
    assert exc.value.kind == "transport"


async def test_retrieve_parses_session():
    rec = _Recorder([(200, _session_body(status="complete", payment_status="paid",
                                        payment_intent="pi_1"))])
    client = _client(rec)
    session = await client.retrieve_checkout_session("cs_live_1")
    assert session.status == "complete"
    assert session.payment_status == "paid"
    assert session.payment_intent == "pi_1"
    assert rec.requests[0].method == "GET"
    assert str(rec.requests[0].url).endswith("/v1/checkout/sessions/cs_live_1")


async def test_timeout_is_configured():
    cfg = _payments(timeout_s=3.5)
    client = _client(_Recorder([]), cfg)
    assert client._http.timeout == httpx.Timeout(3.5)


async def test_secrets_never_logged(caplog):
    caplog.set_level("DEBUG")
    key = "sk_live_SUPERSECRETKEY"
    rec = _Recorder([(400, {"error": {"message": "bad", "type": "invalid_request_error"}}),
                     (500, {"error": {"message": "boom", "type": "api_error"}})])
    client = _client(rec, key=key)
    with pytest.raises(StripeError):
        await client.create_checkout_session(request_id=1, token="t", now=NOW)
    rec2 = _Recorder([(500, {"error": {"message": "boom"}})])
    client2 = _client(rec2, cfg=_payments(retries=0), key=key)
    with pytest.raises(StripeError):
        await client2.create_checkout_session(request_id=1, token="t", now=NOW)
    text = caplog.text
    assert "SUPERSECRETKEY" not in text
    assert "whsec_" not in text
    assert "cs_live_1_secret" not in text


# ---------------------------------------------------------------------- config
def test_payments_inactive_when_any_env_missing():
    cfg = Payments(enabled=True)
    full = {"STRIPE_SECRET_KEY": "sk_live_x", "STRIPE_PUBLISHABLE_KEY": "pk_live_x",
            "STRIPE_WEBHOOK_SECRET": "whsec_x"}
    assert cfg.active(full) is True
    for missing in full:
        partial = {k: v for k, v in full.items() if k != missing}
        assert cfg.active(partial) is False
    assert Payments(enabled=False).active(full) is False


def test_mode_and_price_follow_key_prefix():
    cfg = Payments(price_id_live="price_live_1", price_id_test="price_test_1")
    assert cfg.mode("sk_live_x") == "live"
    assert cfg.mode("rk_live_x") == "live"
    assert cfg.mode("sk_test_x") == "test"
    assert cfg.mode("rk_test_x") == "test"
    assert cfg.mode("pk_live_x") == ""
    assert cfg.price_id("sk_live_x") == "price_live_1"
    assert cfg.price_id("sk_test_x") == "price_test_1"


@pytest.mark.parametrize("kwargs", [
    {"min_cents": 0},
    {"min_cents": 7000, "max_cents": 6769},
    {"checkout_expiry_s": 60},
    {"webhook_tolerance_s": 0},
    {"retries": -1},
    {"paid_max_length": 0},
    {"per_client_per_10min": 0},
    {"max_attempts": 0},
])
def test_payments_validation(kwargs):
    with pytest.raises(ValidationError):
        Payments(**kwargs)
