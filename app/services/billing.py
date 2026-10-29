import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import ProcessedRazorpayEvent, User

RAZORPAY_API = "https://api.razorpay.com/v1"
WEEK = timedelta(days=7)


class BillingService:
    def __init__(self, db: AsyncSession, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self._charged_subscription_credits = 0

    def _ensure_billing(self) -> None:
        if not self.settings.billing_enabled or not self.settings.razorpay_billing_ready:
            raise HTTPException(status_code=503, detail="Paid plans are not available")

    async def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(
                    method,
                    f"{RAZORPAY_API}/{path}",
                    auth=(self.settings.razorpay_key_id, self.settings.razorpay_key_secret),
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.HTTPError as exc:
            raise HTTPException(
                status_code=502, detail="Razorpay could not complete the billing request"
            ) from exc
        if not isinstance(result, dict):
            raise HTTPException(status_code=502, detail="Invalid response from Razorpay")
        return result

    async def create_subscription_checkout(self, user: User) -> dict[str, str]:
        self._ensure_billing()
        if (
            user.subscription_status in {"created", "pending"}
            and user.razorpay_subscription_checkout_url
        ):
            return {"checkout_url": user.razorpay_subscription_checkout_url}
        plan = await self._request(
            "GET", f"plans/{self.settings.razorpay_monthly_plan_id}"
        )
        item = plan.get("item") or {}
        if (
            plan.get("period") != "monthly"
            or plan.get("interval") != 1
            or item.get("amount") != self.settings.razorpay_monthly_price_paise
            or item.get("currency") != "INR"
        ):
            raise HTTPException(
                status_code=503,
                detail="Razorpay monthly plan does not match the displayed price",
            )
        period_end = user.subscription_current_period_end
        if period_end and period_end.tzinfo is None:
            period_end = period_end.replace(tzinfo=timezone.utc)
        if user.subscription_status in {"authenticated", "active", "pending"} or (
            user.subscription_status == "cancelled"
            and period_end is not None
            and period_end > datetime.now(timezone.utc)
        ):
            raise HTTPException(status_code=409, detail="A subscription is already active")
        result = await self._request(
            "POST",
            "subscriptions",
            {
                "plan_id": self.settings.razorpay_monthly_plan_id,
                "total_count": self.settings.subscription_total_cycles,
                "quantity": 1,
                "customer_notify": 1,
                "notes": {"user_id": str(user.id), "product": "lifeos_monthly"},
            },
        )
        subscription_id = result.get("id")
        checkout_url = result.get("short_url")
        if not subscription_id or not checkout_url:
            raise HTTPException(status_code=502, detail="Razorpay returned an incomplete subscription")
        user.razorpay_subscription_id = str(subscription_id)
        user.razorpay_subscription_checkout_url = str(checkout_url)
        user.subscription_status = str(result.get("status") or "created")
        await self.db.commit()
        return {"checkout_url": str(checkout_url)}

    async def create_topup_checkout(self, user: User) -> dict[str, str]:
        self._ensure_billing()
        result = await self._request(
            "POST",
            "payment_links",
            {
                "amount": self.settings.razorpay_topup_amount_paise,
                "currency": "INR",
                "accept_partial": False,
                "description": f"LifeOS {self.settings.razorpay_topup_credits} AI credits",
                "customer": {"email": user.email},
                "notify": {"email": True, "sms": False},
                "reminder_enable": False,
                "notes": {
                    "user_id": str(user.id),
                    "credits": str(self.settings.razorpay_topup_credits),
                    "product": "lifeos_credit_topup",
                },
            },
        )
        checkout_url = result.get("short_url")
        if not checkout_url:
            raise HTTPException(status_code=502, detail="Razorpay returned no payment URL")
        return {"checkout_url": str(checkout_url)}

    @staticmethod
    def options(settings: Settings) -> dict[str, Any]:
        enabled = settings.billing_enabled and settings.razorpay_billing_ready
        return {
            "enabled": enabled,
            "monthly_price_paise": settings.razorpay_monthly_price_paise if enabled else None,
            "weekly_credits": settings.subscription_weekly_credits if enabled else None,
            "topup_amount_paise": settings.razorpay_topup_amount_paise if enabled else None,
            "topup_credits": settings.razorpay_topup_credits if enabled else None,
        }

    async def status(self, user: User) -> dict[str, Any]:
        await self._refresh_subscription_credits(user)
        await self.db.commit()
        return {
            "credit_balance": user.credit_balance,
            "weekly_subscription_credits": user.weekly_subscription_credits,
            "subscription_status": user.subscription_status,
            "subscription_next_credit_at": user.subscription_next_credit_at,
            "subscription_current_period_end": user.subscription_current_period_end,
            "subscription_cancel_at_period_end": user.subscription_cancel_at_period_end,
            "billing_enabled": self.options(self.settings)["enabled"],
        }

    async def cancel_subscription(self, user: User) -> dict[str, str]:
        self._ensure_billing()
        if (
            not user.razorpay_subscription_id
            or user.subscription_status not in {"authenticated", "active", "pending"}
        ):
            raise HTTPException(status_code=409, detail="No active subscription to cancel")
        await self._request(
            "POST",
            f"subscriptions/{user.razorpay_subscription_id}/cancel",
            {"cancel_at_cycle_end": True},
        )
        user.subscription_cancel_at_period_end = True
        await self.db.commit()
        return {"status": "cancellation_scheduled"}

    async def _refresh_subscription_credits(
        self, user: User, now: datetime | None = None
    ) -> None:
        now = now or datetime.now(timezone.utc)
        period_end = user.subscription_current_period_end
        if period_end and period_end.tzinfo is None:
            period_end = period_end.replace(tzinfo=timezone.utc)
        entitled = (
            user.subscription_status
            in {"authenticated", "active", "pending", "cancelled", "halted"}
            and period_end is not None
            and period_end > now
        )
        if not entitled:
            user.credit_balance = max(0, user.credit_balance - user.weekly_subscription_credits)
            user.weekly_subscription_credits = 0
            user.subscription_next_credit_at = None
            return

        next_credit = user.subscription_next_credit_at
        if next_credit and next_credit.tzinfo is None:
            next_credit = next_credit.replace(tzinfo=timezone.utc)
        if user.subscription_status in {"pending", "cancelled", "halted"}:
            if next_credit is None or next_credit <= now:
                user.credit_balance = max(
                    0, user.credit_balance - user.weekly_subscription_credits
                )
                user.weekly_subscription_credits = 0
                user.subscription_next_credit_at = None
            return
        if next_credit is None:
            user.subscription_next_credit_at = now + WEEK
            user.weekly_subscription_credits = self.settings.subscription_weekly_credits
            user.credit_balance += user.weekly_subscription_credits
        elif next_credit <= now and user.subscription_status in {"authenticated", "active"}:
            old_credits = user.weekly_subscription_credits
            user.weekly_subscription_credits = self.settings.subscription_weekly_credits
            user.credit_balance = max(0, user.credit_balance - old_credits)
            user.credit_balance += user.weekly_subscription_credits
            user.subscription_next_credit_at = now + WEEK

    async def handle_webhook(self, payload: bytes, signature: str) -> dict[str, bool]:
        secret = self.settings.razorpay_webhook_secret
        if not secret:
            raise HTTPException(status_code=503, detail="Razorpay webhook is not configured")
        digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, signature):
            raise HTTPException(status_code=400, detail="Invalid Razorpay webhook signature")
        try:
            import json

            event = json.loads(payload)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid webhook payload") from exc
        if not isinstance(event, dict):
            raise HTTPException(status_code=400, detail="Invalid webhook event")

        event_id = event.get("id")
        event_type = event.get("event")
        if not isinstance(event_id, str) or not isinstance(event_type, str):
            raise HTTPException(status_code=400, detail="Invalid webhook event")
        self.db.add(ProcessedRazorpayEvent(id=event_id, event_type=event_type))
        try:
            await self.db.flush()
        except IntegrityError:
            await self.db.rollback()
            existing = await self.db.execute(
                select(ProcessedRazorpayEvent.id).where(
                    ProcessedRazorpayEvent.id == event_id
                )
            )
            if existing.scalar_one_or_none() is not None:
                return {"received": True}
            raise

        if event_type == "payment_link.paid":
            await self._apply_topup(event)
        elif event_type.startswith("subscription."):
            await self._apply_subscription_event(event, event_type)
        await self.db.commit()
        return {"received": True}

    async def _apply_topup(self, event: dict[str, Any]) -> None:
        entity = (
            event.get("payload", {})
            .get("payment_link", {})
            .get("entity", {})
        )
        if entity.get("status") != "paid":
            return
        if (
            entity.get("currency") != "INR"
            or entity.get("amount") != self.settings.razorpay_topup_amount_paise
        ):
            raise HTTPException(status_code=400, detail="Top-up payment does not match configured offer")
        notes = entity.get("notes") or {}
        if notes.get("product") != "lifeos_credit_topup":
            return
        user = await self._user_from_notes(notes)
        try:
            credits = int(notes.get("credits", ""))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid top-up metadata") from exc
        if credits != self.settings.razorpay_topup_credits:
            raise HTTPException(status_code=400, detail="Top-up amount does not match configured offer")
        user.credit_balance += credits

    async def _apply_subscription_event(
        self, event: dict[str, Any], event_type: str
    ) -> None:
        entity = (
            event.get("payload", {})
            .get("subscription", {})
            .get("entity", {})
        )
        subscription_id = entity.get("id")
        notes = entity.get("notes") or {}
        user = None
        if subscription_id:
            result = await self.db.execute(
                select(User)
                .where(User.razorpay_subscription_id == subscription_id)
                .with_for_update()
            )
            user = result.scalar_one_or_none()
        if user is None:
            user = await self._user_from_notes(notes)
            if (
                subscription_id
                and user.razorpay_subscription_id
                and user.razorpay_subscription_id != subscription_id
            ):
                return
            user.razorpay_subscription_id = str(subscription_id) if subscription_id else None

        status = entity.get("status")
        if event_type == "subscription.cancelled":
            status = "cancelled"
        elif event_type == "subscription.halted":
            status = "halted"
        elif event_type in {
            "subscription.authenticated",
            "subscription.activated",
            "subscription.charged",
        }:
            status = "active"
        if status:
            user.subscription_status = str(status)
            if status in {"active", "cancelled", "halted"}:
                user.razorpay_subscription_checkout_url = None
            if status == "cancelled":
                user.subscription_cancel_at_period_end = False
        if "cancel_at_cycle_end" in entity:
            user.subscription_cancel_at_period_end = bool(entity["cancel_at_cycle_end"])
        current_end = entity.get("current_end")
        if current_end:
            user.subscription_current_period_end = datetime.fromtimestamp(
                int(current_end), tz=timezone.utc
            )
        if event_type in {
            "subscription.authenticated",
            "subscription.activated",
            "subscription.charged",
        }:
            await self._refresh_subscription_credits(user)

    async def _user_from_notes(self, notes: dict[str, Any]) -> User:
        try:
            user_id = UUID(str(notes.get("user_id", "")))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Invalid billing metadata") from exc
        result = await self.db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = result.scalar_one_or_none()
        if user is None:
            raise HTTPException(status_code=404, detail="Billing account not found")
        return user
