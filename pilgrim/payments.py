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
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pilgrim.config import Payments
from pilgrim.store import Store

log = logging.getLogger("pilgrim.payments")

MAX_WEBHOOK_BYTES = 256 * 1024


class StripeError(RuntimeError):
    """A failed Stripe call: `kind` is `transport`, `rejected` (4xx) or `server`."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


class CheckoutClient(Protocol):
    """Structural type shared by StripeClient and the test fake."""

    async def create_checkout_session(
        self, *, request_id: int, token: str, now: float
    ) -> CheckoutSession: ...

    async def retrieve_checkout_session(self, session_id: str) -> CheckoutSession: ...


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


# --------------------------------------------------------------------------- #
# PaymentService — the only code that changes payment columns (PAYMENT.md §2.2c)
# --------------------------------------------------------------------------- #
def _epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


class PaymentService:
    """State transitions for paid priority requests.

    `apply_paid_session` is the single place a request becomes paid; both the
    webhook and the reconcile path call it. It never trusts the browser.
    """

    def __init__(
        self,
        cfg: Payments,
        store: Store,
        client: CheckoutClient,
        secret_key: str,
        publishable_key: str,
        webhook_secret: str,
        now: Callable[[], float],
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.client = client
        self.secret_key = secret_key
        self.publishable_key = publishable_key
        self.webhook_secret = webhook_secret
        self.now = now
        self.mode = cfg.mode(secret_key)
        self.livemode = self.mode == "live"

    # ------------------------------------------------------------------ checkout
    async def start_checkout(self, row: dict[str, Any]) -> CheckoutSession:
        session = await self.client.create_checkout_session(
            request_id=int(row["id"]), token=str(row["public_token"]), now=self.now()
        )
        self.store.attach_checkout(int(row["id"]), session.id)
        log.info("payment.checkout_created",
                 extra={"request_id": row["id"], "session_id": session.id})
        return session

    # --------------------------------------------------------------------- paid
    def apply_paid_session(self, session: CheckoutSession) -> bool:
        cfg = self.cfg

        def mismatch(check: str) -> bool:
            log.warning("payment.mismatch",
                        extra={"session_id": session.id, "check": check})
            return False

        if session.payment_status != "paid":
            return False
        if session.livemode != self.livemode:
            return mismatch("livemode")
        if (session.currency or "").lower() != cfg.currency:
            return mismatch("currency")
        amount = session.amount_total
        if amount is None or not (cfg.min_cents <= amount <= cfg.max_cents):
            return mismatch("amount")
        row = self.store.get_request_by_session(session.id)
        if row is None:
            return mismatch("unknown_session")
        raw_id = session.metadata.get("request_id")
        if not raw_id or not str(raw_id).isdigit():
            return mismatch("metadata_request_id")
        if int(raw_id) != int(row["id"]):
            return mismatch("request_id")
        if session.client_reference_id != row["public_token"]:
            return mismatch("client_reference_id")
        applied = self.store.mark_request_paid(
            int(row["id"]),
            session_id=session.id,
            payment_intent_id=session.payment_intent,
            amount_cents=amount,
            currency=session.currency or cfg.currency,
            livemode=session.livemode,
        )
        if applied:
            log.info("payment.applied",
                     extra={"request_id": row["id"], "session_id": session.id,
                            "amount_cents": amount})
        else:
            log.info("payment.already_paid",
                     extra={"request_id": row["id"], "session_id": session.id})
        return applied

    # ------------------------------------------------------------------ webhook
    def handle_event(self, event: dict[str, Any]) -> str:
        event_id = str(event["id"])
        type_ = str(event["type"])
        obj = event["data"]["object"]
        session_id = obj.get("id") if isinstance(obj.get("id"), str) else None

        if event["livemode"] != self.livemode:
            self.store.record_stripe_event(event_id, type_, session_id, "ignored")
            log.warning("payment.webhook_livemode_mismatch",
                        extra={"event_id": event_id, "type": type_})
            return "ignored"

        if not self.store.record_stripe_event(event_id, type_, session_id, "processing"):
            log.info("payment.webhook_duplicate", extra={"event_id": event_id})
            return "duplicate"

        outcome = "ignored"
        try:
            if type_ in ("checkout.session.completed",
                         "checkout.session.async_payment_succeeded"):
                session = CheckoutSession.model_validate(obj)
                if session.payment_status == "paid":
                    outcome = "applied" if self.apply_paid_session(session) else "mismatch"
                else:
                    log.info("payment.webhook_unpaid",
                             extra={"session_id": session_id, "type": type_})
            elif type_ in ("checkout.session.async_payment_failed",
                           "checkout.session.expired"):
                row = self.store.get_request_by_session(session_id) if session_id else None
                if row is not None and self.store.mark_request_abandoned(int(row["id"])):
                    outcome = "applied"
            elif type_ == "charge.refunded":
                log.info("payment.refunded",
                         extra={"payment_intent": obj.get("payment_intent")})
            elif type_ == "charge.dispute.created":
                log.info("payment.disputed",
                         extra={"payment_intent": obj.get("payment_intent")})
        except (ValueError, TypeError) as exc:
            outcome = "mismatch"
            log.warning("payment.webhook_bad_object",
                        extra={"event_id": event_id, "error": str(exc)})
        self.store.update_stripe_event_outcome(event_id, outcome)
        return outcome

    # ------------------------------------------------------------------ reconcile
    async def reconcile(self, token: str) -> dict[str, Any] | None:
        row = self.store.get_request_by_token(token)
        if row is None:
            return None
        if row["status"] != "awaiting_payment" or not row.get("checkout_session_id"):
            return row
        now = self.now()
        created = _epoch(row.get("created_at"))
        if created is None or now - created < self.cfg.reconcile_after_s:
            return row
        last = float(row.get("reconciled_at") or 0.0)
        if now - last < self.cfg.reconcile_min_interval_s:
            return row
        self.store.touch_reconciled(int(row["id"]), now)
        try:
            session = await self.client.retrieve_checkout_session(
                str(row["checkout_session_id"]))
        except StripeError as exc:
            log.warning("payment.reconcile_failed",
                        extra={"request_id": row["id"], "kind": exc.kind})
            return self.store.get_request_by_token(token)
        self.apply_paid_session(session)
        return self.store.get_request_by_token(token)
