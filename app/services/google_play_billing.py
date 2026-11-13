import hashlib
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import HTTPException
from jose import jwt
from jose.exceptions import JOSEError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ProcessedGooglePlayPurchase, User
from app.schemas import GooglePlayPurchaseVerification

OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
ANDROID_PUBLISHER_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
ANDROID_PUBLISHER_API = (
    "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
)


class GooglePlayBillingService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings

    @staticmethod
    def obfuscated_account_id(user: User) -> str:
        return hashlib.sha256(str(user.id).encode()).hexdigest()

    @staticmethod
    def options(settings: Settings, user: User) -> dict[str, Any]:
        enabled = settings.google_play_billing_ready
        return {
            "enabled": enabled,
            "product_id": settings.google_play_topup_product_id if enabled else None,
            "credits": settings.google_play_topup_credits if enabled else None,
            "obfuscated_account_id": (
                GooglePlayBillingService.obfuscated_account_id(user) if enabled else None
            ),
        }

    async def verify_topup(
        self, user: User, body: GooglePlayPurchaseVerification
    ) -> dict[str, Any]:
        if not self.settings.google_play_billing_ready:
            raise HTTPException(status_code=503, detail="Google Play top-ups are not available")
        if body.product_id != self.settings.google_play_topup_product_id:
            raise HTTPException(status_code=400, detail="Google Play product does not match this offer")

        token_hash = hashlib.sha256(body.purchase_token.encode()).hexdigest()
        locked_user_result = await self.db.execute(
            select(User).where(User.id == user.id).with_for_update()
        )
        billing_user = locked_user_result.scalar_one_or_none()
        if billing_user is None:
            raise HTTPException(status_code=404, detail="Billing account not found")

        existing_result = await self.db.execute(
            select(ProcessedGooglePlayPurchase)
            .where(ProcessedGooglePlayPurchase.token_hash == token_hash)
            .with_for_update()
        )
        existing = existing_result.scalar_one_or_none()
        if existing is not None:
            if existing.user_id != billing_user.id:
                raise HTTPException(status_code=409, detail="Google Play purchase was already claimed")
            return {"verified": True, "credits_added": 0}

        access_token = await self._access_token()
        purchase = await self._publisher_purchase(
            access_token, body.product_id, body.purchase_token
        )
        account_id = self.obfuscated_account_id(billing_user)
        if (
            purchase.get("productId") != body.product_id
            or purchase.get("purchaseToken") not in (None, body.purchase_token)
            or purchase.get("obfuscatedExternalAccountId") != account_id
        ):
            raise HTTPException(
                status_code=400,
                detail="Google Play purchase does not match this account or product",
            )
        if purchase.get("purchaseState") != 0:
            raise HTTPException(status_code=409, detail="Google Play purchase is not complete")
        if purchase.get("quantity", 1) != 1:
            raise HTTPException(status_code=400, detail="Google Play purchase quantity is invalid")

        row = ProcessedGooglePlayPurchase(
            token_hash=token_hash,
            user_id=billing_user.id,
            product_id=body.product_id,
            order_id=purchase.get("orderId"),
            credits_added=self.settings.google_play_topup_credits,
        )
        self.db.add(row)
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            existing_result = await self.db.execute(
                select(ProcessedGooglePlayPurchase)
                .where(ProcessedGooglePlayPurchase.token_hash == token_hash)
            )
            existing = existing_result.scalar_one_or_none()
            if existing is not None and existing.user_id == user.id:
                return {"verified": True, "credits_added": 0}
            raise

        billing_user.credit_balance += self.settings.google_play_topup_credits
        await self.db.commit()
        return {
            "verified": True,
            "credits_added": self.settings.google_play_topup_credits,
        }

    async def _access_token(self) -> str:
        issued_at = int(time.time())
        private_key = self.settings.google_play_service_account_private_key.replace(
            "\\n", "\n"
        )
        try:
            assertion = jwt.encode(
                {
                    "iss": self.settings.google_play_service_account_email,
                    "scope": ANDROID_PUBLISHER_SCOPE,
                    "aud": OAUTH_TOKEN_URL,
                    "iat": issued_at,
                    "exp": issued_at + 3600,
                },
                private_key,
                algorithm="RS256",
            )
        except (JOSEError, TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=503, detail="Google Play service-account credentials are invalid"
            ) from exc

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    OAUTH_TOKEN_URL,
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
                response.raise_for_status()
                result = response.json()
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=503, detail="Google Play service account was rejected"
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail="Could not reach Google Play billing"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=502, detail="Invalid response from Google Play authorization"
            ) from exc
        if not isinstance(result, dict) or not isinstance(result.get("access_token"), str):
            raise HTTPException(
                status_code=502, detail="Invalid response from Google Play authorization"
            )
        return result["access_token"]

    async def _publisher_purchase(
        self, access_token: str, product_id: str, purchase_token: str
    ) -> dict[str, Any]:
        package = quote(self.settings.google_play_package_name, safe="")
        product = quote(product_id, safe="")
        token = quote(purchase_token, safe="")
        url = (
            f"{ANDROID_PUBLISHER_API}/{package}/purchases/products/"
            f"{product}/tokens/{token}"
        )
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(
                    url,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                response.raise_for_status()
                result = response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise HTTPException(
                    status_code=400, detail="Google Play purchase token is invalid"
                ) from exc
            if exc.response.status_code in {401, 403}:
                raise HTTPException(
                    status_code=503, detail="Google Play Publisher API access was rejected"
                ) from exc
            raise HTTPException(
                status_code=502, detail="Google Play could not verify the purchase"
            ) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail="Could not reach Google Play billing"
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=502, detail="Invalid response from Google Play billing"
            ) from exc
        if not isinstance(result, dict):
            raise HTTPException(
                status_code=502, detail="Invalid response from Google Play billing"
            )
        return result
