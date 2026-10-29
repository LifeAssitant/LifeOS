import pytest
from pydantic import ValidationError

from app.schemas import GoogleAuthRequest, LoginRequest, RegisterRequest


@pytest.mark.parametrize(
    ("request_model", "payload"),
    [
        (RegisterRequest, {"email": "person@example.com", "password": "long-password"}),
        (LoginRequest, {"email": "person@example.com", "password": "long-password"}),
        (GoogleAuthRequest, {"access_token": "a" * 24}),
    ],
)
def test_auth_requires_adult_confirmation(request_model, payload):
    with pytest.raises(ValidationError):
        request_model(**payload, confirm_adult=False)

    request_model(**payload, confirm_adult=True)
