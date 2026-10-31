"""Fake Stripe for tests (PAYMENT.md §2.3). Never touches the network.

Same surface as `pilgrim.payments.StripeClient` for the calls the app makes,
plus `pay()` / `expire()` which return a *correctly signed* webhook event so
server tests exercise the real `verify_webhook` verifier.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any

from pilgrim.config import Payments
from pilgrim.payments import CheckoutSession, StripeError


def sign_event(event: dict[str, Any], secret: str, now: float) -> tuple[bytes, str]:
    """Sign an event exactly as Stripe does: HMAC-SHA256 over `<t>.<raw body>`."""
    payload = json.dumps(event, separators=(",", ":"), sort_keys=True).encode()
    timestamp = int(now)
    signature = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return payload, f"t={timestamp},v1={signature}"


class FakeStripe:
    """In-memory Checkout Sessions with failure injection."""

    def __init__(
        self,
        cfg: Payments | None = None,
        secret_key: str = "sk_test_fake",
        webhook_secret: str = "whsec_fake",
        now: float = 1_800_000_000.0,
    ) -> None:
        self.cfg = cfg or Payments()
        self.secret_key = secret_key
        self.webhook_secret = webhook_secret
        self.now = now
        self.livemode = self.cfg.mode(secret_key) == "live"
        self.sessions: dict[str, CheckoutSession] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.retrieve_calls: list[str] = []
        self.fail_next_create: str | None = None
        self.fail_next_retrieve: str | None = None
        self.delay_s: float = 0.0
        self._seq = 0

    # ------------------------------------------------------------- StripeClient
    async def create_checkout_session(
        self, *, request_id: int, token: str, now: float
    ) -> CheckoutSession:
        self.create_calls.append({"request_id": request_id, "token": token, "now": now})
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_next_create:
            kind, self.fail_next_create = self.fail_next_create, None
            raise StripeError(kind, f"injected {kind} failure")
        self._seq += 1
        session_id = f"cs_{'live' if self.livemode else 'test'}_fake_{self._seq}"
        session = CheckoutSession(
            id=session_id,
            status="open",
            payment_status="unpaid",
            livemode=self.livemode,
            client_secret=f"{session_id}_secret_{self._seq}",
            amount_total=self.cfg.min_cents,
            currency=self.cfg.currency,
            client_reference_id=token,
            metadata={"request_id": str(request_id), "token": token},
            expires_at=int(now + self.cfg.checkout_expiry_s),
        )
        self.sessions[session_id] = session
        return session

    async def retrieve_checkout_session(self, session_id: str) -> CheckoutSession:
        self.retrieve_calls.append(session_id)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_next_retrieve:
            kind, self.fail_next_retrieve = self.fail_next_retrieve, None
            raise StripeError(kind, f"injected {kind} failure")
        session = self.sessions.get(session_id)
        if session is None:
            raise StripeError("rejected", f"no such session {session_id}")
        return session

    async def aclose(self) -> None:
        return None

    # -------------------------------------------------------------- test helpers
    def pay(self, session_id: str, amount_cents: int | None = None) -> tuple[bytes, str]:
        """Mark a session paid and return the signed `checkout.session.completed`."""
        self._seq += 1
        session = self.sessions[session_id].model_copy(
            update={
                "status": "complete",
                "payment_status": "paid",
                "amount_total": amount_cents or self.sessions[session_id].amount_total,
                "payment_intent": f"pi_fake_{self._seq}",
            }
        )
        self.sessions[session_id] = session
        return self.event("checkout.session.completed", session)

    def expire(self, session_id: str) -> tuple[bytes, str]:
        session = self.sessions[session_id].model_copy(update={"status": "expired"})
        self.sessions[session_id] = session
        return self.event("checkout.session.expired", session)

    def async_failed(self, session_id: str) -> tuple[bytes, str]:
        session = self.sessions[session_id].model_copy(
            update={"status": "complete", "payment_status": "unpaid"}
        )
        self.sessions[session_id] = session
        return self.event("checkout.session.async_payment_failed", session)

    def event(
        self, type_: str, session: CheckoutSession, event_id: str | None = None
    ) -> tuple[bytes, str]:
        self._seq += 1
        event = {
            "id": event_id or f"evt_fake_{self._seq}",
            "object": "event",
            "type": type_,
            "livemode": session.livemode,
            "data": {"object": session.model_dump()},
        }
        return sign_event(event, self.webhook_secret, self.now)
