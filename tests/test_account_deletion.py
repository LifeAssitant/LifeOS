import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from cryptography.fernet import Fernet

from app.config import Settings
from app.core.encryption import SecretBox
from app.services.lifecycle import UserService


class FakeSession:
    def __init__(self):
        self.deleted = None
        self.committed = False

    async def delete(self, user):
        self.deleted = user

    async def commit(self):
        self.committed = True


class FakeHttpResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class FakeHttpClient:
    response = FakeHttpResponse(204)
    expected_user_id = ""

    def __init__(self, timeout):
        assert timeout == 10

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def delete(self, url, headers):
        assert url.endswith(f"/auth/v1/admin/users/{self.expected_user_id}")
        assert headers["apikey"] == "service-role-secret"
        assert headers["Authorization"] == "Bearer service-role-secret"
        return self.response


def service(db):
    return UserService(db, SecretBox(Fernet.generate_key().decode()))


def test_delete_account_removes_database_user(monkeypatch):
    db = FakeSession()
    user = SimpleNamespace(supabase_user_id=None)
    monkeypatch.setattr("app.services.lifecycle.httpx.AsyncClient", FakeHttpClient)

    asyncio.run(service(db).delete_account(user, Settings()))

    assert db.deleted is user
    assert db.committed


def test_supabase_account_deletion_requires_service_role_key():
    db = FakeSession()
    user = SimpleNamespace(supabase_user_id="supabase-user-id")
    settings = Settings(supabase_url="https://project.supabase.co")

    with pytest.raises(HTTPException) as error:
        asyncio.run(service(db).delete_account(user, settings))

    assert error.value.status_code == 503
    assert db.deleted is None
    assert not db.committed


def test_supabase_account_is_deleted_before_local_data(monkeypatch):
    user_id = str(uuid4())
    FakeHttpClient.expected_user_id = user_id
    db = FakeSession()
    user = SimpleNamespace(supabase_user_id=user_id)
    settings = Settings(
        supabase_url="https://project.supabase.co",
        supabase_service_role_key="service-role-secret",
    )
    FakeHttpClient.response = FakeHttpResponse(204)
    monkeypatch.setattr("app.services.lifecycle.httpx.AsyncClient", FakeHttpClient)

    asyncio.run(service(db).delete_account(user, settings))

    assert db.deleted is user
    assert db.committed


def test_failed_supabase_deletion_preserves_local_data(monkeypatch):
    user_id = str(uuid4())
    FakeHttpClient.expected_user_id = user_id
    db = FakeSession()
    user = SimpleNamespace(supabase_user_id=user_id)
    settings = Settings(
        supabase_url="https://project.supabase.co",
        supabase_service_role_key="service-role-secret",
    )
    FakeHttpClient.response = FakeHttpResponse(403)
    monkeypatch.setattr("app.services.lifecycle.httpx.AsyncClient", FakeHttpClient)

    with pytest.raises(HTTPException) as error:
        asyncio.run(service(db).delete_account(user, settings))

    assert error.value.status_code == 502
    assert db.deleted is None
    assert not db.committed


def test_supabase_network_failure_preserves_local_data(monkeypatch):
    db = FakeSession()
    user = SimpleNamespace(supabase_user_id="supabase-user-id")
    settings = Settings(
        supabase_url="https://project.supabase.co",
        supabase_service_role_key="service-role-secret",
    )

    class NetworkFailureClient(FakeHttpClient):
        async def delete(self, *_args, **_kwargs):
            raise httpx.ConnectError("unavailable")

    monkeypatch.setattr(
        "app.services.lifecycle.httpx.AsyncClient", NetworkFailureClient
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(service(db).delete_account(user, settings))

    assert error.value.status_code == 502
    assert db.deleted is None
    assert not db.committed
