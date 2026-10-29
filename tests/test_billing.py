import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
import stripe
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models import ProcessedStripeEvent
from app.services.billing import BillingService


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeSession:
    def __init__(self, user):
        self.user = user
        self.processed_events = {}
        self.pending_event = None
        self.last_event_id = None

    def add(self, row):
        self.pending_event = row
        self.last_event_id = row.id

    async def flush(self):
        if self.pending_event.id in self.processed_events:
            raise IntegrityError("duplicate", {}, Exception("unique constraint"))

    async def execute(self, statement):
        if ProcessedStripeEvent.__tablename__ in str(statement):
            return FakeResult(self.processed_events.get(self.last_event_id))
        return FakeResult(self.user)

    async def commit(self):
        if self.pending_event is not None:
            self.processed_events[self.pending_event.id] = self.pending_event.id
            self.pending_event = None

    async def rollback(self):
        self.pending_event = None


def checkout_event(event_id, user_id, credits=25):
    return {
        "id": event_id,
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "payment_status": "paid",
                "metadata": {"user_id": str(user_id), "credits": str(credits)},
            }
        },
    }


def test_stripe_webhook_credits_a_purchase_only_once(monkeypatch):
    user = SimpleNamespace(id=uuid4(), credit_balance=10)
    db = FakeSession(user)
    event = checkout_event("evt_1", user.id)
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda **_: event)
    service = BillingService(db, Settings(stripe_webhook_secret="whsec_test"))

    asyncio.run(service.handle_webhook(b"payload", "signature"))
    asyncio.run(service.handle_webhook(b"payload", "signature"))

    assert user.credit_balance == 35
    assert db.processed_events == {"evt_1": "evt_1"}


def test_stripe_webhook_rejects_invalid_purchase_metadata(monkeypatch):
    event = checkout_event("evt_2", "not-a-uuid")
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda **_: event)
    service = BillingService(FakeSession(None), Settings(stripe_webhook_secret="whsec_test"))

    with pytest.raises(HTTPException) as error:
        asyncio.run(service.handle_webhook(b"payload", "signature"))

    assert error.value.status_code == 400


def test_paid_checkout_is_disabled_by_default():
    service = BillingService(
        FakeSession(None),
        Settings(
            stripe_secret_key="sk_test_configured",
            stripe_price_id_credits="price_configured",
        ),
    )

    with pytest.raises(HTTPException) as error:
        service._ensure_stripe()

    assert error.value.status_code == 503
    assert error.value.detail == "Paid credit purchases are not available"
