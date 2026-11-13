import asyncio
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models import ProcessedRazorpayEvent
from app.schemas import RazorpayPaymentVerification
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
        if ProcessedRazorpayEvent.__tablename__ in str(statement):
            return FakeResult(self.processed_events.get(self.last_event_id))
        return FakeResult(self.user)

    async def commit(self):
        if self.pending_event is not None:
            self.processed_events[self.pending_event.id] = self.pending_event.id
            self.pending_event = None

    async def rollback(self):
        self.pending_event = None


def configured_settings(**overrides):
    values = {
        "billing_enabled": True,
        "razorpay_key_id": "rzp_test_id",
        "razorpay_key_secret": "test_secret",
        "razorpay_webhook_secret": "webhook_secret",
        "razorpay_monthly_plan_id": "plan_test",
        "razorpay_monthly_price_paise": 49900,
        "subscription_weekly_credits": 100,
        "razorpay_topup_amount_paise": 9900,
        "razorpay_topup_credits": 100,
    }
    values.update(overrides)
    return Settings(**values)


def topup_payload(user_id, event_id="evt_topup"):
    return json.dumps(
        {
            "id": event_id,
            "event": "payment_link.paid",
            "payload": {
                "payment_link": {
                    "entity": {
                        "status": "paid",
                        "amount": 9900,
                        "currency": "INR",
                        "notes": {
                            "product": "lifeos_credit_topup",
                            "user_id": str(user_id),
                            "credits": "100",
                        },
                    }
                }
            },
        }
    ).encode()


def sign(payload, secret="webhook_secret"):
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def test_razorpay_webhook_credits_topup_only_once():
    user = SimpleNamespace(id=uuid4(), credit_balance=10)
    db = FakeSession(user)
    payload = topup_payload(user.id)
    service = BillingService(db, configured_settings())

    asyncio.run(service.handle_webhook(payload, sign(payload)))
    asyncio.run(service.handle_webhook(payload, sign(payload)))

    assert user.credit_balance == 110
    assert db.processed_events == {"evt_topup": "evt_topup"}


def test_razorpay_webhook_rejects_bad_signature():
    service = BillingService(FakeSession(None), configured_settings())

    with pytest.raises(HTTPException) as error:
        asyncio.run(service.handle_webhook(b"{}", "invalid"))

    assert error.value.status_code == 400


def test_billing_disabled_without_complete_razorpay_configuration():
    service = BillingService(
        FakeSession(None),
        Settings(razorpay_key_id="rzp_test_id", razorpay_key_secret="test_secret"),
    )

    with pytest.raises(HTTPException) as error:
        service._ensure_billing()

    assert error.value.status_code == 503


def test_checkout_options_enable_topups_without_subscription_configuration():
    settings = Settings(
        billing_enabled=True,
        razorpay_key_id="rzp_test_id",
        razorpay_key_secret="test_secret",
        razorpay_topup_amount_paise=9900,
        razorpay_topup_credits=20,
    )

    options = BillingService.options(settings)

    assert options["topup_enabled"] is True
    assert options["subscription_enabled"] is False
    assert options["enabled"] is True


def test_topup_order_uses_configured_amount_and_user_notes(monkeypatch):
    user = SimpleNamespace(id=uuid4(), email="customer@example.com")
    service = BillingService(FakeSession(user), configured_settings())
    calls = []

    async def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return {"id": "order_test", "amount": 9900, "currency": "INR"}

    monkeypatch.setattr(service, "_request", fake_request)

    result = asyncio.run(service.create_topup_order(user))

    assert result["order_id"] == "order_test"
    assert result["key_id"] == "rzp_test_id"
    method, path, payload = calls[0]
    assert (method, path) == ("POST", "orders")
    assert payload["amount"] == 9900
    assert payload["currency"] == "INR"
    assert payload["receipt"].startswith("lifeos_")
    assert payload["notes"] == {
        "user_id": str(user.id),
        "credits": "100",
        "product": "lifeos_credit_topup",
    }


def test_topup_order_rejects_amount_below_razorpay_minimum():
    user = SimpleNamespace(id=uuid4())
    settings = configured_settings(razorpay_topup_amount_paise=99)
    service = BillingService(FakeSession(user), settings)

    with pytest.raises(HTTPException) as error:
        asyncio.run(service.create_topup_order(user))

    assert error.value.status_code == 400


def test_topup_payment_verification_checks_signature_and_grants_once(monkeypatch):
    user = SimpleNamespace(id=uuid4(), credit_balance=12)
    db = FakeSession(user)
    service = BillingService(db, configured_settings())
    calls = []
    order = {
        "id": "order_test",
        "amount": 9900,
        "currency": "INR",
        "status": "paid",
        "notes": {
            "user_id": str(user.id),
            "credits": "100",
            "product": "lifeos_credit_topup",
        },
    }
    payment = {
        "order_id": "order_test",
        "amount": 9900,
        "currency": "INR",
        "status": "captured",
    }

    async def fake_request(method, path, payload=None):
        calls.append((method, path))
        return order if path == "orders/order_test" else payment

    async def fake_user_from_notes(_notes):
        return user

    monkeypatch.setattr(service, "_request", fake_request)
    monkeypatch.setattr(service, "_user_from_notes", fake_user_from_notes)
    signature = hmac.new(
        b"test_secret",
        b"order_test|pay_test",
        hashlib.sha256,
    ).hexdigest()
    body = RazorpayPaymentVerification(
        razorpay_order_id="order_test",
        razorpay_payment_id="pay_test",
        razorpay_signature=signature,
    )

    first = asyncio.run(service.verify_topup_payment(user, body))
    duplicate = asyncio.run(service.verify_topup_payment(user, body))

    assert first == {"verified": True, "credits_added": 100}
    assert duplicate == {"verified": True, "credits_added": 0}
    assert user.credit_balance == 112
    assert db.processed_events == {
        "checkout_payment:pay_test": "checkout_payment:pay_test"
    }
    assert calls == [
        ("GET", "orders/order_test"),
        ("GET", "payments/pay_test"),
        ("GET", "orders/order_test"),
        ("GET", "payments/pay_test"),
    ]


def test_topup_payment_verification_rejects_missing_fields():
    user = SimpleNamespace(id=uuid4(), credit_balance=0)
    service = BillingService(FakeSession(user), configured_settings())

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            service.verify_topup_payment(
                user,
                RazorpayPaymentVerification(razorpay_order_id="order_test"),
            )
        )

    assert error.value.status_code == 400


def test_topup_payment_verification_rejects_bad_signature(monkeypatch):
    user = SimpleNamespace(id=uuid4(), credit_balance=0)
    service = BillingService(FakeSession(user), configured_settings())

    async def unexpected_request(*_args, **_kwargs):
        raise AssertionError("Razorpay must not be queried for an invalid signature")

    monkeypatch.setattr(service, "_request", unexpected_request)

    with pytest.raises(HTTPException) as error:
        asyncio.run(
            service.verify_topup_payment(
                user,
                RazorpayPaymentVerification(
                    razorpay_order_id="order_test",
                    razorpay_payment_id="pay_test",
                    razorpay_signature="invalid",
                ),
            )
        )

    assert error.value.status_code == 400


def test_subscription_checkout_rejects_mismatched_dashboard_plan(monkeypatch):
    user = SimpleNamespace(
        id=uuid4(),
        email="customer@example.com",
        subscription_status=None,
        subscription_current_period_end=None,
        razorpay_subscription_checkout_url=None,
    )
    service = BillingService(FakeSession(user), configured_settings())

    async def wrong_plan(*_args, **_kwargs):
        return {
            "period": "monthly",
            "interval": 1,
            "item": {"amount": 19900, "currency": "INR"},
        }

    monkeypatch.setattr(service, "_request", wrong_plan)

    with pytest.raises(HTTPException) as error:
        asyncio.run(service.create_subscription_checkout(user))

    assert error.value.status_code == 503
    assert "does not match" in error.value.detail


def test_topup_checkout_uses_configured_amount_and_credit_metadata(monkeypatch):
    user = SimpleNamespace(id=uuid4(), email="customer@example.com")
    service = BillingService(FakeSession(user), configured_settings())
    calls = []

    async def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        return {"short_url": "https://rzp.io/i/test"}

    monkeypatch.setattr(service, "_request", fake_request)

    result = asyncio.run(service.create_topup_checkout(user))

    assert result["checkout_url"] == "https://rzp.io/i/test"
    method, path, payload = calls[0]
    assert (method, path) == ("POST", "payment_links")
    assert payload["amount"] == 9900
    assert payload["currency"] == "INR"
    assert payload["notes"] == {
        "user_id": str(user.id),
        "credits": "100",
        "product": "lifeos_credit_topup",
    }


def test_weekly_subscription_allowance_refreshes_without_rollover():
    now = datetime.now(timezone.utc)
    user = SimpleNamespace(
        credit_balance=35,
        weekly_subscription_credits=25,
        subscription_status="active",
        subscription_current_period_end=now + timedelta(days=10),
        subscription_next_credit_at=now - timedelta(minutes=1),
    )
    service = BillingService(FakeSession(user), configured_settings())

    asyncio.run(service._refresh_subscription_credits(user, now))

    assert user.weekly_subscription_credits == 100
    assert user.credit_balance == 110
    assert user.subscription_next_credit_at == now + timedelta(days=7)


def test_expired_subscription_credits_are_removed_from_balance():
    now = datetime.now(timezone.utc)
    user = SimpleNamespace(
        credit_balance=120,
        weekly_subscription_credits=100,
        subscription_status="cancelled",
        subscription_current_period_end=now - timedelta(seconds=1),
        subscription_next_credit_at=now + timedelta(days=1),
    )
    service = BillingService(FakeSession(user), configured_settings())

    asyncio.run(service._refresh_subscription_credits(user, now))

    assert user.weekly_subscription_credits == 0
    assert user.credit_balance == 20
    assert user.subscription_next_credit_at is None
