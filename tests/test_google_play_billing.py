import asyncio
import hashlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.models import ProcessedGooglePlayPurchase
from app.schemas import GooglePlayPurchaseVerification
from app.services.google_play_billing import (
    ANDROID_PUBLISHER_API,
    ANDROID_PUBLISHER_SCOPE,
    OAUTH_TOKEN_URL,
    GooglePlayBillingService,
)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class FakeGooglePlaySession:
    def __init__(self, user, token_hash):
        self.user = user
        self.token_hash = token_hash
        self.purchases = {}
        self.pending_purchase = None

    def add(self, purchase):
        self.pending_purchase = purchase

    async def execute(self, statement):
        if ProcessedGooglePlayPurchase.__tablename__ in str(statement):
            return FakeResult(self.purchases.get(self.token_hash))
        return FakeResult(self.user)

    async def flush(self):
        if self.pending_purchase.token_hash in self.purchases:
            raise IntegrityError("duplicate", {}, Exception("unique constraint"))

    async def commit(self):
        if self.pending_purchase is not None:
            self.purchases[self.pending_purchase.token_hash] = self.pending_purchase
            self.pending_purchase = None

    async def rollback(self):
        self.pending_purchase = None


def configured_settings(**overrides):
    values = {
        "google_play_billing_enabled": True,
        "google_play_package_name": "com.lifeos.app",
        "google_play_topup_product_id": "lifeos_credit_topup_20",
        "google_play_topup_credits": 20,
        "google_play_service_account_email": "lifeos@example.iam.gserviceaccount.com",
        "google_play_service_account_private_key": "test-key",
    }
    values.update(overrides)
    return Settings(**values)


def google_purchase(service, user, **overrides):
    purchase = {
        "productId": "lifeos_credit_topup_20",
        "purchaseToken": "google-purchase-token",
        "purchaseState": 0,
        "quantity": 1,
        "orderId": "GPA.test-order",
        "obfuscatedExternalAccountId": service.obfuscated_account_id(user),
    }
    purchase.update(overrides)
    return purchase


def test_google_play_options_are_account_bound():
    user = SimpleNamespace(id=uuid4())

    options = GooglePlayBillingService.options(configured_settings(), user)

    assert options == {
        "enabled": True,
        "product_id": "lifeos_credit_topup_20",
        "credits": 20,
        "obfuscated_account_id": GooglePlayBillingService.obfuscated_account_id(user),
    }


def test_google_play_options_hide_unavailable_offer():
    user = SimpleNamespace(id=uuid4())

    options = GooglePlayBillingService.options(Settings(), user)

    assert options == {
        "enabled": False,
        "product_id": None,
        "credits": None,
        "obfuscated_account_id": None,
    }


def test_google_play_purchase_is_verified_and_credited_once(monkeypatch):
    user = SimpleNamespace(id=uuid4(), credit_balance=3)
    token_hash = hashlib.sha256(b"google-purchase-token").hexdigest()
    db = FakeGooglePlaySession(user, token_hash)
    service = GooglePlayBillingService(db, configured_settings())
    publisher_calls = []

    async def fake_access_token():
        return "google-access-token"

    async def fake_purchase(access_token, product_id, purchase_token):
        publisher_calls.append((access_token, product_id, purchase_token))
        return google_purchase(service, user)

    monkeypatch.setattr(service, "_access_token", fake_access_token)
    monkeypatch.setattr(service, "_publisher_purchase", fake_purchase)
    body = GooglePlayPurchaseVerification(
        product_id="lifeos_credit_topup_20",
        purchase_token="google-purchase-token",
    )

    first = asyncio.run(service.verify_topup(user, body))
    duplicate = asyncio.run(service.verify_topup(user, body))

    assert first == {"verified": True, "credits_added": 20}
    assert duplicate == {"verified": True, "credits_added": 0}
    assert user.credit_balance == 23
    assert list(db.purchases) == [token_hash]
    assert publisher_calls == [
        ("google-access-token", "lifeos_credit_topup_20", "google-purchase-token")
    ]


def test_google_play_purchase_rejects_another_accounts_token(monkeypatch):
    user = SimpleNamespace(id=uuid4(), credit_balance=3)
    db = FakeGooglePlaySession(user, "b" * 64)
    service = GooglePlayBillingService(db, configured_settings())

    async def fake_access_token():
        return "google-access-token"

    async def fake_purchase(_access_token, _product_id, _purchase_token):
        return google_purchase(
            service, SimpleNamespace(id=uuid4())
        )

    monkeypatch.setattr(service, "_access_token", fake_access_token)
    monkeypatch.setattr(service, "_publisher_purchase", fake_purchase)
    body = GooglePlayPurchaseVerification(
        product_id="lifeos_credit_topup_20",
        purchase_token="google-purchase-token",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(service.verify_topup(user, body))

    assert error.value.status_code == 400
    assert user.credit_balance == 3
    assert not db.purchases


def test_google_play_purchase_rejects_pending_purchase(monkeypatch):
    user = SimpleNamespace(id=uuid4(), credit_balance=3)
    db = FakeGooglePlaySession(user, "b" * 64)
    service = GooglePlayBillingService(db, configured_settings())

    async def fake_access_token():
        return "google-access-token"

    async def fake_purchase(_access_token, _product_id, _purchase_token):
        return google_purchase(service, user, purchaseState=2)

    monkeypatch.setattr(service, "_access_token", fake_access_token)
    monkeypatch.setattr(service, "_publisher_purchase", fake_purchase)
    body = GooglePlayPurchaseVerification(
        product_id="lifeos_credit_topup_20",
        purchase_token="google-purchase-token",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(service.verify_topup(user, body))

    assert error.value.status_code == 409
    assert user.credit_balance == 3
    assert not db.purchases


def test_google_play_purchase_rejects_unconfigured_product():
    user = SimpleNamespace(id=uuid4(), credit_balance=3)
    service = GooglePlayBillingService(
        FakeGooglePlaySession(user, "b" * 64), configured_settings()
    )
    body = GooglePlayPurchaseVerification(
        product_id="different_product",
        purchase_token="google-purchase-token",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(service.verify_topup(user, body))

    assert error.value.status_code == 400
    assert user.credit_balance == 3


def test_google_play_service_account_jwt_requests_publisher_access(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    settings = configured_settings(
        google_play_service_account_private_key=private_key_pem
    )
    service = GooglePlayBillingService(FakeGooglePlaySession(None, ""), settings)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "publisher-access-token"}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, data):
            assert url == OAUTH_TOKEN_URL
            assert data["grant_type"] == (
                "urn:ietf:params:oauth:grant-type:jwt-bearer"
            )
            claims = jwt.decode(
                data["assertion"],
                public_key_pem,
                algorithms=["RS256"],
                audience=OAUTH_TOKEN_URL,
            )
            assert claims["iss"] == settings.google_play_service_account_email
            assert claims["scope"] == ANDROID_PUBLISHER_SCOPE
            return FakeResponse()

    monkeypatch.setattr(
        "app.services.google_play_billing.httpx.AsyncClient", FakeClient
    )

    assert asyncio.run(service._access_token()) == "publisher-access-token"


def test_google_play_purchase_lookup_escapes_tokens_and_uses_bearer_auth(monkeypatch):
    settings = configured_settings()
    service = GooglePlayBillingService(FakeGooglePlaySession(None, ""), settings)
    expected_url = (
        f"{ANDROID_PUBLISHER_API}/com.lifeos.app/purchases/products/"
        "lifeos_credit_topup_20/tokens/token%2Fwith%20spaces"
    )

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"purchaseState": 0}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers):
            assert url == expected_url
            assert headers == {"Authorization": "Bearer google-access-token"}
            return FakeResponse()

    monkeypatch.setattr(
        "app.services.google_play_billing.httpx.AsyncClient", FakeClient
    )

    result = asyncio.run(
        service._publisher_purchase(
            "google-access-token",
            "lifeos_credit_topup_20",
            "token/with spaces",
        )
    )

    assert result == {"purchaseState": 0}
