"""Stripe client + webhook verification for paid priority requests.

PAYMENT.md §2.2. Two halves live here: the thin async Stripe client (Checkout
Sessions only) and the pure `verify_webhook` function. State transitions
(`PaymentService`) arrive with the store in Step 3/4.

Nothing in this module is imported by `scheduler.py`, `selector.py` or
`producer.py` (AGENTS §1.11, PAYMENT.md §0.3).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pilgrim.config import Payments

log = logging.getLogger("pilgrim.payments")

MAX_WEBHOOK_BYTES = 256 * 1024


class StripeError(RuntimeError):
    """A failed Stripe call: `kind` is `transport`, `rejected` (4xx) or `server`."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class WebhookError(RuntimeError):
    """A webhook that cannot be trusted: bad signature, stale, or malformed."""


class CheckoutSession(BaseModel):
    """Only the fields we use (docs/backends.md §8). Unknown fields ignored."""

    model_config = ConfigDict(extra="ignore")

    id: str
    status: str
    payment_status: str
    livemode: bool
    client_secret: str | None = None
    amount_total: int | None = None
    currency: str | None = None
    payment_intent: str | None = None
    client_reference_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)
    expires_at: int | None = None


def _parse_session(payload: dict[str, Any]) -> CheckoutSession:
    try:
        return CheckoutSession.model_validate(payload)
    except ValidationError as exc:
        detail = exc.errors()[0]
        raise StripeError("server", f"unexpected checkout session shape: {detail}") from exc


def _stripe_message(resp: httpx.Response) -> tuple[str, str]:
    """Return `(kind, human message)` from a Stripe error response."""
    kind = "server" if resp.status_code >= 500 else "rejected"
    try:
        err = resp.json().get("error") or {}
    except ValueError:
        err = {}
    message = str(err.get("message") or f"HTTP {resp.status_code}")
    return kind, message


class StripeClient:
    """Checkout Sessions only: create + retrieve, with bounded retries."""

    def __init__(
        self,
        cfg: Payments,
        secret_key: str,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._cfg = cfg
        self._secret = secret_key
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=cfg.api_base,
            timeout=cfg.timeout_s,
            headers={"Stripe-Version": cfg.api_version},
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # ---------------------------------------------------------------- internals
    async def _call(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        stage: str,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        last: StripeError | None = None
        for attempt in range(self._cfg.retries + 1):
            started = time.monotonic()
            try:
                resp = await self._http.request(
                    method, path, data=data, headers=headers, auth=(self._secret, "")
                )
            except httpx.HTTPError as exc:
                last = StripeError("transport", f"{type(exc).__name__}: {exc}")
                log.warning(
                    "payment.stripe_transport_error",
                    extra={"stage": stage, "attempt": attempt + 1,
                           "duration_ms": round((time.monotonic() - started) * 1000, 1)},
                )
            else:
                duration_ms = round((time.monotonic() - started) * 1000, 1)
                if resp.status_code < 400:
                    try:
                        body = resp.json()
                    except ValueError as exc:
                        raise StripeError("server", "Stripe returned non-JSON") from exc
                    log.info(
                        "payment.stripe_call",
                        extra={"stage": stage, "status": resp.status_code,
                               "duration_ms": duration_ms, "attempt": attempt + 1},
                    )
                    return body
                kind, message = _stripe_message(resp)
                try:
                    err = resp.json().get("error") or {}
                except ValueError:
                    err = {}
                log.warning(
                    "payment.stripe_error",
                    extra={"stage": stage, "status": resp.status_code, "kind": kind,
                           "error_type": err.get("type"), "error_code": err.get("code"),
                           "duration_ms": duration_ms, "attempt": attempt + 1},
                )
                last = StripeError(kind, message)
                if kind == "rejected":
                    raise last
            if attempt < self._cfg.retries:
                await asyncio.sleep(0.5 * (2 ** attempt))
        assert last is not None
        raise last

    # ---------------------------------------------------------------- public API
    async def create_checkout_session(
        self, *, request_id: int, token: str, now: float
    ) -> CheckoutSession:
        cfg = self._cfg
        price = cfg.price_id(self._secret)
        if not price:
            raise StripeError("rejected", "no price configured for this key's mode")
        data = {
            "ui_mode": "embedded_page",
            "mode": "payment",
            "line_items[0][price]": price,
            "line_items[0][quantity]": "1",
            "redirect_on_completion": "never",
            "client_reference_id": token,
            "metadata[request_id]": str(request_id),
            "metadata[token]": token,
            "payment_intent_data[metadata][request_id]": str(request_id),
            "payment_intent_data[description]": "Pilgrim Dot Farm priority song request",
            "expires_at": str(int(now + cfg.checkout_expiry_s)),
            "managed_payments[enabled]": "true" if cfg.managed_payments else "false",
        }
        body = await self._call(
            "POST", "/v1/checkout/sessions", data=data,
            idempotency_key=f"pilgrim-checkout-{request_id}", stage="checkout.create",
        )
        return _parse_session(body)

    async def retrieve_checkout_session(self, session_id: str) -> CheckoutSession:
        body = await self._call(
            "GET", f"/v1/checkout/sessions/{session_id}", stage="checkout.retrieve"
        )
        return _parse_session(body)


# --------------------------------------------------------------------------- #
# Webhook verification — pure function, stdlib only (PAYMENT.md §2.2b)
# --------------------------------------------------------------------------- #
def verify_webhook(
    payload: bytes, sig_header: str, secret: str, now: float, tolerance_s: int
) -> dict[str, Any]:
    """Return the parsed event, or raise WebhookError.

    `now` is injected: this function never reads the wall clock itself.
    """
    if len(payload) > MAX_WEBHOOK_BYTES:
        raise WebhookError("payload too large")
    if not secret:
        raise WebhookError("missing webhook secret")
    if not sig_header:
        raise WebhookError("missing signature header")

    timestamp: int | None = None
    v1: list[str] = []
    for part in sig_header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep or not key:
            raise WebhookError("malformed signature header")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise WebhookError("bad timestamp") from exc
        elif key == "v1":
            v1.append(value)
    if timestamp is None:
        raise WebhookError("missing timestamp")
    if not v1:
        raise WebhookError("missing v1 signature")
    if abs(now - timestamp) > tolerance_s:
        raise WebhookError("stale timestamp")

    signed = f"{timestamp}.".encode() + payload
    expected = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in v1):
        raise WebhookError("bad signature")

    try:
        event = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookError("bad json") from exc
    if not isinstance(event, dict):
        raise WebhookError("event is not an object")
    if not isinstance(event.get("id"), str) or not event["id"]:
        raise WebhookError("event.id missing")
    if not isinstance(event.get("type"), str) or not event["type"]:
        raise WebhookError("event.type missing")
    if not isinstance(event.get("livemode"), bool):
        raise WebhookError("event.livemode missing")
    data = event.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("object"), dict):
        raise WebhookError("event.data.object missing")
    return event
