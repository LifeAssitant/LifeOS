from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
import stripe

from app.config import Settings
from app.models import User
from app.schemas import CheckoutResponse


class BillingService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        if settings.stripe_secret_key:
            stripe.api_key = settings.stripe_secret_key

    def _ensure_stripe(self) -> None:
        if not self.settings.stripe_secret_key or not self.settings.stripe_price_id_credits:
            raise HTTPException(
                status_code=503,
                detail="Stripe billing is not configured",
            )

    async def create_checkout(self, user: User) -> CheckoutResponse:
        self._ensure_stripe()
        session = stripe.checkout.Session.create(
            mode="payment",
            line_items=[{"price": self.settings.stripe_price_id_credits, "quantity": 1}],
            success_url=self.settings.stripe_success_url,
            cancel_url=self.settings.stripe_cancel_url,
            customer_email=user.email,
            metadata={
                "user_id": str(user.id),
                "credits": str(self.settings.credits_per_purchase),
            },
        )
        if not session.url:
            raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
        return CheckoutResponse(checkout_url=session.url)

    async def handle_webhook(self, payload: bytes, signature: str) -> dict:
        if not self.settings.stripe_webhook_secret:
            raise HTTPException(status_code=503, detail="Stripe webhook not configured")
        try:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=signature,
                secret=self.settings.stripe_webhook_secret,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid webhook: {exc}") from exc

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            meta = session.get("metadata") or {}
            user_id = meta.get("user_id")
            credits = int(meta.get("credits") or self.settings.credits_per_purchase)
            if user_id:
                from uuid import UUID

                from sqlalchemy import select

                from app.models import User as UserModel

                result = await self.db.execute(
                    select(UserModel).where(UserModel.id == UUID(user_id))
                )
                user = result.scalar_one_or_none()
                if user:
                    user.credit_balance += credits
                    await self.db.commit()

        return {"received": True}
