from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import stripe

from app.config import Settings
from app.models import ProcessedStripeEvent, User
from app.schemas import CheckoutResponse


class BillingService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        if settings.stripe_secret_key:
            stripe.api_key = settings.stripe_secret_key

    def _ensure_stripe(self) -> None:
        if (
            not self.settings.billing_enabled
            or not self.settings.stripe_secret_key
            or not self.settings.stripe_price_id_credits
        ):
            raise HTTPException(
                status_code=503,
                detail="Paid credit purchases are not available",
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
        except (ValueError, stripe.error.SignatureVerificationError) as exc:
            raise HTTPException(status_code=400, detail=f"Invalid webhook: {exc}") from exc

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            if session.get("payment_status") != "paid":
                return {"received": True}

            meta = session.get("metadata") or {}
            user_id = meta.get("user_id")
            event_id = event.get("id")
            try:
                credits = int(meta.get("credits") or self.settings.credits_per_purchase)
                user_uuid = UUID(user_id)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid checkout metadata") from exc
            if not event_id or credits <= 0:
                raise HTTPException(status_code=400, detail="Invalid checkout event")

            self.db.add(
                ProcessedStripeEvent(id=event_id, event_type=event["type"])
            )
            try:
                await self.db.flush()
            except IntegrityError:
                await self.db.rollback()
                existing = await self.db.execute(
                    select(ProcessedStripeEvent.id).where(
                        ProcessedStripeEvent.id == event_id
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    return {"received": True}
                raise

            result = await self.db.execute(select(User).where(User.id == user_uuid))
            user = result.scalar_one_or_none()
            if user is None:
                await self.db.rollback()
                raise HTTPException(status_code=404, detail="Checkout account not found")
            user.credit_balance += credits
            await self.db.commit()

        return {"received": True}
